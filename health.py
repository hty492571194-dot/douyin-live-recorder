#!/usr/bin/env python3
"""组件健康心跳登记 —— 「系统健康」面板(方案 B)的唯一数据源。

只做三件事:声明组件、登记心跳、按阈值出快照。**不做任何自愈动作**
(不重启、不重连、不写日志),避免健康检查本身成为新的故障源。

为什么需要它
------------
2026-09-07~09 出过一次典型静默故障:服务进程活着、Web UI 秒开、NAS 显示
已挂载,但检测协程因为继承了失效代理全部失败,两天半没有产生任何录制。
红绿灯式的"进程在不在"完全抓不到这种情况 —— 唯一暴露它的是
**「最后一次成功执行距今多久」**。所以本模块以心跳老化为核心判据。

判定口径
--------
    age = now - 最后心跳
    age <= grace            → ok    (正常)
    grace < age <= grace*2  → warn  (延迟,可能是长周期任务或偶发阻塞)
    age >  grace*2          → down  (基本可判定卡死)
    从未打点                → idle  (启动宽限内) 或 down

grace 一律按最坏情况取(轮询周期 + 抖动 + 退避上限 + 余量),宁可迟报不误报。
"""

import threading
import time

_LOCK = threading.Lock()
_BEATS = {}    # key -> 最后心跳时间戳(float)
_ERRORS = {}   # key -> 本轮异常说明(str),正常时移除
_GAUGES = {}   # key -> 附加数值(如活跃路数)
_START = time.time()

# 组件元数据表:key -> (分组, 显示名, 说明, grace 秒, 来源)
#   来源 beat   : 由常驻协程自己打点(本模块判定)
#   来源 inject : 由 webui 注入实时结果(进程层/外部依赖,不走心跳)
SPECS = {
    # ---- 进程层(inject) ----
    "main":       ("process", "监控主进程", "承载全部检测/录制逻辑的 Python 进程", 0, "inject"),
    "webui":      ("process", "Web 控制台", "8780 端口的 HTTP 服务", 0, "inject"),
    "launchd":    ("process", "自启托管", "launchd job,决定开机自启与崩溃拉起", 0, "inject"),
    "ffmpeg":     ("process", "录制进程", "ffmpeg 拉流子进程,路数随在播人数变化", 0, "inject"),
    # ---- 常驻协程(beat) ----
    "detect":     ("tasks", "主播检测", "每主播一个协程,轮询开播状态", 2100, "beat"),
    "watchdog":   ("tasks", "录制看门狗", "20s 检查在录文件是否仍在增长", 180, "beat"),
    "danmaku":    ("tasks", "弹幕/字幕巡检", "60s 对齐分片、检测缺口、重生成 ass", 300, "beat"),
    "archive":    ("tasks", "归档巡检", "60s 消化归档队列(失败时退避至 600s)", 900, "beat"),
    "retention":  ("tasks", "孤儿目录清理", "6h 扫描并清理到期的主播目录", 23400, "beat"),
    "analysis":   ("tasks", "热点分析", "60s 轮询,每日 analysis_hour 全量重算", 300, "beat"),
    "auth":       ("tasks", "登录态守护", "12h±2h 校验一次 Cookie", 54000, "beat"),
    # ---- 外部依赖(inject) ----
    "nas_mount":  ("deps", "NAS 挂载", "归档目标是否可写", 0, "inject"),
    "cookie":     ("deps", "抖音登录态", "Cookie 是否有效,失效则检测退化为 302", 0, "inject"),
    "ffmpeg_bin": ("deps", "ffmpeg 可执行", "录制依赖的二进制文件", 0, "inject"),
}

GROUPS = [
    ("process", "进程"),
    ("tasks", "常驻任务"),
    ("deps", "外部依赖"),
]

# 启动宽限:进程刚起来时长周期任务(6h/12h)还没跑过第一轮,
# 这期间显示 idle(灰)而不是 down(红),否则每次重启都"一片红"。
STARTUP_GRACE = 300


def beat(key, error=None, count=None):
    """登记一次心跳。

    key   : SPECS 里的组件标识。未登记的 key 也能记,但不会出现在快照里。
    error : 本轮异常说明。给了就记录(面板显示黄灯并附原因),不给表示本轮正常。
    count : 附加数值(如活跃路数),面板上显示为「×N」。
    """
    now = time.time()
    with _LOCK:
        _BEATS[key] = now
        if count is not None:
            _GAUGES[key] = count
        if error:
            _ERRORS[key] = str(error)[:200]
        else:
            _ERRORS.pop(key, None)
    return now


def error(key, message):
    """只登记异常(不刷新心跳时间戳)。用于「本轮失败但协程还活着」。"""
    with _LOCK:
        _ERRORS[key] = str(message)[:200]


def reset():
    """清空全部登记(仅供测试与进程内重启场景使用)。"""
    global _START
    with _LOCK:
        _BEATS.clear()
        _ERRORS.clear()
        _GAUGES.clear()
        _START = time.time()


def started_at():
    return _START


def uptime(now=None):
    return max(0, int((now or time.time()) - _START))


def last_beat(key):
    with _LOCK:
        return _BEATS.get(key)


def _grace_for(key, cfg):
    """计算宽限秒数。detect 随配置动态变化(cold_interval 可达 30 分钟)。"""
    spec = SPECS.get(key)
    if spec is None:
        return 300
    base = spec[3]
    if key == "detect":
        sch = (cfg or {}).get("schedule") or {}
        vals = [float(sch.get("live_interval", 1200) or 0),
                float(sch.get("cold_interval", 1800) or 0),
                float(sch.get("hot_max", 360) or 0)]
        # +300 覆盖启动随机相位(≤60s)、熔断冷却后的补测、以及单轮超时
        return max([base] + vals) + 300
    return base


def _judge(key, now, grace, up):
    """返回 (status, detail)。detail 为 None 时用默认文案。"""
    with _LOCK:
        last = _BEATS.get(key)
        err = _ERRORS.get(key)
    if last is None:
        if up < STARTUP_GRACE:
            return "idle", "启动中,尚未产生首次心跳"
        return "down", "从未产生心跳"
    age = now - last
    if err:
        # 有异常但协程仍在打点:降级而非故障
        return "warn", err
    if age <= grace:
        return "ok", None
    if age <= grace * 2:
        return "warn", "心跳延迟(超过 %.0f 秒未执行)" % grace
    return "down", "心跳超时(超过 %.0f 秒未执行)" % (grace * 2)


def snapshot(cfg=None, overrides=None, now=None):
    """出一份分组快照。

    overrides: {key: {"status":..., "detail":..., "count":...}}
        inject 类组件的实时结果由调用方(webui)算好传进来;
        未提供的项显示 idle + "未接入"。
    """
    now = now if now is not None else time.time()
    up = uptime(now)
    over = overrides or {}
    groups = []
    counts = {"ok": 0, "warn": 0, "down": 0, "idle": 0}

    for gkey, gname in GROUPS:
        items = []
        for key, spec in SPECS.items():
            if spec[0] != gkey:
                continue
            _, name, desc, _, source = spec
            grace = _grace_for(key, cfg)
            with _LOCK:
                count = _GAUGES.get(key)
            if source == "inject":
                o = over.get(key) or {}
                status = o.get("status", "idle")
                detail = o.get("detail") or ("未接入" if status == "idle" else None)
                if o.get("count") is not None:
                    count = o["count"]
                last = o.get("last_beat")
                age = int(now - last) if last else None
            else:
                status, detail = _judge(key, now, grace, up)
                last = _BEATS.get(key)
                age = int(now - last) if last else None
            counts[status] = counts.get(status, 0) + 1
            items.append({
                "key": key, "name": name, "desc": desc,
                "status": status, "detail": detail,
                "last_beat": last, "age_s": age,
                "grace_s": int(grace), "count": count,
                "source": source,
            })
        # 组内排序:故障优先,然后 warn/idle/ok
        order = {"down": 0, "warn": 1, "idle": 2, "ok": 3}
        items.sort(key=lambda x: (order.get(x["status"], 9), x["name"]))
        groups.append({"key": gkey, "name": gname, "items": items})

    total = sum(counts.values())
    return {
        "generated_at": now,
        "uptime_s": up,
        "ok": counts["down"] == 0 and counts["warn"] == 0,
        "degraded": counts["warn"] > 0,
        "failed": counts["down"] > 0,
        "counts": counts,
        "total": total,
        "groups": groups,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
