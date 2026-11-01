#!/usr/bin/env python3
"""生成/校验礼物展示的黄金样例（`contract/fixtures/gift_display_golden.json`）。

用途：让"下游与录制端展示一致"变成**可执行**的断言，而不是文档里的一句承诺。
样例里同时冻结三样东西：

    input.events    合成礼物事件（覆盖 98/99 边界、匿名不合并、完整通道连击、未知价格）
    input.gift_dict 合成字典（不依赖真实礼物表，任何时候都能跑）
    input.segments  合成分片边界
    expected        当时的完整展示计划（槽位/起止/分段文本/统计）

录制端单测与下游自测都拿这份文件比对：**跑通即一致**，跑不通说明规则实现漂移。

用法：
    python scripts/gift_display_fixture.py            # 校验（不一致退出码 1）
    python scripts/gift_display_fixture.py --write    # 显式重建（规则改动后人工确认再跑）
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import gift_display as gd  # noqa: E402

FIXTURE = os.path.join(BASE, "contract", "fixtures", "gift_display_golden.json")
ORIGIN_MS = 1793200000000          # 会话起点（合成值，与真实录制无关）

# 合成字典：id → 名称/单价。刻意包含 98（快）与 99（慢）这一对边界。
GIFT_DICT = {
    "1001": {"name": "小心心", "diamond": 1},
    "1002": {"name": "闪烁星河", "diamond": 98},
    "1003": {"name": "亲吻", "diamond": 99},
    "1004": {"name": "嘉年华", "diamond": 30000},
    "1005": {"name": "免费祝福", "diamond": 0},
}


class _Dict:
    """最小字典实现，满足 gift_display 的 info()/find_by_name() 协议。"""

    def __init__(self, raw):
        self.raw = {int(k): v for k, v in raw.items()}
        self.by_name = {}
        for gid, info in self.raw.items():
            self.by_name.setdefault(info["name"], []).append(gid)

    def info(self, gift_id):
        return self.raw.get(int(gift_id))

    def find_by_name(self, name):
        return list(self.by_name.get(name, ()))


def _ev(offset_ms, gift_id, diamond, count=1, uid="", user="", channel="light"):
    return {"ts": ORIGIN_MS + offset_ms, "type": "gift",
            "gift": f"礼物#{gift_id}", "diamond": diamond, "count": count,
            "uid": uid, "user": user, "channel": channel}


# 事件清单（顺序即 jsonl 顺序；源索引就是这里的下标）
EVENTS = [
    # 匿名轻通道：同一个人送三次也不会合并（3 个原始事件 → 3 项）
    _ev(1000, 1001, 1),
    _ev(1500, 1001, 1),
    _ev(2000, 1001, 1),
    # 98 抖币 → 快车道
    _ev(2200, 1002, 98),
    # 99 抖币 → 慢车道
    _ev(2400, 1003, 99),
    # 免费礼物（已知 0 抖币）→ 快车道，金额显示 0 而不是 "?"
    _ev(2600, 1005, 0),
    # 完整通道连击：count 是累计值，合并后取最大值，文本分段递增
    _ev(3000, 1001, 1, 1, uid="u1", user="观众甲", channel="full"),
    _ev(4000, 1001, 1, 5, uid="u1", user="观众甲", channel="full"),
    _ev(5000, 1001, 1, 9, uid="u1", user="观众甲", channel="full"),
    # 超过 3 秒窗口 → 另起一项
    _ev(9000, 1001, 1, 12, uid="u1", user="观众甲", channel="full"),
    # P0 高价值
    _ev(9500, 1004, 30000, 1, uid="u2", user="观众乙", channel="full"),
    # 未知价格（字典型无此 id，事件也没给单价）→ 保守进慢车道
    {"ts": ORIGIN_MS + 10000, "type": "gift", "gift": "礼物#9999",
     "diamond": 0, "count": 1, "uid": "", "user": "", "channel": "light"},
]

SEGMENTS = [
    {"idx": 0, "start_ms": 0, "end_ms": 8000},
    {"idx": 1, "start_ms": 8000, "end_ms": 60000},
]


def build():
    """按当前实现算出计划（fixture 的 expected 部分）。"""
    cfg = gd.config(None)
    items = gd.build_display_items(EVENTS, cfg, _Dict(GIFT_DICT))
    items = gd.shift_items(items, ORIGIN_MS)
    return gd.plan_session_display(items, SEGMENTS, cfg)


def make_document():
    return {
        "schema": 1,
        "rule_version": gd.RULE_VERSION,
        "layout_profile": gd.DEFAULTS["layout_profile"],
        "note": "合成样例：主播A/观众甲乙/合成礼物。下游跑通即视为与录制端一致。",
        "input": {
            "events": EVENTS,
            "gift_dict": GIFT_DICT,
            "segments": SEGMENTS,
            "origin_ms": ORIGIN_MS,
        },
        "expected": build(),
    }


def write():
    doc = make_document()
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    with open(FIXTURE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return doc


def check():
    try:
        with open(FIXTURE, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return [f"黄金样例不可读: {e}"]
    issues = []
    if doc.get("rule_version") != gd.RULE_VERSION:
        issues.append(f"样例 rule_version={doc.get('rule_version')!r} "
                      f"≠ 代码 {gd.RULE_VERSION!r}（规则变了要重建样例）")
    got = build()
    want = doc.get("expected") or {}
    if json.dumps(got, ensure_ascii=False, sort_keys=True) != \
            json.dumps(want, ensure_ascii=False, sort_keys=True):
        issues.append("当前实现与黄金样例不一致（展示计划漂移）")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="重建黄金样例")
    a = ap.parse_args()
    if a.write:
        doc = write()
        st = (doc["expected"].get("stats") or {})
        print(f"✓ 已写入 {os.path.relpath(FIXTURE, BASE)}："
              f"原始 {st.get('source_events')} 笔 → 展示 {st.get('items')} 项，"
              f"片尾溢出 {st.get('overflow_events')}")
        return 0
    issues = check()
    if issues:
        for i in issues:
            print("✗", i)
        return 1
    print("✓ 黄金样例与当前实现一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
