#!/usr/bin/env python3
"""内嵌 Web UI:参数展示/调整 + 主播状态。"""
import glob
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from preflight import run_checks, find_ffmpeg
import health as health_mod
import nas as nas_mod
import preview as preview_mod
import link_resolver
import subtitle as subtitle_mod
import retention as retention_mod

BASE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE, "static", "index.html")


def _monitor_live():
    """取「真正跑着监控循环」的那个 monitor 模块对象,而不是它的副本。

    monitor.py 作为脚本启动时,sys.modules 里的名字是 __main__。此时在本文件里
    写 `import monitor`,Python 会按名字找不到、于是把 monitor.py 再加载一遍,
    得到一个**全新的模块对象**:它的 _history 永远停在源码里的初始值(空列表)。
    后果有两处,都很隐蔽:
      · 读:本想读实时 history,结果读到空列表,静默退化到 state.history;
      · 写:本想改写 history,结果改的是副本,真实状态纹丝不动(下次落盘又被冲掉)。
    所以这里优先认 __main__(带 _history 等运行时状态),取不到才回退 import。
    单元测试里 webui 是被 import 的,__main__ 是 pytest,故回退路径同样必要。
    """
    try:
        m = sys.modules.get("__main__")
        if m is not None and hasattr(m, "_history"):
            return m
    except Exception:
        pass
    try:
        import monitor as monitor_mod
        return monitor_mod
    except Exception:
        return None


def _history_snapshot(state=None):
    """取当前录制历史(优先运行时实时列表,回退 state 快照)。"""
    monitor_mod = _monitor_live()
    hist = getattr(monitor_mod, "_history", None) if monitor_mod else None
    if not hist and state is not None:
        hist = state.get_history()
    return list(hist or [])


class State:
    """线程安全的配置与状态容器,供监控循环与 Web UI 共享。"""

    # schedule 数值字段校验规则: 字段 -> (类型, 下限, 上限)
    _SCHEDULE_RULES = {
        "cold_interval": ("数值", 30, None),
        "cold_jitter": ("数值", 0, 1),
        "hot_min": ("数值", 30, None),
        "hot_max": ("数值", 30, None),
        "live_interval": ("数值", 30, None),
        "hotspot_half_width": ("数值", 1, None),
        "hotspot_max": ("整数", 1, 5),
        "hotspot_merge_hours": ("数值", 0.1, 24),
        "hotspot_decay_days": ("整数", 1, None),
        "analysis_hour": ("整数", 0, 23),
        "analysis_cluster_min": ("整数", 10, 720),
        "analysis_min_events": ("整数", 1, None),
        "event_retention_days": ("整数", 1, 90),
        "recency_weight": ("数值", 1, 10),
        "deviation_trigger_minutes": ("整数", 5, 720),
        "max_witness_gap": ("数值", 60, None),
        "circuit_errors": ("整数", 1, None),
        "circuit_cooldown": ("数值", 1, None),
    }

    def __init__(self, cfg_path: str, config: dict, store):
        self.cfg_path = cfg_path
        self.config = config
        self.store = store
        self.lock = threading.RLock()
        self.streamers = {}
        self.commands = []
        self.monitors_version = 0
        self.nas_status = {"mounted": False, "backoff_s": 0, "queue_depth": 0, "failed": 0, "last_archive": None}
        self.refresh_pending = {}  # name -> bool:手动刷新请求(由 run_streamer 轮询消费)
        self.recordings = {}  # name -> 录制状态(recording/started_at/duration/output_path/stream_url)
        self.history = []  # 录制历史(由 monitor 写入)
        self.rec_ctrl = None  # {stop, start} 录制手动控制回调(由 monitor 注入)
        self.cookie_guard = None  # Cookie 使用护栏(由 monitor 注入,界面只读统计)

    def set_rec_ctrl(self, ctrl):
        with self.lock:
            self.rec_ctrl = ctrl

    def get_rec_ctrl(self):
        with self.lock:
            return self.rec_ctrl

    def request_refresh(self, name):
        with self.lock:
            self.refresh_pending[name] = True

    def take_refresh(self, name):
        with self.lock:
            v = self.refresh_pending.get(name, False)
            if v:
                self.refresh_pending[name] = False
            return v

    def set_recording(self, name, rec):
        with self.lock:
            self.recordings[name] = rec

    def remove_recording(self, name):
        with self.lock:
            self.recordings.pop(name, None)

    def set_history(self, entries):
        with self.lock:
            self.history = json.loads(json.dumps(entries))

    def get_history(self):
        with self.lock:
            return json.loads(json.dumps(self.history))

    def get_config(self):
        with self.lock:
            return json.loads(json.dumps(self.config))

    def set_nas_status(self, status):
        with self.lock:
            self.nas_status = json.loads(json.dumps(status))

    def get_nas_status(self):
        with self.lock:
            return json.loads(json.dumps(self.nas_status))

    def _validate(self, patch: dict):
        """校验 patch,返回错误字符串;通过返回 None。"""
        if "monitors" in patch:
            mons = patch["monitors"]
            if not isinstance(mons, list):
                return "monitors: 必须是数组"
            for m in mons:
                if not isinstance(m, dict):
                    return "monitors: 每一项必须是对象"
                if "auto_record" in m and not isinstance(m["auto_record"], bool):
                    return "monitors.auto_record: 必须是布尔值"
                if "note" in m:
                    if not isinstance(m["note"], str):
                        return "monitors.note: 必须是字符串"
                    if len(m["note"]) > 200:
                        return "monitors.note: 最多 200 字"
        if "detection" in patch:
            det = patch["detection"]
            if not isinstance(det, dict):
                return "detection: 必须是对象"
            mode = det.get("mode")
            if mode is not None and mode not in ("302", "api", "mix"):
                return "detection.mode: 必须是 302/api/mix 之一"
        if "schedule" in patch:
            sched = patch["schedule"]
            if not isinstance(sched, dict):
                return "schedule: 必须是对象"
            for k, v in sched.items():
                if k not in self._SCHEDULE_RULES:
                    continue
                kind, lo, hi = self._SCHEDULE_RULES[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return f"schedule.{k}: 必须是数值"
                if kind == "整数" and isinstance(v, float) and not v.is_integer():
                    return f"schedule.{k}: 必须是整数"
                if v < lo:
                    return f"schedule.{k}: 不能小于 {lo}"
                if hi is not None and v > hi:
                    return f"schedule.{k}: 不能大于 {hi}"
            merged = {**self.config.get("schedule", {}), **sched}
            if merged["hot_min"] > merged["hot_max"]:
                return "schedule: hot_min 必须 <= hot_max"
        if "danmaku" in patch:
            dm = patch["danmaku"]
            if not isinstance(dm, dict):
                return "danmaku: 必须是对象"
            for k in ("enabled", "capture_member"):
                if k in dm and not isinstance(dm[k], bool):
                    return f"danmaku.{k}: 必须是布尔值"
            for k, lo, hi in (("offset_seconds", -3600, 3600),
                              ("gift_offset_seconds", -3600, 3600),
                              ("font_size", 10, 200),
                              ("queue_lines", 1, 8), ("queue_seconds", 2, 60)):
                if k in dm:
                    v = dm[k]
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        return f"danmaku.{k}: 必须是数值"
                    if v < lo or v > hi:
                        return f"danmaku.{k}: 取值范围 {lo} ~ {hi}"
            if "style" in dm and dm["style"] not in ("queue", "scroll"):
                return "danmaku.style: 只能是 queue 或 scroll"
        return None

    def update_config(self, patch: dict):
        """合并更新配置并校验落盘;返回契约 §2 结构。"""
        with self.lock:
            err = self._validate(patch)
            if err:
                return {"ok": False, "error": err, "restart_required": False}

            restart_required = False
            if "webui" in patch and isinstance(patch["webui"], dict):
                old = self.config.get("webui", {})
                new = patch["webui"]
                if ("host" in new and new["host"] != old.get("host")) or \
                        ("port" in new and new["port"] != old.get("port")):
                    restart_required = True

            monitors_changed = False
            if "monitors" in patch:
                mons = patch["monitors"]
                if isinstance(mons, list):
                    # 历史主播没有这个字段,真实添加时间无法还原。统一以「当前
                    # 时间」为基准,再按配置里的数组顺序往前推每分钟一个 ——
                    # 时间戳都落在"刚刚"(数据整齐),既保留原有先后顺序,又让后
                    # 续新加的主播(写入真实时间戳)始终排在最前。全部相同会让
                    # 排序退化成按姓名排,排序项形同虚设。
                    now = int(time.time())
                    n = len(mons)
                    for i, m in enumerate(mons):
                        if isinstance(m, dict) and not m.get("added_at"):
                            m["added_at"] = now - (n - i) * 60
                old_mon = json.dumps(self.config.get("monitors", []), ensure_ascii=False, sort_keys=True)
                new_mon = json.dumps(patch["monitors"], ensure_ascii=False, sort_keys=True)
                monitors_changed = old_mon != new_mon

            for k, v in patch.items():
                # dict 配置项做「合并」而非整体替换:前端表单只提交部分字段,
                # 整体替换会丢掉未提交字段(如 recorder.output_dir/enabled)
                if k in ("detection", "schedule", "recorder", "preview", "archive", "nas", "danmaku") \
                        and isinstance(v, dict) and isinstance(self.config.get(k), dict):
                    self.config[k].update(v)
                else:
                    self.config[k] = v

            self._save()
            if monitors_changed:
                self.monitors_version += 1
                # 事件触发:主播刚被删掉就登记进孤儿台账,让 30 天冷静期从
                # 删除那一刻起算,而不是等下一次定时巡检才被发现。
                # 只登记不清理 —— 真正的删除由 retention_loop / 手动 sweep 执行。
                try:
                    m = retention_mod.mark(self.config)
                    if m["added"]:
                        self.last_retention_mark = m
                except Exception:
                    pass
            # 配置改了就把护栏规则重新载入,否则界面上改半天不生效
            if self.cookie_guard is not None:
                try:
                    self.cookie_guard.configure(self.config)
                except Exception:
                    pass
            return {"ok": True, "error": None, "restart_required": restart_required}

    def set_cookie_guard(self, guard):
        """注入 Cookie 护栏实例(由 monitor.main 调用)。

        webui 多线程读它做统计展示,monitor 的检测循环用它做裁决,两边共用
        同一个实例,界面上看到的用量就是真实的用量。
        """
        with self.lock:
            self.cookie_guard = guard

    def get_cookie_guard(self):
        with self.lock:
            return self.cookie_guard

    def _save(self):
        # 写前留一份上一版:整段提交 monitors 是覆盖式写入,出错没有回头路。
        # 只留最近一份,不堆积文件。
        if os.path.basename(str(self.cfg_path)) == "config.json":
            try:
                if os.path.exists(self.cfg_path) and os.path.getsize(self.cfg_path) > 0:
                    with open(self.cfg_path, "rb") as src, \
                            open(os.path.join(os.path.dirname(str(self.cfg_path)) or ".",
                                              "config.prev.json"), "wb") as dst:
                        dst.write(src.read())
            except OSError:
                pass
        tmp = self.cfg_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cfg_path)
        except OSError:
            pass

    def set_streamer(self, name, snap):
        with self.lock:
            self.streamers[name] = snap

    def remove_streamer(self, name):
        with self.lock:
            self.streamers.pop(name, None)

    def add_command(self, pid, name, cmd):
        with self.lock:
            self.commands.append({"pid": pid, "name": name, "cmd": cmd, "exit_code": None})
            if len(self.commands) > 100:
                self.commands = self.commands[-100:]

    def mark_command_exit(self, pid, code):
        with self.lock:
            for c in self.commands:
                if c["pid"] == pid:
                    c["exit_code"] = code
                    break

    def get_status(self):
        with self.lock:
            return {
                "config": json.loads(json.dumps(self.config)),
                "streamers": json.loads(json.dumps(self.streamers)),
                "recordings": json.loads(json.dumps(self.recordings)),
                "commands": json.loads(json.dumps(self.commands)),
                "nas": json.loads(json.dumps(self.nas_status)),
            }


def _allowed_roots(cfg):
    """允许打开的根目录白名单:录制目录 + NAS 挂载点 + 外接硬盘目录。

    放宽原因:归档后文件已不在录制目录,需要能打开归档目标中的文件。
    仍严格限制在这三处,防止任意路径打开。
    """
    roots = []
    out = (cfg.get("recorder") or {}).get("output_dir", "") or os.path.join(BASE, "recordings")
    roots.append(os.path.realpath(os.path.expanduser(out)))
    nas_cfg = cfg.get("nas") or {}
    if nas_cfg.get("enabled", False):
        mp = nas_cfg.get("mount_point") or "~/DouyinArchive"
        roots.append(os.path.realpath(os.path.expanduser(mp)))
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext:
        roots.append(os.path.realpath(os.path.expanduser(ext)))
    return [r for r in roots if r]


# ── 项目指引:可跳转的目录清单 ──
# 想加/减目录项,只改这里:rel 相对项目根;dynamic 走特殊解析(nas_root 读配置里的
# 挂载点, launchagents 是固定的系统目录)。前端按 group 分组渲染,不需要改前端。
GUIDE_FOLDERS = [
    {"group": "项目目录", "items": [
        {"key": "root", "name": "项目根目录", "rel": ".",
         "desc": "源码、配置、脚本都在这里"},
        {"key": "logs", "name": "主业务日志目录", "rel": "logs",
         "desc": "monitor-日期.log 按天轮转,开播/录制/归档全记录"},
        {"key": "previews", "name": "主播头像截图目录", "rel": "previews",
         "desc": "按主播分目录,主播卡片头像取自这里"},
        {"key": "recordings", "name": "录制暂存目录", "rel": "recordings",
         "desc": "flv 成片与 ass 弹幕,下播后自动归档到 NAS"},
        {"key": "spool", "name": "归档队列目录", "rel": "spool",
         "desc": "待归档任务 pending.json,正常情况为空"},
        {"key": "auth", "name": "登录状态目录", "rel": "auth",
         "desc": "登录态与扫码用的 Chrome profile"},
    ]},
    {"group": "系统与归档", "items": [
        {"key": "nas_root", "name": "NAS 归档总库", "dynamic": "nas_root",
         "desc": "按主播/日期归档的成片,读配置里的挂载点"},
        {"key": "launchagents", "name": "服务定义目录", "dynamic": "launchagents",
         "desc": "com.douyin.monitor.plist 所在,改服务配置会用到"},
    ]},
    # 文档项指向文件而非目录,_guide_folders/_open_guide_folder 用 exists 判定,
    # 所以既列得出也点得开。
    {"group": "项目文档", "items": [
        {"key": "doc_framework", "name": "项目框架说明书", "rel": "项目框架说明书.md",
         "desc": "目录结构 / 模块职责边界 / 数据流 / 配置项 / 安全边际三区,交接先看这个"},
        {"key": "doc_migrate", "name": "项目整理与迁移方案", "rel": "项目整理与迁移方案.md",
         "desc": "清理风险清单与整体迁移的执行记录"},
    ]},
]


def _guide_path(item, cfg):
    """把指引项解析成实际路径。相对项基于 BASE,所以整个项目搬走后仍然正确。"""
    if item.get("rel") is not None:
        return os.path.realpath(os.path.join(BASE, item["rel"]))
    kind = item.get("dynamic")
    if kind == "nas_root":
        nas_cfg = cfg.get("nas") or {}
        mp = os.path.expanduser(nas_cfg.get("mount_point") or "~/DouyinArchive")
        rd = (nas_cfg.get("root_dir") or "").strip()
        return os.path.realpath(os.path.join(mp, rd) if rd else mp)
    if kind == "launchagents":
        return os.path.realpath(os.path.expanduser("~/Library/LaunchAgents"))
    return ""


def _guide_folders(cfg):
    """返回分组后的目录清单,含实际路径与是否存在(前端据此置灰)。"""
    groups = []
    for g in GUIDE_FOLDERS:
        items = []
        for it in g["items"]:
            p = _guide_path(it, cfg)
            # 目录项与文档项混排,故用 exists 而非 isdir
            items.append({"key": it["key"], "name": it["name"], "desc": it["desc"],
                          "path": p, "exists": os.path.exists(p)})
        groups.append({"group": g["group"], "items": items})
    return {"groups": groups, "base": BASE}


def _open_guide_folder(key, cfg):
    """按 key 打开目录。只认清单里登记过的 key,不接受任意路径 ——
    比通用的 /api/open 白名单更紧,清单外根本进不来。"""
    for g in GUIDE_FOLDERS:
        for it in g["items"]:
            if it["key"] != key:
                continue
            p = _guide_path(it, cfg)
            if not os.path.exists(p):
                return {"ok": False, "error": f"路径不存在:{p}"}
            try:
                if sys.platform == "darwin":
                    subprocess.Popen(["open", p])
                elif sys.platform == "win32":
                    subprocess.Popen(["explorer", p])
                else:
                    subprocess.Popen(["xdg-open", p])
            except OSError as e:
                return {"ok": False, "error": f"打开失败:{e}"}
            return {"ok": True, "name": it["name"], "path": p}
    return {"ok": False, "error": "未知的目录项"}


# 挂载状态缓存。历史列表这类接口会对**每一条**记录问一次"NAS 挂载了吗",
# 而判定的实质是 subprocess 跑一遍 mount——180 条历史就是 180 次子进程,
# 实测把 /api/history 拖到 12 秒才返回。挂载状态是分钟级变化的东西,
# 几秒内复用完全够用。按挂载点做 key,改了配置也不会读到旧结果。
_MOUNT_CACHE = {"key": None, "ts": 0.0, "val": False}
_MOUNT_TTL = 5.0


def _nas_mounted(cfg, ttl=_MOUNT_TTL):
    """NAS 当前是否已挂载(供前端判断归档路径能否打开)。

    结果按 ttl 秒缓存(见 _MOUNT_CACHE);ttl<=0 表示强制重新探测。
    """
    nas_cfg = cfg.get("nas") or {}
    if not nas_cfg.get("enabled", False):
        return False
    try:
        mp = nas_mod._mount_point(nas_cfg)
    except Exception:
        return False
    now = time.time()
    # 用 .get() 取值:缓存字典可能被外部清空,硬取键会 KeyError 并让整个接口 500
    if (ttl > 0
            and _MOUNT_CACHE.get("key") == mp
            and now - (_MOUNT_CACHE.get("ts") or 0) < ttl):
        return _MOUNT_CACHE.get("val", False)
    try:
        val = bool(nas_mod._is_mounted(mp))
    except Exception:
        val = False
    _MOUNT_CACHE.update(key=mp, ts=now, val=val)
    return val


def _history_files(entry, cfg):
    """计算一场录制各文件的**当前实际位置**(随归档状态自动切换本机/归档目标)。

    返回:
      video    视频分片模板路径(可能含 %03d)
      ass      字幕(与视频同名同目录)
      meta_dir .meta 目录(弹幕/礼物明细,与视频同目录)
      jsonl    弹幕礼物事件流
      align    时间戳对齐元数据
      location local|archive(供前端显示"存在哪")
    """
    out = entry.get("output_path") or ""
    if not out:
        return {}
    src_dir = os.path.dirname(out)
    base = os.path.basename(out)          # <主播>-<ts>-%03d.flv
    stem = base
    for suf in ("-%03d.flv", "-%03d.mp4", "-%03d.ts"):
        if base.endswith(suf):
            stem = base[: -len(suf)]
            break
    else:
        stem = os.path.splitext(base)[0]
        if len(stem) > 3 and stem[-4] == "-" and stem[-3:].isdigit():
            stem = stem[:-4]
    arch = entry.get("archive") or {}
    archived = arch.get("state") == "archived"
    root = (arch.get("dest") or src_dir) if archived else src_dir
    ext = os.path.splitext(base)[1] or ".flv"
    # 存在性:归档目标未挂载时不做 stat(避免 SMB 长时间阻塞),标记 None=未知
    probe = (not archived) or _nas_mounted(cfg) or os.path.isdir(root)

    def _ex(p):
        if not probe:
            return None
        try:
            return os.path.exists(p)
        except OSError:
            return False

    video_p = os.path.join(root, base)
    meta_p = os.path.join(root, ".meta")
    return {
        "video": video_p, "video_exists": _ex(os.path.dirname(video_p)),
        "ass": os.path.join(root, stem + "-000" + ".ass"),
        "ass_exists": _ex(os.path.join(root, stem + "-000" + ".ass")),
        "meta_dir": meta_p, "meta_exists": _ex(meta_p),
        "jsonl": os.path.join(root, ".meta", stem + ".danmaku.jsonl"),
        "jsonl_exists": _ex(os.path.join(root, ".meta", stem + ".danmaku.jsonl")),
        "align": os.path.join(root, ".meta", stem + ".align.json"),
        "align_exists": _ex(os.path.join(root, ".meta", stem + ".align.json")),
        "dir": root, "dir_exists": _ex(root),
        "location": "archive" if archived else "local",
        "ext": ext,
    }


def _open_folder(state, body):
    """打开文件(默认播放器)或文件夹(Finder)。

    安全校验:仅允许「录制目录 / NAS 挂载点 / 外接硬盘目录」三处,
    其余一律拒绝(防止任意路径打开)。

    action: "open"=直接打开(文件用默认播放器,目录开 Finder)
            "reveal"=在 Finder 中定位显示(选中目标)
    """
    path = (body.get("path") or "").strip()
    action = (body.get("action") or "open").strip()
    if not path:
        return {"ok": False, "error": "path 不能为空"}
    if action not in ("open", "reveal"):
        return {"ok": False, "error": "action 必须是 open 或 reveal"}
    cfg = state.get_config()
    roots = _allowed_roots(cfg)
    out = roots[0] if roots else os.path.realpath(os.path.join(BASE, "recordings"))
    target = os.path.realpath(os.path.expanduser(path))
    if not any(target == r or target.startswith(r + os.sep) for r in roots):
        return {"ok": False, "error": "路径不在允许范围内(录制目录/NAS/外接硬盘)"}
    if os.path.isfile(target):
        open_target = target  # 文件:默认播放器打开
    elif os.path.isdir(target):
        open_target = target  # 目录:打开 Finder
    else:
        # 文件可能已删或含 %03d 占位符,打开其父目录
        parent = os.path.dirname(target)
        open_target = parent if os.path.isdir(parent) else out
    if not os.path.exists(open_target):
        # 归档目标未挂载(NAS/外接硬盘离线)时给明确提示,而不是静默失败
        return {"ok": False, "error": "目标不可访问(可能 NAS 未挂载或外接硬盘未连接)"}
    try:
        if action == "reveal":
            # Finder 中定位:目标存在则选中显示,不存在则回退父目录
            reveal = target if os.path.exists(target) else open_target
            subprocess.Popen(["open", "-R", reveal])
        else:
            subprocess.Popen(["open", open_target])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _save_nas(state, body):
    """校验并保存归档配置:归档模式(auto/nas/external) + NAS 连接信息。

    mode != external 时校验 host/share;mode == external 时校验 external_dir;
    NAS 密码入钥匙串,config 只存 password_ref。
    """
    mode = body.get("mode") or "auto"
    if mode not in ("auto", "nas", "external"):
        return {"ok": False, "error": "mode: 必须是 auto/nas/external 之一"}
    ext_dir = (body.get("external_dir") or "").strip()

    archive = {"mode": mode, "external_dir": ext_dir}
    if mode == "external" and not ext_dir:
        return {"ok": False, "error": "external_dir: 外接硬盘目录不能为空"}

    nas_patch = None
    if mode != "external":
        host = nas_mod.clean_host(body.get("host", ""))
        share = nas_mod.clean_share(body.get("share", ""))
        if not host:
            return {"ok": False, "error": "host: 不能为空"}
        if not share:
            return {"ok": False, "error": "share: 不能为空"}
        if "/" in share:
            return {"ok": False, "error": "share: 共享名不能包含 /,子目录请填到「归档根目录」(root_dir)"}
        try:
            port = int(body.get("port", 445))
        except (TypeError, ValueError):
            return {"ok": False, "error": "port: 必须是数字"}
        if not (1 <= port <= 65535):
            return {"ok": False, "error": "port: 必须在 1-65535 之间"}

        password = body.get("password", "")
        if password and not nas_mod.set_password(password):
            return {"ok": False, "error": "password: 写入钥匙串失败"}

        nas_patch = {
            "enabled": bool(body.get("enabled", False)),
            "protocol": body.get("protocol", "smb"),
            "host": host,
            "port": port,
            "share": share,
            "username": body.get("username", ""),
            "password_ref": "keychain:douyin-nas",  # 密码永不落盘
            "mount_point": body.get("mount_point", "/Volumes/douyin-archive"),
            "root_dir": body.get("root_dir", "录播"),
        }
        # 保留白名单之外的运行时字段(IP 自动识别配置),避免 UI 保存时被剥掉
        _old_nas = state.get_config().get("nas") or {}
        for _k in ("auto_discover", "host_check_hours"):
            if _k in _old_nas:
                nas_patch[_k] = _old_nas[_k]
    else:
        # 仅外接硬盘:只更新 root_dir(归档子目录名),保留其余 nas 字段不动
        old_nas = state.get_config().get("nas") or {}
        nas_patch = {**old_nas, "root_dir": body.get("root_dir", "录播")}

    patch = {"archive": archive}
    if nas_patch is not None:
        patch["nas"] = nas_patch
    res = state.update_config(patch)
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    return {"ok": True, "error": None}


def _select_preview(state, name, fname):
    """设为正式预览图并删除该主播其余图片。"""
    cfg = state.get_config()
    target = preview_mod.safe_join(cfg, name, fname)
    if not target or not os.path.isfile(target):
        return {"ok": False, "error": "文件不存在"}
    monitors = cfg.get("monitors", [])
    found = False
    for m in monitors:
        if m.get("name") == name:
            m["preview"] = fname
            found = True
            break
    if not found:
        return {"ok": False, "error": "主播不存在"}
    res = state.update_config({"monitors": monitors})
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    preview_mod.cleanup_others(cfg, name, fname)
    return {"ok": True, "selected": fname}


def _delete_preview(state, name, fname):
    """删除单张预览图;若删的是当前预览图则清空 monitors[].preview。"""
    cfg = state.get_config()
    target = preview_mod.safe_join(cfg, name, fname)
    if not target or not os.path.isfile(target):
        return {"ok": False, "error": "文件不存在"}
    try:
        os.remove(target)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    monitors = cfg.get("monitors", [])
    changed = False
    for m in monitors:
        if m.get("name") == name and m.get("preview") == fname:
            m.pop("preview", None)
            changed = True
    if changed:
        state.update_config({"monitors": monitors})
    return {"ok": True}


def _upload_preview(state, name, data):
    """base64 上传预览图;按魔数识别真实格式,忽略客户端声明的扩展名。限 5MB。"""
    import base64
    if not data:
        return {"ok": False, "error": "data 不能为空"}
    if len(data) > 7 * 1024 * 1024:  # base64 长度上限(约 5MB 原始图)
        return {"ok": False, "error": "图片过大(限 5MB)"}
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return {"ok": False, "error": "base64 解码失败"}
    if raw[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif raw[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = "webp"
    else:
        return {"ok": False, "error": "图片格式无法识别(仅 jpg/png/webp)"}
    cfg = state.get_config()
    d = preview_mod.streamer_dir(cfg, name)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return {"ok": False, "error": "无法创建预览图目录"}
    import uuid
    fname = f"user-{uuid.uuid4().hex[:8]}.{ext}"
    try:
        with open(os.path.join(d, fname), "wb") as f:
            f.write(raw)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "file": fname}


def _delete_history(state, keys=None, clear=False):
    """删除录制历史条目。

    keys: [{started_at, output_path}] 按 (开始时间, 输出路径) 匹配删除;
    clear: True 清空全部。同步落盘(history.json)。
    """
    monitor_mod = _monitor_live()
    if monitor_mod is None:
        return {"ok": False, "error": "monitor 模块不可用"}
    entries = state.get_history()
    if clear:
        entries = []
    elif keys:
        entries = [e for e in entries
                   if not any(e.get("started_at") == k.get("started_at")
                              and e.get("output_path") == k.get("output_path") for k in keys)]
    monitor_mod._history = entries
    # 优先走统一出口(落盘 + 同步前端);旧版本只有 _save_history 时自动降级
    commit = getattr(monitor_mod, "_commit_history", None) or monitor_mod._save_history
    commit()
    state.set_history(entries)
    return {"ok": True, "count": len(entries)}


def _session_alive(path):
    """本机该路径下是否还能找到本场次的文件(按文件名前缀判断)。

    用 glob 取首个匹配即返回,不做完整 listdir——目录可能很大或在网络盘上,
    全量列举会把请求卡住(HTTP 服务是单线程的,会连带拖慢整个界面)。
    """
    try:
        d = os.path.dirname(path) or "."
        base = os.path.basename(path)
        stem = base.split("-%03d.")[0] if "-%03d." in base else os.path.splitext(base)[0]
        if not stem or not os.path.isdir(d):
            return False
        return any(True for _ in glob.iglob(os.path.join(d, glob.escape(stem) + "*")))
    except OSError:
        return False


def _danmaku_resolve_output(state, path):
    """把历史里的「本机录制模板路径」解析成当前实际位置(归档后指向归档目标)。

    历史条目永远存的是录制时生成的本机路径;归档后本机文件已被移走,
    直接用原路径会让弹幕面板误报「本场没有弹幕明细」。
    这里按该条 history 的归档状态重定位到 archive.dest(NAS/外接硬盘)。
    优先用运行中的实时 history:state.history 是给前端的快照,若某个变更点
    忘了同步,这里就会拿到过期的归档状态(表现为已归档场次被判成"NAS 未挂载")。
    """
    monitor_mod = _monitor_live()
    hist = getattr(monitor_mod, "_history", None) if monitor_mod else None
    hist = hist or state.history
    if not hist:
        return path
    cfg = state.get_config()
    for e in hist:
        if (e.get("output_path") or "") != path:
            continue
        try:
            f = _history_files(e, cfg)
        except Exception:
            return path
        return f.get("video") or path
    return path


def _dir_reachable(cfg, d):
    """目录当前是否可访问;NAS 未挂载时不做 stat(避免 SMB 长时间阻塞)。"""
    nas_cfg = cfg.get("nas") or {}
    if nas_cfg.get("enabled", False):
        try:
            mp = os.path.realpath(os.path.expanduser(
                nas_cfg.get("mount_point") or "~/DouyinArchive"))
            real = os.path.realpath(d)
            if real == mp or real.startswith(mp + os.sep):
                # 只在 mount 判定失败时才补一次 stat:mount 输出格式/编码一变
                # 就会误判成未挂载,而目录其实好好地在那儿。反过来,陈旧挂载
                # (SMB 已断但 mount 仍有记录)时 mount 判定为 True,直接返回,
                # 不会去做可能卡死的 stat。
                return _nas_mounted(cfg) or os.path.isdir(d)
        except Exception:
            pass
    try:
        return os.path.isdir(d)
    except OSError:
        return False


_ARCHIVE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _probe_archived_dir(state, path):
    """按归档目录结构直接探测落点——history 里没有归档信息时的兜底。

    归档布局固定为 <挂载点>/<root_dir>/<主播>/<YYYY-MM-DD>/,而录制路径的
    最后两级恰好是 <主播>/<日期>,所以不依赖 history 也能算出候选落点。
    用于归档元数据丢失(改过 history / 从备份恢复)但仍想读弹幕的场景;
    路径形状对不上就直接放弃,不做任何扫描。
    """
    d = os.path.dirname(path)
    date = os.path.basename(d)
    streamer = os.path.basename(os.path.dirname(d))
    if not streamer or not _ARCHIVE_DATE_RE.match(date):
        return None
    cfg = state.get_config()
    nas_cfg = cfg.get("nas") or {}
    root_dir = nas_cfg.get("root_dir") or "直播回放"
    cands = []
    if nas_cfg.get("enabled", False) and _nas_mounted(cfg):
        mp = nas_mod._mount_point(nas_cfg)
        if mp:
            cands.append(os.path.join(mp, root_dir, streamer, date))
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext:
        cands.append(os.path.join(os.path.expanduser(ext), root_dir, streamer, date))
    for c in cands:
        try:
            if os.path.isdir(c):
                return c
        except OSError:
            pass
    return None


def _danmaku_safe_output(state, path):
    """校验并解析录制输出路径(模板或文件),防路径穿越。返回实际路径或 None。

    白名单 = 录制目录 + NAS 挂载点 + 外接硬盘(放宽原因:归档后文件已移出
    录制目录,弹幕面板仍要能回放已归档场次)。
    本机已无该文件时,先按 history 归档状态重定位到归档目标再校验。
    """
    if not path or not isinstance(path, str) or "\x00" in path:
        return None
    cfg = state.get_config()
    roots = _allowed_roots(cfg)

    def _check(p):
        if not p:
            return None
        try:
            real = os.path.realpath(os.path.dirname(p) or ".")
        except Exception:
            return None
        return p if any(real == r or real.startswith(r + os.sep) for r in roots) else None

    # 先做纯路径白名单校验(不做任何 IO),再探测文件:
    # 反过来的话,白名单外的路径(如 /Users/a/Documents)会先触发一次目录列举,
    # 大目录/网络盘会把单线程 HTTP 请求卡住。
    if not _check(path):
        return None
    if _session_alive(path):
        return path
    relocated = _danmaku_resolve_output(state, path)
    if relocated != path:
        return _check(relocated)
    # history 里没查到归档信息,而本机文件又已经不在了(归档元数据丢失的典型症状):
    # 按归档目录结构探测一次落点,别让整场弹幕都读不出来。
    d = _probe_archived_dir(state, path)
    if d:
        return _check(os.path.join(d, os.path.basename(path)))
    return _check(path)


def _danmaku_guard(state, path):
    """弹幕接口的统一前置校验,返回 (out_path, cfg) 或 (None, 错误响应)。"""
    cfg = state.get_config()
    out = _danmaku_safe_output(state, path)
    if not out:
        return None, {"ok": False, "error": "路径不在允许范围内(录制目录/NAS/外接硬盘)"}
    if not _dir_reachable(cfg, os.path.dirname(out) or "."):
        return None, {"ok": False, "error": "目标不可访问(NAS 未挂载或外接硬盘未连接)"}
    return out, cfg


def _danmaku_session(state, path):
    """弹幕微调面板数据:分片列表(锚点/偏移)、全局偏移、缺口、汇总。"""
    out, guard = _danmaku_guard(state, path)
    if not out:
        return guard
    cfg = guard
    align = subtitle_mod.load_align(out)
    segs = subtitle_mod.anchor_segments(out, align)
    jsonl = subtitle_mod.jsonl_for(out)
    if not os.path.exists(jsonl):
        # 兼容旧布局:迁移前 jsonl/align 在视频同目录而非 .meta/
        alt = jsonl.replace(os.sep + ".meta" + os.sep, os.sep)
        if os.path.exists(alt):
            jsonl = alt
    has_events = os.path.exists(jsonl)
    segments = []
    for i, (idx, p, anchor) in enumerate(segs):
        ass = subtitle_mod.ass_path_for_segment(p)
        key = idx if idx is not None else "_"
        extra = (align.get("per_segment") or {}).get(key, 0) or 0
        g_extra = (align.get("gift_per_segment") or {}).get(key, 0) or 0
        off = subtitle_mod.effective_offset(align, cfg, idx)
        segments.append({
            "idx": key,
            "file": os.path.basename(p),
            "anchor": anchor,
            "anchor_str": time.strftime("%H:%M:%S", time.localtime(anchor / 1000)),
            "offset": off,
            "extra": extra,
            "gift_extra": g_extra,
            "gift_offset": subtitle_mod.effective_gift_offset(align, cfg, idx),
            "ass": os.path.exists(ass),
            "ass_file": os.path.basename(ass) if ass else "",
        })
    return {
        "ok": True, "path": out,
        "segments": segments,
        "global_offset": align.get("global_offset"),
        "config_offset": (cfg.get("danmaku") or {}).get("offset_seconds", 0) or 0,
        "gift_global_offset": align.get("gift_global_offset"),
        "gift_config_offset": (cfg.get("danmaku") or {}).get("gift_offset_seconds", 0) or 0,
        "gaps": align.get("gaps", []),
        "drift_ms": align.get("drift_ms", 0),
        "has_events": has_events,
        "summary": align.get("summary") or
                   (subtitle_mod.summarize(jsonl) if has_events else None),
    }


def _apply_one_offset_group(align, body, gkey, pkey, seg_label):
    """写入一组人工偏移(全局键 gkey + 分片键 pkey);返回错误串或 None。

    语义与旧版一致:body 里没有该键则不动;全局键传 null 表示清除(回到配置
    默认);分片键传 {} 表示清空全部单分片偏移。弹幕与礼物各走一组。
    """
    if gkey in body:
        if body[gkey] is None:
            align.pop(gkey, None)  # 清除本场人工偏移,回到配置默认
        else:
            try:
                v = float(body[gkey])
            except (TypeError, ValueError):
                return f"{gkey} 必须是数值"
            if abs(v) > 3600:
                return f"{gkey} 范围 ±3600 秒"
            align[gkey] = v
    ps = body.get(pkey)
    if ps is not None:
        if not isinstance(ps, dict):
            return f"{pkey} 必须是对象"
        merged = {} if not ps else align.setdefault(pkey, {})
        for k, v in ps.items():
            try:
                v = float(v)
            except (TypeError, ValueError):
                return f"分片 {k} {seg_label}必须是数值"
            if abs(v) > 3600:
                return f"分片 {k} {seg_label}范围 ±3600 秒"
            merged[str(k)] = v
        align[pkey] = merged
    return None


def _danmaku_apply_offset(state, body):
    """应用人工偏移并从 jsonl 重新生成 .ass(无损、可反复调)。

    两组偏移各自独立:global_offset/per_segment 管弹幕,
    gift_global_offset/gift_per_segment 管礼物(叠加在弹幕之上)。
    """
    out, guard = _danmaku_guard(state, body.get("path"))
    if not out:
        return guard
    cfg = guard
    align = subtitle_mod.load_align(out)
    for args in (("global_offset", "per_segment", "偏移"),
                 ("gift_global_offset", "gift_per_segment", "礼物偏移")):
        err = _apply_one_offset_group(align, body, *args)
        if err:
            return {"ok": False, "error": err}
    subtitle_mod.generate_session(out, cfg, align, drift_ms=align.get("drift_ms") or 0)
    return _danmaku_session(state, out)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json; charset=utf-8",
                  no_store=False):
            if isinstance(body, bytes):
                data = body
            else:
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False)
                data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if no_store:
                # 禁止浏览器缓存页面:避免更新 index.html 后用户仍跑旧版 JS
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # 客户端提前断开(前端轮询时页面刷新、curl 超时)是常态,
            # 不处理的话每次都会往 launchd.err.log 里刷一整段 BrokenPipeError 堆栈,
            # 把真正需要看的错误淹掉。
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                try:
                    with open(INDEX, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8", no_store=True)
                except OSError:
                    self._send(500, "index.html 缺失")
            elif path == "/api/config":
                self._send(200, state.get_config())
            elif path == "/api/status":
                # 带上归档根白名单:前端据此把长路径折叠成「归档根」胶囊,
                # 否则录制卡片里的文件夹地址会撑破卡片。
                st = state.get_status()
                st["archive_roots"] = _allowed_roots(state.get_config())
                self._send(200, st)
            elif path == "/api/retention":
                # 孤儿目录延迟清理台账(只读):前端展示「谁在等待、还剩几天」
                try:
                    self._send(200, retention_mod.status(state.get_config(),
                                                         _history_snapshot(state)))
                except Exception as e:
                    self._send(500, {"error": str(e)})
            elif path == "/api/folders":
                # 项目指引页:目录清单 + 实际路径(整个项目搬走后路径自动跟着变)
                self._send(200, _guide_folders(state.get_config()))
            elif path == "/api/preflight":
                try:
                    self._send(200, run_checks(state.get_config()))
                except Exception as e:
                    self._send(500, {"error": str(e)})
            elif path == "/api/nas":
                cfg = state.get_config().get("nas") or {}
                masked = {**cfg, "password": ""}  # 脱敏:密码字段恒为空串
                self._send(200, {"config": masked,
                                 "archive": state.get_config().get("archive") or {},
                                 **state.get_nas_status()})
            elif path == "/api/history":
                cfg_h = state.get_config()
                entries = []
                for e in state.get_history():
                    item = dict(e)
                    item["files"] = _history_files(e, cfg_h)  # 各文件当前实际位置
                    entries.append(item)
                self._send(200, {"entries": entries,
                                 "nas_mounted": _nas_mounted(cfg_h),
                                 "archive_roots": _allowed_roots(cfg_h)})
            elif path == "/api/danmaku/session":
                # query 参数 path=录制输出路径(模式或文件)
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                self._send(200, _danmaku_session(state, (q.get("path") or [""])[0]))
            elif path == "/api/auth":
                self._send(200, _auth_status(state))
            elif path == "/api/service":
                self._send(200, _service_status())
            elif path == "/api/health":
                # 系统健康面板(方案 B):常驻协程按心跳老化判定,
                # 进程层与外部依赖由 _health_overrides 现算后注入。
                try:
                    self._send(200, health_mod.snapshot(
                        state.get_config(), _health_overrides(state)))
                except Exception as e:
                    self._send(500, {"error": str(e)})
            elif path == "/api/shortcuts":
                # 健康页的「终端快捷指令」卡片:内容来自 scripts/ctl.py(唯一真源)
                self._send(200, _shortcut_help())
            elif path.startswith("/previews/"):
                # 静态图片服务:/previews/<主播>/<文件名>(realpath 校验防路径穿越)
                rest = path[len("/previews/"):]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    name = unquote(parts[0]); fname = unquote(parts[1])
                    cfg = state.get_config()
                    target = preview_mod.safe_join(cfg, name, fname)
                    if target and os.path.isfile(target):
                        ctype = "image/jpeg"
                        if fname.lower().endswith(".png"):
                            ctype = "image/png"
                        elif fname.lower().endswith(".webp"):
                            ctype = "image/webp"
                        try:
                            with open(target, "rb") as f:
                                self._send(200, f.read(), ctype)
                        except OSError:
                            self._send(500, {"error": "读取失败"})
                        return
                self._send(404, {"error": "not found"})
            elif path.startswith("/api/previews/"):
                name = unquote(path[len("/api/previews/"):])
                cfg = state.get_config()
                self._send(200, {
                    "name": name,
                    "images": preview_mod.list_images(cfg, name),
                    "selected": preview_mod.selected_preview(cfg, name),
                    "dir": preview_mod.streamer_dir(cfg, name),
                })
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            try:
                body = json.loads(raw)
            except Exception as e:
                self._send(400, {"ok": False, "error": f"请求体: {e}"})
                return
            if path == "/api/config":
                try:
                    res = state.update_config(body)
                except Exception as e:
                    res = {"ok": False, "error": str(e), "restart_required": False}
                self._send(200 if res["ok"] else 400, res)
            elif path == "/api/nas":
                res = _save_nas(state, body)
                self._send(200 if res["ok"] else 400, res)
            elif path == "/api/nas/test":
                self._send(200, nas_mod.test_connection(body))
            elif path == "/api/nas/shares":
                ok, shares, err = nas_mod.list_shares(
                    body.get("host", ""), body.get("username", ""),
                    body.get("password", "") or nas_mod.get_password())
                self._send(200, {"ok": ok, "shares": shares, "error": err})
            elif path == "/api/retention/sweep":
                # 手动触发一次孤儿目录清理。默认 dry-run,只有 body.apply=true 才真删。
                try:
                    r = retention_mod.sweep(
                        state.get_config(), _history_snapshot(state),
                        grace_days=int(body.get("grace_days") or
                                       retention_mod.GRACE_DAYS),
                        apply=bool(body.get("apply")),
                        allow_missing=bool(body.get("allow_missing")))
                    self._send(200, {"ok": True, **r})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path == "/api/retention/mark":
                try:
                    self._send(200, {"ok": True,
                                     **retention_mod.mark(state.get_config())})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path == "/api/folders/open":
                res = _open_guide_folder((body.get("key") or "").strip(),
                                         state.get_config())
                self._send(200 if res["ok"] else 400, res)
            elif path.startswith("/api/streamers/") and path.endswith("/refresh"):
                name = unquote(path[len("/api/streamers/"):-len("/refresh")])
                if any(m.get("name") == name for m in state.get_config().get("monitors", [])):
                    state.request_refresh(name)
                    self._send(200, {"ok": True, "name": name})
                else:
                    self._send(404, {"ok": False, "error": "主播不存在"})
            elif path.startswith("/api/streamers/") and path.endswith("/rec-stop"):
                name = unquote(path[len("/api/streamers/"):-len("/rec-stop")])
                ctrl = state.get_rec_ctrl()
                if ctrl and ctrl.get("stop"):
                    ctrl["stop"](name)
                    self._send(200, {"ok": True, "name": name})
                else:
                    self._send(400, {"ok": False, "error": "录制控制未就绪"})
            elif path.startswith("/api/streamers/") and path.endswith("/rec-start"):
                name = unquote(path[len("/api/streamers/"):-len("/rec-start")])
                ctrl = state.get_rec_ctrl()
                if ctrl and ctrl.get("start"):
                    ok = ctrl["start"](name)
                    self._send(200, {"ok": ok, "name": name,
                                     "error": None if ok else "暂无缓存流地址,请稍候或点刷新"})
                else:
                    self._send(400, {"ok": False, "error": "录制控制未就绪"})
            elif path == "/api/open-folder":
                res = _open_folder(state, body)
                self._send(200 if res["ok"] else 400, res)
            elif path == "/api/resolve":
                self._send(200, link_resolver.resolve(body.get("text", "")))
            elif path.startswith("/api/previews/") and path.endswith("/upload"):
                name = unquote(path[len("/api/previews/"):-len("/upload")])
                self._send(200, _upload_preview(state, name, body.get("data", "")))
            elif path.startswith("/api/previews/") and path.endswith("/select"):
                name = unquote(path[len("/api/previews/"):-len("/select")])
                self._send(200, _select_preview(state, name, body.get("file", "")))
            elif path.startswith("/api/previews/") and path.endswith("/delete"):
                name = unquote(path[len("/api/previews/"):-len("/delete")])
                self._send(200, _delete_preview(state, name, body.get("file", "")))
            elif path == "/api/history/delete":
                self._send(200, _delete_history(state, body.get("keys"), body.get("clear", False)))
            elif path == "/api/danmaku/offset":
                self._send(200, _danmaku_apply_offset(state, body))
            elif path == "/api/auth/login":
                self._send(200, _auth_login(state, body))
            elif path == "/api/auth/cancel":
                self._send(200, _auth_cancel(state, body))
            elif path == "/api/auth/manual":
                self._send(200, _auth_manual(state, body))
            elif path == "/api/auth/logout":
                self._send(200, _auth_logout(state))
            elif path == "/api/auth/verify":
                self._send(200, _auth_verify(state))
            elif path == "/api/service/control":
                action = (body.get("action") or "").strip()
                ok, msg = _service_control(action)
                self._send(200 if ok else 400, {"ok": ok, "action": action, "message": msg})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return Handler


# ── 服务控制(launchd 启 / 停 / 重启) ──────────────────────
# 只操作本服务自己的固定 label,不接受任何来自请求的路径或参数,避免命令注入。
LAUNCHD_LABEL = "com.douyin.monitor"
LAUNCHD_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.douyin.monitor.plist")


def _gui_domain():
    """当前用户的 launchd GUI domain,如 gui/501。"""
    return "gui/%d" % os.getuid()


def _launchctl(*args, timeout=15):
    """执行 launchctl 子命令,返回 (ok, 合并输出)。"""
    try:
        p = subprocess.run(["launchctl", *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


def _parse_etime(s):
    """解析 ps 的 etime([[DD-]hh:]mm:ss 或 DD-HH:MM:SS)为秒。"""
    if not s:
        return None
    try:
        days = 0
        if "-" in s:
            head, s = s.split("-", 1)
            days = int(head)
        nums = [int(x) for x in s.split(":")]
        if len(nums) == 3:
            h, m, sec = nums
        elif len(nums) == 2:
            h, m, sec = 0, nums[0], nums[1]
        else:
            h, m, sec = 0, 0, nums[0]
        return days * 86400 + h * 3600 + m * 60 + sec
    except Exception:
        return None


def _run(*args, timeout=10):
    """执行普通命令,返回 (ok, 合并输出)。失败静默,不影响主流程。"""
    try:
        p = subprocess.run(list(args), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception:
        return False, ""


def _webui_port(default=8780):
    """当前 Web 端口:环境变量优先(与 monitor.py 一致),其次 config.json。"""
    try:
        return int(os.environ.get("DOUYIN_MONITOR_PORT") or "")
    except ValueError:
        pass
    try:
        with open(os.path.join(BASE, "config.json"), "r", encoding="utf-8") as f:
            return int((json.load(f).get("webui") or {}).get("port", default))
    except Exception:
        return default


def _port_owner_pid(port, timeout=6):
    """返回监听该端口的进程 pid,查不到返回 None。

    PATH 被改写时(例如服务是从 IDE/Agent 环境拉起的,PATH 前排塞了各种 shim)
    裸 lsof 可能落到 shim 上或直接找不到,所以退化到绝对路径再试一次。
    """
    for exe in ("lsof", "/usr/sbin/lsof"):
        ok, out = _run(exe, "-tiTCP:%d" % port, "-sTCP:LISTEN", timeout=timeout)
        for p in (out.split() if ok and out.strip() else []):
            try:
                return int(p)
            except ValueError:
                continue
    return None


def _probe_unmanaged():
    """launchd 里查不到时,回退探测「手动/一键启动」拉起的实例。

    迁移、重装或 launchd 未注册之后很常见:进程活得好好的、Web UI 也正常响应,
    但 launchd 名下没有这个 job。只凭 launchd 判断会误报「服务已停止」,
    所以这里按监听端口找进程兜底,并标记 source=manual 让前端提示去补注册。
    """
    res = {"running": False, "pid": None, "uptime": None, "source": None}
    pid = _port_owner_pid(_webui_port())
    if not pid:
        return res
    res["running"] = True
    res["source"] = "manual"
    res["pid"] = pid
    ok2, out2 = _run("ps", "-o", "etime=", "-p", str(pid))
    if ok2 and out2:
        res["uptime"] = _parse_etime(out2.splitlines()[0].strip())
    return res


def _fmt_dur(sec):
    """把秒数写成「3 天 5 小时」这类短文案(取两级单位)。"""
    try:
        sec = int(sec)
    except Exception:
        return "-"
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分" % (sec // 60)
    if sec < 86400:
        h, m = divmod(sec // 60, 60)
        return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h
    d, h = divmod(sec // 3600, 24)
    return "%d 天 %d 小时" % (d, h) if h else "%d 天" % d


# ── 系统健康页:终端快捷指令清单(与 douyin help / 一键启动窗口同源) ──
_CTL_MODULE = {}


def _load_ctl():
    """按文件路径加载 scripts/ctl.py —— 命令清单的唯一真源。

    不能写成顶层 `import ctl`:① ctl.py 自己 `import webui`,顶层导入会成环;
    ② scripts/ 是一次性脚本目录(不是包),不在 sys.path 里。
    按路径加载则任何工作目录下都稳,且只在首次请求付一次解析成本。
    """
    mod = _CTL_MODULE.get("m")
    if mod is not None:
        return mod
    path = os.path.join(BASE, "scripts", "ctl.py")
    spec = importlib.util.spec_from_file_location("douyin_ctl", path)
    if spec is None or spec.loader is None:
        raise ImportError("找不到命令清单源文件:%s" % path)
    mod = importlib.util.module_from_spec(spec)
    # 先登记再执行:模块内若有按名字回查自己的逻辑,缺了这一步会出错
    sys.modules.setdefault("douyin_ctl", mod)
    spec.loader.exec_module(mod)
    _CTL_MODULE["m"] = mod
    return mod


def _shortcut_help():
    """终端快捷指令清单(供「系统健康」页展示)。

    内容全部来自 scripts/ctl.py 的 cheatsheet(),Web 端**不另写一份表**:
    本项目踩过「两个入口各写一份规则、只修了一边」的坑(表现为点重启没反应),
    命令清单同理 —— 写两遍迟早一边是过期的。

    另外告诉前端「入口装了没」:指令的入口在 ~/.local/bin(项目外),
    搬家或换机后最容易失效,页面上直接标出来比让人踩空强。
    """
    entry = os.path.expanduser(os.path.join("~", ".local", "bin", "douyin"))
    try:
        installed = os.path.exists(entry)
    except OSError:
        installed = False
    meta = {"ok": True, "installed": installed, "entry": entry}
    try:
        data = _load_ctl().cheatsheet()
    except Exception as e:            # 加载失败就降级,不让整个健康页跟着挂
        return dict(meta, ok=False, error=str(e),
                    install="bash scripts/install_cli.sh",
                    rows=[], aliases=[], options=[], notes=[])
    return dict(meta, **data)


def _health_overrides(state):
    """进程层与外部依赖的实时状态(不走心跳,每次请求现算)。

    常驻协程由 health.py 的心跳判定;这里是「没法靠心跳判断」的那些:
    进程是否托管、NAS 能不能写、Cookie 是否有效、ffmpeg 在不在。
    """
    now = time.time()
    cfg = state.get_config()
    over = {}

    # ── 进程层 ──
    # 能响应这个请求本身就证明主进程和 Web 服务都活着
    over["main"] = {"status": "ok", "last_beat": now,
                    "detail": "已运行 %s" % _fmt_dur(health_mod.uptime())}
    over["webui"] = {"status": "ok", "last_beat": now,
                     "detail": "%d 端口响应正常" % _webui_port()}

    try:
        svc = _service_status()
    except Exception:
        svc = {"managed": False, "running": False, "source": None}
    if svc.get("managed") and svc.get("source") == "launchd":
        over["launchd"] = {"status": "ok", "last_beat": now,
                           "detail": "已托管:开机自启,崩溃自动拉起"}
    elif svc.get("managed"):
        over["launchd"] = {"status": "warn", "last_beat": now,
                           "detail": "已装载,但当前实例不是它启动的(点「重启」交回托管)"}
    else:
        over["launchd"] = {"status": "warn", "last_beat": now,
                           "detail": "未装载:开机不会自启,崩溃不会自动拉起"}

    # ffmpeg 路数来自实时录制表;没人开播时是 idle 而不是故障
    try:
        st = state.get_status()
        recs = st.get("recordings") or {}
        n = sum(1 for v in recs.values()
                if (v or {}).get("recording") is True
                or str((v or {}).get("status")) == "recording")
    except Exception:
        n = 0
    over["ffmpeg"] = ({"status": "ok", "last_beat": now, "count": n,
                       "detail": "%d 路在录" % n} if n
                      else {"status": "idle", "last_beat": now, "count": 0,
                            "detail": "当前无在播录制"})

    # ── 外部依赖 ──
    try:
        ns = state.get_nas_status() or {}
    except Exception:
        ns = {}
    if ns.get("mounted"):
        q = ns.get("queue_depth") or 0
        over["nas_mount"] = {"status": "ok", "last_beat": now, "count": q,
                             "detail": ("已挂载,待归档 %d 项" % q) if q else "已挂载,队列为空"}
    elif (cfg.get("nas") or {}).get("enabled") or \
            (cfg.get("archive") or {}).get("external_dir"):
        over["nas_mount"] = {"status": "down", "last_beat": now,
                             "detail": "已配置但未挂载(退避 %ss),归档暂停"
                                       % ns.get("backoff_s", "?")}
    else:
        over["nas_mount"] = {"status": "idle", "last_beat": now,
                             "detail": "未配置归档目标"}

    try:
        a = _auth_status(state) or {}
    except Exception:
        a = {}
    if a.get("logged_in"):
        over["cookie"] = {"status": "ok", "last_beat": now,
                          "detail": "已登录%s" % (" · " + a["nickname"]
                                                 if a.get("nickname") else "")}
    else:
        # 登录态只影响检测通道(退化为 302),录制不受影响,所以是降级不是故障
        over["cookie"] = {"status": "warn", "last_beat": now,
                          "detail": "未登录:检测退化为 302 模式"}

    # 注意 preflight.find_ffmpeg() 无参(它自己扫 PATH + 常见 Homebrew 前缀)
    try:
        ff = find_ffmpeg()
    except Exception:
        ff = None
    over["ffmpeg_bin"] = ({"status": "ok", "last_beat": now, "detail": ff} if ff
                          else {"status": "warn", "last_beat": now,
                                "detail": "未找到 ffmpeg,开播时将无法录制"})
    return over


def _service_status():
    """查询本服务在 launchd 下的托管与运行状态。

    managed : launchd 是否已装载该 job(未装载则界面上的「启动」才有意义)
    running : 进程是否在跑(launchd 托管不到时,会回退探测手动启动的实例)
    source  : "launchd" | "manual" | None,便于前端区分提示
    """
    info = {"ok": True, "label": LAUNCHD_LABEL, "plist": LAUNCHD_PLIST,
            "plist_exists": os.path.exists(LAUNCHD_PLIST),
            "managed": False, "running": False, "pid": None, "uptime": None,
            "source": None}
    ok, out = _launchctl("print", "%s/%s" % (_gui_domain(), LAUNCHD_LABEL))
    if not ok:
        # print 失败 = job 未装载(或被 bootout 过)。但进程可能由一键启动手动
        # 拉起,不能据此就报「已停止」—— 回退按端口探测一次。
        info.update(_probe_unmanaged())
        return info
    info["managed"] = True
    info["source"] = "launchd"
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("state = ") and not info["running"]:
            # 第一段 state 属于 job 自身,其后缩进的是子进程
            info["running"] = s.split("=", 1)[1].strip() == "running"
        elif s.startswith("pid = "):
            try:
                info["pid"] = int(s.split("=", 1)[1].strip())
            except ValueError:
                pass
    if info["pid"]:
        try:
            p = subprocess.run(["ps", "-o", "etime=", "-p", str(info["pid"])],
                               capture_output=True, text=True, timeout=10)
            info["uptime"] = _parse_etime(p.stdout.strip())
        except Exception:
            pass
    if not info["running"]:
        # 已装载但 job 没在跑(state 可能是 spawn scheduled / waiting / 上次异常退出后
        # 还没拉起)。此时进程仍可能是手动或一键启动拉起的实例:launchd 名下查不到,
        # 但端口确实有人在服务。只信 launchd 会误报「已装载但未运行」,误导用户以为
        # 整个服务挂了(实际上录制/弹幕/归档全在跑),所以这里同样回退探测一次。
        probe = _probe_unmanaged()
        if probe["running"]:
            info.update(probe)     # source 变为 manual,managed 仍为 True
    return info


def _spawn_delayed(shell_cmd, delay=0.8):
    """脱离本进程、延迟执行一段 shell。

    stop/restart 会杀掉本进程:同步执行的话 HTTP 响应根本来不及写出,前端只能看到
    网络错误。这里 fork 一个脱离会话的 sh —— 先睡再执行,响应照常返回,
    前端随后轮询等待服务回来。start 更必须如此:那时本进程已经死了。
    """
    subprocess.Popen(
        ["/bin/sh", "-c", "sleep %s; %s" % (delay, shell_cmd)],
        start_new_session=True,          # 脱离进程组,本进程退出不影响它
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _outsider_pid():
    """端口上「不在 launchd 名下」的实例 pid,没有则 None。

    kickstart 只对 launchd 自己拉起的进程生效。若端口被手动/一键启动(或上次
    残留)的实例占着,新实例一 bind 就 Address already in use 退出,KeepAlive
    又立刻重拉 —— 每 10 秒崩一次、日志被刷满,而界面始终连着那个旧实例,
    点「重启」看起来毫无反应。所以重启前必须先把这个外来户请走,让出端口。
    """
    st = _service_status()
    managed_pid = st.get("pid") if st.get("source") == "launchd" else None
    pid = _port_owner_pid(_webui_port())
    if pid and pid != managed_pid:
        return pid
    return None


def _service_plan(action):
    """把「启 / 停 / 重启」展开成有序步骤:[{kind, label, cmd, pid?, wait_s?} …]。

    为什么单独抽出来
    ----------------
    同一套操作有两个入口,而且必须**行为一致**:
      · Web 界面按钮 —— 命令要延迟 + 脱离进程组执行,否则杀到自己时 HTTP 响应
        还没写出去(见 _spawn_delayed);
      · 终端快捷指令(scripts/ctl.py) —— 用户就坐在终端前,应当逐步同步执行、
        每步等一等再校验,最后把结果直接打给他。
    若把「先清外来户再 kickstart」「bootstrap 前先 bootout」这些次序规则各写一遍,
    两边迟早会悄悄分叉(一边修了、另一边还留着老 Bug)。所以规则只在本函数里
    写一次,两个入口都从这里取。

    kind 是给调用方判断「要不要等」用的:
        term / kill          结束端口上非 launchd 名的实例
        bootout / bootstrap  卸载 / 装载 launchd job
        kickstart            拉起进程
        pause                等待(wait_s 秒)

    返回 (ok, message, steps);action 非法或 plist 缺失时 steps 为空列表。
    """
    domain = _gui_domain()
    target = "%s/%s" % (domain, LAUNCHD_LABEL)
    q = shlex.quote

    if action == "restart":
        if not os.path.exists(LAUNCHD_PLIST):
            return False, "未找到 launchd 配置:%s" % LAUNCHD_PLIST, []
        outsider = _outsider_pid()
        if outsider:
            # 先 SIGTERM:monitor 收到后走 _handle_term,停 ffmpeg、落历史再退出,
            # 不会留下孤儿录制进程让新实例重复拉流。给 3 秒收尾,僵住再 SIGKILL。
            return (True,
                    "端口被非 launchd 实例占用(pid %d),已先结束它,"
                    "服务将在数秒内以最新代码恢复" % outsider,
                    [
                        {"kind": "term", "pid": outsider, "label":
                         "结束占用端口的非托管实例 pid %d(让它停录并落历史)" % outsider,
                         "cmd": "kill -TERM %d 2>/dev/null" % outsider},
                        {"kind": "pause", "wait_s": 3, "label": "等它收尾",
                         "cmd": "sleep 3"},
                        {"kind": "kill", "pid": outsider, "label": "仍未退出,强制结束",
                         "cmd": "kill -KILL %d 2>/dev/null" % outsider},
                        {"kind": "pause", "wait_s": 1, "label": "让它让出端口",
                         "cmd": "sleep 1"},
                        {"kind": "kickstart", "label": "重新拉起服务",
                         "cmd": "launchctl kickstart -k %s" % q(target)},
                    ])
        # -k = 先 kill 再拉起;KeepAlive 为 true,launchd 会重新 spawn
        return True, "重启指令已发出,服务将在数秒内恢复", [
            {"kind": "kickstart", "label": "重启服务",
             "cmd": "launchctl kickstart -k %s" % q(target)},
        ]

    if action == "stop":
        return True, "关闭指令已发出,服务即将退出(需点「启动」恢复)", [
            {"kind": "bootout", "label": "从 launchd 卸载并停止服务",
             "cmd": "launchctl bootout %s" % q(target)},
        ]

    if action == "start":
        if not os.path.exists(LAUNCHD_PLIST):
            return False, "未找到 launchd 配置:%s" % LAUNCHD_PLIST, []
        # job 已装载时直接 bootstrap 会报 already loaded,必须先卸载再装载。
        # 界面上「启动」多半就出现在这个状态(已装载但未运行),不清掉就永远起不来。
        return True, "启动指令已发出,服务将在数秒内就绪", [
            {"kind": "bootout", "label": "卸载可能已装载的旧 job",
             "cmd": "launchctl bootout %s 2>/dev/null" % q(target)},
            {"kind": "pause", "wait_s": 1, "label": "等它卸载完", "cmd": "sleep 1"},
            {"kind": "bootstrap", "label": "按 plist 装载 launchd job",
             "cmd": "launchctl bootstrap %s %s 2>/dev/null"
                    % (q(domain), q(LAUNCHD_PLIST))},
            {"kind": "pause", "wait_s": 1, "label": "等它装载完", "cmd": "sleep 1"},
            {"kind": "kickstart", "label": "拉起服务",
             "cmd": "launchctl kickstart -k %s 2>/dev/null" % q(target)},
        ]

    return False, "未知操作:%s" % action, []


def _service_control(action):
    """Web 界面按钮入口:按 _service_plan 拼成一条命令,延迟 + 脱离进程组执行。

    注意 bootout 会把 job 从 launchd 卸载,此后必须经 bootstrap 才能再启动,
    所以界面上「关闭」与「启动」是配对出现的。
    """
    ok, message, steps = _service_plan(action)
    if not ok:
        return False, message
    _spawn_delayed("; ".join(s["cmd"] for s in steps))
    return True, message


# ── 登录态(仅服务检测通道,不触碰弹幕/录制) ──────────────────
def _log_line(msg):
    """写一行日志:优先复用 monitor 的日志出口(带日期分文件),失败降级 print。

    登录链路的关键分支(浏览器启动/超时/取消/落库失败)都必须留下痕迹,
    否则用户只看到"窗口闪一下就没了",无从排查。
    """
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        import monitor as monitor_mod
        if hasattr(monitor_mod, "log"):
            monitor_mod.log(msg)
            return
    except Exception:
        pass
    print(line, flush=True)


def _auth_status(state):
    """登录态概览 + Cookie 用量。响应中不含任何 Cookie 明文。"""
    try:
        import auth as auth_mod
        out = auth_mod.status(state.get_config())
    except Exception as e:
        out = {"ok": False, "logged_in": False, "state": "none",
               "error": f"登录模块不可用: {type(e).__name__}"}
    # 附上使用量:账号风控额度是消耗品,界面上必须能看到还剩多少
    g = state.get_cookie_guard()
    if g is not None:
        try:
            out["cookie_guard"] = g.snapshot()
        except Exception:
            pass
    return out


def _auth_login(state, body):
    """启动扫码登录(异步执行,前端轮询 /api/auth 获取结果)。"""
    try:
        import auth as auth_mod
        r = auth_mod.login(timeout=int(body.get("timeout", 180) or 180),
                           log=_log_line)
        # 浏览器已打开但读不到 Cookie 时不算失败,前端转为手动粘贴模式
        if r.get("manual_required"):
            r["ok"] = True
            r["started"] = True
        if r.get("ok"):
            # 登录态已入库 → 配置切为钥匙串引用,清掉可能存在的明文
            det = state.config.setdefault("detection", {})
            det["cookie_ref"] = "keychain:douyin-cookie"
            det.pop("cookie", None)
            state._save()
            r["status"] = auth_mod.status(state.get_config())
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _auth_cancel(state, body):
    """用户主动取消登录。只停止等待,不关闭浏览器窗口。"""
    try:
        import auth as auth_mod
        _log_line("[登录] 用户取消登录")
        return auth_mod.cancel_login()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _auth_manual(state, body):
    """手动粘贴 Cookie 兜底(浏览器无法自动回读时使用)。"""
    try:
        import auth as auth_mod
        ok, err = auth_mod.set_manual_cookie(
            (body or {}).get("cookie", ""), log=_log_line)
        if not ok:
            return {"ok": False, "error": err}
        det = state.config.setdefault("detection", {})
        det["cookie_ref"] = "keychain:douyin-cookie"
        det.pop("cookie", None)
        state._save()
        return {"ok": True, "status": auth_mod.status(state.get_config())}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _auth_logout(state):
    """退出登录:清钥匙串、profile 与配置引用。"""
    try:
        import auth as auth_mod
        auth_mod.logout()
        det = state.config.setdefault("detection", {})
        det["cookie_ref"] = ""
        state._save()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _auth_verify(state):
    """立即校验一次登录态(不必等巡检周期)。"""
    try:
        import asyncio
        import httpx
        import auth as auth_mod

        cfg = state.get_config()

        async def run():
            async with httpx.AsyncClient(
                    timeout=10, follow_redirects=False,
                    trust_env=False) as c:
                return await auth_mod.verify(c, cfg)

        # webui 运行在独立线程,此处无运行中的事件循环,asyncio.run 安全
        r = asyncio.run(run())
        r["status"] = auth_mod.status(cfg)
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def start(state: State, host: str, port: int):
    try:
        srv = ThreadingHTTPServer((host, port), make_handler(state))
    except OSError as e:
        # 端口被占是最常见的启动失败:多半是上一个实例没退干净(手动拉起/旧进程
        # 残留)。此时 KeepAlive 会不断重拉,每 10 秒崩一次、日志被 traceback
        # 刷满,而界面始终连着那个旧实例、看不出异常。这里把原因和占用者 pid
        # 直接讲清楚,别只留一行异常栈。
        owner = _port_owner_pid(port)
        detail = "端口 %d 已被占用" % port
        detail += ("(占用进程 pid=%d)" % owner) if owner else "(占用进程未知)"
        detail += ",本实例退出。请结束该进程后重试: kill %s" % (owner or "<pid>")
        try:
            _log_line("[启动][!] " + detail)
        except Exception:
            pass
        print("[启动][!] %s | 原始错误: %s" % (detail, e), flush=True)
        raise
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t
