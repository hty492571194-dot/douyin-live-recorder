#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证缩放链路能扛住源流参数变化(滤镜图重配置)。

为什么需要这个脚本
------------------
ffmpeg 7.1 的 `scale_vt` **撑不住滤镜图重配置**。直播源只要在运行中改变帧参数
(分辨率、位深,哪怕只跳一帧又变回去),ffmpeg 就要重建滤镜图,重建时:

    Impossible to convert between the formats supported by the filter
    'Parsed_scale_vt_0' and the filter 'auto_scale_0'
    Error reinitializing filters! → error code -78 (Function not implemented)

vf 线程随即终止,整个 ffmpeg 退出。2026-10-29 的归因结论:8 天 86 次录制崩溃
全是这一条,与并发路数/画幅/档位无关,只取决于源流稳不稳。修复是把缩放从
GPU `scale_vt` 换成 CPU `scale`(见 `quality.SCALE_VT_ENABLED`)。

单测只能断言"参数里没有 scale_vt",断言不了"ffmpeg 真的不会崩" —— 所以把当日
的复现固化成这条命令:

  1. 造一段「1088×1920 → 720×1280 → 1088×1920」的抖动流(两次重配置);
  2. 用 **monitor 当前参数** 跑一遍 → 必须成功,且输出尺寸逐段跟随;
  3. 【对照组】把 scale_vt 那套参数拼回来再跑 → **必须崩**。
     这一步保证脚本本身不是摆设:如果哪天它连对照组都跑不出崩溃,
     说明验证环境(ffmpeg/硬件)已经变了,第 2 步的"通过"也就不作数了。

用法
----
    .venv/bin/python scripts/verify_scale_reinit.py

退出码:0 = 修复有效;1 = 有问题(会打印原因);2 = 环境不支持(跳过)。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor  # noqa: E402
import quality as Q  # noqa: E402

# 三段抖动流:(宽, 高),按顺序拼成一条
SHAPES = [(1088, 1920), (720, 1280), (1088, 1920)]
SEG_SEC = 3
FPS = 30
EXPECT_FRAMES = len(SHAPES) * SEG_SEC * FPS          # 270

# scale_vt 时代的参数(对照组):正是它在源参数变化时把进程打死
LEGACY_EXTRA = ["-hwaccel_output_format", "videotoolbox_vld"]


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ffmpeg():
    ff = monitor.find_ffmpeg()
    if not ff:
        return None
    try:
        enc = _run([ff, "-hide_banner", "-encoders"], timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return ff if "h264_videotoolbox" in enc else None


def _make_clip(ff, path, w, h):
    """造一段无声视频(附一条静音音轨,贴近真实流)。用 libx264,不依赖硬编。"""
    return _run([
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={FPS}:duration={SEG_SEC}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-c:v", "libx264", "-preset", "ultrafast", "-g", str(FPS // 2),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
        "-t", str(SEG_SEC), path])


def _make_flapping_stream(ff, workdir):
    """把不同分辨率的片段用 concat demuxer 拼成一条流:解码器会在接缝处报参数变化。"""
    clips = []
    for i, (w, h) in enumerate(SHAPES):
        p = os.path.join(workdir, "clip%d.flv" % i)
        r = _make_clip(ff, p, w, h)
        if r.returncode != 0:
            raise RuntimeError("生成测试片段失败: %s" % (r.stderr or "")[-400:])
        clips.append(p)
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in clips:
            f.write("file '%s'\n" % p.replace("'", r"'\''"))
    out = os.path.join(workdir, "flapping.flv")
    r = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
              "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("拼接测试流失败: %s" % (r.stderr or "")[-400:])
    return out


def _recorder_args(ff, src, workdir, extra=()):
    """用 monitor 的真实参数生成器,保证验证对象就是线上那条链路。"""
    cfg = dict(monitor.DEFAULT_CONFIG["recorder"])
    cfg["resolution"] = 1080          # 有缩放,才会踩到滤镜
    cfg["bitrate"] = 1200
    cfg["video_mode"] = "vt"
    cfg["hw_decode"] = True
    cfg["force_hw"] = True
    cfg["segment_time"] = 0           # 不分片,单文件看帧数与尺寸
    args, _out = monitor._build_ffmpeg_args(
        ff, src, workdir, "verify", "flv", cfg, "20261029-000000")
    i = args.index("-i")
    args[i:i] = list(extra)           # extra 只放输入侧选项(对照组用)
    # 打开 info 级别并用 showinfo 读每一帧的真实尺寸
    j = args.index("-loglevel")
    args[j + 1] = "info"
    k = args.index("-vf")
    args[k + 1] = args[k + 1] + ",showinfo"
    return args


def _probe(ff, src, workdir, extra=()):
    """跑一遍 → (returncode, 帧数, 出现过的输出尺寸序列)。"""
    args = _recorder_args(ff, src, workdir, extra)
    out = os.path.join(workdir, "out.flv")
    args.append(out)
    r = _run(args, timeout=240)
    text = (r.stderr or "") + (r.stdout or "")
    frames = 0
    m = re.findall(r"frame=\s*(\d+)", text)
    if m:
        frames = max(int(x) for x in m)
    sizes = re.findall(r"\bs:(\d+)x(\d+)", text)
    seq = []
    for w, h in sizes:
        cur = "%sx%s" % (w, h)
        if not seq or seq[-1] != cur:
            seq.append(cur)
    return r.returncode, frames, seq, text


def main():
    ff = _ffmpeg()
    if not ff:
        print("跳过:本机没有可用的 ffmpeg + h264_videotoolbox")
        return 2
    print("ffmpeg: %s" % ff)
    print("SCALE_VT_ENABLED = %s(必须为 False)" % Q.SCALE_VT_ENABLED)
    workdir = tempfile.mkdtemp(prefix="dy-scale-reinit-")
    try:
        print("\n[1/3] 造抖动流 %s ..." % " → ".join("%dx%d" % s for s in SHAPES))
        src = _make_flapping_stream(ff, workdir)

        print("[2/3] 用 monitor 当前参数跑(必须成功且尺寸跟随)")
        rc, frames, seq, text = _probe(ff, src, workdir)
        ok_frames = frames >= EXPECT_FRAMES
        ok_sizes = seq == ["1080x1920", "720x1280", "1080x1920"]
        print("      returncode=%s 帧数=%d(期望 ≥%d) 尺寸序列=%s"
              % (rc, frames, EXPECT_FRAMES, " → ".join(seq) or "(无)"))
        if rc != 0 or not ok_frames or not ok_sizes:
            print("\n✗ 失败:当前参数扛不住源参数变化")
            for line in text.strip().splitlines()[-12:]:
                print("   | " + line)
            if not ok_sizes:
                print("   尺寸序列不对(应 1080x1920 → 720x1280 → 1080x1920),"
                      "说明缩放表达式没有跟随新尺寸重新求值")
            return 1

        print("[3/3] 对照组:把 scale_vt + output_format 拼回去(必须崩)")
        rc2, frames2, _seq2, text2 = _probe(ff, src, workdir, extra=LEGACY_EXTRA)
        crashed = rc2 != 0 and "auto_scale_0" in text2
        print("      returncode=%s 崩溃=%s" % (rc2, crashed))
        if not crashed:
            print("\n✗ 对照组没有崩溃 —— 验证环境与当日不同,"
                  "第 2 步的「通过」不能作为证据。请人工复核。")
            for line in text2.strip().splitlines()[-10:]:
                print("   | " + line)
            return 1

        print("\n✓ 通过:当前参数在源参数两次变化下全部录完(%d 帧),"
              "对照组按预期崩溃。" % frames)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
