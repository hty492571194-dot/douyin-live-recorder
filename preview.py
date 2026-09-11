#!/usr/bin/env python3
"""预览图管理:目录解析、ffmpeg 抽帧、列举、安全校验、清理。

模块导入面:resolve_dir / streamer_dir / capture_frame / list_images /
            cleanup_others / safe_join / selected_preview。
"""
import os
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PREVIEW_DIR = os.path.join(BASE, "previews")

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def resolve_dir(cfg):
    """返回预览图根目录:config.preview.dir 为空则用项目内 previews/。"""
    pv = cfg.get("preview") or {}
    d = (pv.get("dir") or "").strip()
    return os.path.expanduser(d) if d else DEFAULT_PREVIEW_DIR


def streamer_dir(cfg, name):
    """某主播的预览图目录(绝对路径)。"""
    return os.path.join(resolve_dir(cfg), name)


def capture_frame(ffmpeg, stream_url, out_path, timeout=15):
    """从直播流抽一帧到 out_path。返回是否成功。

    -timeout 为 ffmpeg 输入超时(微秒),配合 subprocess 超时避免卡死;
    抽帧失败(流地址过期/网络抖动)由调用方跳过,不阻塞录制。
    """
    if not ffmpeg or not stream_url:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-timeout", str(int(timeout * 1_000_000)),
             "-i", stream_url, "-frames:v", "1", "-q:v", "3", out_path],
            capture_output=True, timeout=timeout + 5,
        )
        return (r.returncode == 0 and os.path.exists(out_path)
                and os.path.getsize(out_path) > 0)
    except Exception:
        return False


def list_images(cfg, name):
    """列出某主播目录下全部图片文件名(按名排序)。"""
    d = streamer_dir(cfg, name)
    if not os.path.isdir(d):
        return []
    out = []
    try:
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(IMG_EXTS):
                out.append(fn)
    except OSError:
        pass
    return out


def safe_join(cfg, name, filename):
    """校验 filename 落在该主播目录内,返回绝对路径;越界返回 None(防路径穿越)。"""
    if not filename or "/" in filename or "\\" in filename:
        return None
    d = os.path.realpath(streamer_dir(cfg, name))
    target = os.path.realpath(os.path.join(d, filename))
    if target != d and not target.startswith(d + os.sep):
        return None
    return target


def cleanup_others(cfg, name, keep_file):
    """删除某主播目录下除 keep_file 外的全部图片(确认预览图后清理)。"""
    d = streamer_dir(cfg, name)
    if not os.path.isdir(d):
        return
    for fn in list_images(cfg, name):
        if fn == keep_file:
            continue
        try:
            os.remove(os.path.join(d, fn))
        except OSError:
            pass


def selected_preview(cfg, name):
    """返回某主播已选定的预览图文件名(未设置返回空串)。"""
    for m in cfg.get("monitors", []) or []:
        if m.get("name") == name:
            return m.get("preview", "") or ""
    return ""
