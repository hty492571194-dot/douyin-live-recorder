#!/usr/bin/env python3
"""缺号主播 web_rid 自动补齐 —— 纯逻辑层(窗口 / 调度 / 判定 / 写回合并 / 熔断)。

**这个模块不做任何 IO、不联网、不 import playwright。** 所有副作用(开浏览器、
读盘写盘、打日志)都在 monitor.rid_backfill_loop 与 web_rid_browser 里。这样拆
的理由很实际:全量单测(1100+ 项)绝不能因为引入浏览器而变慢或变脆,而"什么时候
跑、跑谁、结果算不算失败"这类判断恰恰是**最容易写错、最需要穷举**的部分。

设计依据(实测 2026-10-26,详见 `缺号主播web_rid自动补齐-实施方案A.md`):
- 抖音号 / 主页链接都拿不到 web_rid,只有「渲染后的主页里那个 live.douyin.com/<数字>」才有;
- 主播没开播时页面干净(无该链接、资料接口 room_id_str="0"),所以"没开播"是**能确认的结论**,
  不是"查不到";
- 因此判定必须三态:**live / offline / unknown**,`offline` 算成功,只有 `unknown` 算失败。
  把 offline 当失败,三次巡检就会把所有待补主播全熔断,巡检等于废掉。
"""

import time

# ── 默认参数(唯一真源在 monitor.DEFAULT_CONFIG["rid_backfill"],这里只做兜底) ──
TIERS_DEFAULT = ((40, 50), (80, 90), (110, 120))   # 分钟,闭区间
# 权重 0.5/0.3/0.2 → 均值 ≈ 71 分钟(0.5×45+0.3×85+0.2×115)。
# 刻意把重心压在短档:补号巡检的目标是"尽快发现首播",长间隔只是为了让节奏像人。
WEIGHTS_DEFAULT = (0.5, 0.3, 0.2)
WINDOW_DAYS_DEFAULT = 7

# 单主播连续失败 → 退避 6h;全局风控类冷却 3h,再犯 12h。
SINGLE_FAILS = 3
SINGLE_HOLD_SEC = 6 * 3600
GLOBAL_HOLD_SEC = 3 * 3600
GLOBAL_HOLD2_SEC = 12 * 3600
DAY_UNKNOWN_RATIO = 0.5      # 当天 unknown 比例超过这个数 → 停到次日
DAY_MIN_SAMPLES = 4          # 样本太少不判比例(3 次全失败 = 100%,不该直接停机)
DAY_MAX_COOLDOWNS = 3        # 当天触发过这么多次冷却 → 停到次日

# unknown 的原因码。前 5 个是「抖音在拦我们」(风控类),触发全局冷却;
# 网络类只让该主播退避,不该拿整台机器陪葬(本机断网时冷却毫无意义)。
RISK_REASONS = ("captcha", "login_wall", "http_429", "http_403", "xhr_blocked")
NET_REASONS = ("timeout", "nav_error")
OTHER_REASONS = ("empty_shell", "browser_crash", "rid_mismatch", "unknown")
ALL_REASONS = RISK_REASONS + NET_REASONS + OTHER_REASONS

# 成功判定(连败清零)。live 之外 offline 也算成功 —— 见模块头说明。
SUCCESS_VERDICTS = ("live", "offline")


# ── 小工具 ──────────────────────────────────────────────────────────────────
def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v, default=True):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    if v is None:
        return default
    return bool(v)


def day_str(now):
    """本地自然日,用于「当天次数/比例」的归零判定。"""
    return time.strftime("%Y-%m-%d", time.localtime(now))


def empty_state(now=0):
    """巡检状态的初始形态。落盘时原样写,读回来缺字段由 merge_state 补齐。"""
    return {
        "window_start": 0, "window_until": 0,
        "pending_key": "",                 # 上一次评估时的待补名单指纹(判「新增主播」)
        "last_run_at": 0, "next_run_at": 0,
        "last_tier": None, "last_interval_sec": 0,
        "cursor": 0,
        "day": day_str(now) if now else "",
        "checks_today": 0,
        "per_monitor": {},
        "circuit": {"global_until": 0, "reason": "", "cooldown_today": 0,
                    "unknown_today": 0, "checks_today": 0,
                    "last_round_unknown": 0},
        "last_result": {},
        "stats": {"filled_total": 0, "last_filled_at": 0, "last_filled_name": ""},
    }


def merge_state(raw, now=0):
    """读回来的 state 补齐缺字段(旧版本/手改/损坏都不许炸)。"""
    base = empty_state(now)
    if not isinstance(raw, dict):
        return base
    for k, v in raw.items():
        if k in ("per_monitor", "circuit", "stats", "last_result") and isinstance(v, dict):
            tgt = base.get(k) if isinstance(base.get(k), dict) else {}
            tgt.update(v)
            base[k] = tgt
        else:
            base[k] = v
    return base


def _monitor_state(state, name):
    per = state.setdefault("per_monitor", {})
    st = per.get(name)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("last_check", 0)
    st.setdefault("verdict", "")
    st.setdefault("rid", None)
    st.setdefault("fails", 0)
    st.setdefault("last_fail_at", 0)
    st.setdefault("last_reason", "")
    st.setdefault("backoff_until", 0)
    st.setdefault("rid_blacklist", [])
    per[name] = st
    return st


# ── 配置校验 ────────────────────────────────────────────────────────────────
def _enabled(v):
    """"auto" / true / false 三态归一。

    前端「高级参数」里是 select,存下来是字符串("auto"/"on"/"off"/"true"/"false"),
    这里必须把它们折成布尔,否则 `is True` 判断永远不成立 —— 用户点「强制开启」
    却静默按 auto 跑,是最难查的那种 bug。
    """
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "auto").strip().lower()
    if s in ("true", "1", "on", "yes", "force", "always"):
        return True
    if s in ("false", "0", "off", "no", "never", "disabled"):
        return False
    return "auto"


def normalize_cfg(section):
    """把 config 里的 rid_backfill 段规整成可用形态。

    非法配置(空 / 上下界倒置 / 档间重叠 / 权重全 0)一律回落默认并打 `_normalized`
    标记 —— 巡检是旁路功能,配置写错绝不能把主循环带崩,也不该静默乱跑。
    返回 dict,含 `_normalized`(因非法被回落)与 `_reason`("config_invalid" 或 "")。
    """
    src = section if isinstance(section, dict) else {}
    out = {
        "enabled": _enabled(src.get("enabled", "auto")),
        "window_days": max(1, _int(src.get("window_days"), WINDOW_DAYS_DEFAULT)),
        "interval_tiers_min": [list(t) for t in TIERS_DEFAULT],
        "tier_weights": list(WEIGHTS_DEFAULT),
        "avoid_same_tier_twice": _bool(src.get("avoid_same_tier_twice"), True),
        "second_jitter": _bool(src.get("second_jitter"), True),
        "max_per_round": max(1, _int(src.get("max_per_round"), 3)),
        "gap_sec_min": max(0, _int(src.get("gap_sec_min"), 25)),
        "gap_sec_max": max(0, _int(src.get("gap_sec_max"), 45)),
        "timeout_sec": max(10, _int(src.get("timeout_sec"), 45)),
        "round_budget_sec": max(30, _int(src.get("round_budget_sec"), 180)),
        "max_checks_per_day": max(1, _int(src.get("max_checks_per_day"), 100)),
        # 开关型配置必须在这里过一遍,否则调用方 `norm.get(key, 默认值)` 会永远拿到默认值 ——
        # 用户把 notify_on_fill 关掉却照样收到推送,就是这么来的(2026-10-26 实测踩到)。
        "notify_on_fill": _bool(src.get("notify_on_fill"), True),
        "quiet_hours": list(src.get("quiet_hours") or []),
        "_normalized": False,
        "_reason": "",
    }

    # ① 档位:必须是 [[lo,hi],…] 的正整数对,lo<=hi,整体升序且互不重叠。
    #    键缺失 = 用默认值(正常情况,不算非法);键存在但内容坏 = 回落默认并标记。
    tiers, ok = [], True
    raw_tiers = src.get("interval_tiers_min")
    if isinstance(raw_tiers, (list, tuple)) and len(raw_tiers) >= 1:
        for t in raw_tiers:
            if not isinstance(t, (list, tuple)) or len(t) != 2:
                ok = False
                break
            lo, hi = _int(t[0], -1), _int(t[1], -1)
            if lo <= 0 or hi <= 0 or lo > hi:
                ok = False
                break
            tiers.append([lo, hi])
        if ok:
            tiers.sort(key=lambda x: x[0])
            if any(tiers[i][1] >= tiers[i + 1][0] for i in range(len(tiers) - 1)):
                ok = False          # 相邻档重叠(边界相接也算重叠,否则同一分钟会被两档命中)
    elif raw_tiers is not None:
        ok = False                  # 给了但不是列表 → 非法
    if ok and tiers:
        out["interval_tiers_min"] = tiers
    elif raw_tiers is not None:
        out["interval_tiers_min"] = [list(t) for t in TIERS_DEFAULT]
        out["_normalized"] = True

    # ② 权重:长度与档位一致、非负、和 > 0;对不上就用「默认权重」或「等权」
    n_tiers = len(out["interval_tiers_min"])
    raw_w = src.get("tier_weights")
    ws = None
    if isinstance(raw_w, (list, tuple)) and len(raw_w) == n_tiers:
        try:
            cand = [float(x) for x in raw_w]
        except (TypeError, ValueError):
            cand = None
        if cand and all(w >= 0 for w in cand) and sum(cand) > 0:
            total = sum(cand)
            ws = [w / total for w in cand]
        else:
            out["_normalized"] = True
    elif raw_w is not None:
        out["_normalized"] = True
    if ws is None:
        # 自定义档位数量时用等权;正好是默认三档则沿用默认权重(保住 0.5/0.3/0.2)
        ws = (list(WEIGHTS_DEFAULT) if n_tiers == len(TIERS_DEFAULT)
              else [1.0 / n_tiers] * n_tiers)
    out["tier_weights"] = ws

    if out["gap_sec_max"] < out["gap_sec_min"]:
        out["gap_sec_max"] = out["gap_sec_min"]
    if out["_normalized"]:
        out["_reason"] = "config_invalid"
    return out


# ── 待补名单与窗口 ──────────────────────────────────────────────────────────
def pending_monitors(cfg):
    """`web_rid` 为空的主播(config 顺序)。空串/None/纯空格都算缺。"""
    out = []
    for m in (cfg or {}).get("monitors") or []:
        if not isinstance(m, dict):
            continue
        if not str(m.get("web_rid") or "").strip():
            out.append(m)
    return out


def pending_key(pending):
    """待补名单指纹:名字排序拼起来。用途是识别「有主播新加进来」。"""
    return "|".join(sorted(str(m.get("name") or "") for m in pending))


def ensure_window(norm, state, pending, now):
    """维护 7 天窗口。返回 (window_ok, opened, expired)。

    规则(与方案 §3.3 一致):
    - 没有待补主播 → 关窗(不自动重开),由调用方彻底停掉浏览器;
    - 有待补且从未开窗 → 开 7 天;
    - 窗口已过期但**待补名单变了**(新加了主播)→ 重新开 7 天;
    - 窗口已过期且名单没变 → 保持 expired,**不自动续期** —— 无限期自动巡检是最容易
      把 IP 打黑的做法,续期必须由人点一下或由"新主播入场"触发。
    """
    pc = state.get("circuit") if isinstance(state.get("circuit"), dict) else {}
    state["circuit"] = pc                       # 补回,防止空 state 用一半
    key = pending_key(pending)
    days = norm.get("window_days", WINDOW_DAYS_DEFAULT) * 86400

    if norm.get("enabled") is True:
        # 人工强制开:无视窗口(但没主播可补时也没意义)
        if not pending:
            return False, False, False
        if not state.get("window_until") or now >= state["window_until"]:
            state["window_start"], state["window_until"] = now, now + days
            state["pending_key"] = key
            return True, True, False
        return True, False, False

    if not pending:
        state["window_start"] = 0
        state["window_until"] = 0
        state["pending_key"] = ""
        return False, False, False

    until = _int(state.get("window_until"), 0)
    if until and now < until:
        state["pending_key"] = key
        return True, False, False
    if not until or key != str(state.get("pending_key") or ""):
        state["window_start"], state["window_until"] = now, now + days
        state["pending_key"] = key
        return True, True, False
    return False, False, True                   # 过期且名单未变


# ── 能不能跑 ────────────────────────────────────────────────────────────────
def should_run(norm, state, pending, now):
    """本轮能不能开跑。返回 (ok, reason)。reason 会直接下发前端,用来解释"为什么没跑"。"""
    if norm.get("enabled") is False:
        return False, "disabled"
    if not pending:
        return False, "no_pending"
    win_ok, _opened, expired = ensure_window(norm, state, pending, now)
    if expired:
        return False, "window_expired"
    if not win_ok:
        return False, "window_expired"
    c = state.get("circuit") or {}
    if _int(c.get("global_until"), 0) > now:
        return False, "cooling"
    if _int(state.get("checks_today"), 0) >= norm.get("max_checks_per_day", 100):
        return False, "daily_cap"
    # 所有待补主播都在退避中 → 本轮无事可做(不算故障)。注意用只读查询,
    # 不能在这里推进 cursor —— 否则每轮会被 should_run 和 task_batch 各推一次。
    if not available_monitors(state, pending, now):
        return False, "backoff"
    return True, "ok"


# ── 两段随机 ────────────────────────────────────────────────────────────────
def _weighted_choice(weights, rng, exclude=None):
    """按权重抽下标,可排除一个下标(用于「连续两轮不落同一档」)。"""
    idxs = [i for i in range(len(weights)) if i != exclude]
    if not idxs:                                # 只有一档时没得排除
        idxs = list(range(len(weights)))
    total = sum(weights[i] for i in idxs)
    if total <= 0:
        return rng.choice(idxs)
    r = rng.random() * total
    acc = 0.0
    for i in idxs:
        acc += weights[i]
        if r <= acc:
            return i
    return idxs[-1]


def plan_next_run(norm, state, now, rng=None):
    """算下一轮时间:两段随机(先随机档位,再档位内随机分钟)+ 秒级抖动。

    返回 {tier, minutes, interval_sec, next_run_at}。`now` 是**本轮结束**的时刻 ——
    从结束算起,慢轮才不会把两次访问压成贴背。
    """
    import random as _random
    rng = rng or _random.SystemRandom()
    tiers = norm.get("interval_tiers_min") or [list(t) for t in TIERS_DEFAULT]
    weights = norm.get("tier_weights") or [1.0 / len(tiers)] * len(tiers)

    exclude = None
    if norm.get("avoid_same_tier_twice") and state.get("last_tier") is not None:
        lt = _int(state.get("last_tier"), -1)
        if 0 <= lt < len(tiers):
            exclude = lt
    tier = _weighted_choice(weights, rng, exclude=exclude)
    lo, hi = tiers[tier][0], tiers[tier][1]

    minutes = rng.randint(lo, hi)
    # 与上一轮落在同一分钟 → 档内平移一格(端点回绕),保证相邻两轮不同值
    last_sec = _int(state.get("last_interval_sec"), 0)
    if last_sec and minutes * 60 <= last_sec < (minutes + 1) * 60:
        minutes = lo if minutes >= hi else minutes + 1

    interval = minutes * 60
    if norm.get("second_jitter", True):
        interval += rng.randint(0, 59)
    state["last_tier"] = tier
    state["last_interval_sec"] = interval
    return {"tier": tier, "minutes": minutes, "interval_sec": interval,
            "next_run_at": int(now) + interval}


def next_midnight(now):
    """次日 00:00(+0–10 分钟随机),日上限/日级熔断到点后的下一次时间。"""
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(base + 86400)


# ── 判定 ────────────────────────────────────────────────────────────────────
def decide_verdict(signals):
    """把浏览器层采集到的信号判成 (verdict, rid, reason)。

    signals 字段(浏览器层负责填,缺省按"没看到"处理):
      nav_error     导航异常文本
      dom_rids      DOM 里抽到的 live.douyin.com/<digits> 列表
      page_bytes    documentElement.outerHTML 长度
      see_text      页面可见文本(前若干字),用于认登录墙/滑块文案
      profile_ok    profile/other 接口是否拿到 200 + 正常 JSON
      profile_room_id_str  该接口里的 user.room_id_str
      xhr_status    profile 接口的 HTTP 状态(拿不到时 None)
      http_status   主页文档的 HTTP 状态
    """
    sig = signals or {}
    if sig.get("nav_error"):
        return "unknown", None, "nav_error"

    dom = [str(r) for r in (sig.get("dom_rids") or []) if str(r).isdigit()]
    if dom:
        return "live", dom[0], None            # 有直播入口 = 最硬的证据,优先采信

    text = str(sig.get("see_text") or "")
    if any(k in text for k in ("请完成下列验证", "拖动滑块", "安全验证", "captcha")):
        return "unknown", None, "captcha"
    if _int(sig.get("http_status"), 200) == 429:
        return "unknown", None, "http_429"
    if _int(sig.get("http_status"), 200) == 403:
        return "unknown", None, "http_403"
    if _int(sig.get("xhr_status"), 200) in (403, 412):
        return "unknown", None, "xhr_blocked"

    if not sig.get("profile_ok"):
        if any(k in text for k in ("登录后查看", "扫码登录", "登录抖音", "验证码登录")):
            return "unknown", None, "login_wall"
        if _int(sig.get("page_bytes"), 0) < 20480:
            return "unknown", None, "empty_shell"
        return "unknown", None, "unknown"

    # 接口正常返回:room_id_str 为 "0"/空 = 此刻没开播(这是**能确认的结论**)
    if str(sig.get("profile_room_id_str") or "0").strip() in ("0", ""):
        return "offline", None, None
    # 接口说在播,但 DOM 里没抓到入口链接(可能渲染慢)→ 看不清,下轮再来
    return "unknown", None, "unknown"


# ── 写回合并 ────────────────────────────────────────────────────────────────
def merge_web_rid(cfg_now, name, anchor, rid, now):
    """把 web_rid 合并进一份**刚从磁盘读出来的** cfg。返回 (新 cfg 或 None, 结果码)。

    只碰三个字段:web_rid / web_rid_source / web_rid_filled_at。其余内容原样保留 ——
    调用方写盘前还要比对 mtime,冲突就整轮放弃(见 monitor._rid_backfill_apply)。
    """
    rid = str(rid or "").strip()
    if not rid.isdigit():
        return None, "bad_rid"
    for m in (cfg_now or {}).get("monitors") or []:
        if not isinstance(m, dict):
            continue
        if m.get("name") != name:
            continue
        if str(m.get("anchor") or "") != str(anchor or ""):
            continue
        if str(m.get("web_rid") or "").strip():
            return None, "already_has_rid"
        m["web_rid"] = rid
        m["web_rid_source"] = "auto"
        m["web_rid_filled_at"] = int(now)
        return cfg_now, "ok"
    return None, "not_found"


def available_monitors(state, pending, now):
    """待补名单里**不在退避中**的那些。纯查询,不动 cursor。"""
    out = []
    for m in pending:
        st = _monitor_state(state, m.get("name"))
        if _int(st.get("backoff_until"), 0) > now:
            continue
        out.append(m)
    return out


# ── 本轮任务批次 ────────────────────────────────────────────────────────────
def task_batch(norm, state, pending, limit=None, now=None):
    """按 cursor 轮转取本轮要查的主播,跳过退避中的。会推进 cursor。

    注意 `take = min(limit, len(avail))`:待补主播少于单轮上限时**不能**用
    「不足就从头补」的写法 —— 那会把同一个人重复塞进同一轮(既浪费配额,
    又让"这一轮查了 3 个"的日志说谎)。
    """
    limit = int(limit or norm.get("max_per_round", 3))
    now = int(now if now is not None else time.time())
    avail = available_monitors(state, pending, now)
    if not avail:
        return []
    take = min(max(1, limit), len(avail))
    cur = _int(state.get("cursor"), 0) % len(avail)
    picked = (avail[cur:] + avail[:cur])[:take]
    state["cursor"] = (cur + take) % len(avail)
    return picked


# ── 熔断 ────────────────────────────────────────────────────────────────────
class Circuit:
    """三层熔断状态机。**只看 unknown,不看 offline。**

    1) 单主播:连续 3 轮 unknown → 退避 6h(任何一次 offline/live 立刻清零);
    2) 全局:风控类原因出现 1 次 → 冷却 3h,当天第 2 次起 12h;
    3) 日级:当天 unknown 比例 >50%(样本 ≥4)或当天冷却 ≥3 次 → 停到次日。

    纯网络类原因(timeout/nav_error)只影响该主播退避,不触发全局冷却 ——
    本机断网时把巡检整体停掉毫无意义,反而让日报看起来像"抖音在拦"。
    """

    def __init__(self):
        self.single_fails = SINGLE_FAILS
        self.single_hold = SINGLE_HOLD_SEC
        self.global_hold = GLOBAL_HOLD_SEC
        self.global_hold2 = GLOBAL_HOLD2_SEC
        self.day_ratio = DAY_UNKNOWN_RATIO
        self.day_min_samples = DAY_MIN_SAMPLES
        self.day_max_cooldowns = DAY_MAX_COOLDOWNS

    # -- 单次结果 ---------------------------------------------------------
    def record(self, state, name, verdict, reason, now, rid=None):
        """登记一次判定结果,返回 {action, ...} 供调用方决定日志/通知。"""
        st = _monitor_state(state, name)
        st["last_check"] = int(now)
        st["verdict"] = verdict
        c = state.setdefault("circuit", {})

        if verdict in SUCCESS_VERDICTS:
            st["fails"] = 0
            st["last_reason"] = ""
            if verdict == "live" and rid:
                st["rid"] = str(rid)
            return {"action": "success", "verdict": verdict}

        # ---- unknown ----
        st["fails"] = _int(st.get("fails"), 0) + 1
        st["last_fail_at"] = int(now)
        st["last_reason"] = reason or "unknown"
        c["unknown_today"] = _int(c.get("unknown_today"), 0) + 1

        action = "unknown"
        if reason == "rid_mismatch":
            # 抓到别人的房间号:不退避(是本机解析问题),但拉黑这个 rid 防反复抓错
            bad = str(rid or "")
            if bad and bad not in st["rid_blacklist"]:
                st["rid_blacklist"].append(bad)
            return {"action": "rid_mismatch", "reason": reason}

        if st["fails"] >= self.single_fails:
            st["backoff_until"] = int(now) + self.single_hold
            action = "single_backoff"

        if reason in RISK_REASONS:
            c["cooldown_today"] = _int(c.get("cooldown_today"), 0) + 1
            hold = (self.global_hold2 if c["cooldown_today"] >= 2
                    else self.global_hold)
            c["global_until"] = int(now) + hold
            c["reason"] = reason
            action = "global_cooldown"
        return {"action": action, "reason": reason, "fails": st["fails"],
                "backoff_until": st.get("backoff_until", 0)}

    # -- 每轮结束 ---------------------------------------------------------
    def record_round(self, state, unknown_count, checked_count, now):
        """一轮结束后判「本轮 ≥2 个 unknown」与日级阈值。返回动作或 None。"""
        c = state.setdefault("circuit", {})
        c["last_round_unknown"] = int(unknown_count)
        c["checks_today"] = _int(c.get("checks_today"), 0) + int(checked_count)

        if unknown_count >= 2 and _int(c.get("global_until"), 0) <= now:
            c["cooldown_today"] = _int(c.get("cooldown_today"), 0) + 1
            hold = (self.global_hold2 if c["cooldown_today"] >= 2
                    else self.global_hold)
            c["global_until"] = int(now) + hold
            c["reason"] = "round_unknown>=2"
            return {"action": "global_cooldown", "reason": "round_unknown>=2"}

        checks = _int(c.get("checks_today"), 0)
        unk = _int(c.get("unknown_today"), 0)
        if checks >= self.day_min_samples and checks and (unk / checks) > self.day_ratio:
            c["global_until"] = next_midnight(now)
            c["reason"] = "day_ratio"
            return {"action": "day_stop", "reason": "day_ratio"}
        if _int(c.get("cooldown_today"), 0) >= self.day_max_cooldowns:
            c["global_until"] = next_midnight(now)
            c["reason"] = "day_cooldowns"
            return {"action": "day_stop", "reason": "day_cooldowns"}
        return None

    # -- 查询 -------------------------------------------------------------
    def blocked(self, state, name, now):
        st = _monitor_state(state, name)
        until = _int(st.get("backoff_until"), 0)
        return until if until > now else None

    def global_blocked(self, state, now):
        until = _int((state.get("circuit") or {}).get("global_until"), 0)
        return until if until > now else None

    # -- 日切 -------------------------------------------------------------
    def reset_day(self, state, now):
        """跨自然日:清零当天计数(窗口/退避/连败计数都不动)。"""
        d = day_str(now)
        if state.get("day") == d:
            return False
        state["day"] = d
        state["checks_today"] = 0
        c = state.setdefault("circuit", {})
        c["unknown_today"] = 0
        c["checks_today"] = 0
        c["cooldown_today"] = 0
        return True
