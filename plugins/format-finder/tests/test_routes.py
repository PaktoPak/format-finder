"""Offline tests for route memory in common.try_routes: within one process, a route that keeps failing (or shows a
captcha) for an operation is skipped while another route remains. Run: python -m unittest discover -s tests"""
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

from format_finder import browser, common  # noqa: E402
from format_finder.browser import BotCheck  # noqa: E402
from format_finder.common import AllRoutesFailed, NotConfigured, RouteError, try_routes  # noqa: E402


class Route:
    """A route that answers from a script: each call takes the next outcome (an exception is raised)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def captcha():
    return BotCheck("TikTok asked for a captcha (verify/slide)", captcha=True)


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


class Memory(unittest.TestCase):
    def setUp(self):
        common.reset_route_memory()
        self.addCleanup(common.reset_route_memory)
        self.err = io.StringIO()
        redirect = contextlib.redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def call(self, routes, op="tiktok.search"):
        try:
            return try_routes(routes, "meal prep", limit=1, _op=op)
        except AllRoutesFailed as exc:
            return exc

    def test_two_failures_in_a_row_skip_the_route_while_another_remains(self):
        bad, good = Route(RouteError("refused")), Route(["v1"])
        routes = [("omkarcloud", bad), ("browser", good)]
        for _ in range(2):
            env = self.call(routes)
            self.assertEqual((env["route"], env["result"]), ("browser", ["v1"]))
            self.assertEqual([e["route"] for e in env["errors"]], ["omkarcloud"])
        env = self.call(routes)
        self.assertEqual(len(bad.calls), 2)
        self.assertEqual(len(good.calls), 3)
        self.assertEqual(env["errors"], [])
        self.assertEqual(env["skipped_routes"], ["omkarcloud"])
        self.call(routes)
        skips = common.route_skips()
        self.assertEqual(len(skips), 1)
        keys = ("op", "route", "captcha", "failed_calls", "skipped_calls", "active")
        self.assertEqual({k: skips[0][k] for k in keys},
                         {"op": "tiktok.search", "route": "omkarcloud", "captcha": False, "failed_calls": 2,
                          "skipped_calls": 2, "active": True})
        self.assertIn("refused", skips[0]["reason"])
        self.assertEqual(self.err.getvalue().count("skipping the omkarcloud route"), 1)  # logged once

    def test_a_result_resets_the_count_and_an_empty_one_changes_nothing(self):
        flaky = Route(RouteError("x"), ["v"], RouteError("x"), [], RouteError("x"))
        routes = [("omkarcloud", flaky), ("browser", Route(["b"]))]
        for _ in range(4):
            self.call(routes)
        self.assertEqual(common.route_skips(), [])  # fail, result (reset), fail, empty
        self.call(routes)  # the 2nd failure in a row: the empty result between them did not reset the count
        self.assertEqual(len(common.route_skips()), 1)
        self.call(routes)
        self.assertEqual(len(flaky.calls), 5)

    def test_a_captcha_skips_the_route_at_once(self):
        omkar, browser = Route(RouteError("bot-check page")), Route(captcha())
        routes = [("omkarcloud", omkar), ("dtk", Route(NotConfigured("dtk_url is not set"))), ("browser", browser)]
        first = self.call(routes)
        self.assertIsInstance(first, AllRoutesFailed)
        self.assertEqual([e["route"] for e in first.errors], ["omkarcloud", "dtk", "browser"])
        second = self.call(routes)
        self.assertEqual(len(browser.calls), 1)  # skipped after one captcha
        self.assertEqual([e["route"] for e in second.errors], ["omkarcloud", "dtk"])
        self.assertEqual(second.skipped, ["browser"])
        skip = common.route_skips()[0]
        self.assertEqual((skip["route"], skip["captcha"], skip["failed_calls"]), ("browser", True, 1))
        self.assertIn("captcha", self.err.getvalue())

    def test_when_every_route_is_skipped_failed_ones_are_tried_again_but_not_a_captcha(self):
        omkar, browser = Route(RouteError("refused"), RouteError("refused"), ["v"]), Route(captcha())
        routes = [("browser", browser), ("omkarcloud", omkar)]
        self.call(routes)
        self.call(routes)  # omkarcloud: 2nd failure in a row, now skipped too
        self.assertEqual({s["route"] for s in common.route_skips()}, {"omkarcloud", "browser"})
        env = self.call(routes)  # nothing else remains, so omkarcloud is tried again; the captcha route is not
        self.assertEqual(env["route"], "omkarcloud")
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(env["skipped_routes"], ["browser"])
        self.assertFalse(next(s for s in common.route_skips() if s["route"] == "omkarcloud")["active"])
        self.assertIn("answered again", self.err.getvalue())

    def test_only_captcha_routes_left_fails_without_a_request(self):
        browser = Route(captcha())
        self.call([("browser", browser)])
        exc = self.call([("browser", browser)])
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(exc.skipped, ["browser"])
        self.assertTrue(exc.errors[0]["error"].startswith("RouteSkipped: captcha"))

    def test_other_operations_and_calls_without_op_are_not_affected(self):
        browser = Route(captcha(), {"posts": [1], "followers": 5})
        self.call([("browser", browser)], op="tiktok.search")
        self.assertEqual(self.call([("browser", browser)], op="tiktok.account")["route"], "browser")
        plain = Route(captcha())
        for _ in range(3):
            with self.assertRaises(AllRoutesFailed):
                try_routes([("browser", plain)], "x")
        self.assertEqual(len(plain.calls), 3)
        self.assertEqual([s["op"] for s in common.route_skips()], ["tiktok.search"])

    def test_op_is_not_passed_to_routes_and_reset_forgets(self):
        route = Route(RouteError("x"))
        routes = [("a", route), ("b", Route(["ok"]))]
        self.call(routes)
        self.assertEqual(route.calls[0], (("meal prep",), {"limit": 1}))
        self.call(routes)
        common.reset_route_memory()
        self.call(routes)
        self.assertEqual(len(route.calls), 3)
        self.assertEqual(common.route_skips(), [])

    def test_a_login_wall_or_empty_reply_counts_as_a_failure_not_a_captcha(self):
        browser = Route(BotCheck("TikTok sent an empty search reply"))
        routes = [("browser", browser), ("omkarcloud", Route(["v"]))]
        self.call(routes)
        self.assertEqual(common.route_skips(), [])
        self.call(routes)
        self.assertEqual(common.route_skips()[0]["captcha"], False)
        self.call(routes)
        self.assertEqual(len(browser.calls), 2)

    def test_an_empty_answer_is_marked_and_not_found_is_no_route_failure(self):
        env = try_routes([("a", Route({"posts": [], "followers": None})), ("b", Route({"posts": [], "followers": 0}))])
        self.assertEqual((env["route"], env["empty"]), ("b", True))
        self.assertNotIn("empty", try_routes([("a", Route({"posts": [{"id": 1}]}))]))
        missing = Route(common.NotFound("no profile @x"))
        for _ in range(3):
            with self.assertRaises(AllRoutesFailed):
                try_routes([("browser", missing)], "x", _op="instagram.account")
        self.assertEqual(len(missing.calls), 3)
        self.assertEqual(common.route_skips(), [])

    def test_not_found_stops_the_later_routes(self):
        first, later = Route(common.NotFound("no profile @x")), Route({"posts": [{"id": 1}]})
        for _ in range(3):
            with self.assertRaises(AllRoutesFailed) as ctx:
                try_routes([("omkarcloud", first), ("browser", later)], "x", _op="tiktok.account")
        self.assertEqual((len(first.calls), len(later.calls)), (3, 0))
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["omkarcloud"])
        self.assertEqual(common.route_skips(), [])
        failing = Route(RuntimeError("down"))
        env = try_routes([("a", failing), ("b", Route({"posts": [], "followers": None})), ("c", first),
                          ("d", later)], "x")
        self.assertEqual((env["route"], env["empty"], len(later.calls)), ("b", True, 0))

    def test_a_not_found_that_is_not_final_asks_the_later_routes(self):
        unsure, later = Route(common.NotFound("no profile @x, or no data", final=False)), Route({"posts": [{"id": 1}]})
        for _ in range(3):
            env = try_routes([("omkarcloud", unsure), ("browser", later)], "x", _op="tiktok.account")
        self.assertEqual((env["route"], len(unsure.calls), len(later.calls)), ("browser", 3, 3))
        self.assertEqual(common.route_skips(), [])  # not counted as a route failure
        with self.assertRaises(AllRoutesFailed) as ctx:
            try_routes([("omkarcloud", unsure), ("browser", Route(common.NotFound("no profile @x")))], "x")
        self.assertEqual([e["route"] for e in ctx.exception.errors], ["omkarcloud", "browser"])


if __name__ == "__main__":
    unittest.main()
