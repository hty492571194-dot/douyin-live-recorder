#!/usr/bin/env python3
"""/api/history 分页 + 探测预算 + 惰性补算收敛 的守卫测试。

背景(2026-10-31 实测):用户反馈「录制历史每次点击都要转很久」。实测
`/api/history` 两次分别 165 s / 194 s,TTFB 就是全部耗时、传输只占 0.01 s。
用 py-spy 的替代手段(sample + lsof)跟了一整轮,定位到四个叠加原因:

  1. 每轮对**全部** 500 条历史逐条判文件存在性。498 条已归档、全在 SMB 上,
     去重后 204 个目录;macOS smbfs 单会话串行,真冷约 2 目录/秒 —— 这个
     环节本人就 100~200 秒。代码注释里「并发 6 实测 1~2 秒」是被内核目录
     缓存骗了:缓存热时确实是毫秒,冷却时并发几乎不加速。
  2. `_DIR_TTL = 60 s` 小于单轮耗时,且预热按「旧→新」、补算按「新→旧」,
     方向相反 ⇒ 扫到后面前面已过期,同一批目录一轮里被扫了不止一遍。
  3. `_backfill_gift_fields` 的额度只在**补成功**时递增,而补不上的条目
     (519 条缺礼物字段,对应 .jsonl 全部不存在)永远不计数 ⇒ 每轮全量重试,
     永不收敛。
  4. 前端 20 秒超时 < 后端 194 秒 ⇒ 每轮必然丢弃重来,用户永远看不到结果。

修复:分页(只探当页)+ 条目级缓存 + 探测预算硬上限 + 老值先返回
(stale-while-revalidate)+ 补算「尝试即计数」与 `_*_absent` 标记。

本文件守住这几条,防止有人（包括未来的我）把「全量探测」加回来。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402

DAY = "2026-08-29"
STEM = "主播A-20260829-120000"


def _entry(idx, root, archived=False, dest=None):
    """造一条历史。idx 用来区分不同场次(不同日期目录)。"""
    day = "2026-08-%02d" % (1 + idx % 28)
    out = os.path.join(root, "主播A", day, "%s-%02d-%%03d.flv" % (STEM, idx))
    e = {"name": "主播A", "status": "done", "started_at": 1000.0 + idx,
         "ended_at": 2000.0 + idx, "duration": 100, "size": 1024,
         "segments": 1, "media": {"w": 1080, "h": 1920, "kbps": 1200},
         "output_path": out, "archive": {"state": "none", "dest": None}}
    if archived:
        e["archive"] = {"state": "archived", "target": "nas", "dest": dest}
    return e


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = self.tmp.name
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": False, "mount_point": "", "root_dir": "",
                    "share": "", "host": ""},
            "archive": {"mode": "auto", "external_dir": ""},
        }
        self._saved = {
            "_DIR_TTL": webui._DIR_TTL,
            "_HIST_TTL": webui._HIST_TTL,
            "_HIST_SWR": webui._HIST_SWR,
            "_HIST_PROBE_BUDGET": webui._HIST_PROBE_BUDGET,
            "_ABSENT_RETRY": webui._ABSENT_RETRY,
        }
        webui._hist_cache_clear()
        webui._dir_cache_clear()
        self.probes = []          # 真实 scandir 过的目录

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(webui, k, v)
        webui._hist_cache_clear()
        webui._dir_cache_clear()
        self.tmp.cleanup()

    def _state(self, hist):
        outer = self

        class _St:
            def __init__(self):
                self.hist = hist
                self.config_reads = 0

            def get_config(self):
                self.config_reads += 1
                return json.loads(json.dumps(outer.cfg))

            def get_history(self):
                return json.loads(json.dumps(self.hist))

            def set_history(self, entries):
                self.hist = json.loads(json.dumps(entries))
        return _St()

    def _spy_scandir(self):
        """把 _dir_listing 的真实 scandir 记下来(缓存命中的不算)。"""
        outer = self.probes
        real = webui.os.scandir

        def fake(path, *a, **kw):
            outer.append(str(path))
            return real(path, *a, **kw)
        webui.os.scandir = fake
        self.addCleanup(setattr, webui.os, "scandir", real)


class TestPaging(_Base):
    """只返回当前页,且只探测当前页的目录。"""

    def _hist(self, n=50):
        hist = []
        for i in range(n):
            out = os.path.join(self.rec, "主播A", "2026-08-%02d" % (1 + i % 28),
                               "%s-%02d-%%03d.flv" % (STEM, i))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            hist.append(_entry(i, self.rec))
        return hist

    def test_default_page_is_20(self):
        st = self._state(self._hist(50))
        p = webui._history_payload(st)
        self.assertEqual(len(p["entries"]), 20)
        self.assertEqual(p["total"], 50)
        self.assertEqual(p["offset"], 0)
        self.assertEqual(p["limit"], 20)

    def test_second_page(self):
        st = self._state(self._hist(50))
        p = webui._history_payload(st, 20, 20)
        self.assertEqual(p["offset"], 20)
        self.assertEqual(len(p["entries"]), 20)
        # 新到旧 + 跳过前 20 条:剩下的第一条是 i=29(started_at 1029)
        self.assertEqual(p["entries"][0]["started_at"], 1000.0 + 29)
        self.assertGreater(p["entries"][0]["started_at"],
                           p["entries"][-1]["started_at"])

    def test_out_of_range_offset_rolls_back_to_last_page(self):
        """删掉几条后前端还停在旧页码 → 回退最后一页,不能给空列表。"""
        st = self._state(self._hist(25))
        p = webui._history_payload(st, 40, 20)
        self.assertEqual(p["offset"], 20)
        self.assertEqual(len(p["entries"]), 5)

    def test_limit_is_clamped(self):
        st = self._state(self._hist(300))
        p = webui._history_payload(st, 0, 10 ** 6)
        self.assertLessEqual(p["limit"], webui._HIST_MAX_LIMIT)
        self.assertEqual(len(p["entries"]), webui._HIST_MAX_LIMIT)

    def test_only_current_page_dirs_are_probed(self):
        """🔴 核心守卫:一轮只许探测当页要用的目录,不许回到「全量 204 个」。

        50 条历史落在 28 个日期目录;只取 20 条(当页)时,真实 scandir
        的目录数必须显著少于全量 —— 否则就是把 165 秒请回来了。
        """
        hist = self._hist(50)
        st = self._state(hist)
        webui._dir_cache_clear()
        self._spy_scandir()
        webui._history_payload(st, 0, 20)
        page_probes = len(self.probes)
        self.assertLessEqual(
            page_probes, 20 * 3,
            "当页 20 条却探测了 %d 个目录:分页没有生效" % page_probes)

        # 对照:全量(limit 远大于总数)必然更多
        webui._dir_cache_clear()
        self.probes.clear()
        webui._history_payload(st, 0, 200)
        all_probes = len(self.probes)
        self.assertGreater(all_probes, page_probes)


class TestEntryLevelCache(_Base):
    """条目级 files 缓存:同一页再来一次不打 SMB。"""

    def test_second_call_hits_entry_cache(self):
        hist = [_entry(0, self.rec)]
        os.makedirs(os.path.dirname(hist[0]["output_path"]), exist_ok=True)
        st = self._state(hist)
        webui._history_payload(st)
        self._spy_scandir()
        webui._HIST_TTL = 0            # 强制走真算路径,不许整份 payload 早退
        p2 = webui._history_payload(st)
        self.assertEqual(self.probes, [],
                         "条目级缓存没命中,同一页又打了一趟 SMB")
        self.assertIn("files", p2["entries"][0])

    def test_files_cache_keyed_by_archive_state(self):
        """归档状态变了就是另一条结论,不能复用旧 files。"""
        hist = [_entry(0, self.rec)]
        d = os.path.dirname(hist[0]["output_path"])
        os.makedirs(d, exist_ok=True)
        webui._history_files_cached(hist[0], self.cfg)
        dest = os.path.join(self.rec, "归档份")
        os.makedirs(dest, exist_ok=True)
        hist[0]["archive"] = {"state": "archived", "dest": dest}
        f = webui._history_files_cached(hist[0], self.cfg)
        self.assertEqual(f["location"], "archive")

    def test_dir_cache_clear_also_clears_files_cache(self):
        hist = [_entry(0, self.rec)]
        os.makedirs(os.path.dirname(hist[0]["output_path"]), exist_ok=True)
        webui._history_files_cached(hist[0], self.cfg)
        self.assertTrue(webui._FILES_CACHE)
        webui._dir_cache_clear()
        self.assertFalse(webui._FILES_CACHE,
                         "清目录缓存却留着条目结论 → 归档完还显示本机路径")


class TestProbeBudget(_Base):
    """预算耗尽后不许再打 SMB,且必须报 UNKNOWN(不是 MISSING)。"""

    def test_budget_exhausted_short_circuits(self):
        d = os.path.join(self.rec, "预算目录")
        os.makedirs(d)
        webui._dir_cache_clear()
        old = webui._probe_budget(0)          # deadline 立刻到期
        try:
            state, names = webui._dir_listing(d)
        finally:
            webui._probe_budget_restore(old)
        self.assertEqual(state, webui._DIR_UNKNOWN)
        self.assertEqual(names, frozenset())

    def test_unknown_is_not_cached(self):
        """「这次没探到」不是目录的真实状态,不能缓存 —— 否则一次抖动
        会把这个目录锁死成「无法确认」整整一个 TTL。"""
        d = os.path.join(self.rec, "未探目录")
        os.makedirs(d)
        webui._dir_cache_clear()
        old = webui._probe_budget(0)
        try:
            webui._dir_listing(d)
        finally:
            webui._probe_budget_restore(old)
        self.assertNotIn(os.path.abspath(d), webui._DIR_CACHE)
        # 预算恢复后立刻能探到
        self.assertEqual(webui._dir_listing(d)[0], webui._DIR_OK)

    def test_prefetch_workers_share_budget(self):
        """线程池里的 worker 必须挂上主线程的预算,否则它会一路打满 SMB。"""
        dirs = []
        for i in range(12):
            p = os.path.join(self.rec, "批量%02d" % i)
            os.makedirs(p)
            dirs.append(p)
        webui._dir_cache_clear()
        budget = webui._ProbeBudget(0)        # 立刻耗尽
        self._spy_scandir()
        webui._prefetch_dirs(dirs, workers=4, budget=budget)
        self.assertEqual(self.probes, [],
                         "worker 绕过了预算,预取照样打满了 SMB")

    def test_payload_has_hard_time_ceiling(self):
        """真实回归:即使每个目录都慢,一轮也不许无限拖下去。

        本机 APFS 的 scandir 是微秒级,直接跑量不出上限,所以这里把 scandir
        注入 50 ms 延迟,模拟真冷 SMB(实测约 0.5~1 s/目录的量级)。60 个目录
        串起来要 3 秒,预算 0.4 秒 ⇒ 必须提前收尾,且剩余条目报「无法确认」。
        """
        orig_budget = webui._HIST_PROBE_BUDGET
        webui._HIST_PROBE_BUDGET = 0.4
        real = webui.os.scandir

        def slow(path, *a, **kw):
            time.sleep(0.05)
            return real(path, *a, **kw)

        hist = []
        for i in range(60):
            e = _entry(i, self.rec)
            os.makedirs(os.path.dirname(e["output_path"]), exist_ok=True)
            hist.append(e)
        st = self._state(hist)
        webui._dir_cache_clear()
        webui.os.scandir = slow
        try:
            t = time.time()
            p = webui._history_payload(st, 0, 60)
            dt = time.time() - t
        finally:
            webui.os.scandir = real
            webui._HIST_PROBE_BUDGET = orig_budget
        # 无预算时这些目录要 60×2×0.05 ≈ 6 s;预算 0.4 s ⇒ 必须明显更短
        self.assertLess(dt, 3.0, "预算没有生效,单轮耗时 %.1fs" % dt)
        self.assertEqual(len(p["entries"]), 60)
        # 预算耗尽只会让存在性变 None(无法确认),绝不能被当成「缺失」
        any_none = any(v is None
                       for e in p["entries"]
                       for k, v in (e["files"] or {}).items()
                       if k.endswith("_exists"))
        self.assertTrue(any_none, "预算耗尽应产生「无法确认」而不是全 True/False")


class TestStaleWhileRevalidate(_Base):
    """老值先返回,后台再刷新。"""

    def test_expired_cache_still_returns_payload(self):
        hist = [_entry(0, self.rec)]
        os.makedirs(os.path.dirname(hist[0]["output_path"]), exist_ok=True)
        st = self._state(hist)
        p1 = webui._history_payload(st)
        webui._HIST_TTL = 0                   # 让缓存立刻「过期」
        webui._HIST_SWR = True
        # TTL=0 时 SWR 主动关闭(语义:每次都要真算),所以先验证正常 SWR 路径
        webui._HIST_TTL = 1
        time.sleep(1.05)
        p2 = webui._history_payload(st)
        self.assertIs(p1, p2, "过期后应当先把老数据返回,而不是阻塞等重算")

    def test_swr_can_be_disabled(self):
        hist = [_entry(0, self.rec)]
        os.makedirs(os.path.dirname(hist[0]["output_path"]), exist_ok=True)
        st = self._state(hist)
        webui._HIST_TTL = 1
        webui._HIST_SWR = False
        p1 = webui._history_payload(st)
        time.sleep(1.05)
        p2 = webui._history_payload(st)
        self.assertIsNot(p1, p2)

    def test_ttl_zero_never_swr(self):
        """TTL<=0 表示「每次都要真算」(测试与运维都依赖这个语义)。"""
        hist = [_entry(0, self.rec)]
        os.makedirs(os.path.dirname(hist[0]["output_path"]), exist_ok=True)
        st = self._state(hist)
        webui._HIST_TTL = 0
        webui._HIST_SWR = True
        p1 = webui._history_payload(st)
        p2 = webui._history_payload(st)
        self.assertIsNot(p1, p2)


class TestBackfillConverges(_Base):
    """补算必须收敛:素材确实不存在的条目不能每轮重探。"""

    def test_absent_entries_are_marked_and_skipped(self):
        """缺字段但素材不存在 → 记进内存,下一轮直接跳过。

        标记必须只活在进程内:它是「探测结论」不是录制事实,写进 history.json
        会污染历史契约,也会让纯查询接口产生写盘副作用
        (见 test_skips_when_no_meta_files / test_backfill_missing_files_is_noop)。
        """
        import types
        dest = os.path.join(self.rec, "归档", DAY)
        os.makedirs(os.path.join(dest, ".meta"))
        hist = [_entry(0, self.rec, archived=True, dest=dest)]
        self.assertIsNone(hist[0].get("gift_diag"))

        mon = types.ModuleType("__main__")
        mon._history = hist
        mon._commit_history = lambda: None
        old_main = sys.modules.get("__main__")
        sys.modules["__main__"] = mon
        webui._absent_cache_clear()

        class _St:
            def __init__(self):
                self.hist = hist

            def get_config(self):
                return json.loads(json.dumps(self.cfg))

            def get_history(self):
                return json.loads(json.dumps(self.hist))

            def set_history(self, e):
                self.hist = json.loads(json.dumps(e))

        st = _St()
        try:
            webui._backfill_gift_fields(st)
            key = (hist[0]["output_path"], "gift")
            self.assertIn(key, webui._ABSENT_PROBE,
                          "首轮应记下「素材不存在」")
            # 第二轮:直接跳过,不再有任何探测
            real = webui._has_entry_file
            calls = {"n": 0}

            def counting(d, subdir, name):
                calls["n"] += 1
                return real(d, subdir, name)

            webui._has_entry_file = counting
            try:
                changed = webui._backfill_gift_fields(st)
            finally:
                webui._has_entry_file = real
            self.assertFalse(changed)
            self.assertEqual(calls["n"], 0,
                             "第二轮还在重探已确认不存在的素材 ⇒ 不会收敛")
        finally:
            sys.modules["__main__"] = old_main
            webui._absent_cache_clear()

    def test_cap_counts_attempts_not_successes(self):
        """额度必须「尝试即计数」。

        原 bug:只在补成功时 remote_used += 1,而补不上的条目永不计数 ⇒
        519 条缺字段的存量每轮全部跑一遍、永不收敛。这里造 5 条远端缺字段、
        素材都不存在,断言一轮只**尝试** _GIFT_LAZY_REMOTE_CAP 条
        (被尝试过的才会记进 _ABSENT_PROBE,用它数比数 _has_entry_file 调用
        次数准 —— 每条对 2 个候选目录 × 2 个文件,次数会翻几倍)。
        """
        import types
        dest = os.path.join(self.rec, "归档", DAY)
        os.makedirs(os.path.join(dest, ".meta"))
        hist = [_entry(i, self.rec, archived=True, dest=dest) for i in range(5)]
        webui._absent_cache_clear()

        mon = types.ModuleType("__main__")
        mon._history = hist
        mon._commit_history = lambda: None
        old_main = sys.modules.get("__main__")
        sys.modules["__main__"] = mon

        class _St:
            def get_config(self):
                return json.loads(json.dumps(self.cfg))

            def get_history(self):
                return []

            def set_history(self, e):
                pass

        try:
            webui._backfill_gift_fields(_St())
            marked = sum(1 for e in mon._history
                         if (e["output_path"], "gift") in webui._ABSENT_PROBE)
        finally:
            sys.modules["__main__"] = old_main
            webui._absent_cache_clear()
        self.assertEqual(
            marked, webui._GIFT_LAZY_REMOTE_CAP,
            "一轮应只尝试 %d 条远端,实际尝试了 %d 条(额度没按「尝试」计数)"
            % (webui._GIFT_LAZY_REMOTE_CAP, marked))


class TestFrontendPaging(unittest.TestCase):
    """前端必须带 offset/limit,且页码由后端回传的 offset 决定。"""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "index.html")
        with open(path, encoding="utf-8") as f:
            self.html = f.read()

    def test_load_history_sends_paging_params(self):
        i = self.html.index("async function loadHistory()")
        body = self.html[i:i + 900]
        self.assertIn("offset=", body)
        self.assertIn("limit=", body)

    def test_render_uses_backend_paging(self):
        """正常路径必须直接用后端给的当页数据,不再自己 reverse+slice。

        留了 legacy 分支兼容「静态文件已更新、后端还没重启」的窗口期(那时
        旧后端忽略 offset/limit 并返回全量、不给 total)。所以只要求:
          · 正常路径 histEntries 直接取 raw(不切片);
          · reverse 只出现在 legacy 分支里,且该分支有明确标注。
        """
        i = self.html.index("function renderHistory(payload)")
        body = self.html[i:i + 3000]
        self.assertIn("histTotal", body)
        self.assertIn("payload.total", body)
        # 正常路径不切片
        self.assertIn("histEntries = raw;", body,
                      "正常路径应当直接使用后端返回的当页 entries")
        # reverse 只能是 legacy 兼容分支
        self.assertLessEqual(body.count(".reverse()"), 1,
                             "正常路径不该再本地反转:后端已按新到旧给数据")
        rv = body.find(".reverse()")
        if rv > 0:
            self.assertIn("legacy", body[:rv],
                          "reverse 只允许出现在 legacy 兼容分支里")
            self.assertIn("legacy", body[max(0, rv - 400):rv + 400])

    def test_legacy_backend_still_renders(self):
        """旧后端(无 total)不能让页面变成「暂无录制记录」。"""
        i = self.html.index("function renderHistory(payload)")
        body = self.html[i:i + 3000]
        self.assertIn("!(payload.total >= 0)", body,
                      "缺少 total 时必须能识别出旧后端并退化处理")

    def test_next_button_guards_last_page(self):
        i = self.html.index('document.getElementById("hist-next")')
        self.assertIn("histPages", self.html[i:i + 400])


if __name__ == "__main__":
    unittest.main()
