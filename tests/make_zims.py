"""Builds the small ZIM files the tests search, with python-libzim:

  python3 tests/make_zims.py OUTDIR

- test-wiki_en: an English encyclopedia (exact titles in two casings, a long
  article to read in parts, reference markers, a navigation box and a table)
- test-qa_en: an English Q&A site, with a tag listing page
- test-wiki_bg: a Bulgarian book (kiwix-serve refuses to search two languages
  at once, so the tool must search them separately)
- test-guides_en: repair guides without a full-text index (only titles can be
  searched, as in some real ZIMs)
- test-lt_en: a LibreTexts-style app book (pages only redirect; the text is in
  content/page_content_<id>.json)
"""
import json
import sys

from libzim.writer import Creator, Hint, IndexData, Item, StringProvider


class Index(IndexData):
    """Index text given by the scraper, as openZIM's LibreTexts ZIMs do for their app pages."""

    def __init__(self, title, content):
        super().__init__()
        self.title, self.content = title, content

    def has_indexdata(self):
        return True

    def get_title(self):
        return self.title

    def get_content(self):
        return self.content

    def get_keywords(self):
        return ""

    def get_wordcount(self):
        return len(self.content.split())

    def get_geoposition(self):
        return None


class Page(Item):
    def __init__(self, path, title, body, mimetype="text/html", front=True):
        super().__init__()
        self.path, self.title, self.body, self.mimetype, self.front = path, title, body, mimetype, front

    def get_path(self):
        return self.path

    def get_title(self):
        return self.title

    def get_mimetype(self):
        return self.mimetype

    def get_contentprovider(self):
        return StringProvider(self.body)

    def get_hints(self):
        return {Hint.FRONT_ARTICLE: self.front}


class IndexedPage(Page):
    """A page whose index text comes from the scraper (defining get_indexdata at all
    replaces libzim's own HTML indexing, so plain pages must not have it)."""

    def __init__(self, path, title, body, index):
        super().__init__(path, title, body)
        self.index = index

    def get_indexdata(self):
        return Index(self.title, self.index)


def html(title, body):
    return (f"<!DOCTYPE html><html class='client-js vector-feature-toc-pinned'><head><title>{title}</title></head>"
            f"<body><nav>Main menu Random page</nav><div id='content'><h1>{title}</h1>{body}</div>"
            f"<footer>Footer text</footer></body></html>")


LONG = "".join(f"<p>Paragraph {i}: chloroplast thylakoid stroma calvin cycle light reactions. " + "Lorem ipsum dolor sit amet. " * 12 + "</p>"
               for i in range(1, 41))

BOOKS = {
    "test-wiki_en": ("eng", "Test Wiki", "A tiny encyclopedia for tests", [
        Page("Sofia", "Sofia", html("Sofia", "<p>Sofia is the capital and largest city of Bulgaria.<sup class='reference'>[1]</sup> "
                                    "It lies at the foot of Vitosha.</p>"
                                    "<table><tr><td>Tablecell population 1.2 million</td></tr></table>"
                                    "<div class='navbox'>Navboxtext Cities of Bulgaria</div>"
                                    "<span class='mw-editsection'>[edit]</span><h2>History</h2><p>Known as Serdica in antiquity.</p>"
                                    "<div class='comment-thread'><p>Commenttext: great article!</p></div>")),
        Page("SOFIA", "SOFIA", html("SOFIA", "<p>SOFIA was an airborne observatory, a telescope on a Boeing 747.</p>")),
        Page("Photosynthesis", "Photosynthesis", html("Photosynthesis", "<p>Photosynthesis turns light into chemical energy.</p>" + LONG)),
        Page("Artificial_photosynthesis", "Artificial photosynthesis",
             html("Artificial photosynthesis", "<p>Artificial photosynthesis mimics photosynthesis to make fuels.</p>")),
        Page("Burn", "Burn", html("Burn", "<p>A burn is an injury to skin caused by heat. First aid for burns: cool the burn "
                                  "under running water for twenty minutes.</p>")),
    ]),
    "test-qa_en": ("eng", "Test Q&A", "A tiny Q&A site for tests", [
        Page("questions/1/treat-minor-burn", "How should I treat a minor burn from the oven?",
             html("How should I treat a minor burn from the oven?", "<p>I touched the oven tray. What is the first aid for a minor burn?</p>"
                  "<div class='answer'><p>Cool the burn with running water, then cover it loosely.</p></div>")),
        Page("questions/2/treated-wood", "How do I dispose of treated wood?",
             html("How do I dispose of treated wood?", "<p>Pressure treated wood should not be burned at home.</p>")),
        Page("questions/tagged/burns", "Questions tagged burns",
             html("Questions tagged burns", "<p>burn burns burn first aid treat minor burn oven burn burn</p>")),
        Page("questions/3/servo-jitter", "Servo jitter on a robot arm",
             html("Servo jitter on a robot arm", "<p>My servo jitters when the robot arm holds still.</p>")),
        Page("questions/4/servo-power", "Servo power supply for a robot",
             html("Servo power supply for a robot", "<p>Each servo needs its own power supply; the servo jitters otherwise.</p>")),
        Page("questions/5/servo-pwm", "Servo PWM frequency",
             html("Servo PWM frequency", "<p>Servo PWM should be 50 Hz or the servo jitters.</p>")),
    ]),
    "test-wiki_bg": ("bul", "Тест Уики", "Мъничка енциклопедия за тестове", [
        Page("София", "София", html("София", "<p>София е столицата и най-големият град на България.</p>")),
    ]),
    "test-guides_en": ("eng", "Test Guides", "Repair guides, without a full-text index", [
        Page("Guide/iPhone+6+Battery+Replacement/1", "iPhone 6 Battery Replacement",
             html("iPhone 6 Battery Replacement", "<p>Step 1: heat the screen. Step 2: remove the battery adhesive.</p>")),
        Page("Guide/iPhone+6+Screen+Replacement/2", "iPhone 6 Screen Replacement",
             html("iPhone 6 Screen Replacement", "<p>Step 1: remove the two pentalobe screws.</p>")),
    ]),
    "test-lt_en": ("eng", "Test LibreTexts", "An app-style textbook for tests", [
        Page("index.html", "Test LibreTexts", "<html><head><title>Test LibreTexts</title></head><body><div id='app'></div></body></html>", front=False),
        IndexedPage("index/page_7", "Chlorophyll pigments",
             "<html><head><title>Chlorophyll pigments</title><meta http-equiv='refresh' content=\"0;URL='../index.html#/Chlorophyll'\" />"
             "</head><body></body></html>", "Chlorophyll absorbs red and blue light and reflects green."),
        Page("content/page_content_7.json", "",
             json.dumps({"htmlBody": "<p>Chlorophyll absorbs red and blue light and reflects green.</p>"}),
             mimetype="application/json", front=False),
    ]),
}

out = sys.argv[1]
for name, (lang, title, desc, pages) in BOOKS.items():
    with Creator(f"{out}/{name}_2026-01.zim").config_indexing(name != "test-guides_en", lang) as c:
        c.set_mainpath(pages[0].path)
        for p in pages:
            c.add_item(p)
        for k, v in {"Name": name, "Title": title, "Language": lang, "Creator": "tests", "Publisher": "tests",
                     "Date": "2026-01-01", "Description": desc}.items():
            c.add_metadata(k, v)
    print("built", name)
