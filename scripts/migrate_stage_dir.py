#!/usr/bin/env python3
"""把项目内 `recordings/` 的老场次搬到录制暂存根(项目外),并改写历史里的路径前缀。

为什么需要它(2026-10-30):
暂存目录外迁后,新场次直接写 `<暂存根>/<主播>/<日期>/`;但项目内 `recordings/`
里还压着迁移前的场次(实测 13.2G,其中 8.9G 因 history 只保留最近 500 条而
**永远等不到自动归档**)。这些目录不清出去,「项目文件夹只放系统文件」这条
就是空的;而直接删又不行 —— 它们是唯一副本。

安全性设计(存量素材不可再生,谨慎优先):
- 默认 dry-run,只打印将要搬的场次;真正落盘必须显式 `--apply`。
- **只搬日期层目录** `<主播>/<YYYY-MM-DD>/`;`recordings/` 顶层的元数据
  (history.json、_ass_backup-*.tar.gz)一律不动 —— 它们是项目文件。
- **疑似在写的场次跳过**:目录内任意文件的 mtime 在 `--recent-sec`(默认 300s)
  以内、或 history 里该目录仍有 `status=recording` 的条目,都跳过并列出。
- 断点续传 + 幂等:目标已有同名同大小的文件就跳过;同名不同大小 → 报冲突
  不覆盖(`--apply` 下该场次整体标 conflict,源保持原样)。
- 搬完改写 `history.json` 里对应条目的 `output_path` 前缀(写前先备份
  history.json.bak-<时间戳>)。不改写 → 历史页播放、字幕重生成、
  `regen_subtitles` 全部指向已搬走的路径,表现为「文件不见了」。
- 逐场次写进度 `state/migrate_stage.progress.json`,中断后重跑自动接着做。

用法:
    .venv/bin/python scripts/migrate_stage_dir.py                    # 预演
    .venv/bin/python scripts/migrate_stage_dir.py --apply            # 真搬
    .venv/bin/python scripts/migrate_stage_dir.py --apply --target /Volumes/A/DouyinRecordings
    .venv/bin/python scripts/migrate_stage_dir.py --apply --only 主播甲
"""
import argparse
import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor as mon  # noqa: E402  只取常量与解析函数,导入无副作用

DATE_ONLY = len("YYYY-MM-DD")
PROGRESS_PATH = os.path.join(BASE, "state", "migrate_stage.progress.json")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def newest_mtime(d):
    """目录内(递归)最新的 mtime;空目录返回 0。"""
    newest = 0.0
    for root, _dirs, files in os.walk(d):
        for fn in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                pass
    return newest


def session_dirs(root):
    """列出 <root>/<主播>/<YYYY-MM-DD>/ ,返回 [(主播, 日期, 绝对路径)]。"""
    out = []
    try:
        streamers = sorted(os.listdir(root))
    except OSError:
        return out
    for name in streamers:
        sd = os.path.join(root, name)
        if not os.path.isdir(sd):
            continue
        try:
            days = sorted(os.listdir(sd))
        except OSError:
            continue
        for day in days:
            if len(day) != DATE_ONLY or day[4] != "-" or day[7] != "-":
                continue
            p = os.path.join(sd, day)
            if os.path.isdir(p):
                out.append((name, day, p))
    return out


def copy_tree_resume(src, dst, dry):
    """逐文件搬到 dst(同名同大小跳过)。返回 (copied, skipped, conflicts)。"""
    copied = skipped = 0
    conflicts = []
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        if not dry:
            os.makedirs(target_dir, exist_ok=True)
        for fn in files:
            s = os.path.join(root, fn)
            d = os.path.join(target_dir, fn)
            try:
                ssize = os.path.getsize(s)
            except OSError:
                continue
            if os.path.exists(d):
                try:
                    if os.path.getsize(d) == ssize:
                        skipped += 1
                        continue
                except OSError:
                    pass
                # 同名不同大小:绝不覆盖(可能是另一场同名分片,也可能是半搬)
                conflicts.append(os.path.relpath(s, src))
                continue
            if dry:
                copied += 1
                continue
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.copy2(s, d + ".part")
            if os.path.getsize(d + ".part") != ssize:
                os.remove(d + ".part")
                conflicts.append(os.path.relpath(s, src))
                continue
            os.replace(d + ".part", d)
            copied += 1
    return copied, skipped, conflicts


def rewrite_history_prefix(old_root, new_root, moved_dirs, apply_):
    """把 history 中 output_path 的 old_root 前缀换成 new_root。

    只改**已确认搬过去**的目录(避免把跳过的场次改成不存在的路径)。
    返回 (改动条数, 备份路径)。
    """
    hist_path = mon.HISTORY_PATH
    history = load_json(hist_path, None)
    if not isinstance(history, list):
        return 0, None
    moved = {os.path.normpath(p) for p in moved_dirs}
    changed = 0
    for e in history:
        if not isinstance(e, dict):
            continue
        p = e.get("output_path") or ""
        if not p:
            continue
        d = os.path.normpath(os.path.dirname(p))
        if d not in moved:
            continue
        rel = os.path.relpath(p, old_root)
        e["output_path"] = os.path.join(new_root, rel)
        changed += 1
    if changed and apply_:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{hist_path}.bak-{stamp}"
        try:
            shutil.copy2(hist_path, backup)
        except OSError:
            backup = None
        save_json(hist_path, history)
    return changed, (f"{hist_path}.bak-<{time.strftime('%Y%m%d-%H%M%S')}>"
                     if changed and apply_ else None)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把项目内 recordings/ 的老场次搬到录制暂存根(默认预演)")
    ap.add_argument("--apply", action="store_true", help="真正搬动(默认只打印)")
    ap.add_argument("--target", default="", help="目标暂存根(默认取当前配置的生效值)")
    ap.add_argument("--only", default="", help="只处理某个主播")
    ap.add_argument("--recent-sec", type=int, default=300,
                    help="目录内文件该秒数内被写过就视为「可能仍在录」,跳过(默认 300)")
    args = ap.parse_args(argv)

    cfg = load_json(os.path.join(BASE, "config.json"), {})
    old_root = mon.RECORD_DIR
    new_root = os.path.realpath(os.path.expanduser(args.target)) if args.target \
        else mon._resolve_output_dir(cfg)
    if mon._under_project(new_root):
        print(f"✗ 目标暂存根不能放在项目内:{new_root}")
        return 2
    if not os.path.isdir(old_root):
        print(f"· 项目内没有 recordings/ 目录({old_root}),无需迁移")
        return 0
    if os.path.realpath(new_root) == os.path.realpath(old_root):
        print("✗ 目标与源相同,无需迁移")
        return 2

    history = load_json(mon.HISTORY_PATH, [])
    recording_dirs = {os.path.normpath(os.path.dirname(e.get("output_path") or ""))
                      for e in history
                      if isinstance(e, dict) and e.get("status") == "recording"}

    dirs = session_dirs(old_root)
    if args.only:
        dirs = [t for t in dirs if t[0] == args.only]
    print(f"源      : {old_root}")
    print(f"目标    : {new_root}")
    print(f"场次目录: {len(dirs)} 个{'  (预演,加 --apply 才真搬)' if not args.apply else ''}")
    print()

    progress = load_json(PROGRESS_PATH, {"sessions": {}})
    planned_bytes = 0
    moved, skipped, conflicts, kept = [], [], [], []
    now = time.time()

    for name, day, src in dirs:
        key = os.path.relpath(src, old_root)
        dst = os.path.join(new_root, name, day)
        try:
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _d, fs in os.walk(src) for f in fs)
        except OSError:
            size = 0
        # ① 仍在录(history 说 recording / 文件刚被写)⇒ 跳过
        if os.path.normpath(src) in recording_dirs:
            kept.append((key, "history 里仍在录制"))
            continue
        if now - newest_mtime(src) < args.recent_sec:
            kept.append((key, f"{args.recent_sec}s 内有写入,疑似仍在录制"))
            continue
        copied, same, conf = copy_tree_resume(src, dst, dry=not args.apply)
        planned_bytes += size
        if conf:
            conflicts.append((key, conf[:3]))
            print(f"  ! {key:42s} 冲突 {len(conf)} 个文件(不覆盖,源保持原样)")
            continue
        if args.apply:
            # ② 复核无冲突后再删源(逐文件已校验大小)
            shutil.rmtree(src, ignore_errors=True)
            # 顺手清掉空的 <主播>/ 目录
            try:
                os.rmdir(os.path.dirname(src))
            except OSError:
                pass
            progress["sessions"][key] = {"done_at": time.time(), "bytes": size,
                                         "files_new": copied, "files_skipped": same}
            save_json(PROGRESS_PATH, progress)
            print(f"  ✓ {key:42s} 搬完 {size / 2**30:6.2f}G "
                  f"(新传 {copied} / 续传跳过 {same})")
        else:
            print(f"  · {key:42s} 将搬 {size / 2**30:6.2f}G "
                  f"(新传 {copied} / 已存在 {same})")
        moved.append(src)

    print()
    if moved and args.apply:
        n, backup = rewrite_history_prefix(old_root, new_root, moved, True)
        print(f"history 路径前缀已改写 {n} 条" + (f"(备份 {backup})" if backup else ""))
    elif moved:
        n, _ = rewrite_history_prefix(old_root, new_root, moved, False)
        print(f"history 将改写 {n} 条 output_path 前缀(预演未写盘)")
    if kept:
        print(f"\n跳过 {len(kept)} 个(仍在录/刚写过,等它录完再跑):")
        for k, why in kept[:20]:
            print(f"  - {k} : {why}")
    if conflicts:
        print(f"\n冲突 {len(conflicts)} 个(同名不同大小,源保持原样,请人工核对):")
        for k, files in conflicts:
            print(f"  - {k} : {', '.join(files)}")
    total_gb = planned_bytes / 2**30
    print(f"\n合计 {total_gb:.2f}G "
          + ("已搬出项目" if args.apply else "待搬出项目(预演)"))
    if not args.apply:
        print("加 --apply 真正执行。建议先确认 NAS 归档队列为空,避免与搬运抢 IO。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
