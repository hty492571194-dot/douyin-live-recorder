#!/usr/bin/env python3
"""企微机器人双向交互(菜单模式) —— bot.py

交互设计(2026-10-12 定稿):
  用户发任意消息 →
    ① 是「确认/取消」且有挂起的写操作 → 执行/放弃,并回报结果
    ② 匹配到某功能的关键词 → 立即执行命令,或返回该功能的用法引导
    ③ 什么都不是 → 回菜单(当前支持的全部功能 + 用法提示)

怎么加新功能(设计目标:只动一处):
  在 ACTIONS 注册表加一项 {key,label,patterns,kind,run,...} —— 菜单文案自动收录,
  引导/确认流程按 kind 自动生效。本文件不写死任何菜单文案。

安全红线:
  - 命令解析是白名单正则全匹配;消息内容**永不**进入任何动态执行路径(eval/exec/format 拼命令)。
    CLI 的输出自带 security_notice(外部不可信来源) —— 把消息当纯文本对待。
  - 写操作三道闸:sender 必须 == 授权人 → 参数校验(重名/格式) → 用户显式回复「确认」才执行。
  - 收消息是轮询拉取(chat messages list),失败/超时静默降级,绝不拖垮监控主循环。

消息结构说明:官方文档未给 messages list 的条目样例,_extract 按多候选键容错;
首次收到无法解析的消息会 dump 原始结构到日志(同 msg_id 只 dump 一次),便于修正键名。
"""
import asyncio
import datetime
import json
import os
import re
import shutil
import subprocess
import time

import notify as notify_mod

# 待确认的写操作(模块级:同一进程内全局只有一份会话流)
_PENDING = None          # {"kind":..., "args":..., "expires": ts} 或 None
_CONFIRM_TTL = 300       # 确认有效期(秒),过期作废

# 已处理消息 id(防重:轮询窗口有重叠 + 重启后 bot_state.json 兜底)
_SEEN = set()
_SEEN_MAX = 400
_LAST_POLL = 0.0         # 上次拉取窗口起点;0 = 未初始化(启动时设为当前时间)
_CHAT_CACHE = None       # 授权人 id 缓存(避免每轮 whoami)
_UNKNOWN_DUMPED = set()  # 已 dump 过原始结构的 msg_id


# ── CLI 包装(路径/环境复用 notify 的探测与 PATH 补丁) ────────────────

def _run_cli(args, timeout=40):
    """跑一条 wecom-cli 命令,返回 (dict, err)。输出可能夹 JSON 外文本,取花括号段解析。"""
    cli = notify_mod._find_wecom_cli()
    if not cli:
        return None, "wecom-cli 未找到"
    try:
        r = subprocess.run([cli] + args, capture_output=True, text=True,
                           timeout=timeout, env=notify_mod._cli_env())
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    out = r.stdout or ""
    try:
        d = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except Exception:
        return None, f"输出解析失败: {out[:120]!r}"
    if isinstance(d.get("error"), dict):
        return None, f"API 错误: {str(d['error'].get('message', ''))[:120]}"
    return d, ""


def _resolve_chat_id(log=print):
    """授权人 id(收发都发他本人)。缓存;拿不到返回空串。"""
    global _CHAT_CACHE
    if _CHAT_CACHE:
        return _CHAT_CACHE
    d, err = _run_cli(["identity", "whoami"], timeout=25)
    if not d:
        log(f"[机器人] whoami 失败: {err}")
        return ""
    blob = json.dumps(d, ensure_ascii=False)
    m = notify_mod._RE_AUTHORIZED_ID.search(blob)
    if not m:
        log("[机器人] whoami 输出里找不到授权真人 ID")
        return ""
    _CHAT_CACHE = m.group(1)
    return _CHAT_CACHE


def _fetch_messages(chat_id, begin_ts, end_ts):
    """拉 (messages, err)。时间窗超过 7 天会被 API 拒,调用方保证窗口短。"""
    fmt = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    d, err = _run_cli(["chat", "messages", "list",
                       "--chat-id", chat_id,
                       "--begin-time", fmt(begin_ts), "--end-time", fmt(end_ts)])
    if not d:
        return None, err
    return d.get("messages") or [], ""


def _extract(msg):
    """容错提取 (msg_id, text, sender)。结构未定稿,候选键取第一个命中的。"""
    if not isinstance(msg, dict):
        return None, "", ""
    mid = msg.get("id") or msg.get("msg_id") or msg.get("message_id") or ""
    content = msg.get("text")
    if isinstance(content, dict):
        content = content.get("content") or content.get("text")
    if not isinstance(content, str):
        content = msg.get("content")
    if isinstance(content, dict):
        content = content.get("content")
    text = content.strip() if isinstance(content, str) else ""
    sender = msg.get("from") or msg.get("sender") or msg.get("userid") or ""
    if isinstance(sender, dict):
        sender = sender.get("id") or sender.get("userid") or ""
    return str(mid), text, str(sender)


# ── 功能实现(命令类) ────────────────────────────────────────────────

def _cmd_menu(state, cfg, m=None):
    return menu_text(cfg)


def _cmd_recording(state, cfg, m=None):
    st = state.get_status() or {}
    recs = st.get("recordings") or {}
    live = [(n, r) for n, r in recs.items()
            if isinstance(r, dict) and r.get("recording")]
    if not live:
        return "当前没有正在录制的场次。"
    lines = ["🔴 正在录制 %d 路:" % len(live)]
    for n, r in live:
        start = r.get("started_at") or 0
        dur = format_dur(time.time() - start) if start else "?"
        seg = f" · {r['segments']} 段" if r.get("segments") else ""
        lines.append(f"▸ {n}(已录 {dur}{seg})")
    return "\n".join(lines)


def _stage_root(cfg):
    """录制暂存根(唯一真源 monitor._resolve_output_dir,空=默认根)。

    2026-10-30 暂存外迁前这里直接读 `recorder.output_dir`,空串时
    `shutil.disk_usage("")` 抛异常又被 `except: pass` 吞掉 —— 机器人
    「主盘剩余」这一行从来就没显示过,而且没人发现。
    """
    try:
        import monitor as mon
        return mon._resolve_output_dir(cfg)
    except Exception:                          # noqa: BLE001
        out = (cfg.get("recorder") or {}).get("output_dir") or ""
        return os.path.expanduser(out) if out else ""


def _cmd_status(state, cfg, m=None):
    st = state.get_status() or {}
    recs = st.get("recordings") or {}
    live = sum(1 for r in recs.values()
               if isinstance(r, dict) and r.get("recording"))
    today = time.strftime("%Y-%m-%d")
    n_today = sum(1 for x in (state.get_history() or [])
                  if time.strftime("%Y-%m-%d", time.localtime(x.get("started_at") or 0)) == today)
    dr = cfg.get("daily_report") or {}
    cookie = "正常" if _cookie_ok(cfg) else "⚠ 异常(详见日报/系统健康)"
    lines = ["📊 今日概要:",
             f"▸ 正在录制:{live} 路",
             f"▸ 今日场次:{n_today}",
             f"▸ 登录态:{cookie}"]
    out_dir = _stage_root(cfg)
    try:
        free = shutil.disk_usage(out_dir).free / 2**30
        lines.append(f"▸ 主盘剩余:{free:.0f}GB")
    except Exception:
        pass
    return "\n".join(lines)


def _cmd_disk(state, cfg, m=None):
    out_dir = _stage_root(cfg)
    lines = []
    for label, path in (("主盘", out_dir),
                        ("归档盘", (cfg.get("archive") or {}).get("external_dir") or "")):
        if not path or not os.path.isdir(path):
            continue
        try:
            u = shutil.disk_usage(path)
            lines.append(f"▸ {label}:剩 {u.free / 2**30:.0f}GB / 共 {u.total / 2**30:.0f}GB")
        except Exception:
            lines.append(f"▸ {label}:容量查询失败")
    return "\n".join(lines) if lines else "没有可查询的磁盘路径。"


def _cmd_console(state, cfg, m=None):
    host = (cfg.get("webui") or {}).get("host", "127.0.0.1")
    port = (cfg.get("webui") or {}).get("port", 8780)
    addr = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    return ("🖥 控制台地址(网页端,功能更全):\n"
            f"http://{addr}:{port}\n"
            "(当前仅本机可访问;手机访问需改绑定并加鉴权,暂未开放)")


def _cookie_ok(cfg):
    try:
        import auth as auth_mod
        return bool((auth_mod.status(cfg) or {}).get("ok"))
    except Exception:
        return False


def format_dur(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m} 分 {s:02d} 秒" if m else f"{s} 秒"


# ── 添加主播(引导 + 参数 + 确认三段式) ──────────────────────────────

_RE_ANCHOR = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")


def _cmd_add(state, cfg, m):
    """kind=guide:带参数进确认流,不带参数回用法。m 是正则匹配对象。"""
    anchor = (m.group(1) if m and m.group(1) else "").strip()
    if not anchor:
        return ("➕ 添加主播的用法:\n"
                "发「添加主播 抖音号」(例:添加主播 li777cm)\n"
                "我会先和你确认,再执行。也可以在控制台网页端操作。")
    if not _RE_ANCHOR.fullmatch(anchor):
        return (f"「{anchor}」不像有效的抖音号(只允许字母/数字/._-,2~64 位)。\n"
                "请检查后重新发送:添加主播 抖音号")
    monitors = state.get_config().get("monitors", [])
    if any(x.get("anchor") == anchor or x.get("douyin_id") == anchor for x in monitors):
        return f"「{anchor}」已经在监控列表里了,不用重复添加。"
    _set_pending({"kind": "add", "anchor": anchor})
    return (f"将添加抖音号「{anchor}」为监控主播(自动录制:开)。\n"
            f"回复「确认」执行,「取消」放弃({_CONFIRM_TTL // 60} 分钟内有效)。")


def _do_add(state, cfg, args):
    """真正写入:读-改-写**增量**追加 —— 绝不整体替换 monitors(误删 9 位主播的前科)。"""
    anchor = args.get("anchor", "")
    monitors = list(state.get_config().get("monitors", []))
    if any(x.get("anchor") == anchor or x.get("douyin_id") == anchor for x in monitors):
        return f"「{anchor}」已在监控列表(刚才可能已添加过)。"
    monitors.append({"name": anchor, "anchor": anchor, "auto_record": True})
    res = state.update_config({"monitors": monitors})
    if not res or not res.get("ok"):
        return f"❌ 添加失败:{(res or {}).get('error', '未知错误')}"
    return (f"✅ 已添加「{anchor}」并开始监控(自动录制:开)。\n"
            "检测到开播后自动录制;可在控制台主播管理里改名与调整。")


# ── 注册表(加功能 = 加一项)──────────────────────────────────────────
#
# kind 说明:
#   command  **整条消息**等于关键词才触发(fullmatch) —— 防"我没看录制中的回放"误触发
#   guide    宽松 search 抓参数:没参数回用法,带参数进确认流
# pattern 命中的正则会作为 m 传给 run(guide 类用来抓参数)。

ACTIONS = [
    {"key": "menu", "label": "菜单", "patterns": [r"菜单", r"帮助", r"功能", r"你能做什么", r"在吗"],
     "kind": "command", "run": _cmd_menu},
    {"key": "recording", "label": "查询录制中", "patterns": [r"录制中", r"在录", r"谁在播", r"谁在录"],
     "hint": "查当前正在录制的主播", "kind": "command", "run": _cmd_recording},
    {"key": "status", "label": "今日概要", "patterns": [r"状态", r"今日概要"],
     "hint": "在录/今日场次/登录态/磁盘一览", "kind": "command", "run": _cmd_status},
    {"key": "disk", "label": "查磁盘", "patterns": [r"磁盘", r"剩余空间"],
     "hint": "查各盘剩余空间", "kind": "command", "run": _cmd_disk},
    {"key": "add", "label": "添加主播", "patterns": [r"添加主播\s*[:：]?\s*(\S+)?", r"加个?主播\s*[:：]?\s*(\S+)?"],
     "hint": "发「添加主播 抖音号」,确认后生效", "kind": "guide", "run": _cmd_add},
    {"key": "console", "label": "控制台", "patterns": [r"控制台", r"网页"],
     "hint": "网页端地址", "kind": "command", "run": _cmd_console},
]

_RE_OF = {a["key"]: [re.compile(p, re.I) for p in a["patterns"]] for a in ACTIONS}


def menu_text(cfg=None):
    """菜单由 ACTIONS 自动生成 —— 注册表改了菜单就变,不用维护两份。"""
    lines = ["🤖 我能做这些(发关键词即可):"]
    for a in ACTIONS:
        if a["key"] == "menu":
            continue
        lines.append(f"▸ 「{a['label']}」 {a.get('hint', '')}")
    lines.append("看不懂的消息我会回这份菜单;发「菜单」随时回到这里。")
    return "\n".join(lines)


# ── 核心分发(除 _PENDING 外无副作用,可直接单测) ─────────────────────

def _set_pending(p):
    global _PENDING
    _PENDING = dict(p, expires=time.time() + _CONFIRM_TTL)


def _exec_pending(state, cfg):
    global _PENDING
    p, _PENDING = _PENDING, None
    if p and p["kind"] == "add":
        return _do_add(state, cfg, p)
    return "未知操作,已丢弃。"


def handle(text, state, cfg):
    """分发一条用户消息,返回要回复的文本。"""
    t = (text or "").strip()
    if not t:
        return ""
    global _PENDING

    # ① 确认/取消流优先(有过期)
    if _PENDING and time.time() > _PENDING.get("expires", 0):
        _PENDING = None
    if _PENDING:
        if re.fullmatch(r"确认|确定|好|是的?|ok", t, re.I):
            return _exec_pending(state, cfg)
        if re.fullmatch(r"取消|算了|不用了?|no", t, re.I):
            _PENDING = None
            return "已取消,什么都没改。"

    # ② 白名单正则匹配注册表
    for a in ACTIONS:
        for pat in _RE_OF[a["key"]]:
            m = (pat.fullmatch(t) if a["kind"] == "command" else pat.search(t))
            if m:
                return a["run"](state, cfg, m)

    # ③ 兜底:菜单(用户明确要求:不能理解的消息回菜单选项)
    return menu_text(cfg)


# ── 轮询协程 ────────────────────────────────────────────────────────

def _state_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


def _load_seen():
    global _SEEN
    try:
        with open(_state_path(), encoding="utf-8") as f:
            _SEEN = set(json.load(f).get("seen") or [])
    except Exception:
        _SEEN = set()


def _save_seen():
    try:
        keep = list(_SEEN)[-_SEEN_MAX:]
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seen": keep}, f)
        os.replace(tmp, _state_path())
    except Exception:
        pass


async def poll_loop(state, log=print):
    """轮询授权人单聊 → 分发 → 回复。任何异常自愈,绝不拖垮主循环。"""
    global _LAST_POLL
    _load_seen()
    _LAST_POLL = time.time()          # 启动前的历史消息不处理(seen 兜底)
    log("[机器人] 交互轮询已启动(菜单模式;发「菜单」查看支持的功能)")
    while True:
        try:
            cfg = state.get_config()
            try:
                import health as health_mod
                health_mod.beat("botloop")
            except Exception:
                pass
            b = cfg.get("bot") or {}
            interval = max(15, float(b.get("poll_interval", 45) or 45))
            if b.get("enabled", True):
                chat_id = await asyncio.get_running_loop().run_in_executor(
                    None, _resolve_chat_id, log)
                if chat_id:
                    msgs, err = await asyncio.get_running_loop().run_in_executor(
                        None, _fetch_messages, chat_id,
                        _LAST_POLL - 30, time.time() + 1)
                    if err:
                        log(f"[机器人] 拉取失败: {err}"[:180])
                    else:
                        for msg in msgs or []:
                            mid, text, sender = _extract(msg)
                            if not mid or mid in _SEEN:
                                continue
                            _SEEN.add(mid)
                            _save_seen()
                            if not text:
                                # 结构未知:dump 一次原始结构到日志,便于修正 _extract 的候选键
                                if mid not in _UNKNOWN_DUMPED:
                                    _UNKNOWN_DUMPED.add(mid)
                                    log(f"[机器人] 无法解析的消息结构: "
                                        f"{json.dumps(msg, ensure_ascii=False)[:400]}")
                                continue
                            # 单聊会话只有授权人+机器人(机器人消息不进列表),
                            # sender 缺失视为授权人;配了 allowed_sender 则严格校验
                            allowed = b.get("allowed_sender") or ""
                            if allowed and sender and sender != allowed:
                                log(f"[机器人] 忽略非授权发送者: {sender[:20]}")
                                continue
                            reply = handle(text, state, cfg)
                            if reply:
                                ok = await asyncio.get_running_loop().run_in_executor(
                                    None, notify_mod.wecom_cli_notify, reply)
                                if not ok[0]:
                                    log(f"[机器人] 回复发送失败: {ok[1]}"[:150])
                    _LAST_POLL = time.time()
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[机器人] 轮询异常: {type(e).__name__}: {e}"[:180])
            await asyncio.sleep(60)


if __name__ == "__main__":
    # 手动自测:python bot.py "录制中"
    import sys

    class _S:
        def get_config(self):
            return {}
        def get_status(self):
            return {"recordings": {}}
        def get_history(self):
            return []
        def update_config(self, patch):
            return {"ok": True}

    print(handle(sys.argv[1] if len(sys.argv) > 1 else "菜单", _S(), {}))
