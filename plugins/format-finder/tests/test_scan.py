"""Offline tests for scan.py with stand-in platform modules. Run: python -m unittest discover -s tests"""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import browser, scan  # noqa: E402
from format_finder.common import AllRoutesFailed, make_account, make_video, try_routes  # noqa: E402

SETTINGS = {"recent_days": 60, "search_limit": 30, "baseline_posts": 20, "max_accounts_per_run": 150,
            "max_details_per_run": 40, "request_delay_seconds": 0, "outlier_multiple_min": 5, "top_fraction": 0.1, "baseline_min_posts": 5,
            "boost_ratio": 0.1, "outlier_views_min": None, "recent_normal_posts": None}


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class BrowserStarted(BaseException):
    """Raised past every route's error handling, so a test that reaches a real browser fails loudly."""


def _isolate_settings():
    """The user's own ~/.format-finder/settings.json must not change what the tests see."""
    own = _mock.patch.object(_ff_settings, "USER_PATH", _Path(_tempfile.mkdtemp()) / "settings.json")
    own.start()
    unittest.addModuleCleanup(own.stop)


def setUpModule():
    """No unit test may start a real browser (it would send real requests to TikTok)."""
    guard = mock.patch.object(browser.Browser, "start", side_effect=BrowserStarted("a unit test started a browser"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)
    _isolate_settings()


def vid(platform, id, author, views, username=None, posted_at=None):
    return make_video(platform=platform, id=id, author_id=author, author_username=username, views=views,
                      posted_at=posted_at, route="stub")


def history(platform, author, n=6, views=100, username=None):
    posts = [vid(platform, f"{author}-old{i}", author, views, username, iso(i + 1)) for i in range(n)]
    return make_account(platform=platform, id=author, username=username or f"name-{author}", followers=1000,
                        posts=posts, route="stub")


def stub(name, search=None, account=None, video=None):
    calls = {"search": [], "account": [], "video": []}

    def wrap(op, fn):
        def call(*args, **kwargs):
            calls[op].append(args[0])
            return try_routes([("stub", fn)], *args, **kwargs)
        return call

    mod = types.ModuleType(f"format_finder.{name}")
    mod.search = wrap("search", search or (lambda q, **k: []))
    mod.account = wrap("account", account or (lambda h, **k: history(name, h)))
    mod.video = wrap("video", video or (lambda r: vid(name, r, "given", 50)))
    mod.calls = calls
    return mod


def not_configured(*a, **k):
    from format_finder.common import NotConfigured
    raise NotConfigured("Instagram needs a login: run format-finder instagram-login")


def blocked(*a, **k):
    raise RuntimeError("TikTok refused the search session")


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(scan.settings, "get", side_effect=SETTINGS.__getitem__)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def terms(self, **data):
        path = self.dir / "terms.json"
        path.write_text(json.dumps(data))
        return path

    def run_scan(self, mods, terms, platforms, **kw):
        with mock.patch.dict(sys.modules, {f"format_finder.{k}": v for k, v in mods.items()}):
            return scan.run(terms, platforms, self.dir / "out", **kw)

    def test_tags_dedupe_accounts_and_scores(self):
        def search(q, **k):
            if q == "meal plan":
                return [vid("youtube", "v1", "UC1", 1000), vid("youtube", "v2", "UC2", 100)]
            return [vid("youtube", "v1", "UC1", 1000), vid("youtube", "v3", "UC1", 50)]

        yt = stub("youtube", search=search)
        out = self.run_scan({"youtube": yt}, self.terms(niche=["meal plan"], far=["#gym tips"]), ["youtube"], details=0)
        self.assertEqual(yt.calls["search"], ["meal plan", "gym tips"])
        self.assertEqual(yt.calls["account"], ["UC1", "UC2"])  # UC1 has 2 candidates, so it goes first
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["accounts_with_candidates"], 2)
        cands = json.loads((self.dir / "out" / "candidates.json").read_text())
        v1 = next(c for c in cands if c["id"] == "v1")
        self.assertEqual(v1["distance"], "niche")
        self.assertEqual(len(v1["found_by"]), 2)
        self.assertEqual(v1["multiple"], 10)
        self.assertEqual(v1["author_username"], "name-UC1")  # filled from the account record
        self.assertIn("meets_multiple", v1["flags"])
        self.assertEqual(cands[0]["id"], "v1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["top"][0]["multiple"], 10)
        self.assertEqual(out["routes_used"]["youtube"]["search"], {"stub": 2})

    def test_not_configured_platform_is_skipped_without_retrying(self):
        ig = stub("instagram", search=not_configured, account=not_configured)
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", "v1", "UC1", 1000)])
        terms = self.terms(niche=["a", "b", "c"], competitor_accounts={"instagram": ["x", "y"]})
        out = self.run_scan({"youtube": yt, "instagram": ig}, terms, ["youtube", "instagram"])
        self.assertEqual(len(ig.calls["search"]), 1)
        self.assertEqual(len(ig.calls["account"]), 1)
        skipped = {(s["platform"], s["what"]): s for s in out["skipped"]}
        self.assertEqual(skipped[("instagram", "search")]["count"], 2)
        self.assertIn("not configured", skipped[("instagram", "search")]["reason"])
        self.assertTrue(out["ok"])
        log = json.loads((self.dir / "out" / "run_log.json").read_text())
        self.assertTrue(any(e["platform"] == "instagram" for e in log["errors"]))

    def test_failing_search_stops_after_two_but_accounts_still_run(self):
        tk = stub("tiktok", search=blocked)
        terms = self.terms(niche=["a", "b"], adjacent=["c"], competitor_accounts={"tiktok": ["brand"]})
        out = self.run_scan({"tiktok": tk}, terms, ["tiktok"])
        self.assertEqual(tk.calls["search"], ["a", "b"])
        self.assertEqual(tk.calls["account"], ["brand"])
        self.assertEqual(out["counts"]["per_platform"]["tiktok"]["competitor_posts"], 6)

    def test_competitor_posts_outside_window_and_provided_videos(self):
        def account(h, **k):
            acc = history("tiktok", "c1", n=6, username="brand")
            acc["posts"].append(vid("tiktok", "ancient", "c1", 10**6, "brand", iso(400)))
            return acc

        tk = stub("tiktok", account=account, video=lambda r: vid("tiktok", r, "c1", 900, "brand"))
        terms = self.terms(competitor_accounts={"tiktok": ["brand"]}, videos=[{"platform": "tiktok", "ref": "77"},
                                                                              {"platform": "instagram", "ref": "x"}])
        out = self.run_scan({"tiktok": tk}, terms, ["tiktok"])
        counts = out["counts"]["per_platform"]["tiktok"]
        self.assertEqual(counts["competitor_posts_too_old"], 1)
        self.assertEqual(counts["provided_videos"], 1)
        self.assertEqual(tk.calls["account"], ["brand"])  # the provided video's account was already fetched
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertEqual(cands["77"]["distance"], "provided")
        self.assertEqual(cands["77"]["multiple"], 9)
        self.assertIn(("instagram", "video"), {(s["platform"], s["what"]) for s in out["skipped"]})

    def test_undated_instagram_competitor_posts_use_the_media_id_time_for_the_window(self):
        """Instagram's Reels tab (browser route) gives no dates: the media id's time decides the window only."""
        from format_finder import instagram

        def pk(days_ago):
            ms = int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000)
            return str((ms - instagram.ID_EPOCH_MS) << 23 | 1234)

        def account(h, **k):
            posts = [vid("instagram", pk(d), "5200000002", 100, "brand") for d in (1, 2, 3, 4, 5, 6, 100, 400)]
            posts.append(vid("instagram", "not-a-pk", "5200000002", 100, "brand"))  # no time known: kept
            return make_account(platform="instagram", id="5200000002", username="brand", followers=1000,
                                posts=posts, route="browser")

        ig = stub("instagram", account=account)
        out = self.run_scan({"instagram": ig}, self.terms(competitor_accounts={"instagram": ["brand"]}),
                            ["instagram"], details=0)
        counts = out["counts"]["per_platform"]["instagram"]
        self.assertEqual((counts["competitor_posts"], counts["competitor_posts_too_old"]), (7, 2))
        cands = json.loads((self.dir / "out" / "candidates.json").read_text())
        self.assertEqual(len(cands), 7)
        self.assertTrue(all(c["posted_at"] is None for c in cands))  # posted_at is not filled from the id
        self.assertTrue(scan._older_than(vid("instagram", pk(61), "a", 1), 60))
        self.assertFalse(scan._older_than(vid("instagram", pk(59), "a", 1), 60))
        self.assertFalse(scan._older_than(vid("tiktok", pk(400), "a", 1), 60))  # other platforms: no date, kept

    def test_undated_competitor_posts_are_counted(self):
        """YouTube account histories carry no dates: those posts skip the window check, and the count says so."""
        def account(h, **k):
            acc = history("youtube", "UCfakebrand", n=6, username="fakebrand")
            for post in acc["posts"][:4]:
                post["posted_at"] = None
            return acc

        yt = stub("youtube", account=account)
        out = self.run_scan({"youtube": yt}, self.terms(competitor_accounts={"youtube": ["fakebrand"]}), ["youtube"],
                            details=0)
        counts = out["counts"]["per_platform"]["youtube"]
        self.assertEqual((counts["competitor_posts"], counts["competitor_posts_undated"],
                          counts["competitor_posts_too_old"]), (6, 4, 0))
        self.assertEqual(out["counts"]["paid_unknown"], 6)  # no YouTube route reports paid status

    def test_details_fill_top_outliers_and_drop_old_ones(self):
        def video(ref):
            vid_id = ref.rsplit("/", 1)[-1]
            v = vid("youtube", vid_id, "UC1", 999_999, posted_at=iso(5 if vid_id == "new" else 300))
            v.update(likes=10, comments=2)
            return v

        def search(q, **k):
            return [make_video(platform="youtube", id=i, url=f"https://www.youtube.com/shorts/{i}", author_id="UC1",
                               views=1000) for i in ("new", "old")] + [vid("youtube", "low", "UC1", 100)]

        yt = stub("youtube", search=search, video=video)
        out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], details=5)
        self.assertEqual(sorted(yt.calls["video"]), ["https://www.youtube.com/shorts/new",
                                                     "https://www.youtube.com/shorts/old"])
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertNotIn("old", cands)
        self.assertEqual(cands["new"]["views"], 999_999)  # video()'s exact count replaces search's rounded one
        self.assertEqual(cands["new"]["likes"], 10)
        self.assertIsNotNone(cands["new"]["engagement_rate"])
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["too_old_after_details"], 1)
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["detail_lookups"], 2)

    def test_details_off_counts_no_lookups(self):
        def search(q, **k):
            return [make_video(platform="youtube", id="new", author_id="UC1", views=1000)]

        yt = stub("youtube", search=search)
        out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], details=0)
        self.assertEqual(yt.calls["video"], [])
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["detail_lookups"], 0)

    def test_widened_youtube_search_is_recorded(self):
        def search(q, **k):
            env = try_routes([("stub", lambda q, **k: [vid("youtube", "w1", "UC1", 1000)])], q, **k)
            env["window"] = {"asked": "month", "used": "year"}
            return env

        yt = stub("youtube")
        yt.search = search
        out = self.run_scan({"youtube": yt}, self.terms(niche=["meal prep"]), ["youtube"])
        self.assertEqual(out["widened_searches"], [{"platform": "youtube", "phrase": "meal prep", "asked": "month",
                                                    "used": "year", "results": 1}])
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertEqual(cands["w1"]["found_by"][0]["window"], "year")
        log = json.loads((self.dir / "out" / "run_log.json").read_text())
        self.assertEqual(log["counts"]["youtube"]["widened_searches"], 1)

    def test_competitor_platform_names_any_case_and_provided_position(self):
        tk = stub("tiktok", account=lambda h, **k: history("tiktok", "c1", username="brand"),
                  video=lambda r: vid("tiktok", r, "c1", 900, "brand"))
        terms = self.terms(competitor_accounts={"TikTok": ["brand"], "Vimeo": ["x"]},
                           videos=[{"platform": "tiktok", "ref": "77"}])
        self.run_scan({"tiktok": tk}, terms, ["tiktok"])
        self.assertEqual(tk.calls["account"], ["brand"])
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertIsNone(cands["77"]["found_by"][0]["position"])

    def test_account_cap(self):
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", f"v{i}", f"UC{i}", 10) for i in range(4)])
        out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], max_accounts=2)
        self.assertEqual(len(yt.calls["account"]), 2)
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["accounts_over_cap"], 2)

    def test_account_cap_spreads_across_phrases(self):
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", f"{q}{i}", f"UC{q}{i}", 10) for i in range(3)])
        self.run_scan({"youtube": yt}, self.terms(niche=["a"], far=["b"]), ["youtube"], max_accounts=2)
        self.assertEqual(yt.calls["account"], ["UCa0", "UCb0"])

    def test_over_cap_accounts_are_flagged_not_looked_up_and_high_views_go_first(self):
        views = {"UC0": 12, "UC1": 412_000, "UC2": 30, "UC3": None}
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", f"v{i}", f"UC{i}", views[f"UC{i}"]) for i in range(4)])
        with mock.patch.dict(SETTINGS, outlier_views_min=5000), contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], max_accounts=2, details=0)
        self.assertEqual(yt.calls["account"], ["UC1", "UC3"])  # can pass the floor (or views unknown) first
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertEqual(cands["v0"]["flags"], ["account_not_looked_up", "low_views"])
        self.assertNotIn("too_few_posts", cands["v2"]["flags"])
        self.assertIn("meets_multiple", cands["v1"]["flags"])
        self.assertEqual(out["counts"]["accounts_over_cap"], 2)
        self.assertEqual(out["counts"]["accounts_over_cap_above_floor"], 0)  # a second pass could add no outlier
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["flags"]["account_not_looked_up"], 2)
        self.assertIn("[scan] account histories 2/2 (youtube)", err.getvalue())

    def test_over_cap_accounts_that_could_pass_the_view_floor_are_counted(self):
        views = {"UC0": 9_000, "UC1": 412_000, "UC2": 30, "UC3": None}
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", f"v{i}", f"UC{i}", views[f"UC{i}"]) for i in range(4)])
        with mock.patch.dict(SETTINGS, outlier_views_min=5000), contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], max_accounts=2, details=0)
        self.assertEqual(yt.calls["account"], ["UC0", "UC1"])
        counts = out["counts"]["per_platform"]["youtube"]
        self.assertEqual((counts["accounts_over_cap"], counts["accounts_over_cap_above_floor"]), (2, 1))  # UC3: views unknown
        self.assertEqual(out["counts"]["accounts_over_cap_above_floor"], 1)
        self.assertIn("2 over max_accounts_per_run (1 of them with a result that can pass outlier_views_min)",
                      err.getvalue())

    def test_failed_account_lookups_are_flagged_and_counted(self):
        def account(h, **k):
            raise RuntimeError("HTTP 429")

        yt = stub("youtube", search=lambda q, **k: [vid("youtube", f"v{i}", f"UC{i}", 400_000) for i in range(8)],
                  account=account)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.run_scan({"youtube": yt}, self.terms(niche=["a"]), ["youtube"], details=0)
        self.assertEqual(len(yt.calls["account"]), 5)  # the scan stops asking after 5 failures in a row
        counts = out["counts"]["per_platform"]["youtube"]
        self.assertEqual((counts["accounts_over_cap"], counts["accounts_failed"], out["counts"]["accounts_failed"]),
                         (0, 8, 8))
        self.assertEqual((counts["accounts_failed_above_floor"], out["counts"]["accounts_failed_above_floor"]), (8, 8))
        cands = json.loads((self.dir / "out" / "candidates.json").read_text())
        self.assertEqual({tuple(c["flags"]) for c in cands}, {("account_lookup_failed",)})
        self.assertNotIn("too_few_posts", counts["flags"])
        self.assertIn("8 account histories came back from no route", err.getvalue())

    def test_saved_account_checks_are_used_again(self):
        checks = self.dir / "account_checks"
        checks.mkdir()
        acc = history("tiktok", "7001", username="fake.brand")
        (checks / "tiktok-fake.brand.json").write_text(json.dumps(
            {"platform": "tiktok", "command": "account", "ok": True, "route": "stub", "errors": [], "result": acc,
             "handle": "@Fake.Brand"}))
        (checks / "tiktok-gone.json").write_text(json.dumps(
            {"platform": "tiktok", "command": "account", "ok": False, "route": None, "errors": [], "result": None}))
        tk = stub("tiktok", search=blocked)
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"tiktok": tk}, self.terms(competitor_accounts={"tiktok": ["fake.brand", "other"]}),
                                ["tiktok"], details=0, accounts_from=[str(checks)])
        self.assertEqual(tk.calls["account"], ["other"])  # fake.brand came from its saved check
        counts = out["counts"]["per_platform"]["tiktok"]
        self.assertEqual((counts["accounts_reused"], counts["competitor_posts"]), (1, 12))
        rows = json.loads((self.dir / "out" / "accounts.json").read_text())
        self.assertEqual(rows[0]["reused_from"], str(checks / "tiktok-fake.brand.json"))
        found = scan.load_accounts([str(checks / "tiktok-fake.brand.json")])
        self.assertIn(("tiktok", "fake.brand"), found)
        (self.dir / "empty").mkdir()
        with self.assertRaises(ValueError):
            scan.load_accounts([str(self.dir / "empty")])
        (self.dir / "bad.json").write_text(json.dumps({"command": "search"}))
        with self.assertRaises(ValueError):
            scan.load_accounts([str(self.dir / "bad.json")])

    def test_saved_account_with_followers_but_no_posts_is_used_again(self):
        """A profile that answered with followers and a post count but listed no reels is a real answer: not asked
        again. One that came back with nothing at all may have been blocked: asked again."""
        checks = self.dir / "account_checks"
        checks.mkdir()
        for handle, acc in (("fake.noreels", make_account(platform="instagram", id="5200000009", username="fake.noreels",
                                                          followers=1500, post_count=250, route="browser")),
                            ("fake.blank", make_account(platform="instagram", username="fake.blank", route="browser"))):
            (checks / f"instagram-{handle}.json").write_text(json.dumps(
                {"platform": "instagram", "command": "account", "ok": True, "route": "browser", "errors": [],
                 "result": acc, "handle": handle, "empty": True}))
        found = scan.load_accounts([str(checks)])
        self.assertIn(("instagram", "fake.noreels"), found)
        self.assertNotIn(("instagram", "fake.blank"), found)
        ig = stub("instagram")
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"instagram": ig}, self.terms(competitor_accounts={"instagram": ["fake.noreels",
                                                                                                "fake.blank"]}),
                                ["instagram"], details=0, accounts_from=[str(checks)])
        self.assertEqual(ig.calls["account"], ["fake.blank"])
        self.assertEqual(out["counts"]["per_platform"]["instagram"]["accounts_reused"], 1)

    def test_paid_outliers_are_counted(self):
        def account(h, **k):
            acc = history("tiktok", "c1", n=6, username="brand")
            ad = vid("tiktok", "ad1", "c1", 900_000, "brand", iso(2))
            ad["is_paid"] = True
            acc["posts"].insert(0, ad)
            return acc

        tk = stub("tiktok", account=account)
        out = self.run_scan({"tiktok": tk}, self.terms(competitor_accounts={"tiktok": ["brand"]}), ["tiktok"], details=0)
        self.assertEqual(out["counts"]["paid_outliers"], 1)
        self.assertEqual(out["counts"]["outliers_without_drop_flags"], 0)
        # the other 6 posts are not marked: on TikTok that means not an ad, and a paid partnership is never reported
        self.assertEqual((out["counts"]["paid"], out["counts"]["paid_unknown"]), (1, 6))
        self.assertEqual(out["counts"]["per_platform"]["tiktok"]["paid_unknown"], 6)
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertEqual(cands["ad1"]["account_paid_posts"], 0)  # its own post is not in its baseline
        self.assertEqual(cands["c1-old0"]["account_paid_posts"], 1)

    def test_a_follow_up_scan_reuses_earlier_account_histories(self):
        yt = stub("youtube", search=lambda q, **k: [vid("youtube", "v1", "UC1", 1000), vid("youtube", "v2", "UC2", 900)])
        tk = stub("tiktok", search=blocked, account=lambda h, **k: history("tiktok", "c1", username="brand"))
        terms = self.terms(niche=["a"], competitor_accounts={"tiktok": ["@Brand"]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_scan({"youtube": yt, "tiktok": tk}, terms, ["youtube"], details=0)
            first = self.dir / "first"
            (self.dir / "out").rename(first)
            yt2 = stub("youtube", search=lambda q, **k: [vid("youtube", "v1", "UC1", 1000), vid("youtube", "v3", "UC3", 5)])
            out = self.run_scan({"youtube": yt2}, self.terms(niche=["b"]), ["youtube"], details=0,
                                accounts_from=[str(first)])
        self.assertEqual(yt2.calls["account"], ["UC3"])  # UC1 came from the first scan
        self.assertEqual(out["counts"]["per_platform"]["youtube"]["accounts_reused"], 1)
        rows = json.loads((self.dir / "out" / "accounts.json").read_text())
        self.assertEqual([r.get("reused_from") for r in rows], [str(first / "accounts.json"), None])
        cands = {c["id"]: c for c in json.loads((self.dir / "out" / "candidates.json").read_text())}
        self.assertEqual(cands["v1"]["multiple"], 10)
        # competitor handles are matched however they are written
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_scan({"tiktok": tk}, self.terms(competitor_accounts={"tiktok": ["@Brand"]}), ["tiktok"])
            (self.dir / "out").rename(self.dir / "tk")
            out = self.run_scan({"tiktok": tk}, self.terms(competitor_accounts={"tiktok": ["brand"]}), ["tiktok"],
                                accounts_from=[str(self.dir / "tk" / "accounts.json")])
        self.assertEqual(tk.calls["account"], ["@Brand"])
        self.assertEqual(out["counts"]["per_platform"]["tiktok"]["competitor_posts"], 6)

    def test_skip_reason_names_each_route_that_is_not_configured(self):
        from format_finder.common import NotConfigured

        def search(q, **k):
            def off(msg):
                def route(*a, **kw):
                    raise NotConfigured(msg)
                return route
            return try_routes([("instagrapi", off("Instagram needs a login")), ("aiograpi", off("Instagram needs a login")),
                               ("browser", off("Instagram search through the browser is off"))], q, **k)

        ig = stub("instagram")
        ig.search = search
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"instagram": ig}, self.terms(niche=["a", "b"]), ["instagram"])
        reason = next(s["reason"] for s in out["skipped"] if s["what"] == "search")
        self.assertEqual(reason, "not configured: instagrapi/aiograpi: Instagram needs a login; "
                                 "browser: Instagram search through the browser is off")

    def test_handles_with_no_profile_do_not_stop_account_lookups(self):
        from format_finder.common import NotFound

        def account(h, **k):
            if h.startswith("gone"):
                raise NotFound(f"no profile @{h}")
            return history("instagram", "5", username=h)

        ig = stub("instagram", search=not_configured, account=account)
        handles = [f"gone{i}" for i in range(6)] + ["real"]
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"instagram": ig}, self.terms(competitor_accounts={"instagram": handles}), ["instagram"])
        self.assertEqual(ig.calls["account"], handles)
        self.assertNotIn(("instagram", "account"), {(s["platform"], s["what"]) for s in out["skipped"]})

    def test_a_not_found_followed_by_a_failing_route_counts_as_a_failure(self):
        def account(h, **k):
            raise AllRoutesFailed([{"route": "omkarcloud", "error": f"NotFound: no profile @{h}, or no data"},
                                   {"route": "browser", "error": "BotCheck: fake captcha"}])

        tt = stub("tiktok", search=not_configured, account=account)
        handles = [f"brand{i}" for i in range(7)]
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"tiktok": tt}, self.terms(competitor_accounts={"tiktok": handles}), ["tiktok"])
        self.assertEqual(len(tt.calls["account"]), 5)  # FAILS_TO_SKIP for account lookups
        self.assertIn(("tiktok", "account"), {(s["platform"], s["what"]) for s in out["skipped"]})

    def test_nothing_works(self):
        out = self.run_scan({"youtube": stub("youtube", search=blocked)}, self.terms(niche=["a"]), ["youtube", "vimeo"])
        self.assertFalse(out["ok"])
        self.assertIn(("vimeo", "platform"), {(s["platform"], s["what"]) for s in out["skipped"]})
        self.assertTrue((self.dir / "out" / "run_log.json").is_file())



def routed(name, search_routes=(), account_routes=()):
    """A stand-in platform module whose search and account run several routes with route memory (_op)."""
    mod = stub(name)
    if search_routes:
        mod.search = lambda q, **k: try_routes(list(search_routes), q, _op=f"{name}.search", **k)
    if account_routes:
        mod.account = lambda h, **k: try_routes(list(account_routes), h, _op=f"{name}.account", **k)
    return mod


class Counted:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, *a, **k):
        self.calls += 1
        return self.fn(*a, **k)


def captcha(*a, **k):
    from format_finder.browser import BotCheck
    raise BotCheck("TikTok asked for a captcha (verify/slide)", captcha=True)


class RouteMemory(unittest.TestCase):
    setUp, terms, run_scan = Scan.setUp, Scan.terms, Scan.run_scan

    def test_captcha_skips_the_browser_route_and_the_scan_rule_still_stops_search(self):
        omkar, browser = Counted(blocked), Counted(captcha)
        tk = routed("tiktok", search_routes=[("omkarcloud", omkar), ("browser", browser)])
        terms = self.terms(niche=["a", "b"], far=["c"], competitor_accounts={"tiktok": ["brand"]})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = self.run_scan({"tiktok": tk}, terms, ["tiktok"])
        self.assertEqual((omkar.calls, browser.calls), (2, 1))  # browser skipped after its captcha
        self.assertIn(("tiktok", "search"), {(s["platform"], s["what"]) for s in out["skipped"]})  # scan rule: 2 in a row
        self.assertEqual(out["counts"]["per_platform"]["tiktok"]["competitor_posts"], 6)  # accounts unaffected
        skips = {(s["op"], s["route"]): s for s in out["route_skips"]}
        self.assertTrue(skips[("tiktok.search", "browser")]["captcha"])
        self.assertEqual(skips[("tiktok.search", "browser")]["skipped_calls"], 1)
        self.assertFalse(skips[("tiktok.search", "omkarcloud")]["captcha"])
        log = json.loads((self.dir / "out" / "run_log.json").read_text())
        self.assertEqual(log["route_skips"], out["route_skips"])
        self.assertEqual(err.getvalue().count("skipping the browser route"), 1)

    def test_failing_route_is_skipped_while_the_next_one_answers(self):
        omkar = Counted(blocked)
        ytdlp = Counted(lambda h, **k: history("tiktok", h))
        search = lambda q, **k: [vid("tiktok", f"{q}{i}", f"acc{q}{i}", 1000) for i in range(2)]
        tk = routed("tiktok", search_routes=[("stub", search)],
                    account_routes=[("omkarcloud", omkar), ("yt-dlp", ytdlp)])
        with contextlib.redirect_stderr(io.StringIO()):
            out = self.run_scan({"tiktok": tk}, self.terms(niche=["a", "b"]), ["tiktok"], details=0)
        self.assertEqual((omkar.calls, ytdlp.calls), (2, 4))
        self.assertEqual(out["counts"]["per_platform"]["tiktok"]["accounts_with_posts"], 4)
        self.assertEqual(out["routes_used"]["tiktok"]["account"], {"yt-dlp": 4})
        self.assertEqual([(s["op"], s["route"], s["skipped_calls"]) for s in out["route_skips"]],
                         [("tiktok.account", "omkarcloud", 2)])
        self.assertNotIn("tiktok", {s["platform"] for s in out["skipped"]})

    def test_each_scan_starts_with_no_route_memory(self):
        browser = Counted(captcha)
        tk = routed("tiktok", search_routes=[("omkarcloud", Counted(lambda q, **k: [])), ("browser", browser)])
        with contextlib.redirect_stderr(io.StringIO()):
            self.run_scan({"tiktok": tk}, self.terms(niche=["a", "b"]), ["tiktok"])
            self.assertEqual(browser.calls, 1)
            out = self.run_scan({"tiktok": tk}, self.terms(niche=["a"]), ["tiktok"])
        self.assertEqual(browser.calls, 2)
        self.assertEqual(len(out["route_skips"]), 1)


if __name__ == "__main__":
    unittest.main()
