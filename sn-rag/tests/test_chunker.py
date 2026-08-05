"""Phase 2 acceptance tests: golden fixtures over the 5 Phase-0 documents,
plus the two mandatory property tests from the build spec §9."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import (CORPUS_PATH, PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                    CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
from ingest.chunker import (ATOMIC_KINDS, chunk_document, parse_frontmatter,
                            tokenize_blocks, build_parents, extract_api_symbols,
                            unescape_markdown)

# The 5 representative documents captured in Phase 0 (see docs/BUILD-LOG.md).
FIXTURES = {
    "api_list": "ServiceNowOfficialDocs/api-reference/api-client-next.md",
    "api_detail": "ServiceNowOfficialDocs/api-reference/GlideFormAPINX.md",
    "task": "ServiceNowOfficialDocs/it-service-management/accept-chat-ai-native-itsm.md",
    "release_note": "ServiceNowOfficialDocs/release-notes/australia-all-other-fixes.md",
    "personal": "Notion/ServiceNow/AI & VA/50+ (Un)documented Virtual Agent variables (vaInpu 1f6c42ce9a56808d8943f42feeb822c6.md",
}


def load(name: str) -> tuple[str, str]:
    rel_path = FIXTURES[name]
    path = CORPUS_PATH / rel_path
    if not path.exists():
        pytest.skip(f"fixture not present: {rel_path}")
    return path.read_text(encoding="utf-8"), rel_path


def chunk(name: str):
    text, rel_path = load(name)
    return chunk_document(text, rel_path, PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                          CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP), text, rel_path


ALL_FIXTURES = list(FIXTURES)


# --- Property test 1: parents tile the body exactly -------------------------

@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parents_reproduce_body_verbatim(name):
    (parents, _), text, _ = chunk(name)
    _, body = parse_frontmatter(text)
    assert "".join(p.text for p in parents) == body


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parents_reproduce_body_modulo_whitespace(name):
    """The spec's stated property, checked independently of exact tiling."""
    (parents, _), text, _ = chunk(name)
    _, body = parse_frontmatter(text)
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    assert norm("".join(p.text for p in parents)) == norm(body)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_parent_indexes_are_contiguous(name):
    (parents, _), _, _ = chunk(name)
    assert [p.parent_idx for p in parents] == list(range(len(parents)))
    assert len({p.parent_id for p in parents}) == len(parents)


# --- Property test 2: no boundary inside a code fence or table --------------

def atomic_spans(body: str) -> list[tuple[int, int]]:
    """Character spans of every atomic block in the body."""
    spans, offset = [], 0
    for block in tokenize_blocks(body):
        if block.kind in ATOMIC_KINDS:
            spans.append((offset, offset + len(block.text)))
        offset += len(block.text)
    return spans


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_parent_boundary_inside_atomic_block(name):
    (parents, _), text, _ = chunk(name)
    _, body = parse_frontmatter(text)
    spans = atomic_spans(body)
    offset = 0
    boundaries = []
    for p in parents[:-1]:
        offset += len(p.text)
        boundaries.append(offset)
    for boundary in boundaries:
        for start, end in spans:
            assert not (start < boundary < end), \
                f"parent boundary at {boundary} splits atomic block {start}-{end}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_child_splits_an_atomic_block(name):
    """Every atomic block must appear whole inside at least one child chunk."""
    (_, children), text, _ = chunk(name)
    _, body = parse_frontmatter(text)
    child_texts = [c.text for c in children]
    for block in tokenize_blocks(body):
        if block.kind not in ATOMIC_KINDS:
            continue
        assert any(block.text in ct for ct in child_texts), \
            f"atomic {block.kind} block was split across children: {block.text[:80]!r}"


def test_giant_code_block_is_never_split():
    """Spec §9 Phase 2: test with a doc containing a 6,000-char code block."""
    payload = "\n".join(f"gs.info('line {i} of a very long GlideRecord example');" for i in range(120))
    assert len(payload) > 6000
    doc = f"# Title\n\nIntro paragraph.\n\n## Example\n\n```javascript\n{payload}\n```\n\nTrailing text.\n"
    parents, children = chunk_document(doc, "synthetic/giant-code.md", PARENT_CHUNK_MIN_CHARS,
                                       PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    fence = f"```javascript\n{payload}\n```"
    assert any(fence in p.text for p in parents), "giant code block was split across parents"
    assert any(fence in c.text for c in children), "giant code block was split across children"
    assert "".join(p.text for p in parents) == doc


def test_giant_table_is_never_split():
    rows = "\n".join(f"|field_{i}|String|Description number {i} for the field.|" for i in range(200))
    doc = f"# Title\n\n## Parameters\n\n|Name|Type|Description|\n|----|----|-----------|\n{rows}\n\nAfter table.\n"
    table_start = "|Name|Type|Description|"
    parents, children = chunk_document(doc, "synthetic/giant-table.md", PARENT_CHUNK_MIN_CHARS,
                                       PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    holder = [p for p in parents if table_start in p.text]
    assert len(holder) == 1
    assert f"|field_199|String|Description number 199 for the field.|" in holder[0].text
    assert "".join(p.text for p in parents) == doc


def test_html_table_is_atomic():
    inner = "\n".join(f"<tr><td>row {i}</td><td>value {i}</td></tr>" for i in range(150))
    doc = f'# Title\n\n<table id="t1" class="parameters">\n{inner}\n</table>\n\nAfter.\n'
    parents, _ = chunk_document(doc, "synthetic/html-table.md", PARENT_CHUNK_MIN_CHARS,
                                PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    holder = [p for p in parents if "<table" in p.text]
    assert len(holder) == 1
    assert "</table>" in holder[0].text
    assert "<tr><td>row 149</td><td>value 149</td></tr>" in holder[0].text


# --- Size discipline --------------------------------------------------------

@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_oversized_parents_are_only_ever_single_atomic_blocks(name):
    """A parent may exceed max_chars only because one indivisible block does."""
    (parents, _), text, _ = chunk(name)
    for p in parents:
        if len(p.text) <= PARENT_CHUNK_MAX_CHARS:
            continue
        blocks = tokenize_blocks(p.text)
        oversized = [b for b in blocks if len(b.text) > PARENT_CHUNK_MAX_CHARS]
        assert oversized and all(b.kind in ATOMIC_KINDS for b in oversized), \
            f"parent {p.parent_idx} is oversized without an oversized atomic block"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_children_cover_all_parent_text(name):
    """Every parent with content produces at least one child."""
    (parents, children), _, _ = chunk(name)
    by_parent = {}
    for c in children:
        by_parent.setdefault(c.parent_id, []).append(c)
    for p in parents:
        if p.text.strip():
            assert by_parent.get(p.parent_id), f"parent {p.parent_idx} produced no children"


# --- Property: no chunk is whitespace-only ----------------------------------
# A blank-line remnant between sections used to flush as its own child whose
# text was "\n". That is a real vector — indexed, searchable, and rendering an
# empty snippet if it ever ranks. It surfaced as the top hit for "incident
# management" and broke test_search_returns_populated_hits. Measured before the
# fix: 432 of 14,930 children (2.89%) across a 1,500-file sample, 16.7% of files.

@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_no_whitespace_only_children(name):
    """Children become vectors, so a blank one is a real defect."""
    (_, children), _, _ = chunk(name)
    blank = [c.chunk_id for c in children if not c.text.strip()]
    assert not blank, f"whitespace-only children: {blank[:5]}"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_whitespace_only_parents_are_unreachable(name):
    """Parents may be blank; they must never be retrievable.

    Parents are deliberately NOT filtered: they must tile the body verbatim
    (test_parents_reproduce_body_verbatim), so dropping a blank-line remnant
    would lose the invariant that no source text is silently discarded. That is
    safe only because a blank parent has no children, and `sn_get_section` is
    reachable exclusively through a search hit's `parent_id` — i.e. through a
    child. Measured over 800 corpus files: 44 blank parents, 0 reachable.

    If this ever fails, blank parents became addressable and must be merged into
    an adjacent parent rather than dropped.
    """
    (parents, children), _, _ = chunk(name)
    referenced = {c.parent_id for c in children}
    reachable_blanks = [p.parent_id for p in parents
                        if not p.text.strip() and p.parent_id in referenced]
    assert not reachable_blanks, f"blank parents are retrievable: {reachable_blanks[:5]}"


def test_no_whitespace_only_chunks_in_known_regression_document():
    """The document that actually produced the '\\n' chunk in production."""
    rel_path = ("ServiceNowOfficialDocs/operational-technology/"
                "operational-technology-incident-management/"
                "operational-technology-incident-management.md")
    path = CORPUS_PATH / rel_path
    if not path.exists():
        pytest.skip(f"fixture not present: {rel_path}")
    parents, children = chunk_document(
        path.read_text(encoding="utf-8"), rel_path,
        PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
        CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    assert children, "document should still produce children"
    assert all(c.text.strip() for c in children), (
        "the blank-chunk regression is back: "
        f"{[c.chunk_id for c in children if not c.text.strip()]}")


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_child_ids_unique(name):
    (_, children), _, _ = chunk(name)
    assert len({c.chunk_id for c in children}) == len(children)


# --- Breadcrumbs ------------------------------------------------------------

def test_h_path_is_a_breadcrumb():
    doc = "# GlideRecord\n\nIntro.\n\n## Methods\n\nText.\n\n### addQuery()\n\nDetail.\n"
    blocks = tokenize_blocks(doc)
    paths = [b.h_path for b in blocks if b.kind == "heading"]
    assert paths == ["GlideRecord", "GlideRecord > Methods", "GlideRecord > Methods > addQuery()"]


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_children_have_non_empty_h_path(name):
    (_, children), _, _ = chunk(name)
    assert children, "fixture produced no children"
    assert all(c.h_path for c in children[1:]), "child lost its heading breadcrumb"


def test_h_path_is_unescaped():
    """h_path is prepended to embedded text, so markdown escapes are noise."""
    doc = "# GlideForm \\(Next Experience\\) - Client\n\n## addQuery\\(String name\\)\n\nText.\n"
    blocks = tokenize_blocks(doc)
    paths = [b.h_path for b in blocks if b.kind == "heading"]
    assert paths[0] == "GlideForm (Next Experience) - Client"
    assert paths[1] == "GlideForm (Next Experience) - Client > addQuery(String name)"
    assert "\\" not in paths[1]


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_child_breadcrumb_matches_its_own_heading(name):
    """Regression: a blank-line remnant of the previous section must not pin a
    child's breadcrumb to a heading its content does not belong to."""
    (_, children), _, _ = chunk(name)
    for c in children:
        m = re.search(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$", c.text, re.MULTILINE)
        if not m:
            continue
        first_heading = unescape_markdown(m.group(1).strip())
        # If the chunk opens with a heading (only blank text before it), that
        # heading must be the tail of the breadcrumb.
        before = c.text[:m.start()]
        if before.strip():
            continue
        assert c.h_path.split(" > ")[-1] == first_heading, \
            f"chunk opens with {first_heading!r} but breadcrumb ends {c.h_path.split(' > ')[-1]!r}"


# --- api_symbols extraction (acceptance: verified against API-ref fixture) ---

def test_api_symbols_from_api_reference_fixture():
    (parents, _), _, _ = chunk("api_detail")
    symbols = set()
    for p in parents:
        symbols.update(p.api_symbols)
    # Methods documented in headings and demonstrated in code fences.
    assert "g_form.addChoice" in symbols
    assert "addChoice" in symbols
    assert "GlideForm" in symbols
    assert "addDecoration" in symbols


def test_api_symbols_reject_urls_and_filenames():
    blocks = tokenize_blocks("```\nvar x = 'https://www.servicenow.com/docs/index.html';\ngr.addQuery('a');\n```\n")
    symbols = extract_api_symbols(blocks)
    assert "gr.addQuery" in symbols
    assert not any(s.endswith(".com") or s.endswith(".html") for s in symbols)


def test_api_symbols_are_language_agnostic():
    blocks = tokenize_blocks("```python\nresult = client.records.fetch_all(table='incident')\n```\n")
    symbols = extract_api_symbols(blocks)
    assert "client.records.fetch_all" in symbols


# --- Edge cases -------------------------------------------------------------

def test_frontmatter_is_excluded_from_body():
    doc = "---\ntitle: Test\nrelease: australia\n---\n\n# Heading\n\nBody text.\n"
    fm, body = parse_frontmatter(doc)
    assert "title: Test" in fm
    assert body.startswith("\n# Heading") or body.startswith("# Heading")
    assert "title: Test" not in body


def test_document_with_no_headings():
    doc = "Just a paragraph with no heading at all.\n\nAnd another one.\n"
    parents, children = chunk_document(doc, "synthetic/no-heading.md", PARENT_CHUNK_MIN_CHARS,
                                       PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    assert "".join(p.text for p in parents) == doc
    assert children


def test_empty_document():
    parents, children = chunk_document("", "synthetic/empty.md", PARENT_CHUNK_MIN_CHARS,
                                       PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    assert parents == [] or "".join(p.text for p in parents) == ""
    assert children == []


def test_unclosed_code_fence_does_not_hang_or_lose_text():
    doc = "# Title\n\n```javascript\nvar x = 1;\nno closing fence here\n"
    parents, _ = chunk_document(doc, "synthetic/unclosed.md", PARENT_CHUNK_MIN_CHARS,
                                PARENT_CHUNK_MAX_CHARS, CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    assert "".join(p.text for p in parents) == doc


def test_ids_are_deterministic():
    doc = "# A\n\nText.\n"
    a = chunk_document(doc, "x/y.md", PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                       CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    b = chunk_document(doc, "x/y.md", PARENT_CHUNK_MIN_CHARS, PARENT_CHUNK_MAX_CHARS,
                       CHILD_CHUNK_CHARS, CHILD_CHUNK_OVERLAP)
    assert [p.parent_id for p in a[0]] == [p.parent_id for p in b[0]]
    assert [c.chunk_id for c in a[1]] == [c.chunk_id for c in b[1]]


# --- embedded-text recipe -------------------------------------------------
# These guard the signal that was missing until 2026-08-05: the filename and
# title were never embedded, so a descriptively-named document was unreachable
# under its own name. Asserting on the assembled STRING, not on counts — the
# defect this replaces was invisible to every count-based test.

def test_embed_text_includes_title_and_filename_words():
    from ingest.embed import build_embed_text
    out = build_embed_text("Body prose here.", h_path="Setup > Auth",
                           doc_title="Building AI Agents",
                           rel_path="raw/inbox/servicenow-sdk-guide.md")
    assert "Building AI Agents" in out
    # words present in the stem but NOT in the title must survive
    for word in ("servicenow", "sdk", "guide"):
        assert word in out, f"filename word {word!r} lost from embedded text"
    assert "Setup > Auth" in out
    assert out.endswith("Body prose here.")


def test_embed_text_does_not_duplicate_title_words_from_stem():
    from ingest.embed import build_embed_text
    out = build_embed_text("Body.", h_path="", doc_title="Building AI Agents",
                           rel_path="notes/building-ai-agents.md")
    assert out.lower().count("building") == 1, out
    assert out.lower().count("agents") == 1, out


def test_embed_text_flag_off_reproduces_the_old_recipe():
    from ingest.embed import build_embed_text
    out = build_embed_text("Body.", h_path="A > B", doc_title="T",
                           rel_path="x/y.md", include_title=False)
    assert out == "A > B\n\nBody."


def test_embed_text_survives_missing_title_and_path():
    from ingest.embed import build_embed_text
    assert build_embed_text("Body.") == "Body."
    assert build_embed_text("Body.", h_path="H") == "H\n\nBody."
