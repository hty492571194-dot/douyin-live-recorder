# -*- coding: utf-8 -*-
"""孤儿主播目录的延迟清理(30 天冷静期)。

主播卡片被删掉后,`previews/<name>/` 与 `recordings/<name>/` 不会立刻消失 ——
删除本身可能是误操作,而录制素材删掉就没了。这里给每个「已无对应主播」的
目录建一条台账,记下**首次发现它是孤儿**的时间,满 30 天才真正清理;期间只要
主播被重新加回来,记录立即撤销(``mark`` 幂等)。

边界与护栏
----------
* **只清本地**:待删目录必须是 ``BASE`` 下的一级子目录(realpath 校验,拒绝
  符号链接 / 挂载点)。NAS ``~/DouyinArchive/直播回放`` 与外接硬盘上的任何
  东西都不在清理范围内 —— 已归档到 NAS 的文件与目录一律跳过、保持原样。
* **归档护栏**:``recordings/<name>/`` 只有在该主播于 ``history.json`` 里的
  条目全部 ``archived``(或压根没有条目)时才允许删;存在 ``none`` /
  ``pending`` / ``missing`` 的一律跳过,不猜、交给人判断。
  ``previews/<name>/`` 是头像缩略图,可重建,不需要归档护栏。
* **默认 dry-run**:删除必须显式 ``apply=True``。
* **幂等**:台账驱动,重复调用不会重复删,也不会刷新 ``marked_at``。

触发方式
--------
1. 定时:`monitor.retention_loop`(每 6 小时,启动时先跑一次)
2. 事件:`webui.State.update_config` 检测到 monitors 变化时立即 ``mark``,
   让 30 天倒计时从删除那一刻起算,而不是等下一次定时扫描
3. 手动:``python retention.py --sweep --apply`` 或 ``POST /api/retention/sweep``
"""

import json
import os
import shutil
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_PATH = os.path.join(BASE, "spool", "retention.json")

GRACE_DAYS = 30                 # 冷静期(天)
CHECK_INTERVAL = 6 * 3600       # 定时巡检间隔(秒)
DAY = 86400

# 清理范围:只允许这两类目录,且必须是 BASE 的直接子目录
WATCH_ROOTS = ("previews", "recordings")
# 需要做归档护栏的根(缩略图不需要)
GUARDED_ROOTS = ("recordings",)

LEDGER_VERSION = 1


# ── 台账读写 ────────────────────────────────────────────────────────────────

def _load(path=None):
    p = path or LEDGER_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": LEDGER_VERSION, "items": {}}


def _save(data, path=None):
    p = path or LEDGER_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ── 基础判定 ────────────────────────────────────────────────────────────────

def monitor_names(cfg):
    """配置里当前存在的主播名集合。"""
    return {str(m.get("name", "")) for m in (cfg or {}).get("monitors", [])
            if isinstance(m, dict) and m.get("name")}


def _safe_name(name):
    """名字必须干净:非空、不含路径分隔符、不是 . / ..。"""
    if not isinstance(name, str) or not name.strip():
        return False
    if name in (".", ".."):
        return False
    return not any(ch in name for ch in ("/", "\\", "\0", os.sep))


def orphan_dirs(root=BASE):
    """扫描 previews/ 与 recordings/ 下的一级目录,返回 {name: [绝对路径]}。"""
    out = {}
    for rel in WATCH_ROOTS:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            if not _safe_name(name):
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p):
                out.setdefault(name, []).append(p)
    return out


def _is_local_safe_dir(path, root=BASE):
    """待删目录的安全校验:必须是 root 下的一级子目录,且非符号链接/挂载点。

    realpath 校验可挡住「目录其实是 NAS 的软链」这类情况 —— 挂载点上的东西
    一律不碰(已迁移到 NAS 的文件与目录跳过)。
    """
    try:
        if os.path.islink(path) or os.path.ismount(path):
            return False
        real = os.path.realpath(path)
        base_real = os.path.realpath(root)
        if not real.startswith(base_real + os.sep):
            return False
        # 必须是 root/<WATCH_ROOT>/<name> 这三层,不能更深
        rel = os.path.relpath(real, base_real)
        parts = rel.split(os.sep)
        return len(parts) == 2 and parts[0] in WATCH_ROOTS
    except OSError:
        return False


def _archive_summary(history, name):
    """统计某主播在 history.json 里的归档状态。

    返回 {"total": n, "states": {...}, "blocked": bool, "reason": str}
    """
    states = {}
    for x in (history or []):
        if not isinstance(x, dict) or x.get("name") != name:
            continue
        st = (x.get("archive") or {}).get("state") or "none"
        states[st] = states.get(st, 0) + 1
    total = sum(states.values())
    blocked = False
    reason = ""
    for st in ("none", "pending", "missing"):
        if states.get(st):
            blocked = True
            reason = "存在 %d 条 %s 的录制记录" % (states[st], st)
            break
    return {"total": total, "states": states, "blocked": blocked, "reason": reason}


# ── mark:发现孤儿并登记 ─────────────────────────────────────────────────────

def mark(cfg, now=None, path=None, root=BASE):
    """把「无对应主播」的目录登记进台账;已加回的主播撤销记录。

    幂等:已登记的不刷新 marked_at,已删掉的记录自动清理(目录不存在)。
    返回 {"added": [...], "revoked": [...], "pruned": [...]}
    """
    now = now if now is not None else time.time()
    data = _load(path)
    items = data["items"]
    known = monitor_names(cfg)
    found = orphan_dirs(root)

    added, revoked, pruned = [], [], []

    for name, dirs in found.items():
        if name in known:
            continue                                  # 有主播,正常
        rels = sorted(os.path.relpath(d, root).replace(os.sep, "/") for d in dirs)
        it = items.get(name)
        if it is None:
            items[name] = {"marked_at": now, "dirs": rels}
            added.append(name)
        else:
            it["dirs"] = sorted(set(it.get("dirs", [])) | set(rels))
            # 不动 marked_at —— 重复扫描不能无限续命

    for name in list(items):
        if name in known:
            items.pop(name)
            revoked.append(name)
        elif not any(os.path.isdir(os.path.join(root, d))
                     for d in items[name].get("dirs", [])):
            items.pop(name)                           # 目录已不在(多半被人工删了)
            pruned.append(name)

    if added or revoked or pruned:
        _save(data, path)
    return {"added": added, "revoked": revoked, "pruned": pruned}


# ── sweep:到期的真正清理 ────────────────────────────────────────────────────

def sweep(cfg, history=None, now=None, grace_days=GRACE_DAYS, apply=False,
          allow_missing=False, path=None, root=BASE, log=None):
    """清理冷静期已过的孤儿目录。

    apply=False 时只报告不删。返回:
    {"deleted": [...], "skipped": [{name, reason}], "pending": [{name, days_left}]}
    """
    def _log(msg):
        if log:
            log(msg)

    now = now if now is not None else time.time()
    grace = grace_days * DAY
    known = monitor_names(cfg)
    data = _load(path)
    items = data["items"]

    deleted, skipped, pending = [], [], []
    changed = False

    for name, it in sorted(items.items()):
        if name in known:
            skipped.append({"name": name, "reason": "主播已重新添加,取消清理"})
            continue
        if not _safe_name(name):
            skipped.append({"name": name, "reason": "主播名不合法,拒绝处理"})
            continue

        marked = float(it.get("marked_at") or 0)
        age = now - marked
        if age < grace:
            pending.append({"name": name,
                            "days_left": int((grace - age) // DAY) + 1,
                            "dirs": it.get("dirs", [])})
            continue

        # 归档护栏:recordings/ 下要求全部已归档
        guard_blocked = False
        for rel in it.get("dirs", []):
            top = rel.split("/")[0]
            if top not in GUARDED_ROOTS:
                continue
            summ = _archive_summary(history, name)
            if summ["blocked"] and not (allow_missing and
                                        set(summ["states"]) <= {"archived", "missing"}):
                guard_blocked = True
                skipped.append({"name": name,
                                "reason": "%s(未确认已归档,跳过)" % summ["reason"]})
                break
        if guard_blocked:
            continue

        # 真正删除
        for rel in it.get("dirs", []):
            p = os.path.join(root, rel.replace("/", os.sep))
            if not os.path.isdir(p):
                continue                              # 已被人工删掉,幂等
            if not _is_local_safe_dir(p, root):
                skipped.append({"name": name,
                                "reason": "路径不在允许范围内(疑似 NAS/挂载点): %s" % rel})
                continue
            if apply:
                try:
                    shutil.rmtree(p)
                    _log(f"[清理] 已删除孤儿目录: {rel}(孤儿 {int(age // DAY)} 天)")
                    deleted.append(rel)
                    changed = True
                except OSError as e:
                    skipped.append({"name": name, "reason": "删除失败: %s" % e})
            else:
                deleted.append(rel)                   # dry-run:列入「将会删除」

    if changed:
        # 目录可能已经全没了,交回给 mark 去剪枝;这里只移除已删空的条目
        for name in [n for n in items if not any(
                os.path.isdir(os.path.join(root, d)) for d in items[n].get("dirs", []))]:
            items.pop(name, None)
        _save(data, path)

    return {"deleted": deleted, "skipped": skipped, "pending": pending,
            "applied": bool(apply)}


def status(cfg, history=None, now=None, grace_days=GRACE_DAYS, path=None, root=BASE):
    """给前端/CLI 用的只读视图:待清理项 + 剩余天数 + 是否会被护栏拦下。"""
    now = now if now is not None else time.time()
    grace = grace_days * DAY
    known = monitor_names(cfg)
    items = _load(path)["items"]
    out = []
    for name, it in sorted(items.items()):
        age = now - float(it.get("marked_at") or 0)
        summ = _archive_summary(history, name)
        out.append({
            "name": name,
            "dirs": it.get("dirs", []),
            "marked_at": it.get("marked_at"),
            "age_days": int(age // DAY),
            "days_left": max(0, int((grace - age) // DAY) + 1),
            "ready": age >= grace,
            "re_added": name in known,
            "archive": summ,
        })
    return {"grace_days": grace_days, "items": out}


def run_due(cfg, history=None, now=None, grace_days=GRACE_DAYS, path=None,
            root=BASE, log=None):
    """定时任务入口:先登记新孤儿,再清理到期的。"""
    m = mark(cfg, now=now, path=path, root=root)
    s = sweep(cfg, history=history, now=now, grace_days=grace_days, apply=True,
              path=path, root=root, log=log)
    return {"mark": m, "sweep": s}


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    ap = argparse.ArgumentParser(description="孤儿主播目录延迟清理")
    ap.add_argument("--status", action="store_true", help="显示台账与剩余天数")
    ap.add_argument("--sweep", action="store_true", help="执行清理扫描")
    ap.add_argument("--apply", action="store_true", help="与 --sweep 同用,真正删除")
    ap.add_argument("--grace-days", type=int, default=GRACE_DAYS)
    ap.add_argument("--allow-missing", action="store_true",
                    help="允许清理归档目标已丢失(missing)的条目")
    args = ap.parse_args()

    cfg_path = os.path.join(BASE, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        print("读不到 config.json"); return 1

    hist_path = os.path.join(BASE, "recordings", "history.json")
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, ValueError):
        history = []

    if args.sweep:
        r = sweep(cfg, history, grace_days=args.grace_days, apply=args.apply,
                  allow_missing=args.allow_missing, log=print)
        print("applied:", r["applied"])
        print("删除:", r["deleted"] or "无")
        for s in r["skipped"]:
            print("  跳过 %s — %s" % (s["name"], s["reason"]))
        for p_ in r["pending"]:
            print("  未满冷静期 %s(还剩 %d 天)" % (p_["name"], p_["days_left"]))
        return 0

    # 默认 + --status 都先登记再显示
    m = mark(cfg)
    if m["added"]:
        print("新登记孤儿:", m["added"])
    if m["revoked"]:
        print("已撤销(主播加回):", m["revoked"])
    st = status(cfg, history, grace_days=args.grace_days)
    print("冷静期 %d 天 | 台账 %d 项" % (st["grace_days"], len(st["items"])))
    for it in st["items"]:
        flag = "可清理" if (it["ready"] and not it["archive"]["blocked"]) else \
               ("待等待" if not it["ready"] else "被归档护栏拦下")
        print("  %-14s 已 %3d 天, 还剩 %2d 天 | %s | %s"
              % (it["name"], it["age_days"], it["days_left"], flag,
                 it["archive"]["reason"] or it["archive"]["states"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
