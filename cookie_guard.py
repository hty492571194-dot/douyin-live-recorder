#!/usr/bin/env python3
"""真实用户 Cookie 的使用护栏。

背景:Cookie 是**真实抖音账号的登录态**,每一次带 Cookie 的请求都在消耗账号的
风控额度。检测通道每天要跑几百轮,如果任何一轮都能随便用 Cookie,账号很快就会被
限流甚至封禁。所以这里把「能不能用 Cookie」集中到一个地方裁决,规则只有一条主线:

    Cookie 是稀缺资源,只在「值得」的时候用,且用完要有账。

三道闸门,从粗到细:
  1. 场景门:非热点时段不用(热点 = 该主播历史上大概率开播的窗口)。
     但有两种情况可以豁免 —— 见 HOTSPOT_EXEMPT,它们的共同点是「误判代价高」。
  2. 频率门:全局最小间隔 + 单主播最小间隔。前者挡住「N 个主播挤在同一分钟」
     造成的瞬时突刺(实测最多 3 个主播同时处于热点),后者挡住单个主播在
     302 持续假阳性时对同一房间反复打 API。
  3. 配额门:小时配额 + 日配额。兜住最坏情况 —— 就算前两道闸都被绕过,
     一天的 Cookie 用量也有硬上限。

统计全程留痕(放行/拒绝次数、按原因分类、剩余配额),供界面观测。
"""
import threading
import time

# 拒绝原因 —— 界面直接展示中文,日志里也用它,便于对照
REASON_TEXT = {
    "ok": "放行",
    "disabled": "护栏未启用",
    "no_cookie": "无可用 Cookie",
    "not_hotspot": "非热点时段",
    "global_interval": "全局最小间隔未到",
    "streamer_interval": "该主播间隔未到",
    "hourly_quota": "小时配额已用尽",
    "daily_quota": "日配额已用尽",
}

# 允许绕过「仅热点时段」的场景。
# conflict:判定在播却拿不到流地址 —— 要么真的在播要救回录制,要么就是假阳性要纠正,
#           两头都很值钱,不该因为「不在热点」就放弃。
# error   :302 探测自身出错,此时 302 结果不可信,需要 API 兜底。
HOTSPOT_EXEMPT = ("conflict", "error")

_DEFAULTS = {
    "enabled": True,
    "hotspot_only": True,       # 是否只在热点时段使用
    "min_interval_sec": 45.0,           # 全局最小间隔(秒)
    "per_streamer_interval_sec": 300.0,  # 同一主播最小间隔(秒)
    "hourly_quota": 60,         # 每小时配额
    "daily_quota": 300,         # 每日配额
}


class CookieGuard:
    """Cookie 使用裁决器。线程安全(webui 是多线程 HTTP 服务)。"""

    def __init__(self, cfg=None):
        self.lock = threading.Lock()
        self.rules = dict(_DEFAULTS)
        self._recent = []            # 近 1 小时的使用时间戳(滑动窗口)
        self._by_anchor = {}         # anchor -> 上次使用时间戳
        self._day_key = self._today_key()
        self._used_today = 0
        self.stats = {"allowed": 0, "denied": 0, "by_reason": {}}
        self.configure(cfg or {})

    # ── 配置 ──

    def configure(self, cfg):
        """从 config 的 detection.cookie_guard 段读取规则,缺失项用默认值。"""
        g = ((cfg or {}).get("detection") or {}).get("cookie_guard") or {}
        with self.lock:
            for k, default in _DEFAULTS.items():
                v = g.get(k, default)
                # 数值字段做类型收敛:配置里可能是字符串
                if isinstance(default, bool):
                    self.rules[k] = bool(v)
                elif isinstance(default, int):
                    # 先经 float 再取整:界面表单可能提交 "7" 或 7.9 这类值,
                    # 直接 int("7.9") 会抛 ValueError 并静默退回默认值
                    try:
                        self.rules[k] = int(float(v))
                    except (TypeError, ValueError):
                        self.rules[k] = default
                else:
                    try:
                        self.rules[k] = float(v)
                    except (TypeError, ValueError):
                        self.rules[k] = default

    # ── 裁决 ──

    def allow(self, anchor, reason="live", in_hotspot=False, has_cookie=True):
        """是否允许这次 Cookie 调用。返回 (bool, 原因码)。

        anchor      :主播锚点(用于单主播节流)
        reason      :调用场景(live / error / conflict),决定是否豁免热点门
        in_hotspot  :当前是否处于该主播的热点窗口
        has_cookie  :是否真的有 Cookie 可用
        """
        r = self.rules
        with self.lock:
            self._rollover()

            def deny(why):
                self.stats["denied"] += 1
                self.stats["by_reason"][why] = self.stats["by_reason"].get(why, 0) + 1
                return False, why

            if not r["enabled"]:
                return deny("disabled")
            if not has_cookie:
                return deny("no_cookie")
            if r["hotspot_only"] and not in_hotspot and reason not in HOTSPOT_EXEMPT:
                return deny("not_hotspot")

            now = time.time()
            if self._recent and now - self._recent[-1] < r["min_interval_sec"]:
                return deny("global_interval")
            last = self._by_anchor.get(anchor)
            if last is not None and now - last < r["per_streamer_interval_sec"]:
                return deny("streamer_interval")
            if len(self._recent) >= r["hourly_quota"]:
                return deny("hourly_quota")
            if self._used_today >= r["daily_quota"]:
                return deny("daily_quota")

            return True, "ok"

    def record(self, anchor):
        """记一次实际使用。必须在真正发起请求**之前**调用,否则并发下会超发。"""
        now = time.time()
        with self.lock:
            self._rollover()
            self._recent.append(now)
            self._by_anchor[anchor] = now
            self._used_today += 1
            self.stats["allowed"] += 1

    # ── 观测 ──

    def snapshot(self):
        """当前用量快照,供界面展示。"""
        with self.lock:
            self._rollover()
            r = self.rules
            return {
                "enabled": r["enabled"],
                "hotspot_only": r["hotspot_only"],
                "used_last_hour": len(self._recent),
                "hourly_quota": r["hourly_quota"],
                "used_today": self._used_today,
                "daily_quota": r["daily_quota"],
                "allowed": self.stats["allowed"],
                "denied": self.stats["denied"],
                "by_reason": dict(self.stats["by_reason"]),
                "hotspot_exempt": list(HOTSPOT_EXEMPT),
            }

    # ── 内部 ──

    @staticmethod
    def _today_key():
        return time.strftime("%Y-%m-%d")

    def _rollover(self):
        """滑动窗口清理 + 跨日重置。调用方需已持锁。"""
        now = time.time()
        self._recent = [t for t in self._recent if now - t < 3600]
        key = self._today_key()
        if key != self._day_key:
            self._day_key = key
            self._used_today = 0
