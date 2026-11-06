"""Instagram Reels: route 1 instagrapi, route 2 aiograpi (both through the session saved by instagram-login), and
last instagram.com's own pages in a headless browser (account, video, download; search only when the
instagram_browser_search setting is true), which needs a login the person made themselves in the tool's browser
(browser-login instagram).
The browser_route setting turns the last one off.

Records are parsed from Instagram's raw private-API JSON, not from the libraries' Media objects: their
extractor fills missing like, view and play counts with 0, and a missing count must stay None here.
Search is keyword only (the app's Reels search tab). The tool asks for no hashtag search and no tag endpoint itself.
instagram.com's keyword search page, though, sends its own tag-information request for the keyword
(fetch__XDTTagInfo, seen on 2026-11-05), so the browser route for search stays off unless instagram_browser_search
is true.
"""
import asyncio
import contextlib
import functools
import importlib
import inspect
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse

from . import common, settings
from .common import (TERMINAL_COMMAND, NotConfigured, NotFound, RouteError, iso_from_timestamp, keywords, log,
                     make_account, make_video, to_int, try_routes)

LIBS = ("instagrapi", "aiograpi")
LOGIN_HINT = (f"run {TERMINAL_COMMAND} instagram-login --sessionid in your own terminal and paste your sessionid "
              "cookie at its hidden prompt")
NEEDS_LOGIN = f"Instagram needs a login: {LOGIN_HINT}"
EXPIRED = f"Instagram rejected the saved session (expired or logged out): copy a fresh cookie and {LOGIN_HINT}"
CHECKPOINT = ("Instagram wants a security check for this account: open instagram.com in your browser, "
              f"complete it, then {LOGIN_HINT}")
SESSIONID_HELP = ("In a browser where you are logged in to instagram.com, open the developer tools, find the cookie "
                  "named sessionid and copy its value (digits, then %3A, then more characters).")
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
VIEW_KEYS = ("play_count", "ig_play_count", "view_count", "video_view_count", "video_play_count")
PAGE_ITEMS = 12  # Instagram's app pages reels 12 at a time
MAX_PAGES = 5
PINNED_MAX = 3  # pinned reels come first on a profile whatever their date
ID_EPOCH_MS = 1314220021721  # a media pk holds milliseconds since this moment above its lowest 23 bits

_last_call = [None]
_blocked = [None]


def session_path():
    return common.FF_HOME / "instagram_session.json"


# ---- pure parsing -------------------------------------------------------------------------------

def pk_from_code(code):
    """Shortcode -> numeric media pk (Instagram's base64 id; only the first 11 characters count)."""
    pk = 0
    for ch in code[:11]:
        pk = pk * 64 + CODE_ALPHABET.index(ch)
    return str(pk)


def code_from_pk(pk):
    """Numeric media pk -> shortcode (the inverse of pk_from_code)."""
    n, code = int(pk), ""
    while n:
        n, digit = divmod(n, 64)
        code = CODE_ALPHABET[digit] + code
    return code or CODE_ALPHABET[0]


def time_from_pk(pk):
    """The creation time (UTC datetime) a numeric media pk ('123', '123_456') encodes, else None.

    It is (pk >> 23) milliseconds after ID_EPOCH_MS. On the 12 reels of one scan (2026-11-05) it was 10 s to 7 min
    before their taken_at. The scan uses it only for its date window; it is never a record's posted_at.
    """
    pk = str(pk or "").split("_")[0]
    if not pk.isdigit():
        return None
    try:
        return datetime.fromtimestamp(((int(pk) >> 23) + ID_EPOCH_MS) / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_ref(ref):
    """(kind, value): kind is 'pk', 'code', 'audio', 'share' (a share link to resolve) or None."""
    ref = str(ref or "").strip()
    if "instagram.com" in ref or ref.startswith(("http://", "https://")):
        url = ref if "://" in ref else f"https://{ref}"
        parts = [p for p in urlparse(url).path.split("/") if p]
        if parts[:1] == ["share"]:
            return "share", url
        for i, part in enumerate(parts[:-1]):
            if part in ("reels", "reel") and parts[i + 1] == "audio":
                return ("audio", parts[i + 2]) if len(parts) > i + 2 and parts[i + 2].isdigit() else (None, ref)
            if part in ("p", "reel", "reels", "tv"):
                return "code", parts[i + 1]
        return None, ref
    if re.fullmatch(r"\d+(_\d+)?", ref):
        return "pk", ref.split("_")[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", ref):
        return "code", ref
    return None, ref


def username_from(handle):
    """'@name', 'name' or a profile URL -> 'name'."""
    handle = str(handle or "").strip()
    if "instagram.com" in handle:
        parts = [p for p in urlparse(handle if "://" in handle else f"https://{handle}").path.split("/") if p]
        handle = parts[0] if parts else ""
    return handle.lstrip("@").strip().lower()


def _is_media(d):
    return isinstance(d.get("code"), str) and "taken_at" in d and "media_type" in d and bool(d.get("pk") or d.get("id"))


def _media_key(node):
    return str(node.get("pk") or str(node.get("id")).split("_")[0])


def media_nodes(data):
    """Every media dict inside a raw private-API response, in order, once each (carousel children skipped)."""
    found, seen = [], set()

    def walk(x):
        if isinstance(x, dict):
            if _is_media(x):
                if _media_key(x) not in seen:
                    seen.add(_media_key(x))
                    found.append(x)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return found


def next_cursor(raw):
    """Next-page cursor of a raw response, or None when Instagram says there is no more."""
    if not isinstance(raw, dict):
        return None
    paging, grid = raw.get("paging_info") or {}, raw.get("media_grid") or {}
    flags = (raw.get("has_more"), raw.get("more_available"), paging.get("more_available"), grid.get("has_more"))
    more = next((f for f in flags if f is not None), None)
    cursor = (raw.get("reels_max_id") or raw.get("next_max_id") or paging.get("max_id") or raw.get("max_id")
              or grid.get("next_max_id"))
    return str(cursor) if cursor and more is not False else None


def _count(node, keys, hidden=False):
    for key in keys:
        value = to_int(node.get(key))
        if value is not None:
            return None if hidden and value == 0 else value
    return None


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(float(value), 2)


def _is_ad(node):
    return node.get("product_type") == "ad" or bool(node.get("ad_id"))


def _paid(node):
    if node.get("sponsor_tags") or _is_ad(node):
        return True
    value = node.get("is_paid_partnership")
    return value if isinstance(value, bool) else None


def sound_of(node):
    """(sound_id, title, author) of a media dict or of a clips/music `metadata` dict. Nones when absent.

    sound_id is the audio_cluster_id of licensed music or the audio_asset_id of an original sound,
    the id in instagram.com/reels/audio/<id>/.
    """
    meta = node.get("clips_metadata") or node.get("music_metadata") or node
    music = (meta.get("music_info") or {}).get("music_asset_info") or {}
    original = meta.get("original_sound_info") or {}
    if music:
        sid, title, author = music.get("audio_cluster_id") or music.get("id"), music.get("title"), music.get("display_artist")
    elif original:
        sid, title = original.get("audio_asset_id"), original.get("original_audio_title")
        author = (original.get("ig_artist") or {}).get("username")
    else:
        attr = node.get("clips_music_attribution_info") or {}
        sid, title, author = attr.get("audio_id"), attr.get("song_name"), attr.get("artist_name")
    return (str(sid) if sid not in (None, "") else None), title or None, author or None


def music_canonical_id(node):
    """The licensed-music id some reels carry instead of music_info (clips_metadata.music_canonical_id), or None."""
    meta = node.get("clips_metadata") or node.get("music_metadata") or {}
    value = meta.get("music_canonical_id") if isinstance(meta, dict) else None
    return str(value) if value not in (None, "") else None


def video_from_node(node, route, user=None):
    """One raw media dict -> Video. Views are play_count (Reels), else view_count (older video types)."""
    code = node.get("code")
    owner = node.get("user") or node.get("owner") or {}
    author_id = getattr(user, "pk", None) or owner.get("pk") or owner.get("id")
    username = getattr(user, "username", None) or owner.get("username")
    followers = getattr(user, "follower_count", None) if user is not None else owner.get("follower_count")
    caption = node.get("caption")
    caption = caption.get("text") if isinstance(caption, dict) else caption if isinstance(caption, str) else None
    is_video = node.get("media_type") == 2
    hidden = bool(node.get("like_and_view_counts_disabled"))
    sound_id, sound_title, sound_author = sound_of(node)
    kind = "reel" if node.get("product_type") == "clips" else "p"
    return make_video(
        platform="instagram",
        id=_media_key(node),
        url=f"https://www.instagram.com/{kind}/{code}/" if code else None,
        caption=caption or None,
        author_id=str(author_id) if author_id else None,
        author_username=username,
        author_url=f"https://www.instagram.com/{username}/" if username else None,
        author_followers=to_int(followers),
        views=_count(node, VIEW_KEYS, hidden) if is_video else None,
        likes=_count(node, ("like_count",), hidden),
        comments=_count(node, ("comment_count",)),
        shares=_count(node, ("reshare_count", "share_count")),
        posted_at=iso_from_timestamp(node.get("taken_at")),
        duration_s=_duration(node.get("video_duration")) if is_video else None,
        sound_id=sound_id,
        sound_title=sound_title,
        sound_author=sound_author,
        is_paid=_paid(node),
        route=route,
        raw_ref=code or _media_key(node),
    )


def clean_sessionid(value):
    """The sessionid cookie in the URL-encoded form browsers store it in ('123%3Aabc...')."""
    value = str(value or "").strip().strip("'\"").strip()
    if value.startswith("sessionid="):
        value = value[len("sessionid="):]
    value = quote(unquote(value.split(";")[0].strip()), safe="")
    if len(value) <= 30 or not re.match(r"\d+%3A", value):
        raise RouteError(f"that is not an Instagram sessionid cookie. {SESSIONID_HELP}")
    return value


# ---- client, session, routes --------------------------------------------------------------------

def _delay():
    return settings.get("request_delay_seconds") or 0


def _throttle():
    """Space Instagram requests in this process at least request_delay_seconds apart."""
    wait = 0 if _last_call[0] is None else _last_call[0] + _delay() - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


async def _call(fn, *args, **kwargs):
    """Call a client method that makes a request: sync for instagrapi, a coroutine for aiograpi."""
    _throttle()
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _client(lib):
    """A logged-in client of `lib` loaded from the session file. No request is made."""
    path = session_path()
    if not path.is_file():
        raise NotConfigured(NEEDS_LOGIN)
    try:
        client_class = importlib.import_module(lib).Client
    except ImportError as exc:
        raise NotConfigured(f"{lib} is not installed: run /format-finder:setup") from exc
    cl = client_class(with_challenge_flow=False)
    try:
        cl.load_settings(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NotConfigured(f"the Instagram session file {path} is unreadable ({exc}): {LOGIN_HINT}") from exc
    if not cl.user_id or not cl.sessionid:
        raise NotConfigured(f"the Instagram session file {path} has no login in it: {LOGIN_HINT}")
    return cl


async def _close(cl):
    for name in ("private", "public", "graphql"):
        session = getattr(cl, name, None)
        if hasattr(session, "__aexit__"):
            with contextlib.suppress(Exception):
                await session.__aexit__(None, None, None)


def _session_problem(lib, exc):
    """The error to raise when `exc` means this session must stop for now (login, checkpoint, rate limit), else None."""
    try:
        errors = importlib.import_module(f"{lib}.exceptions")
    except ImportError:
        return None

    def classes(*names):
        return tuple(c for c in (getattr(errors, n, None) for n in names) if isinstance(c, type))

    if isinstance(exc, classes("LoginRequired")):
        return NotConfigured(EXPIRED)
    if isinstance(exc, classes("ChallengeError", "ChallengeRequired")):
        return NotConfigured(CHECKPOINT)
    if isinstance(exc, classes("PleaseWaitFewMinutes", "RateLimitError", "FeedbackRequired", "ClientThrottledError")):
        return RouteError(f"Instagram asked to slow down ({type(exc).__name__}: {exc}); try again later")
    return None


def _missing(lib, exc):
    """common.NotFound when `exc` is the library saying the account or media does not exist, else None. Route memory,
    the route fallback and the scan then treat a bad handle or link as an answer, not as a failing route."""
    try:
        errors = importlib.import_module(f"{lib}.exceptions")
    except ImportError:
        return None
    for name, what in (("UserNotFound", "account"), ("MediaNotFound", "reel")):
        cls = getattr(errors, name, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return NotFound(f"Instagram has no such {what} ({name}: {exc}). Check the handle or link (this is not a "
                            "login problem)")
    return None


async def _run(lib, op, *args, **kwargs):
    cl = _client(lib)
    if _blocked[0] is not None:
        raise type(_blocked[0])(f"{_blocked[0]} (not retried with this route)")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            return await op(cl, lib, *args, **kwargs)
    except Exception as exc:
        missing = _missing(lib, exc)
        if missing is not None:
            raise missing from exc
        stop = _session_problem(lib, exc)
        if stop is None:
            raise
        _blocked[0] = stop
        raise stop from exc
    finally:
        await _close(cl)


def _route(lib, op, *args, **kwargs):
    return asyncio.run(_run(lib, op, *args, **kwargs))


def _routes(op, browser_fn=None):
    """The session routes, then the browser route last (when given, unless the browser_route setting is false)."""
    routes = [(lib, functools.partial(_route, lib, op)) for lib in LIBS]
    if browser_fn is not None and settings.get("browser_route"):
        routes.append(("browser", browser_fn))
    return routes


def _pages_for(limit):
    return max(1, min(MAX_PAGES, math.ceil(limit / PAGE_ITEMS) + 1))


async def _collect(fetch, limit, keep=None, pages=None, route=None):
    """(media nodes, first raw page) from fetch(cursor, previous_raw) pages, until limit, the last page or the cap.

    A later page that fails keeps the reels already read, unless the session must stop (_session_problem).
    """
    nodes, seen, raw, first, cursor = [], set(), None, None, None
    for page in range(pages or _pages_for(limit)):
        try:
            raw = await fetch(cursor, raw)
        except Exception as exc:  # noqa: BLE001 - re-raised unless earlier pages can be kept
            if not page or not nodes or (route and _session_problem(route, exc)):
                raise
            log(f"[{route}] page {page + 1} failed, keeping {len(nodes)} reels: {type(exc).__name__}: {exc}")
            break
        first = raw if first is None else first
        for node in media_nodes(raw):
            key = _media_key(node)
            if key not in seen and (keep is None or keep(node)):
                seen.add(key)
                nodes.append(node)
        cursor = next_cursor(raw)
        if len(nodes) >= limit or not cursor:
            break
    return nodes[:limit], first


def _rank_token(raw):
    return raw.get("rank_token") if isinstance(raw, dict) else None


async def _pk(cl, ref):
    kind, value = parse_ref(ref)
    if kind == "pk":
        return value
    if kind == "code":
        return pk_from_code(value)
    if kind == "share":
        return str(await _call(cl.media_pk_from_url, value))
    raise RouteError(f"not an Instagram reel URL, shortcode or media id: {ref}")


async def _media_node(cl, ref):
    pk = await _pk(cl, ref)
    nodes = media_nodes(await _call(cl.private_request, f"media/{pk}/info/"))
    if not nodes:
        raise RouteError(f"Instagram returned no media for {ref}")
    return nodes[0]


async def _search(cl, route, query, days=None, limit=30):
    q = keywords(query)
    if not q:
        raise RouteError("empty search phrase")
    limit = int(limit or 30)
    cutoff = time.time() - days * 86400 if days else None

    def keep(node):
        ts = to_int(node.get("taken_at"))
        return node.get("media_type") == 2 and not _is_ad(node) and (cutoff is None or (ts is not None and ts >= cutoff))

    def reels(cursor, prev):
        return _call(cl.fbsearch_reels_v2, q, reels_max_id=cursor, rank_token=_rank_token(prev))

    nodes, first = await _collect(reels, limit, keep, route=route)
    if not media_nodes(first):
        log(f"[{route}] Reels search gave no reels for {q!r}; reading reels from the keyword Top tab")
        nodes, _ = await _collect(lambda cursor, prev: _call(cl.fbsearch_topsearch_v2, q), limit,
                                  lambda n: keep(n) and n.get("product_type") == "clips", pages=1, route=route)
    return [video_from_node(n, route) for n in nodes]


async def _account(cl, route, handle, limit=20):
    username = username_from(handle)
    if not username:
        raise RouteError(f"not an Instagram username or profile URL: {handle}")
    limit = int(limit or 20)
    user = await _call(cl.user_info_by_username, username)

    def clips(cursor, prev):
        data = {"target_user_id": int(user.pk), "max_id": cursor or "", "page_size": min(limit + PINNED_MAX, 50),
                "include_feed_video": "true"}
        return _call(cl.private_request, "clips/user/", data=data)

    nodes, _ = await _collect(clips, limit + PINNED_MAX, lambda n: n.get("media_type") == 2, route=route)
    posts = sorted((video_from_node(n, route, user=user) for n in nodes), key=lambda v: v["posted_at"] or "", reverse=True)
    private = getattr(user, "is_private", None)
    return make_account(platform="instagram", id=str(user.pk), username=user.username,
                        url=f"https://www.instagram.com/{user.username}/", followers=to_int(user.follower_count),
                        post_count=to_int(getattr(user, "media_count", None)),
                        is_private=private if isinstance(private, bool) else None, posts=posts[:limit], route=route)


async def _video(cl, route, ref):
    return video_from_node(await _media_node(cl, ref), route)


async def _audio_reels(cl, route, audio_id, limit, title=None, author=None):
    """Reels on one audio page: clips/music first, the streamed audio page when that has none."""
    def is_video(node):
        return node.get("media_type") == 2

    nodes, first = await _collect(lambda cursor, prev: _call(cl.track_info_by_id, audio_id, max_id=cursor or ""),
                                  limit, is_video, route=route)
    if not nodes:
        try:
            nodes, first = await _collect(
                lambda cursor, prev: _call(cl.track_stream_info_by_id, audio_id, max_id=cursor or ""), limit, is_video,
                route=route)
        except Exception as exc:  # noqa: BLE001 - the plain audio page already answered; this is a second look
            if _session_problem(route, exc):
                raise
            log(f"[{route}] streamed audio page failed: {type(exc).__name__}: {exc}")
    meta = first.get("metadata") if isinstance(first, dict) else None
    _, meta_title, meta_author = sound_of(meta or {})
    videos = []
    for node in nodes:
        v = video_from_node(node, route)
        v.update(sound_id=v["sound_id"] or str(audio_id), sound_title=v["sound_title"] or meta_title or title,
                 sound_author=v["sound_author"] or meta_author or author)
        videos.append(v)
    return videos


async def _sound(cl, route, ref, limit=30):
    kind, value = parse_ref(ref)
    limit = int(limit or 30)
    if kind in ("code", "share"):
        node = await _media_node(cl, ref)
        audio_id = sound_of(node)[0]
        if audio_id:
            return await _audio_reels(cl, route, audio_id, limit)
        canonical = music_canonical_id(node)
        if not canonical:
            raise RouteError(f"{ref} has no audio to follow")
        track = await _call(cl.track_info_by_canonical_id, canonical)
        return await _audio_reels(cl, route, str(track.audio_cluster_id), limit, title=track.title,
                                  author=track.display_artist)
    if kind not in ("audio", "pk"):
        raise RouteError(f"not an Instagram audio id, music canonical id or reel: {ref}")
    first_error = None
    try:
        videos = await _audio_reels(cl, route, value, limit)
    except Exception as exc:  # noqa: BLE001 - a bare number may be a music canonical id instead
        if kind == "audio" or _session_problem(route, exc):
            raise
        log(f"[{route}] {value} is not an audio id ({type(exc).__name__}: {exc}); trying it as a music canonical id")
        videos, first_error = [], exc
    if videos or kind == "audio":
        return videos
    try:
        track = await _call(cl.track_info_by_canonical_id, value)
    except Exception as exc:  # noqa: BLE001 - an audio page with no reels is an empty answer, not an error
        if first_error is None and not _session_problem(route, exc):
            return videos
        raise
    cluster = str(track.audio_cluster_id)
    if cluster == value:
        return videos
    return await _audio_reels(cl, route, cluster, limit, title=track.title, author=track.display_artist)


async def _download(cl, route, ref, out_dir):
    pk = await _pk(cl, ref)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = await _call(cl.clip_download, int(pk), folder)
    return {"video_path": str(Path(path).resolve()) if path else None, "captions_text": None, "captions_source": None}


# ---- platform interface (DESIGN.md) -------------------------------------------------------------

def search(query, days=None, limit=30, region=None):
    """Reels for a keyword phrase from the Reels search tab, posted in the last `days`. Instagram has no region filter.
    The browser route runs only when the instagram_browser_search setting is true (BROWSER_SEARCH_OFF says why);
    its results carry no views (instagram.com's search gives none)."""
    browser_fn = _browser_search if settings.get("instagram_browser_search") else _browser_search_off
    return try_routes(_routes(_search, browser_fn), query, days=days, limit=limit, _op="instagram.search")


def account(handle, limit=20):
    """Follower count and the newest `limit` reels (play counts) of a username or profile URL."""
    return try_routes(_routes(_account, _browser_account), handle, limit=limit, _op="instagram.account")


def video(ref):
    """One reel by URL, shortcode or media pk. The browser route is left out for a share link or an audio link
    (_browser_code), so it records no failure for that call."""
    return try_routes(_routes(_video, _browser_video if _browser_code(ref) else None), ref, _op="instagram.video")


def sound(ref, limit=30):
    """Reels using an audio: an audio id or instagram.com/reels/audio/<id>/ URL, a music canonical id, or a reel URL/code."""
    return try_routes(_routes(_sound), ref, limit=limit, _op="instagram.sound")


def download(ref, out_dir):
    """Save the reel's MP4 into out_dir. Instagram has no captions, so captions_text is None. The browser route is left
    out for a share link or an audio link (_browser_code), like video()."""
    return try_routes(_routes(_download, _browser_download if _browser_code(ref) else None), ref, out_dir,
                      _op="instagram.download")


# ---- last route: instagram.com's own pages in a headless browser (browser.py) -------------------
# It needs a login the person made themselves in the tool's browser profile (format-finder browser-login instagram).
# The page's own scripts make their own requests; the tool reads the replies and the page's embedded JSON over
# DevTools and types nothing into the page. What instagram.com loaded on 2026-11-05 (one Mac, logged in):
# - keyword search (/explore/search/keyword/?q=): /api/graphql replies with data.xdt_fbsearch__top_serp_graphql,
#   24 media each (edges[].node.items[]) with taken_at, like_count, comment_count, caption and user, but view_count
#   null and no play_count: the web search gives no views. Scrolling loaded one more reply per scroll.
# - a profile's Reels tab (/<username>/reels/): data.fetch__XDTUserDict.clips_connection replies (12 reels, then 3
#   per reply) with play_count, like_count and comment_count but no taken_at; pinned reels list the profile's id in
#   clips_tab_pinned_user_ids. A data.user reply carries follower_count (data.viewer is the logged-in person).
# - a reel page (/reel/<code>/, which went on to /reels/<code>/): <script type="application/json"> documents with
#   data.xig_fetch_media: taken_at, likes, comments, caption, user, clips_metadata (sound), a DASH manifest
#   (duration) and video_versions (progressive MP4 files on Instagram's CDN, 720x1280 for one reel); no play_count.
# - a profile page for a username that does not exist: GraphQL replies arrive (inbox, feed, notifications) but no
#   data.user, and the page shows Instagram's "page isn't available" notice (PROFILE_MISSING_JS).
# - Another of the reel page's scripts (the page's config) held the csrf and DTSG tokens, so only the parts under
#   REEL_KEYS are picked inside the page (browser.JsonScripts keys) and nothing else crosses DevTools.
# - the keyword search page also asked for tag information about the keyword (fetch__XDTTagInfo) by itself, so the
#   browser route for search is off unless the instagram_browser_search setting is true.

WEB = "https://www.instagram.com"
REEL_KEYS = ("xig_fetch_media", "xdt_api__v1__clips__home__connection_v2")
BROWSER_SEARCH_OFF = ("Instagram search through the browser is off: instagram.com's keyword search page sends its own "
                      "tag-information request (fetch__XDTTagInfo) for the keyword. Set instagram_browser_search to "
                      f"true in the settings file to allow it, or use {TERMINAL_COMMAND} instagram-login for the "
                      "session routes")
GRAPHQL = r"^/(?:api/)?graphql"
BROWSER_SEARCH_PAGES = 8   # search replies read per search at most (24 media each)
BROWSER_REEL_PAGES = 8     # Reels-tab replies read per profile at most (12 reels, then 3 each)
BROWSER_NEEDS_LOGIN = (f"Instagram needs a login in the tool's browser: run {TERMINAL_COMMAND} browser-login "
                       f"instagram in your own terminal and log in yourself (or use {TERMINAL_COMMAND} "
                       "instagram-login for the faster routes)")
USERNAME_RE = re.compile(r"[a-z0-9._]{1,30}")
# A profile page for a username Instagram does not have: its "page isn't available" notice, found by words (English)
# or by its shape in any language (a <main> with a link home, no profile header and no og:title), as instagram.com
# showed it on 2026-11-05 for two handles it does not have.
PROFILE_MISSING_JS = r"""(() => {
  const text = document.body ? document.body.innerText.slice(0, 5000) : '';
  if (/Sorry, this page isn't available|Profile isn't available/.test(text)) return true;
  const main = document.querySelector('main');
  return !!main && !main.querySelector('header') && !!main.querySelector('a[href="/"]')
    && !document.querySelector('meta[property="og:title"]');
})()"""
DOWNLOAD_WIDTH = 480  # the smallest video file at least this wide is saved (else the smallest there is)
DOWNLOAD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/130.0.0.0 Safari/537.36")
DASH_DURATION = re.compile(r'mediaPresentationDuration="PT(?:([\d.]+)H)?(?:([\d.]+)M)?(?:([\d.]+)S)?"')


def _dash_duration(manifest):
    """Seconds in a DASH manifest's mediaPresentationDuration ("PT42.5S"), or None."""
    match = DASH_DURATION.search(manifest) if isinstance(manifest, str) else None
    if not match:
        return None
    hours, minutes, seconds = (float(x) if x else 0.0 for x in match.groups())
    return _duration(hours * 3600 + minutes * 60 + seconds)


def _web_video(node, user=None):
    """A media dict from instagram.com's own replies -> Video (route browser). A video's link is /reel/<code>/ (web
    search items have no product_type); the duration comes from the DASH manifest when there is no video_duration."""
    v = video_from_node(node, "browser", user=user)
    if node.get("media_type") == 2 and node.get("code"):
        v["url"] = f"{WEB}/reel/{node['code']}/"
        if v["duration_s"] is None:
            v["duration_s"] = _dash_duration(node.get("video_dash_manifest"))
    return v


def _graphql_docs(reply):
    """The JSON documents of a /api/graphql reply the page received ([] when it failed or is not JSON)."""
    if reply.get("error") or (reply.get("status") or 200) >= 400:
        return []
    body = (reply.get("body") or "").strip()
    try:
        docs = [json.loads(body)]
    except ValueError:  # a streamed reply: one document per line
        docs = []
        for line in body.splitlines():
            with contextlib.suppress(ValueError):
                docs.append(json.loads(line))
    return [d for d in docs if isinstance(d, dict)]


def _data(doc):
    data = doc.get("data")
    return data if isinstance(data, dict) else {}


def _gql_error(doc):
    errors = doc.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    return first.get("message") or first.get("summary") or "no message"


def _web_failure(what, summary, error=None):
    """Why an instagram.com page gave nothing: a login page (NotConfigured), another wall, a reply error, a timeout."""
    from .browser import BotCheck, is_captcha
    blocked = summary.get("blocked")
    ended_at = urlparse(summary.get("url") or "").path
    if (blocked and ("login page" in blocked or "login wall" in blocked)) \
            or re.match(r"/(accounts/login|challenge)(/|$)", ended_at):
        return NotConfigured(BROWSER_NEEDS_LOGIN)
    if blocked:
        return BotCheck(f"Instagram showed {blocked} on the {what} page instead of results. Try again later",
                        captcha=is_captcha(blocked))
    if error is not None:
        return error
    status = f" (the page answered HTTP {summary['status']})" if (summary.get("status") or 200) >= 400 else ""
    if summary.get("timed_out"):
        return RouteError(f"Instagram's {what} page gave no results within {summary.get('seconds')}s{status}")
    return RouteError(f"Instagram's {what} page loaded but carried no results{status}")


def _browser_search_off(query, days=None, limit=30, region=None):
    """The browser route for search while instagram_browser_search is false: it opens nothing."""
    raise NotConfigured(BROWSER_SEARCH_OFF)


def _browser_search(query, days=None, limit=30, region=None):
    """Keyword search on instagram.com/explore/search/keyword/: videos only, no ads, inside `days`. The page has no
    date filter, so more replies are read for videos inside the window. Views stay None: the web search has none."""
    from . import browser
    q = keywords(query)
    if not q:
        raise RouteError("empty search phrase")
    limit = int(limit or 30)
    cutoff = time.time() - days * 86400 if days else None
    state = {"pages": 0, "more": True, "error": None, "nodes": [], "seen": set()}

    def keep(node):
        ts = to_int(node.get("taken_at"))
        return node.get("media_type") == 2 and not _is_ad(node) and (cutoff is None or (ts is not None and ts >= cutoff))

    def take(reply):
        for doc in _graphql_docs(reply):
            data = _data(doc)
            if "xdt_fbsearch__top_serp_graphql" not in data:
                continue
            serp = data["xdt_fbsearch__top_serp_graphql"]
            if not isinstance(serp, dict):
                state["error"] = RouteError(f"Instagram's search reply carried no results: {_gql_error(doc)}")
                return True
            state["pages"] += 1
            nodes = media_nodes(serp.get("edges"))
            state["more"] = bool((serp.get("page_info") or {}).get("has_next_page")) and bool(nodes)
            for node in nodes:
                key = _media_key(node)
                if key not in state["seen"] and keep(node):
                    state["seen"].add(key)
                    state["nodes"].append(node)
            if not state["more"] or len(state["nodes"]) >= limit or state["pages"] >= BROWSER_SEARCH_PAGES:
                return True
        return False

    _throttle()
    summary = browser.tab().capture(f"{WEB}/explore/search/keyword/?q={quote(q)}", pattern=GRAPHQL, on_reply=take,
                                    timeout=settings.get("browser_search_timeout_seconds"),
                                    max_scrolls=BROWSER_SEARCH_PAGES, scroll_gap=_delay())
    if not state["pages"]:
        raise _web_failure("search", summary, state["error"])
    found = state["nodes"][:limit]
    if len(found) < limit and state["more"]:
        why = state["error"] or summary.get("blocked") or (
            "page limit" if state["pages"] >= BROWSER_SEARCH_PAGES else "no more replies loaded")
        log(f"[browser] Instagram search {q!r} stopped after {state['pages']} replies, {len(found)} videos in the "
            f"window: {why}")
    return [_web_video(n) for n in found]


def _browser_account(handle, limit=20):
    """A profile's Reels tab: reels with views (play_count) in grid order, newest first, pinned reels and reels
    another account posted left out; follower count and id from the page's data.user reply. No dates (the tab's
    replies carry none): posted_at is None.

    No Reels-tab reply (or one with an error) is a RouteError even when the profile reply came; an empty post list is
    returned only for a private profile the viewer does not follow, or a Reels-tab reply that lists no reels."""
    from . import browser
    username = username_from(handle)
    if not USERNAME_RE.fullmatch(username):
        raise RouteError(f"not an Instagram username or profile URL: {handle}")
    limit = int(limit or 20)
    state = {"user": None, "pages": [], "more": True, "closed": False, "error": None, "missing": False}

    def owner_id():
        user = state["user"] or {}
        uid = user.get("pk") or user.get("id")
        return str(uid) if uid else next((p[0] for p in state["pages"] if p[0]), None)

    def own_reels():
        uid, out, seen = owner_id(), [], set()
        for page_owner, media in state["pages"]:
            if uid and page_owner and page_owner != uid:
                continue
            for node in media:
                key = _media_key(node)
                author = (node.get("user") or {}).get("pk") or (node.get("user") or {}).get("id")
                pinned = [str(x) for x in node.get("clips_tab_pinned_user_ids") or []]
                if key in seen or node.get("media_type") != 2 or _is_ad(node) or (uid in pinned if uid else pinned) \
                        or (uid and author and str(author) != uid):
                    continue
                seen.add(key)
                out.append(node)
        return out

    def take(reply):
        for doc in _graphql_docs(reply):
            data = _data(doc)
            user = data.get("user")
            if isinstance(user, dict) and str(user.get("username") or "").lower() == username:
                state["user"] = user
                following = (user.get("friendship_status") or {}).get("following")
                state["closed"] = bool(user.get("is_private")) and not following
            holder = data.get("fetch__XDTUserDict")
            clips = holder.get("clips_connection") if isinstance(holder, dict) else None
            if isinstance(clips, dict):
                media = [(e.get("node") or {}).get("media") for e in clips.get("edges") or [] if isinstance(e, dict)]
                media = [m for m in media if isinstance(m, dict) and m.get("code") and (m.get("pk") or m.get("id"))]
                state["pages"].append((str(holder["id"]) if holder.get("id") else None, media))
                state["more"] = bool((clips.get("page_info") or {}).get("has_next_page")) and bool(media)
            elif "fetch__XDTUserDict" in data and (not isinstance(holder, dict) or "clips_connection" in holder):
                state["error"] = RouteError(f"Instagram's Reels-tab reply for @{username} carried no reels: "
                                            f"{_gql_error(doc)}")
        if state["user"] is None:
            return False
        return state["closed"] or len(state["pages"]) >= BROWSER_REEL_PAGES or (
            bool(state["pages"]) and (not state["more"] or len(own_reels()) >= limit))

    def gone(tab):
        """At a quiet moment with no profile yet: does the page say the profile does not exist?"""
        if state["user"] is not None or state["pages"]:
            return False
        try:
            state["missing"] = bool(tab.evaluate(PROFILE_MISSING_JS))
        except RouteError:
            return False
        return state["missing"]

    _throttle()
    summary = browser.tab().capture(f"{WEB}/{username}/reels/", pattern=GRAPHQL, on_reply=take,
                                    timeout=settings.get("browser_page_timeout_seconds"),
                                    max_scrolls=BROWSER_REEL_PAGES, scroll_gap=_delay(), on_idle=gone)
    user = state["user"] or {}
    if state["missing"]:
        raise NotFound(f"Instagram has no profile @{username}: its page says the page isn't available. Check the "
                       "handle (this is not a login problem)")
    if not state["pages"] and not state["closed"]:
        why = state["error"]
        if why is None and user:
            waited = f" within {summary.get('seconds')}s" if summary.get("timed_out") else ""
            why = RouteError(f"Instagram's profile page for @{username} sent the profile but no Reels-tab reply{waited}")
        raise _web_failure("profile", summary, why)
    uid, name = owner_id(), user.get("username") or username
    followers = to_int(user.get("follower_count"))
    if not user:
        log(f"[browser] @{username}: the page sent no profile reply, so the follower count is unknown")
    reels = own_reels()
    if len(reels) < limit and state["more"] and state["pages"]:
        log(f"[browser] @{username}: Reels tab stopped after {len(state['pages'])} replies, {len(reels)} reels: "
            f"{summary.get('blocked') or 'no more replies loaded'}")
    owner = SimpleNamespace(pk=uid, username=name, follower_count=followers)
    private = user.get("is_private")
    return make_account(platform="instagram", id=uid, username=name, url=f"{WEB}/{name}/", followers=followers,
                        post_count=to_int(user.get("media_count")),
                        is_private=private if isinstance(private, bool) else None,
                        posts=[_web_video(n, owner) for n in reels[:limit]], route="browser")


def _browser_code(ref):
    """The shortcode the browser route opens for `ref` (a reel or post link, a shortcode or a media pk), else None."""
    kind, value = parse_ref(ref)
    if kind == "code":
        return value
    if kind == "pk":
        return code_from_pk(value)
    return None


def _reel_node(docs, code):
    """The media dict for `code` in a reel page's embedded documents: xig_fetch_media first, else any media dict."""
    def found(x, key):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == key:
                    yield v
                yield from found(v, key)
        elif isinstance(x, list):
            for v in x:
                yield from found(v, key)

    for node in found(docs, "xig_fetch_media"):
        if isinstance(node, dict) and node.get("code") == code:
            return node
    return next((n for n in media_nodes(docs) if n.get("code") == code), None)


def _browser_reel(ref):
    """(code, media dict) from a reel page's embedded JSON (xig_fetch_media). Only the parts under REEL_KEYS are read
    from the page's scripts (picked inside the page)."""
    from . import browser
    code = _browser_code(ref)
    if not code:
        raise RouteError(f"the browser route opens a reel by its link, shortcode or media id, not {ref!r}")
    state = {}

    def got(docs):
        state["node"] = _reel_node(docs, code)
        return True

    _throttle()
    summary = browser.tab().capture(f"{WEB}/reel/{code}/", embedded=browser.JsonScripts(code, keys=REEL_KEYS),
                                    on_embedded=got,
                                    timeout=settings.get("browser_page_timeout_seconds"))
    if not state.get("node"):
        raise _web_failure("reel", summary)
    return code, state["node"]


def _browser_video(ref):
    """A reel page: everything comes from the page's embedded JSON (xig_fetch_media). Views stay None (none there)."""
    return _web_video(_browser_reel(ref)[1])


def _download_rank(version):
    """Sort key for video_versions, best first: the files at least DOWNLOAD_WIDTH wide before the others; among
    them, files that list both sizes before one missing its height; then the smallest (area, or width alone)."""
    width, height = to_int(version.get("width")), to_int(version.get("height"))
    return ((width or 0) < DOWNLOAD_WIDTH, height is None, (width or 0) * (height or 1))


def _browser_download(ref, out_dir):
    """The reel's MP4 from the video files its own page lists (video_versions, progressive MP4 with sound), fetched
    with plain urllib: no cookie is read or sent. The file link is signed by Instagram and is never printed."""
    import urllib.error
    import urllib.request
    code, node = _browser_reel(ref)
    versions = [v for v in node.get("video_versions") or [] if isinstance(v, dict) and v.get("url")]
    if not versions:
        raise RouteError("the reel page lists no video file (video_versions)")
    pick = min(versions, key=_download_rank)
    req = urllib.request.Request(pick["url"], headers={"User-Agent": DOWNLOAD_UA, "Referer": f"{WEB}/"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=common.ssl_context()) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RouteError(f"Instagram's video file answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:  # the reason, never the signed link
        reason = getattr(exc, "reason", None)
        raise RouteError(f"Instagram's video file could not be fetched: {type(exc).__name__}"
                         + (f" ({type(reason).__name__})" if reason is not None else "")) from None
    if body[4:8] != b"ftyp":
        raise RouteError("Instagram's video file is not an MP4")
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{code}.mp4"
    path.write_bytes(body)
    return {"video_path": str(path.resolve()), "captions_text": None, "captions_source": None}


# ---- login (CLI: instagram-login) ---------------------------------------------------------------

def _read_settings_file(path):
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RouteError(f"settings file not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise RouteError(f"settings file is not readable JSON: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise RouteError(f"settings file is not an instagrapi settings object: {path}")
    auth, cookies = data.get("authorization_data") or {}, data.get("cookies") or {}
    if not (auth.get("sessionid") or cookies.get("sessionid")):
        raise RouteError(f"settings file has no sessionid in it, so it is not a logged-in session: {path}")
    return data


def _save_session(cl, path):
    """dump_settings to a private (0600) temp file, then move it over the session file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    cl.dump_settings(tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def login(sessionid=None, settings_file=None):
    """Check a browser sessionid cookie or an instagrapi settings file with one request and save it as the session."""
    if bool(sessionid) == bool(settings_file):
        raise RouteError("give exactly one of --sessionid or --settings-file")
    data = None if sessionid else _read_settings_file(settings_file)
    sessionid = clean_sessionid(sessionid) if sessionid else None
    try:
        from instagrapi import Client
    except ImportError as exc:
        raise NotConfigured("instagrapi is not installed: run /format-finder:setup") from exc
    cl = Client(with_challenge_flow=False)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if sessionid:
                cl.login_by_sessionid(sessionid)
                username = cl.username
            else:
                cl.set_settings(data)
                if not cl.user_id:
                    raise RouteError("the settings file has no ds_user_id")
                username = cl.user_info_v1(cl.user_id).username
    except Exception as exc:  # noqa: BLE001 - every failure gets the same instruction
        raise RouteError(f"Instagram did not accept this session ({type(exc).__name__}: {exc}). {SESSIONID_HELP} "
                         f"Then {LOGIN_HINT}") from exc
    if not username:
        raise RouteError(f"Instagram did not say which account this session belongs to. {SESSIONID_HELP}")
    path = session_path()
    _save_session(cl, path)
    _blocked[0] = None
    return {"ok": True, "username": username, "session_path": str(path)}

