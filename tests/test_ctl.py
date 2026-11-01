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
import shutil
import subprocess
import sys
import tempfile
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
    """默认让计划里不出现外来进程、job 视为未装载,用例再按需覆盖。"""

    def setUp(self):
        self.orig_outsider = webui._outsider_pid
        self.orig_plist = webui.LAUNCHD_PLIST
        self.orig_status = webui._service_status
        webui._outsider_pid = lambda: None
        # start 现在要看 launchd 装载状态才决定 kickstart 还是 bootstrap。
        # 不打桩就会读本机真实状态 —— 测试结果会随「这台机器上服务在不在跑」漂移,
        # 而这类测试恰恰必须在录制中的机器上也能跑绿。
        webui._service_status = lambda: {"managed": False, "running": False,
                                         "source": None, "pid": None}
        self.addCleanup(setattr, webui, "_outsider_pid", self.orig_outsider)
        self.addCleanup(setattr, webui, "LAUNCHD_PLIST", self.orig_plist)
        self.addCleanup(setattr, webui, "_service_status", self.orig_status)


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

    def test_start_never_unloads_the_job(self):
        """start 绝不能 bootout —— 那会 SIGTERM 掉正在录制的进程。

        来历(2026-10-29 实测):start 曾无条件 bootout → bootstrap,理由是
        「job 已装载时裸 bootstrap 会报 already loaded」。这条理由只在「已装载
        但进程没跑」时成立,却被套到了所有状态上:bootout 会给正在跑的进程发
        SIGTERM,一条 douyin start 于是中断了 2 路录制;而它记下的退出时刻还会
        打开 launchd 的 ThrottleInterval(60 秒)节流窗口,新实例要等满一分钟才
        spawn —— 用户看到的正是「重启失败」。
        """
        for managed in (False, True):
            webui._service_status = lambda m=managed: {
                "managed": m, "running": m,
                "source": "launchd" if m else None, "pid": 4242 if m else None}
            _, _, steps = webui._service_plan("start")
            joined = " ".join(s["cmd"] for s in steps)
            self.assertNotIn("bootout", joined,
                             "start 又开始卸载 job 了 —— 在录的服务会被一起掐掉")
            self.assertNotIn("kickstart -k", joined,
                             "start 不该带 -k:它只保证服务在跑,不该杀在跑的实例")

    def test_start_when_loaded_just_kickstarts(self):
        """job 已装载就只 kickstart(不带 -k),不再重装一遍。

        「已装载但进程没跑」时裸 bootstrap 确实报 already loaded —— 正确解法是
        kickstart 把 job 拉起来,而不是先卸载再装载。
        """
        webui._service_status = lambda: {"managed": True, "running": False,
                                         "source": "launchd", "pid": None}
        ok, _, steps = webui._service_plan("start")
        self.assertTrue(ok)
        self.assertEqual([s["kind"] for s in steps], ["kickstart"])
        self.assertIn(webui.LAUNCHD_LABEL, steps[0]["cmd"])

    def test_start_bootstraps_only_when_not_loaded(self):
        """未装载才需要 bootstrap;装载完再 kickstart 一次确保真的起来。"""
        ok, _, steps = webui._service_plan("start")
        self.assertTrue(ok)
        self.assertEqual([s["kind"] for s in steps], ["bootstrap", "kickstart"])

    def test_start_refuses_when_outsider_holds_port(self):
        """端口被非托管实例占着时,start 不猜也不动手。

        照旧 bootstrap 的话,新实例一 bind 就 Address already in use、退出,
        KeepAlive 再拉 —— 每 60 秒崩一次(2026-09-11 那次 err.log 涨到 7.5MB
        就是这个循环)。清场带收尾流程,那是 restart 的职责。
        """
        webui._outsider_pid = lambda: 24190
        ok, msg, steps = webui._service_plan("start")
        self.assertFalse(ok)
        self.assertIn("24190", msg)
        self.assertIn("重启", msg, "拒绝时必须指出该走哪条命令")
        self.assertEqual(steps, [], "拒绝时不能留下任何可执行的命令")

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
        self.orig_status = webui._service_status
        webui._outsider_pid = lambda: None
        # 与 _NoSideEffectMixin 同理:start 的计划取决于本机装载状态,必须打桩
        webui._service_status = lambda: {"managed": False, "running": False,
                                         "source": None, "pid": None}
        self.addCleanup(setattr, webui, "_outsider_pid", self.orig_outsider)
        self.addCleanup(setattr, webui, "_service_status", self.orig_status)

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
      ③ 启动脚本只负责显示,不许自己抄一份命令表 —— 抄一份就会过期一份;
      ④ 启动窗口的快捷操作(打开控制台/状态/重启/关闭)必须还在,且按键转发到的
         命令必须真实存在 —— 否则就是个按键下去毫无反应的哑键。
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
        text = self._launcher_text()
        self.assertIn("scripts/ctl.py", text, "启动脚本没引用共享的命令表")
        self.assertIn("help --short", text, "启动脚本没渲染速查表")

    def _launcher_text(self):
        with open(os.path.join(BASE, "一键启动.command"), encoding="utf-8") as f:
            return f.read()

    def test_launcher_variables_before_cjk_are_braced(self):
        """变量后面紧跟中文必须写 `${var}`。

        实测过的坑:bash 3.2(macOS 自带)不是多字节感知的,`$what会中断` 会被
        解析成变量 `what会中断` —— `set -u` 下直接 unbound variable 崩掉,
        而且要等到「正在录制时按 q」那条分支才暴露(平时测不到)。
        """
        # 注释里出现的示例不算(注释不会被 bash 解析)
        body = "\n".join(ln for ln in self._launcher_text().splitlines()
                         if not ln.lstrip().startswith("#"))
        bad = re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", body)
        self.assertEqual(bad, [], "变量后紧跟中文会被当成变量名的一部分:%s" % bad)

    def test_launcher_console_quick_actions(self):
        """窗口里的四个快捷操作不许被删:改文案可以,改到按键消失不行。

        「o 打开控制台」是 2026-10-29 加的 —— 之前想开浏览器只能记 http 地址。
        """
        text = self._launcher_text()
        for line in ("o  打开 Web 控制台", "s  检测运行状态",
                     "r  重启 / 启动", "q  关闭服务"):
            self.assertIn(line, text, f"启动窗口少了快捷操作「{line}」")

    def test_launcher_console_forwards_to_real_commands(self):
        """按键动作必须转发到 ctl.py 里真实存在的命令,不能凭空写一个。

        这里收**全部** `ctl <子命令>` 转发、再和 COMMANDS 对账,而不是写白名单 ——
        白名单会漏掉打错的那个词(把 open 打成 openweb,白名单根本匹配不上,
        测试照样是绿的,只有按键下去才发现没反应)。
        """
        used = set(re.findall(r"\bctl ([a-z][a-z_-]*)", self._launcher_text()))
        self.assertTrue(used, "启动脚本没有转发任何 ctl 命令(菜单就成了摆设)")
        names = {c[0] for c in ctl.COMMANDS}
        self.assertEqual(used - names, set(),
                         "启动脚本调用了 ctl.py 里不存在的命令,按键会没反应")

    def test_launcher_syntax_ok(self):
        rc = subprocess.run(["/bin/bash", "-n",
                             os.path.join(BASE, "一键启动.command")],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)


class TestLauncherDirGuard(unittest.TestCase):
    """启动器必须认得出「这个目录里的代码不是同一代」。

    来历(2026-10-29 事故):~/Downloads 下并存历史发布快照目录。凌晨把新版
    「一键启动.command」拷进 8 月那份快照里双击 —— 那里的 monitor.py、
    static/index.html 还停在三个月前那一代,于是端口上跑起一个「入口新、
    页面旧」的杂交实例,整站界面回退;launchd 的正式服务每 60 秒撞一次端口
    退出,而外层探活只看 HTTP 200 就报了"响应正常",所以谁都没察觉。

    这里守三件事:
      ① 目录自检发生在**任何进程被拉起之前**(自检晚了就白检);
      ② 探活成功 ≠ 服务是本目录的 —— 必须比对实例身份;
      ③ 锚点表要与真实仓库对得上(写错锚点会让每次启动都被拒)。
    """

    def _text(self):
        with open(os.path.join(BASE, "一键启动.command"), encoding="utf-8") as f:
            return f.read()

    def test_verify_runs_before_any_spawn(self):
        text = self._text()
        self.assertIn("if ! verify_project; then", text,
                      "启动脚本没有目录自检")
        # 顶格的 start_child 才是「真的去拉进程」那一行(定义写作 start_child() {)
        spawn = text.index("\nstart_child\n")
        self.assertLess(text.index("if ! verify_project; then"), spawn,
                        "目录自检排在拉起进程之后 —— 那时端口已经被抢了")

    def test_generation_anchors_exist_in_repo(self):
        """锚点与真实仓库对账:写错会变成「每次启动都被拒」。

        不写死符号名,而是从脚本里的锚点表解析 —— 换实现时改脚本即同步,
        但改漏了(脚本里写着、仓库里没有)会在这里立刻报红。
        """
        text = self._text()
        begin = text.index("check_same_generation() {")
        end = text.index("verify_project() {")
        anchors = re.findall(r'"([\w./]+)\|([^"]+)"', text[begin:end])
        self.assertTrue(anchors, "脚本里解析不到同代锚点,是不是换了写法?")
        for rel, pat in anchors:
            path = os.path.join(BASE, rel)
            self.assertTrue(os.path.isfile(path), "锚点指向的文件不存在: %s" % rel)
            with open(path, encoding="utf-8", errors="replace") as f:
                self.assertIn(pat, f.read(),
                              "%s 里找不到锚点 %s —— 换实现可以,锚点表要同步改"
                              % (rel, pat))

    def test_instance_identity_not_just_http_200(self):
        """探活通过只证明端口上有人讲 HTTP,不能当成「跑的是本目录」。"""
        text = self._text()
        self.assertIn("instance_root", text, "启动脚本没有比对实例身份")
        self.assertIn("project_root", text, "没有走后端自报的 project_root")
        # 后端还没升到带 project_root 的版本时靠 cwd 兜底,否则退化成"身份不明"
        # —— 而"身份不明"正是这次事故里最危险的状态。
        self.assertIn("-d cwd", text, "缺少 lsof cwd 兜底")
        main = text[text.index("# ══ 主流程"):]
        self.assertIn("instance_root", main, "主流程没用上身份比对")

    def test_open_browser_only_after_identity_check(self):
        """起服务后「等就绪再开浏览器」也必须验身份,不能只看 HTTP 通不通。

        2026-10-29 事故的完整链条里,这是最后一环:本目录的 monitor 因为端口被
        历史副本占着而启动即退出,而轮询只要 probe 成功就 open —— 屏幕上出现
        的是别人那份的页面(界面回退三个月),窗口里却写着「已启动」。
        """
        text = self._text()
        begin = text.index("open_browser_when_ready() {")
        end = text.index("cleanup_poller() {")
        body = text[begin:end]
        self.assertIn("instance_root", body, "开浏览器前没有校验实例身份")
        self.assertIn('"$r" = "$ROOT"', body, "身份相等才该开浏览器")

    def test_child_launch_uses_absolute_path(self):
        """子进程 cmdline 要带 monitor.py 的绝对路径。

        否则 `pkill -9 -f "$ROOT/monitor.py"` 这条兜底清理认不出自己拉起的进程
        (相对路径的 cmdline 里没有这个绝对路径),僵死时清不掉、只能手工 kill。
        """
        text = self._text()
        self.assertIn('"$PY" -u "$ROOT/monitor.py"', text,
                      "启动子进程仍用相对路径,pkill 兜底会失效")
        self.assertNotIn('"$PY" -u monitor.py', text)

    def test_side_dir_hands_over_to_official(self):
        """在非正式目录双击时,要能把人直接转交给正式目录那一份。

        这就是「让它启动最新版本」的实际动作:不做静默跨目录启动(那会让人以为
        在 A 目录跑),而是问一句后 exec 转交,规则仍只有这一处。
        """
        text = self._text()
        self.assertIn('exec /bin/bash "$official/一键启动.command"', text,
                      "非正式目录没有转交正式目录的路径")


        with open(os.path.join(BASE, "webui.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('st["project_root"] = BASE', src,
                      "/api/status 没自报 project_root,启动器只能靠 cwd 猜")

    def test_legacy_snapshot_dir_is_refused(self):
        """真跑一次:文件齐但停在旧代的目录必须被拒,且不得拉起任何进程。

        判据是「输出里说明原因」而不是只看退出码 —— 自检失效时脚本会继续往下
        走,在临时目录里因为找不到 setup.sh 同样非零退出,只测退出码是测不出的。
        """
        tmp = tempfile.mkdtemp(prefix="dy-launcher-guard-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in ("monitor.py", "webui.py", "static/index.html",
                    "scripts/ctl.py"):
            p = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("# 历史快照占位:不含本代符号\n")
        shutil.copy2(os.path.join(BASE, "一键启动.command"),
                     os.path.join(tmp, "一键启动.command"))
        rc = subprocess.run(
            ["/bin/bash", os.path.join(tmp, "一键启动.command")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env=dict(os.environ, DOUYIN_ALLOW_SIDE_COPY="1"), timeout=60)
        out = rc.stdout + rc.stderr
        self.assertNotEqual(rc.returncode, 0,
                            "旧代目录被放行了 —— 会跑起「入口新、页面旧」的实例")
        self.assertIn("不是同一代", out, "拒绝原因没讲清楚:\n%s" % out)


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
