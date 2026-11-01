#!/usr/bin/env python3
"""礼物展示规则引擎 —— 唯一真源（纯逻辑、零 IO）。

设计依据：`字幕礼物展示限流部署方案-竖屏版-2026-10-29.md`（下称"部署方案"）。
本模块只做"从原始礼物事件推导出该显示什么、显示在哪、显示多久"，
不读文件、不访问网络、不写 ASS；渲染由 `subtitle.py` 负责，实时预览由
`danmaku.py` 取 `build_live_display()` 的结果，下游消费 `.giftdisplay.json`。

核心规则（与部署方案逐条对应）：

1. 价格边界唯一：`slow_min_diamond = 99`
   —— `diamond <= 98` 快车道，`diamond >= 99` 慢车道，98 永远快、99 永远慢。
   未知价格保守进慢车道；官方字典里明确为 0 抖币的属于"已知价格"，不算未知。
2. 合并只发生在完整通道：同一 `(uid, gift_id)` 且相邻间隔 ≤ `merge_window_sec`
   —— 数量取连击累计 `count` 的**最大值**（累加会放大成 N(N+1)/2 倍）。
   匿名轻通道**永不合并**（uid 恒空，跨人聚合等于捏造送礼人）。
3. 份数升级：`count >= promote_min_count` 的小额礼物升级为慢车道视觉样式与
   普通慢优先级（只改语义，不改金额，不因到达时间不同而不同）。
4. 八槽位统一调度：上 2 槽是慢车道偏好、下 6 槽是快车道偏好，**都可借用**；
   借槽不改变样式/时长/优先级/文案。
5. 停留时长按"同类等待量"动态选取，已开始的项不回溯修改。
6. 全场只规划一次（跨分片由同一状态携带），展示项不跨片；
   放不下就推迟到下一片，最后一片仍放不下 → `tail_overflow`（可审计，不假装可见）。
"""

from __future__ import annotations

import math

RULE_VERSION = "1.0.0"
LAYOUT_PROFILE = "portrait-v1"

SLOW = "slow"
FAST = "fast"
P0, P1, P2 = 0, 1, 2

# 配置默认值（config.json → subtitle.gift_display 覆盖，缺失即用这里的值）
DEFAULTS = {
    "enabled": True,
    "layout_profile": LAYOUT_PROFILE,
    "slow_min_diamond": 99,          # >= 99 进慢车道；<= 98 进快车道
    "high_priority_diamond": 1000,   # >= 1000 为 P0
    "promote_min_count": 10,         # 份数 >= 10 升级为慢车道视觉
    "slots_total": 14,               # 同屏上限（8 行常用场景绰绰有余，密集时最多 14 行）
    "slow_preferred_slots": 3,
    "reference_width": 1080,
    "reference_height": 1920,
    "top_margin": 60,
    "side_margin": 48,
    # 行距按实际墨迹贴合：实测 38 号汉字的墨迹只有 25px（PingFang SC 约 0.66em），
    # 行高给 38 时相邻两行墨迹之间只留 ~13px 呼吸位；原来 54 的行高会留 22px，
    # 看着就像"每行之间的间隔比字还高"。慢/快共用一套行高（槽位要能互相借用）。
    "row_height": 38,
    "slow_font": 38,
    "fast_font": 30,
    "slow_font_min": 32,
    "fast_font_min": 26,
    # 礼物区总高硬上限（占画面高度比例）。多行叠加最多占 2/5，超出自动裁槽。
    "max_area_ratio": 0.4,
    "fast_hold_sec": 1.5,
    "fast_hold_mid_sec": 1.0,
    "fast_hold_busy_sec": 0.8,
    "fast_hold_min_sec": 0.6,
    "slow_hold_sec": 5.0,
    "slow_hold_busy_sec": 3.0,
    "slow_hold_min_sec": 2.5,
    "merge_window_sec": 3.0,
    "max_segments": 6,
    "min_segment_sec": 0.6,
    "slow_combo_max_sec": 8.0,
    "fast_combo_max_sec": 3.0,
    "unknown_price_lane": SLOW,
    "anonymous_merge": False,
}

_TUNING_KEYS = frozenset(DEFAULTS)


# ── 配置 ────────────────────────────────────────────────
def config(cfg=None):
    """取项目 config 里的 subtitle.gift_display，补全默认值。

    只接受已知键（未知键忽略，防止拼错的配置悄悄生效又不生效）。
    """
    raw = ((cfg or {}).get("subtitle") or {}).get("gift_display") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in _TUNING_KEYS and v is not None:
                out[k] = v
    return out


def enabled(cfg=None):
    """新版礼物规划是否启用（false 时上层回退到旧的单横幅路径）。"""
    return bool(config(cfg).get("enabled", True))


def layout_of(cfg):
    """礼物版式参数（基准像素，渲染层再按画布缩放）。

    槽数受两条约束同时限制：
      ① `slots_total`（想要的同屏行数上限）
      ② `max_area_ratio`（多行叠加最多占画面高度 2/5）→ 超出自动裁槽
    第二条是硬上限：换任何画幅/字号都不会把礼物区顶到弹幕区。
    """
    c = config(cfg) if not isinstance(cfg, dict) or "slow_min_diamond" not in cfg else cfg
    top = float(c["top_margin"])
    row_h = max(1.0, float(c["row_height"]))
    ref_h = int(c["reference_height"])
    max_area = float(c["max_area_ratio"]) * ref_h
    # 第 i 个槽的底边 = top + (i+1)*row_h，要求 ≤ max_area
    fit_slots = int((max_area - top) // row_h)
    rows = max(1, min(int(c["slots_total"]), fit_slots))
    return {
        "profile": c["layout_profile"],
        "ref_w": int(c["reference_width"]),
        "ref_h": ref_h,
        "top_margin": top,
        "side_margin": float(c["side_margin"]),
        "row_height": row_h,
        "slots": rows,
        "slot_y": [int(round(top + i * row_h)) for i in range(rows)],
        "area_bottom": int(round(top + (rows - 1) * row_h + row_h)),
        "max_area_ratio": float(c["max_area_ratio"]),
        "max_area_px": int(round(max_area)),
        "slow_font": int(c["slow_font"]),
        "fast_font": int(c["fast_font"]),
        "slow_font_min": int(c["slow_font_min"]),
        "fast_font_min": int(c["fast_font_min"]),
        "slow_preferred_slots": int(c["slow_preferred_slots"]),
    }


# ── 事件归一 ────────────────────────────────────────────
def _dict_info(gift_dict, gift_id):
    """从字典取 {name, diamond}；dict / 带 info() 的对象都兼容。"""
    if gift_dict is None or gift_id in (None, "", 0):
        return None
    fn = getattr(gift_dict, "info", None)
    if callable(fn):
        try:
            return fn(gift_id)
        except Exception:
            return None
    try:
        v = gift_dict.get(str(gift_id))
        if v is None:
            v = gift_dict.get(int(gift_id))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if isinstance(v, dict) else None


def gift_id_of(event, gift_dict=None):
    """从 jsonl 礼物事件里取 gift_id。

    匿名轻礼物写的是 `礼物#<id>`；若事件里是真实名称（完整通道），
    按名称在字典里反查——同名多 ID 时用单价排除，仍不唯一则放弃（返回 None）。
    """
    raw = str(event.get("gift") or "")
    if raw.startswith("礼物#"):
        tail = raw[3:].strip()
        return int(tail) if tail.isdigit() else None
    if gift_dict is None or not raw:
        return None
    finder = getattr(gift_dict, "find_by_name", None)
    if not callable(finder):
        return None
    try:
        ids = list(finder(raw) or [])
    except Exception:
        return None
    if len(ids) == 1:
        return int(ids[0])
    dia = event.get("diamond")
    if dia is None:
        return None
    same = [i for i in ids if int((_dict_info(gift_dict, i) or {}).get("diamond") or 0) == int(dia)]
    return int(same[0]) if len(same) == 1 else None


def decide_lane(diamond, price_known, cfg=None):
    """价格 → 视觉车道。98 快、99 慢；未知价格走 `unknown_price_lane`。"""
    c = config(cfg)
    if not price_known:
        return c["unknown_price_lane"]
    return FAST if int(diamond) < int(c["slow_min_diamond"]) else SLOW


def priority_of(diamond, price_known, count, cfg=None):
    """调度优先级：P0 高价值 → P1 慢/未知/升级 → P2 普通快车道。"""
    c = config(cfg)
    lane = decide_lane(diamond, price_known, c)
    if price_known and int(diamond) >= int(c["high_priority_diamond"]):
        return P0
    if lane == SLOW or int(count or 1) >= int(c["promote_min_count"]):
        return P1
    return P2


def normalize_event(event, cfg=None, gift_dict=None):
    """jsonl 礼物事件 → 展示用事件（不修改入参）。"""
    c = config(cfg)
    gid = gift_id_of(event, gift_dict)
    info = _dict_info(gift_dict, gid)
    ev_diamond = event.get("diamond")
    if info is not None:
        diamond = int(info.get("diamond") or 0)
        name = info.get("name") or str(event.get("gift") or f"礼物#{gid}")
        price_known = True
    else:
        diamond = int(ev_diamond or 0)
        name = str(event.get("gift") or (f"礼物#{gid}" if gid else "?"))
        price_known = diamond > 0
    count = int(event.get("count") or 1)
    uid = str(event.get("uid") or "")
    user = str(event.get("user") or "")
    channel = str(event.get("channel") or "")
    if channel not in (SLOW, FAST, "light"):
        # 老数据没有 channel：有送礼人即完整通道，否则匿名
        channel = "full" if uid else "light"
    if channel in (SLOW, FAST):
        channel = "full"
    lane = decide_lane(diamond, price_known, c)
    visual = SLOW if (lane == SLOW or count >= int(c["promote_min_count"])) else FAST
    return {
        "gift_id": gid,
        "name": name,
        "diamond": diamond,
        "price_known": bool(price_known),
        "count": count,
        "uid": uid,
        "user": user,
        "channel": channel,
        "lane": lane,                 # 价格车道（决定金额语义）
        "visual_lane": visual,        # 视觉车道（含份数升级，决定样式与偏好槽）
        "priority": priority_of(diamond, price_known, count, c),
        "ts": int(event.get("ts") or 0),
    }


# ── 文案 ────────────────────────────────────────────────
def render_text(ne, count=None):
    """统一文案：full=`昵称 送出 礼物名 ×N（金额 抖币）`；light=`礼物名 ×N`。

    未知价格显示 `（? 抖币）`；已知 0 抖币显示 `（0 抖币）`（免费礼物不当未知）。
    """
    n = int(ne.get("count") if count is None else count)
    name = ne.get("name") or "?"
    if ne.get("channel") == "full":
        who = ne.get("user") or ne.get("uid") or "某"
        money = f"{int(ne.get('diamond') or 0) * n} 抖币" if ne.get("price_known") else "? 抖币"
        return f"{who} 送出 {name} ×{n}（{money}）"
    return f"{name} ×{n}"


# ── 合并与展示项 ────────────────────────────────────────
def _combo_cap_ms(ne, cfg):
    """该展示项的连击总时长上限（慢 8s / 快 3s）。"""
    c = config(cfg)
    cap = c["slow_combo_max_sec"] if ne["visual_lane"] == SLOW else c["fast_combo_max_sec"]
    return int(float(cap) * 1000)


def _merge_key(ne, seq):
    """展示键。full 用 (uid, gift_id) 在会话内配对；light 每个事件唯一。"""
    if ne["channel"] == "full" and ne["uid"]:
        return f"full|{ne['uid']}|{ne['gift_id']}"
    return f"light|{seq}"


def build_display_items(events, cfg=None, gift_dict=None):
    """原始礼物事件流 → 展示项列表（按时间升序）。

    合并规则见模块头。返回的每项含：
      key / lane / visual_lane / priority / channel / merged / price_known /
      gift_id / name / diamond / count / ready_ms / combo_end_ms /
      source_event_indexes / segments(list of {t_ms,text}) / user / uid
    """
    c = config(cfg)
    merge_gap = int(float(c["merge_window_sec"]) * 1000)
    max_seg = int(c["max_segments"])
    min_seg_ms = int(float(c["min_segment_sec"]) * 1000)
    items = []
    open_by_key = {}
    for seq, ev in enumerate(events):
        if ev.get("type") != "gift":
            continue
        ts = int(ev.get("ts") or 0)
        if ts <= 0:
            continue
        ne = normalize_event(ev, c, gift_dict)
        key = _merge_key(ne, seq)
        cur = open_by_key.get(key) if ne["channel"] == "full" and ne["uid"] else None
        if cur is not None:
            cap_ms = _combo_cap_ms(ne, c)
            span = ts - cur["ready_ms"]
            # 超过连击窗口 / 超过车道总时长上限 → 另起一项
            if ts - cur["combo_end_ms"] <= merge_gap and span <= cap_ms:
                cur["count"] = max(cur["count"], ne["count"])   # 累计语义取最大值
                cur["combo_end_ms"] = ts
                cur["source_event_indexes"].append(seq)
                cur["priority"] = min(cur["priority"], ne["priority"])
                cur["_ne"] = ne
                # 分段只记"从哪一刻起数量变成多少"；间隔太短则不切段（数量跳变并入上一段）
                raw = cur["_raw_segments"]
                if ts - raw[-1]["t_ms"] >= min_seg_ms:
                    if len(raw) < max_seg:
                        raw.append({"t_ms": ts, "count": ne["count"]})
                    else:
                        raw[-1]["count"] = ne["count"]
                else:
                    raw[-1]["count"] = ne["count"]
                continue
        item = {
            "key": key,
            "channel": ne["channel"],
            "lane": ne["lane"],
            "visual_lane": ne["visual_lane"],
            "priority": ne["priority"],
            "gift_id": ne["gift_id"],
            "name": ne["name"],
            "diamond": ne["diamond"],
            "price_known": ne["price_known"],
            "count": ne["count"],
            "user": ne["user"],
            "uid": ne["uid"],
            "merged": False,
            "ready_ms": ts,
            "combo_end_ms": ts,
            "source_event_indexes": [seq],
            "segments": [],
            "_raw_segments": [{"t_ms": ts, "count": ne["count"]}],
            "_ne": ne,
        }
        items.append(item)
        if ne["channel"] == "full" and ne["uid"]:
            open_by_key[key] = item
    for it in items:
        it["merged"] = len(it["source_event_indexes"]) > 1
        raw = it.pop("_raw_segments")
        # 最后一段必须展示最终 count
        if raw and raw[-1]["count"] != it["count"]:
            raw[-1]["count"] = it["count"]
        ne = it.pop("_ne")
        it["segments"] = [{"t_ms": s["t_ms"], "text": render_text(ne, s["count"])}
                          for s in raw]
    items.sort(key=lambda it: (it["ready_ms"], it["source_event_indexes"][0]))
    return items


def shift_items(items, origin_ms):
    """把展示项的时间从绝对 epoch 毫秒换成会话相对毫秒（规划器统一口径）。

    `build_display_items` 产出的 `ready_ms/combo_end_ms/segments[].t_ms` 都是原始
    事件时间（epoch ms）；`plan_session_display` 的分片边界是"距会话起点多久"。
    两边必须同口径，否则每一项都会被误判成不在任何分片内 → 全进 tail_overflow。
    """
    off = int(origin_ms or 0)
    if not off:
        return items
    for it in items:
        it["ready_ms"] = int(it["ready_ms"]) - off
        it["combo_end_ms"] = int(it["combo_end_ms"]) - off
        for sg in it.get("segments") or []:
            sg["t_ms"] = int(sg["t_ms"]) - off
    return items


# ── 停留时长 ────────────────────────────────────────────
def hold_ms(lane, queue_len, cfg=None):
    """按同类等待队列长度选本次停留（部署方案 §6.2 的四档）。"""
    c = config(cfg)
    q = int(queue_len or 0)
    if lane == SLOW:
        if q <= 5:
            sec = c["slow_hold_sec"]
        elif q <= 12:
            sec = c["slow_hold_busy_sec"]
        else:
            sec = c["slow_hold_min_sec"]
    else:
        if q <= 3:
            sec = c["fast_hold_sec"]
        elif q <= 6:
            sec = c["fast_hold_mid_sec"]
        elif q <= 12:
            sec = c["fast_hold_busy_sec"]
        else:
            sec = c["fast_hold_min_sec"]
    return int(round(float(sec) * 1000))


# ── 全场调度 ────────────────────────────────────────────
def _slot_pref_lanes(cfg):
    c = config(cfg)
    n = int(c["slots_total"])
    slow_n = max(0, min(int(c["slow_preferred_slots"]), n))
    return [SLOW] * slow_n + [FAST] * (n - slow_n)


def _seg_of(segments, t_ms):
    """返回 (seg_index_in_list, seg_conf) —— t 落在哪个分片。"""
    for i, s in enumerate(segments):
        if s["start_ms"] <= t_ms < s["end_ms"]:
            return i, s
    return None, None


def plan_session_display(items, segments, cfg=None):
    """全场一次性调度：事件前缀稳定（已分配项不再改动）。

    items     : `build_display_items()` 的输出
    segments  : [{"idx": int, "start_ms": int, "end_ms": int}]（会话相对毫秒，
                按时间升序；`idx` 用于回填分片序号）

    返回 DisplayPlan dict（可直接 JSON 序列化）：
      {"rule_version", "layout_profile", "cfg", "segments":[{idx,items:[]}],
       "tail_overflow":[...], "stats":{...}}
    """
    c = config(cfg)
    segs = sorted((dict(s) for s in segments), key=lambda s: s["start_ms"])
    pref = _slot_pref_lanes(c)
    n_slots = len(pref)
    slot_free = [-(10 ** 15)] * n_slots        # 槽位空闲时刻（会话相对 ms）
    slot_lane = [None] * n_slots               # 该槽位最近一次被谁占用（紧凑堆叠要用）
    placed = []
    pending = sorted(enumerate(items),
                     key=lambda p: (p[1]["ready_ms"], p[1]["source_event_indexes"][0]))
    deferred = {}
    overflow = []
    now = pending[0][1]["ready_ms"] if pending else 0
    guard = 0
    hard_guard = max(1000, len(items) * 20)

    while pending:
        guard += 1
        if guard > hard_guard:      # 防御：绝不因逻辑漏洞死循环
            for _i, it in pending:
                overflow.append(_overflow_entry(it, "planner_guard"))
            pending = []
            break
        # 当前可分配（ready 已到）的项里，按 优先级 → ready → 原始序号 取一个
        ready = [p for p in pending if p[1]["ready_ms"] <= now]
        if not ready:
            now = pending[0][1]["ready_ms"]
            continue
        ready.sort(key=lambda p: (p[1]["priority"], p[1]["ready_ms"],
                                  p[1]["source_event_indexes"][0]))
        pos, it = ready[0]
        lane = it["visual_lane"]
        # 同类等待量 → 决定本次停留时长
        q = sum(1 for p in pending
                if p[1]["visual_lane"] == lane and p[1]["ready_ms"] <= now)
        base = hold_ms(lane, q, c)
        span_ms = max(0, it["combo_end_ms"] - it["ready_ms"])
        dur = span_ms + base
        cap = _combo_cap_ms({**it, "visual_lane": lane}, c)
        dur = min(dur, max(base, cap)) if span_ms else base

        start = max(now, it["ready_ms"])
        # 选槽：偏好槽优先，其次借用任意空槽；都忙则推进到最早空闲时刻
        chosen, borrowed = _pick_slot(pref, slot_free, slot_lane, lane, start)
        if chosen is None:
            now = min(slot_free)
            continue
        # 必须完整落在某一片内；放不下就推迟到下一片，末片放不下 → tail_overflow
        si, seg = _seg_of(segs, start)
        if seg is None:
            nxt = next((s for s in segs if s["start_ms"] > start), None)
            if nxt is None:
                overflow.append(_overflow_entry(it, "no_segment"))
                pending.remove((pos, it))
                continue
            deferred[it["source_event_indexes"][0]] = deferred.get(it["source_event_indexes"][0], 0) + 1
            if deferred[it["source_event_indexes"][0]] > len(segs) + 2:
                overflow.append(_overflow_entry(it, "defer_limit"))
                pending.remove((pos, it))
                continue
            it["ready_ms"] = nxt["start_ms"]
            now = min(now, it["ready_ms"])
            continue
        if start + dur > seg["end_ms"]:
            nxt = segs[si + 1] if si + 1 < len(segs) else None
            if nxt is None:
                overflow.append(_overflow_entry(it, "tail_video_end"))
                pending.remove((pos, it))
                continue
            key0 = it["source_event_indexes"][0]
            deferred[key0] = deferred.get(key0, 0) + 1
            if deferred[key0] > len(segs) + 2:
                overflow.append(_overflow_entry(it, "defer_limit"))
                pending.remove((pos, it))
                continue
            it["ready_ms"] = nxt["start_ms"]
            now = min(now, it["ready_ms"])
            continue

        slot_free[chosen] = start + dur
        slot_lane[chosen] = lane
        placed.append({
            "seg": seg["idx"],
            "slot": chosen,
            "borrowed_slot": bool(borrowed),
            "start_ms": start,
            "end_ms": start + dur,
            "ready_ms": it["ready_ms"],
            "delay_ms": start - it["ready_ms"],
            "lane": lane,
            "price_lane": it["lane"],
            "priority": it["priority"],
            "channel": it["channel"],
            "merged": it["merged"],
            "price_known": it["price_known"],
            "gift_id": it["gift_id"],
            "name": it["name"],
            "diamond": it["diamond"],
            "count": it["count"],
            "user": it["user"],
            "uid": it["uid"],
            "key": it["key"],
            "source_event_indexes": list(it["source_event_indexes"]),
            "segments": [dict(s) for s in it["segments"]],
        })
        pending.remove((pos, it))
        if not ready:
            now = min((p[1]["ready_ms"] for p in pending), default=now)

    by_seg = {}
    for p in placed:
        by_seg.setdefault(p["seg"], []).append(p)
    return {
        "rule_version": RULE_VERSION,
        "layout_profile": c["layout_profile"],
        "cfg": {k: c[k] for k in ("slow_min_diamond", "high_priority_diamond",
                                  "slots_total", "slow_preferred_slots",
                                  "promote_min_count", "merge_window_sec",
                                  "anonymous_merge")},
        "segments": [{"idx": s["idx"], "start_ms": s["start_ms"], "end_ms": s["end_ms"],
                      "items": by_seg.get(s["idx"], [])} for s in segs],
        "tail_overflow": overflow,
        "stats": _stats(items, placed, overflow, c),
    }


def _pick_slot(pref, slot_free, slot_lane, lane, t):
    """在 t 时刻选槽：**紧凑堆叠，不留空行**。

    旧实现按"偏好槽"挑（慢只在 0/1、快只在 3 以后），于是只来一行慢+一行快时
    中间必然空出两格，画面看着就是"行距特别宽"。改为连续堆叠：

    - 慢项：从 0 号槽起取第一个空闲槽 —— 大额永远贴着顶部。
    - 快项：紧接在当前**仍在显示**的慢项之后取第一个空闲槽（无慢项时从 0 起）
      —— 小额自然贴在慢项下方，整块区域连续。

    `borrowed` 只在"本车道偏好槽在 t 时刻全忙"时为真（真的因为拥挤而借位），
    紧凑堆叠导致的跨区落位不算借槽。它只作统计标记，不改样式/时长/文案。

    返回 (slot_index | None, borrowed)。
    """
    n = len(pref)
    free_now = [i for i in range(n) if slot_free[i] <= t]
    if not free_now:
        return None, False

    def _own_busy():
        own = [k for k in range(n) if pref[k] == lane]
        return bool(own) and all(slot_free[k] > t for k in own)

    if lane == SLOW:
        i = free_now[0]
        return i, _own_busy()
    busy_slow = [i for i in range(n) if slot_lane[i] == SLOW and slot_free[i] > t]
    low = (max(busy_slow) + 1) if busy_slow else 0
    after = [i for i in free_now if i >= low]
    if after:
        return after[0], _own_busy()
    return free_now[0], True


def _overflow_entry(it, reason):
    return {
        "key": it["key"],
        "reason": reason,
        "source_event_indexes": list(it["source_event_indexes"]),
        "count": it["count"],
        "diamond": it["diamond"],
        "total_diamond": int(it["diamond"]) * int(it["count"]),
        "name": it["name"],
        "user": it["user"],
        "lane": it["visual_lane"],
        "ready_ms": it["ready_ms"],
    }


def _pct(vals, p):
    if not vals:
        return None
    arr = sorted(vals)
    k = max(0, min(len(arr) - 1, int(math.ceil(p / 100.0 * len(arr))) - 1))
    return arr[k]


def _stats(items, placed, overflow, cfg):
    """覆盖率与延迟统计（部署方案 §8.2 的 stats 段）。"""
    src = sum(len(it["source_event_indexes"]) for it in items)
    vis = sum(len(p["source_event_indexes"]) for p in placed)
    ovf = sum(len(o["source_event_indexes"]) for o in overflow)
    delays = {}
    for lane in (SLOW, FAST):
        vals = [p["delay_ms"] for p in placed if p["lane"] == lane]
        delays[lane] = {
            "n": len(vals),
            "p50": _pct(vals, 50), "p95": _pct(vals, 95),
            "max": max(vals) if vals else None,
        }
    p0 = [p["delay_ms"] for p in placed if p["priority"] == P0]
    delays["p0"] = {"n": len(p0), "max": max(p0) if p0 else None}
    return {
        "source_events": src,
        "visible_events": vis,
        "merged_events": vis - len(placed),
        "overflow_events": ovf,
        "items": len(placed),
        "borrowed_items": sum(1 for p in placed if p["borrowed_slot"]),
        "delay_ms": delays,
        "covered": src == vis + ovf,
    }


# ── 实时预览语义 ────────────────────────────────────────
def build_live_display(event, cfg=None, gift_dict=None, seq=0):
    """单条礼物事件 → 实时预览用的 display 语义（前端只渲染，不再判断）。

    - full：`merge_mode="full_combo"` + 稳定 key，前端同 key 更新原行；
             `count` 是累计值，金额按"新总额 − 旧总额"增量累加，避免重复计入。
    - light：`merge_mode="none"` + 每事件唯一 key，永不跨事件合并。
    """
    c = config(cfg)
    ne = normalize_event(event, c, gift_dict)
    if ne["channel"] == "full" and ne["uid"]:
        key = f"full|{ne['uid']}|{ne['gift_id']}"
        mode = "full_combo"
    else:
        key = f"light|{int(seq or 0)}"
        mode = "none"
    return {
        "text": render_text(ne),
        "lane": ne["visual_lane"],
        "priority": ne["priority"],
        "key": key,
        "merge_mode": mode,
        "merged": mode == "full_combo",
        "count": ne["count"],
        "total_diamond": int(ne["diamond"]) * int(ne["count"]),
        "price_known": ne["price_known"],
        "gift_id": ne["gift_id"],
        "name": ne["name"],
        "rule_version": RULE_VERSION,
    }


# ── 计划 → 分片渲染视图 ─────────────────────────────────
def items_for_segment(plan, seg_idx):
    """取某分片的展示项（会话相对 ms，渲染层再减去该片起点）。"""
    for s in plan.get("segments") or []:
        if s.get("idx") == seg_idx:
            return s.get("items") or []
    return []


def plan_summary_text(plan):
    """一行可读摘要（写日志/审计用）。"""
    st = plan.get("stats") or {}
    d = st.get("delay_ms") or {}
    fast = d.get(FAST) or {}
    slow = d.get(SLOW) or {}
    return (f"礼物计划 v{plan.get('rule_version')} · 原始 {st.get('source_events')} 笔 → "
            f"展示 {st.get('items')} 项（合并 {st.get('merged_events')}）· "
            f"借槽 {st.get('borrowed_items')} · 片尾溢出 {st.get('overflow_events')}"
            f" · 延迟 fast p95={fast.get('p95')}ms max={fast.get('max')}ms"
            f" / slow max={slow.get('max')}ms"
            + ("" if st.get("covered") else " ⚠️ 覆盖未闭合"))
