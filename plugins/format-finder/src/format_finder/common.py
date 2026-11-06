"""Shared records, errors and the route fallback used by every platform module."""
import json
import os
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

FF_HOME = Path(os.environ.get("FORMAT_FINDER_HOME", Path.home() / ".format-finder"))
# How to run the tool in the user's own terminal: the plugin's bin/ is on PATH only inside Claude Code, so messages
# that tell the user to run a command (instagram-login, browser-login) give the launcher's full path, quoted if needed.
TERMINAL_COMMAND = shlex.quote(str(Path(__file__).resolve().parents[2] / "bin" / "format-finder"))

VIDEO_KEYS = (
    "platform", "id", "url", "caption", "author_id", "author_username", "author_url",
    "author_followers", "views", "likes", "comments", "shares", "posted_at", "duration_s",
    "sound_id", "sound_title", "sound_author", "is_paid", "route", "raw_ref",
)
ACCOUNT_KEYS = ("platform", "id", "username", "url", "followers", "post_count", "is_private", "posts", "route")


class RouteError(Exception):
    """A route ran and failed."""


class NotConfigured(RouteError):
    """A route cannot run at all: missing login, service not running, dependency missing."""


class NotFound(RouteError):
    """The route works, and the platform says there is no such account or video (a bad handle or link).

    final=False: this route cannot tell a missing account from one it gets no data for, so later routes still ask."""

    def __init__(self, message, final=True):
        super().__init__(message)
        self.final = final


class AllRoutesFailed(RouteError):
    def __init__(self, errors, skipped=()):
        self.errors = errors
        self.skipped = list(skipped)
        super().__init__("; ".join(f"{e['route']}: {e['error']}" for e in errors) or "no routes")


def make_video(**fields):
    unknown = set(fields) - set(VIDEO_KEYS)
    if unknown:
        raise ValueError(f"unknown video fields: {sorted(unknown)}")
    return {k: fields.get(k) for k in VIDEO_KEYS}


def make_account(**fields):
    unknown = set(fields) - set(ACCOUNT_KEYS)
    if unknown:
        raise ValueError(f"unknown account fields: {sorted(unknown)}")
    record = {k: fields.get(k) for k in ACCOUNT_KEYS}
    record["posts"] = record["posts"] or []
    return record


def keywords(text):
    """A search phrase as plain keywords: '#' is removed so no phrase can turn into a hashtag lookup."""
    return " ".join(str(text or "").replace("#", " ").split())


def to_int(value):
    """Counts arrive as ints, floats or strings like '1,234'; anything else is None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def iso_from_timestamp(ts):
    if ts in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def iso_from_yyyymmdd(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _is_empty(result):
    if result is None:
        return True
    if isinstance(result, (list, tuple)) and not result:
        return True
    if isinstance(result, dict) and "posts" in result and not result["posts"] and not result.get("followers"):
        return True
    return False


# ---- route memory ---------------------------------------------------------------------
# Within one process (one scan), a route that keeps failing for an operation is skipped, so every later call
# goes straight to the route that works. Keyed by operation ("tiktok.search") and route name; nothing is saved.

ROUTE_FAILS_TO_SKIP = 2  # failed calls in a row before a route is skipped
_route_fails = {}   # (op, route) -> failed calls in a row
_route_off = {}     # (op, route) -> the skip record while the route is skipped
_route_skips = []   # every skip record of this process, in order


def reset_route_memory():
    """Forget every failure and skip (a scan starts with this)."""
    _route_fails.clear()
    _route_off.clear()
    _route_skips.clear()


def route_skips():
    """Every route skip of this process: {op, route, captcha, reason, failed_calls, skipped_calls, active}."""
    return [dict(s) for s in _route_skips]


def _skipped_now(op, names):
    """Routes of this call that route memory skips.

    A route skipped after failing is skipped only while another route of the call is still in use; when every
    route is skipped, those are tried again. A route that showed a captcha stays skipped for the whole run.
    """
    if op is None:
        return set()
    off = {n for n in names if (op, n) in _route_off}
    if len(off) < len(set(names)):
        return off
    return {n for n in off if _route_off[(op, n)]["captcha"]}


def _route_failed(op, name, exc):
    if op is None:
        return
    key = (op, name)
    captcha = getattr(exc, "captcha", False) is True
    entry = _route_off.get(key)
    if entry is not None:  # tried again because every route was skipped, and failed again
        entry["failed_calls"] += 1
        if captcha and not entry["captcha"]:
            entry.update(captcha=True, reason=f"captcha: {exc}"[:400])
        return
    _route_fails[key] = failed = _route_fails.get(key, 0) + 1
    if not captcha and failed < ROUTE_FAILS_TO_SKIP:
        return
    reason = (f"captcha: {exc}" if captcha else f"failed on {failed} calls in a row; last: {type(exc).__name__}: {exc}")
    entry = {"op": op, "route": name, "captcha": captcha, "reason": reason[:400], "failed_calls": failed,
             "skipped_calls": 0, "active": True}
    _route_off[key] = entry
    _route_skips.append(entry)
    log(f"[routes] {op}: skipping the {name} route for the rest of this run "
        + ("(it showed a captcha, and more would follow)" if captcha else
           f"while another route remains (it failed on {failed} calls in a row)"))


def _route_worked(op, name):
    if op is None:
        return
    _route_fails.pop((op, name), None)
    entry = _route_off.pop((op, name), None)
    if entry is not None:
        entry["active"] = False
        log(f"[routes] {op}: the {name} route answered again and is used again")


def try_routes(routes, *args, _op=None, **kwargs):
    """Run (name, fn) routes in order. Move on when a route raises or returns an empty result, and stop at NotFound:
    the platform has answered that the account or video does not exist, so later routes would only ask again. A
    NotFound with final=False moves on like an error, without counting as a route failure.

    Returns {"route", "errors", "result"}, plus "skipped_routes" when route memory skipped some. Raises
    AllRoutesFailed only when every route tried raised (or was skipped); when some route returned an empty result,
    that empty result is returned with the errors and "empty": true.

    `_op` ("tiktok.search") turns on route memory for the call: a route that raised on ROUTE_FAILS_TO_SKIP calls
    of that operation in a row is skipped for the rest of the process while another route remains, and a route
    whose error says `captcha` is skipped at once. A result resets the count; an empty result changes nothing.
    """
    errors, skipped = [], []
    empty = None
    skip = _skipped_now(_op, [name for name, _ in routes])
    for name, fn in routes:
        if name in skip:
            _route_off[(_op, name)]["skipped_calls"] += 1
            skipped.append(name)
            continue
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and the next route tried
            errors.append({"route": name, "error": f"{type(exc).__name__}: {exc}"})
            log(f"[{name}] failed: {type(exc).__name__}: {exc}")
            if isinstance(exc, NotFound):  # the route answered: a bad handle is no reason to skip it
                if exc.final:
                    break
                continue
            _route_failed(_op, name, exc)
            continue
        if _is_empty(result):
            errors.append({"route": name, "error": "empty result"})
            log(f"[{name}] returned nothing, trying next route")
            empty = (name, result)
            continue
        _route_worked(_op, name)
        return _envelope(name, errors, result, skipped)
    if empty is not None:
        return dict(_envelope(empty[0], errors, empty[1], skipped), empty=True)
    if not errors:
        errors = [{"route": n, "error": f"RouteSkipped: {_route_off[(_op, n)]['reason']}"} for n in skipped]
    raise AllRoutesFailed(errors, skipped)


def _envelope(route, errors, result, skipped):
    env = {"route": route, "errors": errors, "result": result}
    if skipped:
        env["skipped_routes"] = skipped
    return env


def ssl_context():
    """python.org builds on macOS often ship without a CA bundle; use certifi's when installed."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def log(message):
    print(message, file=sys.stderr, flush=True)


def pause(seconds):
    if seconds and seconds > 0:
        time.sleep(seconds)


def emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
