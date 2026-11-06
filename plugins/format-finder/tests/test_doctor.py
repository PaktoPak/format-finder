"""Offline tests for doctor's browser checks and its TikTok summary. No browser starts and nothing touches the
network. Run: python -m unittest discover -s tests"""
import contextlib
import io
import sys
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import appstore, browser, common, doctor, tiktok  # noqa: E402

READY = {"python:yt_dlp": True, "node": True, "youtubei.js": True, "yt.mjs": True, "omkarcloud": True,
         "dtk": False, "tiktok_proxy": False, "python:instagrapi": True, "python:aiograpi": True,
         "instagram session": False, "browser": True, "python:websockets": True}


def live(route, check, ok, detail="", captcha=False):
    row = {"platform": "tiktok", "route": route, "check": check, "ok": ok, "detail": detail, "seconds": 1.0}
    if captcha:
        row["captcha"] = True
    return row



def _isolate_settings():
    """The user's own ~/.format-finder/settings.json must not change what the tests see."""
    own = _mock.patch.object(_ff_settings, "USER_PATH", _Path(_tempfile.mkdtemp()) / "settings.json")
    own.start()
    unittest.addModuleCleanup(own.stop)


def setUpModule():
    _isolate_settings()


class TikTokSummary(unittest.TestCase):
    def tiktok(self, by_name=READY, results=None):
        return doctor._platforms(dict(by_name), results)["tiktok"]

    def test_not_checked_without_live(self):
        tk = self.tiktok()
        self.assertIsNone(tk["search_via_browser"])
        self.assertTrue(tk["routes"]["browser"])
        self.assertIn("browser route: not checked; run doctor --live", " ".join(tk["notes"]))

    def test_captcha_says_puzzle_and_browser_login(self):
        tk = self.tiktok(results=[
            live("omkarcloud", "profile @tiktok, 1 post", True), live("omkarcloud", "search 'meal prep'", False),
            live("browser", "profile @tiktok, 1 post", True),
            live("browser", "search 'meal prep', 1 result, any date", False, "BotCheck: captcha", captcha=True)])
        self.assertIs(tk["search"], False)
        self.assertIs(tk["search_via_browser"], False)
        notes = " ".join(tk["notes"])
        self.assertIn("does not work right now: TikTok is asking this IP for a puzzle; try later, or run "
                      f"{common.TERMINAL_COMMAND} browser-login tiktok in your own terminal", notes)
        self.assertIn("untested", notes)
        self.assertIn("No TikTok keyword search works right now", notes)
        self.assertTrue(tk["routes"]["browser"])  # the profile read worked
        self.assertFalse(any("could not read a profile" in n for n in tk["notes"]))
        tk = self.tiktok(results=[live("browser", "profile @tiktok, 1 post", False, "BotCheck: a captcha",
                                       captcha=True)])
        self.assertFalse(tk["routes"]["browser"])
        self.assertIn("the browser route could not read a profile right now: BotCheck: a captcha", tk["notes"])

    def test_other_failure_is_quoted_and_success_is_plain(self):
        tk = self.tiktok(results=[live("omkarcloud", "search 'meal prep'", False),
                                  live("browser", "search 'meal prep'", False, "BotCheck: TikTok sent an empty reply")])
        self.assertIn("does not work right now: BotCheck: TikTok sent an empty reply", " ".join(tk["notes"]))
        tk = self.tiktok(results=[live("omkarcloud", "search 'meal prep'", False),
                                  live("browser", "search 'meal prep'", True, "1 result(s)")])
        self.assertIs(tk["search"], True)
        self.assertIn("keyword search through the browser route works right now (live check)", tk["notes"])

    def test_browser_off_or_missing(self):
        with mock.patch.object(doctor.settings, "get", return_value=False):
            tk = self.tiktok(dict(READY, browser=False))
        self.assertFalse(tk["routes"]["browser"])
        self.assertIn("off (browser_route is false)", " ".join(tk["notes"]))
        with mock.patch.object(doctor.settings, "get", return_value=True):
            tk = self.tiktok(dict(READY, browser=False))
        self.assertIn("no browser found", " ".join(tk["notes"]))


class Checks(unittest.TestCase):
    def test_browser_check_names_path_and_version_or_none(self):
        with mock.patch.object(browser, "locate", return_value=("Brave", "/x/Brave Browser")), \
                mock.patch.object(browser, "version", return_value="Brave Browser 154.1.96.59"), \
                mock.patch.object(doctor.settings, "get", return_value=True):
            self.assertEqual(doctor._browser(), "Brave: /x/Brave Browser (Brave Browser 154.1.96.59)")
        with mock.patch.object(browser, "locate", side_effect=browser.NotConfigured("no Chrome ... was found")):
            checks = []
            doctor._check(checks, "browser", doctor._browser, required=False)
        self.assertEqual((checks[0]["ok"], checks[0]["required"]), (False, False))
        self.assertIn("was found", checks[0]["detail"])

    def test_profile_check_only_looks_at_the_folder(self):
        with mock.patch.object(browser, "PROFILE", Path("/nonexistent/browser-profile")):
            with self.assertRaises(RuntimeError) as ctx:
                doctor._browser_profile()
        self.assertIn("does not exist yet", str(ctx.exception))

    def test_live_marks_a_captcha(self):
        def blocked(*a, **k):
            raise browser.BotCheck("TikTok asked for a captcha", captcha=True)

        results = []
        with contextlib.redirect_stderr(io.StringIO()):
            doctor._live(results, "tiktok", "browser", "search", blocked)
            doctor._live(results, "tiktok", "browser", "profile", lambda: {"posts": [1], "username": "u"})
        self.assertTrue(results[0]["captcha"])
        self.assertNotIn("captcha", results[1])

    def test_live_browser_checks_are_one_profile_and_one_search_then_the_browser_closes(self):
        calls = []
        with mock.patch.object(tiktok, "_browser_account", lambda *a, **k: calls.append(("account", a, k)) or
                               {"posts": [1], "username": "tiktok"}), \
                mock.patch.object(tiktok, "_browser_search", lambda *a, **k: calls.append(("search", a, k)) or [{}]), \
                mock.patch.object(browser, "shutdown") as shutdown, \
                mock.patch.object(appstore, "search", return_value={"result": [{"name": "x"}], "errors": []}), \
                mock.patch.object(doctor, "pause"), contextlib.redirect_stderr(io.StringIO()):
            off = {k: False for k in READY}
            results = doctor._live_checks(dict(off, browser=True, **{"python:websockets": True}))
        self.assertEqual([c[0] for c in calls], ["account", "search"])
        self.assertEqual(calls[1][2], {"days": None, "limit": 1})
        shutdown.assert_called_once()
        self.assertEqual([r["route"] for r in results if r["platform"] == "tiktok" and r["ok"]], ["browser", "browser"])
        self.assertEqual([r["ok"] for r in results if r["platform"] == "appstore"], [True])  # the stand-in


if __name__ == "__main__":
    unittest.main()
