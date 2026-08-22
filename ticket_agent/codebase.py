"""Codebase lookup: build a file tree (local dir or GitHub repo), pick relevant files, read them.

Produces a compact 'code context' string the analyze/ask prompts use to ground questions
in real files, plus a list of suspected files for the brief.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import requests

IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", "dist", "build", ".hallmark"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".html", ".css", ".md", ".toml", ".yaml", ".yml", ".json"}
MAX_FILE_CHARS = 6000
MAX_FILES = 12


@dataclass
class Codebase:
    root: str                                  # local path or "owner/repo[@ref]"
    files: list[str] = field(default_factory=list)
    _cache: dict[str, str] = field(default_factory=dict)

    # ---- construction ----
    @classmethod
    def open(cls, spec: str | None) -> "Codebase | None":
        """spec: local directory path, or 'owner/repo' / 'owner/repo@branch' on GitHub."""
        if not spec:
            return None
        if os.path.isdir(spec):
            cb = cls(root=spec)
            cb.files = cb._walk_local()
        else:
            cb = cls(root=spec)
            cb.files = cb._walk_github()
        return cb

    def _walk_local(self) -> list[str]:
        out = []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for f in fns:
                if Path(f).suffix in CODE_EXT:
                    out.append(os.path.relpath(os.path.join(dp, f), self.root))
        return sorted(out)

    def _gh(self, url: str):
        headers = {"Accept": "application/vnd.github+json"}
        if tok := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {tok}"
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def _walk_github(self) -> list[str]:
        repo, _, ref = self.root.partition("@")
        if not ref:
            ref = self._gh(f"https://api.github.com/repos/{repo}")["default_branch"]
        self._ref = ref
        tree = self._gh(f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1")["tree"]
        return sorted(
            t["path"] for t in tree
            if t["type"] == "blob" and Path(t["path"]).suffix in CODE_EXT
            and not any(part in IGNORE_DIRS for part in Path(t["path"]).parts)
        )

    # ---- reading ----
    def read(self, path: str) -> str:
        if path in self._cache:
            return self._cache[path]
        if os.path.isdir(self.root):
            text = Path(self.root, path).read_text(errors="replace")
        else:
            repo, _, _ = self.root.partition("@")
            r = requests.get(f"https://raw.githubusercontent.com/{repo}/{self._ref}/{path}", timeout=30)
            r.raise_for_status()
            text = r.text
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n... [truncated, {len(text)} chars total]"
        self._cache[path] = text
        return text

    def tree_text(self) -> str:
        return "\n".join(self.files)

    def context_text(self, paths: list[str]) -> str:
        blocks = []
        for p in paths[:MAX_FILES]:
            if p in self.files:
                blocks.append(f"===== {p} =====\n{self.read(p)}")
        return "\n\n".join(blocks)
