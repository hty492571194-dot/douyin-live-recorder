"""「打开我的抖音」按钮(auth.open_home)的回归守卫。

2026-10-21 的故障与修复(改这里之前先读完)
------------------------------------------
现象:高级参数 → 抖音登录 → 「打开我的抖音」点了像没反应。

真实链路(实测日志 19:33:19 → 19:34:58,整整 99 秒):
  1. launch_browser 冷启动专用 profile 的 Chrome,CDP 就绪(端口 50584);
  2. `page.goto(_HOME_URL, timeout=45000)` —— 用的是默认 ``wait_until="load"``,
     而抖音首页挂长连接 / 埋点 / 风控 iframe,``load`` 事件几十秒都不触发,
     于是 45 秒后抛 TimeoutError;
  3. 这被当成「CDP 打开失败」,退化去 ``webbrowser.open`` 开系统默认浏览器
     (无登录态);而那扇 Chrome 窗口里**页面其实早就打开好了**
     (同刻 /json/list 里标题已是「抖音精选电脑版…」);
  4. 前端每 5 秒 loadAuth 重绘整张卡片,把「打开中…」抹掉,用户完全看不到
     任何进行中的反馈 —— 观感就是「点了没用」。

守的点:
  · 打开主页的导航必须 ``wait_until="commit"``,不许再用 ``load``;
  · 导航事件超时但地址栏已落在抖音域 → 算成功,不许退化系统浏览器;
  · 同一时刻只允许一次打开(非阻塞锁,连点第二下立刻返回 busy);
  · 已有「我的抖音」页面要复用切前台,不堆窗口;
  · 死端口要靠 TCP 快探跳过,不能每个白等 3 秒;
  · 前端必须用模块级 ``_homeOpening`` 记住进行中状态,重绘时保持禁用态。

2026-10-26 的故障与修复(第二段,改这里之前先读完)
----------------------------------------------
现象:点「打开我的抖音」,用户正在浏览的那台专用 Chrome 被整个关掉(11 个进程),
      抖音页面全丢,然后冷启动一个新窗口。

真实链路:10-22 01:03 给 ``_BROWSER_FLAGS`` 加了三条反节流开关,``LAUNCH_ARGS_REV``
  1→2;但当时在跑的进程内存里还是 1,02:30 冷启动把 ``args_rev:1`` 写进 cdp.json;
  服务当天下午重启后内存变成 2 —— 10-26 05:04 第一次点按钮,判成「参数已更新」,
  走 ``_kill_profile_chrome()`` 杀掉整个浏览器。日志里那句「启动参数已更新」是
  固定文案,看不出真因。

现在守住:
  · **按钮路径永不杀进程**(``kill_stale=False``):参数落后只降级复用,不换实例;
  · 有实例但没有可复用页 → 用 CDP ``Target.createTarget(newWindow)`` 开新**窗口**,
    现有标签页一个都不动(不再往用户窗口里塞标签、也不再留 about:blank 残留);
  · 复用判定只认 ``/user/self``,不许再把任意抖音页顶到前台;
  · 拿不到 CDP 时先把 URL 转发进原实例,**绝不**退化系统默认浏览器(另一个 profile);
  · 关浏览器的入口只剩两个:显式「重开浏览器」按钮,和 ``logout()``。
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


class _FakePage:
    """极简 page 桩:只实现 _goto_home / _focus_existing_home 用到的方法。"""

    def __init__(self, url="", boom=None):
        self._url = url
        self.boom = boom
        self.goto_kwargs = []
        self.front = 0

    def goto(self, url, **kw):
        self.goto_kwargs.append(kw)
        if self.boom is not None:
            raise self.boom
        self._url = url

    @property
    def url(self):
        return self._url

    def bring_to_front(self):
        self.front += 1


class _FakeCtx:
    def __init__(self, pages):
        self.pages = pages


class TestGotoHome(unittest.TestCase):
    """导航策略:commit + 地址栏兜底。"""

    def test_source_never_waits_for_load(self):
        """硬约束:打开主页不许等 load 事件(这正是故障根因)。"""
        src = _read("auth.py")
        seg = src[src.index("def _goto_home"):src.index("def _focus_existing_home")]
        # 只对**代码行**断言:docstring 里为了说明来龙去脉必然要提到旧写法
        parts = seg.split('"""')
        code = parts[2] if len(parts) >= 3 else seg
        self.assertIn('wait_until="commit"', code, "必须用 commit:导航提交即返回")
        self.assertNotIn('wait_until="load"', code, "不许等 load:抖音首页 load 事件不触发")
        self.assertNotIn("timeout=45000", code, "45000ms 的同步等待会把按钮拖成「没反应」")

    def test_timeout_but_url_on_douyin_is_success(self):
        page = _FakePage(url="https://www.douyin.com/jingxuan",
                         boom=RuntimeError("Timeout 15000ms exceeded."))
        ok, err = auth._goto_home(page, log=lambda m: None)
        self.assertTrue(ok, "地址栏已经在抖音域内,页面就是打开了,不能判失败")
        self.assertEqual(err, "")

    def test_timeout_and_url_elsewhere_is_failure(self):
        page = _FakePage(url="about:blank", boom=RuntimeError("Timeout 15000ms exceeded."))
        ok, err = auth._goto_home(page, log=lambda m: None)
        self.assertFalse(ok)
        self.assertIn("Timeout", err)

    def test_success_path_uses_commit(self):
        page = _FakePage(url="about:blank")
        ok, err = auth._goto_home(page, log=lambda m: None)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(page.goto_kwargs[0].get("wait_until"), "commit")


class TestFocusExistingHome(unittest.TestCase):
    """已开着「我的抖音」页面时复用,不新开。

    2026-10-26:匹配范围从「任意 douyin.com」收窄到 ``/user/self``。旧实现的
    后果是用户正在看别人主页/信息流时,点按钮只把那一页顶到前台,自己的主页
    根本没打开 —— 语义完全错位。
    """

    def test_returns_matching_page(self):
        blank = _FakePage(url="about:blank")
        home = _FakePage(url="https://www.douyin.com/user/self")
        page = auth._focus_existing_home(_FakeCtx([blank, home]))
        self.assertIs(page, home)

    def test_matches_with_query_string(self):
        home = _FakePage(url="https://www.douyin.com/user/self?from_tab_name=main")
        self.assertIs(auth._focus_existing_home(_FakeCtx([home])), home)

    def test_does_not_hijack_other_douyin_pages(self):
        """正在看别人的主页 / 信息流时不许顶到前台,要另开窗口。"""
        feed = _FakePage(url="https://www.douyin.com/?recommend=1")
        other = _FakePage(url="https://www.douyin.com/user/MS4wLjABAAAA-xxx")
        self.assertIsNone(auth._focus_existing_home(_FakeCtx([feed, other])),
                          "只有 /user/self 才算「我的抖音」,别的抖音页不许被劫持")

    def test_none_when_no_douyin_tab(self):
        self.assertIsNone(auth._focus_existing_home(_FakeCtx([_FakePage("about:blank")])))

    def test_ignores_page_whose_url_raises(self):
        class _Bad(_FakePage):
            @property
            def url(self):
                raise RuntimeError("target closed")
        home = _FakePage(url="https://www.douyin.com/user/self")
        page = auth._focus_existing_home(_FakeCtx([_Bad(), home]))
        self.assertIs(page, home)


class TestOpenHomeConcurrency(unittest.TestCase):
    """并发与返回结构。"""

    def test_second_call_returns_busy_without_touching_browser(self):
        called = []
        orig = auth.find_browser
        auth.find_browser = lambda: (called.append(1), ("", ""))[1]
        self.addCleanup(setattr, auth, "find_browser", orig)
        auth._home_lock.acquire()
        self.addCleanup(auth._home_lock.release)
        r = auth.open_home(log=lambda m: None)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("busy"))
        self.assertIn("正在打开", r["error"])
        self.assertEqual(called, [], "繁忙时必须立刻返回,不许再去碰浏览器")

    def test_lock_released_after_exception(self):
        def _boom():
            raise RuntimeError("boom")
        orig = auth.find_browser
        auth.find_browser = _boom
        self.addCleanup(setattr, auth, "find_browser", orig)
        with self.assertRaises(RuntimeError):
            auth.open_home(log=lambda m: None)
        self.assertTrue(auth._home_lock.acquire(blocking=False), "异常路径也必须解锁")
        auth._home_lock.release()

    def test_no_chrome_degrades_to_system_with_elapsed(self):
        orig_fb, orig_open = auth.find_browser, auth.webbrowser.open
        auth.find_browser = lambda: ("", "")
        auth.webbrowser.open = lambda u: True
        self.addCleanup(setattr, auth, "find_browser", orig_fb)
        self.addCleanup(setattr, auth.webbrowser, "open", orig_open)
        r = auth.open_home(log=lambda m: None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "system")
        self.assertFalse(r["injected"])
        self.assertIn("elapsed", r, "前端要能显示耗时,便于判断是不是冷启动")


class TestPortAlive(unittest.TestCase):
    """死端口快探:cdp.json 里的历史端口大多早已退出。"""

    def test_alive_then_dead(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            self.assertTrue(auth._port_alive(port, timeout=1))
        finally:
            s.close()
        # 关闭后(端口无监听)必须判死,否则又回到「每个死端口白等 3 秒」
        self.assertFalse(auth._port_alive(port, timeout=0.3))


class TestHomeUi(unittest.TestCase):
    """前端反馈:进行中状态必须跨重绘保留。"""

    def setUp(self):
        self.html = _read("static/index.html")

    def test_opening_state_is_module_level(self):
        self.assertIn("let _homeOpening = false;", self.html,
                      "状态必须记在模块里,否则 5 秒一次的 loadAuth 会把按钮抹回可点")

    def test_button_renders_disabled_while_opening(self):
        self.assertIn('data-auth="open-home" disabled', self.html,
                      "打开中要渲染禁用态按钮,不能让人重复点")
        self.assertIn("打开中…", self.html)

    def test_success_uses_toast_not_alert(self):
        self.assertIn("gtoast(r.detail", self.html,
                      "cdp 成功路径用轻量提示,不要弹 alert 打断")
        self.assertNotIn('alert("打开失败:" + (r.error || "未知错误"));', self.html,
                         "旧文案已换成 gtoast(error),别退回弹窗")

    def test_handler_sets_and_clears_opening(self):
        seg = self.html[self.html.index('act === "open-home"'):]
        seg = seg[:seg.index('act === "verify"')]
        self.assertIn("_homeOpening = true;", seg)
        self.assertIn("_homeOpening = false;", seg)
        self.assertIn("if (_homeOpening) return;", seg, "客户端也要挡住重复点击")

    def test_restart_button_requires_confirm(self):
        """「重开浏览器」是唯一会关掉页面的入口,必须二次确认。"""
        self.assertIn('data-auth="restart-browser"', self.html)
        seg = self.html[self.html.index('act === "restart-browser"'):]
        seg = seg[:seg.index('act === "open-home"')]
        self.assertIn("confirm(", seg, "关浏览器前必须确认")
        self.assertIn("/api/auth/restart-browser", seg)

    def test_streamer_page_entry_reuses_same_handler(self):
        """主播管理页顶部也要有入口:排在「＋ 添加主播」左边,且复用同一个
        ``data-auth="open-home"`` 处理 —— 不许在工具栏里另写一份请求逻辑。"""
        html = self.html
        start = html.index('class="view-tools"')
        seg = html[start:html.index('id="rec-filter"', start)]
        self.assertIn('id="home-open"', seg, "按钮要放进标题行工具组")
        self.assertLess(seg.index('id="home-open"'), seg.index('id="add-open"'),
                        "要排在「＋ 添加主播」左边")
        self.assertIn('data-auth="open-home"', seg, "复用同一套处理,别写第二份")
        self.assertNotIn("/api/auth/open-home", seg, "按钮上不许再挂一份请求逻辑")

    def test_streamer_page_entry_state_synced(self):
        """卡片外的入口不会被 renderAuth 重绘,必须单独同步禁用态与文案。"""
        html = self.html
        self.assertIn("function _syncTopHomeBtn", html)
        self.assertIn('getElementById("home-open")', html)
        self.assertIn("_syncTopHomeBtn(_authLast)", html,
                      "点击瞬间就要把另一个入口也变灰,不等 5 秒轮询")
        start = html.index("function renderAuth(a)")
        seg = html[start:html.index("document.addEventListener", start)]
        self.assertIn("_syncTopHomeBtn(_authLast)", seg,
                      "每次 loadAuth 重绘都要同步一次顶部入口")
        self.assertIn("logging_in", seg, "扫码登录中顶部入口也要禁用")


class TestHomeTarget(unittest.TestCase):
    """打开的页面必须轻,且不许重复加载。

    2026-10-21 二轮反馈「弹出来很卡、加载慢」实测:
      /jingxuan(首页落点)  节点 2947 / 资源 250 / DCL 10.8s / load 15.9s
      /user/self(个人主页) 节点 1150 / 资源 250 / DCL  3.4s / load  6.0s
    同一个页面在干净浏览器里只要 1.7s 就能加载完、稳 75fps —— 说明卡在「打开
    哪个页面」和「重复加载」,不是抖音本身。
    """

    def test_personal_home_is_default_target(self):
        self.assertTrue(hasattr(auth, "_MY_HOME_URL"))
        self.assertIn("/user/self", auth._MY_HOME_URL,
                      "「我的抖音」要打开个人主页,不是首页信息流")
        page = _FakePage(url="about:blank")
        auth._goto_home(page, log=lambda m: None)
        self.assertEqual(page.goto_kwargs[0].get("wait_until"), "commit")
        self.assertIn("/user/self", page._url)

    def test_reuse_does_not_reload(self):
        """复用已开着的抖音标签页时禁止 reload —— 等于把重页面重载一遍。"""
        src = _read("auth.py")
        seg = src[src.index("def _open_home"):src.index("def _run_browser_login")]
        self.assertNotIn("page.reload(", seg,
                         "复用标签页不许刷新,否则每次点击都要等十几秒重新加载")
        self.assertIn("_focus_existing_home(ctx)", seg)

    def test_activates_window_after_open(self):
        src = _read("auth.py")
        seg = src[src.index("def _open_home"):src.index("def _run_browser_login")]
        self.assertIn("_activate_browser(name, log)", seg,
                      "CDP 的 bringToFront 不保证把窗口抬到最前,要补一次 activate")


class TestBrowserFlags(unittest.TestCase):
    """专用浏览器的启动参数与参数版本。"""

    def test_anti_throttle_flags_present(self):
        need = ("--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling")
        for f in need:
            self.assertIn(f, auth._BROWSER_FLAGS,
                          "窗口在后台/被遮挡时 Chrome 会压低渲染频率,这三条要留着")

    def test_args_rev_recorded_with_port(self):
        """改参数必须能被记录,否则旧实例会被一直复用、新参数永不生效。"""
        import shutil, tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        orig = auth.AUTH_DIR
        auth.AUTH_DIR = os.path.join(tmp, "auth")
        self.addCleanup(setattr, auth, "AUTH_DIR", orig)
        auth._save_cdp_port(12345)
        st = auth._cdp_state()
        self.assertEqual(st.get("port"), 12345)
        self.assertEqual(st.get("args_rev"), auth.LAUNCH_ARGS_REV)

    def test_cdp_state_tolerates_bad_file(self):
        import shutil, tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        orig = auth.AUTH_DIR
        auth.AUTH_DIR = os.path.join(tmp, "auth")
        self.addCleanup(setattr, auth, "AUTH_DIR", orig)
        os.makedirs(auth.AUTH_DIR, exist_ok=True)
        with open(auth._cdp_state_path(), "w", encoding="utf-8") as f:
            f.write("{坏 json")
        self.assertEqual(auth._cdp_state(), {})
        self.assertEqual(auth._cdp_ports(), [])

    def test_clear_profile_singleton(self):
        """单例锁残留会让新 Chrome「转发 URL 后自杀」,必须能清掉。"""
        import shutil, tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        orig = auth.PROFILE_DIR
        auth.PROFILE_DIR = os.path.join(tmp, "profile")
        self.addCleanup(setattr, auth, "PROFILE_DIR", orig)
        os.makedirs(auth.PROFILE_DIR, exist_ok=True)
        for fn in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            os.symlink("/tmp/whatever", os.path.join(auth.PROFILE_DIR, fn))
        auth._clear_profile_singleton()
        left = [fn for fn in ("SingletonLock", "SingletonSocket", "SingletonCookie")
                if os.path.lexists(os.path.join(auth.PROFILE_DIR, fn))]
        self.assertEqual(left, [])


class TestReuseGuard(unittest.TestCase):
    """参数升级不能伤到正在进行的扫码登录(影响原有功能的红线)。"""

    def _state(self, rev):
        orig_state, orig_thread = auth._cdp_state, auth._login_thread
        auth._cdp_state = lambda: {"port": 1, "args_rev": rev}
        self.addCleanup(setattr, auth, "_cdp_state", orig_state)
        self.addCleanup(setattr, auth, "_login_thread", orig_thread)

    def test_same_rev_reuses(self):
        self._state(auth.LAUNCH_ARGS_REV)
        ok, why = auth._can_reuse_running_instance()
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_stale_rev_rotates_instance(self):
        self._state(auth.LAUNCH_ARGS_REV - 1)
        auth._login_thread = None
        ok, why = auth._can_reuse_running_instance()
        self.assertFalse(ok, "参数版本落后时必须换实例,否则新参数永远不生效")
        self.assertIn("参数", why)

    def test_stale_rev_but_logging_in_keeps_instance(self):
        """扫码登录进行中 → 不许杀窗口,宁可新参数晚点生效。"""
        self._state(auth.LAUNCH_ARGS_REV - 1)

        class _Alive:
            def is_alive(self):
                return True
        auth._login_thread = _Alive()
        ok, why = auth._can_reuse_running_instance()
        self.assertTrue(ok, "登录进行中不许结束浏览器")
        self.assertIn("扫码登录", why)

    def test_no_kill_stale_never_rotates(self):
        """「打开我的抖音」路径:参数落后也必须复用,绝不能关掉用户页面。"""
        self._state(auth.LAUNCH_ARGS_REV - 1)
        auth._login_thread = None
        ok, why = auth._can_reuse_running_instance(kill_stale=False)
        self.assertTrue(ok, "kill_stale=False 时版本落后也要复用")
        self.assertIn("复用", why)

    def test_reason_reports_real_version_delta(self):
        """日志必须打真实差异。旧实现固定打「启动参数已更新」,查不出真因。"""
        self._state(auth.LAUNCH_ARGS_REV - 1)
        auth._login_thread = None
        ok, why = auth._can_reuse_running_instance()
        self.assertFalse(ok)
        self.assertIn(f"v{auth.LAUNCH_ARGS_REV - 1}→v{auth.LAUNCH_ARGS_REV}", why)

    def test_missing_cdp_state_says_so(self):
        """cdp.json 缺失/损坏要明说,不能冒充「参数已更新」。"""
        orig_state, orig_thread = auth._cdp_state, auth._login_thread
        auth._cdp_state = lambda: {}
        auth._login_thread = None
        self.addCleanup(setattr, auth, "_cdp_state", orig_state)
        self.addCleanup(setattr, auth, "_login_thread", orig_thread)
        ok, why = auth._can_reuse_running_instance()
        self.assertFalse(ok)
        self.assertIn("缺失", why)


class TestNewWindowViaCdp(unittest.TestCase):
    """新开窗口走浏览器级 Target.createTarget(newWindow=True)。"""

    class _Sess:
        def __init__(self, reply=None, boom=None):
            self.reply = reply or {}
            self.boom = boom
            self.calls = []

        def send(self, method, params=None):
            self.calls.append((method, params or {}))
            if self.boom is not None:
                raise self.boom
            return self.reply

    class _Browser:
        def __init__(self, sess):
            self._sess = sess

        def new_browser_cdp_session(self):
            return self._sess

    def test_uses_new_window_flag(self):
        sess = self._Sess({"targetId": "TID123"})
        tid = auth._new_window_via_cdp(self._Browser(sess), auth._MY_HOME_URL)
        self.assertEqual(tid, "TID123")
        method, params = sess.calls[0]
        self.assertEqual(method, "Target.createTarget")
        self.assertIs(params.get("newWindow"), True,
                      "必须是新窗口;new_page() 只能开标签,会改用户的标签栏")
        self.assertIs(params.get("background"), False, "新窗口要前台可见")
        self.assertEqual(params.get("url"), auth._MY_HOME_URL,
                         "URL 必须在 createTarget 里给(raw target 拿不到 Page,不能事后 goto)")

    def test_empty_response_falls_back(self):
        self.assertEqual(auth._new_window_via_cdp(self._Browser(self._Sess({})), "u"), "")

    def test_exception_falls_back(self):
        sess = self._Sess(boom=RuntimeError("boom"))
        self.assertEqual(auth._new_window_via_cdp(self._Browser(sess), "u"), "")


class TestOpenHomeNeverKills(unittest.TestCase):
    """硬约束:点「打开我的抖音」永远不许杀专用浏览器(2026-10-26 事故)。"""

    def _seg(self):
        src = _read("auth.py")
        return src[src.index("def _open_home"):src.index("def _run_browser_login")]

    def test_open_home_segment_has_no_kill(self):
        self.assertNotIn("_kill_profile_chrome", self._seg(),
                         "这条路径一旦杀进程,用户开着的页面就全没了")

    def test_open_home_passes_kill_stale_false(self):
        seg = self._seg()
        self.assertIn("kill_stale=False", seg)
        self.assertIn("open_tab=False", seg, "复用路径不许再替他开空白标签")

    def test_open_home_uses_new_window(self):
        self.assertIn("_new_window_via_cdp(b, target)", self._seg(),
                      "有实例但没有可复用页时要新开窗口,不是往现有窗口塞标签")

    def test_forward_before_system_browser(self):
        seg = self._seg()
        self.assertIn("_forward_to_existing(target, log)", seg)
        # 用 rindex:段首「未检测到 Chrome」那条分支也有一个 webbrowser.open
        self.assertLess(seg.index("_forward_to_existing(target, log)"),
                        seg.rindex("webbrowser.open"),
                        "拿不到 CDP 时要先转发进原实例,系统浏览器只能是最后兜底")

    def test_launch_browser_guards_both_kill_sites(self):
        src = _read("auth.py")
        seg = src[src.index("def launch_browser"):src.index("def _wait_cdp")]
        self.assertEqual(seg.count("_kill_profile_chrome("), 2,
                         "两条杀进程路径都得在(参数落后 / 端口被旧实例占)")
        self.assertIn("if not kill_stale and _profile_chrome_pids():", seg,
                      "实例活着但拿不到端口时不许再 spawn 一个(会白弹空白窗口)")


class TestForwardToExisting(unittest.TestCase):
    """拿不到 CDP 时的降级:转发进原实例,不换浏览器。"""

    def test_returns_false_without_chrome(self):
        orig = auth.find_browser
        auth.find_browser = lambda: ("", "")
        self.addCleanup(setattr, auth, "find_browser", orig)
        self.assertFalse(auth._forward_to_existing(auth._MY_HOME_URL, log=lambda m: None))

    def test_spawns_new_window_in_same_profile(self):
        orig_find, orig_popen = auth.find_browser, auth.subprocess.Popen
        orig_run = auth.subprocess.run
        spawned = []
        auth.find_browser = lambda: ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                     "Google Chrome")
        self.addCleanup(setattr, auth, "find_browser", orig_find)
        auth.subprocess.Popen = lambda args, **kw: spawned.append(args)
        self.addCleanup(setattr, auth.subprocess, "Popen", orig_popen)

        # LaunchServices 这条路要按「失败」算,才能测到 Popen 兜底的参数 ——
        # 否则真去 open 一个 Chrome,测试会污染用户浏览器。
        class _R:
            returncode = 1
            stderr = "blocked-in-test"
            stdout = ""
        auth.subprocess.run = lambda *a, **kw: _R()
        self.addCleanup(setattr, auth.subprocess, "run", orig_run)

        orig_wait = auth._wait_cdp
        auth._wait_cdp = lambda port, timeout=0: ""
        self.addCleanup(setattr, auth, "_wait_cdp", orig_wait)
        self.assertTrue(auth._forward_to_existing(auth._MY_HOME_URL, log=lambda m: None))
        self.assertEqual(len(spawned), 1)
        self.assertIn(f"--user-data-dir={auth.PROFILE_DIR}", spawned[0],
                      "必须是对同一个 profile 转发,否则会开成另一台浏览器")
        self.assertIn("--new-window", spawned[0])
        self.assertIn(auth._MY_HOME_URL, spawned[0])


class TestSpawnViaLaunchServices(unittest.TestCase):
    """Chrome 必须由 LaunchServices 起,不能从后台服务直接 fork。

    根因(2026-10-26 实测):服务的 launchd plist 是 ``ProcessType=Background``,
    Popen 出来的 Chrome 会**继承后台 QoS** —— 主进程 ``pti_priority=4``
    (正常用户进程 31,Safari 也是 31)。WindowServer 随后以低优先级给它合成,
    Chrome 的 BeginFrame 被压到 12~14Hz:同一个实例里连 ``about:blank``
    空白页都只有 14fps,而同屏 Safari 75fps、系统 Chrome 155 换个 profile
    起也是 77fps。此时 CPU 只占 5%、GPU 走 ANGLE Metal 正常、
    窗口 normal/visible/hasFocus 全为真、AppleScript activate 也无效。

    这个属性在 exec 时固化,事后 ``setpriority(PRIO_DARWIN_PROCESS, pid, 0)``
    返回成功但 pri 仍是 4(实测),补救不了 —— 只能在启动时打断继承,
    所以启动一律走 ``open -n -b <bundle> --args``(GUI 会话创建,QoS 正常)。
    """

    def test_bundle_id_from_app_path(self):
        self.assertEqual(
            auth._bundle_id_for("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "com.google.Chrome")

    def test_bundle_id_empty_for_non_app_path(self):
        self.assertEqual(auth._bundle_id_for("/usr/bin/google-chrome"), "")
        self.assertEqual(auth._bundle_id_for(""), "")

    def _patch(self, returncode):
        calls = {"run": [], "popen": []}
        orig_popen, orig_run = auth.subprocess.Popen, auth.subprocess.run

        class _R:
            pass
        r = _R()
        r.returncode = returncode
        r.stderr = "" if returncode == 0 else "boom"
        r.stdout = ""

        auth.subprocess.Popen = lambda args, **kw: calls["popen"].append(args)
        auth.subprocess.run = lambda cmd, *a, **kw: (calls["run"].append(cmd), r)[1]
        self.addCleanup(setattr, auth.subprocess, "Popen", orig_popen)
        self.addCleanup(setattr, auth.subprocess, "run", orig_run)
        return calls

    def test_prefers_launchservices(self):
        calls = self._patch(0)
        how = auth._spawn_browser(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ["--user-data-dir=/tmp/x", "https://www.douyin.com/user/self"],
            log=lambda m: None)
        self.assertEqual(how, "open", "macOS 上必须先走 LaunchServices")
        self.assertEqual(calls["popen"], [], "LaunchServices 成功时不许再 fork 一份")
        cmd = calls["run"][0]
        self.assertEqual(cmd[:2], ["open", "-n"],
                         "-n 必须带:否则系统里任何 Chrome 在跑都只会激活旧窗口、参数全丢")
        self.assertIn("-b", cmd)
        self.assertIn("com.google.Chrome", cmd)
        self.assertIn("--args", cmd)
        self.assertIn("--user-data-dir=/tmp/x", cmd)
        self.assertIn("https://www.douyin.com/user/self", cmd)

    def test_falls_back_to_popen(self):
        calls = self._patch(1)
        how = auth._spawn_browser(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ["--user-data-dir=/tmp/x"], log=lambda m: None)
        self.assertEqual(how, "popen", "LaunchServices 失败要退回直接 spawn,不能直接放弃")
        self.assertEqual(len(calls["popen"]), 1)
        self.assertEqual(calls["popen"][0][0],
                         "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

    def test_chrome_launch_sites_use_spawn_browser(self):
        """守卫:两个启动点都不许再裸写 subprocess.Popen 起 Chrome。

        直接 Popen 会让 Chrome 继承后台 QoS → 窗口只有十几帧,
        而且从代码上看不出任何异常(资源占用、GPU、可见性全都正常)。
        """
        src = _read("auth.py")
        for fn, nxt in (("def launch_browser", "def _wait_cdp"),
                        ("def _forward_to_existing", "def open_home")):
            seg = src[src.index(fn):src.index(nxt)]
            self.assertIn("_spawn_browser", seg, f"{fn} 必须经 _spawn_browser 启动")
            self.assertNotIn("subprocess.Popen", seg,
                             f"{fn} 不许直接 Popen Chrome(会继承 launchd 后台 QoS)")

    def test_open_home_reports_browser_priority(self):
        """诊断要能被看见:返回体里带 chrome_pri,否则「很卡」只能靠用户感觉。"""
        src = _read("auth.py")
        seg = src[src.index("def open_home"):src.index("def _open_home")]
        self.assertIn("_proc_priority", seg)
        self.assertIn("chrome_pri", seg)

    # ── open 的返回码不可信(2026-10-29) ──────────────────────
    def test_verify_rejects_silent_open(self):
        """``open`` 返回 0 但进程表里没有 Chrome → 必须退回 Popen。

        实测:从 AI 会话的 shell 里 ``open -n -b com.google.Chrome --args ...``
        全部 rc=0,可 ``open -W`` 0.2 秒就返回、进程表无新进程、也没有崩溃报告;
        同样写法起 TextEdit 却正常。只信返回码的后果是白等 12 秒 CDP,再退化成
        「已在系统默认浏览器打开」(另一个 profile、没有登录态)。
        """
        calls = self._patch(0)
        how = auth._spawn_browser(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ["--user-data-dir=/tmp/x"], log=lambda m: None,
            verify=lambda: False)
        self.assertEqual(how, "popen", "verify 不通过就不能报成功")
        self.assertEqual(len(calls["popen"]), 1,
                         "校验失败必须真的再 spawn 一份,而不是空手而归")

    def test_verify_accepts_real_open(self):
        calls = self._patch(0)
        how = auth._spawn_browser(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ["--user-data-dir=/tmp/x"], log=lambda m: None,
            verify=lambda: True)
        self.assertEqual(how, "open")
        self.assertEqual(calls["popen"], [], "真起来了就不许多 fork 一份")

    def test_cold_spawn_passes_verify(self):
        """冷启动路径必须把 ``_wait_profile_chrome`` 当 verify 传进去。

        传 None 等于没校验 —— 上面那两条守卫就形同虚设。
        """
        src = _read("auth.py")
        seg = src[src.index("def launch_browser"):src.index("def _wait_cdp")]
        self.assertIn("verify=_wait_profile_chrome", seg)

    def test_wait_profile_chrome_polls_pids(self):
        """``_wait_profile_chrome`` 要以「进程表里出现本 profile 的 Chrome」为准。"""
        orig = auth._profile_chrome_pids
        seq = [[], [], [1234]]
        auth._profile_chrome_pids = lambda: seq.pop(0) if seq else [1234]
        self.addCleanup(setattr, auth, "_profile_chrome_pids", orig)
        self.assertTrue(auth._wait_profile_chrome(timeout=3.0))
        auth._profile_chrome_pids = lambda: []
        self.assertFalse(auth._wait_profile_chrome(timeout=0.5),
                         "一直没有进程就要如实返回 False")


class TestRestartBrowser(unittest.TestCase):
    """显式「重开浏览器」:新启动参数唯一的人工生效入口。"""

    def test_webui_route_wired(self):
        """路由别只加在后端忘了接:前端按钮打的地址必须真有条目。"""
        webui = _read("webui.py")
        self.assertIn('"/api/auth/restart-browser"', webui)
        self.assertIn("auth_mod.restart_browser(log=_log_line)", webui)

    def test_refuses_while_logging_in(self):
        orig_thread, orig_kill = auth._login_thread, auth._kill_profile_chrome
        killed = []

        class _Alive:
            def is_alive(self):
                return True
        auth._login_thread = _Alive()
        auth._kill_profile_chrome = lambda *a, **kw: killed.append(1) or 7
        try:
            r = auth.restart_browser(log=lambda m: None)
        finally:
            auth._login_thread, auth._kill_profile_chrome = orig_thread, orig_kill
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("busy"))
        self.assertEqual(killed, [], "扫码进行中不许结束浏览器")

    def test_kills_when_idle(self):
        orig_thread, orig_kill = auth._login_thread, auth._kill_profile_chrome
        auth._login_thread = None
        auth._kill_profile_chrome = lambda *a, **kw: 5
        try:
            r = auth.restart_browser(log=lambda m: None)
        finally:
            auth._login_thread, auth._kill_profile_chrome = orig_thread, orig_kill
        self.assertTrue(r["ok"])
        self.assertEqual(r["killed"], 5)
        self.assertIn("5", r["detail"])

    def test_reports_when_nothing_running(self):
        orig_thread, orig_kill = auth._login_thread, auth._kill_profile_chrome
        auth._login_thread = None
        auth._kill_profile_chrome = lambda *a, **kw: 0
        try:
            r = auth.restart_browser(log=lambda m: None)
        finally:
            auth._login_thread, auth._kill_profile_chrome = orig_thread, orig_kill
        self.assertTrue(r["ok"])
        self.assertEqual(r["killed"], 0)
        self.assertIn("没在运行", r["detail"])


class TestLogoutKillsBrowser(unittest.TestCase):
    """退出登录必须**显式**结束专用浏览器。

    以前是靠「删掉 cdp.json → 下次启动判定参数版本不匹配 → 顺手杀进程」这个
    隐式副作用收尾的;打开主页改成永不杀进程之后,不显式杀就会留下一台前台
    还登录着的浏览器。
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.orig = {k: getattr(auth, k) for k in (
            "AUTH_DIR", "META_PATH", "PROFILE_DIR",
            "_keychain_delete", "_kill_profile_chrome")}
        auth.AUTH_DIR = os.path.join(self.tmp, "auth")
        os.makedirs(auth.AUTH_DIR, exist_ok=True)
        auth.META_PATH = os.path.join(auth.AUTH_DIR, "meta.json")
        auth.PROFILE_DIR = os.path.join(auth.AUTH_DIR, "profile")
        os.makedirs(auth.PROFILE_DIR, exist_ok=True)
        with open(os.path.join(auth.PROFILE_DIR, "Cookies"), "w"):
            pass
        auth._keychain_delete = lambda: True
        self.snapshots = []
        auth._kill_profile_chrome = (
            lambda *a, **kw: self.snapshots.append(
                os.path.isdir(auth.PROFILE_DIR)) or 3)
        self._shutil = shutil

    def tearDown(self):
        for k, v in self.orig.items():
            setattr(auth, k, v)
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kills_browser_before_removing_profile(self):
        auth.logout()
        self.assertEqual(len(self.snapshots), 1, "退出登录必须显式结束专用浏览器")
        self.assertTrue(self.snapshots[0], "必须先杀进程再删 profile")
        self.assertFalse(os.path.exists(auth.PROFILE_DIR))


class TestActivateBrowser(unittest.TestCase):
    """窗口激活是 best-effort,失败不能影响打开页面。"""

    def test_success_returns_true(self):
        orig = auth.subprocess.run
        auth.subprocess.run = lambda *a, **kw: type("R", (), {"returncode": 0, "stderr": ""})()
        self.addCleanup(setattr, auth.subprocess, "run", orig)
        self.assertTrue(auth._activate_browser("Google Chrome", lambda m: None))

    def test_failure_is_non_fatal(self):
        orig = auth.subprocess.run
        def _boom(*a, **kw):
            raise OSError("no osascript")
        auth.subprocess.run = _boom
        self.addCleanup(setattr, auth.subprocess, "run", orig)
        self.assertFalse(auth._activate_browser("Google Chrome", lambda m: None))

    def test_skips_system_default_browser(self):
        self.assertFalse(auth._activate_browser("系统默认浏览器", lambda m: None))
        self.assertFalse(auth._activate_browser("", lambda m: None))


if __name__ == "__main__":
    unittest.main()
