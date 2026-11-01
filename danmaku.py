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

礼物口径(2026-10-29 定案,全主播统一):**唯一真源是轻礼物通道**
(`WebcastLightGiftMessage`)。完整通道(`WebcastGiftMessage`)匿名侧 349 场实测
恒 0 条,且与轻通道是服务端「事件级互斥路由」—— 两条都收会把同一笔礼物重复
计数。所以:轻礼物直接落 jsonl;完整通道事件只计数 + 打一行限频日志,不落盘、
不参与任何统计或健康度。健康度只判定「轻通道给了什么 vs 实际录下了什么」,
不判定「谁送的/送的是什么」(轻通道协议本身不含这两项,换登录态也拿不到)。
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
                           MemberMessage, LightGiftMessage, RoomRankMessage)
import gift_display as gift_display_mod   # 礼物展示规则(唯一真源，纯逻辑)
import incidents                            # 崩溃/故障事件流(零依赖,不反向 import monitor)

BASE = os.path.dirname(os.path.abspath(__file__))
SIGN_JS = os.path.join(BASE, "scripts", "danmaku_sign.js")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
MAX_RETRIES = 3          # 连接失败重试次数,超过即放弃
RETRY_BACKOFF = (5, 15, 30)

# 实时预览环形缓冲容量:预页面首开时能拿到约最近 300 条(足够铺满一屏+回看),
# 更久远的历史走 jsonl 文件(既有能力,不受影响)。
_FEED_MAX = 300

# 贡献榜快照落盘间隔(秒)。服务端 3-5 条/秒地推 RoomRank,全落会把 jsonl
# 撑爆;60s 一条足够回答"这一分钟谁在涨分",又不至于丢大额打赏。
_RANK_EMIT_SEC = 60

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

    def __init__(self, name, room_id, jsonl_path, capture_member=False, cfg=None):
        self.name = name
        self.cfg = cfg or {}
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
        # ── 礼物计量(礼物静默告警的判定基准)──────────────────────
        # last_gift_ts 用「本地墙钟」而不是消息里的服务器时间:静默时长要跟
        # 「本场录了多久」比,服务器时间带钟差与漂移(见 clock_drift_ms),
        # 拿它算时长会漂出几十秒。这两个字段在 WS 线程写、监控协程读,
        # CPython 下单次 float/int 读写是原子的,无需加锁。
        self.last_gift_ts = 0.0      # 最后一次收到礼物的本地时刻(0=本场还没收到过)
        self.gift_count = 0          # 本场累计礼物条数(仅轻通道,见下)
        # ── 按 jsonl 的对账账本(2026-10-29 二次修复)─────────────────
        # 三段账天生属于「某一份 jsonl」,而录制重启会换 jsonl(retarget)。
        # 收尾结算(_history_finish)读历史条目时可能已经切到新文件:只读**当前**
        # 计数就会拿 B 场的「收到」去对 A 场的「落盘」,`lost` 凭空变大或变小。
        # 所以 retarget 把上一份的账存档,结算方按 jsonl 路径取对应账本
        # (见 ledger_for)。键是 jsonl 绝对路径,值是 _ledger_snapshot()。
        self._gift_ledgers = {}
        # ── 轻通道分账(2026-10-29):「收到 / 落盘 / 失败」三段对账 ──────
        # 礼物唯一真源 = WebcastLightGiftMessage(见模块 docstring)。健康度只回答
        # 一件事:轻通道下发的每一条,是不是都完整落进了 jsonl。因此必须把三个
        # 环节分开计数 —— 合成一个数字就再也说不清「丢在哪一步」。
        self.gift_light_recv = 0     # 收到的轻礼物消息数(含解析失败,dispatch 入口即加)
        self.gift_light_count = 0    # 成功写入 jsonl 的轻礼物条数
        self.gift_full_count = 0     # 完整通道事件数(匿名恒 0,仅诊断口径)
        self._light_field_miss = collections.Counter()  # 轻通道 4 字段缺失计数
        self._light_write_errors = 0  # 礼物行写盘失败数(磁盘满/句柄失效)
        self._full_ignored_log = 0.0  # 完整通道事件被忽略的限频日志时间戳
        self.chat_count = 0          # 聊天事件数(判「房间是否活跃」用)
        self._started_local = 0.0    # 会话起始本地时刻(算每分钟聊天数)
        # ── 贡献榜快照(RoomRank)──────────────────────────────────
        # 盲场(full_only)里逐条礼物拿不到,但榜单分=累计送礼抖币仍会下发,
        # 按 _RANK_EMIT_SEC 限频落盘,供下游算"何时有人送大额/累计多少"。
        self.rank_snapshots = 0      # 本场落盘条数
        self._rank_last_emit = 0.0   # 上次落盘本地时刻(限频)
        self._rank_last_sig = None   # 上次落盘的榜面签名(去重,没变化不写)
        # 榜单快照写独立文件:danmaku.jsonl 的 type 集合是对外契约
        # (chat/gift/member),往里塞 rank 会波及字幕与下游 asrclip;
        # 用 .giftrank.jsonl 与既有 .giftwatch.jsonl 同级同风格。
        self.rank_path = giftrank_for(jsonl_path)
        self._rfh = None
        # ── 诊断计数(礼物丢失排查):消息类型分布 + 静默失败统计 ──
        self.method_counter = collections.Counter()   # method -> 条数,会话结束打印
        self._silent_errors = 0                       # 其他消息解析失败数
        self._gift_errors = 0                         # 完整礼物类解析失败数
        self._light_gift_errors = 0                   # 轻礼物解析失败数(独立计,否则会在告警里"隐形")
        self._frame_errors = 0                        # PushFrame/Response 层失败数
        self._last_frame_err_log = 0.0                # 帧错误限频打印时间戳
        # 实时预览的礼物展示语义(名称/车道/文案)由 gift_display 统一产出，
        # 前端只渲染；字典懒加载一次(失败则为 None，名称降级为 礼物#id)
        self._gift_dict = None
        self._gift_dict_tried = False
        # ── 实时预览环形缓冲(WebUI 弹幕/礼物面板增量读取)──────────
        # 只保留最近 _FEED_MAX 条、带自增 seq;jsonl 落盘逻辑一行不动。
        # 写入在 WS 线程、读取在 webui 线程,统一走 _fh_lock 保证快照一致。
        self._feed = collections.deque(maxlen=_FEED_MAX)
        self._feed_seq = 0

    # ── 生命周期 ──────────────────────────────────────────
    def start(self):
        os.makedirs(os.path.dirname(self.jsonl_path) or ".", exist_ok=True)
        self._started_local = time.time()
        self._fh = open(self.jsonl_path, "a", encoding="utf-8", buffering=1)
        try:
            os.makedirs(os.path.dirname(self.rank_path) or ".", exist_ok=True)
            self._rfh = open(self.rank_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._rfh = None      # 榜单文件开不了不影响主链路
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
            try:
                if self._rfh:
                    self._rfh.close()
            except Exception:
                pass
        # ── 会话结束诊断输出:消息类型分布(礼物丢失排查关键证据) ──
        if self.method_counter:
            dist = ", ".join(f"{m}={c}" for m, c in self.method_counter.most_common(20))
            _log(f"[弹幕] {self.name} 消息分布: {dist}")
        if self._gift_errors:
            _log(f"[弹幕] {self.name} ⚠️ gift 消息解析失败 {self._gift_errors} 条"
                 f"(proto 与线上协议可能失配)")
        if self._light_gift_errors:
            _log(f"[弹幕] {self.name} ⚠️ 轻礼物消息解析失败 {self._light_gift_errors} 条"
                 f"(匿名侧唯一的礼物来源,proto 与线上协议可能失配)")
        if self._silent_errors:
            _log(f"[弹幕] {self.name} 其他消息解析失败 {self._silent_errors} 条")
        if self._frame_errors:
            _log(f"[弹幕] {self.name} 帧解析失败 {self._frame_errors} 包")
        # ── 通道自报(2026-10-29):本场礼物可见性到底属于哪一类 ──
        # 不看这行,0 条礼物会被误读成"没人送礼";实际可能是服务端只走完整通道。
        # 2026-10-29 二次收口:礼物口径固定轻通道,故这里同时打出「收到/落盘/失败」
        # 三段账 —— 它们不相等就说明数据没落全,而不是"这场没人送礼"。
        dur = (time.time() - self._started_local) if self._started_local else 0.0
        ch = classify_gift_channel(self.gift_light_recv, self.chat_count, dur)
        miss = {k: v for k, v in self._light_field_miss.items() if v}
        _log(f"[弹幕] {self.name} 礼物通道={ch}"
             f"(轻收 {self.gift_light_recv} · 落盘 {self.gift_light_count} · "
             f"解析失败 {self._light_gift_errors} · 写盘失败 {self._light_write_errors}"
             + (f" · 字段缺失 {miss}" if miss else "")
             + f" · 完整通道忽略 {self.gift_full_count}"
             f" · 聊天 {self.chat_count} · 榜单快照 {self.rank_snapshots})"
             + ("  ← 本场服务端未下发轻礼物,逐条不可见,只有榜单金额可参考"
                if ch == "full_only" else ""))

    @property
    def dead(self):
        return self._dead

    # ── 诊断读数(礼物静默告警用;只读,不给外部改内部计数的机会) ──
    @property
    def retries(self):
        return self._retries

    @property
    def gift_errors(self):
        """gift 类消息解析失败数 —— 非 0 说明礼物消息到了但解析不了(proto 失配)。"""
        return self._gift_errors

    @property
    def light_gift_errors(self):
        """轻礼物消息解析失败数 —— 匿名侧唯一礼物来源,非 0 必须告警。"""
        return self._light_gift_errors

    @property
    def light_write_errors(self):
        """轻礼物解析成功但写盘失败数(磁盘满/句柄失效)。

        与解析失败分开报:一个是"协议变了",一个是"盘写不进去",处置完全不同。
        """
        return self._light_write_errors

    @property
    def light_field_miss(self):
        """轻通道 4 字段(ts/gift/diamond/count)缺失计数快照。"""
        return dict(self._light_field_miss)

    @property
    def silent_errors(self):
        return self._silent_errors

    @property
    def frame_errors(self):
        return self._frame_errors

    def _ledger_snapshot(self):
        """当前 jsonl 的轻通道账本快照(收到/落盘/失败/字段缺项)。"""
        return {"recv": self.gift_light_recv,
                "kept": self.gift_light_count,
                "parse_errors": self._light_gift_errors,
                "write_errors": self._light_write_errors,
                "field_missing": dict(self._light_field_miss)}

    def ledger_for(self, jsonl_path):
        """取某份 jsonl 的对账账本:当前文件返回实时值,已切走返回存档。

        结算方(monitor._history_finish)必须按**历史条目自己的 jsonl**取账,
        不能直接读会话当前计数 —— 场次切换后两者已经不是同一份文件。

        返回 None 表示既不是当前文件、也没有存档(补算老条目、会话已重建),
        此时调用方按「拿不到对账数据」处理(只验字段,不谎报已对账)。
        """
        if not jsonl_path:
            return None
        if jsonl_path == self.jsonl_path:
            return self._ledger_snapshot()
        led = self._gift_ledgers.get(jsonl_path)
        return dict(led) if led else None

    def retarget(self, jsonl_path):
        """录制重启产生新文件前缀时,切换 jsonl 输出目标(同一直播会话内)。

        轻通道对账计数**归零**:recv/落盘/失败都是"针对某一份 jsonl"的量,
        跨文件累加会让收尾时 `lost = recv - 落盘` 凭空变大,把正常的录制重启
        误报成"丢条"。切之前的账先存档(按旧路径),再归零 —— 结算方用
        `ledger_for(旧路径)` 仍能拿到那一份的账。切之前的账另打进日志留痕。

        场次计数(gift_count / last_gift_ts / msg_count / chat_count)一并归零
        (2026-10-29 修复):
          · `gift_count` 原先跨场累加,于是「本场 0 条礼物」的诊断快照里仍显示
            上一场累计的 101 条,与同一行的 `light.recv/kept = 0` 自相矛盾,
            用户看到的就是「礼物统计被清零了」;
          · `last_gift_ts` 不归零更糟:新场次的静默时长会从上一场最后一次送礼
            算起,一开播就可能立刻误报礼物静默。
        """
        if jsonl_path == self.jsonl_path:
            return
        prev = (f"轻收 {self.gift_light_recv} · 落盘 {self.gift_light_count} · "
                f"解析失败 {self._light_gift_errors} · 写盘失败 {self._light_write_errors}")
        self._gift_ledgers[self.jsonl_path] = self._ledger_snapshot()
        new_rank = giftrank_for(jsonl_path)
        with self._fh_lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            try:
                if self._rfh:
                    self._rfh.close()
            except Exception:
                pass
            os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
            self._fh = open(jsonl_path, "a", encoding="utf-8", buffering=1)
            try:
                os.makedirs(os.path.dirname(new_rank) or ".", exist_ok=True)
                self._rfh = open(new_rank, "a", encoding="utf-8", buffering=1)
            except OSError:
                self._rfh = None
        self.gift_light_recv = 0
        self.gift_light_count = 0
        self._light_gift_errors = 0
        self._light_write_errors = 0
        self._light_field_miss.clear()
        self.gift_count = 0
        self.last_gift_ts = 0.0
        self.msg_count = 0
        self.chat_count = 0
        self.jsonl_path = jsonl_path
        self.rank_path = new_rank
        _log(f"[弹幕] {self.name} 切换输出(轻通道对账归零,上一份: {prev})")

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
                # 这是「礼物与字幕整段不再产生」的直接原因,原先只有一行日志。
                # 录制照旧,所以健康面板的 danmaku 心跳(巡检协程)也看不出来。
                try:
                    incidents.record(
                        "danmaku.crash", f"{self.name} 放弃弹幕抓取",
                        detail=f"连续重连 {MAX_RETRIES} 次失败;此后本场礼物明细与字幕不再产生"
                               f"(视频录制不受影响)",
                        name=self.name, level=incidents.LEVEL_ERROR)
                except Exception:               # noqa: BLE001
                    pass
                break
            backoff = RETRY_BACKOFF[min(self._retries, len(RETRY_BACKOFF) - 1)]
            self._retries += 1
            _log(f"[弹幕] {self.name} 连接断开,{backoff}s 后重试({self._retries}/{MAX_RETRIES})")
            # 第一次断开算常态(直播 CDN 会主动踢线);从第二次起才告警 ——
            # 反复断开会造成礼物/字幕的零散缺口,这种事没人会去翻日志。
            if self._retries >= 2:
                try:
                    incidents.record(
                        "danmaku.crash", f"{self.name} 弹幕连接反复断开",
                        detail=f"已重试 {self._retries}/{MAX_RETRIES} 次;"
                               f"断开窗口内的礼物与弹幕不会补回",
                        name=self.name, level=incidents.LEVEL_WARN)
                except Exception:               # noqa: BLE001
                    pass
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
                self.chat_count += 1
            elif method == "WebcastGiftMessage":
                # 完整通道:匿名侧恒 0 条(349 场实录),登录态抽检连接才见得到。
                # 按「轻通道唯一真源」口径**不落盘、不计数入礼物统计** —— 它与
                # 轻通道是事件级互斥路由,同收会重复计同一笔礼物。仅保留计数与
                # 限频日志,用于回答"这个房间是否只走了完整通道"。
                self.gift_full_count += 1
                now_t = time.time()
                if now_t - self._full_ignored_log >= 300:
                    self._full_ignored_log = now_t
                    _log(f"[弹幕] {self.name} 收到完整通道礼物事件 "
                         f"(累计 {self.gift_full_count} 条),按轻通道口径忽略不落盘")
            elif method == "WebcastLightGiftMessage":
                # 礼物唯一真源。recv 在解析之前就加:解析失败也是"轻通道给过这条",
                # 差额要靠 recv - 落盘 - 失败 才能对上账。
                self.gift_light_recv += 1
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
                self._note_light_fields(ts=ts, gname=gname, diamond=diamond, count=count)
                ev = {"ts": ts, "type": "gift", "channel": "light",
                      "user": cu.nick_name if cu else "",
                      "uid": str(cu.id) if cu else "",
                      "gift": gname, "count": count, "diamond": diamond}
                if self._write(ev):
                    self.gift_light_count += 1
                    self._note_gift()
                else:
                    # 解析成功但没写进 jsonl = 真实丢数据,必须与解析失败分开计
                    self._light_write_errors += 1
            elif method == "WebcastMemberMessage" and self.capture_member:
                m = MemberMessage().parse(msg.payload)
                ts = (m.common.create_time * 1000) if m.common.create_time > 0 else batch_ts
                self._write({"ts": ts, "type": "member",
                             "user": m.user.nick_name, "uid": str(m.user.id),
                             "content": "进入直播间"})
            elif method == "WebcastRoomRankMessage":
                self._note_rank(msg, batch_ts)
        except Exception as e:
            # 单条消息解析失败:记录而不是静默丢弃(礼物丢失排查关键)
            if method == "WebcastGiftMessage":
                self._gift_errors += 1
                _log(f"[弹幕] {self.name} ⚠️ GiftMessage 解析失败: {e!r}")
            elif method == "WebcastLightGiftMessage":
                # 轻礼物是匿名侧唯一的礼物事件来源,失败必须独立计数并升级告警
                # (2026-10-29 前它被算进 _silent_errors,而巡检只看 gift_errors
                #  → proto 一变礼物静默归零,看板还全绿)
                self._light_gift_errors += 1
                _log(f"[弹幕] {self.name} ⚠️ LightGiftMessage 解析失败: {e!r}")
            else:
                self._silent_errors += 1
                _log(f"[弹幕] {self.name} {method} 解析失败: {e!r}")

    def _note_rank(self, msg, batch_ts):
        """贡献榜快照落盘(限频 + 榜面无变化不写)。

        盲场(full_only)下一次礼物事件都收不到,但榜单分仍会下发且
        单位=累计送礼抖币(实测 1:1)。落成 type:"rank" 行后,下游可算
        "某人何时开始上榜/这一分钟送了多少",是盲场唯一的金额线索。
        限频默认 60s,避免一条榜单消息一行把 jsonl 撑大(实测 3-5 条/秒)。
        """
        now = time.time()
        if self._rank_last_emit and now - self._rank_last_emit < _RANK_EMIT_SEC:
            return
        m = RoomRankMessage().parse(msg.payload)
        ranks = []
        for r in m.ranks_list[:20]:      # 榜尾对"谁送了大额"没有信息量,截断
            ranks.append({"rank": int(r.rank),
                          "uid": str(r.user.id) if r.user else "",
                          "name": r.user.nick_name if r.user else "",
                          "score": int(r.score),
                          "gap": int(r.gap)})
        if not ranks:
            return                        # 空榜(只有 users_list)不落,免得谎报"没人送"
        sig = tuple((x["rank"], x["uid"], x["score"]) for x in ranks)
        if sig == self._rank_last_sig:
            return                        # 榜面没变,不重复写
        self._write_rank({"ts": _common_ts_ms(m.common.create_time, batch_ts),
                          "ts_local": int(now * 1000), "type": "rank",
                          "ranks": ranks, "top_score": ranks[0]["score"]})
        self._rank_last_sig = sig
        self._rank_last_emit = now
        self.rank_snapshots += 1

    def _write_rank(self, ev):
        """榜单快照单独落 .giftrank.jsonl —— 不进 danmaku.jsonl(契约 type 集合)。"""
        line = json.dumps(ev, ensure_ascii=False)
        with self._fh_lock:
            try:
                if self._rfh:
                    self._rfh.write(line + "\n")
            except Exception:
                pass

    def _write(self, ev):
        """落一行 jsonl,返回是否**真的写进去了**。

        返回值是轻通道对账的关键:解析成功但写盘失败(磁盘满/句柄失效/目录被删)
        会表现为「收到 N 条、落盘 M < N」而又没有解析失败 —— 只有这个布尔值
        能把这种缺口归因到写盘而不是协议。历史实现吞掉异常直接 pass,等于把
        丢数据记成"没收到"。
        """
        ev["ts_local"] = int(time.time() * 1000)
        self.msg_count += 1
        line = json.dumps(ev, ensure_ascii=False)
        ok = False
        with self._fh_lock:
            try:
                if self._fh:
                    self._fh.write(line + "\n")
                    ok = True
            except Exception:
                ok = False
            # 实时预览缓冲:seq 自增 + 入队(与落盘同一临界区,快照不会读到半写)
            self._feed_seq += 1
            feed_ev = dict(ev)
            feed_ev["seq"] = self._feed_seq
            if ev.get("type") == "gift":
                # 展示语义挂在这里而不是写进 jsonl：原始事件永不增加展示字段
                try:
                    feed_ev["display"] = gift_display_mod.build_live_display(
                        ev, self.cfg, self._gift_dict_or_none(), self._feed_seq)
                except Exception:
                    pass
            self._feed.append(feed_ev)
        return ok

    def feed(self, after_seq=0):
        """增量读取实时预览缓冲。返回 (当前最大 seq, seq>after 的消息列表)。

        断点续传语义:客户端记住上次的 seq,下次传 after=seq 即可,不丢不重。
        """
        with self._fh_lock:
            seq = self._feed_seq
            items = [e for e in self._feed if e.get("seq", 0) > after_seq]
        return seq, items

    def _gift_dict_or_none(self):
        """懒加载官方礼物字典（只读本地快照，不在弹幕线程里联网）。"""
        if not self._gift_dict_tried:
            self._gift_dict_tried = True
            try:
                import gift_dict as gift_dict_mod
                self._gift_dict = gift_dict_mod.load()
            except Exception:
                self._gift_dict = None
        return self._gift_dict

    def _note_gift(self):
        """记一次**轻通道**礼物成功落盘 —— 礼物静默告警的唯一时间基准。

        只有真正写进 jsonl 的轻礼物才调它。完整通道事件不再调用:它已不参与
        礼物口径(见模块 docstring),若还拿它重置静默计时,会出现「jsonl 里
        一条礼物都没有,但静默告警永远不触发」的自欺状态。
        """
        self.last_gift_ts = time.time()
        self.gift_count += 1

    def _note_light_fields(self, ts, gname, diamond, count):
        """对账轻通道「协议承诺的 4 个字段」有没有给齐。

        轻通道天生不带送礼人与礼物名(实测 100% 缺失,是协议设计不是抓漏),
        所以完整性只看这 4 项:时间 / 礼物标识 / 单价 / 份数。判定规则与离线
        统计共用 danmaku.light_row_missing 一份,monitor 只读计数不再自己判。
        """
        for k in light_row_missing({"ts": ts, "gift": gname,
                                    "diamond": diamond, "count": count}):
            self._light_field_miss[k] += 1


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


def giftrank_for(jsonl_path):
    """由 danmaku.jsonl 推导贡献榜快照文件(<基名>.giftrank.jsonl,同 .meta 目录)。

    为什么不写进 danmaku.jsonl:那份文件的 type 集合(chat/gift/member)是对外
    契约的一部分,字幕生成与下游 asrclip 都按 type 过滤。榜单快照放到同目录
    独立文件,与既有 .giftwatch.jsonl(诊断类)保持同一风格,不动主事件流。
    """
    suf = ".danmaku.jsonl"
    if jsonl_path.endswith(suf):
        return jsonl_path[: -len(suf)] + ".giftrank.jsonl"
    stem, _ext = os.path.splitext(jsonl_path)
    return stem + ".giftrank.jsonl"


# 礼物覆盖判定的「连续间隔」:两个礼物事件间隔 ≤ 此值视为通道持续在供。
# 取 600s 与 _gift_diagnose 的 WS_SILENT 判据(10 分钟无任何消息≈连接已断)一致,
# 两处语义互为镜像:巡检侧 600s 无消息判「断」,统计侧 600s 无礼物事件记「中断」。
GIFT_COVERAGE_GAP_SEC = 600

# 判「房间是否活跃」的每分鐘聊天条数门槛。低于它且零礼物事件时,
# 无法区分「真的没人送礼」与「通道裁剪」,只能记 quiet。
GIFT_CHANNEL_CHAT_PER_MIN = 1.0


def _common_ts_ms(create_time, fallback):
    """把 Common.create_time 规整成**毫秒**时间戳。

    ⚠️ 各消息的 create_time 单位并不统一(2026-10-29 实锤):
      · Chat/Gift/LightGift 给的是**秒**(项目一直 ×1000);
      · RoomRank 的 common.create_time 直接就是**毫秒**
        (原始字节实测 1793208275359,若再 ×1000 会变成 1.79e15 的假时间)。
    按量级判断,别按消息名硬编码 —— 服务端随时可能改单位。
    """
    try:
        ct = float(create_time or 0)
    except (TypeError, ValueError):
        return fallback
    if ct <= 0:
        return fallback
    if ct > 1e14:      # 微秒
        return int(ct / 1000)
    if ct > 1e11:      # 毫秒(1e11 ms ≈ 1973 年,正常服务器时间必然大于它)
        return int(ct)
    if ct > 1e9:       # 秒
        return int(ct * 1000)
    return fallback


# 轻通道**协议承诺**的字段:时间 / 礼物标识 / 单价 / 份数。
# 送礼人昵称、送礼人 uid、礼物名都不在其中 —— 实测 100% 不下发(登录态也一样),
# 拿"缺送礼人"去算完整性,等于给每一条礼物都记一次不完整,指标永远红。
LIGHT_FIELDS = ("ts", "gift", "diamond", "count")


def light_row_missing(row):
    """一条礼物行缺了哪些轻通道字段(空列表 = 齐全)。

    运行期(danmaku._note_light_fields)与离线统计(gift_channel_coverage)
    共用这一份判定 —— 否则会出现"巡检说缺、历史页说齐"的口径分裂。
    """
    row = row or {}
    miss = []
    ts = row.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        miss.append("ts")
    gift = row.get("gift")
    if not gift or gift == "?":
        miss.append("gift")
    dia = row.get("diamond")
    if not isinstance(dia, (int, float)) or dia < 0:
        miss.append("diamond")
    cnt = row.get("count")
    if not isinstance(cnt, (int, float)) or cnt < 1:
        miss.append("count")
    return miss


def classify_gift_channel(light_events, chat_events=0, duration_sec=0.0,
                          min_chat_per_min=None):
    """判定本场礼物通道类别 —— 回答"0 条礼物到底是没人送,还是压根没下发"。
    2026-10-29 实测依据(见 录制侧三要素与登录一致性分析):
      · 服务端把礼物事件分两条通道下发,匿名身份只收得到 light 那条;
      · 走 light 时匿名侧与登录侧逐条一致(7/7、42/42,价值 100%);
      · 只走完整通道时匿名侧恒为 0,而房间照样热闹;
      · 同一场直播内会中途切换(实测 01:42 从 light 切到 full)。

    传入的 light_events 是**收到的轻礼物消息数**(含解析失败),不是落盘条数 ——
    解析失败说明"通道给了、我们没接住",那属于完整性故障,不能算成 channel 未开。

    返回值:
      "light"      本场收到过轻礼物 → 逐条礼物为「轻通道全量」(完整通道仍不可见)
      "full_only"  零轻礼物、但房间活跃 → 轻通道本场未下发(平台侧开关),禁止当成"没人送"
      "quiet"      零轻礼物、房间也冷清 → 无法归因
      "unknown"    缺少时长/聊天信息  → 无法判定

    纯函数、零 IO,便于单测与前端复用;不要在别处再写一套阈值。
    """
    light = int(light_events or 0)
    if light > 0:
        return "light"
    thr = (GIFT_CHANNEL_CHAT_PER_MIN if min_chat_per_min is None
           else float(min_chat_per_min))
    dur = float(duration_sec or 0)
    if dur <= 0:
        return "unknown"
    rate = float(chat_events or 0) / (dur / 60.0)
    return "full_only" if rate >= thr else "quiet"


def gift_channel_coverage(jsonl_path, duration_sec, started_at=None, gap_sec=None,
                          recv=None, parse_errors=0, write_errors=0,
                          extra_paths=None):
    """一场录制的礼物完整性(轻通道口径,2026-10-29 定案)。

    输出两件**互不替代**的事实:

    ① `ratio` / `covered_sec` —— 「存在礼物信息」的时间占比(2026-10-16 起的
       老口径,保留以兼容存量条目与消费方):每个礼物事件把通道视为「有信息」,
       并从该事件起向前延伸 gap_sec(默认 600s);相邻事件间隔 ≤ gap_sec 时覆盖
       区间连续延伸。它回答"这场有没有礼物",**不回答**"拿到的礼物录全了没有"。

    ② `light` —— 轻通道完整性(健康度的**唯一**判据):把「轻通道下发了多少条」
       与「jsonl 里实际落下多少条、字段齐不齐」逐项对账。轻通道只承诺 4 个字段
       (时间/礼物标识/单价/份数,见 LIGHT_FIELDS),健康度就只查这 4 个;
       送礼人与礼物名不在其列(协议不下发,换登录态也没有)。

    recv / parse_errors / write_errors 由 DanmakuRecorder 的实时计数提供:
      recv          收到的轻礼物消息数(含解析失败)
      parse_errors  解析失败数
      write_errors  解析成功但写盘失败数
    事后补算(历史条目、归档后目录)拿不到这三个数 → recv 传 None,
    此时只能验字段完整性,status 落 "field_only"(不谎报"已对账")。

    ③ `extra_paths` —— 相邻场次的 jsonl(2026-10-29 补的兜底)。
       **归属应按事件时间,而不是按文件**:录制重启后弹幕会话要等巡检/换场
       才切输出,那几十秒里的新礼物会落进**上一场**的文件。此时把同目录其它
       jsonl 里 ts 落在本场 [开播, 开播+时长] 窗口内的礼物行一并计入,并在
       `light.adopted` 里如实报出借入条数(0 表示没用上兜底)。
       没有 started_at 就没有窗口,一律不借 —— 宁可少算,不能把整场算重。

    返回 {"scope":"light", "ratio", "covered_sec", "events", "chat_events",
          "duration_sec", "gap_sec", "gift_channel", "light": {...}};
    jsonl 不存在/读不了或 duration_sec<=0 → None(调用方据此跳过)。
    """
    if not duration_sec or duration_sec <= 0:
        return None
    if not jsonl_path or not os.path.exists(jsonl_path):
        return None
    gap = float(gap_sec) if gap_sec else float(GIFT_COVERAGE_GAP_SEC)
    dur = float(duration_sec)
    # 场次窗口:事件缓冲窗一律裁进 [开播, 开播+时长],尾部不得越界(贡献时长
    # 只算窗口内的部分);无 started_at 时退化为「总覆盖 ≤ 时长」的粗裁剪。
    win_start_ms = win_end_ms = None
    if started_at:
        win_start_ms = float(started_at) * 1000.0
        win_end_ms = win_start_ms + dur * 1000.0

    def _scan(path, windowed):
        """扫一份 jsonl → (ts 列表, 聊天数, 轻通道行数, 完整通道行数, 缺项计数)。

        windowed=True 时(只用于 extra_paths)丢弃 ts 不在本场窗口内的礼物行:
        借入别的场次的文件,必须按时间收窄,否则整场会算重。
        """
        ts_out, chat, light, full = [], 0, 0, 0
        miss = collections.Counter()
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if '"gift"' not in line:
                    # 顺路数聊天条数(判"房间活跃度"),不额外解析整行
                    if '"chat"' in line:
                        chat += 1
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = row.get("type")
                if typ == "chat":      # 聊天内容里恰好带 "gift" 字样
                    chat += 1
                    continue
                if typ != "gift":      # type:"rank" 等新行不参与礼物统计
                    continue
                ts = row.get("ts")
                ts_ok = isinstance(ts, (int, float)) and ts > 0
                if windowed:
                    if not (ts_ok and win_start_ms <= float(ts) <= win_end_ms):
                        continue
                if row.get("channel") == "full":
                    full += 1          # 口径外(light-only 之后正常恒 0)
                else:
                    light += 1         # channel 缺失 = 2026-10-29 之前的历史行,全是轻通道
                    for k in light_row_missing(row):
                        miss[k] += 1
                if ts_ok:
                    ts_out.append(float(ts))
        return ts_out, chat, light, full, miss

    try:
        ts_list, chat_n, light_n, full_n, miss_counter = _scan(jsonl_path, False)
    except OSError:
        return None
    adopted = 0
    if extra_paths and win_start_ms is not None:
        seen = {jsonl_path}
        for ep in extra_paths:
            if not ep or ep in seen or not os.path.exists(ep):
                continue
            seen.add(ep)
            try:
                ets, _ec, elight, _ef, emiss = _scan(ep, True)
            except OSError:
                continue
            if not elight:
                continue
            ts_list.extend(ets)
            light_n += elight
            adopted += elight
            miss_counter.update(emiss)
    ts_list.sort()
    covered_ms = 0
    last_end = None  # 当前覆盖区间的右端(epoch ms)
    for ts in ts_list:
        s = ts
        e = ts + gap * 1000.0
        if win_start_ms is not None:
            s = max(s, win_start_ms)
            e = min(e, win_end_ms)
            if e <= s:
                continue    # 整段落在窗口外(理论上不会发生,防御)
        if last_end is None or s > last_end:
            covered_ms += e - s
            last_end = e
        elif e > last_end:
            covered_ms += e - last_end
            last_end = e
    covered = covered_ms / 1000.0
    covered = min(covered, dur)  # 尾部缓冲窗不得超出场次时长
    ratio = round(min(1.0, covered / dur), 4)
    # ── 轻通道完整性对账(健康度唯一判据)────────────────────────
    recv_v = None if recv is None else max(0, int(recv))
    if recv_v is not None and adopted:
        # 借入的行不属于本会话的 recv 账,却实实在在落进本场的视频时间轴。
        # 把 recv 抬到落盘数,避免出现「收到 0 条 / 落盘 101 条」的矛盾展示;
        # 借入条数在 adopted 里单列,不隐藏。
        recv_v = max(recv_v, light_n)
    parse_v = max(0, int(parse_errors or 0))
    write_v = max(0, int(write_errors or 0))
    # lost = 收到了、既没解析失败也没写盘失败、却不在 jsonl 里 —— 对不上的账
    lost = (None if recv_v is None
            else max(0, recv_v - light_n - parse_v - write_v))
    miss_total = int(sum(miss_counter.values()))
    # 分母用 max(落盘, 收到):收到多于落盘时自动压低完整率(丢条要体现在数上)
    denom = len(LIGHT_FIELDS) * max(light_n, recv_v or 0)
    num = max(0, len(LIGHT_FIELDS) * light_n - miss_total)
    light_ratio = round(num / denom, 4) if denom else 0.0
    field_ratio = round(num / (len(LIGHT_FIELDS) * light_n), 4) if light_n else 0.0
    reasons = []
    if lost:
        reasons.append("event_lost")
    if parse_v:
        reasons.append("parse_error")
    if write_v:
        reasons.append("write_error")
    if miss_total:
        reasons.append("field_missing")
    if light_n == 0 and not recv_v:
        status = "channel_off"      # 本场服务端没下发轻礼物:能力边界,不是缺陷
        complete = None
    elif reasons:
        status = "partial"
        complete = False
    elif recv_v is None:
        status = "field_only"       # 事后补算:字段齐,但没得对账
        complete = True
    else:
        status = "complete"
        complete = True
    return {"scope": "light",
            "ratio": ratio, "covered_sec": round(covered, 1), "events": len(ts_list),
            "chat_events": chat_n, "duration_sec": round(dur, 1), "gap_sec": gap,
            "gift_channel": classify_gift_channel(light_n or parse_v, chat_n, dur),
            "light": {"recv": recv_v, "events": light_n, "full_rows": full_n,
                      "lost": lost, "adopted": adopted,
                      "errors": {"parse": parse_v, "write": write_v},
                      "field_missing": {k: v for k, v in miss_counter.items() if v},
                      "field_ratio": field_ratio, "ratio": light_ratio,
                      "complete": complete, "status": status,
                      "reasons": reasons}}


def gift_session_stats(jsonl_path):
    """统计一场录制的礼物明细(每场分开统计的量化层,2026-10-16 礼物界面改版)。

    与 subtitle.summarize 同口径:diamonds = Σ 单价(diamond) × 份数(count),
    但这里单遍流式读、不整载事件列表,收尾时与 gift_channel_coverage 各读一遍
    jsonl 可接受(每场只发生一次)。返回:
      {"msgs": 礼物消息条数, "count": 总份数, "diamonds": 总抖币,
       "kinds": 礼物种类数, "top": [{"gift","count"} ×5]}
    jsonl 缺失/读不了 → None(调用方据此跳过,历史条目不写字段)。
    """
    if not jsonl_path or not os.path.exists(jsonl_path):
        return None
    msgs = count = diamonds = 0
    top = {}
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '"gift"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "gift":
                    continue
                msgs += 1
                try:
                    n = int(row.get("count") or 1)
                except (TypeError, ValueError):
                    n = 1
                if n < 1:
                    n = 1
                count += n
                diamonds += (row.get("diamond") or 0) * n
                k = row.get("gift") or "?"
                top[k] = top.get(k, 0) + n
    except OSError:
        return None
    if not msgs:
        return {"msgs": 0, "count": 0, "diamonds": 0, "kinds": 0, "top": []}
    kind_items = sorted(top.items(), key=lambda kv: -kv[1])[:5]
    return {"msgs": msgs, "count": count, "diamonds": diamonds,
            "kinds": len(top), "top": [{"gift": k, "count": v} for k, v in kind_items]}


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
                          capture_member=bool(dm_cfg.get("capture_member", False)),
                          cfg=cfg)
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


def live_feed(name, after_seq=0):
    """实时预览:读取某主播弹幕缓冲的增量。

    返回 {"active": bool, "seq": int, "items": [...]}:
    - active=False 表示当前没有该主播的弹幕会话(未开播/未开弹幕/已下播);
    - items 为 seq > after_seq 的消息(断点续传,不丢不重)。
    """
    rec = get(name)
    if rec is None:
        return {"active": False, "seq": 0, "items": []}
    try:
        seq, items = rec.feed(int(after_seq or 0))
    except Exception:
        return {"active": True, "seq": 0, "items": []}
    return {"active": True, "seq": seq, "items": items}


def all_sessions():
    return {n: s["rec"] for n, s in _sessions.items()}
