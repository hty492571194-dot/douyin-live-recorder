#!/usr/bin/env python3
"""崩溃/故障事件登记表 —— 左侧导航告警条的唯一数据源。

为什么不能只靠 `health.snapshot()`
----------------------------------
`health.py` 回答的是「**现在**有没有组件不健康」,它按心跳老化判定,天然抓不到
**秒级恢复的故障**:ffmpeg 崩溃后 `_refresh_recording` 在同一秒就把它拉起来,
文件几乎不断、心跳也没缺席 —— 2026-10-22~29 的 86 次录制崩溃就是这样,页面上
一个字都看不到,`health` 全程 `ok`。所以需要的是**事件流**:「刚才崩过,崩在
哪里,崩了几次」,与「此刻是否健康」是两件事,两个都要有。

本模块只做三件事,不做任何自愈(不重启、不重连):
    ① `record()` 记一条事件(同类同主体在窗口内合并计数,避免刷屏)
    ② `recent()` / `summary()` 供接口与导航条读取
    ③ 落盘 + 主进程异常退出检测(重启后仍能看到「上次是崩着退出的」)

与 `health.py` 的分工(不要混):
    health    → 当前状态(ok/warn/down),按组件,内存态,重启即清零
    incidents → 历史事件(什么时候、崩在哪、几次),按事件,落盘,重启仍在

覆盖面由调用方保证:`monitor` 接入录制侧的 4 个崩溃点,`webui` 把 health 里
down/warn 的组件并进同一个告警条。凡是「出事」的地方都必须能在这里被看见。
"""
import json
import os
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(BASE, "state", "incidents.json")

# 事件级别:只有 error 会把导航条变红,其余变黄/灰
LEVEL_ERROR = "error"      # 崩溃,内容已丢或录制中断,必须人看
LEVEL_WARN = "warn"        # 降级/异常,服务还在跑,可能自己恢复
LEVEL_INFO = "info"        # 已恢复/提示性

# 事件类型 → 中文标签(前端只用 label,不硬编码 kind)
KIND_LABELS = {
    "record.crash":    "录制崩溃",
    "record.streamend": "流中断重连",
    "record.restart":  "录制重启",
    "record.giveup":   "放弃本场",
    "record.nostream": "拉流地址缺失",
    "record.startfail": "起录失败",
    "process.crash":   "主进程异常退出",
    "process.start":   "服务启动",
    "task.error":      "后台任务异常",
    "danmaku.crash":   "弹幕会话异常",
    "auth.expired":    "登录态失效",
    "archive.fail":    "归档失败",
}

# 落盘上限:只保留最近这么多条。崩溃是低频事件(最快 20s 一次),200 条够看一周。
MAX_EVENTS = 200
# 同类合并窗口:同一 key 在这段时间内重复发生只累加 count,不新增条目。
# 没有它,一次流地址失效能在几分钟内刷出几十条,把真正的问题埋掉。
MERGE_WINDOW = 300.0
# 写盘节流:事件本身是低频的(录制崩溃最快 20s 一次),而**落盘迟到等于丢证据** ——
# 进程往往是崩完才轮到写盘,所以这里不做「攒一攒再写」的优化:每次 record 立刻落盘,
# 50KB 的 JSON 写一次约 1ms,不值得为它冒「最关键的那条没写进去」的风险。
SAVE_MIN_INTERVAL = 0.0

_LOCK = threading.RLock()
_PATH = DEFAULT_PATH
_EVENTS = []          # 最新在前
_SEQ = 0
_SAVED_AT = 0.0
_LAST_SAVE = 0.0
_DIRTY = False
_SESSION_OPEN = False


def configure(path):
    """改落盘路径(测试注入临时目录用)。会清空内存态并重新加载。"""
    global _PATH, _EVENTS, _SEQ, _DIRTY, _LAST_SAVE
    with _LOCK:
        _PATH = path or DEFAULT_PATH
        _EVENTS = []
        _SEQ = 0
        _DIRTY = False
        _LAST_SAVE = 0.0
        _load()


def _load():
    """从磁盘载入事件(仅由 configure / begin_session 调用,持锁)。"""
    global _EVENTS, _SEQ
    if not _PATH or not os.path.exists(_PATH):
        _EVENTS = []
        return None
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # 损坏就当没有:告警表读不出来不该拖累启动
        return None
    evs = data.get("events")
    if isinstance(evs, list):
        clean = []
        for e in evs:
            if not isinstance(e, dict) or "ts" not in e:
                continue
            clean.append(e)
        clean.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
        _EVENTS = clean[:MAX_EVENTS]
        try:
            _SEQ = max([int(e.get("seq") or 0) for e in _EVENTS] + [0])
        except (TypeError, ValueError):
            _SEQ = 0
    return data


def _save_locked(force=False, clean_exit=None):
    """写盘(调用方持锁)。节流 + 原子替换。"""
    global _LAST_SAVE, _DIRTY
    now = time.time()
    if not force and not _DIRTY:
        return
    if not force and SAVE_MIN_INTERVAL and (now - _LAST_SAVE) < SAVE_MIN_INTERVAL:
        _DIRTY = True
        return
    payload = {"saved_at": now, "events": _EVENTS[:MAX_EVENTS]}
    if clean_exit is not None:
        payload["clean_exit"] = bool(clean_exit)
    elif os.path.exists(_PATH):
        # 保留上一次写下的 clean_exit:平时保存不该把它改掉
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and "clean_exit" in old:
                payload["clean_exit"] = old["clean_exit"]
        except (OSError, ValueError):
            pass
    try:
        os.makedirs(os.path.dirname(_PATH) or ".", exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _PATH)
        _LAST_SAVE = now
        _DIRTY = False
    except OSError:
        # 写不进去只影响「重启后还能不能看见」,不影响内存态与界面
        pass


def begin_session(now=None):
    """进程启动时调用:载入历史 + 检测上次是否非正常退出。

    判定靠磁盘上的 `clean_exit` 标记:`_handle_term` 干净退出时会写 True,
    所以读到 False 只有一种可能 —— 上次是崩着走的(未捕获异常、OOM、被 kill -9)。
    首次运行(文件不存在/无该字段)不报,否则每次全新部署都先挨一条假告警。
    """
    global _SESSION_OPEN
    with _LOCK:
        data = _load()
        _SESSION_OPEN = True
        crashed = None
        if isinstance(data, dict) and data.get("clean_exit") is False:
            prev = data.get("saved_at")
            crashed = record(
                "process.crash", "监控主进程异常退出",
                detail="上次运行没有走正常退出流程(未捕获异常/被强杀),"
                       "最后一次落盘在 %s" % (_fmt(prev) if prev else "未知"),
                level=LEVEL_ERROR, key="process.crash", now=now)
        # 立刻把 clean_exit 置 False:这次运行期间若崩掉,下次启动才认得出
        _save_locked(force=True, clean_exit=False)
        return crashed


def note_start(detail="", now=None):
    """记一条「服务启动」(info)。由 `monitor.main()` 在 begin_session 之后调用。

    刻意**不**塞进 `begin_session()`:那个函数只回答「上次是否崩着退出」,
    混进一条常规事件会让所有「重启后会多出什么」的测试基线漂移。
    合并键固定:5 分钟内反复重启只累加一条,不刷屏(用户要的是「重启看得见」,
    不是「重启把历史冲掉」)。
    """
    return record("process.start", "监控服务启动", detail=detail,
                  level=LEVEL_INFO, key="process.start", now=now)


def end_session(now=None):
    """干净退出时调用(SIGTERM/SIGINT 清理路径),标记下次启动不必报警。"""
    global _SESSION_OPEN
    with _LOCK:
        _SESSION_OPEN = False
        _save_locked(force=True, clean_exit=True)


def _fmt(ts):
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def record(kind, title, detail="", name="", level=LEVEL_ERROR,
           key=None, now=None):
    """记一条事件 → 返回事件 dict(合并时返回被更新的那条)。

    kind  : KIND_LABELS 里的类型(未登记的类型也能记,label 回落成 kind 本身)
    title : 一句话结论(导航条详情里直接显示)
    detail: 补充说明(原因/前后文),可以为空
    name  : 涉及的主播名(可空);同 key 合并靠它区分主体
    level : error / warn / info
    key   : 合并键。默认 `kind:name`;给 None 时自动拼。
            同一 key 在 MERGE_WINDOW 内重复 → count+1、ts 刷新、置为未读。
    """
    global _SEQ, _DIRTY
    now = float(now if now is not None else time.time())
    k = key if key is not None else "%s:%s" % (kind, name or "-")
    with _LOCK:
        # 先找可合并的(只在最前面几条里找:窗口外的老事件不该被复活)
        for ev in _EVENTS[:20]:
            if ev.get("merge_key") != k:
                continue
            if now - float(ev.get("ts") or 0) > MERGE_WINDOW:
                continue
            ev["count"] = int(ev.get("count") or 1) + 1
            ev["ts"] = now
            # title 跟着最新一次走:同一个合并键下,「数据停滞」比「进程退出」
            # 更能说明问题,界面得显示最新那次的原因。
            if title:
                ev["title"] = str(title)[:160]
            ev["detail"] = detail or ev.get("detail") or ""
            ev["level"] = level
            # 合并后重新置顶 + 未读:又崩了一次,不能因为"看过了"就沉默
            ev["read"] = False
            _EVENTS.remove(ev)
            _EVENTS.insert(0, ev)
            _DIRTY = True
            _save_locked()
            return ev
        _SEQ += 1
        ev = {
            "id": "evt-%d-%d" % (int(now * 1000), _SEQ),
            "seq": _SEQ,
            "ts": now,
            "first_ts": now,
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind),
            "level": level,
            "title": str(title or "")[:160],
            "detail": str(detail or "")[:400],
            "name": str(name or ""),
            "merge_key": k,
            "count": 1,
            "read": False,
        }
        _EVENTS.insert(0, ev)
        del _EVENTS[MAX_EVENTS:]
        _DIRTY = True
        _save_locked()
        return ev


def recent(limit=50, level=None, unread_only=False):
    """最近的事件(最新在前)。"""
    with _LOCK:
        out = []
        for ev in _EVENTS:
            if level and ev.get("level") != level:
                continue
            if unread_only and ev.get("read"):
                continue
            out.append(dict(ev))
            if len(out) >= int(limit or 50):
                break
        return out


def summary(now=None):
    """导航条要的聚合数字。"""
    now = float(now if now is not None else time.time())
    with _LOCK:
        err = [e for e in _EVENTS if e.get("level") == LEVEL_ERROR]
        warn = [e for e in _EVENTS if e.get("level") == LEVEL_WARN]
        unread = [e for e in _EVENTS if not e.get("read")]
        last = _EVENTS[0] if _EVENTS else None
        # 24h 内的次数(按条目计,不按合并后的 count,免得一个高频故障把视频刷成大数)
        day = [e for e in _EVENTS if now - float(e.get("ts") or 0) <= 86400]
        return {
            "events": len(_EVENTS),
            "error": len(err),
            "warn": len(warn),
            "unread": len(unread),
            "unread_error": len([e for e in unread if e.get("level") == LEVEL_ERROR]),
            "last_ts": (float(last["ts"])
                        if last and last.get("ts") else None),
            "last_level": last.get("level") if last else None,
            "last_title": last.get("title") if last else None,
            "last_age_s": (round(now - float(last.get("ts")), 1)
                           if last and last.get("ts") else None),
            "recent_24h": len(day),
            "merge_window_s": MERGE_WINDOW,
        }


def dismiss(ids=None):
    """标记已读。ids 为空 = 全部已读。返回受影响条数。"""
    global _DIRTY
    with _LOCK:
        n = 0
        if ids:
            want = set(str(x) for x in ids)
            for ev in _EVENTS:
                if str(ev.get("id")) in want and not ev.get("read"):
                    ev["read"] = True
                    n += 1
        else:
            for ev in _EVENTS:
                if not ev.get("read"):
                    ev["read"] = True
                    n += 1
        if n:
            _DIRTY = True
            _save_locked()
        return n


def clear():
    """清空全部事件(仅供测试)。"""
    global _EVENTS, _SEQ, _DIRTY
    with _LOCK:
        _EVENTS = []
        _SEQ = 0
        _DIRTY = True
        _save_locked(force=True)


def snapshot(limit=50):
    """接口一次性要的数据:聚合 + 事件列表 + 类型标签。"""
    return {
        "summary": summary(),
        "events": recent(limit=limit),
        "labels": dict(KIND_LABELS),
        "generated_at": time.time(),
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=1))
