"""`reel doctor [--live] [--probe-container]`: health checks (spec §12).

Offline: config, ffmpeg (+libass/harfbuzz/fribidi), ffprobe, fonts, CUDA, whisper model,
claude CLI, keyring backend + secret names present, inbox, disk, DB integrity,
scheduled tasks, mode / LIVE-OK, vault target.
--live (read-only Instagram calls): token valid (/me) with the configured auth style
(and the other style, to settle VERIFY), account_type, publishing quota, recent media,
per-metric insights support + the reels_skip_rate unit, token expiry.
--probe-container: ALSO creates one unpublished Reel container on each candidate host
(graph.instagram.com / graph.facebook.com) to settle the container-host VERIFY; it is
never uploaded or published and expires by itself after 24 h.
"""
from __future__ import annotations

import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import db, secrets, tokens
from .brain import resolve_cli
from .config import live_ok_recorded, validate
from .ffmpeg import run as run_bin
from .ffmpeg import setup_hint
from .graph import GraphClient, GraphError

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _add(out: list, name: str, status: str, detail: str = "") -> None:
    out.append({"check": name, "status": status, "detail": detail})


REQUIRED_FFMPEG_FLAGS = ("--enable-libass", "--enable-libharfbuzz", "--enable-libx264", "--enable-libzimg")
_PROBE_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,0,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,\u0645\u0631\u062d\u0628\u0627
"""


def fribidi_probe(ffmpeg_bin: Path, timeout: float = 30) -> tuple[bool, str]:
    """Render one frame through the `ass` filter at -v verbose and look for libass's
    "Shaper: FriBidi ..." line (builds that link FriBidi inside libass have no
    --enable-libfribidi flag). Temp dir, list args, timeout."""
    with tempfile.TemporaryDirectory(prefix="reel-fribidi-") as td:
        (Path(td) / "probe.ass").write_text(_PROBE_ASS, encoding="utf-8")
        p = run_bin(ffmpeg_bin, ["-hide_banner", "-nostdin", "-v", "verbose", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
                                 "-vf", "ass=probe.ass", "-frames:v", "1", "-f", "null", "-"], cwd=Path(td), timeout=timeout, check=False)
    err = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else str(p.stderr or "")
    line = next((ln.strip() for ln in err.splitlines() if "Shaper:" in ln and "FriBidi" in ln), "")
    return (bool(line), line[line.find("Shaper:"):][:120] if line else "no 'Shaper: FriBidi' line from libass")


def ffmpeg_check(cfg) -> tuple[str, str]:
    v = run_bin(cfg.ffmpeg, ["-hide_banner", "-version"], timeout=20).stdout.decode(errors="replace")
    need = [f for f in REQUIRED_FFMPEG_FLAGS if f not in v]
    if "--enable-libfribidi" in v:
        fb_ok, fb = True, "FriBidi: --enable-libfribidi"
    else:
        try:
            fb_ok, detail = fribidi_probe(cfg.ffmpeg)
        except Exception as exc:  # noqa: BLE001
            fb_ok, detail = False, f"probe failed: {str(exc)[:120]}"
        fb = f"FriBidi via libass ({detail})" if fb_ok else f"FriBidi missing ({detail})"
    detail = v.splitlines()[0][:80] + f"; {fb}" + (f"; missing {need}" if need else "")
    return ("ok" if not need and fb_ok else "fail"), detail


def offline_checks(cfg, conn) -> list[dict]:
    out: list[dict] = []
    errs = validate(cfg.data)
    _add(out, "config", "ok" if not errs else "fail", "; ".join(errs) or str(cfg.source))
    _add(out, "os", "ok", f"{platform.platform()} ({platform.machine()})")
    try:
        _add(out, "ffmpeg", *ffmpeg_check(cfg))
    except Exception as exc:  # noqa: BLE001
        _add(out, "ffmpeg", "fail", str(exc)[:200])
    _add(out, "ffprobe", "ok" if cfg.ffprobe.exists() else "fail", str(cfg.ffprobe))
    fonts = list(cfg.fonts_dir.glob("IBMPlexSansArabic-*.ttf"))
    _add(out, "fonts", "ok" if len(fonts) >= 4 else "fail", f"{len(fonts)} IBM Plex Sans Arabic files")
    from .transcribe import cuda_available

    if sys.platform == "darwin":
        _add(out, "cuda", *_mac_cpu_check(cfg))
    else:
        ok, detail = cuda_available()
        _add(out, "cuda", "ok" if ok else "warn", detail + ("" if ok else " (CPU int8 fallback will be used)"))
    mp = cfg.resolve(cfg["whisper"]["model_path"])
    _add(out, "whisper_model", "ok" if (mp / "model.bin").exists() else "fail", str(mp))
    cli = resolve_cli(cfg["brain"]["cli_path"])
    if cli:
        try:
            p = subprocess.run([str(cli), "--version"], capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)  # nosec B603
            _add(out, "claude_cli", "ok" if p.returncode == 0 else "fail", f"{p.stdout.decode(errors='replace').strip()} at {cli}")
        except Exception as exc:  # noqa: BLE001
            _add(out, "claude_cli", "fail", str(exc)[:200])
    else:
        where = ("PATH, ~/.local/bin, ~/.claude/local, /opt/homebrew/bin, /usr/local/bin or the Claude app"
                 if sys.platform == "darwin" else "PATH or ~/.vscode/extensions")
        _add(out, "claude_cli", "fail", f"not found on {where} (or set brain.cli_path)")
    _add(out, "keyring", "ok", secrets.backend_name())
    has_tok = secrets.has_secret(secrets.ACCESS_TOKEN)
    _add(out, "secret:ig_access_token", "ok" if has_tok else "warn", "present" if has_tok else "missing (run `reel connect` after account setup)")
    st = tokens.state(conn)
    left = tokens.days_left(conn)
    tstat = st.get("status") or "missing"
    _add(out, "token_state", "ok" if tstat == "ok" and (left or 0) > 7 else ("fail" if tstat == "auth_needed" else "warn"),
         f"{tstat}; expires in {left} days" if left is not None else tstat)
    _add(out, "inbox", "ok" if cfg.inbox.exists() else "warn", str(cfg.inbox) + ("" if cfg.inbox.exists() else f" (missing: create it or run {setup_hint()})"))
    free = shutil.disk_usage(cfg.data_dir if cfg.data_dir.exists() else cfg.root).free / 1e9
    _add(out, "disk", "ok" if free >= cfg["backup"]["min_free_gb"] else "warn", f"{free:.0f} GB free")
    qc = conn.execute("PRAGMA quick_check").fetchone()[0]
    _add(out, "database", "ok" if qc == "ok" else "fail", f"{cfg.db_path} quick_check={qc}")
    if sys.platform == "darwin":
        from . import macsched

        ts = macsched.status()
        missing = [n for n, s in ts.items() if not s.get("exists")]
        tzw = macsched.tz_warning(cfg)
        detail = ("all loaded in launchd" if not missing else
                  f"not loaded: {', '.join(missing)} (reel schedule plist / install --mac after Yazeed's OK)")
        _add(out, "scheduled_tasks", "ok" if not missing and not tzw else "warn", detail + (f"; {tzw}" if tzw else ""))
    else:
        from .winsched import tasks_status

        ts = tasks_status()
        missing = [n for n, s in ts.items() if not s.get("exists")]
        _add(out, "scheduled_tasks", "ok" if not missing else "warn",
             "all registered" if not missing else f"not registered: {', '.join(missing)} (run scripts/register-tasks.ps1 after Yazeed's OK)")
    _add(out, "desktop_notifications", *_desktop_check(cfg))
    _add(out, "telegram", *_telegram_check(cfg))
    lo = live_ok_recorded(cfg)
    _add(out, "mode", "ok", f"{cfg.mode}; paused={cfg.pause_file.exists()}; LIVE-OK {'recorded' if lo else 'not recorded'}")
    vp = Path(cfg["vault"]["path"])
    _add(out, "vault", "ok" if vp.exists() else "warn",
         f"{'writes to the vault' if cfg['vault']['enabled'] else 'preview only (data/vault_preview)'}; vault path {'found' if vp.exists() else 'missing'}")
    return out


def _mac_cpu_check(cfg) -> tuple[str, str]:
    ct = cfg["whisper"]["cpu_compute_type"]
    try:
        import ctranslate2

        types = ctranslate2.get_supported_compute_types("cpu")
    except Exception as exc:  # noqa: BLE001
        return "warn", f"n/a on macOS; ctranslate2 check failed: {str(exc)[:120]}"
    msg = "n/a on macOS: CPU int8 (ctranslate2 has no Metal backend)"
    return ("ok", msg) if ct in types else ("warn", f"{msg}; cpu_compute_type {ct!r} not in {sorted(types)}")


def _desktop_check(cfg) -> tuple[str, str]:
    on = cfg["notify"]["toast"]
    if sys.platform == "darwin":
        from .notify import OSASCRIPT

        if not on:
            return "ok", "off ([notify] toast = false)"
        return ("ok", f"macOS notifications via {OSASCRIPT}") if Path(OSASCRIPT).exists() else ("warn", f"{OSASCRIPT} not found")
    if sys.platform == "win32":
        return "ok", "Windows toast" + ("" if on else " (off: [notify] toast = false)")
    return "ok", "no desktop channel on this OS (dashboard only)"


def _telegram_check(cfg) -> tuple[str, str]:
    from .notify import telegram_state

    state, detail = telegram_state(cfg)
    return ("ok" if state in ("off", "ready") else "warn"), detail


def live_checks(cfg, conn, probe_container: bool = False, client: GraphClient | None = None) -> list[dict]:
    out: list[dict] = []
    if not secrets.has_secret(secrets.ACCESS_TOKEN):
        _add(out, "live", "fail", "no token in keyring; run `reel connect`")
        return out
    c = client or tokens.client(cfg, conn)
    try:
        me = c.me()
        _add(out, f"auth_mode={cfg['instagram']['auth_mode']}", "ok", f"/me ok: @{me.get('username')} user_id {me.get('user_id') or me.get('id')}")
        at = me.get("account_type")
        _add(out, "account_type", "ok" if at in ("BUSINESS", "MEDIA_CREATOR", "CREATOR") else "warn",
             f"{at} (Business or Creator both work with Instagram Login; if publishing is refused, check Settings > Account type)")
    except GraphError as exc:
        _add(out, f"auth_mode={cfg['instagram']['auth_mode']}", "fail", str(exc)[:200])
        if exc.kind == "auth":
            return out
    other = "query" if cfg["instagram"]["auth_mode"] == "header" else "header"
    try:
        alt = GraphClient(cfg.with_overrides({"instagram": {"auth_mode": other}}), conn, c._token)
        alt.me()
        _add(out, f"auth_mode={other}", "ok", "also works (VERIFY settled: both styles accepted)")
    except GraphError as exc:
        _add(out, f"auth_mode={other}", "warn", f"not accepted: {exc.kind}")
    uid = tokens.ig_user_id(cfg, conn)
    try:
        usage, total = c.publishing_limit(uid)
        _add(out, "publishing_quota", "ok" if total > 0 else "warn", f"{usage}/{total} used in 24 h (never hardcoded; checked before each publish)")
    except GraphError as exc:
        _add(out, "publishing_quota", "fail", str(exc)[:200])
    try:
        media = c.recent_media(uid, limit=5)
        _add(out, "recent_media", "ok", f"{len(media)} recent items")
        reel = next((m for m in media if m.get("media_product_type") == "REELS"), None)
        if reel:
            for group in cfg["instagram"]["media_metric_groups"]:
                for m in group:
                    try:
                        from .graph import parse_metric_values

                        v = parse_metric_values(c.media_insights(reel["id"], [m]), [m])[m]
                        _add(out, f"metric:{m}", "ok", f"value {v}" + (" (unit check: >1 means percent)" if m == "reels_skip_rate" and v is not None else ""))
                    except GraphError as exc:
                        _add(out, f"metric:{m}", "warn", f"not supported: {str(exc)[:120]}")
        else:
            _add(out, "metrics", "warn", "no Reel published yet; metric names are checked after the first live post")
    except GraphError as exc:
        _add(out, "recent_media", "fail", str(exc)[:200])
    left = tokens.days_left(conn)
    _add(out, "token_expiry", "ok" if left and left > 7 else "warn", f"{left} days left (refreshed weekly when >= 7 days old)")
    if probe_container:
        out += probe_containers(cfg, conn, c, uid)
    return out


def probe_containers(cfg, conn, c: GraphClient, uid: str) -> list[dict]:
    """Settle two VERIFY items with unpublished test containers: which host accepts Reel
    containers, and whether the `is_ai_generated` parameter is accepted (on an error with
    code 100, retry once without it and report which variant Meta accepted)."""
    out: list[dict] = []
    for host in dict.fromkeys([cfg["instagram"]["container_host"], "https://graph.instagram.com", "https://graph.facebook.com"]):
        variants = [("with is_ai_generated", True), ("without is_ai_generated", False)] if cfg["instagram"]["send_is_ai_generated"] else [("without is_ai_generated", False)]
        for label, send_flag in variants:
            try:
                cc = GraphClient(cfg.with_overrides({"instagram": {"container_host": host, "send_is_ai_generated": send_flag}}), conn, c._token)
                r = cc.create_reel_container(uid, "Reel Engine doctor probe (never published)", 0)
                _add(out, f"container_host={host}", "ok", f"accepted {label}: container {r.get('id')} (unpublished; expires in 24 h)")
                if send_flag is False and cfg["instagram"]["send_is_ai_generated"]:
                    _add(out, "is_ai_generated", "warn", "rejected with code 100 but accepted without it: set instagram.send_is_ai_generated = false")
                break
            except GraphError as exc:
                if send_flag and exc.code == 100:
                    continue  # retry once without the parameter
                _add(out, f"container_host={host}", "warn", f"{label}: {exc.kind}: {str(exc)[:160]}")
                break
    return out


def run(cfg, conn, live: bool = False, probe_container: bool = False) -> dict[str, Any]:
    checks = offline_checks(cfg, conn)
    if live:
        checks += live_checks(cfg, conn, probe_container)
    worst = "fail" if any(c["status"] == "fail" for c in checks) else "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"status": worst, "checks": checks, "at": db.now_iso()}
