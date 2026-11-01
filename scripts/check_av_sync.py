#!/usr/bin/env python3
"""音画同步自检:统计 FLV 里「音频采样数」与「视轨跨度」的差。

为什么要单独一个工具
--------------------
容器时长走 PTS,采样数走内容,两者可以不一致 —— 旧参数
(`-use_wallclock_as_timestamps 1`)逐包把 PTS 换成"读到它的墙钟",
包成批到达时 PTS 被压扁、断流处直接跳秒,于是:

    文件看起来 38 分钟,真解码只有 37 分 52 秒(asrclip 报的 -8.43s)

播放器/ffprobe 读的是 PTS,看不出异常;只有**数采样数**才暴露。
本工具就是那只眼睛,录制结束后随时可复跑。

判定口径
--------
    音频内容 = 音频 tag 数 × 1024 / 采样率      (AAC 每包恒定 1024 采样)
    视轨跨度 = 末视频 tag ts − 首视频 tag ts
    差值超阈值 → 该文件音轨与视轨脱钩

用法
----
    python3 scripts/check_av_sync.py <文件|目录|场次目录> [...]
    python3 scripts/check_av_sync.py --threshold 0.5 recordings/主播甲/2026-10-27
    python3 scripts/check_av_sync.py --json recordings/主播甲/2026-10-27

退出码:全部达标 0;有超阈值文件 1(可直接挂进归档/巡检)。
"""
import argparse
import json
import os
import struct
import sys

DEFAULT_SAMPLE_RATE = 48000     # 源流实测 48 kHz,录制不重采样
AAC_SAMPLES_PER_PACKET = 1024   # AAC-LC 恒定 1024 采样/包
DEFAULT_THRESHOLD = 0.5         # 秒;后端能容忍的对齐误差上限


def scan_flv(path):
    """顺序跳过 body 只读 tag 头,返回音频包数 / 视频帧数 / 首末时间戳。

    大文件(几百 MB)靠 seek 跳 body,不把整个文件读进内存。
    """
    size = os.path.getsize(path)
    audio = video = 0
    a_first = a_last = v_first = v_last = None
    with open(path, "rb") as f:
        head = f.read(13)
        if len(head) < 13 or head[:3] != b"FLV":
            raise ValueError("不是 FLV 文件")
        pos = 13
        while pos < size - 4:
            f.seek(pos)
            h = f.read(11)
            if len(h) < 11:
                break
            t = h[0]
            dsize = (h[1] << 16) | (h[2] << 8) | h[3]
            ts = (h[7] << 24) | (h[4] << 16) | (h[5] << 8) | h[6]
            if t not in (8, 9, 18):
                break                       # 尾部损坏,按已读部分统计
            b = f.read(min(dsize, 2))
            # AAC/AVC sequence header 是编码配置包,不是采样/帧,要排除
            if t == 8 and len(b) >= 2 and not ((b[0] >> 4) == 10 and b[1] == 0):
                audio += 1
                if a_first is None:
                    a_first = ts
                a_last = ts
            elif t == 9 and len(b) >= 2 and not ((b[0] & 0x0F) == 7 and len(b) > 1 and b[1] == 0):
                video += 1
                if v_first is None:
                    v_first = ts
                v_last = ts
            pos += 11 + dsize + 4
    return {"audio_packets": audio, "video_frames": video,
            "audio_first": a_first, "audio_last": a_last,
            "video_first": v_first, "video_last": v_last, "size": size}


def check(path, sample_rate=DEFAULT_SAMPLE_RATE):
    d = scan_flv(path)
    if not d["audio_packets"] or not d["video_frames"]:
        return {"file": path, "ok": False, "error": "单流文件(缺音频或视频)"}
    audio_sec = d["audio_packets"] * AAC_SAMPLES_PER_PACKET / float(sample_rate)
    video_span = (d["video_last"] - d["video_first"]) / 1000.0
    pts_span = (d["audio_last"] - d["audio_first"]) / 1000.0
    return {
        "file": path,
        "ok": True,
        "audio_sec": round(audio_sec, 3),
        "video_span": round(video_span, 3),
        "audio_pts_span": round(pts_span, 3),
        # ① 内容差:音频采样数 vs 视轨跨度 —— 下游音画同步的直接指标
        "content_delta": round(audio_sec - video_span, 3),
        # ② 自洽差:PTS 跨度 vs 采样数 —— 录制端自己有没有把时间轴写歪
        "pts_vs_samples": round(pts_span - audio_sec, 3),
        "mb": round(d["size"] / 1e6, 1),
    }


def iter_files(targets):
    for t in targets:
        if os.path.isdir(t):
            for root, _dirs, names in os.walk(t):
                for n in sorted(names):
                    if n.lower().endswith((".flv", ".mp4")):
                        yield os.path.join(root, n)
        elif os.path.isfile(t):
            yield t
        else:
            print("跳过(不存在): %s" % t, file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="音画同步自检(音频采样数 vs 视轨跨度)")
    ap.add_argument("targets", nargs="+", help="FLV 文件 / 目录 / 多个场次目录")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help="判定阈值(秒),默认 %(default)s")
    ap.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE,
                    help="音频采样率,默认 %(default)s")
    ap.add_argument("--json", action="store_true", help="输出 JSON(给下游消费)")
    args = ap.parse_args(argv)

    rows, bad = [], 0
    for p in iter_files(args.targets):
        try:
            r = check(p, args.sample_rate)
        except (OSError, ValueError) as exc:
            rows.append({"file": p, "ok": False, "error": str(exc)})
            bad += 1
            continue
        if r.get("ok") and (abs(r["content_delta"]) > args.threshold
                            or abs(r["pts_vs_samples"]) > args.threshold):
            r["verdict"] = "超阈值"
            bad += 1
        else:
            r["verdict"] = "正常" if r.get("ok") else "不可判"
        rows.append(r)

    if args.json:
        print(json.dumps({"threshold": args.threshold, "bad": bad, "items": rows},
                         ensure_ascii=False, indent=2))
        return 1 if bad else 0

    print("阈值 %.2fs | 音频采样数 vs 视轨跨度" % args.threshold)
    print("%-46s %9s %9s %10s %10s  %s"
          % ("文件", "音频内容", "视轨跨度", "内容差", "PTS-采样", "判定"))
    for r in rows:
        name = os.path.basename(r["file"])
        if len(name) > 44:
            name = name[:41] + "..."
        if not r.get("ok"):
            print("%-46s %s" % (name, r.get("error", "")))
            continue
        print("%-46s %9.2f %9.2f %+10.2f %+10.2f  %s"
              % (name, r["audio_sec"], r["video_span"],
                 r["content_delta"], r["pts_vs_samples"], r["verdict"]))
    print("\n共 %d 个文件,超阈值 %d 个。" % (len(rows), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
