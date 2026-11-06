"""Offline tests for browser.py: finding a browser, the profile lock, the DevTools port file, browser-login's
command line. No browser is started and nothing touches the network. Run: python -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
import tempfile as _tempfile
from pathlib import Path as _Path
from unittest import mock as _mock

from format_finder import settings as _ff_settings
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from format_finder import __main__ as cli, browser  # noqa: E402
from format_finder.common import NotConfigured, RouteError  # noqa: E402

CFT_MAC = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def fake_exe(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


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


class Discovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home, self.ff, self.root = self.tmp / "home", self.tmp / "ffhome", self.tmp / "root"
        for d in (self.home, self.ff, self.root):
            d.mkdir()

    def find(self, system, configured=None, environ=None, which=lambda name: None):
        return browser.find_browser(configured, system=system, home=self.home, environ=environ or {},
                                    which=which, ff_home=self.ff, root=self.root)

    def test_mac_order_setting_then_download_then_installed_then_playwright(self):
        cache = self.home / "Library/Caches/ms-playwright"
        old_cft = fake_exe(cache / "chromium-1223/chrome-mac-x64" / CFT_MAC)
        new_cft = fake_exe(cache / "chromium-1247/chrome-mac-x64" / CFT_MAC)
        fake_exe(cache / "chromium_headless_shell-1247/chrome-headless-shell-mac-x64/chrome-headless-shell")
        self.assertEqual(self.find("Darwin"), str(new_cft))
        self.assertNotEqual(self.find("Darwin"), str(old_cft))

        edge = fake_exe(self.root / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        self.assertEqual(self.find("Darwin"), str(edge))
        brave = fake_exe(self.home / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        self.assertEqual(self.find("Darwin"), str(brave))
        chromium = fake_exe(self.root / "Applications/Chromium.app/Contents/MacOS/Chromium")
        self.assertEqual(self.find("Darwin"), str(chromium))
        chrome = fake_exe(self.root / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        self.assertEqual(self.find("Darwin"), str(chrome))

        downloaded = fake_exe(self.ff / "browser/chrome/mac_arm-140.0.1/chrome-mac-arm64" / CFT_MAC)
        self.assertEqual(self.find("Darwin"), str(downloaded))

        own = fake_exe(self.tmp / "my browser/chrome")
        self.assertEqual(self.find("Darwin", configured=str(own)), str(own))

    def test_linux_commands_paths_and_playwright(self):
        cft = fake_exe(self.home / ".cache/ms-playwright/chromium-1247/chrome-linux64/chrome")
        self.assertEqual(self.find("Linux"), str(cft))
        snap = fake_exe(self.root / "snap/bin/chromium")
        self.assertEqual(self.find("Linux"), str(snap))
        brave = fake_exe(self.tmp / "bin/brave-browser")
        self.assertEqual(self.find("Linux", which=lambda n: str(brave) if n == "brave-browser" else None), str(snap))
        chrome = fake_exe(self.tmp / "bin/google-chrome")
        found = self.find("Linux", which=lambda n: {"brave-browser": str(brave), "google-chrome": str(chrome)}.get(n))
        self.assertEqual(found, str(chrome))

    def test_windows_program_folders_and_playwright_env(self):
        local = self.tmp / "Local"
        env = {"LOCALAPPDATA": str(local), "PROGRAMFILES": str(self.tmp / "Program Files")}
        cft = fake_exe(local / "ms-playwright/chromium-1247/chrome-win64/chrome.exe")
        self.assertEqual(self.find("Windows", environ=env), str(cft))
        edge = fake_exe(self.tmp / "Program Files/Microsoft/Edge/Application/msedge.exe")
        self.assertEqual(self.find("Windows", environ=env), str(edge))
        chrome = fake_exe(local / "Google/Chrome/Application/chrome.exe")
        self.assertEqual(self.find("Windows", environ=env), str(chrome))
        own_cache = fake_exe(self.tmp / "pw/chromium-1300/chrome-win/chrome.exe")
        cands = browser.candidates(system="Windows", home=self.home, ff_home=self.ff, root=self.root,
                                   environ={"PLAYWRIGHT_BROWSERS_PATH": str(self.tmp / "pw")})
        self.assertIn(own_cache, [p for _, p in cands])

    def test_nothing_found_says_run_setup(self):
        with self.assertRaises(NotConfigured) as ctx:
            self.find("Darwin")
        self.assertIn("/format-finder:setup", str(ctx.exception))

    def test_bad_setting_is_named(self):
        with self.assertRaises(NotConfigured) as ctx:
            self.find("Darwin", configured=str(self.tmp / "missing/chrome"))
        self.assertIn("browser_path", str(ctx.exception))


    def test_only_downloaded_env_ignores_installed_browsers(self):
        fake_exe(self.root / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        fake_exe(self.home / "Library/Caches/ms-playwright/chromium-1247/chrome-mac-x64" / CFT_MAC)
        only = {browser.ONLY_DOWNLOADED_ENV: "1"}
        with self.assertRaises(NotConfigured):
            self.find("Darwin", environ=only)
        downloaded = fake_exe(self.ff / "browser/chrome/mac-142.0.7444.59/chrome-mac-x64" / CFT_MAC)
        self.assertEqual(self.find("Darwin", environ=only), str(downloaded))
        self.assertEqual(browser.locate(None, system="Darwin", home=self.home, environ=only, ff_home=self.ff,
                                        root=self.root), ("downloaded by setup", str(downloaded)))
        self.assertEqual(self.find("Darwin", environ={browser.ONLY_DOWNLOADED_ENV: "0"}), str(downloaded))

    def test_locate_names_where_it_was_found_and_version(self):
        exe = self.tmp / "my-chrome"
        exe.write_text("#!/bin/sh\necho 'Google Chrome for Testing 142.0.7444.59 '\n")
        exe.chmod(0o755)
        self.assertEqual(browser.locate(str(exe)), ("browser_path setting", str(exe)))
        self.assertEqual(browser.version(exe), "Google Chrome for Testing 142.0.7444.59")
        self.assertIsNone(browser.version(self.tmp / "missing"))
        brave = fake_exe(self.root / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        self.assertEqual(browser.locate(None, system="Darwin", home=self.home, environ={}, ff_home=self.ff,
                                        root=self.root), ("Brave", str(brave)))

    def test_captcha_reasons(self):
        self.assertTrue(browser.is_captcha("a captcha (captcha-verify-container)"))
        self.assertTrue(browser.is_captcha('a bot check ("Drag the slider to fit the puzzle")'))
        self.assertFalse(browser.is_captcha("a login wall (the site asks to log in)"))
        self.assertFalse(browser.is_captcha(None))
        self.assertFalse(browser.BotCheck("x").captcha)
        self.assertTrue(browser.BotCheck("x", captcha=True).captcha)


class LaunchPieces(unittest.TestCase):
    def test_devtools_port_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "DevToolsActivePort"
            self.assertIsNone(browser.devtools_url(f))
            f.write_text("53412\n")
            self.assertIsNone(browser.devtools_url(f))  # still being written
            f.write_text("53412\n/devtools/browser/0b1c-fake\n")
            self.assertEqual(browser.devtools_url(f), "ws://127.0.0.1:53412/devtools/browser/0b1c-fake")

    def test_headless_args_use_the_dedicated_profile_and_localhost(self):
        b = browser.Browser(exe="/x/chrome", profile="/tmp/ff-profile")
        args = b.args()
        self.assertIn("--user-data-dir=/tmp/ff-profile", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--remote-debugging-port=0", args)
        self.assertIn("--headless=new", args)
        for flag in ("--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                     "--disable-backgrounding-occluded-windows"):  # keep the headless page visible (drawing frames)
            self.assertIn(flag, args)
        visible = browser.Browser(exe="/x/chrome", profile="/tmp/ff-profile", headless=False).args("https://a.test/")
        self.assertNotIn("--headless=new", visible)
        self.assertEqual(visible[-1], "https://a.test/")

    def test_bot_check_header(self):
        self.assertEqual(browser.bot_check_kind('{"code":"10000","type":"verify","subtype":"whirl"}'), "verify/whirl")
        self.assertEqual(browser.bot_check_kind("not json"), "captcha")

    def test_headers_drop_set_cookie(self):
        self.assertEqual(browser._headers({"headers": {"Set-Cookie": "a=b", "Content-Type": "x"}}),
                         {"content-type": "x"})


class FakeCdp:
    """A DevTools connection that plays scripted events and answers commands from a page stand-in."""

    def __init__(self, script, page):
        self.events, self.script, self.page, self.sent = deque(), deque(script), page, []

    def drain(self):
        self.events.clear()

    def send(self, method, params=None, session=None, timeout=30):
        self.sent.append((method, params))
        if method == "Page.navigate":
            return {} if params["url"] == "about:blank" else {"frameId": "F", "loaderId": "L1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": self.page(params["expression"])}}
        if method == "Network.getResponseBody":
            return {"body": '{"itemList": [1]}', "base64Encoded": False}
        if method == "Input.dispatchMouseEvent" and self.stuck_wheel:
            raise RouteError(f"the browser did not answer {method} within {timeout}s")
        return {}

    stuck_wheel = False

    def event(self, timeout):
        if self.events:
            return self.events.popleft()
        if self.script:
            return self.script.popleft()
        time.sleep(min(timeout, 0.01))
        return None


def doc(loader, frame="F", url="https://www.tiktok.com/@someone"):
    return {"method": "Network.responseReceived", "sessionId": "S",
            "params": {"type": "Document", "loaderId": loader, "frameId": frame, "requestId": f"D-{loader}",
                       "response": {"url": url, "status": 200, "headers": {}}}}


def loaded(loader):
    return {"method": "Page.lifecycleEvent", "sessionId": "S",
            "params": {"name": "DOMContentLoaded", "loaderId": loader, "frameId": "F"}}


class Capture(unittest.TestCase):
    """Tab.capture against a fake DevTools connection: no browser starts."""

    def tab(self, script, data_by_loader, blocked=None):
        tab_ref = {}

        def page(expression):
            if "getElementById" in expression:
                data = data_by_loader.get(tab_ref["tab"].loader)
                return json.dumps(data) if data is not None else None
            if "captcha" in expression:
                return blocked
            if expression == "location.href":
                return "https://www.tiktok.com/@someone"
            if "readyState" in expression:
                return ["loading", "https://www.tiktok.com/@someone"]
            return True

        fake_browser = types.SimpleNamespace(cdp=FakeCdp(script, page), alive=lambda: True)
        tab_ref["tab"] = browser.Tab(fake_browser, "T", "S")
        return tab_ref["tab"]

    def test_a_page_that_reloads_itself_is_read_from_its_new_document(self):
        api = {"method": "Network.responseReceived", "sessionId": "S",
               "params": {"type": "Fetch", "loaderId": "L2", "requestId": "R1",
                          "response": {"url": "https://www.tiktok.com/api/post/item_list/?x=1", "status": 200,
                                       "headers": {}}}}
        finished = {"method": "Network.loadingFinished", "sessionId": "S", "params": {"requestId": "R1"}}
        script = [doc("L1"), loaded("L1"), doc("IFRAME-1", frame="other"), doc("L2"), loaded("L2"), api, finished]
        tab = self.tab(script, {"L2": {"__DEFAULT_SCOPE__": {"webapp.user-detail": {}}}})
        seen, replies = [], []
        summary = tab.capture("https://www.tiktok.com/@someone", pattern=r"^/api/post/item_list/",
                              on_reply=lambda r: replies.append(r) or True, embedded=browser.UNIVERSAL_DATA,
                              on_embedded=lambda d: seen.append(d) or False, timeout=5, idle=0.2)
        self.assertEqual(seen, [{"__DEFAULT_SCOPE__": {"webapp.user-detail": {}}}])  # only the second document's
        self.assertEqual([r["body"] for r in replies], ['{"itemList": [1]}'])
        self.assertEqual((summary["documents"], summary["embedded"], summary["replies"]), (2, True, 1))
        self.assertEqual(tab.loader, "L2")  # an iframe's document is not followed

    def test_no_data_and_no_new_document_ends_after_idle_without_calling_back(self):
        tab = self.tab([doc("L1"), loaded("L1")], {}, blocked='a bot check ("Drag the slider to fit the puzzle")')
        seen = []
        summary = tab.capture("https://www.tiktok.com/@/video/1", embedded=browser.UNIVERSAL_DATA,
                              on_embedded=lambda d: seen.append(d) or True, timeout=5, idle=0.2)
        self.assertEqual(seen, [])
        self.assertFalse(summary["embedded"])
        self.assertFalse(summary["timed_out"])
        self.assertTrue(browser.is_captcha(summary["blocked"]))
        self.assertLess(summary["seconds"], 2)


    def test_a_scroll_that_is_never_answered_ends_the_scrolling_not_the_read(self):
        tab = self.tab([doc("L1"), loaded("L1")], {"L1": {"__DEFAULT_SCOPE__": {}}})
        tab.browser.cdp.stuck_wheel = True
        with contextlib.redirect_stderr(io.StringIO()):
            summary = tab.capture("https://www.tiktok.com/@someone", pattern=r"^/api/post/item_list/",
                                  on_reply=lambda r: True, embedded=browser.UNIVERSAL_DATA,
                                  on_embedded=lambda d: False, timeout=5, idle=0.1, max_scrolls=8)
        self.assertIn("Input.dispatchMouseEvent", summary["scroll_error"])
        self.assertEqual(summary["scrolls"], 0)
        self.assertFalse(summary["timed_out"])
        self.assertTrue(summary["embedded"])
        wheel = [p for m, p in tab.browser.cdp.sent if m == "Input.dispatchMouseEvent"]
        self.assertEqual(len(wheel), 1)


class Lock(unittest.TestCase):
    def test_second_holder_waits_then_fails_naming_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path, owner = Path(tmp) / "p.lock", Path(tmp) / "p.owner"
            first = browser.ProfileLock(lock_path, owner).acquire(wait=0)
            try:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(RouteError) as ctx:
                    browser.ProfileLock(lock_path, owner).acquire(wait=0.6)
                self.assertIn("in use by another format-finder command", str(ctx.exception))
                self.assertIn(f"pid {os.getpid()}", str(ctx.exception))
            finally:
                first.release()
            again = browser.ProfileLock(lock_path, owner).acquire(wait=0)
            again.release()


class LoginCommand(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_unknown_site_is_a_usage_error(self):
        code, out = self.run_cli("browser-login", "myspace")
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])

    def test_answers_json_without_secrets(self):
        seen = {}

        def fake(site, url=None, timeout=900):
            seen.update(site=site, url=url, timeout=timeout)
            return {"site": site, "opened": url or browser.LOGIN_PAGES[site], "closed_by": "timeout",
                    "seconds_open": 0.1, "browser": "/x/chrome", "profile": "/p"}

        with mock.patch.object(browser, "login_window", fake):
            code, out = self.run_cli("browser-login", "tiktok", "--url", "about:blank", "--timeout", "0")
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"site": "tiktok", "url": "about:blank", "timeout": 0})
        self.assertTrue(out["ok"])
        self.assertEqual(out["command"], "browser-login")
        self.assertIn("never reads", out["note"])

    def test_login_pages(self):
        self.assertEqual(browser.LOGIN_PAGES["tiktok"], "https://www.tiktok.com/login")
        with self.assertRaises(ValueError):
            browser.login_window("myspace")


if __name__ == "__main__":
    unittest.main()
