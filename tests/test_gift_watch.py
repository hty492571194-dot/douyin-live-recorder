"""礼物健康度(轻通道完整性):对账判定 / 结论码 / 分级推送 / 合并 / 冷却 / 落盘。

这些是「拦分叉」用的守卫 —— 2026-10-10 加功能时一并写的,2026-10-29 改为
**轻通道口径**后重写。改这块代码前先跑:
    .venv/bin/python -m unittest tests.test_gift_watch

设计要点(容易改坏的地方都在这里钉住):
  1. 判定顺序不能乱:断连必须先于「无人送礼」判定,否则 WS 挂了会被当成没人送礼;
  2. 健康度只查「轻通道给了什么 vs 录下了什么」(条数/字段),不查"谁送的/送什么"
     —— 那两项轻通道协议本身不含,拿它算不完整等于每天报警;
  3. 零礼物一律 quiet:349 场实测证明"弹幕热闹却零礼物"多是服务端未下发轻礼物,
     推手机只会把人训练成无视告警;quiet 级永不推手机;
  4. 完整性缺口不等静默阈值 —— 每轮巡检都要能报(quiet_phase=False 也报);
  5. 冷却不能拦住「结论升级」(正常→可疑→故障必须立刻再报)。
"""
import asyncio
import collections
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import danmaku
import monitor
import notify
import webui


class _FakeDm:
    """DanmakuRecorder 的最小替身(只提供监控器读到的字段)。"""

    def __init__(self, **kw):
        self.name = kw.get("name", "主播甲")
        self.dead = kw.get("dead", False)
        self.retries = kw.get("retries", 0)
        # 完整通道解析失败:2026-10-29 起只作诊断,不再进健康度(它不落盘)
        self.gift_errors = kw.get("gift_errors", 0)
        # 轻通道三段账 —— 健康度的唯一判据
        self.gift_light_recv = kw.get("light_recv", 0)
        self.gift_light_count = kw.get("light_kept", 0)
        self.light_gift_errors = kw.get("light_errors", 0)
        self.light_write_errors = kw.get("light_write_errors", 0)
        self.light_field_miss = kw.get("light_field_miss", {})
        self.gift_full_count = kw.get("full_count", 0)
        self.silent_errors = kw.get("silent_errors", 0)
        self.frame_errors = kw.get("frame_errors", 0)
        self.msg_count = kw.get("msg_count", 100)
        self.gift_count = kw.get("gift_count", 0)
        self.last_gift_ts = kw.get("last_gift_ts", 0.0)
        self.last_msg_ts = kw.get("last_msg_ts", time.time())
        self.method_counter = collections.Counter(
            kw.get("methods", {"WebcastChatMessage": 100}))


class TestGiftDiagnose(unittest.TestCase):
    """结论码:先判会话、再判连接、再判轻通道完整性、最后才是"有没有礼物"。"""

    def _diag(self, dm, base=None, session_sec=3000.0, busy_rate=2.0,
              quiet_phase=True):
        return monitor._gift_diagnose(dm, base or time.time(), 45 * 60,
                                      session_sec, busy_rate, quiet_phase)

    def test_no_session(self):
        self.assertEqual(self._diag(None)[:2], ("DM_OFF", "fault"))

    def test_no_session_not_reported_before_threshold(self):
        """开场几秒会话可能还没建起来 —— 静默未达阈值时不得报 DM_OFF。"""
        self.assertIsNone(self._diag(None, quiet_phase=False))

    def test_dead_session(self):
        self.assertEqual(self._diag(_FakeDm(dead=True, retries=3))[:2],
                         ("WS_DEAD", "fault"))

    def test_light_parse_error_is_fault(self):
        """轻礼物到了却解析不了 = 真故障(proto 失配),必须推。"""
        self.assertEqual(self._diag(_FakeDm(light_errors=7))[:2],
                         ("LIGHT_GIFT_PARSE_ERR", "fault"))

    def test_light_write_error_is_fault(self):
        """解析成功但没写进 jsonl = 数据真丢了(磁盘/权限),与解析失败分开报。"""
        code, level, detail = self._diag(_FakeDm(light_write_errors=2))
        self.assertEqual((code, level), ("LIGHT_GIFT_WRITE_ERR", "fault"))
        self.assertIn("写盘失败", detail)

    def test_light_field_miss_is_suspect(self):
        """落盘了但字段缺项 → 数据在、不完整 → suspect。"""
        dm = _FakeDm(light_recv=10, light_kept=10, light_field_miss={"diamond": 3})
        code, level, detail = self._diag(dm)
        self.assertEqual((code, level), ("LIGHT_FIELD_MISS", "suspect"))
        self.assertIn("diamond", detail)

    def test_light_event_lost_is_suspect(self):
        """收到 10 条、落盘 8 条,既没解析失败也没写盘失败 → 账对不上。"""
        dm = _FakeDm(light_recv=10, light_kept=8)
        code, level, detail = self._diag(dm)
        self.assertEqual((code, level), ("LIGHT_EVENT_LOST", "suspect"))
        self.assertIn("差 2 条", detail)

    def test_full_channel_parse_error_is_not_health_issue(self):
        """完整通道解析失败不再是健康度问题(2026-10-29 起它不落盘、不进口径)。"""
        dm = _FakeDm(gift_errors=7, msg_count=150)
        self.assertNotEqual(self._diag(dm)[1], "fault")

    def test_integrity_reported_before_quiet_threshold(self):
        """完整性缺口必须在静默未达阈值时就报 —— 别等 45 分钟才发现 proto 变了。"""
        dm = _FakeDm(light_errors=1)
        self.assertEqual(self._diag(dm, quiet_phase=False)[:2],
                         ("LIGHT_GIFT_PARSE_ERR", "fault"))

    def test_no_msg_at_all(self):
        self.assertEqual(self._diag(_FakeDm(msg_count=0))[:2],
                         ("WS_NO_MSG", "fault"))

    def test_ws_silent(self):
        dm = _FakeDm(last_msg_ts=time.time() - 900)
        code, level, detail = self._diag(dm)
        self.assertEqual((code, level), ("WS_SILENT", "fault"))
        self.assertIn("没收到任何消息", detail)

    def test_busy_but_no_light_gift_is_quiet_channel_off(self):
        """弹幕热闹却零轻礼物 —— 349 场实测属"服务端未下发",只记录不推送。"""
        dm = _FakeDm(msg_count=150, methods={"WebcastChatMessage": 150})
        code, level, detail = self._diag(dm)
        self.assertEqual((code, level), ("LIGHT_CHAN_OFF", "quiet"))
        self.assertIn("平台侧", detail)
        self.assertFalse(monitor._gift_should_push({"level": level},
                                                  {"notify_level": "fault"}),
                         "quiet 级任何配置下都不该推手机")

    def test_cold_room_is_quiet(self):
        """直播间本来就冷清(0.6 条/分钟)→ 正常无人送礼,不该打扰。"""
        dm = _FakeDm(msg_count=30, methods={"WebcastChatMessage": 30})
        self.assertEqual(self._diag(dm)[:2], ("LIGHT_QUIET", "quiet"))

    def test_gift_absence_alone_is_not_evidence(self):
        """回归守卫:礼物类消息从未出现 ≠ 通道故障。

        抖音只在有人送礼时才推礼物消息,所以「没礼物消息」与「没人送礼」
        在这条判据上完全无法区分 —— 第一版就栽在这里,把所有冷门场次都判成
        可疑并推手机。判据必须是弹幕活跃度的对照,改回去这条会红。
        """
        dm = _FakeDm(msg_count=10, methods={"WebcastChatMessage": 10})
        self.assertNotEqual(self._diag(dm)[1], "suspect")

    def test_dead_wins_over_parse_error(self):
        """断连优先于解析错误:连接都没了,解析失败数是上一轮的旧账。"""
        dm = _FakeDm(dead=True, light_errors=5)
        self.assertEqual(self._diag(dm)[0], "WS_DEAD")


class TestGiftScan(unittest.TestCase):
    """扫描:阈值、基准重置、冷却、升级、落盘。"""

    def setUp(self):
        self._rec_backup = dict(monitor._recordings)
        self._alert_backup = dict(monitor._gift_alerts)
        monitor._recordings.clear()
        monitor._gift_alerts.clear()
        self.tmp = tempfile.mkdtemp(prefix="giftwatch-")

    def tearDown(self):
        monitor._recordings.clear()
        monitor._recordings.update(self._rec_backup)
        monitor._gift_alerts.clear()
        monitor._gift_alerts.update(self._alert_backup)

    def _add(self, name, started_ago, dm=None):
        monitor._recordings[name] = {
            "started_at": time.time() - started_ago,
            "output_path": os.path.join(self.tmp, name, "2026-10-10",
                                        f"{name}-20261010-000000-%03d.flv"),
            "stream_url": "", "format": "flv", "proc": None,
        }
        if dm is not None:
            dm.name = name
        return dm

    def _scan(self, dm_by_name, **gw):
        cfg = {"gift_watch": {"enabled": True, "silent_minutes": 45,
                              "cooldown_minutes": 60, **(gw or {})}}
        with mock.patch.object(monitor.danmaku_mod, "get",
                               side_effect=lambda n: dm_by_name.get(n)):
            return monitor._gift_scan(cfg)

    def test_below_threshold_is_silent(self):
        self._add("主播甲", 10 * 60)
        self.assertEqual(self._scan({}), [])

    def test_over_threshold_hits(self):
        self._add("主播甲", 50 * 60)
        hits = self._scan({"主播甲": _FakeDm(msg_count=30)})   # 冷清:0.6 条/分钟
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["code"], "LIGHT_QUIET")
        self.assertEqual(hits[0]["level"], "quiet")
        self.assertGreaterEqual(hits[0]["silent_min"], 49)

    def test_recent_gift_resets_the_clock(self):
        """5 分钟前刚收到过礼物 → 静默计时从那时起算,不该触发。"""
        self._add("主播甲", 50 * 60)
        dm = _FakeDm(last_gift_ts=time.time() - 5 * 60)
        self.assertEqual(self._scan({"主播甲": dm}), [])

    def test_cooldown_suppresses_same_verdict(self):
        self._add("主播甲", 50 * 60)
        dm = _FakeDm()
        self.assertEqual(len(self._scan({"主播甲": dm})), 1)
        self.assertEqual(self._scan({"主播甲": dm}), [], "同结论冷却期内不得重复报")

    def test_escalation_bypasses_cooldown(self):
        """结论升级(正常 → 故障)必须立刻再报,不能被冷却压住。"""
        self._add("主播甲", 50 * 60)
        dm = _FakeDm(msg_count=30)
        first = self._scan({"主播甲": dm})
        self.assertEqual(first[0]["level"], "quiet")
        dm.dead = True                      # 同一场,线路挂了
        again = self._scan({"主播甲": dm})
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["code"], "WS_DEAD")

    def test_diagnosis_is_recorded_to_disk(self):
        """每轮结论都要落盘 —— 排查靠的是记录,不是当时有没有收到推送。"""
        self._add("主播甲", 50 * 60)
        self._scan({"主播甲": _FakeDm(light_errors=3)})
        path = os.path.join(self.tmp, "主播甲", "2026-10-10", ".meta",
                            "主播甲-20261010-000000.giftwatch.jsonl")
        self.assertTrue(os.path.exists(path), "诊断快照没落盘")
        with open(path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["code"], "LIGHT_GIFT_PARSE_ERR")
        self.assertEqual(rec["snapshot"]["light"]["parse_errors"], 3,
                         "快照要带轻通道三段账,事后才能判断丢在哪一步")
        self.assertIn("method_top", rec["snapshot"])

    def test_sorted_by_severity(self):
        self._add("主播甲", 50 * 60)          # 健康,只是没人送礼
        self._add("主播乙", 50 * 60)          # 线路故障
        hits = self._scan({"主播甲": _FakeDm(msg_count=30),
                           "主播乙": _FakeDm(dead=True)})
        self.assertEqual([h["name"] for h in hits], ["主播乙", "主播甲"])


class TestGiftPushGate(unittest.TestCase):
    """分级闸门与合并推送。"""

    def test_suspect_level_pushes_fault_and_suspect_only(self):
        gw = {"notify_level": "suspect"}
        self.assertTrue(monitor._gift_should_push({"level": "fault"}, gw))
        self.assertTrue(monitor._gift_should_push({"level": "suspect"}, gw))
        self.assertFalse(monitor._gift_should_push({"level": "quiet"}, gw),
                         "线路健康只是没人送礼,不该推手机")

    def test_fault_level_is_quieter(self):
        gw = {"notify_level": "fault"}
        self.assertTrue(monitor._gift_should_push({"level": "fault"}, gw))
        self.assertFalse(monitor._gift_should_push({"level": "suspect"}, gw))

    def test_multi_streamer_merges_into_one_message(self):
        """多路同时告警合并成一条 —— 逐条推会刷屏也白吃额度。"""
        calls = []

        async def fake_push(title, body="", url="", local=True, wecom_cli=False):
            calls.append({"title": title, "body": body, "url": url, "wecom_cli": wecom_cli})
            return {"ok": True, "channels": ["wecom"], "error": ""}

        items = [{"name": "主播甲", "code": "WS_DEAD", "level": "fault",
                  "detail": "连接已放弃", "silent_min": 50.0},
                 {"name": "主播乙", "code": "NO_GIFT_METHOD", "level": "suspect",
                  "detail": "没有礼物类消息", "silent_min": 46.0}]
        with mock.patch.object(monitor.notify_mod, "push_async", new=fake_push):
            asyncio.run(monitor._gift_push(items, {"webhook_url": "https://example.invalid/hook"}))
        self.assertEqual(len(calls), 1, "必须合并成一条")
        self.assertIn("2 路", calls[0]["title"])
        self.assertIn("主播甲", calls[0]["body"])
        self.assertIn("WS_DEAD", calls[0]["body"])
        self.assertIn("giftwatch.jsonl", calls[0]["body"], "推送里要指明排查记录在哪")


class TestChannelDispatch(unittest.TestCase):
    """notify_channel 分发:auto 优先 webhook、缺省回落连接器;强制档互不干扰。"""

    ITEMS = [{"name": "主播甲", "code": "WS_DEAD", "level": "fault",
              "detail": "d", "silent_min": 50.0}]
    HOOK = "https://example.invalid/hook"

    def _push(self, gw):
        calls = []

        async def fake_push(title, body="", url="", local=True, wecom_cli=False):
            calls.append({"url": url, "wecom_cli": wecom_cli})
            return {"ok": True, "channels": [], "error": ""}

        with mock.patch.object(monitor.notify_mod, "push_async", new=fake_push):
            asyncio.run(monitor._gift_push(self.ITEMS, gw))
        return calls[0]

    def test_auto_without_webhook_falls_back_to_cli(self):
        """没配 webhook 时 auto 走连接器 —— 无需建群机器人也能收到。"""
        r = self._push({"notify_channel": "auto", "webhook_url": ""})
        self.assertTrue(r["wecom_cli"])
        self.assertEqual(r["url"], "")

    def test_auto_prefers_webhook_when_configured(self):
        r = self._push({"notify_channel": "auto", "webhook_url": self.HOOK})
        self.assertFalse(r["wecom_cli"], "auto 下配了 webhook 就不再走连接器,避免双重打扰")
        self.assertEqual(r["url"], self.HOOK)

    def test_forced_cli_ignores_webhook(self):
        r = self._push({"notify_channel": "wecom_cli", "webhook_url": self.HOOK})
        self.assertTrue(r["wecom_cli"])
        self.assertEqual(r["url"], "")

    def test_forced_webhook_ignores_cli(self):
        r = self._push({"notify_channel": "webhook", "webhook_url": self.HOOK})
        self.assertFalse(r["wecom_cli"])
        self.assertEqual(r["url"], self.HOOK)


class TestWecomCliChannel(unittest.TestCase):
    """连接器通道:whoami 解析 / 未授权降级 / send 结果判定。"""

    WHOAMI = json.dumps({
        "security_notice": "<security_notice>…</security_notice>",
        "extra_identity_context": "机器人身份：\n名字：某机器人\nID：aibotXYZ\n"
                                  "授权真人用户身份：\n名字：甲\nID：woREAL123\n"
                                  "CLI 调用一定由你的机器人身份代用户执行。",
    }, ensure_ascii=False)

    def test_extracts_authorized_id_not_bot_id(self):
        """必须锚定「授权真人用户身份」—— 拿错会把告警发给机器人自己(无声丢失)。"""
        with mock.patch.object(notify, "_find_wecom_cli", return_value="/fake/cli"), \
             mock.patch("notify.subprocess.run") as mr:
            mr.return_value = mock.Mock(stdout=self.WHOAMI, returncode=0)
            uid = notify._authorized_id("/fake/cli")
        self.assertEqual(uid, "woREAL123")

    def test_send_ok_and_payload_shape(self):
        with mock.patch.object(notify, "_find_wecom_cli", return_value="/fake/cli"), \
             mock.patch("notify.subprocess.run") as mr:
            mr.side_effect = [
                mock.Mock(stdout=self.WHOAMI, returncode=0),
                mock.Mock(stdout='{"success": true}', returncode=0),
            ]
            ok, err = notify.wecom_cli_notify("标题\n正文")
        self.assertTrue(ok, err)
        self.assertEqual(err, "")
        cmd = mr.call_args_list[1][0][0]
        payload = json.loads(cmd[cmd.index("--json") + 1])
        self.assertEqual(payload["chat_id"], "woREAL123")
        self.assertEqual(payload["msg_type"], "markdown")
        self.assertIn("正文", payload["markdown"]["content"])

    def test_unauthorized_degrades_without_raise(self):
        with mock.patch.object(notify, "_find_wecom_cli", return_value="/fake/cli"), \
             mock.patch("notify.subprocess.run") as mr:
            mr.return_value = mock.Mock(stdout="{}", returncode=0)
            ok, err = notify.wecom_cli_notify("x")
        self.assertFalse(ok)
        self.assertIn("未授权", err)

    def test_missing_cli_degrades(self):
        with mock.patch.object(notify, "_find_wecom_cli", return_value=""):
            ok, err = notify.wecom_cli_notify("x")
        self.assertFalse(ok)
        self.assertIn("未找到", err)


class TestNotifyChannel(unittest.TestCase):
    """企微通道:payload 格式、长度截断、无地址时不算失败。"""

    def test_wecom_payload_shape(self):
        p = notify.wecom_payload("标题\n正文")
        self.assertEqual(p["msgtype"], "text")          # 企微是 msgtype 连写
        self.assertIn("正文", p["text"]["content"])

    def test_overlong_message_is_clipped(self):
        p = notify.wecom_payload("啊" * 5000)           # 中文 3 字节,最容易超
        self.assertLessEqual(len(p["text"]["content"].encode("utf-8")), notify.MAX_BYTES)
        self.assertTrue(p["text"]["content"].endswith("…(已截断)"))

    def test_no_url_is_not_a_failure(self):
        """没配 webhook 只记日志,不能算推送失败(否则面板会飘红)。"""
        r = asyncio.run(notify.push_async("标题", "正文", url="", local=False))
        self.assertTrue(r["ok"])
        self.assertEqual(r["channels"], [])


class TestGiftMetering(unittest.TestCase):
    """弹幕侧礼物计量:只有轻通道算数(2026-10-29 起 light-only 口径)。"""

    def test_note_gift_updates_clock_and_count(self):
        r = danmaku.DanmakuRecorder("主播甲", "123456", "/tmp/never-opened.jsonl")
        self.assertEqual(r.last_gift_ts, 0.0)
        self.assertEqual(r.gift_count, 0)
        r._note_gift()
        self.assertGreater(r.last_gift_ts, 0)
        self.assertEqual(r.gift_count, 1)

    def test_only_light_channel_registers_gift(self):
        """完整通道事件不再重置静默计时:它不落盘,登记了就会出现
        「jsonl 里一条礼物都没有、静默告警却永远不触发」的自欺状态。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "danmaku.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("self._note_gift()"), 1,
                         "只有 WebcastLightGiftMessage 分支登记礼物")

    def test_full_channel_events_are_counted_but_not_written(self):
        """完整通道只计数 + 日志,不落 jsonl(与轻通道互斥,同收会重复计)。"""
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "danmaku.py"), encoding="utf-8") as f:
            src = f.read()
        seg = src.split('elif method == "WebcastGiftMessage":')[1].split(
            'elif method == "WebcastLightGiftMessage":')[0]
        self.assertIn("self.gift_full_count += 1", seg)
        self.assertNotIn("self._write(", seg,
                         "完整通道事件不得写入 jsonl —— 口径只认轻通道")


if __name__ == "__main__":
    unittest.main()


class TestGiftwatchRecords(unittest.TestCase):
    """webui._giftwatch_records:诊断记录汇总(扫描 / 排序 / 白名单)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gwapi-")
        self.cfg = {"recorder": {"output_dir": self.tmp},
                    "nas": {"enabled": False}, "archive": {}}

    def _write(self, streamer, date, stem, rows):
        import os
        d = os.path.join(self.tmp, streamer, date, ".meta")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{stem}.giftwatch.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)

    def test_scans_and_sorts_desc(self):
        self._write("主播甲", "2026-10-10", "主播甲-20261010-010000", [
            {"code": "HEALTHY_NO_GIFT", "level": "quiet", "at": 100.0, "name": "主播甲"},
            {"code": "WS_DEAD", "level": "fault", "at": 300.0, "name": "主播甲"},
        ])
        self._write("主播乙", "2026-10-10", "主播乙-20261010-020000", [
            {"code": "NO_GIFT_WHILE_BUSY", "level": "suspect", "at": 200.0, "name": "主播乙"},
        ])
        r = webui._giftwatch_records(self.cfg)
        self.assertEqual(r["total"], 3)
        self.assertEqual([x["code"] for x in r["records"]],
                         ["WS_DEAD", "NO_GIFT_WHILE_BUSY", "HEALTHY_NO_GIFT"],
                         "必须按时间倒序,最新的故障排最前")
        self.assertEqual(r["records"][0]["session"], "主播甲-20261010-010000",
                         "at=300 的记录属于主播甲的场次")

    def test_limit_keeps_newest(self):
        for i in range(5):
            self._write("主播甲", "2026-10-10", f"主播甲-20261010-01000{i}",
                        [{"code": "WS_DEAD", "level": "fault", "at": float(i), "name": "主播甲"}])
        r = webui._giftwatch_records(self.cfg, limit=3)
        self.assertEqual(len(r["records"]), 3)
        self.assertEqual(r["records"][0]["at"], 4.0, "截断也要保住最新的")

    def test_only_scans_allowed_roots(self):
        """NAS 未启用时,归档盘上的同名文件不得混进来(扫描范围=白名单)。"""
        self._write("主播甲", "2026-10-10", "主播甲-20261010-010000",
                    [{"code": "WS_DEAD", "level": "fault", "at": 1.0, "name": "主播甲"}])
        outside = os.path.join(self.tmp, "..", "gwapi-outside-{}".format(id(self)))
        outside = os.path.abspath(outside)
        d = os.path.join(outside, "主播乙", "2026-10-10", ".meta")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "主播乙-x.giftwatch.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"code": "DM_OFF", "level": "fault", "at": 9.0,
                                "name": "主播乙"}, ensure_ascii=False) + "\n")
        try:
            self.assertEqual(webui._giftwatch_records(self.cfg)["total"], 1)
        finally:
            import shutil as _sh
            _sh.rmtree(outside, ignore_errors=True)
