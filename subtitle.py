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
- 礼物独立偏移:礼物走与弹幕不同的消息通道(轻礼物是连击聚合消息,时间戳
  常整体滞后),故额外支持 gift_global_offset(本场) + gift_per_segment(单分片)
  + 配置 danmaku.gift_offset_seconds。生效值叠加在弹幕偏移之上,默认 0 即
  与弹幕完全一致(向后兼容)。

字幕样式(danmaku.style):
- queue(默认):屏幕下方最多 5 行队列,每条发言独立一行;新发言从最下方
  挤入,旧字幕逐行上移,满 5 行最旧的滑出顶部;每条最长停留 queue_seconds 秒。
- scroll(旧):从右向左横向滚动。

画布(PlayRes)与播放器兼容:
- 下文所有「版式单位」都指 1080p 横屏设计画布(1920×1080),字号/边距/行高
  常量都以此为基准。
- ASS 的 PlayResX/PlayResY 是坐标空间声明,必须与视频真实尺寸一致。抖音直播
  流是竖屏(典型 1088×1920),照抄横屏 1920×1080 会让文件声明的宽高比与画面
  不符:libass 系按「横向 W/PlayResX、纵向 H/PlayResY」非等比映射,而等比
  适配的播放器会把整块弹幕挤到画面中部,结果因播放器而异。
- 现在每片按自身 FLV 的 onMetaData 取真实尺寸当画布(见 Canvas),横向坐标按
  宽度比、字号与纵向坐标按高度比换算;横屏 1080p 视频换算系数为 1,输出与
  历史版本逐字节一致,竖屏则与旧文件在 libass 下渲染结果一致。
- 由此还修掉一处同源缺陷:自动缩字号的宽度阈值原先用版式宽度 1920 判断,
  而竖屏真实可用宽度只有 1920×W/H(1088 宽 → 612 版式单位),长弹幕会按原
  字号渲染后左右被切掉。现在按画布宽度判断。
"""
import glob
import json
import os
import struct
import time

from danmaku import jsonl_for
import gift_dict as gift_dict_mod
import gift_display as gift_display_mod

GRACE_AFTER_END = 60          # 视频末尾之后仍保留的弹幕秒数(播放器会忽略超长时刻)
LANES = 12                     # 滚动弹幕轨道数
SCROLL_SEC = 10                # 弹幕滚过屏幕耗时
GIFT_SEC = 5                   # 礼物横幅停留秒数(旧版单横幅路径;新版见 gift_display)

# 版式设计画布(横屏 1080p):所有字号/边距/行高常量都以它为设计单位。
# 真实画布 = 该片视频的像素尺寸(见 Canvas);横屏 1080p 视频下换算系数为 1。
DESIGN_W, DESIGN_H = 1920, 1080
PLAY_RES_X, PLAY_RES_Y = DESIGN_W, DESIGN_H   # 兼容旧名:版式基准宽高

# 底部队列样式参数
QUEUE_MAX_LINES = 8            # queue_lines 上限
QUEUE_BOTTOM_MARGIN = 60       # 底部 5 行区域距屏幕底边
QUEUE_TOP_MARGIN = 60          # 顶部礼物横幅区域
SLIDE_MS = 150                 # 行间上移动画时长(ms)
TEXT_SIDE_MARGIN = 80          # 单行左右预留(版式单位),超出即自动缩字号
FONT_SIZE_FLOOR = 20           # 自动缩字号下限(版式单位)


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


def flv_last_ts_any(path: str, window: int = 524288):
    """录制中文件的「最近一包时间戳」:严格版读不到时按窗口回扫。

    `flv_last_ts` 只认一种布局 —— 文件最后 4 字节正好是 prevSize(写完时才成立)。
    而 ffmpeg 边写边 flush 时,文件末尾几乎总压着一截没写完的 tag,于是它大概率
    返回 None(实测:录制中连试 3 次全 None),时钟偏移就采不到、锚点退化 birthtime。

    这里退一步:读末尾 window 字节,找出其中**最后一个自洽的 tag**
    (type ∈ {8,9,18} 且紧随其后的 prevSize 对得上),用它当"最近一包"。
    精度足够 —— 偏移取的是最小值,要的是"近期",不是"恰好最末一帧"。
    """
    try:
        size = os.path.getsize(path)
        if size < 26:
            return None
        start = max(13, size - window)
        with open(path, "rb") as f:
            f.seek(start)
            buf = f.read(size - start)
        best = None
        for i in range(len(buf) - 15):
            if buf[i] not in (8, 9, 18):
                continue
            dsize = (buf[i + 1] << 16) | (buf[i + 2] << 8) | buf[i + 3]
            end = i + 11 + dsize
            if end + 4 > len(buf):
                continue
            prev = struct.unpack(">I", buf[end:end + 4])[0]
            if prev != dsize + 11:
                continue
            best = (buf[i + 7] << 24) | (buf[i + 4] << 16) | (buf[i + 5] << 8) | buf[i + 6]
        return best
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


# ── 源流时间戳模式下的绝对时钟还原(2026-10-27) ──────────────
# recorder.src_timestamps=True 时录制不再加 -use_wallclock_as_timestamps:
# FLV tag 里是**推流端相对毫秒**(整场连续、音视频同一坐标系),不是墙钟。
# 字幕锚点仍然需要绝对墙钟,所以由 monitor 在巡检时采
# 「当前墙钟 − 末包源时间戳」并存进 align.json,录制期取**最小值**
# (= 最小缓冲/网络延迟,比单次采样更接近真值)。
# 旧文件(墙钟已烧进 tag,值 > RELATIVE_TS_MAX)走原解回绕通道,两种形态共存。
CLOCK_OFFSET_KEY = "clock_offset_ms"


def clock_offset(align: dict):
    """align.json 里的「源ts → 绝对墙钟」偏移(毫秒);未采样/旧文件返回 None。"""
    v = (align or {}).get(CLOCK_OFFSET_KEY)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def note_clock_offset(output_path: str, sample_ms: int):
    """记录一次 (墙钟 − 末包源ts) 采样,保留最小值。返回当前生效值。

    只在变小的时候落盘(原子写),避免 20s 一轮巡检反复重写 align.json。
    写盘失败不抛 —— 采样是旁路,绝不能打断录制链路。

    采样值的物理含义是「推流起点对应的墙钟毫秒」,必然落在 (0, 现在] 之间;
    越界说明末包时间戳解析到了垃圾字节,直接丢弃(否则最小值会被一条坏样本毒死)。
    """
    try:
        now_ms = int(time.time() * 1000)
        if not isinstance(sample_ms, int) or sample_ms <= 0 or sample_ms > now_ms:
            return clock_offset(load_align(output_path))
        align = load_align(output_path)
        cur = clock_offset(align)
        if cur is not None and sample_ms >= cur:
            return cur
        # .meta/ 可能还不存在(弹幕未开或 jsonl 尚未落盘),先建目录再原子写
        p = align_path_for(output_path)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        align[CLOCK_OFFSET_KEY] = int(sample_ms)
        save_align(output_path, align)
        return int(sample_ms)
    except (OSError, ValueError, TypeError):
        return None



# ffmpeg 的 FLV muxer 把墙钟时间戳截成 **31 位**(mod 2^31),不是标准 mod 2^32!
# 实测(2026-09-21,某场次分片):tag 值 899,855,307,
# 文件 mtime 的 epoch mod 2^32 = 3,047,340,114,差恰为 2^31(余 1.2s 是 mtime
# 与末帧 tag 的正常缓冲差)。等价于 flvenc 内部 int32 溢出后丢掉符号位。
# epoch mod 2^32 在 [2^31, 2^32) 区间时(2026-09-18 15:26 ~ 2026-10-11),
# 按 32 位解会偏 ±24.8 天 → 被 _sanitize_abs 拒绝 → *_ts_abs 全部返回 None,
# 弹幕 gap 巡检/媒体时长估计等依赖方**静默失效**(停滞检测走文件大小,不受影响)。
# 按 31 位域解,新旧文件(无论当时 epoch 落在哪个窗口)都正确。
_UNWRAP_MASK = 0x7FFFFFFF
_UNWRAP_HALF = 0x40000000     # ±2^30 ≈ ±12.4 天,远超任何单文件录制/归档偏移


def _unwrap_ts(raw, ref_ms):
    """FLV tag 时间戳 → 绝对毫秒(31 位回绕,见上方 UNWRAP_MASK 说明)。

    以 ref_ms(通常为文件 birthtime)为基准,解到同 31 位周期内最近的绝对值。"""
    if raw is None or ref_ms is None:
        return None
    delta = ((raw - (ref_ms & _UNWRAP_MASK) + _UNWRAP_HALF) & _UNWRAP_MASK) - _UNWRAP_HALF
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


def flv_first_av_ts_abs(path: str, offset_ms=None):
    """首个音/视频 tag 的绝对毫秒。两种录制形态都认:

    · 旧形态(tag 里是墙钟,值 > RELATIVE_TS_MAX):解 31 位回绕后即绝对值;
    · 新形态(tag 里是推流端相对毫秒,见 CLOCK_OFFSET_KEY):`源ts + offset_ms`。

    `offset_ms` 传 align.json 的 clock_offset_ms;新形态下拿不到偏移就返回 None
    (调用方回退 birthtime,与旧逻辑一致)。
    """
    raw = flv_first_av_ts(path)
    if raw is None:
        return None
    if raw >= RELATIVE_TS_MAX:
        birth = _birthtime_ms(path)
        return _sanitize_abs(_unwrap_ts(raw, birth), birth)
    if offset_ms is None:
        return None
    return int(raw) + int(offset_ms)


def flv_last_ts_abs(path: str, offset_ms=None):
    """最后一个 tag 的绝对毫秒,用于停滞检测与时长估计。

    尾部损坏/数值不合理时返回 None,调用方跳过本次检测(不写入荒谬 gap)。
    `offset_ms` 语义同 flv_first_av_ts_abs。
    """
    raw = flv_last_ts(path)
    if raw is None:
        return None
    if raw >= RELATIVE_TS_MAX:
        birth = _birthtime_ms(path)
        return _sanitize_abs(_unwrap_ts(raw, birth), birth)
    if offset_ms is None:
        return None
    return int(raw) + int(offset_ms)


# ── 视频画布尺寸(FLV onMetaData,纯 Python 解析 AMF0)──────
# 为什么不用 ffprobe:本机 PATH 上不一定有 ffprobe(项目只依赖 ffmpeg),
# 而 onMetaData 就在文件头部第一个 script tag 里,录制中的文件也能直接读。
_AMF0_NUMBER, _AMF0_BOOL, _AMF0_STR, _AMF0_OBJ = 0x00, 0x01, 0x02, 0x03
_AMF0_ECMA, _AMF0_STRICT, _AMF0_NULL, _AMF0_UNDEF, _AMF0_DATE = 0x08, 0x0A, 0x05, 0x06, 0x0B
_SIZE_MIN, _SIZE_MAX = 16, 16384


def _amf0_skip_body(b: bytes, i: int, depth: int):
    """跳过 AMF0 object / ECMA array 的键值体(以 00 00 09 结束),返回新下标。"""
    while True:
        if i is None or i + 2 > len(b):
            return None
        ln = int.from_bytes(b[i:i + 2], "big")
        i += 2
        if ln == 0:
            return i + 1 if i < len(b) and b[i] == 0x09 else None
        i += ln
        i = _amf0_skip(b, i, depth + 1)


def _amf0_skip(b: bytes, i: int, depth: int = 0):
    """跳过 AMF0 值,返回新下标;越界/未知类型返回 None。"""
    if i is None or i >= len(b) or depth > 8:
        return None
    t = b[i]
    i += 1
    if t == _AMF0_NUMBER:
        return i + 8 if i + 8 <= len(b) else None
    if t in (_AMF0_BOOL, _AMF0_NULL, _AMF0_UNDEF):
        return i + 1 if t == _AMF0_BOOL and i + 1 <= len(b) else i
    if t in (_AMF0_STR, 0x0C):                     # string / long string
        n = 2 if t == _AMF0_STR else 4
        if i + n > len(b):
            return None
        ln = int.from_bytes(b[i:i + n], "big")
        return i + n + ln if i + n + ln <= len(b) else None
    if t == _AMF0_OBJ:
        return _amf0_skip_body(b, i, depth)
    if t == _AMF0_ECMA:
        return _amf0_skip_body(b, i + 4, depth) if i + 4 <= len(b) else None
    if t == _AMF0_STRICT:
        if i + 4 > len(b):
            return None
        n = int.from_bytes(b[i:i + 4], "big")
        i += 4
        for _ in range(n):
            i = _amf0_skip(b, i, depth + 1)
            if i is None:
                return None
        return i
    if t == _AMF0_DATE:
        return i + 10 if i + 10 <= len(b) else None
    return None


def _meta_size(body: bytes):
    """script tag 载荷 → (width, height);缺失或异常返回 None。"""
    if not body or body[0] != _AMF0_STR:
        return None
    i = _amf0_skip(body, 0)                        # 跳过 "onMetaData"
    if i is None or i >= len(body) or body[i] != _AMF0_ECMA:
        return None
    i += 5                                         # 0x08 + 4 字节计数
    found = {}
    while True:
        if i + 2 > len(body):
            return None
        ln = int.from_bytes(body[i:i + 2], "big")
        i += 2
        if ln == 0:
            break
        key = body[i:i + ln].decode("utf-8", "replace")
        i += ln
        if i < len(body) and body[i] == _AMF0_NUMBER and i + 9 <= len(body):
            val = struct.unpack(">d", body[i + 1:i + 9])[0]
            if key in ("width", "height"):
                found[key] = val
        i = _amf0_skip(body, i)
        if i is None:
            return None
    try:
        w, h = int(found["width"]), int(found["height"])
    except (KeyError, ValueError, OverflowError):
        return None
    if _SIZE_MIN <= w <= _SIZE_MAX and _SIZE_MIN <= h <= _SIZE_MAX:
        return (w, h)
    return None


def flv_video_size(path: str):
    """FLV 头部 onMetaData 里的真实画面尺寸 (width, height);读不到返回 None。

    只读文件头部(metadata 是第一个 tag),录制中的分片同样可读;
    无 script tag、无该键或数值离谱时返回 None,由调用方回退版式基准。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(13)
            if len(head) < 13 or head[:3] != b"FLV":
                return None
            for _ in range(4):
                h = f.read(11)
                if len(h) < 11:
                    return None
                size = (h[1] << 16) | (h[2] << 8) | h[3]
                if h[0] == 18:                     # script tag(onMetaData)
                    return _meta_size(f.read(size))
                f.seek(size + 4, 1)                # 跳过 body + PreviousTagSize
    except OSError:
        return None
    return None


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
    锚点优先级:FLV 首 tag(减钟差)> 文件 birthtime > 当前时刻(兜底)。

    首 tag 的绝对时间有两种来源(见 CLOCK_OFFSET_KEY):
    · 旧文件:tag 本身就是墙钟;
    · 新文件:tag 是推流端相对毫秒,加 align.json 里采样得来的 clock_offset_ms。

    固化锚点的校正:固化值若与 FLV 权威首帧差 > 3s,说明它是 birthtime 回退的
    产物 —— 2026-09-18~09-21 期间 ffmpeg 31 位截断让 flv_first_av_ts_abs 集体
    失效(见 _UNWRAP_MASK),当时固化的锚点普遍晚 1~3s(ffmpeg 起分片的启动延迟),
    表现为该分片字幕整体偏晚。FLV 首帧是权威值,差超阈值时覆盖固化值。
    """
    anchors = align.setdefault("anchors", {})
    off = clock_offset(align)
    out = []
    for idx, path in find_segments(output_path):
        key = idx if idx is not None else "_"
        ts = flv_first_av_ts_abs(path, off)
        exact = int(ts - drift_ms) if ts is not None else None
        fixed = anchors.get(key)
        if fixed:
            if exact is not None and abs(fixed - exact) > 3000:
                anchors[key] = exact          # birthtime 回退的陈旧锚点 → 校正
                out.append((idx, path, exact))
            else:
                out.append((idx, path, fixed))
            continue
        a = exact if exact is not None else (_birthtime_ms(path) or int(time.time() * 1000))
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


# ── 画布换算 ─────────────────────────────────────────────
class Canvas:
    """把「1080p 横屏版式」换算到某一片视频的真实画布。

    横向与纵向分开换算:
    - 纵向(含字号)= 画布高 / 版式高(竖屏 1920/1080 = 1.78),文字大小由画面
      高度决定,故字号跟随纵向;
    - 横向 = 画布宽 / 版式宽(竖屏 1088/1920 = 0.57)。
    两者只在横屏 1080p 视频下都等于 1,所以横屏视频输出与历史版本完全一致。
    """
    __slots__ = ("w", "h", "kx", "ky")

    def __init__(self, w: int, h: int):
        self.w, self.h = int(w), int(h)
        self.kx = self.w / DESIGN_W
        self.ky = self.h / DESIGN_H

    @classmethod
    def for_video(cls, path):
        """按视频真实尺寸建画布;路径为空或读不到元数据时回退版式基准。"""
        size = flv_video_size(path) if path else None
        return cls(*size) if size else cls(DESIGN_W, DESIGN_H)

    def x(self, v) -> int:
        """版式横向坐标 → 画布横向坐标。"""
        return int(round(v * self.kx))

    def y(self, v) -> int:
        """版式纵向坐标 → 画布纵向坐标。"""
        return int(round(v * self.ky))

    def size(self, v) -> int:
        """版式字号 → 画布字号(与纵向同比例)。"""
        return int(round(v * self.ky))

    @property
    def is_design(self) -> bool:
        return (self.w, self.h) == (DESIGN_W, DESIGN_H)

    def __repr__(self):
        return f"Canvas({self.w}x{self.h})"


def _text_em(text: str) -> float:
    """文本宽度估算(单位:字号)。CJK/全角算 1.0 em,ASCII 算 0.55 em。"""
    n = 0.0
    for ch in text:
        n += 1.0 if ord(ch) >= 0x1100 else 0.55
    return n


def _fit_font(text: str, font_size: int, canvas: Canvas):
    """按画布宽度自动缩字号,返回 (画布字号, 是否缩过)。

    可用宽度必须用画布宽度:字号随画布高度缩放、横向坐标随画布宽度缩放,
    竖屏画面下两者相差约 3 倍(1088×1920 → 可用宽度只有 612 版式单位)。
    沿用版式宽度 1920 判断「放得下」,长弹幕会按原字号渲染后左右被切掉。
    """
    fs = canvas.size(font_size)
    avail = canvas.w - canvas.x(TEXT_SIDE_MARGIN)
    est = _text_em(text) * fs
    if est <= avail:
        return fs, False
    return max(canvas.size(FONT_SIZE_FLOOR), int(fs * avail / est)), True


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


def _ass_header(font_size: int, canvas: Canvas, gift_layout: dict = None) -> str:
    """Script Info + 样式表,PlayRes 写视频真实尺寸,字号/边距换算到画布。

    Style 的对齐与边距是「内联 \\pos 被忽略时」的兜底:弹幕底部居中、
    礼物横幅顶部居中,与内联标签的意图一致(硬件播放器固件、ASS→SRT 转换
    工具常丢掉 \\pos/动画,兜底值能保证位置仍然合理)。

    gift_layout 非空时（竖屏 portrait-v1）额外输出 GiftSlow / GiftFast 两种样式：
    左上锚点(\\an7 兜底 Alignment=7)、字号直接是画布真实像素（版式层已按
    scale 换算），慢车道粗体高亮、快车道常规。
    """
    gift_styles = ""
    if gift_layout:
        gift_styles = (
            f"Style: GiftSlow,PingFang SC,{gift_layout['slow_font']},&H0000D7FF,&H00000000,"
            f"&H80000000,-1,2,1,7,{gift_layout['side_margin_px']},{gift_layout['side_margin_px']},"
            f"{gift_layout['top_margin_px']},1\n"
            f"Style: GiftFast,PingFang SC,{gift_layout['fast_font']},&H00E6E6E6,&H00000000,"
            f"&H80000000,0,2,1,7,{gift_layout['side_margin_px']},{gift_layout['side_margin_px']},"
            f"{gift_layout['top_margin_px']},1\n"
        )
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas.w}
PlayResY: {canvas.h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,PingFang SC,{canvas.size(font_size)},&H00FFFFFF,&H00000000,&H80000000,-1,2,0,2,10,10,{canvas.y(QUEUE_BOTTOM_MARGIN)},1
Style: Gift,PingFang SC,{canvas.size(max(24, font_size - 6))},&H0000E6E6,&H00000000,&H80000000,-1,2,1,8,20,20,{canvas.y(QUEUE_TOP_MARGIN)},1
Style: Member,PingFang SC,{canvas.size(max(20, font_size - 14))},&H00C8C8C8,&H00000000,&H80000000,0,1,0,2,10,10,10,1
{gift_styles}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ── 竖屏礼物版式(portrait-v1)──────────────────────────────
def gift_layout_portrait(canvas: Canvas, cfg) -> dict:
    """按分片真实像素算礼物版式；非竖屏返回 None（走旧的单横幅路径）。

    与 `Canvas.ky` 的横屏放大规则不同：新版用独立基准 1080×1920，
    `scale = min(w/1080, h/1920)`，1080×1920 下为 1、720×1280 下为 2/3。
    这样竖屏不会因为"高/1080=1.78"把礼物字撑到 78px。
    """
    gc = gift_display_mod.config(cfg)
    lay = gift_display_mod.layout_of(gc)
    if canvas.h <= canvas.w:            # 横屏/方屏：本次不覆盖
        return None
    scale = min(canvas.w / float(lay["ref_w"]), canvas.h / float(lay["ref_h"]))
    row_h = lay["row_height"] * scale
    top = lay["top_margin"] * scale
    side = lay["side_margin"] * scale
    slots = lay["slots"]
    # 硬上限：多行叠加最多占画面高度 max_area_ratio（默认 2/5）。layout_of 已按基准
    # 裁过一轮，这里再兜一次底，防止缩放取整后越界。
    max_area_px = float(lay["max_area_ratio"]) * canvas.h
    while slots > 1 and top + slots * row_h > max_area_px + 0.5:
        slots -= 1
    return {
        "profile": lay["profile"],
        "scale": scale,
        "slots": slots,
        "row_height": row_h,
        "top_margin_px": int(round(top)),
        "side_margin_px": int(round(side)),
        "slot_y": [int(round(top + i * row_h)) for i in range(slots)],
        "area_bottom": int(round(top + (slots - 1) * row_h + row_h)),
        "max_area_ratio": lay["max_area_ratio"],
        "max_area_px": int(round(max_area_px)),
        "slow_font": max(1, int(round(lay["slow_font"] * scale))),
        "fast_font": max(1, int(round(lay["fast_font"] * scale))),
        "slow_font_min": max(1, int(round(lay["slow_font_min"] * scale))),
        "fast_font_min": max(1, int(round(lay["fast_font_min"] * scale))),
        "avail_w": max(1, canvas.w - 2 * int(round(side))),
        "canvas_w": canvas.w,
        "canvas_h": canvas.h,
    }


def _fit_gift_text(text, font_size, font_min, avail_w):
    """礼物文案适配：先缩字号（到下限），再截断昵称；礼物名/份数/金额不截断。

    返回 (最终文本, 字号)。
    """
    fs = font_size
    if _text_em(text) * fs <= avail_w:
        return text, fs
    if _text_em(text) * font_min <= avail_w:
        return text, font_min
    # 仍超宽 → 缩昵称（只对含 " 送出 " 的完整通道文案生效）
    head, sep, tail = text.partition(" 送出 ")
    if not sep:
        return text, font_min
    nick = head
    while len(nick) > 2:
        nick = nick[:-1]
        cand = nick + "…" + sep + tail
        if _text_em(cand) * font_min <= avail_w:
            return cand, font_min
    return nick + "…" + sep + tail, font_min


def gift_dialogues(items, layout, anchor_ms, off, g_extra, session_start_ms, seg_end_sec=None):
    """把展示计划里的礼物项渲染成 ASS Dialogue 行（\\an7 左上锚点）。"""
    lines = []
    if not items or not layout:
        return lines
    for it in items:
        if "start_ms" not in it or "end_ms" not in it:
            continue                      # 未经调度的项不渲染（防御：只接受计划项）
        slot = int(it.get("slot") or 0)
        y = layout["slot_y"][min(slot, len(layout["slot_y"]) - 1)]
        x = layout["side_margin_px"]
        start_abs = session_start_ms + int(it["start_ms"])
        t0 = (start_abs - anchor_ms) / 1000.0 + off + g_extra
        dur = max(0.0, (int(it["end_ms"]) - int(it["start_ms"])) / 1000.0)
        if t0 < 0 or dur <= 0:
            continue
        style = "GiftSlow" if it.get("lane") == "slow" else "GiftFast"
        base_fs = layout["slow_font"] if style == "GiftSlow" else layout["fast_font"]
        fs_min = layout["slow_font_min"] if style == "GiftSlow" else layout["fast_font_min"]
        segs = it.get("segments") or [{"t_ms": it["start_ms"], "text": it.get("name", "?")}]
        for i, sg in enumerate(segs):
            s_off = max(0.0, (int(sg["t_ms"]) - int(it["start_ms"])) / 1000.0)
            s_start = t0 + s_off
            s_end = t0 + ((int(segs[i + 1]["t_ms"]) - int(it["start_ms"])) / 1000.0
                          if i + 1 < len(segs) else dur)
            if seg_end_sec is not None:
                s_end = min(s_end, seg_end_sec)
            if s_end <= s_start:
                continue
            text, fs = _fit_gift_text(_esc(sg.get("text") or ""), base_fs, fs_min,
                                      layout["avail_w"])
            fs_tag = f"\\fs{fs}" if fs != base_fs else ""
            lines.append(f"Dialogue: 2,{_ass_ts(s_start)},{_ass_ts(s_end)},{style},,0,0,0,,"
                         f"{{\\an7\\pos({x},{y}){fs_tag}}}{text}")
    return lines


def _lane_assign(lanes, start_sec):
    """滚动弹幕轨道分配:取最早空闲轨道。"""
    best, best_t = 0, None
    for i, t in enumerate(lanes):
        if best_t is None or t < best_t:
            best, best_t = i, t
    return best


def _scroll_lines(filtered, font_size, end_ms, anchor_ms, offset_sec, canvas):
    """旧样式:从右向左横向滚动。filtered 为 [(ev, t)]。"""
    lines = []
    lane_free = [0.0] * LANES
    lane_h = font_size + 10
    cx = canvas.x(PLAY_RES_X // 2)
    for ev, t in filtered:
        user = _esc(ev.get("user", ""))
        if ev.get("type") == "gift":
            count = ev.get("count") or 1
            dia = ev.get("diamond") or 0
            extra = f"({dia * count} 抖币)" if dia else ""
            text = _esc(f"{user} 送出 {ev.get('gift', '?')} ×{count} {extra}")
            lines.append(f"Dialogue: 1,{_ass_ts(t)},{_ass_ts(t + GIFT_SEC)},Gift,,0,0,0,,"
                         f"{{\\an2\\pos({cx},{canvas.y(PLAY_RES_Y - 70)})}}{text}")
            continue
        content = _esc(ev.get("content", ""))
        if not content:
            continue
        text = f"{user}: {content}" if user and ev.get("type") == "chat" else \
               (f"{user} {content}" if user else content)
        # 文本宽度:按字号宽度比换算到画布横向单位(见 _fit_font 注释)
        w = _text_em(text) * canvas.size(font_size)
        lane = _lane_assign(lane_free, t)
        y = canvas.y(60 + lane * lane_h)
        lane_free[lane] = t + SCROLL_SEC * (0.6 if w < canvas.w else 1.0)
        lines.append(
            f"Dialogue: 0,{_ass_ts(t)},{_ass_ts(t + SCROLL_SEC)},Danmaku,,0,0,0,,"
            f"{{\\move({canvas.x(PLAY_RES_X + 40)},{y},{-w - canvas.x(40)},{y})}}{text}")
    return lines


def _queue_dialogue(text, x, y_from, y_to, start, end, font_size, canvas):
    """底部队列一条行段:从 y_from 在 SLIDE_MS 内滑到 y_to(\\an2 底部对齐)。

    x/y 均为画布坐标;超宽时按画布宽度自动缩字号(见 _fit_font)。

    位置动画用 \\move 而不是 \\t(0,150,\\pos(...)):libass 不支持用 \\t 改 \\pos
    (实测 ffmpeg/libass 下 \\t 版本在 2s 后仍停在起始 y),于是每条弹幕都比
    设计位置低一行、最新一条永远贴在画面最底边。\\move 在 libass/VSFilter/
    mpv/VLC 下都生效(已实测终态落到 y_to)。
    缩字号标签必须写在覆盖块 {} 内 —— 旧代码写在块外,播放器会把它当正文
    原样显示(该分支此前也从未触发过,所以一直没暴露)。
    """
    fs, shrunk = _fit_font(text, font_size, canvas)
    fs_tag = f"\\fs{fs}" if shrunk else ""
    if abs(y_from - y_to) < 0.5:
        pos = f"{{\\an2\\pos({x},{y_from}){fs_tag}}}"
    else:
        pos = f"{{\\an2\\move({x},{y_from},{x},{y_to},0,{SLIDE_MS}){fs_tag}}}"
    return f"Dialogue: 0,{_ass_ts(start)},{_ass_ts(end)},Danmaku,,0,0,0,,{pos}{text}"


def _queue_lines(filtered, font_size, end_ms, anchor_ms, offset_sec, dm_cfg, canvas):
    """新样式:屏幕下方最多 queue_lines 行队列,新发言从最下方挤入,旧字幕逐行上移。

    每条消息独立一行;行号变化时刻 = 后续消息到来时刻(全体上移一格),
    被顶出(超过 max_lines)或停留超过 queue_seconds 秒后消失。
    行高与各 y 直接算成画布坐标(版式常量先换算,避免逐次取整漂移)。"""
    max_lines = max(1, min(int(dm_cfg.get("queue_lines", 5) or 5), QUEUE_MAX_LINES))
    hold = max(2.0, min(float(dm_cfg.get("queue_seconds", 8) or 8), 60.0))
    line_h = canvas.size(font_size + 10)
    base_y = canvas.y(PLAY_RES_Y - QUEUE_BOTTOM_MARGIN)
    y_above = canvas.y(QUEUE_TOP_MARGIN) - line_h   # 屏幕外顶部一行
    y_below = base_y + line_h                        # 屏幕外底部一行
    x = canvas.x(PLAY_RES_X // 2)
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
                     f"{{\\an2\\pos({x},{canvas.y(QUEUE_TOP_MARGIN)})}}{text}")
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
                                         seg_start, seg_end, font_size, canvas))
            prev_y = y_now
            if seg_end >= end_t - 1e-6:
                # 被顶出(非到期)时,追加一段滑出顶部动画
                if eject_t is not None and end_t == eject_t and \
                        (end_sec is None or end_t < end_sec):
                    lines.append(_queue_dialogue(text, x, y_now, y_above,
                                                 end_t, min(end_t + 0.2, end_sec or 1e18),
                                                 font_size, canvas))
                break
    return lines


def _sibling_video(ass_path: str):
    """由 .ass 路径推出同名的视频文件(生成时两者同目录同名)。"""
    stem = os.path.splitext(ass_path)[0]
    for ext in (".flv", ".mp4"):
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def _ass_secs(ts: str) -> float:
    """'0:00:01.00' → 1.0;解析失败返回 +inf(排到最后,不丢行)。"""
    try:
        h, m, s = str(ts).split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return float("inf")


def _sort_dialogues(lines):
    """把 Dialogue 行按开始时刻稳定排序。

    礼物与弹幕来自两条消息通道,原先各自成块输出(礼物块整体排在弹幕块
    之前),与真实时间轴不同序。多数播放器无所谓,但部分实现(硬件播放器
    固件、ASS→SRT 转换工具)假定事件时间单调,会整块错位。层级仍由 Layer
    字段决定,事件语义不变,只调整行序。
    """
    def key(line):
        parts = line.split(",", 2)
        return _ass_secs(parts[1]) if len(parts) > 2 else float("inf")
    return sorted(lines, key=key)


def generate_ass_for_segment(path_out, events, anchor_ms, offset_sec, cfg,
                             end_ms=None, gift_extra=0.0, video_path=None,
                             gift_items=None, session_start_ms=None):
    """为一个分片生成 .ass。events 为该时间区间内的弹幕事件列表。

    gift_extra:礼物事件额外偏移(秒),叠加在 offset_sec 之上(弹幕不动)。
    只影响事件时刻,不影响分片时长换算(end_ms 仍按原始 ts 过滤)。
    video_path:该片视频文件;默认由 path_out 推断同目录同名视频,
    用它的真实尺寸作为 PlayRes 画布(读不到则回退版式基准)。

    gift_items:全场礼物计划里属于本分片的展示项(见 gift_display)。给了就用
    新版竖屏礼物编排(8 槽位、快慢车道、连击分段);给 None 则走旧的单横幅路径。
    session_start_ms:整个会话第一个分片的锚点(原始 epoch ms),用于把计划的
    会话相对毫秒还原成绝对时间。
    """
    dm_cfg = cfg.get("danmaku") or {}
    font_size = int(dm_cfg.get("font_size", 44))
    show_member = bool(dm_cfg.get("capture_member", False))
    canvas = Canvas.for_video(video_path or _sibling_video(path_out))
    style = str(dm_cfg.get("style", "queue") or "queue").lower()
    # 新版礼物编排只在 queue 样式下生效；scroll 保留原状(一次不改两套样式)
    layout = gift_layout_portrait(canvas, cfg) if (gift_items is not None and style == "queue") else None
    use_plan = layout is not None
    lines = [_ass_header(font_size, canvas, layout if use_plan else None)]

    filtered = []
    for ev in events:
        if ev.get("type") == "member" and not show_member:
            continue
        t = ((ev.get("ts") or 0) - anchor_ms) / 1000.0 + offset_sec
        if ev.get("type") == "gift":
            t += gift_extra
        if t < 0:
            continue  # 事件早于视频起点(录制启动前),丢弃
        if end_ms is not None and (ev.get("ts") or 0) > end_ms + GRACE_AFTER_END * 1000:
            continue
        filtered.append((ev, t))

    if style == "scroll":
        body = _scroll_lines(filtered, font_size, end_ms, anchor_ms, offset_sec, canvas)
    elif use_plan:
        # 礼物交给展示计划渲染，队列只画弹幕/进场，避免同一笔礼物画两次
        chat_only = [(ev, t) for ev, t in filtered if ev.get("type") != "gift"]
        body = _queue_lines(chat_only, font_size, end_ms, anchor_ms, offset_sec,
                            dm_cfg, canvas)
        end_sec = None if end_ms is None else (end_ms - anchor_ms) / 1000.0 + offset_sec
        body += gift_dialogues(gift_items, layout, anchor_ms, offset_sec, gift_extra,
                               int(session_start_ms or anchor_ms), end_sec)
    else:
        body = _queue_lines(filtered, font_size, end_ms, anchor_ms, offset_sec,
                            dm_cfg, canvas)
    lines += _sort_dialogues(body)

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


def gift_extra_offset(align: dict, cfg, idx) -> float:
    """礼物**额外**偏移(叠加在弹幕偏移之上,默认 0 即与弹幕完全一致)。

    为什么礼物要单独一格:礼物走的是与弹幕不同的消息通道,抖音的轻礼物
    (WebcastLightGiftMessage)是连击聚合消息,同一串连击的时间戳可能整体
    滞后或提前,即使弹幕已经对齐,礼物横幅仍可能偏。两者共用一个偏移时
    只能取折中,分开后各自可调。

    优先级与弹幕一致:人工本场 > 配置默认;分片级再叠加。
    """
    manual = align.get("gift_global_offset")
    if not isinstance(manual, (int, float)):
        manual = float((cfg.get("danmaku") or {}).get("gift_offset_seconds", 0) or 0)
    extra = (align.get("gift_per_segment") or {}).get(idx if idx is not None else "_", 0) or 0
    return float(manual) + float(extra)


def effective_gift_offset(align: dict, cfg, idx) -> float:
    """礼物事件在字幕时间轴上的总偏移 = 弹幕偏移 + 礼物额外偏移。"""
    return effective_offset(align, cfg, idx) + gift_extra_offset(align, cfg, idx)


def giftdisplay_path_for(output_path: str) -> str:
    """礼物展示计划的产物路径：与 danmaku.jsonl 同目录，基名 + .giftdisplay.json。"""
    jp = jsonl_for(output_path)
    suf = ".danmaku.jsonl"
    stem = jp[:-len(suf)] if jp.endswith(suf) else os.path.splitext(jp)[0]
    return stem + ".giftdisplay.json"


def _write_giftdisplay(path: str, plan: dict, seg_meta, canvases, finalized=False):
    """把展示计划落成可选产物（下游照抄即可，不必重写规则）。"""
    import json as _json
    out = {
        "rule_version": plan.get("rule_version"),
        "layout_profile": plan.get("layout_profile"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finalized": bool(finalized),
        "cfg": plan.get("cfg") or {},
        "segments": [],
        "tail_overflow": plan.get("tail_overflow") or [],
        "stats": plan.get("stats") or {},
    }
    for s in plan.get("segments") or []:
        idx = s.get("idx")
        meta = next((m for m in seg_meta if m["idx"] == idx), {})
        lay = canvases.get(idx) or {}
        out["segments"].append({
            "seg": idx,
            "canvas": [lay.get("canvas_w"), lay.get("canvas_h")],
            "scale": round(float(lay.get("scale") or 0), 6),
            "slot_y": lay.get("slot_y") or [],
            "seg_start_ms": meta.get("start_ms"),
            "seg_end_ms": meta.get("end_ms"),
            "items": s.get("items") or [],
        })
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    except OSError:
        pass
    return out


def plan_gifts_for_session(output_path, segs, events, cfg, align):
    """全场礼物规划（只做一次）。返回 (plan, items_by_seg, seg_meta, layout_by_seg)。

    跨分片的等待队列与槽位状态必须由同一状态携带，否则会在片边界重复或丢失
    ——所以这里一次规划完，再按 `seg` 分发给各分片渲染。
    """
    dm_cfg = cfg.get("danmaku") or {}
    if not gift_display_mod.enabled(cfg) or str(dm_cfg.get("style", "queue")).lower() != "queue":
        return None, {}, [], {}
    gd = None
    try:
        gd = gift_dict_mod.load()          # 只读本地字典，不在生成链路里联网
    except Exception:
        gd = None
    items = gift_display_mod.build_display_items(events, cfg, gd)
    session_start = int(segs[0][2])
    # 事件是绝对 epoch ms，分片边界是"距会话起点"→ 先换算成同一口径
    items = gift_display_mod.shift_items(items, session_start)
    off_ms = clock_offset(align) or 0
    seg_meta = []
    for i, (idx, path, anchor) in enumerate(segs):
        nxt = segs[i + 1][2] if i + 1 < len(segs) else None
        if nxt is not None:
            end_raw = int(nxt)
        else:
            last = flv_last_ts_abs(path, off_ms)
            end_raw = int(last) if last else int(anchor) + 3600_000
        seg_meta.append({"idx": idx, "start_ms": int(anchor) - session_start,
                         "end_ms": max(0, end_raw - session_start)})
    plan = gift_display_mod.plan_session_display(items, seg_meta, cfg)
    items_by_seg = {s["idx"]: (s.get("items") or []) for s in plan.get("segments") or []}
    layout_by_seg = {}
    for idx, path, anchor in segs:
        canvas = Canvas.for_video(path)
        lay = gift_layout_portrait(canvas, cfg)
        if lay:
            layout_by_seg[idx] = lay
    return plan, items_by_seg, seg_meta, layout_by_seg


def generate_session(output_path: str, cfg, align=None, drift_ms=0.0, finalized=False):
    """为一次录制会话(输出路径/模式)生成全部分片的 .ass。
    返回生成的分片信息列表 [{idx, file, anchor, offset}]。

    礼物改为**全场一次规划**再分发到各分片（见 plan_gifts_for_session）；
    弹幕仍按原有逐片逻辑生成。`finalized=True` 时额外把展示计划标成终版。
    """
    if align is None:
        align = load_align(output_path)
    segs = anchor_segments(output_path, align, drift_ms)
    if not segs:
        return []
    events = load_events(jsonl_for(output_path))
    plan, items_by_seg, seg_meta, layout_by_seg = plan_gifts_for_session(
        output_path, segs, events, cfg, align)
    session_start = int(segs[0][2]) if segs else 0
    info = []
    for i, (idx, path, anchor) in enumerate(segs):
        next_anchor = segs[i + 1][2] if i + 1 < len(segs) else None
        # 本分片时间区间 [anchor, next_anchor);末片吃到剩余全部
        evs = [e for e in events
               if e.get("ts", 0) >= anchor and (next_anchor is None or e.get("ts", 0) < next_anchor)]
        last = flv_last_ts_abs(path, clock_offset(align))
        end_ms = int(last - drift_ms) if last is not None else None
        off = effective_offset(align, cfg, idx)
        g_extra = gift_extra_offset(align, cfg, idx)
        generate_ass_for_segment(ass_path_for_segment(path), evs, anchor, off, cfg,
                                 end_ms, gift_extra=g_extra, video_path=path,
                                 gift_items=items_by_seg.get(idx) if plan else None,
                                 session_start_ms=session_start)
        info.append({"idx": idx, "file": path, "anchor": anchor, "offset": off,
                     "gift_extra": g_extra, "gift_offset": off + g_extra})
    if plan:
        _write_giftdisplay(giftdisplay_path_for(output_path), plan, seg_meta,
                           layout_by_seg, finalized=finalized)
        try:
            _log_gift_plan(output_path, plan)
        except Exception:
            pass
    dm_cfg = cfg.get("danmaku") or {}
    align.setdefault("config_offset", float(dm_cfg.get("offset_seconds", 0) or 0))
    align.setdefault("gift_config_offset", float(dm_cfg.get("gift_offset_seconds", 0) or 0))
    save_align(output_path, align)
    return info


def _log_gift_plan(output_path, plan):
    """把礼物计划摘要写进业务日志（每场生成都会有一条，便于回溯）。"""
    try:
        import monitor as _mon          # 延迟导入：subtitle 被 monitor 导入，避免环
        log = getattr(_mon, "log", None)
    except Exception:
        log = None
    msg = f"[礼物展示] {os.path.basename(output_path)} " + gift_display_mod.plan_summary_text(plan)
    if callable(log):
        log(msg)
    else:
        print(msg, flush=True)


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
        last = flv_last_ts_abs(path, clock_offset(align))
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
    info = generate_session(output_path, cfg, align, drift_ms, finalized=True)
    align["summary"] = summarize(jsonl_for(output_path))
    save_align(output_path, align)
    return info
