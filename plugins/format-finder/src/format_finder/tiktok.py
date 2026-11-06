"""TikTok: omkarcloud tiktok-scraper (route 1), your own Douyin_TikTok_Download_API (route 2),
yt-dlp (partial fallback for account, video and download), and last TikTok's own web pages in a headless
browser (search, account, video), which is slower and runs only when the direct routes fail (the browser_route
setting turns it off). Route memory (common.try_routes, _op) skips a route that keeps failing within a scan.

Search is by keyword only. No hashtag endpoint or page is called anywhere in this module.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from . import settings
from .common import (FF_HOME, TERMINAL_COMMAND, NotConfigured, NotFound, RouteError, iso_from_timestamp, keywords,
                     log, make_account, make_video, pause, to_int, try_routes)

VENDOR = FF_HOME / "vendor" / "tiktok-scraper"
SITE = "https://www.tiktok.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# omkarcloud's `published` filter values (tiktok/search.py PUBLISHED), smallest window first.
PUBLISHED = (("day", 1), ("week", 7), ("month", 30), ("3months", 90), ("6months", 180))
SEC_UID_RE = re.compile(r"^MS4wLjABAAAA[A-Za-z0-9_-]{20,120}$")
VIDEO_ID_RE = re.compile(r"/(?:video|photo|v)/(\d{15,25})")
MIN_SHORT_SIDE = 480          # smallest rendition still good for frames
MAX_LINKS = 6                 # media links tried per download

DTK_WAIT = 25                 # ?wait= on content reads; the instance ceiling defaults to 30
DTK_DEADLINE = 120            # seconds a task or a stored download is polled
DTK_PAGE = 35                 # TikTok page ceiling on DTK
DTK_RETRYABLE = {"RATE_LIMITED", "QUEUE_FULL", "IDENTITY_POOL_EXHAUSTED", "ENDPOINT_CIRCUIT_OPEN",
                 "UPSTREAM_RISK_CONTROL", "SIGNING_FAILED", "UPSTREAM_UNREACHABLE", "INTERNAL",
                 "DOWNLOADER_UNAVAILABLE"}
# webapp.user-detail statusCode values: no such user (the vendored scraper's tiktok/shared.py USER_NOT_FOUND), and
# the one TikTok gives a profile it keeps hidden ("ErrBizUserSecret", seen on 2026-11-05).
USER_NOT_FOUND = (10202, 10221, 10223)
USER_SECRET = 10222


# ---- interface -----------------------------------------------------------------------

def search(query, days=None, limit=30, region=None):
    """Videos a keyword finds, posted in the last `days`. TikTok answers video search only from a US IP."""
    query = keywords(query)
    if not query:
        raise RouteError("empty search phrase")
    return try_routes(_with_browser([("omkarcloud", _omkar_search), ("dtk", _dtk_search)], _browser_search),
                      query, days=days, limit=limit, region=region, _op="tiktok.search")


def account(handle, limit=20):
    """An account's follower count and its latest posts with views, newest first."""
    return try_routes(_with_browser([("omkarcloud", _omkar_account), ("dtk", _dtk_account),
                                     ("yt-dlp", _ytdlp_account)], _browser_account),
                      handle, limit=limit, _op="tiktok.account")


def video(ref):
    """One video by id or link. The browser route is tried only for a full link with the username in it
    (_browser_link): it is left out for a bare id, so it records no failure for that call."""
    routes = [("omkarcloud", _omkar_video), ("dtk", _dtk_video), ("yt-dlp", _ytdlp_video)]
    if _browser_link(ref):
        routes = _with_browser(routes, _browser_video)
    return try_routes(routes, ref, _op="tiktok.video")


def sound(ref, limit=30):
    """Videos using a sound. `ref` is a sound id, a /music/ link, or a video link (its sound is used)."""
    return try_routes([("omkarcloud", _omkar_sound), ("dtk", _dtk_sound)], ref, limit=limit, _op="tiktok.sound")


def download(ref, out_dir):
    """An MP4 at the lowest reasonable quality, plus TikTok's own captions when it has them."""
    return try_routes([("omkarcloud", _omkar_download), ("dtk", _dtk_download), ("yt-dlp", _ytdlp_download)],
                      ref, out_dir, _op="tiktok.download")


def _with_browser(routes, browser_fn):
    """The routes, with the browser route last unless the browser_route setting is false."""
    return routes + [("browser", browser_fn)] if settings.get("browser_route") else routes


# ---- shared helpers ------------------------------------------------------------------

def _delay():
    return settings.get("request_delay_seconds")


def _no_posts_listed(posts, post_count, private=None):
    """Raise when TikTok listed no posts for a public profile that says it has some (its post list throttles
    per IP after a few dozen calls), so the next route is tried instead of an empty history."""
    if not posts and (to_int(post_count) or 0) > 0 and private is not True:
        raise RouteError(f"TikTok listed no posts although the profile has {post_count}: the post list is "
                         "probably throttled for this IP")


def _username(handle):
    """'@somebrand', 'somebrand' or a profile/video link -> 'somebrand'. A secUid comes back unchanged."""
    text = str(handle or "").strip()
    match = re.search(r"tiktok\.com/@([^/?#]+)", text)
    if match:
        text = match.group(1)
    text = text.lstrip("@")
    if not text:
        raise ValueError(f"not a TikTok account: {handle!r}")
    return text if SEC_UID_RE.match(text) else text.lower()


def _video_id(ref):
    """The numeric id in a video id or a full video link; None for a share link."""
    text = str(ref or "").strip()
    if re.fullmatch(r"\d{15,25}", text):
        return text
    match = VIDEO_ID_RE.search(text)
    return match.group(1) if match else None


def _video_url(ref):
    vid = _video_id(ref)
    return f"{SITE}/@/video/{vid}" if vid and not str(ref).startswith("http") else str(ref).strip()


def _iso(value):
    """Any ISO 8601 string -> 'YYYY-MM-DDTHH:MM:SSZ' in UTC, else None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _published(days):
    """The smallest omkarcloud `published` window that covers `days`; None means no filter."""
    if not days:
        return None
    return next((name for name, span in PUBLISHED if days <= span), None)


def _recent(videos, days):
    """Drop videos known to be older than `days`. Videos with no date are kept."""
    if not days:
        return videos
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [v for v in videos if not v["posted_at"] or v["posted_at"] >= cutoff]


def _unique(videos):
    seen, out = set(), []
    for v in videos:
        if v["id"] and v["id"] not in seen:
            seen.add(v["id"])
            out.append(v)
    return out


def _newest_first(videos):
    return sorted(_unique(videos), key=lambda v: v["posted_at"] or "", reverse=True)


def _out_path(out_dir, video_id):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out / f"tiktok_{video_id}.mp4"


def _is_mp4(head):
    return len(head) >= 8 and head[4:8] == b"ftyp"


def _pick_track(tracks):
    """The original-language WebVTT track, else any WebVTT track, else None."""
    vtt = [t for t in tracks if t.get("link") and (t.get("format") or "").lower() in ("webvtt", "vtt")]
    originals = [t for t in vtt if not t.get("is_translation")]
    return (originals or vtt or [None])[0]


def _caption_source(track):
    return f"tiktok:{track.get('source') or 'captions'}:{track.get('language') or 'unknown'}"


def _vtt_text(text):
    """WebVTT or SRT -> the spoken text on one line, repeated lines dropped."""
    lines = []
    for line in (text or "").replace("\r", "").split("\n"):
        line = re.sub(r"<[^>]+>", "", line).strip()
        if (not line or line.isdigit() or "-->" in line
                or line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Kind:", "Language:"))):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return " ".join(lines) or None


def _http_get(url, timeout=60):
    """GET with a browser User-Agent -> (status, bytes)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": SITE + "/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _save_links(links, path):
    """Save the first link that answers with an MP4 (plain urllib, no cookies)."""
    tried = []
    for link in list(dict.fromkeys(l for l in links if l))[:MAX_LINKS]:
        try:
            status, body = _http_get(link, timeout=120)
        except (urllib.error.URLError, OSError) as exc:
            tried.append(type(exc).__name__)
            continue
        if status in (200, 206) and _is_mp4(body[:16]):
            path.write_bytes(body)
            return path
        tried.append(f"HTTP {status}" if status not in (200, 206) else "not an MP4")
    raise RouteError(f"no media link gave an MP4 ({', '.join(tried) or 'no links'})")


# ---- route 1: omkarcloud tiktok-scraper ----------------------------------------------

def _omkar():
    """The vendored tiktok-scraper modules, imported on first use with tiktok_proxy applied."""
    if not (VENDOR / "tiktok" / "fetch.py").is_file():
        raise NotConfigured("TikTok route 1 (omkarcloud tiktok-scraper) is not installed: run /format-finder:setup")
    proxy = settings.get("tiktok_proxy")
    if proxy:
        os.environ["TIKTOK_PROXY"] = proxy      # config.py reads it at import time
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    try:
        import config
        from tiktok import discovery, fetch, refs, search as tk_search, users, videos
    except ImportError as exc:
        raise NotConfigured(f"TikTok route 1 cannot load ({exc}): run /format-finder:setup") from exc
    if proxy and config.TIKTOK_PROXY != proxy:
        config.TIKTOK_PROXY, config.TIKTOK_USE_PROXY = proxy, True
    return SimpleNamespace(config=config, discovery=discovery, fetch=fetch, refs=refs, search=tk_search,
                           users=users, videos=videos)


def _from_omkar(v, followers=None):
    author = v.get("author") or {}
    stats = v.get("stats") or {}
    music = v.get("music") or {}
    if followers is None:
        followers = (v.get("author_stats") or {}).get("follower_count")
    return make_video(
        platform="tiktok", id=v.get("id"), url=v.get("link"), caption=v.get("description"),
        author_id=author.get("id"), author_username=author.get("username"), author_url=author.get("link"),
        author_followers=followers, views=stats.get("play_count"), likes=stats.get("like_count"),
        comments=stats.get("comment_count"), shares=stats.get("share_count"), posted_at=v.get("created_at"),
        duration_s=(v.get("video") or {}).get("duration_seconds"), sound_id=music.get("id"),
        sound_title=music.get("title"), sound_author=music.get("author_name"),
        is_paid=True if v.get("is_ad") else None,  # isAd marks promoted ads; a paid partnership is not reported
        route="omkarcloud", raw_ref=v.get("id"),
    )


def _omkar_search(query, days=None, limit=30, region=None):
    tk = _omkar()
    found, cursor = [], None
    while len(found) < limit:
        try:
            page = tk.search.search_videos(query, country=(region or "").lower() or None,
                                           published=_published(days), cursor=cursor)
        except tk.fetch.TikTokUpstreamError as exc:  # TikTokBlocked is one kind
            if found:
                log(f"[omkarcloud] search stopped after {len(found)} videos: {exc}")
                break
            if not isinstance(exc, tk.fetch.TikTokBlocked):
                raise
            hint = "" if settings.get("tiktok_proxy") else (
                ". TikTok answers video search only from a US IP: outside the US set tiktok_proxy"
                f" in {settings.USER_PATH}")
            raise RouteError(f"TikTok refused the search session ({exc}){hint}") from exc
        found += [_from_omkar(v) for v in page["videos"]]
        cursor = page["next_cursor"]
        if not cursor or not page["videos"]:
            break
        pause(_delay())
    if not found and not settings.get("tiktok_proxy"):
        log("[omkarcloud] TikTok answers video search only from a US IP: outside the US set tiktok_proxy")
    return _recent(_unique(found), days)[:limit]


def _omkar_account(handle, limit=20):
    tk = _omkar()
    ref = tk.refs.resolve_user(handle)
    try:
        profile = tk.users.get_profile(ref)
    except tk.fetch.TikTokNotFound as exc:  # its own NotFound class, not common.NotFound
        # It also says "not found" for a hidden profile and for an answer with no profile in it (what a blocked or
        # captcha page gives), so the later routes still ask.
        raise NotFound(f"TikTok has no profile @{_username(handle)}, keeps it hidden, or sent no profile data "
                       f"(omkarcloud: {exc})", final=False) from exc
    followers = (profile.get("stats") or {}).get("follower_count")
    posts, cursor = [], None
    while len(posts) < limit:
        pause(_delay())
        try:
            page = tk.users.get_posts(ref, sort="latest", cursor=cursor)
        except tk.fetch.TikTokUpstreamError as exc:
            if not posts:
                raise
            log(f"[omkarcloud] post list stopped after {len(posts)} posts: {exc}")
            break
        posts += [_from_omkar(v, followers) for v in page["videos"]]
        cursor = page["next_cursor"]
        if not cursor or not page["videos"]:
            break
    post_count = to_int((profile.get("stats") or {}).get("video_count"))
    _no_posts_listed(posts, post_count, profile.get("is_private"))
    private = profile.get("is_private")
    return make_account(platform="tiktok", id=profile.get("id"), username=profile.get("username"),
                        url=profile.get("link"), followers=followers, post_count=post_count,
                        is_private=private if isinstance(private, bool) else None,
                        posts=_newest_first(posts)[:limit], route="omkarcloud")


def _omkar_video(ref):
    tk = _omkar()
    return _from_omkar(tk.videos.get_details(tk.refs.resolve_video(ref)))


def _omkar_music_id(tk, ref):
    try:
        return tk.refs.resolve_music(ref)
    except ValueError:
        pass
    v = tk.videos.get_details(tk.refs.resolve_video(ref))
    music_id = (v.get("music") or {}).get("id")
    if not music_id:
        raise RouteError(f"video {v.get('id')} has no sound")
    pause(_delay())
    return music_id


def _omkar_sound(ref, limit=30):
    tk = _omkar()
    music_id = _omkar_music_id(tk, ref)
    found, cursor = [], None
    while len(found) < limit:
        try:
            page = tk.discovery.get_music_posts(music_id, cursor=cursor)
        except tk.fetch.TikTokUpstreamError as exc:
            if not found:
                raise
            log(f"[omkarcloud] sound posts stopped after {len(found)} videos: {exc}")
            break
        found += [_from_omkar(v) for v in page["videos"]]
        cursor = page["next_cursor"]
        if not cursor or not page["videos"]:
            break
        pause(_delay())
    return _unique(found)[:limit]


def _renditions(media):
    """Play links, lowest reasonable quality first: the smallest size with a short side >= 480, h264 on a tie."""
    def rank(q):
        side = min(q.get("width") or 0, q.get("height") or 0)
        return (side < MIN_SHORT_SIDE, side, "264" not in (q.get("codec") or ""), q.get("bitrate") or 0)
    links = [link for q in sorted(media.get("qualities") or [], key=rank)
             for link in (q.get("play_links") or [q.get("play_link")])]
    links += [media.get("play_link"), media.get("download_link")]
    return list(dict.fromkeys(l for l in links if l))


def _omkar_save(tk, links, path):
    """Stream the first link that answers with an MP4, on the pooled session whose cookies minted the links."""
    tried = []
    client = tk.fetch._acquire(tk.fetch.default_country())
    part = path.with_suffix(".part")
    try:
        for link in links[:MAX_LINKS]:
            try:
                resp = client.session.get(link, headers={"referer": SITE + "/"}, timeout=120, stream=True)
            except Exception as exc:  # noqa: BLE001 - curl_cffi raises its own types; try the next link
                tried.append(type(exc).__name__)
                continue
            try:
                if resp.status_code not in (200, 206):
                    tried.append(f"HTTP {resp.status_code}")
                    continue
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content():
                        fh.write(chunk)
            finally:
                resp.close()
            with open(part, "rb") as fh:
                ok = _is_mp4(fh.read(16))
            if ok:
                part.replace(path)
                return path
            tried.append("not an MP4")
    finally:
        tk.fetch._release(client)
        part.unlink(missing_ok=True)
    raise RouteError(f"no TikTok media link gave an MP4 ({', '.join(tried) or 'no links'})")


def _omkar_captions(tk, tracks):
    track = _pick_track(tracks)
    if not track:
        return None, None
    try:
        text = _vtt_text(tk.fetch.get_text(track["link"]))
    except Exception as exc:  # noqa: BLE001 - missing captions never fail a download
        log(f"[omkarcloud] captions not fetched: {type(exc).__name__}: {exc}")
        return None, None
    return text, _caption_source(track) if text else None


def _omkar_download(ref, out_dir):
    tk = _omkar()
    v = tk.videos.get_details(tk.refs.resolve_video(ref))
    media = v.get("video")
    if not media:
        raise RouteError(f"post {v.get('id')} is a photo post, not a video")
    path = _omkar_save(tk, _renditions(media), _out_path(out_dir, v["id"]))
    text, source = _omkar_captions(tk, media.get("subtitles") or [])
    return {"video_path": str(path), "captions_text": text, "captions_source": source}


# ---- route 2: Douyin_TikTok_Download_API (self-hosted REST) --------------------------

def _dtk_http(method, path, params=None, body=None):
    """One HTTP request to the DTK instance -> (status, bytes)."""
    base = settings.get("dtk_url")
    if not base:
        raise NotConfigured(f"TikTok route 2 is off: set dtk_url in {settings.USER_PATH}")
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{str(base).rstrip('/')}{path}{'?' + query if query else ''}"
    headers = {"Accept": "application/json"}
    if settings.get("dtk_api_key"):
        headers["X-API-Key"] = settings.get("dtk_api_key")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DTK_WAIT + 30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise NotConfigured(f"TikTok route 2: nothing answers at {base} ({exc.reason}). "
                            "Start your Douyin_TikTok_Download_API or set dtk_url to null") from exc


def _dtk_error(error, status=None):
    code = error.get("code") or (f"HTTP {status}" if status else "unknown")
    message = f"DTK {code}: {error.get('message') or ''}".strip()
    if code in ("UNAUTHENTICATED", "FORBIDDEN_SCOPE"):
        return NotConfigured(f"{message}. Check dtk_api_key in {settings.USER_PATH} "
                             "(the key needs tiktok:read, plus media:read and media:write for downloads)")
    if code == "NOT_CONFIGURED":
        return NotConfigured(message)
    return RouteError(message)


def _dtk_call(method, path, params=None, body=None):
    """One enveloped DTK call -> (status, data). Retries twice on the codes DTK marks retryable."""
    for attempt in range(3):
        status, raw = _dtk_http(method, path, params, body)
        try:
            envelope = json.loads(raw)
        except ValueError:
            raise RouteError(f"DTK answered HTTP {status} without JSON") from None
        if envelope.get("success"):
            return status, envelope.get("data")
        error = envelope.get("error") or {}
        if error.get("code") in DTK_RETRYABLE and attempt < 2:
            wait = min(float(error.get("retry_after") or 2 ** (attempt + 1)), 30)
            log(f"[dtk] {error.get('code')}, retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        raise _dtk_error(error, status)


def _dtk_task(task_id):
    """Poll a task until it settles. A failed task is HTTP 200 with state 'failed'."""
    delay, end = 0.5, time.monotonic() + DTK_DEADLINE
    while time.monotonic() < end:
        _, task = _dtk_call("GET", f"/api/v1/tasks/{task_id}")
        if task.get("state") == "done":
            return task.get("data")
        if task.get("state") == "failed":
            raise _dtk_error(task.get("error") or {})
        time.sleep(delay)
        delay = min(delay * 2, 5)
    raise RouteError(f"DTK task {task_id} still running after {DTK_DEADLINE}s")


def _dtk_fetch(path, **params):
    """A content read: submit with ?wait=, and poll the task when it answers 202 (still running)."""
    status, data = _dtk_call("GET", path, {**params, "wait": DTK_WAIT})
    if status == 202:
        return _dtk_task(data["task_id"])
    return data


def _dtk_pages(path, limit, **params):
    """Walk a DTK list endpoint until `limit` posts or the last page."""
    found, cursor = [], None
    while len(found) < limit:
        page = _dtk_fetch(path, count=min(max(limit, 1), DTK_PAGE), cursor=cursor, **params) or {}
        items = page.get("items") or []
        found += [_from_dtk(c) for c in items]
        cursor = page.get("cursor")
        if not (items and cursor and page.get("has_more")):
            break
        pause(_delay())
    return _unique(found)


def _from_dtk(c, followers=None):
    author = c.get("author") or {}
    stats = c.get("stats") or {}
    music = c.get("music") or {}
    if followers is None:
        followers = (author.get("stats") or {}).get("follower_count")
    duration_ms = to_int(c.get("duration_ms"))
    return make_video(
        platform="tiktok", id=c.get("content_id"), url=c.get("web_url"),
        caption=c.get("description") or c.get("title") or None, author_id=author.get("uid"),
        author_username=author.get("unique_id"), author_url=author.get("web_url"), author_followers=followers,
        views=stats.get("play_count"), likes=stats.get("digg_count"), comments=stats.get("comment_count"),
        shares=stats.get("share_count"), posted_at=_iso(c.get("created_at")),
        duration_s=round(duration_ms / 1000) if duration_ms is not None else None,
        sound_id=music.get("music_id"), sound_title=music.get("title"), sound_author=music.get("author"),
        is_paid=None, route="dtk", raw_ref=c.get("content_id"),
    )


def _dtk_post(ref):
    vid = _video_id(ref)
    return {"aweme_id": vid} if vid else {"url": str(ref).strip()}


def _dtk_search(query, days=None, limit=30, region=None):
    """DTK has no date filter or region: posts are filtered by date here, region is ignored."""
    return _recent(_dtk_pages("/api/v1/tiktok/search/posts", limit, keyword=query), days)[:limit]


def _dtk_account(handle, limit=20):
    name = _username(handle)
    who = {"sec_user_id": name} if SEC_UID_RE.match(name) else {"url": f"{SITE}/@{name}"}
    author = _dtk_fetch("/api/v1/tiktok/user", **who) or {}
    followers = (author.get("stats") or {}).get("follower_count")
    sec_uid = author.get("sec_uid") or who.get("sec_user_id")
    if not sec_uid:
        raise RouteError("DTK profile carried no secUid, so the post list cannot be asked for")
    pause(_delay())
    posts = _dtk_pages("/api/v1/tiktok/user/posts", limit, sec_user_id=sec_uid)
    post_count = to_int((author.get("stats") or {}).get("content_count"))
    _no_posts_listed(posts, post_count)  # DTK's Author has no private flag
    for post in posts:
        post["author_followers"] = followers
    return make_account(platform="tiktok", id=author.get("uid"), username=author.get("unique_id"),
                        url=author.get("web_url"), followers=followers, post_count=post_count,
                        posts=_newest_first(posts)[:limit], route="dtk")


def _dtk_video(ref):
    return _from_dtk(_dtk_fetch("/api/v1/tiktok/video", **_dtk_post(ref)) or {})


def _dtk_sound(ref, limit=30):
    text = str(ref or "").strip()
    if text.isdigit():
        which = {"music_id": text}
    elif "/music/" in text:
        which = {"url": text}
    else:
        music_id = ((_dtk_fetch("/api/v1/tiktok/video", **_dtk_post(text)) or {}).get("music") or {}).get("music_id")
        if not music_id:
            raise RouteError(f"no sound found for {text!r}")
        which = {"music_id": music_id}
        pause(_delay())
    return _dtk_pages("/api/v1/tiktok/music/posts", limit, **which)[:limit]


def _dtk_stored_file(content_id, path):
    """Have the DTK downloader sidecar store the post, then fetch its video file from the instance."""
    _, job = _dtk_call("POST", "/api/v1/downloads",
                       body={"platform": "tiktok", "content_id": content_id, "skip_existing": True})
    download_id = job["download_id"]
    delay, end = 1, time.monotonic() + DTK_DEADLINE
    while True:
        _, row = _dtk_call("GET", f"/api/v1/downloads/{download_id}")
        state = row.get("state")
        if state in ("done", "partial"):
            break
        if state in ("failed", "cancelled"):
            raise RouteError(f"DTK download {download_id} {state}: {row.get('error')}")
        if time.monotonic() > end:
            raise RouteError(f"DTK download {download_id} still {state} after {DTK_DEADLINE}s")
        time.sleep(delay)
        delay = min(delay * 2, 5)
    name = next((f.get("name") for f in row.get("files") or []
                 if f.get("kind") == "video" and f.get("state") == "done"), None)
    if not name:
        raise RouteError(f"DTK download {download_id} stored no video file")
    status, body = _dtk_http("GET", f"/api/v1/downloads/{download_id}/files/{urllib.parse.quote(name)}")
    if status != 200 or not _is_mp4(body[:16]):
        raise RouteError(f"DTK file {name} of download {download_id} answered HTTP {status}")
    path.write_bytes(body)
    return path


def _raw_tracks(item):
    """Caption tracks from a raw TikTok itemStruct (video.subtitleInfos)."""
    return [{"language": row.get("LanguageCodeName"), "source": (row.get("Source") or "").lower() or None,
             "is_translation": row.get("Source") == "MT", "format": row.get("Format"), "link": row.get("Url")}
            for row in ((item.get("video") or {}).get("subtitleInfos") or []) if isinstance(row, dict)]


def _dtk_download(ref, out_dir):
    post = _dtk_fetch("/api/v1/tiktok/video", include_raw="true", **_dtk_post(ref)) or {}
    if post.get("kind") not in (None, "video"):
        raise RouteError(f"post {post.get('content_id')} is a {post.get('kind')}, not a video")
    path = _out_path(out_dir, post.get("content_id"))
    try:
        _dtk_stored_file(post.get("content_id"), path)
    except NotConfigured as exc:
        log(f"[dtk] {exc}; fetching the stream link directly")
        media = post.get("media") or {}
        streams = [media.get("video") or {}] + sorted(media.get("streams") or [], key=lambda s: s.get("bitrate") or 0)
        _save_links([link for s in streams for link in [s.get("url")] + (s.get("urls") or [])], path)
    text, source = None, None
    track = _pick_track(_raw_tracks(post.get("raw") or {}))
    if track:
        try:
            status, body = _http_get(track["link"])
            text = _vtt_text(body.decode("utf-8", "replace")) if status == 200 else None
        except (urllib.error.URLError, OSError) as exc:
            log(f"[dtk] captions not fetched: {type(exc).__name__}: {exc}")
        source = _caption_source(track) if text else None
    return {"video_path": str(path), "captions_text": text, "captions_source": source}


# ---- partial fallback: yt-dlp (no search, no follower count, no sound lists) ---------

def _ytdlp(opts=None):
    try:
        import yt_dlp
    except ImportError as exc:
        raise NotConfigured("yt-dlp is missing: run /format-finder:setup") from exc
    quiet = SimpleNamespace(debug=lambda m: None, info=lambda m: None, warning=lambda m: None,
                            error=lambda m: log(f"[yt-dlp] {m}"))
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True, "logger": quiet,
            "cachedir": False}
    return yt_dlp.YoutubeDL({**base, **(opts or {})})


def _from_ytdlp(e):
    uploader = e.get("uploader")
    artists = e.get("artists") or ([e["artist"]] if e.get("artist") else [])
    return make_video(
        platform="tiktok", id=e.get("id"), url=e.get("webpage_url") or e.get("url"),
        caption=e.get("description") or e.get("title"), author_id=e.get("uploader_id"),
        author_username=uploader, author_url=f"{SITE}/@{uploader}" if uploader else None, author_followers=None,
        views=to_int(e.get("view_count")), likes=to_int(e.get("like_count")),
        comments=to_int(e.get("comment_count")), shares=to_int(e.get("repost_count")),
        posted_at=iso_from_timestamp(e.get("timestamp")), duration_s=e.get("duration"), sound_id=None,
        sound_title=e.get("track"), sound_author=", ".join(artists) or None, is_paid=None, route="yt-dlp",
        raw_ref=e.get("id"),
    )


def _ytdlp_account(handle, limit=20):
    name = _username(handle)
    url = f"tiktokuser:{name}" if SEC_UID_RE.match(name) else f"{SITE}/@{name}"
    with _ytdlp({"extract_flat": "in_playlist", "playlistend": limit}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    posts = [_from_ytdlp(e) for e in info.get("entries") or [] if e]
    username = next((p["author_username"] for p in posts if p["author_username"]), None)
    if username is None and not SEC_UID_RE.match(name):
        username = name
    return make_account(platform="tiktok", id=next((p["author_id"] for p in posts if p["author_id"]), None),
                        username=username, url=f"{SITE}/@{username}" if username else None, followers=None,
                        posts=_newest_first(posts)[:limit], route="yt-dlp")


def _ytdlp_video(ref):
    with _ytdlp() as ydl:
        return _from_ytdlp(ydl.extract_info(_video_url(ref), download=False) or {})


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - ffmpeg is optional here: TikTok formats carry audio and video together
        return None


def _ytdlp_download(ref, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    opts = {"skip_download": False, "format": "b[vcodec^=h264]/b", "format_sort": [f"res:{MIN_SHORT_SIDE + 60}"],
            "outtmpl": str(out / "tiktok_%(id)s.%(ext)s"), "overwrites": True}
    if _ffmpeg():
        opts["ffmpeg_location"] = _ffmpeg()
    with _ytdlp(opts) as ydl:
        info = ydl.extract_info(_video_url(ref), download=True) or {}
        files = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
        path = Path(files[0]) if files else Path(ydl.prepare_filename(info))
        if not path.is_file():
            raise RouteError("yt-dlp finished without a video file")
        tracks = [{"language": lang, "source": None, "is_translation": False,
                   "format": sub.get("ext"), "link": sub.get("url")}
                  for lang, subs in (info.get("subtitles") or {}).items() for sub in subs]
        text, source = None, None
        track = _pick_track(tracks)
        if track:
            try:
                text = _vtt_text(ydl.urlopen(track["link"]).read().decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001 - missing captions never fail a download
                log(f"[yt-dlp] captions not fetched: {type(exc).__name__}: {exc}")
            source = _caption_source(track) if text else None
    return {"video_path": str(path), "captions_text": text, "captions_source": source}


# ---- last route: TikTok's own pages in a headless browser (browser.py) ----------------
# The page's own scripts sign their requests; the replies are read over DevTools. Slower than the direct
# routes (a browser starts once per process), so it comes last.

SEARCH_API = r"^/api/search/item/full/"
POSTS_API = r"^/api/post/item_list/"
BROWSER_SEARCH_PAGES = 10      # result pages read per search at most (TikTok sends 12 videos a page)
BROWSER_POST_PAGES = 8         # post-list pages read per profile at most
# Seconds per search (all pages) and per profile or video page: the browser_search_timeout_seconds and
# browser_page_timeout_seconds settings (defaults/settings.json).
CAPTCHA_HINT = (f"TikTok is asking this IP for a puzzle; try later, or run {TERMINAL_COMMAND} browser-login tiktok in "
                "your own terminal and log in or complete it yourself in the window (whether that clears it is "
                "untested)")


def _stat(stats, stats_v2, key):
    """A count from statsV2 (exact, as text) when present, else from stats (TikTok rounds some of those: a video
    page on 2026-11-05 gave authorStats followerCount 180600 for an account route 1 counted at 180582)."""
    value = to_int((stats_v2 or {}).get(key))
    return value if value is not None else to_int((stats or {}).get(key))


def _from_item(it, followers=None, route="browser"):
    """A raw TikTok web itemStruct (search, post list, video page) -> Video."""
    author = it.get("author") or {}
    if isinstance(author, str):
        author = {"uniqueId": author, "id": it.get("authorId")}
    stats, stats_v2 = it.get("stats"), it.get("statsV2")
    if followers is None:
        followers = _stat(it.get("authorStats"), it.get("authorStatsV2"), "followerCount")
    music = it.get("music") or {}
    vid = str(it["id"]) if it.get("id") else None
    username = author.get("uniqueId") or None
    kind = "photo" if it.get("imagePost") else "video"
    return make_video(
        platform="tiktok", id=vid, url=f"{SITE}/@{username or ''}/{kind}/{vid}" if vid else None,
        caption=it.get("desc") or None, author_id=str(author["id"]) if author.get("id") else None,
        author_username=username, author_url=f"{SITE}/@{username}" if username else None,
        author_followers=followers, views=_stat(stats, stats_v2, "playCount"),
        likes=_stat(stats, stats_v2, "diggCount"), comments=_stat(stats, stats_v2, "commentCount"),
        shares=_stat(stats, stats_v2, "shareCount"), posted_at=iso_from_timestamp(it.get("createTime")),
        duration_s=to_int((it.get("video") or {}).get("duration")) or None,
        sound_id=str(music["id"]) if music.get("id") else None, sound_title=music.get("title") or None,
        sound_author=music.get("authorName") or None,
        is_paid=True if it.get("isAd") else None,  # isAd marks promoted ads; a paid partnership is not reported
        route=route, raw_ref=vid,
    )


def _api_reply(reply, what):
    """A reply the page received from TikTok's web API -> its JSON. Raises RouteError on failure."""
    from .browser import BotCheck, bot_check_kind
    body = (reply.get("body") or "").strip()
    check = (reply.get("headers") or {}).get("bdturing-verify")
    if check and not body:
        raise BotCheck(f"TikTok asked for a captcha ({bot_check_kind(check)}) instead of sending the {what} reply. "
                       f"The tool does not solve captchas. {CAPTCHA_HINT}", captcha=True)
    if reply.get("error"):
        raise RouteError(f"TikTok's {what} request failed in the browser: {reply['error']}")
    if (reply.get("status") or 200) >= 400:
        raise RouteError(f"TikTok answered the {what} request with HTTP {reply['status']}")
    if not body:
        raise BotCheck(f"TikTok sent an empty {what} reply (it has done this to an IP it flagged after repeated "
                       "searches); try again later")
    try:
        data = json.loads(body)
    except ValueError:
        raise RouteError(f"TikTok's {what} reply is not JSON") from None
    code = data.get("status_code", data.get("statusCode"))
    if code not in (None, 0, "0"):
        msg = data.get("status_msg") or data.get("statusMsg") or "no message"
        raise RouteError(f"TikTok refused the {what} request (status {code}: {msg})")
    return data


def _search_page(data):
    """/api/search/item/full/ JSON -> (items, more)."""
    return [i for i in data.get("item_list") or [] if isinstance(i, dict)], bool(to_int(data.get("has_more")))


def _posts_page(data):
    """/api/post/item_list/ JSON -> (items, more)."""
    more = data.get("hasMore")
    return [i for i in data.get("itemList") or [] if isinstance(i, dict)], bool(more) and more not in ("0", "false")


def _scope(universal, key):
    return ((universal or {}).get("__DEFAULT_SCOPE__") or {}).get(key)


def _user_detail(universal, name):
    """The profile in a page's __UNIVERSAL_DATA_FOR_REHYDRATION__ -> userInfo {user, stats, ...}, or None.

    A missing user raises NotFound. A profile TikTok keeps hidden (status USER_SECRET: TikTok calls it 'secret')
    gives a private profile with no posts, so the account comes back empty as private."""
    detail = _scope(universal, "webapp.user-detail")
    if not detail:
        return None
    status, message = to_int(detail.get("statusCode")), detail.get("statusMsg") or "no message"
    if status in USER_NOT_FOUND:
        raise NotFound(f"TikTok has no profile @{name} (status {status}: {message}). Check the handle")
    if status == USER_SECRET or "secret" in str(message).lower():
        log(f"[browser] TikTok keeps @{name} hidden (status {status}: {message}): taken as a private account")
        return {"user": {"uniqueId": name, "privateAccount": True}, "stats": {}}
    if status not in (None, 0):
        raise RouteError(f"TikTok shows no profile @{name} (status {status}: {message})")
    return detail.get("userInfo") or None


def _video_detail(universal, ref):
    """The video in a page's __UNIVERSAL_DATA_FOR_REHYDRATION__ -> itemStruct, or None."""
    detail = _scope(universal, "webapp.video-detail")
    if not detail:
        return None
    if detail.get("statusCode") not in (None, 0):
        raise RouteError(f"TikTok shows no video {ref} (status {detail.get('statusCode')}: "
                         f"{detail.get('statusMsg') or 'no message'})")
    return (detail.get("itemInfo") or {}).get("itemStruct") or None


def _page_failure(what, summary, error=None):
    """Why a page gave nothing: the bot check or login wall it showed, the reply error, or a timeout."""
    from .browser import BotCheck, is_captcha
    if summary.get("blocked"):
        captcha = is_captcha(summary["blocked"])
        hint = (CAPTCHA_HINT if captcha else
                f"Log in once with {TERMINAL_COMMAND} browser-login tiktok in your own terminal (you log in "
                "yourself), or try again later"
                if "login" in summary["blocked"] else "Try again later")
        return BotCheck(f"TikTok showed {summary['blocked']} on the {what} page instead of results. {hint}",
                        captcha=captcha)
    if error is not None:
        return error
    status = f" (the page answered HTTP {summary['status']})" if (summary.get("status") or 200) >= 400 else ""
    if summary.get("timed_out"):
        return RouteError(f"TikTok's {what} page gave no results within {summary.get('seconds')}s{status}")
    return RouteError(f"TikTok's {what} page loaded but carried no results{status}")


def _browser_search(query, days=None, limit=30, region=None):
    """Keyword search on tiktok.com/search/video. TikTok picks the region from your IP; `region` is not used.
    The page has no date filter: videos outside `days` are dropped here, and more pages are read for them."""
    from . import browser
    found, state = [], {"pages": 0, "more": True, "error": None}

    def take(reply):
        try:
            items, more = _search_page(_api_reply(reply, "search"))
        except RouteError as exc:
            state["error"] = exc
            return True
        state["pages"] += 1
        state["more"] = more and bool(items)
        found.extend(_from_item(i) for i in items)
        return (not state["more"] or len(_recent(_unique(found), days)) >= limit
                or state["pages"] >= BROWSER_SEARCH_PAGES)

    url = f"{SITE}/search/video?q={urllib.parse.quote(query)}"
    summary = browser.tab().capture(url, pattern=SEARCH_API, on_reply=take,
                                    timeout=settings.get("browser_search_timeout_seconds"),
                                    max_scrolls=BROWSER_SEARCH_PAGES, scroll_gap=_delay())
    if not found:
        if state["pages"] and state["error"] is None:
            return []  # TikTok answered with a valid reply that lists no videos: nothing matches the phrase
        raise _page_failure("search", summary, state["error"])
    result = _recent(_unique(found), days)[:limit]
    if len(result) < limit and state["more"]:
        why = state["error"] or summary.get("blocked") or (
            "page limit" if state["pages"] >= BROWSER_SEARCH_PAGES else "no more pages loaded")
        log(f"[browser] search {query!r} stopped after {state['pages']} pages, {len(result)} videos in the window: {why}")
    return result


def _browser_account(handle, limit=20):
    """Profile page: follower count from its embedded data, posts from the post-list replies it loads."""
    from . import browser
    name = _username(handle)
    if SEC_UID_RE.match(name):
        raise RouteError("the browser route opens profiles by username, not by secUid")
    state = {"info": None, "items": [], "pages": 0, "more": True, "error": None}

    def got_profile(data):
        state["info"] = _user_detail(data, name)
        info = state["info"] or {}
        stats = {**(info.get("stats") or {}), **(info.get("statsV2") or {})}  # statsV2 holds exact counts as text
        state["items"] += [i for i in info.get("itemList") or [] if isinstance(i, dict)]
        return bool((info.get("user") or {}).get("privateAccount")) or to_int(stats.get("videoCount")) == 0

    def got_posts(reply):
        try:
            items, more = _posts_page(_api_reply(reply, "post list"))
        except RouteError as exc:
            state["error"] = exc
            return True
        state["pages"] += 1
        state["more"] = more and bool(items)
        state["items"] += items
        return not state["more"] or len({i.get("id") for i in state["items"]}) >= limit \
            or state["pages"] >= BROWSER_POST_PAGES

    summary = browser.tab().capture(f"{SITE}/@{urllib.parse.quote(name)}", pattern=POSTS_API, on_reply=got_posts,
                                    embedded=browser.UNIVERSAL_DATA, on_embedded=got_profile,
                                    timeout=settings.get("browser_page_timeout_seconds"),
                                    max_scrolls=BROWSER_POST_PAGES, scroll_gap=_delay())
    info = state["info"]
    if not info:
        raise _page_failure("profile", summary)
    user = info.get("user") or {}
    stats = {**(info.get("stats") or {}), **(info.get("statsV2") or {})}  # statsV2 holds exact counts as text
    followers = to_int(stats.get("followerCount"))
    uid, username = user.get("id"), user.get("uniqueId") or name

    def own(item):
        author = item.get("author")
        author_id = author.get("id") if isinstance(author, dict) else item.get("authorId")
        return not author_id or not uid or str(author_id) == str(uid)

    posts = [_from_item(i, followers) for i in state["items"] if own(i)]
    if not posts and (summary.get("blocked") or state["error"]):
        raise _page_failure("profile", summary, state["error"])
    _no_posts_listed(posts, stats.get("videoCount"), user.get("privateAccount"))
    if len(_unique(posts)) < limit and state["more"] and posts:
        log(f"[browser] @{username}: post list stopped after {state['pages']} pages, {len(_unique(posts))} posts: "
            f"{state['error'] or summary.get('blocked') or 'no more pages loaded'}")
    private = user.get("privateAccount")
    return make_account(platform="tiktok", id=str(uid) if uid else None, username=username,
                        url=f"{SITE}/@{username}", followers=followers, post_count=to_int(stats.get("videoCount")),
                        is_private=private if isinstance(private, bool) else None,
                        posts=_newest_first(posts)[:limit], route="browser")


def _browser_link(ref):
    """The link the browser route opens for `ref`, or None. Only a link that names the account: on 2026-11-05 the
    /@/video/ID form of a bare id showed TikTok's captcha while the same video's full link, 5 s later from the same
    IP and profile, read normally (one load of each)."""
    text = str(ref or "").strip()
    if re.match(r"(?:www\.|m\.)?tiktok\.com/", text):
        text = "https://" + text
    if not text.startswith(("https://", "http://")) or re.search(r"tiktok\.com/@(?:[/?#]|$)", text):
        return None
    return text


def _browser_video(ref):
    """Video page: everything comes from its embedded webapp.video-detail data."""
    from . import browser
    url = _browser_link(ref)
    if url is None:
        raise RouteError(f"the browser route opens a video only by its full link (https://www.tiktok.com/@name/video/"
                         f"ID), not {ref!r}")
    state = {}

    def got(data):
        state["data"] = data
        return True

    summary = browser.tab().capture(url, embedded=browser.UNIVERSAL_DATA, on_embedded=got,
                                    timeout=settings.get("browser_page_timeout_seconds"))
    item = _video_detail(state.get("data"), ref)
    if not item:
        raise _page_failure("video", summary)
    return _from_item(item)
