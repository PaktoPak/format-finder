"""Offline tests for Instagram's browser route: parsing a keyword-search reply, a profile's Reels-tab replies and a
reel page's embedded JSON, and how the route stops, filters and fails. A fake tab hands over fixture data; no
browser starts and nothing touches the network.

Fixtures in tests/fixtures/instagram_*.json keep the keys and nesting of what instagram.com loaded on 2026-11-05
(logged in, one Mac): search: /api/graphql data.xdt_fbsearch__top_serp_graphql; Reels tab:
data.fetch__XDTUserDict.clips_connection and the data.user profile reply; reel page: its
<script type="application/json"> documents with data.xig_fetch_media. Every id, username, name, code, caption, link
and count in them is made up. The recordings showed no ad; the ad test marks an item the way the app API marks ads
(product_type "ad"), which is the rule the route applies.
Run: python -m unittest discover -s tests"""
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import browser, common, doctor, instagram  # noqa: E402
from format_finder import settings as ff_settings  # noqa: E402
from format_finder.common import ACCOUNT_KEYS, VIDEO_KEYS, AllRoutesFailed, NotConfigured, RouteError  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DAY = 86400
LOGIN_PAGE = "the site sent the page to its login page (/accounts/login/)"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class BrowserStarted(BaseException):
    """Raised past every route's error handling, so a test that reaches a real browser fails loudly."""


def setUpModule():
    """No unit test may start a real browser (it would send real requests to Instagram), and the user's own
    settings file must not change what the tests see."""
    guard = mock.patch.object(browser.Browser, "start", side_effect=BrowserStarted("a unit test started a browser"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)
    own = mock.patch.object(ff_settings, "USER_PATH", Path(tempfile.mkdtemp()) / "settings.json")
    own.start()
    unittest.addModuleCleanup(own.stop)


def reply(data, status=200, path="/api/graphql"):
    body = data if isinstance(data, str) else json.dumps(data)
    return {"url": f"https://www.instagram.com{path}", "status": status, "body": body, "error": None, "headers": {}}


OTHER_REPLY = reply({"data": {"get_slide_mailbox_for_iris_subscription": {"threads": []}}})


def search_page(ages_days, more, start=0, media_types=None):
    """The search fixture's first video, copied once per age (taken `ages_days` ago), with fresh ids."""
    page = load("instagram_search_reply.json")
    serp = page["data"]["xdt_fbsearch__top_serp_graphql"]
    base = serp["edges"][0]["node"]["items"][0]
    items = []
    for i, age in enumerate(ages_days):
        item = copy.deepcopy(base)
        n = start + i + 1
        item.update(pk=str(3100000000000001000 + n), id=f"{3100000000000001000 + n}_5100000001",
                    code=f"FakeCode{n:03d}", taken_at=int(time.time() - age * DAY))
        if media_types:
            item["media_type"] = media_types[i]
        items.append(item)
    serp["edges"] = [{"node": {"__typename": "XDTTopSerpMediaGridUnit", "items": items}, "cursor": ""}]
    serp["page_info"] = {"has_next_page": more, "end_cursor": "FAKE_CURSOR" if more else None}
    return page


def reels_page(codes, more, owner="5200000002", pinned=()):
    """A Reels-tab reply with one reel per code ("R1", "R2", ...)."""
    page = load("instagram_profile_reels_reply.json")
    holder = page["data"]["fetch__XDTUserDict"]
    base = holder["clips_connection"]["edges"][1]["node"]["media"]
    edges = []
    for i, code in enumerate(codes):
        media = copy.deepcopy(base)
        media.update(code=code, pk=str(3200000000000001000 + int(code.lstrip("R"))),
                     clips_tab_pinned_user_ids=[owner] if code in pinned else [])
        media["id"] = f"{media['pk']}_{owner}"
        media["user"] = dict(media["user"], pk=owner, id=owner)
        edges.append({"node": {"media": media, "__typename": "XDTClipsItemDict"}, "cursor": None})
    holder["clips_connection"] = {"edges": edges, "page_info": {"end_cursor": "C" if more else None,
                                                                "has_next_page": more}}
    holder["id"] = owner
    return page


class FakeTab:
    """Stands in for browser.Tab.capture: hands the embedded data, then each reply, to the route's callbacks."""

    def __init__(self, replies=(), embedded=None, blocked=None, status=200, ended_at=None):
        self.replies, self.embedded, self.blocked_reason, self.status = list(replies), embedded, blocked, status
        self.ended_at = ended_at
        self.urls, self.kwargs, self.handed = [], [], 0

    def capture(self, url, pattern=None, on_reply=None, embedded=None, on_embedded=None, **kw):
        self.urls.append(url)
        self.kwargs.append(dict(kw, pattern=pattern, embedded=embedded))
        summary = {"replies": 0, "scrolls": 0, "embedded": None, "blocked": None, "timed_out": False,
                   "seconds": 0.1, "url": self.ended_at or url, "status": self.status}
        if embedded:
            summary["embedded"] = self.embedded is not None
            if self.embedded is None and not pattern:  # like Tab.capture: on_embedded is not called without data
                summary["blocked"] = self.blocked_reason
                return summary
            if self.embedded is not None and (on_embedded and on_embedded(self.embedded) or not pattern):
                return summary
        for r in self.replies:
            summary["replies"] += 1
            self.handed += 1
            if on_reply(r):
                break
        else:
            summary["blocked"] = self.blocked_reason
        return summary


class Quiet(unittest.TestCase):
    def setUp(self):
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)
        self.home = Path(tempfile.mkdtemp())
        for patch in (mock.patch.object(instagram, "_delay", return_value=0),
                      mock.patch.object(common, "FF_HOME", self.home),  # no saved session: routes 1-2 are off
                      contextlib.redirect_stderr(io.StringIO())):
            patch.__enter__()
            self.addCleanup(patch.__exit__, None, None, None)
        instagram._blocked[0] = None

    def with_tab(self, tab):
        patcher = mock.patch.object(browser, "tab", return_value=tab)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tab


class Search(Quiet):
    def test_reply_items_become_video_records(self):
        tab = self.with_tab(FakeTab([OTHER_REPLY, reply(load("instagram_search_reply.json"))]))
        out = instagram._browser_search("meal prep", days=None, limit=30)
        self.assertEqual(tab.urls, ["https://www.instagram.com/explore/search/keyword/?q=meal%20prep"])
        self.assertEqual(tab.kwargs[0]["pattern"], instagram.GRAPHQL)
        self.assertEqual([v["raw_ref"] for v in out], ["FakeCodeA01", "FakeCodeB02", "FakeCodeD04"])  # photo left out
        v = out[0]
        self.assertEqual(tuple(v), VIDEO_KEYS)
        self.assertEqual((v["platform"], v["id"], v["url"]),
                         ("instagram", "3100000000000000001", "https://www.instagram.com/reel/FakeCodeA01/"))
        self.assertEqual((v["author_id"], v["author_username"], v["author_url"], v["author_followers"]),
                         ("5100000001", "fake_creator_a", "https://www.instagram.com/fake_creator_a/", None))
        self.assertIsNone(v["views"])  # instagram.com's search gives no views
        self.assertEqual((v["likes"], v["comments"], v["shares"]), (5300, 31, None))
        self.assertEqual((v["posted_at"], v["duration_s"], v["caption"]),
                         ("2026-09-21T14:13:20Z", 30.4, "fake caption 1"))
        self.assertEqual((v["route"], v["is_paid"]), ("browser", None))
        hidden = out[1]
        self.assertEqual((hidden["likes"], hidden["comments"], hidden["duration_s"]), (None, 4, 61.5))

    def test_reads_more_replies_until_enough_inside_the_window(self):
        pages = [search_page([5, 100, 10], True), search_page([20, 1, 400], True, start=3),
                 search_page([2, 3, 4], False, start=6)]
        tab = self.with_tab(FakeTab([reply(p) for p in pages]))
        out = instagram._browser_search("meal prep", days=60, limit=4)
        self.assertEqual([v["raw_ref"] for v in out], ["FakeCode001", "FakeCode003", "FakeCode004", "FakeCode005"])
        self.assertEqual(tab.handed, 2)  # the third reply is not read
        self.assertEqual(tab.kwargs[0]["max_scrolls"], instagram.BROWSER_SEARCH_PAGES)

    def test_stops_when_instagram_has_no_more_and_at_the_page_cap(self):
        tab = self.with_tab(FakeTab([reply(search_page([1, 2], False)), reply(search_page([1], True, start=2))]))
        self.assertEqual(len(instagram._browser_search("x", days=60, limit=30)), 2)
        self.assertEqual(tab.handed, 1)
        many = [reply(search_page([1], True, start=i)) for i in range(instagram.BROWSER_SEARCH_PAGES + 3)]
        tab = self.with_tab(FakeTab(many))
        self.assertEqual(len(instagram._browser_search("x", days=60, limit=30)), instagram.BROWSER_SEARCH_PAGES)
        self.assertEqual(tab.handed, instagram.BROWSER_SEARCH_PAGES)

    def test_ads_photos_and_carousels_are_left_out(self):
        page = search_page([1, 1, 1, 1], False, media_types=[2, 1, 8, 2])
        items = page["data"]["xdt_fbsearch__top_serp_graphql"]["edges"][0]["node"]["items"]
        items[3]["product_type"] = "ad"
        self.with_tab(FakeTab([reply(page)]))
        self.assertEqual([v["raw_ref"] for v in instagram._browser_search("x", days=60, limit=30)], ["FakeCode001"])

    def test_empty_search_is_an_empty_list(self):
        self.with_tab(FakeTab([reply(search_page([], False))]))
        self.assertEqual(instagram._browser_search("x", days=60, limit=30), [])

    def test_login_page_raises_not_configured(self):
        self.with_tab(FakeTab([OTHER_REPLY], blocked=LOGIN_PAGE))
        with self.assertRaises(NotConfigured) as ctx:
            instagram._browser_search("x", days=60, limit=30)
        self.assertEqual(str(ctx.exception), instagram.BROWSER_NEEDS_LOGIN)
        # the launcher's full path: format-finder is on PATH only inside Claude Code, not in the user's terminal
        self.assertIn(f"{common.TERMINAL_COMMAND} browser-login instagram", str(ctx.exception))
        self.assertIn(f"{common.TERMINAL_COMMAND} instagram-login", str(ctx.exception))
        self.assertTrue(common.TERMINAL_COMMAND.strip("'").endswith("/bin/format-finder"))
        # a capture that ended on the login page without a wall check says the same
        self.with_tab(FakeTab([], ended_at="https://www.instagram.com/accounts/login/?next=%2Fexplore%2F"))
        with self.assertRaises(NotConfigured):
            instagram._browser_search("x", days=60, limit=30)

    def test_no_search_reply_is_a_route_error(self):
        self.with_tab(FakeTab([OTHER_REPLY, reply("not json")]))
        with self.assertRaises(RouteError) as ctx:
            instagram._browser_search("x", days=60, limit=30)
        self.assertNotIsInstance(ctx.exception, NotConfigured)
        error = {"data": {"xdt_fbsearch__top_serp_graphql": None}, "errors": [{"message": "fake failure"}]}
        self.with_tab(FakeTab([reply(error)]))
        with self.assertRaisesRegex(RouteError, "fake failure"):
            instagram._browser_search("x", days=60, limit=30)

    def test_browser_search_is_off_by_default(self):
        """The keyword search page sends its own tag-information request, so the browser route for search runs only
        when instagram_browser_search is true; account and video keep the browser route."""
        self.assertIs(ff_settings.get("instagram_browser_search"), False)
        tab = self.with_tab(FakeTab([reply(load("instagram_search_reply.json"))]))
        with self.assertRaises(AllRoutesFailed) as ctx:
            instagram.search("meal prep", days=60, limit=1)
        self.assertEqual(tab.urls, [])
        errors = {e["route"]: e["error"] for e in ctx.exception.errors}
        self.assertEqual(list(errors), ["instagrapi", "aiograpi", "browser"])
        self.assertEqual(errors["browser"], f"NotConfigured: {instagram.BROWSER_SEARCH_OFF}")
        self.assertIn("instagram_browser_search", errors["browser"])
        self.assertIn("fetch__XDTTagInfo", errors["browser"])
        tab = self.with_tab(FakeTab(embedded=load("instagram_reel_page.json")))
        self.assertEqual(instagram.video("FakeCodeR01")["route"], "browser")

    def test_keyword_only_route_order_and_setting(self):
        sent = []

        def browser_route(query, **kw):
            sent.append(query)
            return [instagram._web_video(load("instagram_search_reply.json")["data"]["xdt_fbsearch__top_serp_graphql"]
                                         ["edges"][0]["node"]["items"][0])]

        ff_settings.USER_PATH.write_text(json.dumps({"instagram_browser_search": True}), encoding="utf-8")
        self.addCleanup(ff_settings.USER_PATH.unlink)
        with mock.patch.object(instagram, "_browser_search", browser_route):
            env = instagram.search("#meal #prep", days=60, limit=1)
        self.assertEqual(env["route"], "browser")
        self.assertEqual([e["route"] for e in env["errors"]], ["instagrapi", "aiograpi"])
        self.assertTrue(all(e["error"].startswith("NotConfigured: Instagram needs a login") for e in env["errors"]))
        self.with_tab(FakeTab([reply(search_page([], False))]))
        instagram._browser_search("#meal  #prep", days=60, limit=1)
        self.assertEqual(browser.tab().urls, ["https://www.instagram.com/explore/search/keyword/?q=meal%20prep"])
        ff_settings.USER_PATH.write_text(json.dumps({"browser_route": False, "instagram_browser_search": True}),
                                         encoding="utf-8")
        with mock.patch.object(instagram, "_browser_search") as off, self.assertRaises(AllRoutesFailed) as ctx:
            instagram.search("meal prep", days=60, limit=1)
        off.assert_not_called()
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi", "aiograpi"])


class Account(Quiet):
    def test_reels_tab_record_followers_and_grid_order(self):
        tab = self.with_tab(FakeTab([reply(load("instagram_profile_user_reply.json")), OTHER_REPLY,
                                     reply(load("instagram_profile_reels_reply.json"))]))
        acc = instagram._browser_account("@Fake_Profile", limit=20)
        self.assertEqual(tab.urls, ["https://www.instagram.com/fake_profile/reels/"])
        self.assertEqual(tuple(acc), ACCOUNT_KEYS)
        self.assertEqual((acc["platform"], acc["id"], acc["username"], acc["url"], acc["followers"], acc["route"]),
                         ("instagram", "5200000002", "fake_profile", "https://www.instagram.com/fake_profile/",
                          123456, "browser"))
        # pinned FakeReel001 and FakeReel003 (posted by another account) are left out; grid order is kept
        self.assertEqual([p["raw_ref"] for p in acc["posts"]], ["FakeReel002", "FakeReel004", "FakeReel005"])
        p = acc["posts"][0]
        self.assertEqual(tuple(p), VIDEO_KEYS)
        self.assertEqual((p["id"], p["url"]), ("3200000000000000002", "https://www.instagram.com/reel/FakeReel002/"))
        self.assertEqual((p["views"], p["likes"], p["comments"], p["posted_at"]), (12000, 300, 12, None))
        self.assertEqual((p["author_id"], p["author_username"], p["author_followers"]),
                         ("5200000002", "fake_profile", 123456))

    def test_the_logged_in_viewer_is_never_taken_for_the_profile(self):
        viewer_only = {"data": {"user": {"pk": "40000000001", "username": "fake_viewer", "follower_count": 7},
                                "viewer": {"user": {"pk": "40000000001"}}}}
        self.with_tab(FakeTab([reply(viewer_only), reply(load("instagram_profile_reels_reply.json"))]))
        acc = instagram._browser_account("fake_profile", limit=20)
        self.assertIsNone(acc["followers"])
        self.assertEqual(acc["id"], "5200000002")  # from the Reels tab reply's owner
        self.assertEqual(len(acc["posts"]), 3)

    def test_paging_stops_at_the_limit_or_when_there_is_no_more(self):
        user = reply(load("instagram_profile_user_reply.json"))
        pages = [reels_page(["R1", "R2", "R3", "R4"], True, pinned={"R1"}), reels_page(["R5", "R6", "R7"], True),
                 reels_page(["R8", "R9", "R10"], True)]
        tab = self.with_tab(FakeTab([user] + [reply(p) for p in pages]))
        acc = instagram._browser_account("fake_profile", limit=5)
        self.assertEqual([p["raw_ref"] for p in acc["posts"]], ["R2", "R3", "R4", "R5", "R6"])
        self.assertEqual(tab.handed, 3)
        tab = self.with_tab(FakeTab([user, reply(reels_page(["R1"], False)), reply(reels_page(["R2"], True))]))
        self.assertEqual(len(instagram._browser_account("fake_profile", limit=5)["posts"]), 1)
        self.assertEqual(tab.handed, 2)
        self.assertEqual(tab.kwargs[0]["max_scrolls"], instagram.BROWSER_REEL_PAGES)

    def test_private_profile_ends_with_followers_and_no_posts(self):
        private = load("instagram_profile_user_reply.json")
        private["data"]["user"]["is_private"] = True
        tab = self.with_tab(FakeTab([reply(private), reply(load("instagram_profile_reels_reply.json"))]))
        acc = instagram._browser_account("fake_profile")
        self.assertEqual((acc["followers"], acc["posts"]), (123456, []))
        self.assertEqual(tab.handed, 1)

    def test_profile_reply_without_reels_tab_reply_is_a_route_error(self):
        user = reply(load("instagram_profile_user_reply.json"))
        failed = {"data": {"fetch__XDTUserDict": None}, "errors": [{"message": "fake please wait"}]}
        for replies, message in (([user, reply(failed)], "fake please wait"),  # the Reels-tab query failed
                                 ([reply(failed), user], "fake please wait"),
                                 ([user, OTHER_REPLY], "sent the profile but no Reels-tab reply")):  # it never came
            self.with_tab(FakeTab(replies))
            with self.assertRaisesRegex(RouteError, message) as ctx:
                instagram._browser_account("fake_profile", limit=20)
            self.assertNotIsInstance(ctx.exception, NotConfigured)
        self.with_tab(FakeTab([user, reply(failed)]))
        with self.assertRaises(AllRoutesFailed) as ctx:
            instagram.account("fake_profile", limit=20)
        self.assertIn("fake please wait", ctx.exception.errors[-1]["error"])
        # a Reels-tab reply that lists no reels and has no next page is a real empty tab
        self.with_tab(FakeTab([user, reply(reels_page([], False))]))
        acc = instagram._browser_account("fake_profile", limit=20)
        self.assertEqual((acc["followers"], acc["posts"]), (123456, []))

    def test_missing_profile_is_not_found_and_stops_at_the_first_quiet_moment(self):
        class MissingTab(FakeTab):
            """The page sends other replies, then sits quiet on Instagram's "page isn't available" notice."""
            checks = 0

            def evaluate(self, expression):
                MissingTab.checks += 1
                return expression == instagram.PROFILE_MISSING_JS

            def capture(self, url, pattern=None, on_reply=None, on_idle=None, **kw):
                for r in self.replies:
                    on_reply(r)
                self.urls.append(url)
                ended = bool(on_idle and on_idle(self))
                return {"replies": len(self.replies), "scrolls": 0 if ended else 8, "embedded": None,
                        "blocked": None, "timed_out": False, "seconds": 4.1, "url": url, "status": 200}

        self.with_tab(MissingTab([OTHER_REPLY, OTHER_REPLY]))
        with self.assertRaises(common.NotFound) as ctx:
            instagram._browser_account("nosuchprofile1")
        self.assertIn("no profile @nosuchprofile1", str(ctx.exception))
        self.assertIn("not a login problem", str(ctx.exception))
        self.assertEqual(MissingTab.checks, 1)
        with self.assertRaises(AllRoutesFailed) as ctx:  # a bad handle does not make route memory skip the route
            instagram.account("nosuchprofile1")
        self.assertTrue(ctx.exception.errors[-1]["error"].startswith("NotFound: "))
        self.assertEqual(common.route_skips(), [])
        # a real profile's quiet moment never asks the page
        MissingTab.checks = 0
        self.with_tab(MissingTab([reply(load("instagram_profile_user_reply.json")),
                                  reply(load("instagram_profile_reels_reply.json"))]))
        self.assertEqual(len(instagram._browser_account("fake_profile")["posts"]), 3)
        self.assertEqual(MissingTab.checks, 0)

    def test_account_record_has_post_count_and_private_flag(self):
        user = load("instagram_profile_user_reply.json")
        user["data"]["user"].update(media_count=57, is_private=False)
        self.with_tab(FakeTab([reply(user), reply(load("instagram_profile_reels_reply.json"))]))
        acc = instagram._browser_account("fake_profile")
        self.assertEqual((acc["post_count"], acc["is_private"]), (57, False))

    def test_login_page_and_empty_page(self):
        self.with_tab(FakeTab([OTHER_REPLY], blocked=LOGIN_PAGE))
        with self.assertRaises(NotConfigured) as ctx:
            instagram._browser_account("fake_profile")
        self.assertEqual(str(ctx.exception), instagram.BROWSER_NEEDS_LOGIN)
        self.with_tab(FakeTab([OTHER_REPLY]))
        with self.assertRaises(RouteError) as ctx:
            instagram._browser_account("fake_profile")
        self.assertNotIsInstance(ctx.exception, NotConfigured)
        tab = self.with_tab(FakeTab([]))
        with self.assertRaises(RouteError):
            instagram._browser_account("not a/username")
        self.assertEqual(tab.urls, [])


class ReelPage(Quiet):
    def test_embedded_media_becomes_a_record(self):
        tab = self.with_tab(FakeTab(embedded=load("instagram_reel_page.json")))
        v = instagram._browser_video("https://www.instagram.com/reel/FakeCodeR01/?igsh=fake")
        self.assertEqual(tab.urls, ["https://www.instagram.com/reel/FakeCodeR01/"])
        self.assertIsInstance(tab.kwargs[0]["embedded"], browser.JsonScripts)
        self.assertEqual(tab.kwargs[0]["embedded"].marker, "FakeCodeR01")
        self.assertEqual(tab.kwargs[0]["embedded"].keys, instagram.REEL_KEYS)
        self.assertIsNone(tab.kwargs[0]["pattern"])
        self.assertEqual(tuple(v), VIDEO_KEYS)
        self.assertEqual((v["id"], v["url"], v["raw_ref"]),
                         ("3300000000000000001", "https://www.instagram.com/reel/FakeCodeR01/", "FakeCodeR01"))
        self.assertEqual((v["author_id"], v["author_username"], v["author_followers"]),
                         ("5300000003", "fake_reel_maker", None))
        self.assertIsNone(v["views"])
        self.assertEqual((v["likes"], v["comments"], v["posted_at"], v["caption"]),
                         (4321, 87, "2026-09-21T14:21:40Z", "fake reel caption"))
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]),
                         ("1700000000000001", "Original audio", "fake_reel_maker"))
        self.assertEqual((v["duration_s"], v["route"]), (42.5, "browser"))

    def test_seed_media_wins_whatever_the_script_order(self):
        docs = list(reversed(load("instagram_reel_page.json")))
        self.with_tab(FakeTab(embedded=docs))
        self.assertEqual(instagram._browser_video("FakeCodeR01")["caption"], "fake reel caption")
        self.with_tab(FakeTab(embedded=docs[:1]))  # only the reels feed: the same reel is found there
        v = instagram._browser_video("FakeCodeR01")
        self.assertEqual((v["likes"], v["caption"]), (4321, None))

    def test_a_media_pk_opens_its_shortcode(self):
        from instagrapi.utils import InstagramIdCodec
        for code in ("FakeReel01A", "C_x9-Ab_12Q", "DAbcdEFGhij"):
            pk = instagram.pk_from_code(code)
            self.assertEqual(instagram.code_from_pk(pk), code)
            self.assertEqual(instagram.code_from_pk(pk), InstagramIdCodec.encode(int(pk)))
        tab = self.with_tab(FakeTab(embedded=load("instagram_reel_page.json")))
        with self.assertRaises(RouteError):
            instagram._browser_video(instagram.pk_from_code("FakeReel01A"))  # that reel is not on this page
        self.assertEqual(tab.urls, ["https://www.instagram.com/reel/FakeReel01A/"])

    def test_login_page_missing_reel_and_refs_it_does_not_open(self):
        self.with_tab(FakeTab(embedded=None, blocked=LOGIN_PAGE))
        with self.assertRaises(NotConfigured) as ctx:
            instagram._browser_video("FakeCodeR01")
        self.assertEqual(str(ctx.exception), instagram.BROWSER_NEEDS_LOGIN)
        self.with_tab(FakeTab(embedded=None))
        with self.assertRaises(RouteError) as ctx:
            instagram._browser_video("FakeCodeR01")
        self.assertNotIsInstance(ctx.exception, NotConfigured)
        tab = self.with_tab(FakeTab(embedded=load("instagram_reel_page.json")))
        for ref in ("https://www.instagram.com/share/reel/BAfake123/", "https://www.instagram.com/reels/audio/123/"):
            with self.assertRaises(RouteError):
                instagram._browser_video(ref)
            with self.assertRaises(AllRoutesFailed) as ctx:
                instagram.video(ref)
            self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi", "aiograpi"])
        self.assertEqual(tab.urls, [])
        env = instagram.video("https://www.instagram.com/reel/FakeCodeR01/")
        self.assertEqual((env["route"], env["result"]["likes"]), ("browser", 4321))


class IdTime(unittest.TestCase):
    def test_time_from_pk(self):
        when = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        pk = (int(when.timestamp() * 1000) - instagram.ID_EPOCH_MS) << 23 | 0x5A5A5A  # low 23 bits: shard, sequence
        self.assertEqual(instagram.time_from_pk(str(pk)), when)
        self.assertEqual(instagram.time_from_pk(f"{pk}_5200000002"), when)
        self.assertEqual(instagram.time_from_pk(pk), when)
        for bad in (None, "", "FakeCode001", "12ab", "-5", "9" * 400):
            self.assertIsNone(instagram.time_from_pk(bad), bad)


class SoundAndDownload(Quiet):
    def test_no_browser_route_for_sound(self):
        tab = self.with_tab(FakeTab())
        with self.assertRaises(AllRoutesFailed) as ctx:
            instagram.sound("777")
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi", "aiograpi"])
        self.assertEqual(tab.urls, [])

    def reel_with_files(self):
        page = load("instagram_reel_page.json")
        node = instagram._reel_node(page, "FakeCodeR01")
        node["video_versions"] = [{"type": 101, "width": 720, "height": 1280, "url": "https://cdn.invalid/big.mp4"},
                                  {"type": 102, "width": 480, "height": 854, "url": "https://cdn.invalid/small.mp4"},
                                  {"type": 103, "width": 360, "height": 640, "url": "https://cdn.invalid/tiny.mp4"}]
        return page

    def test_browser_download_saves_the_smallest_file_at_least_480_wide(self):
        self.with_tab(FakeTab(embedded=self.reel_with_files()))
        fetched = []

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None, context=None):
            fetched.append((req.full_url, req.get_header("Cookie")))
            return Answer(b"\x00\x00\x00\x18ftypmp42fake")

        with mock.patch("urllib.request.urlopen", urlopen):
            env = instagram.download("https://www.instagram.com/reel/FakeCodeR01/", str(self.home / "dl"))
        self.assertEqual(env["route"], "browser")
        self.assertEqual(fetched, [("https://cdn.invalid/small.mp4", None)])  # no cookie is sent
        path = Path(env["result"]["video_path"])
        self.assertEqual((path.name, path.read_bytes()[4:8]), ("FakeCodeR01.mp4", b"ftyp"))
        self.assertIsNone(env["result"]["captions_text"])

    def test_a_file_missing_its_height_is_not_taken_for_the_smallest(self):
        rank = instagram._download_rank
        versions = [{"width": 1080, "url": "https://cdn.invalid/1080.mp4"},
                    {"width": 720, "height": 1280, "url": "https://cdn.invalid/720.mp4"},
                    {"width": 480, "height": 854, "url": "https://cdn.invalid/480.mp4"},
                    {"width": 360, "height": 640, "url": "https://cdn.invalid/360.mp4"},
                    {"url": "https://cdn.invalid/unknown.mp4"}]
        self.assertEqual(min(versions, key=rank)["url"], "https://cdn.invalid/480.mp4")
        self.assertEqual(min(versions[:1] + versions[3:], key=rank)["url"], "https://cdn.invalid/1080.mp4")
        self.assertEqual(min(versions[3:], key=rank)["url"], "https://cdn.invalid/360.mp4")

    def test_browser_download_failures_never_show_the_file_link(self):
        self.with_tab(FakeTab(embedded=load("instagram_reel_page.json")))  # the fixture lists no video files
        with self.assertRaisesRegex(RouteError, "video_versions"):
            instagram._browser_download("FakeCodeR01", str(self.home / "dl"))
        self.with_tab(FakeTab(embedded=self.reel_with_files()))
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("https://cdn.invalid/small.mp4",
                                                                                     403, "no", {}, None)):
            with self.assertRaises(RouteError) as ctx:
                instagram._browser_download("FakeCodeR01", str(self.home / "dl"))
        self.assertEqual(str(ctx.exception), "Instagram's video file answered HTTP 403")
        tab = self.with_tab(FakeTab(embedded=self.reel_with_files()))
        with self.assertRaises(AllRoutesFailed) as ctx:
            instagram.download("https://www.instagram.com/share/reel/BAfake123/", str(self.home / "dl"))
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi", "aiograpi"])
        self.assertEqual(tab.urls, [])


class JsonScriptsRead(unittest.TestCase):
    def test_reads_only_matching_application_json_scripts(self):
        tab = browser.Tab(mock.Mock(), "T", "S")
        texts = [json.dumps({"require": [1]}), "not json"]
        with mock.patch.object(tab, "evaluate", return_value=texts) as evaluate:
            self.assertEqual(tab._embedded(browser.JsonScripts("FakeCodeR01")), [{"require": [1]}])
        expression = evaluate.call_args[0][0]
        self.assertIn('script[type="application/json"]', expression)
        self.assertIn('"FakeCodeR01"', expression)
        with mock.patch.object(tab, "evaluate", return_value=[]):
            self.assertIsNone(tab._embedded(browser.JsonScripts("x")))  # none: capture waits for a new document
        with mock.patch.object(tab, "evaluate", return_value='{"a": 1}'):
            self.assertEqual(tab._embedded(browser.UNIVERSAL_DATA), {"a": 1})
        with mock.patch.object(tab, "evaluate", return_value=[]) as evaluate:
            tab._embedded(browser.JsonScripts("FakeCodeR01", keys=instagram.REEL_KEYS))
        self.assertIn(json.dumps(list(instagram.REEL_KEYS)), evaluate.call_args[0][0])

    @unittest.skipUnless(shutil.which("node"), "needs Node.js to run the page expression")
    def test_in_page_selection_leaves_token_scripts_and_other_parts_behind(self):
        """The page expression itself, run in Node.js on a stand-in document: a script with the code and tokens but
        none of the keys is never returned, and from a data script only the parts under the keys are."""
        config = {"require": [["fake", "DTSGInitialData", [], {"token": "FAKE-NOT-A-TOKEN"}]],
                  "csrf_token": "FAKE-NOT-A-CSRF", "route": "/reel/FakeCodeR01/"}
        data = {"require": [["fake", {"__bbox": {"result": {"data": {
            "xig_fetch_media": {"code": "FakeCodeR01", "like_count": 1}}}}, "csrf_token": "FAKE-NOT-A-CSRF"}]]}
        feed = {"xdt_api__v1__clips__home__connection_v2": {"edges": [], "csrf_token": "FAKE-NOT-A-CSRF"},
                "code": "FakeCodeR01"}
        other = {"xig_fetch_media": {"code": "SomeOtherCode"}}
        scripts = [json.dumps(d) for d in (config, data, feed, other)] + ["not json FakeCodeR01 \"xig_fetch_media\""]
        expression = browser.JsonScripts("FakeCodeR01", keys=instagram.REEL_KEYS).expression()
        program = ("const texts = JSON.parse(process.argv[1]);\n"
                   "globalThis.document = {querySelectorAll: () => texts.map(t => ({textContent: t}))};\n"
                   f"process.stdout.write(JSON.stringify({expression}));\n")
        out = subprocess.run(["node", "-e", program, json.dumps(scripts)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        parts = json.loads(out.stdout)
        self.assertEqual([json.loads(p) for p in parts],
                         [{"xig_fetch_media": {"code": "FakeCodeR01", "like_count": 1}}])  # the feed part holds a csrf
        self.assertNotIn("FAKE-NOT-A", out.stdout)
        whole = browser.JsonScripts("FakeCodeR01").expression()  # no keys: every script with the code, whole
        out = subprocess.run(["node", "-e", program.replace(expression, whole), json.dumps(scripts)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(len(json.loads(out.stdout)), 4)


class DoctorSummary(unittest.TestCase):
    def test_browser_route_is_unknown_without_a_session(self):
        ready = {"python:yt_dlp": True, "node": True, "youtubei.js": True, "yt.mjs": True, "omkarcloud": True,
                 "dtk": False, "tiktok_proxy": False, "python:instagrapi": True, "python:aiograpi": True,
                 "instagram session": False, "browser": True, "python:websockets": True}
        ig = doctor._platforms(dict(ready), None)["instagram"]
        self.assertEqual(ig["routes"], {"instagrapi": False, "aiograpi": False, "browser": None})
        self.assertIsNone(ig["available"])
        self.assertIn("browser-login instagram", " ".join(ig["notes"]))
        ig = doctor._platforms(dict(ready, browser=False), None)["instagram"]
        self.assertEqual((ig["routes"]["browser"], ig["available"]), (False, False))
        ig = doctor._platforms(dict(ready, **{"instagram session": True}), None)["instagram"]
        self.assertIs(ig["available"], True)


if __name__ == "__main__":
    unittest.main()
