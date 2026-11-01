#!/usr/bin/env python3
"""为 recordings/history.json 的存量场次补算礼物字段(2026-10-16;2026-10-29 改口径)。

  · gift_integrity —— 轻通道完整性(2026-10-29 起主判据是 `light` 子对象:
    「轻通道收到多少条 vs jsonl 落了多少条、4 个承诺字段齐不齐」。顶层 `ratio`
    仍是老口径的「存在礼物信息的时间占比」,保留兼容。详见
    danmaku.gift_channel_coverage。补算拿不到实时收到数 → recv=null、
    status=field_only,如实标注"未对账");
  · gift_diag     —— 诊断记录级别计数(fault/suspect/muted/quiet/total,
    与 monitor._gift_diag_counts 同口径;历史行警示胶囊数据源)。

⚠ 2026-10-16 二次瘦身:gift_stats(份数/抖币/礼物榜等具体礼物明细)已按用户
反馈整体下线,前端不展示、本脚本不再补算、收尾也不再写入 —— 请勿恢复。

💡 一般不再需要本脚本:webui._backfill_gift_fields 已把补算挂到历史页
(每次 /api/history 增量补一批,无需停服)。本脚本保留用于「一次性全量补齐」
或历史页补算被跳过(如 jsonl 暂时缺失)的兜底。

用法:
    python scripts/backfill_gift_integrity.py            # 预演,只列结果不写
    python scripts/backfill_gift_integrity.py --apply    # 实际写回 history.json
    python scripts/backfill_gift_integrity.py --force    # 已有字段也全部重算

安全边界:
  - 只读取 jsonl / giftwatch.jsonl 统计,绝不改动场次文件与 .meta/;
  - 只更新 history.json 条目的两个 gift_* 字段(原子写:tmp + os.replace);
  - 正在录制(status=recording)的条目跳过;jsonl 找不到的条目跳过并计数;
  - 🔴 --apply 必须在服务停止时执行:服务进程内存里的 _history 会在下次
    落盘时把本脚本写回的内容整份覆写掉。脚本会先探 WebUI 端口,服务在跑
    就拒绝写回(预演不受限)。
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from danmaku import gift_channel_coverage, jsonl_for  # noqa: E402  (唯一真源,别抄第二份)

HISTORY_PATH = os.path.join(BASE, "recordings", "history.json")


def stem_of(path: str) -> str:
    """录制模板路径 → 场次前缀(去掉 -%03d.ext / -000.ext)。"""
    b = os.path.basename(path)
    if "-%03d." in b:
        return b.split("-%03d.")[0]
    stem, _ = os.path.splitext(b)
    if len(stem) > 3 and stem[-4] == "-" and stem[-3:].isdigit():
        stem = stem[:-4]
    return stem


def jsonl_candidates(entry: dict):
    """该条目可能的礼物明细 jsonl:本机(新旧两种布局)→ 归档目标。"""
    out = []
    op = entry.get("output_path") or ""
    if op:
        out.append(jsonl_for(op))                       # 新布局:<场次目录>/.meta
        sd = os.path.dirname(op)
        out.append(os.path.join(os.path.dirname(sd), ".meta",
                                stem_of(op) + ".danmaku.jsonl"))  # 旧布局:<主播>/.meta
        arch = entry.get("archive") or {}
        dest = arch.get("dest")
        if arch.get("state") == "archived" and dest:
            out.append(os.path.join(dest, ".meta", stem_of(op) + ".danmaku.jsonl"))
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def giftwatch_candidates(entry: dict):
    """该条目可能的诊断快照 giftwatch.jsonl(与 jsonl 候选同目录换后缀)。"""
    out = []
    suffix = ".danmaku.jsonl"
    for jp in jsonl_candidates(entry):
        base = jp[: -len(suffix)] if jp.endswith(suffix) else jp
        out.append(base + ".giftwatch.jsonl")
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def diag_counts_of(path: str):
    """giftwatch.jsonl → 级别计数(与 monitor._gift_diag_counts 同口径)。"""
    counts = {"fault": 0, "suspect": 0, "muted": 0, "quiet": 0}
    total = 0
    with open(path, encoding="utf-8") as f:
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
    return counts


def service_alive() -> bool:
    """WebUI 还在响应 → 服务活着,--apply 会被内存 _history 覆写,必须拒绝。"""
    try:
        import webui                       # noqa  (端口解析唯一真源 _webui_port)
        port = webui._webui_port()
    except Exception:
        port = 8780
    try:
        import urllib.request
        with urllib.request.urlopen(          # noqa: S310  (本机探活,固定地址)
                f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="补算历史条目的礼物三件套(完整性/统计/诊断计数)")
    ap.add_argument("--apply", action="store_true", help="实际写回(默认预演)")
    ap.add_argument("--force", action="store_true", help="已有字段也全部重算")
    args = ap.parse_args()

    if not os.path.exists(HISTORY_PATH):
        print("找不到 recordings/history.json")
        return 1
    if args.apply and service_alive():
        print("🔴 检测到 WebUI 服务仍在运行 —— --apply 会被内存 _history 覆写。"
              "请先停止服务(douyin stop)再执行本脚本;预演模式不受限。")
        return 1

    with open(HISTORY_PATH, encoding="utf-8") as f:
        history = json.load(f)

    done = skipped_rec = skipped_nofiles = updated = 0
    for e in history:
        if e.get("status") == "recording":
            skipped_rec += 1
            continue
        # 2026-10-29 结构升级:礼物完整性的主判据换成 gift_integrity.light
        # (轻通道收到 vs 落盘 vs 字段缺项)。只有时间占比的老条目也要重算一次,
        # 否则历史页永远只有旧口径。拿不到实时计数 → recv=null(status=field_only)。
        gi_old = e.get("gift_integrity")
        need_cov = (args.force or not gi_old
                    or (isinstance(gi_old, dict) and "light" not in gi_old))
        need_diag = args.force or not e.get("gift_diag")
        if not (need_cov or need_diag):
            continue
        done += 1
        pieces = []

        jp = next((p for p in jsonl_candidates(e) if os.path.exists(p)), None) \
            if need_cov else None
        if need_cov and not jp:
            skipped_nofiles += 1
            print(f"  - {e.get('name')} {e.get('started_at')}: jsonl 未找到,跳过")
            continue

        if need_cov:
            dur = float(e.get("duration") or 0)
            gi = gift_channel_coverage(jp, dur, started_at=e.get("started_at")) \
                if dur > 0 else None
            if gi is None:
                pieces.append("完整性:时长未知跳过")
            else:
                e["gift_integrity"] = gi
                lt = gi.get("light") or {}
                pieces.append(f"轻通道 {lt.get('status', '?')}"
                              f"({lt.get('events', 0)} 条 · 覆盖 {gi['ratio'] * 100:.0f}%)")
        if need_diag:
            gwp = next((p for p in giftwatch_candidates(e) if os.path.exists(p)), None)
            if gwp:
                try:
                    gd = diag_counts_of(gwp)
                except OSError:
                    gd = None
                if gd:
                    e["gift_diag"] = gd
                    pieces.append(f"诊断 {gd['fault']}F/{gd['suspect']}S/{gd['muted']}M")
        if pieces:
            updated += 1
            print(f"  ✓ {e.get('name')} {e.get('started_at')}: " + ",".join(pieces))
        else:
            print(f"  - {e.get('name')} {e.get('started_at')}: 无可补字段")

    print(f"\n共 {len(history)} 条:待补 {done},已更新 {updated},"
          f"jsonl 缺失 {skipped_nofiles},录制中跳过 {skipped_rec}")
    if not args.apply:
        print("预演结束(未写盘)。确认无误后加 --apply 写回(须先停止服务)。")
        return 0
    if updated:
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_PATH)
        print(f"已写回 {HISTORY_PATH}")
    else:
        print("没有需要写回的变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
