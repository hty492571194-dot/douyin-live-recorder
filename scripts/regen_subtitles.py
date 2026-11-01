#!/usr/bin/env python3
"""按当前字幕链路重生成存量 .ass(画布 / 缩字号 / 位置动画修复后的版本)。

为什么需要它:`.ass` 只在录制过程中由巡检(patrol)与收尾(finalize)生成,
录完就定稿。这次修掉的三处问题(PlayRes 与视频宽高比不符、长弹幕不缩字号、
行间上移动画在 libass 下不生效)都只影响新生成的文件,存量文件仍是旧版。

安全性设计(存量文件不可再生,谨慎优先):
- 默认 dry-run,只打印将要改动的文件;真正落盘必须显式 `--apply`。
- 只处理「jsonl 存在且至少有 1 条事件」的场次;jsonl 缺失的场次一律跳过,
  绝不写出只有表头的空字幕。
- 重生成后比对:新文件 Dialogue 行数为 0 而旧文件非 0 → 判为异常,
  回滚该文件并计入失败。
- `--apply` 前先把所有待改文件打进一个 tar.gz 备份(默认放项目根),失败可整体还原。

用法:
    .venv/bin/python scripts/regen_subtitles.py               # 预演
    .venv/bin/python scripts/regen_subtitles.py --apply       # 落盘(先自动备份)
    .venv/bin/python scripts/regen_subtitles.py --apply --only 主播甲
"""
import argparse
import glob
import json
import os
import re
import sys
import tarfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import subtitle as S  # noqa: E402
from danmaku import jsonl_for  # noqa: E402

SEG_RE = re.compile(r"^(?P<stem>.+)-(\d{3})$")


def gift_plan_stats(pattern):
    """读该场次的礼物展示计划产物 → (是否存在, stats/tail_overflow)。

    重生成时必须能回答三个问题：原始礼物事件有没有被完整覆盖、有没有片尾溢出、
    ASS 的礼物行是不是来自同一份计划。缺产物不报错（可能是横屏/未启用）。
    """
    path = S.giftdisplay_path_for(pattern)
    if not os.path.exists(path):
        return False, None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, None
    return True, {"stats": doc.get("stats") or {},
                  "overflow": len(doc.get("tail_overflow") or []),
                  "segments": len(doc.get("segments") or [])}


def load_cfg():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def find_sessions(root):
    """扫 recordings/ 下的场次 → [(场次模式路径, 代表分片)]。"""
    out, seen = [], set()
    for path in sorted(glob.glob(os.path.join(root, "*", "*", "*.flv"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        m = SEG_RE.match(stem)
        if not m:
            continue
        key = os.path.join(os.path.dirname(path), m.group("stem"))
        if key in seen:
            continue
        seen.add(key)
        out.append((key + "-%03d.flv", path))
    return out


def event_count(jsonl_path):
    n = 0
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except OSError:
        return 0
    return n


def dialogues(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.startswith("Dialogue"))
    except OSError:
        return None


def canvas_str(path):
    size = S.flv_video_size(path)
    return f"{size[0]}x{size[1]}" if size else "未知"


def is_active(segs, minutes):
    """场次是否仍在录制:.flv 在最近 minutes 分钟内被写过。

    正在录制的场次必须跳过 —— 服务巡检每 60s 会用**内存里的旧代码**重写这
    些 .ass,与我们互相覆盖,还可能读到写了一半的文件。等录完再跑。
    """
    if minutes <= 0:
        return False
    now = time.time()
    for _idx, path in segs:
        try:
            if now - os.path.getmtime(path) < minutes * 60:
                return True
        except OSError:
            continue
    return False


def restore_from_backup(backup, path):
    """从备份包还原单个文件(不整包解压,避免路径穿越)。"""
    member = os.path.relpath(path, BASE)
    try:
        with tarfile.open(backup, "r:gz") as tar:
            try:
                src = tar.extractfile(member)
            except KeyError:
                return False
            if src is None:
                return False
            data = src.read()
    except (OSError, tarfile.TarError):
        return False
    if not data:
        return False
    with open(path, "wb") as f:
        f.write(data)
    return True


def main():
    ap = argparse.ArgumentParser(description="重生成存量 .ass")
    ap.add_argument("--apply", action="store_true", help="真正落盘(默认只预演)")
    ap.add_argument("--only", help="只处理名字包含该子串的主播目录")
    ap.add_argument("--root", default=os.path.join(BASE, "recordings"),
                    help="扫描根目录(默认 recordings/)")
    ap.add_argument("--active-window", type=float, default=3.0, metavar="分钟",
                    help="分片在最近 N 分钟内被写过则视为正在录制并跳过(默认 3,0=不跳过)")
    args = ap.parse_args()

    cfg = load_cfg()
    sessions = find_sessions(args.root)
    if args.only:
        sessions = [s for s in sessions if args.only in s[0]]

    plan, skipped = [], []
    for pattern, sample in sessions:
        jsonl = jsonl_for(pattern)
        if not os.path.exists(jsonl):
            skipped.append((pattern, "无 jsonl"))
            continue
        if event_count(jsonl) == 0:
            skipped.append((pattern, "jsonl 无事件"))
            continue
        segs = S.find_segments(pattern)
        if not segs:
            skipped.append((pattern, "无分片文件"))
            continue
        if is_active(segs, args.active_window):
            skipped.append((pattern, "正在录制"))
            continue
        for idx, path in segs:
            ass = S.ass_path_for_segment(path)
            before = dialogues(ass)
            if before is None:
                continue                      # 没有旧文件,不新增
            if before == 0:
                skipped.append((ass, "旧文件无弹幕行"))
                continue
            plan.append({"pattern": pattern, "seg": path, "ass": ass,
                         "before": before, "canvas": canvas_str(path)})
        # 每场只报一次 jsonl 情况
    print(f"扫描 {len(sessions)} 个场次,待重生成 {len(plan)} 个 .ass"
          f"(跳过 {len(skipped)})")
    if skipped:
        print("\n跳过明细(最多 10 条):")
        for p, why in skipped[:10]:
            print(f"  - {why}: {os.path.relpath(p, BASE)}")
    if not plan:
        return 0

    # 画布分布,便于确认竖屏识别生效
    dist = {}
    for it in plan:
        dist[it["canvas"]] = dist.get(it["canvas"], 0) + 1
    print("\n画布分布:", ", ".join(f"{k} × {v}" for k, v in sorted(dist.items())))

    if not args.apply:
        print("\n[预演] 未改动任何文件。确认无误后加 --apply 落盘。")
        for it in plan[:5]:
            print(f"  {os.path.relpath(it['ass'], BASE)}  弹幕行 {it['before']} → 待生成")
        if len(plan) > 5:
            print(f"  ...(其余 {len(plan) - 5} 个)")
        return 0

    # 备份 → 重生成
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(args.root, f"_ass_backup-{stamp}.tar.gz")
    with tarfile.open(backup, "w:gz") as tar:
        for it in plan:
            tar.add(it["ass"], arcname=os.path.relpath(it["ass"], BASE))
    print(f"\n已备份 {len(plan)} 个文件 → {backup}"
          f"(recordings/ 已 gitignore,不会进公开仓库)")

    ok, bad = [], []
    done = set()
    for it in plan:                                   # 留下改动前的字节(算实际改动数)
        try:
            with open(it["ass"], "rb") as f:
                it["before_bytes"] = f.read()
        except OSError:
            it["before_bytes"] = None
    for it in plan:
        if it["pattern"] in done:
            continue
        done.add(it["pattern"])
        try:
            S.generate_session(it["pattern"], cfg,
                               align=S.load_align(it["pattern"]))
        except Exception as e:                                  # noqa: BLE001
            bad.append((it["pattern"], f"{type(e).__name__}: {e}"))
    for it in plan:                                   # 逐文件复核生成结果
        after = dialogues(it["ass"])
        if not after:
            # 例如:录制已停但弹幕通道还在收,事件全落在视频时长之外 →
            # 用当前数据重生成必然为空。此时保留原文件,不制造空字幕。
            back = restore_from_backup(backup, it["ass"])
            bad.append((it["ass"],
                        f"重生成后 0 行(旧 {it['before']} 行)"
                        f"{' → 已从备份还原' if back else ' → 还原失败,见备份包'}"))
        else:
            ok.append(it)
    # 礼物展示计划审计：覆盖率 / 片尾溢出 / 产物是否生成
    g_ok = g_miss = g_overflow = 0
    g_events = g_items = 0
    for pattern, _sample in sessions:
        has, st = gift_plan_stats(pattern)
        if not has:
            g_miss += 1
            continue
        g_ok += 1
        g_overflow += int(st["overflow"])
        g_events += int((st["stats"] or {}).get("source_events") or 0)
        g_items += int((st["stats"] or {}).get("items") or 0)
    if g_ok or g_miss:
        print(f"\n礼物展示计划: 已生成 {g_ok} 场 (缺 {g_miss} 场 · 多为横屏或未启用)"
              f",原始礼物 {g_events} 笔 → 展示 {g_items} 项,片尾溢出 {g_overflow} 项")
        if g_overflow:
            print("  ⚠ 存在片尾溢出:末片时长放不下 → 见各场 .giftdisplay.json 的 tail_overflow")

    changed = 0
    for it in ok:
        try:
            with open(it["ass"], "rb") as f:
                if it["before_bytes"] is None or f.read() != it["before_bytes"]:
                    changed += 1
        except OSError:
            changed += 1
    print(f"重生成完成:{len(ok)} 个文件 / {len(done)} 个场次,实际内容变化 {changed} 个")
    if bad:
        print(f"\n⚠ 异常 {len(bad)} 个:")
        for p, why in bad:
            print(f"  - {os.path.relpath(p, BASE)}: {why}")
    print(f"\n备份包:{backup}\n如需整体还原:tar -xzf {backup} -C {BASE}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
