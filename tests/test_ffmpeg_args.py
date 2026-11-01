#!/usr/bin/env python3
"""ffmpeg 参数构造的单元测试(_build_ffmpeg_args)。

这个函数是录制的核心,却一直没有测试覆盖 —— 它同时承担三件事:
  ① 时间基:默认(src_timestamps=True)**不加** `-use_wallclock_as_timestamps`,
     让 FLV tag 保留源流时间戳(音视频同一坐标系);回退路径
     (src_timestamps=False)才加,且必须紧邻 `-i` 之前。`-copyts` 一直要在;
  ② 码率与转码预设:`bitrate` 空 → `-c copy` 不重编码;非空 → libx264 + preset;
  ③ 分片:段时长、%03d 命名、不重置时间戳(跨分片连续)。

① 的来历(2026-10-27):墙钟打戳是**逐包**覆盖,包成批到达时 PTS 被压扁、
断流处跳秒,音轨采样数与视轨脱钩(转录端实测少 8.43 s)。详见
`音画不同步-根因分析与修复方案.md`。

其中 preset 是 2026-09-18 加的参数。起因:实测 4 路并发下 libx264 默认的
medium 每路只剩 1.23x 实时余量(几乎没有),veryfast 有 2.63x。
而**非法 preset 直接透传给 ffmpeg 会让拉流进程一起来就退出** ——
表现成「检测到开播却录不到文件」,很难往参数上想,所以这里锁白名单。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402


def args_for(rec_cfg):
    args, _ = monitor._build_ffmpeg_args(
        "/usr/bin/ffmpeg", "http://example/stream.flv", "/tmp/out",
        "主播甲", "flv", rec_cfg, "20260918-130000")
    return args


class TestAlignmentGroundwork(unittest.TestCase):
    """时间基参数:动它们等于动整条字幕 + 音画同步链路。"""

    def test_wallclock_off_by_default(self):
        """默认必须**不带**逐包墙钟 —— 它会压扁 PTS、让音轨采样数与视轨脱钩。"""
        args = args_for({})
        self.assertNotIn("-use_wallclock_as_timestamps", args,
                         "默认(src_timestamps=True)不许逐包覆盖时间戳")

    def test_wallclock_fallback_sits_right_before_input(self):
        """回退路径(src_timestamps=False)保留旧行为,且开关必须紧邻 -i 之前。"""
        args = args_for({"src_timestamps": False})
        i = args.index("-i")
        self.assertEqual(args[i - 2:i], ["-use_wallclock_as_timestamps", "1"],
                         "墙钟时间戳必须紧邻 -i 之前,否则对输入不生效")

    def test_copyts_present(self):
        self.assertIn("-copyts", args_for({}))

    def test_segment_does_not_reset_timestamps(self):
        args = args_for({"segment_time": 2300})
        self.assertIn("-f", args)
        self.assertIn("segment", args)
        self.assertIn("-reset_timestamps", args)
        self.assertEqual(args[args.index("-reset_timestamps") + 1], "0",
                         "分片重置时间戳会让跨分片锚点断裂")


class TestAudioSync(unittest.TestCase):
    """断流补静音:只在新时间基下启用,且绝不能跟墙钟打戳同时出现。

    为什么这么在意组合:`aresample=async=1` 是按 PTS 硬补偿。墙钟打戳时 PTS
    本身被压扁,补偿会把**真实采样**当多余丢掉(38 分钟实测丢 ~160s);
    源时间戳模式下正常偏差仅 0.44ms,阈值 0.5s 不会误伤。
    """

    def test_aresample_present_for_reencode_modes(self):
        for cfg in ({"video_mode": "vt", "bitrate": 1200},
                    {"video_mode": "sw", "bitrate": 1200}):
            args = args_for(cfg)
            self.assertIn("-af", args, cfg)
            # ★ 期望值硬编码,不引用 monitor.AUDIO_SYNC_FILTER:
            #   引用常量的话,把阈值改坏(0.5→5 会误伤正常抖动)测试照样绿。
            self.assertEqual(args[args.index("-af") + 1],
                             "aresample=async=1:min_hard_comp=0.5",
                             "补静音阈值必须锁 0.5s: %r" % (cfg,))

    def test_aresample_absent_in_copy_mode(self):
        """copy 模式音频原样透传,加 -af 会强制重编码、失去 copy 的意义。"""
        args = args_for({"video_mode": "copy", "bitrate": 0})
        self.assertNotIn("-af", args)

    def test_aresample_absent_when_falling_back_to_wallclock(self):
        """★ 墙钟 + 硬补偿会吃掉真实采样 —— 两个开关互斥。"""
        args = args_for({"src_timestamps": False, "video_mode": "vt", "bitrate": 1200})
        self.assertNotIn("-af", args, "回退路径绝不能带 aresample:会丢真实音频采样")


class TestBitrateAndPreset(unittest.TestCase):
    def test_empty_bitrate_means_stream_copy(self):
        args = args_for({})
        self.assertIn("-c", args)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertNotIn("-c:v", args, "不重编码时不该出现编码器参数")
        self.assertNotIn("-preset", args, "copy 模式下 preset 无意义")

    def test_digit_bitrate_becomes_k_suffix(self):
        args = args_for({"bitrate": 1500})
        self.assertEqual(args[args.index("-b:v") + 1], "1500k")

    def test_string_bitrate_passes_through(self):
        for v in ("3000k", "3M"):
            a = args_for({"bitrate": v})
            self.assertEqual(a[a.index("-b:v") + 1], v)

    def test_zero_bitrate_is_copy(self):
        self.assertIn("copy", args_for({"bitrate": 0}))

    def test_preset_used_when_transcoding(self):
        args = args_for({"bitrate": 1500, "preset": "veryfast"})
        self.assertEqual(args[args.index("-preset") + 1], "veryfast")
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")

    def test_missing_preset_keeps_ffmpeg_default(self):
        """老配置没有 preset 键 → 不带 -preset(由 libx264 用默认 medium)。

        行为与加这个参数之前一致,不给老配置强加改动。
        """
        self.assertNotIn("-preset", args_for({"bitrate": 1500}))

    def test_illegal_preset_is_dropped(self):
        """★ 非法 preset 绝不能透传:ffmpeg 会立即退出,表现为「录不到文件」。"""
        for bad in ("turbo", "fastest", "1", "veryfast; rm -rf /", None, ""):
            args = args_for({"bitrate": 1500, "preset": bad})
            self.assertNotIn("-preset", args, "非法值 %r 被透传了" % (bad,))
            self.assertIn("-b:v", args)

    def test_preset_case_insensitive(self):
        self.assertIn("-preset", args_for({"bitrate": 1500, "preset": "VeryFast"}))


class TestNormPreset(unittest.TestCase):
    def test_whitelist(self):
        for p in monitor.X264_PRESETS:
            self.assertEqual(monitor._norm_preset(p), p)

    def test_normalises_case_and_space(self):
        self.assertEqual(monitor._norm_preset("  Fast "), "fast")

    def test_rejects_unknown(self):
        for bad in ("", None, "nope", "very fast", "1", "veryfast;"):
            self.assertEqual(monitor._norm_preset(bad), "", repr(bad))

    def test_blank_padding_is_normalised_not_rejected(self):
        self.assertEqual(monitor._norm_preset(" medium "), "medium")


class TestDefaultConfigPreset(unittest.TestCase):
    def test_default_is_veryfast(self):
        """默认必须是 veryfast:4 路并发下 medium 只剩 1.23x 实时余量。"""
        self.assertEqual(monitor.DEFAULT_CONFIG["recorder"].get("preset"), "veryfast")

    def test_default_preset_is_legal(self):
        self.assertIn(monitor.DEFAULT_CONFIG["recorder"]["preset"], monitor.X264_PRESETS)


class TestFrontendPresetField(unittest.TestCase):
    """前端下拉选项必须 ⊆ 后端白名单 —— 否则用户选了个后端不认的值,
    保存后 ffmpeg 起来就退出,表现成「录不到文件」。"""

    def setUp(self):
        import webui
        with open(os.path.join(webui.BASE, "static", "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def _options(self):
        import re
        # 2026-10-11:preset 下拉改为带说明的 4 档(选项= [值, 说明] 对),
        # 数据源在 PRESET_OPTIONS 常量;这里提取各选项的值。
        m = re.search(r"const PRESET_OPTIONS = \[(.*?)\];", self.html, re.S)
        self.assertIsNotNone(m, "前端 PRESET_OPTIONS 未找到(recorder.preset 的选项源)")
        opts = re.findall(r'\["([a-zA-Z0-9_]+)",', m.group(1))
        self.assertTrue(opts, "前端 preset 选项为空")
        return opts

    def test_field_exists_and_is_select(self):
        self.assertIn('"recorder.preset"', self.html)

    def test_options_are_subset_of_whitelist(self):
        opts = self._options()
        self.assertTrue(opts, "下拉选项为空")
        bad = [o for o in opts if o not in monitor.X264_PRESETS]
        self.assertEqual(bad, [], "前端提供了后端不认的 preset: %s" % bad)

    def test_veryfast_offered(self):
        self.assertIn("veryfast", self._options())


if __name__ == "__main__":
    unittest.main()

class TestFfmpegErrLog(unittest.TestCase):
    """ffmpeg 的报错必须留下来。

    原先 stderr 被丢进 DEVNULL,于是「起来 20 秒就退出」在日志里只剩一句
    「已退出」,看不出任何原因(2026-09-18 排查某主播时只能手动复现才拿到那行 404)。
    """

    def test_safe_file_stem_handles_odd_names(self):
        self.assertEqual(monitor._safe_file_stem("主播甲"), "主播甲")
        for odd in ("a/b", "x y", "p|q", "", None, "带，全角标点"):
            stem = monitor._safe_file_stem(odd)
            self.assertNotIn("/", stem)
            self.assertTrue(stem, repr(odd))

    def test_err_tail_reads_last_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                         encoding="utf-8") as f:
            for i in range(20):
                f.write("line%d\n" % i)
            path = f.name
        try:
            tail = monitor._ffmpeg_err_tail(path, lines=3)
            self.assertIn("line19", tail)
            self.assertNotIn("line0 |", tail)
            self.assertEqual(len(tail.split(" | ")), 3)
        finally:
            os.remove(path)

    def test_err_tail_missing_is_empty(self):
        self.assertEqual(monitor._ffmpeg_err_tail("/nonexistent/x.log"), "")
        self.assertEqual(monitor._ffmpeg_err_tail(None), "")
        self.assertEqual(monitor._ffmpeg_err_tail(""), "")

    def test_err_path_under_logs_dir(self):
        p = monitor._ffmpeg_err_path("主播甲")
        self.assertTrue(p.endswith(".err.log"))
        # err 日志必须落在 <LOG_DIR>/ffmpeg/ 下。
        # 注意:测试进程的 LOG_DIR 是隔离临时目录(tests/__init__.py 会覆盖成
        # mkdtemp 随机名),所以断言与 monitor.LOG_DIR 对齐,而不是写死 "logs" 字串
        # —— 后者在「包模式」运行(mkdtemp 带随机后缀)时会误报。
        self.assertEqual(os.path.dirname(p), os.path.join(monitor.LOG_DIR, "ffmpeg"))

    def test_log_ffmpeg_exit_records_reason(self):
        lines = []
        orig = monitor.log
        monitor.log = lines.append
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                             encoding="utf-8") as f:
                f.write("Server returned 404 Not Found\n")
                path = f.name
            try:
                monitor._log_ffmpeg_exit("主播甲", {"err_path": path})
            finally:
                os.remove(path)
        finally:
            monitor.log = orig
        self.assertTrue(lines, "退出原因没进日志")
        self.assertIn("404", lines[0])
        self.assertIn("主播甲", lines[0])

    def test_log_ffmpeg_exit_silent_when_no_err_path(self):
        lines = []
        orig = monitor.log
        monitor.log = lines.append
        try:
            monitor._log_ffmpeg_exit("主播甲", {})
        finally:
            monitor.log = orig
        self.assertEqual(lines, [], "没有报错内容时不该打日志")


class TestErrLogRolling(unittest.TestCase):
    """err.log 从「每次覆盖」改成「追加 + 启动标记」(2026-10-31)。

    原先 `open(err_path, "w")` 让一场里连续崩 4 次只剩最后一次现场 —— 而崩溃
    归因最需要「同样几条报错重复了几次、中间有没有夹着别的错」。2026-10-29 的
    scale_vt 归因就卡在这里:证据是被我们自己删掉的。
    """

    MARK = monitor._ERR_START_MARK

    def test_tail_cuts_at_last_start_marker(self):
        """尾部只取最后一次启动之后 —— 秒退不产出的那一轮不能背上一轮的锅。"""
        with tempfile.NamedTemporaryFile("w", suffix=".err.log", delete=False,
                                         encoding="utf-8") as f:
            f.write(self.MARK + "2026-10-30 12:00:00 tier=1080\n")
            f.write("Server returned 404 Not Found\n")
            f.write(self.MARK + "2026-10-30 12:00:20 tier=1080\n")
            path = f.name
        try:
            self.assertEqual(monitor._ffmpeg_err_tail(path), "",
                             "把上一轮的 404 当成本次的归因了")
        finally:
            os.remove(path)

    def test_tail_reads_the_newest_round(self):
        with tempfile.NamedTemporaryFile("w", suffix=".err.log", delete=False,
                                         encoding="utf-8") as f:
            f.write(self.MARK + "t1\n")
            f.write("server returned 404 Not Found\n")
            f.write(self.MARK + "t2\n")
            f.write("Impossible to convert between the formats\n")
            path = f.name
        try:
            tail = monitor._ffmpeg_err_tail(path)
            self.assertIn("Impossible to convert", tail)
            self.assertNotIn("404", tail)
        finally:
            os.remove(path)

    def test_rotate_keeps_one_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.err.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x" * 500)
            orig = monitor._ERR_LOG_MAX
            monitor._ERR_LOG_MAX = 100
            try:
                monitor._rotate_err_log(p)
            finally:
                monitor._ERR_LOG_MAX = orig
            self.assertFalse(os.path.exists(p), "超限后应滚走,让新内容从空文件开始")
            self.assertTrue(os.path.exists(p + ".1"))

    def test_rotate_noop_below_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.err.log")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
            monitor._rotate_err_log(p)
            self.assertTrue(os.path.exists(p))
            self.assertFalse(os.path.exists(p + ".1"))

    def test_start_recording_appends_with_marker(self):
        """守卫:开机点必须**追加**写并打启动标记,否则尾部切分与现场留存全失效。"""
        import inspect
        src = inspect.getsource(monitor._start_recording)
        self.assertIn('open(err_path, "a"', src, "err.log 必须追加写,不能覆盖")
        self.assertIn("_ERR_START_MARK", src)
        self.assertNotIn('open(err_path, "w"', src)
