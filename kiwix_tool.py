"""
title: Kiwix offline library
author: Edgar Barrantes
author_url: https://github.com/EdgarBarrantes/open-webui-kiwix
description: Search and read your own offline Kiwix library (Wikipedia, Stack Exchange, iFixit, Project Gutenberg, MedlinePlus... as ZIM files, served by kiwix-serve), with links to every source.
version: 0.1.0
license: MIT
"""

# Needs only requests and pydantic, which Open WebUI already has.
# Talks to kiwix-serve's HTTP API: /catalog/v2/entries (the books), /suggest
# (title matches, per book), /search (full text, several books of one language
# at once) and /content (the articles). Nothing leaves your network.

import asyncio
import html
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import requests
from pydantic import BaseModel, Field

ATOM = {"a": "http://www.w3.org/2005/Atom"}

# Dropped from English full-text queries: kiwix-serve's search (Xapian) ranks
# "how to treat a burn" by "how", "to" and "a" too, and finds "treated wood".
STOP = set(
    """a about after all also am an and any are as at be been before but by can could did do does doing for from
    get got had has have how i if in into is it its me my of on or our should so some than that the their them
    then there these they this to was we were what when where which while who whom why will with would you your""".split()
)

# Parts of a page that are not the article: menus, reference markers, navigation boxes.
SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg", "table", "figure", "button", "form"}
SKIP_CLASSES = {"reference", "mw-editsection", "navbox", "noprint", "metadata", "mw-jump-link", "toc",
                "mw-references-wrap", "usa-banner", "breadcrumbs", "page-toc"}
# Where a page keeps its article, when it says so: only that part is kept.
MAIN_IDS = {"content", "main-content", "mw-content-text", "mplus-content", "mainContent"}
# Blank line around these, a line break around the others.
PARAGRAPHS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "section", "article"}
LINES = {"br", "li", "div", "dd", "dt", "tr", "ul", "ol", "dl"}
PARA = "\x00"
# Lists of other pages (Stack Exchange's tag pages) are left out of search results.
LISTING = re.compile(r"/questions/tagged/|/tags/|/users/")
# openZIM's LibreTexts ZIMs (mindtouch scraper) are an app: index/page_<id> only
# redirects, the text is in content/page_content_<id>.json.
MINDTOUCH = re.compile(r"^([^/]+)/index/page_(\d+)$")


def _is_comments(css_class: str) -> bool:
    """User comments are not the article either (iFixit has hundreds under each step)."""
    return "comment" in css_class and not css_class.startswith(("has-", "no-"))


class _Text(HTMLParser):
    """Plain text of an HTML page: its main part when it marks one, without menus, tables and reference markers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.main, self.title, self._in_title = [], [], "", False
        self._skip_tag, self._skip_depth = None, 0
        self._main_tag, self._main_depth = None, 0

    def _add(self, s):
        self.out.append(s)
        if self._main_tag:
            self.main.append(s)

    def handle_starttag(self, tag, attrs):
        if self._skip_tag:
            self._skip_depth += tag == self._skip_tag
            return
        a = dict(attrs)
        if self._main_tag:
            self._main_depth += tag == self._main_tag
        elif not self.main and (tag == "main" or a.get("role") == "main" or a.get("id") in MAIN_IDS):
            self._main_tag, self._main_depth = tag, 1
        classes = (a.get("class") or "").split()
        if tag in SKIP_TAGS or SKIP_CLASSES.intersection(classes) or any(_is_comments(c) for c in classes):
            self._skip_tag, self._skip_depth = tag, 1
        elif tag == "title":
            self._in_title = True
        elif tag in PARAGRAPHS:
            self._add(PARA + ("#" * int(tag[1]) + " " if tag in ("h1", "h2", "h3", "h4") else ""))
        elif tag in LINES:
            self._add("\n- " if tag == "li" else "\n")

    def handle_startendtag(self, tag, attrs):
        if not self._skip_tag and tag == "br":
            self._add("\n")

    def handle_endtag(self, tag):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
        elif tag in PARAGRAPHS:
            self._add(PARA)
        elif tag in LINES:
            self._add("\n")
        if self._main_tag and tag == self._main_tag:
            self._main_depth -= 1
            if not self._main_depth:
                self._main_tag = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_tag:
            self._add(data)

    def text(self) -> str:
        main = "".join(self.main)
        t = main if len(main.strip()) > 200 else "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r" *\n[ \n]*", "\n", t)  # line breaks
        t = re.sub(r"\s*\x00[\s\x00]*", "\n\n", t)  # paragraph breaks
        t = re.sub(r"^(-|#+) *\n+ *", r"\1 ", t, flags=re.M)  # "-\nSummary" -> "- Summary"
        t = re.sub(r"^- *$\n?", "", t, flags=re.M)
        return t.strip()


def _parts(text: str, size: int) -> list:
    """Split text into parts of about `size` characters, at paragraph or line breaks where possible."""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", size // 2, size)
        if cut < 0:
            cut = text.rfind(" ", size // 2, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut].strip())
        text = text[cut:]
    return parts + [text.strip()]


def _keywords(query: str) -> str:
    words = re.findall(r"\w[\w'-]*", query)
    kept = [w for w in words if w.lower() not in STOP]
    return " ".join(kept or words)


class Tools:
    class Valves(BaseModel):
        kiwix_url: str = Field(
            "http://kiwix:8080",
            description="kiwix-serve as Open WebUI reaches it (in Docker: the container name, e.g. http://kiwix:8080)",
        )
        public_url: str = Field(
            "",
            description="kiwix-serve as your browser reaches it, for the links shown in answers (empty: same as kiwix_url)",
        )
        books: str = Field(
            "",
            description="Only these books, comma-separated names as list_offline_library shows them, e.g. wikipedia_en_all,ifixit_en_all (empty: all)",
        )
        languages: str = Field(
            "",
            description="Only books in these languages, comma-separated ISO 639-3 codes, e.g. eng,spa (empty: all)",
        )
        results: int = Field(5, description="Search results returned to the model")
        per_book: int = Field(2, description="At most this many results from any one book (exact title matches aside), so one big site doesn't fill every slot")
        snippet_chars: int = Field(500, description="Text per search result")
        article_chars: int = Field(6000, description="Text per part returned by read_offline_article")
        timeout: int = Field(30, description="Seconds to wait for kiwix-serve")

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False  # sources are sent one per result instead (older Open WebUI would add the whole output)
        self._catalog, self._catalog_at, self._catalog_key = [], 0.0, None

    # --- kiwix-serve ---------------------------------------------------------

    def _base(self) -> str:
        return self.valves.kiwix_url.rstrip("/")

    def _get(self, path: str, **params) -> requests.Response:
        r = requests.get(self._base() + path, params=params or None, timeout=self.valves.timeout)
        r.raise_for_status()
        return r

    def _books(self) -> list:
        """The library, from kiwix-serve's catalog; cached for 5 minutes."""
        key = (self.valves.kiwix_url, self.valves.books, self.valves.languages)
        if self._catalog_key == key and time.time() - self._catalog_at < 300:
            return self._catalog
        root = ET.fromstring(self._get("/catalog/v2/entries", count=-1).content)
        wanted = {b.strip() for b in self.valves.books.split(",") if b.strip()}
        langs = {l.strip() for l in self.valves.languages.split(",") if l.strip()}
        books = []
        for e in root.findall("a:entry", ATOM):
            href = next((l.get("href") for l in e.findall("a:link", ATOM) if l.get("type") == "text/html"), "")
            b = {
                "id": e.findtext("a:id", "", ATOM).rsplit(":", 1)[-1],
                "name": e.findtext("a:name", "", ATOM),  # stable, e.g. wikipedia_en_all
                "path": href.rstrip("/").rsplit("/", 1)[-1],  # in URLs, e.g. wikipedia_en_all_maxi_2026-02
                "title": e.findtext("a:title", "", ATOM),
                "summary": e.findtext("a:summary", "", ATOM),
                "language": e.findtext("a:language", "", ATOM),
                "articles": int(e.findtext("a:articleCount", "0", ATOM) or 0),
            }
            if wanted and not wanted & {b["name"], b["path"]}:
                continue
            if langs and not langs & set(b["language"].split(",")):
                continue
            books.append(b)
        self._catalog, self._catalog_at, self._catalog_key = books, time.time(), key
        return books

    def _pick(self, books: list, book: str) -> list:
        """Books matching the model's `book` argument: part of a name or title, any case."""
        if not book.strip():
            return books
        b = book.strip().lower()
        return [x for x in books if b in x["name"].lower() or b in x["path"].lower() or b in x["title"].lower()]

    def _link(self, content_path: str) -> str:
        """content_path is URL-encoded, e.g. wikipedia_en_all_maxi_2026-02/Photosynthesis."""
        return f"{(self.valves.public_url or self.valves.kiwix_url).rstrip('/')}/content/{content_path}"

    def _page(self, content_path: str) -> _Text:
        p = _Text()
        p.feed(self._get("/content/" + content_path).content.decode("utf-8", "replace"))
        m = MINDTOUCH.match(content_path)
        if m and not p.text():
            try:
                body = self._get(f"/content/{m.group(1)}/content/page_content_{m.group(2)}.json").json()["htmlBody"]
            except Exception:
                return p
            q = _Text()
            q.title = p.title
            q.feed(body)
            return q
        return p

    def _title_matches(self, books: list, query: str) -> tuple:
        """From kiwix-serve's title suggestions, per book:
        - exact: the title is the query (any case), the best-cased first ("sofia" -> Sofia before SOFIA);
        - keyword: the title has every keyword, at least as the start of a word ("replace battery iphone 6" ->
          "iPhone 6 Battery Replacement"), shortest titles first. This also finds articles in books that have
          no full-text index."""
        q = query.strip().rstrip("?!. ")
        kw = _keywords(q)
        if not kw:
            return [], []
        words = [w.lower() for w in re.findall(r"\w+", kw)]

        def suggest(b):
            try:
                return b, self._get("/suggest", content=b["path"], term=kw, count=10).json()
            except Exception:
                return b, []

        with ThreadPoolExecutor(8) as ex:
            answers = list(ex.map(suggest, books))
        exact, keyword = [], []
        for b, items in answers:
            for x in items:
                if x.get("kind") != "path":
                    continue
                title = html.unescape(x.get("value", ""))
                hit = (title, f"{b['path']}/{quote(x['path'], safe='/:')}", b, None)
                if title.lower() in (q.lower(), kw.lower()):
                    exact.append(((title not in (q, kw), title != q[:1].upper() + q[1:], -b["articles"]), hit))
                else:
                    title_words = re.findall(r"\w+", title.lower())
                    if all(any(t.startswith(w) for t in title_words) for w in words):
                        keyword.append((len(title_words), hit))
        exact.sort(key=lambda h: h[0])
        keyword.sort(key=lambda h: h[0])
        return [h for _, h in exact], [h for _, h in keyword]

    def _full_text(self, books: list, query: str, count: int) -> list:
        """kiwix-serve's full-text search, one request per language (it refuses to mix them)."""
        by_path = {b["path"]: b for b in books}
        groups = {}
        for b in books:
            groups.setdefault(b["language"], []).append(b)
        found = []
        for lang, group in groups.items():
            pattern = _keywords(query) if "eng" in lang.split(",") else query
            params = [("books.id", b["id"]) for b in group] + [
                ("pattern", pattern), ("format", "xml"), ("pageLength", str(count))]
            try:
                r = requests.get(self._base() + "/search", params=params, timeout=self.valves.timeout)
                r.raise_for_status()
            except requests.RequestException:
                continue  # the other languages may still answer
            for it in ET.fromstring(r.content).findall("./channel/item"):
                path = it.findtext("link", "").split("/content/", 1)[-1]
                book = by_path.get(path.split("/", 1)[0])
                snippet = re.sub(r"<[^>]+>", "", html.unescape(it.findtext("description", "")))
                found.append((html.unescape(it.findtext("title", "")), path, book, re.sub(r"\s+", " ", snippet).strip()))
        return found

    # --- what the model calls -----------------------------------------------

    async def search_offline_library(self, query: str, book: str = "", __event_emitter__=None) -> str:
        """
        Search the user's offline library (Kiwix): encyclopedias, Q&A sites, repair guides, medical references,
        textbooks and books, depending on what the user installed. Use short keywords or the likely article title
        (e.g. "photosynthesis", "iPhone 6 battery replacement", "burns first aid"), not a whole sentence. Each result
        is only the start of an article: when the user wants steps, instructions or details, call read_offline_article
        on the best result before answering. Answer from what you read and cite the links; never ask the user to call
        a function.
        :param query: keywords or an article title
        :param book: optional: search only books whose name contains this, e.g. "wikipedia" or "ifixit" (see list_offline_library)
        """
        await _status(__event_emitter__, f"Searching the offline library for “{query}”")
        try:
            out = await asyncio.to_thread(self._search, query, book)
        except Exception as e:
            await _status(__event_emitter__, "The offline library is not reachable", done=True)
            return self._unreachable(e)
        text, results = out
        for title, link, snippet in results:
            await _cite(__event_emitter__, title, link, snippet)
        await _status(__event_emitter__, f"Found {len(results)} result(s) in the offline library", done=True)
        return text

    def _search(self, query: str, book: str):
        v = self.valves
        books = self._pick(self._books(), book)
        if not books:
            return f"No book in the offline library matches “{book}”. Call list_offline_library to see the books.", []
        exact, keyword = self._title_matches(books, query)
        # Exact titles first, then the best title with every keyword from each book, then full text;
        # at most per_book results from any one book after the exact titles.
        firsts = {}
        for h in keyword:
            firsts.setdefault(h[2]["path"], h)
        hits, per_book, seen = [], {}, set()
        for n, h in enumerate(exact[:2] + list(firsts.values()) + self._full_text(books, query, max(v.results * 4, 20))):
            if h[1] in seen or LISTING.search(h[1]):
                continue
            key = h[2]["path"] if h[2] else ""
            if n >= len(exact[:2]):
                if per_book.get(key, 0) >= v.per_book:
                    continue
                per_book[key] = per_book.get(key, 0) + 1
            seen.add(h[1])
            hits.append(h)
            if len(hits) >= v.results:
                break
        out, results = [], []
        for title, path, b, snippet in hits:
            if snippet is None or len(snippet.strip(" .")) < 40:  # title match or no excerpt: start of the article
                try:
                    snippet = self._page(path).text()
                except Exception:
                    snippet = ""
            snippet = " ".join(snippet.replace("#", " ").split())[: v.snippet_chars]  # one line: results stay apart
            link = self._link(path)
            source = f"{title} ({b['title']})" if b else title
            out.append(f"## {source}\nLink: {link}\n{snippet}")
            results.append((source, link, snippet))
        if not out:
            return f"Nothing found in the offline library for “{query}”. Try other keywords or another book.", []
        return "\n\n".join(out), results

    async def read_offline_article(self, link: str, part: int = 1, __event_emitter__=None) -> str:
        """
        Read an article from the offline library, given a Link from search_offline_library. Long articles come in
        parts; ask for the next part only if the answer is not in this one.
        :param link: the article's Link, exactly as the search returned it
        :param part: which part of a long article, starting at 1
        """
        path = urlsplit(link).path if "://" in link else link
        if "/content/" not in path:
            return "That is not a link from the offline library; use a Link returned by search_offline_library."
        path = path.split("/content/", 1)[1]
        await _status(__event_emitter__, "Reading an article from the offline library")
        try:
            page = await asyncio.to_thread(self._page, path)
        except requests.HTTPError as e:
            await _status(__event_emitter__, "Article not found", done=True)
            return f"The offline library has no such article ({e.response.status_code})."
        except Exception as e:
            await _status(__event_emitter__, "The offline library is not reachable", done=True)
            return self._unreachable(e)
        parts = _parts(page.text(), max(self.valves.article_chars, 500))
        part = min(max(1, int(part)), len(parts))
        await _status(__event_emitter__, f"Read “{page.title.strip()}”", done=True)
        head = f"# {page.title.strip()}\nLink: {self._link(path)}\nPart {part} of {len(parts)}"
        if part < len(parts):
            head += f" (call read_offline_article with part={part + 1} for more)"
        return f"{head}\n\n{parts[part - 1] or 'The article has no text.'}"

    async def list_offline_library(self, __event_emitter__=None) -> str:
        """
        List the books in the user's offline library (name, title, language, size, description). Use it to know
        what can be searched, or to pick a book for search_offline_library.
        """
        await _status(__event_emitter__, "Listing the offline library")
        try:
            books = await asyncio.to_thread(self._books)
        except Exception as e:
            await _status(__event_emitter__, "The offline library is not reachable", done=True)
            return self._unreachable(e)
        await _status(__event_emitter__, f"The offline library has {len(books)} books", done=True)
        if not books:
            return "The offline library has no books (or none match the tool's book and language settings)."
        lines = [f"- {b['name']}: {b['title']} [{b['language']}, {b['articles']:,} articles] {b['summary']}" for b in
                 sorted(books, key=lambda b: -b["articles"])]
        return f"{len(books)} books:\n" + "\n".join(lines)

    def _unreachable(self, e: Exception) -> str:
        return (f"The offline library (kiwix-serve at {self.valves.kiwix_url}) is not reachable ({type(e).__name__}). "
                "Tell the user that kiwix-serve seems to be off, or that the tool's kiwix_url setting is wrong.")


async def _status(emitter, description: str, done: bool = False):
    if emitter:
        await emitter({"type": "status", "data": {"description": description, "done": done}})


async def _cite(emitter, title: str, url: str, text: str):
    if emitter:
        await emitter({"type": "citation", "data": {
            "document": [text], "metadata": [{"source": url, "name": title}], "source": {"name": title, "url": url}}})
