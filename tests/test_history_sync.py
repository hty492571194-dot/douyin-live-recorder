#!/usr/bin/env python3
"""history 变更同步到前端 + 归档落点兜底探测 的单元测试(临时目录,不碰真实数据)。

覆盖的是同一条故障链上的两个根因:
  1. 归档状态回写时只落盘、没同步给前端 state → 前端/弹幕面板读到旧快照,
     已归档场次被当成"本机文件已不在"→ 误报「NAS 未挂载」。
  2. monitor.py 作为脚本运行时模块名是 __main__,webui 里 `import monitor`
     会拿到一份全新副本(其 _history 恒为空),既读不到实时状态,
     写也写在副本上。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import nas as nas_mod  # noqa: E402
import webui  # noqa: E402

DAY = "2026-08-29"
STEM = "主播A-20260829-120000"
ROOT_DIR = "直播回放"


class FakeState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.history = []

    def get_config(self):
        return self.cfg

    def set_history(self, h):
        self.history = h


class TestArchiveStateSync(unittest.TestCase):
    """归档状态回写必须同时落盘 + 同步前端(只落盘会留下过期快照)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        self.session = os.path.join(self.rec, "主播A", DAY)
        os.makedirs(self.session, exist_ok=True)
        self.out = os.path.join(self.session, STEM + "-%03d.flv")

        # 真实 _save_history 会写项目里的 recordings/history.json,测试必须拦掉
        self.orig_save = monitor._save_history
        self.orig_history = monitor._history
        self.orig_web_state = monitor._web_state
        self.saved = []
        monitor._save_history = lambda: self.saved.append(list(monitor._history))
        monitor._history = [{"name": "主播A", "output_path": self.out,
                             "status": "done", "archive": {"state": "none"}}]
        self.state = FakeState({})
        monitor._web_state = self.state

    def tearDown(self):
        monitor._save_history = self.orig_save
        monitor._history = self.orig_history
        monitor._web_state = self.orig_web_state
        self.tmp.cleanup()

    def test_archive_state_pushed_to_web_state(self):
        """归档完成后,前端 state 里必须已是 archived + dest,而不是停留在 none。"""
        dest = os.path.join("/nas", ROOT_DIR, "主播A", DAY)
        monitor._mark_archive_state(self.session, "archived", dest=dest, target="nas")
        self.assertEqual(len(self.saved), 1, "归档状态应当落盘")
        self.assertEqual(self.state.history[0]["archive"]["state"], "archived")
        self.assertEqual(self.state.history[0]["archive"]["dest"], dest)

    def test_pending_state_also_pushed(self):
        """入队(pending)同样要同步——不然前端会一直显示"未归档"。 """
        monitor._mark_archive_state(self.session, "pending", target="nas")
        self.assertEqual(self.state.history[0]["archive"]["state"], "pending")

    def test_no_web_state_does_not_raise(self):
        """_web_state 未注入(如退出清理场景)时只落盘,不能抛异常。"""
        monitor._web_state = None
        monitor._mark_archive_state(self.session, "archived",
                                    dest=os.path.join("/nas", ROOT_DIR, "主播A", DAY))
        self.assertEqual(len(self.saved), 1)

    def test_history_add_and_finish_push(self):
        """开播/收尾两条链路同样走统一出口(防止后续改动漏同步)。"""
        monitor._history_add(self.state, "主播B", os.path.join(self.session, "b-%03d.flv"), 1.0)
        self.assertTrue(any(e["output_path"].endswith("b-%03d.flv") for e in self.state.history))
        monitor._history_finish(self.state, "主播B", ended_at=2.0)
        self.assertEqual(len(self.saved), 2)


class TestMountProbeCache(unittest.TestCase):
    """挂载探测必须缓存——否则历史列表每条记录都要 spawn 一次 mount。"""

    def setUp(self):
        self.orig = nas_mod._is_mounted
        self.calls = {"n": 0}
        nas_mod._is_mounted = self._counting
        webui._MOUNT_CACHE.update(key=None, ts=0.0, val=False)

    def tearDown(self):
        nas_mod._is_mounted = self.orig
        webui._MOUNT_CACHE.update(key=None, ts=0.0, val=False)

    def _counting(self, mp):
        self.calls["n"] += 1
        return True

    def test_repeated_calls_hit_cache(self):
        cfg = {"nas": {"enabled": True, "mount_point": "/mnt/a", "root_dir": ROOT_DIR}}
        for _ in range(50):
            self.assertTrue(webui._nas_mounted(cfg))
        self.assertEqual(self.calls["n"], 1, "50 次查询只应真正探测 1 次")

    def test_cache_expires_after_ttl(self):
        cfg = {"nas": {"enabled": True, "mount_point": "/mnt/b", "root_dir": ROOT_DIR}}
        webui._nas_mounted(cfg)
        webui._MOUNT_CACHE["ts"] -= (webui._MOUNT_TTL + 1)
        webui._nas_mounted(cfg)
        self.assertEqual(self.calls["n"], 2, "超过 TTL 应重新探测")

    def test_ttl_zero_forces_probe(self):
        cfg = {"nas": {"enabled": True, "mount_point": "/mnt/c", "root_dir": ROOT_DIR}}
        webui._nas_mounted(cfg)
        webui._nas_mounted(cfg, ttl=0)
        self.assertEqual(self.calls["n"], 2)

    def test_cleared_cache_does_not_raise(self):
        """缓存字典被清空后必须退化为重新探测,不能 KeyError 让接口 500。"""
        cfg = {"nas": {"enabled": True, "mount_point": "/mnt/d", "root_dir": ROOT_DIR}}
        webui._MOUNT_CACHE.clear()
        try:
            self.assertTrue(webui._nas_mounted(cfg))
        except KeyError:
            self.fail("缓存被清空后 _nas_mounted 抛了 KeyError")
        self.assertEqual(self.calls["n"], 1)

    def test_cache_keyed_by_mount_point(self):
        """换了挂载点不能复用上一个的缓存结果。"""
        webui._nas_mounted({"nas": {"enabled": True, "mount_point": "/mnt/d"}})
        webui._nas_mounted({"nas": {"enabled": True, "mount_point": "/mnt/e"}})
        self.assertEqual(self.calls["n"], 2)

    def test_disabled_nas_never_probes(self):
        self.assertFalse(webui._nas_mounted({"nas": {"enabled": False}}))
        self.assertEqual(self.calls["n"], 0)


class TestArchiveHookWiring(unittest.TestCase):
    """按 main() 的方式注册归档钩子,验证 nas 回调 → 前端 state 整条链路。

    单测 _mark_archive_state 只能证明函数本身对;这条证明「回调确实接上了」——
    钩子漏注册或改签名,只会表现为归档后前端状态不动,很难从代码上看出。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = os.path.join(self.tmp.name, "X", DAY)
        os.makedirs(self.session, exist_ok=True)
        self.out = os.path.join(self.session, "X-20260901-120000-%03d.flv")
        self.orig_save = monitor._save_history
        self.orig_history = monitor._history
        self.orig_web_state = monitor._web_state
        self.orig_hook = nas_mod._archive_hook
        monitor._save_history = lambda: None      # 不落盘,避免污染真实 history.json
        monitor._history = [{"name": "X", "output_path": self.out, "status": "done",
                             "archive": {"state": "none"}}]
        self.state = FakeState({})
        monitor._web_state = self.state

    def tearDown(self):
        monitor._save_history = self.orig_save
        monitor._history = self.orig_history
        monitor._web_state = self.orig_web_state
        nas_mod.set_archive_hook(self.orig_hook)
        self.tmp.cleanup()

    def test_hook_delivers_state_to_frontend(self):
        nas_mod.set_archive_hook(monitor._on_archive_result)   # 与 main() 一致
        dest = os.path.join("/nas", ROOT_DIR, "X", DAY)
        nas_mod._notify_archive(self.session, "pending", target="nas")
        self.assertEqual(self.state.history[0]["archive"]["state"], "pending")
        nas_mod._notify_archive(self.session, "archived", dest=dest, target="nas")
        self.assertEqual(self.state.history[0]["archive"]["state"], "archived")
        self.assertEqual(self.state.history[0]["archive"]["dest"], dest)


class FakeMainModule:
    """模拟「monitor.py 作为 __main__ 运行」的模块对象。"""

    def __init__(self, history):
        self._history = history


class TestMonitorLiveModule(unittest.TestCase):
    """_monitor_live 必须拿到真正跑着监控循环的模块,而不是 import 出来的副本。"""

    def test_prefers_main_when_it_has_history(self):
        fake = FakeMainModule([{"output_path": "x"}])
        orig = sys.modules.get("__main__")
        sys.modules["__main__"] = fake
        try:
            self.assertIs(webui._monitor_live(), fake)
        finally:
            sys.modules["__main__"] = orig

    def test_falls_back_to_imported_module(self):
        """__main__ 没有 _history(单测里就是如此)时回退到 import 的 monitor。"""
        sentinel = object()
        orig = sys.modules.get("__main__")
        sys.modules["__main__"] = sentinel
        try:
            self.assertIs(webui._monitor_live(), monitor)
        finally:
            sys.modules["__main__"] = orig

    def test_duplicate_import_is_a_distinct_module(self):
        """回归护栏:确认 `import monitor` 与 __main__ 确实是两个对象。

        这条断言本身就是这个 bug 存在过的证据——将来若改成 `python -m monitor`
        (模块名变成 monitor)或把 monitor 拆成包,这条会先报警。
        """
        fake = FakeMainModule([])
        orig = sys.modules.get("__main__")
        sys.modules["__main__"] = fake
        try:
            import importlib
            copy_mod = importlib.import_module("monitor")
            if fake is not copy_mod:
                self.assertIsNot(webui._monitor_live(), copy_mod)
        finally:
            sys.modules["__main__"] = orig


class TestArchivedDirFallback(unittest.TestCase):
    """归档元数据丢失时,按 <根>/<主播>/<日期> 结构兜底定位。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        self.nas = os.path.join(self.base, "nas")
        self.arch = os.path.join(self.nas, ROOT_DIR, "主播A", DAY)
        os.makedirs(self.arch, exist_ok=True)          # NAS 上已有该场次
        self.out = os.path.join(self.rec, "主播A", DAY, STEM + "-%03d.flv")
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": True, "mount_point": self.nas, "root_dir": ROOT_DIR},
            "archive": {"external_dir": ""},
        }
        self.state = FakeState(self.cfg)
        self.orig_nas_mounted = webui._nas_mounted
        webui._nas_mounted = lambda cfg: True

    def tearDown(self):
        webui._nas_mounted = self.orig_nas_mounted
        self.tmp.cleanup()

    def test_probe_finds_archived_dir(self):
        self.assertEqual(webui._probe_archived_dir(self.state, self.out), self.arch)

    def test_probe_ignores_non_date_layout(self):
        """路径形状不是 <主播>/<日期> 时直接放弃,不做任何扫描。"""
        flat = os.path.join(self.rec, "主播A", STEM + "-%03d.flv")
        self.assertIsNone(webui._probe_archived_dir(self.state, flat))

    def test_probe_returns_none_when_nas_offline(self):
        webui._nas_mounted = lambda cfg: False
        self.assertIsNone(webui._probe_archived_dir(self.state, self.out))

    def test_safe_output_rescues_session_without_archive_meta(self):
        """端到端:history 里归档状态缺失、本机文件也已移走 → 仍能定位到 NAS。"""
        self.state.history = [{"name": "主播A", "output_path": self.out,
                               "status": "done", "archive": {"state": "none"}}]
        got = webui._danmaku_safe_output(self.state, self.out)
        self.assertEqual(got, os.path.join(self.arch, STEM + "-%03d.flv"))

    def test_safe_output_keeps_local_path_when_files_still_here(self):
        """本机文件还在时不许乱指到归档目标。"""
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        open(os.path.join(os.path.dirname(self.out), STEM + "-000.flv"), "w").close()
        self.state.history = [{"name": "主播A", "output_path": self.out,
                               "status": "done", "archive": {"state": "none"}}]
        self.assertEqual(webui._danmaku_safe_output(self.state, self.out), self.out)


class TestDirReachable(unittest.TestCase):
    """目录可达性判定不能因 mount 解析的微小差异而误杀。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.nas = os.path.join(self.base, "nas")
        self.d = os.path.join(self.nas, ROOT_DIR, "主播A", DAY)
        os.makedirs(self.d, exist_ok=True)
        self.cfg = {"nas": {"enabled": True, "mount_point": self.nas,
                            "root_dir": ROOT_DIR}}
        self.orig_nas_mounted = webui._nas_mounted

    def tearDown(self):
        webui._nas_mounted = self.orig_nas_mounted
        self.tmp.cleanup()

    def test_reachable_even_if_mount_check_says_no(self):
        """mount 输出没匹配上,但目录实实在在存在 → 判为可达。"""
        webui._nas_mounted = lambda cfg: False
        self.assertTrue(webui._dir_reachable(self.cfg, self.d))

    def test_unreachable_when_dir_missing_and_mount_says_no(self):
        webui._nas_mounted = lambda cfg: False
        self.assertFalse(webui._dir_reachable(self.cfg, os.path.join(self.nas, "不存在")))

    def test_mounted_short_circuits_without_stat(self):
        """挂载正常时直接返回 True,不去 stat(避免陈旧挂载上的 SMB 阻塞)。"""
        webui._nas_mounted = lambda cfg: True
        self.assertTrue(webui._dir_reachable(self.cfg, os.path.join(self.nas, "不存在")))


if __name__ == "__main__":
    unittest.main()
