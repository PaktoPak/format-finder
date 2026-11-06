"""Unit tests for instagram.py with fake clients and raw dicts shaped like Instagram's private API.

No request reaches Instagram. These tests cover the session routes (instagrapi, aiograpi), so the browser route is
off in this module (browser_route false in its own settings file); tests/test_instagram_browser.py covers it.
Run: python -m unittest discover -s tests
"""
import asyncio
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import __main__ as cli  # noqa: E402
from format_finder import browser, common, instagram  # noqa: E402
from format_finder.common import AllRoutesFailed, NotConfigured, RouteError, iso_from_timestamp  # noqa: E402

NOW = int(time.time())
DAY = 86400
SESSIONID = "1234567890%3AAbCdEfGhIjKlMn%3A12%3AAYdummyDummyDummy"


def reel(pk, code, days_ago=1, drop=(), **extra):
    """A raw reel dict like the ones in clips/user, media info and search responses."""
    node = {
        "pk": pk, "id": f"{pk}_42", "code": code, "taken_at": NOW - days_ago * DAY, "media_type": 2,
        "product_type": "clips", "user": {"pk": 42, "username": "maker", "full_name": "Maker"},
        "caption": {"text": "how I budget"}, "play_count": 1000, "ig_play_count": 990, "view_count": 0,
        "like_count": 50, "comment_count": 5, "video_duration": 14.533, "is_paid_partnership": False,
        "clips_metadata": {"music_info": None, "original_sound_info": {
            "audio_asset_id": 777, "original_audio_title": "Original audio", "ig_artist": {"username": "maker"}}},
    }
    node.update(extra)
    for key in drop:
        node.pop(key, None)
    return node



def _isolate_settings():
    """The user's own ~/.format-finder/settings.json must not change what the tests see."""
    own = _mock.patch.object(_ff_settings, "USER_PATH", _Path(_tempfile.mkdtemp()) / "settings.json")
    own.start()
    unittest.addModuleCleanup(own.stop)


class BrowserStarted(BaseException):
    """Raised past every route's error handling, so a test that reaches a real browser fails loudly."""


def setUpModule():
    """No unit test may start a real browser (it would send real requests to Instagram)."""
    guard = _mock.patch.object(browser.Browser, "start", side_effect=BrowserStarted("a unit test started a browser"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)
    _isolate_settings()
    _ff_settings.USER_PATH.write_text(json.dumps({"browser_route": False}), encoding="utf-8")


class Fake:
    """Client stand-in: each method returns (or raises) its next canned response and records the call."""

    def __init__(self, asynchronous=False, **responses):
        self.calls = []
        self.responses = {k: list(v) for k, v in responses.items()}
        self.asynchronous = asynchronous

    def __getattr__(self, name):
        if "hashtag" in name:
            raise AssertionError(f"hashtag method used: {name}")
        if name.startswith("_") or name not in self.responses:
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            value = self.responses[name].pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        async def amethod(*args, **kwargs):
            return method(*args, **kwargs)

        return amethod if self.asynchronous else method

    def names(self):
        return [c[0] for c in self.calls]


def run(op, *args, **kwargs):
    return asyncio.run(op(*args, **kwargs))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._saved = (common.FF_HOME, instagram._delay, instagram._client)
        common.FF_HOME = self.home
        instagram._delay = lambda: 0
        instagram._blocked[0] = None

    def tearDown(self):
        common.FF_HOME, instagram._delay, instagram._client = self._saved
        instagram._blocked[0] = None
        self.tmp.cleanup()


class Refs(unittest.TestCase):
    def test_pk_from_code_matches_instagrapi(self):
        from instagrapi.utils import InstagramIdCodec
        for code in ("FakeReel01A", "FakeReel02B", "C_x9-Ab_12Q", "DAbcdEFGhij"):
            self.assertEqual(instagram.pk_from_code(code), str(InstagramIdCodec.decode(code)))
        self.assertEqual(instagram.pk_from_code("FakeReel01A"), "6243248125794274624")
        self.assertEqual(instagram.pk_from_code("FakeLongCodeForTestsOnlyAbcdefghij01234"), "6243247724364311069")

    def test_parse_ref(self):
        p = instagram.parse_ref
        self.assertEqual(p("https://www.instagram.com/reel/C_x9-Ab_12Q/?igsh=abc"), ("code", "C_x9-Ab_12Q"))
        self.assertEqual(p("https://instagram.com/p/FakeReel01A/"), ("code", "FakeReel01A"))
        self.assertEqual(p("instagram.com/reels/FakeReel01A"), ("code", "FakeReel01A"))
        self.assertEqual(p("https://www.instagram.com/maker/reel/FakeReel01A/"), ("code", "FakeReel01A"))
        self.assertEqual(p("https://www.instagram.com/reels/audio/1234567890123456/"), ("audio", "1234567890123456"))
        self.assertEqual(p("https://www.instagram.com/share/p/FakeShare1/")[0], "share")
        self.assertEqual(p("FakeReel01A"), ("code", "FakeReel01A"))
        self.assertEqual(p("6243248125794274624"), ("pk", "6243248125794274624"))
        self.assertEqual(p("6243248125794274624_5200000002"), ("pk", "6243248125794274624"))
        self.assertEqual(p("https://www.tiktok.com/@a/video/1")[0], None)
        self.assertEqual(p("")[0], None)

    def test_username_from(self):
        self.assertEqual(instagram.username_from("@Maker"), "maker")
        self.assertEqual(instagram.username_from("https://www.instagram.com/maker/?hl=en"), "maker")
        self.assertEqual(instagram.username_from("instagram.com/maker"), "maker")

    def test_clean_sessionid(self):
        self.assertEqual(instagram.clean_sessionid(SESSIONID), SESSIONID)
        self.assertEqual(instagram.clean_sessionid(" sessionid=" + SESSIONID + "; Path=/ "), SESSIONID)
        self.assertEqual(instagram.clean_sessionid('"' + SESSIONID.replace("%3A", ":") + '"'), SESSIONID)
        for bad in ("abc", "", "notdigits%3Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "123%3Ashort"):
            with self.assertRaises(RouteError):
                instagram.clean_sessionid(bad)


class Parsing(unittest.TestCase):
    def test_media_nodes_shapes(self):
        a, b, c = reel(1, "AAAAAAAAAAA"), reel(2, "BBBBBBBBBBB"), reel(3, "CCCCCCCCCCC")
        clips_user = {"items": [{"media": a}, {"media": b}], "paging_info": {"max_id": "x", "more_available": True}}
        self.assertEqual([n["pk"] for n in instagram.media_nodes(clips_user)], [1, 2])
        serp = {"reels_serp_modules": [{"module_type": "clips", "clips": [{"media": a}, {"media": c}]}],
                "extra": {"nested": [{"media": a}]}, "reels_max_id": "r", "has_more": True}
        self.assertEqual([n["pk"] for n in instagram.media_nodes(serp)], [1, 3])
        top = {"media_grid": {"sections": [{"layout_content": {"one_by_two_item": {"clips": {"items": [{"media": b}]}},
                                                               "fill_items": [{"media": c}]}}]}}
        self.assertEqual([n["pk"] for n in instagram.media_nodes(top)], [2, 3])
        carousel = reel(9, "DDDDDDDDDDD", media_type=8, carousel_media=[reel(10, "EEEEEEEEEEE")])
        self.assertEqual([n["pk"] for n in instagram.media_nodes({"items": [carousel]})], [9])
        self.assertEqual(instagram.media_nodes({"metadata": {"music_info": {"music_asset_info": {"id": "1"}}}}), [])
        self.assertEqual(instagram.media_nodes(None), [])

    def test_next_cursor(self):
        nc = instagram.next_cursor
        self.assertEqual(nc({"paging_info": {"max_id": "m1", "more_available": True}}), "m1")
        self.assertIsNone(nc({"paging_info": {"max_id": "m1", "more_available": False}}))
        self.assertEqual(nc({"reels_max_id": "r2", "has_more": True}), "r2")
        self.assertEqual(nc({"next_max_id": 55}), "55")
        self.assertIsNone(nc({"has_more": True}))
        self.assertIsNone(nc({"items": []}))
        self.assertIsNone(nc(None))

    def test_reel_record(self):
        node = reel(3456, "C_x9-Ab_12Q", sponsor_tags=[], reshare_count=12)
        v = instagram.video_from_node(node, "instagrapi")
        self.assertEqual(v["platform"], "instagram")
        self.assertEqual(v["id"], "3456")
        self.assertEqual(v["url"], "https://www.instagram.com/reel/C_x9-Ab_12Q/")
        self.assertEqual(v["views"], 1000)  # play_count, not ig_play_count or view_count
        self.assertEqual((v["likes"], v["comments"], v["shares"]), (50, 5, 12))
        self.assertEqual(v["caption"], "how I budget")
        self.assertEqual((v["author_id"], v["author_username"]), ("42", "maker"))
        self.assertEqual(v["author_url"], "https://www.instagram.com/maker/")
        self.assertIsNone(v["author_followers"])
        self.assertEqual(v["posted_at"], iso_from_timestamp(node["taken_at"]))
        self.assertTrue(v["posted_at"].endswith("Z"))
        self.assertEqual(v["duration_s"], 14.53)
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]), ("777", "Original audio", "maker"))
        self.assertIs(v["is_paid"], False)
        self.assertEqual((v["route"], v["raw_ref"]), ("instagrapi", "C_x9-Ab_12Q"))
        self.assertEqual(set(v), set(common.VIDEO_KEYS))

    def test_licensed_music_and_attribution(self):
        music = {"music_info": {"music_asset_info": {"audio_cluster_id": 555, "id": "111", "title": "Song",
                                                     "display_artist": "Artist"}}, "original_sound_info": None}
        v = instagram.video_from_node(reel(1, "AAAAAAAAAAA", clips_metadata=music), "aiograpi")
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]), ("555", "Song", "Artist"))
        attr = {"audio_id": "999", "song_name": "Other", "artist_name": "Someone"}
        v = instagram.video_from_node(reel(1, "AAAAAAAAAAA", drop=("clips_metadata",),
                                           clips_music_attribution_info=attr), "aiograpi")
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]), ("999", "Other", "Someone"))
        v = instagram.video_from_node(reel(1, "AAAAAAAAAAA", drop=("clips_metadata",)), "aiograpi")
        self.assertEqual((v["sound_id"], v["sound_title"], v["sound_author"]), (None, None, None))

    def test_older_video_uses_view_count(self):
        node = reel(1, "AAAAAAAAAAA", product_type="feed", view_count=900, drop=("play_count", "ig_play_count"))
        v = instagram.video_from_node(node, "instagrapi")
        self.assertEqual(v["views"], 900)
        self.assertEqual(v["url"], "https://www.instagram.com/p/AAAAAAAAAAA/")

    def test_missing_values_stay_none(self):
        node = reel(1, "AAAAAAAAAAA", caption=None, drop=(
            "play_count", "ig_play_count", "view_count", "like_count", "comment_count", "video_duration",
            "is_paid_partnership", "clips_metadata"))
        v = instagram.video_from_node(node, "instagrapi")
        for key in ("views", "likes", "comments", "shares", "duration_s", "is_paid", "caption", "sound_id"):
            self.assertIsNone(v[key], key)
        v = instagram.video_from_node(reel(1, "AAAAAAAAAAA", taken_at=None, video_duration=0), "instagrapi")
        self.assertIsNone(v["posted_at"])
        self.assertIsNone(v["duration_s"])

    def test_hidden_counts_and_photos(self):
        hidden = reel(1, "AAAAAAAAAAA", like_and_view_counts_disabled=True, like_count=0, play_count=0,
                      drop=("ig_play_count", "view_count"))
        v = instagram.video_from_node(hidden, "instagrapi")
        self.assertIsNone(v["likes"])
        self.assertIsNone(v["views"])
        shown = instagram.video_from_node(reel(1, "AAAAAAAAAAA", like_count=0), "instagrapi")
        self.assertEqual(shown["likes"], 0)
        photo = instagram.video_from_node(reel(1, "AAAAAAAAAAA", media_type=1, product_type="feed"), "instagrapi")
        self.assertIsNone(photo["views"])
        self.assertIsNone(photo["duration_s"])
        paid = instagram.video_from_node(reel(1, "AAAAAAAAAAA", sponsor_tags=[{"sponsor": {"pk": 7}}]), "x")
        self.assertIs(paid["is_paid"], True)

    def test_user_object_fills_author(self):
        user = SimpleNamespace(pk="42", username="maker", follower_count=1234)
        v = instagram.video_from_node(reel(1, "AAAAAAAAAAA", user={"pk": 42}), "instagrapi", user=user)
        self.assertEqual((v["author_followers"], v["author_username"]), (1234, "maker"))


class Ops(Base):
    def search_pages(self):
        page1 = {"reels_serp_modules": [{"clips": [{"media": reel(1, "AAAAAAAAAAA", days_ago=2)},
                                                   {"media": reel(2, "BBBBBBBBBBB", days_ago=100)},
                                                   {"media": reel(3, "CCCCCCCCCCC", media_type=1)}]}],
                 "has_more": True, "reels_max_id": "p2", "rank_token": "rt1"}
        page2 = {"reels_serp_modules": [{"clips": [{"media": reel(4, "DDDDDDDDDDD", days_ago=3)},
                                                   {"media": reel(1, "AAAAAAAAAAA", days_ago=2)},
                                                   {"media": reel(5, "EEEEEEEEEEE", days_ago=5)}]}],
                 "has_more": False}
        return page1, page2

    def test_search_paginates_filters_and_strips_hashes(self):
        for asynchronous in (False, True):
            cl = Fake(asynchronous, fbsearch_reels_v2=self.search_pages())
            out = run(instagram._search, cl, "aiograpi" if asynchronous else "instagrapi", "#budget  app  tips",
                      days=60, limit=10)
            self.assertEqual([v["id"] for v in out], ["1", "4", "5"])
            self.assertEqual(cl.names(), ["fbsearch_reels_v2", "fbsearch_reels_v2"])
            self.assertEqual(cl.calls[0][1], ("budget app tips",))
            self.assertEqual(cl.calls[0][2], {"reels_max_id": None, "rank_token": None})
            self.assertEqual(cl.calls[1][2], {"reels_max_id": "p2", "rank_token": "rt1"})

    def test_search_stops_at_limit(self):
        cl = Fake(fbsearch_reels_v2=self.search_pages())
        out = run(instagram._search, cl, "instagrapi", "budget", days=None, limit=2)
        self.assertEqual([v["id"] for v in out], ["1", "2"])
        self.assertEqual(len(cl.calls), 1)

    def test_search_falls_back_to_top_tab_reels(self):
        top = {"media_grid": {"sections": [{"layout_content": {"fill_items": [
            {"media": reel(7, "GGGGGGGGGGG")}, {"media": reel(8, "HHHHHHHHHHH", product_type="feed")}]}}]}}
        cl = Fake(fbsearch_reels_v2=[{"status": "ok"}], fbsearch_topsearch_v2=[top])
        out = run(instagram._search, cl, "instagrapi", "budget", days=60, limit=5)
        self.assertEqual([v["id"] for v in out], ["7"])
        self.assertEqual(cl.names(), ["fbsearch_reels_v2", "fbsearch_topsearch_v2"])

    def test_search_rejects_empty_phrase(self):
        with self.assertRaises(RouteError):
            run(instagram._search, Fake(), "instagrapi", " # ")

    def test_account_newest_first_without_pinned(self):
        user = SimpleNamespace(pk="42", username="maker", follower_count=5000)
        page = {"items": [{"media": reel(1, "AAAAAAAAAAA", days_ago=400)},  # pinned old hit
                          {"media": reel(2, "BBBBBBBBBBB", days_ago=9)},
                          {"media": reel(3, "CCCCCCCCCCC", days_ago=1)},
                          {"media": reel(4, "DDDDDDDDDDD", days_ago=4)}],
                "paging_info": {"max_id": "", "more_available": False}}
        for asynchronous in (False, True):
            cl = Fake(asynchronous, user_info_by_username=[user], private_request=[page])
            acct = run(instagram._account, cl, "instagrapi", "@Maker", limit=2)
            self.assertEqual(cl.calls[0][1], ("maker",))
            name, args, kwargs = cl.calls[1]
            self.assertEqual(args, ("clips/user/",))
            self.assertEqual(kwargs["data"]["target_user_id"], 42)
            self.assertEqual([p["id"] for p in acct["posts"]], ["3", "4"])
            self.assertEqual((acct["id"], acct["username"], acct["followers"]), ("42", "maker", 5000))
            self.assertEqual(acct["url"], "https://www.instagram.com/maker/")
            self.assertTrue(all(p["author_followers"] == 5000 for p in acct["posts"]))
            self.assertEqual(set(acct), set(common.ACCOUNT_KEYS))

    def test_video_by_url_code_and_pk(self):
        node = reel(6243248125794274624, "FakeReel01A")
        for ref in ("https://www.instagram.com/reel/FakeReel01A/", "FakeReel01A", "6243248125794274624"):
            cl = Fake(private_request=[{"items": [node], "num_results": 1}])
            v = run(instagram._video, cl, "instagrapi", ref)
            self.assertEqual(cl.calls[0][1], ("media/6243248125794274624/info/",))
            self.assertEqual(v["views"], 1000)
        with self.assertRaises(RouteError):
            run(instagram._video, Fake(private_request=[{"items": []}]), "instagrapi", "FakeReel01A")
        with self.assertRaises(RouteError):
            run(instagram._video, Fake(), "instagrapi", "not a ref!")

    def test_sound_from_audio_url(self):
        page = {"items": [{"media": reel(1, "AAAAAAAAAAA", drop=("clips_metadata",))}],
                "paging_info": {"more_available": False},
                "metadata": {"original_sound_info": {"audio_asset_id": 777, "original_audio_title": "Original audio",
                                                     "ig_artist": {"username": "maker"}}}}
        cl = Fake(track_info_by_id=[page])
        out = run(instagram._sound, cl, "instagrapi", "https://www.instagram.com/reels/audio/777/", limit=5)
        self.assertEqual(cl.calls[0][1:], (("777",), {"max_id": ""}))
        self.assertEqual((out[0]["sound_id"], out[0]["sound_title"], out[0]["sound_author"]),
                         ("777", "Original audio", "maker"))

    def test_sound_follows_a_reels_audio(self):
        music = {"music_info": {"music_asset_info": {"audio_cluster_id": 555, "title": "Song", "display_artist": "A"}}}
        cl = Fake(private_request=[{"items": [reel(1, "AAAAAAAAAAA", clips_metadata=music)]}],
                  track_info_by_id=[{"items": [{"media": reel(2, "BBBBBBBBBBB", clips_metadata=music)}],
                                     "paging_info": {"more_available": True, "max_id": "n2"}},
                                    {"items": [{"media": reel(3, "CCCCCCCCCCC", clips_metadata=music)}],
                                     "paging_info": {"more_available": False}}])
        out = run(instagram._sound, cl, "instagrapi", "AAAAAAAAAAA", limit=10)
        self.assertEqual(cl.names(), ["private_request", "track_info_by_id", "track_info_by_id"])
        self.assertEqual(cl.calls[1][1], ("555",))
        self.assertEqual(cl.calls[2][2], {"max_id": "n2"})
        self.assertEqual([v["id"] for v in out], ["2", "3"])

    def test_search_leaves_out_ads(self):
        page = {"reels_serp_modules": [{"clips": [{"media": reel(1, "AAAAAAAAAAA", product_type="ad")},
                                                  {"media": reel(2, "BBBBBBBBBBB")}]}], "has_more": False}
        out = run(instagram._search, Fake(fbsearch_reels_v2=[page]), "instagrapi", "budget", days=None, limit=5)
        self.assertEqual([v["id"] for v in out], ["2"])
        self.assertTrue(instagram.video_from_node(reel(1, "AAAAAAAAAAA", product_type="ad"), "t")["is_paid"])

    def test_sound_follows_a_reels_licensed_music_id(self):
        # Some reels carry only clips_metadata.music_canonical_id (seen in a real 2021 clips/user reply).
        track = SimpleNamespace(audio_cluster_id=555, title="Song", display_artist="Artist")
        only_canonical = {"music_info": None, "original_sound_info": None, "music_canonical_id": "18000000000000001"}
        cl = Fake(private_request=[{"items": [reel(1, "AAAAAAAAAAA", clips_metadata=only_canonical)]}],
                  track_info_by_canonical_id=[track],
                  track_info_by_id=[{"items": [{"media": reel(2, "BBBBBBBBBBB", drop=("clips_metadata",))}]}])
        out = run(instagram._sound, cl, "instagrapi", "AAAAAAAAAAA", limit=5)
        self.assertEqual(cl.names(), ["private_request", "track_info_by_canonical_id", "track_info_by_id"])
        self.assertEqual(cl.calls[1][1], ("18000000000000001",))
        self.assertEqual((out[0]["sound_id"], out[0]["sound_title"]), ("555", "Song"))

    def test_sound_by_music_canonical_id(self):
        track = SimpleNamespace(audio_cluster_id=555, title="Song", display_artist="Artist")
        cl = Fake(track_info_by_id=[{"items": []}, {"items": [{"media": reel(2, "BBBBBBBBBBB", drop=("clips_metadata",))}]}],
                  track_stream_info_by_id=[{"items": []}], track_info_by_canonical_id=[track])
        out = run(instagram._sound, cl, "instagrapi", "18000000000000001", limit=5)
        self.assertEqual(cl.names(), ["track_info_by_id", "track_stream_info_by_id", "track_info_by_canonical_id",
                                      "track_info_by_id"])
        self.assertEqual(cl.calls[3][1], ("555",))
        self.assertEqual((out[0]["sound_id"], out[0]["sound_title"], out[0]["sound_author"]), ("555", "Song", "Artist"))

    def test_sound_with_no_reels_is_empty(self):
        from instagrapi.exceptions import TrackNotFound
        cl = Fake(track_info_by_id=[{"items": []}], track_stream_info_by_id=[ValueError("stream not JSON")],
                  track_info_by_canonical_id=[TrackNotFound()])
        self.assertEqual(run(instagram._sound, cl, "instagrapi", "123456789012345", limit=5), [])

    def test_download(self):
        out_dir = self.home / "run"

        def clip_download(pk, folder):
            path = Path(folder) / f"maker_{pk}.mp4"
            path.write_bytes(b"\x00")
            return path

        cl = Fake()
        cl.clip_download = clip_download  # a real attribute wins over __getattr__
        res = run(instagram._download, cl, "instagrapi", "FakeReel01A", out_dir)
        self.assertEqual(res, {"video_path": str((out_dir / "maker_6243248125794274624.mp4").resolve()),
                               "captions_text": None, "captions_source": None})


class Sessions(Base):
    def write_session(self, **auth):
        from instagrapi import Client
        settings = Client().get_settings()
        settings["authorization_data"] = auth
        settings["cookies"] = {"sessionid": auth.get("sessionid", "")} if auth else {}
        instagram.session_path().write_text(json.dumps(settings))

    def test_missing_session_is_not_configured_everywhere(self):
        calls = [(instagram.search, ("budget app",), {"days": 60, "limit": 5}),
                 (instagram.account, ("maker",), {"limit": 5}),
                 (instagram.video, ("FakeReel01A",), {}),
                 (instagram.sound, ("777",), {"limit": 5}),
                 (instagram.download, ("FakeReel01A", str(self.home / "dl")), {})]
        for fn, args, kwargs in calls:
            with self.assertRaises(AllRoutesFailed) as ctx:
                fn(*args, **kwargs)
            self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi", "aiograpi"])
            for e in ctx.exception.errors:
                self.assertEqual(e["error"], f"NotConfigured: Instagram needs a login: {instagram.LOGIN_HINT}")
                self.assertNotIn("<", e["error"])  # the hint never puts the cookie on the command line

    def test_session_file_loads_in_both_libraries(self):
        self.write_session(ds_user_id="1234567890", sessionid=SESSIONID, should_use_header_over_cookies=True)

        async def load(lib):
            cl = instagram._client(lib)
            try:
                return cl.user_id, cl.sessionid, cl.with_challenge_flow
            finally:
                await instagram._close(cl)

        for lib in instagram.LIBS:
            self.assertEqual(asyncio.run(load(lib)), (1234567890, SESSIONID, False))

    def test_session_without_login_or_unreadable(self):
        self.write_session()
        with self.assertRaises(NotConfigured):
            instagram._client("instagrapi")
        instagram.session_path().write_text("{not json")
        with self.assertRaises(NotConfigured):
            instagram._client("aiograpi")

    def test_login_required_stops_both_routes(self):
        from instagrapi.exceptions import LoginRequired
        cl = Fake(fbsearch_reels_v2=[LoginRequired("login_required")])
        instagram._client = lambda lib: cl
        with self.assertRaises(AllRoutesFailed) as ctx:
            instagram.search("budget", days=60, limit=5)
        errors = [e["error"] for e in ctx.exception.errors]
        self.assertTrue(errors[0].startswith("NotConfigured: Instagram rejected the saved session"))
        self.assertTrue(errors[1].endswith("(not retried with this route)"))
        self.assertEqual(len(cl.calls), 1)

    def test_a_missing_handle_is_not_found_and_stops_the_routes(self):
        from instagrapi.exceptions import UserNotFound
        clients = {"instagrapi": Fake(user_info_by_username=[UserNotFound("User not found")] * 3),
                   "aiograpi": Fake(True)}
        instagram._client = lambda lib: clients[lib]
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)
        with _mock.patch.object(instagram, "_browser_account") as browser_account:
            for _ in range(3):
                with self.assertRaises(AllRoutesFailed) as ctx:
                    instagram.account("nobody_made_up", limit=5)
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["instagrapi"])  # aiograpi and browser not asked
        self.assertTrue(ctx.exception.errors[0]["error"].startswith("NotFound: Instagram has no such account"))
        self.assertIn("User not found", ctx.exception.errors[0]["error"])
        browser_account.assert_not_called()
        self.assertEqual(common.route_skips(), [])  # three bad handles are no reason to skip the session routes

    def test_other_errors_move_to_next_route(self):
        page = {"reels_serp_modules": [{"clips": [{"media": reel(1, "AAAAAAAAAAA")}]}], "has_more": False}
        clients = {"instagrapi": Fake(fbsearch_reels_v2=[KeyError("items")]),
                   "aiograpi": Fake(True, fbsearch_reels_v2=[page])}
        instagram._client = lambda lib: clients[lib]
        env = instagram.search("budget", days=60, limit=5)
        self.assertEqual(env["route"], "aiograpi")
        self.assertEqual(env["errors"], [{"route": "instagrapi", "error": "KeyError: 'items'"}])
        self.assertEqual(env["result"][0]["route"], "aiograpi")


class Login(Base):
    def test_bad_input_makes_no_request(self):
        with self.assertRaisesRegex(RouteError, "not an Instagram sessionid cookie"):
            instagram.login(sessionid="abc")
        with self.assertRaisesRegex(RouteError, "not found"):
            instagram.login(settings_file=str(self.home / "missing.json"))
        empty = self.home / "empty.json"
        empty.write_text(json.dumps({"cookies": {}, "authorization_data": {}}))
        with self.assertRaisesRegex(RouteError, "no sessionid"):
            instagram.login(settings_file=str(empty))
        with self.assertRaises(RouteError):
            instagram.login()
        self.assertFalse(instagram.session_path().exists())

    def test_sessionid_login_saves_private_session(self):
        from instagrapi import Client

        def fake_login(self, sessionid):  # what login_by_sessionid leaves behind, without the request
            self.settings["cookies"] = {"sessionid": sessionid}
            self.init()
            self.authorization_data = {"ds_user_id": "1234567890", "sessionid": sessionid,
                                       "should_use_header_over_cookies": True}
            self.username = "tester"
            return True

        saved = Client.login_by_sessionid
        Client.login_by_sessionid = fake_login
        try:
            out = instagram.login(sessionid=SESSIONID.replace("%3A", ":"))
        finally:
            Client.login_by_sessionid = saved
        path = instagram.session_path()
        self.assertEqual(out, {"ok": True, "username": "tester", "session_path": str(path)})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())["authorization_data"]["sessionid"], SESSIONID)
        self.assertEqual(instagram._client("instagrapi").user_id, 1234567890)

    def test_settings_file_login_checks_and_copies(self):
        from instagrapi import Client
        source = self.home / "mine.json"
        settings = Client().get_settings()
        settings["authorization_data"] = {"ds_user_id": "1234567890", "sessionid": SESSIONID}
        source.write_text(json.dumps(settings))
        saved = Client.user_info_v1
        Client.user_info_v1 = lambda self, user_id, *a, **k: SimpleNamespace(username=f"user{user_id}")
        try:
            out = instagram.login(settings_file=str(source))
        finally:
            Client.user_info_v1 = saved
        self.assertEqual(out["username"], "user1234567890")
        self.assertEqual(json.loads(instagram.session_path().read_text())["uuids"], settings["uuids"])

    def test_rejected_session_is_a_clear_error(self):
        from instagrapi import Client
        from instagrapi.exceptions import LoginRequired
        saved = Client.login_by_sessionid
        Client.login_by_sessionid = lambda self, s: (_ for _ in ()).throw(LoginRequired("login_required"))
        try:
            with self.assertRaisesRegex(RouteError, "Instagram did not accept this session"):
                instagram.login(sessionid=SESSIONID)
        finally:
            Client.login_by_sessionid = saved
        self.assertFalse(instagram.session_path().exists())


class Cli(Base):
    def test_cli_without_session_prints_not_configured(self):
        for argv in (["search", "instagram", "budget app", "--limit", "3"], ["account", "instagram", "maker"],
                     ["video", "instagram", "FakeReel01A"], ["sound", "instagram", "777"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(argv)
            out = json.loads(buf.getvalue())
            self.assertEqual((code, out["ok"], out["platform"]), (1, False, "instagram"))
            self.assertTrue(all(e["error"].startswith("NotConfigured: Instagram needs a login") for e in out["errors"]))


if __name__ == "__main__":
    unittest.main()
