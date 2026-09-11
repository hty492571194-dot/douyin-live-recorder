"""「系统健康 → 终端快捷指令」卡片的单测(后端接口 + 前端接线)。

这张卡片展示的命令清单**不在前端硬编码**,而是后端 `GET /api/shortcuts` 现取,
后端又是直接读 `scripts/ctl.py` 的 COMMANDS 表 —— 与终端 `douyin help`、
「一键启动.command」窗口同源。这里守三件事:
  ① 接口吐出来的东西与 ctl.py 的真源逐字一致(不是抄一份);
  ② 真源读不到时接口降级报错,而不是把整个健康页带崩;
  ③ 前端只负责排版:静态 HTML 里不许出现写死的命令表,且必须挂在健康视图里。
"""
import json
import os
import re
import unittest

import webui

BASE = webui.BASE


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


class TestShortcutPayload(unittest.TestCase):
    def test_matches_source_of_truth(self):
        """接口内容 = ctl.cheatsheet(),一个字都不许改。"""
        d = webui._shortcut_help()
        sheet = webui._load_ctl().cheatsheet()
        self.assertTrue(d["ok"])
        self.assertEqual(d["rows"], list(sheet["rows"]))
        self.assertEqual(d["aliases"], list(sheet["aliases"]))
        self.assertEqual(d["options"], list(sheet["options"]))
        self.assertEqual(d["notes"], list(sheet["notes"]))
        self.assertEqual(d["install"], sheet["install"])

    def test_rows_cover_every_command(self):
        """COMMANDS 表里的每条命令都要出现在卡片上,且三列都不为空。"""
        ctl = webui._load_ctl()
        d = webui._shortcut_help()
        shown = {r["command"] for r in d["rows"]}
        self.assertEqual(shown, {c[0] for c in ctl.COMMANDS})
        for r in d["rows"]:
            self.assertTrue(r["usage"].startswith("douyin"), r)
            self.assertTrue(r["effect"].strip(), r)

    def test_alias_column_is_json_friendly(self):
        """别名要能直接喂给前端(JSON 里是数组,不带 None)。"""
        d = webui._shortcut_help()
        for r in d["rows"]:
            self.assertIsInstance(r["aliases"], list)
            self.assertTrue(all(isinstance(a, str) and a for a in r["aliases"]), r)
        # 能序列化 = 前端拿得到;顺带确认没有 tuple 之类的类型残留
        json.dumps(d, ensure_ascii=False)

    def test_reports_entry_installation(self):
        """入口在项目外(~/.local/bin),卡片要能标出「装了没」。"""
        d = webui._shortcut_help()
        self.assertTrue(d["entry"].endswith(os.path.join(".local", "bin", "douyin")))
        self.assertIsInstance(d["installed"], bool)

    def test_degrades_when_source_unreadable(self):
        """读不到 ctl.py 时给降级说明,不抛异常、不带崩健康页。"""
        orig = webui._load_ctl
        webui._load_ctl = lambda: (_ for _ in ()).throw(ImportError("模拟缺失"))
        self.addCleanup(setattr, webui, "_load_ctl", orig)

        d = webui._shortcut_help()
        self.assertFalse(d["ok"])
        self.assertIn("模拟缺失", d["error"])
        self.assertEqual(d["rows"], [])
        self.assertTrue(d["install"], "降级时也要告诉用户怎么修")
        json.dumps(d, ensure_ascii=False)


class TestCliJson(unittest.TestCase):
    def test_help_json_is_the_same_sheet(self):
        """`douyin help --json` 与 Web 卡片同源 —— 终端里能核对页面上看到的东西。"""
        import io
        import sys

        # 自己按路径加载 ctl(与 webui 同一种加载方式),顺便验证它不依赖 cwd
        ctl = webui._load_ctl()
        buf = io.StringIO()
        orig, sys.stdout = sys.stdout, buf
        try:
            rc = ctl.main(["help", "--json"])
        finally:
            sys.stdout = orig
        self.assertEqual(rc, ctl.SUCCESS)
        self.assertEqual(json.loads(buf.getvalue()), ctl.cheatsheet())


class TestFrontendWiring(unittest.TestCase):
    def setUp(self):
        self.html = _read(os.path.join("static", "index.html"))

    def test_card_lives_in_health_view(self):
        sec = self.html.split('id="view-health"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="sc-body"', sec, "快捷指令卡片不在系统健康视图里")
        self.assertIn('id="sc-sum"', sec)
        self.assertIn("终端快捷指令", sec)

    def test_loaded_when_entering_view(self):
        m = re.search(r'if \(name === "health"\) \{(.*?)\}', self.html, re.S)
        self.assertIsNotNone(m, "找不到 showView 里的 health 分支")
        self.assertIn("loadShortcuts", m.group(1), "进入健康页时没加载快捷指令")

    def test_table_is_rendered_from_api(self):
        """静态 HTML 里不许有写死的命令表(写死就会与 ctl.py 分叉)。"""
        self.assertIn('fetch("/api/shortcuts")', self.html)
        # 只留静态标记:去掉 <style>(那里本来就有 .sc-* 样式)与 <script>
        static = self.html.split("<script>", 1)[0]
        static = re.sub(r"<style>.*?</style>", "", static, flags=re.S)
        self.assertNotIn("sc-table", static, "命令表被写死在 HTML 里了")
        self.assertNotIn("sc-cmd", static, "命令格被写死在 HTML 里了")
        self.assertIn("d.rows", self.html, "表格应逐行由接口数据渲染")
        # 复制交互复用同一套胶囊逻辑(认 .dy-ic 图标),健康页要自己挂委托
        self.assertIn('querySelector(".dy-ic")', self.html)
        self.assertIn('getElementById("sc-body")', self.html)


if __name__ == "__main__":
    unittest.main()
