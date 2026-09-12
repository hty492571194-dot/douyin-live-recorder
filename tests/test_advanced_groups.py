"""「监控与开播检测」卡片分组表的结构不变量。

这张卡从「25 个字段平铺」改成「按用途分 5 组 + 组内一行一字段 + 不常改的组默认收起」,
风险集中在两处:

  ① 把扁平表挪进分组表时漏字段或写错路径 —— 前端照样渲染出输入框,
     但保存下去的键后端不认,表现成「改了没反应」,极难察觉;
  ② 折叠若改成从 DOM 里摘掉节点 —— 收起组里的字段会被 collectFields 漏掉,
     一按「保存并应用」就把那些键清空(config 的 monitors 是整体替换语义,
     这类静默丢配置在本项目已经发生过一次)。

所以这里不看「渲染出来长什么样」,而是把 index.html 里的 ADVANCED_GROUPS
当数据解析出来,与后端 monitor.DEFAULT_CONFIG 双向对账:
  · 后端登记的可配键(弹幕 / 停滞自愈 / 检测 Cookie / 钩子命令)必须都在表里 —— 防漏;
  · 表里的路径必须都能在后端配置里解析到 —— 防写错路径、防失效键;
  · 路径不得重复,也不得与「常用参数」卡重复 —— 同一个键两处渲染会互相覆盖;
  · 折叠必须是 class 切换而非摘节点 —— 防保存漏项。

字段表是唯一真源:加字段只改 ADVANCED_GROUPS,本测试把「只改一处」从
口头约定变成可验证的约束。
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import monitor
import webui

BASE = webui.BASE

# 字段项:[路径, 标签, 类型, 单位/选项, 说明]。第 4 位有三种形态 ——
# null(text/textarea 无单位)、"秒"(number 的单位)、["queue","scroll"](select 的选项);
# 要求第 3 位是已知类型,才能把字段项与表里其它方括号区分开,避免误匹配。
FIELD_RE = re.compile(
    r'\[\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,'
    r'\s*"(text|number|select|bool|textarea)"\s*,\s*(null|\[|"[^"]*")')
KNOWN_TYPES = {"text", "number", "select", "bool", "textarea"}


def _read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def _balanced(src, anchor, open_ch, close_ch):
    """从 anchor 起取出第一个配平的括号块(表里没有括号出现在字符串里,够用)。"""
    i = src.index(anchor)
    j = src.index(open_ch, i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == open_ch:
            depth += 1
        elif src[k] == close_ch:
            depth -= 1
            if depth == 0:
                return src[j:k + 1]
    raise AssertionError("未闭合: " + anchor)


def _top_level_objects(block):
    """拆出 [...] 里的一级 { ... } 对象文本。"""
    out, depth, start = [], 0, None
    for i, ch in enumerate(block):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append(block[start:i + 1])
    return out


def _parse_groups(html):
    block = _balanced(html, "const ADVANCED_GROUPS = [", "[", "]")
    groups = []
    for text in _top_level_objects(block):
        name = re.search(r'name:\s*"([^"]+)"', text)
        note = re.search(r'note:\s*"([^"]+)"', text)
        open_ = re.search(r"open:\s*(true|false)", text)
        fields = [{"path": p, "label": lab, "type": t, "opts": u == "["}
                  for p, lab, t, u in FIELD_RE.findall(text)]
        groups.append({
            "name": name.group(1) if name else "",
            "note": note.group(1) if note else "",
            "open": (open_.group(1) == "true") if open_ else None,
            "fields": fields,
            "raw": text,
        })
    return groups


def _cfg_get(cfg, path):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class TestAdvancedGroupTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read("static/index.html")
        cls.groups = _parse_groups(cls.html)
        cls.paths = [f["path"] for g in cls.groups for f in g["fields"]]
        cls.cfg = monitor.DEFAULT_CONFIG

    # ── 表本身 ──
    def test_groups_are_non_empty_and_well_formed(self):
        """组名 / 组头说明 / 默认态 / 组内字段一个都不许缺。"""
        self.assertGreaterEqual(len(self.groups), 2, "至少要分成 2 组")
        for g in self.groups:
            self.assertTrue(g["name"].strip(), g)
            self.assertTrue(g["note"].strip(), g)
            self.assertIsNotNone(g["open"], "缺 open 默认态: " + g["name"])
            self.assertTrue(g["fields"], "空组: " + g["name"])

    def test_group_names_unique(self):
        names = [g["name"] for g in self.groups]
        self.assertEqual(len(names), len(set(names)), "组名重复会让折叠记忆互相覆盖")

    def test_field_labels_non_empty(self):
        for g in self.groups:
            for f in g["fields"]:
                self.assertTrue(f["label"].strip(), f)

    def test_types_are_known(self):
        for g in self.groups:
            for f in g["fields"]:
                self.assertIn(f["type"], KNOWN_TYPES, f)

    def test_select_fields_have_options(self):
        """select 的第 4 位必须是选项数组,否则渲染出来的下拉是空的。"""
        for g in self.groups:
            for f in g["fields"]:
                if f["type"] == "select":
                    self.assertTrue(f["opts"], "select 缺选项数组: " + f["path"])

    # ── 与后端配置对账 ──
    def test_covers_every_danmaku_key(self):
        """后端登记的每个 danmaku.* 键都必须能在卡片上改到。"""
        for key in self.cfg["danmaku"]:
            self.assertIn("danmaku." + key, self.paths, "漏了弹幕字段: " + key)

    def test_covers_every_stall_key(self):
        """停滞自愈三键:后端读它们、页面也该能改。"""
        stall = [k for k in self.cfg["recorder"] if k.startswith("stall_")]
        self.assertTrue(stall, "默认配置里没有 stall_* 键")
        for key in stall:
            self.assertIn("recorder." + key, self.paths, "漏了停滞自愈字段: " + key)

    def test_covers_detection_cookie_and_hooks(self):
        self.assertIn("detection.cookie", self.paths)
        for key in ("on_live_command", "on_offline_command"):
            self.assertIn(key, self.paths, "漏了钩子命令字段: " + key)

    def test_every_path_resolves_in_default_config(self):
        """反向:表里的路径必须在配置里真实存在 —— 挡住写错的键(改了不生效)。"""
        for path in self.paths:
            self.assertIsNotNone(_cfg_get(self.cfg, path),
                                 "配置里没有这个路径,保存上去也没人读: " + path)

    def test_no_duplicate_paths(self):
        dup = {p for p in self.paths if self.paths.count(p) > 1}
        self.assertEqual(dup, set(), "同一字段出现多次会互相覆盖: %s" % dup)

    def test_no_overlap_with_common_fields(self):
        """与「常用参数」卡不能有交集:两处渲染同一个键,回填与收集会打架。"""
        common = set(re.findall(r'\["([a-z_]+\.[a-z_]+)",', self.html.split("const COMMON_FIELDS")[1]
                                .split("];")[0]))
        self.assertTrue(common, "没能解析出 COMMON_FIELDS")
        overlap = common & set(self.paths)
        self.assertEqual(overlap, set(), "同一字段出现在两张卡上: %s" % overlap)

    # ── 折叠行为 ──
    def test_some_groups_collapsed_by_default(self):
        """全默认展开的话折叠就白做了 —— 至少要有一组收起。"""
        collapsed = [g["name"] for g in self.groups if not g["open"]]
        self.assertTrue(collapsed, "没有任何默认收起的组")
        expanded = [g["name"] for g in self.groups if g["open"]]
        self.assertTrue(expanded, "全部默认收起会让首屏看不到可改项")

    def test_collapse_is_class_based_not_dom_removal(self):
        """折叠必须切 class:摘节点会让 collectFields 漏掉收起组里的字段。"""
        self.assertRegex(self.html, r"\.fg:not\(\.open\)\s+\.fg-body\s*\{\s*display:\s*none")
        body = self.html.split("function renderFieldGroups")[1].split("\nfunction ")[0]
        for bad in (".remove()", "removeChild", "innerHTML = \"\""):
            self.assertNotIn(bad, body, "折叠不许增删 DOM(会漏保存): " + bad)

    def test_toggle_all_wired(self):
        self.assertIn('id="adv-toggle-all"', self.html)
        self.assertIn('getElementById("adv-toggle-all").addEventListener("click"', self.html)

    def test_group_memo_persisted(self):
        """折叠态要落 localStorage,否则每次刷新都回到默认。"""
        self.assertIn('const ADV_GROUP_MEMO = "advGroupOpen"', self.html)
        self.assertIn("localStorage.setItem(ADV_GROUP_MEMO", self.html)

    def test_collect_fields_label_fallback_covers_rows(self):
        """数字校验报错时要用行内 label 报字段名 —— 新结构是 .frow,别只认 .field。"""
        self.assertIn('.closest(".frow, .field")', self.html)

    # ── 旧结构不许回潮 ──
    def test_legacy_tables_and_renderer_removed(self):
        for gone in ("ADVANCED_FIELDS", "ADVANCED_TEXT", "renderTextFields"):
            self.assertNotIn(gone, self.html, "旧字段表/渲染器残留,会出现两处真源: " + gone)

    def test_container_is_not_a_grid(self):
        """分组容器不能再挂 .grid(会把分组当格子铺开)。"""
        m = re.search(r'<div([^>]*id="grid-advanced"[^>]*)>', self.html)
        self.assertIsNotNone(m)
        self.assertNotIn('class="grid"', m.group(1))

    def test_init_uses_group_renderer(self):
        self.assertIn('renderFieldGroups(ADVANCED_GROUPS, "grid-advanced")', self.html)
        self.assertNotIn('renderTextFields(', self.html)

    def test_control_factory_is_shared(self):
        """两条渲染路径共用 makeControl —— 免得布尔文案/选项渲染各写一份。"""
        self.assertEqual(self.html.count("= makeControl(path, type, unit)"), 2,
                         "renderFields 与 renderFieldGroups 都应调用 makeControl")

    def test_js_syntax_ok(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 node,跳过前端语法检查")
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", self.html, re.S)
        self.assertTrue(blocks, "没找到 script 块")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write("\n;\n".join(blocks))
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, "前端 JS 语法错误:\n" + r.stderr)
        finally:
            os.unlink(tmp)


class TestParserSelfCheck(unittest.TestCase):
    """解析器自身的守卫:表结构变了要让它先报错,而不是静默解析出空表。"""

    def test_parser_finds_expected_shape(self):
        html = _read("static/index.html")
        groups = _parse_groups(html)
        self.assertGreaterEqual(len(groups), 2)
        total = sum(len(g["fields"]) for g in groups)
        self.assertGreaterEqual(total, 20, "解析出的字段太少,可能解析器失效了")
        for g in groups:
            self.assertTrue(g["note"], "组头说明丢了: " + g["name"])


if __name__ == "__main__":
    unittest.main()
