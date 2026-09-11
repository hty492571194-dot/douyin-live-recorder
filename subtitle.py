#!/usr/bin/env python3
"""弹幕字幕生成与时间对齐(分片 FLV ↔ 同名 .ass)。

对齐模型:
- 每个视频分片独立锚定:字幕时间 = (事件墙钟 − 该分片首包墙钟) + 偏移。
  前面片段缺失/断链重连不会传播错位(每片锚自己的起点)。
- 分片首包墙钟来源:ffmpeg 加 -use_wallclock_as_timestamps 1 -copyts 后,
  FLV tag 时间戳为 epoch 毫秒截断到 32 位(回绕),以文件 birthtime 为基准
  解回绕;相对时间戳或解析失败时回退 birthtime 本身。
- 人工微调:align.json 的 global_offset(本场) + per_segment(单分片),
  从 jsonl 原始明细无损重新生成 .ass。

字幕样式(danmaku.style):
- queue(默认):屏幕下方最多 5 行队列,每条发言独立一行;新发言从最下方
  挤入,旧字幕逐行上移,满 5 行最旧的滑出顶部;每条最长停留 queue_seconds 秒。
- scroll(旧):从右向左横向滚动。
"""
import glob
import json
import os
import struct
import time

from danmaku import jsonl_for

GRACE_AFTER_END = 60          # 视频末尾之后仍保留的弹幕秒数(播放器会忽略超长时刻)
LANES = 12                     # 滚动弹幕轨道数
SCROLL_SEC = 10                # 弹幕滚过屏幕耗时
GIFT_SEC = 5                   # 礼物横幅停留秒数
PLAY_RES_X, PLAY_RES_Y = 1920, 1080

# 底部队列样式参数
QUEUE_MAX_LINES = 8            # queue_lines 上限
QUEUE_BOTTOM_MARGIN = 60       # 底部 5 行区域距屏幕底边
QUEUE_TOP_MARGIN = 60          # 顶部礼物横幅区域
SLIDE_MS = 150                 # 行间上移动画时长(ms)


# ── FLV tag 时间戳解析 ────────────────────────────────────
def _tag_ts(header: bytes) -> int:
    """tag 头 11 字节 → 毫秒时间戳(含扩展位)。"""
    lower = (header[4] << 16) | (header[5] << 8) | header[6]
    ext = header[7]
    return (ext << 24) | lower


def flv_first_av_ts(path: str):
    """首个音/视频数据 tag 的原始 32 位时间戳(ms)。解析失败返回 None。

    注意:ffmpeg 在 -use_wallclock_as_timestamps 1 -copyts 下写入的是
    epoch 毫秒截断到 32 位(回绕值),需要用 flv_first_av_ts_abs() 解回绕。
    AVC/AAC sequence header(编码配置包,ts 恒为 0)会被跳过。"""
    try:
        with open(path, "rb") as f:
            head = f.read(13)
            if len(head) < 13 or head[:3] != b"FLV":
                return None
            # 跳过 PreviousTagSize0,顺序读 tag(metadata + seq header 之后必到数据帧)
            for _ in range(12):
                h = f.read(11)
                if len(h) < 11:
                    return None
                size = (h[1] << 16) | (h[2] << 8) | h[3]
                if h[0] in (8, 9):  # audio / video
                    b = f.read(2)
                    if len(b) < 2:
                        return None
                    if h[0] == 9 and (b[0] & 0x0F) == 7 and b[1] == 0 or \
                            h[0] == 8 and (b[0] >> 4) == 10 and b[1] == 0:
                        f.seek(size - 2 + 4, 1)  # AVC/AAC sequence header,跳过
                        continue
                    return _tag_ts(h)
                # 跳过 body + 4 字节 PreviousTagSizeN
                f.seek(size + 4, 1)
            return None
    except OSError:
        return None


def flv_last_ts(path: str):
    """最后一个 tag 的时间戳(ms),用于停滞检测与时长估计。

    文件末尾不一定是完整 tag(ffmpeg 仍在写 / 文件损坏),除了长度检查,
    还做 tag 头自洽校验:type 必须是 8(音频)/9(视频)/18(script),
    且头里的 data_size 必须等于 PreviousTagSize - 11。
    不满足说明定位到了垃圾字节——直接返回 None,交由调用方跳过本次检测。
    """
    try:
        size = os.path.getsize(path)
        if size < 26:
            return None
        with open(path, "rb") as f:
            f.seek(size - 4)
            prev = struct.unpack(">I", f.read(4))[0]
            start = size - 4 - prev
            if start < 13 or prev < 11:
                return None
            f.seek(start)
            h = f.read(11)
            if len(h) < 11:
                return None
            if h[0] not in (8, 9, 18):
                return None
            data_size = int.from_bytes(h[1:4], "big")
            if data_size + 11 != prev:
                return None
            return _tag_ts(h)
    except OSError:
        return None


def _birthtime_ms(path: str):
    try:
        st = os.stat(path)
        bt = getattr(st, "st_birthtime", None)
        if bt:
            return int(bt * 1000)
    except OSError:
        pass
    return None


def is_epoch_ms(ts):
    """copyts 生效时 tag 时间戳是绝对毫秒(>2001-09-09);未生效则是小相对值。"""
    return ts is not None and ts > 1e12


RELATIVE_TS_MAX = 86_400_000  # 24h:原始值小于此视为相对时间戳(copyts 未生效)


def _unwrap_ts(raw, ref_ms):
    """FLV tag 时间戳为 32 位,epoch ms 必然回绕。
    以 ref_ms(通常为文件 birthtime)为基准解到最近的绝对毫秒。"""
    if raw is None or ref_ms is None:
        return None
    delta = ((raw - (ref_ms & 0xFFFFFFFF) + 0x80000000) & 0xFFFFFFFF) - 0x80000000
    return ref_ms + delta


# 解绕后相对「文件创建时间」的合理窗口。超出即判为不可信:
# 下界 -24h 是为了兼容归档文件(NAS 上的 birthtime 是复制时间,晚于录制时间);
# 上界 7 天远超任何单文件录制时长。
TS_SANITY_LO = -24 * 3600_000
TS_SANITY_HI = 7 * 86400_000


def _sanitize_abs(abs_ms, ref_ms):
    """过滤解绕后落在荒谬区间的时间戳。

    即便 tag 头自洽校验通过,损坏的字节流仍可能解出「看似合法」的时间戳;
    _unwrap_ts 的搜索范围是 ref ± 2^31 ms(约 ±24.8 天),垃圾值几乎必然
    被推到该范围边界,表现为「停滞 500+ 小时」这类不可能的告警。
    """
    if abs_ms is None or ref_ms is None:
        return abs_ms
    delta = abs_ms - ref_ms
    if delta < TS_SANITY_LO or delta > TS_SANITY_HI:
        return None
    return abs_ms


def flv_first_av_ts_abs(path: str):
    """首个音/视频 tag 的绝对毫秒(已解 32 位回绕)。
    相对时间戳模式(copyts 未生效)、解析失败或数值不合理返回 None → 调用方回退 birthtime。"""
    raw = flv_first_av_ts(path)
    if raw is None or raw < RELATIVE_TS_MAX:
        return None
    return _sanitize_abs(_unwrap_ts(raw, _birthtime_ms(path)), _birthtime_ms(path))


def flv_last_ts_abs(path: str):
    """最后一个 tag 的绝对毫秒(已解回绕),用于停滞检测与时长估计。
    尾部损坏/数值不合理时返回 None,调用方跳过本次检测(不写入荒谬 gap)。"""
    raw = flv_last_ts(path)
    if raw is None or raw < RELATIVE_TS_MAX:
        return None
    birth = _birthtime_ms(path)
    return _sanitize_abs(_unwrap_ts(raw, birth), birth)


# ── 分片发现与锚定 ────────────────────────────────────────
def _split_output(output_path: str):
    """录制输出路径 → (目录, 文件前缀, 扩展名, 是否分片模式)。"""
    d = os.path.dirname(output_path)
    base = os.path.basename(output_path)
    segmented = "%03d" in base
    stem, ext = os.path.splitext(base)
    if segmented:
        stem = stem[: stem.rfind("-")] if stem.rfind("-") >= 0 else stem
    return d, stem, ext or ".flv", segmented


def find_segments(output_path: str):
    """由录制输出路径(可为 %03d 模式或单文件)返回 [(idx, 文件路径)] 列表。
    idx: 分片序号字符串('000'...)或单文件模式 None。"""
    d, stem, ext, segmented = _split_output(output_path)
    if segmented:
        out = []
        for p in sorted(glob.glob(os.path.join(d, stem + "-*" + ext))):
            idx = os.path.splitext(os.path.basename(p))[0].rsplit("-", 1)[-1]
            if idx.isdigit():
                out.append((idx, p))
        return out
    p = os.path.join(d, stem + ext)
    return [(None, p)] if os.path.exists(p) else []


def align_path_for(output_path: str):
    return jsonl_for(output_path).replace(".danmaku.jsonl", ".align.json")


def load_align(output_path: str):
    p = align_path_for(output_path)
    for cand in (p, p.replace(os.sep + ".meta" + os.sep, os.sep)):
        try:
            with open(cand, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save_align(output_path: str, data: dict):
    p = align_path_for(output_path)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def anchor_segments(output_path: str, align: dict, drift_ms: float = 0.0):
    """为所有分片建立锚点(绝对毫秒,已校钟差)。返回 [(idx, path, anchor_ms)]。
    锚点优先级:FLV 首 tag(减钟差)> 文件 birthtime > 当前时刻(兜底)。"""
    anchors = align.setdefault("anchors", {})
    out = []
    for idx, path in find_segments(output_path):
        key = idx if idx is not None else "_"
        if key in anchors and anchors[key]:
            out.append((idx, path, anchors[key]))
            continue
        ts = flv_first_av_ts_abs(path)
        if ts is not None:
            a = int(ts - drift_ms)
        else:
            a = _birthtime_ms(path) or int(time.time() * 1000)
        anchors[key] = a
        out.append((idx, path, a))
    return out


# ── jsonl 事件读取 ───────────────────────────────────────
def load_events(jsonl_path: str):
    evs = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    evs.sort(key=lambda e: e.get("ts") or 0)
    return evs


# ── ASS 生成 ─────────────────────────────────────────────
def _ass_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return (str(text or "").replace("\\", "＼")
            .replace("{", "｛").replace("}", "｝").replace("\n", " "))


def _ass_header(font_size: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,PingFang SC,{font_size},&H00FFFFFF,&H00000000,&H80000000,-1,2,0,1,10,10,10,1
Style: Gift,PingFang SC,{max(24, font_size - 6)},&H0000E6E6,&H00000000,&H80000000,-1,2,1,2,20,20,20,1
Style: Member,PingFang SC,{max(20, font_size - 14)},&H00C8C8C8,&H00000000,&H80000000,0,1,0,1,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _lane_assign(lanes, start_sec):
    """滚动弹幕轨道分配:取最早空闲轨道。"""
    best, best_t = 0, None
    for i, t in enumerate(lanes):
        if best_t is None or t < best_t:
            best, best_t = i, t
    return best


def _scroll_lines(filtered, font_size, end_ms, anchor_ms, offset_sec):
    """旧样式:从右向左横向滚动。filtered 为 [(ev, t)]。"""
    lines = []
    lane_free = [0.0] * LANES
    lane_h = font_size + 10
    for ev, t in filtered:
        user = _esc(ev.get("user", ""))
        if ev.get("type") == "gift":
            count = ev.get("count") or 1
            dia = ev.get("diamond") or 0
            extra = f"({dia * count} 抖币)" if dia else ""
            text = _esc(f"{user} 送出 {ev.get('gift', '?')} ×{count} {extra}")
            lines.append(f"Dialogue: 1,{_ass_ts(t)},{_ass_ts(t + GIFT_SEC)},Gift,,0,0,0,,"
                         f"{{\\an2\\pos({PLAY_RES_X // 2},{PLAY_RES_Y - 70})}}{text}")
            continue
        content = _esc(ev.get("content", ""))
        if not content:
            continue
        text = f"{user}: {content}" if user and ev.get("type") == "chat" else \
               (f"{user} {content}" if user else content)
        w = len(text) * font_size  # 估算文字宽度(CJK 近似全宽)
        lane = _lane_assign(lane_free, t)
        y = 60 + lane * lane_h
        lane_free[lane] = t + SCROLL_SEC * (0.6 if w < PLAY_RES_X else 1.0)
        lines.append(
            f"Dialogue: 0,{_ass_ts(t)},{_ass_ts(t + SCROLL_SEC)},Danmaku,,0,0,0,,"
            f"{{\\move({PLAY_RES_X + 40},{y},{-w - 40},{y})}}{text}")
    return lines


def _queue_dialogue(text, x, y_from, y_to, start, end, font_size):
    """底部队列一条行段:从 y_from 在 SLIDE_MS 内滑到 y_to(\an2 底部对齐)。"""
    fs_tag = ""
    w = len(text) * font_size
    if w > PLAY_RES_X - 80:  # 超宽自动缩字号,保持单行完整
        fs = max(20, int(font_size * (PLAY_RES_X - 80) / w))
        fs_tag = f"\\fs{fs}"
    if abs(y_from - y_to) < 0.5:
        pos = f"{{\\an2\\pos({x},{y_from})}}"
    else:
        pos = f"{{\\an2\\pos({x},{y_from})\\t(0,{SLIDE_MS},\\pos({x},{y_to}))}}"
    return f"Dialogue: 0,{_ass_ts(start)},{_ass_ts(end)},Danmaku,,0,0,0,,{pos}{fs_tag}{text}"


def _queue_lines(filtered, font_size, end_ms, anchor_ms, offset_sec, dm_cfg):
    """新样式:屏幕下方最多 queue_lines 行队列,新发言从最下方挤入,旧字幕逐行上移。

    每条消息独立一行;行号变化时刻 = 后续消息到来时刻(全体上移一格),
    被顶出(超过 max_lines)或停留超过 queue_seconds 秒后消失。"""
    max_lines = max(1, min(int(dm_cfg.get("queue_lines", 5) or 5), QUEUE_MAX_LINES))
    hold = max(2.0, min(float(dm_cfg.get("queue_seconds", 8) or 8), 60.0))
    line_h = font_size + 10
    base_y = PLAY_RES_Y - QUEUE_BOTTOM_MARGIN
    y_above = QUEUE_TOP_MARGIN - line_h     # 屏幕外顶部一行
    y_below = base_y + line_h               # 屏幕外底部一行
    x = PLAY_RES_X // 2
    end_sec = None
    if end_ms is not None:
        end_sec = (end_ms - anchor_ms) / 1000.0 + offset_sec

    def y_of(row):
        if row == 0:
            return y_below
        if row == max_lines + 1:
            return y_above
        return base_y - (max_lines - row) * line_h

    chat_items = []   # (t, text)
    gift_lines = []
    for ev, t in filtered:
        user = _esc(ev.get("user", ""))
        if ev.get("type") == "gift":
            count = ev.get("count") or 1
            dia = ev.get("diamond") or 0
            extra = f"({dia * count} 抖币)" if dia else ""
            text = _esc(f"{user} 送出 {ev.get('gift', '?')} ×{count} {extra}")
            gift_lines.append((t, text))
            continue
        content = _esc(ev.get("content", ""))
        if not content:
            continue
        text = f"{user}: {content}" if user and ev.get("type") == "chat" else \
               (f"{user} {content}" if user else content)
        chat_items.append((t, text))
    chat_items.sort(key=lambda it: it[0])

    lines = []
    # 礼物:顶部横幅,不占 5 行队列
    for t, text in gift_lines:
        lines.append(f"Dialogue: 1,{_ass_ts(t)},{_ass_ts(t + GIFT_SEC)},Gift,,0,0,0,,"
                     f"{{\\an2\\pos({x},{QUEUE_TOP_MARGIN})}}{text}")
    # 队列弹幕:每条消息的行时间表
    n = len(chat_items)
    for i, (t_i, text) in enumerate(chat_items):
        eject_t = chat_items[i + max_lines][0] if i + max_lines < n else None
        end_t = min(t_i + hold, eject_t) if eject_t is not None else t_i + hold
        if end_sec is not None and end_t > end_sec:
            end_t = end_sec
        prev_y = y_below
        for j in range(max_lines):
            idx_j = i + j
            seg_start = chat_items[idx_j][0] if idx_j < n else end_t
            if seg_start >= end_t:
                break
            seg_end = min(chat_items[idx_j + 1][0], end_t) if idx_j + 1 < n else end_t
            if seg_end <= seg_start:
                continue
            row = max_lines - j
            y_now = y_of(row)
            lines.append(_queue_dialogue(text, x, prev_y, y_now,
                                         seg_start, seg_end, font_size))
            prev_y = y_now
            if seg_end >= end_t - 1e-6:
                # 被顶出(非到期)时,追加一段滑出顶部动画
                if eject_t is not None and end_t == eject_t and \
                        (end_sec is None or end_t < end_sec):
                    lines.append(_queue_dialogue(text, x, y_now, y_above,
                                                 end_t, min(end_t + 0.2, end_sec or 1e18),
                                                 font_size))
                break
    return lines


def generate_ass_for_segment(path_out, events, anchor_ms, offset_sec, cfg,
                             end_ms=None):
    """为一个分片生成 .ass。events 为该时间区间内的弹幕事件列表。"""
    dm_cfg = cfg.get("danmaku") or {}
    font_size = int(dm_cfg.get("font_size", 44))
    show_member = bool(dm_cfg.get("capture_member", False))
    lines = [_ass_header(font_size)]
    style = str(dm_cfg.get("style", "queue") or "queue").lower()

    filtered = []
    for ev in events:
        if ev.get("type") == "member" and not show_member:
            continue
        t = ((ev.get("ts") or 0) - anchor_ms) / 1000.0 + offset_sec
        if t < 0:
            continue  # 事件早于视频起点(录制启动前),丢弃
        if end_ms is not None and (ev.get("ts") or 0) > end_ms + GRACE_AFTER_END * 1000:
            continue
        filtered.append((ev, t))

    if style == "scroll":
        lines += _scroll_lines(filtered, font_size, end_ms, anchor_ms, offset_sec)
    else:
        lines += _queue_lines(filtered, font_size, end_ms, anchor_ms, offset_sec, dm_cfg)

    with open(path_out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def ass_path_for_segment(seg_path: str) -> str:
    return os.path.splitext(seg_path)[0] + ".ass"


def effective_offset(align: dict, cfg, idx) -> float:
    """分片生效偏移 = (人工本场全局 or 配置全局) + 单分片额外偏移。"""
    manual = align.get("global_offset")
    if not isinstance(manual, (int, float)):
        manual = float((cfg.get("danmaku") or {}).get("offset_seconds", 0) or 0)
    extra = (align.get("per_segment") or {}).get(idx if idx is not None else "_", 0) or 0
    return float(manual) + float(extra)


def generate_session(output_path: str, cfg, align=None, drift_ms=0.0):
    """为一次录制会话(输出路径/模式)生成全部分片的 .ass。
    返回生成的分片信息列表 [{idx, file, anchor, offset}]。"""
    if align is None:
        align = load_align(output_path)
    segs = anchor_segments(output_path, align, drift_ms)
    if not segs:
        return []
    events = load_events(jsonl_for(output_path))
    info = []
    for i, (idx, path, anchor) in enumerate(segs):
        next_anchor = segs[i + 1][2] if i + 1 < len(segs) else None
        # 本分片时间区间 [anchor, next_anchor);末片吃到剩余全部
        evs = [e for e in events
               if e.get("ts", 0) >= anchor and (next_anchor is None or e.get("ts", 0) < next_anchor)]
        last = flv_last_ts_abs(path)
        end_ms = int(last - drift_ms) if last is not None else None
        off = effective_offset(align, cfg, idx)
        generate_ass_for_segment(ass_path_for_segment(path), evs, anchor, off, cfg, end_ms)
        info.append({"idx": idx, "file": path, "anchor": anchor, "offset": off})
    align.setdefault("config_offset", float((cfg.get("danmaku") or {}).get("offset_seconds", 0) or 0))
    save_align(output_path, align)
    return info


def summarize(jsonl_path: str):
    """jsonl → 汇总(弹幕数/礼物抖币/礼物榜)。供面板与 AI 管线消费。"""
    chat = gift = diamonds = 0
    gift_top = {}
    for e in load_events(jsonl_path):
        if e.get("type") == "chat":
            chat += 1
        elif e.get("type") == "gift":
            gift += 1
            n = e.get("count") or 1
            diamonds += (e.get("diamond") or 0) * n
            k = e.get("gift") or "?"
            gift_top[k] = gift_top.get(k, 0) + n
    top = sorted(gift_top.items(), key=lambda kv: -kv[1])[:10]
    return {"chat": chat, "gift": gift, "diamonds": diamonds,
            "gift_top": [{"gift": k, "count": v} for k, v in top]}


# ── 巡检(录制中每 60s 由 monitor 调用)─────────────────────
def patrol(output_path: str, cfg, drift_ms=0.0, last_msg_ts=0.0):
    """单次巡检:锚定新分片、检测停滞/弹幕缺口、重生成本次会话全部 .ass。
    返回 (seg_info, 新发现的异常列表)。异常写入 align.json 的 gaps。"""
    align = load_align(output_path)
    segs = anchor_segments(output_path, align, drift_ms)
    anomalies = []
    if segs:
        idx, path, anchor = segs[-1]
        last = flv_last_ts_abs(path)
        if last is not None:
            lag = time.time() * 1000 - (last - drift_ms)
            gaps = align.setdefault("gaps", [])
            if lag > 30_000:
                if not gaps or gaps[-1].get("type") != "stall" or \
                        gaps[-1].get("to", 0) < time.time() * 1000 - 90_000:
                    gaps.append({"type": "stall", "from": last - drift_ms,
                                 "to": time.time() * 1000,
                                 "note": f"最后分片时间戳落后墙钟 {lag / 1000:.0f}s"})
                    anomalies.append(f"视频停滞 {lag / 1000:.0f}s")
    if last_msg_ts and time.time() - last_msg_ts > 60:
        gaps = align.setdefault("gaps", [])
        if not gaps or gaps[-1].get("type") != "danmaku_gap" or \
                gaps[-1].get("to", 0) < time.time() * 1000 - 180_000:
            gaps.append({"type": "danmaku_gap", "from": (last_msg_ts or time.time()) * 1000,
                         "to": time.time() * 1000, "note": "弹幕消息中断超过 60s"})
            anomalies.append("弹幕中断")
    if drift_ms:
        align["drift_ms"] = round(drift_ms, 1)
    info = generate_session(output_path, cfg, align, drift_ms)
    return info, anomalies


def finalize(output_path: str, cfg, drift_ms=0.0):
    """录制结束:最后生成一次全部 .ass 并写入汇总。"""
    align = load_align(output_path)
    align["finished_at"] = int(time.time() * 1000)
    info = generate_session(output_path, cfg, align, drift_ms)
    align["summary"] = summarize(jsonl_for(output_path))
    save_align(output_path, align)
    return info
