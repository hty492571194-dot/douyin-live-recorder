#!/usr/bin/env python3
"""礼物消息诊断脚本：统计 WS 收到的所有 method，并对 gift 类消息尝试解析。

用法:
    ./.venv/bin/python diag_gift.py <room_id> [秒数=90]

room_id 取直播间内部 ID（19 位，config 里主播直播时 monitor 日志会打，或
从 https://live.douyin.com/<web_rid> 页面 reflow 里提取）。

输出:
    1) 收到的所有 method 类型及计数 —— 看礼物消息到底以什么 method 到达
    2) gift 类消息解析结果（成功字段 / 异常堆栈）—— 定位 proto 是否失配
"""
import gzip
import os
import random
import sys
import threading
import time
import traceback
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websocket  # websocket-client
from danmaku import _fetch_ttwid, _sign, _wss_url, UA
from danmaku_proto import GiftMessage, PushFrame, Response

method_counter = Counter()
gift_log = []   # (method, status, detail)
parse_err_detail = []  # 非 gift 消息解析异常（对照组）


def on_message(ws, message):
    now_ms = time.time() * 1000
    try:
        frame = PushFrame().parse(message)
        payload = frame.payload
        if frame.payload_encoding == "gzip" or (payload[:1] == b"\x1f"):
            payload = gzip.decompress(payload)
        resp = Response().parse(payload)
    except Exception:
        return

    for msg in resp.messages_list:
        method = msg.method
        method_counter[method] += 1
        if "gift" in method.lower():
            try:
                m = GiftMessage().parse(msg.payload)
                gift_log.append((
                    method, "OK",
                    "user=%s uid=%s gift=%s gift_id=%s diamond=%s "
                    "repeat=%s combo=%s total=%s display_for_self=%s"
                    % (
                        m.user.nick_name if m.user else None,
                        m.user.id if m.user else None,
                        m.gift.name if m.gift else None,
                        m.gift_id,
                        m.gift.diamond_count if m.gift else None,
                        m.repeat_count, m.combo_count, m.total_count,
                        m.display_for_self,
                    ),
                ))
            except Exception as e:
                gift_log.append((method, "ERR", f"{type(e).__name__}: {e} payload_len={len(msg.payload)}"))
                traceback.print_exc()
        elif method == "WebcastChatMessage":
            try:
                from danmaku_proto import ChatMessage
                c = ChatMessage().parse(msg.payload)
                if c.content:
                    parse_err_detail.append(f"chat OK: {c.user.nick_name}: {c.content[:20]}")
            except Exception as e:
                parse_err_detail.append(f"chat ERR: {type(e).__name__}: {e}")
    ws.send(PushFrame(log_id=frame.log_id, payload_type="ack",
                      payload=resp.internal_ext.encode("utf-8")).SerializeToString(),
            websocket.ABNF.OPCODE_BINARY) if getattr(resp, "need_ack", False) else None


def on_open(ws):
    print(f"[{time.strftime('%H:%M:%S')}] 已连接，开始收集（心跳 10s）", flush=True)

    def heartbeat():
        while True:
            try:
                ws.send(PushFrame(payload_type="hb").SerializeToString(),
                        websocket.ABNF.OPCODE_BINARY)
            except Exception:
                break
            time.sleep(10)
    threading.Thread(target=heartbeat, daemon=True).start()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    room_id = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    uuid = str(random.randint(10 ** 18, 10 ** 19 - 1))

    ttwid = _fetch_ttwid()
    print(f"[{time.strftime('%H:%M:%S')}] room_id={room_id} ttwid={'有' if ttwid else '无(可能拿不到)'}")
    wss = _wss_url(room_id, uuid)
    wss += "&signature=" + _sign(wss)

    ws = websocket.WebSocketApp(wss, header={"cookie": f"ttwid={ttwid}", "user-agent": UA},
                                on_open=on_open, on_message=on_message)

    def stopper():
        time.sleep(duration)
        ws.close()
    threading.Thread(target=stopper, daemon=True).start()

    ws.run_forever()

    print(f"\n{'='*60}\n[统计] 收到消息类型（共 {sum(method_counter.values())} 条）")
    for m, c in method_counter.most_common(30):
        print(f"  {m:<45} {c}")
    print(f"\n[礼物] gift 类消息解析（共 {len(gift_log)} 条）")
    if not gift_log:
        print("  ⚠️ 期间没有收到任何 gift 类 method —— 说明 WS 层就没推礼物消息")
        print("     (可能是:匿名连接服务端裁剪 / 该时段无人送礼 / 房间非直播)")
    for method, status, detail in gift_log[:15]:
        print(f"  [{status}] {method}: {detail}")
    print(f"\n[对照] chat 抽样（{len(parse_err_detail)} 条，验证解析框架）")
    for line in parse_err_detail[:3]:
        print(f"  {line}")


if __name__ == "__main__":
    main()
