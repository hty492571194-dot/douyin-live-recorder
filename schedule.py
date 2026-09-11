#!/usr/bin/env python3
"""热点窗口存储 + 开播事件日志 + 自适应轮询间隔调度。

三层结构(解决「重启时已在播导致开播时间被污染」与「开播时段漂移」):

- 实时层(hotspots.json 主体): 每主播最多 hotspot_max 个窗口,供运行时冷/热调度判断。
  新开播按环形距离在 hotspot_merge_hours(默认 5h)内并入现有窗口,合并取较早时刻
  (保守策略:防止晚开播/重启污染把中心往后拖)。
- 分析层(hotspot_events.json): 原始开播事件日志(默认保留 7 天)。两个触发点:
  (a) 新事件偏离现有窗口中心超过 deviation_trigger_minutes(默认 60min) → 立即分析;
  (b) 每日 analysis_hour(默认 05:00)全量分析兜底「缓慢漂移」。
  分析 = 90min 环形聚类 + 近 3 天事件加权(recency_weight, 默认 ×2),取强度 Top-2
  覆盖实时层;7 天内事件不足 analysis_min_events(默认 5)则跳过,保留现槽位
  (防止偶发事件覆盖稳定模式)。
- 会话层(hotspots.json 的 _meta 段): 持久化每主播 (last_room_id, was_live, last_check_ts),
  重启后恢复,用于「重启时已在播」防污染判定与新开播时间的见证区间估计。
"""
import json
import os
import random
import time
from datetime import datetime

MINUTE = 60
META_KEY = "_meta"  # hotspots.json 中会话状态段,key 不会与主播 anchor 冲突


def _minute_of_day(ts: float) -> int:
    dt = datetime.fromtimestamp(ts)
    return dt.hour * 60 + dt.minute


def _minute_distance(a: int, b: int) -> int:
    d = abs(a - b)
    return min(d, 1440 - d)  # 跨零点取最短距离


def _unwrap_near(base: int, m: int) -> int:
    """把分钟 m 环形解到 base 同侧(用于同簇/同窗口内的平均与比较)。"""
    if m - base > 720:
        return m - 1440
    if base - m > 720:
        return m + 1440
    return m


class HotspotStore:
    """按主播持久化热点窗口(以"当天分钟数"为中心, 跨零点取模)+ 事件日志 + 会话状态。"""

    def __init__(self, path: str, cfg: dict, events_path: str = None):
        self.path = path
        self.events_path = events_path or (path + ".events.json")
        self.cfg = cfg
        self.data = {}
        self.events = {}
        self._load()
        self._load_events()

    # --- 持久化 ---

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if not isinstance(self.data, dict):
                    self.data = {}
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _load_events(self):
        if os.path.exists(self.events_path):
            try:
                with open(self.events_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    # 兼容旧格式:纯时间戳列表 → 统一为对象
                    for a, evs in raw.items():
                        if isinstance(evs, list):
                            self.events[a] = [
                                e if isinstance(e, dict) else {"ts": float(e), "src": "legacy", "seen": float(e)}
                                for e in evs if isinstance(e, (int, float, dict))
                            ]
            except (OSError, json.JSONDecodeError):
                self.events = {}

    def _save_events(self):
        tmp = self.events_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.events, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.events_path)
        except OSError:
            pass

    # --- 会话层(重启防污染 + 见证区间) ---

    def get_session(self, anchor: str):
        """返回 {last_room_id, was_live, last_check_ts} 或 None。"""
        return (self.data.get(META_KEY) or {}).get(anchor)

    def set_session(self, anchor: str, room_id, is_live: bool, now: float):
        """每轮检测完成后调用:完整落盘该主播的会话状态。"""
        meta = self.data.setdefault(META_KEY, {})
        meta[anchor] = {"last_room_id": room_id, "was_live": bool(is_live), "last_check_ts": now}
        self.save()

    def touch_session(self, anchor: str, now: float):
        """检测异常时只延长见证区间(不动房间/在播状态)。"""
        m = (self.data.get(META_KEY) or {}).get(anchor)
        if not m:
            self.set_session(anchor, None, False, now)
            return
        m["last_check_ts"] = now
        self.save()

    def forget(self, anchor: str):
        """主播被移除时清理会话状态(窗口/事件保留,便于重新添加后延续)。"""
        meta = self.data.get(META_KEY)
        if isinstance(meta, dict) and anchor in meta:
            del meta[anchor]
            self.save()

    # --- 事件日志 + 实时层 ---

    def has_data(self, anchor: str) -> bool:
        """该主播是否已有热点数据(窗口或事件),用于「新主播豁免」判定。"""
        return bool(self.data.get(anchor)) or bool(self.events.get(anchor))

    def record_open(self, anchor: str, start_ts: float, source: str, now: float = None):
        """记录一次开播:写事件日志 + 更新实时层窗口。

        返回 "analyzed"(触发并完成了分析层重算)或 "merged"(仅实时层合并/新建)。
        偏离现有窗口中心 > deviation_trigger_minutes 时先尝试立即分析;
        分析被跳过(事件不足)则回落到实时层合并,保证不丢槽位。
        """
        now = now if now is not None else time.time()
        self.events.setdefault(anchor, []).append(
            {"ts": float(start_ts), "src": source, "seen": now})
        self.prune_events(now)
        self._save_events()

        minute = _minute_of_day(start_ts)
        ws = self.data.get(anchor) or []
        dev = int(self.cfg.get("deviation_trigger_minutes", 60))
        if ws:
            dist = min(_minute_distance(minute, w["center"]) for w in ws)
            if dist > dev and self.analyze(anchor, now):
                return "analyzed"
        self._merge_realtime(anchor, minute, now)
        return "merged"

    def _merge_realtime(self, anchor: str, minute: int, now: float):
        """实时层合并:merge_hours 内并入现有窗口(取较早),否则新建槽位。"""
        half = int(self.cfg.get("hotspot_half_width", 1800)) // MINUTE
        merge = int(float(self.cfg.get("hotspot_merge_hours", 5)) * MINUTE)
        mx = max(1, int(self.cfg.get("hotspot_max", 2)))
        ws = self.data.setdefault(anchor, [])
        for w in ws:
            if _minute_distance(minute, w["center"]) <= merge:
                m = _unwrap_near(w["center"], minute)
                # 合并取较早:保守防污染;精确中心由分析层重算
                w["center"] = min(w["center"], m) % 1440
                w["last_hit"] = now
                w["hits"] = w.get("hits", 0) + 1
                self.save()
                return
        ws.append({"center": minute, "half": half, "last_hit": now, "hits": 1})
        ws.sort(key=lambda w: w["last_hit"], reverse=True)
        del ws[mx:]
        self.save()

    # --- 分析层 ---

    def prune_events(self, now: float = None):
        """淘汰超过 event_retention_days 的开播事件。返回是否有变化。"""
        now = now if now is not None else time.time()
        cutoff = now - int(self.cfg.get("event_retention_days", 7)) * 86400
        changed = False
        for a in list(self.events):
            keep = [e for e in self.events[a] if e.get("ts", 0) >= cutoff]
            if len(keep) != len(self.events[a]):
                changed = True
            if keep:
                self.events[a] = keep
            else:
                del self.events[a]
                changed = True
        if changed:
            self._save_events()
        return changed

    def analyze(self, anchor: str, now: float = None) -> bool:
        """环形聚类重算该主播的热点窗口,成功覆盖实时层返回 True。

        - 事件窗口:近 event_retention_days 天
        - 聚类:按分钟排序后,相邻事件环形距离 <= analysis_cluster_min 归入同簇,
          首尾再环形合并一次(处理跨零点)
        - 加权:近 3 天事件权重 × recency_weight,权重和即簇强度
        - 事件数 < analysis_min_events → 跳过(保留现槽位,防偶发覆盖稳定模式)
        """
        now = now if now is not None else time.time()
        min_events = int(self.cfg.get("analysis_min_events", 5))
        cluster_min = int(self.cfg.get("analysis_cluster_min", 90))
        rw = float(self.cfg.get("recency_weight", 2))
        half = int(self.cfg.get("hotspot_half_width", 1800)) // MINUTE
        retention = int(self.cfg.get("event_retention_days", 7))

        evs = [e for e in self.events.get(anchor, [])
               if now - e.get("ts", 0) <= retention * 86400]
        if len(evs) < min_events:
            return False

        evs.sort(key=lambda e: _minute_of_day(e["ts"]))
        clusters = []
        for e in evs:
            m = _minute_of_day(e["ts"])
            if clusters and (m - clusters[-1][-1][0]) % 1440 <= cluster_min:
                clusters[-1].append((m, e))
            else:
                clusters.append([(m, e)])
        # 首尾环形邻接 → 合并(如 23:50 与 00:10)
        if len(clusters) >= 2 and \
                (clusters[0][0][0] - clusters[-1][-1][0]) % 1440 <= cluster_min:
            clusters[0] = clusters[-1] + clusters[0]
            clusters.pop()

        scored = []
        for cl in clusters:
            base = cl[0][0]
            acc = wsum = 0.0
            for m, e in cl:
                w = rw if now - e["ts"] <= 3 * 86400 else 1.0
                acc += _unwrap_near(base, m) * w
                wsum += w
            scored.append({"center": int(round(acc / wsum)) % 1440,
                           "strength": wsum, "events": len(cl)})
        scored.sort(key=lambda c: c["strength"], reverse=True)

        mx = max(1, int(self.cfg.get("hotspot_max", 2)))
        self.data[anchor] = [{"center": c["center"], "half": half,
                              "last_hit": now, "hits": c["events"],
                              "strength": round(c["strength"], 1)}
                             for c in scored[:mx]]
        self.save()
        return True

    # --- 查询 ---

    def in_hotspot(self, anchor: str, now: float) -> bool:
        minute = _minute_of_day(now)
        for w in self.data.get(anchor, []):
            if _minute_distance(minute, w["center"]) <= w.get("half", 30):
                return True
        return False

    def decay(self):
        """淘汰超过 decay_days 未命中的热点窗口;同时清理 30 天未活动的会话状态。"""
        limit = int(self.cfg.get("hotspot_decay_days", 14)) * 86400
        now = time.time()
        changed = False
        for anchor in list(self.data.keys()):
            if anchor == META_KEY:
                continue
            keep = [w for w in self.data[anchor] if now - w.get("last_hit", 0) <= limit]
            if len(keep) != len(self.data[anchor]):
                changed = True
            if keep:
                self.data[anchor] = keep
            else:
                del self.data[anchor]
                changed = True
        meta = self.data.get(META_KEY)
        if isinstance(meta, dict):
            keep_meta = {a: v for a, v in meta.items()
                         if now - v.get("last_check_ts", 0) <= 30 * 86400}
            if len(keep_meta) != len(meta):
                self.data[META_KEY] = keep_meta
                changed = True
        if changed:
            self.save()

    def windows(self, anchor: str):
        return self.data.get(anchor, [])


def next_interval(recording: bool, in_hot: bool, cfg: dict) -> float:
    """按阶段返回下次轮询间隔(秒),带随机抖动。"""
    s = cfg.get("schedule", cfg)
    if recording:
        base = float(s.get("live_interval", 1200))
        return base * (1 + random.uniform(-0.2, 0.2))
    if in_hot:
        return random.uniform(float(s.get("hot_min", 300)), float(s.get("hot_max", 360)))
    base = float(s.get("cold_interval", 1800))
    return base * (1 + random.uniform(-float(s.get("cold_jitter", 0.2)), float(s.get("cold_jitter", 0.2))))
