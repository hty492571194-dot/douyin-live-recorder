#!/usr/bin/env python3
"""契约周报(contract-digest,每周日 21:00 automation 触发;也可手动跑)。

内容(方案 v1.0 §3.1 P2-周报):
  ① 契约快照核对:真源 vs NAS _对接/contract/ 的 sha256(漂移 → 告警条目);
  ② 本周消费的问题反馈汇总:从 _对接/问题反馈/已处理/ 里取 ts 落在近 7 天的包;
  ③ 本周投递的诊断包清单:_对接/诊断包/ 近 7 天的 zip;
  ④ 契约变更:contract/CHANGELOG.md 里日期落在近 7 天的版本条目。

发送策略:有新条目 **或** 快照漂移才发(安静周不打扰);--dry-run 只打印不发送。
通道复用 notify.push_by_config(与问题反馈告警同一配置,webhook/连接器自动降级)。

用法:
    .venv/bin/python scripts/contract_digest.py            # 组装并按需发送
    .venv/bin/python scripts/contract_digest.py --dry-run  # 只打印
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import interop  # noqa: E402

WINDOW_DAYS = 7


def _changelog_recent(changelog_path, now=None):
    """CHANGELOG.md 里日期落在近 7 天的 `## vX.Y.Z(YYYY-MM-DD)` 条目头。"""
    try:
        with open(changelog_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    cutoff = (now or time.time()) - WINDOW_DAYS * 86400
    out = []
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        m = None
        for token in line.replace("(", " ").replace(")", " ").split():
            try:
                ts = time.mktime(datetime.strptime(token, "%Y-%m-%d").timetuple())
            except ValueError:
                continue
            m = ts
            break
        if m and m >= cutoff:
            out.append(line.lstrip("# ").strip())
    return out


def _feedback_recent(done_dir, now=None):
    """近 7 天消费的反馈包(读 JSON 的 ts 字段;坏包跳过不拦周报)。"""
    cutoff = (now or time.time()) - WINDOW_DAYS * 86400
    out = []
    try:
        names = sorted(os.listdir(done_dir))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(done_dir, n), encoding="utf-8") as f:
                rec = json.load(f)
            if float(rec.get("ts") or 0) >= cutoff:
                out.append(rec)
        except Exception:                          # noqa: BLE001
            continue
    return out


def _diag_recent(diag_dir, now=None):
    """近 7 天投递的诊断包(按落位 mtime)。"""
    cutoff = (now or time.time()) - WINDOW_DAYS * 86400
    try:
        names = sorted(os.listdir(diag_dir))
    except OSError:
        return []
    return [n for n in names if n.endswith(".zip")
            and os.path.getmtime(os.path.join(diag_dir, n)) >= cutoff]


def _snapshot_check(cfg):
    """契约快照核对。返回 (漂移列表, 文本行)。"""
    ok, info = interop.nas_paths(cfg)
    if not ok:
        return [], f"- NAS 不可用,快照未核对({info})"
    drift, lines = [], []
    for name in (interop.CONTRACT_FILE, interop.CHANGELOG_FILE,
                 interop.NOTICE_FILE):
        src = os.path.join(interop.CONTRACT_DIR, name)
        dst = os.path.join(info["contract_dir"], name)
        if not os.path.exists(src):
            if name == interop.NOTICE_FILE:
                continue          # 尚无通告文件:不算漂移
            drift.append(f"{name} 真源缺失")
            lines.append(f"- ✗ {name}: 真源缺失")
            continue
        if not os.path.exists(dst):
            drift.append(f"{name} 快照缺失")
            lines.append(f"- ✗ {name}: NAS 快照缺失(需跑 sync_contract.py)")
            continue
        same = interop._sha256(src) == interop._sha256(dst)
        lines.append(f"- {'✓' if same else '✗'} {name}: {'一致' if same else '与 NAS 快照不一致(需重跑 sync_contract.py)'}")
        if not same:
            drift.append(name)
    return drift, "\n".join(lines)


def _notice_recent(notice_path, now=None):
    """NOTICE.md 里时间落在近 7 天的条目头(`## [YYYY-MM-DD HH:MM] 类型 — 摘要`)。"""
    try:
        with open(notice_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    cutoff = (now or time.time()) - WINDOW_DAYS * 86400
    out = []
    for line in text.splitlines():
        if not line.startswith("## ["):
            continue
        try:
            end = line.index("]")
            stamp = line[4:end]          # '## [' 之后、']' 之前 = 'YYYY-MM-DD HH:MM'
            ts = time.mktime(
                datetime.strptime(stamp, "%Y-%m-%d %H:%M").timetuple())
        except (ValueError, IndexError):
            continue
        if ts >= cutoff:
            out.append(line.lstrip("# ").strip())
    return out


def build_digest(cfg, now=None):
    """组装周报。返回 (send: bool, text: str)。"""
    now = now or time.time()
    lines = [f"契约周报 · {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')}"]
    drift, snap_lines = _snapshot_check(cfg)
    lines.append("【契约快照核对】")
    lines.append(snap_lines)
    changed = _changelog_recent(os.path.join(interop.CONTRACT_DIR,
                                             interop.CHANGELOG_FILE), now)
    lines.append("【契约变更(近 7 天)】")
    lines.append("\n".join(f"- {c}" for c in changed) if changed else "- 无")
    notices = _notice_recent(os.path.join(interop.CONTRACT_DIR,
                                          interop.NOTICE_FILE), now)
    lines.append("【产物变更通告(近 7 天,S5)】")
    lines.append("\n".join(f"- {n}" for n in notices) if notices else "- 无")
    ok, info = interop.nas_paths(cfg)
    consumed, diags = [], []
    if ok:
        consumed = _feedback_recent(info["done_dir"], now)
        diags = _diag_recent(info["diag_dir"], now)
    else:
        lines.append(f"(NAS 不可用,本周消费/诊断包清单未核对:{info})")
    lines.append("【本周消费的问题反馈】")
    lines.append("\n".join(
        f"- [{rec.get('severity')}] {rec.get('session_key')} — {rec.get('summary')}"
        f"(stage={rec.get('stage')}, run_id={rec.get('run_id')})"
        for rec in consumed) if consumed else "- 无")
    lines.append("【本周投递的诊断包】")
    lines.append("\n".join(f"- {n}" for n in diags) if diags else "- 无")
    send = bool(changed or consumed or diags or drift)
    return send, "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="契约周报(contract-digest)")
    ap.add_argument("--dry-run", action="store_true", help="只打印,不发送")
    ap.add_argument("--force", action="store_true", help="安静周也发送")
    args = ap.parse_args()
    cfg = interop.load_cfg()
    send, text = build_digest(cfg)
    print(text)
    if args.dry_run:
        print(f"\n[dry-run] send={send}")
        return 0
    if not send and not args.force:
        print("\n安静周(无变更/无消费/无漂移),不发送")
        return 0
    import asyncio
    import notify as notify_mod
    first = text.splitlines()[0]
    body = "\n".join(text.splitlines()[1:])
    result = asyncio.run(notify_mod.push_by_config(
        cfg.get("gift_watch") or {}, first, body))
    print(f"\n发送结果: {result}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
