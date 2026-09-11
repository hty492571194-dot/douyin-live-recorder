"""扫码登录链路测试。

重点覆盖这次修掉的三个真实故障:
  1. 匿名访客也会下发的 passport_csrf_token 被误判为"已登录"
     → 页面一打开就结束等待、立即关窗,用户根本看不到二维码;
     同时一串匿名 Cookie 被写进钥匙串。
  2. 只 goto 首页(实际跳转 /jingxuan 信息流),从不点击「登录」
     → 二维码压根不渲染。
  3. 强制 ctx.close() 关窗 —— 用户看到的就是"几秒后自动消失"。
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402


ANON_COOKIES = [
    {"name": "passport_csrf_token", "value": "a" * 32, "expires": -1},
    {"name": "ttwid", "value": "1|abc", "expires": -1},
    {"name": "UIFID_TEMP", "value": "x" * 40, "expires": -1},
    {"name": "enter_pc_once", "value": "1", "expires": -1},
]

LOGGED_IN_COOKIES = ANON_COOKIES + [
    {"name": "sessionid", "value": "deadbeef", "expires": -1},
    {"name": "sessionid_ss", "value": "deadbeef", "expires": -1},
]


class TestLoginDetection(unittest.TestCase):
    """登录判定:匿名 Cookie 绝不能被当成已登录。"""

    def test_anonymous_csrf_token_is_not_login(self):
        """回归用例:只有 passport_csrf_token 时必须判为未登录。

        这条是"窗口弹出几秒就自动关闭"的直接成因 —— 抖音给任何访客
        都下发该字段,一打开页面就命中旧判据,于是立刻收尾关窗。
        """
        self.assertFalse(auth._has_login(ANON_COOKIES))

    def test_only_csrf_token_singular(self):
        self.assertFalse(auth._has_login(
            [{"name": "passport_csrf_token", "value": "x" * 32}]))

    def test_sessionid_counts_as_login(self):
        self.assertTrue(auth._has_login(LOGGED_IN_COOKIES))

    def test_sessionid_alone_counts(self):
        self.assertTrue(auth._has_login([{"name": "sessionid", "value": "a"}]))

    def test_sessionid_ss_counts(self):
        self.assertTrue(auth._has_login([{"name": "sessionid_ss", "value": "a"}]))

    def test_sid_guard_counts(self):
        self.assertTrue(auth._has_login([{"name": "sid_guard", "value": "a"}]))

    def test_empty_value_does_not_count(self):
        self.assertFalse(auth._has_login([{"name": "sessionid", "value": ""}]))

    def test_empty_list(self):
        self.assertFalse(auth._has_login([]))
        self.assertFalse(auth._has_login(None))

    def test_csrf_token_not_in_session_keys(self):
        self.assertNotIn("passport_csrf_token", auth._SESSION_KEYS)


class TestResolveCookie(unittest.TestCase):
    """钥匙串里存了匿名 Cookie 时,不能拿去打 API。"""

    def setUp(self):
        self.orig = auth._keychain_get
        auth._keychain_get = lambda: "enter_pc_once=1; UIFID_TEMP=abc"

    def tearDown(self):
        auth._keychain_get = self.orig

    def test_anonymous_keychain_cookie_is_ignored(self):
        det = {"cookie_ref": "keychain:douyin-cookie"}
        self.assertEqual(auth.resolve_cookie(det), "")

    def test_valid_keychain_cookie_is_used(self):
        auth._keychain_get = lambda: "sessionid=abc; ttwid=1"
        det = {"cookie_ref": "keychain:douyin-cookie"}
        self.assertEqual(auth.resolve_cookie(det), "sessionid=abc; ttwid=1")

    def test_manual_cookie_still_works(self):
        det = {"cookie": "sessionid=manual"}
        self.assertEqual(auth.resolve_cookie(det), "sessionid=manual")


class TestBrowserDetection(unittest.TestCase):
    """跨平台 Chrome 探测。"""

    def test_candidates_non_empty_on_all_platforms(self):
        orig = auth.platform.system
        try:
            for sysname in ("Darwin", "Windows", "Linux"):
                auth.platform.system = lambda s=sysname: s
                cands = auth._browser_candidates()
                self.assertTrue(len(cands) >= 3,
                                f"{sysname} 候选浏览器过少: {cands}")
                for p, name in cands:
                    self.assertIsInstance(p, str)
                    self.assertTrue(name)
        finally:
            auth.platform.system = orig

    def test_darwin_prefers_google_chrome(self):
        orig = auth.platform.system
        try:
            auth.platform.system = lambda: "Darwin"
            cands = auth._browser_candidates()
            self.assertIn("Google Chrome", [n for _, n in cands[:1]] or
                          [n for _, n in cands])
            self.assertTrue(any("Google Chrome.app" in p for p, _ in cands))
        finally:
            auth.platform.system = orig

    def test_windows_includes_edge_and_chrome(self):
        orig = auth.platform.system
        try:
            auth.platform.system = lambda: "Windows"
            names = {n for _, n in auth._browser_candidates()}
            self.assertIn("Google Chrome", names)
            self.assertIn("Microsoft Edge", names)
        finally:
            auth.platform.system = orig

    def test_find_browser_returns_existing_or_empty(self):
        p, name = auth.find_browser()
        if p:
            self.assertTrue(os.path.exists(p), f"探测到的浏览器不存在: {p}")
            self.assertTrue(name)
        else:
            self.assertEqual(name, "")

    def test_free_port_is_bindable(self):
        import socket
        port = auth._free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()

    def test_wait_cdp_times_out_on_dead_port(self):
        port = auth._free_port()
        t = time.time()
        self.assertEqual(auth._wait_cdp(port, timeout=1), "")
        self.assertLess(time.time() - t, 5)


class TestManualCookieFallback(unittest.TestCase):
    """浏览器读不到 Cookie 时的手动兜底。"""

    def setUp(self):
        self.orig_set = auth._keychain_set
        self.orig_meta = auth._save_meta
        self.orig_load = auth._load_meta
        self.written = []
        auth._keychain_set = lambda v: (self.written.append(v), True, "")[1:]
        auth._save_meta = lambda m: None
        auth._load_meta = lambda: {}

    def tearDown(self):
        auth._keychain_set = self.orig_set
        auth._save_meta = self.orig_meta
        auth._load_meta = self.orig_load

    def test_rejects_empty(self):
        ok, err = auth.set_manual_cookie("")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_rejects_without_sessionid(self):
        ok, err = auth.set_manual_cookie("ttwid=1; UIFID_TEMP=abc")
        self.assertFalse(ok)
        self.assertIn("sessionid", err)

    def test_accepts_valid_cookie(self):
        ok, err = auth.set_manual_cookie("sessionid=abc; ttwid=1")
        self.assertTrue(ok, err)
        self.assertEqual(err, "")
        self.assertEqual(self.written, ["sessionid=abc; ttwid=1"])

    def test_strips_whitespace(self):
        ok, _ = auth.set_manual_cookie("  sessionid=abc  ")
        self.assertTrue(ok)
        self.assertEqual(self.written, ["sessionid=abc"])


class TestLoginFlowControl(unittest.TestCase):
    """登录流程的取消与兜底分支。"""

    def test_cancel_sets_event(self):
        ev = threading.Event()
        orig = auth._cancel_event
        auth._cancel_event = ev
        try:
            auth.cancel_login()
            self.assertTrue(ev.is_set())
            self.assertEqual(auth._login_info["phase"], "cancelled")
        finally:
            auth._cancel_event = orig

    def test_launch_browser_falls_back_to_default(self):
        """检测不到 Chrome 时,退回系统默认浏览器并标记为手动模式。"""
        orig_find = auth.find_browser
        orig_open = auth.webbrowser.open
        opened = []
        auth.find_browser = lambda: ("", "")
        auth.webbrowser.open = lambda u: opened.append(u) or True
        try:
            ws, name, mode, err = auth.launch_browser(auth.LOGIN_URL, log=lambda m: None)
        finally:
            auth.find_browser = orig_find
            auth.webbrowser.open = orig_open
        self.assertEqual(ws, "")
        self.assertEqual(mode, "system")
        self.assertEqual(err, "")
        self.assertEqual(opened, [auth.LOGIN_URL])

    def test_launch_browser_reports_error_when_all_fail(self):
        orig_find = auth.find_browser
        orig_open = auth.webbrowser.open
        auth.find_browser = lambda: ("", "")
        auth.webbrowser.open = lambda u: False
        try:
            ws, name, mode, err = auth.launch_browser(auth.LOGIN_URL, log=lambda m: None)
        finally:
            auth.find_browser = orig_find
            auth.webbrowser.open = orig_open
        self.assertEqual(ws, "")
        self.assertTrue(err)

    def test_open_login_panel_is_safe_on_blank_page(self):
        """找不到登录按钮时不应抛异常,只返回 False。"""

        class FakeLoc:
            def count(self): return 0

        class FakePage:
            def locator(self, sel): return FakeLoc()

        self.assertFalse(auth._open_login_panel(FakePage(), log=lambda m: None))


class TestNoAutoClose(unittest.TestCase):
    """窗口不应被自动关闭 —— 这是用户报的第二个症状。"""

    def test_run_login_does_not_call_browser_close(self):
        """_run_browser_login 成功路径不得关闭浏览器。"""
        closed = []

        class FakeCtx:
            def cookies(self): return LOGGED_IN_COOKIES

            def storage_state(self, path=None): return {}

        class FakePage:
            url = "https://www.douyin.com/"
            frames = []

            def bring_to_front(self): pass
            def wait_for_timeout(self, ms): pass
            def inner_text(self, sel): return "扫码登录"

        class FakeCtx2(FakeCtx):
            @property
            def pages(self): return [FakePage()]

        class FakeBrowser:
            contexts = [FakeCtx2()]

            def close(self): closed.append("browser")

        class FakeChromium:
            def connect_over_cdp(self, ws): return FakeBrowser()

        class FakePW:
            chromium = FakeChromium()

        class FakeSync:
            def __enter__(self): return FakePW()

            def __exit__(self, *a): return False

        import playwright.sync_api as sa
        orig_sync = sa.sync_playwright
        orig_launch = auth.launch_browser
        orig_panel = auth._open_login_panel
        auth.launch_browser = lambda url, log=print: (
            "ws://127.0.0.1:1/devtools/x", "Google Chrome", "cdp", "")
        auth._open_login_panel = lambda page, log=print: True
        sa.sync_playwright = lambda: FakeSync()
        try:
            cookies, err = auth._run_browser_login(
                30, cancel=threading.Event(), log=lambda m: None)
        finally:
            sa.sync_playwright = orig_sync
            auth.launch_browser = orig_launch
            auth._open_login_panel = orig_panel
        self.assertTrue(cookies, f"应拿到 Cookie,实际 err={err}")
        self.assertEqual(err, "")
        self.assertEqual(closed, [], "浏览器被自动关闭了 —— 正是要修掉的自动关窗")

    def test_manual_required_signal(self):
        """能打开浏览器但拿不到 CDP 时,应返回手动粘贴信号而非报错。"""
        orig_launch = auth.launch_browser
        auth.launch_browser = lambda url, log=print: (
            "", "系统默认浏览器", "system", "")
        try:
            cookies, err = auth._run_browser_login(
                5, cancel=threading.Event(), log=lambda m: None)
        finally:
            auth.launch_browser = orig_launch      # 必须还原,否则污染其它用例
        self.assertIsNone(cookies)
        self.assertEqual(err, "MANUAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
