"""Apple App Store search through the free iTunes Search API. No key, no login."""
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .common import RouteError, log, ssl_context, to_int, try_routes

SEARCH_URL = "https://itunes.apple.com/search"
MIN_INTERVAL_S = 3.1  # Apple allows roughly 20 calls per minute; stay under it
TIMEOUT_S = 20
DESCRIPTION_CHARS = 400
USER_AGENT = f"format-finder/{__version__}"
SITE_BYTES = 2 * 2**20  # most of a developer's home page read for its social links
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
# Profile links on a developer's site. Instagram: only instagram.com itself (not help., about., api. and other
# subdomains), the profile in an /_u/ app link, and no path that is not a profile (posts, reels, tag and explore
# pages, embed and static files), so no hashtag page is ever listed. YouTube: @handles as they are, the other forms
# as youtube.com/... paths that account() opens as given.
SOCIAL = {
    "tiktok": re.compile(r"tiktok\.com/@([A-Za-z0-9._]{2,24})", re.I),
    "instagram": re.compile(r"(?<![\w.-])(?:www\.)?instagram\.com/(?:_u/)?([A-Za-z0-9._]{1,30})(?=[/\"'?#\s<]|$)",
                            re.I),
    "youtube": re.compile(r"youtube\.com/(@[A-Za-z0-9._-]{3,30}|channel/UC[\w-]{22}|c/[\w.-]+|user/[\w.-]+)", re.I),
}
NOT_PROFILES = {"instagram": {"p", "reel", "reels", "explore", "tv", "stories", "accounts", "about", "developer",
                              "legal", "direct", "web", "privacy", "share", "s", "_u", "_n", "oauth", "static", "embed",
                              "embed.js"}}
FILE_ENDINGS = (".js", ".css", ".json", ".xml", ".txt", ".html", ".php", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                ".webp", ".ico", ".mp4")  # a link to a file, not a profile (handles may contain dots: brand.app)

_last_call = [None]


def _throttle():
    """Space calls in this process at least MIN_INTERVAL_S apart."""
    wait = 0 if _last_call[0] is None else _last_call[0] + MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        log(f"[itunes-search] waiting {wait:.1f}s to stay under Apple's rate limit")
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _ssl_context():
    return ssl_context()


def _get_json(url):
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RouteError(f"HTTP {exc.code} from iTunes Search API") from exc
    except urllib.error.URLError as exc:
        raise RouteError(f"could not reach iTunes Search API: {exc.reason}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise RouteError("iTunes Search API did not return JSON") from exc


def _price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rating(value):
    return round(float(value), 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize(item):
    """One iTunes software result -> {id, name, developer, developer_url, genre, genres, rating_count, rating, url,
    price, description}. developer_url is the developer's own website (iTunes sellerUrl), where a brand usually
    links its real social accounts."""
    url = item.get("trackViewUrl")
    description = (item.get("description") or "").strip()
    return {
        "id": str(item["trackId"]) if item.get("trackId") is not None else None,
        "name": item.get("trackName"),
        "developer": item.get("artistName") or item.get("sellerName"),
        "developer_url": item.get("sellerUrl") or None,
        "genre": item.get("primaryGenreName"),
        "genres": list(item["genres"]) if item.get("genres") else None,
        "rating_count": to_int(item.get("userRatingCount")),
        "rating": _rating(item.get("averageUserRating")),
        "url": url.split("?")[0] if url else None,
        "price": _price(item.get("price")),
        "description": description[:DESCRIPTION_CHARS] or None,
    }


def social_links(html):
    """{"tiktok": [...], "instagram": [...], "youtube": [...]}: the profile handles a page links to, in page order."""
    out = {}
    for platform, rx in SOCIAL.items():
        found = []
        for m in rx.finditer(html or ""):
            name = m.group(1)
            if platform == "instagram" and name.lower().endswith(FILE_ENDINGS):
                continue
            if platform == "youtube" and not name.startswith("@"):
                name = f"youtube.com/{name}"
            if name.lower() in NOT_PROFILES.get(platform, ()) or name.lower() in {f.lower() for f in found}:
                continue
            found.append(name)
        out[platform] = found
    return out


def _site_links(url):
    """The social profile links on a developer's home page, or {"error": ...}. Plain GET, no cookies."""
    if not re.match(r"https?://", url or "", re.I):
        return {"error": f"not a web address: {url!r}"}
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_ssl_context()) as resp:
            html = resp.read(SITE_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:  # IncompleteRead and others
        return {"error": f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"}
    return social_links(html)


def _itunes_search(query, country="us", limit=25, links=False):
    limit = max(1, min(int(limit or 25), 200))
    params = {"term": query, "entity": "software", "country": (country or "us").lower(), "limit": limit}
    data = _get_json(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
    if not isinstance(data, dict) or "results" not in data:
        raise RouteError("unexpected iTunes Search API response")
    apps = [normalize(r) for r in data["results"] if isinstance(r, dict) and r.get("trackId") is not None][:limit]
    if links:
        sites = {}
        for app in apps:
            site = app["developer_url"]
            if site and site not in sites:
                log(f"[apps] reading social links from {site}")
                try:
                    sites[site] = _site_links(site)
                except Exception as exc:  # noqa: BLE001 - one bad site must not lose the other apps
                    sites[site] = {"error": f"{type(exc).__name__}: {exc}"}
            app["social_links"] = sites.get(site) if site else {"error": "the App Store lists no developer website"}
    return apps


def search(query, country="us", limit=25, links=False):
    """Competitor apps for a keyword from the App Store of `country` (two-letter code). links=True also reads each
    developer's website once for the TikTok, Instagram and YouTube profiles it links to (social_links)."""
    return try_routes([("itunes-search", _itunes_search)], query, country=country, limit=limit, links=links)
