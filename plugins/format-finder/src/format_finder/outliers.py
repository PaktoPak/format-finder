"""Outlier maths: account normal, multiple, flags, batch rank and format checks. Pure functions, no network."""
import math
import re
from datetime import datetime, timezone
from statistics import median

FLAG_ORDER = ("account_not_looked_up", "account_lookup_failed", "too_few_posts", "no_views", "low_views", "paid",
              "suspected_boost", "rising_account", "meets_multiple", "top_fraction")
# Flags that keep a video from counting as an outlier (find-formats step 6; formats_check leaves them out).
DROP_FLAGS = ("account_not_looked_up", "account_lookup_failed", "too_few_posts", "no_views", "low_views", "paid",
              "suspected_boost", "rising_account")
SHARED_WORDS = 6  # words in a row two accounts' captions share to be flagged
FADING_MIN_DATED = 4  # dated videos the fading check needs (two per half)
# Two counted videos on two platforms this close in length (seconds; YouTube
# gives whole seconds) and posted this close together (days) may be one video one producer posted twice.
SAME_LENGTH_S = 1.0
SAME_POST_DAYS = 7


def account_key(video):
    """(platform, author) used to look up a video's account: author_id when known, else author_username.

    Build the `accounts` map passed to score() with this same function.
    """
    author = video.get("author_id")
    if author in (None, ""):
        author = video.get("author_username")
    return (video.get("platform"), None if author in (None, "") else str(author))


def dedupe(candidates):
    """Keep the first copy of each (platform, id), filling its None fields from later copies. Input is not changed."""
    out, seen = [], {}
    for c in candidates:
        key = (c.get("platform"), c.get("id"))
        if key[1] is None:
            out.append(dict(c))
            continue
        if key not in seen:
            seen[key] = dict(c)
            out.append(seen[key])
            continue
        kept = seen[key]
        for field, value in c.items():
            if kept.get(field) is None and value is not None:
                kept[field] = value
    return out


def _baseline(posts, exclude_id=None):
    """(views, paid): views of the posts with known views, in list order (accounts list posts newest first), leaving
    out exclude_id, repeated ids and posts the platform marks as paid (is_paid True: an ad, or on Instagram also a
    paid partnership), whose views may be bought; paid is how many of those were left out."""
    views, seen, paid = [], set(), 0
    for p in posts or []:
        pid = p.get("id")
        if pid is not None:
            if pid == exclude_id or pid in seen:
                continue
            seen.add(pid)
        if p.get("views") is None:
            continue
        if p.get("is_paid") is True:
            paid += 1
            continue
        views.append(p["views"])
    return views, paid


def _baseline_views(posts, exclude_id=None):
    return _baseline(posts, exclude_id)[0]


def account_normal(posts, exclude_id=None, min_posts=5):
    """Median views of the unpaid posts with known views, leaving out exclude_id; None when fewer than min_posts."""
    views = _baseline_views(posts, exclude_id)
    if not views or len(views) < min_posts:
        return None
    return median(views)


def _engagement(video):
    """(likes + comments) per view. An unknown comment count counts as 0 (yt-dlp leaves it empty on Shorts with no
    comments); unknown likes leave the rate unknown."""
    views, likes, comments = video.get("views"), video.get("likes"), video.get("comments")
    if not views or likes is None:
        return None
    return (likes + (comments or 0)) / views


def _setting(cfg, key):
    value = (cfg or {}).get(key)
    return value["value"] if isinstance(value, dict) and "value" in value else value


def _find_account(video, accounts):
    key = account_key(video)
    if key in accounts:
        return accounts[key]
    name = video.get("author_username")
    return accounts.get((video.get("platform"), str(name))) if name not in (None, "") else None


def _fill_from_account(video, account):
    """When search left views/likes/comments unknown, take them from the same video in the account history. is_paid
    too: True from either copy wins (some routes do not report ads, so one copy can say None where the other says
    paid)."""
    if not account or video.get("id") is None:
        return video
    same = next((p for p in account.get("posts") or [] if p.get("id") == video["id"]), None)
    if same:
        for field in ("views", "likes", "comments"):
            if video.get(field) is None and same.get(field) is not None:
                video[field] = same[field]
        if same.get("is_paid") is True or (video.get("is_paid") is None and same.get("is_paid") is not None):
            video["is_paid"] = same["is_paid"]
    return video


def _posts_before(video, posts):
    """The account's posts made before `video`, in list order (accounts list posts newest first): the posts after it
    when it is in the list; else, when its posted_at and some posts' posted_at are known, the posts dated earlier;
    else [] (a video missing from the list is older than every post there, since the list holds the newest ones)."""
    posts = list(posts or [])
    if video.get("id") is not None:
        at = next((i for i, p in enumerate(posts) if p.get("id") == video["id"]), None)
        if at is not None:
            return posts[at + 1:]
    posted = _parse_time(video.get("posted_at"))
    dated = [(p, _parse_time(p.get("posted_at"))) for p in posts]
    if posted is not None and any(t is not None for _, t in dated):
        return [p for p, t in dated if t is not None and t < posted]
    return []


def engagement_medians(videos):
    """{platform: median engagement_rate} over the videos with a rate: the batch rate suspected_boost compares with."""
    rates = {}
    for v in videos:
        if v.get("engagement_rate") is not None:
            rates.setdefault(v.get("platform"), []).append(v["engagement_rate"])
    return {p: median(r) for p, r in rates.items()}


def score(candidates, accounts, cfg, lookup_failed=()):
    """Add account_normal, account_posts_counted, account_paid_posts, multiple, recent_normal, recent_multiple,
    engagement_rate, boost_checked, flags and batch_rank to each candidate.

    candidates: list of Video dicts (deduped here by (platform, id)). accounts: {account_key: Account}.
    cfg: setting values for outlier_multiple_min, top_fraction, baseline_min_posts, boost_ratio, outlier_views_min and
    recent_normal_posts (a missing one turns its check off). A video whose account is not in `accounts` is flagged
    account_lookup_failed when its key is in lookup_failed (the history was asked for and none came back), else
    account_not_looked_up (never asked: over the lookup cap, or no author); too_few_posts is only for a history that
    came back. The recent normal is the median of the recent_normal_posts unpaid posts made just before the video
    (_posts_before); with fewer than that many before it, there is none.
    Returns new dicts sorted by multiple, highest first, unknown multiples last.
    """
    multiple_min = _setting(cfg, "outlier_multiple_min")
    top_fraction = _setting(cfg, "top_fraction")
    min_posts = _setting(cfg, "baseline_min_posts") or 0
    boost_ratio = _setting(cfg, "boost_ratio")
    views_min = _setting(cfg, "outlier_views_min")
    recent_posts = _setting(cfg, "recent_normal_posts")
    accounts = accounts or {}
    lookup_failed = set(lookup_failed or ())

    scored = []
    for c in dedupe(candidates):
        account = _find_account(c, accounts)
        c = _fill_from_account(c, account)
        posts = account.get("posts") if account else []
        baseline, paid = _baseline(posts, c.get("id"))
        counted = len(baseline)
        normal = median(baseline) if baseline and counted >= min_posts else None
        before = _baseline(_posts_before(c, posts), c.get("id"))[0][:recent_posts] if recent_posts else []
        recent = median(before) if normal is not None and recent_posts and len(before) >= recent_posts else None
        views = c.get("views")
        multiple = views / normal if views is not None and normal else None
        c.update(account_normal=normal, account_posts_counted=counted, account_paid_posts=paid, multiple=multiple,
                 recent_normal=recent, recent_multiple=views / recent if views is not None and recent else None,
                 engagement_rate=_engagement(c), boost_checked=False, batch_rank=None)
        failed = account is None and account_key(c) in lookup_failed
        c["flags"] = {"account_not_looked_up": account is None and not failed, "account_lookup_failed": failed,
                      "too_few_posts": account is not None and counted < min_posts,
                      "no_views": views is None,
                      "low_views": views is not None and bool(views_min) and views < views_min,
                      "paid": c.get("is_paid") is True}
        scored.append(c)

    platform_rate = engagement_medians(scored)  # likes per view differ by platform
    ranked = sorted((c for c in scored if c["multiple"] is not None), key=lambda c: -c["multiple"])
    for i, c in enumerate(ranked):
        prev = ranked[i - 1] if i else None
        c["batch_rank"] = prev["batch_rank"] if prev and prev["multiple"] == c["multiple"] else i + 1
    top_n = math.ceil(round(top_fraction * len(ranked), 9)) if top_fraction else 0

    for c in scored:
        flags = c["flags"]
        batch_rate = platform_rate.get(c.get("platform"))
        c["boost_checked"] = bool(boost_ratio) and batch_rate is not None and c["engagement_rate"] is not None
        flags["suspected_boost"] = c["boost_checked"] and c["engagement_rate"] < boost_ratio * batch_rate
        flags["meets_multiple"] = c["multiple"] is not None and multiple_min is not None and c["multiple"] >= multiple_min
        flags["rising_account"] = (flags["meets_multiple"] and c["recent_multiple"] is not None
                                   and c["recent_multiple"] < multiple_min)
        flags["top_fraction"] = c["batch_rank"] is not None and c["batch_rank"] <= top_n
        c["flags"] = [f for f in FLAG_ORDER if flags[f]]

    return ranked + [c for c in scored if c["multiple"] is None]


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fading(videos, min_span_days=None):
    dated = []
    for v in videos:
        t = _parse_time(v.get("posted_at"))
        if t is not None and v.get("multiple") is not None:
            dated.append((t, v))
    dated.sort(key=lambda tv: tv[0])
    out = {"dated_videos": len(dated), "min_dated_videos": FADING_MIN_DATED, "older_median_multiple": None,
           "newer_median_multiple": None, "older_span": None, "newer_span": None, "span_days": None,
           "too_recent": None}
    if len(dated) >= 2:
        out["span_days"] = round((dated[-1][0] - dated[0][0]).total_seconds() / 86400, 1)
        if min_span_days:
            out["too_recent"] = out["span_days"] < min_span_days
    if len(dated) < FADING_MIN_DATED or out["too_recent"]:
        return out
    half = len(dated) // 2
    older, newer = dated[:half], dated[-half:]
    out.update(
        older_median_multiple=median(v["multiple"] for _, v in older),
        newer_median_multiple=median(v["multiple"] for _, v in newer),
        older_span=[older[0][1]["posted_at"], older[-1][1]["posted_at"]],
        newer_span=[newer[0][1]["posted_at"], newer[-1][1]["posted_at"]],
    )
    return out


def not_counted(video, cfg=None):
    """Why a grouped video does not count toward its format ([] when it counts): its drop flags, plus what its own
    fields show when it was scored before a flag existed (is_paid, views under outlier_views_min), no multiple, or a
    multiple under outlier_multiple_min."""
    flags = video.get("flags") or []
    why = [f for f in DROP_FLAGS if f in flags]
    if "paid" not in why and video.get("is_paid") is True:
        why.append("paid")
    views_min = _setting(cfg, "outlier_views_min")
    if "low_views" not in why and views_min and video.get("views") is not None and video["views"] < views_min:
        why.append("low_views")
    multiple_min = _setting(cfg, "outlier_multiple_min")
    if video.get("multiple") is None:
        why.append("no_multiple")
    elif multiple_min is not None and video["multiple"] < multiple_min:
        why.append("below_outlier_multiple_min")
    return why


def _caption_words(caption):
    """A caption's words, lower case, hashtags and @mentions left out."""
    text = " ".join(w for w in str(caption or "").split() if not w.startswith(("#", "@")))
    return re.findall(r"[^\W_]+", text.lower())


def _runs(words):
    """Every SHARED_WORDS-word run of a word list."""
    return {tuple(words[i:i + SHARED_WORDS]) for i in range(len(words) - SHARED_WORDS + 1)}


def _phrases(captions, runs):
    """The runs as they read in these captions (lists of words), overlapping runs joined, in caption order."""
    out = []
    for words in captions:
        i = 0
        while i <= len(words) - SHARED_WORDS:
            if tuple(words[i:i + SHARED_WORDS]) not in runs:
                i += 1
                continue
            j = i
            while tuple(words[j + 1:j + 1 + SHARED_WORDS]) in runs:
                j += 1
            phrase = " ".join(words[i:j + SHARED_WORDS])
            if phrase not in out:
                out.append(phrase)
            i = j + 1
    return out


def _shared_captions(videos):
    """Accounts whose captions share a SHARED_WORDS-word run (a show's name, a repost's title, or only a common call
    to action): one entry per set of accounts that share runs, with those runs as phrases and how many accounts are
    left when that set alone counts as one producer. Also returns how many are left when every set counts as one."""
    captions, names = {}, {}
    for v in videos:
        key = account_key(v)
        if key[1] is not None:
            captions.setdefault(key, []).append(_caption_words(v.get("caption")))
            names[key] = names.get(key) or v.get("author_username") or key[1]
    holders = {}
    for key, word_lists in captions.items():
        for run in set().union(*map(_runs, word_lists)):
            holders.setdefault(run, set()).add(key)
    groups = {}
    for run, keys in holders.items():
        if len(keys) > 1:
            groups.setdefault(frozenset(keys), set()).add(run)
    parent = {k: k for k in captions}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    shared = []
    for keys, runs in sorted(groups.items(), key=lambda g: (-len(g[0]), sorted(map(str, g[0])))):
        members = sorted(keys, key=str)
        for k in members[1:]:
            parent[root(k)] = root(members[0])
        shared.append({"accounts": [names[k] for k in members], "platforms": sorted({k[0] for k in members}),
                       "phrases": _phrases(captions[members[0]], runs),
                       "accounts_if_one_producer": len(captions) - len(members) + 1})
    return shared, len({root(k) for k in captions})


def _plain_name(video):
    return re.sub(r"[^a-z0-9]", "", str(video.get("author_username") or "").lower())


def _names_on_two_platforms(videos):
    """Usernames (letters and digits only, lower case) that appear on more than one platform."""
    seen = {}
    for v in videos:
        name = _plain_name(v)
        if name:
            seen.setdefault(name, set()).add(v.get("platform"))
    return {n: sorted(p) for n, p in seen.items() if len(p) > 1}


def _cross_posts(videos):
    """Pairs of videos on two platforms, under different names, whose lengths are within SAME_LENGTH_S and whose
    dates, when both are known, are within SAME_POST_DAYS: possibly one video one producer posted under two handles.
    Returns [(video, video, days apart or None)]. Pairs under the same name are left to _names_on_two_platforms."""
    timed = [v for v in videos
             if isinstance(v.get("duration_s"), (int, float)) and not isinstance(v["duration_s"], bool)]
    pairs = []
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if a.get("platform") == b.get("platform") or (_plain_name(a) and _plain_name(a) == _plain_name(b)):
                continue
            if abs(a["duration_s"] - b["duration_s"]) > SAME_LENGTH_S:
                continue
            ta, tb = _parse_time(a.get("posted_at")), _parse_time(b.get("posted_at"))
            days = round(abs((ta - tb).total_seconds()) / 86400, 1) if ta and tb else None
            if days is None or days <= SAME_POST_DAYS:
                pairs.append((a, b, days))
    return pairs


def _cross_post_warning(a, b, days):
    def say(v):
        return (f"{v.get('platform')} {v.get('id')} ({v.get('author_username') or v.get('author_id')}, "
                f"{v['duration_s']:g} s)")
    when = f"{days:g} days apart" if days is not None else "a posting date unknown"
    return (f"{say(a)} and {say(b)} are within {SAME_LENGTH_S:g} s in length, {when}: possibly one video one "
            "producer posted on two platforms; compare the two videos")


def formats_check(groups, min_accounts, cfg=None):
    """Per format label: which videos count (not_counted), distinct accounts among them, whether that reaches
    min_accounts, the accounts that share caption phrases (each set with its own phrases and the count if that set
    alone is one producer; accounts_after_shared_captions is the lowest count, every set taken as one), median
    multiple (per video and per account), median views, and a fading signal.

    groups: {label: [scored candidate dicts]}. cfg: setting values for outlier_multiple_min, outlier_views_min and
    fading_min_span_days (missing ones turn their check off). Only counted videos go into the medians and the fading
    check. Fading compares the median multiple of the older and newer halves by posted_at (videos with a date and a
    multiple; odd middle one left out; needs FADING_MIN_DATED of them, and their dates must span fading_min_span_days:
    too_recent says whether they do, whenever two or more are dated). It reports both medians and does not decide.
    Warnings name a username on two platforms, and two videos on two platforms of about the same length posted close
    together (_cross_posts).
    """
    report = {}
    for label, videos in groups.items():
        videos = dedupe(videos)
        counted, left_out = [], []
        for v in videos:
            why = not_counted(v, cfg)
            if why:
                left_out.append({"platform": v.get("platform"), "id": v.get("id"),
                                 "author": v.get("author_username") or v.get("author_id"), "reasons": why})
            else:
                counted.append(v)
        listed = {k for k in (account_key(v) for v in videos) if k[1] is not None}
        known = {k for k in (account_key(v) for v in counted) if k[1] is not None}
        per_account = {}
        for v in counted:
            if v.get("multiple") is not None and account_key(v)[1] is not None:
                per_account.setdefault(account_key(v), []).append(v["multiple"])
        multiples = [v["multiple"] for v in counted if v.get("multiple") is not None]
        views = [v["views"] for v in counted if v.get("views") is not None]
        shared, after_shared = _shared_captions(counted)
        warnings = [f"{len(left_out)} of {len(videos)} videos do not count (see not_counted)"] if left_out else []
        warnings += [f"accounts {', '.join(s['accounts'])} share the caption words "
                     + " and ".join(f'"{x}"' for x in s["phrases"]) + ": if that names one show, series or creator, "
                     f"they are one producer ({s['accounts_if_one_producer']} accounts then)" for s in shared]
        warnings += [f"the name {name!r} posts on {' and '.join(p)}: probably one creator"
                     for name, p in _names_on_two_platforms(counted).items()]
        warnings += [_cross_post_warning(*pair) for pair in _cross_posts(counted)]
        report[label] = {
            "videos": len(videos),
            "videos_counted": len(counted),
            "accounts": len(known),
            "accounts_listed": len(listed),
            "videos_without_author": sum(1 for v in videos if account_key(v)[1] is None),
            "meets_min_accounts": len(known) >= min_accounts,
            "accounts_after_shared_captions": after_shared if known else 0,
            "shared_captions": shared,
            "median_multiple": median(multiples) if multiples else None,
            "median_multiple_per_account": median(median(m) for m in per_account.values()) if per_account else None,
            "median_views": median(views) if views else None,
            "videos_with_multiple": sum(1 for v in videos if v.get("multiple") is not None),
            "not_counted": left_out,
            "warnings": warnings,
            "fading": _fading(counted, _setting(cfg, "fading_min_span_days")),
        }
    return report
