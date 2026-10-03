"""Vault writer (spec §11). Writes ONLY inside `<vault>/12 Social Media/`, only
between `<!-- reel-engine:start KEY -->` and `<!-- reel-engine:end KEY -->` markers
(text outside the blocks is Yazeed's and is never touched), never in
`10 Source Archive/`. Frontmatter follows the vault's CLAUDE.md conventions.

Until Yazeed approves (config vault.enabled = true), everything goes to
data/vault_preview/12 Social Media/ instead, so the flow is exercised safely.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import yaml

from .log import get_logger

log = get_logger("vault")
FORBIDDEN_PARTS = ("10 Source Archive",)


class VaultError(Exception):
    pass


def root(cfg) -> Path:
    sub = cfg["vault"]["subdir"]
    if cfg["vault"]["enabled"]:
        return Path(cfg["vault"]["path"]) / sub
    return cfg.dir("vault_preview") / sub


def _safe_path(cfg, rel: str) -> Path:
    if any(p in rel for p in FORBIDDEN_PARTS) or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise VaultError(f"refusing vault path {rel!r}")
    base = root(cfg).resolve()
    p = (base / rel).resolve()
    if base not in p.parents and p != base:
        raise VaultError(f"{rel!r} escapes {base}")
    if p.suffix != ".md":
        raise VaultError("vault notes must be .md")
    return p


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reel-", suffix=".md", dir=str(p.parent))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, p)


def _block(key: str, content: str) -> str:
    return f"<!-- reel-engine:start {key} -->\n{content.rstrip()}\n<!-- reel-engine:end {key} -->"


def write_block(cfg, rel: str, key: str, content: str, *, title: str, frontmatter: dict, intro: str = "") -> Path:
    """Create the note (frontmatter + title + block) or replace only the named block."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", key):
        raise VaultError("bad block key")
    p = _safe_path(cfg, rel)
    blk = _block(key, content)
    if p.exists():
        text = p.read_text(encoding="utf-8")
        pat = re.compile(rf"<!-- reel-engine:start {re.escape(key)} -->.*?<!-- reel-engine:end {re.escape(key)} -->", re.S)
        text = pat.sub(lambda _m: blk, text, count=1) if pat.search(text) else text.rstrip() + "\n\n" + blk + "\n"
    else:
        fm = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        text = f"---\n{fm}\n---\n# {title}\n\n" + (intro.strip() + "\n\n" if intro else "") + blk + "\n"
    _atomic_write(p, text)
    return p


def append_log_line(cfg, line: str) -> Path:
    """Append one line to the generated block of `Reels Log.md` (newest first)."""
    rel = "Reels Log.md"
    p = _safe_path(cfg, rel)
    lines: list[str] = []
    if p.exists():
        m = re.search(r"<!-- reel-engine:start log -->\n(.*?)\n<!-- reel-engine:end log -->", p.read_text(encoding="utf-8"), re.S)
        if m:
            lines = [ln for ln in m.group(1).splitlines() if ln.strip()]
    lines.insert(0, line.strip())
    return write_block(cfg, rel, "log", "\n".join(lines[:2000]), title="Reels Log",
                       frontmatter={"type": "social-log", "tags": ["social-media", "reels", "reel-engine"], "generated_by": "Reel Engine"},
                       intro="Every reel the engine published or rehearsed (dry-run). Generated; edit outside the block only.")
