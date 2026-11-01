#!/usr/bin/env python3
"""录制质量调度 —— 分辨率档位 / 收益判定 / 自动降级状态机(**纯逻辑,零 IO**)。

为什么单独一个模块
------------------
「分辨率档位」与「自动降级」共用同一套换算:档位 → 目标像素 → 引擎需求,
以及 (分辨率, 码率) 二元组的收益比较。若参数生成(`monitor._build_ffmpeg_args`)
与降级判定(`monitor._quality_loop`)各写一份,**两处必然漂移** —— 一处改了档位表,
另一处还按旧像素算收益,降级就会「以为省了引擎其实没省」。所以换算只在这里实现
一次,monitor 只负责注入上下文(配置 / 指标 / 源尺寸)。

本模块**不 import monitor**(monitor 要 import 本模块,反向依赖成环),也不做任何
文件/网络 IO —— 状态持久化由调用方给路径,便于单测注入时钟与临时目录。

三条实测事实(2026-10-22 本机 M4 + ffmpeg 7.1,详见
《视频录制改造计划书-分辨率档位-自动降级-强制硬件编码.md》)
------------------------------------------------------------------------
1. **体积只由码率决定**:同码率下四个档位的 30 s 体积完全一致(4.33 MB / 30 s)。
   「降分辨率省体积」是 q 值模型下的错觉 —— q 固定时码率随像素自动下降,
   换成直配码率后这个因果关系不存在了。想省体积只能降码率。
2. **引擎占用由像素率决定**:7 路原生 322 MP/s = 71.5%、864 档 45.4%、
   720P 档 31.5%(按 ENGINE_MP_S=450 规划)。想省引擎只能降分辨率。
3. **码率越低,降分辨率的观感代价越小**:1200k 时 720P 比原生低 3.43 分(VMAF),
   500k 时只低 0.68 分 —— 这正是 L1/L2 采用「分辨率 + 码率双降」的实测依据:
   两件事一起做时,分辨率那部分损失几乎被码率下降的编码增益抵消掉了。
"""
import json
import os
import time

# ── 档位表(唯一真源) ────────────────────────────────────────────────────
# 档位值 = 目标**短边**像素数;0 是显式的一档「原生 = 完全不缩放」,
# 不能与 1080 合并:「不缩放」与「缩到 1080P」是两个概念,源高于 1080P 时结果不同。
TIERS = {0: "原生", 1080: "1080P", 864: "高清", 720: "720P"}
TIER_VALUES = (0, 1080, 864, 720)
# 竖屏标准 9:16 尺寸(短边, 长边);横屏时对调。取 864 而非 870 的理由:
# 864 = 1080 × 0.8(与 1080P 精确 0.8 倍)、= 16 × 54(双 16 对齐,VideoToolbox
# 无需补边),实测同码率画质还略优于 870(+0.16 VMAF)。
SHORT_TO_STD = {1080: (1080, 1920), 864: (864, 1536), 720: (720, 1280)}

# 引擎吞吐规划值(MP/s):实测 460–700,规划取保守值;首月用观测回标。
ENGINE_MP_S = 450.0
# 降级最小收益门槛:两个维度都不到 10% 就不值得付一次重启丢帧的代价。
MIN_GAIN = 0.10
# 体积反向守卫:降级会让体积变大时,只有引擎收益 ≥ 25% 且磁盘有余量才放行。
ENGINE_GAIN_FOR_SIZE_RISE = 0.25
# 源尺寸未知时的兜底像素(竖屏 1080×1920;横屏像素数与之相同,故不分方向)。
ASSUMED_SRC = (1080, 1920)

# ── 缩放后端:GPU `scale_vt` 已被实测否决(2026-10-29) ──────────────────────
# ffmpeg 7.1 的 `scale_vt` **撑不住滤镜图重配置**。直播流只要在运行中改变帧
# 参数(分辨率、位深,哪怕只跳一帧又变回去),ffmpeg 就要重建滤镜图;重建时
# `scale_vt` 输出的 `videotoolbox_vld` 与自动插入的 `scale` 协商失败:
#
#   Impossible to convert between the formats supported by the filter
#   'Parsed_scale_vt_0' and the filter 'auto_scale_0'
#   Error reinitializing filters! → error code -78 (Function not implemented)
#
# vf 线程随即终止,整个 ffmpeg 退出。8 天 86 次崩溃**全是这一条**,且与并发
# 路数、画幅、分辨率档位无关 —— 只取决于源流稳不稳,所以集中在少数几个主播。
# 上游自 2023-07 引入 scale_vt 至今未修(2026-04 的最新提交只补了个错误码),
# 升级 ffmpeg 解决不了。
#
# 所以缩放一律走 CPU `scale`:硬解仍然保留(`-hwaccel videotoolbox`),帧由
# 解码器下载到内存,代价实测 0.040 → 0.213 核/路(+0.17,见崩溃归因报告)。
# 改成 True 才会回到 GPU 缩放 —— 改之前先看
# `tests/test_vt_args.py::TestScaleVtDisabled`(它会把这条结论钉死)。
SCALE_VT_ENABLED = False


# ── 崩溃原因分类 ────────────────────────────────────────────────────────
# 崩溃信号的用途是给「并发过多 / 引擎不够」做旁证,但 ffmpeg 退出有一大半
# 与本地负载无关:抖音 CDN 的 flv 地址带签名、有时效,主播侧切码率或重连之后
# 旧地址直接 404,ffmpeg 一起来就退,看护每 20 s 拉起一次。
#
# 2026-10-27 实测(事故复盘):当天 19 次退出**全部**是 `404 Not Found`,没有
# 一条编码器/内存报错,而全程 CPU < 25%、引擎 < 25%(阈值 92%);却被判成
# 「并发过多」一次降到底,又因为「崩溃后 30 min 内不恢复」被这类高频重启
# 无限推后,卡在 720P 达 2 小时 20 分。
#
# 所以先分类:只有本地侧(resource)参与降级判定,流侧(external)只记账不判。
CRASH_RESOURCE = "resource"      # 本地:编码器/显存/内存/线程/硬件初始化
CRASH_EXTERNAL = "external"      # 外部:地址失效、连不上、流被切断
CRASH_UNKNOWN = "unknown"        # 信息不足(stderr 为空、被信号终止)

CRASH_CAUSES = (CRASH_RESOURCE, CRASH_EXTERNAL, CRASH_UNKNOWN)

# 流侧特征:命中即判 external —— 这些串只在「拿不到流」时出现。
_EXTERNAL_MARKS = (
    "error opening input",              # 后面紧跟具体 HTTP 状态
    "server returned 4",                # 401/403/404:签名失效或本场已结束
    "server returned 5",
    "http error",
    "connection refused",
    "connection timed out",
    "connection reset",
    "no route to host",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "invalid data found when processing input",
    "end of file",
    "input/output error",
)

# 本地侧特征。顺序必须「先流侧后本地侧」:一条 404 后面常跟着解码器初始化
# 日志(含 videotoolbox 字样),先查流侧才是正确的归因方向。
_RESOURCE_MARKS = (
    "error while opening encoder",
    "error initializing",
    "cannot load ",
    "videotoolbox",
    "no such device",
    "device not configured",
    "cannot allocate memory",
    "out of memory",
    "resource temporarily unavailable",
    "too many open files",
    "cannot create thread",
    "conversion failed",
    "filter graph",
)


def classify_exit(text):
    """把 ffmpeg stderr 尾部归成 `resource` / `external` / `unknown`。

    **空文本一律 unknown,不是 resource**:stderr 为空的退出多半是被信号终止
    或正常收尾,没有任何证据指向本地负载。把空文本算进资源类,会让「两路同时
    被切流」又一次触发降档 —— 这正是本次要修的那个误判。
    """
    s = (text or "").lower()
    if not s.strip():
        return CRASH_UNKNOWN
    for mark in _EXTERNAL_MARKS:
        if mark in s:
            return CRASH_EXTERNAL
    for mark in _RESOURCE_MARKS:
        if mark in s:
            return CRASH_RESOURCE
    return CRASH_UNKNOWN


def crash_cause_text(cause):
    """分类 → 一句人话(业务日志、诊断块、前端徽标共用)。"""
    return {CRASH_RESOURCE: "本地资源",
            CRASH_EXTERNAL: "流侧故障",
            CRASH_UNKNOWN: "原因未知"}.get(cause, "原因未知")


def _restore_crash(row):
    """状态文件里的一条崩溃事件 → (ts, kind, cause)。

    v2 的二元组 `[ts, kind]` 没有 cause,按 **resource** 解读 —— 保守方向:
    升级后不会因为「读不出原因」而把刚崩过的机器当成没崩过、立刻恢复。
    """
    ts = float(row[0])
    kind = str(row[1])
    cause = CRASH_RESOURCE
    if len(row) > 2:
        c = str(row[2] or "").strip().lower()
        if c in CRASH_CAUSES:
            cause = c
    return (ts, kind, cause)


# 状态文件(相对项目根)。含 boot_id,崩溃重启后据此识别「刚崩过」。
STATUS_FILE = os.path.join("state", "quality_state.json")

# 状态机状态码 → 一句人话(前端徽标与日志共用同一句,避免两处措辞不一致)
STATE_TEXT = {
    "ARMED": "自动降级已启用",
    "PARTIAL": "仅 L2 档生效(L1 不轻于当前配置)",
    "DISABLED_OFF": "自动降级被手动关闭",
    "DISABLED_NOCODEC": "当前为流复制(copy)模式,没有编码参数可降",
    "DISABLED_DOMINATED": "自动降级已自动失效:当前配置已不高于降级档位,降级无收益",
}


# ── 档位换算 ────────────────────────────────────────────────────────────
def norm_tier(value):
    """把配置里的档位值归一成 int;非法值返回 None(**绝不透传**)。

    透传非法值的后果不是「参数不起作用」,而是 `-vf` 参数错误 → ffmpeg
    启动即退出 → 表现成「检测到开播却录不到文件」,极难往档位上想。
    空值与 None 视作 0(原生),前端下拉的 value 是字符串("864"),故先 str 再 int。
    """
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    try:
        f = float(s)                 # 容忍 "864.0"(JSON 里偶尔会是浮点)
    except (TypeError, ValueError):
        return None
    if f != int(f):                  # 864.5 这种半档值一律拒绝,不做截断
        return None
    v = int(f)
    return v if v in TIER_VALUES else None


def tier_size(tier, src_w=None, src_h=None):
    """该档位实际编码的 (w, h);None 表示不缩放。

    规则:只缩不放;短边对齐档位;按横/竖方向套标准 9:16 尺寸;宽高取偶。
    **只缩不放**是硬要求 —— 720×1280 的主播选 1080P 时放大既费引擎又更糊。
    """
    t = norm_tier(tier)
    if not t:
        return None
    try:
        w0, h0 = int(src_w), int(src_h)
    except (TypeError, ValueError):
        return None                  # 源尺寸未知 → 由 scale_expr 的表达式兜底只缩不放
    if w0 <= 0 or h0 <= 0:
        return None
    portrait = h0 >= w0
    if min(w0, h0) <= t:
        return None                  # 源本身就比档位小 → 不放大
    w, h = SHORT_TO_STD[t]
    if not portrait:
        w, h = h, w
    return (w // 2 * 2, h // 2 * 2)


def effective_size(tier, src_w=None, src_h=None):
    """该档位实际产出尺寸(估算):真实源尺寸优先,未知时用标准/兜底值。

    与 `scale_expr` 的关系:`scale_expr` 是交给 ffmpeg 的**执行**口径(表达式),
    这里是给界面与引擎估算用的**数值**口径;两者语义一致(只缩不放、横竖选向),
    所以引擎需求不会与实际产物脱节。
    """
    size = tier_size(tier, src_w, src_h)
    if size:
        return size
    try:
        w0, h0 = int(src_w), int(src_h)
        if w0 > 0 and h0 > 0:
            return (w0, h0)
    except (TypeError, ValueError):
        pass
    t = norm_tier(tier)
    if t:
        return SHORT_TO_STD[t]
    return ASSUMED_SRC


def pixels(tier, src_w=None, src_h=None):
    """该档位实际编码的像素数 —— 引擎需求(**唯一**)数据源。

    参数生成与降级收益必须同源:这里算出来的像素数就是 ffmpeg 真正要编的量,
    否则降级逻辑会与实际产物脱节(以为降了,其实没降)。
    """
    w, h = effective_size(tier, src_w, src_h)
    return w * h


def scale_expr(tier):
    """缩放滤镜(`scale`)的 w/h 表达式;原生档返回 None。

    这里**故意用表达式而不是写死数字**(2026-10-22 实测 ffmpeg 7.1 的 `scale`
    与 `scale_vt` 的 w/h 都接受表达式,语法同款):

      w = if(竖屏, min(iw, 短边档位), min(iw, 长边标准))
      h = if(竖屏, min(ih, 长边标准), min(ih, 短边档位))

    两个好处:①「只缩不放」由滤镜自身保证,不依赖启动前探测源尺寸(开播瞬间
    往往还不知道源分辨率);② 横竖屏自动选向,主播临时横屏直播也不会把画面拉变形。
    """
    t = norm_tier(tier)
    if not t:
        return None
    short, long_ = SHORT_TO_STD[t]               # 竖屏:(短边, 长边)
    return ("w='if(gte(iw,ih),min(iw,{L}),min(iw,{S}))':"
            "h='if(gte(iw,ih),min(ih,{S}),min(ih,{L}))'").format(S=short, L=long_)


def vf_filter(tier, hw=True):
    """缩放滤镜串;原生档返回 None。

    `hw=True` 只表示「这一路本来可以走 GPU 缩放」(即启用了硬解),**是否真的**
    用 `scale_vt` 由 `SCALE_VT_ENABLED` 决定,默认 False → 一律 CPU `scale`。
    原因见 `SCALE_VT_ENABLED` 上方:`scale_vt` 撑不住滤镜图重配置,会把进程打死。

    ⚠️ 若将来真的启用 scale_vt,`-hwaccel_output_format videotoolbox_vld` 必须
    同步加回(见 `monitor._build_ffmpeg_args`),否则 scale_vt 收到普通 yuv 帧
    直接 `Conversion failed`(实测)。
    """
    expr = scale_expr(tier)
    if not expr:
        return None
    return ("scale_vt=" if (hw and SCALE_VT_ENABLED) else "scale=") + expr


def tier_fps_pixels(tier, src_w, src_h, fps):
    """(像素数 × 帧率) 的 Mbps 等效量,供 demand_pct 累加。"""
    try:
        f = float(fps) or 0.0
    except (TypeError, ValueError):
        f = 0.0
    return pixels(tier, src_w, src_h) * max(f, 0.0)


def demand_pct(streams, engine_mp_s=ENGINE_MP_S):
    """需求侧引擎占用率(%):Σ(宽 × 高 × fps) ÷ 引擎吞吐。

    `streams` 是 [(w, h, fps), ...]。VideoToolbox 没有公开的利用率 API
    (`powermetrics` 需要 root),所以只能按像素率折算,再用跟随率校正
    (见 `engine_util`)。
    """
    mp = 0.0
    for w, h, fps in streams:
        try:
            mp += float(w) * float(h) * float(fps) / 1e6
        except (TypeError, ValueError):
            continue
    return round(mp / max(float(engine_mp_s), 1e-6) * 100, 1)


def engine_util(demand, follow_rate):
    """引擎占用率估算 = 需求 ÷ 跟随率(跟随率下限 0.5)。

    跟随率掉到 0.7 说明引擎只喂到需求的 70%,此时「占用」就是 100% —— 这个
    反推比单纯看需求侧更接近真相:需求 45% 但跟在 0.5 时,引擎其实已经满了。
    """
    try:
        f = float(follow_rate)
    except (TypeError, ValueError):
        f = 1.0
    if f <= 0:
        f = 0.5
    return round(float(demand) / max(f, 0.5), 1)


# ── 收益与有效性判定 ────────────────────────────────────────────────────
def _num(value, default=0.0):
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def gains(u_res, u_br, d_res, d_br, src_w=None, src_h=None):
    """返回 (gain_engine, gain_size),正数表示有收益。

    · gain_engine:降分辨率才有(实测原生 → 720P = +55.9%)
    · gain_size  :降码率才有(实测 1200 → 700 = +41.7%、1200 → 500 = +58.3%)
    """
    p_u = pixels(u_res, src_w, src_h)
    p_d = pixels(d_res, src_w, src_h)
    g_engine = (1.0 - p_d / p_u) if p_u else 0.0
    ub, db = _num(u_br), _num(d_br)
    g_size = (1.0 - db / ub) if ub else 0.0
    return round(g_engine, 4), round(g_size, 4)


def usable(u_res, u_br, d_res, d_br, src_w=None, src_h=None, disk_ok=True,
           min_gain=MIN_GAIN):
    """该降级档此刻是否可用(第 2、3 层合并)。

    第 2 层:两个维度收益都不足(`min_gain`)→ 不可用。用户指定的边界
            「分辨率 720P 且码率 ≤ L2」在这里必然被判死,与阈值取值无关
            (像素相同 → 引擎 0;码率不低于 → 体积 ≤ 0)。
    第 3 层:体积会涨(gain_size < 0)时,只有引擎收益 ≥ 25% 且磁盘有余量才放行,
            防止「为省引擎把体积放大 2~3 倍」把归档盘打满。
    """
    ge, gs = gains(u_res, u_br, d_res, d_br, src_w, src_h)
    if not (ge >= min_gain or gs >= min_gain):
        return False
    if gs < 0:
        return bool(ge >= ENGINE_GAIN_FOR_SIZE_RISE and disk_ok)
    return True


def _level_block(cfg, prefix, u_res, u_br, src_w, src_h, disk_ok, min_gain):
    res = norm_tier(cfg.get(prefix + "_resolution"))
    br = _num(cfg.get(prefix + "_bitrate"))
    ge, gs = gains(u_res, u_br, res, br, src_w, src_h) if res is not None else (0.0, 0.0)
    ok = res is not None and usable(u_res, u_br, res, br, src_w, src_h,
                                    disk_ok, min_gain)
    return {
        "resolution": res if res is not None else 0,
        "bitrate": int(br) if float(br).is_integer() else br,
        "gain_engine": ge,
        "gain_size": gs,
        "valid": bool(ok),
    }


def _fmt_tier(tier):
    t = norm_tier(tier)
    return TIERS.get(t or 0, "原生")


def degrade_plan(cfg, src_w=None, src_h=None, disk_ok=True):
    """把配置算成一份「降级可行性快照」—— 保存校验与每轮采样共用。

    两处共用是刻意的:保存时说合法、运行时说不可用(或反过来)会让用户完全
    无从理解;共用同一函数就不会出现两套口径。
    """
    cfg = cfg or {}
    rec = cfg.get("recorder") if isinstance(cfg.get("recorder"), dict) else cfg
    u_res = norm_tier(rec.get("resolution"))
    u_br = _num(rec.get("bitrate"))
    mode = str(rec.get("video_mode") or "vt").strip().lower()
    auto = rec.get("auto_degrade", True) is not False
    # 配置里 min_gain 用**百分数**(10 = 10%,前端就是一个数字输入框),
    # 内部一律用比例,免得「10 还是 0.1」在几处各解释一遍。
    min_gain = _num(rec.get("min_gain"), MIN_GAIN * 100) / 100.0
    min_gain = min(max(min_gain, 0.0), 0.5)
    l1 = _level_block(rec, "degrade_l1", u_res, u_br, src_w, src_h, disk_ok, min_gain)
    l2 = _level_block(rec, "degrade_l2", u_res, u_br, src_w, src_h, disk_ok, min_gain)

    if not auto:
        state, max_level = "DISABLED_OFF", 0
    elif mode != "vt" or not u_br:
        state, max_level = "DISABLED_NOCODEC", 0
    elif not l1["valid"] and not l2["valid"]:
        state, max_level = "DISABLED_DOMINATED", 0
    elif not l1["valid"]:
        state, max_level = "PARTIAL", (2 if l2["valid"] else 0)
    else:
        state, max_level = "ARMED", (2 if l2["valid"] else 1)

    plan = {
        "user": {"resolution": u_res if u_res is not None else 0,
                 "bitrate": int(u_br) if float(u_br).is_integer() else u_br,
                 "video_mode": mode},
        "l1": l1, "l2": l2,
        "state": state, "max_level": max_level,
        "min_gain": round(min_gain, 3),
        "engine_mp_s": ENGINE_MP_S,
    }
    plan["reason"] = reason_text(plan)
    return plan


def level_tier(cfg, level):
    """第 level 档对应的 (resolution, bitrate, label);0 档 = 用户主配置。"""
    rec = (cfg or {}).get("recorder") if isinstance((cfg or {}).get("recorder"), dict) else (cfg or {})
    if not level:
        return {"resolution": norm_tier(rec.get("resolution")) or 0,
                "bitrate": _num(rec.get("bitrate")), "level": 0, "label": "L0"}
    pre = "degrade_l%d" % (2 if level >= 2 else 1)
    return {"resolution": norm_tier(rec.get(pre + "_resolution")) or 0,
            "bitrate": _num(rec.get(pre + "_bitrate")),
            "level": 2 if level >= 2 else 1,
            "label": "L%d" % (2 if level >= 2 else 1)}


def reason_text(plan):
    """状态 → 一句人话(前端徽标、日志、接口共用)。"""
    state = plan.get("state")
    l1, l2 = plan.get("l1") or {}, plan.get("l2") or {}
    if state == "ARMED":
        return ("自动降级已启用:L1 引擎 −%.0f%% / 体积 −%.0f%%;"
                "L2 引擎 −%.0f%% / 体积 −%.0f%%"
                % (l1.get("gain_engine", 0) * 100, l1.get("gain_size", 0) * 100,
                   l2.get("gain_engine", 0) * 100, l2.get("gain_size", 0) * 100))
    if state == "PARTIAL":
        return ("L1 不轻于当前配置(引擎 %+.0f%%、体积 %+.0f%%,双维反向),已跳过;仅 L2 生效"
                % (l1.get("gain_engine", 0) * 100, l1.get("gain_size", 0) * 100))
    if state == "DISABLED_DOMINATED":
        u = plan.get("user") or {}
        return ("自动降级已自动失效:当前 %s / %s kbps 已不高于 L2 档位"
                "(%s / %s kbps),降级无收益"
                % (_fmt_tier(u.get("resolution")), fmt_int(u.get("bitrate")),
                   _fmt_tier(l2.get("resolution")), fmt_int(l2.get("bitrate"))))
    if state == "DISABLED_NOCODEC":
        return ("当前为流复制(copy)模式,没有编码参数可降,自动降级不适用")
    return STATE_TEXT.get(state, "")


def fmt_int(v):
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(v)


def validate_levels(cfg):
    """保存时(第 1 层)的反支配校验:返回错误串;通过返回 None。

    拒绝条件:`P(L2) ≥ P(L1) 且 B(L2) ≥ B(L1)` —— L2 被 L1 双维支配,
    「降两级」等于原地踏步,配置本身没意义。
    **只要求 L2 至少在一个维度严格更轻**,因此允许交叉组合:
    L1 只降码率 + L2 只降分辨率(如 L1=864/900k → L2=720P/500k)是合法配置。
    允许两档相对 L0 更重:运行时按第 2 层判无效并跳过 —— 否则用户改主配置
    (比如把码率调到 400k)就会让「保存」突然报错,而他并没动降级档。
    """
    rec = (cfg or {}).get("recorder") if isinstance((cfg or {}).get("recorder"), dict) else (cfg or {})
    # 四个键缺任何一个都不判:老配置/局部提交(patch 只带 format)本来就没有降级档,
    # 拿「全都是 0」去比对必然得出「L2 不轻于 L1」的假错误,把正常保存挡死。
    keys = ("degrade_l1_resolution", "degrade_l1_bitrate",
            "degrade_l2_resolution", "degrade_l2_bitrate")
    if not all(k in rec and rec[k] not in (None, "") for k in keys):
        return None
    l1r, l2r = norm_tier(rec.get("degrade_l1_resolution")), norm_tier(rec.get("degrade_l2_resolution"))
    l1b, l2b = _num(rec.get("degrade_l1_bitrate")), _num(rec.get("degrade_l2_bitrate"))
    if l1r is None or l2r is None:
        return None                       # 白名单校验在别处负责,这里只管单调性
    p1 = pixels(l1r) if l1r else ASSUMED_SRC[0] * ASSUMED_SRC[1]
    p2 = pixels(l2r) if l2r else ASSUMED_SRC[0] * ASSUMED_SRC[1]
    if p2 >= p1 and l2b >= l1b:
        return ("recorder: 降级 L2(%s / %s kbps)不轻于 L1(%s / %s kbps),"
                "降两级等于原地踏步 —— L2 至少要在分辨率或码率上比 L1 更低"
                % (_fmt_tier(l2r), fmt_int(l2b), _fmt_tier(l1r), fmt_int(l1b)))
    return None


def pick_target(plan, trigger):
    """触发时选目标档(第 4 层)。

    A/B(资源类,本该「升一级」):
      L1 通过但体积会涨(gain_size < 0),而 L2 通过且体积不涨
        → 选 L2(同样省引擎,还不多花体积;见计划书场景 7);
      否则 → 选候选中的最低档。
    C(崩溃):一次性降到底,不再试探。
    """
    l1_ok = bool((plan.get("l1") or {}).get("valid"))
    l2_ok = bool((plan.get("l2") or {}).get("valid"))
    if not l1_ok and not l2_ok:
        return 0
    if trigger == "crash":
        return 2 if l2_ok else (1 if l1_ok else 0)
    if l1_ok and l2_ok:
        if (plan["l1"]["gain_size"] < 0) and (plan["l2"]["gain_size"] >= 0):
            return 2
        return 1
    return 1 if l1_ok else 2


# ── 状态机 ──────────────────────────────────────────────────────────────
def _median(win):
    if not win:
        return 0.0
    s = sorted(win)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


class Governor:
    """自动降级状态机:**纯内存 + 可注入时钟**,不碰 IO(便于单测)。

    一轮 `observe()` 做四件事:采样入库 → 中位数滤波 → 触发/恢复判定 →
    抖动防护(最小间隔 / 指数退避 / 24 h 锁定)。切换与否只由「指标 + 配置 +
    有效性快照」决定,当前档位以外的历史不影响判定,所以状态文件丢了也只是
    回到 L0(宁可少降,不可错降)。

    防抖参数全部取自 recorder 段,缺键用默认值(见 monitor.DEFAULT_CONFIG):
      触发 92% / 恢复 72%(20 个百分点滞回)、窗口 12 次(60 s)、
      触发持续 60 s / 恢复持续 600 s、最小间隔 900 s、
      当日恢复上限 4 次、24 h 切换 > 6 次即锁定最低可用档。
    """

    WINDOW = 12          # 滑动窗长度(次)
    WINDOW_MIN = 10      # 至少积累这么多样本才允许判定(12 次里 ≥10 次的条件)
    CRASH_WINDOW = 300   # 崩溃事件回看窗口(秒)
    LOCK_SPAN = 86400    # 抖动锁定持续时间(需人工解锁)

    def __init__(self, cfg=None, now=None):
        self.cfg = dict(cfg or {})
        self.level = 0
        self.state = "ARMED"
        self.reason = ""
        self.last_reason = ""
        self.cpu_win = []
        self.eng_win = []
        self.over_since = 0.0
        self.under_since = 0.0
        self.last_change = 0.0
        self.switches = 0            # 当日降级次数(用于指数退避与抖动锁定)
        self.restores = 0            # 当日恢复次数(用于「一天最多恢复 N 次」)
        self.switch_ts = []
        self.day = ""
        self.unstable = False
        self.locked_until = 0.0
        self.last_crash = 0.0        # 最近一次**本地资源类**崩溃(挡住恢复的闸门)
        self.last_external = 0.0     # 最近一次流侧故障(只记账,不参与判定)
        self.crashes = []             # [(ts, kind, cause)]
        self.history = []
        self.metrics = {}
        self.plan = None
        self.active_tier = {}
        self.boot_id = ""
        self.started_at = float(now if now is not None else time.time())

    # -- 配置读取 --
    def set_config(self, cfg):
        rec = (cfg or {}).get("recorder") if isinstance((cfg or {}).get("recorder"), dict) else (cfg or {})
        self.cfg = dict(rec or {})

    def _n(self, key, default):
        return _num(self.cfg.get(key), default)

    def _thresholds(self):
        return {
            "cpu_hi": self._n("degrade_cpu", 92),
            "engine_hi": self._n("degrade_engine", 92),
            "recover": self._n("degrade_recover", 72),
            "hold": self._n("degrade_hold", 60),
            "recover_hold": self._n("degrade_recover_hold", 600),
            "min_gap": self._n("degrade_min_gap", 900),
            "max_restore": int(self._n("degrade_max_restore", 4)),
            "unlock_switches": int(self._n("degrade_max_switches", 6)),
        }

    # -- 事件登记 --
    def note_crash(self, kind, now=None, cause=None):
        """登记一次崩溃类事件(启动失败 / 主进程被拉起 / 看护连续重启)。

        `cause` 取 `CRASH_CAUSES`,由调用方按 ffmpeg stderr 分类后传入
        (见 `classify_exit`)。缺省按 **resource** 记:老的调用方不传时宁可
        多降一档,也不放过真正的编码器崩溃 —— 但已知会高频误报的那两个
        restart / start_fail 调用点必须显式传。

        `last_crash` 只被资源类刷新,这条是「恢复被无限推后」的根治点:
        `observe()` 的 `no_crash` 闸门读的就是它。
        """
        now = float(now if now is not None else time.time())
        c = str(cause or "").strip().lower()
        if c not in CRASH_CAUSES:
            c = CRASH_RESOURCE
        self.crashes.append((now, str(kind), c))
        if c == CRASH_RESOURCE:
            self.last_crash = now
        else:
            self.last_external = now
        cutoff = now - 86400
        self.crashes = [c0 for c0 in self.crashes if c0[0] >= cutoff]

    def _crash_counts(self, now):
        """回看窗口内各类崩溃事件数。

        **只有 resource 类参与判定**。崩溃信号本来是用来给「并发过多」做旁证
        的,而流侧故障(404/403/断流)与本地负载毫无关系,算进来就是纯误判。
        `ignored` 单独返回给面板和日志,用来回答「这次为什么没降档」。
        """
        cutoff = now - self.CRASH_WINDOW
        start_fail = boot = 0
        streams, ignored = set(), set()
        for ev in self.crashes:
            ts, kind = ev[0], ev[1]
            cause = ev[2] if len(ev) > 2 else CRASH_RESOURCE
            if ts < cutoff:
                continue
            name = kind.split(":", 1)[1] if ":" in kind else kind
            if cause != CRASH_RESOURCE:
                ignored.add(name)
                continue
            if kind.startswith("start_fail"):
                start_fail += 1
            elif kind.startswith("boot"):
                boot += 1
            elif kind.startswith("restart:"):
                streams.add(name)
        return {"start_fail": start_fail, "boot": boot, "streams": len(streams),
                "ignored": len(ignored)}

    def unlock(self, now=None):
        """人工解除抖动锁定(UI「解除锁定」按钮 / `POST /api/quality/unlock`)。

        解锁后**回到 L0**(用户主配置):锁定期间档位被压在最低档,解不解锁却还
        停在最低档等于没解。同时把 `last_change` 设为当前时刻,让下一次切换仍要
        等满最小间隔,避免「一解锁就立刻又降回去」。
        """
        now = float(now if now is not None else time.time())
        self.unstable = False
        self.locked_until = 0.0
        self.switch_ts = []
        self.switches = 0
        self.restores = 0
        self.level = 0
        self.over_since = self.under_since = 0.0
        self.last_change = now
        self.last_reason = "人工解除抖动锁定"
        self.history.append({"at": now, "from": 2, "to": 0,
                             "reason": "人工解除抖动锁定,回到 L0"})
        return self.snapshot(now)

    # -- 主循环 --
    def observe(self, now, plan, *, cpu=0.0, demand=0.0, engine=0.0,
                follow_min=1.0, streams=0, disk_ok=True):
        """采样一轮。返回 {"changed", "level", "state", "reason", "trigger"}。"""
        now = float(now)
        th = self._thresholds()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if self.day != today:
            self.day = today
            self.switches = 0
            self.restores = 0
            self.switch_ts = [t for t in self.switch_ts if now - t < 86400]

        self.plan = plan
        self.state = plan.get("state") or "ARMED"
        self.reason = plan.get("reason") or ""
        max_level = int(plan.get("max_level") or 0)
        self.metrics = {"cpu": round(float(cpu), 1), "engine": round(float(engine), 1),
                        "demand_pct": round(float(demand), 1),
                        "follow_min": round(float(follow_min), 3),
                        "streams": int(streams)}

        # ① 功能整体失效(关闭 / copy / 双档都被支配):不采样、不判定、不切换
        if self.state.startswith("DISABLED"):
            if self.level:
                self.history.append({"at": now, "from": self.level, "to": 0,
                                     "reason": self.state})
            self.level = 0
            self.over_since = self.under_since = 0.0
            return self._result(False, "disabled")

        self.cpu_win.append(float(cpu))
        self.eng_win.append(float(engine))
        del self.cpu_win[:-self.WINDOW]
        del self.eng_win[:-self.WINDOW]
        med_cpu = _median(self.cpu_win)
        med_eng = _median(self.eng_win)
        ready = len(self.cpu_win) >= self.WINDOW_MIN

        # ② 抖动锁定:机器确实扛不住时,自动恢复只会害人,必须人工解锁
        if self.unstable and now < self.locked_until:
            self.level = max_level
            return self._result(False, "locked")
        self.unstable = self.unstable and now < self.locked_until

        # ③ C 类(崩溃):**只认本地资源类**,一次性降到底,不再试探。
        #    流侧故障(404/403/断流)走 `ignored` 分支,只记账不判档 ——
        #    否则「主播重连导致两路 404」会被当成「并发过多」而降画质。
        crashes = self._crash_counts(now)
        crash_hit = (crashes["start_fail"] >= 2 or crashes["boot"] >= 1
                     or crashes["streams"] >= 2)
        if crash_hit and max_level > self.level:
            why = ("5 分钟内本地崩溃信号:启动失败 %d / 主进程被拉起 %d / "
                   "看护连续重启 %d 路"
                   % (crashes["start_fail"], crashes["boot"], crashes["streams"]))
            if crashes["ignored"]:
                why += "(另有 %d 路流侧故障,已忽略)" % crashes["ignored"]
            return self._switch(now, max_level, "crash", why, th)

        # ④ A/B 类(资源):越过上阈值并持续 hold 才降一级
        hot = med_cpu >= th["cpu_hi"] or med_eng >= th["engine_hi"]
        if hot and ready:
            if not self.over_since:
                self.over_since = now
        else:
            self.over_since = 0.0
        if ready and self.over_since and (now - self.over_since) >= th["hold"]:
            # 资源类触发是「再降一级」,**不是**「降到 pick_target 说的那一级」——
            # 否则档位卡在 L1 永远上不去 L2(实测踩过:pick_target 对同一份配置
            # 恒定返回 1,A/B 第二次触发时 target 不大于 level,直接空转)。
            # pick_target 只负责给出「起点应该跳到哪里」(场景 7 要跳过会涨体积的 L1)。
            target = min(max(self.level + 1, pick_target(plan, "resource")), max_level)
            if target > self.level:
                which = ("CPU 中位数 %.0f%% ≥ %.0f%%" % (med_cpu, th["cpu_hi"])
                         if med_cpu >= th["cpu_hi"] else
                         "引擎占用 %.0f%% ≥ %.0f%%" % (med_eng, th["engine_hi"]))
                res = self._switch(now, target, "resource",
                                   "%s,持续 %.0f s" % (which, now - self.over_since), th)
                if res["changed"]:
                    return res

        # ⑤ 恢复:双阈值滞回 + 更长的持续时间 + 当日次数上限
        cool = med_cpu <= th["recover"] and med_eng <= th["recover"]
        if cool and ready and self.level > 0:
            if not self.under_since:
                self.under_since = now
            else:
                long_enough = (now - self.under_since) >= th["recover_hold"]
                gap_ok = (now - self.last_change) >= th["min_gap"]
                # last_crash=0 表示「从没崩过」,不能拿它去减时间(否则时钟起点小
                # 的场景永远算成"刚崩过",恢复被无限期挡死)
                # 只认**本地资源类**崩溃:流侧 404 高频发生时若也算进来,静默期
                # 会被无限刷新,恢复永远等不到(2026-10-27 事故的直接原因)。
                no_crash = (not self.last_crash) or (now - self.last_crash) >= 1800
                # 配额算的是「恢复过几次」,不是「降过几次」—— 用 switches 会把
                # 一次降级就耗掉恢复额度,变成「只降不升」
                quota = self.restores < th["max_restore"]
                if long_enough and gap_ok and no_crash and quota:
                    return self._switch(now, self.level - 1, "recover",
                                        "CPU %.0f%% / 引擎 %.0f%% 已连续 %.0f s 低于 %.0f%%"
                                        % (med_cpu, med_eng, now - self.under_since,
                                           th["recover"]), th)
        else:
            self.under_since = 0.0
        return self._result(False, "hold")

    def _switch(self, now, target, trigger, why, th):
        gap = th["min_gap"]
        if trigger == "resource" and self.switches:
            # 指数退避:反复过载时越降越稳,不再每 15 min 试探一次
            gap = max(gap, min(900 * (2 ** (self.switches - 1)), 14400))
        if self.last_change and (now - self.last_change) < gap:
            return self._result(False, "gap")
        src = self.level
        self.level = int(target)
        self.last_change = now
        self.over_since = self.under_since = 0.0
        if trigger == "recover":
            self.restores += 1
        else:
            self.switches += 1
            self.switch_ts.append(now)
            recent = [t for t in self.switch_ts if now - t < 86400]
            self.switch_ts = recent
            if len(recent) > th["unlock_switches"]:
                self.unstable = True
                self.locked_until = now + self.LOCK_SPAN
                self.last_reason = ("24 h 内切换 %d 次,判定机器扛不住 → 锁定最低可用档"
                                    "(需人工解锁)" % len(recent))
                self.history.append({"at": now, "from": src, "to": self.level,
                                     "reason": self.last_reason})
                return self._result(True, "lock")
        self.last_reason = why
        self.history.append({"at": now, "from": src, "to": self.level, "reason": why})
        del self.history[:-30]
        return self._result(True, trigger)

    def _result(self, changed, trigger):
        return {"changed": bool(changed), "level": int(self.level),
                "state": self.state, "reason": self.reason,
                "trigger": trigger, "last_reason": self.last_reason}

    # -- 快照 / 持久化 --
    def set_active(self, active):
        """记录「当前档位真正生效的编码参数」(由 monitor 在每轮判定后回填)。

        为什么必须在 monitor 侧回填:`w/h` 取决于源分辨率与档位,而
        「源高于档位才缩」这条规则只有拿到源尺寸才算得准。
        """
        self.active_tier = dict(active or {})

    def snapshot(self, now=None):
        now = float(now if now is not None else time.time())
        plan = self.plan or {}
        th = self._thresholds()
        hold_s = round(now - self.over_since, 1) if self.over_since else 0
        recover_in = 0
        if self.level and self.under_since:
            recover_in = max(0, round(th["recover_hold"] - (now - self.under_since), 1))
        return {
            "level": int(self.level),
            "state": self.state,
            "reason": self.reason,
            "last_reason": self.last_reason,
            "active_tier": dict(self.active_tier or {}),
            "user_tier": plan.get("user") or {},
            "l1": plan.get("l1") or {},
            "l2": plan.get("l2") or {},
            "cpu": self.metrics.get("cpu", 0.0),
            "engine": self.metrics.get("engine", 0.0),
            "demand_pct": self.metrics.get("demand_pct", 0.0),
            # 跟随率:最低那一路的「实际输出时间 ÷ 墙钟」。前端用它解释为什么降档,
            # 也是引擎占用估算的校正项(需求 ÷ max(跟随, 0.5))。
            "follow_min": self.metrics.get("follow_min", 1.0),
            "streams": self.metrics.get("streams", 0),
            "max_hw_streams": int(self._n("max_hw_streams", 8)),
            "hold_s": hold_s,
            "recover_in_s": recover_in,
            "unstable": bool(self.unstable),
            # 崩溃信号分档:只有 resource 挡恢复。面板要能回答「刚才那几路
            # 退出算不算数」,所以 external 的近期计数也一并给出。
            "crashes_recent": self._crash_counts(now),
            "last_crash_at": round(float(self.last_crash), 1),
            "last_external_at": round(float(self.last_external), 1),
            "locked_until": round(self.locked_until, 1),
            "switches_today": int(self.switches),
            "restores_today": int(self.restores),
            "history": list(self.history[-8:]),
            "thresholds": th,
            "min_gain": plan.get("min_gain", MIN_GAIN),
            "engine_mp_s": plan.get("engine_mp_s", ENGINE_MP_S),
        }

    def dump(self):
        """可落盘的状态(崩溃重启后沿用档位;30 min 内崩过则不再往上恢复)。"""
        return {
            "version": 3,
            "boot_id": self.boot_id,
            "level": int(self.level),
            "level_changed_at": float(self.last_change),
            "level_history": list(self.history[-10:]),
            "switches_today": int(self.switches),
            "restores_today": int(self.restores),
            "day": self.day,
            "unstable": bool(self.unstable),
            "locked_until": float(self.locked_until),
            "last_crash_at": float(self.last_crash),
            "last_external_at": float(self.last_external),
            "crashes": [[ts, k, c] for ts, k, c in self.crashes[-20:]],
            "switch_ts": [float(t) for t in self.switch_ts[-20:]],
            "state": self.state,
        }

    def restore(self, data, now=None):
        """从状态文件恢复;任何异常都退回 L0(宁可少降,不可错降)。

        `crashes` 兼容 v2 的 `[ts, kind]` 二元组(v3 起是 `[ts, kind, cause]`):
        老文件没有 cause 字段,一律按 **resource** 解读 —— 保守方向,不会因为
        升级把「刚崩过」误判成「没崩过」而立刻恢复。
        """
        now = float(now if now is not None else time.time())
        if not isinstance(data, dict):
            return False
        try:
            level = int(data.get("level") or 0)
            self.level = level if level in (0, 1, 2) else 0
            self.last_change = float(data.get("level_changed_at") or 0)
            self.switches = int(data.get("switches_today") or 0)
            self.restores = int(data.get("restores_today") or 0)
            self.day = str(data.get("day") or "")
            self.unstable = bool(data.get("unstable"))
            self.locked_until = float(data.get("locked_until") or 0)
            self.last_crash = float(data.get("last_crash_at") or 0)
            self.last_external = float(data.get("last_external_at") or 0)
            self.crashes = [_restore_crash(row) for row in (data.get("crashes") or [])
                            if isinstance(row, (list, tuple)) and len(row) >= 2]
            self.switch_ts = [float(t) for t in (data.get("switch_ts") or [])]
            hist = data.get("level_history") or []
            self.history = [h for h in hist if isinstance(h, dict)][-10:]
            # 崩溃重启后 30 min 内一律不再往上恢复(来源:计划书 §3.8 状态持久化)
            if self.last_crash and (now - self.last_crash) < 1800:
                self.under_since = 0.0
            return True
        except (TypeError, ValueError):
            self.level = 0
            return False


def load_state(path, now=None):
    """读状态文件;损坏/缺失返回 None(调用方回 L0 + 记日志)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_state(path, data):
    """原子写(tmp + os.replace),失败只返回 False(由调用方决定是否记日志)。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def new_boot_id(now=None, pid=None):
    """本进程的唯一标识:主进程被拉起后 boot_id 变化 = 崩过一次的可靠证据。"""
    now = now if now is not None else time.time()
    pid = pid if pid is not None else os.getpid()
    return "%s-%s" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), pid)
