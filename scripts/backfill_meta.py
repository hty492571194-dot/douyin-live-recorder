#!/usr/bin/env python3
"""补传归档时漏掉的 .meta/(弹幕明细 + 时间戳对齐)到归档目标(NAS / 外接硬盘)。

背景:早期归档只复制了视频与 .ass 字幕,未带 .meta/ 目录,导致这些场次在
历史页「弹幕」面板里读不到明细。本机 recordings/ 下仍保留这些 jsonl/align,
此处按 history 的 archive.dest 把它们补到归档目标。

用法:
    python scripts/backfill_meta.py            # 预演,只列清单不写
    python scripts/backfill_meta.py --apply    # 实际复制(只增不覆盖)

安全边界:
  - 只复制,绝不删除/覆盖;目标文件已存在则跳过;
  - 目标路径必须在 archive.dest 下(desc 目录),不写其他位置;
  - 归档目标未挂载(目录不可写)时跳过并提示,不报错中断。
"""
import argparse
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RECORD_DIR = os.path.join(BASE, "recordings")
HISTORY_PATH = os.path.join(RECORD_DIR, "history.json")
SIDECARS = (".danmaku.jsonl", ".align.json")


def stem_of(path: str) -> str:
    """录制模板路径 → 场次前缀(去掉 -%03d.ext)。"""
    b = os.path.basename(path)
    if "-%03d." in b:
        return b.split("-%03d.")[0]
    return os.path.splitext(b)[0]


def local_meta_dirs(entry: dict):
    """本机 .meta 候选目录:新布局 <输出根>/<主播>/<日期>/.meta,旧布局 <输出根>/<主播>/.meta。"""
    sd = os.path.dirname(entry.get("output_path") or "")
    if not sd:
        return []
    return [os.path.join(sd, ".meta"), os.path.join(os.path.dirname(sd), ".meta")]


def build_plan(history):
    """返回 [(src, dst)] 待补传清单(目标已存在的跳过)。"""
    plan = []
    for e in history:
        arch = e.get("archive") or {}
        dest = arch.get("dest")
        if arch.get("state") != "archived" or not dest:
            continue
        stem = stem_of(e["output_path"])
        dst_dir = os.path.join(dest, ".meta")
        for md in local_meta_dirs(e):
            for suf in SIDECARS:
                src = os.path.join(md, stem + suf)
                dst = os.path.join(dst_dir, stem + suf)
                if os.path.exists(src) and not os.path.exists(dst):
                    plan.append((src, dst, dst_dir))
            break  # 只用第一个存在的布局(新旧不混)
    return plan


def main():
    ap = argparse.ArgumentParser(description="补传 .meta/ 到归档目标")
    ap.add_argument("--apply", action="store_true", help="实际复制(默认预演)")
    args = ap.parse_args()

    if not os.path.exists(HISTORY_PATH):
        print("找不到 recordings/history.json")
        return 1
    with open(HISTORY_PATH, encoding="utf-8") as f:
        history = json.load(f)

    plan = build_plan(history)
    if not plan:
        print("没有需要补传的 .meta 文件")
        return 0

    total = 0
    for src, dst, _ in plan:
        try:
            total += os.path.getsize(src)
        except OSError:
            pass
    print(f"待补传 {len(plan)} 个文件,合计 {total / 1024 / 1024:.2f} MB")
    if not args.apply:
        print("\n预演模式,列出前 20 条(加 --apply 实际执行):")
        for src, dst, _ in plan[:20]:
            print(f"  {src}\n    -> {dst}")
        return 0

    ok = skip_mount = fail = 0
    by_target = {}
    for src, dst, dst_dir in plan:
        try:
            os.makedirs(dst_dir, exist_ok=True)
        except OSError:
            # 归档目标未挂载 / 只读:整批跳过,不逐个报错
            if not os.path.isdir(os.path.dirname(dst_dir)):
                skip_mount += 1
                by_target.setdefault(os.path.dirname(dst_dir), 0)
                by_target[os.path.dirname(dst_dir)] += 1
                continue
            fail += 1
            continue
        try:
            tmp = dst + ".part"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            ok += 1
        except OSError as e:
            fail += 1
            print(f"  失败 {dst}: {e}")

    print(f"\n完成: 成功 {ok} / 目标未挂载 {skip_mount} / 失败 {fail}")
    for d, n in by_target.items():
        print(f"  未挂载跳过 {n} 个: {d}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
