#!/usr/bin/env python3
"""主播卡「在播却没在录」提示的静态守卫测试。

背景（2026-09-19）：用户报「显示在播状态，但录制中断了/显示在直播但并没有录制」。
根因是**恢复路径断链**——`_give_up()` 会把主播从 `_recordings` 里摘掉，
而录制看护循环只遍历 `_recordings` ⇒ 冷却到期后没人重启录制，只能等下一次检测
（live_interval=1200s），实测空窗 28.5 分钟。

而界面上，这种状态只显示一句含糊的「检测到直播,录制已暂停或等待中…」，
用户完全看不出「是冷却保护、多久恢复」还是「真坏了」。这个文件锁住两件事：
  1. 暂停原因与剩余冷却时间必须真的从后端传到前端（字段名对得上）；
  2. 卡片必须把它们渲染出来，而不是只说「等待中」。

只做**确定**的检查（元素/字段/文案存在），不做语义判断。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(BASE, "static", "index.html")
MONITOR = os.path.join(BASE, "monitor.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestPauseStateFlowsToFrontend(unittest.TestCase):
    """字段链路：monitor._snap → /api/status.streamers[] → 前端渲染。"""

    def setUp(self):
        self.html = _read(HTML)
        self.monitor = _read(MONITOR)

    def test_backend_snap_exposes_pause_fields(self):
        for field in ("rec_paused_until", "rec_pause_reason"):
            self.assertIn('"%s"' % field, self.monitor,
                          "monitor._snap 没有输出 %s" % field)

    def test_frontend_consumes_pause_fields(self):
        for field in ("rec_paused_until", "rec_pause_reason"):
            self.assertIn(field, self.html, "前端没有消费 %s" % field)

    def test_card_explains_why_not_recording(self):
        """卡片必须说清原因，不能只说「等待中」。"""
        self.assertIn("录制暂时停止", self.html)
        self.assertIn("后自动重试", self.html)
        self.assertIn("这是项目的止损保护", self.html)
        # 冷却中时状态行也要跟着变，不能仍显示成正常的「● 直播中」
        self.assertIn("● 直播中 · 录制暂停", self.html)
        # 非冷却的「在播却没在录」也要有提示（巡检会自动救回）
        self.assertIn("判定在播但未在录制", self.html)

    def test_old_vague_copy_is_gone(self):
        """含糊的旧文案必须删掉，避免两套说法并存。

        只检查**实际会渲染的代码**——注释里为说明来龙去脉引用旧文案是允许的,
        所以先剥掉 `//` 行注释再断言。
        """
        code = re.sub(r"//[^\n]*", "", self.html)
        self.assertNotIn("检测到直播,录制已暂停或等待中", code)

    def test_pause_reason_is_escaped(self):
        """原因串来自后端日志文案，渲染时仍要走转义。"""
        i = self.html.index("rec_pause_reason ||")
        snippet = self.html[max(0, i - 40):i + 120]      # 往前多取一点,把 esc( 包进来
        self.assertIn("esc(", snippet, "暂停原因没转义")


class TestWatchdogCoversMissingRecordings(unittest.TestCase):
    """看护循环必须覆盖「判定在播却未录制」（后端侧的接线守卫）。"""

    def setUp(self):
        self.monitor = _read(MONITOR)

    def test_watchdog_calls_rescue(self):
        i = self.monitor.index("async def _recording_watchdog")
        j = self.monitor.index("def _rescue_missing_recordings")
        self.assertIn("_rescue_missing_recordings(state, cfg)",
                      self.monitor[i:j],
                      "看护循环没有调用抢救逻辑 —— 冷却到期后又没人重启录制了")

    def test_rescue_requests_refresh_not_direct_start(self):
        """必须走「提前复检」而不是拿旧地址硬开：旧地址多半已过期。"""
        i = self.monitor.index("def _rescue_missing_recordings")
        j = self.monitor.index("async def _preview_capture_loop", i)
        body = self.monitor[i:j]
        self.assertIn("state.request_refresh(name)", body)
        self.assertNotIn("_start_recording(", body)


if __name__ == "__main__":
    unittest.main()
