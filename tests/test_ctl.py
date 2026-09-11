#!/usr/bin/env python3
"""终端快捷指令(scripts/ctl.py)的单元测试。

两条底线必须守住,都来自真实事故:
  ① **两个入口共用一份执行计划** —— Web 按钮与终端指令若各写一份「先清外来实例
     再 kickstart」的规则,迟早一边修了、另一边还留着老 Bug(上次「点重启没
     反应」就是这么来的)。所以这里既测计划本身,也测 Web 端确实照计划执行。
  ② **测试绝不真的启停服务** —— 那会杀掉正在跑的监控、中断录制。所有会落到
     launchctl 的路径都用 dry-run 或打桩拦下。
"""
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import webui  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "ctl", os.path.join(BASE, "scripts", "ctl.py"))
ctl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ctl)

# CLI 里的 kind 白名单:新增一种 kind 却忘了在 _run_action 里处理,
# 表现为「某一步被静默跳过」—— 这条测试就是拦住这种情况的。
KNOWN_KINDS = {"term", "kill", "pause", "bootout", "bootstrap", "kickstart"}


def _run_cli(argv):
    """在内存里跑一次 CLI,返回 (退出码, stdout 文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ctl.main(argv)
    return rc, buf.getvalue()


class _NoSideEffectMixin:
    """默认让计划里不出现外来进程、plist 视为存在,用例再按需覆盖。"""

    def setUp(self):
        self.orig_outsider = webui._outsider_pid
        self.orig_plist = webui.LAUNCHD_PLIST
        webui._outsider_pid = lambda: None
        self.addCleanup(setattr, webui, "_outsider_pid", self.orig_outsider)
        self.addCleanup(setattr, webui, "LAUNCHD_PLIST", self.orig_plist)


class TestServicePlan(_NoSideEffectMixin, unittest.TestCase):
    """执行计划:启 / 停 / 重启各自该做什么、按什么次序做。"""

    def test_unknown_action_yields_no_steps(self):
        ok, msg, steps = webui._service_plan("bogus")
        self.assertFalse(ok)
        self.assertIn("未知操作", msg)
        self.assertEqual(steps, [], "非法动作绝不能产生可执行的命令")

    def test_all_steps_are_wellformed(self):
        """每一步都要有 kind / label / cmd —— 终端端按 kind 决定怎么等。"""
        for action in ("restart", "stop", "start"):
            _, _, steps = webui._service_plan(action)
            self.assertTrue(steps, f"{action} 不该是空计划")
            for s in steps:
                self.assertIn(s["kind"], KNOWN_KINDS)
                self.assertTrue(s.get("cmd"), f"{action} 有步骤缺命令")
                self.assertTrue(s.get("label"), f"{action} 有步骤缺说明")

    def test_restart_without_outsider_is_plain_kickstart(self):
        ok, _, steps = webui._service_plan("restart")
        self.assertTrue(ok)
        self.assertEqual([s["kind"] for s in steps], ["kickstart"])
        self.assertIn("kickstart -k", steps[0]["cmd"])

    def test_restart_clears_outsider_before_kickstart(self):
        """端口被非 launchd 实例占着时,必须先请走它。

        kickstart 只作用于 launchd 名下进程;端口若被别人占着,新实例一 bind 就
        Address already in use,KeepAlive 再拉 —— 每 10 秒崩一次,而界面始终连着
        旧实例,用户看到的就是「点了重启没反应」。
        """
        webui._outsider_pid = lambda: 24190
        ok, msg, steps = webui._service_plan("restart")
        self.assertTrue(ok)
        self.assertIn("24190", msg)
        kinds = [s["kind"] for s in steps]
        self.assertEqual(kinds, ["term", "pause", "kill", "pause", "kickstart"])
        # 顺序:先 TERM 让它停 ffmpeg、落历史,再 KILL,最后才拉起
        joined = " ; ".join(s["cmd"] for s in steps)
        self.assertLess(joined.index("kill -TERM 24190"), joined.index("kill -KILL 24190"))
        self.assertLess(joined.index("kill -KILL 24190"), joined.index("kickstart"))
        # 只有真正需要清场的那两步带 pid,别的步骤不该带上(否则终端端会误等)
        self.assertEqual([s["pid"] for s in steps if s.get("pid")], [24190, 24190])

    def test_stop_is_bootout_only(self):
        ok, _, steps = webui._service_plan("stop")
        self.assertTrue(ok)
        self.assertEqual([s["kind"] for s in steps], ["bootout"])
        self.assertIn("bootout", steps[0]["cmd"])

    def test_start_unloads_before_bootstrap(self):
        """已装载时裸 bootstrap 会报 already loaded,必须先 bootout。"""
        _, _, steps = webui._service_plan("start")
        kinds = [s["kind"] for s in steps]
        self.assertEqual(kinds[0], "bootout")
        self.assertIn("bootstrap", kinds)
        self.assertLess(kinds.index("bootout"), kinds.index("bootstrap"))
        self.assertEqual(kinds[-1], "kickstart")

    def test_missing_plist_blocks_start_and_restart(self):
        webui.LAUNCHD_PLIST = "/definitely/not/here.plist"
        for action in ("start", "restart"):
            ok, msg, steps = webui._service_plan(action)
            self.assertFalse(ok)
            self.assertIn("launchd 配置", msg)
            self.assertEqual(steps, [])

    def test_commands_only_touch_own_label(self):
        """安全边界:命令里只能出现本项目自己的固定 label。"""
        for action in ("restart", "stop", "start"):
            _, _, steps = webui._service_plan(action)
            joined = " ".join(s["cmd"] for s in steps)
            for other in ("com.apple", "com.douyin.other", "launchctl remove"):
                self.assertNotIn(other, joined)


class TestControlSharesPlan(unittest.TestCase):
    """回归:Web 按钮执行的命令必须**逐字**来自同一份计划。"""

    def setUp(self):
        self.orig_popen = subprocess.Popen
        self.calls = []
        subprocess.Popen = self._fake_popen
        self.addCleanup(setattr, subprocess, "Popen", self.orig_popen)
        self.orig_outsider = webui._outsider_pid
        webui._outsider_pid = lambda: None
        self.addCleanup(setattr, webui, "_outsider_pid", self.orig_outsider)

    def _fake_popen(self, args, **kw):
        self.calls.append((args, kw))
        return object()

    def _spawn_cmd(self):
        for args, _ in reversed(self.calls):
            if args and args[0] == "/bin/sh":   # subprocess.run 也走 Popen,靠这个认
                return args[2]
        self.fail("未找到延迟执行的命令")

    def test_joined_command_matches_plan(self):
        for action in ("restart", "stop", "start"):
            self.calls.clear()
            _, _, steps = webui._service_plan(action)
            self.calls.clear()
            ok, _ = webui._service_control(action)
            self.assertTrue(ok)
            self.assertIn("; ".join(s["cmd"] for s in steps), self._spawn_cmd(),
                          f"{action} 的命令与计划不一致 —— 两个入口已经开始分叉")

    def test_still_delayed_and_detached(self):
        """延迟是为了让 HTTP 响应先发出去;脱离进程组是为了本进程死了命令还在。"""
        webui._service_control("restart")
        _, kw = self.calls[-1]
        self.assertTrue(self._spawn_cmd().startswith("sleep "))
        self.assertTrue(kw.get("start_new_session"))


class TestCliDispatch(unittest.TestCase):
    """命令分发与别名:敲哪个词对应哪个动作。"""

    def test_aliases_map_to_the_same_action(self):
        parser = ctl.build_parser()
        expect = {
            "status": ctl.cmd_status, "st": ctl.cmd_status, "s": ctl.cmd_status,
            "start": ctl.cmd_start, "on": ctl.cmd_start, "up": ctl.cmd_start,
            "stop": ctl.cmd_stop, "off": ctl.cmd_stop, "down": ctl.cmd_stop,
            "restart": ctl.cmd_restart, "r": ctl.cmd_restart, "reload": ctl.cmd_restart,
            "health": ctl.cmd_health, "doctor": ctl.cmd_doctor, "open": ctl.cmd_open,
            "help": ctl.cmd_help,
        }
        for word, fn in expect.items():
            args = parser.parse_args([word])
            self.assertIs(args.fn, fn, f"{word} 没映射到 {fn.__name__}")

    def test_no_args_shows_status(self):
        """什么都不带 = 看状态,这是最常用的一个。"""
        args = ctl.build_parser().parse_args([])
        self.assertFalse(getattr(args, "fn", None), "默认动作由 main 兜底指定")
        orig_http = ctl._http
        ctl._http = lambda *a, **k: None      # 不依赖服务是否在跑,测试要确定
        self.addCleanup(setattr, ctl, "_http", orig_http)
        rc, out = _run_cli([])
        self.assertEqual(rc, ctl.SUCCESS)
        self.assertIn("服务", out)

    def test_unknown_command_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            ctl.build_parser().parse_args(["bogus"])
        self.assertEqual(cm.exception.code, 2)

    def test_no_free_form_path_arguments(self):
        """安全边界:不接受任何路径/标签参数,免得被诱导去操作别的服务。"""
        for argv in (["stop", "/tmp/x"], ["start", "--plist", "/tmp/x"],
                     ["restart", "com.other.job"]):
            with self.assertRaises(SystemExit):
                ctl.build_parser().parse_args(argv)

    def test_log_defaults(self):
        args = ctl.build_parser().parse_args(["log"])
        self.assertEqual(args.lines, 30)
        self.assertFalse(args.follow)


class TestDryRun(_NoSideEffectMixin, unittest.TestCase):
    """干跑:只打印计划,一条命令都不执行 —— 让人先看清再点头。"""

    def setUp(self):
        super().setUp()
        self.orig_sh = ctl._sh
        ctl._sh = lambda *a, **k: self.fail("干跑模式不该执行任何命令")
        self.addCleanup(setattr, ctl, "_sh", self.orig_sh)

    def test_dry_run_executes_nothing(self):
        for argv in (["restart", "-n"], ["stop", "--dry-run"], ["start", "-n"]):
            rc, out = _run_cli(argv)
            self.assertEqual(rc, ctl.SUCCESS, argv)
            self.assertIn("干跑", out)
            self.assertIn("launchctl", out)

    def test_dry_run_shows_outsider_step(self):
        webui._outsider_pid = lambda: 24190
        _, out = _run_cli(["restart", "-n"])
        self.assertIn("24190", out)
        self.assertIn("kill -TERM", out)


class TestActionGuards(_NoSideEffectMixin, unittest.TestCase):
    def test_action_fails_loudly_without_plist(self):
        webui.LAUNCHD_PLIST = "/definitely/not/here.plist"
        rc, out = _run_cli(["start", "-n"])
        self.assertEqual(rc, ctl.FAILURE)
        self.assertIn("launchd 配置", out)


class TestCheatsheet(unittest.TestCase):
    """命令速查(`douyin help`,以及「一键启动.command」窗口里显示的那张表)。

    它是「有哪些命令、各自什么效果」的唯一真源:启动窗口、终端指令、argparse
    的子命令全部从 COMMANDS / ZSH_ALIASES 渲染。这里守三件事:
      ① 速查表里列出来的必须真的能敲(反向也成立);
      ② 表里写的短别名与 install_cli.sh 写进 ~/.zshrc 的那份逐字一致;
      ③ 启动脚本只负责显示,不许自己抄一份命令表 —— 抄一份就会过期一份。
    """

    def setUp(self):
        # 服务没起来(甚至从没启动)时也得能看速查表,所以任何 HTTP 都算越界
        self.orig_http = ctl._http
        ctl._http = lambda *a, **k: self.fail("命令速查不该访问服务接口")
        self.addCleanup(setattr, ctl, "_http", self.orig_http)

    def test_lists_every_command_with_effect(self):
        rc, out = _run_cli(["help"])
        self.assertEqual(rc, ctl.SUCCESS)
        for name, _aliases, effect, usage in ctl.COMMANDS:
            self.assertIn(usage, out, f"{name} 没出现在速查表里")
            self.assertIn(effect, out, f"{name} 少了「效果」说明")

    def test_short_mode_is_compact(self):
        """一键启动窗口里一屏要放得下 —— 短版必须比完整版短,且不省命令。"""
        _, short = _run_cli(["help", "--short"])
        _, full = _run_cli(["help"])
        self.assertLess(len(short.splitlines()), len(full.splitlines()))
        self.assertLessEqual(len(short.rstrip().splitlines()), 16)
        for name, _aliases, _effect, usage in ctl.COMMANDS:
            self.assertIn(usage, short, f"{name} 在短版里被省掉了")

    def test_subcommand_list_matches_table(self):
        """反向:解析器里真能敲的命令,速查表里必须都露过面。"""
        orig = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "200"      # 免得帮助文本折行把子命令列表切断

        def _restore():
            if orig is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = orig
        self.addCleanup(_restore)

        text = ctl.build_parser().format_help()
        m = re.search(r"\{([^}]+)\}", text, re.S)
        self.assertIsNotNone(m, "帮助文本里找不到子命令列表")
        declared = {w.strip() for w in m.group(1).replace("\n", "").split(",")
                    if w.strip()}
        expected = set()
        for name, aliases, _effect, _usage in ctl.COMMANDS:
            expected.add(name)
            expected.update(aliases)
        self.assertEqual(declared, expected, "速查表与实际能敲的命令分叉了")

    def test_zsh_aliases_match_installer(self):
        """短别名在 ctl.py 与 install_cli.sh 各写一处,必须逐字一致。"""
        with open(os.path.join(BASE, "scripts", "install_cli.sh"),
                  encoding="utf-8") as f:
            text = f.read()
        written = re.findall(r"^alias\s+(\w+)='([^']+)'", text, re.M)
        self.assertTrue(written, "install_cli.sh 里没找到别名,是不是换了写法?")
        self.assertEqual([(n, t) for n, t, _ in ctl.ZSH_ALIASES], written)

    def test_launcher_renders_from_same_source(self):
        """启动窗口里的表必须来自 scripts/ctl.py,不能另抄一份。"""
        with open(os.path.join(BASE, "一键启动.command"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("scripts/ctl.py", text, "启动脚本没引用共享的命令表")
        self.assertIn("help --short", text, "启动脚本没渲染速查表")

    def test_launcher_syntax_ok(self):
        rc = subprocess.run(["/bin/bash", "-n",
                             os.path.join(BASE, "一键启动.command")],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)


class TestHelpers(unittest.TestCase):
    def test_pid_alive(self):
        self.assertTrue(ctl._pid_alive(os.getpid()))
        self.assertFalse(ctl._pid_alive(999999))   # 几乎不可能存在的 pid
        self.assertFalse(ctl._pid_alive(None))

    def test_wait_returns_true_when_satisfied(self):
        self.assertTrue(ctl._wait(lambda: True, 1))

    def test_wait_times_out(self):
        self.assertFalse(ctl._wait(lambda: False, 0.3, step=0.1))

    def test_dot_has_no_ansi_when_not_tty(self):
        """非终端(重定向到文件/管道)时不吐转义符,否则日志里全是乱码。"""
        orig = ctl._use_color
        ctl._use_color = lambda: False
        self.addCleanup(setattr, ctl, "_use_color", orig)
        self.assertEqual(ctl._dot("ok"), ctl.DOTS["ok"])

    def test_doctor_flags_pythonpath_and_proxy(self):
        """doctor 守的是本项目最贵的两个坑:sitecustomize 注入与残留代理。"""
        env = dict(os.environ)
        orig = dict(os.environ)
        try:
            os.environ["PYTHONPATH"] = "/tmp/shim"
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
            rc, out = _run_cli(["doctor"])
            self.assertEqual(rc, ctl.FAILURE)
            self.assertIn("PYTHONPATH", out)
            self.assertIn("HTTP_PROXY", out)
        finally:
            os.environ.clear()
            os.environ.update(orig)
            self.assertEqual(dict(os.environ), env)

    def test_doctor_clean_env_passes(self):
        orig = dict(os.environ)
        try:
            for k in ("PYTHONPATH", "http_proxy", "https_proxy", "all_proxy",
                      "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                os.environ.pop(k, None)
            os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            rc, out = _run_cli(["doctor"])
            self.assertEqual(rc, ctl.SUCCESS)
            self.assertIn("干净", out)
        finally:
            os.environ.clear()
            os.environ.update(orig)


if __name__ == "__main__":
    unittest.main()
