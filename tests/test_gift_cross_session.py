#!/usr/bin/env python3
"""跨场礼物归属 + 计数口径(2026-10-29 二次修复)的单元测试。

为什么需要这一组
----------------
2026-10-29 复盘「预览里看得见礼物、点开统计却清零」时发现,三个独立缺陷
叠在一起,每一个都能单独让数字对不上:

  ① ffmpeg 崩溃重建后,弹幕会话要等 `danmaku_patrol_loop`(60s)才切 jsonl
     ⇒ 这期间收到的礼物写进**上一场**的文件;上一场早已结算完毕,本场又只认
     自己的文件 —— 实测某主播 19:05:50–19:06:03 的 41 条轻礼物谁的账都不算;
  ② `DanmakuRecorder.retarget` 只归零轻通道三段账,漏了 `gift_count`
     ⇒ 本场 0 条礼物的诊断快照仍显示上一场累计的 101 条,与同一行的
     `light.recv/kept = 0` 并排自相矛盾;
  ③ 收尾结算读**会话当前计数**,场次切换后就变成「B 场的收到数对 A 场的落盘数」。

对应三处修复,本文件逐条守:
  ① monitor._restart_recording 重开录制后**立即** retarget(不再等巡检);
  ② danmaku.retarget 归零 gift_count / last_gift_ts / msg_count / chat_count;
  ③ danmaku.DanmakuRecorder.ledger_for(按 jsonl 取账)+
     gift_channel_coverage(extra_paths=...) 按事件时间把迟到的行借回来。

设计要点
--------
- 夹具全是合成值(主播A / 192.168.1.10),不碰真实录制数据。
- 借入必须**按本场时间窗口收窄**:整场借会把同一批礼物算进两个场次,那比丢
  41 条更糟 —— 所以 `test_no_started_at_never_adopts` 是硬守卫。
- 本组不启动 WS 线程:DanmakuRecorder 的构造函数只赋初值,start() 才连网。
"""
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import danmaku  # noqa: E402
import monitor  # noqa: E402

DAY = "2026-10-29"
STEM_A = "主播A-20261029-100000"
STEM_B = "主播A-20261029-110000"
ROOM = "123456"


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _gift(ts_sec, gift="小花花", count=1, diamond=1, channel="light"):
    return {"ts": int(ts_sec * 1000), "ts_local": int(ts_sec * 1000),
            "type": "gift", "gift": gift, "count": count,
            "diamond": diamond, "channel": channel}


def _chat(ts_sec, text="路过"):
    return {"ts": int(ts_sec * 1000), "type": "chat", "text": text}


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def out_path(self, stem):
        """录制输出模板路径(与 monitor 真实形态一致)。"""
        return os.path.join(self.root, DAY, stem + "-%03d.flv")

    def jsonl(self, stem):
        return danmaku.jsonl_for(self.out_path(stem))

    def recorder(self, jsonl_path):
        """构造一个**不启动 WS** 的会话(retarget 会 open 句柄,这里负责收尾)。"""
        rec = danmaku.DanmakuRecorder("主播A", ROOM, jsonl_path, cfg={})

        def _close():
            for fh in (rec._fh, rec._rfh):
                try:
                    if fh:
                        fh.close()
                except Exception:               # noqa: BLE001
                    pass

        self.addCleanup(_close)
        return rec


class TestRetargetResetsSessionCounters(_TmpCase):
    """② retarget 必须把「场次口径」的计数一起归零。"""

    def test_retarget_zeroes_gift_and_session_counters(self):
        """★ 换场后 gift_count/last_gift_ts/msg_count/chat_count 全部归零。

        不归零的后果(实测):本场一条礼物都没有,诊断快照里仍显示上一场累计的
        101 条,前端把它渲染成「轻通道落盘 101 条」,同一行又写「落盘 0 条」。
        """
        rec = self.recorder(self.jsonl(STEM_A))
        rec.gift_count = 41
        rec.last_gift_ts = 1793271950.0
        rec.msg_count = 600
        rec.chat_count = 553
        rec.gift_light_recv = 41
        rec.gift_light_count = 41
        rec.retarget(self.jsonl(STEM_B))
        self.assertEqual(rec.gift_count, 0, "gift_count 必须归零(本场口径)")
        self.assertEqual(rec.last_gift_ts, 0.0, "静默计时必须从新场次重新起算")
        self.assertEqual(rec.msg_count, 0)
        self.assertEqual(rec.chat_count, 0)
        self.assertEqual(rec.gift_light_recv, 0)
        self.assertEqual(rec.gift_light_count, 0)

    def test_same_path_is_noop(self):
        """同一份 jsonl 上重入 retarget 不能清零(巡检每 60s 都会调一次)。"""
        rec = self.recorder(self.jsonl(STEM_A))
        rec.gift_count = 7
        rec.retarget(self.jsonl(STEM_A))
        self.assertEqual(rec.gift_count, 7)


class TestLedgerFor(_TmpCase):
    """③ 账本按 jsonl 配对:结算方拿到的必须是「这一份文件」的账。"""

    def test_current_file_returns_live_ledger(self):
        rec = self.recorder(self.jsonl(STEM_A))
        rec.gift_light_recv = 5
        rec.gift_light_count = 4
        rec._light_gift_errors = 1
        rec._light_field_miss["gift"] = 2
        led = rec.ledger_for(self.jsonl(STEM_A))
        self.assertEqual(led["recv"], 5)
        self.assertEqual(led["kept"], 4)
        self.assertEqual(led["parse_errors"], 1)
        self.assertEqual(led["field_missing"], {"gift": 2})
        # 快照必须是副本:调用方改了不能污染会话计数
        led["recv"] = 999
        self.assertEqual(rec.gift_light_recv, 5)

    def test_previous_file_keeps_its_own_ledger_after_retarget(self):
        """★ 切走之后,旧文件的账仍是旧值 —— 这正是「A 场的收到数」不再被
        「B 场的落盘数」顶替的原因。"""
        old, new = self.jsonl(STEM_A), self.jsonl(STEM_B)
        rec = self.recorder(old)
        rec.gift_light_recv = 41
        rec.gift_light_count = 41
        rec.retarget(new)
        rec.gift_light_recv = 60          # 新场次继续收
        rec.gift_light_count = 60
        self.assertEqual(rec.ledger_for(old)["recv"], 41)
        self.assertEqual(rec.ledger_for(new)["recv"], 60)

    def test_unknown_path_returns_none(self):
        """没见过也没存档的路径 → None(只验字段,不谎报已对账)。"""
        rec = self.recorder(self.jsonl(STEM_A))
        self.assertIsNone(rec.ledger_for(self.jsonl(STEM_B)))
        self.assertIsNone(rec.ledger_for(""))
        self.assertIsNone(rec.ledger_for(None))



class TestCrossSessionAdoption(_TmpCase):
    """①③ 迟到的礼物按**事件时间**归回本场,而不是按文件归属。

    事故的真实形态:本场(B)的礼物被写进了**上一场**(A)的文件 —— 因为 B 开局后
    弹幕会话还没切输出。所以结算 B 时,要从兄弟文件 A 里把「落在 B 窗口内」的
    事件借回来。
    """

    # A 场:10:00:00 起录 3000s(到 10:50);B 场:10:50:00 起录 600s。
    T_A = 1793258400.0          # 2026-10-29 10:00:00 +08:00(仅作算术锚点)
    DUR_A = 3000.0
    T_B = T_A + 3000.0
    DUR_B = 600.0

    def _scene(self):
        """A 文件:A 自己的 21 条 + 落在 B 窗口内的 2 条迟到行;
        B 文件:B 自己的 2 条。"""
        a_rows = [_gift(self.T_A + 100), _gift(self.T_A + 200), _gift(self.T_A + 300),
                  _gift(self.T_B + 10), _gift(self.T_B + 20)]
        b_rows = [_gift(self.T_B + 100), _gift(self.T_B + 200)]
        pa = _write_jsonl(self.jsonl(STEM_A), a_rows)
        pb = _write_jsonl(self.jsonl(STEM_B), b_rows)
        return pa, pb

    def test_adopts_only_events_inside_window(self):
        """★ 兄弟文件里落在本场窗口内的礼物被借入,窗口外的(A 自己的)一条不借。"""
        pa, pb = self._scene()
        gi = danmaku.gift_channel_coverage(
            pb, self.DUR_B, started_at=self.T_B, extra_paths=[pa])
        self.assertIsNotNone(gi)
        # 本场 2 条 + A 文件里属于本场窗口的 2 条
        self.assertEqual(gi["light"]["events"], 4)
        self.assertEqual(gi["light"]["adopted"], 2, "借入条数必须如实报出")

    def test_out_of_window_events_in_main_file_still_counted(self):
        """主文件不做窗口裁剪(保持 2026-10-29 之前的口径,避免存量数字漂移)。"""
        pa = _write_jsonl(self.jsonl(STEM_A),
                          [_gift(self.T_A + 100), _gift(self.T_A + 9000)])
        gi = danmaku.gift_channel_coverage(pa, self.DUR_A, started_at=self.T_A)
        self.assertEqual(gi["light"]["events"], 2)
        self.assertEqual(gi["light"]["adopted"], 0)

    def test_no_started_at_never_adopts(self):
        """★ 没有 started_at 就没有窗口 → 一律不借。

        没有这条闸,补算老条目时会按整份兄弟文件借入,把同一批礼物同时算进
        两个场次 —— 比丢数据更糟(数字看着更"完整",却全是错的)。
        """
        pa, pb = self._scene()
        gi = danmaku.gift_channel_coverage(pb, self.DUR_B, extra_paths=[pa])
        self.assertEqual(gi["light"]["events"], 2)
        self.assertEqual(gi["light"]["adopted"], 0)

    def test_missing_sibling_is_ignored(self):
        """兄弟文件读不到/不存在 → 静默跳过,不影响本场统计。"""
        pb = _write_jsonl(self.jsonl(STEM_B), [_gift(self.T_B + 100)])
        gi = danmaku.gift_channel_coverage(
            pb, self.DUR_B, started_at=self.T_B,
            extra_paths=[os.path.join(self.root, "不存在.danmaku.jsonl"), "", None])
        self.assertEqual(gi["light"]["events"], 1)
        self.assertEqual(gi["light"]["adopted"], 0)

    def test_adopted_keeps_recv_consistent(self):
        """★ 借入后不能出现「收到 0 条 / 落盘 4 条」的矛盾展示:
        recv 抬到落盘数,而不是原样留着 0。"""
        pa, pb = self._scene()
        gi = danmaku.gift_channel_coverage(
            pb, self.DUR_B, started_at=self.T_B, recv=0, extra_paths=[pa])
        self.assertEqual(gi["light"]["recv"], 4)
        self.assertEqual(gi["light"]["lost"], 0)
        self.assertEqual(gi["light"]["status"], "complete")

    def test_coverage_ratio_uses_adopted_events(self):
        """覆盖时长也按借入后的事件算,否则字幕后重新生成会少画横幅。"""
        pa = _write_jsonl(self.jsonl(STEM_A), [_gift(self.T_B + 10)])
        pb = _write_jsonl(self.jsonl(STEM_B), [_gift(self.T_B + 500)])
        only_self = danmaku.gift_channel_coverage(
            pb, self.DUR_B, started_at=self.T_B)
        with_sib = danmaku.gift_channel_coverage(
            pb, self.DUR_B, started_at=self.T_B, extra_paths=[pa])
        self.assertGreater(with_sib["covered_sec"], only_self["covered_sec"])



class TestSiblingJsonls(_TmpCase):
    """兄弟文件清单:只收同目录的 danmaku.jsonl,且排除自己。"""

    def test_lists_only_danmaku_jsonl_and_excludes_self(self):
        pa = _write_jsonl(self.jsonl(STEM_A), [_gift(1000)])
        pb = _write_jsonl(self.jsonl(STEM_B), [_gift(2000)])
        # 同目录的其它 .jsonl(榜单/诊断)不该被当成兄弟
        meta = os.path.dirname(pa)
        for name in ("主播A-20261029-100000.giftrank.jsonl",
                     "主播A-20261029-100000.giftwatch.jsonl",
                     "主播A-20261029-100000.align.json"):
            _write_jsonl(os.path.join(meta, name), [{"x": 1}])
        got = monitor._sibling_danmaku_jsonls(pa)
        self.assertEqual(got, [pb])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(monitor._sibling_danmaku_jsonls(""), [])
        self.assertEqual(monitor._sibling_danmaku_jsonls(
            os.path.join(self.root, "没有这个目录", "x.danmaku.jsonl")), [])


class TestRestartSwitchesDanmakuImmediately(_TmpCase):
    """① 崩溃重建后必须**立即**切 jsonl,不能等 60s 巡检。

    实测事故:某主播 19:05:11 换场,19:06:03 才切输出,52 秒里的 41 条礼物
    全写进了上一场已结算的文件。这条测试锁的就是「重开录制后马上切」。
    """

    def test_restart_retargets_danmaku_right_away(self):
        name = "主播A"
        new_out = self.out_path(STEM_B)
        fake_dm = mock.Mock()

        def _fake_start(state, nm, url, cfg, room_id=None):
            # 真实 _start_recording 会把新条目写进 _recordings
            monitor._recordings[nm] = {"proc": mock.Mock(), "output_path": new_out,
                                       "started_at": 1.0, "stream_url": url}
            return monitor._recordings[nm]["proc"]

        self.addCleanup(monitor._recordings.pop, name, None)
        self.addCleanup(monitor._danmaku_paths.pop, name, None)
        self.addCleanup(monitor._rec_health.pop, name, None)

        with mock.patch.object(monitor, "_carry_segments"), \
                mock.patch.object(monitor, "_start_recording", side_effect=_fake_start), \
                mock.patch.object(monitor, "_history_finish"), \
                mock.patch.object(monitor.danmaku_mod, "get", return_value=fake_dm), \
                mock.patch.object(monitor.danmaku_mod, "retarget_for") as retarget:
            monitor._restart_recording(None, name, "http://example/s.flv", {}, "1")

        retarget.assert_called_once_with(name, new_out)
        self.assertEqual(monitor._danmaku_paths.get(name), new_out)

    def test_restart_survives_retarget_failure(self):
        """切不动不能把录制带下去:巡检 60s 后仍会兜住。"""
        name = "主播A"
        new_out = self.out_path(STEM_B)

        def _fake_start(state, nm, url, cfg, room_id=None):
            monitor._recordings[nm] = {"proc": mock.Mock(), "output_path": new_out,
                                       "started_at": 1.0, "stream_url": url}
            return monitor._recordings[nm]["proc"]

        self.addCleanup(monitor._recordings.pop, name, None)
        self.addCleanup(monitor._danmaku_paths.pop, name, None)
        self.addCleanup(monitor._rec_health.pop, name, None)

        with mock.patch.object(monitor, "_carry_segments"), \
                mock.patch.object(monitor, "_start_recording", side_effect=_fake_start), \
                mock.patch.object(monitor, "_history_finish"), \
                mock.patch.object(monitor.danmaku_mod, "get", return_value=mock.Mock()), \
                mock.patch.object(monitor.danmaku_mod, "retarget_for",
                                  side_effect=OSError("磁盘满")), \
                mock.patch.object(monitor, "log") as log:
            monitor._restart_recording(None, name, "http://example/s.flv", {}, "1")

        self.assertTrue(any("切换输出失败" in str(c) for c in log.call_args_list),
                        "失败必须留日志,否则又是一次静默缺陷")


if __name__ == "__main__":
    unittest.main()
