"""Health check: is setup complete, which routes can run, and (live) does each route answer one tiny request."""
import filecmp
import importlib
import importlib.metadata
import json
import subprocess
import time
from pathlib import Path

from . import settings
from .common import FF_HOME, TERMINAL_COMMAND, AllRoutesFailed, log, pause
from .youtube import NODE_MIN

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (("yt_dlp", "yt-dlp"), ("yt_dlp_ejs", "yt-dlp-ejs"), ("curl_cffi", "curl_cffi"),
            ("instagrapi", "instagrapi"), ("aiograpi", "aiograpi"), ("faster_whisper", "faster-whisper"),
            ("imageio_ffmpeg", "imageio-ffmpeg"), ("numpy", "numpy"), ("certifi", "certifi"),
            ("marshmallow", "marshmallow"), ("websockets", "websockets"))
SETUP = "run /format-finder:setup"
LIVE_QUERY = "meal prep"
LIVE_ACCOUNT = "tiktok"


def _check(checks, name, fn, required=True):
    """Run one offline check. fn returns a detail string, or raises with the reason it failed."""
    try:
        ok, detail = True, fn()
    except Exception as exc:  # noqa: BLE001 - every check reports, none stops the others
        ok, detail = False, str(exc) or type(exc).__name__
    checks.append({"name": name, "ok": ok, "required": required, "detail": detail})
    return ok


def _package(module, dist):
    importlib.import_module(module)
    return f"{dist} {importlib.metadata.version(dist)}"


def _ffmpeg():
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    first = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30).stdout.splitlines()
    return f"{first[0] if first else 'ffmpeg'} ({exe})"


def _node():
    from .youtube import _node_path
    node = _node_path()
    if not node:
        raise RuntimeError(f"node not found: install Node.js {NODE_MIN} or newer from https://nodejs.org/, then {SETUP}")
    version = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=30).stdout.strip()
    if int(version.lstrip("v").split(".")[0] or 0) < NODE_MIN:
        raise RuntimeError(f"node {version} is too old: yt-dlp needs Node {NODE_MIN} or newer for YouTube")
    return f"node {version} ({node})"


def _youtubei():
    pkg = FF_HOME / "node" / "node_modules" / "youtubei.js" / "package.json"
    if not pkg.is_file():
        raise RuntimeError(f"YouTube.js is missing from {pkg.parent.parent}: {SETUP}")
    return f"youtubei.js {json.loads(pkg.read_text(encoding='utf-8')).get('version')}"


def _yt_mjs():
    from .youtube import NODE_SCRIPT
    if not NODE_SCRIPT.is_file():
        raise RuntimeError(f"{NODE_SCRIPT} is missing: {SETUP}")
    plugin_copy = PLUGIN_ROOT / "node" / "yt.mjs"
    if plugin_copy.is_file() and not filecmp.cmp(plugin_copy, NODE_SCRIPT, shallow=False):
        return f"{NODE_SCRIPT} differs from the plugin's copy: {SETUP} to update it"
    return str(NODE_SCRIPT)


def _vendor():
    from .tiktok import VENDOR
    if not (VENDOR / "tiktok" / "fetch.py").is_file():
        raise RuntimeError(f"omkarcloud tiktok-scraper is missing from {VENDOR}: {SETUP}")
    head = VENDOR / ".git" / "HEAD"
    commit = head.read_text().strip() if head.is_file() else "unknown commit"
    return f"{VENDOR} at {commit[:12]}"


def _settings():
    errors = settings.check()[0]
    if errors:
        raise RuntimeError("; ".join(errors))
    eff = settings.effective()
    changed = sorted(k for k, v in eff.items() if v["overridden"])
    where = settings.USER_PATH if settings.USER_PATH.is_file() else "defaults only (no user file)"
    return f"{where}; overridden: {', '.join(changed) or 'none'}"


def _setting_names():
    warnings = settings.check()[1]
    if warnings:
        raise RuntimeError("; ".join(warnings))
    return "every setting name in the user file is known"


def _instagram_session():
    from . import instagram
    path = instagram.session_path()
    if not path.is_file():
        raise RuntimeError(f"not logged in: {instagram.LOGIN_HINT}")
    cl = instagram._client("instagrapi")
    return f"{path} (user id {cl.user_id}, loaded offline)"


def _dtk():
    url = settings.get("dtk_url")
    if not url:
        raise RuntimeError("dtk_url is not set, so TikTok route 2 is off (optional)")
    return f"{url} ({'API key set' if settings.get('dtk_api_key') else 'no API key set'})"


def _proxy():
    if not settings.get("tiktok_proxy"):
        raise RuntimeError("tiktok_proxy is not set: TikTok search answers only from a US IP (optional)")
    return "tiktok_proxy is set"


def _browser():
    """Which browser the browser route would run (path and version), or why there is none."""
    from . import browser
    label, path = browser.locate(browser._configured_path())
    found = f"{label}: {path} ({browser.version(path) or 'version not reported'})"
    if not settings.get("browser_route"):
        raise RuntimeError(f"browser_route is false, so the browser route is off (found {found})")
    return found


def _browser_profile():
    """Whether the browser route's own profile folder exists. Nothing inside it is read."""
    from . import browser
    if not browser.PROFILE.is_dir():
        raise RuntimeError(f"{browser.PROFILE} does not exist yet: the first browser-route request or "
                           "format-finder browser-login creates it")
    return f"{browser.PROFILE} exists"


def _whisper_model():
    name = settings.get("whisper_model")
    if not (FF_HOME / "models" / f"models--Systran--faster-whisper-{name}").is_dir():
        raise RuntimeError(f"Whisper model {name} is not downloaded yet: the first transcript downloads it")
    return f"Whisper model {name} is in {FF_HOME / 'models'}"


def _live(results, platform, route, what, fn, *args, **kwargs):
    """One tiny live request. Records ok, a short detail or the error, and how long it took."""
    started = time.monotonic()
    captcha = False
    try:
        out = fn(*args, **kwargs)
        if isinstance(out, dict) and "result" in out and "errors" in out:
            out = out["result"]
        ok, detail = bool(out), _describe(out)
    except AllRoutesFailed as exc:
        ok, detail = False, "; ".join(e["error"] for e in exc.errors)
    except Exception as exc:  # noqa: BLE001 - a blocked route is a result, not a crash
        ok, detail = False, f"{type(exc).__name__}: {exc}"
        captcha = getattr(exc, "captcha", False) is True
    results.append({"platform": platform, "route": route, "check": what, "ok": ok, "detail": detail[:500],
                    "seconds": round(time.monotonic() - started, 1)})
    if captcha:
        results[-1]["captcha"] = True
    log(f"[doctor] {platform} {route} {what}: {'ok' if ok else 'FAILED'}")
    return ok


def _describe(out):
    if isinstance(out, list):
        first = out[0] if out else {}
        shown = f"views={first.get('views')}" if "views" in first else repr(first.get("name"))
        return f"{len(out)} result(s); first: {first.get('id') or first.get('name')} {shown}"
    if isinstance(out, dict) and "posts" in out:
        return f"@{out.get('username')}: followers={out.get('followers')}, {len(out['posts'])} post(s)"
    return str(out)[:200] if out else "empty result"


def _skipped(results, platform, route, what, reason):
    results.append({"platform": platform, "route": route, "check": what, "ok": None, "detail": f"skipped: {reason}",
                    "seconds": 0})


def _live_checks(by_name):
    """One small request per route. Routes that cannot run offline are skipped, not tried."""
    from . import appstore, tiktok, youtube
    results, delay = [], settings.get("request_delay_seconds")
    # Any upload date: a date filter can make YouTube answer "No results found" for a route that works.
    if by_name["python:yt_dlp"] and by_name["node"]:
        _live(results, "youtube", "yt-dlp", f"search {LIVE_QUERY!r}, 1 result", youtube._ytdlp_search,
              LIVE_QUERY, days=None, limit=1)
    else:
        _skipped(results, "youtube", "yt-dlp", "search", "yt-dlp or node missing")
    if by_name["node"] and by_name["youtubei.js"] and by_name["yt.mjs"]:
        pause(delay)
        _live(results, "youtube", "youtube.js", f"search {LIVE_QUERY!r}, 1 result", youtube._js_search,
              LIVE_QUERY, days=None, limit=1)
    else:
        _skipped(results, "youtube", "youtube.js", "search", "node, youtubei.js or yt.mjs missing")

    if by_name["omkarcloud"]:
        _live(results, "tiktok", "omkarcloud", f"profile @{LIVE_ACCOUNT}, 1 post", tiktok._omkar_account,
              LIVE_ACCOUNT, limit=1)
        pause(delay)
        _live(results, "tiktok", "omkarcloud", f"search {LIVE_QUERY!r}, 1 result", tiktok._omkar_search,
              LIVE_QUERY, days=30, limit=1)
    else:
        _skipped(results, "tiktok", "omkarcloud", "profile", "vendor folder missing")
    if by_name["dtk"]:
        pause(delay)
        _live(results, "tiktok", "dtk", f"profile @{LIVE_ACCOUNT}, 1 post", tiktok._dtk_account, LIVE_ACCOUNT, limit=1)
    else:
        _skipped(results, "tiktok", "dtk", "profile", "dtk_url not set")
    if by_name["python:yt_dlp"]:
        pause(delay)
        _live(results, "tiktok", "yt-dlp", f"profile @{LIVE_ACCOUNT}, 1 post (partial fallback)",
              tiktok._ytdlp_account, LIVE_ACCOUNT, limit=1)
    else:
        _skipped(results, "tiktok", "yt-dlp", "profile", "yt-dlp missing")
    if _browser_ready(by_name):
        # TikTok flags an IP after repeated logged-out searches, so this is one search for one result.
        from . import browser
        try:
            pause(delay)
            _live(results, "tiktok", "browser", f"profile @{LIVE_ACCOUNT}, 1 post", tiktok._browser_account,
                  LIVE_ACCOUNT, limit=1)
            pause(delay)
            _live(results, "tiktok", "browser", f"search {LIVE_QUERY!r}, 1 result, any date", tiktok._browser_search,
                  LIVE_QUERY, days=None, limit=1)
        finally:
            browser.shutdown()
    else:
        _skipped(results, "tiktok", "browser", "profile and search", _browser_off_reason(by_name))

    if by_name["instagram session"]:
        _live(results, "instagram", "instagrapi", "the session's own profile", _instagram_me)
    else:
        _skipped(results, "instagram", "instagrapi", "session", "not logged in")
    _live(results, "appstore", "itunes-search", "search 'calendar', 1 app", appstore.search, "calendar", limit=1)
    return results


def _browser_ready(by_name):
    return bool(by_name.get("browser") and by_name.get("python:websockets"))


def _browser_off_reason(by_name):
    if not by_name.get("python:websockets"):
        return f"the Python package websockets is missing: {SETUP}"
    return "browser_route is false" if not settings.get("browser_route") else \
        f"no browser found: {SETUP} or set browser_path"


def _browser_search_note(by_name, live, checked):
    """One plain sentence: does TikTok keyword search work through the browser right now."""
    if not _browser_ready(by_name):
        return f"keyword search through the browser route: off ({_browser_off_reason(by_name)})"
    if checked is None:
        return "keyword search through the browser route: not checked; run doctor --live to test it now"
    if checked:
        return "keyword search through the browser route works right now (live check)"
    hit = next((r for r in live or [] if (r["platform"], r["route"]) == ("tiktok", "browser")
                and r["check"].startswith("search")), {})
    if hit.get("captcha"):
        from .tiktok import CAPTCHA_HINT
        return f"keyword search through the browser route does not work right now: {CAPTCHA_HINT}"
    return f"keyword search through the browser route does not work right now: {hit.get('detail') or 'failed'}"


def _either(*values):
    """True if any is True, else None if any is unknown, else False."""
    return True if True in values else None if None in values else False


def _platforms(by_name, live):
    """Per platform: which routes can run (True/False, or None when unknown) and what to tell the user."""
    def live_ok(platform, route, prefix=""):
        hits = [r["ok"] for r in live or [] if (r["platform"], r["route"]) == (platform, route)
                and r["check"].startswith(prefix) and r["ok"] is not None]
        return all(hits) if hits else None

    def route(ready, platform, name, prefix=""):
        if not ready:
            return False
        checked = live_ok(platform, name, prefix)
        return True if checked is None else checked

    yt = {"yt-dlp": route(by_name["python:yt_dlp"] and by_name["node"], "youtube", "yt-dlp"),
          "youtube.js": route(by_name["node"] and by_name["youtubei.js"] and by_name["yt.mjs"], "youtube", "youtube.js")}
    tk = {"omkarcloud": route(by_name["omkarcloud"], "tiktok", "omkarcloud", "profile"),
          "dtk": route(by_name["dtk"], "tiktok", "dtk"),
          "yt-dlp": route(by_name["python:yt_dlp"], "tiktok", "yt-dlp"),
          "browser": route(_browser_ready(by_name), "tiktok", "browser", "profile")}
    direct_search = live_ok("tiktok", "omkarcloud", "search") if by_name["omkarcloud"] or by_name["dtk"] else False
    browser_search = live_ok("tiktok", "browser", "search") if _browser_ready(by_name) else False
    tk_search = _either(direct_search, browser_search)
    tk_notes = []
    if direct_search is False:
        tk_notes.append("keyword search through omkarcloud failed from this IP: TikTok answers it only from a US IP; "
                        "set tiktok_proxy, or run your own Douyin_TikTok_Download_API (dtk_url).")
    elif direct_search is None and not by_name["tiktok_proxy"]:
        tk_notes.append("keyword search through omkarcloud answers only from a US IP; run doctor --live to test it "
                        "from here")
    tk_notes.append(_browser_search_note(by_name, live, browser_search))
    profile = next((r for r in live or [] if (r["platform"], r["route"]) == ("tiktok", "browser")
                    and r["check"].startswith("profile") and r["ok"] is False), None)
    if profile:
        tk_notes.append(f"the browser route could not read a profile right now: {profile['detail']}")
    if tk_search is False:
        tk_notes.append("No TikTok keyword search works right now. Accounts, videos and sounds can still work.")
    # The Instagram browser route works only with a login the person made in the tool's browser; doctor never reads
    # that profile's cookies, so it cannot tell offline: None (unknown) when a browser is ready.
    ig = {"instagrapi": route(by_name["python:instagrapi"] and by_name["instagram session"], "instagram", "instagrapi"),
          "aiograpi": by_name["python:aiograpi"] and by_name["instagram session"],
          "browser": None if _browser_ready(by_name) else False}
    ig_notes = [] if by_name["instagram session"] else [
        f"not logged in: {instagram_hint()}"]
    ig_notes.append("the browser route (account and video; search only when instagram_browser_search is true) works "
                    f"only if you logged in to Instagram yourself with {TERMINAL_COMMAND} browser-login instagram; "
                    "doctor does not check that" if ig["browser"] is None
                    else f"the browser route is off ({_browser_off_reason(by_name)})")
    ig_available = True if ig["instagrapi"] or ig["aiograpi"] else ig["browser"]
    return {
        "youtube": {"available": any(yt.values()), "routes": yt, "notes": []},
        "tiktok": {"available": any(tk.values()), "search": tk_search, "search_via_browser": browser_search,
                   "routes": tk, "notes": tk_notes},
        "instagram": {"available": ig_available, "routes": ig, "notes": ig_notes},
        "appstore": {"available": route(True, "appstore", "itunes-search"), "routes": {"itunes-search":
                     route(True, "appstore", "itunes-search")}, "notes": []},
    }


def _instagram_me():
    """One request with the saved session: the logged-in account's own profile."""
    from . import instagram

    async def me(cl, lib):
        return await instagram._call(cl.user_info_v1, cl.user_id)

    user = instagram._route("instagrapi", me)
    return f"logged in as @{user.username}"


def instagram_hint():
    from . import instagram
    return instagram.LOGIN_HINT


def run(live=False):
    """Offline checks of everything setup installs; with live=True also one tiny request per route."""
    checks = []
    for module, dist in PACKAGES:
        _check(checks, f"python:{module}", lambda m=module, d=dist: _package(m, d))
    _check(checks, "ffmpeg", _ffmpeg)
    _check(checks, "node", _node)
    _check(checks, "youtubei.js", _youtubei)
    _check(checks, "yt.mjs", _yt_mjs)
    _check(checks, "omkarcloud", _vendor)
    _check(checks, "settings", _settings)
    _check(checks, "setting names", _setting_names, required=False)
    _check(checks, "instagram session", _instagram_session, required=False)
    _check(checks, "dtk", _dtk, required=False)
    _check(checks, "tiktok_proxy", _proxy, required=False)
    _check(checks, "browser", _browser, required=False)
    _check(checks, "browser profile", _browser_profile, required=False)
    _check(checks, "whisper model", _whisper_model, required=False)
    by_name = {c["name"]: c["ok"] for c in checks}
    setup_ok = all(c["ok"] for c in checks if c["required"])
    settings_errors = settings.check()[0]
    live_results = None
    if live:
        live_results = [] if settings_errors else _live_checks(by_name)
        if settings_errors:
            _skipped(live_results, "all", None, "live checks", f"the settings file has errors: {settings_errors[0]}")
    return {
        "ok": setup_ok, "command": "doctor", "setup_ok": setup_ok,
        "next_step": (f"fix the settings file: {settings_errors[0]}" if settings_errors
                      else None if setup_ok else f"setup is incomplete: {SETUP}"),
        "home": str(FF_HOME), "plugin_root": str(PLUGIN_ROOT), "terminal_command": TERMINAL_COMMAND, "checks": checks,
        "platforms": _platforms(by_name, live_results), "live": live_results,
    }
