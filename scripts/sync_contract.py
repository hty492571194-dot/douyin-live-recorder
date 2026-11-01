#!/usr/bin/env python3
"""契约快照同步:contract/ 真源 → NAS `_对接/contract/`(原子写 + 回读校验)。

用法:
    python scripts/sync_contract.py           # 同步(幂等:内容不变时重写同内容)
    python scripts/sync_contract.py --check   # 只核对 NAS 快照与真源的 sha256

规则(契约 S1):
  - contract_version 变更时必须跑一次(守卫 scripts/check_contract_sync.py
    只保证「版本↔CHANGELOG 联动」,跨机分发靠本脚本);
  - 原子写(临时文件 + rename);写后回读 sha256,不一致即报错退出;
  - NAS 未挂载/不可用 → 报错退出(人工/CI 触发的动作,不静默、不重试)。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import interop  # noqa: E402

SYNC_FILES = (interop.CONTRACT_FILE, interop.CHANGELOG_FILE)
# S5 变更通告与契约同批投递(存在才同步:首次/清空后缺失不算失败,提示即可)
OPTIONAL_FILES = (interop.NOTICE_FILE,)


def _sha(p):
    return interop._sha256(p)


def _dst(info, name):
    return os.path.join(info["contract_dir"], name)


def check_only(cfg):
    ok, info = interop.nas_paths(cfg)
    if not ok:
        print(f"✗ NAS 不可用: {info}")
        return 1
    bad = 0
    for name in SYNC_FILES + OPTIONAL_FILES:
        src = os.path.join(interop.CONTRACT_DIR, name)
        dst = _dst(info, name)
        if not os.path.exists(src):
            if name in OPTIONAL_FILES:
                print(f"= {name}: 真源尚无(跳过)")
                continue
            print(f"✗ 真源缺失: {src}")
            bad += 1
            continue
        if not os.path.exists(dst):
            if name in OPTIONAL_FILES:
                print(f"= {name}: 快照缺失(通告未投递过,跑一次同步即可)")
                continue
            print(f"✗ 快照缺失: {dst}")
            bad += 1
            continue
        s, d = _sha(src), _sha(dst)
        mark = "✓" if s == d else "✗"
        print(f"{mark} {name}: 真源 {s[:12]}… / 快照 {d[:12]}…")
        if s != d:
            bad += 1
    print("结论: " + ("快照与真源一致" if bad == 0 else f"{bad} 项不一致,请重跑同步"))
    return 0 if bad == 0 else 1


def sync(cfg):
    ok, info = interop.nas_paths(cfg)
    if not ok:
        print(f"✗ NAS 不可用: {info}(不静默、不重试,恢复挂载后再跑)")
        return 1
    try:
        ver = interop.load_contract()["contract_version"]
    except Exception as e:                         # noqa: BLE001
        print(f"✗ 真源不可解析: {e}(先修真源,绝不投坏契约)")
        return 1
    os.makedirs(info["contract_dir"], exist_ok=True)
    fail = 0
    for name in SYNC_FILES + OPTIONAL_FILES:
        src = os.path.join(interop.CONTRACT_DIR, name)
        dst = _dst(info, name)
        if not os.path.exists(src):
            if name in OPTIONAL_FILES:
                print(f"= {name}: 真源尚无(跳过,产物未变更过时不需通告)")
                continue
            print(f"✗ 真源缺失: {src}")
            fail += 1
            continue
        try:
            # 字节级文本复制(不走 json 重序列化):快照必须与真源逐字节一致,
            # 否则 sha256 对账(本脚本 --check / 周报核对)永远报漂移。
            with open(src, encoding="utf-8") as f:
                interop._atomic_write_text(dst, f.read())
            if _sha(dst) != _sha(src):
                raise OSError("回读 sha256 与真源不一致")
            print(f"✓ {name} → {dst}")
        except Exception as e:                     # noqa: BLE001
            print(f"✗ {name} 同步失败: {e}")
            fail += 1
    if fail:
        print(f"结论: {fail} 个文件失败(临时文件已清理,快照不会出现半成品)")
        return 1
    print(f"结论: 契约 v{ver} 快照已落位并校验通过")
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description="契约快照同步到 NAS _对接/contract/")
    ap.add_argument("--check", action="store_true", help="只核对,不写入")
    args = ap.parse_args()
    cfg = interop.load_cfg()
    return check_only(cfg) if args.check else sync(cfg)


if __name__ == "__main__":
    sys.exit(main())
