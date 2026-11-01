#!/usr/bin/env python3
"""补号巡检的接入层单测:循环编排 / 写回 config 的三重保护 / 失败审计。

做法:把 monitor 里所有会碰外部世界的名字换成 shim(浏览器、睡眠、状态文件、
写回函数),然后真跑 `rid_backfill_loop`。这样测的是**编排逻辑**(何时跑、跑几个、
什么情况写盘),而不是浏览器本身 —— 浏览器那层的真机验证由
`scripts/probe_web_rid.py` 负责。
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor
import web_rid_probe as P

NOW = 1792962471
SEC = "MS4wLjABAAAAsynthetic_fixture_000000000000000"
RID = "388770149407"


def cfg_with(pending=True, **section):
    m = {"name": "主播甲", "anchor": SEC}
    if not pending:
        m["web_rid"] = RID
    cfg = {"monitors": [m]}
    if section:
        cfg["rid_backfill"] = section
    return cfg


class _AsyncioShim:
    """把 asyncio 的三样东西替掉:sleep 记时长并限次,to_thread 直接同步跑。"""
    CancelledError = asyncio.CancelledError

    def __init__(self, max_sleeps=8):
        self.sleeps = []
        self.max_sleeps = max_sleeps

    async def sleep(self, sec):
        self.sleeps.append(sec)
        if len(self.sleeps) > self.max_sleeps:
            raise asyncio.CancelledError()
        return None

    async def to_thread(self, fn, *a, **kw):
        return fn(*a, **kw)


class _BrowserShim:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def fetch_web_rid(self, sec_uid, **kw):
        self.calls.append((sec_uid, kw))
        if self.results:
            return self.results.pop(0)
        return {"verdict": "unknown", "rid": None, "reason": "timeout",
                "detail": "no canned result", "elapsed_ms": 10}


class _NotifyShim:
    """替掉真实告警通道:测试里绝不能真往企业微信发消息。"""
    def __init__(self):
        self.sent = []

    async def push_by_config(self, cfg, title, text, **kw):
        self.sent.append((title, text))
        return {"ok": True, "channels": ["shim"]}


class _StateShim:
    def __init__(self, cfg):
        self.cfg = cfg
        self.refreshed = []

    def get_config(self):
        return self.cfg

    def get_status(self):
        return {"recordings": []}

    def request_refresh(self, name):
        self.refreshed.append(name)


def live(rid=RID):
    return {"verdict": "live", "rid": rid, "reason": None,
            "detail": "bytes=900000 xhr=200 room=0 http=200", "elapsed_ms": 1300}


def offline():
    return {"verdict": "offline", "rid": None, "reason": None,
            "detail": "bytes=880000 xhr=200 room=0 http=200", "elapsed_ms": 4000}


def unknown(reason="xhr_blocked"):
    return {"verdict": "unknown", "rid": None, "reason": reason,
            "detail": "profile XHR status=412", "elapsed_ms": 2000}


class LoopHarness:
    """跑一次 rid_backfill_loop,收集所有外部副作用。

    `max_sleeps` 决定跑几"段":循环开头固定有一个 sleep(20) 启动宽限,所以
    max_sleeps=1 → 只跑一轮就靠 CancelledError 退出(不多跑、不重复拉结果)。
    """

    def __init__(self, cfg, results, max_sleeps=1, initial_state=None):
        self.state = _StateShim(cfg)
        self.browser = _BrowserShim(results)
        self.notify = _NotifyShim()
        self.aio = _AsyncioShim(max_sleeps)
        self.rid_state = P.merge_state(initial_state or {}, NOW)
        self.saves = []
        self.applied = []
        self.audits = []
        self.refreshed = self.state.refreshed
        self._orig = {}

    def _patch(self):
        self._orig = {
            "asyncio": monitor.asyncio,
            "rid_browser": monitor.rid_browser,
            "notify_mod": monitor.notify_mod,
            "_rid_state_load": monitor._rid_state_load,
            "_rid_state_save": monitor._rid_state_save,
            "_rid_apply_web_rid": monitor._rid_apply_web_rid,
            "_rid_fail_audit": monitor._rid_fail_audit,
            "clock": time.time,
        }
        # **时钟必须和状态一起注入。** 循环内的跨日滚零读 `time.time()`
        # (`now = int(time.time())` → `day_str`),而状态里的 `day` 来自固定
        # `NOW`;只注入状态不注入时钟,只要真实日期走过 NOW 那一天,
        # `checks_today` 就会被归零、`daily_cap` 拦不住,`test_daily_cap_blocks`
        # 必然报红 —— 2026-10-27 实际踩到过(前一天的用例全绿,过了零点就红)。
        time.time = lambda: float(NOW)
        monitor.asyncio = self.aio
        monitor.rid_browser = self.browser
        monitor.notify_mod = self.notify
        monitor._rid_state_load = lambda: self.rid_state
        monitor._rid_state_save = lambda st: self.saves.append(json.loads(json.dumps(st)))
        monitor._rid_apply_web_rid = self._fake_apply
        monitor._rid_fail_audit = lambda e, cap=500: self.audits.append(e)

    def _restore(self):
        monitor.asyncio = self._orig["asyncio"]
        monitor.rid_browser = self._orig["rid_browser"]
        monitor.notify_mod = self._orig["notify_mod"]
        monitor._rid_state_load = self._orig["_rid_state_load"]
        monitor._rid_state_save = self._orig["_rid_state_save"]
        monitor._rid_apply_web_rid = self._orig["_rid_apply_web_rid"]
        monitor._rid_fail_audit = self._orig["_rid_fail_audit"]
        time.time = self._orig["clock"]

    def _fake_apply(self, name, anchor, rid, log=None):
        self.applied.append((name, rid))
        return "ok", None

    def run(self, rounds=1):
        self._patch()
        try:
            async def _go():
                # 每轮跑到 sleep 完才返回,靠 _AsyncioShim 的限次退出
                try:
                    await monitor.rid_backfill_loop(self.state, log=lambda *a: None)
                except asyncio.CancelledError:
                    pass
            asyncio.run(_go())
        finally:
            self._restore()
        return self


class TestLoopOrchestration(unittest.TestCase):
    def test_no_pending_means_zero_browser(self):
        """没有缺号主播 → 一次浏览器都不许开(用户明确要求默认关闭)。"""
        h = LoopHarness(cfg_with(pending=False), []).run()
        self.assertEqual(h.browser.calls, [])
        self.assertEqual(h.saves[-1]["window_until"], 0)
        self.assertEqual(h.saves[-1]["last_reason"], "no_pending")

    def test_disabled_means_zero_browser(self):
        h = LoopHarness(cfg_with(pending=True, enabled="off"), []).run()
        self.assertEqual(h.browser.calls, [])
        self.assertEqual(h.saves[-1]["last_reason"], "disabled")

    def test_live_fills_rid_and_refreshes(self):
        h = LoopHarness(cfg_with(), [live()]).run()
        self.assertEqual(h.applied, [("主播甲", RID)])
        self.assertEqual(h.refreshed, ["主播甲"])
        self.assertEqual(h.saves[-1]["stats"]["filled_total"], 1)
        self.assertEqual(h.saves[-1]["stats"]["last_filled_name"], "主播甲")
        self.assertEqual(h.audits, [])
        self.assertEqual(len(h.notify.sent), 1)          # 默认 notify_on_fill=true

    def test_notify_can_be_turned_off(self):
        """关掉 notify_on_fill 就必须一条都不发(否则用户关了还在收推送)。"""
        h = LoopHarness(cfg_with(notify_on_fill=False), [live()]).run()
        self.assertEqual(h.applied, [("主播甲", RID)])
        self.assertEqual(h.notify.sent, [])

    def test_offline_is_not_a_failure(self):
        """没开播反复查到:不写盘、不记失败、不进审计。"""
        h = LoopHarness(cfg_with(), [offline()]).run()
        self.assertEqual(h.applied, [])
        self.assertEqual(h.audits, [])
        self.assertEqual(h.rid_state["per_monitor"]["主播甲"]["fails"], 0)

    def test_offline_repeated_never_trips_circuit(self):
        """连跑多轮 offline:一次都不能熔断(把"没开播"当失败会让巡检自我报废)。"""
        st = P.empty_state(NOW)
        st["window_until"] = NOW + 86400
        st["per_monitor"]["主播甲"] = {"fails": 2}      # 已经攒到 2 次
        h = LoopHarness(cfg_with(), [offline()], initial_state=st).run()
        self.assertEqual(h.rid_state["per_monitor"]["主播甲"]["fails"], 0)
        self.assertFalse(h.rid_state["circuit"].get("global_until", 0) > NOW)

    def test_unknown_writes_audit_with_reason(self):
        h = LoopHarness(cfg_with(), [unknown("xhr_blocked")]).run()
        self.assertEqual(len(h.audits), 1)
        self.assertEqual(h.audits[0]["reason"], "xhr_blocked")
        self.assertEqual(h.audits[0]["name"], "主播甲")
        self.assertIn("ts", h.audits[0])
        self.assertEqual(h.saves[-1]["stats"]["reasons"]["xhr_blocked"], 1)

    def test_risk_reason_triggers_cooling_then_stops(self):
        """风控类原因 → 全局冷却;下一轮直接跳过,不再开浏览器。"""
        h = LoopHarness(cfg_with(), [unknown("captcha")], max_sleeps=2).run()
        self.assertEqual(len(h.browser.calls), 1)
        self.assertGreater(h.rid_state["circuit"]["global_until"], NOW)
        self.assertEqual(h.saves[-1]["last_reason"], "cooling")

    def test_daily_cap_blocks(self):
        st = P.empty_state(NOW)
        st["window_until"] = NOW + 86400
        st["day"] = P.day_str(NOW)
        st["checks_today"] = 100
        h = LoopHarness(cfg_with(), [live()], initial_state=st).run()
        self.assertEqual(h.browser.calls, [])
        self.assertEqual(h.saves[-1]["last_reason"], "daily_cap")

    def test_next_run_uses_tier_interval(self):
        """下一轮时间必须落在某个档位区间内(三档随机的端到端体现)。

        上界要算上 second_jitter:档内随机抽到 120 分钟后,还会再加 0–59 秒
        (`web_rid_probe.next_interval`),所以真实上界是 120+59/60 ≈ 120.98 分钟。
        原先写死 ≤120.5,会以约 1% 的概率随机变红(2026-10-29 全量跑时挂过一次,
        单独重跑 5 次全过 —— 这类「时红时绿」最消耗人的信任)。
        """
        h = LoopHarness(cfg_with(), [offline()]).run()
        st = h.saves[-1]
        mins = (st["next_run_at"] - st["last_run_at"]) / 60.0
        self.assertGreaterEqual(mins, 40, mins)
        self.assertLess(mins, 121, mins)

    def test_loop_survives_fetcher_exception(self):
        class _Boom(_BrowserShim):
            def fetch_web_rid(self, sec_uid, **kw):
                raise RuntimeError("chromium 崩了")

        h = LoopHarness(cfg_with(), [])
        h.browser = _Boom([])
        h.run()
        self.assertEqual(len(h.browser.calls), 0)     # 走的是异常分支
        self.assertEqual(h.audits[0]["reason"], "browser_crash")


class TestApplyWebRid(unittest.TestCase):
    """写回 config 的三重保护:只改字段 / 已有值不覆盖 / mtime 冲突放弃。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")
        self._orig = monitor.CONFIG_PATH
        monitor.CONFIG_PATH = self.path
        self.cfg = {"monitors": [{"name": "主播甲", "anchor": SEC, "note": "保留我"}],
                    "webui": {"port": 8780}}
        self._write(self.cfg)

    def tearDown(self):
        monitor.CONFIG_PATH = self._orig

    def _write(self, cfg):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_only_three_fields_added(self):
        code, _ = monitor._rid_apply_web_rid("主播甲", SEC, RID, log=lambda *a: None)
        self.assertEqual(code, "ok")
        got = self._read()
        m = got["monitors"][0]
        self.assertEqual(m["web_rid"], RID)
        self.assertEqual(m["web_rid_source"], "auto")
        self.assertIn("web_rid_filled_at", m)
        self.assertEqual(m["note"], "保留我")          # 其它字段逐字不动
        self.assertEqual(got["webui"], {"port": 8780})

    def test_existing_rid_not_overwritten(self):
        cfg = self._read()
        cfg["monitors"][0]["web_rid"] = "111111111111"
        self._write(cfg)
        code, _ = monitor._rid_apply_web_rid("主播甲", SEC, RID, log=lambda *a: None)
        self.assertEqual(code, "already_has_rid")
        self.assertEqual(self._read()["monitors"][0]["web_rid"], "111111111111")

    def test_unknown_streamer_gives_up(self):
        code, _ = monitor._rid_apply_web_rid("主播乙", SEC, RID, log=lambda *a: None)
        self.assertEqual(code, "not_found")
        self.assertNotIn("web_rid", self._read()["monitors"][0])

    def test_mtime_conflict_aborts(self):
        """合并期间文件被改(前端保存 config)→ 必须放弃本轮,绝不覆盖。

        用真实的竞态模拟:在 merge 返回后立刻把文件改成"前端刚保存过"的样子,
        于是写前复查必然发现 mtime/size 变了。两次尝试都冲突 → 返回 conflict,
        且落盘内容仍是前端那份(我们一个字都没写)。
        """
        orig_merge = monitor.rid_probe.merge_web_rid

        def racing_merge(cfg_now, name, anchor, rid, now):
            out = orig_merge(cfg_now, name, anchor, rid, now)
            raced = {"monitors": [{"name": "主播甲", "anchor": SEC,
                                   "note": "前端刚改的备注,别覆盖我"}]}
            self._write(raced)
            return out

        monitor.rid_probe.merge_web_rid = racing_merge
        try:
            code, _ = monitor._rid_apply_web_rid("主播甲", SEC, RID, log=lambda *a: None)
        finally:
            monitor.rid_probe.merge_web_rid = orig_merge
        self.assertEqual(code, "conflict")
        m = self._read()["monitors"][0]
        self.assertNotIn("web_rid", m)
        self.assertEqual(m["note"], "前端刚改的备注,别覆盖我")


if __name__ == "__main__":
    unittest.main()
