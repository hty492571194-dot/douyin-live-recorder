#!/usr/bin/env python3
"""归档设置页(前端)与归档接口的静态守卫测试。

为什么需要它:2026-09-18 上线「浏览…」按钮时,pickExtDir 里漏写了
`.then(r => r.json())` —— fetch 的第一个 then 拿到的是 Response 对象,
`d.path` 恒为 undefined,于是**无论后端返回什么**都走进错误分支显示
「未选择目录」。这类错误编译期不报错、单测也碰不到(要真点按钮),
只能靠静态扫描拦住。

这里只做**确定**的检查(元素存在、链式调用是否解析 JSON、前端用到的接口
在后端是否注册),不做语义判断。
"""
import os
import re
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(BASE, "static", "index.html")
WEBUI = os.path.join(BASE, "webui.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _func_body(src, name):
    """取 Python 源码里 `def <name>(` 到下一个顶层 `def ` 之间的片段。

    比「从 index 往后截 N 个字符」可靠:函数顺序或长短一变,截面就不会
    悄悄截到别人身上(断言变成了「别处有这个调用」,检查形同虚设)。
    """
    m = re.search(r"^def %s\(" % re.escape(name), src, re.M)
    if m is None:
        raise AssertionError("webui.py 里找不到 def %s" % name)
    nxt = re.search(r"^def ", src[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(src)
    return src[m.start():end]


def _fetch_calls(js):
    """返回 [(url, 表达式片段)] —— 片段截到该语句结束为止。"""
    out = []
    for m in re.finditer(r'fetch\(\s*"(/api/[^"]+)"', js):
        end = len(js)
        for stop in (";", "\n}"):
            i = js.find(stop, m.start())
            if i > 0:
                end = min(end, i)
        out.append((m.group(1), js[m.start():end]))
    return out


class TestFetchJsonParsing(unittest.TestCase):
    """凡是把 fetch 结果往下链 .then 的地方,必须先 .json() 解出响应体。"""

    def setUp(self):
        self.js = _read(HTML)

    def test_every_chained_fetch_parses_json(self):
        calls = _fetch_calls(self.js)
        self.assertGreater(len(calls), 20, "扫描到的 fetch 调用太少,正则可能失效了")
        bad = [u for u, s in calls if ".then(" in s and ".json()" not in s]
        self.assertEqual(bad, [], "这些 fetch 链了 .then 却没解析 JSON: %s" % bad)

    def test_pick_dir_calls_are_chained_and_parsed(self):
        """重点盯 /api/pick-dir:它就是栽在这个坑上的。

        2026-10-30 起有两处调用(归档外接目录 / 录制暂存目录),两处都必须
        先 `.json()` 解出响应体 —— 数量放开,解析要求不变。
        """
        hits = [(u, s) for u, s in _fetch_calls(self.js) if u == "/api/pick-dir"]
        self.assertGreaterEqual(len(hits), 1, "应至少有一处 /api/pick-dir 调用")
        for _url, snippet in hits:
            self.assertIn(".then(", snippet)
            self.assertIn(".json()", snippet)


class TestArchiveControlsExist(unittest.TestCase):
    """归档设置页该有的控件不能丢。"""

    def setUp(self):
        self.html = _read(HTML)

    def test_browse_button_and_hint(self):
        for eid in ("ext-pick", "ext-hint", "archive-ext"):
            self.assertIn('id="%s"' % eid, self.html, "缺少元素 #%s" % eid)

    def test_backfill_button(self):
        self.assertIn('id="archive-backfill"', self.html)

    def test_space_and_progress_controls(self):
        """空间/进度提示控件不能丢:2026-09-18 那次「归档失败」就是因为
        界面上看不到「盘满」和「还剩多少没传」。"""
        for eid in ("nas-waiting", "nas-progress-wrap", "nas-progress-bar",
                    "nas-progress-text", "nas-disk", "nas-disk-bar",
                    "nas-disk-used", "nas-disk-free", "nas-disk-total",
                    "nas-speed", "nas-space-warn"):
            self.assertIn('id="%s"' % eid, self.html, "缺少元素 #%s" % eid)
        for field in ("disk_total", "disk_used", "disk_free", "disk_used_percent",
                      "waiting_space", "progress", "speed_bps"):
            self.assertIn(field, self.html, "前端没消费 %s 字段" % field)

    def test_deferred_and_freshness_controls(self):
        """「录制中暂缓」计数 + 数据新鲜度提示。

        前者:正在录制的场次不搬(搬走会删掉 ffmpeg 正在写的文件),界面必须
        说明这是正常等待,否则用户会以为卡住了。
        后者:状态卡是 3 秒轮询,页面挂后台时会停在旧值上;2026-09-18 就有人
        对着几分钟前的「空间不足」提示排查了半天,所以把刷新时间显式写出来。
        """
        for eid in ("nas-deferred", "nas-defer-note", "nas-updated"):
            self.assertIn('id="%s"' % eid, self.html, "缺少元素 #%s" % eid)
        self.assertIn("deferred", self.html, "前端没消费 deferred 字段")
        self.assertIn('function _renderNasUpdated', self.html)
        self.assertIn("setInterval(_renderNasUpdated", self.html)
        # 陈旧时必须显式告警,不能静默显示旧值
        self.assertIn("秒没有刷新", self.html)

    def test_stats_are_rendered_with_fallbacks(self):
        """缺字段时渲染「—」,不能把 undefined 打到界面上。"""
        self.assertIn('function _fmtBytes', self.html)
        self.assertIn('function _fmtRate', self.html)

    def test_archive_status_moved_into_history_view(self):
        """归档状态卡在「录制历史」页(2026-09-18 从「归档设置」页移来)。"""
        i = self.html.index('id="view-history"')
        j = self.html.index("</section>", i)
        hist = self.html[i:j]
        for eid in ("nas-status", "nas-disk", "nas-progress-wrap", "archive-backfill"):
            self.assertIn('id="%s"' % eid, hist, "录制历史页缺少 #%s" % eid)
        k = self.html.index('id="view-nas"')
        self.assertNotIn('id="nas-status"', self.html[k:i],
                         "归档设置页不该再留状态卡")

    def test_buttons_are_bound(self):
        self.assertIn('getElementById("ext-pick").addEventListener', self.html)
        self.assertIn('getElementById("archive-backfill").addEventListener', self.html)


class TestArchiveEndpointsRegistered(unittest.TestCase):
    """前端用到的归档接口必须在 webui.py 里真的存在(防前后端分叉)。"""

    def setUp(self):
        self.webui = _read(WEBUI)

    def test_pick_dir_endpoint(self):
        self.assertIn('"/api/pick-dir"', self.webui)

    def test_backfill_endpoint(self):
        self.assertIn('"/api/archive/backfill"', self.webui)


if __name__ == "__main__":
    unittest.main()


class TestArchiveTargetDisplay(unittest.TestCase):
    """历史页的归档位置要区分 NAS / 外接硬盘(2026-09-21)。"""

    def setUp(self):
        with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_consumes_target_field(self):
        """前端要消费 archive.target,存量无字段时按 dest 前缀推断。"""
        self.assertIn("archTarget", self.html)
        self.assertIn('arch.target', self.html)
        self.assertIn('"/Volumes/"', self.html, "存量数据靠 dest 前缀推断")

    def test_no_hardcoded_nas_short(self):
        """不能再把 archived 一律硬编码显示成 NAS。"""
        self.assertNotIn('archived ? "NAS"', self.html)
        self.assertIn("targetName", self.html)


class TestHistoryStatsBackfill(unittest.TestCase):
    """历史页惰性补全:旧条目的容量/段数要能补出来(2026-09-21)。"""

    def test_backend_has_backfill(self):
        src = open(os.path.join(BASE, "webui.py"), encoding="utf-8").read()
        self.assertIn("_backfill_history_stats", src)
        self.assertIn('e.get("size") is not None', src, "补过的不重复算")
        # 归档目标优先于本机(素材大概率已搬走)
        i = src.index("def _backfill_history_stats")
        snippet = src[i:i + 2200]
        self.assertIn('candidates.append(dest)', snippet)
        self.assertIn('_commit_history', snippet, "补全要落盘")

    def test_history_endpoint_calls_backfill(self):
        """历史接口必须触发惰性补算。

        2026-10-26 起补算收进了 `_history_payload`(TTL 缓存 + 单飞,见
        「解析按钮永久卡在解析中」的修复)。2026-10-31 又拆了一层:真算的部分
        搬到 `_build_history_payload`,外层只负责分页缓冲与 stale-while-revalidate。
        所以现在追三层调用链,少任何一层,存量条目的容量/段数/礼物可见度就
        永远补不出来,而页面只是「看起来没数据」:

            路由 → _history_payload → _refresh_hist_sync / _spawn_hist_refresh
                 → _build_history_payload → _backfill_*

        注意不能用「从某个 def 往后截 N 个字符」定位 —— 函数顺序一变断言就
        会指向别人。这里按函数边界切,见 _func_body。
        """
        src = open(os.path.join(BASE, "webui.py"), encoding="utf-8").read()
        i = src.index('elif path == "/api/history":')
        self.assertIn("_history_payload(state, ", src[i:i + 900],
                      "历史接口必须走 _history_payload 并传分页参数")

        outer = _func_body(src, "_history_payload")
        self.assertTrue("_refresh_hist_sync(" in outer or "_spawn_hist_refresh(" in outer,
                        "_history_payload 必须落到真算入口")

        build = _func_body(src, "_build_history_payload")
        self.assertIn("_backfill_history_stats(state)", build)
        self.assertIn("_backfill_gift_fields(state)", build)


class TestHistoryCompactLayout(unittest.TestCase):
    """历史行紧凑版(方案 C:胶囊聚合 + 完整年月日,2026-09-21)的关键元素。"""

    def setUp(self):
        with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_time_shows_start_date_only(self):
        """时间列只给开播「月-日」(2026-11-01 精简)。

        跨天场次原本要写两遍完整区间(10-30 22:08 – 10-31 02:42 = 22 字符 ≈148px),
        是历史行撑破框架的主因。现在只留 10-30;结束时刻与秒级精度由 .h-time 的
        title 承载,开了多久另有时长胶囊(.dur),信息不丢。
        """
        self.assertIn("function fmtDateMD", self.html)
        self.assertIn("fmtDateMD(e.started_at)", self.html)
        self.assertNotIn("fmtTimeRange(e.started_at, e.ended_at)", self.html,
                         "历史行不得再渲染完整时间区间(会把行撑破)")
        i = self.html.index("function fmtDateMD")
        self.assertIn("getMonth", self.html[i:i + 500], "fmtDateMD 要输出月-日")
        # 完整区间要能从悬停 title 里看到,别把信息真删了
        self.assertIn("esc(fmtDateTime(e.started_at))", self.html)
        # 旧方案的固定时间列宽与列模板必须清干净
        self.assertNotIn("118px", self.html, "固定时间列宽会把年月日折叠成省略号")
        self.assertNotIn("grid-template-columns: minmax(70px", self.html)

    def test_flex_layout_replaces_grid(self):
        """h-main 改 flex 聚合:时间不收缩,操作钮 margin-left:auto 恒靠右。"""
        i = self.html.index(".h-main {")
        self.assertIn("display: flex", self.html[i:i + 120])
        j = self.html.index(".h-time {")
        self.assertIn("flex-shrink: 0", self.html[j:j + 160], "时间列不得被压缩折叠")
        k = self.html.index(".h-ops {")
        self.assertIn("margin-left: auto", self.html[k:k + 200])

    def test_pills_for_dur_seg_size(self):
        """时长/段数/体积进胶囊(.h-pill),状态徽标并入胶囊组。"""
        self.assertIn(".h-pill {", self.html)
        self.assertIn('${e.segments} 段</span>', self.html)
        self.assertIn('${fmtSize(e.size)}</span>', self.html)
        self.assertIn("${durPill}${segPill}${sizePill}", self.html)
        self.assertNotIn("h-seg", self.html, "旧 .h-seg 徽标已并入胶囊")

    def test_buttons_are_icon_sized(self):
        """操作钮是 24px 图标钮,不再用文字钮。"""
        self.assertIn(".hist-row .btn.h-btn { width: 24px; height: 22px;", self.html)
        for token in ('data-hist-detail', 'data-open', 'data-dm', 'data-del-s'):
            self.assertIn(token, self.html, "事件绑定不能丢")

    def test_target_badge_is_short(self):
        """徽标显示 SSD/NAS 三字宽;「外接硬盘」全称只允许出现在悬停说明里。"""
        self.assertIn('targetName = archTarget === "external" ? "SSD" : "NAS"', self.html)
        # 徽标文本用短词变量,而不是把全称直接渲染进徽标
        self.assertIn('${locShort}', self.html)
