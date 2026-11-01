# -*- coding: utf-8 -*-
"""崩溃原因分类的守卫:流侧故障不许再降画质、也不许再挡住恢复。

## 为什么需要这一层

崩溃信号(C 类)本来是给「并发过多 / 引擎不够」做旁证的:几路 ffmpeg 同时
起不来,比 CPU 采样更能说明机器撑不住。但这个假设有个前提 —— **退出是本地
原因造成的**。抖音 CDN 的 flv 地址带签名、有时效,主播侧切码率或重连之后旧
地址直接 404,ffmpeg 一起来就退,和机器负载毫无关系。

2026-10-27 的事故复盘(本文件的由来):
  · 当天 19 次 ffmpeg 退出**全部**是 `404 Not Found`,没有一条编码器/内存报错;
  · 全程 CPU < 25%、引擎 < 25%(阈值 92%);
  · 却被判成「并发过多」一次降到底,随后 22:23 / 22:36 / 22:50 / 23:02 /
    23:19 / 23:53 / 00:07 / 00:22 不断有 404 重启刷新 `last_crash`,
    「崩溃后 30 min 内不恢复」的闸门永远等不到,卡在 720P 两小时二十分。

所以判据拆成两条互不相干的路:
  · 只有 `resource` 类参与降档判定与恢复闸门;
  · `external` / `unknown` 只记账(`crashes_recent.ignored`),不判定。

## 破坏性验证

把 `note_crash` 里的 `if c == CRASH_RESOURCE: self.last_crash = now` 改回
无条件赋值,`test_external_crash_does_not_block_recovery` 必须报红;
把 `_crash_counts` 里的 `if cause != CRASH_RESOURCE: continue` 去掉,
`test_external_crash_does_not_degrade` 必须报红。
"""
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import quality as Q  # noqa: E402

SRC = (1088, 1920)

NOT_FOUND = ("[in#0 @ 0x6000028d0300] Error opening input: "
             "Server returned 404 Not Found | Error opening input file "
             "http://pull-flv-l11.douyincdn.com/stage/stream-1_or4.flv")
ENCODER_OOM = ("[h264_videotoolbox @ 0x7f] Error while opening encoder - "
               "maybe incorrect parameters such as bit_rate, rate, width or height")


def rec(**kw):
    base = {"resolution": 0, "bitrate": 1200, "video_mode": "vt",
            "auto_degrade": True, "min_gain": 10,
            "degrade_l1_resolution": 720, "degrade_l1_bitrate": 700,
            "degrade_l2_resolution": 720, "degrade_l2_bitrate": 500,
            "degrade_cpu": 92, "degrade_engine": 92, "degrade_recover": 72,
            "degrade_hold": 60, "degrade_recover_hold": 600,
            "degrade_min_gap": 900, "degrade_max_restore": 4,
            "degrade_max_switches": 6, "max_hw_streams": 8}
    base.update(kw)
    return {"recorder": base}


def make_gov(cfg=None, now=0.0):
    cfg = cfg or rec()
    return Q.Governor(cfg["recorder"], now=now)


def plan_of(cfg=None):
    return Q.degrade_plan(cfg or rec(), *SRC)


def feed(gov, upto, start=0.0, step=5, cpu=10.0, engine=10.0, plan=None):
    """从 start 喂到 upto,返回最后一次结果。"""
    plan = plan or plan_of()
    res = None
    t = start
    while t < upto:
        t += step
        res = gov.observe(t, plan, cpu=cpu, engine=engine, demand=20.0,
                          follow_min=1.0, streams=3)
    return res


class TestClassifyExit(unittest.TestCase):
    """stderr 尾部 → resource / external / unknown。"""

    def test_404_is_external(self):
        """流地址失效是最常见的退出原因,必须归到流侧。"""
        self.assertEqual(Q.classify_exit(NOT_FOUND), Q.CRASH_EXTERNAL)

    def test_403_is_external(self):
        self.assertEqual(
            Q.classify_exit("Error opening input: Server returned 403 Forbidden"),
            Q.CRASH_EXTERNAL)

    def test_encoder_failure_is_resource(self):
        """编码器开不起来才是真正的「本地扛不住」。"""
        self.assertEqual(Q.classify_exit(ENCODER_OOM), Q.CRASH_RESOURCE)

    def test_empty_stderr_is_unknown_not_resource(self):
        """空 stderr 没有任何证据指向本地负载 —— 归 resource 会重演误降档。"""
        self.assertEqual(Q.classify_exit(""), Q.CRASH_UNKNOWN)
        self.assertEqual(Q.classify_exit("   \n\n"), Q.CRASH_UNKNOWN)

    def test_stream_mark_wins_over_encoder_mark(self):
        """404 后面常跟解码器初始化日志,流侧必须优先归因。"""
        mixed = NOT_FOUND + "\n[h264_videotoolbox] setup ok"
        self.assertEqual(Q.classify_exit(mixed), Q.CRASH_EXTERNAL)

    def test_unrecognized_text_is_unknown(self):
        self.assertEqual(Q.classify_exit("something odd happened"),
                         Q.CRASH_UNKNOWN)


class TestExternalCrashIsIgnored(unittest.TestCase):
    """流侧故障不参与降档判定,也不刷新恢复闸门。"""

    def setUp(self):
        self.plan = plan_of()

    def test_external_crash_does_not_degrade(self):
        """本次事故的直接复现:两路 404 同时重启,画质必须原地不动。"""
        g = make_gov()
        feed(g, 45)
        g.note_crash("restart:A", now=50.0, cause=Q.CRASH_EXTERNAL)
        g.note_crash("restart:B", now=51.0, cause=Q.CRASH_EXTERNAL)
        res = g.observe(60.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=2)
        self.assertEqual(res["level"], 0, "流侧 404 不该被当成并发过多")
        self.assertNotEqual(res["trigger"], "crash")

    def test_ignored_events_are_counted_for_diagnosis(self):
        """不判定不等于不记录 —— 面板要能回答「刚才那两路为什么不降」。"""
        g = make_gov()
        feed(g, 45)
        g.note_crash("restart:A", now=50.0, cause=Q.CRASH_EXTERNAL)
        g.note_crash("restart:B", now=51.0, cause=Q.CRASH_EXTERNAL)
        snap = g.snapshot(60.0)
        self.assertEqual(snap["crashes_recent"]["ignored"], 2)
        self.assertEqual(snap["crashes_recent"]["streams"], 0)
        self.assertEqual(snap["last_external_at"], 51.0)

    def test_resource_crash_still_jumps_to_lowest(self):
        """真·编码器崩溃必须还原行为:一次降到底,不许退化成逐级试探。"""
        g = make_gov()
        feed(g, 45)
        g.note_crash("restart:A", now=50.0, cause=Q.CRASH_RESOURCE)
        g.note_crash("restart:B", now=51.0, cause=Q.CRASH_RESOURCE)
        res = g.observe(60.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=2)
        self.assertTrue(res["changed"])
        self.assertEqual(res["level"], 2)
        self.assertEqual(res["trigger"], "crash")

    def test_reason_mentions_ignored_streams(self):
        """降档原因里要写明「另有 N 路流侧故障已忽略」,否则读日志会以为全是并发。"""
        g = make_gov()
        feed(g, 45)
        g.note_crash("restart:A", now=50.0, cause=Q.CRASH_RESOURCE)
        g.note_crash("restart:B", now=50.5, cause=Q.CRASH_RESOURCE)
        g.note_crash("restart:C", now=51.0, cause=Q.CRASH_EXTERNAL)
        res = g.observe(60.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=3)
        self.assertEqual(res["level"], 2)
        self.assertIn("流侧故障", res["last_reason"])
        self.assertIn("1 路", res["last_reason"])


class TestRecoveryNotBlockedByStreamFailures(unittest.TestCase):
    """本次事故的根因回归:404 高频发生时,恢复不能被无限推后。"""

    def test_external_crash_does_not_block_recovery(self):
        g = make_gov(now=0.0)
        g.level = 1
        g.last_change = 0.0
        plan = plan_of()
        # 每 600 s 来一次两路 404(实测节奏:22:23 / 22:36 / 22:50 / 23:02 …)
        got = None
        t = 0.0
        while t < 4000:
            t += 5
            if t % 600 == 0:
                g.note_crash("restart:A", now=t, cause=Q.CRASH_EXTERNAL)
                g.note_crash("restart:B", now=t, cause=Q.CRASH_EXTERNAL)
            res = g.observe(t, plan, cpu=10, engine=10, demand=20,
                            follow_min=1.0, streams=3)
            if res["level"] == 0:
                got = t
                break
        self.assertIsNotNone(got, "流侧 404 持续发生时,恢复仍应在 600 s 量级完成")
        self.assertLess(got, 1500, "恢复用时 %s s,闸门仍被流侧故障推后了" % got)

    def test_resource_crash_keeps_blocking_recovery(self):
        """反向保护:真的编码器崩溃反复发生时,恢复必须继续被挡住。"""
        g = make_gov(now=0.0)
        g.level = 1
        g.last_change = 0.0
        plan = plan_of()
        t = 0.0
        while t < 4000:
            t += 5
            if t % 600 == 0:
                g.note_crash("restart:A", now=t, cause=Q.CRASH_RESOURCE)
                g.note_crash("restart:B", now=t, cause=Q.CRASH_RESOURCE)
            g.observe(t, plan, cpu=10, engine=10, demand=20,
                      follow_min=1.0, streams=3)
        self.assertGreater(g.level, 0,
                           "本地资源崩溃每 10 min 一次时不该恢复(否则降了又升);"
                           "实际到 L2 是正确行为 —— 资源类崩溃本来就是一次降到底")


class TestStateCompatibility(unittest.TestCase):
    """状态文件跨版本可读:v2 的二元组没有 cause,按 resource 解读。"""

    def test_v2_crash_rows_read_as_resource(self):
        g = make_gov()
        ok = g.restore({"level": 1, "last_crash_at": 1.0,
                        "crashes": [[1.0, "restart:X"]]}, now=100.0)
        self.assertTrue(ok)
        self.assertEqual(g.crashes, [(1.0, "restart:X", Q.CRASH_RESOURCE)])

    def test_v3_roundtrip_keeps_cause(self):
        g = make_gov()
        g.note_crash("restart:A", now=10.0, cause=Q.CRASH_EXTERNAL)
        g.note_crash("restart:B", now=11.0, cause=Q.CRASH_RESOURCE)
        d = g.dump()
        self.assertEqual(d["version"], 3)
        g2 = make_gov()
        self.assertTrue(g2.restore(d, now=100.0))
        self.assertEqual([c[2] for c in g2.crashes],
                         [Q.CRASH_EXTERNAL, Q.CRASH_RESOURCE])
        self.assertEqual(g2.last_crash, 11.0, "只有资源类刷 last_crash")
        self.assertEqual(g2.last_external, 10.0)

    def test_default_cause_is_resource(self):
        """老调用点不传 cause 时保守按资源类记 —— 宁可多降一档,不漏真崩溃。"""
        g = make_gov()
        g.note_crash("start_fail:A", now=5.0)
        self.assertEqual(g.crashes[0][2], Q.CRASH_RESOURCE)
        self.assertEqual(g.last_crash, 5.0)

    def test_unknown_cause_falls_back_to_resource(self):
        g = make_gov()
        g.note_crash("restart:A", now=5.0, cause="something-else")
        self.assertEqual(g.crashes[0][2], Q.CRASH_RESOURCE)


class TestExitCauseFromLog(unittest.TestCase):
    """monitor._exit_cause:从本次 err.log 尾部归因(重启前必须读)。"""

    def setUp(self):
        import monitor  # 延迟导入:让纯 quality 的用例不受 monitor 初始化影响
        self.monitor = monitor

    def _cause_for(self, text):
        fd, path = tempfile.mkstemp(suffix=".err.log")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return self.monitor._exit_cause({"err_path": path})
        finally:
            os.unlink(path)

    def test_404_log_yields_external(self):
        self.assertEqual(self._cause_for(NOT_FOUND), Q.CRASH_EXTERNAL)

    def test_missing_file_yields_unknown(self):
        self.assertEqual(self.monitor._exit_cause({"err_path": "/nonexistent/x.log"}),
                         Q.CRASH_UNKNOWN)

    def test_no_rec_yields_unknown(self):
        self.assertEqual(self.monitor._exit_cause(None), Q.CRASH_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
