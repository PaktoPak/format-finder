#!/usr/bin/env bash
# One-time setup for format-finder. Safe to re-run: it updates what is already there.
# Installs into $FORMAT_FINDER_HOME (default ~/.format-finder). Everything installed is free and open source.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FF_HOME="${FORMAT_FINDER_HOME:-$HOME/.format-finder}"
OMK_REPO="https://github.com/omkarcloud/tiktok-scraper"
OMK_COMMIT="358264c2dbadc10d1cf70dd279776828e2809fe9"
# @puppeteer/browsers downloads Chrome for Testing (Google's official builds) when no browser is installed.
PUPPETEER_BROWSERS_VERSION="3.2.5"
export FORMAT_FINDER_HOME="$FF_HOME"  # the tool's own checks below look in the same place

step() { printf '\n==> %s\n' "$1"; }
fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

step "Checking required programs"
command -v python3 >/dev/null || fail "python3 not found. Install Python 3.11 or newer from https://www.python.org/downloads/"
command -v node >/dev/null || fail "node not found. Install Node.js 22 or newer from https://nodejs.org/ (YouTube needs it)"
command -v npm >/dev/null || fail "npm not found. It comes with Node.js from https://nodejs.org/"
command -v git >/dev/null || fail "git not found. Install it from https://git-scm.com/downloads"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "Python $(python3 -c 'import platform; print(platform.python_version())') is too old. instagrapi needs 3.11 or newer."
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 22 ] || fail "Node $(node -v) is too old. yt-dlp needs Node 22 or newer for YouTube."
echo "python $(python3 -c 'import platform; print(platform.python_version())'), node $(node -v), git ok"

mkdir -p "$FF_HOME/vendor" "$FF_HOME/node" "$FF_HOME/models"

step "Python environment in $FF_HOME/venv"
[ -x "$FF_HOME/venv/bin/python" ] || python3 -m venv "$FF_HOME/venv"
"$FF_HOME/venv/bin/python" -m pip install --quiet --upgrade pip
"$FF_HOME/venv/bin/python" -m pip install --quiet --upgrade -r "$PLUGIN_ROOT/requirements.txt"
echo "installed: yt-dlp, instagrapi, aiograpi, faster-whisper, imageio-ffmpeg, websockets"

step "TikTok route 1: omkarcloud tiktok-scraper (MIT), pinned to $OMK_COMMIT"
if [ ! -d "$FF_HOME/vendor/tiktok-scraper/.git" ]; then
  git clone --quiet "$OMK_REPO" "$FF_HOME/vendor/tiktok-scraper"
fi
git -C "$FF_HOME/vendor/tiktok-scraper" fetch --quiet origin
git -C "$FF_HOME/vendor/tiktok-scraper" checkout --quiet "$OMK_COMMIT"

step "YouTube route 2: YouTube.js"
cp "$PLUGIN_ROOT/node/package.json" "$PLUGIN_ROOT/node/yt.mjs" "$FF_HOME/node/"
(cd "$FF_HOME/node" && npm install --silent --omit=dev)

step "Settings"
if [ ! -f "$FF_HOME/settings.json" ]; then
  echo '{}' > "$FF_HOME/settings.json"
  echo "created $FF_HOME/settings.json (empty: the defaults apply; run 'format-finder settings' to see them)"
else
  echo "kept your $FF_HOME/settings.json"
fi

step "Browser for the TikTok browser route"
# The tool finds the browser exactly as the browser route does (browser_path, $FF_HOME/browser, installed Chrome,
# Chromium, Brave or Edge, Chrome for Testing in the Playwright cache). Nothing is downloaded when one is found.
find_browser() { PYTHONPATH="$PLUGIN_ROOT/src" "$FF_HOME/venv/bin/python" -m format_finder.browser; }
set +e; BROWSER_FOUND="$(find_browser)"; BROWSER_STATUS=$?; set -e
if [ "$BROWSER_STATUS" -eq 0 ]; then
  echo "found: $BROWSER_FOUND"
elif [ "$BROWSER_STATUS" -eq 2 ]; then
  echo "browser_route is false in your settings: no browser needed"
elif [ "$BROWSER_STATUS" -eq 3 ]; then
  echo "WARNING: nothing downloaded: fix your settings file first ($BROWSER_FOUND), then re-run setup."
else
  echo "no Chrome, Chromium, Brave or Edge found: downloading Chrome for Testing (free, Google's official build)"
  echo "into $FF_HOME/browser with @puppeteer/browsers $PUPPETEER_BROWSERS_VERSION"
  mkdir -p "$FF_HOME/browser"
  # npm's download cache stays inside $FF_HOME, so removing $FF_HOME removes everything setup added.
  if npm_config_cache="$FF_HOME/npm-cache" npx --yes "@puppeteer/browsers@$PUPPETEER_BROWSERS_VERSION" \
       install chrome@stable --path "$FF_HOME/browser"; then
    set +e; BROWSER_FOUND="$(find_browser)"; BROWSER_STATUS=$?; set -e
    [ "$BROWSER_STATUS" -eq 0 ] && echo "found: $BROWSER_FOUND" \
      || echo "WARNING: the download finished but the tool does not find it: $BROWSER_FOUND"
  else
    echo "WARNING: the Chrome for Testing download failed. The other routes still work; re-run setup to try again,"
    echo "or install Chrome, Chromium, Brave or Edge, or set browser_path in $FF_HOME/settings.json."
  fi
fi

step "Checking everything"
"$PLUGIN_ROOT/bin/format-finder" doctor
