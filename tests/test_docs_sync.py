#!/usr/bin/env python3
"""文档同步检查的自检。

目的不是测脚本本身,而是守住一条约定:**代码改了,文档必须跟着改**。
check_docs_sync.py 检测的是「文档里写的事实 vs 代码真实状态」,这个测试
再补一层:确认它确实抓得住不一致,否则哪天脚本失效了没人知道。

注:全部用打桩构造文档内容,不在项目根落任何临时文件——根目录的 .md 会被
_docs() 收进检查范围,落盘再删容易残留、污染真实检查结果。
"""
import importlib.util
import os
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SPEC = importlib.util.spec_from_file_location(
    "check_docs_sync", os.path.join(BASE, "scripts", "check_docs_sync.py"))
check_docs_sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_docs_sync)


class _FakeDocMixin:
    """把一份虚构文档塞进检查范围,其余文档仍读真实内容。"""

    def _with_doc(self, name, content):
        orig_docs, orig_read = check_docs_sync._docs, check_docs_sync._read

        def fake_docs():
            docs = list(orig_docs())
            if name not in docs:
                docs.append(name)
            return docs

        def fake_read(rel):
            return content if rel == name else orig_read(rel)

        check_docs_sync._docs = fake_docs
        check_docs_sync._read = fake_read
        self.addCleanup(setattr, check_docs_sync, "_docs", orig_docs)
        self.addCleanup(setattr, check_docs_sync, "_read", orig_read)


class TestDocsSync(_FakeDocMixin, unittest.TestCase):
    def test_no_drift_now(self):
        """当前状态:文档与代码一致。"""
        issues = check_docs_sync.check_all()
        self.assertEqual([], issues, "文档与代码不一致:\n  " + "\n  ".join(issues))

    def test_detects_stale_root_path(self):
        """文档里残留迁移前的旧项目根 → 必须被抓出来。"""
        self._with_doc("zz-fake.md",
                       "# 临时\n项目根：`/Users/a/Downloads/douyin-monitor-phase-d-19-g3fe4950`\n")
        issues = check_docs_sync.check_all()
        self.assertTrue(any("zz-fake.md" in i for i in issues),
                        "旧项目根没被检出: %s" % issues)

    def test_detects_port_mismatch(self):
        """文档里写的端口与 config.json 不符 → 必须被抓出来。"""
        self._with_doc("zz-fake.md", "# 临时\n打开 http://127.0.0.1:9999 查看\n")
        issues = check_docs_sync.check_all()
        self.assertTrue(any("9999" in i for i in issues),
                        "端口不一致没被检出: %s" % issues)

    def test_history_mention_allowed(self):
        """明确标注为历史的旧路径引用不应误报。"""
        self._with_doc("zz-fake.md",
                       "# 临时\n迁移历史：由 douyin-monitor-phase-d-19-g3fe4950 改名而来\n")
        issues = check_docs_sync.check_all()
        self.assertFalse(any("zz-fake.md" in i for i in issues),
                         "历史引用被误报: %s" % issues)

    def test_detects_unregistered_module(self):
        """根目录出现说明书没登记的新 .py → 必须被抓出来。"""
        self._with_doc("zz-fake.md", "# 临时\n")
        orig = check_docs_sync._read
        # 只伪造说明书内容:去掉一个真实模块名,模拟"新增模块忘了写文档"
        def fake_read(rel):
            txt = orig(rel)
            if rel == "项目框架说明书.md" and txt and "retention.py" in txt:
                return txt.replace("retention.py", " ")
            return txt
        check_docs_sync._read = fake_read
        self.addCleanup(setattr, check_docs_sync, "_read", orig)
        issues = check_docs_sync.check_all()
        self.assertTrue(any("retention.py" in i for i in issues),
                        "未登记模块没被检出: %s" % issues)

    def _with_guide(self, extra):
        """在真实说明书末尾追加一段,其余文档仍读真实内容。"""
        orig = check_docs_sync._read

        def fake_read(rel):
            txt = orig(rel)
            if rel == "项目框架说明书.md" and txt:
                return txt + "\n" + extra
            return txt
        check_docs_sync._read = fake_read
        self.addCleanup(setattr, check_docs_sync, "_read", orig)
        return check_docs_sync.check_all()

    def test_detects_bogus_endpoint(self):
        """说明书里写了代码里不存在的接口 → 必须被抓出来。"""
        issues = self._with_guide("| 假接口 | `GET /api/zz-not-exist` | 临时 |")
        self.assertTrue(any("/api/zz-not-exist" in i for i in issues),
                        "不存在的接口没被检出: %s" % issues)

    def test_real_endpoint_not_flagged(self):
        """真存在的接口不许误报,前缀路由(/api/previews/)也要认。"""
        issues = self._with_guide("`GET /api/shortcuts`、`GET /api/previews/<主播>/<文件>`")
        self.assertFalse(any("/api/shortcuts" in i or "/api/previews" in i
                             for i in issues),
                         "真接口被误报: %s" % issues)

    def test_placeholder_endpoint_ignored(self):
        """写文档模板用的占位符(/api/xxx、/api/<主播>、/api/{id})不该被当成真接口。"""
        issues = self._with_guide(
            "接口写成 `/api/xxx`、`/api/<主播>/refresh`、`/api/{id}` 这类模板。")
        self.assertFalse(any("/api/xxx" in i or "/api/<" in i or "/api/{" in i
                             for i in issues),
                         "占位符被误报: %s" % issues)


if __name__ == "__main__":
    unittest.main()

class TestLineRefDrift(unittest.TestCase):
    """`符号(L###)` 与 `xxx.py (N 行)` 的漂移必须被抓住（2026-09-19 新增）。

    为什么值得单独一条:行号/行数是最容易腐烂的引用 —— 改一处代码,后面所有
    行号全错,而旧行号看起来依然"很具体",比不写更误导。实测说明书里 7 处
    `符号(L###)` **全部**过期(monitor.py 从 1618 行长到 1821 行,偏移上百行),
    两个模块行数也各差几十行。
    """

    def test_real_guide_has_no_line_ref_drift(self):
        """真实说明书里不能有漂移。"""
        guide = check_docs_sync._read("项目框架说明书.md") or ""
        self.assertEqual(check_docs_sync._check_symbol_line_refs(guide), [])
        self.assertEqual(check_docs_sync._check_module_line_counts(guide), [])

    def test_detects_stale_symbol_line(self):
        issues = check_docs_sync._check_symbol_line_refs("见 `run_streamer`(L1) 的实现")
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("run_streamer", issues[0])
        self.assertIn("但它在", issues[0])

    def test_accepts_correct_symbol_line(self):
        hits = check_docs_sync._definitions("run_streamer")
        self.assertTrue(hits, "run_streamer 应该能找到定义")
        _f, real = hits[0]
        issues = check_docs_sync._check_symbol_line_refs("见 `run_streamer`(L%d)" % real)
        self.assertEqual(issues, [])

    def test_ignores_unknown_symbol(self):
        """外部库/概念名不该被误报。"""
        self.assertEqual(
            check_docs_sync._check_symbol_line_refs("见 `AsyncClient`(L42)"), [])

    def test_ambiguous_symbol_not_reported(self):
        """同名多处定义时无法判定,宁可放过也不误伤。"""
        orig = check_docs_sync._definitions
        check_docs_sync._definitions = lambda name: [("a.py", 10), ("b.py", 99)]
        try:
            self.assertEqual(
                check_docs_sync._check_symbol_line_refs("`dup`(L5)"), [])
        finally:
            check_docs_sync._definitions = orig

    def test_detects_wrong_module_line_count(self):
        issues = check_docs_sync._check_module_line_counts("├── monitor.py   (999 行)")
        self.assertEqual(len(issues), 1, issues)
        self.assertIn("monitor.py", issues[0])
        self.assertIn("999", issues[0])

    def test_accepts_correct_module_line_count(self):
        with open(os.path.join(BASE, "monitor.py"), encoding="utf-8") as f:
            real = len(f.read().splitlines())
        self.assertEqual(
            check_docs_sync._check_module_line_counts("├── monitor.py   (%d 行)" % real), [])

    def test_ignores_unknown_module(self):
        self.assertEqual(
            check_docs_sync._check_module_line_counts("├── nope.py   (1 行)"), [])
