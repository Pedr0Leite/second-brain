"""Phase 6 acceptance: caps enforced in code, structured errors, ingest containment."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CAPS, VAULT_PATH
from mcp_server import caps as C
from mcp_server.ingest_tool import IngestError, resolve_dest, default_dest


# --- caps (pure) ------------------------------------------------------------

def test_truncate_chars_enforces_hard_limit():
    text = "x " * 10000
    out, was = C.truncate_chars(text, 100)
    assert was and len(out) <= 100


def test_truncate_chars_marks_truncation_visibly():
    out, was = C.truncate_chars("y " * 5000, 200)
    assert "[TRUNCATED]" in out


def test_truncate_chars_passes_short_text_through():
    out, was = C.truncate_chars("short", 100)
    assert out == "short" and not was


def test_truncate_words():
    out, was = C.truncate_words(" ".join(["w"] * 500), 10)
    assert was and len(out.split()) <= 11  # +1 for the marker token


def test_snippet_collapses_whitespace():
    assert "\n" not in C.snippet("a\n\n\n   b\t\tc", 50)


def test_cap_result_list_respects_total_chars():
    items = [{"snippet": "z" * 5000, "rel_path": f"f{i}.md"} for i in range(50)]
    kept, meta = C.cap_result_list(items, "snippet", 8, 150, 6000)
    assert len(kept) <= 8
    assert sum(len(k["snippet"]) for k in kept) <= 6000
    assert meta["approx_tokens"] > 0


def test_cap_counts_serialized_size_not_just_snippet():
    """Regression: a 6,000-char cap emitted 7,728 chars because per-item
    metadata overhead was estimated at 120 chars instead of measured. Long
    rel_paths and 40-char ids blow that estimate."""
    import json
    items = [{
        "snippet": "word " * 140,
        "chunk_id": "a" * 40,
        "parent_id": "b" * 40,
        "rel_path": "ServiceNowOfficialDocs/support-and-troubleshooting/"
                    f"application-development/KB0{i:06d} - a fairly long document title.md",
        "h_path": "Some > Deeply > Nested > Heading > Breadcrumb",
        "source": "official",
        "score": 0.1234,
    } for i in range(40)]
    kept, meta = C.cap_result_list(items, "snippet", 8, 150, 6000)
    assert len(json.dumps(kept)) <= 6000, (
        f"serialized output {len(json.dumps(kept))} exceeds the 6000-char cap")


def test_cap_result_list_reports_what_it_dropped():
    items = [{"snippet": "z " * 400, "rel_path": f"f{i}.md"} for i in range(8)]
    kept, meta = C.cap_result_list(items, "snippet", 8, 150, 1000)
    assert meta["available"] == 8
    assert meta["returned"] == len(kept) < 8
    assert meta["dropped_for_budget"] > 0


def test_error_is_structured_never_prose():
    e = C.error("BOOM", "it broke", retryable=True)
    assert e["error_code"] == "BOOM" and e["retryable"] is True and e["ok"] is False


def test_every_tool_has_a_cap_configured():
    for tool in ("sn_search", "sn_get_section", "sn_outline", "sn_lexical",
                 "sn_research", "sn_stats", "sn_ingest"):
        assert C.cap_for(tool)


def test_unknown_tool_cap_raises():
    with pytest.raises(KeyError):
        C.cap_for("sn_nonexistent")


# --- citations must not repeat what hits already carry -----------------------

@pytest.mark.skipif(not VAULT_PATH.is_dir(), reason="corpus not present")
def test_lexical_citations_are_deduped_and_carry_no_empty_fields():
    """ripgrep returns many hits per file; citations are file-level.

    Emitting one citation per hit repeated 100+ char paths verbatim, and every
    entry carried "h_path": "" — a field lexical search can never populate,
    because ripgrep yields line numbers, not the header tree. Measured 886 chars
    of pure duplication on a single realistic query.
    """
    from mcp_server import server as S
    result = S.sn_lexical(pattern="current.setAbortAction", agent="servicenow")
    if not result.get("ok") or not result["hits"]:
        pytest.skip("ripgrep unavailable or pattern absent from this corpus")

    cites = result["citations"]
    paths = [c["rel_path"] for c in cites]
    assert len(paths) == len(set(paths)), f"duplicate citations: {paths}"
    assert set(paths) == {h["rel_path"] for h in result["hits"]}, (
        "citations must cover exactly the files present in hits")
    for c in cites:
        assert set(c) == {"rel_path"}, (
            f"citation carries a field lexical cannot populate: {c}")


# --- ingest path containment (security boundary) ----------------------------

@pytest.mark.parametrize("bad", [
    "/etc/passwd",
    "../../../etc/passwd",
    "raw/../../outside.md",
    "../escape.md",
    "/tmp/absolute.md",
])
def test_resolve_dest_rejects_escapes(bad):
    with pytest.raises(IngestError) as exc:
        resolve_dest(bad)
    assert exc.value.code == "INGEST_BAD_PATH"


def test_resolve_dest_rejects_null_byte():
    with pytest.raises(IngestError):
        resolve_dest("raw/inbox/a\x00b.md")


def test_resolve_dest_rejects_empty():
    with pytest.raises(IngestError):
        resolve_dest("   ")


@pytest.mark.parametrize("bad", ["raw/inbox/x.exe", "raw/inbox/x.sh", "raw/inbox/x.py"])
def test_resolve_dest_rejects_non_markdown(bad):
    with pytest.raises(IngestError) as exc:
        resolve_dest(bad)
    assert exc.value.code == "INGEST_BAD_TYPE"


def test_resolve_dest_accepts_vault_relative_markdown():
    out = resolve_dest("raw/inbox/note.md")
    assert str(out).startswith(str(VAULT_PATH.resolve()))
    assert out.name == "note.md"


def test_default_dest_stays_inside_vault():
    dest = default_dest("mynote.md", "personal")
    assert not dest.startswith("/") and ".." not in dest
    resolve_dest(dest)  # must not raise


def test_default_dest_adds_markdown_suffix():
    assert default_dest("notes", "personal").endswith(".md")


# --- path defaults must not depend on cwd -----------------------------------

def test_config_paths_resolve_independently_of_cwd():
    """Regression: CORPUS_PATH and MANIFEST_DB_PATH defaulted to cwd-relative
    strings ('../obsidian-servicenow-docs', './manifest.db'). Claude Code spawns
    the MCP server with its own cwd, so the manifest resolved to a path that did
    not exist — sqlite then created an empty database and sn_get_section /
    sn_outline returned nothing while reporting success.

    Runs config.py in a subprocess from '/' with the env overrides cleared, so
    it exercises the defaults rather than this session's environment.
    """
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items()
           if k not in ("CORPUS_PATH", "MANIFEST_DB_PATH", "VAULT_PATH")}
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(root)!r}); import config; "
         "print(config.CORPUS_PATH.is_dir(), config.MANIFEST_DB_PATH.parent.is_dir(), "
         "config.MANIFEST_DB_PATH)"],
        cwd="/", env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    corpus_ok, manifest_dir_ok, manifest = proc.stdout.split(maxsplit=2)
    assert corpus_ok == "True", "CORPUS_PATH default does not resolve from another cwd"
    assert manifest_dir_ok == "True", "MANIFEST_DB_PATH default does not resolve from another cwd"
    # The exact location is deliberately not pinned: both CORPUS_PATH and
    # MANIFEST_DB_PATH resolve through ordered candidate lists so a fresh
    # checkout and this deployment both work. What must hold is that the
    # default is absolute and cwd-independent — pinning one path would just
    # re-encode the current machine's layout into the test.
    resolved = Path(manifest.strip())
    assert resolved.is_absolute(), f"manifest default is not absolute: {resolved}"
    assert resolved.name == "manifest.db", f"unexpected manifest filename: {resolved}"
