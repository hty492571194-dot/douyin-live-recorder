# -*- coding: utf-8 -*-
"""画质估算口径的守卫:源尺寸代表谁、日志按哪一档说话。

三个坑都来自同一次事故(2026-10-27),它们不会让录制失败,只会让**判断**
失真,而失真的判断比报错更难查:

  ① `_src_hint` 用「众数」代表所有在录路 → 一路横屏小画面(1040×480)就能
     把整体收益估算拉成 0,而两路竖屏 1080×1920 实际省了 55.6% 的引擎;
  ② `_learn_src_size` 从**编码后**的分片学「源尺寸」→ 降档期间录出的
     720×1280 被当成源,`gains()` 于是永远算出「降到 720 没收益」;
  ③ `_quality_loop` 打印档位参数时用的是 observe **之前**的 plan →
     日志出现「档位 2 生效(crash) → 720P / 700 kbps」,而 L2 是 500 kbps,
     排查时方向直接读反。

守 `_src_hint` / `_learn_src_size` / `_tier_view` / `_streams_detail` 四处的口径。

**破坏性验证**:把 `_src_hint` 的 `max(..., key=像素)` 换回 `max(set(sizes),
key=sizes.count)`,`test_picks_largest_pixel_stream` 必须报红;把 `_tier_view`
调用处换回 `plan["active_tier"]`,`test_sees_new_level` 那类断言在日志里就看不出
—— 那一处只能靠 `_tier_view` 本身的单测钉住。
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor  # noqa: E402


def cfg(**kw):
    rec = {"resolution": 1080, "bitrate": 1200, "video_mode": "vt",
           "auto_degrade": True, "min_gain": 10,
           "degrade_l1_resolution": 720, "degrade_l1_bitrate": 700,
           "degrade_l2_resolution": 720, "degrade_l2_bitrate": 500}
    rec.update(kw)
    return {"recorder": rec}


class _StateIsolated(unittest.TestCase):
    """这两个 dict 是模块级全局,用例之间必须互不污染。"""

    def setUp(self):
        self._size0 = dict(monitor._src_size)
        self._rec0 = dict(monitor._recordings)
        monitor._src_size.clear()
        monitor._recordings.clear()

    def tearDown(self):
        monitor._src_size.clear()
        monitor._src_size.update(self._size0)
        monitor._recordings.clear()
        monitor._recordings.update(self._rec0)

    def _streams(self, *names, **sizes):
        for n in names:
            monitor._recordings[n] = {"resolution": 720, "quality_level": 1}
        for n, wh in sizes.items():
            monitor._src_size[n] = wh


class TestSrcHint(_StateIsolated):
    def test_picks_largest_pixel_stream(self):
        """一路横屏小画面不许代表整体 —— 收益估算按像素最多的那路走。"""
        self._streams("甲", "乙", "丙",
                      甲=(1040, 480), 乙=(1080, 1920), 丙=(1040, 480))
        self.assertEqual(monitor._src_hint(), (1080, 1920))

    def test_no_source_learned_yields_none(self):
        monitor._recordings["甲"] = {"resolution": 720, "quality_level": 1}
        self.assertEqual(monitor._src_hint(), (None, None))

    def test_ignores_streamers_not_recording(self):
        """学到的尺寸属于已经停录的主播时,不能拿来代表当前在录路。"""
        monitor._src_size["已停播"] = (1080, 1920)
        monitor._recordings["在录"] = {"resolution": 720, "quality_level": 1}
        monitor._src_size["在录"] = (1040, 480)
        self.assertEqual(monitor._src_hint(), (1040, 480))


class TestLearnSrcSize(_StateIsolated):
    def test_skips_scaled_segment(self):
        """降档期间录出来的分片尺寸不是源尺寸,学了会让收益估算归零。"""
        self.assertIsNone(monitor._learn_src_size("甲", "/tmp/nope.flv", 1))
        self.assertNotIn("甲", monitor._src_size)

    def test_l2_also_skipped(self):
        self.assertIsNone(monitor._learn_src_size("甲", "/tmp/nope.flv", 2))
        self.assertNotIn("甲", monitor._src_size)

    def test_native_level_does_not_guess(self):
        """L0 可以学,但文件读不到时也不许凭猜测记一个值。"""
        self.assertIsNone(monitor._learn_src_size("甲", "/nonexistent/a.flv", 0))
        self.assertNotIn("甲", monitor._src_size)


class TestTierView(unittest.TestCase):
    """`_tier_view` 是「档位 → 参数」的唯一出口(日志/接口/set_active 共用)。"""

    def test_level_maps_to_its_own_params(self):
        self.assertEqual(monitor._tier_view(cfg(), 1)["bitrate"], 700)
        self.assertEqual(monitor._tier_view(cfg(), 2)["bitrate"], 500)
        self.assertEqual(monitor._tier_view(cfg(), 2)["label"], "L2")
        self.assertEqual(monitor._tier_view(cfg(), 1)["label"], "L1")

    def test_level_zero_follows_user_config(self):
        v = monitor._tier_view(cfg(), 0)
        self.assertEqual((v["resolution"], v["bitrate"]), (1080, 1200))
        self.assertEqual(v["label"], "L0")

    def test_output_size_follows_tier(self):
        """720 档在源未知时按标准 9:16 换算,不能原样透传。"""
        v = monitor._tier_view(cfg(), 1)
        self.assertEqual((v["w"], v["h"]), (720, 1280))


class TestStreamsDetail(_StateIsolated):
    def test_scaled_is_tri_state(self):
        """True=真缩了 / False=源本就不大于档位 / None=源未知(不许写成 False)。"""
        self._streams("横屏", "竖屏", 横屏=(1040, 480), 竖屏=(1080, 1920))
        monitor._recordings["未学到"] = {"resolution": 720, "quality_level": 1}
        rows = {r["name"]: r for r in monitor._streams_detail(cfg())}
        self.assertFalse(rows["横屏"]["scaled"], "源短边 480 ≤ 720,这一路只降码率")
        self.assertTrue(rows["竖屏"]["scaled"], "1080×1920 → 720×1280 是真缩")
        self.assertIsNone(rows["未学到"]["scaled"], "源未知不许写成「没缩」")

    def test_reports_src_and_out(self):
        self._streams("竖屏", 竖屏=(1080, 1920))
        row = monitor._streams_detail(cfg())[0]
        self.assertEqual(row["src"], [1080, 1920])
        self.assertEqual(row["out"], [720, 1280])


if __name__ == "__main__":
    unittest.main()
