"""弹窗层级守卫:通用弹窗必须贴着视口,不许退回「贴文档顶部」。

2026-10-26 修的 bug:预览弹层那段 CSS 用了全局选择器 `.modal-mask{position:absolute}`,
把通用弹窗(启动自检 / 机器人告警 / 添加主播 / 礼物可见度说明)的 `fixed` 一起盖掉了。
`.modal-mask` 一改成 absolute,遮罩就以**文档左上角**为参照 —— 用户滚到历史页深处
点「礼物可见度」胶囊,弹窗弹到页面最顶端,得往上滚才看得见。

守的点:
  · 裸 `.modal-mask` 只允许有一条,且必须是 position: fixed(通用弹窗的外层遮罩);
  · 裸 `.modal-box` / `.modal-head` 也只允许一条(基类那一条),
    预览弹层自己的差异(absolute 遮罩 / 92vw / 16px 圆角)必须写在 `.modal` 作用域内;
  · 四个通用弹窗的打开入口统一走 openMaskModal(),不许再手写 classList.remove("hidden"),
    否则「打开时回滚内容区」这条行为会各写一份、迟早漂移;
  · 改定位不许把交互弄丢:可见度胶囊的点击链路要还在。
"""
import os
import re
import unittest

import webui

BASE = webui.BASE


def _html():
    with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as f:
        return f.read()


def _style():
    html = _html()
    m = re.search(r"<style>([\s\S]*?)</style>", html)
    assert m, "index.html 里没有 <style> 块"
    # 注释里带花括号/选择器字样的说明很多,先剔掉,免得选择器字符串被注释污染
    return re.sub(r"/\*[\s\S]*?\*/", "", m.group(1))


def _rules(style, selector):
    """返回 selector **恰好等于** selector 的规则体列表(带层级的变体不算)。"""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
        sels = [s.strip() for s in m.group(1).split(",")]
        if selector in sels:
            out.append(m.group(2))
    return out


class TestModalLayer(unittest.TestCase):
    """通用弹窗的定位与打开入口。"""

    def setUp(self):
        self.html = _html()
        self.style = _style()

    def test_generic_mask_is_fixed(self):
        """裸 .modal-mask 就是通用弹窗的外层遮罩:只有一条,且必须 fixed。"""
        bodies = _rules(self.style, ".modal-mask")
        self.assertEqual(len(bodies), 1,
                         f"裸 .modal-mask 被重复声明了 {len(bodies)} 次,"
                         "后写的那条会盖掉通用弹窗的定位(见文件头注释)")
        self.assertRegex(bodies[0], r"position:\s*fixed",
                         "通用弹窗遮罩必须 position: fixed,否则会以文档左上角为参照,"
                         "滚到页面深处打开时弹窗跑到最顶端")

    def test_modal_family_base_intact(self):
        """.modal 与 .modal.hidden 两条基类不许少 —— 少一条就变成「一进页面就弹出、
        还挡住所有点击」(改这段 CSS 时最容易顺手删掉,2026-10-26 就误删过一次)。"""
        bases = _rules(self.style, ".modal")
        self.assertEqual(len(bases), 1, f"裸 .modal 声明了 {len(bases)} 条,应恰好 1 条")
        self.assertRegex(bases[0], r"position:\s*fixed", ".modal 必须 fixed 铺满视口")
        self.assertRegex(bases[0], r"display:\s*flex", ".modal 靠 flex 居中")
        hides = _rules(self.style, ".modal.hidden")
        self.assertEqual(len(hides), 1,
                         ".modal.hidden 丢了 → 预览/备注弹层一直显示并挡住整页点击")
        self.assertRegex(hides[0], r"display:\s*none")

    def test_preview_overrides_scoped_under_modal(self):
        """预览弹层的差异必须限定在 .modal 内,不能污染通用弹窗。"""
        mask = _rules(self.style, ".modal .modal-mask")
        self.assertTrue(mask, "预览弹层的内层遮罩样式丢了(应写成 .modal .modal-mask)")
        self.assertRegex(mask[0], r"position:\s*absolute",
                         "预览弹层内层遮罩要 absolute 才能铺满 .modal 那一层")

    def test_preview_mask_does_not_cover_box(self):
        """遮罩不许压在弹窗上面 —— 压上去整个弹层就只剩「看得见、点不到」。

        2026-10-26 实测:裸 `.modal-mask` 带着 z-index:90,而预览弹层的遮罩是
        `.modal-box` 的**兄弟**节点,box 只有 position:relative(z-index:auto=0)。
        结果遮罩盖住整个弹窗,「关闭 / 上传图片 / 设为预览 / 删除」全部失效,
        elementFromPoint 命中的是 mask,Playwright 报 intercepts pointer events,
        用户感知就是「点了截图整体卡住、关不掉」。
        """
        masks = _rules(self.style, ".modal .modal-mask")
        self.assertTrue(masks)
        m = re.search(r"z-index:\s*(-?\d+)", masks[0])
        self.assertTrue(m, "预览弹层遮罩必须显式复位 z-index(裸规则带着 90)")
        self.assertLess(int(m.group(1)), 1, "遮罩的 z-index 必须低于 .modal-box")
        boxes = _rules(self.style, ".modal .modal-box")
        self.assertTrue(boxes, "`.modal .modal-box` 缺少层级声明")
        self.assertRegex(boxes[0], r"z-index:\s*[1-9]",
                         ".modal-box 必须 z-index >= 1,否则遮罩会盖住整个弹窗")

    def test_preview_mask_click_binds_its_own_mask(self):
        """点遮罩关闭必须绑在**预览弹层自己的**遮罩上。

        旧代码是 ``document.querySelector(".modal-mask")`` —— 命中的是 DOM 里第一个
        遮罩(添加主播那个),预览弹层等于没有遮罩出口;更糟的是点添加主播的遮罩
        会顺手把预览弹层关掉。
        """
        self.assertIn('querySelector("#preview-modal .modal-mask")', self.html)
        self.assertNotIn('document.querySelector(".modal-mask")', self.html,
                         "裸 querySelector 会命中别的弹窗的遮罩")

    def test_base_box_and_head_declared_once(self):
        """基类只许声明一次,预览差异走 .modal 作用域。"""
        for sel in (".modal-box", ".modal-head"):
            bodies = _rules(self.style, sel)
            self.assertEqual(len(bodies), 1,
                             f"裸 {sel} 有 {len(bodies)} 条声明;预览弹层的差异请写 "
                             f"`.modal {sel.lstrip('.')}` 形式")

    def test_generic_modals_open_via_helper(self):
        """四个通用弹窗统一走 openMaskModal()。"""
        self.assertIn("function openMaskModal(", self.html, "openMaskModal 助手缺失")
        for mid in ("add-modal", "pf-modal", "gw-modal", "gift-vis-modal"):
            self.assertIn(f'openMaskModal("{mid}")', self.html,
                          f"{mid} 没走 openMaskModal(),打开时不会回滚内容区")
            self.assertNotIn(f'getElementById("{mid}").classList.remove("hidden")',
                             self.html,
                             f"{mid} 还在手写打开逻辑,绕过 openMaskModal()")

    def test_open_helper_does_not_touch_offsets(self):
        """助手只负责显示 + 回滚,不许自己算 top/left(算出来的值会重新把弹窗推歪)。"""
        m = re.search(r"function openMaskModal\([\s\S]*?\n\}", self.html)
        self.assertTrue(m, "openMaskModal 函数体没找到")
        body = m.group(0)
        for bad in ("style.top", "style.left", "scrollIntoView", "scrollTo("):
            self.assertNotIn(bad, body,
                             f"openMaskModal 里出现了 {bad}:定位只能交给 CSS 的 fixed")

    def test_gift_vis_click_still_wired(self):
        """可见度胶囊 → 说明弹窗的链路不能因为改定位而断。"""
        self.assertIn('data-gift-vis', self.html, "可见度胶囊缺失")
        self.assertIn('closest("[data-gift-vis]")', self.html, "胶囊点击绑定缺失")
        self.assertIn('id="gift-vis-modal"', self.html, "说明弹窗缺失")
        m = re.search(r'closest\("\[data-gift-vis\]"\)[\s\S]{0,160}?return;', self.html)
        self.assertTrue(m, "胶囊点击后的处理分支没找到")
        self.assertIn('openMaskModal("gift-vis-modal")', m.group(0),
                      "点胶囊必须打开 gift-vis-modal")


if __name__ == "__main__":
    unittest.main()
