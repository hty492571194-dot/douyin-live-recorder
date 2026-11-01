#!/usr/bin/env python3
"""内嵌 Web UI:参数展示/调整 + 主播状态。"""
import glob
import asyncio
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from preflight import run_checks, find_ffmpeg
import health as health_mod
import nas as nas_mod
import notify as notify_mod      # 告警通道层(礼物诊断测试通知;不依赖 monitor,无循环导入)
import preview as preview_mod
import live_hls as live_hls_mod
import live_preview as live_preview_mod
import interop as interop_mod     # 跨项目对接层(/api/contract 本机自用;全旁路)
import incidents                  # 崩溃/故障事件流(左侧导航告警条;不依赖 monitor)
import link_resolver
import subtitle as subtitle_mod
import danmaku as danmaku_mod     # jsonl_for(录制路径→弹幕 jsonl 路径);不依赖 monitor/webui
import retention as retention_mod

BASE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE, "static", "index.html")
# 补号巡检状态(monitor.rid_backfill_loop 每轮落盘;这里只读)
RID_STATE_PATH = os.path.join(BASE, "state", "rid_backfill.json")


def _giftwatch_records(cfg, limit=200):
    """礼物诊断快照汇总:扫描全部场次的 .meta/<场次>.giftwatch.jsonl,倒序返回。

    「45 分钟无礼物时自动记录诊断」的产物就是这些文件 —— 这里是它们的查看入口。
    归档后 .meta 跟着场次搬到 NAS/外接盘,所以三处根目录都要扫;扫描范围严格
    限定在 _allowed_roots 白名单内(与「打开目录」同一份),不给任意路径读取留口子。
    """
    import glob
    recs = []
    for root in _allowed_roots(cfg):
        pattern = os.path.join(root, "*", "*", ".meta", "*.giftwatch.jsonl")
        for p in glob.glob(pattern):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        r["session"] = os.path.basename(p)[: -len(".giftwatch.jsonl")]
                        r["root"] = os.path.basename(root)
                        recs.append(r)
            except OSError:
                continue
    recs.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return {"ok": True, "total": len(recs), "records": recs[:limit]}


def _giftwatch_session_records(cfg, output_path):
    """按场次取礼物诊断记录(历史行展开区「礼物诊断」块的数据源)。

    给录制输出路径推导 .meta/<基名>.giftwatch.jsonl,在白名单三个根目录里
    定位 —— 场次可能已归档(本机源目录随归档删除),主播/日期两层结构在
    归档目标里保持不变,所以 glob 一份通吃。找不到 → 空列表而不是报错
    (前端显示「本场无诊断记录」)。
    """
    stem = ""
    try:
        stem = os.path.basename(danmaku_mod.jsonl_for(output_path or "") or "")
    except Exception:
        stem = ""
    suffix = ".danmaku.jsonl"
    if stem.endswith(suffix):
        stem = stem[: -len(suffix)]
    if not stem:
        return {"ok": True, "session": "", "records": []}
    recs = []
    for root in _allowed_roots(cfg):
        pattern = os.path.join(root, "*", "*", ".meta", stem + ".giftwatch.jsonl")
        for p in glob.glob(pattern):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        r["session"] = stem
                        r["root"] = os.path.basename(root)
                        recs.append(r)
            except OSError:
                continue
    recs.sort(key=lambda r: r.get("at") or 0, reverse=True)
    return {"ok": True, "session": stem, "records": recs}


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


def _record_windows(cfg):
    """录制期限快照(monitor.record_windows,纯派生)。

    取不到就返回空结构 —— 前端看到没有该主播的条目就不画徽标,不猜、不自算
    「剩几天」。今天(TODAY)只由服务端给,浏览器时区/时钟不可信。
    """
    mon = _monitor_live()
    fn = getattr(mon, "record_windows", None) if mon else None
    if fn is None:
        return {"today": None, "streamers": {}}
    try:
        return fn(cfg)
    except Exception:
        return {"today": None, "streamers": {}}


def _rid_backfill_status(cfg):
    """缺号补号巡检的状态快照(给前端画「巡检中/上次/下次/冷却」)。

    读的是 monitor 落的那份 state 文件 —— webui 不能 import monitor(循环依赖),
    但读同一个文件是安全的。文件不存在 = 从没跑过,返回 running=False 即可。
    所有字段都缺省成零值:前端拿不到就显示「未启动」,不猜。
    """
    import web_rid_probe as probe
    now = int(time.time())
    st = probe.empty_state(now)
    try:
        with open(RID_STATE_PATH, "r", encoding="utf-8") as f:
            st = probe.merge_state(json.load(f), now)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    norm = probe.normalize_cfg((cfg or {}).get("rid_backfill") or {})
    pending = [str(m.get("name") or "") for m in probe.pending_monitors(cfg or {})]
    until = int(st.get("window_until") or 0)
    cu = int((st.get("circuit") or {}).get("global_until") or 0)
    enabled = norm.get("enabled")
    running = bool(pending) and (enabled is True or until > now)
    return {
        "enabled": enabled if enabled != "auto" else "auto",
        "running": running,
        "reason": st.get("last_reason") or "",
        "window_start": int(st.get("window_start") or 0),
        "window_until": until,
        "pending": pending,
        "checks_today": int(st.get("checks_today") or 0),
        "max_checks_per_day": norm.get("max_checks_per_day"),
        "last_run_at": int(st.get("last_run_at") or 0),
        "next_run_at": int(st.get("next_run_at") or 0),
        "last_result": st.get("last_result") or {},
        "stats": st.get("stats") or {},
        "circuit": {"global_until": cu,
                    "reason": (st.get("circuit") or {}).get("reason") or "",
                    "cooldown_today": int((st.get("circuit") or {}).get("cooldown_today") or 0),
                    "cooling": cu > now},
        "per_monitor": {k: {"last_check": int(v.get("last_check") or 0),
                            "verdict": v.get("verdict") or "",
                            "reason": v.get("last_reason") or "",
                            "fails": int(v.get("fails") or 0),
                            "backoff_until": int(v.get("backoff_until") or 0)}
                        for k, v in (st.get("per_monitor") or {}).items()
                        if isinstance(v, dict)},
    }


def _num_plain(value):
    """配置值 → 数值;无法解析返回 None(布尔一律视为非法:True 不是 1)。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _num_kbps(value):
    """码率配置 → kbps 数值;支持 `1200` / `"1200"` / `"3000k"` / `"3M"`。

    单位口径必须与 `monitor._norm_bitrate` 一致:那边拼给 ffmpeg,这边做范围
    校验。两处若各解释一遍,就会出现「校验通过但 ffmpeg 收到看不懂的值」。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    mult = 1.0
    if s.endswith("k"):
        s, mult = s[:-1], 1.0
    elif s.endswith("m"):
        s, mult = s[:-1], 1000.0
    try:
        return float(s) * mult
    except ValueError:
        return None


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
        # 画质调度快照(由 monitor._quality_loop 每 5 s 刷新)+ 控制回调
        self.quality_status = {}
        self.quality_ctrl = None  # {"unlock": fn}(解除抖动锁定)

    def set_quality_status(self, status):
        with self.lock:
            self.quality_status = json.loads(json.dumps(status or {}))

    def get_quality_status(self):
        with self.lock:
            return json.loads(json.dumps(self.quality_status or {}))

    def set_quality_ctrl(self, ctrl):
        with self.lock:
            self.quality_ctrl = ctrl

    def get_quality_ctrl(self):
        with self.lock:
            return self.quality_ctrl

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
                # ── 录制期限(短期录制主播;2026-10-22)──
                # 校验比 UI 宽:UI 只给 1/3/7,后端收 1..365,以后加「30 天」
                # 只改前端。这里必须挡住非法值:record_until 坏了会让到期判定
                # 永远走 missing_until(表现为「主播永远不录」,且毫无提示)。
                if "record_mode" in m and m["record_mode"] not in ("long", "short", "paused"):
                    return "monitors.record_mode: 只能是 long / short / paused"
                if "record_days" in m and m["record_days"] not in (None, ""):
                    d = m["record_days"]
                    if isinstance(d, bool) or not isinstance(d, (int, float)) or \
                            int(d) != d or not (1 <= int(d) <= 365):
                        return "monitors.record_days: 必须是 1~365 的整数"
                if "record_until" in m and m["record_until"] not in (None, ""):
                    try:
                        date.fromisoformat(str(m["record_until"]).strip())
                    except ValueError:
                        return "monitors.record_until: 必须是 YYYY-MM-DD"
                if m.get("record_mode") == "short" and \
                        not str(m.get("record_until") or "").strip():
                    return "monitors: record_mode=short 必须同时给出 record_until"
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
        if "recorder" in patch:
            rec = patch["recorder"]
            if not isinstance(rec, dict):
                return "recorder: 必须是对象"
            # format 白名单:界面已是下拉,这里挡住 API 直调/手改配置的拼错
            # (拼错会让 ffmpeg 启动即失败,表现为「检测到开播却录不到文件」)
            fmt = rec.get("format")
            if fmt is not None and fmt not in ("flv", "mp4", "ts"):
                return "recorder.format: 只能是 flv / mp4 / ts 之一"
            prep = rec.get("preset")
            if prep not in (None, ""):
                # preset 白名单单一真源在 monitor.X264_PRESETS(函数内延迟 import,
                # 避免与 monitor 顶层 `from webui import ...` 形成模块级循环)
                try:
                    from monitor import X264_PRESETS as _presets
                except Exception:
                    _presets = ()
                if _presets and str(prep).strip().lower() not in _presets:
                    return "recorder.preset: 非法值(仅支持 libx264 各档 preset)"
            # 录制暂存目录(2026-10-30):保存时就挡住非法路径 —— 校验放在这里
            # 而不是等起录,是因为「写到项目里」在这条链路上是不可见的:
            # 用户只会看到录制正常,几个月后发现项目涨到几百 G。
            od = rec.get("output_dir")
            if od is not None and not isinstance(od, str):
                return "recorder.output_dir: 必须是字符串路径"
            if isinstance(od, str) and od.strip():
                err = self._validate_stage_dir(od.strip())
                if err:
                    return err
            sw = rec.get("output_dir_switch")
            if sw not in (None, "", "next_session", "now"):
                return "recorder.output_dir_switch: 只能是 next_session / now"
            err = self._validate_recorder_quality(rec)
            if err:
                return err
        return None

    def _validate_stage_dir(self, raw):
        """暂存目录校验:绝对路径 + 不在项目内 + 上级目录存在可写。

        不在这里建目录 —— 校验阶段不该有副作用(用户可能只是点了下保存又改回去)。
        真正建目录在 monitor.set_stage_dir 里做,失败会把配置回滚。
        """
        p = os.path.expanduser(raw)
        if not os.path.isabs(p):
            return "recorder.output_dir: 必须是绝对路径(或以 ~/ 开头)"
        real = os.path.realpath(p)
        base = os.path.realpath(BASE)
        if real == base or real.startswith(base + os.sep):
            return ("recorder.output_dir: 不能放在项目文件夹内"
                    "(项目只存系统文件;录制视频请指向项目外目录,"
                    "默认 ~/Movies/DouyinRecordings)")
        if os.path.exists(real) and not os.path.isdir(real):
            return f"recorder.output_dir: 同名文件已存在,不是目录({real})"
        parent = os.path.dirname(real) or "/"
        # 允许最后一级不存在(新建目录),但父目录必须在
        while parent and not os.path.exists(parent):
            nxt = os.path.dirname(parent)
            if nxt == parent:
                break
            parent = nxt
        if not os.path.isdir(parent):
            return f"recorder.output_dir: 上级目录不存在({parent})"
        if not os.access(parent, os.W_OK):
            return f"recorder.output_dir: 上级目录不可写({parent})"
        return None

    # 画质调度相关配置项:字段 -> (类型, 下限, 上限)
    # 与 monitor.DEFAULT_CONFIG["recorder"] 一一对应(前端高级参数页分组表同源)。
    _RECORDER_RULES = {
        "degrade_cpu": ("数值", 60, 99),
        "degrade_engine": ("数值", 60, 99),
        "degrade_recover": ("数值", 30, 90),
        "degrade_hold": ("数值", 10, 600),
        "degrade_recover_hold": ("数值", 60, 3600),
        "degrade_min_gap": ("数值", 60, 7200),
        "degrade_max_restore": ("整数", 0, 50),
        "degrade_max_switches": ("整数", 1, 50),
        "min_gain": ("数值", 0, 50),
        "max_hw_streams": ("整数", 1, 12),
        "bitrate": ("数值", 0, 6000),
    }
    _VIDEO_MODES = ("vt", "sw", "copy")
    _TIERS = (0, 1080, 864, 720)

    def _validate_recorder_quality(self, rec):
        """校验编码路线 / 档位 / 码率 / 降级六键与阈值。返回错误串或 None。

        为什么这些必须在保存时就挡住:档位非法 → `-vf` 参数错误 → ffmpeg
        启动即退出(表现成「开播却录不到文件」);编码路线非法 → 同上;
        降级档非法 → 触发时静默跳过,用户以为开着其实没开。
        """
        mode = rec.get("video_mode")
        if mode not in (None, "") and str(mode).strip().lower() not in self._VIDEO_MODES:
            return "recorder.video_mode: 只能是 vt(硬编)/ sw(软编)/ copy(流复制)"
        for key in ("hw_decode", "force_hw", "auto_degrade"):
            if key in rec and not isinstance(rec[key], bool):
                return f"recorder.{key}: 必须是布尔值"
        for key in ("resolution", "degrade_l1_resolution", "degrade_l2_resolution"):
            if key not in rec or rec[key] in (None, ""):
                continue
            try:
                tier = int(str(rec[key]).strip())
            except (TypeError, ValueError):
                return f"recorder.{key}: 必须是 0 / 1080 / 864 / 720 之一"
            if tier not in self._TIERS:
                return (f"recorder.{key}: 只能是 0(原生)/ 1080 / 864 / 720,"
                        f"收到 {rec[key]!r}(非法值会让 ffmpeg 起不来)")
        for key, (kind, lo, hi) in self._RECORDER_RULES.items():
            if key not in rec or rec[key] in (None, ""):
                continue
            num = _num_kbps(rec[key]) if key == "bitrate" else _num_plain(rec[key])
            if num is None:
                return f"recorder.{key}: 必须是数值"
            if kind == "整数" and not float(num).is_integer():
                return f"recorder.{key}: 必须是整数"
            if num < lo or num > hi:
                return f"recorder.{key}: 取值范围 {lo} ~ {hi}"
        if "degrade_recover" in rec and "degrade_cpu" in rec:
            try:
                if float(rec["degrade_recover"]) >= float(rec["degrade_cpu"]):
                    return ("recorder.degrade_recover: 必须小于 degrade_cpu"
                            "(否则没有滞回,会在临界点反复切档)")
            except (TypeError, ValueError):
                pass
        # 单调性(第 1 层):L2 被 L1 双维支配时整条降级链没意义
        try:
            from quality import validate_levels as _vl
            err = _vl({"recorder": {**self.config.get("recorder", {}), **rec}})
        except Exception:
            err = None
        if err:
            return err
        return None

    def update_config(self, patch: dict):
        """合并更新配置并校验落盘;返回契约 §2 结构。

        暂存目录切换是**重活**(要重开 ffmpeg),一律放到锁外做:若整段都在
        `self.lock` 里,3 秒一次的 /api/status 轮询会被卡住好几秒,前端表现为
        整页发僵。所以这里只记「变更前后」,保存完出了锁再切。
        """
        stage_req = None
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

            # 暂存目录变更:在合并前记下旧值,合并后算是否需要切换
            old_out = str(((self.config.get("recorder") or {}).get("output_dir")) or "")

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
            new_out = str(((self.config.get("recorder") or {}).get("output_dir")) or "")
            if "recorder" in patch:
                stage_req = (old_out, new_out,
                             str(((self.config.get("recorder") or {})
                                  .get("output_dir_switch")) or "next_session"))
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

        # ── 锁外:暂存目录真的变了才切换 ──
        if stage_req and stage_req[1] != stage_req[0]:
            old_out, new_out, policy = stage_req
            sw = None
            try:
                import monitor as mon          # 延迟 import:monitor 顶层 import webui
                sw = mon.set_stage_dir(self, new_out, now=(policy == "now"))
            except Exception as e:             # noqa: BLE001
                sw = {"ok": False, "error": f"切换暂存目录失败:{e}"}
            if sw and not sw.get("ok"):
                # 回滚:配置里绝不留一个建不出来/不可写的暂存路径
                with self.lock:
                    self.config.setdefault("recorder", {})["output_dir"] = old_out
                    self._save()
                return {"ok": False, "error": sw.get("error"),
                        "restart_required": False}
            return {"ok": True, "error": None, "restart_required": restart_required,
                    "stage_switch": sw}
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

    def get_streamers(self):
        """只取主播状态快照(录制看护循环每 20s 用,不必连 config/commands 一起深拷)。"""
        with self.lock:
            return json.loads(json.dumps(self.streamers))

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
                # 画质调度:当前档位 / 引擎占用 / 降级是否生效(纯展示,不参与判定)
                "quality": json.loads(json.dumps(self.quality_status or {})),
            }


def _allowed_roots(cfg):
    """允许打开的根目录白名单:录制暂存根 + 项目元数据目录 + NAS 挂载点 + 外接盘。

    2026-10-30 暂存外迁后本函数要覆盖**两个**录制根:
      · 当前暂存根(recorder.output_dir,空=默认 ~/Movies/DouyinRecordings);
      · 项目内 recordings/(迁移前的老场次仍在这里,不加就会被 403,
        历史页表现为「文件不存在」—— 素材其实还在)。
    仍严格限制在这几处,防止任意路径打开。
    """
    roots = []
    try:
        import monitor as mon                  # 延迟 import:避免与 monitor 循环导入
        roots.append(mon._resolve_output_dir(cfg))
        roots.append(mon.DEFAULT_STAGE_DIR)
    except Exception:                          # noqa: BLE001
        out = (cfg.get("recorder") or {}).get("output_dir", "") or ""
        roots.append(os.path.realpath(os.path.expanduser(out))
                     if out else os.path.join(BASE, "recordings"))
    roots.append(os.path.realpath(os.path.join(BASE, "recordings")))   # 老场次
    nas_cfg = cfg.get("nas") or {}
    if nas_cfg.get("enabled", False):
        mp = nas_cfg.get("mount_point") or "~/DouyinArchive"
        roots.append(os.path.realpath(os.path.expanduser(mp)))
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext:
        roots.append(os.path.realpath(os.path.expanduser(ext)))
    seen, uniq = set(), []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


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
        {"key": "recordings", "name": "元数据目录", "rel": "recordings",
         "desc": "只放 history.json 与字幕备份包(KB 级);录制视频不再写进项目"},
        {"key": "stage", "name": "录制暂存目录", "dynamic": "stage",
         "desc": "flv 成片与 ass 弹幕写在这里,下播后自动归档到 NAS(设置里可改)"},
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
    if kind == "stage":
        # 录制暂存根:唯一真源在 monitor._resolve_output_dir(空=默认根;
        # 落在项目内会被拒绝并回落),这里绝不自算一份,否则两边必然漂移。
        try:
            import monitor as mon
            return os.path.realpath(mon._resolve_output_dir(cfg))
        except Exception:                      # noqa: BLE001
            out = (cfg.get("recorder") or {}).get("output_dir") or ""
            return os.path.realpath(os.path.expanduser(out)) if out else ""
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


# ── 目录列举缓存(2026-10-26)──────────────────────────────────────
# 背景:/api/history 曾对 500 条历史逐条做 6 次 os.path.exists 判存在性,而
# 493 条是归档条目、全在 NAS 上,每条又指向不同日期目录(冷路径)。实测单次
# 请求 43 秒(archived 条目 304ms/条,本机条目仅 11ms/条),而前端 5 秒轮询
# 一次且当时没有并发守卫 —— 请求无限堆积占满浏览器 6 条同源连接,整个 WebUI
# 连「解析」按钮都点不动,服务端却是空闲的。
#
# 做法:同一目录只 scandir 一次拿到文件名集合,后续全是内存查找。实测 491 条
# 归档条目去重后只有 78 个目录(约 6.3 条/目录),2946 次 SMB 往返 → 78 次。
#
# 三态语义(与前端「✗ 缺失 / ? 无法确认」一一对应,不能混):
#   _DIR_OK      目录可读,用 names 判成员
#   _DIR_MISSING 目录确实不存在 → 里面的文件一律 False(前端显示缺失)
#   _DIR_UNKNOWN 探测不了(未挂载/网络异常) → None(前端显示「无法确认」)
# 🔴 绝不能把 MISSING 当 UNKNOWN 返回,否则已删除的归档会被显示成「未挂载」。
_DIR_OK, _DIR_MISSING, _DIR_UNKNOWN = "ok", "missing", "unknown"
_DIR_CACHE = {}          # abspath -> (ts, state, frozenset)
# TTL 2026-10-31 从 60 s 提到 300 s:一轮「预热 + 当页探测」在真冷 SMB 上
# 要十几秒,60 s 的窗口意味着一轮还没跑完、开头扫过的目录就已过期,等于白扫
# (_history_payload 会再扫一遍)。目录存在性是分钟级甚至天级变化的东西。
# 注意 TTL 长短不再影响响应速度 —— _history_payload 走 stale-while-revalidate,
# 过期时先把旧值返回、后台再刷新;这里只决定「数据最多能旧多久」。
# 真要立即失效有 _dir_cache_clear()(归档/删除后调用)。
_DIR_TTL = 300.0
_DIR_CACHE_MAX = 400
_DIR_LOCK = threading.Lock()


# ── 单轮探测预算(2026-10-31)──────────────────────────────────────
# 目录探测的最小单位是一次 SMB scandir。macOS smbfs 是单会话串行的:代码里
# 并发 6 个线程,实测吞吐仍只有约 2 个目录/秒,真冷时 0.5~1 s/目录。
# 500 条历史去重后 204 个目录 ⇒ 单轮 100~200 秒(线上实测 /api/history
# 165 s / 194 s,期间只算不传,TTFB 就是 165 s)。
# 所以每轮给一个硬预算:预算耗尽后不再真打 SMB,一律返回 _DIR_UNKNOWN
# (前端显示「? 无法确认」),下一轮再补。宁可显示「?」,也不能把整页拖到
# 三分钟 —— 那才是用户说的「点一下要转半天」。
_PROBE = threading.local()


class _ProbeBudget:
    """一轮请求内的探测预算:deadline + 是否已耗尽。"""

    def __init__(self, seconds):
        self.deadline = time.time() + max(0.0, float(seconds))
        self.exhausted = False

    def spend(self):
        """还有预算 → True;已耗尽 → 置位并 False(调用方必须短路,不再打 SMB)。"""
        if self.exhausted:
            return False
        if time.time() >= self.deadline:
            self.exhausted = True
            return False
        return True


def _probe_budget(seconds=None):
    """读/装当前线程的探测预算。

    无参调用返回当前预算(_ProbeBudget 或 None);传秒数则新建并挂到当前线程,
    返回**旧值**供调用方在 finally 里恢复(嵌套调用不会互相踩)。
    """
    if seconds is None:
        return getattr(_PROBE, "budget", None)
    old = getattr(_PROBE, "budget", None)
    _PROBE.budget = _ProbeBudget(seconds)
    return old


def _probe_budget_restore(old):
    """恢复 _probe_budget(seconds) 返回的旧值。"""
    _PROBE.budget = old


def _dir_listing(path, ttl=None):
    """列举目录内容,返回 (state, names)。结果按 ttl 秒缓存(默认 _DIR_TTL)。

    服务是每请求一线程(ThreadingHTTPServer),缓存写入必须加锁;scandir 本身
    放在锁外做 —— NAS 慢的时候不能把别的线程一起堵在锁上。
    """
    if not path:
        return _DIR_UNKNOWN, frozenset()
    try:
        key = os.path.abspath(path)
    except (OSError, ValueError):
        return _DIR_UNKNOWN, frozenset()
    ttl = _DIR_TTL if ttl is None else ttl
    now = time.time()
    if ttl > 0:
        hit = _DIR_CACHE.get(key)          # 只读单键:不需锁,写才加锁
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2]
    # 预算耗尽:不再真打 SMB。返回 UNKNOWN(不是 MISSING)—— 语义必须是
    # 「没确认」,否则前端会把还没探测的条目显示成「文件已缺失」。
    budget = getattr(_PROBE, "budget", None)
    if budget is not None and not budget.spend():
        return _DIR_UNKNOWN, frozenset()
    try:
        with os.scandir(key) as it:
            names = frozenset(e.name for e in it)
        state = _DIR_OK
    except (FileNotFoundError, NotADirectoryError):
        state, names = _DIR_MISSING, frozenset()
    except OSError:
        state, names = _DIR_UNKNOWN, frozenset()
    # 🔴 UNKNOWN 不进缓存(2026-10-31):它表示「这次没探到」(NAS 断开、预算
    # 耗尽、瞬时错误),不是目录的真实状态。原来照缓不误,配合 TTL 60 s 会
    # 让一次抖动把该目录锁成「无法确认」整整一分钟;TTL 提到 180 s 后更糟。
    # 不缓存 → 下一轮立刻重试,探到了就恢复真实状态。
    if state != _DIR_UNKNOWN:
        with _DIR_LOCK:
            if len(_DIR_CACHE) >= _DIR_CACHE_MAX:
                live = {k: v for k, v in _DIR_CACHE.items() if now - v[0] < ttl}
                _DIR_CACHE.clear()
                _DIR_CACHE.update(live)
                if len(_DIR_CACHE) >= _DIR_CACHE_MAX:
                    _DIR_CACHE.clear()     # 目录名一直在变:清空最省事
            _DIR_CACHE[key] = (now, state, names)
    return state, names


def _dir_cache_clear():
    """清空目录/条目文件/素材缺失记忆。归档完成、删除历史后调用。

    三个缓存必须一起清:只清目录缓存的话,条目级结论(_history_files 的
    files dict,含 *_exists)还会按 180 s 的 TTL 继续给旧答案,归档完的
    条目仍显示「本机路径」;而「素材确认不存在」的记忆会让刚归档过来的
    素材继续被跳过 7 天。
    """
    with _DIR_LOCK:
        _DIR_CACHE.clear()
    _files_cache_clear()
    _absent_cache_clear()


def _has_entry_file(d, subdir, name):
    """d/subdir/name 是否存在。走 _dir_listing 缓存,避免逐条打 NAS。

    ⚠️ 只回答「确定存在」;缓存拿不到状态(未挂载/异常)时返回 False,与
    _history_files 的 None 语义不同 —— 这里的调用方都只需要「能不能读到」。
    """
    base = os.path.join(d, subdir) if subdir else d
    state, names = _dir_listing(base)
    return state == _DIR_OK and name in names


def _entry_root(entry):
    """一个历史条目当前实际所在的目录(归档后切到归档目标)。

    🔴 唯一真源:_history_files 与 _prefetch_paths 都必须用它推导 root,
    否则预热的目录和实际探测的目录会对不上,缓存等于白做。
    """
    out = entry.get("output_path") or ""
    if not out:
        return None
    arch = entry.get("archive") or {}
    if arch.get("state") == "archived":
        return arch.get("dest") or os.path.dirname(out)
    return os.path.dirname(out)


def _prefetch_dirs(paths, workers=6, budget=None):
    """并发预热目录缓存(纯提速,任何失败都不影响结果)。

    冷启动要列举上百个目录,串行在 SMB 上实测 8~13 秒;并发 6 只在**内核目录
    缓存还热**时是 1~2 秒 —— 真冷(线上常态)时 smbfs 单会话串行,并发几乎
    不加速,实测吞吐约 2 个/秒。所以这里必须接受一个 budget:预算耗尽后
    worker 里的 _dir_listing 立刻返回 UNKNOWN,不再打 SMB,整个预取迅速收尾。
    每个 _dir_listing 自己吞掉 OSError,单个目录失败不会中断预取。
    """
    todo = [p for p in dict.fromkeys(paths) if p]

    def _scan(p):
        # 预算是 thread-local 的:worker 线程必须自己挂上,否则它读不到
        # 主线程的 budget,会绕过上限一路打满 SMB。
        if budget is None:
            _dir_listing(p)
            return
        old = _probe_budget_restore(budget)
        try:
            _dir_listing(p)
        finally:
            _probe_budget_restore(old)

    if len(todo) < 2:
        for p in todo:
            _scan(p)
        return
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_scan, todo))
    except Exception:                    # 线程池都起不来:退回串行,别让页面挂掉
        for p in todo:
            _scan(p)


def _prefetch_paths(entries, cfg):
    """收集本轮要探测的目录(root 与 root/.meta)供预热。

    NAS 未挂载时跳过归档目录 —— 与 _history_files 里 probe 短路同一个考虑:
    挂载点不可达时不做任何 SMB 探测。
    """
    nas_ok = _nas_mounted(cfg)
    out = []
    for e in entries:
        arch = e.get("archive") or {}
        if arch.get("state") == "archived" and not nas_ok:
            continue
        r = _entry_root(e)
        if not r:
            continue
        out.append(r)
        out.append(os.path.join(r, ".meta"))
    return out


def _history_files(entry, cfg):
    """计算一场录制各文件的**当前实际位置**(随归档状态自动切换本机/归档目标)。

    返回:
      video    视频分片模板路径(可能含 %03d)
      ass      字幕(与视频同名同目录)
      meta_dir .meta 目录(弹幕/礼物明细,与视频同目录)
      jsonl    弹幕礼物事件流
      align    时间戳对齐元数据
      location local|archive(供前端显示"存在哪")

    *_exists 三态:True=在 / False=确实没有 / None=无法确认(未挂载等)。
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
    root = _entry_root(entry) or src_dir      # 与 _prefetch_paths 共用同一推导
    ext = os.path.splitext(base)[1] or ".flv"
    # 存在性:归档目标未挂载时不做 stat(避免 SMB 长时间阻塞),标记 None=未知
    probe = (not archived) or _nas_mounted(cfg) or os.path.isdir(root)

    # 一次 scandir 取代原来 6 次 os.path.exists:归档条目全在 NAS 上,每次
    # exists 都是一趟 SMB 往返(见 _dir_listing 顶部说明)。
    if probe:
        root_state, root_names = _dir_listing(root)
    else:
        root_state, root_names = _DIR_UNKNOWN, frozenset()
    # .meta 不在 root 里就不必再多跑一趟;此时 jsonl/align 必然不存在
    if root_state == _DIR_OK and ".meta" in root_names:
        meta_state, meta_names = _dir_listing(os.path.join(root, ".meta"))
    else:
        meta_state, meta_names = root_state, frozenset()

    def _in(name, state, names):
        if state == _DIR_OK:
            return name in names
        return False if state == _DIR_MISSING else None

    def _is_dir(state):
        return True if state == _DIR_OK else (
            False if state == _DIR_MISSING else None)

    video_p = os.path.join(root, base)
    meta_p = os.path.join(root, ".meta")
    ass_name = stem + "-000" + ".ass"
    jsonl_name = stem + ".danmaku.jsonl"
    align_name = stem + ".align.json"
    return {
        "video": video_p, "video_exists": _is_dir(root_state),
        "ass": os.path.join(root, ass_name),
        "ass_exists": _in(ass_name, root_state, root_names),
        "meta_dir": meta_p, "meta_exists": _in(".meta", root_state, root_names),
        "jsonl": os.path.join(root, ".meta", jsonl_name),
        "jsonl_exists": _in(jsonl_name, meta_state, meta_names),
        "align": os.path.join(root, ".meta", align_name),
        "align_exists": _in(align_name, meta_state, meta_names),
        "dir": root, "dir_exists": _is_dir(root_state),
        "location": "archive" if archived else "local",
        "ext": ext,
    }


# ── 条目级 files 缓存(2026-10-31)────────────────────────────────
# _dir_listing 缓存的是「目录里有什么」,这里缓存「一条历史算完的结论」
# (_history_files 的 files dict)。分开的理由:同一页翻走再翻回来、以及
# 5 秒轮询,都不该重算一遍三态判断;而目录缓存失效时这里也要跟着清。
# TTL 与 _DIR_TTL 对齐:归档状态或目标一变 key 就变了(_files_key 含 dest),
# 所以这里只需要管「文件被人在 NAS 上手工删掉」这种低频变化。
_FILES_TTL = 300.0
_FILES_CACHE = {}                     # (output_path, state, dest) -> (ts, files)
_FILES_CACHE_MAX = 1200
_FILES_LOCK = threading.Lock()


def _files_cache_clear():
    with _FILES_LOCK:
        _FILES_CACHE.clear()


def _files_key(entry):
    """条目缓存键。归档状态或目标变了就是另一条结论,必须换 key。"""
    arch = entry.get("archive") or {}
    return (entry.get("output_path") or "", arch.get("state"), arch.get("dest"))


def _history_files_cached(entry, cfg, ttl=None):
    """_history_files 的条目级缓存版本。"""
    ttl = _FILES_TTL if ttl is None else ttl
    key = _files_key(entry)
    now = time.time()
    if ttl > 0:
        hit = _FILES_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    files = _history_files(entry, cfg)
    with _FILES_LOCK:
        if len(_FILES_CACHE) >= _FILES_CACHE_MAX:
            live = {k: v for k, v in _FILES_CACHE.items() if now - v[0] < ttl}
            _FILES_CACHE.clear()
            _FILES_CACHE.update(live)
            if len(_FILES_CACHE) >= _FILES_CACHE_MAX:
                _FILES_CACHE.clear()
        _FILES_CACHE[key] = (now, files)
    return files


# ── /api/history 结果缓存与分页(2026-10-26 建,2026-10-31 重写)──────
# 2026-10-26 的版本是「整份 payload 缓存 5 秒 + 单飞」,但没有解决根本问题:
# 它每轮仍然要对**全部** 500 条历史逐条判文件存在性,而 498 条在 SMB 上,
# 去重后 204 个目录、smbfs 真冷时约 2 目录/秒 ⇒ 单轮 100~200 秒(线上实测
# 165 s / 194 s,TTFB 就是全部,传输只占 0.01 s)。前端 20 秒超时 < 这个数,
# 于是**每次点进历史页都必然超时重来**,用户感受就是「一直转、非常忙」。
#
# 2026-10-31 改成三条:
#   1) 分页:只探测当前页那 20 条要的目录(20~40 个),不再扫全部 204 个;
#   2) stale-while-revalidate:老数据过期后**先返回老数据**,后台线程再刷新,
#      用户点进去立刻有内容,慢的那部分发生在看不见的地方;
#   3) 预算:单轮真实 SMB 探测最多 _HIST_PROBE_BUDGET 秒,超了标「无法确认」。
# 三条合起来把首屏从 194 秒降到「旧值毫秒级 / 冷启动有硬上限」。
_HIST_TTL = 5.0
_HIST_PROBE_BUDGET = 12.0     # 单轮真实 SMB 探测的硬上限(秒)
_HIST_DEFAULT_LIMIT = 20      # 与前端 histPageSize 默认值一致
_HIST_MAX_LIMIT = 200
_HIST_SWR = True              # 老值先返回 + 后台刷新;测试可关
_HIST_CACHE = {}              # (offset, limit) -> {"ts": float, "payload": dict}
_HIST_LOCK = threading.Lock()
_HIST_REFRESHING = set()      # 正在后台刷新的 (offset, limit),单飞


def _hist_cache_clear():
    """历史缓存失效。删除历史后调用,让前端下一轮立刻看到新列表。"""
    with _HIST_LOCK:
        _HIST_CACHE.clear()


def _norm_page(total, offset, limit):
    """把 offset/limit 夹进合法范围,返回 (offset, limit)。

    越界 offset 回退到最后一页 —— 删掉几条后前端还停在旧页码时,不该给空列表。
    """
    limit = _HIST_DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(1, min(_HIST_MAX_LIMIT, limit))
    offset = max(0, int(offset or 0))
    if total and offset >= total:
        offset = ((total - 1) // limit) * limit
    return offset, limit


def _build_history_payload(state, offset, limit):
    """真算一份 payload。只在缓存 miss、或后台刷新时进这里。

    顺序有讲究:先算用户当页要看的 files,再跑惰性补算。反过来的话补算会
    把这一轮的探测预算吃光,当页反而全是「无法确认」。
    """
    old_budget = _probe_budget(_HIST_PROBE_BUDGET)
    try:
        cfg_h = state.get_config()
        history = state.get_history()      # 深拷贝,500 条几十毫秒:只取一次
        total = len(history)
        offset, limit = _norm_page(total, offset, limit)
        ordered = history[::-1]            # 新到旧:与前端原本的排序一致
        page = ordered[offset:offset + limit]
        # 只预热当前页要探测的目录(20 条最多 40 个),不再是全部 204 个
        _prefetch_dirs(_prefetch_paths(page, cfg_h),
                       budget=_probe_budget())
        entries = []
        for e in page:
            item = dict(e)
            item["files"] = _history_files_cached(e, cfg_h)  # 各文件当前位置
            entries.append(item)
        # 惰性补算是「补历史存量」这类后台数据,排在当页之后、吃剩余预算;
        # 预算耗尽它们自己会快速返回(见 _backfill_* 里的 spend 判定),
        # 没补到的下一轮继续,不影响本页返回。
        _backfill_history_stats(state)     # 旧条目缺容量/段数
        _backfill_gift_fields(state)       # 旧条目缺礼物可见度/诊断计数
        _backfill_media_info(state)        # 旧条目缺实际分辨率/码率
        return {"entries": entries, "total": total,
                "offset": offset, "limit": limit,
                "nas_mounted": _nas_mounted(cfg_h),
                "archive_roots": _allowed_roots(cfg_h)}
    finally:
        _probe_budget_restore(old_budget)


def _refresh_hist_sync(state, key, offset, limit):
    """真算并写缓存。同步路径与后台刷新都走这里 ⇒ 天然单飞(共用 _HIST_LOCK)。"""
    with _HIST_LOCK:
        # 等锁期间别的线程可能已经算好,再判一次,避免排队后重复计算
        hit = _HIST_CACHE.get(key)
        if hit is not None and time.time() - hit["ts"] < _HIST_TTL:
            return hit["payload"]
        payload = _build_history_payload(state, offset, limit)
        _HIST_CACHE[key] = {"ts": time.time(), "payload": payload}
        return payload


def _spawn_hist_refresh(state, key, offset, limit):
    """老值已过期:起后台线程重算,当前请求立刻返回老值(stale-while-revalidate)。

    同一 (offset, limit) 同时只允许一个刷新线程,否则每 5 秒一个新线程、
    SMB 反而更挤 —— 本来就是单会话串行,并发只会互相拖。
    """
    with _HIST_LOCK:
        if key in _HIST_REFRESHING:
            return
        _HIST_REFRESHING.add(key)

    def _run():
        try:
            _refresh_hist_sync(state, key, offset, limit)
        except Exception as exc:           # 后台失败不影响任何已返回的响应
            print(f"[history] 后台刷新失败: {exc}", flush=True)
        finally:
            with _HIST_LOCK:
                _HIST_REFRESHING.discard(key)

    try:
        threading.Thread(target=_run, name="hist-refresh", daemon=True).start()
    except Exception as exc:
        with _HIST_LOCK:
            _HIST_REFRESHING.discard(key)
        print(f"[history] 后台刷新线程起不来: {exc}", flush=True)


def _history_payload(state, offset=0, limit=None):
    """/api/history 的响应体(分页 + 条目缓存 + stale-while-revalidate)。

    返回 entries 只是**当前页**(新到旧),另带 total/offset/limit 供前端
    算页码。调用方只读并 json.dumps,所以多处复用同一个 dict 是安全的。
    """
    limit = _HIST_DEFAULT_LIMIT if limit is None else max(
        1, min(_HIST_MAX_LIMIT, int(limit)))
    offset = max(0, int(offset or 0))
    key = (offset, limit)
    hit = _HIST_CACHE.get(key)
    if hit is not None:
        if time.time() - hit["ts"] < _HIST_TTL:
            return hit["payload"]
        if _HIST_SWR and _HIST_TTL > 0:
            _spawn_hist_refresh(state, key, offset, limit)
            return hit["payload"]          # 老值照给:用户先看到内容
    return _refresh_hist_sync(state, key, offset, limit)


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


def _backfill_history_stats(state):
    """给旧代码收尾的 done 条目惰性补算 size / segments(一次性迁移)。

    2026-09-21 之前 _history_finish 不写 size/segments(_media_duration 对分片
    模板返回 None),历史页这些条目「无容量、无段数」。素材还在(本机或归档
    目标)就能补算;补过的条目带 size 字段,不会重复算。
    """
    mon = _monitor_live()
    if mon is None or not hasattr(mon, "_segment_stats"):
        return False
    changed = False
    try:
        for e in mon._history:
            if e.get("status") != "done" or e.get("size") is not None:
                continue
            tmpl = e.get("output_path") or ""
            if not tmpl:
                continue
            base = os.path.basename(tmpl)
            candidates = []
            dest = (e.get("archive") or {}).get("dest")
            # 走 _dir_listing 缓存:这条在 NAS 上,原样 isdir 每条都是一趟 SMB
            if dest and _dir_listing(dest)[0] == _DIR_OK:
                candidates.append(dest)          # 素材大概率已在归档目标
            local_dir = os.path.dirname(tmpl)
            if local_dir not in candidates:
                candidates.append(local_dir)
            for d in candidates:
                seg = mon._segment_stats(os.path.join(d, base))
                if seg and seg["count"]:
                    e["segments"] = seg["count"]
                    e["size"] = seg["bytes"]
                    changed = True
                    break
    except Exception as exc:                      # 补全失败不影响历史页正常返回
        print(f"[history] 容量/段数补全失败: {exc}", flush=True)
    if changed:
        commit = getattr(mon, "_commit_history", None)
        if commit:
            commit()
        state.set_history(json.loads(json.dumps(mon._history)))
    return changed


# 惰性补算礼物字段的每调用上限:本机 jsonl 读取快(实测几十~几百 KB/场),
# 归档目标(SMB)读慢,分开限额 —— 防止一次 /api/history 请求被补算拖死。
_GIFT_LAZY_LOCAL_CAP = 10
_GIFT_LAZY_REMOTE_CAP = 2

# ── 「本场素材确认不存在」的进程内记忆(2026-10-31)──────────────────
# 没有它时补不上的存量条目**每一轮都会重新探测**:实测 519 条待补
# (437 缺 gift_diag + 82 缺 light)对应的 .jsonl 全部不存在,而额度只在
# 补成功时才推进,于是永远卡在最新的那几条上打转,一轮又一轮地白烧 SMB。
#
# 🔴 只记在内存,不写进 history.json:一是那些是「探测结论」不是录制事实,
# 落盘会污染历史契约、还会让纯查询接口产生写盘副作用(见
# tests/test_gift_integrity.py::test_skips_when_no_meta_files 与
# tests/test_history_media.py::test_backfill_missing_files_is_noop 守的语义);
# 二是重启后重探一轮的代价很小 —— 每轮远端额度只有 2 条,价格可忽略。
_ABSENT_RETRY = 7 * 86400.0        # 确认不存在后,7 天内不再探
_ABSENT_PROBE = {}                 # (output_path, kind) -> 确认时间
_ABSENT_LOCK = threading.Lock()
_ABSENT_MAX = 4000


def _absent_cache_clear():
    with _ABSENT_LOCK:
        _ABSENT_PROBE.clear()


def _absent_skip(entry, kind):
    """本场该 kind 的素材是否已被确认不存在(近期)。"""
    key = ((entry.get("output_path") or ""), kind)
    with _ABSENT_LOCK:
        ts = _ABSENT_PROBE.get(key)
    return bool(ts and time.time() - ts < _ABSENT_RETRY)


def _absent_mark(entry, kind):
    """记住「本场该 kind 的素材确实不存在」,让接下来 7 天别再探。"""
    key = ((entry.get("output_path") or ""), kind)
    with _ABSENT_LOCK:
        if len(_ABSENT_PROBE) >= _ABSENT_MAX:
            # 简单剪枝:清掉最早确认的那批(都是老场次,重探也不亏)
            for k in sorted(_ABSENT_PROBE, key=_ABSENT_PROBE.get)[: _ABSENT_MAX // 2]:
                _ABSENT_PROBE.pop(k, None)
        _ABSENT_PROBE[key] = time.time()


def _backfill_gift_fields(state):
    """为缺 gift_integrity / gift_diag 的 done 条目增量补算(2026-10-16)。

    背景:礼物可见度胶囊与诊断警示都依赖收尾写入的字段,而补算脚本
    (scripts/backfill_gift_integrity.py --apply)必须停服才能跑 —— 用户重启
    服务后存量条目全部无数据,功能「看起来没实现」。把补算挂到历史页:
    每次 /api/history 调用补一小批,多刷新几次就全部补齐,无需停服。

    规则:
      - 只处理 status=done 且缺字段的条目,新到旧优先(用户先看到近期场次);
      - gift_integrity 需要本场 jsonl + duration;gift_diag 只需要
        giftwatch.jsonl,两者独立补、互不阻塞;
      - 本机与归档目标都找(与 backfill 脚本同一套候选规则),但分限额;
      - 文件确实不存在的条目每次请求做几次 exists() 探测,代价可忽略。
    """
    mon = _monitor_live()
    if mon is None or not hasattr(mon, "_history"):
        return False
    budget = _probe_budget()
    changed = False
    local_used = remote_used = 0
    try:
        for e in reversed(mon._history):          # 新到旧:用户先看到近期场次
            if e.get("status") != "done":
                continue
            # 预算耗尽就收工:剩下的留给下一轮。绝不能像原来那样硬跑完 519 条,
            # 那会把同一轮里当页要看的 files 探测也一起挤掉。
            if budget is not None and not budget.spend():
                break
            # 2026-10-29 结构升级:礼物完整性的主判据从「时间占比」换成
            # gift_integrity.light(轻通道收到 vs 落盘 vs 字段缺项)。老条目只有
            # 时间占比、没有对账,这里顺带重算一次把它们补齐(补算拿不到实时
            # 计数 → recv=null、status=field_only,如实标注"未对账",不谎报完整)。
            gi_old = e.get("gift_integrity")
            need_gi = gi_old is None or (isinstance(gi_old, dict) and "light" not in gi_old)
            need_gd = e.get("gift_diag") is None
            if not (need_gi or need_gd):
                continue
            tmpl = e.get("output_path") or ""
            if not tmpl:
                continue
            # 已确认「本场素材真的没有」的条目:近期不再探测(进程内记忆,见 _absent_skip)
            if _absent_skip(e, "gift"):
                continue
            stem = os.path.basename(tmpl)
            if "-%03d." in stem:
                stem = stem.split("-%03d.")[0]
            else:
                s, _ = os.path.splitext(stem)
                if len(s) > 3 and s[-4] == "-" and s[-3:].isdigit():
                    stem = s[:-4]
            dirs = []
            dest = (e.get("archive") or {}).get("dest")
            # ⚠️ 这条在限额检查**之前**,所以缺字段的条目每轮都会走到这里。
            # 原来裸 isdir(dest) 在 NAS 上是每轮 456 趟 SMB(实测 ~3.4s),
            # 走 _dir_listing 缓存后变成内存查找(去重后只有几十个目录)。
            if dest and _dir_listing(dest)[0] == _DIR_OK:
                dirs.append(dest)                 # 归档目标优先(本机多半已删)
            local_dir = os.path.dirname(tmpl)
            if local_dir not in dirs:
                dirs.append(local_dir)
            is_remote = bool(dest and dirs and dirs[0] == dest)
            if is_remote and remote_used >= _GIFT_LAZY_REMOTE_CAP:
                continue
            if not is_remote and local_used >= _GIFT_LAZY_LOCAL_CAP:
                continue
            # 额度**尝试即计数**(2026-10-31):原来只在补成功时 +1,而补不上的
            # 条目(素材不存在)永远不计数 ⇒ 每轮把 519 条全跑一遍,额度形同虚设。
            if is_remote:
                remote_used += 1
            else:
                local_used += 1

            dur = float(e.get("duration") or 0)
            got = False
            if need_gi and dur > 0:
                for d in dirs:
                    jp = os.path.join(d, ".meta", stem + ".danmaku.jsonl")
                    if not _has_entry_file(d, ".meta", stem + ".danmaku.jsonl"):
                        continue
                    try:
                        gi = danmaku_mod.gift_channel_coverage(
                            jp, dur, started_at=e.get("started_at"))
                    except Exception:
                        gi = None
                    if gi is not None:
                        e["gift_integrity"] = gi
                        got = True
                    break
            if need_gd:
                for d in dirs:
                    gp = os.path.join(d, ".meta", stem + ".giftwatch.jsonl")
                    if not _has_entry_file(d, ".meta", stem + ".giftwatch.jsonl"):
                        continue
                    try:
                        counts = {"fault": 0, "suspect": 0, "muted": 0, "quiet": 0}
                        total = 0
                        with open(gp, encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    r = json.loads(line)
                                except Exception:
                                    continue
                                lv = str(r.get("level") or "")
                                counts[lv] = counts.get(lv, 0) + 1
                                total += 1
                        counts["total"] = total
                    except OSError:
                        counts = None
                    if counts is not None:
                        e["gift_diag"] = counts
                        got = True
                    break
            if got:
                changed = True
            else:
                # 什么都没补到:如果所有候选目录的 .meta 都**确认过**(OK 或确实
                # 不存在),说明本场素材真的没有,记进内存让接下来 7 天别再探。
                # 只认 UNKNOWN 之外的状态 —— 预算耗尽/NAS 未挂载时是 UNKNOWN,
                # 那是「这次没看成」,必须下一轮再试,不能误判成「没有」。
                meta_states = [_dir_listing(os.path.join(d, ".meta"))[0]
                               for d in dirs]
                if meta_states and all(st != _DIR_UNKNOWN for st in meta_states):
                    _absent_mark(e, "gift")
    except Exception as exc:                      # 补全失败不影响历史页正常返回
        print(f"[history] 礼物字段补全失败: {exc}", flush=True)
    if changed:
        commit = getattr(mon, "_commit_history", None)
        if commit:
            commit()
        if state is not None:
            state.set_history(json.loads(json.dumps(mon._history)))
    return changed


# 画质/码率补算的每调用上限。与礼物补算同样的分工:本机读得快、SMB 上每一条
# 都是一趟真网络往返,额度必须分开,否则 500 条存量会把历史页拖成 2026-10-26
# 那样的 43 秒请求。
_MEDIA_LAZY_LOCAL_CAP = 20
_MEDIA_LAZY_REMOTE_CAP = 16


def _backfill_media_info(state):
    """给缺 media 的 done 条目惰性补算「实际分辨率 + 实测码率」(2026-10-28)。

    新场次在 monitor._history_finish 收尾时就写好了(那时文件还在本机),
    存量条目(上线前的 500 条)只能事后补:文件名、体积、时长都在 history.json
    里,只缺分辨率 —— 读一次文件头就知道,不必解码、不必 ffprobe。

    规则与礼物补算一致:新到旧、本机/归档目标都找、分限额、失败不影响返回。
    补不到的条目**不写空值**,下一轮还会试(NAS 未挂载是暂时的)。
    """
    mon = _monitor_live()
    if mon is None or not hasattr(mon, "_history"):
        return False
    media_info = getattr(mon, "_media_info", None)
    seg_stats = getattr(mon, "_segment_stats", None)
    if media_info is None:
        return False
    changed = False
    local_used = remote_used = 0
    budget = _probe_budget()
    try:
        for e in reversed(mon._history):          # 新到旧:用户先看到近期场次
            if e.get("status") != "done" or e.get("media"):
                continue
            if budget is not None and not budget.spend():
                break                             # 预算用完:余下的下一轮再补
            tmpl = e.get("output_path") or ""
            if not tmpl:
                continue
            # 素材确认不存在时与礼物补算同样处理:近期别再探(进程内记忆)
            if _absent_skip(e, "media"):
                continue
            base = os.path.basename(tmpl)
            dirs = []
            dest = (e.get("archive") or {}).get("dest")
            # 走 _dir_listing 缓存:直接 isdir 的话每轮就是几百趟 SMB
            if dest and _dir_listing(dest)[0] == _DIR_OK:
                dirs.append(dest)                 # 归档目标优先(本机多半已删)
            local_dir = os.path.dirname(tmpl)
            if local_dir not in dirs:
                dirs.append(local_dir)
            is_remote = bool(dest and dirs and dirs[0] == dest)
            if is_remote and remote_used >= _MEDIA_LAZY_REMOTE_CAP:
                continue
            if not is_remote and local_used >= _MEDIA_LAZY_LOCAL_CAP:
                continue
            if is_remote:
                remote_used += 1
            else:
                local_used += 1
            mi = None
            for d in dirs:
                cand = os.path.join(d, base)
                seg = None if (is_remote or seg_stats is None) else seg_stats(cand)
                # 远端不探末片:多一次 SMB open 只换来「中途降级」这一条信息,
                # 而这批存量场次录制时还没有自动降级功能
                mi = media_info(cand, e.get("size"), e.get("duration"),
                                seg=seg, tail=not is_remote)
                if mi:
                    break
            if mi and mi.get("w"):       # 只认拿到分辨率的结果:只写码率会把条目
                e["media"] = mi           # 锁死(media 非空即视为已补),而归档目标
                changed = True            # 未挂载时恰恰只算得出码率
            else:
                dir_states = [_dir_listing(d)[0] for d in dirs]
                if dir_states and all(st != _DIR_UNKNOWN for st in dir_states):
                    _absent_mark(e, "media")   # 目录都确认过仍读不到:近期别再来
    except Exception as exc:                      # 补全失败不影响历史页正常返回
        print(f"[history] 画质/码率补全失败: {exc}", flush=True)
    if changed:
        commit = getattr(mon, "_commit_history", None)
        if commit:
            commit()
        if state is not None:
            state.set_history(json.loads(json.dumps(mon._history)))
    return changed


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
                # 录制期限快照:唯一真源 monitor.record_state(纯派生、零写盘),
                # 前端只渲染 state/days_left,不自算 —— 两份公式必然漂移。
                st["record_windows"] = _record_windows(state.get_config())
                # 缺号补号巡检状态:前端据此在卡片/详情上显示「巡检中·上次/下次」
                # (没有这段,「缺web_rid」徽标就只是个静态标记,看不出后台在不在试)
                st["rid_backfill"] = _rid_backfill_status(state.get_config())
                # 进程自报「我跑的是哪份代码」。~/Downloads 下可能并存历史发布
                # 快照,谁先起来谁占住端口(2026-10-29 就在快照目录里跑起过一份
                # 入口新、页面旧的实例,整站界面回退三个月,而外层只看到 HTTP 200
                # 便以为"服务正常")。启动器据此比对,发现不是本目录就地说明。
                st["project_root"] = BASE
                # 录制暂存现状(2026-10-30):前端卡片的落盘地址、设置页的
                # 「生效中 / 待生效」提示都读这里,**不在前端各算一份** ——
                # 暂存根可切换后,`config.recorder.output_dir` 已经不等于
                # 「正在写哪儿」(空串时是默认根,在录时可能还是旧根)。
                try:
                    import monitor as mon
                    st["storage"] = mon.stage_switch_snapshot(state.get_config())
                except Exception as e:         # noqa: BLE001
                    st["storage"] = {"error": str(e)}
                # 崩溃告警摘要:顺手塞进这个 3 秒轮询的状态接口 —— 事件一发生,
                # 导航条 3 秒内就能变红。走 /api/incidents 要现算 health(10s 缓存),
                # 不适合做第一响应;那个接口只在展开详情时才拉。
                try:
                    st["incidents"] = incidents.summary()
                except Exception:
                    st["incidents"] = None
                self._send(200, st)
            elif path == "/api/contract":
                # 跨项目契约快照(仅本机自用;跨机分发走 NAS _对接/contract/,
                # 两机零 HTTP —— 见 interop.py 模块注释)
                try:
                    self._send(200, interop_mod.contract_payload())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path == "/api/interop":
                # 对接通道状态(「项目对接」页):NAS 信箱四通道只读探测
                # (interop.mailbox_status,内置 30s 缓存) + 消费协程心跳 +
                # 在录场次 session.json 落点检查。全旁路:探测挂了返回原因,
                # 不带挂 UI。
                data = interop_mod.mailbox_status(state.get_config())
                try:
                    beat_ts = health_mod.last_beat("issue_feedback")
                    data["feedback_loop"] = {
                        "beat_age": (round(time.time() - beat_ts, 1)
                                     if beat_ts else None),
                        "grace": 1500,   # 与 health.SPECS 同值,前端判定用
                    }
                except Exception:
                    data["feedback_loop"] = None
                # 在录场次:session.json 落在本机日期目录(起录写入,归档时
                # 随目录进 NAS 并转 finalized)。output_path 是 %03d 模板,
                # dirname 即日期目录。
                try:
                    st = state.get_status()
                    sessions = []
                    for name, rec in (st.get("recordings") or {}).items():
                        if not ((rec or {}).get("recording") is True
                                or str((rec or {}).get("status")) == "recording"):
                            continue
                        date_dir = os.path.dirname(str((rec or {}).get("output_path") or ""))
                        sess = {"name": name, "date_dir": date_dir,
                                "session_state": None, "prefixes": 0,
                                "contract_version": None}
                        try:
                            with open(os.path.join(date_dir, interop_mod.SESSION_STATE_NAME),
                                      encoding="utf-8") as f:
                                doc = json.load(f)
                            sess.update(session_state=doc.get("state"),
                                        prefixes=len(doc.get("prefixes") or []),
                                        contract_version=doc.get("contract_version"))
                        except Exception:
                            pass
                        sessions.append(sess)
                    data["recording_sessions"] = sessions
                except Exception:
                    data["recording_sessions"] = []
                self._send(200, data)
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
            elif path == "/api/giftwatch":
                # 礼物诊断记录。带 ?path=<录制输出路径> → 只返回该场次的记录
                # (录制历史行展开区用);不带 → 全量倒序(兼容旧消费方)。
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                if q.get("path"):
                    try:
                        self._send(200, _giftwatch_session_records(
                            state.get_config(), (q.get("path") or [""])[0]))
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                else:
                    try:
                        self._send(200, _giftwatch_records(state.get_config()))
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
            elif path == "/api/nas":
                cfg = state.get_config().get("nas") or {}
                masked = {**cfg, "password": ""}  # 脱敏:密码字段恒为空串
                self._send(200, {"config": masked,
                                 "archive": state.get_config().get("archive") or {},
                                 **state.get_nas_status()})
            elif path == "/api/history":
                # 分页 + 只探当前页 + 老值先返回,见 _history_payload 的说明。
                # ?offset=&limit= 缺省为第一页(20 条),与前端 histPageSize 对齐。
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                try:
                    _off = max(0, int((q.get("offset") or ["0"])[0]))
                except (TypeError, ValueError):
                    _off = 0
                try:
                    _lim = int((q.get("limit") or [str(_HIST_DEFAULT_LIMIT)])[0])
                except (TypeError, ValueError):
                    _lim = _HIST_DEFAULT_LIMIT
                self._send(200, _history_payload(state, _off, _lim))
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
            elif path == "/api/incidents":
                # 崩溃/故障事件流(左侧导航告警条)。与 health 的分工:
                #   incidents = 历史事件(崩过几次、崩在哪,秒级恢复的也留痕)
                #   health    = 此刻状态(19 个组件的当前判定)
                # 两个都要:2026-10-29 那 86 次录制崩溃是「同秒重启」的,
                # health 全程 ok,只有事件流能看见。
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                try:
                    limit = max(1, min(200, int((q.get("limit") or ["50"])[0])))
                except (TypeError, ValueError):
                    limit = 50
                try:
                    payload = incidents.snapshot(limit=limit)
                    payload["health"] = _incident_health_faults(state)
                    self._send(200, payload)
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
            elif path == "/live" or path.startswith("/live/"):
                # 实时预览弹出页:静态单页,数据经 /api/live/<主播> 与弹幕增量接口
                try:
                    with open(os.path.join(BASE, "static", "live.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8", no_store=True)
                except OSError:
                    self._send(500, "live.html 缺失")
            elif path.startswith("/live-hls/"):
                # 实时预览分片:/live-hls/<主播>/<文件>(realpath 防穿越)。
                # 均为即时文件,一律 no-store:ffmpeg 重启会重置分片序号,
                # 缓存旧分片会导致播放器拼出坏画面。
                rest = path[len("/live-hls/"):]
                parts = rest.split("/", 1)
                if len(parts) == 2:
                    name = unquote(parts[0]); fname = unquote(parts[1])
                    cfg = state.get_config()
                    target = live_hls_mod.safe_join(cfg, name, fname)
                    if target and os.path.isfile(target):
                        ctype = "application/octet-stream"
                        if fname.lower().endswith(".m3u8"):
                            ctype = "application/vnd.apple.mpegurl"
                        elif fname.lower().endswith(".ts"):
                            ctype = "video/mp2t"
                        try:
                            with open(target, "rb") as f:
                                self._send(200, f.read(), ctype, no_store=True)
                        except OSError:
                            self._send(500, {"error": "读取失败"})
                        return
                self._send(404, {"error": "not found"})
            elif path.startswith("/vendor/"):
                # 本地 vendored 第三方库(hls.js 等,无 CDN 依赖)
                fname = unquote(path[len("/vendor/"):])
                root = os.path.realpath(os.path.join(BASE, "static", "vendor"))
                target = os.path.realpath(os.path.join(root, fname))
                if fname and "/" not in fname and "\\" not in fname \
                        and target.startswith(root + os.sep) and os.path.isfile(target):
                    ctype = ("application/javascript; charset=utf-8"
                             if fname.lower().endswith(".js") else "application/octet-stream")
                    try:
                        with open(target, "rb") as f:
                            self._send(200, f.read(), ctype)
                    except OSError:
                        self._send(500, {"error": "读取失败"})
                else:
                    self._send(404, {"error": "not found"})
            elif path.startswith("/api/live/"):
                name = unquote(path[len("/api/live/"):])
                self._send(200, _live_state(state, name))
            elif path.startswith("/api/danmaku/live/"):
                # 实时预览弹幕增量:?after=<seq> 断点续传
                from urllib.parse import parse_qs, urlparse
                name = unquote(path[len("/api/danmaku/live/"):])
                q = parse_qs(urlparse(self.path).query)
                try:
                    after = int((q.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                try:
                    import danmaku as danmaku_mod
                    self._send(200, danmaku_mod.live_feed(name, after))
                except Exception as e:
                    self._send(200, {"active": False, "seq": 0, "items": [],
                                     "error": str(e)[:120]})
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
            elif path == "/api/incidents/dismiss":
                # 标记已读。只影响导航条上的「未读」计数,条目本身保留 ——
                # 崩溃记录的价值在于事后回看,不能因为点过一次就消失。
                ids = body.get("ids")
                n = incidents.dismiss(ids if isinstance(ids, list) else None)
                self._send(200, {"ok": True, "dismissed": n,
                                 "summary": incidents.summary()})
            elif path == "/api/quality/unlock":
                # 抖动锁定(24 h 内切换 >6 次)只能人工解除:机器确实扛不住时,
                # 自动恢复只会把它反复拉回过载,所以刻意不留自动解锁的路径。
                ctrl = state.get_quality_ctrl() or {}
                fn = ctrl.get("unlock")
                if not fn:
                    self._send(200, {"ok": False, "error": "画质调度未启动"})
                else:
                    try:
                        self._send(200, {"ok": True, "quality": fn()})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
            elif path == "/api/nas":
                res = _save_nas(state, body)
                self._send(200 if res["ok"] else 400, res)
            elif path == "/api/nas/test":
                self._send(200, nas_mod.test_connection(body))
            elif path == "/api/gift-watch/test":
                # 用「表单里还没保存的值」直接测,用户不必先保存再试
                gw = dict(state.get_config().get("gift_watch") or {})
                if isinstance(body.get("gift_watch"), dict):
                    gw.update(body["gift_watch"])
                ch = notify_mod.resolve_channels(gw)
                where = "群机器人" if ch["url"] else "连接器(发给你本人)"
                r = asyncio.run(notify_mod.push_by_config(
                    gw, "礼物告警 · 测试",
                    "这是一条测试通知,收到即代表「录制中 45 分钟无礼物 → 企微告警」"
                    f"的通路可用。当前通道:{where}。"))
                self._send(200, {"ok": r["ok"], "channels": r["channels"], "error": r["error"]})
            elif path == "/api/nas/shares":
                ok, shares, err = nas_mod.list_shares(
                    body.get("host", ""), body.get("username", ""),
                    body.get("password", "") or nas_mod.get_password())
                self._send(200, {"ok": ok, "shares": shares, "error": err})
            elif path == "/api/recorder/stage-dir":
                # 切换录制暂存目录(设置页「默认录制文件夹暂存目录」)。
                # body: {dir: "~/Movies/DouyinRecordings" | "", now: true|false}
                # 走这条而不是让调用方自己拼 /api/config:它是唯一带「在录时怎么办」
                # 语义的入口(now=true 会真的重启在录的 ffmpeg),不该藏在通用保存里。
                # 内部复用 update_config ⇒ 校验、落盘、回滚、切换只有一条实现。
                try:
                    new_dir = str(body.get("dir") or "")
                    res = state.update_config({"recorder": {
                        "output_dir": new_dir,
                        "output_dir_switch": "now" if body.get("now") else "next_session",
                    }})
                except Exception as e:          # noqa: BLE001
                    res = {"ok": False, "error": str(e)}
                self._send(200, res)
            elif path == "/api/pick-dir":
                # 弹 macOS 原生目录选择框。会阻塞到用户点完(默认 180s 上限),
                # 但服务是 ThreadingHTTPServer(每请求一线程),不会拖住别的请求。
                path_, err = nas_mod.pick_directory(
                    body.get("prompt") or "选择外接硬盘上的归档目录",
                    timeout=int(body.get("timeout") or nas_mod.PICK_TIMEOUT))
                if err:
                    self._send(200, {"ok": False, "error": err})
                else:
                    self._send(200, {"ok": True, "path": path_,
                                     "writable": nas_mod._probe_writable(path_)})
            elif path == "/api/archive/backfill":
                # 手动把「未归档但本机还在」的场次补进队列(插上盘后想立刻搬时用)
                try:
                    n = nas_mod.backfill_from_history(state.get_config())
                    self._send(200, {"ok": True, "added": n,
                                     "queue": nas_mod.queue_status()})
                except Exception as e:
                    self._send(200, {"ok": False, "error": str(e)})
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
            elif path.startswith("/api/live/") and path.endswith("/start"):
                # 实时预览(按需独立拉流):打开预览页时启动/续期预览会话
                name = unquote(path[len("/api/live/"):-len("/start")])
                res = _live_start(state, name)
                self._send(200 if res.get("ok") else 400, res)
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
                res = _delete_history(state, body.get("keys"), body.get("clear", False))
                if res.get("ok"):
                    _hist_cache_clear()   # 否则前端最多 5 秒后才看到被删的记录
                self._send(200, res)
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
            elif path == "/api/auth/open-home":
                self._send(200, _auth_open_home(state))
            elif path == "/api/auth/restart-browser":
                self._send(200, _auth_restart_browser(state))
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


_INCIDENT_HEALTH = {"at": 0.0, "data": None}


def _incident_health_faults(state):
    """health 里当前判为 down/warn 的组件 —— 并入告警条的「当前故障」区。

    为什么并入:事件流只记**显式接入**的崩溃点(录制/弹幕/主进程)。协程卡死、
    Cookie 失效、NAS 掉线、硬编不可用这些没有"事件",但 health 已经全都有 ——
    把它的 down/warn 并进来,才能做到「项目哪里出问题都在同一个地方看见」。

    10 秒缓存:`_health_overrides` 会跑 launchctl 与 NAS 探测,导航条轮询比健康
    面板更频繁,不缓存等于把一次翻页放大成十几次系统调用。
    """
    now = time.time()
    c = _INCIDENT_HEALTH
    if c["data"] is not None and (now - c["at"]) < 10:
        return c["data"]
    try:
        snap = health_mod.snapshot(state.get_config(), _health_overrides(state))
    except Exception as e:                       # noqa: BLE001
        return {"items": [], "counts": {}, "ok": True, "error": str(e)}
    items = []
    for g in snap.get("groups") or []:
        for it in g.get("items") or []:
            if it.get("status") not in ("down", "warn"):
                continue
            items.append({
                "key": it.get("key"), "name": it.get("name"),
                "status": it.get("status"), "detail": it.get("detail"),
                "group": g.get("name"), "count": it.get("count"),
                "age_s": it.get("age_s"),
            })
    # down 排前面
    items.sort(key=lambda x: (0 if x["status"] == "down" else 1, x["name"] or ""))
    data = {"items": items, "counts": snap.get("counts") or {},
            "ok": bool(snap.get("ok")), "generated_at": snap.get("generated_at")}
    c["at"], c["data"] = now, data
    return data


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

    # 硬件编码器:默认走 vt,探不到就会整体回落 copy(体积变大但不丢素材),
    # 所以这里必须显式报出来,不能让它静默发生。
    try:
        from preflight import probe_hw_encoder
        hw_ok, hw_why = probe_hw_encoder(ff)
    except Exception as e:
        hw_ok, hw_why = True, "探针异常(按可用处理): %s" % e
    mode = str((cfg.get("recorder") or {}).get("video_mode") or "").lower()
    if hw_ok:
        over["vt_encoder"] = {"status": "ok", "last_beat": now,
                              "detail": "h264_videotoolbox 可用"
                                        + ("(当前生效)" if mode == "vt" else
                                           "(当前配置为 %s,未使用)" % (mode or "sw"))}
    else:
        over["vt_encoder"] = {"status": "warn" if mode == "copy" else "down",
                              "last_beat": now,
                              "detail": "硬件编码不可用:%s(录制将回落流复制)"
                                        % hw_why}
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
    若把「先清外来户再 kickstart」「已装载就直接拉起、未装载才 bootstrap」
    这些次序规则各写一遍,两边迟早会悄悄分叉(一边修了、另一边还留着老 Bug)。
    所以规则只在本函数里写一次,两个入口都从这里取。

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
        # 「启动」必须能安全地对着一个**正在录制**的服务敲(2026-10-29 修)
        # ------------------------------------------------------------------
        # 这里曾经是无条件的 bootout → bootstrap,理由是「job 已装载时裸
        # bootstrap 会报 already loaded」。那条理由只在「已装载但进程没跑」时
        # 成立,却被套用到了所有状态:bootout 会给正在跑的进程发 SIGTERM,
        # 于是「启动」实际上变成了「先掐掉再拉起」。
        # 2026-10-29 实测代价:一条 douyin start 让 2 路录制被中断;更糟的是
        # bootout 记下的退出时刻会打开 launchd 的 ThrottleInterval(60 秒)
        # 节流窗口,新实例要等满一分钟才 spawn,用户看到的是「重启失败」。
        #   · 已装载 → kickstart 就够(不带 -k,进程在跑时是空操作)
        #   · 未装载 → bootstrap 装载即可,KeepAlive 会立即把进程拉起来
        #   · 端口被外人占着 → 不猜也不动手:那种情况只有 restart 有收尾流程
        outsider = _outsider_pid()
        if outsider:
            return (False,
                    "端口 %d 被非 launchd 实例(pid %d)占着,kickstart 拿不到它 —— "
                    "请用「重启」,那边会先让它停录落历史再交回 launchd"
                    % (_webui_port(), outsider), [])
        if _service_status().get("managed"):
            return True, "拉起指令已发出(launchd 名下已装载,直接拉起)", [
                {"kind": "kickstart", "label": "拉起服务",
                 "cmd": "launchctl kickstart %s 2>/dev/null" % q(target)},
            ]
        return True, "启动指令已发出,服务将在数秒内就绪", [
            {"kind": "bootstrap", "label": "按 plist 装载 launchd job",
             "cmd": "launchctl bootstrap %s %s" % (q(domain), q(LAUNCHD_PLIST))},
            {"kind": "kickstart", "label": "拉起服务",
             "cmd": "launchctl kickstart %s 2>/dev/null" % q(target)},
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


def _auth_open_home(state):
    """一键打开已登录抖音主页(复用扫码登录的专用 profile 与 CDP 注入)。"""
    try:
        import auth as auth_mod
        return auth_mod.open_home(log=_log_line)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _auth_restart_browser(state):
    """显式关闭专用浏览器(下次打开按新的启动参数重建)。

    「打开我的抖音」永不杀进程之后,这是唯一会让专用浏览器消失的入口 ——
    新启动参数生效、或调试端口读不到需要恢复时用它。扫码登录中会被拒绝。
    """
    try:
        import auth as auth_mod
        return auth_mod.restart_browser(log=_log_line)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _live_state(state, name):
    """实时预览状态(按需独立拉流架构)。

    供 /live/<主播> 弹出页轮询;对已存在的会话续期(keepalive) —— 页面关闭
    即停止轮询,会话在 idle_timeout 秒后自动回收。页面据此显示
    「建立连接 / 播放中 / 重连中 / 失败 / 未在录制」。
    """
    from urllib.parse import quote
    cfg = state.get_config()
    mon = next((m for m in cfg.get("monitors", []) if m.get("name") == name), None)
    snap = state.get_status()
    st = (snap.get("streamers") or {}).get(name) or {}
    rec = (snap.get("recordings") or {}).get(name) or {}
    lv = cfg.get("live_preview") or {}
    enabled = bool(lv.get("enabled", False))
    sess = live_preview_mod.status(cfg, name)
    if sess.get("active"):
        live_preview_mod.touch(name)      # 轮询即续期
    return {
        "ok": True,
        "name": name,
        "exists": mon is not None,
        "live": bool(st.get("is_live")),
        "recording": bool(rec.get("recording")),
        "started_at": rec.get("started_at"),
        "enabled": enabled,
        "session": sess,                  # {active, state, error, attempt, started_at}
        "ready": bool(enabled and sess.get("state") == "ready"),
        "m3u8": f"/live-hls/{quote(name)}/live.m3u8",
        "room_url": (f"https://live.douyin.com/{mon.get('web_rid')}"
                     if mon and mon.get("web_rid") else ""),
    }


def _live_start(state, name):
    """按需启动预览会话(POST /api/live/<主播>/start)。

    闸门:预览开关已启 + 该主播正在录制(预览会话的流地址取自录制中的拉流)。
    """
    cfg = state.get_config()
    if not (cfg.get("live_preview") or {}).get("enabled", False):
        return {"ok": False, "name": name, "error": "实时预览未开启"}
    snap = state.get_status()
    rec = (snap.get("recordings") or {}).get(name) or {}
    if not rec.get("recording"):
        return {"ok": False, "name": name, "error": "该主播未在录制,无法预览"}
    return live_preview_mod.ensure_start(cfg, name)


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
