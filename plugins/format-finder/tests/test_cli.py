"""Offline tests for the command line, settings checks and small shared helpers. Run: python -m unittest discover -s tests"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import __main__ as cli, browser, common, settings, tiktok, youtube  # noqa: E402
from format_finder.common import AllRoutesFailed, RouteError, keywords  # noqa: E402


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(list(argv))
    return code, json.loads(out.getvalue())


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


class Cli(unittest.TestCase):
    def test_argument_errors_answer_json(self):
        for argv in (["search", "badplatform", "x"], ["account", "youtube"], ["search", "youtube", "x", "--limit", "abc"],
                     ["scan"], []):
            code, out = run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertFalse(out["ok"])
            self.assertTrue(out["errors"][0]["error"].startswith("UsageError: "), out)
        self.assertEqual(run_cli("search", "badplatform", "x")[1]["command"], "search")


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"
        patcher = mock.patch.object(settings, "USER_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)

    def write(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_wrong_type_stops_with_the_file_named(self):
        self.write({"outlier_multiple_min": {"value": "5"}})
        errors, _ = settings.check()
        self.assertIn(str(self.path), errors[0])
        self.assertIn("outlier_multiple_min", errors[0])
        with self.assertRaises(ValueError):
            settings.get("outlier_multiple_min")

    def test_types_that_fit(self):
        self.write({"boost_ratio": 1, "tiktok_proxy": "http://p:1", "in_niche_count": 4, "dtk_url": None,
                    "recent_days": {"value": 30}})
        self.assertEqual(settings.check(), ([], []))
        self.assertEqual(settings.get("recent_days"), 30)
        self.write({"recent_days": True})
        self.assertTrue(settings.check()[0])
        self.write({"recent_days": None})
        self.assertTrue(settings.check()[0])

    def test_browser_settings(self):
        defaults = json.loads(settings.DEFAULTS_PATH.read_text(encoding="utf-8"))
        names = ("browser_route", "browser_path", "browser_launch_timeout_seconds", "browser_load_timeout_seconds",
                 "browser_search_timeout_seconds", "browser_page_timeout_seconds")
        for name in names:
            self.assertTrue(defaults[name]["note"], name)
        self.assertIs(defaults["browser_route"]["value"], True)
        self.write({"browser_route": False, "browser_search_timeout_seconds": 120, "browser_page_timeout_seconds": 60.5,
                    "browser_launch_timeout_seconds": {"value": 45}, "browser_load_timeout_seconds": 40,
                    "browser_path": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"})
        self.assertEqual(settings.check(), ([], []))
        self.assertIs(settings.get("browser_route"), False)
        self.assertEqual(settings.get("browser_page_timeout_seconds"), 60.5)
        for bad in ({"browser_route": "false"}, {"browser_route": 0}, {"browser_route": None},
                    {"browser_search_timeout_seconds": "90"}, {"browser_load_timeout_seconds": True},
                    {"browser_page_timeout_seconds": None}):
            self.write(bad)
            errors = settings.check()[0]
            self.assertEqual(len(errors), 1, bad)
            self.assertIn(next(iter(bad)), errors[0])
        self.write({"browser_route": "no"})
        self.assertIn("must be boolean", settings.check()[0][0])

    def test_browser_route_false_removes_the_browser_route(self):
        self.write({"browser_route": False})
        off = mock.Mock(side_effect=RouteError("off in this test"))
        with mock.patch.object(tiktok, "_omkar_search", off), mock.patch.object(tiktok, "_dtk_search", off), \
                mock.patch.object(tiktok, "_browser_search") as browser_search, \
                mock.patch.object(tiktok, "_browser_account") as browser_account, \
                mock.patch.object(tiktok, "_omkar_account", off), mock.patch.object(tiktok, "_dtk_account", off), \
                mock.patch.object(tiktok, "_ytdlp_account", off), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(AllRoutesFailed) as ctx:
                tiktok.search("meal prep", days=60, limit=1)
            self.assertEqual([e["route"] for e in ctx.exception.errors], ["omkarcloud", "dtk"])
            with self.assertRaises(AllRoutesFailed) as ctx:
                tiktok.account("someone", limit=1)
            self.assertEqual([e["route"] for e in ctx.exception.errors], ["omkarcloud", "dtk", "yt-dlp"])
        browser_search.assert_not_called()
        browser_account.assert_not_called()
        self.write({})
        self.assertEqual([n for n, _ in tiktok._with_browser([("omkarcloud", None)], None)], ["omkarcloud", "browser"])

    def test_unknown_key_is_a_warning(self):
        self.write({"recent_day": 30})
        errors, warnings = settings.check()
        self.assertEqual(errors, [])
        self.assertIn("recent_day", warnings[0])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(settings.get("recent_days"), 60)

    def test_broken_json_names_the_file(self):
        self.write("{recent_days: 3")
        errors, _ = settings.check()
        self.assertIn(str(self.path), errors[0])

    def test_secrets_are_masked(self):
        self.write({"dtk_api_key": "FAKE-KEY-123", "tiktok_proxy": "http://user:FAKEPASS@proxy.example:8080"})
        shown = json.dumps(settings.shown())
        self.assertNotIn("FAKE-KEY-123", shown)
        self.assertNotIn("FAKEPASS", shown)
        self.assertIn("http://user:***@proxy.example:8080", shown)


class Helpers(unittest.TestCase):
    def setUp(self):
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)

    def test_keywords_strip_hashes(self):
        self.assertEqual(keywords("#mealprep  #gym tips"), "mealprep gym tips")

    def test_searches_never_send_a_hash(self):
        sent = []

        def route(query, **kw):
            sent.append(query)
            return []

        with mock.patch.object(youtube, "_ytdlp_search", route), mock.patch.object(youtube, "_js_search", route):
            youtube.search("#mealprep", days=60, limit=1)
        with mock.patch.object(tiktok, "_omkar_search", route), mock.patch.object(tiktok, "_dtk_search", route), \
                mock.patch.object(tiktok, "_browser_search", route):
            tiktok.search("#mealprep", days=60, limit=1)
        self.assertEqual(sent, ["mealprep"] * 5)
        with self.assertRaises(RouteError):
            youtube.search("#", days=60)

    def test_upload_window_never_reaches_past_days(self):
        self.assertEqual([youtube.upload_window(d) for d in (None, 1, 6, 7, 29, 30, 60, 364, 365)],
                         [None, "today", "today", "week", "week", "month", "month", "month", "year"])

    def test_both_youtube_routes_widen_and_report_the_window(self):
        asked = []

        def node(*args):
            window = args[args.index("--upload-date") + 1] if "--upload-date" in args else None
            asked.append(window)
            return [{"id": "abcdefghijk", "views": 5}] if window == "year" else []

        with mock.patch.object(youtube, "_ytdlp_search", side_effect=RuntimeError("down")), \
                mock.patch.object(youtube, "_node", node), mock.patch.object(youtube, "pause"), \
                contextlib.redirect_stderr(io.StringIO()):
            env = youtube.search("meal prep", days=60, limit=1)
        self.assertEqual(asked, ["month", "year"])
        self.assertEqual(env["route"], "youtube.js")
        self.assertEqual(env["window"], {"asked": "month", "used": "year"})

    def test_no_views_label_counts_zero(self):
        self.assertEqual(youtube.count("No views"), 0)
        self.assertTrue(youtube._says_no_views({"a": [{"content": "No views"}]}))
        self.assertFalse(youtube._says_no_views({"a": "12 views"}))

    def test_tiktok_profile_with_posts_but_empty_list_is_an_error(self):
        with self.assertRaises(RouteError):
            tiktok._no_posts_listed([], 120, False)
        tiktok._no_posts_listed([], 120, True)  # private: an empty list is the true answer
        tiktok._no_posts_listed([], 0)
        tiktok._no_posts_listed([{"id": "1"}], 120)

    def test_tiktok_ad_flag(self):
        self.assertTrue(tiktok._from_omkar({"id": "1", "is_ad": True})["is_paid"])
        self.assertIsNone(tiktok._from_omkar({"id": "1", "is_ad": False})["is_paid"])

    def test_empty_account_answer_says_why(self):
        from format_finder.common import make_account, make_video, try_routes
        empty = lambda h: try_routes([("a", lambda h: make_account(platform="tiktok", username=h))], h)
        out = cli.envelope("tiktok", "account", empty, "some.brand")
        self.assertEqual((out["ok"], out["empty"]), (True, True))
        self.assertIn("check the handle", out["empty_reason"])
        private = lambda h: try_routes([("a", lambda h: make_account(platform="tiktok", is_private=True))], h)
        self.assertIn("private", cli.envelope("tiktok", "account", private, "x")["empty_reason"])
        none = lambda h: try_routes([("a", lambda h: make_account(platform="tiktok", post_count=0, followers=0))], h)
        self.assertIn("0 posts", cli.envelope("tiktok", "account", none, "x")["empty_reason"])
        post = make_video(platform="tiktok", id="1", views=10)
        full = lambda h: try_routes([("a", lambda h: make_account(platform="tiktok", followers=5, posts=[post]))], h)
        self.assertNotIn("empty_reason", cli.envelope("tiktok", "account", full, "x"))

    def test_account_with_followers_but_no_posts_is_marked_empty(self):
        """A profile that answered (followers, a post count) but listed no posts still has nothing to score."""
        from format_finder.common import make_account, make_video, try_routes
        no_reels = lambda h: try_routes([("browser", lambda h: make_account(
            platform="instagram", username=h, followers=1500, post_count=250))], h)
        out = cli.envelope("instagram", "account", no_reels, "fake.brand.app")
        self.assertEqual((out["ok"], out["empty"], out["route"]), (True, True, "browser"))
        self.assertIn("Reels tab gave none", out["empty_reason"])
        shorts = lambda h: try_routes([("yt-dlp", lambda h: make_account(platform="youtube", followers=90))], h)
        self.assertIn("Shorts tab", cli.envelope("youtube", "account", shorts, "x")["empty_reason"])
        self.assertNotIn("empty", cli.envelope("youtube", "video", lambda r: try_routes(
            [("yt-dlp", lambda r: make_video(platform="youtube", id=r))], r), "abcdefghijk"))

    def test_tiktok_profile_statuses(self):
        data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 10222, "statusMsg": "FakeUserSecret"}}}
        with contextlib.redirect_stderr(io.StringIO()):
            info = tiktok._user_detail(data, "fake_private_profile")
        self.assertEqual(info["user"], {"uniqueId": "fake_private_profile", "privateAccount": True})
        for status in (10202, 10221, 10223):
            data["__DEFAULT_SCOPE__"]["webapp.user-detail"] = {"statusCode": status, "statusMsg": "fake"}
            with self.assertRaises(common.NotFound) as ctx:
                tiktok._user_detail(data, "fake_missing_profile")
            self.assertIn(str(status), str(ctx.exception))
        data["__DEFAULT_SCOPE__"]["webapp.user-detail"] = {"statusCode": 10101, "statusMsg": "fake"}
        with self.assertRaises(RouteError) as ctx:
            tiktok._user_detail(data, "fake_profile")
        self.assertNotIsInstance(ctx.exception, common.NotFound)

    def test_tiktok_route_1_missing_user_is_not_found_and_the_later_routes_still_ask(self):
        class TikTokNotFound(Exception):  # stands in for the vendored scraper's own class
            pass

        def missing(ref):
            raise TikTokNotFound(f"user @{ref['username']} not found")

        fake = mock.Mock()
        fake.fetch.TikTokNotFound = TikTokNotFound
        fake.refs.resolve_user = lambda h: {"username": h}
        fake.users.get_profile = missing
        later = mock.Mock(side_effect=RouteError("fake later route down"))
        captcha = mock.Mock(side_effect=RouteError("fake captcha on the profile page"))
        with mock.patch.object(tiktok, "_omkar", return_value=fake), \
                mock.patch.object(tiktok, "_dtk_account", later), mock.patch.object(tiktok, "_ytdlp_account", later), \
                mock.patch.object(tiktok, "_browser_account", captcha), contextlib.redirect_stderr(io.StringIO()):
            out = cli.envelope("tiktok", "account", tiktok.account, "fake_missing_brand")
        self.assertFalse(out["ok"])
        self.assertEqual(out["errors"][0]["route"], "omkarcloud")
        self.assertTrue(out["errors"][0]["error"].startswith("NotFound: TikTok has no profile @fake_missing_brand"))
        self.assertEqual(captcha.call_count, 1)  # route 1 cannot tell a missing profile from no data
        self.assertTrue(out["errors"][-1]["error"].startswith("RouteError: fake captcha"))

    def test_tiktok_dtk_account_keeps_the_post_count(self):
        profile = {"uid": "7000000000000000001", "unique_id": "made_up_brand", "sec_uid": "MS4wLjABAAAA" + "x" * 40,
                   "stats": {"follower_count": 0, "content_count": 0}}
        with mock.patch.object(tiktok, "_dtk_fetch", side_effect=[profile, {"items": []}]), \
                mock.patch.object(tiktok, "pause"):
            acc = tiktok._dtk_account("made_up_brand", limit=5)
        self.assertEqual((acc["post_count"], acc["followers"], acc["posts"]), (0, 0, []))
        self.assertIn("0 posts", cli._empty_reason(acc))

    def test_youtube_empty_later_page_is_the_end_of_results(self):
        page = {"contents": [{"messageRenderer": {"text": {"runs": [{"text": "No more results"}]}}}]}
        for first, said in ((True, "page had no Shorts"), (False, "end of results")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(youtube._page_videos(None, page, "yt-dlp", first_page=first), [])
            self.assertIn(said, err.getvalue())
            self.assertEqual(err.getvalue().count("\n"), 1)

    def test_youtube_caption_language_and_whether_it_was_checked(self):
        track = [{"ext": "json3", "url": "u"}]
        info = {"automatic_captions": {"uk-orig": track, "en": track}, "formats": []}
        self.assertEqual(youtube._pick_captions(info)[1:], ("auto-captions:uk-orig", "uk", False))
        info["formats"] = [{"language": "uk", "format_note": "original (default)"}]
        self.assertEqual(youtube._pick_captions(info)[1:], ("auto-captions:uk-orig", "uk", True))
        self.assertEqual(youtube._pick_captions({"subtitles": {"de": track}})[1:], ("subtitles:de", "de", False))
        self.assertEqual(youtube._pick_captions({}), (None, None, None, None))

    def test_app_developer_site_and_its_social_links(self):
        from format_finder import appstore
        app = appstore.normalize({"trackId": 1, "trackName": "Budget", "sellerUrl": "https://www.example.invalid/"})
        self.assertEqual(app["developer_url"], "https://www.example.invalid/")
        html = ('<a href="https://www.tiktok.com/@examplebrand?lang=en">t</a> '
                '<a href="https://instagram.com/examplebrand/">i</a> '
                '<a href="https://www.instagram.com/explore/tags/budget/">tag</a> '
                '<a href="https://www.instagram.com/p/Cabc/">post</a> <a href="https://youtube.com/@examplebrand">y</a> '
                '<a href="https://www.tiktok.com/@EXAMPLEBRAND">again</a>')
        self.assertEqual(appstore.social_links(html),
                         {"tiktok": ["examplebrand"], "instagram": ["examplebrand"], "youtube": ["@examplebrand"]})
        self.assertIn("error", appstore._site_links("ftp://example.invalid"))

    def test_instagram_links_that_are_not_profiles(self):
        from format_finder import appstore
        html = ('<script async src="//www.instagram.com/embed.js"></script> '
                '<blockquote data-instgrm-permalink="https://www.instagram.com/p/FakePost1/"></blockquote> '
                '<a href="https://instagram.com/_u/fakebrand">app</a> '
                '<a href="https://api.instagram.com/oauth/authorize">o</a> '
                '<img src="https://www.instagram.com/static/images/fake.png"> '
                '<a href="https://help.instagram.com/000000000000001">h</a> '
                '<a href="https://about.instagram.com/blog">b</a> '
                '<a href="https://www.instagram.com/fake.brand.app/">dot</a> '
                '<a href="https://www.instagram.com/explore/tags/budget/">tag</a>')
        self.assertEqual(appstore.social_links(html)["instagram"], ["fakebrand", "fake.brand.app"])

    def test_youtube_links_open_the_right_channel(self):
        from format_finder import appstore
        html = ('<a href="https://www.youtube.com/c/TheFakeShow">c</a> <a href="https://youtube.com/user/FakeUser">u</a> '
                f'<a href="https://www.youtube.com/channel/UC{"a" * 22}">id</a> <a href="https://youtube.com/@fakebrand">@</a>')
        links = appstore.social_links(html)["youtube"]
        self.assertEqual([youtube.channel_url(h) for h in links],
                         ["https://www.youtube.com/c/TheFakeShow/shorts", "https://www.youtube.com/user/FakeUser/shorts",
                          f"https://www.youtube.com/channel/UC{'a' * 22}/shorts",
                          "https://www.youtube.com/@fakebrand/shorts"])
        self.assertEqual(youtube.channel_url("c/TheFakeShow"), "https://www.youtube.com/c/TheFakeShow/shorts")

    def test_apps_with_links_reads_each_site_once_and_one_bad_site_loses_nothing(self):
        import http.client
        from format_finder import appstore
        apps = [{"trackId": 1, "trackName": "Fake One", "sellerUrl": "https://shared.example.invalid/"},
                {"trackId": 2, "trackName": "Fake Two", "sellerUrl": "https://shared.example.invalid/"},
                {"trackId": 3, "trackName": "Fake Three", "sellerUrl": "https://broken.example.invalid/"},
                {"trackId": 4, "trackName": "Fake Four"}]
        opened = []

        class Page:
            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                if "broken" in self.url:
                    raise http.client.IncompleteRead(b"abcdef", 5000)
                return b'<a href="https://www.tiktok.com/@fake_shared_brand">t</a>'

        def urlopen(req, timeout=None, context=None):
            opened.append(req.full_url)
            return Page(req.full_url)

        with mock.patch.object(appstore, "_get_json", return_value={"results": apps}), \
                mock.patch.object(appstore.urllib.request, "urlopen", urlopen), contextlib.redirect_stderr(io.StringIO()):
            out = cli.envelope("appstore", "apps", appstore.search, "budget", links=True)
            with mock.patch.object(appstore, "_site_links", side_effect=RuntimeError("fake bug")):
                bug = cli.envelope("appstore", "apps", appstore.search, "budget", links=True)
        self.assertTrue(out["ok"])
        self.assertEqual(opened, ["https://shared.example.invalid/", "https://broken.example.invalid/"])
        links = [a["social_links"] for a in out["result"]]
        self.assertEqual(links[0]["tiktok"], ["fake_shared_brand"])
        self.assertIs(links[0], links[1])
        self.assertTrue(links[2]["error"].startswith("IncompleteRead"))
        self.assertEqual(links[3], {"error": "the App Store lists no developer website"})
        self.assertTrue(bug["ok"])
        self.assertEqual(bug["result"][0]["social_links"], {"error": "RuntimeError: fake bug"})

    def test_whisper_marks_no_speech_and_low_confidence(self):
        from types import SimpleNamespace
        from format_finder import media
        seg = lambda text, logprob, no_speech: SimpleNamespace(start=0.0, end=2.0, text=text, avg_logprob=logprob,
                                                                no_speech_prob=no_speech)
        info = SimpleNamespace(language="en", language_probability=0.677)
        info = SimpleNamespace(language="en", language_probability=0.61)
        for segments, speech, low in (([seg(" La la la.", -1.24, 0.48)] * 2, True, True),
                                      ([seg(" Mm.", -0.3, 0.81)], True, True),  # sure of the words, not of speech
                                      ([seg(" Save $90 this month.", -0.2, 0.01)], True, False), ([], False, None)):
            model = mock.Mock(transcribe=mock.Mock(return_value=(iter(segments), info)))
            with mock.patch.object(media, "probe", return_value={"has_audio": True}), \
                    mock.patch.object(media, "load_audio", return_value=None), \
                    mock.patch.object(media, "_whisper", return_value=model):
                out = media.transcribe("x.mp4", "base")
            self.assertEqual((out["speech"], out["low_confidence"], out["language_probability"]), (speech, low, 0.61))
        self.assertEqual(out["text"], "")

    def test_unchecked_caption_language_is_compared_with_the_audio(self):
        from format_finder import media
        download = {"captions_text": "Це вигадане речення для тесту.", "captions_source": "auto-captions:uk-orig",
                    "captions_language": "uk", "captions_language_checked": False}
        with mock.patch.object(media, "audio_language", return_value=("ru", 0.62)) as detect:
            out = media._transcript(download, "x.mp4", True, [])
        detect.assert_called_once()
        self.assertEqual((out["transcript_language"], out["audio_language"], out["language_mismatch"]), ("uk", "ru", True))
        self.assertIs(out["captions_language_checked"], False)
        with mock.patch.object(media, "audio_language") as detect:
            out = media._transcript(dict(download, captions_language_checked=True), "x.mp4", True, [])
        detect.assert_not_called()
        self.assertNotIn("language_mismatch", out)
        self.assertIs(out["captions_language_checked"], True)  # YouTube's audio language matched: no detection
        with mock.patch.object(media, "audio_language") as detect:
            out = media._transcript(download, "x.mp4", False, [])  # Whisper off: unchecked, and it says so
        detect.assert_not_called()
        self.assertEqual((out["captions_language_checked"], "audio_language" in out), (False, False))
        for caption, heard in (("iw", "he"), ("fil", "tl"), ("jv", "jw")):  # YouTube's codes vs Whisper's
            with mock.patch.object(media, "audio_language", return_value=(heard, 0.9)):
                out = media._transcript(dict(download, captions_language=caption), "x.mp4", True, [])
            self.assertIs(out["language_mismatch"], False, caption)

    def test_whisper_transcript_fields(self):
        from format_finder import media
        heard = {"text": "Made up words.", "segments": [{"start": 0.0, "end": 1.5, "text": "Made up words.",
                                                         "avg_logprob": -0.3, "no_speech_prob": 0.02}],
                 "language": "en", "language_probability": 0.88, "speech": True, "low_confidence": False,
                 "source": "whisper:base"}
        with mock.patch.object(media, "transcribe", return_value=heard) as whisper:
            out = media._transcript({"video_path": "x.mp4", "captions_text": None}, "x.mp4", True, [])
        whisper.assert_called_once_with("x.mp4")
        self.assertEqual(out, {"transcript": "Made up words.", "transcript_source": "whisper:base",
                               "segments": heard["segments"], "transcript_language": "en",
                               "language_probability": 0.88, "speech": True, "low_confidence": False,
                               "captions_language_checked": None})
        with mock.patch.object(media, "transcribe", return_value=None):
            self.assertIs(media._transcript({}, "x.mp4", True, [])["speech"], False)  # no audio stream
        errors = []
        with mock.patch.object(media, "transcribe", side_effect=RuntimeError("fake decode error")), \
                contextlib.redirect_stderr(io.StringIO()):
            out = media._transcript({}, "x.mp4", True, errors)
        self.assertIsNone(out["transcript"])
        self.assertEqual(errors, [{"route": "whisper", "error": "RuntimeError: fake decode error"}])
        with mock.patch.object(media, "transcribe") as whisper:
            media._transcript({}, "x.mp4", False, [])
        whisper.assert_not_called()

    def test_media_fetch_puts_frames_and_transcript_together(self):
        from format_finder import media
        out_dir = Path(tempfile.mkdtemp())
        video = out_dir / "fakeid12345.mp4"
        video.write_bytes(b"fake")
        env = {"route": "yt-dlp", "errors": [], "result": {"video_path": str(video), "captions_text": None,
                                                           "captions_source": None}}
        heard = {"text": "", "segments": [], "language": "en", "language_probability": 0.4, "speech": False,
                 "low_confidence": None, "source": "whisper:base"}
        frames = [{"path": str(out_dir / "frames" / "f1.jpg"), "time_s": 0.5}]
        with mock.patch.object(youtube, "download", return_value=env), \
                mock.patch.object(media, "probe", return_value={"duration_s": 12.345, "has_video": True,
                                                                "has_audio": True}), \
                mock.patch.object(media, "cut_frames", return_value=frames) as cut, \
                mock.patch.object(media, "transcribe", return_value=heard):
            out = media.fetch("youtube", "fakeid12345", str(out_dir), frames=3)
        cut.assert_called_once()
        self.assertEqual((out["ok"], out["command"], out["route"], out["duration_s"], out["frames"], out["speech"]),
                         (True, "media", "yt-dlp", 12.35, frames, False))
        self.assertEqual(out["transcript"], "")
        with mock.patch.object(youtube, "download", side_effect=AllRoutesFailed([{"route": "yt-dlp", "error": "x"}])):
            out = media.fetch("youtube", "fakeid12345", str(out_dir))
        self.assertEqual((out["ok"], out["command"], out["errors"]), (False, "media", [{"route": "yt-dlp", "error": "x"}]))

    def test_tool_subprocesses_never_read_the_callers_input(self):
        """ffmpeg reads keys from stdin while it runs: in a shell `while read` loop it ate the next line's start."""
        from format_finder import media
        done = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(media.subprocess, "run", return_value=done) as run, \
                mock.patch.object(media, "ffmpeg_exe", return_value="ffmpeg"):
            media._run(["-i", "x.mp4"])
            media.load_audio("x.mp4")
        self.assertEqual([c.kwargs.get("stdin") for c in run.call_args_list], [media.subprocess.DEVNULL] * 2)
        script = Path(tempfile.mkdtemp()) / "yt.mjs"
        script.write_text("")
        (script.parent / "node_modules" / "youtubei.js").mkdir(parents=True)
        answer = mock.Mock(returncode=0, stdout='{"ok": true, "result": {"id": "abcdefghijk"}}', stderr="")
        with mock.patch.object(youtube, "NODE_SCRIPT", script), mock.patch.object(youtube, "_node_path", return_value="node"), \
                mock.patch.object(youtube.subprocess, "run", return_value=answer) as run:
            self.assertEqual(youtube._node("video", "abcdefghijk"), {"id": "abcdefghijk"})
        self.assertIs(run.call_args.kwargs.get("stdin"), youtube.subprocess.DEVNULL)

    def test_youtube_js_download_says_whether_the_caption_language_was_checked(self):
        new = {"video_path": "/tmp/x.mp4", "captions_text": "Fake made up caption line.",
               "captions_source": "auto-captions:uk-orig", "captions_language": "uk",
               "captions_language_checked": False}
        old = {k: new[k] for k in ("video_path", "captions_text", "captions_source")}  # an older yt.mjs
        outs = []
        for shape in (new, old, dict(old, captions_text=None, captions_source=None)):
            with mock.patch.object(youtube, "_node", return_value=shape), mock.patch.object(youtube, "_ffmpeg"):
                outs.append(youtube._js_download("abcdefghijk", tempfile.mkdtemp()))
        self.assertEqual([(o["captions_language"], o["captions_language_checked"]) for o in outs],
                         [("uk", False), ("uk", False), (None, None)])
        from format_finder import media
        with mock.patch.object(media, "audio_language", return_value=("ru", 0.9)) as detect:
            heard = media._transcript(outs[1], "x.mp4", True, [])
        detect.assert_called_once()  # the route-2 download gets the same language check as yt-dlp's
        self.assertIs(heard["language_mismatch"], True)

    def test_settings_and_media_say_ok_and_command_like_the_other_commands(self):
        code, out = run_cli("settings")
        self.assertEqual((code, out["ok"], out["command"]), (0, True, "settings"))
        self.assertIn("recent_days", out["settings"])

    def test_check_formats_uses_the_settings(self):
        import tempfile
        rec = {"platform": "youtube", "id": "v1", "author_id": "UC1", "views": 37, "multiple": 18.5,
               "flags": ["meets_multiple"]}
        path = Path(tempfile.mkdtemp()) / "groups.json"
        path.write_text(json.dumps({"f": [rec, dict(rec, id="v2", author_id="UC2", views=90_000)]}))
        code, out = run_cli("check-formats", "--groups", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(out["settings"]["outlier_views_min"], settings.get("outlier_views_min"))
        f = out["formats"]["f"]
        self.assertEqual((f["videos"], f["videos_counted"], f["accounts"]), (2, 1, 1))
        self.assertEqual(f["not_counted"][0]["reasons"], ["low_views"])



class InstagramPages(unittest.TestCase):
    def test_later_page_failure_keeps_earlier_reels(self):
        import asyncio
        from format_finder import instagram

        async def fetch(cursor, prev):
            if cursor:
                raise RuntimeError("HTTP 500 on page 2")
            return {"items": [{"pk": "1", "media_type": 2, "code": "a", "taken_at": 1}], "next_max_id": "c2", "more_available": True}

        with contextlib.redirect_stderr(io.StringIO()):
            nodes, _ = asyncio.run(instagram._collect(fetch, 30, route="instagrapi"))
        self.assertEqual([n["pk"] for n in nodes], ["1"])

        async def first_fails(cursor, prev):
            raise RuntimeError("HTTP 500 on page 1")

        with self.assertRaises(RuntimeError):
            asyncio.run(instagram._collect(first_fails, 30, route="instagrapi"))


if __name__ == "__main__":
    unittest.main()
