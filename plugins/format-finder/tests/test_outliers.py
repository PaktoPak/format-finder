"""Unit tests for outliers.py with synthetic data. Run: python -m unittest discover -s tests"""
import sys
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import outliers  # noqa: E402
from format_finder.common import make_account, make_video  # noqa: E402

CFG = {"outlier_multiple_min": 5, "top_fraction": 0.1, "baseline_min_posts": 5, "boost_ratio": 0.1}


def vid(id, views, author="a1", platform="tiktok", likes=None, comments=None, posted_at=None, username=None):
    return make_video(platform=platform, id=id, views=views, author_id=author, author_username=username,
                      likes=likes, comments=comments, posted_at=posted_at)


def acct(author, views, platform="tiktok", prefix=None):
    prefix = prefix or f"{author}-p"
    posts = [vid(f"{prefix}{i}", v, author=author, platform=platform) for i, v in enumerate(views)]
    return make_account(platform=platform, id=author, posts=posts)


def accounts_for(*accts):
    return {(a["platform"], a["id"]): a for a in accts}


def by_id(scored):
    return {c["id"]: c for c in scored}



def _isolate_settings():
    """The user's own ~/.format-finder/settings.json must not change what the tests see."""
    own = _mock.patch.object(_ff_settings, "USER_PATH", _Path(_tempfile.mkdtemp()) / "settings.json")
    own.start()
    unittest.addModuleCleanup(own.stop)


def setUpModule():
    _isolate_settings()


class AccountNormal(unittest.TestCase):
    def test_median_odd_and_even(self):
        self.assertEqual(outliers.account_normal(acct("a", [1, 5, 3])["posts"], min_posts=1), 3)
        self.assertEqual(outliers.account_normal(acct("a", [1, 2, 3, 10])["posts"], min_posts=1), 2.5)

    def test_excludes_candidate(self):
        posts = acct("a", [100, 100, 100, 100, 100])["posts"] + [vid("cand", 10_000, author="a")]
        self.assertEqual(outliers.account_normal(posts, exclude_id="cand", min_posts=5), 100)
        self.assertEqual(outliers.account_normal(posts, exclude_id="missing", min_posts=5), 100)
        self.assertEqual(outliers.account_normal(posts, exclude_id=None, min_posts=6), 100)

    def test_ignores_none_views(self):
        posts = acct("a", [None, 10, None, 20, 30])["posts"]
        self.assertEqual(outliers.account_normal(posts, min_posts=3), 20)
        self.assertIsNone(outliers.account_normal(posts, min_posts=4))

    def test_too_few_posts(self):
        self.assertIsNone(outliers.account_normal(acct("a", [10, 20, 30, 40])["posts"], min_posts=5))
        self.assertEqual(outliers.account_normal(acct("a", [10, 20, 30, 40, 50])["posts"], min_posts=5), 30)

    def test_single_post(self):
        posts = [vid("x", 42)]
        self.assertEqual(outliers.account_normal(posts, min_posts=1), 42)
        self.assertIsNone(outliers.account_normal(posts, exclude_id="x", min_posts=1))

    def test_empty_and_none(self):
        self.assertIsNone(outliers.account_normal([], min_posts=0))
        self.assertIsNone(outliers.account_normal(None, min_posts=1))

    def test_zero_views_count(self):
        self.assertEqual(outliers.account_normal(acct("a", [0, 0, 0, 0, 0])["posts"], min_posts=5), 0)

    def test_duplicate_post_ids_counted_once(self):
        posts = [vid("p1", 10), vid("p1", 10), vid("p2", 1000)]
        self.assertEqual(outliers.account_normal(posts, min_posts=2), 505)
        self.assertIsNone(outliers.account_normal(posts, min_posts=3))


class AccountKey(unittest.TestCase):
    def test_prefers_author_id(self):
        self.assertEqual(outliers.account_key(vid("v", 1, author="123", username="bob")), ("tiktok", "123"))

    def test_falls_back_to_username(self):
        self.assertEqual(outliers.account_key(vid("v", 1, author=None, username="bob")), ("tiktok", "bob"))
        self.assertEqual(outliers.account_key(vid("v", 1, author="", username="bob")), ("tiktok", "bob"))

    def test_unknown_author(self):
        self.assertEqual(outliers.account_key(vid("v", 1, author=None)), ("tiktok", None))

    def test_int_id_becomes_string(self):
        self.assertEqual(outliers.account_key(vid("v", 1, author=987)), ("tiktok", "987"))


class Dedupe(unittest.TestCase):
    def test_removes_duplicates_keeps_order(self):
        out = outliers.dedupe([vid("a", 1), vid("b", 2), vid("a", 3)])
        self.assertEqual([(c["id"], c["views"]) for c in out], [("a", 1), ("b", 2)])

    def test_same_id_on_other_platform_kept(self):
        out = outliers.dedupe([vid("a", 1, platform="tiktok"), vid("a", 1, platform="youtube")])
        self.assertEqual(len(out), 2)

    def test_fills_unknown_fields_from_later_copy(self):
        out = outliers.dedupe([vid("a", None, likes=5), vid("a", 900, likes=7)])
        self.assertEqual((out[0]["views"], out[0]["likes"]), (900, 5))

    def test_items_without_id_are_kept(self):
        self.assertEqual(len(outliers.dedupe([vid(None, 1), vid(None, 1)])), 2)

    def test_input_not_changed(self):
        first = vid("a", None)
        outliers.dedupe([first, vid("a", 10)])
        self.assertIsNone(first["views"])


class Score(unittest.TestCase):
    def test_multiple_and_fields(self):
        accounts = accounts_for(acct("a1", [100, 200, 300, 400, 500]))
        [c] = outliers.score([vid("c", 3000, author="a1", likes=200, comments=100)], accounts, CFG)
        self.assertEqual(c["account_normal"], 300)
        self.assertEqual(c["account_posts_counted"], 5)
        self.assertEqual(c["multiple"], 10)
        self.assertAlmostEqual(c["engagement_rate"], 0.1)
        self.assertEqual(c["batch_rank"], 1)
        self.assertIn("meets_multiple", c["flags"])
        self.assertIn("top_fraction", c["flags"])
        self.assertNotIn("too_few_posts", c["flags"])

    def test_candidate_excluded_from_its_own_baseline(self):
        a = acct("a1", [100, 100, 100, 100, 100])
        a["posts"].insert(0, vid("c", 100_000, author="a1"))
        [c] = outliers.score([vid("c", 100_000, author="a1")], accounts_for(a), CFG)
        self.assertEqual(c["account_posts_counted"], 5)
        self.assertEqual(c["account_normal"], 100)
        self.assertEqual(c["multiple"], 1000)

    def test_missing_account(self):
        [c] = outliers.score([vid("c", 5000, author="nobody")], {}, CFG)
        self.assertIsNone(c["account_normal"])
        self.assertEqual(c["account_posts_counted"], 0)
        self.assertIsNone(c["multiple"])
        self.assertIsNone(c["batch_rank"])
        self.assertEqual(c["flags"], ["account_not_looked_up"])  # no history was given: not a thin account

    def test_accounts_none(self):
        [c] = outliers.score([vid("c", 5000)], None, CFG)
        self.assertIsNone(c["multiple"])

    def test_too_few_posts(self):
        accounts = accounts_for(acct("a1", [100, None, 100, None, 100]))
        [c] = outliers.score([vid("c", 5000, author="a1")], accounts, CFG)
        self.assertEqual(c["account_posts_counted"], 3)
        self.assertIsNone(c["account_normal"])
        self.assertIsNone(c["multiple"])
        self.assertIn("too_few_posts", c["flags"])

    def test_none_views(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        [c] = outliers.score([vid("c", None, author="a1", likes=5, comments=5)], accounts, CFG)
        self.assertIsNone(c["multiple"])
        self.assertIsNone(c["engagement_rate"])
        self.assertEqual(c["flags"], ["no_views"])

    def test_zero_views_candidate(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        [c] = outliers.score([vid("c", 0, author="a1", likes=0, comments=0)], accounts, CFG)
        self.assertEqual(c["multiple"], 0)
        self.assertIsNone(c["engagement_rate"])
        self.assertEqual(c["batch_rank"], 1)
        self.assertNotIn("no_views", c["flags"])
        self.assertNotIn("meets_multiple", c["flags"])

    def test_zero_normal_gives_unknown_multiple(self):
        accounts = accounts_for(acct("a1", [0] * 5))
        [c] = outliers.score([vid("c", 1000, author="a1")], accounts, CFG)
        self.assertEqual(c["account_normal"], 0)
        self.assertIsNone(c["multiple"])
        self.assertIsNone(c["batch_rank"])

    def test_engagement_needs_likes_and_counts_unknown_comments_as_zero(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        out = by_id(outliers.score([vid("l", 1000, author="a1", likes=10),
                                    vid("m", 1000, author="a1", comments=10)], accounts, CFG))
        self.assertAlmostEqual(out["l"]["engagement_rate"], 0.01)  # yt-dlp leaves comments empty on Shorts with none
        self.assertIsNone(out["m"]["engagement_rate"])
        self.assertTrue(out["l"]["boost_checked"])
        self.assertFalse(out["m"]["boost_checked"])

    def test_meets_multiple_boundary(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        out = by_id(outliers.score([vid("eq", 500, author="a1"), vid("under", 499, author="a1")], accounts, CFG))
        self.assertIn("meets_multiple", out["eq"]["flags"])
        self.assertNotIn("meets_multiple", out["under"]["flags"])

    def test_suspected_boost(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid(f"n{i}", 1000, author="a1", likes=90, comments=10) for i in range(4)]
        cands.append(vid("boosted", 100_000, author="a1", likes=50, comments=10))
        out = by_id(outliers.score(cands, accounts, CFG))
        self.assertIn("suspected_boost", out["boosted"]["flags"])
        self.assertNotIn("suspected_boost", out["n0"]["flags"])

    def test_boost_needs_known_engagement(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("n", 1000, author="a1", likes=90, comments=10), vid("u", 100_000, author="a1")]
        out = by_id(outliers.score(cands, accounts, CFG))
        self.assertNotIn("suspected_boost", out["u"]["flags"])

    def test_boost_ratio_zero_or_none_never_flags(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("n", 1000, author="a1", likes=90, comments=10),
                 vid("n2", 1000, author="a1", likes=90, comments=10),
                 vid("b", 100_000, author="a1", likes=1, comments=0)]
        for ratio in (0, None):
            out = by_id(outliers.score(cands, accounts, {**CFG, "boost_ratio": ratio}))
            self.assertNotIn("suspected_boost", out["b"]["flags"])

    def test_ties_share_rank(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("x", 1000, author="a1"), vid("y", 2000, author="a1"), vid("z", 2000, author="a1"),
                 vid("w", 500, author="a1")]
        out = outliers.score(cands, accounts, CFG)
        self.assertEqual([(c["id"], c["batch_rank"]) for c in out], [("y", 1), ("z", 1), ("x", 3), ("w", 4)])

    def test_tie_at_top_fraction_boundary_includes_both(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("t1", 9000, author="a1"), vid("t2", 9000, author="a1")]
        cands += [vid(f"o{i}", 100 + i, author="a1") for i in range(8)]
        out = by_id(outliers.score(cands, accounts, CFG))
        self.assertIn("top_fraction", out["t1"]["flags"])
        self.assertIn("top_fraction", out["t2"]["flags"])
        self.assertNotIn("top_fraction", out["o7"]["flags"])

    def test_top_fraction_count_has_no_float_error(self):
        # 0.1 * 30 is 3.0000000000000004 in floating point; the cut must still be 3
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid(f"v{i}", 100 * (i + 1), author="a1") for i in range(30)]
        out = outliers.score(cands, accounts, CFG)
        self.assertEqual(sum("top_fraction" in c["flags"] for c in out), 3)

    def test_top_fraction_rounds_up(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid(f"v{i}", 100 * (i + 1), author="a1") for i in range(3)]
        out = outliers.score(cands, accounts, CFG)
        self.assertEqual([c["id"] for c in out if "top_fraction" in c["flags"]], ["v2"])

    def test_top_fraction_counts_only_known_multiples(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid(f"v{i}", 100 * (i + 1), author="a1") for i in range(10)]
        cands += [vid(f"u{i}", 1_000_000, author="nobody") for i in range(10)]
        out = outliers.score(cands, accounts, {**CFG, "top_fraction": 0.2})
        self.assertEqual([c["id"] for c in out if "top_fraction" in c["flags"]], ["v9", "v8"])

    def test_sorted_with_unknown_last(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("none", None, author="a1"), vid("low", 200, author="a1"), vid("noacct", 9, author="zz"),
                 vid("high", 900, author="a1")]
        out = outliers.score(cands, accounts, CFG)
        self.assertEqual([c["id"] for c in out], ["high", "low", "none", "noacct"])

    def test_dedupes_candidates(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        out = outliers.score([vid("c", 1000, author="a1"), vid("c", 1000, author="a1")], accounts, CFG)
        self.assertEqual(len(out), 1)

    def test_input_not_changed(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cand = vid("c", 1000, author="a1")
        outliers.score([cand], accounts, CFG)
        self.assertNotIn("multiple", cand)

    def test_lookup_by_username_when_map_keyed_by_username(self):
        a = acct("bob", [100] * 5)
        [c] = outliers.score([vid("c", 1000, author="555", username="bob")], accounts_for(a), CFG)
        self.assertEqual(c["multiple"], 10)

    def test_fills_views_from_account_history(self):
        a = acct("a1", [100] * 5)
        a["posts"].append(vid("c", 2000, author="a1"))
        [c] = outliers.score([vid("c", None, author="a1")], accounts_for(a), CFG)
        self.assertEqual(c["views"], 2000)
        self.assertEqual(c["multiple"], 20)
        self.assertNotIn("no_views", c["flags"])

    def test_accepts_settings_effective_shape(self):
        cfg = {k: {"value": v, "note": "x"} for k, v in CFG.items()}
        accounts = accounts_for(acct("a1", [100] * 5))
        [c] = outliers.score([vid("c", 1000, author="a1")], accounts, cfg)
        self.assertIn("meets_multiple", c["flags"])

    def test_platforms_kept_apart(self):
        accounts = accounts_for(acct("a1", [100] * 5, platform="youtube"))
        [c] = outliers.score([vid("c", 1000, author="a1", platform="tiktok")], accounts, CFG)
        self.assertIsNone(c["multiple"])

    def test_empty_batch(self):
        self.assertEqual(outliers.score([], {}, CFG), [])

    def test_flag_order_is_stable(self):
        accounts = accounts_for(acct("a1", [100] * 5))
        cands = [vid("n", 1000, author="a1", likes=90, comments=10),
                 vid("n2", 1000, author="a1", likes=90, comments=10),
                 vid("b", 100_000, author="a1", likes=1, comments=0)]
        out = by_id(outliers.score(cands, accounts, CFG))
        self.assertEqual(out["b"]["flags"], ["suspected_boost", "meets_multiple", "top_fraction"])

    def test_paid_videos_are_flagged_and_paid_posts_left_out_of_the_normal(self):
        a = acct("a1", [100] * 5)
        for i in range(3):
            ad = vid(f"ad{i}", 50_000, author="a1")
            ad["is_paid"] = True
            a["posts"].append(ad)
        cand = vid("c", 50_000, author="a1")
        cand["is_paid"] = True
        out = by_id(outliers.score([cand, vid("o", 1000, author="a1")], accounts_for(a), CFG))
        self.assertEqual((out["c"]["account_normal"], out["c"]["account_posts_counted"], out["c"]["account_paid_posts"]),
                         (100, 5, 3))
        self.assertIn("paid", out["c"]["flags"])
        self.assertIn("meets_multiple", out["c"]["flags"])  # still scored; step 6 drops it
        self.assertNotIn("paid", out["o"]["flags"])
        mostly_ads = make_account(platform="tiktok", id="a2", posts=[dict(vid(f"x{i}", 9000, author="a2"), is_paid=True)
                                                                     for i in range(8)] + [vid("y", 10, author="a2")])
        [c] = outliers.score([vid("z", 9000, author="a2")], accounts_for(mostly_ads), CFG)
        self.assertEqual((c["account_posts_counted"], c["account_paid_posts"], c["multiple"]), (1, 8, None))
        self.assertIn("too_few_posts", c["flags"])

    def test_low_views_floor(self):
        accounts = accounts_for(acct("a1", [3] * 5))
        out = by_id(outliers.score([vid("tiny", 37, author="a1"), vid("big", 5000, author="a1")], accounts,
                                   {**CFG, "outlier_views_min": 5000}))
        self.assertEqual(out["tiny"]["flags"][:1], ["low_views"])
        self.assertIn("meets_multiple", out["tiny"]["flags"])
        self.assertNotIn("low_views", out["big"]["flags"])
        out = by_id(outliers.score([vid("tiny", 37, author="a1")], accounts, CFG))  # no floor set: no flag
        self.assertNotIn("low_views", out["tiny"]["flags"])

    RISEN = [1800, 2100, 1500, 2400, 1700, 1900] + [22, 15, 31, 12, 27, 19, 14, 35, 11, 24, 17, 29, 16]

    def test_rising_account_against_the_posts_just_before(self):
        # newest first: the six posts made just before the video already got about 1,850 views; older ones about 20
        a = acct("a1", self.RISEN)
        a["posts"].insert(0, vid("c", 2450, author="a1"))
        cfg = {**CFG, "recent_normal_posts": 6}
        [c] = outliers.score([vid("c", 2450, author="a1")], accounts_for(a), cfg)
        self.assertEqual((c["account_normal"], c["recent_normal"]), (27, 1850))
        self.assertAlmostEqual(c["multiple"], 2450 / 27)
        self.assertAlmostEqual(c["recent_multiple"], 2450 / 1850)
        self.assertEqual(c["flags"], ["rising_account", "meets_multiple", "top_fraction"])
        steady = acct("a2", [100] * 20)
        [c] = outliers.score([vid("d", 2000, author="a2")], accounts_for(steady), cfg)
        self.assertNotIn("rising_account", c["flags"])
        [c] = outliers.score([vid("c", 2450, author="a1")], accounts_for(a), CFG)  # setting off: no recent normal
        self.assertIsNone(c["recent_normal"])

    def test_the_breakout_that_lifted_the_account_is_not_rising(self):
        # newest first: six posts that rode the breakout, the breakout, then the account's old level of about 300
        views = [95_000, 120_000, 140_000, 88_000, 132_000, 101_000, 480_000,
                 310, 280, 295, 330, 270, 305, 290, 315, 285, 300, 325, 275, 320]
        a = acct("a1", views)
        ids = [p["id"] for p in a["posts"]]
        cfg = {**CFG, "recent_normal_posts": 6}
        out = by_id(outliers.score([vid(i, v, author="a1") for i, v in zip(ids[:7], views[:7])], accounts_for(a), cfg))
        brk = out[ids[6]]
        self.assertEqual((brk["account_normal"], brk["recent_normal"]), (315, 300))
        self.assertNotIn("rising_account", brk["flags"])
        self.assertIn("meets_multiple", brk["flags"])
        self.assertIn("rising_account", out[ids[0]]["flags"])  # by then the six posts before it were all big

    def test_recent_normal_by_date_when_the_video_is_not_in_the_history(self):
        posts = [vid(f"n{i}", 50_000, author="a1", posted_at=f"2026-10-{20 - i}T00:00:00Z") for i in range(6)]
        posts += [vid(f"o{i}", 400, author="a1", posted_at=f"2026-08-{20 - i}T00:00:00Z") for i in range(8)]
        a = make_account(platform="tiktok", id="a1", posts=posts)
        cfg = {**CFG, "recent_normal_posts": 6}
        [c] = outliers.score([vid("old", 60_000, author="a1", posted_at="2026-09-01T00:00:00Z")], accounts_for(a), cfg)
        self.assertEqual((c["account_normal"], c["recent_normal"]), (400, 400))
        self.assertNotIn("rising_account", c["flags"])
        undated = make_account(platform="tiktok", id="a1", posts=[dict(p, posted_at=None) for p in posts])
        [c] = outliers.score([vid("old", 60_000, author="a1")], accounts_for(undated), cfg)
        self.assertIsNone(c["recent_normal"])  # not in the newest posts: older than all of them, none known before it
        self.assertNotIn("rising_account", c["flags"])

    def test_recent_normal_needs_that_many_posts_before_the_video(self):
        a = acct("a1", [100] * 20)  # newest first: a1-p13 has six posts after it in the list, a1-p14 five
        cfg = {**CFG, "recent_normal_posts": 6}
        [c] = outliers.score([vid("a1-p13", 2000, author="a1")], accounts_for(a), cfg)
        self.assertEqual(c["recent_normal"], 100)
        [c] = outliers.score([vid("a1-p14", 2000, author="a1")], accounts_for(a), cfg)
        self.assertIsNone(c["recent_normal"])  # only 5 posts came before it

    def test_no_history_is_not_too_few_posts(self):
        out = by_id(outliers.score([vid("u", 400_000, author="skipped"), vid("f", 400_000, author="failed"),
                                    vid("t", 10, author="thin")], accounts_for(acct("thin", [5, 5])), CFG,
                                   lookup_failed={("tiktok", "failed")}))
        self.assertEqual(out["u"]["flags"], ["account_not_looked_up"])
        self.assertEqual(out["f"]["flags"], ["account_lookup_failed"])
        self.assertEqual(out["t"]["flags"], ["too_few_posts"])

    def test_paid_mark_comes_from_the_account_history_copy(self):
        a = acct("m1", [1000] * 8)
        ad = vid("ad1", 900_000, author="m1")
        ad["is_paid"] = True
        a["posts"].insert(0, ad)
        [c] = outliers.score([vid("ad1", None, author="m1")], accounts_for(a), CFG)
        self.assertIs(c["is_paid"], True)
        self.assertIn("paid", c["flags"])
        self.assertEqual(outliers.not_counted(c), ["paid"])
        mine = dict(vid("ad1", 900_000, author="m1"), is_paid=False)
        a["posts"][0]["is_paid"] = None
        [c] = outliers.score([mine], accounts_for(a), CFG)
        self.assertIs(c["is_paid"], False)  # None in the history changes nothing

    def test_boost_median_is_per_platform(self):
        yt = acct("y1", [100] * 5, platform="youtube")
        ig = acct("i1", [100] * 5, platform="instagram")
        cands = [vid(f"y{i}", 1000, author="y1", platform="youtube", likes=5) for i in range(4)]
        cands += [vid(f"i{i}", 1000, author="i1", platform="instagram", likes=200, comments=20) for i in range(4)]
        out = by_id(outliers.score(cands, accounts_for(yt, ig), CFG))
        # 0.005 is far under Instagram's 0.22 median but is YouTube's own median: not a boost
        self.assertNotIn("suspected_boost", out["y0"]["flags"])
        self.assertTrue(out["y0"]["boost_checked"])


def scored(id, multiple, author="a1", posted_at=None, platform="tiktok"):
    v = vid(id, 1000, author=author, platform=platform, posted_at=posted_at)
    v["multiple"] = multiple
    return v


class FormatsCheck(unittest.TestCase):
    def test_counts_distinct_accounts(self):
        group = [scored("v1", 10, author="a"), scored("v2", 20, author="a"), scored("v3", 30, author="b")]
        r = outliers.formats_check({"fmt": group}, min_accounts=2)["fmt"]
        self.assertEqual((r["videos"], r["accounts"], r["meets_min_accounts"]), (3, 2, True))
        r = outliers.formats_check({"fmt": group}, min_accounts=3)["fmt"]
        self.assertFalse(r["meets_min_accounts"])

    def test_same_author_id_on_two_platforms_is_two_accounts(self):
        group = [scored("v1", 10, author="a", platform="tiktok"), scored("v2", 10, author="a", platform="youtube")]
        self.assertEqual(outliers.formats_check({"f": group}, 2)["f"]["accounts"], 2)

    def test_unknown_author_not_counted(self):
        group = [scored("v1", 10, author=None), scored("v2", 10, author="a")]
        r = outliers.formats_check({"f": group}, 1)["f"]
        self.assertEqual((r["accounts"], r["videos_without_author"]), (1, 1))

    def test_median_multiple_skips_unknown(self):
        group = [scored("v1", 2), scored("v2", None), scored("v3", 10), scored("v4", 6)]
        r = outliers.formats_check({"f": group}, 1)["f"]
        self.assertEqual((r["median_multiple"], r["videos_with_multiple"]), (6, 3))

    def test_dedupes_within_group(self):
        group = [scored("v1", 2), scored("v1", 2), scored("v2", 4)]
        r = outliers.formats_check({"f": group}, 1)["f"]
        self.assertEqual((r["videos"], r["median_multiple"]), (2, 3))

    def test_empty_group(self):
        r = outliers.formats_check({"f": []}, 5)["f"]
        self.assertEqual((r["videos"], r["accounts"], r["meets_min_accounts"], r["median_multiple"]), (0, 0, False, None))
        self.assertEqual(r["fading"]["dated_videos"], 0)

    def test_fading_older_vs_newer(self):
        group = [scored("d", 2, posted_at="2026-10-04T00:00:00Z"), scored("a", 20, posted_at="2026-10-01T00:00:00Z"),
                 scored("c", 4, posted_at="2026-10-03T00:00:00Z"), scored("b", 10, posted_at="2026-10-02T00:00:00Z")]
        f = outliers.formats_check({"f": group}, 1)["f"]["fading"]
        self.assertEqual(f["dated_videos"], 4)
        self.assertEqual(f["older_median_multiple"], 15)
        self.assertEqual(f["newer_median_multiple"], 3)
        self.assertEqual(f["older_span"], ["2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"])
        self.assertEqual(f["newer_span"], ["2026-10-03T00:00:00Z", "2026-10-04T00:00:00Z"])

    def test_fading_needs_four_dated_with_multiple(self):
        group = [scored("a", 1, posted_at="2026-10-01T00:00:00Z"), scored("b", 2, posted_at="2026-10-02T00:00:00Z"),
                 scored("c", 3, posted_at="2026-10-03T00:00:00Z"), scored("d", None, posted_at="2026-10-04T00:00:00Z"),
                 scored("e", 5, posted_at=None), scored("f", 6, posted_at="not a date")]
        f = outliers.formats_check({"f": group}, 1)["f"]["fading"]
        self.assertEqual(f["dated_videos"], 3)
        self.assertIsNone(f["older_median_multiple"])
        self.assertIsNone(f["newer_median_multiple"])

    def test_fading_odd_count_drops_middle(self):
        days = [1, 2, 3, 4, 5]
        group = [scored(f"v{d}", m, posted_at=f"2026-10-0{d}T00:00:00Z") for d, m in zip(days, [1, 2, 100, 4, 5])]
        f = outliers.formats_check({"f": group}, 1)["f"]["fading"]
        self.assertEqual((f["older_median_multiple"], f["newer_median_multiple"]), (1.5, 4.5))

    def test_fading_mixed_iso_formats(self):
        group = [scored("a", 1, posted_at="2026-10-01T00:00:00+00:00"), scored("b", 2, posted_at="2026-10-02"),
                 scored("c", 3, posted_at="2026-10-03T10:00:00.123Z"), scored("d", 4, posted_at="2026-10-04T00:00:00Z")]
        f = outliers.formats_check({"f": group}, 1)["f"]["fading"]
        self.assertEqual((f["older_median_multiple"], f["newer_median_multiple"]), (1.5, 3.5))

    def test_several_groups(self):
        r = outliers.formats_check({"x": [scored("1", 1)], "y": [scored("2", 2, author="b")]}, 1)
        self.assertEqual(sorted(r), ["x", "y"])

    def test_only_real_outliers_count_toward_accounts(self):
        cfg = {"outlier_multiple_min": 5, "outlier_views_min": 5000}
        paid = dict(scored("p", 151.2, author="brand"), views=1_250_000, is_paid=True)
        low = dict(scored("l", 18.5, author="tiny"), views=37)
        weak = dict(scored("w", 1.1, author="weak"), views=90_000)
        unscored = dict(scored("u", None, author="u"), views=90_000)
        rising = dict(scored("r", 41.0, author="riser"), views=20_000, flags=["rising_account", "meets_multiple"])
        good = [dict(scored(f"g{i}", 20, author=f"g{i}"), views=50_000) for i in range(2)]
        r = outliers.formats_check({"f": [paid, low, weak, unscored, rising] + good}, 2, cfg)["f"]
        self.assertEqual((r["videos"], r["videos_counted"], r["accounts"], r["accounts_listed"]), (7, 2, 2, 7))
        self.assertTrue(r["meets_min_accounts"])
        reasons = {x["id"]: x["reasons"] for x in r["not_counted"]}
        self.assertEqual(reasons, {"p": ["paid"], "l": ["low_views"], "w": ["below_outlier_multiple_min"],
                                   "u": ["no_multiple"], "r": ["rising_account"]})
        self.assertEqual((r["median_multiple"], r["median_views"]), (20, 50_000))
        self.assertTrue(any("5 of 7" in w for w in r["warnings"]))

    def test_median_per_account(self):
        group = [scored("a1", 100, author="a"), scored("a2", 100, author="a"), scored("a3", 100, author="a"),
                 scored("b1", 10, author="b"), scored("c1", 20, author="c")]
        r = outliers.formats_check({"f": group}, 1)["f"]
        self.assertEqual((r["median_multiple"], r["median_multiple_per_account"]), (100, 20))

    def test_shared_caption_words_join_reposts_of_one_show(self):
        caps = {"FakeShowTV": "Fake Clip One | Made Up Show Title For Tests",
                "DinnerClips": "Fake Clip Two | Made Up Show Title For Tests | With Fakehost",
                "ShowReposts": "Fake Clip Three | Made Up Show Title For Tests | With Fakehost",
                "other": "Would YOU Try It? Fake Date Game #made #up #show #title #for #tests"}
        group = [dict(scored(n, 10, author=n, platform="youtube"), caption=c) for n, c in caps.items()]
        r = outliers.formats_check({"f": group}, 4)["f"]
        self.assertEqual((r["accounts"], r["accounts_after_shared_captions"]), (4, 2))
        show, longer = r["shared_captions"]
        self.assertEqual((show["accounts"], show["phrases"], show["accounts_if_one_producer"]),
                         (["DinnerClips", "FakeShowTV", "ShowReposts"], ["made up show title for tests"], 2))
        self.assertEqual((longer["accounts"], longer["phrases"]),  # overlapping runs read as one phrase
                         (["DinnerClips", "ShowReposts"], ["up show title for tests with fakehost"]))
        self.assertTrue(any("one producer" in w for w in r["warnings"]))

    def test_each_shared_phrase_names_only_the_accounts_that_use_it(self):
        caps = {"AlphaShow": "Made up show title for tests episode 3",
                "BetaClips": "made up show title for tests best moment. Follow for more money saving tips",
                "GammaCoach": "Three bills I cut this month. Follow for more money saving tips",
                "DeltaSaves": "Grocery haul under 40 dollars follow for more money saving tips",
                "EpsilonCash": "My envelope system after one year"}
        group = [dict(scored(n, 10, author=n), caption=c) for n, c in caps.items()]
        r = outliers.formats_check({"f": group}, 5)["f"]
        self.assertEqual((r["accounts"], r["accounts_after_shared_captions"]), (5, 2))
        cta, show = r["shared_captions"]
        self.assertEqual((cta["accounts"], cta["phrases"], cta["accounts_if_one_producer"]),
                         (["BetaClips", "DeltaSaves", "GammaCoach"], ["follow for more money saving tips"], 3))
        self.assertEqual((show["accounts"], show["phrases"], show["accounts_if_one_producer"]),
                         (["AlphaShow", "BetaClips"], ["made up show title for tests"], 4))
        said = next(w for w in r["warnings"] if "made up show title" in w)
        self.assertNotIn("GammaCoach", said)
        self.assertIn("(4 accounts then)", said)

    def test_same_name_on_two_platforms_is_a_warning(self):
        group = [dict(scored("v1", 10, author="1", platform="tiktok"), author_username="Example.Saver"),
                 dict(scored("v2", 10, author="UC2", platform="youtube"), author_username="examplesaver")]
        r = outliers.formats_check({"f": group}, 2)["f"]
        self.assertEqual(r["accounts"], 2)
        self.assertTrue(any("examplesaver" in w and "probably one creator" in w for w in r["warnings"]))

    def test_fading_needs_a_span_of_days(self):
        hours = [scored(c, m, posted_at=f"2026-11-0{3 + i // 3}T0{i}:00:00Z") for i, (c, m) in
                 enumerate(zip("abcd", [8, 9, 20, 30]))]
        f = outliers.formats_check({"f": hours}, 1, {"fading_min_span_days": 14})["f"]["fading"]
        self.assertEqual((f["too_recent"], f["older_median_multiple"], f["newer_median_multiple"]), (True, None, None))
        self.assertLess(f["span_days"], 14)
        f = outliers.formats_check({"f": hours}, 1)["f"]["fading"]
        self.assertIsNone(f["too_recent"])
        self.assertIsNotNone(f["older_median_multiple"])

    def test_too_recent_is_known_with_fewer_than_four_dated_videos(self):
        hours = [scored(c, m, posted_at=f"2026-10-29T0{i}:00:00Z") for i, (c, m) in enumerate(zip("abc", [8, 9, 20]))]
        f = outliers.formats_check({"f": hours}, 1, {"fading_min_span_days": 14})["f"]["fading"]
        self.assertEqual((f["dated_videos"], f["min_dated_videos"], f["too_recent"]), (3, 4, True))
        self.assertIsNone(f["older_median_multiple"])
        months = [scored(c, m, posted_at=f"2026-{mo}-01T00:00:00Z") for c, m, mo in (("a", 8, "08"), ("b", 9, "09"),
                                                                                       ("c", 20, "10"))]
        f = outliers.formats_check({"f": months}, 1, {"fading_min_span_days": 14})["f"]["fading"]
        self.assertEqual((f["dated_videos"], f["too_recent"], f["older_median_multiple"]), (3, False, None))  # needs 4
        f = outliers.formats_check({"f": months[:1]}, 1, {"fading_min_span_days": 14})["f"]["fading"]
        self.assertEqual((f["span_days"], f["too_recent"]), (None, None))  # one date: no span to judge

    def test_same_length_on_two_platforms_under_two_names_is_a_warning(self):
        """A brand that posts one video under two different handles: lengths and dates give it away."""
        yt = dict(scored("FakeVid0001", 30, author="UCfakebrand1", platform="youtube"), author_username="getfakebrand",
                  duration_s=102, posted_at="2026-10-27T21:00:00Z")
        ig = dict(scored("3900000000000000001", 12, author="5200000001", platform="instagram"),
                  author_username="fakebrand_money", duration_s=101.5, posted_at="2026-10-30T19:00:00Z")
        other = dict(scored("FakeVid0002", 9, author="UCmadeup2", platform="youtube"), author_username="madeupsaver",
                     duration_s=45, posted_at="2026-10-28T10:00:00Z")

        def cross_posts(group):
            r = outliers.formats_check({"f": group}, 2)["f"]
            return r, [w for w in r["warnings"] if "possibly one video" in w]

        r, said = cross_posts([yt, ig, other])
        self.assertEqual(r["accounts"], 3)  # a warning only: the count is for you to judge
        self.assertEqual(len(said), 1)
        for part in ("youtube FakeVid0001 (getfakebrand, 102 s)", "instagram 3900000000000000001 (fakebrand_money, 101.5 s)",
                     "2.9 days apart"):
            self.assertIn(part, said[0])
        self.assertEqual(cross_posts([yt, dict(ig, posted_at="2026-12-01T00:00:00Z")])[1], [])  # weeks apart
        self.assertEqual(cross_posts([yt, dict(ig, platform="youtube")])[1], [])  # one platform
        self.assertEqual(cross_posts([yt, dict(ig, duration_s=104)])[1], [])
        r, said = cross_posts([yt, dict(ig, author_username="getfakebrand")])  # same name: the name warning says it
        self.assertEqual(said, [])
        self.assertTrue(any("probably one creator" in w for w in r["warnings"]))
        self.assertIn("a posting date unknown", cross_posts([yt, dict(ig, posted_at=None)])[1][0])
        self.assertEqual(cross_posts([yt, dict(ig, duration_s=None)])[1], [])


class EndToEnd(unittest.TestCase):
    def test_score_then_formats_check(self):
        accts = [acct(f"a{i}", [100, 120, 80, 100, 90, 110]) for i in range(5)]
        cands = [vid(f"c{i}", 1000 * (i + 1), author=f"a{i}", likes=50, comments=5,
                     posted_at=f"2026-10-0{i + 1}T00:00:00Z") for i in range(5)]
        out = outliers.score(cands, accounts_for(*accts), CFG)
        self.assertEqual([c["id"] for c in out], ["c4", "c3", "c2", "c1", "c0"])
        r = outliers.formats_check({"fmt": out}, min_accounts=5)["fmt"]
        self.assertTrue(r["meets_min_accounts"])
        self.assertEqual(r["median_multiple"], 30)
        self.assertEqual((r["fading"]["older_median_multiple"], r["fading"]["newer_median_multiple"]), (15, 45))


if __name__ == "__main__":
    unittest.main()
