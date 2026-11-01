# -*- coding: utf-8 -*-
"""录制暂存目录外迁 + 在录切换的守卫(2026-10-30)。

背景:项目文件夹曾因 output_dir 空串回落 `BASE/recordings` 涨到 99G(视频占 98%,
其中 84G 是一次性手工备份、13G 是滞留的现役暂存)。本次改造立了四条不变式:

  ① 暂存根绝不在项目目录内(配置写进去也要被拒绝并回落默认值);
  ② **一场一个目录**:本场内崩溃/换房/降档重启都不能换目录(否则一场被拆两盘);
  ③ 在录不归档(沿用既有三道闸);
  ④ 改暂存目录要么下一场生效(next_session),要么显式重启切过去(now)。

这四条都靠「钉死 + 解析层硬拦」实现,不是靠 UI 提示,所以必须有测试盯着。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor    # noqa: E402
import nas        # noqa: E402
import webui      # noqa: E402

with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as _f:
    HTML = _f.read()


class FakeProc:
    """假 ffmpeg:只提供录制链路真正会用到的那几个方法。"""

    next_pid = 4000

    def __init__(self, *a, **kw):
        FakeProc.next_pid += 1
        self.pid = FakeProc.next_pid
        self.args = a[0] if a else []
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


class FakeState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.recordings = {}
        self.history = []
        self.refreshed = []

    def get_config(self):
        return self.cfg

    def set_recording(self, name, info):
        self.recordings[name] = info

    def remove_recording(self, name):
        self.recordings.pop(name, None)

    def set_history(self, entries):
        self.history = entries

    def get_history(self):
        return self.history

    def request_refresh(self, name):
        self.refreshed.append(name)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.stage = os.path.join(self.d, "stage")
        self.legacy = os.path.join(self.d, "proj", "recordings")
        os.makedirs(self.stage, exist_ok=True)
        os.makedirs(self.legacy, exist_ok=True)

        self.cfg = {"recorder": {"enabled": True, "output_dir": self.stage,
                                 "output_dir_switch": "next_session"},
                    "nas": {"enabled": False}}
        self.state = FakeState(self.cfg)

        self._orig = {
            "history": monitor._history, "recordings": monitor._recordings,
            "pin": dict(monitor._session_dir_pin),
            "switch": monitor._stage_switch,
            "health": monitor._rec_health, "carry": monitor._rec_carry,
            "session": monitor._session_start,
            "history_path": monitor.HISTORY_PATH,
            "base": monitor.BASE, "record_dir": monitor.RECORD_DIR,
            "popen": monitor.subprocess.Popen, "find_ffmpeg": monitor.find_ffmpeg,
            "enqueue": nas.enqueue,
        }
        monitor._history = []
        monitor._recordings = {}
        monitor._session_dir_pin.clear()
        monitor._stage_switch = None
        monitor._rec_health = {}
        monitor._rec_carry = {}
        monitor._session_start = {}
        monitor.HISTORY_PATH = os.path.join(self.d, "history.json")
        monitor.find_ffmpeg = lambda *a, **kw: "/fake/ffmpeg"
        monitor.subprocess.Popen = FakeProc
        self.enqueued = []
        nas.enqueue = lambda entry: self.enqueued.append(dict(entry))

    def tearDown(self):
        monitor._history = self._orig["history"]
        monitor._recordings = self._orig["recordings"]
        monitor._session_dir_pin.clear()
        monitor._session_dir_pin.update(self._orig["pin"])
        monitor._stage_switch = self._orig["switch"]
        monitor._rec_health = self._orig["health"]
        monitor._rec_carry = self._orig["carry"]
        monitor._session_start = self._orig["session"]
        monitor.HISTORY_PATH = self._orig["history_path"]
        monitor.BASE = self._orig["base"]
        monitor.RECORD_DIR = self._orig["record_dir"]
        monitor.subprocess.Popen = self._orig["popen"]
        monitor.find_ffmpeg = self._orig["find_ffmpeg"]
        nas.enqueue = self._orig["enqueue"]
        self.tmp.cleanup()

    def _start(self, name="主播甲"):
        return monitor._start_recording(self.state, name, "http://example/stream",
                                        self.cfg, room_id="r1")


class TestDefaultStageDir(_Base):
    """① 默认暂存根必须在项目外。"""

    def test_default_is_absolute_and_outside_project(self):
        d = monitor.DEFAULT_STAGE_DIR
        self.assertTrue(os.path.isabs(d), d)
        self.assertFalse(monitor._under_project(d),
                         f"默认暂存根不能落在项目内:{d}")

    def test_empty_output_dir_never_returns_project_recordings(self):
        cfg = {"recorder": {"output_dir": ""}}
        out = monitor._resolve_output_dir(cfg)
        self.assertEqual(os.path.realpath(out), os.path.realpath(monitor.DEFAULT_STAGE_DIR))
        self.assertFalse(monitor._under_project(out))

    def test_missing_recorder_block_falls_back_to_default(self):
        self.assertEqual(os.path.realpath(monitor._resolve_output_dir({})),
                         os.path.realpath(monitor.DEFAULT_STAGE_DIR))

    def test_config_inside_project_is_rejected(self):
        """配置指进项目 → 拒绝并回落默认值(不是只打一行日志)。"""
        cfg = {"recorder": {"output_dir": os.path.join(BASE, "recordings")}}
        out = monitor._resolve_output_dir(cfg)
        self.assertFalse(monitor._under_project(out))
        self.assertEqual(os.path.realpath(out), os.path.realpath(monitor.DEFAULT_STAGE_DIR))

    def test_config_in_subdir_of_project_is_rejected(self):
        cfg = {"recorder": {"output_dir": os.path.join(BASE, "static", "videos")}}
        self.assertFalse(monitor._under_project(monitor._resolve_output_dir(cfg)))

    def test_tilde_path_is_expanded(self):
        cfg = {"recorder": {"output_dir": "~/DouyinTestStage"}}
        self.assertTrue(monitor._resolve_output_dir(cfg).startswith(os.path.expanduser("~")))

    def test_project_base_itself_is_rejected(self):
        cfg = {"recorder": {"output_dir": BASE}}
        self.assertFalse(monitor._under_project(monitor._resolve_output_dir(cfg)))


class TestSessionPin(_Base):
    """② 一场一个目录:本场内任何重启都不换目录。"""

    def test_start_recording_writes_into_configured_stage(self):
        self._start()
        rec = monitor._recordings["主播甲"]
        real_stage = os.path.realpath(self.stage)
        self.assertTrue(rec["out_dir"].startswith(os.path.join(real_stage, "主播甲")),
                        rec["out_dir"])
        self.assertIn("out_dir", rec, "录制态必须显式记下本场目录")
        self.assertTrue(os.path.isdir(rec["out_dir"]))

    def test_pin_survives_config_change_then_restart(self):
        """改配置 + 本场内重启 → 仍写旧目录(否则一场被拆两盘)。"""
        self._start()
        first = monitor._recordings["主播甲"]["out_dir"]
        new_stage = os.path.join(self.d, "stage2")
        self.cfg["recorder"]["output_dir"] = new_stage
        monitor._restart_recording(self.state, "主播甲", "http://example/stream",
                                   self.cfg, "r1", reason="进程退出")
        self.assertEqual(monitor._recordings["主播甲"]["out_dir"], first)
        self.assertFalse(monitor._recordings["主播甲"]["out_dir"].startswith(new_stage))

    def test_pin_released_after_stop(self):
        self._start()
        monitor._stop_recording(self.state, "主播甲")
        self.assertNotIn("主播甲", monitor._session_dir_pin)

    def test_next_session_uses_new_stage_after_stop(self):
        self._start()
        new_stage = os.path.join(self.d, "stage2")
        self.cfg["recorder"]["output_dir"] = new_stage
        monitor._stop_recording(self.state, "主播甲")
        self._start()
        self.assertTrue(monitor._recordings["主播甲"]["out_dir"]
                        .startswith(os.path.realpath(new_stage)))

    def test_same_name_same_day_keeps_one_dir(self):
        self._start()
        a = monitor._recordings["主播甲"]["out_dir"]
        monitor._stop_recording(self.state, "主播甲")
        self._start()
        self.assertEqual(monitor._recordings["主播甲"]["out_dir"], a)


class TestSetStageDir(_Base):
    """④ 在录时改目录:next_session 排队 / now 立刻切。"""

    def test_rejects_project_path(self):
        r = monitor.set_stage_dir(self.state, os.path.join(BASE, "x"))
        self.assertFalse(r["ok"])
        self.assertIn("项目", r["error"])

    def test_creates_missing_dir(self):
        target = os.path.join(self.d, "brand-new", "deep")
        r = monitor.set_stage_dir(self.state, target)
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isdir(target))

    def test_no_active_recording_applies_immediately(self):
        target = os.path.join(self.d, "stage3")
        r = monitor.set_stage_dir(self.state, target)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "immediate")
        self.assertEqual(r["pending"], [])
        self.assertIsNone(monitor._stage_switch)

    def test_next_session_records_pending_and_leaves_recording_alone(self):
        self._start()
        before = monitor._recordings["主播甲"]["output_path"]
        target = os.path.join(self.d, "stage3")
        r = monitor.set_stage_dir(self.state, target, now=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "next_session")
        self.assertEqual(r["pending"], ["主播甲"])
        self.assertEqual(monitor._recordings["主播甲"]["output_path"], before,
                         "next_session 不许动在录场次")
        self.assertEqual(monitor._stage_switch["dir"], os.path.realpath(target))

    def test_now_switches_and_archives_old_half(self):
        self._start()
        old_dir = monitor._recordings["主播甲"]["out_dir"]
        with open(os.path.join(old_dir, "主播甲-20260101-000000-000.flv"), "wb") as f:
            f.write(b"x" * 32)
        target = os.path.join(self.d, "stage3")
        r = monitor.set_stage_dir(self.state, target, now=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["switched"]), 1, r)
        new_dir = monitor._recordings["主播甲"]["out_dir"]
        self.assertTrue(new_dir.startswith(os.path.realpath(target)), new_dir)
        self.assertNotEqual(new_dir, old_dir)
        # 旧半场:已入队(nas 未启用时不入队,这里只校验切换结果与目录解钉)
        self.assertEqual(monitor._session_dir_pin["主播甲"], new_dir)

    def test_now_enqueues_old_dir_when_nas_enabled(self):
        self.cfg["nas"] = {"enabled": True}
        self._start()
        old_dir = monitor._recordings["主播甲"]["out_dir"]
        with open(os.path.join(old_dir, "a-000.flv"), "wb") as f:
            f.write(b"x")
        monitor.set_stage_dir(self.state, os.path.join(self.d, "stage3"), now=True)
        self.assertEqual([e["src_path"] for e in self.enqueued], [old_dir])

    def test_now_does_not_consume_restart_budget(self):
        """主动切换不是故障,不该吃掉「反复失败就放弃本场」的预算。"""
        self._start()
        monitor.set_stage_dir(self.state, os.path.join(self.d, "stage3"), now=True)
        self.assertEqual(int((monitor._rec_health.get("主播甲") or {}).get("restarts") or 0), 0)

    def test_unwritable_target_is_rejected(self):
        ro = os.path.join(self.d, "readonly")
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        try:
            r = monitor.set_stage_dir(self.state, os.path.join(ro, "sub"))
        finally:
            os.chmod(ro, 0o700)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_empty_string_means_default(self):
        r = monitor.set_stage_dir(self.state, "")
        self.assertTrue(r["ok"], r)
        self.assertEqual(os.path.realpath(r["effective"]),
                         os.path.realpath(monitor.DEFAULT_STAGE_DIR))


class TestStorageSnapshot(_Base):
    def test_snapshot_exposes_effective_and_default(self):
        snap = monitor.stage_switch_snapshot(self.cfg)
        self.assertEqual(os.path.realpath(snap["effective_dir"]),
                         os.path.realpath(self.stage))
        self.assertEqual(snap["default_dir"], monitor.DEFAULT_STAGE_DIR)
        self.assertFalse(snap["in_project"])
        self.assertEqual(snap["meta_dir"], monitor.RECORD_DIR)
        self.assertIn("free", snap["disk"])

    def test_snapshot_lists_active_dirs(self):
        self._start()
        snap = monitor.stage_switch_snapshot(self.cfg)
        self.assertEqual(snap["active_dirs"], [monitor._recordings["主播甲"]["out_dir"]])


class TestWebuiValidation(_Base):
    def _store(self):
        return webui.State(os.path.join(self.d, "config.json"), self.cfg, None)

    def test_rejects_relative_path(self):
        err = self._store()._validate_stage_dir("some/relative/dir")
        self.assertIn("绝对路径", err)

    def test_rejects_path_inside_project(self):
        err = self._store()._validate_stage_dir(os.path.join(BASE, "recordings"))
        self.assertIn("项目", err)

    def test_rejects_path_whose_parent_is_missing_beyond_root(self):
        # 父链一路向上仍找不到存在的目录(构造一个不可能存在的挂载点)
        err = self._store()._validate_stage_dir("/nonexistent-abc/x/y/z")
        self.assertTrue(err)

    def test_accepts_absolute_existing_parent(self):
        self.assertIsNone(self._store()._validate_stage_dir(self.stage))

    def test_accepts_new_dir_under_existing_parent(self):
        self.assertIsNone(self._store()._validate_stage_dir(
            os.path.join(self.d, "new-stage")))

    def test_validate_config_patch_blocks_project_path(self):
        err = self._store()._validate(
            {"recorder": {"output_dir": os.path.join(BASE, "recordings")}})
        self.assertIn("项目", err)

    def test_validate_config_patch_blocks_bad_switch_policy(self):
        err = self._store()._validate(
            {"recorder": {"output_dir": self.stage, "output_dir_switch": "maybe"}})
        self.assertIn("output_dir_switch", err)


class TestAllowedRoots(_Base):
    """暂存外迁后白名单必须同时覆盖新旧两个根,否则历史页对老场次 403。"""

    def test_contains_stage_and_legacy_root(self):
        roots = webui._allowed_roots(self.cfg)
        self.assertIn(os.path.realpath(self.stage), roots)
        self.assertIn(os.path.realpath(os.path.join(BASE, "recordings")), roots)

    def test_deduped(self):
        cfg = {"recorder": {"output_dir": os.path.join(BASE, "recordings")},
               "nas": {"enabled": False}}
        roots = webui._allowed_roots(cfg)
        self.assertEqual(len(roots), len(set(roots)))


class TestNasBackfillPath(_Base):
    """history.json 恒定在项目元数据目录,绝不跟着暂存根走。"""

    def test_backfill_reads_project_history_regardless_of_output_dir(self):
        fake_base = os.path.join(self.d, "proj")
        rec_root = os.path.join(fake_base, "recordings")
        session = os.path.join(rec_root, "主播甲", "2026-01-01")
        os.makedirs(session, exist_ok=True)
        with open(os.path.join(session, "主播甲-20260101-120000-000.flv"), "wb") as f:
            f.write(b"x" * 16)
        hist = [{"name": "主播甲", "status": "done",
                 "output_path": os.path.join(session, "主播甲-20260101-120000-%03d.flv"),
                 "archive": {"state": "none"}}]
        with open(os.path.join(rec_root, "history.json"), "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
        # 暂存根指向别处:老实现会去 <暂存根>/history.json 找,找不到 → 入队 0
        cfg = {"recorder": {"output_dir": self.stage}}
        with mock.patch.object(nas, "BASE", fake_base), \
                mock.patch.object(nas, "PENDING_PATH", os.path.join(self.d, "pending.json")), \
                mock.patch.object(nas, "_pending", []):
            n = nas.backfill_from_history(cfg)
        self.assertEqual(n, 1)


class TestFrontendGuards(unittest.TestCase):
    """设置页必须真的有这两项(防止只改后端、界面无入口 —— 上一版就是这样)。"""

    def test_stage_dir_field_present(self):
        self.assertIn('"recorder.output_dir"', HTML)
        self.assertIn("默认录制暂存目录", HTML)

    def test_switch_policy_field_present(self):
        self.assertIn('"recorder.output_dir_switch"', HTML)
        self.assertIn("STAGE_SWITCH_OPTIONS", HTML)
        self.assertIn("next_session", HTML)
        self.assertIn('"now"', HTML)

    def test_path_control_has_browse_button(self):
        self.assertIn('type === "path"', HTML)
        self.assertIn("pickPathFor", HTML)

    def test_card_uses_backend_storage_not_raw_config(self):
        # 卡面落盘地址必须读 /api/status.storage,不许再拼 config.recorder.output_dir
        self.assertIn("storageInfo", HTML)
        self.assertIn("effective_dir", HTML)

    def test_pending_switch_is_surfaced_to_user(self):
        # 在录时按 next_session 排队,必须明确告诉用户「本场结束后生效」
        self.assertIn("stage_switch", HTML)
        self.assertIn("本场结束后生效", HTML)


if __name__ == "__main__":
    unittest.main()
