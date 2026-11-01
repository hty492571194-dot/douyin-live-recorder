#!/usr/bin/env python3
"""贡献榜(RoomRank)解析 + 礼物通道判定 + 榜单落盘 的单元测试。

背景(2026-10-29 实测,详见 录制侧三要素与登录一致性分析):
  匿名观众身份收不到 WebcastGiftMessage(完整通道),但 WebcastRoomRankMessage
  照常下发,且 **分值 = 累计送礼抖币,1:1 精确相等**。
  项目原有 proto 的字段号是错的(把真实 field 3 写成 field 2),解出来 score 恒空,
  本测试用「手工构造的原始 wire 字节」锁定真实布局,防止再被上游 .proto 带偏。

设计要点:
  - 夹具一律合成值(主播A/主播B…),不写真实主播或观众昵称(TestRealRepoIsClean)。
  - 断言重点是 **gap == score[i] - score[i+1]**:这个算术自证关系一旦破了,
    说明字段号又被改错了。
  - 空榜(field 2 只有用户、没有分值)必须解成 ranks_list 为空 ——
    否则会把"无人上榜"谎报成"有人送礼"。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import danmaku  # noqa: E402
from danmaku_proto import RoomRankMessage  # noqa: E402


# ── protobuf wire 编码小工具(只为构造夹具,不引第三方依赖)──────────
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _tag(f, w):
    return _varint((f << 3) | w)


def _ld(f, payload):
    return _tag(f, 2) + _varint(len(payload)) + payload


def _vi(f, n):
    return _tag(f, 0) + _varint(n)


def _user(uid, name):
    return _vi(1, uid) + _ld(3, name.encode())


def _entry(uid, name, score, rank, gap):
    """field 3 的一条榜位:1=User, 2=score, 3=rank, 5=gap。"""
    return _ld(1, _user(uid, name)) + _vi(2, score) + _vi(3, rank) + _vi(5, gap)


def _board(entries, create_time=1793200000000):
    body = (_ld(1, _ld(1, b"WebcastRoomRankMessage") + _vi(2, 111)
                + _vi(3, 222) + _vi(4, create_time))
            + b"".join(_ld(3, e) for e in entries))
    return body


# 9 名榜位,分数递减且 gap 与"与下一名分差"严格相等(取自真实抓包的形态)
_SCORES = [20198, 3000, 2507, 2345, 1889, 1434, 1101, 1089, 1003]


class TestRoomRankProto(unittest.TestCase):
    """真实 wire 布局的解析正确性。"""

    def test_parses_real_layout(self):
        entries = [_entry(1000 + i, f"观众{i}", s, i + 1,
                          (_SCORES[i] - _SCORES[i + 1]) if i + 1 < len(_SCORES) else 0)
                   for i, s in enumerate(_SCORES)]
        m = RoomRankMessage().parse(_board(entries))
        self.assertEqual(len(m.ranks_list), 9)
        self.assertEqual([r.rank for r in m.ranks_list], list(range(1, 10)))
        self.assertEqual([r.score for r in m.ranks_list], _SCORES)
        self.assertEqual(m.ranks_list[0].user.nick_name, "观众0")
        self.assertEqual(m.ranks_list[0].user.id, 1000)
        self.assertEqual(m.common.create_time, 1793200000000)

    def test_gap_equals_score_delta(self):
        """gap 必须等于与下一名的分差 —— 字段号被改错时这条会立刻崩。"""
        entries = [_entry(1000 + i, f"观众{i}", s, i + 1,
                          (_SCORES[i] - _SCORES[i + 1]) if i + 1 < len(_SCORES) else 0)
                   for i, s in enumerate(_SCORES)]
        m = RoomRankMessage().parse(_board(entries))
        for i in range(len(m.ranks_list) - 1):
            self.assertEqual(
                m.ranks_list[i].gap,
                m.ranks_list[i].score - m.ranks_list[i + 1].score,
                f"第 {i + 1} 名的 gap 与分差不等,字段语义可能又被改错")

    def test_empty_board_only_users(self):
        """主播甲那类空榜:只有 field 2 的用户列表,没有分值榜。"""
        payload = _ld(2, _ld(1, _user(2001, "路人甲")))
        m = RoomRankMessage().parse(payload)
        self.assertEqual(len(m.ranks_list), 0, "空榜不得解出榜位")
        self.assertEqual(len(m.users_list), 1)

    def test_empty_payload(self):
        m = RoomRankMessage().parse(b"")
        self.assertEqual(len(m.ranks_list), 0)


class TestClassifyGiftChannel(unittest.TestCase):
    """礼物通道判定:light / full_only / quiet / unknown。"""

    def test_light_when_any_light_event(self):
        self.assertEqual(danmaku.classify_gift_channel(1, 0, 60), "light")
        self.assertEqual(danmaku.classify_gift_channel(262, 5000, 3600), "light")

    def test_full_only_when_active_but_zero_gift(self):
        """主播乙 盲场形态:67 分钟 56 条弹幕(≈0.84/min)…但实测那场更活跃,用 1.2/min。"""
        self.assertEqual(danmaku.classify_gift_channel(0, 120, 6000), "full_only")

    def test_quiet_when_idle(self):
        self.assertEqual(danmaku.classify_gift_channel(0, 2, 6000), "quiet")

    def test_unknown_without_duration(self):
        self.assertEqual(danmaku.classify_gift_channel(0, 100, 0), "unknown")
        self.assertEqual(danmaku.classify_gift_channel(0, 100), "unknown")

    def test_threshold_boundary(self):
        """整 1 条/分钟算活跃(full_only),略低于则 quiet。"""
        self.assertEqual(danmaku.classify_gift_channel(0, 60, 3600), "full_only")
        self.assertEqual(danmaku.classify_gift_channel(0, 59, 3600), "quiet")


class TestCoverageExposesChannel(unittest.TestCase):
    """gift_channel_coverage 必须把类别一起返回(历史条目靠它自动带上)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rows, stem="主播A-20261015-200000"):
        p = os.path.join(self._tmp.name, ".meta", stem + ".danmaku.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    def test_light_session(self):
        start = 1000.0
        p = self._write([{"ts": (start + i * 60) * 1000, "type": "gift", "gift": "礼物#1"}
                         for i in range(10)]
                        + [{"ts": (start + i) * 1000, "type": "chat", "content": "hi"}
                           for i in range(30)])
        gi = danmaku.gift_channel_coverage(p, 600.0, started_at=start)
        self.assertEqual(gi["gift_channel"], "light")
        self.assertEqual(gi["events"], 10)
        self.assertEqual(gi["chat_events"], 30)

    def test_full_only_session(self):
        start = 1000.0
        p = self._write([{"ts": (start + i * 20) * 1000, "type": "chat", "content": "hi"}
                         for i in range(300)])
        gi = danmaku.gift_channel_coverage(p, 600.0, started_at=start)
        self.assertEqual(gi["gift_channel"], "full_only")
        self.assertEqual(gi["events"], 0)

    def test_rank_rows_not_counted_as_gift(self):
        """type:"rank" 不能被算成礼物事件(否则盲场会假报有礼物)。"""
        start = 1000.0
        p = self._write([{"ts": (start + i * 30) * 1000, "type": "rank",
                          "ranks": [{"rank": 1, "score": 100}], "top_score": 100}
                         for i in range(5)]
                        + [{"ts": (start + i * 20) * 1000, "type": "chat", "content": "x"}
                           for i in range(300)])
        gi = danmaku.gift_channel_coverage(p, 600.0, started_at=start)
        self.assertEqual(gi["events"], 0, "rank 行不算礼物")
        self.assertEqual(gi["gift_channel"], "full_only")


class TestRecorderWritesRank(unittest.TestCase):
    """DanmakuRecorder 落 type:"rank" 行:限频 + 榜面无变化不写 + 空榜不写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, ".meta", "主播A-20261015-200000.danmaku.jsonl")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.rec = danmaku.DanmakuRecorder("主播A", "123", self.path)
        self.rec._fh = open(self.path, "a", encoding="utf-8", buffering=1)
        self.rank_file = danmaku.giftrank_for(self.path)
        self.rec._rfh = open(self.rank_file, "a", encoding="utf-8", buffering=1)

    def tearDown(self):
        for h in (self.rec._fh, self.rec._rfh):
            try:
                h.close()
            except Exception:
                pass
        self._tmp.cleanup()

    def _msg(self, payload):
        return type("M", (), {"method": "WebcastRoomRankMessage",
                              "payload": payload, "msg_id": 1})()

    def _rank_lines(self):
        """榜单快照必须写在独立 .giftrank.jsonl,不得污染 danmaku.jsonl。"""
        with open(self.rank_file, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_writes_rank_line(self):
        payload = _board([_entry(1001, "观众A", 900, 1, 700),
                          _entry(1002, "观众B", 200, 2, 0)])
        self.rec._dispatch(self._msg(payload), 1793200000000)
        lines = self._rank_lines()
        self.assertEqual(len(lines), 1)
        e = lines[0]
        self.assertEqual(e["type"], "rank")
        self.assertEqual(e["top_score"], 900)
        self.assertEqual(e["ranks"][0]["name"], "观众A")
        self.assertEqual(e["ranks"][0]["score"], 900)
        self.assertEqual(e["ranks"][1]["rank"], 2)
        # ts 必须是**毫秒**量级:RoomRank 的 create_time 本身就是毫秒,
        # 早期实现又乘了 1000,会写出 1.79e15 的假时间(守卫住)
        self.assertTrue(1e12 < e["ts"] < 1e14, f"ts 量级不对: {e['ts']}")
        self.assertEqual(self.rec.rank_snapshots, 1)

    def test_throttled_and_deduped(self):
        p1 = _board([_entry(1001, "观众A", 900, 1, 700)])
        self.rec._dispatch(self._msg(p1), 1793200000000)
        # 同一榜面再来一次(限频期内) → 不写
        self.rec._dispatch(self._msg(p1), 1793200001000)
        self.assertEqual(len(self._rank_lines()), 1, "限频期内不得重复写")
        # 分数变了但仍在限频期内 → 也不写
        p2 = _board([_entry(1001, "观众A", 1200, 1, 1000)])
        self.rec._dispatch(self._msg(p2), 1793200002000)
        self.assertEqual(len(self._rank_lines()), 1, "限频期内即使变化也不写")
        # 把限频窗口拨回去 → 变化了才写
        self.rec._rank_last_emit -= danmaku._RANK_EMIT_SEC + 1
        self.rec._dispatch(self._msg(p2), 1793200003000)
        self.assertEqual(len(self._rank_lines()), 2, "过了限频且榜面变化 → 应写")

    def test_same_board_not_rewritten(self):
        p1 = _board([_entry(1001, "观众A", 900, 1, 700)])
        self.rec._dispatch(self._msg(p1), 1793200000000)
        self.rec._rank_last_emit -= danmaku._RANK_EMIT_SEC + 1
        self.rec._dispatch(self._msg(p1), 1793200005000)
        self.assertEqual(len(self._rank_lines()), 1, "榜面没变化,过了限频也不写")

    def test_danmaku_jsonl_stays_clean(self):
        """契约守卫:榜单快照绝不能进 danmaku.jsonl(type 集合是 chat/gift/member)。"""
        payload = _board([_entry(1001, "观众A", 900, 1, 700)])
        self.rec._dispatch(self._msg(payload), 1793200000000)
        with open(self.path, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(rows, [], "danmaku.jsonl 不得出现 rank 行")
        self.assertEqual(len(self._rank_lines()), 1, "榜单应落在 .giftrank.jsonl")

    def test_empty_board_not_written(self):
        payload = _ld(2, _ld(1, _user(2001, "路人甲")))
        self.rec._dispatch(self._msg(payload), 1793200000000)
        self.assertEqual(self._rank_lines(), [], "空榜不得落盘,否则会谎报有人送礼")


class TestCommonTsMs(unittest.TestCase):
    """create_time 单位不统一:秒(chat/gift) vs 毫秒(RoomRank)。"""

    def test_seconds_to_ms(self):
        self.assertEqual(danmaku._common_ts_ms(1793215292, 0), 1793215292000)

    def test_ms_kept(self):
        self.assertEqual(danmaku._common_ts_ms(1793215292243, 0), 1793215292243)

    def test_microseconds_normalised(self):
        self.assertEqual(danmaku._common_ts_ms(1793215292243000, 0), 1793215292243)

    def test_missing_falls_back(self):
        self.assertEqual(danmaku._common_ts_ms(0, 12345), 12345)
        self.assertEqual(danmaku._common_ts_ms(None, 999), 999)


class TestLightGiftErrorCounted(unittest.TestCase):
    """轻礼物解析失败必须独立计数(不再混进 _silent_errors)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "x.danmaku.jsonl")
        self.rec = danmaku.DanmakuRecorder("主播A", "123", self.path)
        self.rec._fh = open(self.path, "a", encoding="utf-8")

    def tearDown(self):
        try:
            self.rec._fh.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_light_gift_parse_failure_counted_separately(self):
        bad = type("M", (), {"method": "WebcastLightGiftMessage",
                             "payload": b"\xff\xff\xff\xff", "msg_id": 1})()
        with mock.patch.object(danmaku, "_log"):
            self.rec._dispatch(bad, 1793200000000)
        self.assertEqual(self.rec.light_gift_errors, 1)
        self.assertEqual(self.rec.gift_errors, 0)
        self.assertEqual(self.rec.silent_errors, 0, "不得再算进静默丢弃")


if __name__ == "__main__":
    unittest.main()
