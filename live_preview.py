#!/usr/bin/env python3
"""实时预览(按需独立拉流)会话管理。

架构(2026-10-11 决策回退后定稿,替换原「录制 ffmpeg 第二路输出」方案):
- 录制 ffmpeg 恢复单路输出,与预览完全解耦(预览故障不碰录制);
- 用户打开 /live/<主播> 页面 → POST /api/live/<主播>/start → 本模块启动一条
  **独立 ffmpeg**,从当前流地址拉第二路 CDN 连接,-c copy 源流直出 HLS;
- 页面轮询状态即续期(keepalive);无人轮询超过 idle_timeout 秒(默认 30s)
  自动回收进程与分片 —— 「不看就不拉流」;
- 并发会话上限 max_sessions(默认 3),超限拒绝新会话;
- 流地址由外部 url_provider 注入(monitor 侧提供):仅当主播正在录制且拉流
  进程存活时返回地址;provider 持续返回 None 超过 NONE_GRACE 秒 → 会话结束
  (覆盖录制 watchdog 重启的瞬时空窗);
- ffmpeg 意外退出自动重试(退避 2/5/10/20s;进程稳定存活 RETRY_RESET_SEC 后
  计数归零;连续重试耗尽 → failed,页面「刷新」会重置计数立即再试)。

职责边界:只管预览 ffmpeg 的「进程 + 目录」生命周期,不碰录制/弹幕/检测。
"""
import os
import subprocess
import threading
import time

import live_hls
from preflight import find_ffmpeg

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, "logs", "live")

IDLE_DEFAULT = 30          # 秒;无轮询超过该值回收会话
MAX_DEFAULT = 3            # 并发预览会话上限
RETRY_DELAYS = (2, 5, 10, 20)   # ffmpeg 退出后的重试退避(秒)
RETRY_RESET_SEC = 60       # 进程稳定存活超过该值,重试计数归零
NONE_GRACE = 20            # provider 持续 None 超过该值 → 判源已下线,结束会话
STALL_SEC = 45             # 清单超过该时长未更新 → 视为卡死,杀掉走重试
REAP_INTERVAL = 5          # 回收线程巡检周期(秒)

_lock = threading.Lock()
_sessions = {}             # name -> {proc, attempt, retry_at, error, last_seen, ...}
_url_provider = None       # fn(name) -> str|None
_reaper_started = False
_last_cfg = None           # 最近一次 ensure_start 传入的配置(回收线程复用)
_reap_interval = REAP_INTERVAL


def set_url_provider(fn):
    """注入流地址提供者:仅当主播正在录制且拉流进程存活时返回地址,否则 None。"""
    global _url_provider
    _url_provider = fn


def _cfg_vals(cfg):
    lv = (cfg or {}).get("live_preview") or {}

    def _i(key, default, lo, hi):
        try:
            v = lv.get(key)
            return max(lo, min(hi, int(v if v is not None else default)))
        except (TypeError, ValueError):
            return default

    return {"idle": _i("idle_timeout", IDLE_DEFAULT, 5, 3600),
            "max": _i("max_sessions", MAX_DEFAULT, 1, 10),
            "hls_time": _i("hls_time", 2, 1, 30),
            "list_size": _i("list_size", 6, 2, 30)}


def _err_path(name):
    safe = "".join(ch for ch in str(name) if ch.isalnum() or ch in "._-") or "unknown"
    return os.path.join(LOG_DIR, f"{safe[:60]}.err.log")


def _build_args(ffmpeg, cfg, name, url, vals):
    """预览 ffmpeg 参数:单输出,-c copy 源流直出 HLS(与实测验证过的形态一致)。"""
    d = live_hls.streamer_dir(cfg, name)
    return [ffmpeg, "-y", "-loglevel", "warning",
            # 与录制部署同款的拉流形态(墙钟时间戳 + copyts,播放器侧无感)
            "-use_wallclock_as_timestamps", "1",
            "-i", url, "-copyts",
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
            "-f", "hls", "-hls_time", str(vals["hls_time"]),
            "-hls_list_size", str(vals["list_size"]),
            "-hls_flags", "delete_segments+omit_endlist",
            "-hls_segment_filename", os.path.join(d, "seg-%05d.ts"),
            os.path.join(d, "live.m3u8")]


def _launch(args, err_path):
    """启动 ffmpeg(独立函数,便于测试替换)。"""
    errf = None
    try:
        os.makedirs(os.path.dirname(err_path), exist_ok=True)
        errf = open(err_path, "w", encoding="utf-8", errors="replace")
    except OSError:
        errf = None
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=errf or subprocess.DEVNULL)
    finally:
        if errf:
            errf.close()


def _kill_proc(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        pass


def _tail_err(name):
    """摘 ffmpeg stderr 末两行(≤200 字),给界面显示失败原因。"""
    try:
        with open(_err_path(name), "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        return " / ".join(lines[-2:])[:200]
    except OSError:
        return ""


def _spawn(cfg, name, vals):
    """启动会话的 ffmpeg(调用方持有 _lock)。返回 (proc, err);失败时 proc=None。"""
    url = _url_provider(name) if _url_provider else None
    if not url:
        return None, "暂无可用流地址(该主播未在录制?)"
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None, "未找到 ffmpeg"
    d = live_hls.prepare(cfg, name)
    if not d:
        return None, "预览目录不可用"
    try:
        proc = _launch(_build_args(ffmpeg, cfg, name, url, vals), _err_path(name))
    except Exception as e:
        return None, f"ffmpeg 启动失败: {e}"
    return proc, ""


def _alive(s):
    return s["proc"] is not None and s["proc"].poll() is None


def _state_of(cfg, name, s):
    if _alive(s):
        return "ready" if live_hls.is_ready(cfg, name) else "starting"
    if s["attempt"] > len(RETRY_DELAYS):
        return "failed"
    return "starting"          # 重试等待中


def ensure_start(cfg, name):
    """打开预览页时调用:没有会话就创建,已有会话则续期。返回 {ok, state|error}。"""
    global _last_cfg
    _last_cfg = cfg
    _reap_once()               # 先清掉已空闲/已下线的旧会话,再判断上限
    vals = _cfg_vals(cfg)
    now = time.monotonic()
    with _lock:
        s = _sessions.get(name)
        if s is not None:
            s["last_seen"] = now
            if not _alive(s):
                # 手动重试语义:重置计数,立即再拉一次
                s["attempt"] = 0
                s["dead_noted"] = True
                s["retry_at"] = now
        else:
            if len(_sessions) >= vals["max"]:
                return {"ok": False, "name": name,
                        "error": f"预览会话已达上限({vals['max']} 路),"
                                 f"请先关闭其他预览窗口"}
            proc, err = _spawn(cfg, name, vals)
            if proc is None:
                return {"ok": False, "name": name, "error": err}
            _sessions[name] = {
                "proc": proc, "attempt": 0, "retry_at": None, "error": "",
                "last_seen": now, "none_since": None,
                "started_at": now, "wall_started": time.time(),
                "dead_noted": False, "created_at": now,
            }
            _ensure_reaper()
    _reap_once()               # 处置刚发现的死进程(手动重试立即生效)
    with _lock:
        s = _sessions.get(name)
        return {"ok": True, "name": name,
                "state": _state_of(cfg, name, s) if s else "starting"}


def touch(name):
    """轮询续期(页面每 3s 轮询状态即调用)。"""
    with _lock:
        s = _sessions.get(name)
        if s is not None:
            s["last_seen"] = time.monotonic()


def status(cfg, name):
    """会话状态:{active, state: absent|starting|ready|failed, error, attempt}。"""
    with _lock:
        s = _sessions.get(name)
        if not s:
            return {"active": False, "state": "absent"}
        return {"active": True, "state": _state_of(cfg, name, s),
                "error": s.get("error") or "", "attempt": s["attempt"],
                "started_at": s.get("wall_started")}


def stop(cfg, name):
    """终止会话并清理分片。"""
    with _lock:
        s = _sessions.pop(name, None)
    if s is None:
        return False
    _kill_proc(s["proc"])
    try:
        live_hls.cleanup(cfg, name)
    except Exception:
        pass
    return True


def stop_all(cfg):
    for name in list(_sessions):
        stop(cfg, name)


def _respawn(cfg, name, vals):
    """重试拉起(调用方不持锁)。"""
    with _lock:
        s = _sessions.get(name)
        if s is None or _alive(s):
            return
        proc, err = _spawn(cfg, name, vals)
        if proc is None:
            s["proc"] = None
            s["error"] = err
            s["dead_noted"] = True
            s["attempt"] += 1
            idx = min(s["attempt"], len(RETRY_DELAYS)) - 1
            s["retry_at"] = time.monotonic() + RETRY_DELAYS[idx]
        else:
            s["proc"] = proc
            s["error"] = ""
            s["dead_noted"] = False
            s["started_at"] = time.monotonic()
            s["wall_started"] = time.time()
            s["retry_at"] = None


def _reap_once(now=None):
    """单次巡检:空闲回收 / 源下线 / 退出重试 / 卡死重拉。now 可注入(测试)。"""
    now = time.monotonic() if now is None else now
    cfg = _last_cfg
    if cfg is None:
        return {"stopped": [], "respawned": []}
    vals = _cfg_vals(cfg)
    stop_names, respawn_names = [], []
    with _lock:
        for name, s in list(_sessions.items()):
            # 1) 空闲回收(页面关闭后无人轮询)
            if now - s["last_seen"] > vals["idle"]:
                stop_names.append(name)
                continue
            # 2) 流地址来源状态(未录制/无地址 → 宽限期后结束会话)
            url = _url_provider(name) if _url_provider else None
            if not url:
                if s["none_since"] is None:
                    s["none_since"] = now
                if now - s["none_since"] > NONE_GRACE:
                    stop_names.append(name)
                    continue
            else:
                s["none_since"] = None
            if _alive(s):
                if now - s["started_at"] > RETRY_RESET_SEC:
                    s["attempt"] = 0
                # 3) 卡死检测:清单长期不更新 → 杀掉,走重试
                if _stalled(cfg, name, s):
                    _kill_proc(s["proc"])
                    s["proc"] = None
                    s["dead_noted"] = False
            if not _alive(s):
                if not s["dead_noted"]:
                    s["dead_noted"] = True
                    s["error"] = _tail_err(name)
                    s["attempt"] += 1
                    idx = min(s["attempt"], len(RETRY_DELAYS)) - 1
                    s["retry_at"] = now + RETRY_DELAYS[idx]
                    s["proc"] = None
                elif (s["retry_at"] is not None and now >= s["retry_at"]
                      and url and s["attempt"] <= len(RETRY_DELAYS)):
                    respawn_names.append(name)
    for name in stop_names:
        stop(cfg, name)
    for name in respawn_names:
        _respawn(cfg, name, vals)
    return {"stopped": stop_names, "respawned": respawn_names}


def _stalled(cfg, name, s):
    """清单(m3u8)超过 STALL_SEC 未更新 → 判卡死。还没有清单时给足启动宽限。"""
    import time as _t
    p = live_hls.playlist_path(cfg, name)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return (_t.time() - s.get("wall_started", _t.time())) > STALL_SEC
    return (_t.time() - mt) > STALL_SEC


def _ensure_reaper():
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    th = threading.Thread(target=_reap_loop, daemon=True,
                          name="live-preview-reaper")
    th.start()


def _reap_loop():
    while True:
        time.sleep(_reap_interval)
        try:
            _reap_once()
        except Exception:
            pass
