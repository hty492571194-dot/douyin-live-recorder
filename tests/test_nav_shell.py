"""侧边栏外壳(折叠 / 导航重组 / 弹窗入口)与礼物界面改版的结构守卫。

2026-10-10 UI 重构定下的规矩,改外壳前先跑:
    .venv/bin/python -m unittest tests.test_nav_shell

守的点:
  · 侧边栏必须可折叠(class + localStorage 记忆)—— 用户明确要求的交互;
  · 礼物诊断独立页已废除(2026-10-16 并入录制历史):入口/视图/加载逻辑
    必须全部消失,诊断记录按场次进历史行展开区,不许再长回独立页;
  · 历史页礼物区(二次瘦身):具体礼物明细(摘要条/礼物榜/gift_stats)全部下线,
    保留可见度五档胶囊 + ▲诊断警示 + 展开区诊断块 + 顶部抽检卡片;
  · 启动自检横幅已废除,入口在「运维工具」组,弹窗展示全部检查项 + 告警徽标;
  · 机器人告警弹窗是 gift_watch 配置的唯一编辑入口(高级参数页不许再放第二份)。
"""
import os
import re
import unittest

import webui

BASE = webui.BASE


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


class TestNavShell(unittest.TestCase):
    """侧边栏外壳结构。"""

    def setUp(self):
        self.html = _read("static/index.html")

    def test_sidebar_collapsible(self):
        self.assertIn('id="side-toggle"', self.html, "必须有折叠开关按钮")
        self.assertIn("aside.collapsed", self.html, "必须有折叠态 CSS")
        self.assertIn('localStorage.getItem("sideCollapsed")', self.html,
                      "折叠状态要落 localStorage,否则刷新就弹回")

    def test_nav_texts_wrapped_for_collapse(self):
        """nav-item 的文字要包 .txt —— 折叠时靠隐藏 .txt 只留图标。"""
        m = re.search(r'<aside[\s\S]*?</aside>', self.html)
        self.assertTrue(m)
        aside = m.group(0)
        n_txt = aside.count('<span class="txt">')
        self.assertGreaterEqual(n_txt, 8, f"侧边栏带 .txt 的导航项过少: {n_txt}")

    def test_health_group_without_giftwatch_page(self):
        """礼物诊断独立页已废除(2026-10-16 并入录制历史),不许长回来。"""
        self.assertIn('id="group-health"', self.html, "系统健康要保留二级组")
        self.assertNotIn('data-view="giftwatch"', self.html, "礼物诊断导航入口还在")
        self.assertNotIn('id="view-giftwatch"', self.html, "礼物诊断独立页还在")
        self.assertNotIn("loadGiftDiag", self.html, "礼物诊断独立加载逻辑还在")
        self.assertIn('"/api/giftwatch?path="', self.html,
                      "按场次诊断接口未接(历史展开区诊断块的数据源)")

    def test_ops_group_with_two_modal_entries(self):
        self.assertIn('id="group-ops"', self.html, "运维工具组缺失")
        self.assertIn('id="nav-preflight"', self.html, "启动自检入口缺失")
        self.assertIn('id="nav-giftwatch-cfg"', self.html, "机器人告警入口缺失")

    def test_banner_replaced_by_modal(self):
        """自检横幅已废除:入口与展示全在「运维工具 → 启动自检」弹窗。"""
        self.assertNotIn('id="setup-banner"', self.html,
                         "自检横幅还在 —— 已改为弹窗,别再恢复旧入口")
        self.assertIn('id="pf-modal"', self.html)
        self.assertIn('id="pf-list"', self.html)
        self.assertIn('id="pf-badge"', self.html, "自检告警徽标缺失(主动暴露能力不能丢)")

    def test_giftwatch_cfg_modal(self):
        self.assertIn('id="gw-modal"', self.html)
        self.assertIn('id="gwc-test"', self.html, "测试通知按钮缺失")
        self.assertIn('id="gwc-save"', self.html)
        self.assertIn('"/api/gift-watch/test"', self.html, "测试通知接口未接")

    def test_history_gift_badge_no_details(self):
        """历史页礼物区(2026-10-16 二次瘦身定案):用户明确「不需要具体的礼物情况」。

        下线守卫:摘要条(g-strip)/礼物榜/gift_stats 明细不许回潮;
        2026-10-30 追加:匿名可见性抽检卡整体下线,calib-* 锚点不得复活;
        保留守卫:可见度胶囊 + ▲诊断警示 + 展开区诊断块。
        """
        m = re.search(r'<section class="view" id="view-history"[\s\S]*?</section>',
                      self.html)
        self.assertTrue(m, "历史页区块缺失")
        hist = m.group(0)
        for anchor in ('id="calib-rooms"', 'id="calib-run"', 'id="calib-refresh"',
                       'id="calib-sum"', 'id="calib-plan"'):
            self.assertNotIn(anchor, hist,
                             f"匿名可见性抽检已下线(2026-10-30),不许回潮: {anchor}")
        self.assertNotIn("loadGiftCalib", self.html,
                         "抽检已下线,前端不得再请求 /api/gift-calib")
        self.assertNotIn('class="g-strip"', self.html,
                         "摘要条已下线(不需要具体的礼物情况),不许回潮")
        self.assertNotIn("礼物榜", self.html, "礼物榜已下线,不许回潮")
        self.assertNotIn("gift_stats", self.html, "gift_stats 明细已下线,前端不得引用")
        self.assertIn("gift_diag", self.html, "诊断计数未接(警示胶囊数据源)")
        self.assertIn('g-diag-warn', self.html, "诊断警示胶囊样式缺失")
        self.assertIn("${diagPill}", self.html, "诊断警示胶囊没有渲染进行模板")
        self.assertIn("gift_integrity", self.html, "完整性数据未接(可见度胶囊数据源)")
        self.assertIn('class="g-diag" data-diag-path', self.html, "展开区诊断块缺失")
        self.assertIn("function _hydrateDiag", self.html, "诊断块懒加载逻辑缺失")

    def test_gift_visibility_pill_and_modal(self):
        """礼物完整性胶囊(状态徽章后红框位置)+ 点击才展示的说明弹窗。

        铁律:录制中不展示(收尾才算 gift_integrity,数据契约保证+前端条件双保险)。
        2026-10-29 起胶囊读 gift_integrity.light(轻通道对账),老条目回退 ratio。
        2026-10-30 方案C:改成「4 格 + 百分比 + 满分圆点」,档位表是唯一真源,
        图例由表渲染 —— 旧版图例手写在 HTML 里,0 与 1/4 是同一个红点(ΔE00=0)。
        """
        self.assertIn("function giftVisOf", self.html, "档位映射函数缺失")
        self.assertIn("function giftVisPill", self.html, "胶囊渲染函数缺失")
        self.assertIn("function renderGiftVisLegend", self.html, "弹窗图例渲染函数缺失")
        self.assertIn("function _giftVisTip", self.html, "胶囊 tooltip 生成函数缺失")
        self.assertIn('data-gift-vis', self.html, "可见度胶囊缺失(红框位置)")
        self.assertIn("${visPill}", self.html,
                      "胶囊没有渲染进历史行模板(只定义不渲染 = 白做)")
        self.assertIn("!recording && gi", self.html,
                      "胶囊必须带录制中不渲染条件")
        self.assertIn("const lt = gi.light || null", self.html,
                      "胶囊必须优先读轻通道对账(gift_integrity.light)")
        self.assertIn("h-pill g-vis lv", self.html, "胶囊样式类缺失(应在 giftVisPill 里拼出来)")
        self.assertIn(".g-vis .gv-cell", self.html, "可见度格子样式缺失")
        # 方案C 三条新通道:百分比数字 / 满分圆点 / 空心格(未对账)
        self.assertIn(".g-vis .gv-pct", self.html, "百分比数字样式缺失")
        self.assertIn(".g-vis .gv-dot", self.html, "满分圆点样式缺失")
        self.assertIn(".g-vis .gv-cell.h", self.html, "空心格(未对账)样式缺失")
        self.assertIn('class="gv-pct"', self.html, "百分比没有渲染进胶囊")
        self.assertIn('class="gv-dot"', self.html, "满分圆点没有渲染进胶囊")
        self.assertIn("min-height: 20px", self.html,
                      "胶囊必须与文字胶囊等高(旧的 4 格胶囊只有 14px,行里矮一截)")
        self.assertIn("GIFT_VIS_TIERS", self.html, "档位表(唯一真源)缺失")
        self.assertIn('id="gift-vis-tiers"', self.html, "档位表 JSON 段缺失")
        self.assertIn("renderGiftVisLegend();", self.html, "图例没有初始化渲染")
        self.assertIn('id="gift-vis-modal"', self.html, "说明弹窗缺失")
        self.assertIn('id="gvc-close"', self.html, "说明弹窗关闭按钮缺失")
        self.assertIn('id="gvc-legend"', self.html, "说明弹窗图例容器缺失")
        self.assertIn("六个档位", self.html, "说明弹窗缺六档说明")
        self.assertIn("无数据档多数不是故障", self.html, "说明弹窗缺无数据档语义警示")
        self.assertIn("为什么不看送礼人和礼物名", self.html,
                      "说明弹窗必须写清轻通道不含送礼人/礼物名(否则用户以为漏采)")
        self.assertIn('closest("[data-gift-vis]")', self.html, "胶囊点击绑定缺失")
        # 刻度说法已下线:弹窗是唯一对用户解释档位的地方,里面不许再有 1/4、2/4 这类分数名
        seg = self.html[self.html.index('id="gift-vis-modal"'):self.html.index('id="gift-vis-tiers"')]
        for dead in ("1/4", "2/4", "3/4", "4/4"):
            self.assertNotIn(dead, seg,
                             f"档位名已改百分比,弹窗里不许回潮刻度说法: {dead}")
        self.assertNotIn("GIFT_VIS_CN", self.html, "旧的五档常量已下线(改读档位表)")
        # lvN 颜色类下线了(颜色改由档位表注入),回来就又会出现「相邻两档同色」。
        # lv3 那个 opacity .75 就是「3/4 与 4/4 同色(ΔE00=12)」的祸根。
        for n in range(6):
            self.assertNotIn(f".g-vis.lv{n}", self.html,
                             "lvN 颜色类已下线(颜色从档位表注入),不许回潮")

    def test_view_switch_no_calib_request(self):
        """历史页不再触发抽检档案请求(2026-10-30 下线守卫)。

        抽检卡是历史页唯一的进入时拉取 —— 下线后 showView 里不该再有 history 分支,
        有分支就说明又挂了一个「进页面才拉」的数据源,而抽检已经不存在了。
        """
        i = self.html.index("function showView")
        seg = self.html[i: i + 900]
        self.assertNotIn("loadGiftCalib", seg, "抽检已下线,进历史页不得再拉档案")
        self.assertNotIn('"history"', seg, "历史页不该再有进入时拉取的分支")


if __name__ == "__main__":
    unittest.main()
