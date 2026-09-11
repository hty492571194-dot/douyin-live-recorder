#!/usr/bin/env python3
"""服务控制(launchd 启 / 停 / 重启)的单元测试。

这些测试**只验证参数拼接与解析逻辑,绝不真正执行启停** —— 那会杀掉正在跑的
监控服务(连带中断录制)。真正的 launchctl 调用通过 mock subprocess 拦截。
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402


class TestEtimeParsing(unittest.TestCase):
    """ps 的 etime 有三种格式,拆错就会显示错误的运行时长。"""

    def test_mm_ss(self):
        self.assertEqual(webui._parse_etime("12:34"), 754)

    def test_hh_mm_ss(self):
        self.assertEqual(webui._parse_etime("01:02:03"), 3723)

    def test_dd_hh_mm_ss(self):
        self.assertEqual(webui._parse_etime("2-03:04:05"), 183845)

    def test_seconds_only(self):
        self.assertEqual(webui._parse_etime("45"), 45)

    def test_empty_is_none(self):
        self.assertIsNone(webui._parse_etime(""))
        self.assertIsNone(webui._parse_etime(None))

    def test_garbage_is_none(self):
        self.assertIsNone(webui._parse_etime("not-a-time"))


class TestServiceControl(unittest.TestCase):
    """命令拼接:必须延迟、脱离、且不接受任何外部输入。"""

    def setUp(self):
        self.orig_popen = subprocess.Popen
        self.calls = []
        subprocess.Popen = self._fake_popen
        self.orig_plist = webui.LAUNCHD_PLIST
        # 默认「端口上没有外来实例」,让每个用例从确定的状态出发;要测清场分支的
        # 用例会自己覆盖它。
        self.orig_outsider = webui._outsider_pid
        webui._outsider_pid = lambda: None

    def tearDown(self):
        subprocess.Popen = self.orig_popen
        webui.LAUNCHD_PLIST = self.orig_plist
        webui._outsider_pid = self.orig_outsider

    def _fake_popen(self, args, **kw):
        self.calls.append((args, kw))
        return object()

    def _spawn_call(self):
        """取出 _spawn_delayed 生成的那条命令。

        注意 subprocess.run 内部也走 Popen,_run/_launchctl 同样会被记录,
        所以不能简单取 calls[0] —— 延迟执行的那条特征是 /bin/sh -c。
        """
        for args, kw in reversed(self.calls):
            if args and args[0] == "/bin/sh":
                return args, kw
        self.fail("未找到延迟执行的命令")

    def test_unknown_action_rejected(self):
        ok, msg = webui._service_control("bogus")
        self.assertFalse(ok)
        self.assertIn("未知操作", msg)
        self.assertEqual(self.calls, [], "非法动作不应启动任何进程")

    def test_restart_uses_kickstart_k(self):
        ok, _ = webui._service_control("restart")
        self.assertTrue(ok)
        args, _ = self._spawn_call()
        self.assertIn("kickstart -k", args[2])
        self.assertIn(webui.LAUNCHD_LABEL, args[2])

    def test_restart_kills_outsider_before_kickstart(self):
        """端口被非 launchd 实例占着时,必须先把它请走,否则重启形同虚设。

        kickstart 只作用于 launchd 名下进程;端口若是别人占着,新实例一 bind 就
        Address already in use,退出后 KeepAlive 又拉,形成崩溃循环,而界面始终
        连着旧实例 —— 用户看来就是「点了重启没反应」。
        """
        webui._outsider_pid = lambda: 24190
        ok, msg = webui._service_control("restart")
        self.assertTrue(ok)
        self.assertIn("24190", msg, "提示里应说明结束的是哪个进程")
        args, _ = self._spawn_call()
        cmd = args[2]
        self.assertIn("kill -TERM 24190", cmd)
        self.assertIn("kickstart -k", cmd)
        # 顺序:先 TERM 给收尾时间(停 ffmpeg、落历史),再 KILL,最后才 kickstart
        self.assertLess(cmd.index("kill -TERM"), cmd.index("kill -KILL"))
        self.assertLess(cmd.index("kill -KILL"), cmd.index("kickstart"))

    def test_start_unloads_before_bootstrap(self):
        """job 已装载时裸 bootstrap 会报 already loaded,必须先 bootout。

        界面上「启动」多半就出现在这个状态(已装载但未运行),不清掉就永远起不来。
        """
        ok, _ = webui._service_control("start")
        self.assertTrue(ok)
        args, _ = self._spawn_call()
        self.assertIn("bootout", args[2])
        self.assertIn("bootstrap", args[2])
        self.assertLess(args[2].index("bootout"), args[2].index("bootstrap"))

    def test_stop_uses_bootout(self):
        ok, _ = webui._service_control("stop")
        self.assertTrue(ok)
        args, _ = self.calls[0]
        self.assertIn("bootout", args[2])

    def test_start_uses_bootstrap_with_plist(self):
        ok, _ = webui._service_control("start")
        self.assertTrue(ok)
        args, _ = self.calls[0]
        self.assertIn("bootstrap", args[2])
        self.assertIn("com.douyin.monitor.plist", args[2])

    def test_all_commands_are_delayed_and_detached(self):
        """延迟是为了让 HTTP 响应先发出去;脱离进程组是为了本进程退出后命令仍会执行。"""
        for action in ("restart", "stop", "start"):
            self.calls.clear()
            webui._service_control(action)
            args, kw = self._spawn_call()
            self.assertTrue(args[2].startswith("sleep "), f"{action} 未延迟执行")
            self.assertTrue(kw.get("start_new_session"), f"{action} 未脱离进程组")

    def test_missing_plist_blocks_start_and_restart(self):
        """plist 不存在时不应盲发命令 —— 否则只会得到一个看不懂的 launchctl 报错。"""
        webui.LAUNCHD_PLIST = "/definitely/not/here.plist"
        for action in ("start", "restart"):
            self.calls.clear()
            ok, msg = webui._service_control(action)
            self.assertFalse(ok)
            self.assertIn("launchd 配置", msg)
            self.assertEqual(self.calls, [])

    def test_spawn_delayed_closes_stdio(self):
        """脱离进程若继承 stdio,会吊住服务的管道使其退出后不被回收。"""
        self.calls.clear()
        webui._spawn_delayed("true")
        _, kw = self.calls[0]
        self.assertIs(kw.get("stdin"), subprocess.DEVNULL)
        self.assertIs(kw.get("stdout"), subprocess.DEVNULL)
        self.assertIs(kw.get("stderr"), subprocess.DEVNULL)


class TestOutsiderPid(unittest.TestCase):
    """判断端口占用者是不是 launchd 名下 —— 决定重启前要不要先清场。"""

    def setUp(self):
        self.orig_status = webui._service_status
        self.orig_owner = webui._port_owner_pid

    def tearDown(self):
        webui._service_status = self.orig_status
        webui._port_owner_pid = self.orig_owner

    def test_managed_instance_is_not_outsider(self):
        """端口上就是 launchd 自己的进程 —— 正常 kickstart 即可,不能误杀。"""
        webui._service_status = lambda: {"source": "launchd", "pid": 1287}
        webui._port_owner_pid = lambda port: 1287
        self.assertIsNone(webui._outsider_pid())

    def test_manual_instance_is_outsider(self):
        """端口被手动/一键启动拉起的实例占着(source=manual)。"""
        webui._service_status = lambda: {
            "source": "manual", "pid": 24190, "managed": True}
        webui._port_owner_pid = lambda port: 24190
        self.assertEqual(webui._outsider_pid(), 24190)

    def test_port_occupied_while_launchd_has_no_process(self):
        """job 已装载但名下没有活进程(上次崩了),端口却有人 —— 同样是外来户。"""
        webui._service_status = lambda: {"source": "launchd", "pid": None}
        webui._port_owner_pid = lambda port: 24190
        self.assertEqual(webui._outsider_pid(), 24190)

    def test_no_listener_means_no_outsider(self):
        webui._service_status = lambda: {"source": "launchd", "pid": None}
        webui._port_owner_pid = lambda port: None
        self.assertIsNone(webui._outsider_pid())


class TestServiceStatus(unittest.TestCase):
    """状态查询必须在任何情况下都返回结构完整的字典(前端按字段渲染)。"""

    def test_always_returns_full_shape(self):
        s = webui._service_status()
        for key in ("ok", "label", "plist", "plist_exists",
                    "managed", "running", "pid", "uptime", "source"):
            self.assertIn(key, s)

    def test_unmanaged_and_no_process_is_not_running(self):
        """launchd 未装载、端口也没人监听 —— 那是真的停了。"""
        orig, orig_run = webui._launchctl, webui._run
        webui._launchctl = lambda *a, **k: (False, "Could not find service")
        webui._run = lambda *a, **k: (False, "")
        try:
            s = webui._service_status()
        finally:
            webui._launchctl, webui._run = orig, orig_run
        self.assertFalse(s["managed"])
        self.assertFalse(s["running"])
        self.assertIsNone(s["pid"])

    def test_unmanaged_but_manually_started_is_running(self):
        """迁移/重装后常见:launchd 没注册,但进程由一键启动手动拉起。

        只看 launchd 会把它误报成「服务已停止」,所以必须按端口兜底,
        认出这是运行中的实例并标记 source=manual 供前端提示补注册。
        """
        orig, orig_run = webui._launchctl, webui._run
        webui._launchctl = lambda *a, **k: (False, "Could not find service")

        def fake_run(*a, **k):
            if a[:1] == ("lsof",):
                return True, "43917"
            if a[:1] == ("ps",):
                return True, "01:23"
            return False, ""

        webui._run = fake_run
        try:
            s = webui._service_status()
        finally:
            webui._launchctl, webui._run = orig, orig_run
        self.assertFalse(s["managed"])
        self.assertTrue(s["running"])
        self.assertEqual(s["source"], "manual")
        self.assertEqual(s["pid"], 43917)

    def test_managed_but_not_launched_falls_back_to_probe(self):
        """launchd 已装载、但 job 自己没跑(当前实例是手动拉起的)。

        launchctl print 会给出 `state = spawn scheduled`、`active count = 0`,
        而实际进程活得好好的、录制弹幕都在跑。只信 launchd 会显示
        「已装载但未运行」,让用户以为整个服务挂了 —— 所以同样要回退探测端口。
        """
        orig = webui._launchctl
        orig_probe = webui._probe_unmanaged
        webui._launchctl = lambda *a, **k: (True, "\n".join([
            "gui/501/com.douyin.monitor = {",
            "\tactive count = 0",
            "\tstate = spawn scheduled",
            "\t\tstate = active",
        ]))
        webui._probe_unmanaged = lambda: {
            "running": True, "pid": 24190, "uptime": 1234, "source": "manual"}
        try:
            s = webui._service_status()
        finally:
            webui._launchctl, webui._probe_unmanaged = orig, orig_probe
        self.assertTrue(s["managed"], "已装载的事实不能被探测结果覆盖")
        self.assertTrue(s["running"])
        self.assertEqual(s["source"], "manual")
        self.assertEqual(s["pid"], 24190)

    def test_parses_pid_and_state_from_print(self):
        fake_out = "\n".join([
            "gui/501/com.douyin.monitor = {",
            "\tstate = running",
            "\tpid = 4242",
            "\t\tstate = active",
        ])
        orig = webui._launchctl
        webui._launchctl = lambda *a, **k: (True, fake_out)
        try:
            s = webui._service_status()
        finally:
            webui._launchctl = orig
        self.assertTrue(s["managed"])
        self.assertTrue(s["running"])
        self.assertEqual(s["pid"], 4242)

    def test_non_running_state_not_reported_as_running(self):
        """state 为其它值(如 waiting)且端口确实没人监听时,不能判成运行中。

        否则「关闭」按钮会消失。注意现在非 running 会回退探测端口,所以这里
        必须把探测也打桩成「没找到」,测的才是「真的没在跑」这个分支。
        """
        orig = webui._launchctl
        orig_probe = webui._probe_unmanaged
        webui._launchctl = lambda *a, **k: (True, "\tstate = waiting\n")
        webui._probe_unmanaged = lambda: {
            "running": False, "pid": None, "uptime": None, "source": None}
        try:
            s = webui._service_status()
        finally:
            webui._launchctl, webui._probe_unmanaged = orig, orig_probe
        self.assertTrue(s["managed"])
        self.assertFalse(s["running"])
        self.assertIsNone(s["pid"])


if __name__ == "__main__":
    unittest.main()
