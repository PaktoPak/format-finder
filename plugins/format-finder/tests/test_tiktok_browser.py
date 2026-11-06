"""Offline tests for TikTok's browser route: parsing a search reply, a profile page and a video page, and how the
route stops, filters and fails. A fake tab hands over fixture data; no browser starts, nothing touches the network.

Fixtures in tests/fixtures/ keep the keys and nesting of real captures made on 2026-11-05 (search reply:
/api/search/item/full/; profile and video: the page's __UNIVERSAL_DATA_FOR_REHYDRATION__) with every id, username,
caption, link and count replaced by fakes. The post-list envelope (statusCode, itemList, hasMore, cursor) was not
captured (TikTok asked for a captcha instead); its keys are the ones OpenCLI's tiktok/user.js reads, and its items
reuse the faked search items, which have the same itemStruct shape as video-page items.
Run: python -m unittest discover -s tests"""
import contextlib
import copy
import io
import json
import sys
import time
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import browser, common, tiktok  # noqa: E402
from format_finder.common import VIDEO_KEYS, NotConfigured, RouteError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY = 86400


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def reply(data, headers=None, status=200):
    body = data if isinstance(data, str) else json.dumps(data)
    return {"url": "https://www.tiktok.com/api/x/", "status": status, "body": body, "error": None,
            "headers": headers or {}}


def search_page(ids, ages_days, more, start=0):
    """The search fixture with its three items re-numbered and dated `ages_days` ago."""
    page = load("tiktok_search_reply.json")
    base = page["item_list"]
    page["item_list"] = []
    for i, (vid, age) in enumerate(zip(ids, ages_days)):
        item = copy.deepcopy(base[i % len(base)])
        item["id"] = vid
        item["createTime"] = int(time.time() - age * DAY)
        page["item_list"].append(item)
    page["has_more"], page["cursor"] = int(more), start + len(ids)
    return page


class FakeTab:
    """Stands in for browser.Tab.capture: hands the embedded data, then each reply, to the route's callbacks."""

    def __init__(self, replies=(), embedded=None, blocked=None, status=200):
        self.replies, self.embedded, self.blocked_reason, self.status = list(replies), embedded, blocked, status
        self.urls, self.kwargs = [], []

    def capture(self, url, pattern=None, on_reply=None, embedded=None, on_embedded=None, **kw):
        self.urls.append(url)
        self.kwargs.append(dict(kw, pattern=pattern))
        summary = {"replies": 0, "scrolls": 0, "embedded": None, "blocked": None, "timed_out": False,
                   "seconds": 0.1, "url": url, "status": self.status}
        if embedded:
            summary["embedded"] = self.embedded is not None
            if self.embedded is None and not pattern:  # like Tab.capture: on_embedded is not called without data
                summary["blocked"] = self.blocked_reason
                return summary
            if self.embedded is not None and (on_embedded and on_embedded(self.embedded) or not pattern):
                return summary
        for r in self.replies:
            summary["replies"] += 1
            if on_reply(r):
                break
        else:
            summary["blocked"] = self.blocked_reason
        return summary


class Quiet(unittest.TestCase):
    def setUp(self):
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)
        for patch in (mock.patch.object(tiktok, "_delay", return_value=0),
                      contextlib.redirect_stderr(io.StringIO())):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)

    def with_tab(self, tab):
        patcher = mock.patch.object(browser, "tab", return_value=tab)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tab


class SearchReply(Quiet):
    def test_items_become_video_records(self):
        items, more = tiktok._search_page(tiktok._api_reply(reply(load("tiktok_search_reply.json")), "search"))
        self.assertTrue(more)
        videos = [tiktok._from_item(i) for i in items]
        v = videos[0]
        self.assertEqual(tuple(v), VIDEO_KEYS)
        self.assertEqual(v["id"], "7000000000000000001")
        self.assertEqual(v["url"], "https://www.tiktok.com/@fake_creator_a/video/7000000000000000001")
        self.assertEqual((v["author_id"], v["author_username"], v["author_url"]),
                         ("6800000000000000001", "fake_creator_a", "https://www.tiktok.com/@fake_creator_a"))
        self.assertEqual((v["views"], v["likes"], v["comments"], v["shares"]), (7000, 100, 10, 5))
        self.assertEqual(v["author_followers"], 2000)
        self.assertEqual(v["posted_at"], "2026-09-22T14:13:20Z")
        self.assertEqual(v["duration_s"], 24)
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]),
                         ("7100000000000000001", "original sound - fake_creator_a", "fake_creator_a"))
        self.assertEqual((v["route"], v["raw_ref"]), ("browser", "7000000000000000001"))
        self.assertEqual([x["is_paid"] for x in videos], [None, None, True])

    def test_counts_fall_back_to_statsV2_strings(self):
        item = load("tiktok_search_reply.json")["item_list"][0]
        item["stats"] = {}
        self.assertEqual(tiktok._from_item(item)["views"], 7000)

    def test_exact_statsV2_wins_over_rounded_stats(self):
        item = load("tiktok_search_reply.json")["item_list"][0]
        item["stats"]["playCount"], item["statsV2"]["playCount"] = 7000, "7012"
        item["authorStats"]["followerCount"], item["authorStatsV2"]["followerCount"] = 180600, "180582"
        v = tiktok._from_item(item)
        self.assertEqual((v["views"], v["author_followers"]), (7012, 180582))
        item["statsV2"]["playCount"] = None
        self.assertEqual(tiktok._from_item(item)["views"], 7000)

    def test_captcha_header_and_empty_body(self):
        check = '{"code":"10000","type":"verify","subtype":"whirl"}'
        with self.assertRaises(browser.BotCheck) as ctx:
            tiktok._api_reply(reply("", headers={"bdturing-verify": check}), "search")
        self.assertIn("captcha (verify/whirl)", str(ctx.exception))
        with self.assertRaises(browser.BotCheck):
            tiktok._api_reply(reply(""), "search")
        with self.assertRaises(RouteError) as ctx:
            tiktok._api_reply(reply({"status_code": 2484, "status_msg": "fake refusal"}), "search")
        self.assertIn("2484", str(ctx.exception))

    def test_captcha_flag_for_route_memory(self):
        check = '{"type":"verify","subtype":"slide"}'
        with self.assertRaises(browser.BotCheck) as ctx:
            tiktok._api_reply(reply("", headers={"bdturing-verify": check}), "search")
        self.assertTrue(ctx.exception.captcha)
        self.assertIn("browser-login tiktok", str(ctx.exception))
        with self.assertRaises(browser.BotCheck) as ctx:
            tiktok._api_reply(reply(""), "search")
        self.assertFalse(ctx.exception.captcha)  # an empty reply counts as a failure, not a captcha
        for reason, captcha in (("a captcha (captcha-verify-container)", True),
                                ('a bot check ("Drag the slider to fit the puzzle")', True),
                                ("a captcha demand (verify/whirl)", True),
                                ("a login wall (the site asks to log in)", False),
                                ("the site sent the page to its login page (/login)", False)):
            exc = tiktok._page_failure("search", {"blocked": reason})
            self.assertIsInstance(exc, browser.BotCheck)
            self.assertIs(exc.captcha, captcha, reason)


class Search(Quiet):
    def test_reads_pages_until_enough_inside_the_window(self):
        page1 = search_page(["1001", "1002", "1003"], [5, 100, 10], more=True)
        page2 = search_page(["1004", "1005", "1006"], [20, 1, 400], more=True, start=3)
        page3 = search_page(["1007", "1008", "1009"], [2, 3, 4], more=False, start=6)
        tab = self.with_tab(FakeTab([reply(page1), reply(page2), reply(page3)]))
        out = tiktok._browser_search("meal prep", days=60, limit=4)
        self.assertEqual([v["id"] for v in out], ["1001", "1003", "1004", "1005"])
        self.assertEqual(tab.urls, ["https://www.tiktok.com/search/video?q=meal%20prep"])
        self.assertEqual(tab.kwargs[0]["pattern"], tiktok.SEARCH_API)

    def test_stops_when_tiktok_has_no_more(self):
        tab = self.with_tab(FakeTab([reply(search_page(["1", "2"], [1, 2], more=False)),
                                     reply(search_page(["3"], [1], more=True))]))
        self.assertEqual([v["id"] for v in tiktok._browser_search("x", days=60, limit=30)], ["1", "2"])
        self.assertEqual(len(tab.urls), 1)

    def test_later_captcha_keeps_earlier_results(self):
        check = {"bdturing-verify": '{"type":"verify","subtype":"whirl"}'}
        self.with_tab(FakeTab([reply(search_page(["1", "2"], [1, 2], more=True)), reply("", headers=check)]))
        self.assertEqual(len(tiktok._browser_search("x", days=60, limit=30)), 2)

    def test_first_page_captcha_raises_bot_check(self):
        check = {"bdturing-verify": '{"type":"verify","subtype":"whirl"}'}
        self.with_tab(FakeTab([reply("", headers=check)]))
        with self.assertRaises(browser.BotCheck):
            tiktok._browser_search("x", days=60, limit=30)

    def test_wall_with_no_replies_raises_bot_check(self):
        self.with_tab(FakeTab([], blocked="a login wall (the site asks to log in)"))
        with self.assertRaises(browser.BotCheck) as ctx:
            tiktok._browser_search("x", days=60, limit=30)
        self.assertIn("browser-login tiktok", str(ctx.exception))

    def test_keyword_only_and_route_order(self):
        sent = []

        def browser_route(query, **kw):
            sent.append(query)
            return [tiktok._from_item(load("tiktok_search_reply.json")["item_list"][0])]

        def off(*a, **k):
            raise NotConfigured("off in this test")

        with mock.patch.object(tiktok, "_omkar_search", off), mock.patch.object(tiktok, "_dtk_search", off), \
                mock.patch.object(tiktok, "_browser_search", browser_route):
            env = tiktok.search("#meal #prep", days=60, limit=1)
        self.assertEqual(sent, ["meal prep"])
        self.assertEqual(env["route"], "browser")
        self.assertEqual([e["route"] for e in env["errors"]], ["omkarcloud", "dtk"])


class Profile(Quiet):
    def posts_reply(self, ids, ages, more, author_id="6800000000000000077"):
        items = search_page(ids, ages, more)["item_list"]
        for item in items:
            item["author"] = dict(item["author"], id=author_id, uniqueId="fake_profile")
        return {"statusCode": 0, "itemList": items, "hasMore": more, "cursor": "1790000000000"}

    def test_followers_from_page_posts_from_replies_newest_first(self):
        tab = self.with_tab(FakeTab([reply(self.posts_reply(["11", "12", "13"], [30, 2, 9], True)),
                                     reply(self.posts_reply(["14", "15", "16"], [40, 50, 60], False))],
                                    embedded=load("tiktok_profile_page.json")))
        acc = tiktok._browser_account("@Fake_Profile", limit=5)
        self.assertEqual(tab.urls, ["https://www.tiktok.com/@fake_profile"])
        self.assertEqual((acc["platform"], acc["id"], acc["username"], acc["url"], acc["followers"], acc["route"]),
                         ("tiktok", "6800000000000000077", "fake_profile", "https://www.tiktok.com/@fake_profile",
                          123456, "browser"))
        self.assertEqual([p["id"] for p in acc["posts"]], ["12", "13", "11", "14", "15"])
        self.assertTrue(all(p["author_followers"] == 123456 for p in acc["posts"]))

    def test_other_accounts_items_are_left_out(self):
        mixed = self.posts_reply(["21", "22"], [1, 2], False)
        mixed["itemList"][1]["author"]["id"] = "999"
        self.with_tab(FakeTab([reply(mixed)], embedded=load("tiktok_profile_page.json")))
        self.assertEqual([p["id"] for p in tiktok._browser_account("fake_profile")["posts"]], ["21"])

    def test_post_list_captcha_raises_bot_check(self):
        check = {"bdturing-verify": '{"type":"verify","subtype":"whirl"}'}
        self.with_tab(FakeTab([reply("", headers=check)], embedded=load("tiktok_profile_page.json")))
        with self.assertRaises(browser.BotCheck) as ctx:
            tiktok._browser_account("fake_profile")
        self.assertIn("post list", str(ctx.exception))

    def test_missing_or_private_profile(self):
        page = load("tiktok_profile_page.json")
        page["__DEFAULT_SCOPE__"]["webapp.user-detail"].update(statusCode=10221, statusMsg="fake status")
        self.with_tab(FakeTab([], embedded=page))
        with self.assertRaises(common.NotFound) as ctx:  # no such user: an answer, not a failing route
            tiktok._browser_account("fake_profile")
        self.assertIn("10221", str(ctx.exception))

        page = load("tiktok_profile_page.json")
        page["__DEFAULT_SCOPE__"]["webapp.user-detail"] = {"statusCode": 10222, "statusMsg": "FakeSecretStatus"}
        tab = self.with_tab(FakeTab([], embedded=page))
        acc = tiktok._browser_account("fake_hidden_profile")
        self.assertEqual((acc["username"], acc["is_private"], acc["posts"], acc["followers"]),
                         ("fake_hidden_profile", True, [], None))

        page = load("tiktok_profile_page.json")
        page["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["user"]["privateAccount"] = True
        tab = self.with_tab(FakeTab([reply(self.posts_reply(["1"], [1], False))], embedded=page))
        acc = tiktok._browser_account("fake_profile")
        self.assertEqual((acc["followers"], acc["posts"]), (123456, []))

    def test_page_without_profile_data(self):
        self.with_tab(FakeTab([], embedded=None, blocked="a captcha (captcha-verify-container)"))
        with self.assertRaises(browser.BotCheck):
            tiktok._browser_account("fake_profile")


class VideoPage(Quiet):
    def test_video_detail_becomes_a_record(self):
        tab = self.with_tab(FakeTab(embedded=load("tiktok_video_page.json")))
        v = tiktok._browser_video("https://www.tiktok.com/@fake_creator_v/video/7200000000000000001")
        self.assertEqual(tab.urls, ["https://www.tiktok.com/@fake_creator_v/video/7200000000000000001"])
        self.assertEqual(tab.kwargs[0]["pattern"], None)
        self.assertEqual(tuple(v), VIDEO_KEYS)
        self.assertEqual((v["id"], v["author_username"], v["author_id"], v["author_followers"]),
                         ("7200000000000000001", "fake_creator_v", "6800000000000000099", 4321))
        self.assertEqual((v["views"], v["likes"], v["comments"], v["shares"]), (1003, 1000, 1002, 1001))
        self.assertEqual((v["posted_at"], v["duration_s"], v["sound_id"]),
                         ("2026-09-21T15:13:20Z", 31, "7300000000000000001"))
        self.assertEqual((v["is_paid"], v["route"]), (None, "browser"))

    def test_links_are_opened_as_given(self):
        tab = self.with_tab(FakeTab(embedded=load("tiktok_video_page.json")))
        tiktok._browser_video("https://www.tiktok.com/@fake_creator_v/video/7200000000000000001")
        self.assertEqual(tab.urls, ["https://www.tiktok.com/@fake_creator_v/video/7200000000000000001"])

    def test_missing_video_and_bot_check(self):
        page = load("tiktok_video_page.json")
        page["__DEFAULT_SCOPE__"]["webapp.video-detail"].update(statusCode=10204, statusMsg="")
        self.with_tab(FakeTab(embedded=page))
        with self.assertRaises(RouteError) as ctx:
            tiktok._browser_video("https://www.tiktok.com/@fake_creator_v/video/7200000000000000001")
        self.assertIn("10204", str(ctx.exception))
        self.with_tab(FakeTab(embedded=None, blocked="a bot check (\"Verify to continue\")"))
        with self.assertRaises(browser.BotCheck):
            tiktok._browser_video("https://www.tiktok.com/@fake_creator_v/video/7200000000000000001")
        with self.assertRaises(RouteError):
            tiktok._browser_video("not a video")

    def test_bare_id_is_not_opened(self):
        # https://www.tiktok.com/@/video/<id> showed TikTok's captcha live, so a bare id is refused without a request.
        tab = self.with_tab(FakeTab(embedded=load("tiktok_video_page.json")))
        with self.assertRaises(RouteError):
            tiktok._browser_video("7200000000000000001")
        self.assertEqual(tab.urls, [])


if __name__ == "__main__":
    unittest.main()
