#!/usr/bin/env python3
"""把 recordings/<主播>/<YYYY-MM-DD>/ 下已有的场次目录补入归档队列。

用途:历史录制的视频没有对应队列条目(旧条目被判定无效已清理)时,
一次性重新入队,由服务的保活循环自动搬到 NAS / 外接硬盘。

用法(需先停止 monitor 服务,避免内存队列覆盖文件):
    launchctl kickstart -k gui/$(id -u)/com.douyin.monitor   # 或 kill <pid>
    ./.venv/bin/python scripts/backfill_archive.py
    # 再启动服务
"""
import json
import os
import sys
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import nas  # noqa: E402


def _is_date_dir(name):
    return len(name) == 10 and name[4] == "-" and name[:4].isdigit()


def main():
    rec = os.path.join(BASE, "recordings")
    days = []
    for streamer in sorted(os.listdir(rec)):
        sdir = os.path.join(rec, streamer)
        if not os.path.isdir(sdir):
            continue
        for d in sorted(os.listdir(sdir)):
            ddir = os.path.join(sdir, d)
            if os.path.isdir(ddir) and _is_date_dir(d):
                days.append((streamer, d, ddir))

    pending = []
    if os.path.exists(nas.PENDING_PATH):
        try:
            with open(nas.PENDING_PATH, encoding="utf-8") as f:
                pending = json.load(f)
        except (OSError, json.JSONDecodeError):
            pending = []
    exist = {os.path.normpath(e.get("src_path", "")) for e in pending}

    added = 0
    for streamer, d, ddir in days:
        if os.path.normpath(ddir) in exist:
            continue
        pending.append({"id": uuid.uuid4().hex, "streamer": streamer,
                        "src_path": ddir, "added_at": time.time(),
                        "retries": 0, "status": "pending", "date": d})
        added += 1
    nas._atomic_write(nas.PENDING_PATH, pending)

    print(f"扫描到 {len(days)} 个场次目录,新增入队 {added} 个,队列共 {len(pending)} 条")
    for streamer, d, _ in days:
        print(f"  {streamer}/{d}")
    print("\n启动服务后将按顺序搬运到 <归档根>/直播回放/<主播>/<日期>/")


if __name__ == "__main__":
    main()
