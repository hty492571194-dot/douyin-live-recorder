#!/usr/bin/env python3
"""契约 ↔ 代码 双向对账守卫(纳入 unittest:tests/test_contract_sync.py)。

只做「确定的」不一致检查,避免误报把人训练成无视告警:
 ① contract.json 可解析、contract_version 是 semver、必备段齐全;
 ② CHANGELOG.md 含当前版本条目(防「升了版本忘写变更记录」);
 ③ naming.ready_signal 与 nas.py 真实行为一致(源码确实在写 <日期>.meta.json);
 ④ naming.session_key 模板形状正确;
 ⑤ timestamps 表含 anchors / 场次相对秒 条目(时间基唯一口径不许丢);
 ⑥ files 清单覆盖四件套(flv/ass/danmaku.jsonl/align.json);
 ⑦ producer_duties 段齐全(产物变更通告义务,S5,契约 v1.1.0 起);
 ⑧ capabilities.gift_display 的每个默认值必须与 gift_display.DEFAULTS 逐项相等
    (契约 v1.5.0 起)。礼物展示规则只有一份,契约里抄错一个阈值就等于两份规则。

用法:
    python scripts/check_contract_sync.py     # 有问题退出码 1
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import interop  # noqa: E402

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
REQUIRED_SECTIONS = ("contract_version", "files", "naming", "timestamps",
                     "capabilities", "quality_facts", "security", "diagnostics",
                     "producer_duties")


def check_all(contract_dir=None, nas_py=None, changelog=None):
    """返回问题列表(空 = 一致)。参数可注入,供测试用临时目录做破坏性验证。"""
    cdir = contract_dir or interop.CONTRACT_DIR
    issues = []
    cpath = os.path.join(cdir, interop.CONTRACT_FILE)
    try:
        with open(cpath, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:                         # noqa: BLE001
        return [f"contract.json 不可解析: {e}"]

    ver = str(doc.get("contract_version") or "")
    if not SEMVER_RE.match(ver):
        issues.append(f"contract_version 不是 semver(X.Y.Z): {ver!r}")
    for sec in REQUIRED_SECTIONS:
        if sec not in doc:
            issues.append(f"缺少必备段: {sec}")

    naming = doc.get("naming") or {}
    if naming.get("ready_signal") != "<日期>.meta.json":
        issues.append("naming.ready_signal 必须是 '<日期>.meta.json'(就绪信号唯一判据)")
    if not str(naming.get("session_key") or "").startswith("<主播>/<YYYY-MM-DD>/"):
        issues.append("naming.session_key 模板应为 '<主播>/<YYYY-MM-DD>/<基名>'")

    src = nas_py if nas_py is not None else _read(
        os.path.join(BASE, "nas.py"))
    if src is not None:
        if "_write_meta" not in src or ".meta.json" not in src:
            issues.append("nas.py 里找不到就绪信号写入(_write_meta → *.meta.json),"
                          "与契约 naming.ready_signal 不一致")

    ts = doc.get("timestamps") or []
    joined = "\n".join(str(x) for x in ts)
    if "anchors" not in joined or "场次相对秒" not in joined:
        issues.append("timestamps 表必须包含 anchors 与「场次相对秒」唯一口径条目")

    files_blob = json.dumps(doc.get("files") or [], ensure_ascii=False)
    for token in (".flv", ".ass", ".danmaku.jsonl", ".align.json", ".meta.json"):
        if token not in files_blob:
            issues.append(f"files 清单缺少 {token} 相关条目")

    # ⑦ producer_duties(S5 产物变更通告义务,契约 v1.1.0 起):段必须存在,
    #    通告通道必须指向 _对接/contract/NOTICE.md,履行步骤 ≥2 条。
    #    删掉这段 = 产调不告知后端,机制性回归,红线拦截。
    duties = doc.get("producer_duties") or {}
    blob = json.dumps(duties, ensure_ascii=False)
    if not duties:
        issues.append("缺少 producer_duties 段(产物变更通告义务,契约 v1.1.0 起必备)")
    else:
        if "NOTICE.md" not in blob:
            issues.append("producer_duties.notice_channel 必须指向 _对接/contract/NOTICE.md")
        if not isinstance(duties.get("steps"), list) or len(duties["steps"]) < 2:
            issues.append("producer_duties.steps 必须是 ≥2 条的履行步骤清单")
        if "notify_backend" not in blob:
            issues.append("producer_duties.steps 必须引用唯一入口 scripts/notify_backend.py")

    # ⑧ gift_display:契约里的每个默认值都要与代码一致(唯一真源守卫)
    gdcap = (doc.get("capabilities") or {}).get("gift_display")
    if gdcap is None:
        issues.append("缺少 capabilities.gift_display 段(契约 v1.5.0 起必备)")
    else:
        try:
            import gift_display as _gd
            for key, default in _gd.DEFAULTS.items():
                if key not in gdcap:
                    issues.append(f"capabilities.gift_display 缺少 {key}")
                elif gdcap[key] != default:
                    issues.append(
                        f"capabilities.gift_display.{key}={gdcap[key]!r} 与 "
                        f"gift_display.DEFAULTS={default!r} 不一致(规则必须单一真源)")
            if gdcap.get("version") != _gd.RULE_VERSION:
                issues.append(f"capabilities.gift_display.version={gdcap.get('version')!r} "
                              f"≠ gift_display.RULE_VERSION={_gd.RULE_VERSION!r}")
            fixture = gdcap.get("golden_fixture")
            if fixture:
                fpath = os.path.join(BASE, fixture)
                if not os.path.exists(fpath):
                    issues.append(f"黄金样例缺失: {fixture}")
        except ImportError as e:
            issues.append(f"无法导入 gift_display 做一致性校验: {e}")

    cl = changelog if changelog is not None else _read(
        os.path.join(cdir, interop.CHANGELOG_FILE))
    if cl is None:
        issues.append("CHANGELOG.md 缺失")
    elif ver and not re.search(r"^##\s*v?%s\b" % re.escape(ver), cl, re.M):
        issues.append(f"CHANGELOG.md 没有 v{ver} 的条目(版本↔变更记录必须联动)")

    return issues


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def main():
    issues = check_all()
    if issues:
        for i in issues:
            print(f"✗ {i}")
        return 1
    print(f"✓ 契约 v{interop.contract_version()} 与代码/变更记录一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
