#!/usr/bin/env python3
"""抖音直播弹幕/礼物抓取模块。

设计原则(与录制链路完全隔离,失败绝不影响录制):
- 匿名观众身份连接 WebSocket(webcast/im/push/v2),签名由本地 sign.js 计算,零账号。
- 每场直播每房间一条长连接;连接失败重试 3 次后静默放弃。
- 事件写入 jsonl(服务器时间戳 + 本地接收时间戳双时间),供字幕生成与后期 AI 分析。
- 协议实现参考 saermart/DouyinLiveWebFetcher(AGPL-3.0),仅本地个人使用。

jsonl 行格式:
  {"ts": 服务器毫秒, "ts_local": 本地毫秒, "type": "chat|gift|member",
   "user": 昵称, "uid": "用户ID", "content": "弹幕内容"}
  {"type": "gift", "gift": 礼物名, "count": 数量, "diamond": 单价抖币, ...}
"""
import collections
import gzip
import hashlib
import json
import os
import random
import threading
import time
import urllib.parse

import httpx
import websocket  # websocket-client
from py_mini_racer import MiniRacer

from danmaku_proto import (PushFrame, Response, ChatMessage, GiftMessage,
                           MemberMessage, LightGiftMessage)

BASE = os.path.dirname(os.path.abspath(__file__))
SIGN_JS = os.path.join(BASE, "scripts", "danmaku_sign.js")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
MAX_RETRIES = 3          # 连接失败重试次数,超过即放弃
RETRY_BACKOFF = (5, 15, 30)

_sign_ctx = None
_sign_lock = threading.Lock()


def _log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _signature_ctx():
    """编译并缓存 sign.js(首次约 0.1s,进程内复用)。线程安全。"""
    global _sign_ctx
    with _sign_lock:
        if _sign_ctx is None:
            with open(SIGN_JS, "r", encoding="utf-8") as f:
                ctx = MiniRacer()
                ctx.eval(f.read())
            _sign_ctx = ctx
    return _sign_ctx


def _wss_url(room_id: str, uuid: str) -> str:
    return ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
            "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
            "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
            "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
            "&browser_name=Mozilla&browser_version=5.0%20(Windows%20NT%2010.0%3B%20Win64%3B%20x64)"
            "%20AppleWebKit%2F537.36%20(KHTML%2C%20like%20Gecko)%20Chrome%2F126.0.0.0%20Safari%2F537.36"
            "&browser_online=true&tz_name=Asia/Shanghai"
            "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
            f"&internal_ext=internal_src:dim|wss_push_room_id:{room_id}|wss_push_did:{uuid}"
            f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
            f"wrds_v:7392094459690748497"
            f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
            f"&user_unique_id={uuid}&im_path=/webcast/im/fetch/&identity=audience"
            f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={room_id}"
            f"&heartbeatDuration=0")


def _sign(wss: str) -> str:
    """对 wss URL 的关键参数做 md5 后交给 sign.js 计算 signature。"""
    keys = ("live_id,aid,version_code,webcast_sdk_version,"
            "room_id,sub_room_id,sub_channel_id,did_rule,"
            "user_unique_id,device_platform,device_type,ac,"
            "identity").split(",")
    q = urllib.parse.urlparse(wss).query.split("&")
    m = {}
    for i in q:
        if "=" in i:
            k, _, v = i.partition("=")
            m[k] = v
    joined = ",".join(f"{k}={m.get(k, '')}" for k in keys)
    md5 = hashlib.md5(joined.encode()).hexdigest()
    return _signature_ctx().call("get_sign", md5)


def _fetch_ttwid() -> str:
    """访问抖音直播首页拿 ttwid(匿名 cookie)。失败返回空串。"""
    try:
        r = httpx.get("https://live.douyin.com/", headers={"User-Agent": UA},
                      timeout=10, follow_redirects=False)
        return r.cookies.get("ttwid") or ""
    except Exception:
        return ""


class DanmakuRecorder:
    """一个直播间的弹幕抓取会话(独立线程运行 WS,写 jsonl)。"""

    def __init__(self, name, room_id, jsonl_path, capture_member=False):
        self.name = name
        self.room_id = str(room_id)
        self.jsonl_path = jsonl_path
        self.capture_member = capture_member
        self.uuid = str(random.randint(10 ** 18, 10 ** 19 - 1))  # 19 位随机设备 ID
        self._stopped = False
        self._dead = False          # 重试耗尽,已放弃
        self._ws = None
        self._thread = None
        self._fh = None             # jsonl 句柄(带锁追加)
        self._fh_lock = threading.Lock()
        self._retries = 0
        self.last_msg_ts = 0.0      # 最近一条消息的本地时刻(缺口检测用)
        self.clock_drift_ms = 0.0   # 服务器-本地钟差 EMA(毫秒,server_now - local_now)
        self.msg_count = 0
        # ── 诊断计数(礼物丢失排查):消息类型分布 + 静默失败统计 ──
        self.method_counter = collections.Counter()   # method -> 条数,会话结束打印
        self._silent_errors = 0                       # 非 gift 类解析失败数
        self._gift_errors = 0                         # gift 类解析失败数
        self._frame_errors = 0                        # PushFrame/Response 层失败数
        self._last_frame_err_log = 0.0                # 帧错误限频打印时间戳

    # ── 生命周期 ──────────────────────────────────────────
    def start(self):
        os.makedirs(os.path.dirname(self.jsonl_path) or ".", exist_ok=True)
        self._fh = open(self.jsonl_path, "a", encoding="utf-8", buffering=1)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"danmaku-{self.name}")
        self._thread.start()

    def stop(self):
        self._stopped = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        with self._fh_lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
        # ── 会话结束诊断输出:消息类型分布(礼物丢失排查关键证据) ──
        if self.method_counter:
            dist = ", ".join(f"{m}={c}" for m, c in self.method_counter.most_common(20))
            _log(f"[弹幕] {self.name} 消息分布: {dist}")
        if self._gift_errors:
            _log(f"[弹幕] {self.name} ⚠️ gift 消息解析失败 {self._gift_errors} 条"
                 f"(proto 与线上协议可能失配)")
        if self._silent_errors:
            _log(f"[弹幕] {self.name} 非 gift 消息静默丢弃 {self._silent_errors} 条")
        if self._frame_errors:
            _log(f"[弹幕] {self.name} 帧解析失败 {self._frame_errors} 包")

    @property
    def dead(self):
        return self._dead

    def retarget(self, jsonl_path):
        """录制重启产生新文件前缀时,切换 jsonl 输出目标(同一直播会话内)。"""
        if jsonl_path == self.jsonl_path:
            return
        with self._fh_lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
            self._fh = open(jsonl_path, "a", encoding="utf-8", buffering=1)
        self.jsonl_path = jsonl_path

    # ── WS 主循环 ─────────────────────────────────────────
    def _run(self):
        while not self._stopped:
            ttwid = _fetch_ttwid()
            wss = _wss_url(self.room_id, self.uuid)
            try:
                wss += "&signature=" + _sign(wss)
            except Exception as e:
                _log(f"[弹幕] {self.name} 签名计算失败: {e}")
                break
            headers = {"cookie": f"ttwid={ttwid}", "user-agent": UA}
            self._ws = websocket.WebSocketApp(
                wss, header=headers,
                on_open=self._on_open, on_message=self._on_message,
                on_error=lambda ws, err: None, on_close=self._on_close)
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            if self._stopped:
                break
            # 断开后重试(直播间可能已结束,重试上限内放弃)
            if self._retries >= MAX_RETRIES:
                self._dead = True
                _log(f"[弹幕] {self.name} 重连 {MAX_RETRIES} 次失败,放弃弹幕抓取(录制不受影响)")
                break
            backoff = RETRY_BACKOFF[min(self._retries, len(RETRY_BACKOFF) - 1)]
            self._retries += 1
            _log(f"[弹幕] {self.name} 连接断开,{backoff}s 后重试({self._retries}/{MAX_RETRIES})")
            time.sleep(backoff)

    def _on_open(self, ws):
        _log(f"[弹幕] {self.name} 已连接 room_id={self.room_id}")
        self._retries = 0

        def heartbeat():
            # 抖音 Webcast 协议要求应用层心跳以「二进制数据帧」发送
            # PushFrame(payload_type='hb');用 WS 控制帧 PING 发送 protobuf
            # 字节服务器不识别,约 50s 无有效数据即被踢线。
            while not self._stopped:
                try:
                    hb = PushFrame(payload_type="hb").SerializeToString()
                    ws.send(hb, websocket.ABNF.OPCODE_BINARY)
                except Exception:
                    break
                time.sleep(10)
        threading.Thread(target=heartbeat, daemon=True).start()

    def _on_close(self, ws, *args):
        pass

    # ── 消息解析与落盘 ────────────────────────────────────
    def _on_message(self, ws, message):
        now_ms = time.time() * 1000
        try:
            frame = PushFrame().parse(message)
            payload = frame.payload
            if frame.payload_encoding == "gzip" or (payload[:1] == b"\x1f"):
                payload = gzip.decompress(payload)
            resp = Response().parse(payload)
        except Exception as e:
            # 帧层失败:整包丢弃。限频打印(60s 一条),避免刷屏
            self._frame_errors += 1
            now_t = time.time()
            if now_t - self._last_frame_err_log >= 60:
                self._last_frame_err_log = now_t
                _log(f"[弹幕] {self.name} 帧解析失败(60s内累计{self._frame_errors}): {e!r}")
            return

        # 服务器时间戳 → 本地钟差(EMA 平滑,纠正休眠/时钟跳变)
        if resp.now > 1e12:
            drift = resp.now - now_ms
            self.clock_drift_ms = self.clock_drift_ms * 0.9 + drift * 0.1 \
                if self.clock_drift_ms else drift

        if resp.need_ack:
            try:
                ack = PushFrame(log_id=frame.log_id, payload_type="ack",
                                payload=resp.internal_ext.encode("utf-8")).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            except Exception:
                pass

        batch_ts = resp.now if resp.now > 1e12 else int(now_ms)
        for msg in resp.messages_list:
            self._dispatch(msg, batch_ts)
        self.last_msg_ts = time.time()

    def _dispatch(self, msg, batch_ts):
        method = msg.method
        self.method_counter[method] += 1
        try:
            if method == "WebcastChatMessage":
                m = ChatMessage().parse(msg.payload)
                ts = (m.common.create_time * 1000) if m.common.create_time > 0 else batch_ts
                self._write({"ts": ts, "type": "chat",
                             "user": m.user.nick_name, "uid": str(m.user.id),
                             "content": m.content})
            elif method == "WebcastGiftMessage":
                m = GiftMessage().parse(msg.payload)
                ts = (m.common.create_time * 1000) if m.common.create_time > 0 else batch_ts
                count = m.repeat_count or m.combo_count or m.total_count or 1
                diamond = m.gift.diamond_count if m.gift else 0
                self._write({"ts": ts, "type": "gift",
                             "user": m.user.nick_name if m.user else "",
                             "uid": str(m.user.id) if m.user else "",
                             "gift": m.gift.name if m.gift else "?",
                             "count": count, "diamond": diamond})
            elif method == "WebcastLightGiftMessage":
                # 抖音新版轻礼物消息(观众端礼物主通道,替代 WebcastGiftMessage)
                # 送礼人位于 common.user;礼物名取 gift_struct.name,gift_info 仅 id/钻石
                m = LightGiftMessage().parse(msg.payload)
                ts = (m.common.create_time * 1000) if m.common.create_time > 0 else batch_ts
                cu = m.common.user
                gs, gi = m.gift_struct, m.gift_info
                gname = (gs.name if gs and gs.name
                         else (f"礼物#{gi.gift_id}" if gi and gi.gift_id else "?"))
                diamond = int((gs.diamond_count if gs else 0)
                              or (gi.diamond_count if gi else 0))
                count = int(m.count or m.repeat_count or m.combo_count
                            or m.group_count or 1)
                self._write({"ts": ts, "type": "gift",
                             "user": cu.nick_name if cu else "",
                             "uid": str(cu.id) if cu else "",
                             "gift": gname, "count": count, "diamond": diamond})
            elif method == "WebcastMemberMessage" and self.capture_member:
                m = MemberMessage().parse(msg.payload)
                ts = (m.common.create_time * 1000) if m.common.create_time > 0 else batch_ts
                self._write({"ts": ts, "type": "member",
                             "user": m.user.nick_name, "uid": str(m.user.id),
                             "content": "进入直播间"})
        except Exception as e:
            # 单条消息解析失败:记录而不是静默丢弃(礼物丢失排查关键)
            if method == "WebcastGiftMessage":
                self._gift_errors += 1
                _log(f"[弹幕] {self.name} ⚠️ GiftMessage 解析失败: {e!r}")
            else:
                self._silent_errors += 1
                _log(f"[弹幕] {self.name} {method} 解析失败: {e!r}")

    def _write(self, ev):
        ev["ts_local"] = int(time.time() * 1000)
        self.msg_count += 1
        line = json.dumps(ev, ensure_ascii=False)
        with self._fh_lock:
            try:
                if self._fh:
                    self._fh.write(line + "\n")
            except Exception:
                pass


# ── 会话管理(monitor 调用)────────────────────────────────
_sessions = {}  # name -> {"rec": DanmakuRecorder, "prefix": str}


def jsonl_for(output_path):
    """由录制输出路径推导弹幕 jsonl 路径。

    收纳到视频同目录下的 .meta/ 隐藏子目录,视频目录只保留 1 视频 + 1 字幕
    (.ass 必须与视频同名同目录供播放器自动加载;jsonl/align 无此限制)。
    接受模板(name-ts-%03d.flv)、单文件(name-ts.flv)、实际分片(name-ts-000.flv)
    三种形态,统一推导到 <dir>/.meta/<name-ts>.danmaku.jsonl。"""
    d = os.path.dirname(output_path)
    base = os.path.basename(output_path)
    for suf in ("-%03d.flv", "-%03d.mp4", "-%03d.ts"):
        if base.endswith(suf):
            return os.path.join(d, ".meta", base[: -len(suf)] + ".danmaku.jsonl")
    stem, ext = os.path.splitext(base)
    if ext in (".flv", ".mp4", ".ts"):
        if len(stem) > 3 and stem[-4] == "-" and stem[-3:].isdigit():
            stem = stem[:-4]  # 实际分片 -000.flv → 剥掉序号
        return os.path.join(d, ".meta", stem + ".danmaku.jsonl")
    return os.path.join(d, ".meta", base + ".danmaku.jsonl")


def start_for(name, room_id, output_path, cfg):
    """开播时启动弹幕会话(幂等)。返回是否启动。"""
    if name in _sessions and not _sessions[name]["rec"].dead:
        return True
    dm_cfg = cfg.get("danmaku") or {}
    if not dm_cfg.get("enabled", False):
        return False
    if not room_id:
        return False
    rec = DanmakuRecorder(name, room_id, jsonl_for(output_path),
                          capture_member=bool(dm_cfg.get("capture_member", False)))
    rec.start()
    _sessions[name] = {"rec": rec, "prefix": jsonl_for(output_path)}
    _log(f"[弹幕] {name} 会话启动: {rec.jsonl_path}")
    return True


def retarget_for(name, output_path):
    """录制文件前缀变化(ffmpeg 重启)时切换 jsonl。"""
    s = _sessions.get(name)
    if not s:
        return
    new_jsonl = jsonl_for(output_path)
    if new_jsonl != s["prefix"]:
        s["rec"].retarget(new_jsonl)
        s["prefix"] = new_jsonl
        _log(f"[弹幕] {name} 切换输出: {new_jsonl}")


def stop_for(name):
    """下播/移除时停止弹幕会话。返回会话(供字幕收尾),无会话返回 None。"""
    s = _sessions.pop(name, None)
    if not s:
        return None
    s["rec"].stop()
    _log(f"[弹幕] {name} 会话结束: 共 {s['rec'].msg_count} 条消息,"
         f"钟差 {s['rec'].clock_drift_ms:+.0f}ms")
    return s["rec"]


def get(name):
    s = _sessions.get(name)
    return s["rec"] if s else None


def all_sessions():
    return {n: s["rec"] for n, s in _sessions.items()}
