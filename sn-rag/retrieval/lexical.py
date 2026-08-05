"""ripgrep wrapper for exact-symbol lookup.

Dense retrieval smears exact identifiers; BM25 helps but still tokenizes. When
the user asks about `sys_user_grmember` or `gs.getUserID()`, an exact substring
match over the corpus is both faster and more precise than any vector search.
"""
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

LEXICAL_TIMEOUT_SECONDS = int(os.environ.get("LEXICAL_TIMEOUT_SECONDS", "60"))

# A query is "code-like" if it contains dotted calls, CamelCase, sys_* tables,
# or gs./gr./g_form. prefixes. Used to decide when to prefer lexical search.
CODE_LIKE_RE = re.compile(
    r"(\b[a-z_$][\w$]*\.[a-z_$][\w$]*)"      # dotted call: gs.info, gr.addQuery
    r"|(\b[A-Z][a-z0-9]+[A-Z]\w*)"            # CamelCase: GlideRecord
    r"|(\bsys_\w+)"                           # sys_user, sys_db_object
    r"|(\b(?:gs|gr|g_form|g_user|current|previous)\b)",
    re.IGNORECASE if False else 0,
)


@dataclass(frozen=True)
class LexicalHit:
    rel_path: str
    line_no: int
    line: str
    context_before: tuple
    context_after: tuple


def looks_code_like(query: str) -> bool:
    return bool(CODE_LIKE_RE.search(query))


def extract_symbols(query: str) -> list[str]:
    """Candidate identifiers in a query, for api_symbols payload filtering."""
    out: list[str] = []
    for match in CODE_LIKE_RE.finditer(query):
        token = match.group(0)
        if token and token not in out:
            out.append(token)
    return out


class LexicalSearcher:
    """ripgrep wrapper.

    Resolution order: explicit argument, `$RG_BINARY`, PATH, then the common
    user-local install dir. `shutil.which` alone is not enough — in some shells
    `rg` is a function or alias rather than a binary on PATH, which silently
    disables lexical search (and silently skips its tests).
    """

    FALLBACK_PATHS = ("~/.local/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg")

    def __init__(self, corpus_path: Path, rg_binary: Optional[str] = None):
        import os
        self.corpus_path = Path(corpus_path)
        candidate = rg_binary or os.environ.get("RG_BINARY") or "rg"
        self.rg = shutil.which(candidate)
        if self.rg is None and rg_binary is None:
            for path in self.FALLBACK_PATHS:
                expanded = Path(path).expanduser()
                if expanded.is_file() and os.access(expanded, os.X_OK):
                    self.rg = str(expanded)
                    break

    @property
    def available(self) -> bool:
        return self.rg is not None

    def search(self, pattern: str, max_hits: int = 20, context: int = 2,
               subdirs: Optional[Sequence[str]] = None,
               fixed_string: bool = False, timeout: Optional[int] = None) -> list[LexicalHit]:
        """Run ripgrep and parse hits with +/- `context` lines.

        Raises RuntimeError if ripgrep is missing or the pattern is rejected —
        never returns an empty list to paper over a failure.
        """
        if not self.available:
            raise RuntimeError("ripgrep (rg) not found on PATH")
        # Corpus filesystem dominates this call. Measured over 51,642 files:
        # ~26s on a /mnt/c 9p mount (18s of it system time) vs ~0.2s on ext4 —
        # 127x, and directory excludes do not help because traversal itself is
        # the cost. The default is generous so a slow mount degrades instead of
        # hard-failing; move the corpus to a local filesystem for the real fix.
        if timeout is None:
            timeout = LEXICAL_TIMEOUT_SECONDS

        cmd = [self.rg, "--json", "--max-count", str(max_hits),
               "--context", str(context), "--type", "md"]
        if fixed_string:
            cmd.append("--fixed-strings")
        cmd.extend(["--", pattern])
        cmd.extend([str(self.corpus_path / d) for d in subdirs] if subdirs else [str(self.corpus_path)])

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        # rg exit codes: 0 = matches, 1 = no matches, 2 = error
        if proc.returncode == 2:
            raise RuntimeError(f"ripgrep failed: {proc.stderr.strip()[:400]}")
        if proc.returncode == 1:
            return []

        return self._parse(proc.stdout, max_hits)

    def _parse(self, stdout: str, max_hits: int) -> list[LexicalHit]:
        import json
        hits: list[LexicalHit] = []
        pending_context: list[str] = []
        for raw in stdout.splitlines():
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            data = event.get("data", {})
            if kind == "context":
                pending_context.append(self._text_of(data))
                pending_context[:] = pending_context[-4:]
            elif kind == "match":
                path = data.get("path", {}).get("text", "")
                try:
                    rel = str(Path(path).resolve().relative_to(self.corpus_path.resolve()))
                except ValueError:
                    rel = path
                hits.append(LexicalHit(
                    rel_path=rel,
                    line_no=int(data.get("line_number") or 0),
                    line=self._text_of(data).rstrip("\n"),
                    context_before=tuple(pending_context[-2:]),
                    context_after=(),
                ))
                pending_context.clear()
                if len(hits) >= max_hits:
                    break
        return hits

    @staticmethod
    def _text_of(data: dict) -> str:
        lines = data.get("lines", {})
        return lines.get("text", "") if isinstance(lines, dict) else ""
