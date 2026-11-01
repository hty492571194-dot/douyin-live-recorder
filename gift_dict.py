#!/usr/bin/env python3
"""官方礼物字典：`gift_id → 名称 / 单价`（IO 层，规则不在本文件）。

数据来源（2026-10-29 实测，匿名可拉、免签名、无需房间号）：

    GET https://live.douyin.com/webcast/gift/list/
        ?aid=6383&app_name=douyin_web&device_platform=web&version_code=180800

返回 `data.gifts` 共 1314 条，字段含 `id / name / diamond_count /
is_displayed_on_panel / gift_offline_time`。与本地 7 周录制语料对账：
93 个观测到的 gift_id 命中 92 个（99%），单价冲突 0。

设计边界：
- 本模块只负责"取、存、查"，**不做展示规则**（那是 `gift_display.py` 的事）。
- 抓取失败不得影响录制：所有函数失败都返回 None/空，不抛给主链路。
- 字典是**渲染层**：`.danmaku.jsonl` 永不改写，历史字幕靠字典重算即可。
- 永不删除条目：平台会下架礼物（实测带 `gift_offline_time`），删了历史场次
  就永久变回 `礼物#<id>`。下架的只标 `offline_at`。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, "state")
DICT_PATH = os.path.join(STATE_DIR, "gift_dict.json")

API = ("https://live.douyin.com/webcast/gift/list/"
       "?aid=6383&app_name=douyin_web&device_platform=web&version_code=180800")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
SCHEMA = 1
STALE_SEC = 7 * 24 * 3600      # 超过 7 天视为过期，可触发重抓


# ── 抓取与归一 ───────────────────────────────────────────
def fetch(timeout=20.0):
    """拉官方礼物列表，返回 [{id,name,diamond,panel,combo,type,offline_at}]。"""
    try:
        import httpx
    except ImportError:
        return None
    try:
        r = httpx.get(API, headers={"User-Agent": UA, "Referer": "https://live.douyin.com/"},
                      timeout=timeout, trust_env=False)
        if r.status_code != 200:
            return None
        raw = (r.json().get("data") or {}).get("gifts") or []
    except Exception:
        return None
    out = []
    for g in raw:
        try:
            gid = int(g.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if gid <= 0:
            continue
        name = str(g.get("name") or "").strip()
        try:
            diamond = int(g.get("diamond_count") or 0)
        except (TypeError, ValueError):
            diamond = 0
        off = g.get("gift_offline_time") or 0
        out.append({
            "id": gid,
            "name": name,
            "diamond": diamond,
            "panel": bool(g.get("is_displayed_on_panel")),
            "combo": bool(g.get("combo")),
            "type": int(g.get("type") or 0),
            "offline_at": int(off) if off else 0,
        })
    return out or None


# ── 字典对象 ────────────────────────────────────────────
class GiftDict:
    """只读视图，满足 `gift_display` 需要的 info()/find_by_name() 协议。"""

    __slots__ = ("gifts", "by_name", "version", "updated_at", "source")

    def __init__(self, gifts, version=0, updated_at=0, source=None):
        self.gifts = gifts                       # {int id: {name,diamond,...}}
        self.version = int(version or 0)
        self.updated_at = int(updated_at or 0)
        self.source = source or {}
        by_name = {}
        for gid, info in gifts.items():
            n = info.get("name")
            if n:
                by_name.setdefault(n, []).append(gid)
        self.by_name = by_name

    # gift_display 依赖的两个查询
    def info(self, gift_id):
        try:
            return self.gifts.get(int(gift_id))
        except (TypeError, ValueError):
            return None

    def find_by_name(self, name):
        return list(self.by_name.get(str(name or ""), ()))

    def name_of(self, gift_id, default=None):
        info = self.info(gift_id)
        return (info or {}).get("name") or default

    def diamond_of(self, gift_id, default=None):
        info = self.info(gift_id)
        return (info or {}).get("diamond") if info else default

    def __len__(self):
        return len(self.gifts)


def load(path=None):
    """读本地字典；不存在/损坏 → None（调用方自行决定是否重抓）。"""
    path = path or DICT_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("gifts") or {}
    gifts = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            gifts[int(k)] = v
        except (TypeError, ValueError):
            continue
    if not gifts:
        return None
    return GiftDict(gifts, data.get("version"), data.get("updated_at"), data.get("source"))


def save(items, path=None, version=None, source=None, prune_missing=False):
    """原子写字典。items: fetch() 的结构；已存在则合并（永不删除，除非 prune_missing）。"""
    path = path or DICT_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    old = load(path)
    gifts = dict(old.gifts) if old else {}
    incoming = {}
    for it in items or []:
        gid = int(it["id"])
        incoming[gid] = {
            "name": it.get("name") or "",
            "diamond": int(it.get("diamond") or 0),
            "panel": bool(it.get("panel")),
            "combo": bool(it.get("combo")),
            "type": int(it.get("type") or 0),
            "offline_at": int(it.get("offline_at") or 0),
        }
    today = time.strftime("%Y-%m-%d")
    added = renamed = repriced = 0
    for gid, info in incoming.items():
        cur = gifts.get(gid)
        if cur is None:
            info["first_seen"] = today
            info["last_seen"] = today
            gifts[gid] = info
            added += 1
            continue
        if cur.get("name") and info["name"] and cur["name"] != info["name"]:
            renamed += 1
            cur["name_prev"] = cur["name"]
        if cur.get("diamond") is not None and cur["diamond"] != info["diamond"]:
            repriced += 1
        cur.update({k: v for k, v in info.items() if k != "first_seen"})
        cur.setdefault("first_seen", today)
        cur["last_seen"] = today
    gone = [gid for gid in gifts if gid not in incoming]
    for gid in gone:
        # 不删除：历史场次仍要能渲染。只标记"官方已不在面板里"
        gifts[gid]["absent_since"] = gifts[gid].get("absent_since") or today
    if prune_missing:
        for gid in gone:
            gifts.pop(gid, None)
    ver = int(version if version is not None else ((old.version if old else 0) + 1))
    data = {
        "schema": SCHEMA,
        "version": ver,
        "updated_at": int(time.time()),
        "source": source or {"api": API, "count": len(incoming), "saved_at": today},
        "stats": {"added": added, "renamed": renamed, "repriced": repriced,
                  "absent": len(gone), "total": len(gifts)},
        "gifts": {str(k): v for k, v in sorted(gifts.items())},
    }
    fd, tmp = tempfile.mkstemp(prefix=".gift_dict-", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return data["stats"]


def sync(path=None):
    """抓一次官方列表并落盘。返回 stats；抓取失败返回 None（绝不抛）。"""
    items = fetch()
    if not items:
        return None
    try:
        return save(items, path)
    except OSError:
        return None


def ensure(cfg=None, path=None, max_age_sec=STALE_SEC, allow_fetch=True):
    """取可用字典：有本地就用本地；过期/缺失且允许抓取时抓一次。

    重抓失败也返回旧字典（礼物名降级为 `礼物#id`，不影响录制与字幕生成）。
    """
    gd = load(path)
    stale = gd is None or (time.time() - (gd.updated_at or 0) > float(max_age_sec))
    if stale and allow_fetch:
        sync(path)
        fresh = load(path)
        if fresh is not None:
            return fresh
    return gd


def info_for_event(gift_name, diamond=None, path=None):
    """按 jsonl 里的 `gift` 字段文本反查条目（调试/回填用）。"""
    gd = load(path)
    if gd is None:
        return None
    s = str(gift_name or "")
    if s.startswith("礼物#") and s[3:].isdigit():
        return gd.info(int(s[3:]))
    ids = gd.find_by_name(s)
    if len(ids) == 1:
        return gd.info(ids[0])
    if diamond is not None:
        hit = [i for i in ids if (gd.info(i) or {}).get("diamond") == int(diamond)]
        if len(hit) == 1:
            return gd.info(hit[0])
    return None


if __name__ == "__main__":       # 手动同步:python gift_dict.py
    st = sync()
    print("同完成败" if st is None else f"同步完成 {st}")
