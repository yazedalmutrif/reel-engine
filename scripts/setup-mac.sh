#!/usr/bin/env bash
# Reel Engine: first-time setup and repair on macOS (Apple Silicon). Safe to run again.
#
#   1. checks arm64 + python3.10
#   2. creates .venv if missing
#   3. pip installs requirements.txt WITHOUT the Windows/CUDA-only packages
#      (nvidia-*, pywin32-ctypes, pyreadline3), then `pip install -e .` (skipped if already done)
#   4. downloads ffmpeg + ffprobe into bin/ffmpeg-mac/ from PINNED URLs, verifies PINNED sha256
#      values (refuses on mismatch), removes the quarantine flag on those two files only,
#      checks the build (libass, libharfbuzz, libx264, libzimg + a FriBidi probe)
#   5. copies config.mac.example.toml to config.toml only if config.toml does not exist
#   6. creates the data folders and the inbox from config ([paths].inbox)
#   7. runs `reel calendar seed` and `reel doctor` (a doctor FAIL is reported, not fatal)
#
# No sudo, no Homebrew, no system changes, no scheduled jobs, no secrets. Nothing is written
# outside the repo except the inbox folder named in config.toml.
#
# Usage:  scripts/setup-mac.sh [--dry-run] [--skip-ffmpeg]
#   --dry-run      show what would happen; change nothing
#   --skip-ffmpeg  skip step 4
# Exit codes: 0 all good; 1 a setup step failed; 2 setup done but `reel doctor` reported FAIL.
set -euo pipefail

# ---- pinned ffmpeg build -----------------------------------------------------------------
# Source: Martin Riedl's build server (https://ffmpeg.martin-riedl.de), macOS arm64, release 9.0.2.
# Checksums are published next to each file as <file>.sha256; values and sizes checked by the
# tech lead on 2026-10-15. License: GPLv3 (static GPL build). The binaries are signed by the
# vendor; `codesign -dv` is printed for information only.
# FriBidi is linked inside the static libass (no --enable-libfribidi flag), so it is checked
# with a functional probe, not by the configure flag.
FFMPEG_URL="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip"
FFMPEG_SHA256="c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"   # 28,395,699 bytes
FFPROBE_URL="https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip"
FFPROBE_SHA256="fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6"  # 28,317,701 bytes
REQUIRED_FLAGS=(--enable-libass --enable-libharfbuzz --enable-libx264 --enable-libzimg)

DRY_RUN=0
SKIP_FFMPEG=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-ffmpeg) SKIP_FFMPEG=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (use --dry-run, --skip-ffmpeg)" >&2; exit 1 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
REEL="$VENV/bin/reel"
BIN="$ROOT/bin/ffmpeg-mac"
DATA_SUBDIRS=(raw work out thumbs playbooks backups dashboard logs vault_preview launchd)

step() { printf '\n[%s/7] %s\n' "$1" "$2"; }
ok()   { printf '  ok     %s\n' "$1"; }
plan() { printf '  would  %s\n' "$1"; }
note() { printf '  note   %s\n' "$1"; }
die()  { printf '\nStopped: %s\n' "$1" >&2; exit 1; }

cd "$ROOT"
echo "Reel Engine macOS setup  ($ROOT)"
[ "$DRY_RUN" = 1 ] && echo "Dry run: nothing will be changed."

# 1. platform + Python ----------------------------------------------------------------------
step 1 "Platform and Python 3.10"
[ "$(uname -s)" = "Darwin" ] || die "this script is for macOS (use scripts/setup.ps1 on Windows)."
[ "$(uname -m)" = "arm64" ] || die "expected Apple Silicon (arm64), got $(uname -m)."
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?') on arm64"
PY310="$(command -v python3.10 || true)"
if [ -n "$PY310" ]; then
  ok "python3.10 -> $("$PY310" -c 'import sys; print(sys.version.split()[0])')"
elif [ -x "$PY" ]; then
  note "python3.10 not on PATH, but .venv exists; continuing with it."
else
  die "Python 3.10 not found. Install Python 3.10 (macOS universal2) from python.org yourself, then run this again."
fi

# 2. virtual environment --------------------------------------------------------------------
step 2 "Virtual environment (.venv)"
if [ -x "$PY" ]; then
  v="$("$PY" -c 'import sys; print(sys.version.split()[0])')"
  case "$v" in 3.10.*) ok ".venv present (Python $v)" ;; *) die ".venv uses Python $v, not 3.10. Rename .venv yourself, then run this again." ;; esac
elif [ "$DRY_RUN" = 1 ]; then
  plan "create .venv with $PY310 -m venv"
else
  "$PY310" -m venv "$VENV"
  ok ".venv created"
fi

# 3. Python packages ------------------------------------------------------------------------
step 3 "Python packages (Windows/CUDA-only packages filtered out)"
REQ_MAC="$ROOT/data/requirements-mac.txt"
filtered="$(grep -viE '^(nvidia-|pywin32-ctypes|pyreadline3)' "$ROOT/requirements.txt")"
skipped="$(grep -iE '^(nvidia-|pywin32-ctypes|pyreadline3)' "$ROOT/requirements.txt" | tr '\n' ' ')"
note "not installed on macOS (Windows/CUDA only): ${skipped:-none}"
if [ "$DRY_RUN" = 1 ]; then
  plan "write the filtered list to data/requirements-mac.txt and pip install -r it"
  if [ -x "$PY" ]; then
    missing="$(printf '%s\n' "$filtered" | "$PY" -c '
import sys
from importlib.metadata import version, PackageNotFoundError
bad = []
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith("#") or "==" not in line:
        continue
    name, want = line.split("==", 1)
    try:
        have = version(name)
    except PackageNotFoundError:
        have = None
    if have != want:
        bad.append(f"{name}=={want} (have {have})")
print(" ".join(bad))')"
    if [ -n "$missing" ]; then plan "pip would change: $missing"; else ok "all pinned packages already installed"; fi
  fi
else
  mkdir -p "$ROOT/data"
  printf '%s\n' "$filtered" > "$REQ_MAC"
  "$PY" -m pip install --disable-pip-version-check -q -r "$REQ_MAC"
  ok "requirements satisfied (from data/requirements-mac.txt)"
fi
if [ -x "$REEL" ] && "$PY" -c 'import reelengine' 2>/dev/null; then
  ok "reelengine package already installed (reel at $REEL)"
elif [ "$DRY_RUN" = 1 ]; then
  plan "pip install -e . (the reelengine package and the reel command)"
else
  "$PY" -m pip install --disable-pip-version-check -q -e "$ROOT"
  ok "reelengine installed (editable)"
fi

# 4. ffmpeg ---------------------------------------------------------------------------------
fribidi_probe() {  # $1 = ffmpeg binary; prints the libass Shaper line, fails if no FriBidi
  local d line
  d="$(mktemp -d "$ROOT/bin/.fribidi-probe.XXXXXX")"
  cat > "$d/probe.ass" <<'ASS'
[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,0,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,مرحبا
ASS
  line="$(cd "$d" && "$1" -hide_banner -nostdin -v verbose -f lavfi -i color=c=black:s=320x240:d=0.2 \
           -vf ass=probe.ass -frames:v 1 -f null - 2>&1 | grep -m1 'Shaper: FriBidi' || true)"
  rm -rf "$d"
  [ -n "$line" ] || return 1
  echo "${line#*Shaper: }"
}

check_build() {  # $1 = ffmpeg binary
  local conf f missing=""
  conf="$("$1" -hide_banner -buildconf 2>&1)" || return 1
  for f in "${REQUIRED_FLAGS[@]}"; do
    printf '%s' "$conf" | grep -q -- "$f" || missing="$missing $f"
  done
  [ -z "$missing" ] || { echo "  missing configure flags:$missing" >&2; return 1; }
  ok "build flags: ${REQUIRED_FLAGS[*]}"
  if printf '%s' "$conf" | grep -q -- --enable-libfribidi; then
    ok "FriBidi: --enable-libfribidi"
  else
    local shaper
    shaper="$(fribidi_probe "$1")" || { echo "  FriBidi not found (no 'Shaper: FriBidi' line from libass)" >&2; return 1; }
    ok "FriBidi via libass (Shaper: $shaper)"
  fi
}

fetch_verified() {  # $1 url, $2 sha256, $3 binary name, $4 temp dir
  local url="$1" sha="$2" name="$3" tmp="$4" zip="$4/$3.zip"
  echo "  downloading $url"
  curl -fsSL --proto '=https' --tlsv1.2 -o "$zip" "$url"
  (cd "$tmp" && printf '%s  %s\n' "$sha" "$name.zip" | shasum -a 256 -c -) \
    || die "CHECKSUM MISMATCH for $name.zip: refusing to continue (nothing installed)."
  mkdir -p "$tmp/$name.d"
  unzip -q -o "$zip" -d "$tmp/$name.d"
  local found
  found="$(find "$tmp/$name.d" -type f -name "$name" | head -n1)"
  [ -n "$found" ] || die "$name not found inside $name.zip"
  mv -f "$found" "$BIN/$name"
  chmod +x "$BIN/$name"
  if xattr -p com.apple.quarantine "$BIN/$name" >/dev/null 2>&1; then
    xattr -d com.apple.quarantine "$BIN/$name"
    note "removed the quarantine flag from bin/ffmpeg-mac/$name"
  fi
  codesign -dv "$BIN/$name" 2>&1 | grep -E '^(Identifier|Authority|TeamIdentifier)=' | sed 's/^/  info   /' || true
  ok "bin/ffmpeg-mac/$name installed (sha256 verified)"
}

step 4 "ffmpeg + ffprobe (bin/ffmpeg-mac, sha256 pinned)"
if [ "$SKIP_FFMPEG" = 1 ]; then
  note "skipped (--skip-ffmpeg)"
elif [ -x "$BIN/ffmpeg" ] && [ -x "$BIN/ffprobe" ] && check_build "$BIN/ffmpeg"; then
  ok "ffmpeg + ffprobe present and checked"
elif [ "$DRY_RUN" = 1 ]; then
  plan "download $FFMPEG_URL and $FFPROBE_URL, verify sha256, install into bin/ffmpeg-mac/"
else
  [ -n "$FFMPEG_URL" ] && [ -n "$FFMPEG_SHA256" ] && [ -n "$FFPROBE_URL" ] && [ -n "$FFPROBE_SHA256" ] \
    || die "the pinned ffmpeg URLs/checksums at the top of this script are empty."
  mkdir -p "$BIN"
  TMP="$(mktemp -d "$ROOT/bin/.ffmpeg-dl.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  fetch_verified "$FFMPEG_URL" "$FFMPEG_SHA256" ffmpeg "$TMP"
  fetch_verified "$FFPROBE_URL" "$FFPROBE_SHA256" ffprobe "$TMP"
  rm -rf "$TMP"; trap - EXIT
  check_build "$BIN/ffmpeg" || die "the downloaded ffmpeg lacks a required feature (see above)."
fi

# 5. config.toml ----------------------------------------------------------------------------
step 5 "config.toml"
if [ -f "$ROOT/config.toml" ]; then
  ok "config.toml present (left unchanged)"
elif [ "$DRY_RUN" = 1 ]; then
  plan "copy config.mac.example.toml -> config.toml"
else
  cp "$ROOT/config.mac.example.toml" "$ROOT/config.toml"
  ok "config.toml created from config.mac.example.toml (dry-run mode)"
fi

# 6. folders --------------------------------------------------------------------------------
step 6 "Folders"
CFG_FOR_PATHS="$ROOT/config.toml"
[ -f "$CFG_FOR_PATHS" ] || CFG_FOR_PATHS="$ROOT/config.mac.example.toml"   # dry run before step 5 copied it
if [ -x "$PY" ] && paths="$(REELENGINE_CONFIG="$CFG_FOR_PATHS" "$PY" -c 'from reelengine.config import load_config as L; c=L(); print(c.data_dir); print(c.inbox)' 2>/dev/null)"; then
  DATA_DIR="$(printf '%s\n' "$paths" | sed -n 1p)"
  INBOX="$(printf '%s\n' "$paths" | sed -n 2p)"
else
  [ "$DRY_RUN" = 1 ] || die "could not read paths from the config (is .venv ready?)"
  DATA_DIR="$ROOT/data"; INBOX="(from config.toml once .venv is ready)"
fi
DIRS=("$DATA_DIR")
for s in "${DATA_SUBDIRS[@]}"; do DIRS+=("$DATA_DIR/$s"); done
DIRS+=("$INBOX")
for d in "${DIRS[@]}"; do
  if [ -d "$d" ]; then ok "present  $d"
  elif [ "$DRY_RUN" = 1 ]; then plan "create   $d"
  else mkdir -p "$d"; ok "created  $d"; fi
done

# 7. calendar + doctor ----------------------------------------------------------------------
step 7 "Calendar seed and health check"
if [ "$DRY_RUN" = 1 ]; then
  plan "run: reel calendar seed"
  plan "run: reel doctor"
  echo; echo "Dry run finished: nothing was changed."
  exit 0
fi
"$REEL" calendar seed || die "reel calendar seed failed"
set +e
"$REEL" doctor
rc=$?
set -e
echo
if [ "$rc" -ne 0 ]; then
  echo "Setup finished, but reel doctor reported FAIL (see the lines above)."
  exit 2
fi
echo "Setup finished. Scheduling is NOT set up: run 'reel schedule plist' and, after Yazeed's OK, 'reel schedule install --mac'."
