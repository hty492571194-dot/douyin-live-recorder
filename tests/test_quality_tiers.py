# -*- coding: utf-8 -*-
"""分辨率档位换算(quality.py)与前端档位表的一致性守卫。

档位这件事最容易错的地方不是算法,而是「两个口径」:参数生成按一套算、
降级收益按另一套算,于是降了档却以为省了引擎。所以这里钉三件事:

  ① `tier_size()` 严格「只缩不放 + 按横竖选向 + 取偶」;
  ② `scale_expr()` 交给 ffmpeg 的表达式与本模块的数值口径**语义一致**
     (2026-10-22 本机实测:scale_vt 的 w/h 支持表达式,1088×1920 源在 864 档
      实出 (864,1536),720 档实出 (720,1280));
  ③ 前端 RESOLUTION_OPTIONS / VIDEO_MODE_OPTIONS 必须 ⊆ 后端白名单 ——
     前端多给一个后端不认的值,用户一选就是 `-vf` 报错、ffmpeg 起来即退出
     (表现成「检测到开播却录不到文件」,极难往档位上想)。
"""
import os
import re
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import quality as Q  # noqa: E402
import monitor        # noqa: E402
import webui          # noqa: E402


class TestTierNormalisation(unittest.TestCase):
    """档位值的白名单:非法值绝不能透传。"""

    def test_whitelist_values(self):
        for v in Q.TIER_VALUES:
            self.assertEqual(Q.norm_tier(v), v)

    def test_string_values_from_frontend_select(self):
        """前端下拉的 value 是字符串,必须认。"""
        for v in ("0", "1080", "864", "720", " 864 ", "864.0"):
            self.assertIsNotNone(Q.norm_tier(v), v)

    def test_invalid_values_rejected(self):
        for bad in ("870", 1080.5, "864k", "high", "1920", -1, 2160):
            self.assertIsNone(Q.norm_tier(bad), "非法档位 %r 被放过了" % (bad,))

    def test_empty_means_native(self):
        """空值/None 视作原生(老配置没有这个键时不该报错)。"""
        for v in (None, "", "  "):
            self.assertEqual(Q.norm_tier(v), 0)


class TestTierSize(unittest.TestCase):
    PORTRAIT = (1088, 1920)

    def test_four_tiers_on_real_source(self):
        """真实源 1088×1920(项目内样本场次):四档的目标尺寸。"""
        self.assertIsNone(Q.tier_size(0, *self.PORTRAIT), "原生档不该产生缩放")
        self.assertEqual(Q.tier_size(1080, *self.PORTRAIT), (1080, 1920))
        self.assertEqual(Q.tier_size(864, *self.PORTRAIT), (864, 1536))
        self.assertEqual(Q.tier_size(720, *self.PORTRAIT), (720, 1280))

    def test_never_upscales(self):
        """720×1280 的主播选 1080P 不许放大(放大既费引擎又更糊)。"""
        self.assertIsNone(Q.tier_size(1080, 720, 1280))
        self.assertIsNone(Q.tier_size(864, 720, 1280))
        self.assertIsNone(Q.tier_size(720, 720, 1280))

    def test_landscape_uses_swapped_standard_size(self):
        self.assertEqual(Q.tier_size(720, 1920, 1088), (1280, 720))
        self.assertEqual(Q.tier_size(1080, 1920, 1088), (1920, 1080))
        self.assertEqual(Q.tier_size(864, 2560, 1440), (1536, 864))

    def test_landscape_source_exactly_at_tier_is_not_scaled(self):
        """1920×1080 的源选 1080P:短边正好等于档位,不该再缩一次。"""
        self.assertIsNone(Q.tier_size(1080, 1920, 1080))

    def test_even_dimensions(self):
        for src in ((1089, 1921), (1079, 1919)):
            for t in (1080, 864, 720):
                size = Q.tier_size(t, *src)
                if size:
                    self.assertEqual(size[0] % 2, 0, size)
                    self.assertEqual(size[1] % 2, 0, size)

    def test_pixels_matches_size(self):
        """pixels() 是引擎需求的唯一数据源,必须等于实际编码尺寸的乘积。"""
        self.assertEqual(Q.pixels(720, *self.PORTRAIT), 720 * 1280)
        self.assertEqual(Q.pixels(864, *self.PORTRAIT), 864 * 1536)
        self.assertEqual(Q.pixels(0, *self.PORTRAIT), 1088 * 1920)

    def test_pixels_without_source_uses_standard(self):
        """源尺寸未知时按标准尺寸估(偏差 <1%,只影响引擎估算不影响参数)。"""
        self.assertEqual(Q.pixels(864), 864 * 1536)
        self.assertEqual(Q.pixels(0), Q.ASSUMED_SRC[0] * Q.ASSUMED_SRC[1])

    def test_effective_size_prefers_real_source(self):
        self.assertEqual(Q.effective_size(0, 1088, 1920), (1088, 1920))
        self.assertEqual(Q.effective_size(720, 1088, 1920), (720, 1280))
        self.assertEqual(Q.effective_size(0), Q.ASSUMED_SRC)


class TestScaleExpression(unittest.TestCase):
    """交给 ffmpeg 的表达式:语义必须与 tier_size 一致。"""

    def test_native_has_no_filter(self):
        self.assertIsNone(Q.scale_expr(0))
        self.assertIsNone(Q.vf_filter(0))

    def test_expression_guards_min(self):
        """表达式里必须带 min() —— 它就是「只缩不放」的保证。"""
        for t in (1080, 864, 720):
            expr = Q.scale_expr(t)
            self.assertIn("min(iw,", expr)
            self.assertIn("min(ih,", expr)
            self.assertIn("gte(iw,ih)", expr, "横竖方向必须自动判定")

    def test_hardware_uses_cpu_scale(self):
        """硬解路线也用 CPU `scale`:scale_vt 撑不住滤镜图重配置。

        见 `quality.SCALE_VT_ENABLED` —— 这条不是优化取舍,是崩溃修复。
        """
        self.assertFalse(Q.SCALE_VT_ENABLED)
        vf = Q.vf_filter(720, hw=True)
        self.assertTrue(vf.startswith("scale="), vf)
        self.assertNotIn("scale_vt", vf)

    def test_software_uses_cpu_scale(self):
        self.assertTrue(Q.vf_filter(720, hw=False).startswith("scale="))

    def test_expression_matches_tier_size_numbers(self):
        """把表达式里的四个数字与 tier_size 的横竖两个结果对上(防改一处漏一处)。

        表达式是 `w=if(横屏, min(iw, 横屏宽), min(iw, 竖屏宽))`、h 同理,所以:
            nums[0], nums[2] → 横屏的 (w, h)
            nums[1], nums[3] → 竖屏的 (w, h)
        """
        for t in (1080, 864, 720):
            expr = Q.scale_expr(t)
            nums = [int(x) for x in re.findall(r"min\((?:iw|ih),(\d+)\)", expr)]
            self.assertEqual(len(nums), 4, expr)
            portrait = Q.tier_size(t, 1088, 1920)
            landscape = Q.tier_size(t, 1920, 1088)
            self.assertEqual((nums[1], nums[3]), (portrait[0], portrait[1]), expr)
            self.assertEqual((nums[0], nums[2]), (landscape[0], landscape[1]), expr)


class TestEngineMath(unittest.TestCase):
    def test_demand_of_seven_native_streams(self):
        """7 路原生(1088×1920@20fps)应落在计划的 65–75% 区间。"""
        streams = [(1088, 1920, 20)] * 7
        pct = Q.demand_pct(streams)
        self.assertGreater(pct, 60)
        self.assertLess(pct, 80)

    def test_demand_halves_with_720(self):
        """720P 档的引擎需求应显著低于原生(实测 −55.9%)。"""
        native = Q.demand_pct([(1088, 1920, 20)] * 7)
        low = Q.demand_pct([(720, 1280, 20)] * 7)
        self.assertLess(low, native * 0.5)

    def test_engine_util_penalises_low_follow(self):
        """跟随率掉到 0.5 → 占用翻倍(需求 45% 其实已经把引擎吃满)。"""
        self.assertEqual(Q.engine_util(45.0, 1.0), 45.0)
        self.assertEqual(Q.engine_util(45.0, 0.5), 90.0)
        self.assertEqual(Q.engine_util(45.0, 0.0), 90.0, "跟随率为 0 也要有下限")

    def test_gain_values_match_plan(self):
        """实测基准:原生→720P 引擎 +55.9%;1200→700 体积 +41.7%。"""
        ge, gs = Q.gains(0, 1200, 720, 700, 1088, 1920)
        self.assertAlmostEqual(ge, 1 - (720 * 1280) / (1088 * 1920), places=3)
        self.assertAlmostEqual(gs, 0.4167, places=3)
        self.assertGreater(ge, 0.55)


class TestFrontendTierTable(unittest.TestCase):
    """前端档位/编码方式下拉必须 ⊆ 后端白名单。"""

    def setUp(self):
        with open(os.path.join(webui.BASE, "static", "index.html"),
                  encoding="utf-8") as f:
            self.html = f.read()

    def _values(self, const):
        m = re.search(r"const %s = \[(.*?)\n\];" % const, self.html, re.S)
        self.assertIsNotNone(m, "前端缺少 %s" % const)
        return re.findall(r'\["([^"]+)",', m.group(1))

    def test_resolution_options_subset(self):
        vals = self._values("RESOLUTION_OPTIONS")
        self.assertEqual(vals, [str(v) for v in Q.TIER_VALUES],
                         "档位选项与 quality.TIER_VALUES 不一致(顺序也要一致)")

    def test_video_mode_options_subset(self):
        vals = self._values("VIDEO_MODE_OPTIONS")
        self.assertEqual(sorted(vals), sorted(monitor.VIDEO_MODES))

    def test_recorder_fields_registered(self):
        """录制设置页必须有这四个入口,且都指向后端真键。"""
        for path in ("recorder.resolution", "recorder.bitrate", "recorder.video_mode",
                     "recorder.auto_degrade"):
            self.assertIn('"%s"' % path, self.html)
            self.assertIn(path.split(".", 1)[1], monitor.DEFAULT_CONFIG["recorder"])


class TestNoSeekWithHwScale(unittest.TestCase):
    """回归守卫:录制参数里不许出现 `-ss`,也不许出现 `-hwaccel_output_format`。

    2026-10-22 实测:带上输入侧 `-ss` 后滤镜图报
    `Impossible to convert between the formats ... auto_scale_0` 初始化失败,
    当时把原因记成「seek 与硬解帧直出冲突」。

    **2026-10-29 找到真因**:那条报错是 `scale_vt` 撑不住滤镜图重配置的表现,
    `-ss` 只是当时撞上的一个触发点 —— 生产上真正引爆它的是源流参数变化
    (见 `quality.SCALE_VT_ENABLED`)。现在 scale_vt 与硬解帧直出都已弃用,
    `-ss` 依旧禁止(直播流没有"跳转"概念,测试/剪辑脚本很容易顺手加)。
    """

    def test_record_args_have_no_seek(self):
        args, _ = monitor._build_ffmpeg_args(
            "/usr/bin/ffmpeg", "http://example/s.flv", "/tmp/o", "主播甲", "flv",
            dict(monitor.DEFAULT_CONFIG["recorder"]), "20261022-040000", tier=864)
        self.assertNotIn("-ss", args)
        self.assertNotIn("-hwaccel_output_format", args)


if __name__ == "__main__":
    unittest.main()
