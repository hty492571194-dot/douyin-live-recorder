#!/usr/bin/env python3
"""实时预览 HLS 产物管理:目录解析、准备(清残留)、清理、安全路径。

预览流由 live_preview.py 的「按需独立拉流会话」生成(打开预览页才启动
独立 ffmpeg,-c copy 源流直出 HLS;关页空闲回收)——本模块只管路径与
文件生命周期,不管理任何进程。

职责边界:
- 预览目录默认 BASE/previews-live/<主播>/;config.live_preview.dir 可改根。
- prepare() 在录制启动前调用:建目录 + 清掉上次残留(seg*.ts/live.m3u8)。
- cleanup() 在录制结束时尽力清理(失败不影响任何主流程)。
- safe_join() 供 HTTP 服务做 realpath 防穿越校验。
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LIVE_DIR = os.path.join(BASE, "previews-live")

# 只清这两类 + 临时文件:目录里若混入别的东西(用户手动放的),一律不动
_CLEAN_SUFFIXES = (".ts", ".m3u8", ".tmp")


def resolve_root(cfg):
    """预览 HLS 根目录:config.live_preview.dir 为空则用项目内 previews-live/。"""
    lv = (cfg or {}).get("live_preview") or {}
    d = (lv.get("dir") or "").strip()
    return os.path.expanduser(d) if d else DEFAULT_LIVE_DIR


def streamer_dir(cfg, name):
    """某主播的预览 HLS 目录(绝对路径)。"""
    return os.path.join(resolve_root(cfg), name)


def playlist_path(cfg, name):
    """该主播的播放列表绝对路径。"""
    return os.path.join(streamer_dir(cfg, name), "live.m3u8")


def prepare(cfg, name):
    """录制启动前准备预览目录:建目录 + 清残留。

    返回目录路径;失败返回 None(调用方据此跳过预览输出,录制绝不被阻断)。
    """
    d = streamer_dir(cfg, name)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    try:
        for fn in os.listdir(d):
            if fn.lower().endswith(_CLEAN_SUFFIXES):
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return d


def cleanup(cfg, name):
    """录制结束后的尽力清理:删 ts/m3u8 残留,目录保留。

    不删目录本身:主播名可能被复用、且删除目录与"目录刚好被外部重建"存在
    竞态;清文件已足够(下次 prepare 也会再清一遍)。失败静默。
    """
    d = streamer_dir(cfg, name)
    if not os.path.isdir(d):
        return
    try:
        for fn in os.listdir(d):
            if fn.lower().endswith(_CLEAN_SUFFIXES):
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass


def safe_join(cfg, name, filename):
    """校验 filename 落在该主播的预览目录内,返回绝对路径;越界返回 None。"""
    if not filename or "/" in filename or "\\" in filename:
        return None
    d = os.path.realpath(streamer_dir(cfg, name))
    target = os.path.realpath(os.path.join(d, filename))
    if target != d and not target.startswith(d + os.sep):
        return None
    return target


def is_ready(cfg, name):
    """播放列表是否已存在且非空(前端据此显示"等待流地址/已就绪")。"""
    p = playlist_path(cfg, name)
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except OSError:
        return False
