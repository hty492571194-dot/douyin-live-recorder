#!/usr/bin/env python3
"""「短期录制主播」录制期限判定的边界用例（2026-10-22 拍板语义）。

锁死的事:
  1. 自然日口径 —— 含添加当天,until 当天仍可录,次日 00:00 起不再开新场;
  2. 到期是**纯时间派生**,不写配置:判定函数必须零 IO、可传 today 注入;
  3. paused 优先于 expired —— 手停就是暂停,不能出现双态歧义;
  4. 期限数据坏掉(缺失/格式错)→ missing_until 保守不录,不抛异常不崩;
  5. 到期只禁新场:正在录的场次不能被休眠判定打断(决策6A);
  6. 到期后停止检测(决策5A):is_dormant 对 expired=True。

续期基准(new_until = base + N,base = max(today, until) 或 today-1)是前端
一次性计算,写入 config;days_left/到期判定是后端真源,这里全量锁定。
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import webui  # noqa: E402

TODAY = date(2026, 10, 22)


def _cfg(*mons):
    return {"monitors": list(mons)}


class TestRecordState(unittest.TestCase):
    """record_state(m, today) 三元组: (state, until, days_left)。"""

    def test_long_is_ok(self):
        st, until, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "long"}, TODAY)
        self.assertEqual((st, until, dl), ("ok", None, None))

    def test_missing_mode_is_ok(self):
        """历史主播没有 record_mode -> 按长期处理（老行为不变）。"""
        st, until, dl = monitor.record_state({"name": "甲"}, TODAY)
        self.assertEqual(st, "ok")

    def test_none_monitor_is_ok(self):
        """找不到主播不能误判成「不许录」—— 调用方（如手动开始）会拿到 True。"""
        self.assertEqual(monitor.record_state(None, TODAY)[0], "ok")

    def test_paused(self):
        st, _, _ = monitor.record_state(
            {"name": "甲", "auto_record": False}, TODAY)
        self.assertEqual(st, "paused")

    def test_short_within_window(self):
        st, until, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_days": 3, "record_until": "2026-10-24"}, TODAY)
        self.assertEqual(st, "ok")
        self.assertEqual(until, date(2026, 10, 24))
        self.assertEqual(dl, 3)          # 22/23/24 共 3 个可录自然日

    def test_short_last_day_is_still_ok(self):
        """末日当天（23:59:59 之前）仍可录,「含当天」口径。"""
        st, _, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026-10-22"}, TODAY)
        self.assertEqual(st, "ok")
        self.assertEqual(dl, 1)          # 1 天档边界:剩 1 天

    def test_short_expired_next_day(self):
        st, until, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026-10-21"}, TODAY)
        self.assertEqual(st, "expired")
        self.assertEqual(dl, 0)

    def test_until_earlier_than_today_is_expired(self):
        """until 早于添加/今天（手改配置）-> 直接过期,不给「负天数」。"""
        st, _, _ = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026-10-01"}, TODAY)
        self.assertEqual(st, "expired")

    def test_short_missing_until(self):
        st, until, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short"}, TODAY)
        self.assertEqual((st, until, dl), ("missing_until", None, None))

    def test_short_bad_until_format(self):
        """格式坏掉 -> missing_until（保守不录）,不能抛异常。"""
        st, _, _ = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026/10/24"}, TODAY)
        self.assertEqual(st, "missing_until")
        st, _, _ = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": ""}, TODAY)
        self.assertEqual(st, "missing_until")

    def test_paused_wins_over_expired(self):
        """手停 + 过期同时存在 -> paused。开关语义优先,不能出现双态歧义。"""
        st, until, _ = monitor.record_state(
            {"name": "甲", "auto_record": False, "record_mode": "short",
             "record_until": "2026-10-01"}, TODAY)
        self.assertEqual(st, "paused")
        self.assertEqual(until, date(2026, 10, 1))   # 期限数据保留,便于原样恢复

    def test_cross_year_boundary(self):
        st, _, dl = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026-12-31"}, date(2026, 12, 31))
        self.assertEqual((st, dl), ("ok", 1))
        st, _, _ = monitor.record_state(
            {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2026-12-31"}, date(2027, 1, 1))
        self.assertEqual(st, "expired")

    def test_today_injected_not_wallclock(self):
        """判定必须吃注入的 today —— 服务端「今天」是唯一真源,不能读墙钟。"""
        m = {"name": "甲", "auto_record": True, "record_mode": "short",
             "record_until": "2027-01-05"}
        self.assertEqual(monitor.record_state(m, date(2027, 1, 5))[0], "ok")
        self.assertEqual(monitor.record_state(m, date(2027, 1, 6))[0], "expired")


class TestAutoRecordAndDormant(unittest.TestCase):
    """auto_record_enabled / is_dormant 与期限的联动。"""

    def test_expired_not_autorecord(self):
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "short",
                    "record_until": "2026-10-21"})
        self.assertFalse(monitor.auto_record_enabled(cfg, "甲"))

    def test_missing_until_not_autorecord(self):
        """期限坏掉时保守不录 —— 但绝不能「以为在录」。"""
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "short"})
        self.assertFalse(monitor.auto_record_enabled(cfg, "甲"))

    def test_unexpired_short_autorecord(self):
        """未到期 → 照常开播即录。

        这里的 until 必须按**今天**推算:auto_record_enabled 读墙钟(没有 today 注入),
        写死日期迟早会变成「已到期」,把整套测试跑红(2026-10-24 写死的那个日期
        就在 10-25 之后一直报红)。
        """
        until = (date.today() + timedelta(days=3)).isoformat()
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "short",
                    "record_until": until})
        self.assertTrue(monitor.auto_record_enabled(cfg, "甲"))

    def test_expired_is_dormant(self):
        """决策5A:到期停止检测,省 Cookie 配额。"""
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "short",
                    "record_until": "2026-10-21"})
        self.assertTrue(monitor.is_dormant(cfg, "甲", False))

    def test_expired_but_recording_never_dormant(self):
        """决策6A:到期不中断在录场次 —— 仍要检测下播/收尾/归档。"""
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "short",
                    "record_until": "2026-10-21"})
        self.assertFalse(monitor.is_dormant(cfg, "甲", True))

    def test_long_never_dormant(self):
        cfg = _cfg({"name": "甲", "auto_record": True, "record_mode": "long"})
        self.assertFalse(monitor.is_dormant(cfg, "甲", False))


class TestRecordWindows(unittest.TestCase):
    """/api/status 下发的快照:前端只渲染,不自算「剩几天」。"""

    def test_shape_and_today(self):
        cfg = _cfg(
            {"name": "甲", "record_mode": "long"},
            {"name": "乙", "auto_record": True, "record_mode": "short",
             "record_days": 3, "record_until": "2026-10-24"},
            {"name": "丙", "auto_record": False})
        w = monitor.record_windows(cfg, TODAY)
        self.assertEqual(w["today"], "2026-10-22")
        self.assertEqual(set(w["streamers"]), {"甲", "乙", "丙"})
        self.assertEqual(w["streamers"]["甲"],
                         {"state": "ok", "mode": "long", "until": None,
                          "days_left": None})
        self.assertEqual(w["streamers"]["乙"]["state"], "ok")
        self.assertEqual(w["streamers"]["乙"]["days_left"], 3)
        self.assertEqual(w["streamers"]["丙"]["mode"], "paused")

    def test_mode_fallback_matches_state(self):
        """缺 record_mode 时 mode 按 auto_record 派生,与 record_state 兜底一致。"""
        w = monitor.record_windows(_cfg({"name": "甲", "auto_record": False}), TODAY)
        self.assertEqual(w["streamers"]["甲"]["mode"], "paused")
        w = monitor.record_windows(_cfg({"name": "甲"}), TODAY)
        self.assertEqual(w["streamers"]["甲"]["mode"], "long")

    def test_stopped_wins_over_stale_record_mode(self):
        """手停要压过残留的 record_mode:mode 必须跟着 state 收敛(2026-10-27 修)。

        手工改过 config.json 会留下「auto_record=false 但 record_mode=long」这种
        矛盾组合 —— 字段本身合法,旧的兜底(只在字段缺失时才派生)拦不住,于是
        /api/status 下发出「state=paused + mode=long」。前端 _recBucket 只看 mode,
        就把该主播归进「长期录制」档、徽标写「长期」,而开关是关的:页面自相矛盾。
        """
        for stale in ("long", "short"):
            w = monitor.record_windows(_cfg(
                {"name": "甲", "auto_record": False, "record_mode": stale,
                 "record_days": 3, "record_until": "2026-12-31"}), TODAY)
            one = w["streamers"]["甲"]
            self.assertEqual(one["state"], "paused", f"record_mode={stale} 时 state 不该是别的")
            self.assertEqual(one["mode"], "paused",
                             f"auto_record=false 且 record_mode={stale}:mode 没跟着 state 收敛")

    def test_stale_mode_does_not_leak_into_other_states(self):
        """收敛只发生在 paused 上:正常/到期/期限缺失的 mode 要原样保留。

        到期主播的 mode 仍是 short(前端靠 state 归暂停档,徽标要能写「已到期」,
        写成 paused 会退化成「暂停录制」,丢掉「该续期」的信号)。
        """
        cfg = _cfg(
            {"name": "甲", "auto_record": True, "record_mode": "long"},
            {"name": "乙", "auto_record": True, "record_mode": "short",
             "record_days": 3, "record_until": "2026-10-20"},
            {"name": "丁", "auto_record": True, "record_mode": "short"})
        w = monitor.record_windows(cfg, TODAY)["streamers"]
        self.assertEqual((w["甲"]["state"], w["甲"]["mode"]), ("ok", "long"))
        self.assertEqual((w["乙"]["state"], w["乙"]["mode"]), ("expired", "short"),
                         "到期不能把 mode 改成 paused(徽标要写「已到期」)")
        self.assertEqual((w["丁"]["state"], w["丁"]["mode"]), ("missing_until", "short"))

    def test_single_window(self):
        w = monitor.record_window(_cfg(
            {"name": "乙", "auto_record": True, "record_mode": "short",
             "record_until": "2026-10-22"}), "乙", TODAY)
        self.assertEqual(w["state"], "ok")
        self.assertEqual(w["days_left"], 1)


class TestWebuiValidation(unittest.TestCase):
    """写入端校验:挡脏数据,放过正常数据。"""

    def setUp(self):
        self.state = webui.State("/tmp/never-written.json",
                                 {"monitors": [{"name": "甲"}]}, None)

    def _err(self, patch):
        return self.state._validate(patch)

    def _mons(self, **kw):
        m = {"name": "甲", "anchor": "2"}
        m.update(kw)
        return {"monitors": [m]}

    def test_record_mode_ok(self):
        self.assertIsNone(self._err(self._mons(record_mode="long")))
        self.assertIsNone(self._err(self._mons(record_mode="paused")))
        self.assertIsNone(self._err(self._mons(
            record_mode="short", record_until="2026-10-24")))

    def test_record_mode_rejects_unknown(self):
        err = self._err(self._mons(record_mode="forever"))
        self.assertIn("record_mode", err or "")

    def test_record_days_ok(self):
        for d in (1, 3, 7, 30, 365):
            self.assertIsNone(self._err(self._mons(record_days=d)))

    def test_record_days_rejects_bad(self):
        for d in (0, -1, 366, 1.5, "3", True):
            err = self._err(self._mons(record_days=d))
            self.assertIn("record_days", err or "", f"应拒绝 record_days={d!r}")

    def test_record_until_ok_and_bad(self):
        self.assertIsNone(self._err(self._mons(record_until="2026-10-24")))
        self.assertIsNone(self._err(self._mons(record_until="")))   # 允许留空
        self.assertIn("record_until", self._err(
            self._mons(record_until="2026/10/24")) or "")
        self.assertIn("record_until", self._err(
            self._mons(record_until="10-24")) or "")

    def test_short_requires_until(self):
        err = self._err(self._mons(record_mode="short"))
        self.assertIn("record_until", err or "")
        self.assertIsNone(self._err(self._mons(
            record_mode="short", record_until="2026-10-24")))

    def test_full_short_entry_ok(self):
        """前端添加短期主播时写入的完整字段组合必须能存。"""
        self.assertIsNone(self._err({"monitors": [{
            "name": "新主播", "anchor": "MS4wLjAB", "web_rid": "123",
            "added_at": 1788615342, "auto_record": True, "note": "",
            "record_mode": "short", "record_days": 3,
            "record_until": "2026-10-24"}]}))


if __name__ == "__main__":
    unittest.main()
