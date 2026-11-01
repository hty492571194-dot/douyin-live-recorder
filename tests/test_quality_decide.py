# -*- coding: utf-8 -*-
"""自动降级状态机(quality.Governor)的行为守卫。

状态机的价值全在「什么时候**不**动」:抖动、误判、赔本降级,任何一条失控都会
让录制比不降还糟。所以这里逐个钉住计划书 §3.3 / §3.7 / §3.8 的口径:

  · 触发要**持续**越过阈值(60 s),一次尖峰不算;
  · 恢复要**双阈值**都低下来 + 持续更久(600 s),且离上次切换 ≥ 900 s;
  · 崩溃信号(C)一次性降到底,不再试探;
  · 功能整体失效(DISABLED_DOMINATED 等)时只做一次廉价比较就返回,不采样不切档;
  · 24 h 内切换超过阈值 → 锁定最低可用档,只有人工能解。

时钟是注入的(`observe(now=...)`),所以这里没有任何 sleep,整套跑完 <1 秒。

**破坏性验证**:把 `observe` 里的持续时间判定去掉(改成越线即切),
`test_spike_does_not_trigger` 与 `test_hold_required` 必须报红。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import quality as Q  # noqa: E402

SRC = (1088, 1920)


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


def feed(gov, seconds, cpu=0.0, engine=0.0, start=0.0, step=5, plan=None):
    """从 start 到 start+seconds 按 step 喂样本,返回最后一次的结果。"""
    plan = plan or Q.degrade_plan(rec(), *SRC)
    res = gov.observe(start, plan, cpu=cpu, engine=engine, demand=45.0,
                      follow_min=1.0, streams=1)
    t = start
    while t < start + seconds:
        t += step
        res = gov.observe(t, plan, cpu=cpu, engine=engine, demand=45.0,
                          follow_min=1.0, streams=1)
    return res


class TestTrigger(unittest.TestCase):
    def setUp(self):
        self.plan = Q.degrade_plan(rec(), *SRC)

    def test_needs_full_window_before_judging(self):
        """窗口没攒够(WINDOW_MIN=10)时不许判定 —— 起播头几秒的瞬时高负载很常见。"""
        g = make_gov()
        for i in range(9):
            res = g.observe(i * 5.0, self.plan, cpu=99.0, engine=99.0,
                            demand=90, follow_min=0.6, streams=1)
        self.assertEqual(res["level"], 0)
        self.assertEqual(res["trigger"], "hold")

    def test_hold_required(self):
        """窗口攒满(45 s)后还要再等满 hold(60 s)才动作,59 s 时不许动。"""
        g = make_gov()
        g.observe(0.0, self.plan, cpu=99, engine=99, demand=90, follow_min=0.6, streams=1)
        feed(g, 45, cpu=99, engine=99, start=0.0, step=5)   # 45 s 时窗口刚满
        over_at = g.over_since
        self.assertTrue(over_at, "越线起点没有记录")
        res = feed(g, 59, cpu=99, engine=99,
                   start=over_at, step=1, plan=self.plan)
        self.assertEqual(res["level"], 0, "59 s 就降档了,说明 hold 没生效")
        res = feed(g, 2, cpu=99, engine=99, start=over_at + 59, step=1,
                   plan=self.plan)
        self.assertEqual(res["level"], 1, "越过 hold 阈值后应降一档")
        self.assertLessEqual(g.last_change, over_at + 62,
                             "动作时刻应恰好落在 over_at + hold 之后")
        self.assertEqual(g.history[-1]["to"], 1)
        self.assertIn("持续", g.history[-1]["reason"])

    def test_spike_does_not_trigger(self):
        """★ 只热 20 s 的尖峰(起播/GC/归档 IO)不许触发降档。"""
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        feed(g, 20, cpu=99, engine=99, start=45.0)
        res = feed(g, 120, cpu=0, engine=0, start=65.0)
        self.assertEqual(res["level"], 0)
        self.assertEqual(g.over_since, 0.0, "冷却后越线计时必须清零")

    def test_engine_alone_triggers(self):
        g = make_gov()
        feed(g, 45, cpu=10, engine=0, start=0.0)
        res = feed(g, 250, cpu=10, engine=95, start=45.0)
        self.assertEqual(res["level"], 1, "引擎饱和(CPU 不高)也要降档")

    def test_cpu_alone_triggers(self):
        g = make_gov()
        feed(g, 45, cpu=0, engine=10, start=0.0)
        res = feed(g, 250, cpu=95, engine=10, start=45.0)
        self.assertEqual(res["level"], 1)

    def test_below_threshold_does_not_trigger(self):
        """90% 低于 92% 阈值:不许降。"""
        g = make_gov()
        feed(g, 45, cpu=90, engine=90, start=0.0)
        res = feed(g, 300, cpu=90, engine=90, start=45.0)
        self.assertEqual(res["level"], 0)


class TestRecover(unittest.TestCase):
    def setUp(self):
        self.plan = Q.degrade_plan(rec(), *SRC)

    def _degrade_once(self):
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        feed(g, 250, cpu=99, engine=99, start=45.0)
        self.assertEqual(g.level, 1)
        return g

    def test_recover_needs_long_hold_and_gap(self):
        g = self._degrade_once()
        changed_at = g.last_change
        # 恢复到 L0 需要:cpu/engine ≤ 72% 持续 600 s,且离上次切换 ≥ 900 s
        res = feed(g, 590, cpu=10, engine=10, start=changed_at, step=10)
        self.assertEqual(res["level"], 1, "恢复持续时间没到就回档了")
        res = feed(g, 400, cpu=10, engine=10, start=changed_at + 590, step=10)
        self.assertEqual(res["level"], 0, "满足持续 + 间隔后应恢复")
        recents = [h for h in g.history if h["to"] < h["from"]]
        self.assertTrue(recents, "历史里没有恢复记录")
        self.assertIn("低于", recents[-1]["reason"])

    def test_no_recover_when_engine_still_high(self):
        """CPU 低但引擎还高 → 不许恢复(双阈值必同时满足)。"""
        g = self._degrade_once()
        res = feed(g, 2000, cpu=10, engine=88, start=g.last_change, step=10)
        self.assertEqual(res["level"], 1)

    def test_recover_quota(self):
        """当日恢复次数上限用完 → 之后只降不升(恢复记录不再增加)。"""
        cfg = rec(degrade_min_gap=60, degrade_recover_hold=60, degrade_max_restore=1)
        plan = Q.degrade_plan(cfg, *SRC)
        g = make_gov(cfg)
        t = 0.0
        feed(g, 45, cpu=0, engine=0, start=t, plan=plan)
        for _ in range(3):                       # 三轮「热 → 冷」
            feed(g, 500, cpu=99, engine=99, start=t, step=20, plan=plan)
            t += 500
            feed(g, 1200, cpu=10, engine=10, start=t, step=20, plan=plan)
            t += 1200
        recovers = [h for h in g.history if h["to"] < h["from"]]
        self.assertEqual(len(recovers), 1,
                         "配额是 1 次,却恢复了 %d 次: %r" % (len(recovers), recovers))
        self.assertGreaterEqual(len([h for h in g.history if h["to"] > h["from"]]), 1)


class TestCrashAndLimits(unittest.TestCase):
    def setUp(self):
        self.plan = Q.degrade_plan(rec(), *SRC)

    def test_crash_jumps_to_lowest(self):
        """崩溃信号(C)一次性降到底,不再一级一级试探。"""
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        g.note_crash("start_fail:A", now=50.0)
        g.note_crash("start_fail:B", now=55.0)
        res = g.observe(60.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=0)
        self.assertTrue(res["changed"])
        self.assertEqual(res["level"], 2)
        self.assertEqual(res["trigger"], "crash")

    def test_single_start_failure_is_not_a_crash(self):
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        g.note_crash("start_fail:A", now=50.0)
        res = g.observe(60.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=0)
        self.assertEqual(res["level"], 0, "一次启动失败说明不了并发过载")

    def test_old_crash_events_expire(self):
        """5 分钟窗口外的崩溃事件不算数(否则一次早高峰会永远影响判定)。"""
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        g.note_crash("start_fail:A", now=50.0)
        g.note_crash("start_fail:B", now=51.0)
        res = g.observe(700.0, self.plan, cpu=10, engine=10, demand=20,
                        follow_min=1.0, streams=0)
        self.assertEqual(res["level"], 0)

    def test_flapping_lock_needs_manual_unlock(self):
        """24 h 内切换超过阈值 → 锁在最低可用档,unlock() 才能回 L0。"""
        cfg = rec(degrade_min_gap=60, degrade_hold=10, degrade_max_switches=3,
                  degrade_recover_hold=60, degrade_max_restore=99)
        plan = Q.degrade_plan(cfg, *SRC)
        g = make_gov(cfg)
        t = 0.0
        feed(g, 45, cpu=0, engine=0, start=t, plan=plan)
        for _ in range(8):
            feed(g, 500, cpu=99, engine=99, start=t, step=20, plan=plan)
            t += 500
            feed(g, 2200, cpu=10, engine=10, start=t, step=40, plan=plan)
            t += 2200
        self.assertTrue(g.unstable, "反复抖动之后应进入锁定态")
        snap = g.snapshot(t)
        self.assertTrue(snap["unstable"])
        self.assertEqual(snap["level"], 2, "锁定时应停在最低可用档")
        # 锁定期间继续给出过载信号也不许再动
        res = g.observe(t + 10, plan, cpu=99, engine=99, demand=90,
                        follow_min=0.5, streams=8)
        self.assertEqual(res["trigger"], "locked")
        after = g.unlock(t + 20)
        self.assertFalse(after["unstable"])
        self.assertEqual(after["level"], 0)

    def test_min_gap_blocks_fast_second_switch(self):
        g = make_gov()
        feed(g, 45, cpu=0, engine=0, start=0.0)
        feed(g, 250, cpu=99, engine=99, start=45.0)
        self.assertEqual(g.level, 1)
        changed = g.last_change
        feed(g, 400, cpu=99, engine=99, start=changed, step=10)
        self.assertEqual(g.level, 1, "最小间隔内不许连降两级")
        downs = [h for h in g.history if h["to"] > h["from"]]
        self.assertEqual(len(downs), 1, "900 s 内又降了一次: %r" % (downs,))


class TestDisabledStates(unittest.TestCase):
    def test_dominated_never_switches(self):
        """★ 用户指定边界:720P/500k 时降级整体失效,过载信号再强也不许切档。"""
        cfg = rec(resolution=720, bitrate=500)
        plan = Q.degrade_plan(cfg, *SRC)
        g = make_gov(cfg)
        self.assertEqual(plan["state"], "DISABLED_DOMINATED")
        self.assertEqual(g.level, 0)
        res = feed(g, 600, cpu=99, engine=99, start=0.0, plan=plan)
        self.assertEqual(res["level"], 0)
        self.assertEqual(res["state"], "DISABLED_DOMINATED")
        self.assertEqual(res["trigger"], "disabled")

    def test_dominated_forces_level_back_to_zero(self):
        """运行中把主配置改成 720P/500k → 已生效的档位必须立刻归 0。"""
        cfg = rec()
        g = make_gov(cfg)
        feed(g, 45, cpu=0, engine=0, start=0.0)
        feed(g, 250, cpu=99, engine=99, start=45.0)
        self.assertEqual(g.level, 1)
        new_cfg = rec(resolution=720, bitrate=500)
        plan = Q.degrade_plan(new_cfg, *SRC)
        res = g.observe(200.0, plan, cpu=99, engine=99, demand=90,
                        follow_min=0.9, streams=2)
        self.assertEqual(res["level"], 0)
        self.assertIn("DISABLED", res["state"])

    def test_copy_mode_no_switch(self):
        cfg = rec(video_mode="copy")
        plan = Q.degrade_plan(cfg, *SRC)
        g = make_gov(cfg)
        res = feed(g, 600, cpu=99, engine=99, start=0.0, plan=plan)
        self.assertEqual(res["level"], 0)

    def test_switch_off_no_switch(self):
        cfg = rec(auto_degrade=False)
        plan = Q.degrade_plan(cfg, *SRC)
        g = make_gov(cfg)
        res = feed(g, 600, cpu=99, engine=99, start=0.0, plan=plan)
        self.assertEqual(res["level"], 0)
        self.assertEqual(res["state"], "DISABLED_OFF")


class TestSnapshot(unittest.TestCase):
    def test_snapshot_shape_matches_api_contract(self):
        g = make_gov()
        plan = Q.degrade_plan(rec(), *SRC)
        g.observe(0.0, plan, cpu=41.2, engine=38.5, demand=45.3,
                  follow_min=0.99, streams=7)
        g.set_active({"resolution": 720, "bitrate": 700, "w": 720, "h": 1280})
        snap = g.snapshot(0.0)
        for key in ("level", "state", "reason", "active_tier", "user_tier",
                    "l1", "l2", "cpu", "engine", "demand_pct", "streams",
                    "max_hw_streams", "hold_s", "recover_in_s", "unstable",
                    "switches_today", "thresholds", "min_gain", "engine_mp_s"):
            self.assertIn(key, snap, "接口字段缺失: %s" % key)
        self.assertEqual(snap["active_tier"]["w"], 720)
        self.assertEqual(snap["max_hw_streams"], 8)

    def test_snapshot_survives_before_first_sample(self):
        """刚启动时前端就会来拉状态,不能因为没采样过就 500。"""
        g = make_gov()
        snap = g.snapshot(0.0)
        self.assertEqual(snap["level"], 0)
        self.assertIn("state", snap)


if __name__ == "__main__":
    unittest.main()
