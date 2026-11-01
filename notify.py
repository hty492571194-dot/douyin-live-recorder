#!/usr/bin/env python3
"""告警通道层 —— 把「发生了什么」送到人能看到的地方。

通道按可靠性降级,任何一条失败都不影响主流程(告警失败了录制照录):
  1. 企微群机器人 webhook:配了才发
  2. 企微连接器(we-com-cli):发授权人本人,无需建群机器人
  3. macOS 通知中心:零配置兜底,人坐在电脑前才有用
  4. 日志:必有,永远兜底

全项目告警只认这里一套(礼物静默告警 / 登录态失效 / 每日质检日报都走 push_by_config);
gift_watch.notify_channel/webhook_url 实际是「全局告警通道」配置(第一个用户是礼物告警,
名字沿用至今)—— 新增告警别再各写一份推送代码,走这里。

企微群机器人配置:企业微信群 → 右上角「…」→ 群机器人 → 添加 → 复制 webhook URL。
  payload : {"msgtype": "text", "text": {"content": "..."}}   ← 注意是 msgtype 连写,
            与飞书的 msg_type / content.text 不是一回事,别抄混了
  限制    : 20 条/分钟(本项目多路合并后远达不到)、单条 2048 字节(这里主动截断)

第二条路:企业微信连接器(wecom-cli)。扫码授权后可直接把消息发给授权人本人,
  **无需创建群机器人**(webhook 路线要求群内 ≥3 人,没现成群时这条路更快)。
  代价:依赖连接器的授权登录态,过期后要重新扫码 —— 失败会在这里降级到
  本机通知 + 日志,不会拖垮监控。由 monitor 的 gift_watch.notify_channel 选择。
"""
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import sys

import httpx

# 企微群机器人单条 2048 字节硬上限;aibot markdown 是 20480,都留余量
MAX_BYTES = 1800
WECOM_MD_MAX = 20000

_log_sink = None          # 由 monitor 注入(本模块绝不反向 import monitor)


def set_log(fn):
    """注入项目日志函数;不注入则退回 print。"""
    global _log_sink
    _log_sink = fn


def _log(msg):
    if _log_sink:
        try:
            _log_sink(msg)
            return
        except Exception:
            pass
    print(msg, flush=True)


def _clip(text, limit=MAX_BYTES):
    """按 UTF-8 字节截断 —— 中文 3 字节,按字符切会切出半个字。"""
    raw = str(text).encode("utf-8")
    if len(raw) <= limit:
        return str(text)
    tail = "…(已截断)".encode("utf-8")
    keep = max(0, limit - len(tail))
    return raw[:keep].decode("utf-8", "ignore") + "…(已截断)"


def wecom_payload(text):
    """企微群机器人文本消息体。"""
    return {"msgtype": "text", "text": {"content": _clip(text)}}


def _as_str(s):
    """AppleScript 字符串字面量:转义反斜杠/双引号,换行压成空格(脚本必须单行)。"""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return '"%s"' % s


def local_notify(title, body=""):
    """macOS 通知中心弹窗。

    launchd 托管下能不能显示取决于 GUI 会话,失败返回 False 由调用方降级到日志,
    不抛异常 —— 通知弹不出来绝不能拖累监控主流程。
    """
    if sys.platform != "darwin":
        return False
    try:
        script = "display notification %s with title %s" % (_as_str(body), _as_str(title))
        r = subprocess.run(["osascript", "-e", script], timeout=5,
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def resolve_channels(gw):
    """按 gift_watch.notify_channel 决定启用哪些通道(通道分发的唯一真源)。

    webhook    只走群机器人(未配 URL 时等于不发远端)
    wecom_cli  只走连接器(发授权人本人)
    auto(默认) 配了 webhook 走 webhook,否则走连接器
    """
    channel = str((gw or {}).get("notify_channel") or "auto").lower()
    url = str((gw or {}).get("webhook_url") or "").strip()
    if channel == "webhook":
        return {"url": url, "wecom_cli": False}
    if channel == "wecom_cli":
        return {"url": "", "wecom_cli": True}
    return {"url": url if url else "", "wecom_cli": not url}


async def push_by_config(gw, title, body="", local=None):
    """按 gift_watch 配置发一条告警(webhook / 连接器 / 本机,通道分发见 resolve_channels)。

    monitor 的礼物告警与 webui 的「发送测试通知」都走这里 —— 通道规则只写一遍。
    """
    ch = resolve_channels(gw)
    loc = bool((gw or {}).get("local_notify", True)) if local is None else local
    return await push_async(title, body, url=ch["url"], local=loc, wecom_cli=ch["wecom_cli"])


async def push_async(title, body="", url="", local=True, wecom_cli=False):
    """发一条告警。返回 {"ok": bool, "channels": [...], "error": str}。

    url       企微群机器人 webhook;空串 = 不走这条通道
    local     是否弹 macOS 通知中心
    wecom_cli 是否经企业微信连接器(we-com-cli)发给授权人本人
    ok        一个通道都没配/都失败时,没配通道不算失败,配了发不出才算
    """
    text = f"{title}\n{body}" if body else title
    channels, err = [], ""
    if url:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
                r = await c.post(url, json=wecom_payload(text))
            try:
                data = r.json()
            except Exception:
                data = {}
            # 企微成功返回 errcode=0;失败给 errmsg(如频率限制、key 失效)
            if r.status_code == 200 and int(data.get("errcode", 0) or 0) == 0:
                channels.append("wecom")
            else:
                err = f"HTTP{r.status_code} errcode={data.get('errcode')} {data.get('errmsg', '')}".strip()
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    if wecom_cli:
        # wecom_cli_notify 是子进程调用(阻塞),丢线程池里跑,别卡事件循环
        ok_cli, err_cli = await asyncio.get_running_loop().run_in_executor(
            None, wecom_cli_notify, text)
        if ok_cli:
            channels.append("wecom-cli")
        else:
            err = (err + "; " if err else "") + f"wecom-cli: {err_cli}"
    if local:
        if local_notify(title, body):
            channels.append("local")
    _log("[告警] " + title
         + (" | 通道:" + ",".join(channels) if channels else " | 仅日志")
         + (f" | {err}" if err else ""))
    return {"ok": (not url and not wecom_cli) or bool(channels), "channels": channels, "error": err}


# ── 企业微信连接器通道(wecom-cli)──────────────────────────────────────
#
# 为什么不直接用 webhook:群机器人要求群内 ≥3 人且要手动添加;连接器扫码授权后
# 可以直接把消息发给授权人本人,门槛为零。代价是依赖授权登录态(过期需重新扫码),
# 所以设计成失败即降级 —— 告警通道挂了绝不能拖垮监控主流程。

# wecom-cli 的 aibot markdown 上限 20480 UTF-8 字节(见 WECOM_MD_MAX)


def _find_wecom_cli():
    """定位 wecom-cli。交互终端 which 能命中;launchd 的 PATH 极短,必须逐个探测。"""
    p = shutil.which("wecom-cli")
    if p:
        return p
    for cand in (
        os.path.expanduser("~/.workbuddy/binaries/node/cli-connector-packages/bin/wecom-cli"),
        "/usr/local/bin/wecom-cli", "/opt/homebrew/bin/wecom-cli",
    ):
        if os.path.isfile(cand):
            return cand
    return ""


def _cli_env():
    """wecom-cli 是 node 脚本(shebang 用 env node),launchd 的 PATH 里没有 node,
    不补 PATH 的话 CLI 一起来就 ENOENT —— 这是这条路最大的坑,单独一个函数钉住。"""
    env = dict(os.environ)
    parts = [env.get("PATH", "")]
    parts += glob.glob(os.path.expanduser("~/.workbuddy/binaries/node/versions/*/bin"))
    parts += [os.path.expanduser("~/.workbuddy/binaries/node/cli-connector-packages/bin"),
              "/usr/local/bin", "/opt/homebrew/bin"]
    env["PATH"] = ":".join(p for p in parts if p)
    return env


# whoami 输出里机器人身份与授权真人身份各有一个 ID,必须锚定「授权真人用户身份」
# 取它后面那个,拿错了会发给机器人自己(收不到任何东西且不报错)
_RE_AUTHORIZED_ID = re.compile(r"授权真人用户身份[\s\S]{0,300}?ID：(\S+)")


def _authorized_id(cli):
    """whoami → 授权真人用户 ID(告警收件人)。拿不到返回空串,由调用方降级。"""
    try:
        r = subprocess.run([cli, "identity", "whoami"], capture_output=True,
                           text=True, timeout=20, env=_cli_env())
        out = r.stdout or ""
        try:   # 输出是 JSON,真人 ID 藏在 extra_identity_context 说明文本里
            data = json.loads(out[out.index("{"): out.rindex("}") + 1])
            out = str(data.get("extra_identity_context", "")) + out
        except Exception:
            pass
        m = _RE_AUTHORIZED_ID.search(out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def wecom_cli_notify(text, timeout=25):
    """通过 wecom-cli 把告警发给授权人本人。返回 (ok, error),失败绝不抛异常。"""
    cli = _find_wecom_cli()
    if not cli:
        return False, "wecom-cli 未找到"
    try:
        uid = _authorized_id(cli)
        if not uid:
            return False, "wecom-cli 未授权(需要在企业微信里重新扫码)"
        payload = json.dumps({"chat_id": uid, "msg_type": "markdown",
                              "markdown": {"content": _clip(text, WECOM_MD_MAX)}})
        r = subprocess.run([cli, "message", "aibot", "send", "--json", payload],
                           capture_output=True, text=True, timeout=timeout, env=_cli_env())
        ok = '"success": true' in (r.stdout or "")
        return (ok, "" if ok else f"send 未成功: {(r.stdout or r.stderr or '')[:160]}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
