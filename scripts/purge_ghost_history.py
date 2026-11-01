# -*- coding: utf-8 -*-
"""清理 history.json 里的「幽灵条目」:标记 done 但对应文件已不存在。

背景(2026-09-21):流地址被 CDN 回收(下播后 404)时,ffmpeg 起来秒退,却各自
留下一条 ~20s 的历史条目;一场失败循环能刷出 4-5 条。这些条目随后还会被
归档回写误标成 archived(日期目录里其它文件搬走时按目录匹配兜了一下)。
代码层已修(延迟入史 + give_up 残片回收),本脚本负责清理**存量**。

判据(全部满足才算幽灵,宁缺勿滥):
  - status == done
  - duration < 90s
  - size 为空(旧代码对分片模板从不写 size;新代码会写,有 size 一律保留)
  - 本机没有对应分片文件
  - 归档目标(NAS/外接盘)上也没有对应文件
真实短直播(有文件,哪怕已归档)不受影响。

用法:
    python scripts/purge_ghost_history.py            # 干跑,只列清单
    python scripts/purge_ghost_history.py --apply    # 备份后清理
"""
import argparse
import glob
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY = os.path.join(BASE, "recordings", "history.json")
JUNK_MAX_DURATION = 90.0


def _entries_with_files(template):
    """模板路径 → 实际分片/字幕文件列表(不存在则空)。"""
    if not template:
        return []
    if "%03d" in template:
        d = os.path.dirname(template)
        base = os.path.basename(template)
        prefix = base.split("%03d")[0]
        ext = os.path.splitext(base)[1] or ".flv"
        pats = [os.path.join(d, prefix + "*" + ext),
                os.path.join(d, ".meta", prefix + "*")]
        out = []
        for pat in pats:
            out += glob.glob(pat)
        return out
    return [template] if os.path.exists(template) else []


def find_ghosts(history, ):
    cfg = {}
    try:
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    roots = []
    ext_dir = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext_dir:
        roots.append(ext_dir.rstrip("/"))
    mp = ((cfg.get("nas") or {}).get("mount_point") or "").strip()
    if mp:
        roots.append(os.path.realpath(os.path.expanduser(mp)))
    ghosts, keep = [], []
    for e in history:
        if e.get("status") != "done":
            keep.append(e)
            continue
        dur = e.get("duration") or 0
        p = e.get("output_path") or ""
        if dur >= JUNK_MAX_DURATION or e.get("size"):
            keep.append(e)                    # 时长正常 / 有体积记录 → 保留
            continue
        if _entries_with_files(p):
            keep.append(e)                    # 本机还有文件 → 保留
            continue
        arch = e.get("archive") or {}
        dest = arch.get("dest") or ""
        hits = glob.glob(os.path.join(dest, os.path.basename(p).replace("%03d", "*"))) \
            if dest and os.path.isdir(dest) else []
        if hits:
            keep.append(e)                    # 归档目标上还有文件 → 保留
            continue
        # 兜底:归档目录下按 主播/日期/ 前缀再找一遍(早期 dest 可能为空)
        found = False
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            name = e.get("name") or "?"
            day = time.strftime("%Y-%m-%d", time.localtime(e.get("started_at") or 0))
            pat = os.path.join(root, (cfg.get("nas") or {}).get("root_dir", "直播回放"),
                               name, day, os.path.basename(p).replace("%03d", "*"))
            if glob.glob(pat):
                found = True
                break
        if found:
            keep.append(e)
        else:
            ghosts.append(e)
    return ghosts, keep


def main():
    ap = argparse.ArgumentParser(description="清理 history.json 里的幽灵条目")
    ap.add_argument("--apply", action="store_true", help="实际清理(默认干跑)")
    args = ap.parse_args()

    with open(HISTORY, encoding="utf-8") as f:
        history = json.load(f)
    ghosts, keep = find_ghosts(history)
    print(f"history 总数 {len(history)} → 幽灵 {len(ghosts)} 条,保留 {len(keep)} 条")
    for e in ghosts[:12]:
        print("  - %s  dur=%ss  %s" % (
            time.strftime("%m-%d %H:%M", time.localtime(e.get("started_at") or 0)),
            e.get("duration"), e.get("output_path", "")[-70:]))
    if len(ghosts) > 12:
        print(f"  ...共 {len(ghosts)} 条")
    if not ghosts:
        print("没有需要清理的条目。")
        return 0
    if not args.apply:
        print("\n(干跑,未改动。确认无误后加 --apply 执行)")
        return 0

    bak = HISTORY + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(HISTORY, bak)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)
    print(f"\n已清理 {len(ghosts)} 条,备份: {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
