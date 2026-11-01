# -*- coding: utf-8 -*-
"""硬件编码参数生成(_build_ffmpeg_args 的 vt / sw / copy 三支)守卫。

这一组断言全部来自**实测踩过的坑**(2026-10-22,ffmpeg 7.1 + M4):

  · `-hwaccel` 必须在 `-i` 之前,否则被当成输出选项 → Invalid argument;
  · **`scale_vt` 不许用**(2026-10-29 定案,见 `TestScaleVtDisabled`):它撑不住
    滤镜图重配置,源流参数一变(分辨率/位深)就让整个 ffmpeg 退出;因此
    `-hwaccel_output_format videotoolbox_vld` 也不许出现,缩放一律走 CPU `scale`;
  · `-q:v` 与 `-b:v` 不能并存(q 值会压过码率,体积失控);
  · `h264_videotoolbox` 不接受 `-preset`(给了会被拒);
  · 三个时间戳开关一个都不能动(字幕对齐的地基)。

**破坏性验证**:把 `-hwaccel` 挪到 `-i` 之后;或把 `quality.SCALE_VT_ENABLED`
改成 True 把 GPU 缩放放回来 —— 两种情况本文件都必须报红(均已人工验证)。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor  # noqa: E402
import quality as Q  # noqa: E402


def args_for(rec_cfg, tier=None, progress=None):
    args, _ = monitor._build_ffmpeg_args(
        "/usr/bin/ffmpeg", "http://example/stream.flv", "/tmp/out",
        "主播甲", "flv", rec_cfg, "20261022-040000", tier=tier, progress_path=progress)
    return args


def vt_cfg(**kw):
    cfg = {"video_mode": "vt", "bitrate": 1200, "hw_decode": True, "force_hw": True}
    cfg.update(kw)
    return cfg


class TestHardwarePath(unittest.TestCase):
    def test_hwaccel_before_input(self):
        """★ -hwaccel 在 -i 之前:放后面会被当输出选项,ffmpeg 直接 Invalid argument。"""
        args = args_for(vt_cfg(), tier=864)
        self.assertIn("-hwaccel", args)
        self.assertLess(args.index("-hwaccel"), args.index("-i"))

    def test_no_hw_output_format(self):
        """★ 不许出现 `-hwaccel_output_format`:硬解帧直出会让滤镜图重建失败。

        它存在的唯一理由是喂 `scale_vt`;scale_vt 弃用后它就成了纯粹的雷
        (见 TestScaleVtDisabled)。
        """
        for tier in (1080, 864, 720):
            args = args_for(vt_cfg(), tier=tier)
            self.assertNotIn("-hwaccel_output_format", args, "tier=%s" % tier)

    def test_scale_is_used_for_every_tier(self):
        """★ 缩放档位一律 CPU `scale`,且不再依赖 output_format。"""
        for tier in (1080, 864, 720):
            args = args_for(vt_cfg(), tier=tier)
            vf = args[args.index("-vf") + 1]
            self.assertTrue(vf.startswith("scale="), vf)
            self.assertNotIn("scale_vt", vf, "tier=%s" % tier)

    def test_native_has_no_scale_and_no_output_format(self):
        """原生档:不缩放,也就不需要 videotoolbox_vld 输出格式。"""
        args = args_for(vt_cfg(), tier=0)
        self.assertNotIn("-vf", args)
        self.assertNotIn("-hwaccel_output_format", args)
        self.assertIn("-hwaccel", args)

    def test_no_qv_with_bv(self):
        """★ -q:v 会静默吞掉 -b:v(码率失控),两支永远不能并存。"""
        args = args_for(vt_cfg(), tier=720)
        self.assertIn("-b:v", args)
        self.assertNotIn("-q:v", args)

    def test_no_preset_in_hardware_path(self):
        """硬件编码没有 preset 概念,带了会被拒绝。"""
        args = args_for(vt_cfg(preset="veryfast"), tier=720)
        self.assertNotIn("-preset", args)

    def test_allow_sw_when_force_hw(self):
        args = args_for(vt_cfg(), tier=720)
        self.assertEqual(args[args.index("-allow_sw") + 1], "0")

    def test_allow_sw_absent_when_not_forced(self):
        args = args_for(vt_cfg(force_hw=False), tier=720)
        self.assertNotIn("-allow_sw", args)

    def test_encoder_name(self):
        args = args_for(vt_cfg(), tier=720)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_videotoolbox")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-b:a") + 1], "128k")

    def test_no_hwaccel_when_decode_disabled(self):
        """关掉硬解仍要硬编,但不能带 -hwaccel(有些坏流只有软解扛得住)。"""
        args = args_for(vt_cfg(hw_decode=False), tier=0)
        self.assertNotIn("-hwaccel", args)
        self.assertEqual(args[args.index("-c:v") + 1], "h264_videotoolbox")

    def test_soft_decode_with_scale_uses_cpu_filter(self):
        """软解 + 缩放:ffmpeg 7.1 的 hwupload→videotoolbox_vld 走不通(实测 -22),
        所以必须用 CPU `scale`。(硬解路线现在也一样,见 TestScaleVtDisabled。)"""
        args = args_for(vt_cfg(hw_decode=False), tier=720)
        vf = args[args.index("-vf") + 1]
        self.assertTrue(vf.startswith("scale="), vf)
        self.assertNotIn("scale_vt", vf)
        self.assertNotIn("-hwaccel_output_format", args)


class TestScaleVtDisabled(unittest.TestCase):
    """`scale_vt` 已被实测否决(2026-10-29):它撑不住滤镜图重配置。

    直播流一旦在运行中改变帧参数(分辨率、位深,哪怕只跳一帧又变回去),ffmpeg
    就要重建滤镜图;重建时 `scale_vt` 输出的 `videotoolbox_vld` 与自动插入的
    `scale` 协商失败:

        Impossible to convert between the formats supported by the filter
        'Parsed_scale_vt_0' and the filter 'auto_scale_0'
        Error reinitializing filters! → error code -78 (Function not implemented)

    vf 线程随即终止,整个 ffmpeg 退出 —— 8 天 86 次崩溃全是这一条。触发只取决于
    源流稳不稳,与并发路数、画幅、档位都无关,所以集中在少数主播身上,曾经被
    误判成「资源不足」。上游 2023-07 至今未修,升级 ffmpeg 解决不了。

    这一组把「不许回到 scale_vt」钉死:谁把 SCALE_VT_ENABLED 打开,立刻报红。
    """

    def test_scale_vt_is_disabled_by_design(self):
        self.assertFalse(
            Q.SCALE_VT_ENABLED,
            "scale_vt 撑不住滤镜图重配置,打开它等于恢复 86 次/8 天的录制崩溃")

    def test_no_scale_vt_in_any_tier_or_decode_mode(self):
        for hw_decode in (True, False):
            for tier in (1080, 864, 720):
                args = args_for(vt_cfg(hw_decode=hw_decode), tier=tier)
                vf = args[args.index("-vf") + 1]
                self.assertTrue(vf.startswith("scale="), vf)
                self.assertNotIn("scale_vt", vf,
                                 "tier=%s hw_decode=%s" % (tier, hw_decode))
                self.assertNotIn("-hwaccel_output_format", args)

    def test_hardware_decode_and_encode_are_kept(self):
        """弃用的只是 GPU 缩放:硬解与硬编都留着,代价是 +0.17 核/路而不是回退软解。"""
        args = args_for(vt_cfg(), tier=1080)
        self.assertEqual(args[args.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(args.index("-hwaccel"), args.index("-i"))
        self.assertEqual(args[args.index("-c:v") + 1], "h264_videotoolbox")
        self.assertEqual(args[args.index("-allow_sw") + 1], "0")


class TestTimestampsKept(unittest.TestCase):
    """时间基:任何编码路线都不许逐包打墙钟,`-copyts` 一直都在。"""

    MODES = ({"video_mode": "vt", "bitrate": 1200},
             {"video_mode": "sw", "bitrate": 1200},
             {"video_mode": "copy", "bitrate": 0},
             {})

    def test_no_wallclock_in_any_mode(self):
        for cfg in self.MODES:
            args = args_for(cfg, tier=864)
            self.assertNotIn("-use_wallclock_as_timestamps", args,
                             "默认时间基不许逐包覆盖 PTS: %r" % (cfg,))
            self.assertIn("-copyts", args)

    def test_wallclock_fallback_position(self):
        for cfg in self.MODES:
            args = args_for(dict(cfg, src_timestamps=False), tier=864)
            i = args.index("-i")
            self.assertEqual(args[i - 2:i], ["-use_wallclock_as_timestamps", "1"],
                             "回退时墙钟开关必须紧邻 -i 之前: %r" % (cfg,))

    def test_segment_does_not_reset_timestamps(self):
        for cfg in self.MODES:
            args = args_for(dict(cfg, segment_time=2300), tier=864)
            self.assertIn("-reset_timestamps", args)
            self.assertEqual(args[args.index("-reset_timestamps") + 1], "0",
                             "分片重置时间戳会让跨分片锚点断裂: %r" % (cfg,))


class TestModes(unittest.TestCase):
    def test_copy_when_no_bitrate(self):
        args = args_for({"video_mode": "vt", "bitrate": 0}, tier=720)
        self.assertIn("copy", args)
        self.assertNotIn("-c:v", args)
        self.assertNotIn("-vf", args, "copy 模式不能带滤镜")

    def test_sw_mode_uses_libx264_and_preset(self):
        args = args_for({"video_mode": "sw", "bitrate": 1500, "preset": "veryfast"},
                        tier=720)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-preset") + 1], "veryfast")
        self.assertTrue(args[args.index("-vf") + 1].startswith("scale="))

    def test_illegal_mode_falls_back_to_libx264(self):
        """非法 video_mode 不能透传(否则 ffmpeg 起不来),一律回落老行为。"""
        for bad in ("hardware", "videotoolbox", "nv", "vt2", 1):
            args = args_for({"video_mode": bad, "bitrate": 1200})
            self.assertEqual(args[args.index("-c:v") + 1], "libx264", repr(bad))

    def test_mode_is_case_and_space_insensitive(self):
        """用户在配置文件里手写 " VT "(大小写/空格)不该被判成非法。"""
        args = args_for({"video_mode": " VT ", "bitrate": 1200})
        self.assertEqual(args[args.index("-c:v") + 1], "h264_videotoolbox")

    def test_missing_mode_keeps_legacy_behaviour(self):
        """缺键 = 老行为(libx264)。真实配置由 load_config 补齐默认 'vt'。"""
        args = args_for({"bitrate": 1200})
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")


class TestBitrateUnits(unittest.TestCase):
    def test_numbers_and_suffixes(self):
        for value, expect in ((1200, "1200k"), ("1200", "1200k"), ("1200k", "1200k"),
                              ("3M", "3M"), (1200.0, "1200k")):
            args = args_for(vt_cfg(bitrate=value), tier=0)
            self.assertEqual(args[args.index("-b:v") + 1], expect, repr(value))

    def test_zero_means_copy(self):
        for v in (0, "", None, "0"):
            args = args_for(vt_cfg(bitrate=v), tier=0)
            self.assertIn("copy", args, repr(v))


class TestProgressFile(unittest.TestCase):
    def test_progress_option_before_output(self):
        """-progress 必须存在且落在输出文件之前(否则不会被 ffmpeg 读到)。"""
        args = args_for(vt_cfg(), tier=720, progress="/tmp/x.progress")
        self.assertIn("-progress", args)
        self.assertEqual(args[args.index("-progress") + 1], "/tmp/x.progress")
        self.assertLess(args.index("-progress"), len(args) - 1)
        self.assertIn("-stats_period", args)

    def test_progress_absent_when_not_given(self):
        args = args_for(vt_cfg(), tier=720)
        self.assertNotIn("-progress", args)

    def test_read_progress_parses_microseconds(self):
        """out_time_ms 实际是微秒(ffmpeg 历史命名),解析必须按微秒对待。"""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".progress", delete=False,
                                         encoding="utf-8") as f:
            f.write("frame=88\nfps=0.00\nout_time_us=3954545\nout_time_ms=3954545\n"
                    "progress=continue\n")
            path = f.name
        try:
            got = monitor._read_progress(path)
        finally:
            os.remove(path)
        self.assertEqual(got, {"frame": 88, "out_us": 3954545})

    def test_read_progress_missing_file(self):
        self.assertIsNone(monitor._read_progress("/nonexistent/x.progress"))
        self.assertIsNone(monitor._read_progress(None))


class TestEngineAdmission(unittest.TestCase):
    """准入控制:在录路数到上限后,新增路必须退成 copy(不占引擎)。"""

    def setUp(self):
        self._saved = dict(monitor._recordings)
        self._saved_gov = monitor._quality_gov
        monitor._recordings.clear()
        monitor._quality_gov = None
        monitor._hw_ok = True
        monitor._hw_reason = ""

    def tearDown(self):
        monitor._recordings.clear()
        monitor._recordings.update(self._saved)
        monitor._quality_gov = self._saved_gov
        monitor._hw_ok = None

    def _fake(self, name, mode="vt"):
        class P:
            def poll(self):
                return None
        monitor._recordings[name] = {"proc": P(), "video_mode": mode}

    def test_admits_copy_when_over_limit(self):
        cfg = {"recorder": {"video_mode": "vt", "bitrate": 1200, "max_hw_streams": 2,
                           "resolution": 0, "auto_degrade": False,
                           "degrade_l1_resolution": 720, "degrade_l1_bitrate": 700,
                           "degrade_l2_resolution": 720, "degrade_l2_bitrate": 500}}
        self._fake("A")
        self._fake("B")
        tier, br, mode, level, why = monitor._active_encode(cfg, "C")
        self.assertEqual(mode, "copy")
        self.assertIn("准入上限", why)
        self.assertEqual(level, 0)

    def test_copy_streams_do_not_count(self):
        cfg = {"recorder": {"video_mode": "vt", "bitrate": 1200, "max_hw_streams": 2,
                           "resolution": 0}}
        self._fake("A")
        self._fake("B", mode="copy")
        _t, _b, mode, _l, _w = monitor._active_encode(cfg, "C")
        self.assertEqual(mode, "vt", "copy 路不占引擎,不该挤掉硬编名额")

    def test_hw_unavailable_falls_back_to_copy(self):
        cfg = {"recorder": {"video_mode": "vt", "bitrate": 1200, "max_hw_streams": 8,
                           "resolution": 0}}
        monitor._hw_ok = False
        monitor._hw_reason = "该 ffmpeg 未编入 h264_videotoolbox"
        _t, _b, mode, _l, why = monitor._active_encode(cfg, "A")
        self.assertEqual(mode, "copy")
        self.assertIn("硬件编码不可用", why)


class TestQualityPathHelpers(unittest.TestCase):
    def test_quality_state_path_under_state_dir(self):
        self.assertTrue(monitor.QUALITY_STATE_PATH.endswith(
            os.path.join("state", "quality_state.json")))
        self.assertTrue(os.path.isabs(monitor.QUALITY_STATE_PATH))

    def test_progress_path_under_state_dir(self):
        p = monitor._progress_path("主播甲")
        self.assertTrue(p.endswith(".progress"))
        self.assertIn(os.path.join("state", "progress"), p)

    def test_video_modes_whitelist(self):
        self.assertEqual(tuple(monitor.VIDEO_MODES), ("vt", "sw", "copy"))
        self.assertEqual(sorted(Q.TIER_VALUES), [0, 720, 864, 1080])


if __name__ == "__main__":
    unittest.main()
