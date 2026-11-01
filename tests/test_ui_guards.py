#!/usr/bin/env python3
"""前端「短期录制」UI 守卫(2026-10-22)。

防止下次改动把关键交互改丢:筛选三档、添加弹窗、期限字段、续期入口这些
都藏在 static/index.html 的一大坨 HTML/JS 里,肉眼 review 极易漏。
每个断言对应一个「用户会立刻发现坏了」的行为,改坏必须报红。

注意这里只做**静态结构**断言(不启动服务);真实浏览器端到端在部署前跑。
"""
import os
import re
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


HTML = _read("static/index.html")
MONITOR = _read("monitor.py")
WEBUI = _read("webui.py")


class TestFilterChips(unittest.TestCase):
    """标题行右侧录制方式筛选(红框位置):全部主播 + 长期/短期/暂停三档。"""

    def test_filter_container_exists(self):
        self.assertIn('id="rec-filter"', HTML)

    def test_all_chip_is_first(self):
        """「全部主播」必须紧邻「＋ 添加主播」右侧(= 组内第一个 chip)。

        2026-10-27 用户改口:要一个显式的「全部主播」选项。早前「只给 3 个、
        「全部」靠再点一次已选中的档实现、不加第 4 个」的拍板到此作废。
        """
        start = HTML.index('id="rec-filter"')
        seg = HTML[start:start + HTML[start:].index("</div>")]
        self.assertIn('data-f="all"', seg, "缺「全部主播」选项")
        self.assertLess(seg.index('data-f="all"'), seg.index('data-f="long"'),
                        "「全部主播」要排在长期录制之前(即「＋ 添加主播」正后方)")

    def test_all_chip_clears_filter(self):
        """「全部主播」不是第四类主播,而是「取消筛选」:点它必须落回 null。"""
        self.assertIn('recFilter = (f === "all" || recFilter === f) ? null : f;', HTML)

    def test_all_chip_active_when_unfiltered(self):
        """选中态要反过来判:recFilter 为空时「全部主播」才亮,否则默认态一屏全灰。"""
        self.assertIn('(f === "all") ? !recFilter : recFilter === f', HTML)

    def test_three_buckets_still_exist(self):
        """三档分类不变:长期/短期/暂停(归属与计数共用 _recBucket)。"""
        for f in ("long", "short", "paused"):
            self.assertIn(f'data-f="{f}"', HTML)

    def test_bucket_logic_has_expired_into_pause(self):
        """已到期主播归「暂停录制」档(2026-10-22 用户拍板),不能留在短期档。"""
        self.assertIn('return "pause"', HTML)
        idx_bucket = HTML.index("function _recBucket")
        idx_next = HTML.index("function _recBadgeHtml")
        body = HTML[idx_bucket:idx_next]
        # 必须是「expired 与 missing_until 同一句 return pause」——
        # 只查 return "pause" 出现过不够:paused 分支里也有,改坏照样绿。
        self.assertIn(
            'w.state === "expired" || w.state === "missing_until") return "pause"', body)

    def test_pause_chip_red_dot(self):
        """暂停档里有已到期的要亮红点提示待续期。"""
        self.assertIn("rdot", HTML)

    def test_bucket_logic_has_paused_state(self):
        """手停必须归暂停档,哪怕下发的 mode 还写着 long(2026-10-27 修)。

        后端 `record_window` 已把「auto_record=false + 残留 record_mode」收敛成
        mode=paused;前端这里再认一次 `state`,是为了页面连到旧进程/异常快照时
        也不把「开关是关的」主播画成「长期录制」。
        """
        body = HTML[HTML.index("function _recBucket"):HTML.index("function _recBadgeHtml")]
        self.assertIn('if (w.state === "paused" || w.mode === "paused") return "pause";', body,
                      "paused 要认 state,不能只看 mode")


class TestAddModal(unittest.TestCase):
    """添加主播:弹出悬浮框二级菜单(不再折叠卡)。"""

    def test_entry_button_and_modal(self):
        self.assertIn('id="add-open"', HTML)
        self.assertIn('id="add-modal"', HTML)
        self.assertIn('id="add-btn"', HTML)

    def test_old_collapse_card_removed(self):
        self.assertNotIn('id="add-card"', HTML)
        self.assertNotIn('id="add-toggle"', HTML)

    def test_mode_and_days_control(self):
        self.assertIn('id="add-mode"', HTML)
        self.assertIn('id="add-days"', HTML)
        for d in (1, 3, 7):
            self.assertIn(f'data-days="{d}"', HTML)

    def test_four_close_paths(self):
        """✕ / 取消 / 点遮罩 / ESC 四条关闭路径都要在。"""
        self.assertIn('id="add-close"', HTML)
        self.assertIn('id="add-cancel"', HTML)
        self.assertIn('e.target.id === "add-modal"', HTML)
        self.assertIn('key !== "Escape"', HTML)

    def test_small_button_class(self):
        """「添加主播」按钮缩小(34px)与弹窗按钮(32px)。"""
        self.assertIn("add-open", HTML)
        self.assertIn("btn-sm", HTML)


class TestStreamerDetailModal(unittest.TestCase):
    """详情改成毛玻璃弹窗(2026-10-26):点详情不再把卡片整体撑大。"""

    def test_card_no_longer_holds_detail_panel(self):
        """卡内详情面板必须删干净,否则点详情又会把卡片拉高、列表跳位。"""
        self.assertNotIn('class="s-detail"', HTML, "卡片里不该再有详情面板节点")
        self.assertNotIn(".streamer.open .s-detail", HTML, "卡内展开的样式要删掉")
        self.assertNotIn("expandedStreamers", HTML,
                         "「哪几张卡展开着」的集合已被单个 detailName 取代")

    def test_detail_modal_is_glass(self):
        self.assertIn('id="detail-modal"', HTML)
        self.assertIn('class="modal glass hidden"', HTML, "详情弹窗要带 glass 变体")
        self.assertIn(".modal.glass .modal-box", HTML)
        self.assertIn("backdrop-filter: blur(", HTML, "毛玻璃靠 backdrop-filter,别退化成纯半透明")
        self.assertIn("-webkit-backdrop-filter", HTML, "Safari 需要 -webkit- 前缀")

    def test_detail_has_three_close_paths(self):
        self.assertIn('id="dt-close"', HTML, "✕ 关闭按钮")
        self.assertIn(
            'if (e.target.id === "detail-modal" || e.target.classList.contains("modal-mask")) closeDetail();',
            HTML, "点遮罩也要关")
        start = HTML.index('if (e.key !== "Escape") return;')
        seg = HTML[start:HTML.index("});", start)]
        self.assertIn("closeDetail()", seg, "ESC 也要能关详情弹窗")

    def test_term_buttons_shared_not_duplicated(self):
        """期限按钮已挪到弹窗(卡片外),处理函数必须抽成一份、两处绑定。"""
        self.assertIn("function _termAction(e)", HTML)
        start = HTML.index('document.getElementById("detail-modal").addEventListener("click"')
        seg = HTML[start:HTML.index("});", start)]
        self.assertIn("_termAction(e)", seg,
                      "弹窗里的续期/改长期/暂停只能靠这条委托接住(按钮在 #streamer-list 之外)")
        self.assertIn(
            'document.getElementById("streamer-list").addEventListener("click", e => {\n'
            "  if (_termAction(e)) return;",
            HTML, "卡片那条委托要复用同一函数")
        self.assertEqual(HTML.count("data-renew="), 1,
                         "续期按钮只允许在 _recTermHtml 里生成一处")

    def test_detail_refreshes_after_rerender(self):
        """续期/暂停后是全量重绘卡片:开着的弹窗必须跟着重填。"""
        self.assertIn("if (detailName) fillDetail();", HTML)
        self.assertIn("function _fillSchedFields(", HTML,
                      "调度三项卡片/弹窗共用一份填充逻辑,别写第二遍")

    def test_expired_switch_opens_detail(self):
        """到期点开关要弹出详情(止损必须配套恢复路径)。"""
        start = HTML.index('const cb = e.target.closest(".ar-cb");')
        seg = HTML[start:start + 900]
        self.assertIn("openDetail(m.name)", seg, "到期时应弹出详情让用户续期")
        self.assertNotIn("expandedStreamers", seg)


class TestStreamerModalsGlass(unittest.TestCase):
    """主播管理页的四类弹窗统一毛玻璃(2026-10-26)。

    两族骨架都得认 —— 只给一族写 `.glass` 的结果是「一半毛玻璃一半不透明色块」:
      ① `.modal.glass`      —— 预览图 / 备注 / 详情:外层 .modal fixed 铺满视口,
                               内层 .modal-mask 只当背景;
      ② `.modal-mask.glass` —— 添加主播:遮罩自己就是根节点,box 是它的子节点。
    """

    GLASS_ROOTS = (
        ('<div class="modal-mask glass hidden" id="add-modal">', "添加主播"),
        ('<div id="preview-modal" class="modal glass hidden"', "预览图"),
        ('<div id="note-modal" class="modal glass hidden">', "备注"),
        ('<div id="detail-modal" class="modal glass hidden">', "详情"),
    )

    @staticmethod
    def _glass_rules():
        """返回 (选择器, 规则体) —— 只取选择器里带 .glass 的那些。"""
        style = re.search(r"<style>([\s\S]*?)</style>", HTML).group(1)
        style = re.sub(r"/\*[\s\S]*?\*/", "", style)   # 注释里有选择器字样,先剔掉
        out = []
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
            for sel in m.group(1).split(","):
                if ".glass" in sel:
                    out.append((sel.strip(), m.group(2)))
        return out

    def test_all_four_modals_carry_glass(self):
        for needle, label in self.GLASS_ROOTS:
            self.assertIn(needle, HTML, f"{label}弹窗没带 glass 变体(主播管理页要统一)")

    def test_both_skeletons_supported(self):
        """两种根节点形态都必须有对应的 glass 规则,少一个就有弹窗还是色块。"""
        sels = [s for s, _ in self._glass_rules()]
        for want in (".modal.glass .modal-mask", ".modal-mask.glass",
                     ".modal.glass .modal-box", ".modal-mask.glass .modal-box"):
            self.assertIn(want, sels, f"缺 {want}:那一族弹窗的毛玻璃不会生效")

    def test_blur_has_webkit_prefix(self):
        """Safari 只认 -webkit-backdrop-filter,漏了就是「半透明但不糊」。

        逐条校验:遮罩和面板各自都要带前缀 —— 只查「整体出现过一次」会让
        面板那条漏掉前缀的改动蒙混过关。
        """
        blurred = [(sel, body) for sel, body in self._glass_rules()
                   if "backdrop-filter" in body]
        self.assertGreaterEqual(len(blurred), 2, "遮罩与面板两层都要糊,现在只糊了一层")
        for sel, body in blurred:
            self.assertIn("-webkit-backdrop-filter: blur(", body,
                          f"{sel} 的 backdrop-filter 少了 -webkit- 前缀(Safari 里不糊)")

    def test_glass_rules_never_set_width(self):
        """🔴 glass 规则里不许写 width。

        `.modal-mask.glass .modal-box` 权重是 3 个类,写个 width 就会把
        `.add-modal-box`(620px)与 `.modal .modal-box`(720/92vw)一起盖掉 ——
        添加主播弹窗会突然变宽,表单挤成两行。
        """
        for sel, body in self._glass_rules():
            self.assertNotIn("width", body, f"{sel} 里写了 width,会盖掉各弹窗自己的宽度")

    def test_add_modal_keeps_own_width(self):
        """glass 化不许动添加主播弹窗的 620px 窄版(表单是三段两列排的)。"""
        self.assertIn(".add-modal-box { width: min(620px, 94vw); }", HTML)


class TestRecordFields(unittest.TestCase):
    """期限字段:前端写入端与后端判定端口径一致。"""

    def test_frontend_writes_three_fields(self):
        self.assertIn("record_mode", HTML)
        self.assertIn("record_until", HTML)
        self.assertIn("record_days", HTML)

    def test_frontend_writes_until_from_today(self):
        """起算日=添加当天,到期日=今天+(天数-1) —— 自然日口径。"""
        self.assertIn("_isoAdd(today, addDays - 1)", HTML)

    def test_renew_formula_present(self):
        """续期基准:未到期从原到期日顺延,已过期从今天算。"""
        self.assertIn("function _renewUntil", HTML)
        self.assertIn("todayStr <= untilStr", HTML)

    def test_switch_never_shows_on_when_expired(self):
        """到期时开关必须显示为关 —— 开关开着却不录,页面自相矛盾。"""
        self.assertIn("function _recSwitchOn", HTML)
        self.assertIn('_recSwitchLabel', HTML)
        self.assertIn("已到期", HTML)

    def test_frontend_does_not_self_compute_days_left(self):
        """「剩几天」只吃后端 record_windows,不允许用浏览器时钟自算。"""
        self.assertIn("recWinMap", HTML)
        self.assertIn("record_windows", HTML)
        self.assertIn("recToday", HTML)

    def test_backend_dispatches_record_windows(self):
        self.assertIn("record_windows", WEBUI)
        self.assertIn("def record_state", MONITOR)

    def test_backend_day_range_matches_ui(self):
        """校验放 1..365(UI 只给 1/3/7,以后加档不动后端)。"""
        self.assertIn("1 <= int(d) <= 365", WEBUI)


if __name__ == "__main__":
    unittest.main()
