# -*- coding: utf-8 -*-
"""崩溃/故障事件流(incidents.py)与接入点的守卫。

这一组钉住的是 2026-10-29 那次事故的**可观测性**修复:
8 天 86 次录制崩溃里,ffmpeg 每次都在同一秒被拉起来 —— 文件没停、心跳没断、
`/api/health` 全程 ok,页面上连一个像素都不变。事件流是唯一能留下痕迹的地方,
所以它的每一处行为(合并、落盘、重启识别、重新变未读)都必须被钉死。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import incidents  # noqa: E402


class _TmpCase(unittest.TestCase):
    """每个用例独立的落盘目录 —— incidents 是模块级单例,不隔离会互相污染。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "incidents.json")
        incidents.configure(self.path)
        # ⚠️ 顺序要紧:addCleanup 是后进先出。先注册删目录、后注册改路径,
        # 退出时就变成「先把路径挪到临时目录内(已删)→ 再删目录」,
        # 之后任何测试再 record 都只会 OSError(静默),**不会写进生产的
        # state/incidents.json**。曾直接把路径指回 DEFAULT_PATH,结果一次全量
        # 测试就把「主播甲/主播A 崩了 9 次」这类假事件写进了真实状态文件。
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(incidents.configure,
                        os.path.join(self._tmp.name, "_discarded.json"))

    def disk(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)


class TestNoProductionPollution(unittest.TestCase):
    """★ 整个测试进程的事件表必须落在临时目录(由 `tests/__init__.py` 重定向)。

    2026-10-30 踩过:`test_gift_cross_session` 直接调 `monitor._restart_recording`
    (不带 reason)、`test_record_health`/`test_junk_segments` 直接调 `_give_up`,
    而它们字母序排在 `test_incidents` **之前** —— 那时 incidents 还指向默认路径,
    一次全量测试就把「主播A 录制中断并重启」写进了真实的 `state/incidents.json`,
    界面上真的出现了这条假告警。

    ⚠️ 这个类**故意不继承 `_TmpCase`**:那会把路径改成本例自己的临时目录,
    就再也测不出「包级重定向是否还在」了 —— 守卫必须站在"别的用例都跑过之后"
    的位置上,才有意义。
    """

    def test_package_level_redirect_is_active(self):
        self.assertNotEqual(
            os.path.abspath(incidents._PATH),
            os.path.abspath(incidents.DEFAULT_PATH),
            "测试期间事件表不许指向生产文件 state/incidents.json —— "
            "检查 tests/__init__.py 的重定向是否还在,以及各用例 cleanup 有没有把它改回去")


class TestRecordAndMerge(_TmpCase):
    def test_record_appends_and_summarises(self):
        incidents.begin_session()
        incidents.record("record.crash", "主播甲 录制中断并重启", detail="归因 原因未知",
                         name="主播甲")
        s = incidents.summary()
        self.assertEqual(s["events"], 1)
        self.assertEqual(s["error"], 1)
        self.assertEqual(s["unread"], 1)
        self.assertEqual(s["unread_error"], 1)
        self.assertEqual(s["recent_24h"], 1)
        ev = incidents.recent()[0]
        self.assertEqual(ev["label"], "录制崩溃")
        self.assertFalse(ev["read"])

    def test_same_key_merges_inside_window(self):
        """★ 同主播连续崩(20s 一次)只占一条,count 累加、标题取最新。

        没有合并,一次流地址失效能在几分钟里刷出几十条,把真正的问题埋掉。
        """
        incidents.begin_session()
        incidents.record("record.crash", "主播甲 崩(进程退出)", name="主播甲", now=1000.0)
        incidents.record("record.crash", "主播甲 崩(数据停滞)", name="主播甲", now=1010.0)
        evs = incidents.recent()
        self.assertEqual(len(evs), 1, "同类同主体必须合并成一条")
        self.assertEqual(evs[0]["count"], 2)
        self.assertIn("数据停滞", evs[0]["title"], "标题要跟最新一次走")
        self.assertLessEqual(evs[0]["first_ts"], evs[0]["ts"])

    def test_different_names_do_not_merge(self):
        incidents.begin_session()
        incidents.record("record.crash", "甲崩", name="主播甲", now=1000.0)
        incidents.record("record.crash", "乙崩", name="主播乙", now=1001.0)
        self.assertEqual(len(incidents.recent()), 2)

    def test_different_kinds_do_not_merge(self):
        incidents.begin_session()
        incidents.record("record.crash", "崩", name="主播甲", now=1000.0)
        incidents.record("record.giveup", "放弃", name="主播甲", now=1001.0)
        self.assertEqual(len(incidents.recent()), 2)

    def test_merge_respects_window(self):
        """★ 窗口外的旧事件不许被复活 —— 否则早上的崩溃会一直挂在最上面。"""
        incidents.begin_session()
        incidents.record("record.crash", "早上崩的", name="主播甲", now=1000.0)
        incidents.record("record.crash", "下午崩的", name="主播甲",
                         now=1000.0 + incidents.MERGE_WINDOW + 1)
        self.assertEqual(len(incidents.recent()), 2)

    def test_merge_marks_unread_again(self):
        """★ 已读之后又崩一次,必须重新变未读 —— 不能因为"点过已读"就沉默。"""
        incidents.begin_session()
        incidents.record("record.crash", "崩了", name="主播甲", now=1000.0)
        incidents.dismiss()
        self.assertEqual(incidents.summary()["unread"], 0)
        incidents.record("record.crash", "又崩了", name="主播甲", now=1010.0)
        self.assertEqual(incidents.summary()["unread"], 1)
        self.assertFalse(incidents.recent()[0]["read"])

    def test_ring_buffer_cap(self):
        incidents.begin_session()
        for i in range(incidents.MAX_EVENTS + 25):
            incidents.record("record.crash", "崩 %d" % i, name="主播%d" % i, now=1000.0 + i)
        self.assertEqual(len(incidents.recent(limit=9999)), incidents.MAX_EVENTS)
        # 新的留下,老的被挤掉
        self.assertIn(str(incidents.MAX_EVENTS + 24), incidents.recent()[0]["title"])

    def test_recent_newest_first_and_limit(self):
        incidents.begin_session()
        for i in range(5):
            incidents.record("record.crash", "崩 %d" % i, name="主播%d" % i, now=1000.0 + i)
        evs = incidents.recent(limit=3)
        self.assertEqual(len(evs), 3)
        self.assertIn("崩 4", evs[0]["title"])


class TestPersistence(_TmpCase):
    def test_events_survive_reload(self):
        incidents.begin_session()
        incidents.record("record.giveup", "主播甲 放弃本场", name="主播甲")
        incidents.configure(self.path)          # 模拟重启后重新载入
        evs = incidents.recent()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["title"], "主播甲 放弃本场")

    def test_crash_exit_detected_on_next_start(self):
        """★ 主进程崩着退出(没走 end_session),下次启动必须补一条 process.crash。

        这是「主进程崩溃」唯一的痕迹 —— 它崩溃后 launchd 不会拉起(本机没装载),
        日志里也只会少一行,没有任何人会注意到。
        """
        incidents.begin_session()               # 这一次会写 clean_exit=False
        incidents.record("record.crash", "崩了一路", name="主播甲")
        incidents.configure(self.path)          # 模拟进程重启(不调 end_session)
        self.assertFalse(self.disk().get("clean_exit"), "崩着退出必须留下 False")
        incidents.begin_session()
        kinds = [e["kind"] for e in incidents.recent()]
        self.assertIn("process.crash", kinds)
        ev = [e for e in incidents.recent() if e["kind"] == "process.crash"][0]
        self.assertEqual(ev["level"], incidents.LEVEL_ERROR)

    def test_clean_exit_does_not_report_crash(self):
        incidents.begin_session()
        incidents.end_session()
        self.assertTrue(self.disk().get("clean_exit"))
        incidents.configure(self.path)
        incidents.begin_session()
        kinds = [e["kind"] for e in incidents.recent()]
        self.assertNotIn("process.crash", kinds, "正常退出不许报异常退出")

    def test_first_run_never_reports_crash(self):
        """首次运行(文件不存在)不许报异常退出 —— 否则每次全新部署都先挨一条假告警。"""
        incidents.begin_session()
        self.assertEqual(incidents.recent(), [])

    def test_note_start_is_info_and_merges(self):
        """★ 「服务启动」要留痕(用户要求任何一处重启都看得见),但不能标红。

        主进程重启会打断全部录制,是排查时的重要时间锚点;但它不是故障,
        所以 level=info(只进列表,不把导航条变红),且 5 分钟内反复重启合并成一条。
        """
        incidents.begin_session()
        incidents.note_start("PID 1234", now=1000.0)
        incidents.note_start("PID 5678", now=1010.0)
        evs = [e for e in incidents.recent() if e["kind"] == "process.start"]
        self.assertEqual(len(evs), 1, "短时间连续重启必须合并")
        self.assertEqual(evs[0]["count"], 2)
        self.assertEqual(evs[0]["level"], incidents.LEVEL_INFO)
        self.assertEqual(incidents.summary()["error"], 0, "启动不许算成 error")

    def test_corrupt_file_does_not_break_startup(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        incidents.configure(self.path)
        incidents.begin_session()               # 不该抛
        self.assertIsInstance(incidents.recent(), list)

    def test_save_is_atomic_and_leaves_no_tmp(self):
        incidents.begin_session()
        incidents.record("record.crash", "崩", name="主播甲")
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        self.assertIn("events", self.disk())


class TestDismiss(_TmpCase):
    def test_dismiss_all(self):
        incidents.begin_session()
        for i in range(3):
            incidents.record("record.crash", "崩 %d" % i, name="主播%d" % i, now=1000.0 + i)
        self.assertEqual(incidents.dismiss(), 3)
        self.assertEqual(incidents.summary()["unread"], 0)
        self.assertEqual(len(incidents.recent()), 3, "已读只是标记,条目必须保留")

    def test_dismiss_selected(self):
        incidents.begin_session()
        incidents.record("record.crash", "崩 0", name="主播0", now=1000.0)
        incidents.record("record.crash", "崩 1", name="主播1", now=1001.0)
        target = incidents.recent()[0]["id"]
        self.assertEqual(incidents.dismiss([target]), 1)
        self.assertEqual(incidents.summary()["unread"], 1)

    def test_dismiss_unknown_id_is_noop(self):
        incidents.begin_session()
        incidents.record("record.crash", "崩", name="主播甲")
        self.assertEqual(incidents.dismiss(["不存在"]), 0)


class TestSnapshot(_TmpCase):
    def test_snapshot_shape(self):
        incidents.begin_session()
        incidents.record("record.crash", "崩", name="主播甲")
        snap = incidents.snapshot()
        for k in ("summary", "events", "labels", "generated_at"):
            self.assertIn(k, snap)
        self.assertIn("record.crash", snap["labels"])
        json.dumps(snap, ensure_ascii=False)     # 必须可序列化(接口直接下发)

    def test_snapshot_keeps_read_events_with_flag(self):
        """★ 已读条目必须留在 events 里并带 read:true。

        界面靠这个字段把它们收进「已读」抽屉;若有人图省事改成
        `recent(unread_only=True)` 让已读从接口里彻底消失,崩溃历史就没了
        (事件表存在的意义就是事后回看)。正确做法是**前端按 read 过滤**。
        """
        incidents.begin_session()
        incidents.record("record.crash", "崩", name="主播甲")
        incidents.dismiss()
        evs = incidents.snapshot()["events"]
        self.assertEqual(len(evs), 1, "已读不许从接口里消失")
        self.assertTrue(evs[0]["read"], "已读条目必须带 read 标记供前端过滤")


class TestIncidentBarReadUI(unittest.TestCase):
    """★ 已读必须在界面上真的消失(2026-10-31 用户报障)。

    现象:「点过已读的告警并没有消失,别的报错一产生,之前已读的又全出来了」。
    根因在**渲染层**,不在数据层 —— `dismiss()` 确实把 read 置 True 并落盘
    (`state/incidents.json` 里 True/False 并存就是证据),但 `renderIncidents()`
    把 `recent()` 返回的**全量**事件逐条渲染,已读与未读既没有样式差异、
    也没有过滤。于是 20s 轮询或任何一次重渲染都把同一批文字原样刷一遍。

    三个坑都得钉住:
      ① 列表必须把未读/已读分开,已读默认**不渲染**(收进抽屉);
      ② 展开已读时必须带 `is-read`(降透明),一眼能分出这是回看;
      ③ 点「全部已读」要**就地重绘**(乐观更新)—— 旧版等接口回来再拉一次,
         而那次拉回来的还是同一堆字,视觉上等于没点。
    """

    def setUp(self):
        with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def seg(self, start, end):
        """截取 index.html 里从 start 到 end 的那一段(守卫只对这一段负责,
        全文断言会被别处同名写法误伤 —— `evs.forEach` 在历史页也有)。"""
        i = self.html.index(start)
        return self.html[i:self.html.index(end, i)]

    def test_read_events_are_filtered_out(self):
        body = self.seg("function renderIncidents(d)", "async function loadIncidents")
        self.assertIn("const unread = evs.filter(e => !e.read);", body,
                      "必须按 read 拆成未读/已读两份")
        self.assertIn("const readEvs = evs.filter(e => e.read);", body)
        self.assertNotIn("evs.forEach(e =>", body,
                         "对 evs 全量 forEach 就是本次 bug 的原始写法"
                         "(已读跟着未读一起刷出来)")
        self.assertIn("if (_incidentShowRead) readEvs.forEach", body,
                      "已读只能在抽屉展开时才渲染")

    def test_read_rows_are_dimmed(self):
        self.assertIn("_incidentRow(e, false)", self.html, "未读行渲染入口缺失")
        self.assertIn("_incidentRow(e, true)", self.html, "已读行渲染入口缺失")
        self.assertIn("isRead ? \" is-read\"", self.html, "已读行没加样式类")
        self.assertIn(".incident-row.is-read {", self.html, "已读行样式缺失(会被当成未读看)")

    def test_read_drawer_toggle(self):
        self.assertIn('id="incident-readtoggle"', self.html, "已读抽屉开关缺失")
        self.assertIn("已读 ${readEvs.length} 条", self.html, "抽屉入口必须写明已读条数")
        self.assertIn("_incidentShowRead = !_incidentShowRead;", self.html,
                      "抽屉点击没有切换状态")

    def test_dismiss_repaints_immediately(self):
        """★ dismiss 必须本地先置读并重绘,不能等下一轮轮询。"""
        seg = self.html[self.html.index("async function dismissIncidents"):]
        seg = seg[:seg.index("const _incHead")]
        self.assertIn("e.read = true", seg, "dismiss 没有本地置读")
        self.assertIn("local.summary.unread = 0", seg, "dismiss 没有把未读计数清零")
        self.assertIn("renderIncidents(local)", seg,
                      "dismiss 没有立刻重绘(用户点完仍然一片旧内容)")
        self.assertLess(seg.index("renderIncidents(local)"),
                        seg.index("/api/incidents/dismiss"),
                        "必须先重绘再打接口,否则点完到响应之间界面毫无反应")

    def test_all_read_button_only_when_unread(self):
        """没有未读时不许出现「全部已读」—— 剩下的红是 health 的当前故障。"""
        self.assertIn('unread.length ? `<button id="incident-read">', self.html)

    def test_expand_refetches(self):
        """展开要补拉一次:手里那份最多 20s 旧,用它渲染等于「新崩溃不显示、旧告警回来」。"""
        seg = self.html[self.html.index("const _incHead = document.getElementById"):]
        seg = seg[:seg.index("let healthTimer")]
        self.assertIn("if (_incidentOpen) loadIncidents();", seg,
                      "展开时没有补拉最新数据")


class TestWiring(unittest.TestCase):
    """接入点守卫:崩溃点必须真的往事件流里写。

    这些是「只有事件流能看见」的地方,少接一处就等于那类崩溃继续隐形。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        incidents.configure(os.path.join(self._tmp.name, "incidents.json"))
        # 同 _TmpCase:退出时把路径挪进已删的目录,绝不让后续测试写进生产文件
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(incidents.configure,
                        os.path.join(self._tmp.name, "_discarded.json"))
        incidents.begin_session()

    def test_restart_recording_writes_crash_event(self):
        """★ 本地资源类崩溃(count_it=True)必须写 record.crash 且标红。

        2026-10-31 起流侧退出不再走这里(见下面那条用例):404 / 流被切断是主播
        下播或推流中断,不是我们的故障,标红等于把每天的正常下播都刷成崩溃。
        """
        import monitor
        with mock.patch.object(monitor, "_start_recording", return_value=None), \
             mock.patch.object(monitor, "_history_finish"), \
             mock.patch.object(monitor, "_carry_segments"), \
             mock.patch.object(monitor, "_exit_cause",
                               return_value=monitor.quality_mod.CRASH_RESOURCE), \
             mock.patch.object(monitor, "_quality_gov", None), \
             mock.patch.object(monitor, "log"):
            monitor._rec_health.pop("主播甲", None)
            monitor._restart_recording(None, "主播甲", "http://example/s.flv", {},
                                       None, reason="进程退出")
        evs = [e for e in incidents.recent() if e["kind"] == "record.crash"]
        self.assertEqual(len(evs), 1)
        self.assertIn("主播甲", evs[0]["title"])
        self.assertIn("本地资源", evs[0]["detail"], "退出归因要带进事件里")
        self.assertEqual(evs[0]["level"], incidents.LEVEL_ERROR)

    def test_stream_side_exit_is_info_not_error(self):
        """★ 流侧退出要留痕,但**不能**标红 —— 它就是主播下播/推流中断。

        实测(8 天):404 之后抖音中位 24 秒、典型 67 秒就把状态翻成「已结束」。
        退出当下分不清是下播还是断流,按 error 记就会天天误报。
        """
        import monitor
        with mock.patch.object(monitor, "_start_recording", return_value=None), \
             mock.patch.object(monitor, "_history_finish"), \
             mock.patch.object(monitor, "_carry_segments"), \
             mock.patch.object(monitor, "_exit_cause",
                               return_value=monitor.quality_mod.CRASH_EXTERNAL), \
             mock.patch.object(monitor, "_quality_gov", None), \
             mock.patch.object(monitor, "log"):
            monitor._rec_health.pop("主播甲", None)
            monitor._restart_recording(None, "主播甲", "http://example/s.flv", {},
                                       None, reason="进程退出")
        self.assertEqual([e for e in incidents.recent()
                          if e["kind"] == "record.crash"], [])
        evs = [e for e in incidents.recent() if e["kind"] == "record.streamend"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], incidents.LEVEL_INFO)
        self.assertIn("流侧故障", evs[0]["detail"])

    def test_non_fault_restart_is_logged_as_info_not_error(self):
        """★ 主动重启(换房间)也要留痕,但**不能**标成崩溃。

        用户要求「任何一处重启都要提示」,所以 `count_it=False` 的重启同样进事件流;
        但它是正常生命周期(主播换了新的一场),标红就等于天天误报。
        """
        import monitor
        with mock.patch.object(monitor, "_start_recording", return_value=None), \
             mock.patch.object(monitor, "_history_finish"), \
             mock.patch.object(monitor, "_carry_segments"), \
             mock.patch.object(monitor, "_quality_gov", None), \
             mock.patch.object(monitor, "log"):
            monitor._rec_health.pop("主播甲", None)
            monitor._restart_recording(None, "主播甲", "http://example/s.flv", {},
                                       None, reason="换房间", count_it=False)
        evs = [e for e in incidents.recent() if e["kind"] == "record.restart"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], incidents.LEVEL_INFO)
        self.assertEqual(incidents.summary()["error"], 0, "换房间不许算成崩溃")

    def test_retier_restart_is_warn(self):
        """档位切换记 warn:那是画质调度在自我保护,说明机器有压力,值得看一眼。"""
        import monitor
        with mock.patch.object(monitor, "_start_recording", return_value=None), \
             mock.patch.object(monitor, "_history_finish"), \
             mock.patch.object(monitor, "_carry_segments"), \
             mock.patch.object(monitor, "_quality_gov", None), \
             mock.patch.object(monitor, "log"):
            monitor._rec_health.pop("主播甲", None)
            monitor._restart_recording(None, "主播甲", "http://example/s.flv", {},
                                       None, reason="档位切换", count_it=False)
        evs = [e for e in incidents.recent() if e["kind"] == "record.restart"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], incidents.LEVEL_WARN)

    def test_give_up_writes_event(self):
        """★ 放弃本场是最严重的一类(这一场后续不录了),必须写。"""
        import monitor
        with mock.patch.object(monitor, "_stop_recording"), \
             mock.patch.object(monitor, "_discard_junk_segments"), \
             mock.patch.object(monitor, "log"):
            monitor._rec_giveup_until.pop("主播乙", None)
            monitor._rec_giveup_reason.pop("主播乙", None)
            monitor._give_up(None, "主播乙", 600, "ffmpeg 反复退出,已重启 4 次")
        evs = [e for e in incidents.recent() if e["kind"] == "record.giveup"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["level"], incidents.LEVEL_ERROR)
        self.assertIn("4 次", evs[0]["title"])

    def test_webui_merges_health_faults(self):
        """★ 协程卡死/Cookie/NAS 这些"没有崩溃事件"的故障必须从 health 并进来。

        少了这一步,告警条只能看见录制崩溃,「整个项目哪里出问题」就不成立。
        """
        import webui
        snap = {
            "groups": [
                {"name": "进程", "items": [
                    {"key": "main", "name": "监控主进程", "status": "ok",
                     "detail": "正常", "count": None, "age_s": 1},
                    {"key": "ffmpeg", "name": "录制进程", "status": "warn",
                     "detail": "2 路无数据", "count": 2, "age_s": 3}]},
                {"name": "常驻任务", "items": [
                    {"key": "danmaku", "name": "弹幕/字幕巡检", "status": "down",
                     "detail": "心跳超时", "count": None, "age_s": 900}]},
            ],
            "counts": {"ok": 1, "warn": 1, "down": 1, "idle": 0},
            "ok": False, "generated_at": 1.0,
        }
        fake_state = mock.Mock()
        fake_state.get_config.return_value = {}
        with mock.patch.object(webui.health_mod, "snapshot", return_value=snap), \
             mock.patch.object(webui, "_health_overrides", return_value={}):
            webui._INCIDENT_HEALTH["at"] = 0.0    # 绕开 10s 缓存
            webui._INCIDENT_HEALTH["data"] = None
            got = webui._incident_health_faults(fake_state)
        self.assertEqual([x["key"] for x in got["items"]], ["danmaku", "ffmpeg"],
                         "down 必须排在 warn 前面")
        self.assertEqual(got["counts"]["down"], 1)
        self.assertFalse(got["ok"])

    def test_webui_health_faults_resilient(self):
        """health 算炸了也不许把告警条带崩(它只是旁路)。"""
        import webui
        fake_state = mock.Mock()
        fake_state.get_config.return_value = {}
        with mock.patch.object(webui.health_mod, "snapshot",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(webui, "_health_overrides", return_value={}):
            webui._INCIDENT_HEALTH["at"] = 0.0
            webui._INCIDENT_HEALTH["data"] = None
            got = webui._incident_health_faults(fake_state)
        self.assertEqual(got["items"], [])
        self.assertIn("boom", got.get("error", ""))


if __name__ == "__main__":
    unittest.main()
