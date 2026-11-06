"""YouTube Shorts. Route 1: yt-dlp (Python API). Route 2: YouTube.js through node/yt.mjs."""
import atexit
import base64
import copy
import json
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from . import settings
from .common import (
    FF_HOME, VIDEO_KEYS, NotConfigured, RouteError, iso_from_timestamp, iso_from_yyyymmdd, keywords, log,
    make_account, make_video, pause, to_int, try_routes,
)

PLATFORM = "youtube"
NODE_SCRIPT = FF_HOME / "node" / "yt.mjs"
NODE_MIN = 22  # yt-dlp's YouTube player challenges and YouTube.js both need it
WINDOWS = {"today": 2, "week": 3, "month": 4, "year": 5}  # SearchFilter.Filters.upload_date
WINDOW_DAYS = (("year", 365), ("month", 30), ("week", 7), ("today", 1))  # how far back each filter reaches
WIDER = {"today": "week", "week": "month", "month": "year"}  # never wider than a year
SHORTS_TYPE = 9  # SearchFilter.Filters.type
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|/shorts/|youtu\.be/|/embed/|/live/|/source/)([\w-]{11})")
CHANNEL_ID_RE = re.compile(r"UC[\w-]{22}")
VIDEO_NODE_KEYS = {"shortsLockupViewModel", "videoRenderer", "reelItemRenderer", "lockupViewModel"}
_YTCFG = {}
_CACHE = []
_window_used = [None]  # the upload window the last search route answered with


# ---------- shared helpers ----------

def upload_window(days):
    """The widest upload-date filter YouTube offers that reaches no further back than `days`; None for any date.

    Search results carry no upload date, so a wider window would let older videos through undated:
    under 7 days -> today, 7-29 -> week, 30-364 -> month (the default 60 days), 365 or more -> year.
    """
    if not days or days <= 0:
        return None
    return next((name for name, span in WINDOW_DAYS if days >= span), "today")


def search_params(window=None):
    """The sp= value for a Shorts-only search, e.g. EgIQCQ== (any date) or EgQIAxAJ (this week)."""
    filters = (bytes([0x08, WINDOWS[window]]) if window else b"") + bytes([0x10, SHORTS_TYPE])
    return base64.b64encode(bytes([0x12, len(filters)]) + filters).decode()


def video_id(ref):
    ref = str(ref).strip()
    if re.fullmatch(r"[\w-]{11}", ref):
        return ref
    m = VIDEO_ID_RE.search(ref)
    if not m:
        raise RouteError(f"not a YouTube video id or URL: {ref}")
    return m.group(1)


def channel_url(handle):
    """Shorts tab URL for @handle, a bare handle, a UC... channel id, a channel/..., c/... or user/... path, or any
    channel URL."""
    h = str(handle).strip()
    if re.match(r"(?:channel|c|user)/[^/]+", h):
        h = f"https://www.youtube.com/{h}"
    if "youtube.com" in h:
        parts = [p for p in urllib.parse.urlparse(h if "://" in h else "https://" + h).path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
            return f"https://www.youtube.com/{parts[0]}/{parts[1]}/shorts"
        if parts and parts[0].startswith("@"):
            return f"https://www.youtube.com/{parts[0]}/shorts"
        raise RouteError(f"not a YouTube channel URL: {h}")
    if CHANNEL_ID_RE.fullmatch(h):
        return f"https://www.youtube.com/channel/{h}/shorts"
    return f"https://www.youtube.com/@{h.lstrip('@')}/shorts"


def count(text):
    """'1.7M views' -> 1700000, '423K subscribers' -> 423000, '1,234' -> 1234. YouTube rounds list counts."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    if re.match(r"\s*no\b", str(text), re.I):
        return 0
    m = re.search(r"([\d][\d,]*(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)?(?![a-z])", str(text), re.I)
    if not m:
        return None
    unit = (m.group(2) or "").lower()[:1]
    return int(round(float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "t": 1e3, "m": 1e6, "b": 1e9}[unit]))


def shorts_url(vid):
    return f"https://www.youtube.com/shorts/{vid}"


def _strip_at(handle):
    return handle.lstrip("@") if handle else None


def _channel_link(cid):
    return f"https://www.youtube.com/channel/{cid}" if cid else None


def channel_from_params(params):
    """Shorts lockups have no channel field; the uploader's UC id sits in reelWatchEndpoint.params (proto field 23)."""
    if not params:
        return None
    p = urllib.parse.unquote(params).rstrip("=").replace("+", "-").replace("/", "_")
    try:
        raw = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))
    except ValueError:
        return None
    m = re.search(rb"\xba\x01\x18(UC[\w-]{22})", raw) or re.search(rb"(UC[\w-]{22})", raw)
    return m.group(1).decode() if m else None


def _find(node, keys):
    """Yield (key, dict) for every dict stored under one of `keys`, in document order."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, dict):
                yield k, v
            else:
                yield from _find(v, keys)
    elif isinstance(node, list):
        for item in node:
            yield from _find(item, keys)


def _get(node, *path):
    for key in path:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and -len(node) <= key < len(node):
            node = node[key]
        else:
            return None
    return node


def _continuation(data):
    tokens = [_get(c, "continuationEndpoint", "continuationCommand", "token")
              for _, c in _find(data, {"continuationItemRenderer"})]
    tokens = [t for t in tokens if t]
    return tokens[-1] if tokens else None


def _max_pages(limit):
    return max(1, min(5, (limit or 30) // 20 + 2))


def _lockup_video(vm, route, **extra):
    """A shortsLockupViewModel (search, channel Shorts tab, audio pivot) as a Video."""
    ep = _get(vm, "onTap", "innertubeCommand", "reelWatchEndpoint") or {}
    vid = ep.get("videoId") or (vm.get("entityId") or "").removeprefix("shorts-shelf-item-")
    if not re.fullmatch(r"[\w-]{11}", vid or ""):
        return None
    a11y = vm.get("accessibilityText") or ""
    title = _get(vm, "overlayMetadata", "primaryText", "content")
    if title is None:
        m = re.fullmatch(r"(.+), [^,]+ views? - play Short", a11y)
        title = m.group(1) if m else None
    views = count(_get(vm, "overlayMetadata", "secondaryText", "content"))
    if views is None:
        m = re.search(r", ([^,]+ views?) - play Short$", a11y)
        views = count(m.group(1)) if m else None
    cid = channel_from_params(ep.get("params"))
    fields = dict(author_id=cid, author_url=_channel_link(cid))
    fields.update({k: v for k, v in extra.items() if v is not None})
    return make_video(platform=PLATFORM, id=vid, url=shorts_url(vid), caption=title, views=views,
                      route=route, raw_ref=vid, **fields)


def _says_no_views(node):
    """True when a renderer shows YouTube's 'No views' label, which yt-dlp's count parser reads as unknown."""
    if isinstance(node, str):
        return node.strip().lower() == "no views"
    if isinstance(node, dict):
        return any(_says_no_views(v) for v in node.values())
    if isinstance(node, list):
        return any(_says_no_views(v) for v in node)
    return False


def _renderer_video(ie, key, renderer, route, **extra):
    """A videoRenderer, reelItemRenderer or lockupViewModel, parsed by yt-dlp, kept only when it is a Short."""
    if key == "lockupViewModel" and renderer.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None  # playlists, ads and other lockups are never Shorts (and make yt-dlp print a bug-report warning)
    parse = ie._extract_lockup_view_model if key == "lockupViewModel" else ie._extract_video
    e = parse(renderer) or {}
    if not e.get("id") or not ("/shorts/" in (e.get("url") or "") or (e.get("duration") or 999) <= 180):
        return None
    cid = e.get("channel_id")
    views = to_int(e.get("view_count"))
    if views is None and _says_no_views(renderer):
        views = 0
    return make_video(platform=PLATFORM, id=e["id"], url=shorts_url(e["id"]), caption=e.get("title"),
                      author_id=cid, author_username=_strip_at(e.get("uploader_id")),
                      author_url=e.get("uploader_url") or _channel_link(cid), views=views,
                      duration_s=to_int(e.get("duration")), route=route, raw_ref=e["id"],
                      **{k: v for k, v in extra.items() if v is not None})


# ---------- route 1: yt-dlp ----------

def _node_path():
    return shutil.which("node") or next(
        (p for p in ("/usr/local/bin/node", "/opt/homebrew/bin/node") if Path(p).exists()), None)


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - yt-dlp then looks on PATH
        return shutil.which("ffmpeg")


_LOGGER = SimpleNamespace(
    debug=lambda msg: None,
    info=lambda msg: None,
    warning=lambda msg: log(f"[yt-dlp] {msg}"),
    error=lambda msg: log(f"[yt-dlp] {msg}"),
)


def _cache_dir():
    """yt-dlp's cache (YouTube player code) for this process only: removed at exit, so nothing carries over."""
    if not _CACHE:
        _CACHE.append(tempfile.mkdtemp(prefix="format-finder-yt-dlp-"))
        atexit.register(shutil.rmtree, _CACHE[0], True)
    return _CACHE[0]


def _ydl(**extra):
    try:
        import yt_dlp
    except ImportError as exc:
        raise NotConfigured("yt-dlp is not installed. Run /format-finder:setup.") from exc
    opts = {
        "quiet": True, "noprogress": True, "skip_download": True, "logger": _LOGGER,
        "js_runtimes": {"node": {"path": _node_path()}},  # YouTube's player challenges need a JS runtime
        "socket_timeout": 30, "retries": 3, "extractor_retries": 1, "ffmpeg_location": _ffmpeg(),
        "cachedir": _cache_dir(),
    }
    opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _innertube(ydl, ep, query, item_id, region=None):
    """One InnerTube call through yt-dlp's YouTube client (its ytcfg, headers, visitor data, retries)."""
    ie = ydl.get_info_extractor("YoutubeTab")
    if "web" not in _YTCFG:
        _YTCFG["web"] = ie._download_ytcfg("web", item_id) or {}
    ytcfg = copy.deepcopy(_YTCFG["web"])
    if region:
        context = copy.deepcopy(ie._extract_context(ytcfg, "web"))
        context.setdefault("client", {})["gl"] = region.upper()
        ytcfg["INNERTUBE_CONTEXT"] = context
    headers = ie.generate_api_headers(ytcfg=ytcfg, default_client="web")
    return ie._extract_response(item_id=item_id, query=query, ep=ep, ytcfg=ytcfg, headers=headers,
                                default_client="web")


def _page_videos(ie, data, route, first_page=True, **extra):
    """The Shorts on one results page. A later page with none is the end of the results (YouTube's messageRenderer),
    so only an empty first page is logged as having no Shorts."""
    out, seen_keys = [], set()
    for key, node in _find(data, VIDEO_NODE_KEYS):
        seen_keys.add(key)
        if key == "shortsLockupViewModel":
            v = _lockup_video(node, route, **extra)
        else:
            v = _renderer_video(ie, key, node, route, **extra)
        if v:
            out.append(v)
    if not out and first_page:
        log(f"[yt-dlp] page had no Shorts; video nodes seen: {sorted(seen_keys) or 'none'}; "
            f"node types: {sorted(_node_types(data))[:20]}")
    elif not out:
        log("[yt-dlp] end of results")
    return out


def _node_types(node, out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith(("Renderer", "ViewModel")):
                out.add(k)
            _node_types(v, out)
    elif isinstance(node, list):
        for item in node:
            _node_types(item, out)
    return out


def _paged(ydl, ep, query, item_id, limit, region=None, extra_from=None, first=None):
    """Follow continuations until `limit` Shorts or the page cap. extra_from(first page) adds fields to every Video.

    `first` is a first page already read (a channel tab); then only its continuations are requested.
    """
    ie = ydl.get_info_extractor("YoutubeTab")
    out, seen, extra = [], set(), None
    for page in range(_max_pages(limit)):
        if page:
            pause(settings.get("request_delay_seconds"))
            try:
                data = _innertube(ydl, ep, query, item_id, region)
            except Exception as exc:  # noqa: BLE001 - keep the pages already read
                log(f"[yt-dlp] continuation page {page + 1} failed, keeping {len(out)} results: {exc}")
                break
        else:
            data = first if first is not None else _innertube(ydl, ep, query, item_id, region)
        if extra is None:
            extra = extra_from(data) if extra_from else {}
        for v in _page_videos(ie, data, "yt-dlp", first_page=not page, **extra):
            if v["id"] not in seen:
                seen.add(v["id"])
                out.append(v)
        token = _continuation(data)
        if len(out) >= limit or not token:
            break
        query = {"continuation": token}
    return out[:limit]


def _widened(route, query, days, search_window):
    """search_window(window) for upload_window(days), widened up to a year while YouTube finds nothing.

    YouTube answers "No results found" for some popular phrases with a date filter (e.g. "meal prep"
    this month) while the same phrase has Shorts this year. The window that answered is kept for search().
    """
    window = upload_window(days)
    while True:
        found = search_window(window)
        if found or WIDER.get(window) is None:
            _window_used[0] = window or "any"
            return found
        log(f"[{route}] no Shorts for {query!r} with window={window}; widening to {WIDER[window]}")
        window = WIDER[window]
        pause(settings.get("request_delay_seconds"))


def _ytdlp_search(query, days=None, limit=30, region=None):
    """Keyword search with the Shorts type filter and upload_window(days), widened when empty (see _widened).

    yt-dlp's own results-URL parser drops the gridShelfViewModel that Shorts results now arrive in,
    so this calls the same search endpoint through yt-dlp's client and reads the lockups here.
    Search results carry no upload date.
    """
    with _ydl() as ydl:
        def search_window(window):
            log(f"[yt-dlp] shorts search {query!r} window={window or 'any'} sp={search_params(window)}")
            return _paged(ydl, "search", {"query": query, "params": search_params(window)}, f"search {query}",
                          limit, region)
        return _widened("yt-dlp", query, days, search_window)


def _ytdlp_account(handle, limit=20):
    """Channel details from yt-dlp's tab parser; the Shorts tab's lockups are read here.

    yt-dlp reads a Short's 'No views' label as an unknown count, which would drop 0-view Shorts from the
    account normal; count() reads it as 0, like YouTube.js does.
    """
    url = channel_url(handle)
    with _ydl() as ydl:
        ie = ydl.get_info_extractor("YoutubeTab")
        data, ytcfg = ie._extract_data(url, url)
        tab = ie._extract_selected_tab(ie._extract_tab_renderers(data), fatal=False) or {}
        if ie._extract_tab_id_and_name(tab, url)[0] != "shorts":
            raise RouteError(f"{url} has no Shorts tab")
        if ytcfg and "web" not in _YTCFG:
            _YTCFG["web"] = ytcfg
        info = ie._extract_metadata_from_tabs(url, data)
        cid = info.get("channel_id")
        username = _strip_at(info.get("uploader_id"))
        link = info.get("uploader_url") or _channel_link(cid)
        followers = to_int(info.get("channel_follower_count"))
        posts = _paged(ydl, "browse", None, f"channel {cid or handle}", limit, first=tab.get("content") or {},
                       extra_from=lambda _: dict(author_id=cid, author_username=username, author_url=link,
                                                 author_followers=followers))
    return make_account(platform=PLATFORM, id=cid, username=username, url=link, followers=followers,
                        posts=posts[:limit], route="yt-dlp")


def _pivot_header(data):
    """(source video id, sound title, sound author) from an audio pivot page header."""
    header = next((h for _, h in _find(data, {"pageHeaderViewModel"})), None)
    if not header:
        return None, None, None
    title = _get(header, "title", "dynamicTextViewModel", "text", "content")
    source = next((e.get("videoId") for _, e in _find(header, {"watchEndpoint", "reelWatchEndpoint"})
                   if e.get("videoId")), None)
    author = next((t for _, a in _find(header, {"avatarStackViewModel"})
                   if (t := _get(a, "text", "content"))), None)
    return source, title, author


def _reel_sound(ydl, vid):
    """The sound a Short uses, read from the Shorts player's audio-pivot panel."""
    data = _innertube(ydl, "reel/reel_item_watch",
                      {"playerRequest": {"videoId": vid}, "params": "CAUwAg==", "disablePlayerResponse": True}, vid)
    for _, panel in _find(data, {"engagementPanelSectionListRenderer"}):
        if "audio-pivot" in (panel.get("targetId") or "") or "audio-pivot" in json.dumps(panel.get("identifier")):
            return _pivot_header(panel)
    return None, None, None


def _ytdlp_video(ref):
    vid = video_id(ref)
    with _ydl() as ydl:
        info = ydl.extract_info(shorts_url(vid), download=False, process=False)
        try:
            sound_id, sound_title, sound_author = _reel_sound(ydl, vid)
        except Exception as exc:  # noqa: BLE001 - sound fields are optional
            log(f"[yt-dlp] shorts sound lookup failed: {exc}")
            sound_id = sound_title = sound_author = None
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    return make_video(
        platform=PLATFORM, id=info.get("id") or vid, url=shorts_url(vid), caption=info.get("title"),
        author_id=info.get("channel_id"), author_username=_strip_at(info.get("uploader_id")),
        author_url=info.get("uploader_url") or info.get("channel_url"),
        author_followers=to_int(info.get("channel_follower_count")),
        views=to_int(info.get("view_count")), likes=to_int(info.get("like_count")),
        comments=to_int(info.get("comment_count")),
        posted_at=iso_from_timestamp(info.get("timestamp")) or iso_from_yyyymmdd(info.get("upload_date")),
        duration_s=to_int(info.get("duration")), sound_id=sound_id,
        sound_title=sound_title or info.get("track"), sound_author=sound_author or (", ".join(artists) or None),
        route="yt-dlp", raw_ref=vid)


def _ytdlp_sound(ref, limit=30):
    """Shorts that use this video's audio: yt-dlp's Shorts audio pivot (youtube.com/source/<id>/shorts).

    A Short that borrows audio pivots only to itself, so the sound's source video is looked up first.
    """
    vid = video_id(ref)
    with _ydl() as ydl:
        from yt_dlp.extractor.youtube import YoutubeShortsAudioPivotIE
        try:
            source = _reel_sound(ydl, vid)[0] or vid
        except Exception as exc:  # noqa: BLE001 - fall back to pivoting on the given video
            log(f"[yt-dlp] sound source lookup failed, pivoting on {vid}: {exc}")
            source = vid
        query = {"browseId": "FEsfv_audio_pivot",
                 "params": urllib.parse.unquote(YoutubeShortsAudioPivotIE._generate_audio_pivot_params(source))}

        def sound_fields(data):
            # The pivot header may name another upload of the same song; keep the id video() reports.
            _, title, author = _pivot_header(data)
            return dict(sound_id=source, sound_title=title, sound_author=author)

        return _paged(ydl, "browse", query, f"sound {source}", limit, extra_from=sound_fields)


def _json3_text(body):
    events = json.loads(body).get("events") or []
    return re.sub(r"\s+", " ", " ".join("".join(s.get("utf8", "") for s in e.get("segs") or []) for e in events)).strip()


def _vtt_text(body):
    lines, last = [], None
    for line in body.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if (not line or "-->" in line or line.isdigit()
                or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE"))):
            continue
        if line != last:
            lines.append(line)
            last = line
    return " ".join(lines)


def _original_audio_lang(info):
    """Language of the original audio track. Auto-dubbed videos carry many tracks and many '-orig' captions."""
    for f in info.get("formats") or []:
        if f.get("language") and ("original" in (f.get("format_note") or "").lower() or f.get("language_preference") == 10):
            return f["language"]
    return None


def _pick_captions(info):
    """(tracks, source, language, checked): original-language auto captions (-orig) first, then manual subtitles.
    checked is True when the track's language matches the original audio language YouTube reports, False when that
    language is unknown or differs (the only -orig track, or the first subtitles, were taken unchecked)."""
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if k.endswith("-orig") and v}
    lang = _original_audio_lang(info)
    keys = [f"{lang}-orig", f"{lang.split('-')[0]}-orig"] if lang else []
    key = next((k for k in keys if k in auto), None)
    checked = key is not None
    key = key or (next(iter(auto)) if len(auto) == 1 else None)
    if key:
        return auto[key], f"auto-captions:{key}", key.removesuffix("-orig"), checked
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v}
    if manual:
        base = (lang or "").split("-")[0]
        key = next((k for k in manual if base and k.split("-")[0] == base), None)
        checked = key is not None
        key = key or next(iter(manual))
        return manual[key], f"subtitles:{key}", key, checked
    return None, None, None, None


def _captions(ydl, info):
    """(text, source, language, checked) of the captions picked by _pick_captions; Nones when there are none."""
    tracks, source, language, checked = _pick_captions(info)
    track = next((t for ext in ("json3", "vtt") for t in tracks or [] if t.get("ext") == ext and t.get("url")), None)
    if not track:
        return None, None, None, None
    body = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
    text = _json3_text(body) if track["ext"] == "json3" else _vtt_text(body)
    return (text, source, language, checked) if text else (None, None, None, None)


def _ytdlp_download(ref, out_dir):
    """Video at 480p or below (smallest side), h264/mp4 preferred, merged with imageio-ffmpeg; plus captions."""
    vid = video_id(ref)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    opts = dict(skip_download=False, format="bv*+ba/b", format_sort=["res:480", "vcodec:h264", "ext:mp4:m4a"],
                merge_output_format="mp4", outtmpl=str(out / "%(id)s.%(ext)s"))
    with _ydl(**opts) as ydl:
        info = ydl.extract_info(shorts_url(vid), download=True)
        video_path = next((d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")), None)
        try:
            text, source, language, checked = _captions(ydl, info)
        except Exception as exc:  # noqa: BLE001 - captions are optional
            log(f"[yt-dlp] captions failed: {exc}")
            text = source = language = checked = None
    if not video_path or not Path(video_path).exists():
        raise RouteError("yt-dlp finished without a video file")
    return {"video_path": str(video_path), "captions_text": text, "captions_source": source,
            "captions_language": language, "captions_language_checked": checked}


# ---------- route 2: YouTube.js ----------

def _node(*args, timeout=300):
    node = _node_path()
    if not node:
        raise NotConfigured(f"Node.js is not installed. Install Node {NODE_MIN} or newer from nodejs.org, "
                            "then run /format-finder:setup.")
    if not NODE_SCRIPT.exists() or not (NODE_SCRIPT.parent / "node_modules" / "youtubei.js").exists():
        raise NotConfigured(f"YouTube.js is not set up ({NODE_SCRIPT} or its node_modules is missing). Run /format-finder:setup.")
    proc = subprocess.run([node, str(NODE_SCRIPT), *[str(a) for a in args]], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(NODE_SCRIPT.parent),
                          stdin=subprocess.DEVNULL)  # nothing it runs may read the caller's input (media._run)
    for line in proc.stderr.splitlines()[-20:]:
        log(f"[youtube.js] {line}")
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        raise RouteError(f"yt.mjs exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-400:]}") from exc
    if not out.get("ok"):
        raise RouteError(out.get("error") or "yt.mjs failed")
    return out["result"]


def _node_video(d):
    fields = {k: v for k, v in d.items() if k in VIDEO_KEYS and k not in ("platform", "route", "raw_ref")}
    return make_video(platform=PLATFORM, route="youtube.js", raw_ref=d.get("id"), **fields)


def _delay():
    return settings.get("request_delay_seconds") or 0


def _js_search(query, days=None, limit=30, region=None):
    def search_window(window):
        args = ["search", query, "--limit", limit, "--delay", _delay()]
        if window:
            args += ["--upload-date", window]
        if region:
            args += ["--region", region]
        return [_node_video(d) for d in _node(*args)]
    return _widened("youtube.js", query, days, search_window)


def _js_account(handle, limit=20):
    r = _node("channel", handle, "--limit", limit, "--delay", _delay())
    return make_account(platform=PLATFORM, id=r.get("id"), username=r.get("username"), url=r.get("url"),
                        followers=r.get("followers"), posts=[_node_video(d) for d in r.get("posts") or []],
                        route="youtube.js")


def _js_video(ref):
    return _node_video(_node("video", video_id(ref)))


def _js_sound(ref, limit=30):
    return [_node_video(d) for d in _node("sound", video_id(ref), "--limit", limit, "--delay", _delay())]


def _js_download(ref, out_dir):
    """Like _ytdlp_download: yt.mjs takes the same unchecked track (the only auto-caption track, or the first
    subtitles) when the audio language is unknown, and says so in captions_language_checked. An older yt.mjs that
    does not say is taken as unchecked, with the language read from captions_source."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    r = _node("download", video_id(ref), "--out", Path(out_dir).resolve(), "--ffmpeg", _ffmpeg() or "ffmpeg")
    source, checked = r.get("captions_source"), r.get("captions_language_checked")
    language = r.get("captions_language") or (source.split(":", 1)[-1].removesuffix("-orig") if source else None)
    if r.get("captions_text") and checked is None:
        checked = False
    return {"video_path": r.get("video_path"), "captions_text": r.get("captions_text"), "captions_source": source,
            "captions_language": language if r.get("captions_text") else None,
            "captions_language_checked": checked if r.get("captions_text") else None}


# ---------- public interface ----------

def search(query, days=None, limit=30, region=None):
    """Shorts for a keyword phrase. The envelope's window says which upload-date filter was asked for and which
    one the results came from ("year" after widening an empty search; results carry no date either way)."""
    query = keywords(query)
    if not query:
        raise RouteError("empty search phrase")
    _window_used[0] = None
    env = try_routes([("yt-dlp", _ytdlp_search), ("youtube.js", _js_search)], query, days=days, limit=limit,
                     region=region, _op="youtube.search")
    env["window"] = {"asked": upload_window(days) or "any", "used": _window_used[0]}
    return env


def account(handle, limit=20):
    return try_routes([("yt-dlp", _ytdlp_account), ("youtube.js", _js_account)], handle, limit=limit,
                      _op="youtube.account")


def video(ref):
    return try_routes([("yt-dlp", _ytdlp_video), ("youtube.js", _js_video)], ref, _op="youtube.video")


def sound(ref, limit=30):
    return try_routes([("yt-dlp", _ytdlp_sound), ("youtube.js", _js_sound)], ref, limit=limit,
                      _op="youtube.sound")


def download(ref, out_dir):
    return try_routes([("yt-dlp", _ytdlp_download), ("youtube.js", _js_download)], ref, out_dir,
                      _op="youtube.download")
