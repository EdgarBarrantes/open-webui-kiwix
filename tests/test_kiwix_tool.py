"""Tests the tool against a real kiwix-serve serving the ZIMs from make_zims.py.
tests/run.sh sets it all up; to run by hand: KIWIX_URL=http://127.0.0.1:18089 python3 -m unittest -v tests/test_kiwix_tool.py
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kiwix_tool import Tools  # noqa: E402

KIWIX = os.environ.get("KIWIX_URL", "http://127.0.0.1:18089")


def run(coro):
    return asyncio.run(coro)


def links(text):
    return [l.removeprefix("Link: ") for l in text.splitlines() if l.startswith("Link: ")]


def titles(text):
    return [l.removeprefix("## ") for l in text.splitlines() if l.startswith("## ")]


class KiwixToolTest(unittest.TestCase):
    def setUp(self):
        self.t = Tools()
        self.t.valves.kiwix_url = KIWIX
        self.events = []

    async def emit(self, e):
        self.events.append(e)

    def search(self, query, book="", **valves):
        for k, v in valves.items():
            setattr(self.t.valves, k, v)
        return run(self.t.search_offline_library(query, book, __event_emitter__=self.emit))

    def read(self, link, part=1):
        return run(self.t.read_offline_article(link, part))

    # --- list --------------------------------------------------------------

    def test_list_shows_every_book(self):
        out = run(self.t.list_offline_library())
        self.assertTrue(out.startswith("5 books:"), out)
        for name in ("test-wiki_en", "test-qa_en", "test-wiki_bg", "test-guides_en", "test-lt_en"):
            self.assertIn(f"- {name}: ", out)
        self.assertIn("A tiny encyclopedia for tests", out)

    def test_list_respects_language_and_book_settings(self):
        self.t.valves.languages = "bul"
        self.assertTrue(run(self.t.list_offline_library()).startswith("1 books:"))
        self.t.valves.languages, self.t.valves.books = "", "test-qa_en, test-wiki_en"
        out = run(self.t.list_offline_library())
        self.assertTrue(out.startswith("2 books:"), out)
        self.assertNotIn("test-lt_en", out)

    # --- search ------------------------------------------------------------

    def test_exact_title_comes_first_best_casing_first(self):
        out = self.search("sofia")
        self.assertEqual(titles(out)[:2], ["Sofia (Test Wiki)", "SOFIA (Test Wiki)"], out)
        self.assertIn("capital and largest city", out)  # the title match carries the article's start

    def test_search_covers_books_in_several_languages(self):
        self.assertIn("София (Тест Уики)", titles(self.search("София")))

    def test_question_style_query_finds_the_answer(self):
        out = self.search("How do I treat a minor burn?")
        self.assertIn("How should I treat a minor burn from the oven? (Test Q&A)", titles(out), out)

    def test_titles_with_every_keyword_are_found_without_full_text_index(self):
        out = self.search("how do I replace the battery of an iPhone 6?")
        self.assertIn("iPhone 6 Battery Replacement (Test Guides)", titles(out), out)
        self.assertNotIn("iPhone 6 Screen Replacement (Test Guides)", titles(out), out)

    def test_tag_listing_pages_are_left_out(self):
        self.assertFalse([l for l in links(self.search("burn first aid")) if "/questions/tagged/" in l])

    def test_one_book_fills_at_most_per_book_slots(self):
        qa = [t for t in titles(self.search("servo jitter", results=5, per_book=2)) if t.endswith("(Test Q&A)")]
        self.assertEqual(len(qa), 2, qa)

    def test_results_setting_limits_results(self):
        self.assertLessEqual(len(titles(self.search("photosynthesis", results=1))), 1)

    def test_book_argument_narrows_the_search(self):
        out = self.search("burn", book="qa")
        self.assertTrue(titles(out) and all(t.endswith("(Test Q&A)") for t in titles(out)), out)
        self.assertIn("No book in the offline library matches", self.search("burn", book="nosuchbook"))

    def test_nothing_found(self):
        self.assertIn("Nothing found", self.search("xyzzyquux"))

    def test_links_use_public_url_and_open(self):
        out = self.search("photosynthesis", public_url="https://wiki.example.org/")
        self.assertTrue(links(out)[0].startswith("https://wiki.example.org/content/test-wiki_en_2026-01/"), out)
        self.assertIn("# Photosynthesis", self.read(links(out)[0]))

    def test_search_emits_status_and_one_source_per_result(self):
        out = self.search("photosynthesis")
        kinds = [e["type"] for e in self.events]
        self.assertEqual(kinds[0], "status")
        self.assertTrue(self.events[-1]["data"]["done"])
        cites = [e["data"] for e in self.events if e["type"] == "citation"]
        self.assertEqual(len(cites), len(links(out)))
        self.assertEqual(cites[0]["source"]["url"], links(out)[0])
        self.assertTrue(cites[0]["document"][0])

    def test_libretexts_style_book_has_text(self):
        out = self.search("chlorophyll")
        self.assertIn("Chlorophyll pigments (Test LibreTexts)", titles(out), out)
        self.assertIn("absorbs red and blue light", out)

    # --- read --------------------------------------------------------------

    def test_read_keeps_the_article_and_drops_the_rest(self):
        out = self.read(f"{KIWIX}/content/test-wiki_en_2026-01/Sofia")
        self.assertTrue(out.startswith("# Sofia\nLink: "), out)
        self.assertIn("capital and largest city of Bulgaria. It lies", out)
        self.assertIn("## History", out)
        for junk in ("[1]", "Navboxtext", "Tablecell", "[edit]", "Main menu", "Footer text", "Commenttext"):
            self.assertNotIn(junk, out)

    def test_read_long_article_in_parts(self):
        link = f"{KIWIX}/content/test-wiki_en_2026-01/Photosynthesis"
        self.t.valves.article_chars = 2000
        first, second = self.read(link), self.read(link, 2)
        self.assertRegex(first, r"Part 1 of \d+ \(call read_offline_article with part=2 for more\)")
        self.assertIn("Part 2 of", second)
        self.assertNotEqual(first.split("\n\n", 1)[1][:200], second.split("\n\n", 1)[1][:200])
        last = self.read(link, 999)  # past the end: the last part
        self.assertNotIn("for more", last)
        self.assertIn("Paragraph 40", last)

    def test_read_accepts_paths_and_libretexts_pages(self):
        self.assertIn("Serdica", self.read("/content/test-wiki_en_2026-01/Sofia"))
        self.assertIn("reflects green", self.read(f"{KIWIX}/content/test-lt_en_2026-01/index/page_7"))

    def test_read_bad_links(self):
        self.assertIn("no such article (404)", self.read(f"{KIWIX}/content/test-wiki_en_2026-01/Nope"))
        self.assertIn("not a link from the offline library", self.read("https://example.org/wiki/Sofia"))

    # --- kiwix-serve down --------------------------------------------------

    def test_unreachable_kiwix_is_explained(self):
        self.t.valves.kiwix_url, self.t.valves.timeout = "http://127.0.0.1:9", 3
        for out in (self.search("sofia"), self.read("http://127.0.0.1:9/content/x/y"), run(self.t.list_offline_library())):
            self.assertIn("is not reachable", out)


if __name__ == "__main__":
    unittest.main()
