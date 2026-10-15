"""The "brain": structured-output calls to the headless Claude Code CLI.

Security model (spec §3, §9):
- Writing tasks (edit plan, caption, critique, learner review, brief) run with NO tools
  (`--tools ""`), so a prompt-injected transcript can't execute anything.
- Only the weekly trends task gets read-only web tools (WebSearch, WebFetch).
- `--safe-mode` disables CLAUDE.md, hooks, plugins, MCP servers and skills; the process
  runs in an empty sandbox folder; `--no-session-persistence` keeps nothing on disk.
- ANTHROPIC_API_KEY is removed from the child environment so calls always use
  Yazeed's subscription login, never an API bill.
- Every answer is validated against a JSON schema (plus optional extra checks) and
  retried with the error message up to `brain.retries` times.
- A daily call cap protects the subscription from a runaway loop.

The provider is an interface: FakeBrain (tests) and ClaudeCLIProvider (production).
An Anthropic-API provider can be added later by implementing BrainProvider.run().
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess  # nosec B404 - fixed binary, list args, no shell
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import jinja2
import jsonschema

from . import db
from .log import get_logger, redact

log = get_logger("brain")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
READ_ONLY_WEB_TOOLS = ("WebSearch", "WebFetch")


class BrainError(RuntimeError):
    pass


class BrainBudgetExceeded(BrainError):
    pass


class BrainProvider(Protocol):
    name: str

    def run(self, task: str, prompt: str, schema: dict, *, model: str, timeout: int, tools: tuple[str, ...] = ()) -> dict: ...


def _version_key(s: str) -> tuple:
    return tuple(int(n) for n in s.split(".") if n.isdigit())


def _resolve_cli_mac() -> Path | None:
    """macOS fallbacks after PATH: the native installer, the old local install, Homebrew,
    /usr/local, then the newest binary bundled with the Claude desktop app."""
    home = Path.home()
    for c in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude",
              Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if c.is_file() and os.access(c, os.X_OK):
            return c
    pattern = str(home / "Library" / "Application Support" / "Claude" / "claude-code" / "*" / "*"
                  / "claude.app" / "Contents" / "MacOS" / "claude")
    cands = [Path(p) for p in glob.glob(pattern) if os.access(p, os.X_OK)]
    if not cands:
        return None
    # .../claude-code/<version>/<hash>/claude.app/Contents/MacOS/claude -> parents[4] is <version>
    return max(cands, key=lambda p: _version_key(p.parents[4].name))


def resolve_cli(cli_path: str = "") -> Path | None:
    """`claude` on PATH, else (macOS) the usual install locations, else the newest
    VS Code extension binary (spec §3)."""
    if cli_path:
        p = Path(os.path.expandvars(os.path.expanduser(cli_path)))
        return p if p.exists() else None
    found = shutil.which("claude")
    if found:
        return Path(found)
    if sys.platform == "darwin":
        mac = _resolve_cli_mac()
        if mac:
            return mac
    pattern = str(Path.home() / ".vscode" / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / "claude.exe")
    cands = [Path(p) for p in glob.glob(pattern)]
    if not cands:
        return None

    def ver(p: Path) -> tuple:
        name = p.parents[2].name  # anthropic.claude-code-2.1.283-win32-x64
        parts = name.split("-")
        nums = next((x for x in parts if x[:1].isdigit()), "0")
        return tuple(int(n) for n in nums.split(".") if n.isdigit())

    return max(cands, key=ver)


class ClaudeCLIProvider:
    name = "claude-cli"

    def __init__(self, cli: Path, sandbox: Path):
        self.cli = cli
        self.sandbox = sandbox
        sandbox.mkdir(parents=True, exist_ok=True)

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")}
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def run(self, task: str, prompt: str, schema: dict, *, model: str, timeout: int, tools: tuple[str, ...] = ()) -> dict:
        # the CLI's validator rejects meta keys like "$schema"; local validation keeps the full schema
        cli_schema = {k: v for k, v in schema.items() if k not in ("$schema", "$id")}
        args = [
            str(self.cli), "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(cli_schema, ensure_ascii=False),
            "--no-session-persistence",
            "--safe-mode",
            "--model", model,
        ]
        if tools:
            bad = set(tools) - set(READ_ONLY_WEB_TOOLS)
            if bad:
                raise BrainError(f"refusing non read-only tools for the brain: {sorted(bad)}")
            args += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
        else:
            args += ["--tools", ""]
        try:
            p = subprocess.run(  # nosec B603
                args,
                input=prompt.encode("utf-8"),
                capture_output=True,
                cwd=str(self.sandbox),
                env=self._env(),
                timeout=timeout,
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrainError(f"claude CLI timed out after {timeout}s") from exc
        out = p.stdout.decode("utf-8", "replace").strip()
        if p.returncode != 0 and not out:
            raise BrainError(f"claude CLI exit {p.returncode}: {redact(p.stderr.decode('utf-8', 'replace')[-800:])}")
        try:
            res = json.loads(out.splitlines()[-1] if out and not out.startswith("{") else out)
        except json.JSONDecodeError as exc:
            raise BrainError(f"claude CLI returned non-JSON output: {redact(out[:300])}") from exc
        if res.get("is_error") or res.get("subtype") != "success":
            raise BrainError(f"claude CLI error: {res.get('subtype')} {redact(str(res.get('result'))[:300])}")
        so = res.get("structured_output")
        if so is None:
            try:
                so = json.loads(res.get("result") or "")
            except json.JSONDecodeError as exc:
                raise BrainError("no structured_output in CLI result") from exc
        return so


class FakeBrain:
    """Deterministic provider for tests: handlers[task](prompt, schema) -> dict."""

    name = "fake"

    def __init__(self, handlers: dict[str, Callable[[str, dict], dict]]):
        self.handlers = handlers
        self.calls: list[tuple[str, str]] = []

    def run(self, task: str, prompt: str, schema: dict, *, model: str, timeout: int, tools: tuple[str, ...] = ()) -> dict:
        self.calls.append((task, prompt))
        if task not in self.handlers:
            raise BrainError(f"FakeBrain has no handler for {task}")
        return self.handlers[task](prompt, schema)


_provider_override: BrainProvider | None = None


def set_provider(p: BrainProvider | None) -> None:
    """Tests (and `reel` with a fake config) install a provider here."""
    global _provider_override
    _provider_override = p


def get_provider(cfg) -> BrainProvider:
    if _provider_override is not None:
        return _provider_override
    if cfg["brain"]["provider"] == "fake":
        raise BrainError("brain.provider = fake but no FakeBrain was installed")
    cli = resolve_cli(cfg["brain"]["cli_path"])
    if cli is None:
        raise BrainError("claude CLI not found (PATH or ~/.vscode/extensions/anthropic.claude-code-*)")
    return ClaudeCLIProvider(cli, cfg.dir("work") / "brain_sandbox")


def _env() -> jinja2.Environment:
    return jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False, keep_trailing_newline=True)  # nosec B701 - plain-text prompts, not HTML


def render_prompt(cfg, task: str, context: dict) -> tuple[str, dict]:
    pdir = cfg.prompts_dir
    tpl = (pdir / f"{task}.md").read_text(encoding="utf-8")
    schema = json.loads((pdir / f"{task}.schema.json").read_text(encoding="utf-8"))
    return _env().from_string(tpl).render(**context), schema


def calls_today(conn) -> int:
    """CLI runs in the last 24 h (every attempt counts, not just tasks: fact-check #20)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    return int(conn.execute("SELECT COALESCE(SUM(attempts), 0) FROM brain_calls WHERE ts >= ?", (since,)).fetchone()[0])


def ask(
    cfg,
    conn,
    task: str,
    context: dict,
    *,
    reel_id: int | None = None,
    model: str | None = None,
    tools: tuple[str, ...] = (),
    validate: Callable[[dict], list[str]] | None = None,
    timeout: int | None = None,
) -> dict:
    """Render prompts/<task>.md, call the provider, validate, retry; log to brain_calls."""
    cap = int(cfg["brain"]["max_calls_per_day"])
    if calls_today(conn) >= cap:
        raise BrainBudgetExceeded(f"brain call cap reached ({cap} per 24 h); try again later")
    provider = get_provider(cfg)
    prompt, schema = render_prompt(cfg, task, context)
    model = model or cfg["brain"]["model"]
    attempts = 1 + int(cfg["brain"]["retries"])
    last_err = ""
    t0 = time.time()
    for n in range(1, attempts + 1):
        p = prompt if not last_err else (
            prompt + f"\n\n---\nYour previous answer was rejected: {last_err}\nReturn a corrected answer that satisfies every rule."
        )
        try:
            out = provider.run(task, p, schema, model=model, timeout=int(timeout or cfg["brain"]["timeout_s"]), tools=tools)
            jsonschema.validate(out, schema)
            problems = validate(out) if validate else []
            if problems:
                raise BrainError("; ".join(problems))
            _log_call(conn, task, model, reel_id, time.time() - t0, n, True, None)
            return out
        except BrainBudgetExceeded:
            raise
        except (BrainError, jsonschema.ValidationError) as exc:
            last_err = redact(str(exc).splitlines()[0])[:600]
            log.warning("brain %s attempt %d/%d failed: %s", task, n, attempts, last_err)
    _log_call(conn, task, model, reel_id, time.time() - t0, attempts, False, last_err)
    raise BrainError(f"brain task {task} failed after {attempts} attempts: {last_err}")


def _log_call(conn, task: str, model: str, reel_id: int | None, dur: float, attempts: int, ok: bool, err: str | None) -> None:
    conn.execute(
        "INSERT INTO brain_calls(ts, task, model, reel_id, duration_s, attempts, ok, error) VALUES(?,?,?,?,?,?,?,?)",
        (db.now_iso(), task, model, reel_id, round(dur, 2), attempts, int(ok), err),
    )
