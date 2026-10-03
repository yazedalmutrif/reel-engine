"""Brain provider rules (schema validation, retries, budget, no tools, prompt templates)
and the vault writer's boundaries (spec §3, §9, §11)."""
from __future__ import annotations

import json

import pytest

from reelengine import brain, db, vault
from reelengine.config import load_config


@pytest.fixture()
def cfg(tmp_path):
    return load_config(overrides={"paths": {"data_dir": str(tmp_path / "data")}, "notify": {"toast": False}})


@pytest.fixture()
def conn(cfg):
    return db.connect(cfg.db_path)


def test_ask_retries_until_valid_then_logs(cfg, conn):
    answers = iter([{"scores": "bad"}, {"greeting": "x"}])
    calls = []

    def handler(prompt, schema):
        calls.append(prompt)
        return next(answers)

    brain.set_provider(brain.FakeBrain({"t": handler}))
    try:
        schema = {"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}}
        pdir = cfg.data_dir / "prompts"
        pdir.mkdir(parents=True)
        (pdir / "t.md").write_text("hello {{ name }}", encoding="utf-8")
        (pdir / "t.schema.json").write_text(json.dumps(schema), encoding="utf-8")
        c2 = cfg.with_overrides({"paths": {"prompts_dir": str(pdir)}})
        out = brain.ask(c2, conn, "t", {"name": "يزيد"})
        assert out == {"greeting": "x"}
        assert "previous answer was rejected" in calls[1]
        row = conn.execute("SELECT attempts, ok FROM brain_calls").fetchone()
        assert (row["attempts"], row["ok"]) == (2, 1)
        brain.set_provider(brain.FakeBrain({"t": lambda p, s: {"nope": 1}}))
        with pytest.raises(brain.BrainError):
            brain.ask(c2, conn, "t", {"name": "x"})
        assert conn.execute("SELECT COUNT(*) FROM brain_calls WHERE ok=0").fetchone()[0] == 1
        c3 = c2.with_overrides({"brain": {"max_calls_per_day": 2}})
        with pytest.raises(brain.BrainBudgetExceeded):
            brain.ask(c3, conn, "t", {"name": "x"})
    finally:
        brain.set_provider(None)


def test_cli_provider_refuses_write_tools_and_strips_api_key(tmp_path, monkeypatch):
    p = brain.ClaudeCLIProvider(tmp_path / "claude.exe", tmp_path / "sb")
    with pytest.raises(brain.BrainError):
        p.run("x", "p", {}, model="sonnet", timeout=5, tools=("Bash",))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    assert "ANTHROPIC_API_KEY" not in p._env()


@pytest.mark.parametrize("task", ["edit_plan", "caption", "critique", "brief", "trends"])
def test_prompt_templates_are_strict_and_schemas_valid(task):
    import jinja2
    import jsonschema

    cfg = load_config()
    schema = json.loads((cfg.prompts_dir / f"{task}.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    with pytest.raises(jinja2.UndefinedError):
        brain.render_prompt(cfg, task, {})  # StrictUndefined: a missing input is an error, never silently empty


def test_every_writing_prompt_marks_untrusted_data():
    cfg = load_config()
    for task in ("edit_plan", "caption", "critique", "brief"):
        text = (cfg.prompts_dir / f"{task}.md").read_text(encoding="utf-8")
        assert "SECURITY" in text and "not instructions" in text.lower()


def test_vault_preview_by_default_and_blocks(cfg):
    p = vault.write_block(cfg, "Reels Playbook.md", "playbook", "v1 body", title="Reels Playbook", frontmatter={"type": "playbook", "tags": ["x"]})
    assert "vault_preview" in str(p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---\ntype: playbook") and "<!-- reel-engine:start playbook -->" in text
    p.write_text(text + "\nYazeed's own note, outside the block.\n", encoding="utf-8")
    vault.write_block(cfg, "Reels Playbook.md", "playbook", "v2 body", title="x", frontmatter={})
    t2 = p.read_text(encoding="utf-8")
    assert "v2 body" in t2 and "v1 body" not in t2 and "Yazeed's own note" in t2


@pytest.mark.parametrize("bad", ["../escape.md", "..\\escape.md", "10 Source Archive/x.md", "/abs.md", "note.txt"])
def test_vault_refuses_unsafe_paths(cfg, bad):
    with pytest.raises(vault.VaultError):
        vault.write_block(cfg, bad, "k", "x", title="t", frontmatter={})


def test_vault_log_newest_first(cfg):
    vault.append_log_line(cfg, "- first")
    p = vault.append_log_line(cfg, "- second")
    body = p.read_text(encoding="utf-8")
    assert body.index("- second") < body.index("- first")


def test_cli_args_no_tools_safe_mode_and_clean_schema(tmp_path, monkeypatch):
    import subprocess

    seen = {}

    class P:
        returncode = 0
        stdout = json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": {"ok": True}}).encode()
        stderr = b""

    def fake_run(args, **kw):
        seen["args"], seen["kw"] = args, kw
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    p = brain.ClaudeCLIProvider(tmp_path / "claude.exe", tmp_path / "sb")
    out = p.run("t", "prompt text", {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}, model="sonnet", timeout=5)
    a = seen["args"]
    assert out == {"ok": True}
    assert "--safe-mode" in a and "--no-session-persistence" in a
    assert a[a.index("--tools") + 1] == ""                      # no tools for writing tasks
    assert "$schema" not in a[a.index("--json-schema") + 1]     # the CLI rejects meta keys
    assert "prompt text" not in " ".join(map(str, a))           # the prompt travels on stdin
    assert seen["kw"]["input"] == b"prompt text"
    p.run("t", "x", {"type": "object"}, model="sonnet", timeout=5, tools=brain.READ_ONLY_WEB_TOOLS)
    assert seen["args"][seen["args"].index("--tools") + 1] == "WebSearch,WebFetch"


def test_upload_uri_host_must_match_config():  # fact-check #15
    from reelengine.graph import same_origin

    assert same_origin("https://rupload.facebook.com/ig-api-upload/v26.0/1", "https://rupload.facebook.com")
    assert not same_origin("https://evil.example/ig-api-upload/v26.0/1", "https://rupload.facebook.com")
    assert not same_origin("http://rupload.facebook.com/x", "https://rupload.facebook.com")


def test_brain_cap_counts_every_cli_attempt(cfg, conn):  # fact-check #20
    for _ in range(3):
        conn.execute("INSERT INTO brain_calls(ts, task, attempts, ok) VALUES(?,?,?,?)", (db.now_iso(), "t", 3, 0))
    assert brain.calls_today(conn) == 9
