"""Hierarchical markdown chunker. PURE FUNCTIONS ONLY — no file I/O, no network.

Design invariants (enforced by tests/test_chunker.py):
  1. Parent chunks tile the document body exactly: concatenating them in order
     reproduces the body verbatim. Parents never overlap.
  2. No chunk boundary — parent or child — ever falls inside a fenced code
     block, a pipe table, or an HTML <table>. These are ATOMIC blocks.
  3. An atomic block larger than the size cap is emitted oversized rather than
     split. A bisected code example is worse than a large chunk.
  4. Children carry the heading breadcrumb of the block they came from, which
     may be deeper than their parent's breadcrumb (parents merge sections).
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(\S*)")
TABLE_DELIM_RE = re.compile(r"^ {0,3}\|?[ \t:|-]*-[ \t:|-]*\|?[ \t]*$")
HTML_TABLE_OPEN_RE = re.compile(r"<table\b", re.IGNORECASE)
HTML_TABLE_CLOSE_RE = re.compile(r"</table>", re.IGNORECASE)

ATOMIC_KINDS = frozenset({"code", "table", "html_table"})

# Identifier extraction
DOTTED_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+")
CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")
HEADING_METHOD_RE = re.compile(r"\b([a-z][A-Za-z0-9_]*)\\?\(")

# Trailing segments that mark a dotted token as a URL/filename, not an API symbol.
_NON_SYMBOL_TAILS = frozenset({
    "com", "org", "net", "io", "gov", "edu", "co", "uk",
    "html", "htm", "md", "xml", "json", "csv", "xlsx", "pdf", "txt",
    "png", "jpg", "jpeg", "gif", "svg", "zip", "do", "js", "css",
})
_NOISE_SYMBOLS = frozenset({
    "function", "return", "var", "let", "const", "if", "else", "for", "while",
    "true", "false", "null", "undefined", "new", "this", "typeof", "class",
    "String", "Number", "Boolean", "Object", "Array", "JSON", "Math", "Date",
})


@dataclass(frozen=True)
class Block:
    kind: str            # heading | code | table | html_table | text
    text: str            # verbatim source slice, including line endings
    h_path: str          # heading breadcrumb in effect for this block
    level: Optional[int] = None   # heading level, headings only


@dataclass(frozen=True)
class ParentChunk:
    parent_id: str
    rel_path: str
    parent_idx: int
    h_path: str
    text: str
    api_symbols: tuple = field(default=())


@dataclass(frozen=True)
class ChildChunk:
    chunk_id: str
    parent_id: str
    rel_path: str
    parent_idx: int
    child_idx: int
    h_path: str
    text: str


def parse_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml_or_empty, body). Does not parse the YAML."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~])")


def unescape_markdown(text: str) -> str:
    """Strip backslash escapes. h_path is prepended to embedded text, so
    `addQuery\\(String\\)` must read as `addQuery(String)`, not as noise."""
    return MD_ESCAPE_RE.sub(r"\1", text)


def _breadcrumb(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def tokenize_blocks(body: str) -> list[Block]:
    """Split body into typed blocks. Concatenating block.text reproduces body."""
    lines = body.splitlines(keepends=True)
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    def flush_text(buf: list[str]):
        if buf:
            blocks.append(Block(kind="text", text="".join(buf), h_path=_breadcrumb(stack)))
            buf.clear()

    text_buf: list[str] = []
    while i < n:
        line = lines[i]

        fence = FENCE_OPEN_RE.match(line)
        if fence:
            flush_text(text_buf)
            marker = fence.group(1)
            close_re = re.compile(r"^ {0,3}" + marker[0] + "{" + str(len(marker)) + r",}\s*$")
            j = i + 1
            while j < n and not close_re.match(lines[j]):
                j += 1
            j = min(j + 1, n)  # include closing fence if present
            blocks.append(Block(kind="code", text="".join(lines[i:j]), h_path=_breadcrumb(stack)))
            i = j
            continue

        if HTML_TABLE_OPEN_RE.search(line):
            flush_text(text_buf)
            depth = len(HTML_TABLE_OPEN_RE.findall(line)) - len(HTML_TABLE_CLOSE_RE.findall(line))
            j = i + 1
            while j < n and depth > 0:
                depth += len(HTML_TABLE_OPEN_RE.findall(lines[j])) - len(HTML_TABLE_CLOSE_RE.findall(lines[j]))
                j += 1
            blocks.append(Block(kind="html_table", text="".join(lines[i:j]), h_path=_breadcrumb(stack)))
            i = j
            continue

        if "|" in line and i + 1 < n and TABLE_DELIM_RE.match(lines[i + 1]) and "|" in lines[i + 1]:
            flush_text(text_buf)
            j = i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                j += 1
            blocks.append(Block(kind="table", text="".join(lines[i:j]), h_path=_breadcrumb(stack)))
            i = j
            continue

        heading = HEADING_RE.match(line)
        if heading:
            flush_text(text_buf)
            level = len(heading.group(1))
            title = unescape_markdown(heading.group(2).strip())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            blocks.append(Block(kind="heading", text=line, h_path=_breadcrumb(stack), level=level))
            i += 1
            continue

        text_buf.append(line)
        i += 1

    flush_text(text_buf)
    return blocks


def group_sections(blocks: list[Block], section_max_level: int = 3) -> list[list[Block]]:
    """Group blocks into sections, each starting at an H1-H3 heading."""
    sections: list[list[Block]] = []
    current: list[Block] = []
    for block in blocks:
        if block.kind == "heading" and block.level is not None and block.level <= section_max_level:
            if current:
                sections.append(current)
            current = [block]
        else:
            current.append(block)
    if current:
        sections.append(current)
    return sections


def _blocks_len(blocks: list[Block]) -> int:
    return sum(len(b.text) for b in blocks)


def _split_oversized_section(section: list[Block], max_chars: int) -> list[list[Block]]:
    """Split one too-large section at block boundaries. Atomic blocks stay whole."""
    out: list[list[Block]] = []
    current: list[Block] = []
    for block in section:
        if current and _blocks_len(current) + len(block.text) > max_chars:
            out.append(current)
            current = []
        current.append(block)
    if current:
        out.append(current)
    return out


def build_parents_with_blocks(blocks: list[Block], rel_path: str, min_chars: int,
                              max_chars: int) -> list[tuple[ParentChunk, list[Block]]]:
    """Merge small sections up toward min_chars; split sections over max_chars.

    Returns each parent alongside the blocks it was built from, so children can
    reuse the original heading breadcrumbs instead of re-deriving them from the
    parent's text (which would lose any heading context above the parent).
    """
    groups: list[list[Block]] = []
    pending: list[Block] = []
    for section in group_sections(blocks):
        if _blocks_len(section) > max_chars:
            if pending:
                groups.append(pending)
                pending = []
            groups.extend(_split_oversized_section(section, max_chars))
            continue
        if pending and _blocks_len(pending) + _blocks_len(section) > max_chars:
            groups.append(pending)
            pending = []
        pending.extend(section)
        if _blocks_len(pending) >= min_chars:
            groups.append(pending)
            pending = []
    if pending:
        groups.append(pending)

    out: list[tuple[ParentChunk, list[Block]]] = []
    for idx, group in enumerate(groups):
        parent = ParentChunk(
            parent_id=make_parent_id(rel_path, idx),
            rel_path=rel_path,
            parent_idx=idx,
            h_path=group[0].h_path,
            text="".join(b.text for b in group),
            api_symbols=extract_api_symbols(group),
        )
        out.append((parent, group))
    return out


def build_parents(blocks: list[Block], rel_path: str, min_chars: int, max_chars: int) -> list[ParentChunk]:
    return [p for p, _ in build_parents_with_blocks(blocks, rel_path, min_chars, max_chars)]


def _split_text_into_units(text: str, max_chars: int) -> list[str]:
    """Divide free text into units no larger than max_chars, at safe boundaries."""
    if len(text) <= max_chars:
        return [text]
    units: list[str] = []
    for para in re.split(r"(?<=\n\n)", text):
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                units.append(sentence)
                continue
            words, buf = sentence.split(" "), ""
            for word in words:
                candidate = f"{buf} {word}" if buf else word
                if len(candidate) > max_chars and buf:
                    units.append(buf)
                    buf = word
                else:
                    buf = candidate
            if buf:
                units.append(buf)
    return units or [text]


def _overlap_tail(text: str, overlap: int) -> str:
    """Last `overlap` chars of text, trimmed to a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def build_children(parent: ParentChunk, blocks: list[Block], target_chars: int, overlap: int) -> list[ChildChunk]:
    """Pack blocks into ~target_chars children. Atomic blocks are never split."""
    units: list[tuple[str, str, bool]] = []  # (text, h_path, is_atomic)
    for block in blocks:
        if block.kind in ATOMIC_KINDS:
            units.append((block.text, block.h_path, True))
        else:
            for piece in _split_text_into_units(block.text, target_chars):
                units.append((piece, block.h_path, False))

    children: list[ChildChunk] = []
    buf: list[str] = []
    buf_h_path: Optional[str] = None
    carry = ""

    def flush():
        nonlocal buf, buf_h_path, carry
        if not buf:
            return
        text = carry + "".join(buf)
        idx = len(children)
        children.append(ChildChunk(
            chunk_id=make_chunk_id(parent.rel_path, parent.parent_idx, idx),
            parent_id=parent.parent_id,
            rel_path=parent.rel_path,
            parent_idx=parent.parent_idx,
            child_idx=idx,
            h_path=buf_h_path or parent.h_path,
            text=text,
        ))
        carry = "" if last_was_atomic else _overlap_tail("".join(buf), overlap)
        buf, buf_h_path = [], None

    last_was_atomic = False
    for text, h_path, is_atomic in units:
        current_len = len(carry) + sum(len(t) for t in buf)
        if buf and current_len + len(text) > target_chars:
            flush()
        # Take the breadcrumb of the first *substantive* unit. A blank-line
        # remnant left over from the previous section must not pin the
        # breadcrumb to a heading the chunk's actual content doesn't belong to.
        if buf_h_path is None or (text.strip() and not "".join(buf).strip()):
            buf_h_path = h_path
        buf.append(text)
        last_was_atomic = is_atomic
        if is_atomic or len(carry) + sum(len(t) for t in buf) >= target_chars:
            flush()
    flush()
    return children


def extract_api_symbols(blocks: list[Block]) -> tuple:
    """Identifiers from fenced code and reference-style headings. Domain-agnostic."""
    symbols: set[str] = set()
    for block in blocks:
        if block.kind == "code":
            for match in DOTTED_RE.findall(block.text):
                if _is_symbol(match):
                    symbols.add(match)
            for match in CAMEL_RE.findall(block.text):
                if match not in _NOISE_SYMBOLS:
                    symbols.add(match)
        elif block.kind == "heading":
            for match in HEADING_METHOD_RE.findall(block.text):
                if match not in _NOISE_SYMBOLS:
                    symbols.add(match)
            for match in CAMEL_RE.findall(block.text):
                if match not in _NOISE_SYMBOLS:
                    symbols.add(match)
    return tuple(sorted(symbols))


def _is_symbol(dotted: str) -> bool:
    segments = dotted.split(".")
    if segments[-1].lower() in _NON_SYMBOL_TAILS:
        return False
    if any(seg in _NOISE_SYMBOLS for seg in segments):
        return False
    return True


def make_parent_id(rel_path: str, parent_idx: int) -> str:
    return hashlib.sha1(f"{rel_path}::{parent_idx}".encode()).hexdigest()


def make_chunk_id(rel_path: str, parent_idx: int, child_idx: int) -> str:
    return hashlib.sha1(f"{rel_path}::{parent_idx}::{child_idx}".encode()).hexdigest()


def chunk_document(text: str, rel_path: str, min_chars: int, max_chars: int,
                   child_chars: int, child_overlap: int) -> tuple[list[ParentChunk], list[ChildChunk]]:
    """Full pipeline for one document. Returns (parents, children)."""
    _, body = parse_frontmatter(text)
    blocks = tokenize_blocks(body)
    pairs = build_parents_with_blocks(blocks, rel_path, min_chars, max_chars)

    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []
    for parent, parent_blocks in pairs:
        parents.append(parent)
        children.extend(build_children(parent, parent_blocks, child_chars, child_overlap))
    return parents, children
