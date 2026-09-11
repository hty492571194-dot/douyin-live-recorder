#!/usr/bin/env python3
"""抖音直播自适应监控(双通道检测 + 热点调度 + Web UI)。"""
import asyncio
import glob
import json
import os
import random
import re
import signal
import subprocess
import time

import httpx

from schedule import HotspotStore, next_interval
from detect import check, probe_anchor, confirm_with_cookie
from cookie_guard import CookieGuard
from webui import State, start
from preflight import run_checks, find_ffmpeg
import health as health_mod


# ── 网络层告警 ──────────────────────────────────────────────────────────────
# 单主播的「异常」日志在系统性故障时会刷成一片(10 个主播 × 每轮一条),反而
# 掩盖了「这不是某个主播的问题,是整体连不上」。典型场景:继承了已失效的
# 环境代理 —— 服务活着、Web UI 正常,只有数据停止增长,可以静默好几天。
_NET_WINDOW = 1800          # 统计窗口(秒)
_NET_MIN_SAMPLES = 10       # 窗口内至少这么多次检测才下判断
_NET_MIN_FAILS = 8
_NET_FAIL_RATIO = 0.8
_net_checks = []            # 窗口内检测时间戳
_net_fails = []             # 窗口内失败时间戳
_net_last_alert = 0.0       # 上次告警时间(冷却用)


def _note_network_result(now, failed):
    """累计一次检测结果;命中「全员失败」时打一条带排查建议的告警。"""
    global _net_last_alert
    _net_checks.append(now)
    if failed:
        _net_fails.append(now)
    cut = now - _NET_WINDOW
    while _net_checks and _net_checks[0] < cut:
        _net_checks.pop(0)
    while _net_fails and _net_fails[0] < cut:
        _net_fails.pop(0)
    total, fails = len(_net_checks), len(_net_fails)
    if (total >= _NET_MIN_SAMPLES and fails >= _NET_MIN_FAILS
            and fails / total >= _NET_FAIL_RATIO
            and now - _net_last_alert > _NET_WINDOW):
        _net_last_alert = now
        log("[网络][!] 近 30 分钟 %d/%d 次检测失败(%.0f%%)——"
            "疑似系统性故障,不是某个主播的问题" % (fails, total, 100.0 * fails / total))
        log("[网络]    排查:① 进程是否继承了失效代理变量 "
            "(ps eww -o command -p <pid> | tr ' ' '\\n' | grep -i proxy) "
            "② 网络是否连通 (curl -I https://live.douyin.com) "
            "③ 登录态是否失效 (看 /api/auth)")
        return True
    return False


def _reset_network_stats():
    """仅供测试:清空滑动窗口。"""
    _net_checks.clear()
    _net_fails.clear()
    global _net_last_alert
    _net_last_alert = 0.0


def _purge_env_proxy():
    """清除继承来的环境代理变量,返回清除个数。

    抖音检测与 ffmpeg 拉流都必须直连。若服务是从受管终端 / IDE 集成终端 /
    CI 拉起的,常会继承 HTTP_PROXY / HTTPS_PROXY 等变量:
      · httpx 默认 trust_env=True,会把所有请求送进该代理;
      · ffmpeg 的 HTTP 协议同样读 http_proxy。
    代理一旦不可达(例如它只是某个已退出的沙箱进程留下的端口),症状就是
    「All connection attempts failed」→ 全员检测失败 → 不再判定开播 →
    录制停止 → 历史记录从某个时间点起再无新增。这类故障很隐蔽:服务本身
    活着、Web UI 照常响应,只有数据不再增长。

    必须在进程最早期执行(早于任何 httpx 客户端创建),否则已建连接不受影响。
    逃生开关:确实需要走代理时设 DOUYIN_KEEP_ENV_PROXY=1。
    """
    if os.environ.get("DOUYIN_KEEP_ENV_PROXY") == "1":
        return 0
    names = ("http_proxy", "https_proxy", "all_proxy")
    keys = [k for k in os.environ if k.lower() in names]
    for k in keys:
        os.environ.pop(k, None)
    return len(keys)


PURGED_PROXY_VARS = _purge_env_proxy()
import nas as nas_mod
import preview as preview_mod
import danmaku as danmaku_mod
import subtitle as subtitle_mod
import retention as retention_mod
try:
    import auth as auth_mod      # 登录态管理(仅检测通道);缺依赖时降级为不可用
except Exception:
    auth_mod = None

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
HOTSPOT_PATH = os.path.join(BASE, "hotspots.json")
HOTSPOT_EVENTS_PATH = os.path.join(BASE, "hotspot_events.json")
LOG_DIR = os.path.join(BASE, "logs")
RECORD_DIR = os.path.join(BASE, "recordings")
HISTORY_PATH = os.path.join(RECORD_DIR, "history.json")

DEFAULT_CONFIG = {
    "detection": {"mode": "mix", "cookie": "", "cookie_ref": "",
                  "api_base": "https://live.douyin.com/webcast/room/web/enter/"},
    # 登录态管理(仅服务检测通道;绝不用于弹幕 WS,详见 auth.py 顶部说明)
    "auth": {"enabled": True, "check_interval_hours": 12, "webhook_url": ""},
    "schedule": {
        "cold_interval": 1800, "cold_jitter": 0.2,
        "hot_min": 300, "hot_max": 360, "live_interval": 1200,
        "hotspot_half_width": 1800, "hotspot_max": 2,
        "hotspot_merge_hours": 5, "hotspot_decay_days": 14,
        "analysis_hour": 5, "analysis_cluster_min": 90,
        "analysis_min_events": 5, "event_retention_days": 7,
        "recency_weight": 2, "deviation_trigger_minutes": 60,
        "max_witness_gap": 7200,
        "circuit_errors": 5, "circuit_cooldown": 1800,
    },
    "webui": {"host": "127.0.0.1", "port": 8780},
    # auto_record:开播即自动录制。历史主播缺失该字段时由 _migrate_auto_record
    # 补 True(保持"每次开播都录"的原有行为);新增主播由前端显式写 False。
    "monitors": [{"name": "示例主播", "anchor": "2145431900", "auto_record": True}],
    "nas": {"enabled": False, "protocol": "smb", "host": "", "port": 445,
            "share": "", "username": "", "password_ref": "keychain:douyin-nas",
            "mount_point": "~/DouyinArchive", "root_dir": "直播回放",
            "auto_discover": True, "host_check_hours": 24},
    "archive": {"mode": "auto", "external_dir": ""},
    "preview": {"dir": "", "start_delay": 60, "interval": 30, "shots": 5},
    "recorder": {"output_dir": "", "enabled": True, "format": "flv",
                 "bitrate": 0, "segment_time": 0},
    "danmaku": {"enabled": True, "offset_seconds": 0, "font_size": 44,
                "capture_member": False, "style": "queue",
                "queue_lines": 5, "queue_seconds": 8},
    "on_live_command": "echo '[record] {name} live room_id={room_id}'",
    "on_offline_command": "echo '[stop] {name} offline'",
}


def _resolve_output_dir(cfg):
    """解析录制输出目录:空串 = {BASE}/recordings。"""
    out = (cfg.get("recorder") or {}).get("output_dir", "")
    return out if out else RECORD_DIR


def _deep_merge(base, override):
    """深合并:override 覆盖 base,字典递归合并,用于补齐缺失键。"""
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _keep_prev_config(path):
    """写 config.json 前,把上一版另存为 config.prev.json(只留最近一份)。

    起因:调试时用 `POST /api/config` 整段提交 monitors,把 10 位主播覆盖成 1 位,
    而 config.json 没有任何历史版本,只能靠日志和临时快照拼回。留一份上一版,
    同类误操作就能直接 cp 回来。非 config.json 的写入不受影响。
    """
    if os.path.basename(str(path)) != "config.json":
        return
    prev = os.path.join(os.path.dirname(str(path)) or ".", "config.prev.json")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as src, open(prev, "wb") as dst:
                dst.write(src.read())
    except OSError:
        pass


def _atomic_write_json(path, data):
    """tmp + os.replace 原子写 JSON。"""
    _keep_prev_config(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _migrate_auto_record(cfg):
    """历史主播一次性补 auto_record=True,保持「每次开播都录」的原有行为。

    只在字段缺失时补齐 —— 补齐后字段就常驻配置里了,此后新增主播由前端显式
    写入(默认 False),不会再被这段逻辑改回 True。返回是否有改动。
    """
    changed = False
    for m in cfg.get("monitors") or []:
        if isinstance(m, dict) and "auto_record" not in m:
            m["auto_record"] = True
            changed = True
    return changed


def auto_record_enabled(cfg, name):
    """该主播是否「开播即录」。字段缺失按 True 处理(历史行为)。"""
    for m in cfg.get("monitors") or []:
        if isinstance(m, dict) and m.get("name") == name:
            return bool(m.get("auto_record", True))
    return True


def is_dormant(cfg, name, recording):
    """该主播是否「休眠」:关掉了自动录制、且当前没在录。

    休眠 = 既不自动录、也不自动检测 —— 关掉自动录制就是「这位主播不用管」,
    再每 20~30 分钟查一次纯属浪费请求与 Cookie 配额。想看时点卡片上的「刷新」
    手动查一次,想恢复自动就重新打开开关。
    正在录的(手动开的)不能休眠:还要靠检测循环发现下播、收尾归档。
    """
    if recording:
        return False
    return not auto_record_enabled(cfg, name)


def load_config():
    """读取 config.json;缺失键用默认值补齐;损坏时备份重建默认,不崩溃。"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg = _deep_merge(DEFAULT_CONFIG, raw)
            migrated = _migrate_auto_record(cfg)
            if migrated or json.dumps(cfg, ensure_ascii=False, sort_keys=True) != json.dumps(raw, ensure_ascii=False, sort_keys=True):
                _atomic_write_json(CONFIG_PATH, cfg)  # 补齐缺失键后落盘一次
            return cfg
        except (json.JSONDecodeError, OSError) as e:
            backup = f"{CONFIG_PATH}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(CONFIG_PATH, backup)
                log(f"[警告] config.json 损坏({e}),已备份为 {backup},重建默认配置")
            except OSError:
                log(f"[警告] config.json 损坏({e})且备份失败,重建默认配置")
    _atomic_write_json(CONFIG_PATH, DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


# --- 日志(同时 stdout 与落盘,跨天切分,清理 7 天前) ---
_log_date = None


def _ensure_log():
    global _log_date
    today = time.strftime("%Y-%m-%d")
    if _log_date == today:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    _log_date = today
    try:
        cutoff = time.time() - 7 * 86400
        for fn in os.listdir(LOG_DIR):
            if fn.startswith("monitor-") and fn.endswith(".log"):
                fp = os.path.join(LOG_DIR, fn)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except OSError:
                    pass
    except OSError:
        pass


def log(msg):
    """输出一条日志:stdout + logs/monitor-YYYY-MM-DD.log;写文件失败降级仅 stdout。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _ensure_log()
    if _log_date:
        try:
            with open(os.path.join(LOG_DIR, f"monitor-{_log_date}.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# --- 命令执行(可观测:记录 pid/name/cmd,轮询退出码) ---
_active_procs = {}  # pid -> subprocess.Popen


def run_command(state, template, ctx):
    if not template:
        return
    ctx = {**ctx, "BASE": BASE}  # B1: 所有命令模板统一可用 {BASE}
    try:
        cmd = template.format(**ctx)
    except (KeyError, IndexError):
        log(f"命令模板占位符缺失: {template}")
        return
    log(f"  执行: {cmd}")
    try:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _active_procs[proc.pid] = proc
        state.add_command(proc.pid, ctx.get("name", "?"), cmd)
    except Exception as e:
        log(f"  命令失败: {e}")


def poll_commands(state):
    """轮询活跃子进程;非 0 退出码告警。"""
    for pid in list(_active_procs):
        proc = _active_procs.get(pid)
        if proc is None:
            continue
        code = proc.poll()
        if code is None:
            continue
        del _active_procs[pid]
        state.mark_command_exit(pid, code)
        if code != 0:
            log(f"[命令] pid={pid} 异常退出 code={code}")


# --- 录制管理(ffmpeg 拉流,可观测进度) ---
_recordings = {}  # name -> {proc, started_at, output_path, stream_url, format, room_id}
# 录制健康度:name -> {size, ts, restarts, room_id, stalled}
#   size/ts:最近一次观测到的已落盘字节数与时(用)于判定「文件是否还在增长」
#   restarts:本场因异常重启的次数(达上限后不再重启,避免空转刷历史)
#   重启达上限后进入冷却期,冷却期内不再为该主播启动录制
_rec_health = {}
_rec_giveup_until = {}  # name -> 冷却截止时间戳
_history = []     # 录制历史(内存缓存,持久化到 recordings/history.json)
# webui State 引用(由 main 注入)。history 的任何变更都必须同步过去,
# 否则前端表格与弹幕面板读到的是旧快照:归档后本机文件已被移走,而快照里
# 仍是 archive.state=none,弹幕面板就会重定位失败并误报「NAS 未挂载」。
_web_state = None
_manual_paused = set()   # 用户手动停止录制的主播名(直播中也不再自动重录)
_last_stream_url = {}    # name -> 最近一次检测到的 stream_url(供手动开始)

# --- Cookie 使用护栏 ---
# 真实账号的登录态是稀缺资源,所有带 Cookie 的请求都要先过这道闸。
# 详见 cookie_guard.py;这里持有全局唯一实例,配置变更时重新载入规则。
_cookie_guard = None
# 供矛盾状态校验复用的 HTTP client(由 main 注入,与检测循环同一个)
_http_client = None
# 矛盾状态校验的节流状态:name -> {last, tries, room_id}
_conflict_state = {}
_preview_tasks = {}      # name -> asyncio.Task(自动截图任务,下播/删除时取消)


def _relocate_history(cfg):
    """启动时重新定位历史记录:本机文件已不在(通常因已归档)时,
    到归档目标(NAS / 外接硬盘)里找对应的 <主播>/<日期> 目录并更新指向。

    这让"存储位置变化"后历史里的链接自动指向新位置,而不是留一条死路径。
    返回被重新定位的条目数。
    """
    nas_cfg = cfg.get("nas") or {}
    if not nas_cfg.get("enabled", False):
        return 0
    root = nas_cfg.get("root_dir", "直播回放")
    targets = []
    try:
        mp = nas_mod._mount_point(nas_cfg)
        if os.path.isdir(mp):
            targets.append((os.path.join(mp, root), "nas"))
    except Exception:
        pass
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext and os.path.isdir(ext):
        targets.append((os.path.join(ext, root), "external"))
    if not targets:
        return 0

    relocated = 0
    missing = 0
    for e in _history:
        if e.get("status") != "done":
            continue
        a = e.get("archive") or {}
        if a.get("state") == "archived":
            continue
        p = e.get("output_path") or ""
        if not p:
            continue
        src_dir = os.path.dirname(p)
        # 判据:**视频文件**是否还在本机(而非仅目录存在)。
        # 老格式条目目录里可能只剩 .meta,视频早已搬走,这种情况同样需要重定位。
        base = os.path.basename(p)
        pat = base.replace("-%03d", "-*") if "%03d" in base else base
        if glob.glob(os.path.join(src_dir, pat)):
            continue  # 本机还在,无需重定位
        # 日期目录:优先取路径末段,老格式(无日期层)从文件名 <主播>-20260822-... 提取
        date_dir = os.path.basename(src_dir)
        if not (len(date_dir) == 10 and date_dir[4] == "-" and date_dir[:4].isdigit()):
            m = re.search(r"-(\d{8})-\d{6}", os.path.basename(p))
            if not m:
                continue
            s = m.group(1)
            date_dir = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        name = e.get("name", "?")
        hit = None
        for base, kind in targets:
            cand = os.path.join(base, name, date_dir)
            if os.path.isdir(cand):
                hit = (cand, kind)
                break
        if hit:
            a.update({"state": "archived", "dest": hit[0], "target": hit[1],
                      "at": a.get("at") or time.time(), "error": None})
            relocated += 1
        else:
            # 本机没有、归档目标里也没有 —— 文件确实已不存在,标记避免继续显示死路径
            a.update({"state": "missing", "dest": None, "target": None,
                      "error": "文件已不在本机,归档目标中也未找到"})
            missing += 1
        e["archive"] = a
    if relocated or missing:
        _save_history()
        log(f"[历史] 重新定位: 归档 {relocated} 条 / 文件缺失 {missing} 条")
    return relocated


def _load_history():
    global _history
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _history = data if isinstance(data, list) else []
        else:
            _history = []
    except (json.JSONDecodeError, OSError):
        _history = []
    # 启动时清理孤儿 recording 记录:进程刚启动,_recordings 为空,所有 recording 均为遗留。
    # 文件仍在 → 上次异常中断(ffmpeg 崩溃/进程被杀),标记 done 并回填时长/大小;
    # 文件不存在或分片 pattern → 丢弃。否则会永远卡在"录制中"。
    keep = []
    cleaned = False
    for e in _history:
        if e.get("status") == "recording":
            p = e.get("output_path", "") or ""
            if p and "%03d" not in p and os.path.exists(p):
                e["status"] = "done"
                e["ended_at"] = time.time()
                real = _media_duration(p)
                e["duration"] = round(real if real is not None
                                      else (time.time() - e.get("started_at", time.time())), 1)
                try:
                    e["size"] = os.path.getsize(p)
                except OSError:
                    pass
                keep.append(e)
            else:
                cleaned = True  # 孤儿记录:文件不存在或分片遗留,丢弃
        else:
            keep.append(e)
    if cleaned:
        _history = keep
        _save_history()


def _save_history():
    try:
        os.makedirs(RECORD_DIR, exist_ok=True)
        _atomic_write_json(HISTORY_PATH, _history)
    except OSError:
        pass


def _push_history():
    """把内存 history 同步给 webui state(无引用或异常均静默,不阻断录制主流程)。"""
    if _web_state is None:
        return
    try:
        _web_state.set_history(_history)
    except Exception as e:
        log(f"[历史] 同步到前端失败: {e}")


def _commit_history():
    """history 变更后的统一出口:落盘 + 同步前端。

    只落盘不同步的话,磁盘 history.json 是对的,但内存里的前端快照是旧的,
    前端要等到下次进程重启才看得到——期间弹幕面板会把已归档场次当本机路径处理。
    """
    _save_history()
    _push_history()


def _history_add(state, name, output_path, started_at):
    """录制开始时追加一条历史(status=recording)。"""
    # archive 字段记录归档生命周期:
    #   none(留本机) / pending(已入队) / archiving(搬运中)
    #   archived(已归档,含归档落点 dest) / failed(失败,含原因)
    _history.append({"name": name, "started_at": started_at,
                     "output_path": output_path, "status": "recording",
                     "archive": {"state": "none", "target": None,
                                 "dest": None, "at": None, "error": None}})
    if len(_history) > 500:
        del _history[:-500]  # 只保留最近 500 条
    _commit_history()


def _history_finish(state, name, ended_at=None):
    """录制结束时回填结束时间/时长/大小(status=done);清理该主播所有 recording 条目。

    时长优先取文件真实媒体时长(ffmpeg -i 解析),失败回退墙钟差值(ended - started_at),
    保证历史记录时长与视频文件一致。state 可为 None(退出清理场景)。
    """
    ended = ended_at if ended_at is not None else time.time()
    changed = False
    for e in _history:
        if e.get("name") == name and e.get("status") == "recording":
            e["status"] = "done"
            e["ended_at"] = ended
            p = e.get("output_path")
            real = _media_duration(p) if p else None
            e["duration"] = round(real if real is not None
                                  else (ended - e.get("started_at", ended)), 1)
            if p and os.path.exists(p):
                try:
                    e["size"] = os.path.getsize(p)
                except OSError:
                    pass
            changed = True
    if changed:
        _commit_history()


def _mark_archive_state(session_dir, state_name, dest=None, target=None, error=None):
    """把归档状态回写到录制历史。

    归档条目是场次日期目录,history 条目是其中的文件模板路径,
    用 dirname(output_path) == session_dir 匹配(同场次可一对多)。
    """
    if not session_dir:
        return
    src = os.path.normpath(session_dir)
    changed = False
    for e in _history:
        p = e.get("output_path") or ""
        if not p or os.path.normpath(os.path.dirname(p)) != src:
            continue
        a = e.get("archive") or {}
        # 已归档不再回退到 pending/archiving(避免重入队覆盖最终状态)
        if a.get("state") == "archived" and state_name in ("pending", "archiving"):
            continue
        a["state"] = state_name
        if target is not None:
            a["target"] = target
        if dest is not None:
            a["dest"] = dest
        a["at"] = time.time() if state_name == "archived" else a.get("at")
        a["error"] = error
        e["archive"] = a
        changed = True
    if changed:
        _commit_history()


def _on_archive_result(src_path, state_name, dest=None, target=None, error=None):
    """nas 模块归档回调:更新 history 的归档状态。"""
    _mark_archive_state(src_path, state_name, dest, target, error)


def _media_duration(path):
    """用 ffmpeg -i 解析媒体真实时长(秒);分片 pattern/文件缺失/解析失败返回 None。"""
    if not path or "%03d" in path or not os.path.isfile(path):
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    try:
        r = subprocess.run([ffmpeg, "-i", path], capture_output=True,
                           text=True, timeout=10)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


def _build_ffmpeg_args(ffmpeg_path, stream_url, out_dir, name, fmt, rec_cfg, ts):
    """按 recorder 配置构造 ffmpeg 参数,返回 (args, output_path)。

    - bitrate 非空 → 重编码(libx264 + aac),否则 -c copy 直接复制原流(省 CPU)
    - segment_time > 0 → 分片输出(-f segment,文件名带 %03d 序号),否则单文件
    """
    args = [ffmpeg_path, "-y", "-loglevel", "error",
            # 墙钟时间戳:让 FLV tag 携带绝对毫秒(弹幕字幕按原生时间对齐的关键)
            "-use_wallclock_as_timestamps", "1",
            "-i", stream_url,
            # 输出保留绝对时间戳(copyts),分片不重置(跨分片连续)
            "-copyts"]

    # 码率:数字视为 Kbps(如 5000 → 5000k),兼容 "5000k"/"3M" 字符串
    bitrate = rec_cfg.get("bitrate")
    bitrate_str = ""
    if bitrate not in (None, "", 0):
        b = str(bitrate).strip()
        if b.isdigit():
            b += "k"
        bitrate_str = b
    seg = int(rec_cfg.get("segment_time") or 0) or 0

    if bitrate_str:
        args += ["-c:v", "libx264", "-b:v", bitrate_str, "-c:a", "aac", "-b:a", "128k"]
    else:
        args += ["-c", "copy"]

    if seg > 0:
        pattern = os.path.join(out_dir, f"{name}-{ts}-%03d.{fmt}")
        args += ["-f", "segment", "-segment_time", str(seg),
                 "-reset_timestamps", "0", pattern]
        output_path = pattern
    else:
        path = os.path.join(out_dir, f"{name}-{ts}.{fmt}")
        args.append(path)
        output_path = path
    return args, output_path


def _start_recording(state, name, stream_url, cfg, room_id=None):
    """用 ffmpeg 从 stream_url 拉流录制到 output_dir/name/。返回 proc 或 None。"""
    if not stream_url:
        return None
    rec_cfg = cfg.get("recorder") or {}
    if not rec_cfg.get("enabled", True):
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log(f"[录制] {name} 未找到 ffmpeg,无法录制(请 brew install ffmpeg)")
        return None
    # 本机暂存目录与归档层级一致:<输出根>/<主播>/<YYYY-MM-DD>/(场次归属=开播日)
    out_dir = os.path.join(_resolve_output_dir(cfg), name, time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        log(f"[录制] {name} 无法创建目录 {out_dir}")
        return None
    fmt = rec_cfg.get("format", "flv") or "flv"
    ts = time.strftime("%Y%m%d-%H%M%S")
    args, path = _build_ffmpeg_args(ffmpeg, stream_url, out_dir, name, fmt, rec_cfg, ts)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[录制] {name} 启动失败: {e}")
        return None
    started_at = time.time()
    _recordings[name] = {"proc": proc, "started_at": started_at,
                         "output_path": path, "stream_url": stream_url, "format": fmt,
                         "room_id": room_id}
    log(f"[录制] {name} 开始: {path}")
    _sync_recording_state(state, name)
    _history_add(state, name, path, started_at)
    return proc


def _sync_recording_state(state, name, stalled=False):
    rec = _recordings.get(name)
    if not rec:
        return
    # 录制中:时长一律用墙钟差值(与前端实时计算/历史页公式一致)。
    # 不再用 ffmpeg 解析正在写入的文件——媒体时长 < 墙钟(ffmpeg 启动/缓冲延迟),
    # 会导致主播页与历史页时长对不上;真实媒体时长仅在录制结束时(_history_finish)解析。
    h = _rec_health.get(name) or {}
    state.set_recording(name, {
        "recording": True,
        "started_at": rec["started_at"],
        "duration": round(time.time() - rec["started_at"], 1),
        "output_path": rec["output_path"],
        "stream_url": rec["stream_url"],
        "format": rec["format"],
        # 健康度:文件增长停滞(流已断但 ffmpeg 未退出)= 假在线,前端据此告警
        "stalled": bool(stalled),
        "stall_seconds": round(max(0.0, time.time() - h.get("ts", time.time())), 1),
        "restarts": int(h.get("restarts") or 0),
    })


def _stop_recording(state, name):
    rec = _recordings.pop(name, None)
    _rec_health.pop(name, None)
    if not rec:
        return
    try:
        rec["proc"].terminate()
        try:
            rec["proc"].wait(timeout=5)  # 等 ffmpeg flush 落盘
        except subprocess.TimeoutExpired:
            try:
                rec["proc"].kill()
            except Exception:
                pass
    except Exception:
        pass
    log(f"[录制] {name} 停止: {rec['output_path']}")
    # 弹幕字幕收尾:录制结束即固化本前缀的分片锚点与 .ass(弹幕会话随直播延续)
    if (state.get_config().get("danmaku") or {}).get("enabled", False):
        try:
            dm = danmaku_mod.get(name)
            subtitle_mod.finalize(rec["output_path"], state.get_config(),
                                  dm.clock_drift_ms if dm else 0.0)
        except Exception as e:
            log(f"[弹幕] {name} 字幕收尾失败: {e}")
    _history_finish(state, name)
    state.remove_recording(name)


def _stop_recording_manual(state, name):
    """手动停止录制:终止 ffmpeg 并标记暂停,主播直播中也不再自动重录。"""
    _manual_paused.add(name)
    _stop_recording(state, name)
    log(f"[录制] {name} 手动停止(暂停自动录制)")


def _start_recording_manual(state, name):
    """手动恢复录制:清除暂停标记与冷却,用缓存的最新流地址立即启动。返回是否成功。"""
    _manual_paused.discard(name)
    _rec_giveup_until.pop(name, None)
    url = _last_stream_url.get(name)
    if not url:
        log(f"[录制] {name} 手动开始失败:暂无缓存流地址")
        return False
    _start_recording(state, name, url, state.get_config())
    return True


_danmaku_paths = {}  # name -> 当前录制输出路径(弹幕会话跟踪,前缀变化时切换 jsonl)


def _ensure_danmaku(state, name, room_id, cfg):
    """直播中确保弹幕会话在跑(配置开启且拿到 room_id)。幂等,失败不影响录制。"""
    if not (cfg.get("danmaku") or {}).get("enabled", False) or not room_id:
        return
    rec = _recordings.get(name)
    out = (rec or {}).get("output_path") or _danmaku_paths.get(name)
    if not out:
        return
    _danmaku_paths[name] = out
    if danmaku_mod.get(name) is None:
        danmaku_mod.start_for(name, room_id, out, cfg)
    else:
        danmaku_mod.retarget_for(name, out)


def _stop_danmaku(name, cfg=None):
    """下播/移除主播时停止弹幕会话,并补一次字幕收尾。

    _stop_recording 的 finalize 可能早于 WS 停止,期间到达的最后几条弹幕
    会漏进 jsonl 但不在 .ass 里;此处先停 WS 再重生成一次(幂等)。"""
    out = _danmaku_paths.pop(name, None)
    rec = danmaku_mod.stop_for(name)
    if rec is not None and out and cfg and (cfg.get("danmaku") or {}).get("enabled", False):
        try:
            subtitle_mod.finalize(out, cfg, rec.clock_drift_ms)
        except Exception as e:
            log(f"[弹幕] {name} 停止后收尾失败: {e}")


_VIDEO_EXTS = (".flv", ".mp4", ".ts", ".mkv", ".mov", ".part")


def _rec_output_size(path):
    """当前录制输出已落盘字节数(分片模式取该前缀全部分片之和)。

    只统计视频类文件,排除 .ass/.jsonl 等随行文件(它们由弹幕模块独立写入,
    增长与否不能代表视频流是否正常)。目录不可读返回 None。
    """
    if not path:
        return None
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    stem = base.split("-%03d.")[0] if "-%03d." in base else os.path.splitext(base)[0]
    if not stem:
        return None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    total = 0
    for fn in names:
        if not fn.startswith(stem) or not fn.lower().endswith(_VIDEO_EXTS):
            continue
        try:
            total += os.path.getsize(os.path.join(d, fn))
        except OSError:
            pass
    return total


def _give_up(state, name, cooldown, reason):
    """停止本场录制并进入冷却:短时间内反复救不回来就不再空转。

    冷却期内 _refresh_recording 直接返回,避免每轮巡检都重开一个空文件
    刷出一堆零字节历史条目;主播下播(或下次真正开播)时由检测循环清除。
    """
    log(f"[录制][!] {name} {reason},停止本场录制并冷却 {cooldown}s"
        f"(请检查流地址/网络)")
    _stop_recording(state, name)
    _rec_health.pop(name, None)
    _rec_giveup_until[name] = time.time() + cooldown


def _restart_recording(state, name, stream_url, cfg, room_id, reason=""):
    """停掉当前 ffmpeg 并重开,累计重启次数用于止损。

    先 _history_finish 关闭旧条目(保留已录到的内容),避免孤儿 recording。
    """
    h = _rec_health.get(name) or {}
    restarts = int(h.get("restarts") or 0) + 1
    _recordings.pop(name, None)
    _history_finish(state, name)
    _start_recording(state, name, stream_url, cfg, room_id)
    _rec_health[name] = {"size": 0, "ts": time.time(), "restarts": restarts,
                         "room_id": room_id, "stalled": False}
    log(f"[录制] {name} 已重启({reason or '未知原因'}),本场累计 {restarts} 次")


# --- 矛盾状态校验:判定在播却拿不到流地址 ---
#
# 这是两种故障的分岔口,靠 302 单通道分辨不出来:
#   · 真的在播,只是 reflow 页没解析出地址 → API 能给出流地址,把录制救回来;
#   · 302 假阳性(其实没开播) → API 判 status != 2,纠正状态、停止空转。
# 两种结果都很值钱,所以值得花 Cookie。但 Cookie 很贵,而且本函数的调用方
# _refresh_recording 既被检测循环(5~20 分钟一轮)调用、也被 20 秒一次的
# 健康巡检调用 —— 不节流的话,一场假阳性就能把日配额刷穿。
def _conflict_due(name, cfg, room_id):
    """是否该再发起一次矛盾校验。返回 (是否放行, 原因)。"""
    g = (cfg.get("detection") or {}).get("cookie_guard") or {}
    interval = float(g.get("conflict_probe_interval", 300))
    max_tries = int(g.get("conflict_probe_max", 3))
    now = time.time()
    st = _conflict_state.get(name)
    # 换房间 = 新的一场,计数从头开始
    if st is None or (room_id and st.get("room_id") != room_id):
        _conflict_state[name] = {"last": now, "tries": 1, "room_id": room_id}
        return True, "首次校验"
    if st["tries"] >= max_tries:
        return False, f"本场已校验 {st['tries']} 次,达上限 {max_tries}"
    if now - st["last"] < interval:
        return False, f"距上次校验仅 {int(now - st['last'])}s(< {int(interval)}s)"
    st["last"] = now
    st["tries"] += 1
    st["room_id"] = room_id or st.get("room_id")
    return True, f"第 {st['tries']}/{max_tries} 次校验"


def _schedule_conflict_probe(state, name, cfg, room_id=None):
    """在播却无流地址时,异步发起一次带 Cookie 的专项校验(不阻塞调用方)。"""
    if state is None:
        return
    due, why = _conflict_due(name, cfg, room_id)
    if not due:
        return
    log(f"[矛盾校验] {name} 触发({why})")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环(如单元测试同步调用),跳过
    loop.create_task(_conflict_probe(state, name, cfg, room_id))


async def _conflict_probe(state, name, cfg, room_id=None):
    """用 Cookie 校验「判定在播却拿不到流地址」这一矛盾状态。

    两种结果:
      · API 确认在播且给出流地址 → 补上地址并重新进入录制流程;
      · API 确认未开播 / 无数据 → 判定为 302 假阳性,记录并停止本场录制,
        避免 ffmpeg 反复空转刷历史(这正是某位主播此前 17.82 小时空壳的成因)。
    """
    global _http_client
    if not room_id:
        return
    client = _http_client
    if client is None:
        try:
            client = httpx.AsyncClient(timeout=10, follow_redirects=False,
                                       trust_env=False)
        except Exception:
            return
        own = True
    else:
        own = False
    try:
        res, why = await confirm_with_cookie(
            client, room_id, cfg, guard=_cookie_guard,
            anchor=name, reason="conflict")
        if res is None:
            log(f"[矛盾校验] {name} 未能完成(room_id={room_id}):{why}")
            return
        status = res.get("status")
        su = res.get("stream_url") or ""
        if res.get("is_live") and su:
            log(f"[矛盾校验] {name} 确认在播且取到流地址,补录中")
            _last_stream_url[name] = su
            # 不清除节流:救回一次不等于本场不会再出状况,但 Cookie 经不起反复花。
            # 计数继续累加,本场用完 conflict_probe_max 次即止;换房间会自动重置。
            _refresh_recording(state, name, su, cfg, room_id)
        elif res.get("is_live"):
            log(f"[矛盾校验] {name} 确认在播,但 API 亦无流地址(status={status})")
        else:
            log(f"[矛盾校验] {name} API 判定未开播(status={status})"
                f" —— 302 为假阳性,停止本场空转")
            _stop_recording(state, name)
            _rec_giveup_until.pop(name, None)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[矛盾校验] {name} 异常: {type(e).__name__}: {e}")
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass


def _refresh_recording(state, name, stream_url, cfg, room_id=None):
    """直播中维持录制:未录则开始;进程退出 / 数据停滞 / 换房间则重启;否则刷新时长。

    「虚假在线」防护:ffmpeg 在拉流断开时可能既不退出、也不再写数据
    (进程存活但文件停止增长)。只看 proc.poll() 会一直记为「录制中」却录不到
    内容,历史里留下空壳条目。此处叠加三条止损:
      1. 文件增长停滞超过 stall_seconds(默认 90s)→ 重启;
      2. 换 room_id(主播重开/换号)→ 旧文件已不属于本场,强制重启新文件;
      3. 单场重启超过 stall_max_restarts(默认 4 次)仍无数据 → 停止本场录制,
         不再空转刷历史。
    """
    rec_cfg = cfg.get("recorder") or {}
    stall_sec = int(rec_cfg.get("stall_seconds") or 90)
    max_restarts = int(rec_cfg.get("stall_max_restarts") or 4)
    cooldown = int(rec_cfg.get("stall_cooldown") or 600)

    if not stream_url:
        if name in _recordings:
            return  # 已有录制继续;无流地址不打断
        log(f"[录制] {name} 检测到直播但未解析到拉流地址,无法录制")
        # 矛盾状态:判定在播却拿不到流地址。要么是真在播要救回录制,
        # 要么是 302 假阳性要纠正 —— 用 Cookie 做一次专项校验(自带节流)。
        _schedule_conflict_probe(state, name, cfg, room_id)
        return
    rec = _recordings.get(name)

    # 换房间:room_id 变了说明是另一场直播,旧文件不应再续写。
    # 同时解除上一场的冷却——上一场救不回来不代表这一场也录不了。
    if rec is not None and room_id and rec.get("room_id") and room_id != rec["room_id"]:
        _rec_giveup_until.pop(name, None)
        log(f"[录制] {name} room_id 变更 {rec['room_id']} → {room_id},重启为新场次")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="换房间")
        return

    # 冷却期:本场已被判定为「救不回来」,在冷却结束前不再尝试。
    # 没有这道闸,流地址失效时 ffmpeg 会一退出就被立刻重开(巡检 20s 一轮),
    # 几分钟内刷出几十条空历史条目。
    if time.time() < _rec_giveup_until.get(name, 0):
        return

    if rec is None:
        _start_recording(state, name, stream_url, cfg, room_id)
        _rec_health[name] = {"size": 0, "ts": time.time(), "restarts": 0,
                             "room_id": room_id, "stalled": False}
        return

    if rec["proc"].poll() is not None:
        # 进程退出同样计入重启上限:流地址失效时 ffmpeg 会立刻退出,
        # 不限次的话巡检每轮都会重开一个空文件。
        h = _rec_health.setdefault(name, {"size": 0, "ts": time.time(),
                                          "restarts": 0, "room_id": room_id,
                                          "stalled": False})
        if h["restarts"] >= max_restarts:
            _give_up(state, name, cooldown,
                     f"ffmpeg 反复退出,已重启 {h['restarts']} 次")
            return
        log(f"[录制] {name} ffmpeg 已退出,用新流地址重启")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="进程退出")
        return

    # 数据存活检查:文件长时间不增长 = 假在线
    h = _rec_health.setdefault(name, {"size": 0, "ts": time.time(), "restarts": 0,
                                      "room_id": room_id, "stalled": False})
    size = _rec_output_size(rec["output_path"])
    now = time.time()
    if size is None or size > h["size"]:
        h["size"] = size or 0
        h["ts"] = now
    stalled_for = now - h["ts"]
    # 过半阈值即标记告警(前端显示「无数据流」),满阈值才动手重启,
    # 避免把 ffmpeg 正常的缓冲/网络抖动当成故障。
    h["stalled"] = stalled_for >= stall_sec * 0.5
    if stalled_for >= stall_sec:
        if h["restarts"] >= max_restarts:
            _give_up(state, name, cooldown,
                     f"数据停滞 {int(stalled_for)}s 且已重启 {h['restarts']} 次仍无数据")
            return
        log(f"[录制][!] {name} 数据停滞 {int(stalled_for)}s"
            f"(ffmpeg 存活但无写入),重启第 {h['restarts'] + 1}/{max_restarts} 次")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="数据停滞")
        return
    _sync_recording_state(state, name, stalled=h["stalled"])


async def _recording_watchdog(state, interval=20):
    """录制健康巡检:高频检查在录文件是否仍在增长。

    独立于状态检测周期——直播中的检测间隔可能长达 20 分钟(live_interval),
    只靠检测循环调用 _refresh_recording 的话,流断了要等 20 分钟才发现。
    """
    while True:
        try:
            health_mod.beat("watchdog")
            await asyncio.sleep(interval)
            cfg = state.get_config()
            if not (cfg.get("recorder") or {}).get("enabled", True):
                continue
            for name in list(_recordings.keys()):
                rec = _recordings.get(name)
                if not rec:
                    continue
                # 优先用检测循环缓存的最新流地址(旧地址可能已过期)
                url = _last_stream_url.get(name) or rec.get("stream_url")
                try:
                    _refresh_recording(state, name, url, cfg, rec.get("room_id"))
                except Exception as e:
                    log(f"[巡检] {name} 健康检查异常: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[巡检] 录制健康检查异常: {e}")


def _preview_selected(cfg, name):
    """该主播是否已设置预览图。"""
    return bool(preview_mod.selected_preview(cfg, name))


async def _preview_capture_loop(state, name, ffmpeg):
    """直播中维持 shots 张备选截图:未设头像时,库存不足 shots 张就持续补充。

    用 _last_stream_url 取最新流地址(避免流过期);设置头像时 return,由 reconcile
    在「删头像后」重新拉起;下播/删除主播由 _cancel_preview_capture 取消。
    """
    cfg = state.get_config()
    pv = cfg.get("preview") or {}
    delay = max(1, int(pv.get("start_delay", 60)))
    interval = max(1, int(pv.get("interval", 30)))
    shots = max(1, int(pv.get("shots", 5)))
    out_dir = preview_mod.streamer_dir(cfg, name)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return
    try:
        await asyncio.sleep(delay)
        while True:
            cfg = state.get_config()
            if _preview_selected(cfg, name):
                return  # 已设头像,停止截图
            stream_url = _last_stream_url.get(name)
            if not stream_url:
                await asyncio.sleep(interval)
                continue
            existing = preview_mod.list_images(cfg, name)
            if len(existing) < shots:
                ts = time.strftime("%Y%m%d-%H%M%S")
                out = os.path.join(out_dir, f"shot-{ts}-{len(existing) + 1}.jpg")
                ok = await asyncio.to_thread(preview_mod.capture_frame, ffmpeg, stream_url, out)
                if ok:
                    log(f"[预览] {name} 截图(库存 {len(existing) + 1}/{shots}): {out}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise


def _ensure_preview_capture(state, name):
    """确保「直播中且未设头像」的主播有截图任务在跑。幂等,供 once 与 reconcile 调用。"""
    if _preview_selected(state.get_config(), name):
        return
    if name in _preview_tasks and not _preview_tasks[name].done():
        return
    if not _last_stream_url.get(name):
        return
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return
    task = asyncio.get_running_loop().create_task(
        _preview_capture_loop(state, name, ffmpeg))
    _preview_tasks[name] = task


def _cancel_preview_capture(name):
    """取消某主播的截图任务(下播/删除/退出)。"""
    task = _preview_tasks.pop(name, None)
    if task and not task.done():
        task.cancel()


def _cleanup_recordings():
    """进程退出时终止所有录制子进程,避免 ffmpeg 残留为孤儿进程。"""
    for name in list(_recordings):
        rec = _recordings.pop(name, None)
        if not rec:
            continue
        try:
            rec["proc"].terminate()
            try:
                rec["proc"].wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    rec["proc"].kill()
                except Exception:
                    pass
        except Exception:
            pass
        log(f"[录制] {name} 退出清理: {rec['output_path']}")
        _history_finish(None, name)
    for name in list(_preview_tasks):
        _cancel_preview_capture(name)


def _handle_term(signum, frame):
    log(f"收到信号 {signum},清理录制后退出")
    _cleanup_recordings()
    raise SystemExit(0)


def _register_signal_handlers():
    try:
        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)
    except (ValueError, OSError):
        pass  # 非主线程或受限环境,忽略


def _recent_hotspot(store, anchor):
    """最近热点窗口:返回 {center_minute, last_hit} 或 None。"""
    ws = store.windows(anchor)
    if not ws:
        return None
    w = ws[0]
    return {"center_minute": w.get("center"), "last_hit": w.get("last_hit")}


def _snap(name, anchor, phase, store, last_check, next_in, result, probe=None):
    return {
        "name": name, "anchor": anchor, "probe": probe or anchor, "phase": phase,
        "windows": len(store.windows(anchor)),
        "hotspot": _recent_hotspot(store, anchor),
        "last_check": last_check,
        "next_in": round(next_in, 1) if next_in else None,
        "is_live": bool(result.get("is_live")) if result else False,
        "status": (result or {}).get("status"),
        "method": (result or {}).get("method", ""),
        "error": (result or {}).get("error", ""),
    }


def _record_open(store, name, anchor, result, prev_meta, now, cfg):
    """记录一次开播(热点事件):三源分级取开播时间,写事件日志并更新热点。

    S1 create_time: 检测响应透传的真实开播时间(API/302 通道既有响应自带,零额外请求)。
    S2 witness_mid: 无 create_time 时取见证区间 [上次检测时刻, 本次检测时刻] 中点。
    S3 skip(防污染): 无 create_time 且(重启时已在播 / 见证区间过大)且主播已有热点数据
       → 不记录,避免重启时刻污染既有模式。新主播(无任何数据)豁免:首播必记录。
    返回记录使用的开播时间戳(未记录返回 None)。
    """
    sched = cfg.get("schedule") or {}
    has_data = store.has_data(anchor)
    ct = result.get("create_time")
    try:
        ct = float(ct) if ct is not None else None
    except (TypeError, ValueError):
        ct = None

    if ct and 0 < ct <= now + 60 and now - ct <= 86400:
        # S1:真实开播时间(拒绝未来值与超过 1 天的陈旧值)
        start_ts, source = ct, "create_time"
    else:
        # S2/S3:见证区间估计,配合防污染规则
        gap_max = float(sched.get("max_witness_gap", 7200))
        last_check = (prev_meta or {}).get("last_check_ts") or now
        gap = now - last_check
        resumed = bool((prev_meta or {}).get("was_live")) and \
            (prev_meta or {}).get("last_room_id") == result.get("room_id")
        if resumed and has_data:
            log(f"[热点] {name} 重启恢复会话且无 create_time,跳过本次记录(防污染)")
            return None
        if gap > gap_max and has_data:
            log(f"[热点] {name} 见证区间 {gap / 3600:.1f}h 过大且无 create_time,跳过记录")
            return None
        start_ts, source = last_check + gap / 2, "witness_mid"

    kind = store.record_open(anchor, start_ts, source, now)
    src_desc = "真实开播时间" if source == "create_time" else "见证区间估计"
    log(f"[热点] {name} 开播时刻 {time.strftime('%H:%M', time.localtime(start_ts))}"
        f"({src_desc}{',偏离过大已触发重分析' if kind == 'analyzed' else ''})")
    return start_ts


async def run_streamer(client, mon, state, store):
    name = mon.get("name", "?")
    anchor = str(mon.get("anchor", ""))
    recording = False
    # 会话状态恢复:重启后 last_room_id 不再丢失,
    # 「重启时已在播」由 _record_open 的防污染规则(S3)处理,不会被当新开播记录
    sess = store.get_session(anchor)
    last_room_id = (sess or {}).get("last_room_id")
    err_count = 0
    cooldown_until = 0.0
    warned_no_probe = False
    await asyncio.sleep(random.uniform(0, 60))  # 启动随机相位

    async def once(next_in_hint=None, force=False):
        """执行一次检测并更新状态;返回 (kind, value):
        ('ok', interval) 正常;('noprobe', 120) 缺 web_rid;('cooldown', 剩余秒);
        ('dormant', interval) 休眠中(关了自动录制),不检测。
        next_in_hint: 手动刷新时传入「到原计划检测点的剩余秒」,避免 next_in 被重置。
        force: 手动刷新触发 —— 休眠主播也会被唤醒检测这一次。"""
        nonlocal recording, last_room_id, err_count, cooldown_until, warned_no_probe
        cfg = state.get_config()
        now = time.time()
        # 健康心跳:面板据此判断「检测协程是否还活着」。放在这里而不是成功分支,
        # 是为了让「一直失败但协程没死」也能刷新心跳 —— 那种情况由网络层告警负责,
        # 面板不该把它显示成检测协程卡死。
        health_mod.beat("detect", count=len(cfg.get("monitors", [])))

        # 热重取最新 monitor 条目:UI 上补填 web_rid 无需重启协程,下一轮即生效
        cur = next((m for m in cfg.get("monitors", [])
                    if m.get("name") == name and str(m.get("anchor", "")) == anchor), mon)
        anchor_now = str(cur.get("anchor", ""))
        # 休眠判定放在缺 web_rid 之后:没填 web_rid 的主播仍需提示(那是配置没填全,
        # 不是用户主动休眠),否则关掉自动录制后这条提示也跟着没了、无从排查。
        probe, probe_err = probe_anchor(cur)
        if probe is None:
            # sec_uid 且无 web_rid:不误判开播/冷门,明确提示等待补充
            if not warned_no_probe:
                log(f"[!] {name}: {probe_err}")
                warned_no_probe = True
            state.set_streamer(name, _snap(name, anchor_now, "缺web_rid", store, now, 120,
                                           {"error": probe_err}, probe=""))
            return "noprobe", 120

        if is_dormant(cfg, name, name in _recordings) and not force:
            dorm = float((cfg.get("schedule") or {}).get("cold_interval", 1800))
            state.set_streamer(name, _snap(name, anchor_now, "未监测", store, now, None,
                                           None, probe))
            return "dormant", dorm

        if now < cooldown_until:
            state.set_streamer(name, _snap(name, anchor_now, "冷却", store, None,
                                           cooldown_until - now, None, probe))
            return "cooldown", cooldown_until - now

        in_hot = store.in_hotspot(anchor_now, now)
        result = None
        try:
            # 把「当前是否处于热点窗口」传给检测层:Cookie 护栏据此决定
            # 这次是否值得动用真实账号的登录态(详见 cookie_guard.py)。
            result = await check(client, probe, cfg, last_room_id,
                                 in_hotspot=in_hot, guard=_cookie_guard)
        except Exception as e:
            result = {"is_live": False, "error": str(e)}

        _note_network_result(now, bool(result.get("error")))
        if result.get("error"):
            err_count += 1
            log(f"[!] {name} 异常 {result['error']} (连续 {err_count})")
            if err_count >= int(cfg["schedule"].get("circuit_errors", 5)):
                cooldown_until = now + int(cfg["schedule"].get("circuit_cooldown", 1800))
                log(f"[熔断] {name} 暂停 {cfg['schedule'].get('circuit_cooldown')}s")
                err_count = 0
            # 检测失败:只延长见证区间起点,不动房间/在播状态
            store.touch_session(anchor_now, now)
        else:
            err_count = 0
            if result.get("is_live"):
                room_id = result.get("room_id")
                if room_id and room_id != last_room_id:
                    last_room_id = room_id
                    # prev_meta 为「本次检测之前」的持久化会话状态(重启前/上一轮),
                    # 用于 S3 防污染判定与 S2 见证区间
                    _record_open(store, name, anchor_now, result,
                                 store.get_session(anchor_now), now, cfg)
                    log(f"[开播] {name} room_id={room_id} (via {result.get('method')})")
                    ctx = {
                        "name": name, "anchor": anchor_now, "web_rid": probe, "room_id": room_id,
                        "sec_uid": result.get("sec_uid", anchor_now),
                        "stream_url": result.get("stream_url") or "",
                        "room_url": f"https://live.douyin.com/{probe}",
                    }
                    run_command(state, cfg.get("on_live_command", ""), ctx)
                recording = True
                if result.get("stream_url"):
                    _last_stream_url[name] = result["stream_url"]
                # 自动录制开关:关掉的主播只检测、不自动录(需要时前端手动点
                # 「开始」)。仍在录的(手动开的)继续交给 _refresh_recording,
                # 这样断流自检/重启对手动录制同样生效。
                if name not in _manual_paused and (
                        auto_record_enabled(cfg, name) or name in _recordings):
                    _refresh_recording(state, name, result.get("stream_url"), cfg, room_id)
                # 截图(头像)与录制是两件事:关掉自动录制也照常补头像。
                # 弹幕会话依附录制输出,没录自然起不来(_ensure_danmaku 内部判断)。
                _ensure_preview_capture(state, name)
                _ensure_danmaku(state, name, room_id, cfg)
            else:
                if recording or last_room_id is not None:
                    log(f"[下播] {name}")
                    output_dir = _resolve_output_dir(cfg)
                    ctx = {"name": name, "anchor": anchor_now, "web_rid": probe, "output_dir": output_dir}
                    run_command(state, cfg.get("on_offline_command", ""), ctx)
                    # NAS 启用时入队归档(on_offline_command 仍照常执行,作逃生通道)
                    if (cfg.get("nas") or {}).get("enabled", False):
                        # src = 本场次的日期目录(<输出根>/<主播>/<YYYY-MM-DD>/),
                        # 从当前录制模板路径反推;date 供归档端构建同层级目录。
                        rec = _recordings.get(name) or {}
                        session_dir = (os.path.dirname(rec["output_path"])
                                       if rec.get("output_path")
                                       else os.path.join(output_dir, name,
                                                         time.strftime("%Y-%m-%d")))
                        # 无实际录制文件时目录不存在,直接跳过入队(否则重试 5 次后作废,污染队列)
                        if os.path.isdir(session_dir):
                            nas_mod.enqueue({"streamer": name, "src_path": session_dir,
                                             "room_id": last_room_id, "start": None,
                                             "date": os.path.basename(session_dir)})
                            _mark_archive_state(session_dir, "pending")
                        else:
                            log(f"[归档] {name} 本场无录制文件"
                                f"({os.path.basename(session_dir)}),跳过入队")
                    last_room_id = None
                recording = False
                _manual_paused.discard(name)  # 下播后清除暂停标记,下次开播恢复正常录制
                _rec_giveup_until.pop(name, None)  # 下播即解除冷却,下次开播正常录
                _stop_recording(state, name)
                _stop_danmaku(name, cfg)
                _cancel_preview_capture(name)
            # 会话状态落盘:供重启后恢复(房间号/是否在播/本轮检测时刻)
            store.set_session(anchor_now, last_room_id,
                              bool(result.get("is_live")), now)

        store.decay()
        interval = next_interval(recording, in_hot, cfg)
        phase = "直播中" if recording else ("热点" if in_hot else "冷门")
        ni = interval if next_in_hint is None else next_in_hint
        state.set_streamer(name, _snap(name, anchor_now, phase, store, now, ni, result, probe))
        return "ok", interval

    while True:
        kind, value = await once()
        if kind == "noprobe":
            await asyncio.sleep(value)
            continue
        if kind == "cooldown":
            await asyncio.sleep(min(value, 5))
            continue

        # 正常检测完成:等待到下次自动检测,期间响应手动刷新(不改变 deadline)
        deadline = time.time() + value
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            if state.take_refresh(name):
                # 手动刷新:立即检测一次,不重置 deadline,不影响下次自动检测时间。
                # force=True —— 休眠中的主播也要能被手动唤醒查一次(不然关掉自动
                # 录制后就再也查不到他在不在播了)。
                rk, _ = await once(next_in_hint=max(0.0, deadline - time.time()), force=True)
                if rk in ("noprobe", "cooldown"):
                    break  # 回到外层循环按特殊状态处理
                continue
            await asyncio.sleep(min(0.5, remain))


async def supervise(client, mon, state, store):
    """协程守护:捕获异常按指数退避重启,退避中上报「异常重启」。"""
    name = mon.get("name", "?")
    anchor = str(mon.get("anchor", ""))
    backoff = 60  # 1min
    # 立即上报初始状态,保证 UI 增删主播尽快可见
    state.set_streamer(name, _snap(name, anchor, "启动中", store, None, None, None))
    while True:
        try:
            await run_streamer(client, mon, state, store)
            backoff = 60  # 正常退出才复位(实际 run_streamer 为无限循环)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[守护] {name} 协程异常: {e},{backoff}s 后重启")
            state.set_streamer(name, _snap(name, anchor, "异常重启", store, None, backoff, {"error": str(e)}))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)  # 1→2→4→8→10min 上限


async def reconcile(client, state, store):
    """每 5s 比对运行任务集合与 config.monitors:增启删取消;顺带轮询命令退出码。"""
    tasks = {}  # (name, anchor) -> asyncio.Task
    while True:
        cfg = state.get_config()
        desired = {}
        for m in cfg.get("monitors", []):
            key = (m.get("name", "?"), str(m.get("anchor", "")))
            desired[key] = m

        # 删除不再需要的任务
        for key in list(tasks):
            if key not in desired:
                task = tasks.pop(key)
                name, anchor = key
                snap = state.get_status()["streamers"].get(name)
                if snap and snap.get("is_live"):
                    run_command(state, cfg.get("on_offline_command", ""), {"name": name, "anchor": anchor})
                task.cancel()
                _stop_recording(state, name)
                _stop_danmaku(name, cfg)
                _cancel_preview_capture(name)
                store.forget(anchor)  # 清理会话状态(窗口/事件保留,便于重新添加后延续)
                state.remove_streamer(name)
                log(f"[移除] 停止监控: {name} ({anchor})")

        # 新增任务
        for key, m in desired.items():
            if key not in tasks:
                tasks[key] = asyncio.create_task(supervise(client, m, state, store))
                log(f"[新增] 启动监控: {m.get('name')} ({m.get('anchor')})")

        poll_commands(state)
        # 每 5s 刷新录制时长 + 维持截图任务(直播中且未设头像时保证备选图不中断;
        # 覆盖「用户删除头像后重新截图」「截图任务结束后自动补」场景)
        for rn in list(_recordings):
            _sync_recording_state(state, rn)
            _ensure_preview_capture(state, rn)
        await asyncio.sleep(5)


async def daily_analysis_loop(store):
    """每日 analysis_hour(默认 05:00)全量分析:修剪事件日志 + 重算所有主播热点窗口。

    偏移触发的即时分析在 _record_open → record_open 内完成;
    这里兜底「缓慢漂移」——每次偏离 ≤ 阈值时实时层只做合并,不会触发即时分析,
    只有每日全量重算(近 3 天事件加权 ×2)才能把中心逐步拉到新时段。
    """
    last_date = None
    while True:
        try:
            health_mod.beat("analysis")
            hour = int(store.cfg.get("analysis_hour", 5))
            now = time.time()
            today = time.strftime("%Y-%m-%d")
            if time.localtime(now).tm_hour == hour and last_date != today:
                last_date = today
                store.prune_events(now)
                anchors = set(store.events.keys()) | {
                    a for a in store.data.keys() if a != "_meta"}
                n = 0
                for a in sorted(anchors):
                    if store.analyze(a, now):
                        n += 1
                log(f"[分析] 每日热点分析完成:检查 {len(anchors)} 个主播,重算 {n} 个")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[分析] 每日分析异常: {e}")
        await asyncio.sleep(60)


async def danmaku_patrol_loop(state):
    """每 60s 弹幕对齐巡检:锚定新分片、检测视频停滞/弹幕缺口、重生成 .ass。

    分片 ↔ 字幕一一对应(同目录同名 .ass),每片锚自己的首包墙钟,
    断链/缺失不会向后传播错位;异常写入该场次的 align.json。
    """
    while True:
        try:
            health_mod.beat("danmaku")
            cfg = state.get_config()
            if (cfg.get("danmaku") or {}).get("enabled", False):
                for name in list(_danmaku_paths):
                    dm = danmaku_mod.get(name)
                    if dm is None or dm.dead:
                        continue
                    rec = _recordings.get(name)
                    if rec:  # ffmpeg 重启产生新前缀 → 切换 jsonl
                        danmaku_mod.retarget_for(name, rec["output_path"])
                        _danmaku_paths[name] = rec["output_path"]
                    out = _danmaku_paths.get(name)
                    if not out:
                        continue
                    _, anomalies = subtitle_mod.patrol(
                        out, cfg, drift_ms=dm.clock_drift_ms,
                        last_msg_ts=dm.last_msg_ts)
                    for a in anomalies:
                        log(f"[弹幕] {name} 巡检: {a}(已记录到 align.json)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[弹幕] 巡检异常: {e}")
        await asyncio.sleep(60)


async def retention_loop(state):
    """孤儿主播目录延迟清理:启动时先跑一次,之后每 6 小时一次。

    主播卡片删掉后不立刻清目录 —— 先登记进 spool/retention.json,满 30 天
    且确认内容已归档才真正删除;期间主播被加回来会自动撤销。只清理项目内的
    previews/ 与 recordings/,NAS 上的归档副本不动。
    """
    while True:
        try:
            health_mod.beat("retention")
            cfg = state.get_config()
            r = retention_mod.run_due(cfg, list(_history), log=log)
            m, s = r["mark"], r["sweep"]
            if m["added"]:
                log(f"[清理] 发现孤儿目录,已登记(满 {retention_mod.GRACE_DAYS} 天后清理): "
                    + "、".join(m["added"]))
            if m["revoked"]:
                log(f"[清理] 主播已重新添加,取消清理: " + "、".join(m["revoked"]))
            if s["deleted"]:
                log(f"[清理] 已清理到期孤儿目录: " + "、".join(s["deleted"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[清理] 孤儿目录巡检异常: {e}")
        await asyncio.sleep(retention_mod.CHECK_INTERVAL)


async def main():
    global _web_state, _cookie_guard, _http_client
    _register_signal_handlers()
    config = load_config()
    store = HotspotStore(HOTSPOT_PATH, config.get("schedule", {}), HOTSPOT_EVENTS_PATH)
    state = State(CONFIG_PATH, config, store)
    # Cookie 护栏:全局唯一实例,规则随配置热更新(见 _sync_cookie_guard)
    _cookie_guard = CookieGuard(config)
    state.set_cookie_guard(_cookie_guard)
    # 注入前端 state 引用:此后 history 的每次变更都会同步给前端
    _web_state = state
    _load_history()
    state.set_history(_history)
    # 重新定位:把本机已不在(多为已归档)的历史条目指向归档目标中的实际位置
    try:
        n = _relocate_history(config)
        if n:
            log(f"[历史] 重新定位 {n} 条归档记录(已指向归档目标)")
    except Exception as e:
        log(f"[历史] 重新定位失败: {e}")
    state.set_history(_history)
    # 注入录制手动控制回调(供 webui 停止/开始录制)
    state.set_rec_ctrl({
        "stop": lambda name: _stop_recording_manual(state, name),
        "start": lambda name: _start_recording_manual(state, name),
    })
    # 注入归档结果回调(供 nas 模块回写 history 的归档状态)
    nas_mod.set_archive_hook(_on_archive_result)
    host = config.get("webui", {}).get("host", "127.0.0.1")
    port = int(os.environ.get("DOUYIN_MONITOR_PORT", config.get("webui", {}).get("port", 8780)))
    start(state, host, port)
    log(f"Web UI: http://{host}:{port}")

    monitors = config.get("monitors", [])
    log(f"监控 {len(monitors)} 个主播")

    # 启动自检摘要(B2)
    for c in run_checks(config):
        if c["status"] != "ok":
            log(f"[自检] {c['status']}: {c['title']} — {c['detail']}")

    if PURGED_PROXY_VARS:
        log(f"[自检] 已清除 {PURGED_PROXY_VARS} 个环境代理变量"
            f"(抖音检测与拉流需直连;确需代理请设 DOUYIN_KEEP_ENV_PROXY=1)")

    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
                                 trust_env=False) as client:
        _http_client = client      # 供矛盾状态校验复用,避免每场都新建连接
        nas_task = asyncio.create_task(nas_mod.nas_watch(state))
        analysis_task = asyncio.create_task(daily_analysis_loop(store))
        patrol_task = asyncio.create_task(danmaku_patrol_loop(state))
        retention_task = asyncio.create_task(retention_loop(state))
        # 录制健康巡检(20s):独立于检测周期,及时发现「进程存活但无数据」的假在线
        watchdog_task = asyncio.create_task(_recording_watchdog(state))
        # 登录态巡检(默认 12h ± 2h 抖动):失效只影响检测通道,录制不受影响
        # 注意:不能写 `noop = asyncio.sleep(0)` 再在三元里二选一 —— 未被选中的那个
        # 协程对象永远不会被 await,退出时会刷 "coroutine was never awaited" 警告。
        # 条件表达式只会求值其中一个分支,故直接内联。
        auth_task = asyncio.create_task(
            auth_mod.patrol_loop(state, log)
            if (auth_mod and hasattr(auth_mod, "patrol_loop"))
            else asyncio.sleep(0))
        try:
            await reconcile(client, state, store)
        except asyncio.CancelledError:
            nas_task.cancel()
            analysis_task.cancel()
            patrol_task.cancel()
            watchdog_task.cancel()
            auth_task.cancel()
            retention_task.cancel()
            raise
        except Exception as e:
            # 顶层兜底:log 后 re-raise,交给 launchd 拉起
            log(f"[致命] 监控主循环异常: {e}")
            raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
