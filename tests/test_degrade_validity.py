# -*- coding: utf-8 -*-
"""自动降级的有效性判定:**表驱动**覆盖计划书 §3.5.4 的全部 8 个场景。

这是整条改造里最容易出错的地方,所以把「什么情况下该降、什么情况下必须不降」
写成了逐行对照的用例表(与计划书表格一一对应,改一处必须两边一起改)。

两条最容易踩的坑,这里是显式用例:
  · **场景 1/2(用户指定边界)**:分辨率已是 720P 且码率不高于 L2 →
    两个维度都没有正收益 → 降级必须**整体自动失效**(DISABLED_DOMINATED),
    否则会白白付一次重启丢帧的代价而什么也换不到;
  · **场景 7**:降码率会让体积涨 40%,但引擎收益 55.9% ≥ 25% → 第 3 层守卫
    放行,且此时应优先选 L2(同样省引擎、体积还不涨)。

**破坏性验证**:把 `quality.usable()` 改成恒返回 True,场景 1/2 必须报红
(已人工验证过一次);把 `pick_target()` 的「优先 L2」分支删掉,场景 7 报红。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import quality as Q  # noqa: E402

SRC = (1088, 1920)          # 实测样本场次的源分辨率(1088×1920 竖屏)


def rec(**kw):
    """默认配置(L1=720P/700k、L2=720P/500k)+ 覆盖项。"""
    base = {"resolution": 0, "bitrate": 1200, "video_mode": "vt",
            "auto_degrade": True, "min_gain": 10,
            "degrade_l1_resolution": 720, "degrade_l1_bitrate": 700,
            "degrade_l2_resolution": 720, "degrade_l2_bitrate": 500}
    base.update(kw)
    return {"recorder": base}


def plan(cfg, disk_ok=True, src=SRC):
    return Q.degrade_plan(cfg, src[0], src[1], disk_ok)


class TestScenarioTable(unittest.TestCase):
    """§3.5.4 的 8 个场景,一行一个用例。"""

    def test_scenario1_720p_500k_equal_to_l2(self):
        """① 720P / 500k = 正好等于 L2:降级整体失效(用户指定边界)。"""
        p = plan(rec(resolution=720, bitrate=500))
        self.assertEqual(p["state"], "DISABLED_DOMINATED")
        self.assertFalse(p["l1"]["valid"])
        self.assertFalse(p["l2"]["valid"])
        self.assertEqual(p["max_level"], 0)
        self.assertIn("降级无收益", p["reason"])
        # 引擎维度 0%(像素相同),体积维度 0%(码率相同)
        self.assertEqual(p["l2"]["gain_engine"], 0.0)
        self.assertEqual(p["l2"]["gain_size"], 0.0)

    def test_scenario2_720p_400k_below_l2(self):
        """② 720P / 400k 低于 L2:同样整体失效(再降码率反而是升码率)。"""
        p = plan(rec(resolution=720, bitrate=400))
        self.assertEqual(p["state"], "DISABLED_DOMINATED")
        self.assertLess(p["l2"]["gain_size"], 0, "降级会让体积反向变大,必须为负收益")
        self.assertFalse(p["l2"]["valid"])

    def test_scenario3_720p_800k_only_bitrate_gain(self):
        """③ 720P / 800k:没有引擎收益,只有体积收益 → ARMED(只降码率)。"""
        p = plan(rec(resolution=720, bitrate=800))
        self.assertEqual(p["state"], "ARMED")
        self.assertEqual(p["l1"]["gain_engine"], 0.0)
        self.assertAlmostEqual(p["l1"]["gain_size"], 0.125, places=3)
        self.assertAlmostEqual(p["l2"]["gain_size"], 0.375, places=3)
        self.assertTrue(p["l1"]["valid"])

    def test_scenario4_864_1200k(self):
        """④ 864 / 1200k:两个维度都有收益 → ARMED。"""
        p = plan(rec(resolution=864, bitrate=1200))
        self.assertEqual(p["state"], "ARMED")
        self.assertAlmostEqual(p["l1"]["gain_engine"], 1 - (720 * 1280) / (864 * 1536),
                               places=3)
        self.assertAlmostEqual(p["l1"]["gain_size"], 0.4167, places=3)

    def test_scenario5_native_1200k(self):
        """⑤ 原生 / 1200k(默认):引擎 −55.9%、体积 −41.7% / −58.3%。"""
        p = plan(rec())
        self.assertEqual(p["state"], "ARMED")
        self.assertEqual(p["max_level"], 2)
        self.assertAlmostEqual(p["l1"]["gain_engine"], 0.5588, places=3)
        self.assertAlmostEqual(p["l1"]["gain_size"], 0.4167, places=3)
        self.assertAlmostEqual(p["l2"]["gain_size"], 0.5833, places=3)
        self.assertIn("L1 引擎 −56%", p["reason"])

    def test_scenario6_partial_when_l1_heavier_than_user(self):
        """⑥ L1 被用户改成 864/900k(比 720P/800k 还重)→ PARTIAL,跳过 L1。"""
        p = plan(rec(resolution=720, bitrate=800,
                     degrade_l1_resolution=864, degrade_l1_bitrate=900))
        self.assertEqual(p["state"], "PARTIAL")
        self.assertFalse(p["l1"]["valid"])
        self.assertTrue(p["l2"]["valid"])
        self.assertLess(p["l1"]["gain_engine"], 0, "L1 是放大像素,引擎收益应为负")
        self.assertIn("仅 L2 生效", p["reason"])
        # 触发时必须跳过 L1 直接到 L2
        self.assertEqual(Q.pick_target(p, "resource"), 2)

    def test_scenario7_size_rise_allowed_by_engine_gain(self):
        """⑦ 原生 / 500k:L1 体积会涨 40%,但引擎收益 55.9% ≥ 25% → 放行,且优先选 L2。"""
        p = plan(rec(resolution=0, bitrate=500))
        self.assertEqual(p["state"], "ARMED")
        self.assertAlmostEqual(p["l1"]["gain_size"], -0.4, places=3)
        self.assertTrue(p["l1"]["valid"], "引擎收益够大时体积上涨应放行")
        self.assertTrue(p["l2"]["valid"])
        self.assertEqual(p["l2"]["gain_size"], 0.0)
        self.assertEqual(Q.pick_target(p, "resource"), 2,
                         "L1 会涨体积而 L2 不会 → 应优先 L2")

    def test_scenario7b_disk_guard_blocks_size_rise(self):
        """⑦b 同一场景但磁盘余量不足 → 第 3 层守卫拦下 L1(只剩 L2)。"""
        p = plan(rec(resolution=0, bitrate=500), disk_ok=False)
        self.assertFalse(p["l1"]["valid"], "盘不够时不许为了省引擎把体积放大")
        self.assertTrue(p["l2"]["valid"])
        self.assertEqual(p["state"], "PARTIAL")

    def test_scenario8_copy_mode_disables_degrade(self):
        """⑧ copy 模式没有编码参数可降 → DISABLED_NOCODEC。"""
        p = plan(rec(video_mode="copy"))
        self.assertEqual(p["state"], "DISABLED_NOCODEC")
        self.assertEqual(p["max_level"], 0)
        self.assertIn("流复制", p["reason"])

    def test_switch_off(self):
        p = plan(rec(auto_degrade=False))
        self.assertEqual(p["state"], "DISABLED_OFF")

    def test_soft_encode_does_not_degrade(self):
        """sw(软编)不参与自动降级:CPU 才是瓶颈,应该直接改配置而不是来回切档。"""
        self.assertEqual(plan(rec(video_mode="sw"))["state"], "DISABLED_NOCODEC")


class TestGainMath(unittest.TestCase):
    def test_engine_gain_is_pixel_ratio(self):
        ge, _gs = Q.gains(0, 1200, 720, 1200, *SRC)
        self.assertAlmostEqual(ge, 1 - (720 * 1280) / (1088 * 1920), places=4)

    def test_size_gain_is_bitrate_ratio(self):
        _ge, gs = Q.gains(0, 1200, 0, 500, *SRC)
        self.assertAlmostEqual(gs, 0.5833, places=4)

    def test_same_tier_same_bitrate_gives_zero(self):
        self.assertEqual(Q.gains(720, 700, 720, 700, *SRC), (0.0, 0.0))

    def test_no_bitrate_means_no_size_gain(self):
        """copy 模式(码率 0)不能算出 -inf 或异常值。"""
        self.assertEqual(Q.gains(0, 0, 720, 0, *SRC)[1], 0.0)

    def test_min_gain_gate(self):
        """收益不足 10% 一律不算数(不为 3% 的收益付一次重启丢帧)。"""
        self.assertFalse(Q.usable(0, 1000, 0, 950, *SRC), "5% 体积收益不该放行")
        self.assertTrue(Q.usable(0, 1000, 0, 890, *SRC), "11% 体积收益应放行")


class TestValidateLevels(unittest.TestCase):
    """第 1 层:保存时的反支配校验。"""

    def test_default_config_is_legal(self):
        self.assertIsNone(Q.validate_levels(rec()))

    def test_l2_dominated_by_l1_is_rejected(self):
        err = Q.validate_levels(rec(degrade_l2_bitrate=800))
        self.assertIsNotNone(err)
        self.assertIn("L2", err)

    def test_cross_gradient_is_legal(self):
        """交叉组合(只降码率 + 只降分辨率)必须合法 —— 两档各管一个维度。"""
        err = Q.validate_levels(rec(degrade_l1_resolution=864,
                                    degrade_l1_bitrate=900,
                                    degrade_l2_resolution=720,
                                    degrade_l2_bitrate=500))
        self.assertIsNone(err)

    def test_missing_keys_are_not_checked(self):
        """老配置/局部提交没有降级键:不能拿「全 0」去比对而报假错。"""
        self.assertIsNone(Q.validate_levels({"recorder": {"format": "flv"}}))

    def test_heavier_than_l0_is_allowed_at_save_time(self):
        """两档都比主配置重:保存放行(运行时判无效并跳过),否则用户改主配置会被卡住。"""
        cfg = rec(resolution=720, bitrate=500,
                  degrade_l1_resolution=0, degrade_l1_bitrate=1200)
        self.assertIsNone(Q.validate_levels(cfg))


class TestPickTarget(unittest.TestCase):
    def test_crash_goes_to_lowest(self):
        p = plan(rec())
        self.assertEqual(Q.pick_target(p, "crash"), 2)

    def test_resource_goes_one_step(self):
        self.assertEqual(Q.pick_target(plan(rec()), "resource"), 1)

    def test_partial_skips_l1(self):
        p = plan(rec(resolution=720, bitrate=800, degrade_l1_resolution=864,
                     degrade_l1_bitrate=900))
        self.assertEqual(Q.pick_target(p, "resource"), 2)

    def test_nothing_valid_stays_put(self):
        p = plan(rec(resolution=720, bitrate=500))
        self.assertEqual(Q.pick_target(p, "resource"), 0)


class TestSnapshotsAndState(unittest.TestCase):
    """状态持久化:损坏就回 L0,正常情况下能原样恢复。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "state", "quality_state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip(self):
        g = Q.Governor(rec()["recorder"], now=1000.0)
        g.level = 1
        g.switches = 1
        g.day = "2026-10-22"
        g.boot_id = "boot-1"
        self.assertTrue(Q.save_state(self.path, g.dump()))
        data = Q.load_state(self.path)
        g2 = Q.Governor(rec()["recorder"], now=2000.0)
        self.assertTrue(g2.restore(data, now=2000.0))
        self.assertEqual(g2.level, 1)

    def test_corrupt_file_returns_none(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(Q.load_state(self.path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(Q.load_state(self.path))

    def test_restore_garbage_falls_back_to_l0(self):
        g = Q.Governor(rec()["recorder"], now=0)
        self.assertFalse(g.restore({"level": "abc"}, now=0))
        self.assertEqual(g.level, 0)

    def test_illegal_level_in_file_is_clamped(self):
        g = Q.Governor(rec()["recorder"], now=0)
        g.restore({"level": 9}, now=0)
        self.assertEqual(g.level, 0)

    def test_save_is_atomic_and_creates_dir(self):
        self.assertTrue(Q.save_state(self.path, {"a": 1}))
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
