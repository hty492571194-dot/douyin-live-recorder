#!/usr/bin/env python3
"""抓取 WebcastLightGiftMessage 原始 payload 并按 protobuf wire format 盲解码,
推断新礼物消息的字段结构(社区无公开 schema,直接逆向真实样本)。

用法: ./.venv/bin/python collect_lightgift.py <room_id> [秒数=90]
"""
import gzip
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websocket  # websocket-client
from danmaku import _fetch_ttwid, _sign, _wss_url, UA
from danmaku_proto import PushFrame, Response

WANT = ("WebcastLightGiftMessage", "WebcastGiftMessage")
samples = []
lock = threading.Lock()
stat = {"total": 0, "chat": 0, "last": time.time()}


# ── protobuf wire-format 盲解码器 ─────────────────────────
def _read_varint(buf, i):
    result = 0
    shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint overrun")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result & ((1 << 64) - 1), i
        shift += 7


def try_decode_fields(data):
    """尝试把 bytes 按 protobuf message 解析;非法返回 None。"""
    fields = []
    i = 0
    try:
        while i < len(data):
            tag, i = _read_varint(data, i)
            fnum = tag >> 3
            wt = tag & 7
            if fnum == 0 or fnum > 200000:
                return None
            if wt == 0:
                v, i = _read_varint(data, i)
                fields.append((fnum, "varint", v))
            elif wt == 1:
                if i + 8 > len(data):
                    return None
                fields.append((fnum, "fixed64", data[i:i + 8].hex()))
                i += 8
            elif wt == 2:
                ln, i = _read_varint(data, i)
                if i + ln > len(data):
                    return None
                fields.append((fnum, "bytes", data[i:i + ln]))
                i += ln
            elif wt == 5:
                if i + 4 > len(data):
                    return None
                fields.append((fnum, "fixed32", data[i:i + 4].hex()))
                i += 4
            else:
                return None
    except Exception:
        return None
    return fields


def _utf8_or_hex(b):
    try:
        s = b.decode("utf-8")
        if s and all(ch.isprintable() or ch in "\n\r\t" for ch in s):
            return repr(s)
    except UnicodeDecodeError:
        pass
    return f"hex[{len(b)}B]{b[:24].hex()}{'...' if len(b) > 24 else ''}"


def render(data, depth=0, max_depth=5):
    fields = try_decode_fields(data)
    if fields is None:
        return _utf8_or_hex(data)
    pad = "    " * depth
    lines = []
    for fnum, wt, val in fields[:50]:
        if wt == "bytes":
            sub = try_decode_fields(val)
            is_nested = sub is not None and len(sub) > 0
            # 启发式:能整段解出 >=1 合法字段 且 首字节像 tag(varint<=96) → 嵌套
            if depth < max_depth and is_nested:
                head_ok = val[0] < 96 if val else False
                looks_text = False
                try:
                    t = val.decode("utf-8")
                    if t and all(ch.isprintable() or ch in "\n\r\t" for ch in t):
                        looks_text = True
                except UnicodeDecodeError:
                    pass
                if head_ok or looks_text:
                    if looks_text and not head_ok:
                        lines.append(f"{pad}f{fnum}(str,{len(val)}B): {val.decode('utf-8')!r}")
                    else:
                        lines.append(f"{pad}f{fnum}(msg,{len(val)}B):")
                        lines.append(render(val, depth + 1, max_depth))
                    continue
            lines.append(f"{pad}f{fnum}(str,{len(val)}B): {_utf8_or_hex(val)}")
        else:
            lines.append(f"{pad}f{fnum}({wt}): {val}")
    if len(fields) > 50:
        lines.append(f"{pad}... 共{len(fields)}字段截断")
    return "\n".join(lines)


# ── WS 采集 ────────────────────────────────────────────────
def on_message(ws, message):
    try:
        frame = PushFrame().parse(message)
        payload = frame.payload
        if frame.payload_encoding == "gzip" or (payload[:1] == b"\x1f"):
            payload = gzip.decompress(payload)
        resp = Response().parse(payload)
        if getattr(resp, "need_ack", False):
            ws.send(PushFrame(log_id=frame.log_id, payload_type="ack",
                              payload=resp.internal_ext.encode("utf-8")).SerializeToString(),
                    websocket.ABNF.OPCODE_BINARY)
    except Exception:
        return
    stat["total"] += len(resp.messages_list)
    stat["last"] = time.time()
    for msg in resp.messages_list:
        if msg.method == "WebcastChatMessage":
            stat["chat"] += 1
        if msg.method in WANT:
            with lock:
                samples.append((msg.method, msg.payload))
                n = len(samples)
                print(f"\n[{time.strftime('%H:%M:%S')}] ★ 抓到 {msg.method} 样本#{n} "
                      f"({len(msg.payload)}B)", flush=True)
                if n <= 3:
                    print("--- 盲解码 ---")
                    print(render(msg.payload))
                    print("--- 解码结束 ---", flush=True)


def on_open(ws):
    print(f"[{time.strftime('%H:%M:%S')}] 已连接,等待礼物样本...", flush=True)

    def heartbeat():
        while True:
            try:
                ws.send(PushFrame(payload_type="hb").SerializeToString(),
                        websocket.ABNF.OPCODE_BINARY)
            except Exception:
                break
            time.sleep(10)
        return
    threading.Thread(target=heartbeat, daemon=True).start()

    def reporter():
        while True:
            time.sleep(20)
            print(f"[{time.strftime('%H:%M:%S')}] 状态: 总消息={stat['total']} chat={stat['chat']} "
                  f"礼物样本={len(samples)}", flush=True)
    threading.Thread(target=reporter, daemon=True).start()


def main():
    room_id = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    from danmaku import _sign as _s
    uuid = str(random.randint(10 ** 18, 10 ** 19 - 1)) if False else __import__("random").randint(10 ** 18, 10 ** 19 - 1)
    import random
    random.seed()
    uuid = str(random.randint(10 ** 18, 10 ** 19 - 1))
    wss = _wss_url(room_id, uuid) + "&signature=" + _sign(_wss_url(room_id, uuid))
    ttwid = _fetch_ttwid()
    print(f"[{time.strftime('%H:%M:%S')}] room={room_id} 收集{duration}s", flush=True)
    ws = websocket.WebSocketApp(wss, header={"cookie": f"ttwid={ttwid}", "user-agent": UA},
                                on_open=on_open, on_message=on_message)

    def stopper():
        time.sleep(duration)
        ws.close()
    threading.Thread(target=stopper, daemon=True).start()
    ws.run_forever()

    print(f"\n=== 共抓到 {len(samples)} 条礼物样本 ===")
    if samples:
        os.makedirs("/tmp/lightgift_samples", exist_ok=True)
        for i, (m, p) in enumerate(samples):
            path = f"/tmp/lightgift_samples/{m}_{i}.bin"
            open(path, "wb").write(p)
        print(f"原始 payload 已存 /tmp/lightgift_samples/")
        print("\n=== 第4条起样本的盲解码 ===")
        for i, (m, p) in enumerate(samples[3:9], start=4):
            print(f"\n--- 样本#{i} {m} ({len(p)}B) ---")
            print(render(p))


if __name__ == "__main__":
    main()
