"""契约守卫测试 —— 真契约必须过;故意改坏必须报红(破坏性验证后恢复)。

check_contract_sync.check_all 的参数全部可注入,这里用临时目录做破坏性验证,
真实仓库的 contract/ 则作为「当前必须一致」的常驻断言。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interop  # noqa: E402

# scripts/ 不是包,按 test_sync_github.py 的惯例用 importlib 加载守卫脚本
_spec = importlib.util.spec_from_file_location(
    "check_contract_sync", os.path.join(BASE, "scripts", "check_contract_sync.py"))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _load(name):
    with open(os.path.join(interop.CONTRACT_DIR, name), encoding="utf-8") as f:
        return f.read()


def _mk_contract_dir(tmp, doc=None, changelog=None):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, interop.CONTRACT_FILE), "w", encoding="utf-8") as f:
        if isinstance(doc, str):
            f.write(doc)
        else:
            json.dump(doc if doc is not None else json.loads(_load("contract.json")),
                      f, ensure_ascii=False, indent=2)
    with open(os.path.join(tmp, interop.CHANGELOG_FILE), "w", encoding="utf-8") as f:
        f.write(changelog if changelog is not None else _load("CHANGELOG.md"))
    return tmp


class TestRealContractAlwaysValid(unittest.TestCase):
    def test_repo_contract_passes_guard(self):
        self.assertEqual(guard.check_all(), [])


class TestDestructiveVerification(unittest.TestCase):
    """改坏 → 必须报红(每一类检查都要证明它真能拦住)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="contract-guard-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _doc(self):
        return json.loads(_load("contract.json"))

    def test_broken_version_reports(self):
        doc = self._doc()
        doc["contract_version"] = "one.point.zero"
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("semver" in i for i in issues))

    def test_missing_section_reports(self):
        doc = self._doc()
        doc.pop("timestamps")
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("timestamps" in i for i in issues))

    def test_wrong_ready_signal_reports(self):
        doc = self._doc()
        doc["naming"]["ready_signal"] = "meta.json"
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("ready_signal" in i for i in issues))

    def test_missing_changelog_entry_reports(self):
        doc = self._doc()
        issues = guard.check_all(contract_dir=_mk_contract_dir(
            self.tmp, doc=doc,
            changelog="# 契约变更记录\n\n(没有版本条目)\n"))
        self.assertTrue(any("CHANGELOG" in i for i in issues))

    def test_missing_changelog_file_reports(self):
        tmp = _mk_contract_dir(self.tmp)
        os.remove(os.path.join(tmp, interop.CHANGELOG_FILE))
        issues = guard.check_all(contract_dir=tmp)
        self.assertTrue(any("CHANGELOG" in i for i in issues))

    def test_dropped_timebase_reports(self):
        doc = self._doc()
        doc["timestamps"] = ["无关条目一", "无关条目二"]
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("anchors" in i or "场次相对秒" in i for i in issues))

    def test_missing_sidecar_entry_reports(self):
        doc = self._doc()
        doc["files"] = [f for f in doc["files"] if ".align.json" not in f["path"]]
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any(".align.json" in i for i in issues))

    def test_unparsable_contract_reports(self):
        issues = guard.check_all(contract_dir=_mk_contract_dir(
            self.tmp, doc='{"contract_version": '))
        self.assertTrue(any("不可解析" in i for i in issues))

    def test_ready_signal_must_match_nas_source(self):
        # 契约说就绪信号是 <日期>.meta.json;把 nas.py 源码换成没有该行为的假源码必须报红
        doc = self._doc()
        tmp = _mk_contract_dir(self.tmp, doc=doc)
        issues = guard.check_all(contract_dir=tmp, nas_py="def _write_meta():\n    pass\n")
        self.assertTrue(any("ready_signal" in i or "_write_meta" in i for i in issues))

    def test_missing_producer_duties_reports(self):
        # 删掉 producer_duties = 产调不告知后端的机制性回归,必须报红
        doc = self._doc()
        doc.pop("producer_duties")
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("producer_duties" in i for i in issues))

    def test_duties_without_notice_channel_reports(self):
        doc = self._doc()
        doc["producer_duties"] = {"rule": "x", "steps": ["① x", "② y"]}
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("NOTICE.md" in i for i in issues))

    def test_duties_without_entrypoint_reports(self):
        doc = self._doc()
        doc["producer_duties"] = {
            "notice_channel": "_对接/contract/NOTICE.md",
            "steps": ["① 手工发消息", "② 电话通知"]}
        issues = guard.check_all(contract_dir=_mk_contract_dir(self.tmp, doc=doc))
        self.assertTrue(any("notify_backend" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
