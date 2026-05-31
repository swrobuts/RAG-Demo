from backend.data.cleaner import clean_html


SAMPLE_HTML = """
<div class="mw-parser-output">
  <p>Einleitungstext über Apple.</p>
  <table class="infobox"><tr><td>infobox content (should be dropped)</td></tr></table>
  <h2><span class="mw-headline" id="Geschichte">Geschichte</span><span class="mw-editsection">[edit]</span></h2>
  <p>Apple wurde 1976 gegründet.</p>
  <h3><span class="mw-headline" id="Gr.C3.BCndung">Gründung</span></h3>
  <p>Steve Jobs, Steve Wozniak und Ronald Wayne.</p>
  <h2><span class="mw-headline" id="Produkte">Produkte</span></h2>
  <p>iPhone, iPad, Mac.</p>
  <h2><span class="mw-headline" id="Weblinks">Weblinks</span></h2>
  <p>https://apple.com (should be dropped by section title)</p>
</div>
"""


def test_clean_html_extracts_sections_and_strips_infobox():
    doc = clean_html(SAMPLE_HTML, "Apple")
    headings = [s.heading for s in doc.sections]
    assert "Geschichte" in headings
    assert "Produkte" in headings
    assert "Weblinks" not in headings
    assert "infobox content" not in doc.markdown


def test_section_tree_nests_h3_under_h2():
    doc = clean_html(SAMPLE_HTML, "Apple")
    history = next(s for s in doc.sections if s.heading == "Geschichte")
    assert any(c.heading == "Gründung" for c in history.children)
    gruendung = next(c for c in history.children if c.heading == "Gründung")
    assert gruendung.path == "Geschichte > Gründung"
    assert "Wozniak" in gruendung.text


def test_intro_paragraphs_become_einleitung_section():
    doc = clean_html(SAMPLE_HTML, "Apple")
    intro = next((s for s in doc.sections if s.heading == "Einleitung"), None)
    assert intro is not None
    assert "Einleitungstext" in intro.text


def test_edit_links_are_stripped_from_headings():
    doc = clean_html(SAMPLE_HTML, "Apple")
    assert all("[edit]" not in s.heading for s in doc.sections)


# Modern MediaWiki (~1.43+) wraps each heading in <div class="mw-heading
# mw-headingN">…</div>. The cleaner must walk the wrapper's siblings, not the
# heading's empty sibling list.
MODERN_MW_HTML = """
<div class="mw-parser-output">
  <p>Lead über Apple Inc.</p>
  <div class="mw-heading mw-heading2"><h2 id="Geschichte">Geschichte</h2></div>
  <div class="mw-heading mw-heading3"><h3 id="Gruendung">1976–1980: Gründung</h3></div>
  <p>Apple wurde 1976 von Steve Jobs, Steve Wozniak und Ronald Wayne gegründet.</p>
  <p>Der Apple I war das erste Produkt.</p>
  <div class="mw-heading mw-heading3"><h3 id="Sculley">1985–1996: Sculley-Ära</h3></div>
  <p>Nach Jobs' Weggang übernahm John Sculley.</p>
  <div class="mw-heading mw-heading2"><h2 id="Produkte">Produkte</h2></div>
  <p>Apple verkauft Hardware und Software.</p>
</div>
"""


def test_modern_wrapper_html_captures_h3_text():
    doc = clean_html(MODERN_MW_HTML, "Apple")
    geschichte = next(s for s in doc.sections if s.heading == "Geschichte")
    gruendung = next(c for c in geschichte.children if c.heading == "1976–1980: Gründung")
    sculley = next(c for c in geschichte.children if c.heading == "1985–1996: Sculley-Ära")
    assert "Wozniak" in gruendung.text, "h3 prose must be captured under modern wrapper HTML"
    assert "Sculley" in sculley.text


def test_modern_wrapper_html_lead_is_only_pre_heading_prose():
    doc = clean_html(MODERN_MW_HTML, "Apple")
    intro = next(s for s in doc.sections if s.heading == "Einleitung")
    assert "Lead über Apple Inc." in intro.text
    # Crucial: the lead must NOT swallow the entire article (which was the
    # regression with the older first-heading detection).
    assert "Wozniak" not in intro.text
    assert "Sculley" not in intro.text


def test_modern_wrapper_html_separates_h2_siblings():
    doc = clean_html(MODERN_MW_HTML, "Apple")
    produkte = next(s for s in doc.sections if s.heading == "Produkte")
    assert "Hardware" in produkte.text
    assert "Sculley" not in produkte.text   # Geschichte's content must not leak in
