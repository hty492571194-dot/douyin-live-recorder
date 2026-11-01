#!/usr/bin/env python3
"""web_rid_probe 纯逻辑层单测:窗口 / 两段随机 / 三态判定 / 写回合并 / 三层熔断。

全部离线、不 import playwright。夹具一律合成值(主播甲 / 388770149407 / MS4w…fixture),
守 TestRealRepoIsClean 那条敏感词门。
"""
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_rid_probe as P

NOW = 1792962471          # 2026-10-26 05:07:51 +08
SEC = "MS4wLjABAAAAsynthetic_fixture_000000000000000"
RID = "388770149407"


def cfg_with(*mons, **section):
    cfg = {"monitors": list(mons)}
    if section:
        cfg["rid_backfill"] = section
    return cfg


def mon(name="主播甲", web_rid="", anchor=SEC, **kw):
    m = {"name": name, "anchor": anchor}
    if web_rid:
        m["web_rid"] = web_rid
    m.update(kw)
    return m


class TestNormalizeCfg(unittest.TestCase):
    def test_defaults(self):
        n = P.normalize_cfg({})
        self.assertEqual(n["interval_tiers_min"], [[40, 50], [80, 90], [110, 120]])
        self.assertEqual(n["tier_weights"], [0.5, 0.3, 0.2])
        self.assertEqual(n["max_checks_per_day"], 100)
        self.assertEqual(n["max_per_round"], 3)
        self.assertFalse(n["_normalized"])

    def test_inverted_range_falls_back(self):
        n = P.normalize_cfg({"interval_tiers_min": [[50, 40]], "tier_weights": [1]})
        self.assertTrue(n["_normalized"])
        self.assertEqual(n["_reason"], "config_invalid")
        self.assertEqual(n["interval_tiers_min"], [[40, 50], [80, 90], [110, 120]])

    def test_overlapping_tiers_fall_back(self):
        """档间重叠会让同一分钟被两档命中,必须判非法。边界相接也算重叠。"""
        for bad in ([[40, 90], [80, 100]], [[40, 50], [80, 90], [90, 100]]):
            n = P.normalize_cfg({"interval_tiers_min": bad, "tier_weights": [1, 1, 1]})
            self.assertTrue(n["_normalized"], bad)

    def test_weights_normalized(self):
        n = P.normalize_cfg({"tier_weights": [5, 3, 2]})
        self.assertAlmostEqual(sum(n["tier_weights"]), 1.0)
        self.assertAlmostEqual(n["tier_weights"][0], 0.5)

    def test_zero_weights_fall_back(self):
        n = P.normalize_cfg({"tier_weights": [0, 0, 0]})
        self.assertTrue(n["_normalized"])

    def test_weight_count_mismatch_keeps_defaults(self):
        """权重个数对不上档位数 → 回落默认权重并标记非法(不猜用户想要什么)。"""
        n = P.normalize_cfg({"tier_weights": [0.9]})
        self.assertTrue(n["_normalized"])
        self.assertEqual(n["tier_weights"], [0.5, 0.3, 0.2])

    def test_custom_tier_count_uses_equal_weights(self):
        """自定义成两档但没给权重 → 等权(1/2 各一),不沿用三档权重。"""
        n = P.normalize_cfg({"interval_tiers_min": [[30, 40], [90, 120]]})
        self.assertEqual(len(n["tier_weights"]), 2)
        for w in n["tier_weights"]:
            self.assertAlmostEqual(w, 0.5)

    def test_gap_range_sane(self):
        n = P.normalize_cfg({"gap_sec_min": 60, "gap_sec_max": 10})
        self.assertEqual(n["gap_sec_max"], 60)

    def test_enabled_string_forms(self):
        """前端 select 存的是字符串,必须折成布尔 —— 否则「强制开启」静默失灵。"""
        for raw, want in (("auto", "auto"), ("on", True), ("true", True),
                          ("off", False), ("false", False), (True, True),
                          (False, False), (None, "auto")):
            self.assertEqual(P.normalize_cfg({"enabled": raw})["enabled"], want, raw)

    def test_notify_flag_is_carried_through(self):
        """notify_on_fill 必须原样透传 —— 漏了它,用户关掉通知也照样会收到推送。"""
        self.assertTrue(P.normalize_cfg({})["notify_on_fill"])
        self.assertFalse(P.normalize_cfg({"notify_on_fill": False})["notify_on_fill"])
        self.assertFalse(P.normalize_cfg({"notify_on_fill": "off"})["notify_on_fill"])


class TestPendingAndWindow(unittest.TestCase):
    def test_pending(self):
        cfg = cfg_with(mon("主播甲"), mon("主播乙", web_rid=RID),
                       {"name": "主播丙", "anchor": SEC, "web_rid": "   "})
        names = [m["name"] for m in P.pending_monitors(cfg)]
        self.assertEqual(names, ["主播甲", "主播丙"])

    def test_pending_handles_garbage(self):
        self.assertEqual(P.pending_monitors({}), [])
        self.assertEqual(P.pending_monitors({"monitors": [None, "x"]}), [])

    def test_window_opens_seven_days(self):
        st = P.empty_state(NOW)
        ok, opened, expired = P.ensure_window(P.normalize_cfg({}), st, [mon()], NOW)
        self.assertTrue(ok)
        self.assertTrue(opened)
        self.assertFalse(expired)
        self.assertEqual(st["window_until"] - st["window_start"], 7 * 86400)

    def test_window_closes_when_nothing_pending(self):
        st = P.empty_state(NOW)
        st["window_until"] = NOW + 86400
        ok, opened, expired = P.ensure_window(P.normalize_cfg({}), st, [], NOW)
        self.assertFalse(ok)
        self.assertEqual(st["window_until"], 0)

    def test_window_expired_does_not_auto_renew(self):
        """7 天到期且名单没变 → 必须停在 expired,等人续或新主播入场。"""
        st = P.empty_state(NOW)
        P.ensure_window(P.normalize_cfg({}), st, [mon()], NOW)
        later = st["window_until"] + 10
        ok, opened, expired = P.ensure_window(P.normalize_cfg({}), st, [mon()], later)
        self.assertFalse(ok)
        self.assertFalse(opened)
        self.assertTrue(expired)

    def test_window_reopens_when_new_streamer_added(self):
        st = P.empty_state(NOW)
        P.ensure_window(P.normalize_cfg({}), st, [mon("主播甲")], NOW)
        later = st["window_until"] + 10
        ok, opened, _ = P.ensure_window(P.normalize_cfg({}), st,
                                        [mon("主播甲"), mon("主播乙")], later)
        self.assertTrue(ok)
        self.assertTrue(opened)
        self.assertEqual(st["window_start"], later)

    def test_enabled_true_ignores_window(self):
        st = P.empty_state(NOW)
        st["window_until"] = NOW - 1
        st["pending_key"] = P.pending_key([mon()])
        norm = P.normalize_cfg({"enabled": True})
        ok, opened, _ = P.ensure_window(norm, st, [mon()], NOW)
        self.assertTrue(ok)
        self.assertTrue(opened)


class TestShouldRun(unittest.TestCase):
    def setUp(self):
        self.norm = P.normalize_cfg({})
        self.st = P.empty_state(NOW)
        self.pending = [mon()]

    def test_ok(self):
        ok, why = P.should_run(self.norm, self.st, self.pending, NOW)
        self.assertTrue(ok)
        self.assertEqual(why, "ok")

    def test_disabled(self):
        ok, why = P.should_run(P.normalize_cfg({"enabled": False}), self.st,
                               self.pending, NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "disabled")

    def test_no_pending(self):
        self.st["window_until"] = NOW + 86400
        ok, why = P.should_run(self.norm, self.st, [], NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "no_pending")

    def test_cooling(self):
        P.ensure_window(self.norm, self.st, self.pending, NOW)
        self.st["circuit"]["global_until"] = NOW + 600
        ok, why = P.should_run(self.norm, self.st, self.pending, NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "cooling")

    def test_daily_cap(self):
        P.ensure_window(self.norm, self.st, self.pending, NOW)
        self.st["checks_today"] = 100
        ok, why = P.should_run(self.norm, self.st, self.pending, NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "daily_cap")

    def test_all_in_backoff(self):
        P.ensure_window(self.norm, self.st, self.pending, NOW)
        P._monitor_state(self.st, "主播甲")["backoff_until"] = NOW + 600
        ok, why = P.should_run(self.norm, self.st, self.pending, NOW)
        self.assertFalse(ok)
        self.assertEqual(why, "backoff")

    def test_should_run_does_not_advance_cursor(self):
        """should_run 只读:推进 cursor 的必须是 task_batch,否则每轮被推两次。"""
        P.ensure_window(self.norm, self.st, self.pending, NOW)
        P.should_run(self.norm, self.st, self.pending, NOW)
        self.assertEqual(self.st["cursor"], 0)


class TestPlanNextRun(unittest.TestCase):
    def setUp(self):
        self.norm = P.normalize_cfg({})

    def test_interval_within_tier_and_next_run(self):
        st = P.empty_state(NOW)
        rng = random.Random(11)
        for _ in range(200):
            r = P.plan_next_run(self.norm, st, NOW, rng)
            lo, hi = self.norm["interval_tiers_min"][r["tier"]]
            self.assertGreaterEqual(r["minutes"], lo)
            self.assertLessEqual(r["minutes"], hi)
            self.assertGreaterEqual(r["interval_sec"], r["minutes"] * 60)
            self.assertLessEqual(r["interval_sec"], r["minutes"] * 60 + 59)
            self.assertEqual(r["next_run_at"], NOW + r["interval_sec"])

    def test_never_lands_on_whole_minute(self):
        """秒抖动必须生效:否则 next_run_at 总是 mm:00,是最明显的机器特征。"""
        st = P.empty_state(NOW)
        rng = random.Random(3)
        secs = {P.plan_next_run(self.norm, st, NOW, rng)["interval_sec"] % 60
                for _ in range(100)}
        self.assertGreater(len(secs), 30)

    def test_no_consecutive_same_tier(self):
        """默认 avoid_same_tier_twice:连续两轮绝不落同一档。"""
        st = P.empty_state(NOW)
        rng = random.Random(5)
        prev = None
        for _ in range(300):
            r = P.plan_next_run(self.norm, st, NOW, rng)
            if prev is not None:
                self.assertNotEqual(r["tier"], prev)
            prev = r["tier"]

    def test_no_identical_consecutive_interval(self):
        st = P.empty_state(NOW)
        rng = random.Random(9)
        prev = None
        for _ in range(300):
            r = P.plan_next_run(self.norm, st, NOW, rng)
            self.assertNotEqual(r["interval_sec"], prev)
            prev = r["interval_sec"]

    def test_all_tiers_reachable(self):
        st = P.empty_state(NOW)
        rng = random.Random(13)
        seen = {P.plan_next_run(self.norm, st, NOW, rng)["tier"] for _ in range(300)}
        self.assertEqual(seen, {0, 1, 2})

    def test_invalid_cfg_still_plans(self):
        st = P.empty_state(NOW)
        r = P.plan_next_run(P.normalize_cfg({"interval_tiers_min": [[9, 3]]}), st, NOW)
        self.assertGreater(r["interval_sec"], 0)


class TestDecideVerdict(unittest.TestCase):
    def test_live_from_dom(self):
        v, rid, reason = P.decide_verdict({"dom_rids": [RID], "profile_ok": True})
        self.assertEqual((v, rid, reason), ("live", RID, None))

    def test_offline_needs_profile_ok(self):
        """接口正常 + room_id_str="0" = 确认没开播(算成功,不是失败)。"""
        v, rid, reason = P.decide_verdict({"profile_ok": True,
                                           "profile_room_id_str": "0",
                                           "page_bytes": 900000})
        self.assertEqual((v, rid, reason), ("offline", None, None))

    def test_profile_says_live_but_no_entry(self):
        """接口说在播、DOM 却没有入口链接 → 看不清,下一轮再说(不能当 offline)。"""
        v, _, reason = P.decide_verdict({"profile_ok": True,
                                         "profile_room_id_str": "7700706410765568809",
                                         "page_bytes": 900000})
        self.assertEqual((v, reason), ("unknown", "unknown"))

    def test_reason_codes(self):
        cases = [
            ({"nav_error": "net::ERR_CONNECTION_RESET"}, "nav_error"),
            ({"see_text": "请完成下列验证后继续"}, "captcha"),
            ({"http_status": 429}, "http_429"),
            ({"http_status": 403}, "http_403"),
            ({"profile_ok": False, "xhr_status": 412, "page_bytes": 900000}, "xhr_blocked"),
            ({"profile_ok": False, "see_text": "登录后查看", "page_bytes": 900000}, "login_wall"),
            ({"profile_ok": False, "page_bytes": 1000}, "empty_shell"),
            ({"profile_ok": False, "page_bytes": 900000}, "unknown"),
        ]
        for sig, want in cases:
            v, rid, reason = P.decide_verdict(sig)
            self.assertEqual(v, "unknown", sig)
            self.assertEqual(reason, want, sig)
            self.assertIsNone(rid, sig)

    def test_tolerates_empty_signals(self):
        v, rid, reason = P.decide_verdict({})
        self.assertEqual(v, "unknown")


class TestMergeWebRid(unittest.TestCase):
    def test_only_three_fields_change(self):
        cfg = {"monitors": [mon("主播甲", note="备注", auto_record=True)],
               "webui": {"port": 8780}}
        before = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
        out, code = P.merge_web_rid(cfg, "主播甲", SEC, RID, NOW)
        self.assertEqual(code, "ok")
        m = out["monitors"][0]
        self.assertEqual(m["web_rid"], RID)
        self.assertEqual(m["web_rid_source"], "auto")
        self.assertEqual(m["web_rid_filled_at"], NOW)
        m.pop("web_rid"); m.pop("web_rid_source"); m.pop("web_rid_filled_at")
        self.assertEqual(json.dumps(cfg, ensure_ascii=False, sort_keys=True), before)

    def test_already_has_rid_is_untouched(self):
        cfg = cfg_with(mon("主播甲", web_rid="111111111111"))
        out, code = P.merge_web_rid(cfg, "主播甲", SEC, RID, NOW)
        self.assertIsNone(out)
        self.assertEqual(code, "already_has_rid")
        self.assertEqual(cfg["monitors"][0]["web_rid"], "111111111111")

    def test_name_and_anchor_must_match(self):
        cfg = cfg_with(mon("主播甲", anchor=SEC))
        self.assertEqual(P.merge_web_rid(cfg, "主播甲", SEC + "x", RID, NOW)[1], "not_found")
        self.assertEqual(P.merge_web_rid(cfg, "主播乙", SEC, RID, NOW)[1], "not_found")

    def test_bad_rid_rejected(self):
        cfg = cfg_with(mon())
        self.assertEqual(P.merge_web_rid(cfg, "主播甲", SEC, "abc", NOW)[1], "bad_rid")
        self.assertNotIn("web_rid", cfg["monitors"][0])


class TestCircuit(unittest.TestCase):
    def setUp(self):
        self.c = P.Circuit()
        self.st = P.empty_state(NOW)

    def test_offline_never_trips(self):
        """最关键的一条:没开播反复查到,绝不能熔断(否则巡检三次就自我报废)。"""
        for i in range(10):
            self.c.record(self.st, "主播甲", "offline", None, NOW + i * 3600)
            self.c.record_round(self.st, 0, 1, NOW + i * 3600)
        st = P._monitor_state(self.st, "主播甲")
        self.assertEqual(st["fails"], 0)
        self.assertIsNone(self.c.blocked(self.st, "主播甲", NOW + 10 * 3600))
        self.assertIsNone(self.c.global_blocked(self.st, NOW + 10 * 3600))

    def test_single_backoff_after_three_unknowns(self):
        for i in range(3):
            self.c.record(self.st, "主播甲", "unknown", "timeout", NOW + i * 60)
        until = self.c.blocked(self.st, "主播甲", NOW + 200)
        self.assertIsNotNone(until)
        self.assertEqual(until, NOW + 120 + P.SINGLE_HOLD_SEC)

    def test_success_resets_fails(self):
        self.c.record(self.st, "主播甲", "unknown", "timeout", NOW)
        self.c.record(self.st, "主播甲", "unknown", "timeout", NOW + 1)
        self.c.record(self.st, "主播甲", "offline", None, NOW + 2)
        self.assertEqual(P._monitor_state(self.st, "主播甲")["fails"], 0)
        self.c.record(self.st, "主播甲", "unknown", "timeout", NOW + 3)
        self.assertEqual(P._monitor_state(self.st, "主播甲")["fails"], 1)

    def test_risk_reason_triggers_global_cooldown_escalation(self):
        self.c.record(self.st, "主播甲", "unknown", "xhr_blocked", NOW)
        first = self.c.global_blocked(self.st, NOW + 1)
        self.assertEqual(first, NOW + P.GLOBAL_HOLD_SEC)
        later = NOW + P.GLOBAL_HOLD_SEC + 1
        self.c.record(self.st, "主播甲", "unknown", "captcha", later)
        self.assertEqual(self.c.global_blocked(self.st, later + 1),
                         later + P.GLOBAL_HOLD2_SEC)

    def test_network_reason_does_not_cool_globally(self):
        self.c.record(self.st, "主播甲", "unknown", "timeout", NOW)
        self.c.record(self.st, "主播乙", "unknown", "nav_error", NOW)
        self.assertIsNone(self.c.global_blocked(self.st, NOW + 1))

    def test_rid_mismatch_no_backoff_but_blacklists(self):
        for i in range(4):
            self.c.record(self.st, "主播甲", "unknown", "rid_mismatch", NOW + i, rid=RID)
        st = P._monitor_state(self.st, "主播甲")
        self.assertEqual(st["backoff_until"], 0)
        self.assertEqual(st["rid_blacklist"], [RID])

    def test_round_with_two_unknowns_cools(self):
        act = self.c.record_round(self.st, 2, 3, NOW)
        self.assertEqual(act["action"], "global_cooldown")
        self.assertIsNotNone(self.c.global_blocked(self.st, NOW + 1))

    def test_day_ratio_stops_until_midnight(self):
        st = self.st
        st["circuit"]["checks_today"] = 10
        st["circuit"]["unknown_today"] = 9
        act = self.c.record_round(st, 1, 0, NOW)     # 本轮只 1 个 unknown,不触发轮级
        self.assertEqual(act["action"], "day_stop")
        self.assertEqual(st["circuit"]["global_until"], P.next_midnight(NOW))

    def test_day_ratio_needs_min_samples(self):
        st = self.st
        st["circuit"]["checks_today"] = 3
        st["circuit"]["unknown_today"] = 3
        act = self.c.record_round(st, 1, 0, NOW)
        self.assertIsNone(act)                        # 3/3=100% 但样本太小,不判

    def test_reset_day(self):
        st = self.st
        st["day"] = P.day_str(NOW)
        st["checks_today"] = 50
        st["circuit"]["unknown_today"] = 9
        self.assertTrue(self.c.reset_day(st, NOW + 86400))
        self.assertEqual(st["checks_today"], 0)
        self.assertEqual(st["circuit"]["unknown_today"], 0)
        self.assertFalse(self.c.reset_day(st, NOW + 86401))


class TestTaskBatch(unittest.TestCase):
    def setUp(self):
        self.norm = P.normalize_cfg({"max_per_round": 2})
        self.st = P.empty_state(NOW)
        self.pending = [mon("主播甲"), mon("主播乙"), mon("主播丙")]

    def test_rotation_covers_everyone(self):
        seen = []
        for _ in range(3):
            batch = [m["name"] for m in P.task_batch(self.norm, self.st, self.pending, now=NOW)]
            self.assertEqual(len(batch), len(set(batch)))   # 同一轮不许重复查同一个人
            seen.extend(batch)
        self.assertEqual(set(seen), {"主播甲", "主播乙", "主播丙"})

    def test_batch_never_repeats_when_fewer_than_limit(self):
        """待补少于单轮上限时一轮只能出一个人 —— 重复塞人会白烧配额。"""
        batch = P.task_batch(P.normalize_cfg({"max_per_round": 3}), self.st,
                             [mon("主播甲")], now=NOW)
        self.assertEqual([m["name"] for m in batch], ["主播甲"])

    def test_skips_backoff(self):
        P._monitor_state(self.st, "主播甲")["backoff_until"] = NOW + 600
        got = [m["name"] for m in P.task_batch(self.norm, self.st, self.pending, now=NOW)]
        self.assertNotIn("主播甲", got)

    def test_all_backoff_returns_empty(self):
        for m in self.pending:
            P._monitor_state(self.st, m["name"])["backoff_until"] = NOW + 600
        self.assertEqual(P.task_batch(self.norm, self.st, self.pending, now=NOW), [])


class TestStateHelpers(unittest.TestCase):
    def test_merge_state_fills_missing(self):
        st = P.merge_state({"checks_today": 5, "circuit": {"reason": "x"}}, NOW)
        self.assertEqual(st["checks_today"], 5)
        self.assertEqual(st["circuit"]["reason"], "x")
        self.assertIn("global_until", st["circuit"])

    def test_merge_state_garbage(self):
        self.assertEqual(P.merge_state("not-a-dict", NOW)["checks_today"], 0)

    def test_next_midnight(self):
        """次日 00:00:必须晚于当前、不超过 24h、且落在 00:00(不写死 epoch,免时区依赖)。"""
        import time as _t
        nxt = P.next_midnight(NOW)
        self.assertGreater(nxt, NOW)
        self.assertLessEqual(nxt - NOW, 86400)
        self.assertEqual(_t.localtime(nxt)[3:5], (0, 0))


if __name__ == "__main__":
    unittest.main()
