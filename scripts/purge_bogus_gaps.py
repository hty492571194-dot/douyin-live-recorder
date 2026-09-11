#!/usr/bin/env python3
"""清理 align.json 里「不可能成立」的停滞记录。

成因:FLV 尾部损坏时 flv_last_ts 读到垃圾字节,解绕(_unwrap_ts 搜索范围
±2^31 ms≈±24.8 天)后落到边界,生成「落后墙钟 526 小时」这类荒谬 gap。
新的解析层已加防护(见 subtitle.flv_last_ts / _sanitize_abs),这里清理存量。

判定:note 形如「最后分片时间戳落后墙钟 Ns」且 N 超过 --max-lag(默认 86400,
即 1 天)——分片最长 2300 秒,任何超过 1 天的停滞都不可能是真实数据。

用法:
    python scripts/purge_bogus_gaps.py             # 预演
    python scripts/purge_bogus_gaps.py --apply     # 实际清理(自动备份 .bak)
    python scripts/purge_bogus_gaps.py --no-nas    # 只清本机,不动归档目标

安全边界:
  - 只删除符合条件的 stall 条目,其他 gap(danmaku_gap 等)一律保留;
  - 每个被修改的文件先写同名 .bak 备份,可随时回滚;
  - 归档目标(NAS/外接硬盘)未挂载时自动跳过。
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RECORD_DIR = os.path.join(BASE, "recordings")
STALL_RE = re.compile(r"落后墙钟\s*(\d+)s")


def roots_with_nas(skip_nas=False):
    """待扫描根目录:本机录制目录 + 归档目标(NAS 挂载点/外接硬盘)。"""
    roots = [RECORD_DIR]
    if skip_nas:
        return roots
    try:
        cfg = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
    except Exception:
        return roots
    nas = cfg.get("nas") or {}
    if nas.get("enabled", False):
        mp = os.path.expanduser(nas.get("mount_point") or "~/DouyinArchive")
        if os.path.isdir(mp):
            roots.append(mp)
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext and os.path.isdir(os.path.expanduser(ext)):
        roots.append(os.path.expanduser(ext))
    return roots


def align_files(root):
    return sorted(glob.glob(os.path.join(root, "**", ".meta", "*.align.json"),
                            recursive=True))


def purge(path, max_lag, apply_changes):
    """清理单个 align.json,返回 (总条目, 删除条目, 是否改动)。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0, 0, False
    gaps = data.get("gaps") or []
    keep, removed = [], 0
    for g in gaps:
        if g.get("type") != "stall":
            keep.append(g)
            continue
        m = STALL_RE.search(g.get("note") or "")
        if m and int(m.group(1)) > max_lag:
            removed += 1
            continue
        keep.append(g)
    if not removed:
        return len(gaps), 0, False
    if not apply_changes:
        return len(gaps), removed, True
    try:
        shutil.copy2(path, path + ".bak")
        data["gaps"] = keep
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"  写入失败 {path}: {e}")
        return len(gaps), 0, False
    return len(gaps), removed, True


def main():
    ap = argparse.ArgumentParser(description="清理荒谬的停滞 gap")
    ap.add_argument("--apply", action="store_true", help="实际清理(默认预演)")
    ap.add_argument("--no-nas", action="store_true", help="只清本机,不动归档目标")
    ap.add_argument("--max-lag", type=int, default=86400,
                    help="停滞秒数超过此值视为荒谬(默认 86400)")
    args = ap.parse_args()

    roots = roots_with_nas(args.no_nas)
    total = removed = files = 0
    for root in roots:
        n_root = 0
        for p in align_files(root):
            t, r, changed = purge(p, args.max_lag, args.apply)
            total += t
            removed += r
            if r:
                n_root += 1
                files += 1
        print(f"{root}: 清理 {n_root} 个文件")

    mode = "已清理" if args.apply else "预演(加 --apply 执行)"
    print(f"\n{mode}: 扫描 gap {total} 条,命中荒谬 {removed} 条,涉及 {files} 个文件")
    if args.apply:
        print("原文件已备份为同名 .bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
