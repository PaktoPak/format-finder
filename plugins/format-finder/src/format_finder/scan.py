"""Scan: search every phrase, add competitor and provided videos, fetch account histories, score outliers.

Writes candidates.json, accounts.json and run_log.json into the run folder. A platform that is not
configured or keeps failing is recorded and skipped; it never stops the run.
"""
import importlib
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import outliers, settings
from .common import AllRoutesFailed, keywords, log, pause, reset_route_memory, route_skips
from .instagram import time_from_pk

PLATFORMS = ("youtube", "tiktok", "instagram")
PHRASE_KEYS = ("niche", "adjacent", "far")
DISTANCES = ("niche", "competitor", "adjacent", "far", "provided")  # closest first
FAILS_TO_SKIP = {"search": 2, "account": 5, "video": 5, "details": 5}  # consecutive failures before an op is skipped
TOP_N = 30
DETAIL_FIELDS = ("posted_at", "likes", "comments")
NOT_FILLED = ("platform", "id", "route", "raw_ref")
EXACT_FROM_DETAILS = ("views", "author_followers")  # search lists round these (YouTube: '7.8M views'); video() does not
SCORE_KEYS = ("outlier_multiple_min", "top_fraction", "baseline_min_posts", "boost_ratio", "outlier_views_min",
              "recent_normal_posts")
FILL_FROM_ACCOUNT = {"author_username": "username", "author_url": "url", "author_followers": "followers"}
PROGRESS_EVERY = 20  # account lookups between progress lines on stderr


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, (str, dict)) else list(value)


def load_terms(path):
    """The terms JSON with every key filled in. Phrases are deduped across distances, closest distance kept."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the terms file must be a JSON object")
    terms, seen = {}, set()
    for key in PHRASE_KEYS:
        terms[key] = []
        for phrase in _as_list(data.get(key)):
            phrase = keywords(phrase)
            if phrase and phrase.lower() not in seen:
                seen.add(phrase.lower())
                terms[key].append(phrase)
    competitors = data.get("competitor_accounts") or {}
    if not isinstance(competitors, dict):
        raise ValueError(f"{path}: competitor_accounts must map a platform to a list of accounts")
    terms["competitor_accounts"] = {p: [] for p in PLATFORMS}
    for platform, handles in competitors.items():
        name = str(platform).strip().lower()
        if name not in PLATFORMS:
            log(f"[scan] ignoring competitor accounts for unknown platform {platform!r} (use {', '.join(PLATFORMS)})")
            continue
        terms["competitor_accounts"][name] += [str(h).strip() for h in _as_list(handles) if str(h).strip()]
    terms["videos"] = []
    for item in _as_list(data.get("videos")):
        if not isinstance(item, dict) or not item.get("platform") or not item.get("ref"):
            raise ValueError(f"{path}: each entry in videos needs a platform and a ref, got {item!r}")
        terms["videos"].append({"platform": str(item["platform"]).lower(), "ref": str(item["ref"]).strip()})
    unknown = sorted(set(data) - set(PHRASE_KEYS) - {"competitor_accounts", "videos"})
    if unknown:
        log(f"[scan] ignoring unknown keys in the terms file: {unknown}")
    return terms


def _posted(video):
    """When the video was posted: its posted_at, else for an Instagram video the creation time its media id encodes
    (instagram.time_from_pk; Instagram's Reels tab gives no dates). None when neither is known. The id's time is used
    only for the date window here: posted_at stays as it is."""
    if not video.get("posted_at"):
        return time_from_pk(video.get("id")) if video.get("platform") == "instagram" else None
    try:
        posted = datetime.fromisoformat(str(video["posted_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    return posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)


def _older_than(video, days):
    """True only when the video's time is known (_posted) and it is more than `days` old."""
    posted = _posted(video) if days else None
    return posted is not None and posted < datetime.now(timezone.utc) - timedelta(days=days)


def _handle(video):
    """What to pass to account(): YouTube takes the UC channel id best, the others a username."""
    order = (("author_id", "author_url", "author_username") if video.get("platform") == "youtube"
             else ("author_username", "author_url", "author_id"))
    return next((str(video[k]) for k in order if video.get(k) not in (None, "")), None)


def _account_keys(account, platform):
    """Every key a candidate of this account could be looked up by."""
    keys = {outliers.account_key(p) for p in account.get("posts") or []}
    for value in (account.get("id"), account.get("username")):
        if value not in (None, ""):
            keys.add((platform, str(value)))
    return {k for k in keys if k[1] is not None}


def _tag(video, phrase, distance, position=None, window=None):
    """A candidate with how it was found: phrase (or competitor handle), distance, its place in the results
    (None for provided videos) and, for YouTube search, the upload-date window the results came from."""
    tag = {"phrase": phrase, "distance": distance, "position": position}
    if window:
        tag["window"] = window
    return dict(video, phrase=phrase, distance=distance, found_by=[tag])


def merge_candidates(found):
    """One record per (platform, id): None fields filled from other copies, found_by joined, closest distance kept."""
    out, by_id = [], {}
    for c in found:
        key = (c.get("platform"), c.get("id"))
        if key[1] is None:
            out.append(dict(c))
            continue
        kept = by_id.get(key)
        if kept is None:
            by_id[key] = dict(c, found_by=list(c.get("found_by") or []))
            out.append(by_id[key])
            continue
        for field, value in c.items():
            if kept.get(field) is None and value is not None:
                kept[field] = value
        for tag in c.get("found_by") or []:
            if tag not in kept["found_by"]:
                kept["found_by"].append(tag)
        closest = min(kept["found_by"], key=lambda t: DISTANCES.index(t["distance"]))
        kept.update(phrase=closest["phrase"], distance=closest["distance"])
    return out


class _Run:
    """Bookkeeping for one scan: calls per route, every route error, skipped work, request pacing."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = {}
        self.errors = []
        self.skipped = []
        self.off = {}
        self.fails = Counter()
        self.made = 0
        self.last = None  # the envelope of the last call, for fields beyond the result (YouTube's search window)

    def _stats(self, platform, op):
        return self.calls.setdefault(platform, {}).setdefault(
            op, {"calls": 0, "ok": 0, "empty": 0, "failed": 0, "routes": {}})

    def skip(self, platform, what, reason, count=1):
        for s in self.skipped:
            if (s["platform"], s["what"], s["reason"]) == (platform, what, reason):
                s["count"] += count
                return
        self.skipped.append({"platform": platform, "what": what, "reason": reason, "count": count})

    def call(self, platform, op, ref, fn, *args, **kwargs):
        """Run one platform call and record it. Returns the result, or None when it failed or was skipped."""
        self.last = None
        if (platform, op) in self.off:
            self.skip(platform, op, self.off[(platform, op)])
            return None
        if self.made:
            pause(self.delay)
        self.made += 1
        stats = self._stats(platform, op)
        stats["calls"] += 1
        try:
            env = fn(*args, **kwargs)
            self.last = env
            route, errors, result = env.get("route"), env.get("errors") or [], env.get("result")
        except AllRoutesFailed as exc:
            route, errors, result = None, exc.errors, None
        except Exception as exc:  # noqa: BLE001 - a module bug or a bad ref must not stop the run
            route, errors, result = None, [{"route": None, "error": f"{type(exc).__name__}: {exc}"}], None
        for e in errors:
            self.errors.append({"platform": platform, "op": op, "ref": ref, "route": e.get("route"),
                                "error": e.get("error")})
        if route is None:
            stats["failed"] += 1
            self._failed(platform, op, ref, errors)
            return None
        self.fails[(platform, op)] = 0
        stats["routes"][route] = stats["routes"].get(route, 0) + 1
        empty = not result or (isinstance(result, dict) and "posts" in result and not result["posts"])
        stats["empty" if empty else "ok"] += 1
        return result

    def _failed(self, platform, op, ref, errors):
        messages = [str(e.get("error") or "") for e in errors]
        log(f"[scan] {platform} {op} {ref!r} failed on every route")
        if messages and all(m.startswith("NotConfigured") for m in messages):
            why = {}  # each distinct reason, with the routes that gave it
            for e, m in zip(errors, messages):
                why.setdefault(m.removeprefix("NotConfigured: "), []).append(str(e.get("route") or "?"))
            self.off[(platform, op)] = "not configured: " + "; ".join(
                f"{'/'.join(routes)}: {reason}" for reason, routes in why.items())
        elif messages and messages[-1].startswith("NotFound"):
            pass  # a handle with no profile: the routes work, so this is no reason to stop asking
        else:
            self.fails[(platform, op)] += 1
            if self.fails[(platform, op)] >= FAILS_TO_SKIP.get(op, 5):
                self.off[(platform, op)] = (f"every route failed on {self.fails[(platform, op)]} {op} calls in a "
                                            f"row; last errors: {' | '.join(messages)[:600]}")
        if (platform, op) in self.off:
            log(f"[scan] skipping further {platform} {op} calls: {self.off[(platform, op)]}")


def _module(name, run):
    try:
        return importlib.import_module(f"format_finder.{name}")
    except Exception as exc:  # noqa: BLE001 - a broken platform module must not stop the others
        run.skip(name, "platform", f"module cannot load: {type(exc).__name__}: {exc}")
        return None


def _trim(c):
    caption = c.get("caption")
    return {
        "platform": c.get("platform"), "id": c.get("id"), "url": c.get("url"),
        "author": c.get("author_username") or c.get("author_id"), "views": c.get("views"),
        "account_normal": c.get("account_normal"),
        "multiple": round(c["multiple"], 2) if c.get("multiple") is not None else None,
        "batch_rank": c.get("batch_rank"), "flags": c.get("flags"), "distance": c.get("distance"),
        "phrase": c.get("phrase"), "posted_at": c.get("posted_at"),
        "caption": caption[:100] if isinstance(caption, str) else caption,
    }


def _account_rows(path):
    """The rows of an accounts.json, or one row from the JSON `format-finder account` prints."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get("command") == "account":
        result = data.get("result")
        return [{"platform": data.get("platform"), "handle": data.get("handle") or (result or {}).get("username"),
                 "account": result if isinstance(result, dict) else None}]
    raise ValueError(f"{path}: not an accounts.json written by format-finder scan, nor the output of "
                     "format-finder account")


def _account_files(path):
    """The files --accounts-from names: an accounts.json or a saved account check, a run folder's accounts.json, or
    every saved account check (*.json) in a folder without one."""
    if not path.is_dir():
        return [path]
    if (path / "accounts.json").is_file():
        return [path / "accounts.json"]
    files = sorted(path.glob("*.json"))
    if not files:
        raise ValueError(f"{path}: no accounts.json and no saved account checks (*.json) in this folder")
    return files


def load_accounts(paths):
    """Account histories fetched before: {(platform, key or handle): (account, source path)} from each accounts.json
    (or run folder holding one) and each saved `format-finder account` answer (or folder of them), so a scan does not
    ask for them again. An account with no posts is left out unless it came back with a follower or post count (the
    profile answered and listed none); a reply with neither may be a block, so a later scan asks again."""
    found = {}
    for raw in paths or []:
        for path in _account_files(Path(raw)):
            for row in _account_rows(path):
                account = row.get("account") if isinstance(row, dict) else None
                platform = row.get("platform") if account else None
                if not platform or not (account.get("posts") or account.get("followers") or account.get("post_count")):
                    continue
                names = {tuple(row["key"])} if row.get("key") else set()
                names |= _account_keys(account, platform)
                names |= {(platform, str(v).strip().lstrip("@").lower())
                          for v in (row.get("handle"), account.get("username")) if v not in (None, "")}
                for name in names:
                    found.setdefault(name, (account, str(path)))
    return found


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def run(terms_path, platforms, out_dir, limit=None, max_accounts=None, details=None, accounts_from=None):
    """Search, collect account histories, score, and write the three run files. Returns a summary.

    limit, max_accounts and details override the search_limit, max_accounts_per_run and max_details_per_run
    settings for this run; details caps the video() lookups that fill in dates and likes/comments for the
    top outliers. accounts_from: earlier accounts.json files (or run folders), or saved `format-finder account`
    answers (files or a folder of them), whose account histories are used again instead of being fetched; they do
    not count toward max_accounts_per_run.
    """
    started, clock = _now(), time.monotonic()
    terms = load_terms(terms_path)
    reuse = load_accounts(accounts_from)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfg = {k: settings.get(k) for k in ("recent_days", "search_limit", "baseline_posts", "max_accounts_per_run",
                                         "max_details_per_run", "request_delay_seconds", *SCORE_KEYS)}
    cfg["search_limit"] = limit or cfg["search_limit"]
    cfg["max_accounts_per_run"] = max_accounts if max_accounts is not None else cfg["max_accounts_per_run"]
    cfg["max_details_per_run"] = details = details if details is not None else cfg["max_details_per_run"]
    days, posts_limit = cfg["recent_days"], cfg["baseline_posts"]
    run = _Run(cfg["request_delay_seconds"])
    reset_route_memory()  # route memory (common.try_routes) covers this run only

    wanted = []
    for p in (str(p).strip().lower() for p in platforms or [] if str(p).strip()):
        if p not in PLATFORMS:
            run.skip(p, "platform", f"unknown platform (use {', '.join(PLATFORMS)})")
        elif p not in wanted:
            wanted.append(p)
    mods = {p: m for p in wanted if (m := _module(p, run))}
    counts = {p: {"phrases_searched": 0, "search_results": 0, "competitor_posts": 0, "competitor_posts_too_old": 0,
                  "competitor_posts_undated": 0, "provided_videos": 0, "candidates": 0,
                  "accounts_with_candidates": 0, "account_lookups": 0, "accounts_with_posts": 0,
                  "accounts_over_cap": 0, "accounts_over_cap_above_floor": 0, "accounts_failed": 0,
                  "accounts_failed_above_floor": 0, "candidates_without_account": 0,
                  "accounts_reused": 0, "widened_searches": 0, "detail_lookups": 0, "too_old_after_details": 0,
                  "scored": 0, "meets_multiple": 0, "paid_outliers": 0, "paid_unknown": 0, "flags": {},
                  "engagement_median": None}
              for p in mods}
    found, accounts, account_rows, widened = [], {}, [], []

    def lookups():
        return sum(ops.get("account", {}).get("calls", 0) for ops in run.calls.values())

    def keep_account(platform, handle, source, account, key=None, n=0, reused_from=None):
        keys = _account_keys(account, platform) if account else set()
        if key:
            keys.add(key)
        key = key or next(iter(sorted(keys, key=str)), None)
        row = {"platform": platform, "handle": handle, "key": list(key) if key else None,
               "source": source, "candidates": n, "account": account}
        if reused_from:
            row["reused_from"] = reused_from
            counts[platform]["accounts_reused"] += 1
        account_rows.append(row)
        if account:
            counts[platform]["accounts_with_posts"] += bool(account.get("posts"))
            for k in keys:
                accounts.setdefault(k, account)

    # 1. Videos the user supplied.
    for item in terms["videos"]:
        p = item["platform"]
        if p not in mods:
            run.skip(p, "video", "platform not in this scan's --platforms" if p in PLATFORMS else "unknown platform")
            continue
        v = run.call(p, "video", item["ref"], mods[p].video, item["ref"])
        if v:
            found.append(_tag(v, None, "provided"))
            counts[p]["provided_videos"] += 1

    # 2. Competitor accounts: their recent posts become candidates and their history is the baseline.
    for p, handles in terms["competitor_accounts"].items():
        if handles and p not in mods:
            run.skip(p, "competitor account", "platform not in this scan's --platforms", len(handles))
            continue
        for handle in handles:
            again = reuse.get((p, handle.lstrip("@").lower()))
            if again:
                account = again[0]
                keep_account(p, handle, "competitor", account, reused_from=again[1])
            else:
                account = run.call(p, "account", handle, mods[p].account, handle, limit=posts_limit)
                keep_account(p, handle, "competitor", account)
            for i, post in enumerate((account or {}).get("posts") or []):
                if _older_than(post, days):
                    counts[p]["competitor_posts_too_old"] += 1
                    continue
                found.append(_tag(post, handle, "competitor", i))
                counts[p]["competitor_posts"] += 1
                counts[p]["competitor_posts_undated"] += _posted(post) is None  # no window check: only details date it

    # 3. Keyword search, every phrase on every platform.
    for p, mod in mods.items():
        for distance in PHRASE_KEYS:
            for phrase in terms[distance]:
                videos = run.call(p, "search", phrase, mod.search, phrase, days=days, limit=cfg["search_limit"])
                if videos is None:
                    continue
                window = (run.last or {}).get("window") or {}
                if window.get("used") and window.get("used") != window.get("asked"):
                    counts[p]["widened_searches"] += 1
                    widened.append({"platform": p, "phrase": phrase, "asked": window.get("asked"),
                                    "used": window["used"], "results": len(videos)})
                counts[p]["phrases_searched"] += 1
                counts[p]["search_results"] += len(videos)
                found += [_tag(v, phrase, distance, i, window.get("used")) for i, v in enumerate(videos)]

    candidates = merge_candidates(found)

    # 4. Account histories, up to the cap: first accounts with a result that can pass outlier_views_min (or whose
    #    views are not known yet), then the most frequent, ties to the higher-placed result, so the cap spreads
    #    across phrases and platforms.
    need, example, best, top_views, unknown_views = Counter(), {}, {}, {}, set()
    for c in candidates:
        key = outliers.account_key(c)
        if key[1] is None or c.get("platform") not in mods:
            if c.get("platform") in counts:
                counts[c["platform"]]["candidates_without_account"] += 1
            continue
        need[key] += 1
        example.setdefault(key, c)
        places = [t["position"] for t in c.get("found_by") or [] if t.get("position") is not None]
        best[key] = min([best.get(key, 10**6), *places])
        if c.get("views") is None:
            unknown_views.add(key)
        else:
            top_views[key] = max(top_views.get(key, 0), c["views"])
    views_min = cfg["outlier_views_min"]

    def below_floor(k):
        return bool(views_min) and k not in unknown_views and top_views.get(k, 0) < views_min

    for key in need:
        again = reuse.get(key) if key not in accounts else None
        if again:
            keep_account(key[0], _handle(example[key]), "candidates", again[0], key, need[key], reused_from=again[1])
    todo = sorted((k for k in need if k not in accounts), key=lambda k: (below_floor(k), -need[k], best[k]))
    for p in mods:
        counts[p]["accounts_with_candidates"] = sum(1 for k in need if k[0] == p)
    budget = max(0, cfg["max_accounts_per_run"] - lookups())
    over_cap = set(todo[budget:])
    for key in todo[budget:]:
        counts[key[0]]["accounts_over_cap"] += 1
        counts[key[0]]["accounts_over_cap_above_floor"] += not below_floor(key)  # could give a countable outlier
        run.skip(key[0], "account", f"over max_accounts_per_run ({cfg['max_accounts_per_run']}); accounts whose "
                                    "results all have views under outlier_views_min go last, then those with fewer "
                                    "and lower-placed results")
    if todo:
        above = sum(not below_floor(k) for k in over_cap)
        log(f"[scan] account histories: {min(budget, len(todo))} to look up, {len(over_cap)} over max_accounts_per_run"
            + (f" ({above} of them with a result that can pass outlier_views_min)" if over_cap else ""))
    for i, key in enumerate(todo[:budget], 1):
        p, handle = key[0], _handle(example[key])
        account = run.call(p, "account", handle, mods[p].account, handle, limit=posts_limit)
        keep_account(p, handle, "candidates", account, key, need[key])
        if i % PROGRESS_EVERY == 0 or i == min(budget, len(todo)):
            log(f"[scan] account histories {i}/{min(budget, len(todo))} ({p})")
    lookup_failed = {key for key in todo[:budget] if key not in accounts}  # every route failed, or the op was off
    for key in lookup_failed:
        counts[key[0]]["accounts_failed"] += 1
        counts[key[0]]["accounts_failed_above_floor"] += not below_floor(key)
    if lookup_failed:
        log(f"[scan] {len(lookup_failed)} account histories came back from no route (see errors and skipped)")
    for p in mods:
        counts[p]["account_lookups"] = run.calls.get(p, {}).get("account", {}).get("calls", 0)

    # 5. Fill author fields search left empty from the account record, then score.
    for c in candidates:
        account = accounts.get(outliers.account_key(c))
        for field, source in FILL_FROM_ACCOUNT.items():
            if account and c.get(field) is None and account.get(source) is not None:
                c[field] = account[source]
    score_cfg = {k: cfg[k] for k in SCORE_KEYS}
    scored = outliers.score(candidates, accounts, score_cfg, lookup_failed=lookup_failed)

    # 6. Details for the strongest outliers that search left without a date or likes/comments (YouTube Shorts
    #    results carry neither). Their exact views and followers replace the rounded counts search lists show.
    #    Known-old ones are dropped: the scan is for the last recent_days.
    by_key = {(c.get("platform"), c.get("id")): c for c in candidates if c.get("id") is not None}
    picks = [c for c in scored if "meets_multiple" in c["flags"] and "low_views" not in c["flags"]
             and c.get("id") is not None and any(c.get(f) is None for f in DETAIL_FIELDS)][:details]
    for c in picks:
        p, ref = c["platform"], c.get("url") or c.get("raw_ref") or c["id"]
        v = run.call(p, "details", ref, mods[p].video, ref)
        target = by_key[(p, c["id"])]
        for field, value in (v or {}).items():
            if value is None or field in NOT_FILLED:
                continue
            if target.get(field) is None or field in EXACT_FROM_DETAILS:
                target[field] = value
    for p in mods:
        counts[p]["detail_lookups"] = run.calls.get(p, {}).get("details", {}).get("calls", 0)
    keep = []
    for c in candidates:
        if c.get("distance") != "provided" and _older_than(c, days):
            counts[c["platform"]]["too_old_after_details"] += 1
        else:
            keep.append(c)
    if picks or len(keep) < len(candidates):
        candidates = keep
        scored = outliers.score(candidates, accounts, score_cfg, lookup_failed=lookup_failed)
    for c in scored:
        n = counts.get(c.get("platform"))
        if n is not None:
            flags = c.get("flags") or []
            n["candidates"] += 1
            n["scored"] += c.get("multiple") is not None
            n["meets_multiple"] += "meets_multiple" in flags
            n["paid_outliers"] += "meets_multiple" in flags and "paid" in flags
            n["paid_unknown"] += c.get("is_paid") is None  # no route said whether it is paid
            for f in flags:
                n["flags"][f] = n["flags"].get(f, 0) + 1

    for p, rate in outliers.engagement_medians(scored).items():
        if p in counts:
            counts[p]["engagement_median"] = rate
    routes_used = {p: {op: s["routes"] for op, s in ops.items() if s["routes"]} for p, ops in run.calls.items()}
    skipped_routes = route_skips()
    files = {name: str(out / f"{name}.json") for name in ("candidates", "accounts", "run_log")}
    _write(out / "candidates.json", scored)
    _write(out / "accounts.json", account_rows)
    _write(out / "run_log.json", {
        "started_at": started, "finished_at": _now(), "duration_s": round(time.monotonic() - clock, 1),
        "terms_path": str(Path(terms_path).resolve()), "terms": terms, "platforms": list(mods),
        "settings": cfg, "counts": counts, "calls": run.calls, "routes_used": routes_used,
        "widened_searches": widened, "route_skips": skipped_routes, "skipped": run.skipped, "errors": run.errors,
    })

    distinct = Counter((e["platform"], e["op"], e["route"], (e["error"] or "")[:200]) for e in run.errors)
    return {
        "ok": bool(scored), "command": "scan", "out_dir": str(out), "files": files,
        "counts": {"candidates": len(scored), "scored": sum(1 for c in scored if c.get("multiple") is not None),
                   "meets_multiple": sum(1 for c in scored if "meets_multiple" in (c.get("flags") or [])),
                   "outliers_without_drop_flags": sum(1 for c in scored if "meets_multiple" in (c.get("flags") or [])
                                                      and not set(c["flags"]) & set(outliers.DROP_FLAGS)),
                   "paid": sum(1 for c in scored if "paid" in (c.get("flags") or [])),
                   "paid_outliers": sum(n["paid_outliers"] for n in counts.values()),
                   "paid_unknown": sum(n["paid_unknown"] for n in counts.values()),
                   **{k: sum(n[k] for n in counts.values()) for k in (
                       "accounts_over_cap", "accounts_over_cap_above_floor", "accounts_failed",
                       "accounts_failed_above_floor")},
                   "accounts": sum(1 for r in account_rows if r["account"]), "route_errors": len(run.errors),
                   "per_platform": counts},
        "routes_used": routes_used,
        "widened_searches": widened,
        "route_skips": skipped_routes,
        "skipped": run.skipped,
        "errors": [{"platform": p, "op": op, "route": r, "error": msg, "times": n}
                   for (p, op, r, msg), n in distinct.most_common(15)],
        "top": [_trim(c) for c in scored[:TOP_N]],
    }
