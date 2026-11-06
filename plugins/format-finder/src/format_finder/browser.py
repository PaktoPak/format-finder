"""A small Chrome DevTools client and the one headless browser a format-finder process uses.

Routes that read web pages open them in a Chromium-family browser (Chrome, Chromium, Brave, Edge or Chrome for
Testing) with a profile of its own, FF_HOME/browser-profile, never your everyday browser profile. The site's own
scripts make their own signed requests; the tool reads the replies over the DevTools protocol. Nothing is typed into
a page, and no cookie, password or token is looked up, printed or stored by this module (a reply body that matches
a route's pattern reaches memory whole, so one that carries such a value is ignored by the route's parser). Embedded
<script type="application/json"> data read with JsonScripts(marker, keys) is cut down to those keys inside the page,
so the page's other scripts (Instagram's config script holds its csrf and DTSG tokens) are not read.

One browser per process, reused for every page; it is closed (with all its helper processes) when the process ends.
"""
import atexit
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from .common import FF_HOME, NotConfigured, RouteError, log

PROFILE = FF_HOME / "browser-profile"
DOWNLOADED = FF_HOME / "browser"
LOCK_PATH = FF_HOME / "browser-profile.lock"
OWNER_PATH = FF_HOME / "browser-profile.owner"
LOG_PATH = FF_HOME / "browser.log"
UNIVERSAL_DATA = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
SETUP_HINT = "run /format-finder:setup"
# Set to 1 to look only in FF_HOME/browser (where setup downloads Chrome for Testing) and at browser_path:
# installed browsers and the Playwright cache are then ignored. Used to test setup's download.
ONLY_DOWNLOADED_ENV = "FORMAT_FINDER_ONLY_DOWNLOADED_BROWSER"

# Timings of the browser itself. The first two are fallbacks for the
# browser_launch_timeout_seconds and browser_load_timeout_seconds settings.
LAUNCH_TIMEOUT = 30       # seconds for the browser to start and open its DevTools port
PROFILE_WAIT = 20         # seconds to wait while another format-finder command uses the profile
COMMAND_TIMEOUT = 30      # seconds for one DevTools command
PAGE_TIMEOUT = 30         # seconds for a page's HTML to arrive (DOMContentLoaded)
IDLE = 4.0                # seconds without a wanted reply before the page is scrolled or checked for a wall
CLOSE_TIMEOUT = 5         # seconds for the browser to quit before its processes are killed
SCROLL_TIMEOUT = 5        # seconds for one scroll to be answered; after that the page is not scrolled again
WS_MAX_BYTES = 64 * 2**20  # largest DevTools message accepted (response bodies come through it)
HEADLESS_SIZE = "1280,1000"

MAC_APPS = (
    ("Google Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
    ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
    ("Brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("Microsoft Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
)
LINUX_COMMANDS = (
    ("Google Chrome", ("google-chrome", "google-chrome-stable")),
    ("Chromium", ("chromium", "chromium-browser")),
    ("Brave", ("brave-browser", "brave")),
    ("Microsoft Edge", ("microsoft-edge", "microsoft-edge-stable")),
)
LINUX_PATHS = (
    ("Google Chrome", "opt/google/chrome/chrome"),
    ("Chromium", "snap/bin/chromium"),
    ("Brave", "opt/brave.com/brave/brave"),
    ("Microsoft Edge", "opt/microsoft/msedge/msedge"),
)
WINDOWS_APPS = (
    ("Google Chrome", ("Google", "Chrome", "Application", "chrome.exe")),
    ("Chromium", ("Chromium", "Application", "chrome.exe")),
    ("Brave", ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")),
    ("Microsoft Edge", ("Microsoft", "Edge", "Application", "msedge.exe")),
)
# Chrome for Testing / Chromium builds as Playwright and @puppeteer/browsers unpack them.
TESTING_BUILDS = {
    "Darwin": ("chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
               "chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
    "Linux": ("chrome-linux*/chrome",),
    "Windows": ("chrome-win*/chrome.exe",),
}

# Signs that a page shows a bot check, a captcha or a login wall instead of its content (TikTok and Instagram).
PAGE_CHECK_JS = r"""(() => {
  const seen = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const path = location.pathname;
  if (/^\/(login|accounts\/login|challenge)(\/|$)/.test(path)) return 'the site sent the page to its login page (' + path + ')';
  const text = document.body ? document.body.innerText.slice(0, 20000) : '';
  for (const phrase of ['Drag the slider to fit the puzzle', 'Select 2 objects that are the same shape',
                        'Verify to continue', 'Maximum number of attempts reached', 'Too many attempts']) {
    if (text.includes(phrase)) return 'a bot check ("' + phrase + '")';
  }
  for (const el of document.querySelectorAll('[id*="captcha" i], [class*="captcha" i], iframe[src*="captcha" i]')) {
    const name = [el.id, el.getAttribute('class'), el.getAttribute('src')].find(v => /captcha/i.test(v || '')) || el.tagName;
    if (seen(el)) return 'a captcha (' + String(name).slice(0, 80) + ')';
  }
  for (const el of document.querySelectorAll('[data-e2e="login-modal"], #loginContainer')) {
    if (seen(el)) return 'a login wall (the site asks to log in)';
  }
  return null;
})()"""
SCROLL_JS = "window.scrollTo(0, (document.scrollingElement || document.body || {}).scrollHeight || 1e6); true"
# Run inside the page: of the <script type="application/json"> texts that contain the marker, either the whole text
# (no keys), or for each key found anywhere in a script's JSON, {key: value} as its own text. With keys, a script
# without one of them is never returned, and neither is a part that contains a SKIP_PARTS string, so the rest of the
# page's scripts (Instagram's config script holds the csrf and DTSG tokens) never cross DevTools.
JSON_SCRIPTS_JS = r"""((marker, keys, skip) => {
  const out = [];
  const walk = x => {
    if (Array.isArray(x)) { for (const v of x) walk(v); return; }
    if (!x || typeof x !== 'object') return;
    for (const [k, v] of Object.entries(x)) {
      if (!keys.includes(k)) { walk(v); continue; }
      const part = JSON.stringify({[k]: v});
      if (!skip.some(s => part.includes(s))) out.push(part);
    }
  };
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    const text = s.textContent || '';
    if (!text.includes(marker)) continue;
    if (!keys.length) { out.push(text); continue; }
    if (!keys.some(k => text.includes('"' + k + '"'))) continue;
    try { walk(JSON.parse(text)); } catch (e) { }
  }
  return out;
})(%s, %s, %s)"""
SKIP_PARTS = ('"csrf_token"', '"DTSGInitialData"', '"fb_dtsg"')


class JsonScripts:
    """For Tab.capture(embedded=...): from the page's <script type="application/json"> documents whose text contains
    `marker`, a list of parsed JSON: each whole document, or with `keys`, only {key: value} for each of those keys
    found in them (picked inside the page; see JSON_SCRIPTS_JS). Instagram embeds a page's data this way, spread over
    scripts that have no id."""

    def __init__(self, marker, keys=()):
        self.marker = str(marker)
        self.keys = tuple(str(k) for k in keys)

    def expression(self):
        return JSON_SCRIPTS_JS % (json.dumps(self.marker), json.dumps(list(self.keys)), json.dumps(list(SKIP_PARTS)))

    def __repr__(self):
        return f"JsonScripts({self.marker!r}, keys={self.keys!r})"


class BrowserGone(RouteError):
    """The browser quit or its DevTools connection closed."""


class BotCheck(RouteError):
    """The site showed a captcha, a bot check or a login wall instead of its content (or an empty reply).

    `captcha` is True for a captcha or puzzle: more would follow, so route memory (common.try_routes) stops using
    the route for that operation at once.
    """

    def __init__(self, message, captcha=False):
        super().__init__(message)
        self.captcha = captcha


def is_captcha(reason):
    """True when a blocked-page reason (PAGE_CHECK_JS, or capture's captcha demand) names a captcha or puzzle."""
    return str(reason or "").startswith(("a captcha", "a bot check"))


# ---- finding a browser ----------------------------------------------------------------

def _executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _version_key(path):
    return tuple(int(n) for n in re.findall(r"\d+", str(path)))


def _testing_builds(base, system, depth):
    """Chrome for Testing / Chromium builds under `base`, newest first, up to `depth` folders down."""
    base = Path(base)
    if not base.is_dir():
        return []
    found = {p for tail in TESTING_BUILDS.get(system, ()) for d in range(depth + 1)
             for p in base.glob("*/" * d + tail)}
    return sorted(found, key=_version_key, reverse=True)


def _playwright_cache(system, home, environ):
    if environ.get("PLAYWRIGHT_BROWSERS_PATH") not in (None, "", "0"):
        return Path(environ["PLAYWRIGHT_BROWSERS_PATH"])
    if system == "Darwin":
        return home / "Library" / "Caches" / "ms-playwright"
    if system == "Windows":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "ms-playwright"
    return home / ".cache" / "ms-playwright"


def candidates(system=None, home=None, environ=None, which=shutil.which, ff_home=None, root="/"):
    """Every place a browser is looked for, in order: [(label, path)]. Nothing is checked here except globs.

    Order: a browser setup downloaded into FF_HOME/browser; installed Google Chrome, Chromium, Brave and
    Microsoft Edge; then Chrome for Testing in the Playwright cache. (browser_path, when set, comes before all.)
    """
    system = system or platform.system()
    home = Path(home or Path.home())
    environ = os.environ if environ is None else environ
    root = Path(root)
    out = [("downloaded by setup", p)
           for p in _testing_builds(Path(ff_home or FF_HOME) / "browser", system, depth=4)]
    if environ.get(ONLY_DOWNLOADED_ENV) not in (None, "", "0"):
        return out
    if system == "Darwin":
        for label, rel in MAC_APPS:
            out += [(label, d / rel) for d in (root / "Applications", home / "Applications")]
    elif system == "Windows":
        bases = [environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for label, parts in WINDOWS_APPS:
            out += [(label, Path(b, *parts)) for b in bases if b]
    else:
        for label, commands in LINUX_COMMANDS:
            out += [(label, Path(w)) for c in commands for w in [which(c)] if w]
            out += [(label, root / rel) for name, rel in LINUX_PATHS if name == label]
    out += [("Chrome for Testing (Playwright)", p)
            for p in _testing_builds(_playwright_cache(system, home, environ), system, depth=1)
            if "headless_shell" not in str(p)]
    return out


def locate(configured=None, **where):
    """(label, path) of the browser to run: `configured` (the browser_path setting) first, then candidates()."""
    if configured:
        path = Path(os.path.expanduser(str(configured)))
        if _executable(path):
            return "browser_path setting", str(path)
        raise NotConfigured(f"browser_path is set to {configured}, which is not a browser program on this "
                            "computer: fix it in the settings file (format-finder settings shows where) or set it to null")
    for label, path in candidates(**where):
        if _executable(path):
            return label, str(path)
    raise NotConfigured("no Chrome, Chromium, Brave or Edge browser was found for the browser route: "
                        f"{SETUP_HINT} (or set browser_path in the settings file)")


def find_browser(configured=None, **where):
    """The browser program to run: `configured` (the browser_path setting) first, then candidates() in order."""
    return locate(configured, **where)[1]


def version(exe, timeout=30):
    """The browser's own version line ('Google Chrome 142.0.7444.59'), or None when it does not print one."""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _setting(key, fallback):
    from . import settings
    try:
        return settings.get(key)
    except KeyError:
        return fallback


def _configured_path():
    return _setting("browser_path", None)


# ---- the profile lock -----------------------------------------------------------------

class ProfileLock:
    """An exclusive lock on the dedicated profile, held while this process's browser runs.

    Chrome must never run twice on one profile. A second format-finder command waits up to `wait` seconds,
    then fails with a message naming the command that holds the profile.
    """

    def __init__(self, path=LOCK_PATH, owner_path=OWNER_PATH):
        self.path, self.owner_path, self.fh = Path(path), Path(owner_path), None

    def acquire(self, wait=PROFILE_WAIT):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        deadline = time.monotonic() + wait
        told = False
        while True:
            try:
                _lock_file(fh)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise RouteError(f"the browser profile {PROFILE} is in use by another format-finder command "
                                     f"({self.owner()}); run this again when it has finished") from None
                if not told:
                    log(f"[browser] waiting up to {wait}s for another format-finder command using the browser "
                        f"profile ({self.owner()})")
                    told = True
                time.sleep(0.5)
        self.fh = fh
        try:
            self.owner_path.write_text(json.dumps({"pid": os.getpid(), "command": " ".join(sys.argv[1:3])}),
                                       encoding="utf-8")
        except OSError:
            pass
        return self

    def owner(self):
        try:
            data = json.loads(self.owner_path.read_text(encoding="utf-8"))
            return f"pid {data.get('pid')}: format-finder {data.get('command') or ''}".strip()
        except (OSError, ValueError):
            return "unknown process"

    def release(self):
        if self.fh is None:
            return
        try:
            _unlock_file(self.fh)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


if os.name == "nt":
    import msvcrt

    def _lock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---- processes ------------------------------------------------------------------------

def _new_group():
    """Popen options that start the browser in a process group of its own, so all its helpers can be ended."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _end_group(proc):
    """End the browser and every process in its group: TERM, then KILL after a few seconds."""
    if os.name == "nt":
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        try:
            proc.wait(CLOSE_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    pgid = proc.pid  # start_new_session made the browser its group's leader
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            proc.wait()
        if not _group_alive(pgid):
            return
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        end = time.monotonic() + 3
        while time.monotonic() < end:
            proc.poll()
            if not _group_alive(pgid):
                break
            time.sleep(0.1)
    proc.poll()


def _end_orphans(profile):
    """End a browser left running on the profile by a format-finder process that was killed.

    Called only while this process holds the profile lock, so no live format-finder command owns that browser.
    Chrome's SingletonLock names its process ('host-pid'); it is ended only if its command line names the profile.
    """
    if os.name == "nt":
        _end_orphans_windows(profile)
        return
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return
    match = re.search(r"-(\d+)$", target)
    if not match:
        return
    pid = int(match.group(1))
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 check=False, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if f"--user-data-dir={profile}" not in command:
        return
    log(f"[browser] ending a browser (pid {pid}) left running on the profile by an earlier command")
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        end = time.monotonic() + 3
        while time.monotonic() < end and _group_alive(pgid):
            time.sleep(0.1)
        if not _group_alive(pgid):
            return


# Windows has no SingletonLock link: the processes whose command line names the profile are found with PowerShell
# (the profile argument comes in through an environment variable, so the path needs no quoting).
WINDOWS_FIND_JS = ("$n = $env:FF_PROFILE_ARG; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                   "$_.CommandLine.Contains($n) } | ForEach-Object { $_.ProcessId }")


def _end_orphans_windows(profile, run=subprocess.run):
    """Windows: end every process whose command line has --user-data-dir=<profile>, with its child processes.
    Windows does not end a browser when the process that started it dies. Untested on a real Windows computer."""
    try:
        found = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_FIND_JS],
                    capture_output=True, text=True, check=False, timeout=30,
                    env={**os.environ, "FF_PROFILE_ARG": f"--user-data-dir={profile}"}).stdout
    except (OSError, subprocess.SubprocessError):
        return
    pids = [int(n) for n in found.split() if n.isdigit() and int(n) != os.getpid()]
    if pids:
        log(f"[browser] ending a browser (pids {', '.join(map(str, pids))}) left running on the profile by an "
            "earlier command")
    for pid in pids:
        try:
            run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


# A tiny process that ends the browser's process group when the format-finder process that started it is gone
# (killed with SIGKILL, or crashed), so the browser never keeps running without it. POSIX only.
WATCHDOG = r"""
import os, signal, sys, time
parent, pgid = int(sys.argv[1]), int(sys.argv[2])
def alive():
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
while alive():
    if os.getppid() != parent:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            end = time.monotonic() + 3
            while time.monotonic() < end and alive():
                time.sleep(0.1)
            if not alive():
                break
        break
    time.sleep(1)
"""


def _start_watchdog(proc):
    """Start WATCHDOG for the browser `proc` (its group's leader). None on Windows or when it cannot start."""
    if os.name == "nt":
        return None
    try:
        return subprocess.Popen([sys.executable, "-I", "-c", WATCHDOG, str(os.getpid()), str(proc.pid)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        log(f"[browser] the watchdog did not start ({exc}); a browser left by a killed command is ended by the next one")
        return None


def _no_password_saving(profile):
    """Turn off the browser's offer to save passwords (and auto sign-in) in the dedicated profile, through
    Chromium's own preferences file, so a password typed in the browser-login window is not saved by the browser."""
    path = Path(profile) / "Default" / "Preferences"
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prefs = {}
    except (OSError, ValueError):
        return  # a damaged file is the browser's to repair; it is not overwritten
    if not isinstance(prefs, dict):
        return
    if not isinstance(prefs.get("profile"), dict):
        prefs["profile"] = {}
    wanted = {"credentials_enable_service": False, "credentials_enable_autosignin": False}
    if all(prefs.get(k) is v for k, v in wanted.items()) and prefs["profile"].get("password_manager_enabled") is False:
        return
    prefs.update(wanted)
    prefs["profile"]["password_manager_enabled"] = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("Preferences.format-finder.tmp")
        tmp.write_text(json.dumps(prefs), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log(f"[browser] could not turn off password saving in {path}: {exc}")


def devtools_url(port_file):
    """The browser-level DevTools WebSocket URL from a DevToolsActivePort file, or None while it is incomplete."""
    try:
        lines = Path(port_file).read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].strip().isdigit() or not lines[1].startswith("/devtools/browser/"):
        return None
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1].strip()}"


# ---- the DevTools connection -----------------------------------------------------------

class Cdp:
    """One DevTools WebSocket. Single-threaded: events that arrive while a command waits are kept in order."""

    def __init__(self, url):
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect
        except ImportError as exc:
            raise NotConfigured(f"the Python package websockets is missing ({exc}): {SETUP_HINT}") from exc
        self._closed_error = ConnectionClosed
        # proxy=None: never send the local DevTools connection through a system proxy. legacy=True: one plain
        # connection, closed by close() (websockets 17 warns when connect() is not used as a context manager).
        self.ws = connect(url, max_size=WS_MAX_BYTES, proxy=None, compression=None, ping_interval=None,
                          open_timeout=10, close_timeout=2, legacy=True)
        self.next_id = 0
        self.events = deque()

    def _recv(self, timeout):
        try:
            raw = self.ws.recv(timeout=max(timeout, 0))
        except TimeoutError:
            return None
        except self._closed_error as exc:
            raise BrowserGone("the browser closed its DevTools connection") from exc
        return json.loads(raw)

    def send(self, method, params=None, session=None, timeout=COMMAND_TIMEOUT):
        self.next_id += 1
        mid = self.next_id
        message = {"id": mid, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            self.ws.send(json.dumps(message))
        except self._closed_error as exc:
            raise BrowserGone("the browser closed its DevTools connection") from exc
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise RouteError(f"the browser did not answer {method} within {timeout}s")
            msg = self._recv(left)
            if msg is None:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RouteError(f"browser {method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)

    def event(self, timeout):
        """The next event, or None after `timeout` seconds."""
        if self.events:
            return self.events.popleft()
        end = time.monotonic() + timeout
        while True:
            msg = self._recv(end - time.monotonic())
            if msg is None:
                return None
            if "method" in msg:
                return msg

    def drain(self):
        self.events.clear()
        while self._recv(0) is not None:
            pass

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is fine
            pass


# ---- browser and tab ------------------------------------------------------------------

class Browser:
    """A browser process on the dedicated profile, with its DevTools connection."""

    def __init__(self, exe=None, profile=PROFILE, headless=True):
        self.exe = exe
        self.profile = Path(profile)
        self.headless = headless
        self.proc = self.cdp = self.lock = self._tab = self._log = self.watchdog = None
        self.launch_timeout = LAUNCH_TIMEOUT

    def args(self, url=None):
        args = [self.exe, f"--user-data-dir={self.profile}", "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
                "--use-mock-keychain", "--password-store=basic", "--disable-background-networking",
                "--disable-sync", "--disable-breakpad"]
        if self.headless:
            # Pages are read, not watched: no images, no autoplaying video, no sound. The three --disable-*
            # flags keep the page counted as visible: on a Mac, a headless page sometimes started "hidden", and
            # then it drew no frames, so scrolling never answered and infinite lists never loaded more.
            args += ["--headless=new", f"--window-size={HEADLESS_SIZE}", "--mute-audio",
                     "--blink-settings=imagesEnabled=false", "--autoplay-policy=user-gesture-required",
                     "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                     "--disable-backgrounding-occluded-windows"]
        else:
            args += ["--new-window"]
        args.append(url or "about:blank")
        return args

    def start(self, url=None):
        self.exe = self.exe or find_browser(_configured_path())
        self.launch_timeout = _setting("browser_launch_timeout_seconds", LAUNCH_TIMEOUT)
        self.lock = ProfileLock().acquire(PROFILE_WAIT)
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            _end_orphans(self.profile)
            _no_password_saving(self.profile)
            port_file = self.profile / "DevToolsActivePort"
            port_file.unlink(missing_ok=True)
            self._log = open(LOG_PATH, "wb")
            self.proc = subprocess.Popen(self.args(url), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=self._log, **_new_group())
            self.watchdog = _start_watchdog(self.proc)
            self.cdp = Cdp(self._wait_for_port(port_file))
        except BaseException:
            self.close()
            raise
        _install_exit_handlers()
        _open_browsers.add(self)
        log(f"[browser] started {Path(self.exe).name} ({'headless' if self.headless else 'window'}, "
            f"profile {self.profile})")
        return self

    def _wait_for_port(self, port_file):
        end = time.monotonic() + self.launch_timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RouteError(f"the browser quit as it started (exit {self.proc.returncode}): {self._log_tail()}")
            url = devtools_url(port_file)
            if url:
                return url
            time.sleep(0.1)
        raise RouteError(f"the browser did not open its DevTools port within {self.launch_timeout}s "
                         f"(setting browser_launch_timeout_seconds): {self._log_tail()}")

    def _log_tail(self):
        try:
            self._log.flush()
            lines = [l for l in LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            return " | ".join(lines[-3:])[:400] or "no message"
        except OSError:
            return "no message"

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.cdp is not None

    def tab(self):
        """The tab every page read reuses: the browser's first page, or a new one if it was closed."""
        if self._tab is not None and self._tab.alive:
            return self._tab
        targets = self.cdp.send("Target.getTargets").get("targetInfos") or []
        pages = [t for t in targets if t.get("type") == "page"]
        if pages:
            target_id = pages[0]["targetId"]
        else:
            target_id = self.cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        tab = Tab(self, target_id, session)
        try:
            tab.call("Page.bringToFront", timeout=5)  # the active tab is the visible one (see args())
        except RouteError:
            pass
        tab.call("Page.enable")
        tab.call("Page.setLifecycleEventsEnabled", enabled=True)
        tab.call("Network.enable", maxTotalBufferSize=200 * 2**20, maxResourceBufferSize=50 * 2**20)
        self._tab = tab
        return tab

    def page_targets(self):
        return [t for t in self.cdp.send("Target.getTargets").get("targetInfos") or [] if t.get("type") == "page"]

    def close(self):
        """Quit the browser (gracefully first, so the profile is saved), end its processes, release the lock."""
        _open_browsers.discard(self)
        if self.cdp is not None:
            try:
                self.cdp.send("Browser.close", timeout=3)
            except RouteError:
                pass
            self.cdp.close()
            self.cdp = None
        if self.watchdog is not None:  # this process ends the browser itself
            self.watchdog.kill()
            self.watchdog.wait()
            self.watchdog = None
        if self.proc is not None:
            try:
                self.proc.wait(CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
            _end_group(self.proc)
            self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if self.lock is not None:
            self.lock.release()
            self.lock = None
        self._tab = None


def bot_check_kind(header):
    """TikTok's bdturing-verify header ('{"type":"verify","subtype":"whirl",...}') -> 'verify/whirl'."""
    try:
        data = json.loads(header)
    except (TypeError, ValueError):
        return "captcha"
    return "/".join(str(data[k]) for k in ("type", "subtype") if data.get(k)) or "captcha"


def _headers(response):
    """Response headers with lower-case names; set-cookie is left out (the tool never reads cookies)."""
    return {str(k).lower(): v for k, v in (response.get("headers") or {}).items() if str(k).lower() != "set-cookie"}


class Tab:
    """One page target on a flat DevTools session."""

    def __init__(self, browser, target_id, session):
        self.browser, self.target_id, self.session = browser, target_id, session
        self.loader = self.frame = None
        self.alive = True

    def call(self, method, timeout=COMMAND_TIMEOUT, **params):
        return self.browser.cdp.send(method, params, session=self.session, timeout=timeout)

    def open(self, url):
        """Start loading `url`. The page's own events are read by capture()."""
        self.browser.cdp.drain()
        # Page.navigate answers once the page's response arrives, so it may take as long as the load timeout.
        load_timeout = _setting("browser_load_timeout_seconds", PAGE_TIMEOUT)
        result = self.call("Page.navigate", timeout=max(COMMAND_TIMEOUT, load_timeout), url=url)
        self.loader, self.frame = result.get("loaderId"), result.get("frameId")
        if result.get("errorText"):
            doc = self.document_response()
            status = f" (HTTP {doc.get('status')})" if doc.get("status") else ""
            check = doc.get("headers", {}).get("bdturing-verify")
            if check:
                raise BotCheck(f"the site answered {url} with a captcha demand{status}: {bot_check_kind(check)}",
                               captcha=True)
            raise RouteError(f"could not open {url}: {result['errorText']}{status}")

    def document_response(self):
        """Status and headers of the page's own HTML response, from the events seen so far ({} if none yet)."""
        for ev in self.browser.cdp.events:
            p = ev.get("params") or {}
            if ev.get("sessionId") == self.session and ev.get("method") == "Network.responseReceived" \
                    and p.get("type") == "Document" and p.get("loaderId") == self.loader:
                response = p.get("response") or {}
                return {"status": response.get("status"), "headers": _headers(response)}
        return {}

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            text = (result["exceptionDetails"].get("exception") or {}).get("description") or "script error"
            raise RouteError(f"page script failed: {text[:200]}")
        return (result.get("result") or {}).get("value")

    def embedded_json(self, element_id=UNIVERSAL_DATA):
        """The JSON a page embeds in <script id=element_id>, or None when it has none."""
        text = self.evaluate(f"(() => {{ const el = document.getElementById({json.dumps(element_id)});"
                             f" return el ? el.textContent : null; }})()")
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def json_scripts(self, marker, keys=()):
        """What JsonScripts(marker, keys) picks from the page, parsed ([] when nothing matches)."""
        docs = []
        for text in self.evaluate(JsonScripts(marker, keys).expression()) or []:
            try:
                docs.append(json.loads(text))
            except (TypeError, ValueError):
                continue
        return docs

    def _embedded(self, embedded):
        """capture()'s embedded data: the JsonScripts documents (None when none match), else the JSON in
        <script id=embedded>."""
        if isinstance(embedded, JsonScripts):
            return self.json_scripts(embedded.marker, embedded.keys) or None
        return self.embedded_json(embedded)

    def blocked(self):
        """What the page shows instead of its content (captcha, bot check, login wall), or None."""
        try:
            return self.evaluate(PAGE_CHECK_JS)
        except RouteError:
            return None

    def location(self):
        try:
            return self.evaluate("location.href")
        except RouteError:
            return None

    def scroll(self):
        """Scroll to the bottom like a reader would, so infinite lists ask for their next page.

        Raises RouteError when the wheel event is not answered within SCROLL_TIMEOUT seconds (a page that draws
        no frames never answers it)."""
        self.evaluate(SCROLL_JS)
        width, height = (int(n) for n in HEADLESS_SIZE.split(","))
        self.call("Input.dispatchMouseEvent", timeout=SCROLL_TIMEOUT, type="mouseWheel", x=width // 2,
                  y=height // 2, deltaX=0, deltaY=height * 3)

    def park(self):
        """Leave the page for about:blank so it stops making requests between reads."""
        try:
            self.call("Page.navigate", url="about:blank", timeout=5)
        except RouteError:
            pass

    def _reply(self, request_id, response):
        reply = {"url": response.get("url"), "status": response.get("status"), "body": None, "error": None,
                 "headers": _headers(response)}
        try:
            got = self.call("Network.getResponseBody", requestId=request_id)
        except RouteError as exc:
            reply["error"] = str(exc)
            return reply
        body = got.get("body")
        if got.get("base64Encoded") and body:
            import base64
            body = base64.b64decode(body).decode("utf-8", "replace")
        reply["body"] = body
        return reply

    def capture(self, url, pattern=None, on_reply=None, embedded=None, on_embedded=None, timeout=45.0,
                idle=IDLE, max_scrolls=0, scroll_gap=0.0, on_idle=None):
        """Open `url` and read what the page itself loads.

        - Each finished response whose URL path matches the regex `pattern` goes to `on_reply(reply)`, where
          reply is {url, status, body (text or None), error}.
        - When the HTML has arrived, the JSON in <script id=embedded> goes to `on_embedded(data)`; with
          embedded=JsonScripts(marker, keys), what that picks from the <script type="application/json"> documents.
          When the page has none, the capture waits `idle` seconds for a new document, then ends (on_embedded is
          not called).
        - A callback returning True ends the capture at once.
        - When no wanted reply arrived for `idle` seconds, the page is checked for a captcha or login wall, then
          `on_idle(tab)` is called (when given; True ends the capture, e.g. on a page that says it does not
          exist), then the page is scrolled (at most `max_scrolls` times, at least `scroll_gap` seconds apart)
          so it asks for more.
        - When the page loads a new document in the same tab (a reload or a script redirect), that document is
          the one read from then on.
        Returns {replies, scrolls, embedded (found or not), blocked (reason or None), timed_out, seconds, url,
        status (the HTML's HTTP status), documents (how many the page loaded)}, plus scroll_error when a scroll
        was not answered.
        """
        rx = re.compile(pattern) if pattern else None
        load_timeout = _setting("browser_load_timeout_seconds", PAGE_TIMEOUT)
        started = time.monotonic()
        deadline = started + timeout
        summary = {"replies": 0, "scrolls": 0, "embedded": None, "blocked": None, "timed_out": False,
                   "seconds": None, "url": None, "status": None, "documents": 1}
        tracked, ready, done = {}, False, False
        last = last_scroll = load_started = started
        next_poll = started + 3
        try:
            self.open(url)
            while not done:
                now = time.monotonic()
                if now >= deadline:
                    summary["timed_out"] = True
                    break
                if not ready and now - load_started > load_timeout:
                    raise RouteError(f"{url} did not load within {load_timeout}s (setting browser_load_timeout_seconds)")
                ev = self.browser.cdp.event(min(0.25, deadline - now))
                became_ready = False
                if ev is not None:
                    method, p = ev.get("method"), ev.get("params") or {}
                    if method == "Target.detachedFromTarget" and p.get("sessionId") == self.session:
                        self.alive = False
                        raise RouteError("the browser tab closed while a page was read")
                    if ev.get("sessionId") == self.session and method == "Network.responseReceived" \
                            and p.get("type") == "Document" and self.frame and p.get("frameId") == self.frame \
                            and p.get("loaderId") not in (None, self.loader):
                        # The page loaded a new document in its own tab (TikTok sometimes serves a first page
                        # with no data that reloads itself): read that one instead.
                        self.loader, ready, became_ready = p["loaderId"], False, False
                        tracked.clear()
                        summary["documents"] += 1
                        last = load_started = time.monotonic()
                        next_poll = load_started + 3
                    if ev.get("sessionId") == self.session:
                        if method == "Page.lifecycleEvent" and p.get("name") == "DOMContentLoaded" \
                                and p.get("loaderId") == self.loader:
                            became_ready = not ready
                        elif method == "Network.responseReceived" and p.get("type") == "Document" \
                                and p.get("loaderId") == self.loader:
                            summary["status"] = (p.get("response") or {}).get("status")
                            check = _headers(p.get("response") or {}).get("bdturing-verify")
                            if check:
                                summary["blocked"] = f"a captcha demand ({bot_check_kind(check)})"
                        elif method == "Inspector.targetCrashed":
                            self.alive = False
                            raise RouteError("the page crashed in the browser")
                        elif rx and method == "Network.responseReceived" \
                                and p.get("loaderId") in (self.loader, "", None) \
                                and rx.search(urlsplit((p.get("response") or {}).get("url") or "").path):
                            tracked[p["requestId"]] = p.get("response") or {}
                        elif method in ("Network.loadingFinished", "Network.loadingFailed") \
                                and p.get("requestId") in tracked:
                            response = tracked.pop(p["requestId"])
                            if method == "Network.loadingFinished":
                                reply = self._reply(p["requestId"], response)
                            else:
                                reply = {"url": response.get("url"), "status": response.get("status"),
                                         "body": None, "error": p.get("errorText") or "request failed",
                                         "headers": _headers(response)}
                            summary["replies"] += 1
                            last = time.monotonic()
                            if on_reply and on_reply(reply):
                                done = True
                if not ready and not became_ready and time.monotonic() >= next_poll:
                    # Fallback when the lifecycle event was missed: ask the page itself.
                    next_poll = time.monotonic() + 1
                    try:
                        state = self.evaluate("[document.readyState, location.href]") or ["", ""]
                    except RouteError:  # the old document went away mid-navigation
                        state = ["", ""]
                    became_ready = state[0] in ("interactive", "complete") and state[1] != "about:blank"
                if became_ready:
                    ready = True
                    last = time.monotonic()
                    if embedded:
                        data = self._embedded(embedded)
                        summary["embedded"] = data is not None
                        # No data yet: wait (up to `idle` seconds) in case the page loads a new document.
                        if data is not None and ((on_embedded and on_embedded(data)) or not rx):
                            done = True
                if done or not ready or tracked:
                    continue
                if time.monotonic() - last >= idle:
                    reason = self.blocked()
                    if reason:
                        summary["blocked"] = reason
                        break
                    if on_idle and on_idle(self):
                        break
                    if embedded and not summary["embedded"]:
                        break  # the page has no data and loaded no new document: nothing to scroll for
                    if summary["scrolls"] >= max_scrolls:
                        break
                    gap = scroll_gap - (time.monotonic() - last_scroll)
                    if gap > 0:
                        time.sleep(min(gap, max(deadline - time.monotonic(), 0)))
                    try:
                        self.scroll()
                    except RouteError as exc:
                        summary["scroll_error"] = str(exc)
                        log(f"[browser] stopped scrolling {url}: {exc}")
                        break
                    summary["scrolls"] += 1
                    last = last_scroll = time.monotonic()
            nothing = (rx and not summary["replies"]) or (embedded and not summary["embedded"])
            if nothing and summary["blocked"] is None and ready:
                summary["blocked"] = self.blocked()
            summary["url"] = self.location()
            return summary
        except BrowserGone:
            shutdown()
            raise
        finally:
            summary["seconds"] = round(time.monotonic() - started, 1)
            if self.alive and self.browser.alive():
                self.park()


# ---- one browser per process ----------------------------------------------------------

_shared = None
_open_browsers = set()
_handlers_installed = False


def tab():
    """The shared headless browser's tab, starting the browser on first use."""
    global _shared
    if _shared is not None and not _shared.alive():
        _shared.close()
        _shared = None
    if _shared is None:
        _shared = Browser().start()
    try:
        return _shared.tab()
    except BrowserGone:
        shutdown()
        raise


def shutdown():
    """Close every browser this process started. Runs at exit and on SIGTERM/SIGHUP."""
    global _shared
    for b in list(_open_browsers):
        try:
            b.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must reach every browser
            log(f"[browser] closing failed: {type(exc).__name__}: {exc}")
    _shared = None


def _exit_on_signal(signum, frame):
    raise SystemExit(128 + signum)


def _install_exit_handlers():
    """atexit closes the browsers; SIGTERM and SIGHUP become a normal exit so atexit runs then too."""
    global _handlers_installed
    if _handlers_installed:
        return
    _handlers_installed = True
    atexit.register(shutdown)
    if threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None and signal.getsignal(sig) in (signal.SIG_DFL, None):
            signal.signal(sig, _exit_on_signal)


# ---- a visible window for logging in --------------------------------------------------

LOGIN_PAGES = {
    "tiktok": "https://www.tiktok.com/login",
    "instagram": "https://www.instagram.com/accounts/login/",
}


def login_window(site, url=None, timeout=900):
    """Open a visible window of the dedicated profile at `site`'s login page and wait until it is closed.

    The person logs in themselves. The tool types nothing and reads no cookie; the browser keeps the login in the
    dedicated profile's own files (see README), which the headless browser route uses afterwards. Password saving is
    off in that profile. After `timeout` seconds the window is closed for them.
    """
    if site not in LOGIN_PAGES:
        raise ValueError(f"browser-login knows {', '.join(LOGIN_PAGES)}, not {site!r}")
    start_url = url or LOGIN_PAGES[site]
    if _shared is not None:
        shutdown()
    b = Browser(headless=False)
    b.start(start_url)
    started = time.monotonic()
    opened, closed_by = False, None
    try:
        while closed_by is None:
            if b.proc.poll() is not None:
                closed_by = "you"
                break
            try:
                pages = b.page_targets()
            except BrowserGone:
                closed_by = "you"
                break
            if pages:
                opened = True
            elif opened:
                closed_by = "you"
                break
            elapsed = time.monotonic() - started
            if not opened and elapsed > b.launch_timeout:
                raise RouteError(f"the login window did not open within {b.launch_timeout}s")
            if opened and elapsed >= timeout:
                closed_by = "timeout"
                break
            time.sleep(0.5)
    finally:
        b.close()
    return {"site": site, "opened": start_url, "closed_by": closed_by,
            "seconds_open": round(time.monotonic() - started, 1), "browser": b.exe, "profile": str(b.profile)}


# ---- python -m format_finder.browser (setup.sh) ------------------------------------------

def main():
    """Print, as JSON, the browser the browser route would run. setup.sh uses this so it finds a browser exactly as
    the route does. Exit 0: one is found. 1: none is found (setup then downloads one). 2: the browser_route setting
    is false. 3: the settings file has an error, or browser_path names no browser program (nothing to download)."""
    from . import settings
    try:
        if not settings.get("browser_route"):
            print(json.dumps({"found": None, "browser_route": False}))
            return 2
        configured = _configured_path()
    except ValueError as exc:
        print(json.dumps({"found": False, "error": str(exc), "fix": str(settings.USER_PATH)}))
        return 3
    try:
        label, path = locate(configured)
    except NotConfigured as exc:
        print(json.dumps({"found": False, "error": str(exc), **({"fix": str(settings.USER_PATH)} if configured else {})}))
        return 3 if configured else 1
    print(json.dumps({"found": True, "label": label, "path": path, "version": version(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
