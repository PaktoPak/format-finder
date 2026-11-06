"""format-finder command line. Every command prints one JSON document to stdout."""
import argparse
import getpass
import importlib
import signal
import sys

from . import settings
from .common import AllRoutesFailed, emit

PLATFORMS = ("youtube", "tiktok", "instagram")


class UsageError(Exception):
    """Bad command-line arguments."""


class Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing usage and exiting, so the error can be answered as JSON."""

    def error(self, message):
        raise UsageError(f"{message}. Usage: {' '.join(self.format_usage().split()).removeprefix('usage: ')}")


def platform_module(name):
    return importlib.import_module(f"format_finder.{name}")


NO_POSTS_LISTED = {"instagram": "its Reels tab gave none (a profile with photo posts but no reels looks like this)",
                   "youtube": "its Shorts tab gave none", "tiktok": "its post list gave none"}


def _empty_reason(result):
    if isinstance(result, dict) and result.get("is_private"):
        return "the account is private: it shows no posts to this viewer"
    if isinstance(result, dict) and result.get("post_count") == 0:
        return "the profile exists but says it has 0 posts: probably not the brand's real account; check the handle"
    if isinstance(result, dict) and "posts" in result and (result.get("followers") or result.get("post_count")):
        where = NO_POSTS_LISTED.get(result.get("platform"), "it listed none")
        return f"the profile answered (a follower or post count) but has no posts to judge or score: {where}"
    if isinstance(result, dict) and "posts" in result:
        return ("no posts and no follower count: a missing, private, empty or blocked account looks the same here; "
                "check the handle")
    return "no results"


def envelope(platform, command, fn, *args, **kwargs):
    try:
        out = fn(*args, **kwargs)
    except AllRoutesFailed as exc:
        return {"platform": platform, "command": command, "ok": False, "route": None,
                "errors": exc.errors, "result": None}
    result = out.get("result")
    if isinstance(result, dict) and "posts" in result and not result["posts"]:
        out = dict(out, empty=True)  # also an account that came back with followers but no posts
    if out.get("empty"):
        out = dict(out, empty_reason=_empty_reason(result))
    return {"platform": platform, "command": command, "ok": True, **out}


def cmd_search(a):
    mod = platform_module(a.platform)
    days = a.days if a.days is not None else settings.get("recent_days")
    limit = a.limit or settings.get("search_limit")
    return envelope(a.platform, "search", mod.search, a.query, days=days, limit=limit, region=a.region)


def cmd_account(a):
    mod = platform_module(a.platform)
    limit = a.limit or settings.get("baseline_posts")
    return dict(envelope(a.platform, "account", mod.account, a.handle, limit=limit), handle=a.handle)


def cmd_video(a):
    return envelope(a.platform, "video", platform_module(a.platform).video, a.ref)


def cmd_sound(a):
    return envelope(a.platform, "sound", platform_module(a.platform).sound, a.ref, limit=a.limit or 30)


def cmd_apps(a):
    from . import appstore
    return envelope("appstore", "apps", appstore.search, a.query, country=a.country, limit=a.limit,
                    links=a.links)


def cmd_scan(a):
    from . import scan
    return scan.run(a.terms, a.platforms.split(","), a.out, limit=a.limit, max_accounts=a.max_accounts,
                    details=a.max_details, accounts_from=a.accounts_from)


def cmd_check_formats(a):
    import json
    from pathlib import Path
    from . import outliers
    groups = json.loads(Path(a.groups).read_text(encoding="utf-8"))
    if not isinstance(groups, dict):
        raise ValueError("groups file must map each format name to a list of candidate records")
    min_accounts = settings.get("min_accounts_per_format")
    cfg = {k: settings.get(k) for k in ("outlier_multiple_min", "outlier_views_min", "fading_min_span_days")}
    return {"ok": True, "command": "check-formats", "min_accounts_per_format": min_accounts, "settings": cfg,
            "formats": outliers.formats_check(groups, min_accounts, cfg)}


def cmd_media(a):
    from . import media
    return media.fetch(a.platform, a.ref, a.out, frames=a.frames, transcribe=not a.no_transcribe)


def _read_secret(prompt):
    """A value typed at a hidden prompt, or the first line of piped input. Never echoed or kept in shell history."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip()


def cmd_instagram_login(a):
    from . import instagram
    sessionid = a.sessionid
    if sessionid == "-":
        sessionid = _read_secret("Paste your Instagram sessionid cookie (hidden), then press Enter: ")
        if not sessionid:
            raise ValueError(f"no sessionid was given. {instagram.SESSIONID_HELP}")
    return dict(instagram.login(sessionid=sessionid, settings_file=a.settings_file), command="instagram-login")


def cmd_browser_login(a):
    from . import browser
    if a.timeout is not None and a.timeout < 0:
        raise ValueError("--timeout must be 0 or more seconds")
    out = browser.login_window(a.site, url=a.url, timeout=900 if a.timeout is None else a.timeout)
    uses = ("the headless browser route uses it" if a.site == "tiktok" else
            "Instagram's headless browser route (account and video; search only when the "
            "instagram_browser_search setting is true) uses it; the instagrapi and aiograpi routes use the session "
            "from format-finder instagram-login instead")
    return {"ok": True, "command": "browser-login", **out,
            "note": "You log in yourself in that window. format-finder types nothing into it and never reads, "
                    "prints or stores a password, cookie or token. The browser keeps the login in its own files in "
                    f"the dedicated profile (not protected by your system keychain), and {uses}; delete that "
                    "profile folder to log out. Password saving is off there."}


def cmd_settings(a):
    _, warnings = settings.check()
    return {"ok": True, "command": "settings", "settings": settings.shown(), "user_file": str(settings.USER_PATH),
            "warnings": warnings}


def cmd_doctor(a):
    from . import doctor
    return doctor.run(live=a.live)


def build_parser():
    p = Parser(prog="format-finder", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="keyword search for recent short-form videos")
    s.add_argument("platform", choices=PLATFORMS)
    s.add_argument("query")
    s.add_argument("--days", type=int)
    s.add_argument("--limit", type=int)
    s.add_argument("--region")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("account", help="an account's recent posts with views, newest first")
    s.add_argument("platform", choices=PLATFORMS)
    s.add_argument("handle")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_account)

    s = sub.add_parser("video", help="one video's details")
    s.add_argument("platform", choices=PLATFORMS)
    s.add_argument("ref")
    s.set_defaults(fn=cmd_video)

    s = sub.add_parser("sound", help="videos using a sound")
    s.add_argument("platform", choices=PLATFORMS)
    s.add_argument("ref")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_sound)

    s = sub.add_parser("apps", help="search the Apple App Store for competitor apps")
    s.add_argument("query")
    s.add_argument("--country", default="us")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--links", action="store_true",
                   help="also open each developer's website once and list the TikTok, Instagram and YouTube "
                        "profile links it shows")
    s.set_defaults(fn=cmd_apps)

    s = sub.add_parser("scan", help="search every phrase, fetch account histories, score outliers")
    s.add_argument("--terms", required=True, help="JSON file of search phrases (see skill)")
    s.add_argument("--platforms", default=",".join(PLATFORMS))
    s.add_argument("--out", required=True, help="run directory")
    s.add_argument("--limit", type=int, help="results per phrase per platform (default: the search_limit setting)")
    s.add_argument("--max-accounts", type=int, help="account lookups in this run (default: max_accounts_per_run)")
    s.add_argument("--max-details", type=int, help="video lookups that add dates and likes/comments to the top "
                                                   "outliers search left without them (default: max_details_per_run)")
    s.add_argument("--accounts-from", action="append", metavar="PATH",
                   help="an earlier scan's run folder or accounts.json, or the saved output of format-finder account "
                        "(a file, or a folder of them): those account histories are used again instead of fetched "
                        "(repeatable; they do not count toward --max-accounts)")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("check-formats", help="distinct accounts, median multiple and fading signal per format")
    s.add_argument("--groups", required=True, help="JSON file mapping each format name to its candidate records")
    s.set_defaults(fn=cmd_check_formats)

    s = sub.add_parser("media", help="download a video, cut frames, get captions or a transcript")
    s.add_argument("platform", choices=PLATFORMS)
    s.add_argument("ref")
    s.add_argument("--out", required=True)
    s.add_argument("--frames", type=int)
    s.add_argument("--no-transcribe", action="store_true")
    s.set_defaults(fn=cmd_media)

    s = sub.add_parser("instagram-login", help="save an Instagram session from your browser's sessionid cookie")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--sessionid", nargs="?", const="-",
                   help="with no value (or -) the cookie is read from a hidden prompt or from piped input")
    g.add_argument("--settings-file", help="an instagrapi settings JSON you already have")
    s.set_defaults(fn=cmd_instagram_login)

    s = sub.add_parser("browser-login", help="open a visible window of format-finder's own browser profile at a "
                                             "site's login page; you log in yourself, then close the window")
    s.add_argument("site", choices=("tiktok", "instagram"))
    s.add_argument("--timeout", type=int, help="seconds to wait for you to close the window before it is closed "
                                               "for you (default 900)")
    s.add_argument("--url", help=argparse.SUPPRESS)  # tests only: open this page instead of the login page
    s.set_defaults(fn=cmd_browser_login)

    s = sub.add_parser("settings", help="show effective settings, each with a note on what it does")
    s.set_defaults(fn=cmd_settings)

    s = sub.add_parser("doctor", help="check dependencies and routes")
    s.add_argument("--live", action="store_true", help="also make one small live request per route")
    s.set_defaults(fn=cmd_doctor)
    return p


def _command(argv):
    words = [w for w in (sys.argv[1:] if argv is None else argv) if not w.startswith("-")]
    return words[0] if words else None


def _signal_name(code):
    try:
        return f"{signal.Signals(code - 128).name}, exit {code}" if code > 128 else f"exit {code}"
    except ValueError:
        return f"exit {code}"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        emit({"command": _command(argv), "ok": False, "errors": [{"route": None, "error": f"UsageError: {exc}"}]})
        return 2
    try:
        result = args.fn(args)
    except (KeyboardInterrupt, SystemExit) as exc:  # Ctrl-C, or SIGTERM/SIGHUP (browser.py makes them SystemExit)
        code = 130 if isinstance(exc, KeyboardInterrupt) else exc.code if isinstance(exc.code, int) and exc.code else 1
        how = "Ctrl-C" if isinstance(exc, KeyboardInterrupt) else _signal_name(code)
        emit({"command": args.command, "ok": False, "errors": [{"route": None, "error": f"Interrupted ({how})"}]})
        return code
    except Exception as exc:  # noqa: BLE001 - always answer with JSON
        emit({"command": args.command, "ok": False, "errors": [{"route": None, "error": f"{type(exc).__name__}: {exc}"}]})
        return 1
    emit(result)
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
