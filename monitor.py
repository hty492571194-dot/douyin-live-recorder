#!/usr/bin/env python3
"""抖音直播自适应监控(双通道检测 + 热点调度 + Web UI)。"""
import asyncio
import glob
import json
import os
import random
import shutil
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import date

import httpx

from schedule import HotspotStore, next_interval
from detect import check, probe_anchor, confirm_with_cookie
from cookie_guard import CookieGuard
from webui import State, start
from preflight import run_checks, find_ffmpeg
import health as health_mod
import web_rid_probe as rid_probe
import web_rid_browser as rid_browser


# ── 网络层告警 ──────────────────────────────────────────────────────────────
# 单主播的「异常」日志在系统性故障时会刷成一片(10 个主播 × 每轮一条),反而
# 掩盖了「这不是某个主播的问题,是整体连不上」。典型场景:继承了已失效的
# 环境代理 —— 服务活着、Web UI 正常,只有数据停止增长,可以静默好几天。
_NET_WINDOW = 1800          # 统计窗口(秒)
_NET_MIN_SAMPLES = 10       # 窗口内至少这么多次检测才下判断
_NET_MIN_FAILS = 8
_NET_FAIL_RATIO = 0.8
_net_checks = []            # 窗口内检测时间戳
_net_fails = []             # 窗口内失败时间戳
_net_last_alert = 0.0       # 上次告警时间(冷却用)


def _note_network_result(now, failed):
    """累计一次检测结果;命中「全员失败」时打一条带排查建议的告警。"""
    global _net_last_alert
    _net_checks.append(now)
    if failed:
        _net_fails.append(now)
    cut = now - _NET_WINDOW
    while _net_checks and _net_checks[0] < cut:
        _net_checks.pop(0)
    while _net_fails and _net_fails[0] < cut:
        _net_fails.pop(0)
    total, fails = len(_net_checks), len(_net_fails)
    if (total >= _NET_MIN_SAMPLES and fails >= _NET_MIN_FAILS
            and fails / total >= _NET_FAIL_RATIO
            and now - _net_last_alert > _NET_WINDOW):
        _net_last_alert = now
        log("[网络][!] 近 30 分钟 %d/%d 次检测失败(%.0f%%)——"
            "疑似系统性故障,不是某个主播的问题" % (fails, total, 100.0 * fails / total))
        log("[网络]    排查:① 进程是否继承了失效代理变量 "
            "(ps eww -o command -p <pid> | tr ' ' '\\n' | grep -i proxy) "
            "② 网络是否连通 (curl -I https://live.douyin.com) "
            "③ 登录态是否失效 (看 /api/auth)")
        return True
    return False


def _reset_network_stats():
    """仅供测试:清空滑动窗口。"""
    _net_checks.clear()
    _net_fails.clear()
    global _net_last_alert
    _net_last_alert = 0.0


def _purge_env_proxy():
    """清除继承来的环境代理变量,返回清除个数。

    抖音检测与 ffmpeg 拉流都必须直连。若服务是从受管终端 / IDE 集成终端 /
    CI 拉起的,常会继承 HTTP_PROXY / HTTPS_PROXY 等变量:
      · httpx 默认 trust_env=True,会把所有请求送进该代理;
      · ffmpeg 的 HTTP 协议同样读 http_proxy。
    代理一旦不可达(例如它只是某个已退出的沙箱进程留下的端口),症状就是
    「All connection attempts failed」→ 全员检测失败 → 不再判定开播 →
    录制停止 → 历史记录从某个时间点起再无新增。这类故障很隐蔽:服务本身
    活着、Web UI 照常响应,只有数据不再增长。

    必须在进程最早期执行(早于任何 httpx 客户端创建),否则已建连接不受影响。
    逃生开关:确实需要走代理时设 DOUYIN_KEEP_ENV_PROXY=1。
    """
    if os.environ.get("DOUYIN_KEEP_ENV_PROXY") == "1":
        return 0
    names = ("http_proxy", "https_proxy", "all_proxy")
    keys = [k for k in os.environ if k.lower() in names]
    for k in keys:
        os.environ.pop(k, None)
    return len(keys)


PURGED_PROXY_VARS = _purge_env_proxy()
import nas as nas_mod
import live_preview as live_preview_mod
import preview as preview_mod
import danmaku as danmaku_mod
import subtitle as subtitle_mod
import notify as notify_mod       # 告警通道层(企微群机器人 / 本机通知 / 日志)
import quality as quality_mod     # 画质调度:档位换算/收益判定/降级状态机(纯逻辑)
import interop as interop_mod     # 跨项目对接层(契约快照/问题反馈/诊断包;全旁路)
import incidents as incidents_mod  # 崩溃/故障事件流(左侧导航告警条的数据源)
try:
    import bot as bot_mod         # 企微机器人双向交互(菜单模式);缺依赖时降级为不可用
except Exception:
    bot_mod = None
import retention as retention_mod
try:
    import auth as auth_mod      # 登录态管理(仅检测通道);缺依赖时降级为不可用
except Exception:
    auth_mod = None

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
HOTSPOT_PATH = os.path.join(BASE, "hotspots.json")
HOTSPOT_EVENTS_PATH = os.path.join(BASE, "hotspot_events.json")
def _under_unittest():
    """是否在 `python -m unittest` 下运行。

    判据:`-m unittest` 会把 `__main__.__spec__.name` 置为 `unittest.__main__`,
    并加载 `unittest.main` 子模块;生产运行(直接跑 monitor.py / launchd)
    两者都没有,所以不会误判。
    ⚠️ 别用 `"unittest.__main__" in sys.modules` —— Python 3.13 实测**不会**注册这个键,
    照那个写判据会永远为 False(踩过)。
    """
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if getattr(spec, "name", "") == "unittest.__main__":
        return True
    return "unittest.main" in sys.modules


def _resolve_log_dir():
    """决定日志目录。**测试进程强制走临时目录**。

    为什么必须这样:跑单元测试会把夹具数据(「主播A」「示例主播」)写进**正式业务日志**,
    而正式日志是排查线上问题的第一手材料。2026-09-18 排查「服务反复重启」时就栽在这:
    日志里 12:42–12:58 每分钟一条 `[历史] 重新定位`,看着像进程被反复拉起,
    实际那是测试写进去的 —— `launchctl` 显示 job 只跑过 2 次,根本没重启过。

    也可用环境变量 `DOUYIN_LOG_DIR` 显式指定。
    """
    env = os.environ.get("DOUYIN_LOG_DIR")
    if env:
        return env
    if _under_unittest():
        d = os.path.join(tempfile.gettempdir(), "douyin-test-logs")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
    return os.path.join(BASE, "logs")


LOG_DIR = _resolve_log_dir()
# ⚠️ 本目录已**不再是录制视频目录**(2026-10-30 改造)。
# 视频一律写到 `recorder.output_dir`(空 = DEFAULT_STAGE_DIR,见 _resolve_output_dir),
# 这里降级为**元数据目录**:只放 history.json 与 _ass_backup-*.tar.gz 这类 KB 级文件。
# 历史页、补归档、scripts/backfill_* 全部按这个恒定路径读,所以暂存目录怎么改都
# 不影响它们;反过来说,谁再把视频写进这里,就是把「项目仅存系统文件」这条契约
# 打破了 —— 2026-10-30 实测该项目因此涨到 99G,其中视频占 98%。
RECORD_DIR = os.path.join(BASE, "recordings")
HISTORY_PATH = os.path.join(RECORD_DIR, "history.json")
# 录制暂存默认根:放在项目**外**,让「项目文件夹」与「素材」彻底解耦。
# 同块盘不改变磁盘总量,只是不再污染项目;要换外接盘只改
# recorder.output_dir 一个配置项(界面:常用参数 → 默认录制暂存目录)。
#
# 与 LOG_DIR 同一套路:**测试进程强制走临时目录**,否则一次单测就会在用户
# 家目录里建出一堆 <主播>/<日期>/ 空壳(测试里大量 _start_recording 用的
# 都是空 output_dir);`DOUYIN_STAGE_DIR` 供自检脚本/端到端实例隔离用。
def _resolve_default_stage_dir():
    env = os.environ.get("DOUYIN_STAGE_DIR")
    if env:
        return os.path.realpath(os.path.expanduser(env))
    if _under_unittest():
        d = os.path.join(tempfile.gettempdir(), "douyin-test-stage")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
    return os.path.expanduser("~/Movies/DouyinRecordings")


DEFAULT_STAGE_DIR = _resolve_default_stage_dir()

# 缺号主播补号巡检的状态与审计(不进 config:巡检每轮都改,高频写盘会跟前端保存打架)
RID_STATE_PATH = os.path.join(BASE, "state", "rid_backfill.json")
RID_FAILS_PATH = os.path.join(BASE, "state", "rid_backfill_fails.jsonl")
RID_BROWSER_DIR = os.path.join(BASE, "state", "browser", "rid")

DEFAULT_CONFIG = {
    "detection": {"mode": "mix", "cookie": "", "cookie_ref": "",
                  "api_base": "https://live.douyin.com/webcast/room/web/enter/"},
    # 登录态管理(仅服务检测通道;绝不用于弹幕 WS,详见 auth.py 顶部说明)
    # webhook_url 已弃用(2026-10-10):失效告警统一走 notify 通道层
    # (gift_watch.notify_channel/webhook_url),此键仅保留兼容旧配置,不再被读取。
    "auth": {"enabled": True, "check_interval_hours": 12, "webhook_url": ""},
    "schedule": {
        "cold_interval": 1800, "cold_jitter": 0.2,
        "hot_min": 300, "hot_max": 360, "live_interval": 1200,
        "hotspot_half_width": 1800, "hotspot_max": 2,
        "hotspot_merge_hours": 5, "hotspot_decay_days": 14,
        "analysis_hour": 5, "analysis_cluster_min": 90,
        "analysis_min_events": 5, "event_retention_days": 7,
        "recency_weight": 2, "deviation_trigger_minutes": 60,
        "max_witness_gap": 7200,
        "circuit_errors": 5, "circuit_cooldown": 1800,
    },
    "webui": {"host": "127.0.0.1", "port": 8780},
    # auto_record:开播即自动录制。历史主播缺失该字段时由 _migrate_auto_record
    # 补 True(保持"每次开播都录"的原有行为);新增主播由前端显式写 False。
    "monitors": [{"name": "示例主播", "anchor": "2145431900", "auto_record": True}],
    "nas": {"enabled": False, "protocol": "smb", "host": "", "port": 445,
            "share": "", "username": "", "password_ref": "keychain:douyin-nas",
            "mount_point": "~/DouyinArchive", "root_dir": "直播回放",
            "auto_discover": True, "host_check_hours": 24},
    "archive": {"mode": "auto", "external_dir": ""},
    "preview": {"dir": "", "start_delay": 60, "interval": 30, "shots": 5},
    # 实时预览(WebUI 弹出页):按需独立拉流 —— 打开预览页才启动一条独立
    # ffmpeg(第二路 CDN 连接)把源流 -c copy 直出 HLS;页面关闭后空闲
    # idle_timeout 秒自动回收,「不看就不拉流」。录制 ffmpeg 保持单路输出、
    # 与预览完全解耦;enabled=false 时预览页提示未开启,录制不受任何影响。
    "live_preview": {"enabled": True, "hls_time": 2, "list_size": 6,
                     "idle_timeout": 30, "max_sessions": 3},
    "recorder": {"output_dir": "", "enabled": True, "format": "flv",
                 # ── 录制暂存目录(2026-10-30 外迁)──
                 # output_dir 空 = DEFAULT_STAGE_DIR(~/Movies/DouyinRecordings),
                 # **不再回落项目内 recordings/**:项目只存系统文件,视频一律外置。
                 # 路径落在项目目录内会被 _resolve_output_dir 拒绝并回落默认值。
                 # output_dir_switch 决定「在录时改目录」怎么落地:
                 #   next_session(默认)= 本场不动,下一场生效(一场一个目录,零风险)
                 #   now = 立刻逐路受控重启切过去(本场被切成两段:两条历史、两次归档)
                 "output_dir_switch": "next_session",
                 # ── 时间基(2026-10-27 音画不同步根治)──
                 # True(默认) = **不加** -use_wallclock_as_timestamps:FLV tag 保留源流
                 #   时间戳,音视频落在同一坐标系。音频采样数与时间戳对齐、视频不丢帧;
                 #   绝对墙钟由巡检采样 clock_offset_ms 还原(subtitle.CLOCK_OFFSET_KEY),
                 #   字幕锚点照旧、不漂。
                 # False = 回退旧行为:每个包的时间戳被"读到它的墙钟"覆盖。包成批到达时
                 #   PTS 被压扁、断流处直接跳秒 —— 音轨采样数与视轨脱钩(asrclip 报的
                 #   -8.43s),且视频在 CFR 下丢帧 38%。仅在排查兼容问题时才置 False。
                 "src_timestamps": True,
                 # ── 编码器路线(2026-10-22 硬编改造,见下方整段说明) ──
                 # vt = VideoToolbox 硬解 + 硬编(默认):单路 0.02 核,7 路合计 0.3–0.4 核;
                 # sw = libx264 软编(回退用);copy = 直接复制原流(最省,但无参数可调)。
                 # ⚠️ 缺键时的兜底是 sw 而不是 vt —— 老配置/合成调用走原行为,
                 #    真实配置由 load_config 用本默认值补齐,所以线上默认仍是 vt。
                 "video_mode": "vt",
                 "hw_decode": True,      # -hwaccel videotoolbox(硬解)
                 "force_hw": True,       # -allow_sw 0:硬件不可用就立刻失败,绝不静默回落软编
                 # 分辨率档位:0=原生(不缩放)/1080/864/720,值为目标短边像素。
                 # 换算与收益判定真源在 quality.py,这里只是配置。
                 "resolution": 0,
                 # 视频码率(kbps):**体积的唯一决定因素**(同码率下各档位体积相同)。
                 # 0/空 = 流复制(与 video_mode=copy 等价)。
                 "bitrate": 1200, "segment_time": 0,
                 # 转码预设:只在 video_mode=sw 时生效(硬件无 preset 概念)。
                 # 默认 veryfast 而不是 libx264 自带的 medium —— 2026-09-18 实测
                 # 4 路并发下 veryfast 每路仍有 2.6x 实时余量,medium 只剩 1.23x
                 # (直播流是实时的,余量不足会越落越远直至丢帧断流)。
                 "preset": "veryfast",
                 # ── 自动降级(2026-10-22):过载前降档,而不是等停滞了再重启 ──
                 # 只在 video_mode=vt 生效(copy 没有编码参数可降)。
                 # L1/L2 是 (分辨率, 码率) 二元组,放在「录制设置」页可改;
                 # 有效性判定走 quality.usable —— 用户把主配置配得比降级档还轻时,
                 # 降级会整体自动失效(DISABLED_DOMINATED),不会反向降级。
                 "auto_degrade": True,
                 "degrade_l1_resolution": 720, "degrade_l1_bitrate": 700,
                 "degrade_l2_resolution": 720, "degrade_l2_bitrate": 500,
                 # 阈值(高级参数页可改;92/72 是 20 个百分点的滞回)
                 "degrade_cpu": 92, "degrade_engine": 92, "degrade_recover": 72,
                 "degrade_hold": 60, "degrade_recover_hold": 600,
                 "degrade_min_gap": 900, "degrade_max_restore": 4,
                 "degrade_max_switches": 6,
                 # 收益门槛(%):两个维度都不到这个值就不值得付一次重启丢帧的代价
                 "min_gain": 10,
                 # 准入控制:在录路数超过它时,新增路直接 -c copy(引擎预算 450÷46≈9.7,留余量)
                 "max_hw_streams": 8,
                 # 停滞自愈(「假在线」防护):_maintain_recording 读取,高级参数页可改。
                 # 必须登记在默认配置里,否则 /api/config 不返回它们、页面回填为空,
                 # 显示的「当前值」会与真正生效的 90/4/600 不一致。
                 "stall_seconds": 90, "stall_max_restarts": 4, "stall_cooldown": 600,
                 # ── 流中断等流窗(2026-10-31) ──
                 # 流侧断开(404 / Stream ends prematurely)与本地资源故障是两类事:
                 # 前者是主播下播或推流中断,抖音翻状态要 24~70 秒,这段窗口里拉不到
                 # 流属正常现象。所以它**不消耗** stall_max_restarts,改用退避重连 +
                 # 本窗口等待;窗内确认下播则干净收尾、不报错,窗满仍拉不到才升级故障。
                 # 详见 _await_stream。
                 "stream_await_seconds": 900},
    "danmaku": {"enabled": True, "offset_seconds": 0, "gift_offset_seconds": 0,
                "font_size": 44, "capture_member": False, "style": "queue",
                "queue_lines": 5, "queue_seconds": 8},
    # 礼物静默告警:录制中的场次超过阈值一条礼物都没收到 → 诊断 + 告知。
    # silent_minutes 45 是用户给的经验值。⚠️ 直播间长时间无人送礼在冷门/凌晨场次
    # 完全正常,所以告警必须分级(见 _gift_diagnose):线路健康时只落日志 + 前端标记,
    # 不推手机 —— 否则几天后告警就会变成噪音被无视。
    # webhook_url 填企微群机器人地址(群 →「…」→ 群机器人 → 添加),留空=只记本机。
    "gift_watch": {"enabled": True, "silent_minutes": 45, "webhook_url": "",
                   "notify_level": "suspect", "cooldown_minutes": 60,
                   # 通道选择:auto=配了 webhook 走 webhook,否则走连接器 CLI(发授权人本人,
                   # 无需建群机器人);webhook/wecom_cli=强制只用其中一条
                   "notify_channel": "auto",
                   # 弹幕活跃到这个速率(条/分钟)却一个礼物都没有,才怀疑礼物通道
                   # 出了问题 —— 直播间本来就冷清时属于正常,不该打扰
                   "busy_chat_rate": 2,
                   "local_notify": True},
    # 每日质检日报:每天 hour 点(±抖动)体检机器人通路/登录态/磁盘/归档并发日报。
    # 发日报这个动作本身就是机器人通路的日常验证 —— 通路坏了,当天没收到日报
    # 就是信号;正常项也发(always_send)是给"一切正常"一个正反馈,免得巡检
    # 结果只躺在日志里没人看。强制发送见 _daily_report_items 的 force 条件。
    "daily_report": {"enabled": True, "hour": 4, "jitter_minutes": 30,
                     "always_send": True,
                     "disk_min_gb": 10,
                     "expire_warn_days": 14, "expire_alert_days": 7},
    # 缺号主播 web_rid 自动补齐(旁路,2026-10-26):
    # 添加主播时若只拿到 sec_uid / 抖音号,web_rid 要等主播开播后用浏览器渲染主页
    # 才看得到(DOM 里「直播中」入口的 href)。这里只管「把 web_rid 弄到手」,
    # 补上之后立刻回到 detect 的既有检测/录制链路,不引入第二套检测。
    #   enabled="auto"  → 有待补主播就自动开一个 7 天窗口,没有就彻底不启浏览器
    #   window 到期不自动续期 —— 无限期自动巡检是最容易把 IP 打黑的做法,
    #   续期要么人工点,要么等新主播入场(待补名单变化)时自动重开
    #   间隔三档两段随机(先按权重选档,再档内随机分钟):默认均值≈76min
    "rid_backfill": {
        "enabled": "auto",
        "window_days": 7,
        "interval_tiers_min": [[40, 50], [80, 90], [110, 120]],
        "tier_weights": [0.5, 0.3, 0.2],
        "avoid_same_tier_twice": True,
        "second_jitter": True,
        "max_per_round": 3,
        "gap_sec_min": 25, "gap_sec_max": 45,
        "timeout_sec": 45, "round_budget_sec": 180,
        "max_checks_per_day": 100,
        "notify_on_fill": True,
        "quiet_hours": [],
    },
    # 企微机器人双向交互(菜单模式,详见 bot.py 顶部说明):轮询授权人单聊,
    # 白名单命令 + 无法理解回菜单。发消息不需要任何配置(复用告警通道的连接器授权)。
    "bot": {"enabled": True, "poll_interval": 45, "allowed_sender": ""},
    "on_live_command": "echo '[record] {name} live room_id={room_id}'",
    "on_offline_command": "echo '[stop] {name} offline'",
}


def _under_project(path):
    """path 是否落在项目目录内(realpath 比较,含 BASE 本身)。"""
    try:
        b = os.path.realpath(BASE)
        p = os.path.realpath(path)
    except OSError:
        return False
    return p == b or p.startswith(b + os.sep)


def _resolve_output_dir(cfg):
    """解析录制暂存根:空串 = DEFAULT_STAGE_DIR;绝不返回项目目录内的路径。

    为什么要在解析层硬拦(而不是只在 webui 校验层拦):配置可以直接改
    `config.json`、也可以用 POST /api/config 直调。只要有一条路能写进项目,
    「项目文件夹不存录制视频」这条就守不住 —— 实测外泄 99G 就是这么来的
    (output_dir 空串默默回落 BASE/recordings,而该字段此前没有 UI 入口)。
    """
    raw = ((cfg.get("recorder") or {}).get("output_dir") or "").strip()
    cand = os.path.expanduser(raw) if raw else DEFAULT_STAGE_DIR
    if _under_project(cand):
        log(f"[录制] 暂存目录 {cand} 位于项目目录内,已拒绝并回落到 "
            f"{DEFAULT_STAGE_DIR}(配置项 recorder.output_dir)")
        return DEFAULT_STAGE_DIR
    return os.path.realpath(cand)


# 本场暂存目录钉死表:name -> <暂存根>/<主播>/<YYYY-MM-DD>
# 铁律:**一场一个目录**。崩溃重启 / 换房间重启 / 降档重启都读它,不重新解析
# 配置 —— 否则用户一场录到一半改了暂存目录,下一次重启就会把后半场写到另一个
# 盘,归档、字幕、礼物结算全按「一个日期目录」为单位,一场被拆两处就全散。
# 只在 _stop_recording(下播/放弃/手动停)时解除。
_session_dir_pin = {}

# 延后生效的暂存目录:{dir, since} 或 None。仅 next_session 策略下产生:
# 在录时改目录只记这里,本场不受影响,下一场起录自然读新配置。
# 不落盘:服务重启后所有会话已停,新配置本身就已生效,没有「pending」可言。
_stage_switch = None


def _session_out_dir(name, cfg):
    """本场暂存目录(钉死)。首次调用时按当前配置钉下,之后一直返回同一个。"""
    cur = _session_dir_pin.get(name)
    if cur:
        return cur
    d = os.path.join(_resolve_output_dir(cfg), name, time.strftime("%Y-%m-%d"))
    _session_dir_pin[name] = d
    return d


def _deep_merge(base, override):
    """深合并:override 覆盖 base,字典递归合并,用于补齐缺失键。"""
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _keep_prev_config(path):
    """写 config.json 前,把上一版另存为 config.prev.json(只留最近一份)。

    起因:调试时用 `POST /api/config` 整段提交 monitors,把 10 位主播覆盖成 1 位,
    而 config.json 没有任何历史版本,只能靠日志和临时快照拼回。留一份上一版,
    同类误操作就能直接 cp 回来。非 config.json 的写入不受影响。
    """
    if os.path.basename(str(path)) != "config.json":
        return
    prev = os.path.join(os.path.dirname(str(path)) or ".", "config.prev.json")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, "rb") as src, open(prev, "wb") as dst:
                dst.write(src.read())
    except OSError:
        pass


def _atomic_write_json(path, data):
    """tmp + os.replace 原子写 JSON。"""
    _keep_prev_config(path)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _migrate_auto_record(cfg):
    """历史主播一次性补 auto_record=True,保持「每次开播都录」的原有行为。

    只在字段缺失时补齐 —— 补齐后字段就常驻配置里了,此后新增主播由前端显式
    写入(默认 False),不会再被这段逻辑改回 True。返回是否有改动。
    """
    changed = False
    for m in cfg.get("monitors") or []:
        if isinstance(m, dict) and "auto_record" not in m:
            m["auto_record"] = True
            changed = True
    return changed


def _find_monitor(cfg, name):
    """按主播名取 monitors 里的那一项;找不到返回 None。"""
    for m in cfg.get("monitors") or []:
        if isinstance(m, dict) and m.get("name") == name:
            return m
    return None


def _parse_until(m):
    """解析 record_until(YYYY-MM-DD,含当天);缺失/格式坏一律返回 None。"""
    try:
        return date.fromisoformat(str((m or {}).get("record_until") or "").strip())
    except ValueError:
        return None


def record_state(m, today=None):
    """该主播的录制期限判定。**纯时间函数,零 IO,配置零写盘**。

    返回 (state, until, days_left):
      state ∈ "ok" | "paused" | "expired" | "missing_until"
      until    date | None  解析后的 record_until(short 才有)
      days_left int | None  含今天的剩余可录自然日(仅 ok/short 有意义)

    为什么是派生而不是后台任务去改配置:
      · 到期是纯时间函数 —— 算一次即可;写盘方案要处理「续期后恢复」的反向
        迁移,漏一步就永久不录(违反「止损必须配套主动恢复路径」);
      · config.json 是整表替换,历史上出过误删主播的事故面,能不写就不写;
      · 不新增常驻协程,不占 health.SPECS,没有新的静默失败点。

    判定优先级(2026-10-22 拍板):
      1. auto_record=False        → paused   手停就是暂停,哪怕期限还没到
      2. short 且 today > until   → expired  到期:不再开新场(决策5A:归暂停档、停止检测)
      3. short 且 until 缺/坏     → missing_until  保守不录,UI 标「期限缺失」
      4. 其余(long / 缺字段)      → ok
    """
    if m is None:
        return "ok", None, None
    if m.get("auto_record", True) is False:
        return "paused", _parse_until(m), None
    if m.get("record_mode") != "short":
        return "ok", None, None
    until = _parse_until(m)
    if until is None:
        return "missing_until", None, None
    today = today or date.today()
    if today > until:
        return "expired", until, 0
    return "ok", until, (until - today).days + 1


def record_window(cfg, name, today=None):
    """单个主播的期限快照(给 /api/status 下发;前端只渲染不自算)。"""
    m = _find_monitor(cfg, name)
    state, until, days_left = record_state(m, today)
    mode = (m or {}).get("record_mode")
    if mode not in ("long", "short", "paused"):
        # 缺字段按 auto_record 派生,与 record_state 的兜底一致
        mode = "paused" if (m is not None and m.get("auto_record", True) is False) else "long"
    # state 是判定的最终结论,mode 必须跟它走,不能各说各话。
    # 手工改过 config.json 会留下「auto_record=false 但 record_mode=long」这种
    # 矛盾组合(字段本身合法,上面的兜底拦不住):原样下发的话前端 _recBucket
    # 会把它归进「长期录制」档、徽标写「长期」,而开关是关的 —— 页面自相矛盾。
    # 手停优先(record_state 已判 paused),这里统一收敛,不写盘、不改配置。
    if state == "paused":
        mode = "paused"
    return {"state": state, "mode": mode,
            "until": until.isoformat() if until else None,
            "days_left": days_left}


def record_windows(cfg, today=None):
    """全部主播的期限快照 + 服务端「今天」。

    今天的日期只由服务端给 —— 浏览器时区/时钟不可信,「剩几天」一律用这里的值。
    """
    today = today or date.today()
    streamers = {}
    for m in cfg.get("monitors") or []:
        if isinstance(m, dict) and m.get("name"):
            streamers[m["name"]] = record_window(cfg, m["name"], today)
    return {"today": today.isoformat(), "streamers": streamers}


def auto_record_enabled(cfg, name):
    """该主播是否「开播即录」。

    执行真源是 record_state(期限 + 开关合一):字段缺失按 True 处理(历史行为),
    short 且已到期返回 False(到期只禁新场;正在录的场次由调用方继续收尾)。
    """
    return record_state(_find_monitor(cfg, name))[0] == "ok"


# 到期只记一次日志(决策7A:不推企微,只留业务日志);进程内去重,不写盘。
_expiry_logged = set()


def log_expiry_once(cfg, name):
    m = _find_monitor(cfg, name)
    state, until, _ = record_state(m)
    if state != "expired" or name in _expiry_logged:
        return
    _expiry_logged.add(name)
    log(f"[·] {name}: 短期录制已到期(至 {until}),不再自动录制;卡片「详情」里可续期或改为长期")


def is_dormant(cfg, name, recording):
    """该主播是否「休眠」:不可自动录制、且当前没在录。

    休眠 = 既不自动录、也不自动检测 —— 不用管的主播,再每 20~30 分钟查一次
    纯属浪费请求与 Cookie 配额。想看时点卡片上的「刷新」手动查一次,想恢复
    自动就重新打开开关 / 续期。
    「不可自动录制」含两种:用户关了开关(paused)、短期录制已到期(expired,
    决策5A:到期也停止检测)。
    正在录的(手动开的)不能休眠:还要靠检测循环发现下播、收尾归档(决策6A:
    到期不中断在录场次)。
    """
    if recording:
        return False
    return not auto_record_enabled(cfg, name)


def load_config():
    """读取 config.json;缺失键用默认值补齐;损坏时备份重建默认,不崩溃。"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            cfg = _deep_merge(DEFAULT_CONFIG, raw)
            migrated = _migrate_auto_record(cfg)
            if migrated or json.dumps(cfg, ensure_ascii=False, sort_keys=True) != json.dumps(raw, ensure_ascii=False, sort_keys=True):
                _atomic_write_json(CONFIG_PATH, cfg)  # 补齐缺失键后落盘一次
            return cfg
        except (json.JSONDecodeError, OSError) as e:
            backup = f"{CONFIG_PATH}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(CONFIG_PATH, backup)
                log(f"[警告] config.json 损坏({e}),已备份为 {backup},重建默认配置")
            except OSError:
                log(f"[警告] config.json 损坏({e})且备份失败,重建默认配置")
    _atomic_write_json(CONFIG_PATH, DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


# --- 日志(同时 stdout 与落盘,跨天切分,清理 7 天前) ---
_log_date = None


def _ensure_log():
    global _log_date
    today = time.strftime("%Y-%m-%d")
    if _log_date == today:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    _log_date = today
    try:
        cutoff = time.time() - 7 * 86400
        for fn in os.listdir(LOG_DIR):
            if fn.startswith("monitor-") and fn.endswith(".log"):
                fp = os.path.join(LOG_DIR, fn)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except OSError:
                    pass
    except OSError:
        pass


def log(msg):
    """输出一条日志:stdout + logs/monitor-YYYY-MM-DD.log;写文件失败降级仅 stdout。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _ensure_log()
    if _log_date:
        try:
            with open(os.path.join(LOG_DIR, f"monitor-{_log_date}.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


notify_mod.set_log(log)   # 告警通道层的日志并入项目日志(notify 不反向 import monitor)


# --- 命令执行(可观测:记录 pid/name/cmd,轮询退出码) ---
_active_procs = {}  # pid -> subprocess.Popen


def run_command(state, template, ctx):
    if not template:
        return
    ctx = {**ctx, "BASE": BASE}  # B1: 所有命令模板统一可用 {BASE}
    try:
        cmd = template.format(**ctx)
    except (KeyError, IndexError):
        log(f"命令模板占位符缺失: {template}")
        return
    log(f"  执行: {cmd}")
    try:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _active_procs[proc.pid] = proc
        state.add_command(proc.pid, ctx.get("name", "?"), cmd)
    except Exception as e:
        log(f"  命令失败: {e}")


def poll_commands(state):
    """轮询活跃子进程;非 0 退出码告警。"""
    for pid in list(_active_procs):
        proc = _active_procs.get(pid)
        if proc is None:
            continue
        code = proc.poll()
        if code is None:
            continue
        del _active_procs[pid]
        state.mark_command_exit(pid, code)
        if code != 0:
            log(f"[命令] pid={pid} 异常退出 code={code}")


# --- 录制管理(ffmpeg 拉流,可观测进度) ---
_recordings = {}  # name -> {proc, started_at, output_path, stream_url, format, room_id}
# 礼物静默告警:name -> {code, level, detail, silent_min, at}(供前端展示与冷却去重)
_gift_alerts = {}
# 录制健康度:name -> {size, ts, restarts, room_id, stalled}
#   size/ts:最近一次观测到的已落盘字节数与时(用)于判定「文件是否还在增长」
#   restarts:本场因异常重启的次数(达上限后不再重启,避免空转刷历史)
#   重启达上限后进入冷却期,冷却期内不再为该主播启动录制
_rec_health = {}
_rec_giveup_until = {}  # name -> 冷却截止时间戳
# 「放弃录制」的原因是给人看的:界面要在「直播中」旁边说清楚为什么没在录。
_rec_giveup_reason = {}  # name -> 最近一次放弃录制的原因
# ── 流中断「等流」态(2026-10-31) ────────────────────────────────────────
# name -> {"fp": 失败地址指纹, "since": 起等时刻, "fails": 同址失败次数,
#          "next_at": 下次允许重试的时刻, "url": 失败地址(给人看)}
#
# 为什么必须有它(实测数据在《断流与下播判定-数据研究与报错逻辑重构》):
# 流侧退出(404 / Stream ends prematurely)**不是**本地故障,它有两种截然不同的
# 成因 —— 主播真下播,或主播推流短暂中断。抖音翻状态有 24~70 秒延迟,所以
# 退出当下**无法区分**。原实现把这个不确定性按最坏情况处理:每次退出都立刻
# 重启并吃掉一格 `stall_max_restarts`(默认 4),80 秒内烧光 → 冷却 600s。
# 8 天实测:61 次放弃里 18 次在 603~621s(冷却刚满)就恢复,且 2~5 分钟区间
# **0 次** —— 闸门根本不让试,那 18 次是白扔的 10 分钟。
#
# 所以把「流侧退出」和「本地资源故障」拆成两套预算:本字典记流中断 episode,
# 其内所有重试都不消耗 stall_max_restarts(那是给资源故障用的),改用退避 +
# 等流窗。判定靠 `fp`(地址指纹)与抖音的 is_live,不靠重试次数。
_rec_stream_wait = {}
# 地址指纹里参与比对的签名参数。抖音同一场直播断流前后这几项**完全不变**,
# 下播后 reflow 页还会继续下发同一条占位地址 —— 带这些参数才能认出「同一条」。
_FP_KEYS = ("unique_id", "sign", "wsSecret", "expire", "wsTime", "keeptime")
# 流中断 episode 的重试退避(秒)。第一档紧到 20s(= 巡检周期),让"抖一下就回来"
# 的常见断流仍然秒级恢复;之后指数拉开,避免对着一条死地址每 20s 空转。
_STREAM_BACKOFF = (20, 60, 120, 240, 480)
# 「判定在播、却不在录制」是异常态,需要提前复检;这是同一异常两次复检的最小间隔,
# 避免每 20s 的看护巡检把它变成请求风暴。
_anomaly_probe_at = {}   # name -> 上次因该异常请求复检的时刻
_ANOMALY_RECHECK_SEC = 120
_history = []     # 录制历史(内存缓存,持久化到 recordings/history.json)
# webui State 引用(由 main 注入)。history 的任何变更都必须同步过去,
# 否则前端表格与弹幕面板读到的是旧快照:归档后本机文件已被移走,而快照里
# 仍是 archive.state=none,弹幕面板就会重定位失败并误报「NAS 未挂载」。
_web_state = None
_manual_paused = set()   # 用户手动停止录制的主播名(直播中也不再自动重录)
_last_stream_url = {}    # name -> 最近一次检测到的 stream_url(供手动开始)

# ── 画质调度运行时(2026-10-22 硬编改造) ──────────────────────────────────
# _quality_gov:降级状态机实例(quality.Governor)。为 None 表示质量调度未启动
#   (单测 / 极早期启动阶段),此时一律按 L0(用户主配置)编码,行为与改造前等价。
# _src_size:name -> (w, h) 本场源分辨率。从首个分片的 FLV onMetaData 学到,
#   只用于**引擎需求估算**(缩放本身由 scale 表达式保证只缩不放)。
# _hw_ok:硬件编码能力探针结果缓存(None=未探测),探不到就整体回 copy。
_quality_gov = None
_src_size = {}
_hw_ok = None
_hw_reason = ""
_quality_status = {}     # /api/status.quality 的快照(由 _quality_loop 每轮刷新)

# --- Cookie 使用护栏 ---
# 真实账号的登录态是稀缺资源,所有带 Cookie 的请求都要先过这道闸。
# 详见 cookie_guard.py;这里持有全局唯一实例,配置变更时重新载入规则。
_cookie_guard = None
# 供矛盾状态校验复用的 HTTP client(由 main 注入,与检测循环同一个)
_http_client = None
# 矛盾状态校验的节流状态:name -> {last, tries, room_id}
_conflict_state = {}
_preview_tasks = {}      # name -> asyncio.Task(自动截图任务,下播/删除时取消)


def _relocate_history(cfg):
    """启动时重新定位历史记录:本机文件已不在(通常因已归档)时,
    到归档目标(NAS / 外接硬盘)里找对应的 <主播>/<日期> 目录并更新指向。

    这让"存储位置变化"后历史里的链接自动指向新位置,而不是留一条死路径。
    返回被重新定位的条目数。
    """
    nas_cfg = cfg.get("nas") or {}
    if not nas_cfg.get("enabled", False):
        return 0
    root = nas_cfg.get("root_dir", "直播回放")
    targets = []
    try:
        mp = nas_mod._mount_point(nas_cfg)
        if os.path.isdir(mp):
            targets.append((os.path.join(mp, root), "nas"))
    except Exception:
        pass
    ext = ((cfg.get("archive") or {}).get("external_dir") or "").strip()
    if ext and os.path.isdir(ext):
        targets.append((os.path.join(ext, root), "external"))
    if not targets:
        return 0

    relocated = 0
    missing = 0
    for e in _history:
        if e.get("status") != "done":
            continue
        a = e.get("archive") or {}
        if a.get("state") == "archived":
            continue
        p = e.get("output_path") or ""
        if not p:
            continue
        src_dir = os.path.dirname(p)
        # 判据:**视频文件**是否还在本机(而非仅目录存在)。
        # 老格式条目目录里可能只剩 .meta,视频早已搬走,这种情况同样需要重定位。
        base = os.path.basename(p)
        pat = base.replace("-%03d", "-*") if "%03d" in base else base
        if glob.glob(os.path.join(src_dir, pat)):
            continue  # 本机还在,无需重定位
        # 日期目录:优先取路径末段,老格式(无日期层)从文件名 <主播>-20260822-... 提取
        date_dir = os.path.basename(src_dir)
        if not (len(date_dir) == 10 and date_dir[4] == "-" and date_dir[:4].isdigit()):
            m = re.search(r"-(\d{8})-\d{6}", os.path.basename(p))
            if not m:
                continue
            s = m.group(1)
            date_dir = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        name = e.get("name", "?")
        hit = None
        for base, kind in targets:
            cand = os.path.join(base, name, date_dir)
            if os.path.isdir(cand):
                hit = (cand, kind)
                break
        if hit:
            a.update({"state": "archived", "dest": hit[0], "target": hit[1],
                      "at": a.get("at") or time.time(), "error": None})
            relocated += 1
        else:
            # 本机没有、归档目标里也没有 —— 文件确实已不存在,标记避免继续显示死路径
            a.update({"state": "missing", "dest": None, "target": None,
                      "error": "文件已不在本机,归档目标中也未找到"})
            missing += 1
        e["archive"] = a
    if relocated or missing:
        _save_history()
        log(f"[历史] 重新定位: 归档 {relocated} 条 / 文件缺失 {missing} 条")
    return relocated


def _load_history():
    global _history
    try:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _history = data if isinstance(data, list) else []
        else:
            _history = []
    except (json.JSONDecodeError, OSError):
        _history = []
    # 启动时清理孤儿 recording 记录:进程刚启动,_recordings 为空,所有 recording 均为遗留。
    # 文件仍在 → 上次异常中断(ffmpeg 崩溃/进程被杀),标记 done 并回填时长/大小;
    # 文件不存在或分片 pattern → 丢弃。否则会永远卡在"录制中"。
    keep = []
    cleaned = False
    for e in _history:
        if e.get("status") == "recording":
            p = e.get("output_path", "") or ""
            if p and "%03d" not in p and os.path.exists(p):
                e["status"] = "done"
                e["ended_at"] = time.time()
                real = _media_duration(p)
                e["duration"] = round(real if real is not None
                                      else (time.time() - e.get("started_at", time.time())), 1)
                try:
                    e["size"] = os.path.getsize(p)
                except OSError:
                    pass
                keep.append(e)
            else:
                cleaned = True  # 孤儿记录:文件不存在或分片遗留,丢弃
        else:
            keep.append(e)
    if cleaned:
        _history = keep
        _save_history()


def _save_history():
    try:
        os.makedirs(RECORD_DIR, exist_ok=True)
        _atomic_write_json(HISTORY_PATH, _history)
    except OSError:
        pass


def _push_history():
    """把内存 history 同步给 webui state(无引用或异常均静默,不阻断录制主流程)。"""
    if _web_state is None:
        return
    try:
        _web_state.set_history(_history)
    except Exception as e:
        log(f"[历史] 同步到前端失败: {e}")


def _commit_history():
    """history 变更后的统一出口:落盘 + 同步前端。

    只落盘不同步的话,磁盘 history.json 是对的,但内存里的前端快照是旧的,
    前端要等到下次进程重启才看得到——期间弹幕面板会把已归档场次当本机路径处理。
    """
    _save_history()
    _push_history()


def _history_add(state, name, output_path, started_at):
    """录制开始时追加一条历史(status=recording)。"""
    # archive 字段记录归档生命周期:
    #   none(留本机) / pending(已入队) / archiving(搬运中)
    #   archived(已归档,含归档落点 dest) / failed(失败,含原因)
    _history.append({"name": name, "started_at": started_at,
                     "output_path": output_path, "status": "recording",
                     "archive": {"state": "none", "target": None,
                                 "dest": None, "at": None, "error": None}})
    if len(_history) > 500:
        del _history[:-500]  # 只保留最近 500 条
    _commit_history()


def _sibling_danmaku_jsonls(jsonl_path):
    """同目录下**其它场次**的 danmaku.jsonl(跨场礼物归属的兜底数据源)。

    2026-10-29 实测事故:ffmpeg 19:05:11 崩溃重建,弹幕会话直到 19:06:03 才
    切输出(见 _restart_recording 的立即 retarget,已把窗口压到毫秒级),那
    52 秒里收到的 41 条礼物全写进了上一场的 jsonl —— 上一场早已结算(读到 0 条),
    本场又只认自己文件,41 条谁的账都不算。

    这里把兄弟文件交给 gift_channel_coverage 的 extra_paths,由它**按本场
    时间窗口收窄**后借入(没有 started_at 就不借),既是兜底又不会把整场算重。
    读不了目录 → 空列表(退化为只统计本场文件,行为与改造前一致)。
    """
    if not jsonl_path:
        return []
    d = os.path.dirname(jsonl_path)
    base = os.path.basename(jsonl_path)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [os.path.join(d, fn) for fn in names
            if fn != base and fn.endswith(".danmaku.jsonl")]


def _history_finish(state, name, ended_at=None):
    """录制结束时回填结束时间/时长/大小(status=done);清理该主播所有 recording 条目。

    时长优先取文件真实媒体时长(ffmpeg -i 解析),失败回退墙钟差值(ended - started_at),
    保证历史记录时长与视频文件一致。state 可为 None(退出清理场景)。
    """
    ended = ended_at if ended_at is not None else time.time()
    # 轻通道对账计数(会话可能已 stop → None,此时历史只记字段完整性,不谎报已对账)
    dm_now = None
    try:
        dm_now = danmaku_mod.get(name)
    except Exception:
        dm_now = None
    changed = False
    done_dirs = set()
    for e in _history:
        if e.get("name") == name and e.get("status") == "recording":
            e["status"] = "done"
            e["ended_at"] = ended
            p = e.get("output_path")
            real = _media_duration(p) if p else None
            e["duration"] = round(real if real is not None
                                  else (ended - e.get("started_at", ended)), 1)
            # 分片场次:模板路径不是实际文件,os.path.exists 恒 False ⇒ size 一直是
            # 缺的;顺带把段数写进历史,历史页才能显示「分了几段」。
            seg = _segment_stats(p) if (p and _SEG_TAIL in p) else None
            if seg is not None:
                e["segments"] = seg["count"]
                if seg["count"]:
                    e["size"] = seg["bytes"]
            elif p and os.path.exists(p):
                try:
                    e["size"] = os.path.getsize(p)
                except OSError:
                    pass
            # 实际画质与码率(2026-10-28):历史页要能一眼看出「这一场录成什么样」。
            # 配置里的 resolution/bitrate 是**目标值**,落盘值才是**事实** ——
            # 自动降级、源本身低清、流复制(copy)不重编码,三种情况都会让两者
            # 不一致,只看配置会得出错误结论。此时文件还在本机,读取几乎无成本。
            try:
                mi = _media_info(p, e.get("size"), e.get("duration"), seg=seg)
            except Exception:
                mi = None
            if mi:
                e["media"] = mi
            # 礼物完整性(2026-10-16 起;2026-10-29 改为轻通道口径)。
            # gift_integrity 里现在有两层:
            #   ratio        "存在礼物信息"的时间占比(老口径,保留兼容)
            #   light        轻通道完整性 —— 收到 / 落盘 / 字段缺项逐项对账,
            #                这才是健康度判据(送礼人/礼物名不在其列:协议不给)
            # jsonl 缺失 / 时长未知 / 读取异常 → 不写字段(前端无胶囊)。
            jp = None
            led = None
            try:
                jp = danmaku_mod.jsonl_for(p) if p else None
                # 账本必须按「本条目自己的 jsonl」取(2026-10-29 修复):
                # 场次切换后会话的当前计数已经属于下一份文件,直接读
                # gift_light_recv 会拿 B 场的收到数去对 A 场的落盘数。
                if dm_now is not None and jp:
                    try:
                        led = dm_now.ledger_for(jp)
                    except Exception:
                        led = None
                gi = (danmaku_mod.gift_channel_coverage(
                    jp, float(e["duration"]),
                    started_at=e.get("started_at"),
                    recv=(led["recv"] if led
                          else getattr(dm_now, "gift_light_recv", None)),
                    parse_errors=(led["parse_errors"] if led
                                  else getattr(dm_now, "light_gift_errors", 0) or 0),
                    write_errors=(led["write_errors"] if led
                                  else getattr(dm_now, "light_write_errors", 0) or 0),
                    extra_paths=_sibling_danmaku_jsonls(jp))
                    if (jp and e.get("duration")) else None)
            except Exception:
                gi = None
            if gi is not None:
                e["gift_integrity"] = gi
            # 礼物诊断计数:fault/suspect/muted/total,历史行警示胶囊数据源。
            # giftwatch.jsonl 不存在(没触发过诊断)→ 不写字段。
            # (2026-10-16 二次瘦身:gift_stats 明细统计已按用户反馈下线,
            #  具体礼物情况不再采集落盘;danmaku.gift_session_stats 保留作工具函数。)
            try:
                gd = _gift_diag_counts(p) if p else None
            except Exception:
                gd = None
            if gd is not None:
                e["gift_diag"] = gd
            changed = True
            done_dirs.add(os.path.dirname(e.get("output_path") or ""))
    if changed:
        _commit_history()
        # S2 契约版本随数据走:收尾场次所在日期目录 session.json 翻 finalized
        # (随档复制到 NAS;归档侧另有兜底,见 nas._process_one 对 interop 的调用)
        for d in done_dirs:
            if not d:
                continue
            try:
                interop_mod.mark_finalized(d)
            except Exception as exc:
                log(f"[对接] session.json 收尾失败(不影响归档): {exc}")


def _mark_archive_state(session_dir, state_name, dest=None, target=None, error=None):
    """把归档状态回写到录制历史。

    归档条目是场次日期目录,history 条目是其中的文件模板路径,
    用 dirname(output_path) == session_dir 匹配(同场次可一对多)。
    """
    if not session_dir:
        return
    src = os.path.normpath(session_dir)
    changed = False
    for e in _history:
        p = e.get("output_path") or ""
        if not p or os.path.normpath(os.path.dirname(p)) != src:
            continue
        a = e.get("archive") or {}
        # 已归档是**终态**:不得被 pending / archiving / failed 降级。
        # 成因(2026-09-18 实际发生):队列里残留的同名场次条目(源早已被搬走)
        # 被反复重试,耗尽后回调把整个日期目录的 history 条目从 archived
        # 覆写成 failed —— 素材明明在目标盘上,历史页却显示「归档失败」。
        if a.get("state") == "archived" and state_name != "archived":
            continue
        a["state"] = state_name
        if target is not None:
            a["target"] = target
        if dest is not None:
            a["dest"] = dest
        a["at"] = time.time() if state_name == "archived" else a.get("at")
        a["error"] = error
        e["archive"] = a
        changed = True
    if changed:
        _commit_history()


def _on_archive_result(src_path, state_name, dest=None, target=None, error=None):
    """nas 模块归档回调:更新 history 的归档状态。"""
    _mark_archive_state(src_path, state_name, dest, target, error)


def _media_duration(path):
    """解析媒体真实时长(秒);文件缺失/解析失败返回 None。

    分片模板路径(含 %03d)**不走 ffmpeg**:录制用 `-copyts` + `-reset_timestamps 0`,
    时间戳跨分片连续,所以「末片末帧 − 首片首帧」就是整场媒体跨度 ——
    用项目自己的纯 Python FLV 解析(subtitle_mod),比逐片 `ffmpeg -i` 快得多。
    之前模板直接返回 None,导致 `_history_finish` 回退墙钟差值,历史页显示的
    时长和视频文件对不上(ffmpeg 启动缓冲/断流重试都会拉开差距)。

    ⚠️ 这里**只取原始 tag 值作差**,不走 `*_ts_abs`:跨分片时间戳连续,差值就是
    跨度,与时间戳是"墙钟"还是"源流相对值"无关 —— 后者在 src_timestamps 模式下
    需要 clock_offset 才能还原绝对时间,而时长根本不需要绝对时间。单场远小于
    2^31 ms(24.8 天),差值不会踩到 FLV 的 31 位截断。
    """
    if not path:
        return None
    if _SEG_TAIL in path:
        try:
            # find_segments 返回 [(idx, 路径)],取路径
            segs = [p for _idx, p in subtitle_mod.find_segments(path)]
            if not segs:
                return None
            a = subtitle_mod.flv_first_av_ts(segs[0])
            b = subtitle_mod.flv_last_ts(segs[-1])
        except Exception:
            return None
        if a is not None and b is not None and b > a:
            return (b - a) / 1000.0
        return None
    if not os.path.isfile(path):
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    try:
        r = subprocess.run([ffmpeg, "-i", path], capture_output=True,
                           text=True, timeout=10)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


# ── 实际画质与码率(2026-10-28)──────────────────────────────
# 历史页要能回答「这一场到底录成什么样」。用户配置里的 resolution/bitrate 是
# **目标值**,落盘文件里的才是**事实** —— 自动降级、源本身低清、流复制(copy)
# 不重编码,这三种情况都会让目标值与实际值不一致,只看配置会得出错误结论。
# 所以分辨率读文件本身(FLV 头部 onMetaData,纯 Python,见 subtitle.flv_video_size),
# 码率用「体积 ÷ 媒体时长」实测(含音轨与容器开销,与播放器报的平均码率同义)。
_DIM_CACHE = {}                      # 文件路径 → (w, h);只缓存成功结果
_DIM_CACHE_MAX = 4096


def _video_dims(path):
    """读一个视频文件的真实编码尺寸;非 FLV / 不存在 / 读不到返回 None。

    成功结果按路径缓存:归档后这些文件在 NAS 上,一次 open 就是一趟 SMB,
    而 /api/history 每 5 秒要重算 500 条。**失败不缓存** —— 录制中的首片
    onMetaData 可能还没落盘,缓存空值会让这场永远读不到尺寸。
    """
    if not path:
        return None
    hit = _DIM_CACHE.get(path)
    if hit:
        return hit
    try:
        size = subtitle_mod.flv_video_size(path)
    except Exception:
        size = None
    if not size:
        return None
    dims = (int(size[0]), int(size[1]))
    if len(_DIM_CACHE) >= _DIM_CACHE_MAX:
        _DIM_CACHE.clear()           # 不做 LRU:条目量级有限,一次清空足够
    _DIM_CACHE[path] = dims
    return dims


def _media_paths(output_path, seg=None, tail=True):
    """录制输出路径 → (首片路径, 末片路径);都找不到时返回 (None, None)。

    分片模式优先用 seg(`_segment_stats` 的结果,已经知道末片文件名),
    避免为了取个尺寸再 listdir 一次 —— 归档后那是 NAS 上的一趟目录列举。
    """
    if not output_path:
        return (None, None)
    d = os.path.dirname(output_path)
    base = os.path.basename(output_path)
    if _SEG_TAIL not in base:
        p = output_path if os.path.isfile(output_path) else None
        return (p, p)
    stem, ext = os.path.splitext(base)
    stem = stem[: -len(_SEG_TAIL)]           # "...-20261013-210743-"
    first = os.path.join(d, stem + "000" + (ext or ".flv"))
    if not os.path.isfile(first):
        # 首片被手动删掉/改名(归档侧改名等)→ 退回 glob,由 find_segments 兜底
        try:
            segs = subtitle_mod.find_segments(output_path)
        except Exception:
            segs = []
        if not segs:
            return (None, None)
        return (segs[0][1], segs[-1][1] if tail else segs[0][1])
    last = first
    if tail:
        latest = (seg or {}).get("latest")
        if latest:
            last = os.path.join(d, latest)
        else:
            try:
                segs = subtitle_mod.find_segments(output_path)
            except Exception:
                segs = []
            if segs:
                last = segs[-1][1]
    return (first, last)


def _media_info(output_path, nbytes=None, seconds=None, seg=None, tail=True):
    """历史条目要展示的「实际画质」:分辨率 + 实测平均码率。

    返回 {"w","h","kbps"[,"tail":[w,h]]};文件不在 / 什么都取不到返回 None。
    `tail` 仅在末片分辨率与首片不同时出现(中途自动降级/换档),前端据此提示。

    tail=False 供 webui 的存量补算用:归档后文件在 NAS 上,为一条「途中降级」
    多付一次 SMB open,不值得(降级是新功能,老场次本来就没有降级可看)。
    """
    info = {}
    try:
        first, last = _media_paths(output_path, seg=seg, tail=tail)
    except Exception:
        first = last = None
    if first:
        dims = _video_dims(first)
        if dims:
            info["w"], info["h"] = dims
        if tail and last and last != first:
            t = _video_dims(last)
            if t and t != dims:
                if dims:
                    info["tail"] = [t[0], t[1]]
                else:
                    info["w"], info["h"] = t
    try:
        nb, sec = float(nbytes or 0), float(seconds or 0)
    except (TypeError, ValueError):
        nb = sec = 0.0
    if nb > 0 and sec > 0:
        # 平均码率 = 字节 × 8 ÷ 秒。分母用媒体时长(首帧→末帧),与「体积÷时长」
        # 的直觉一致;ffmpeg 启动缓冲造成的墙钟差值不计入,否则码率会被摊薄。
        info["kbps"] = int(round(nb * 8.0 / sec / 1000.0))
    return info or None


# 转码可用的 x264 preset(白名单)。非法值不能直接透传给 ffmpeg —— 那会让
# 拉流进程**一起来就退出**,表现为「检测到开播却录不到文件」,很难往参数上想。
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow")


def _norm_preset(value):
    """校验 x264 preset;非法/空返回 ""(由 libx264 用自带默认 medium)。"""
    p = str(value or "").strip().lower()
    return p if p in X264_PRESETS else ""


# 编码器路线白名单。vt = 硬件(videotoolbox),sw = libx264,copy = 不重编码。
VIDEO_MODES = ("vt", "sw", "copy")

# 断流补静音(仅 src_timestamps=True 时启用)。
# 只在"真空档"超过 0.5s 时硬补,小抖动一律放过:源时间戳模式下正常时段
# 采样间隔偏差实测 0.44ms,远小于阈值不会被误伤;真断流(网络掉 5s)时补等长
# 静音,让「音频采样数 ÷ 采样率 = 时间戳跨度」,下游不必猜着补。
# ⚠️ 绝不能与 -use_wallclock_as_timestamps 1 同用:那种形态下 PTS 本身被压扁,
#    硬补偿会把**真实采样**当多余丢掉(38 分钟实测丢 ~160s),所以只在关闭墙钟时加。
AUDIO_SYNC_FILTER = "aresample=async=1:min_hard_comp=0.5"


def _norm_bitrate(value):
    """码率配置 → ffmpeg 字符串;0/空 → ""(含义:不重编码)。

    单位**就是 kbps**:纯数字补 k(`1200` → `1200k`),兼容 `"3000k"` / `"3M"` 字符串。
    (沿用 2026-09 起的解析规则,v2 改造只是把它从「决定编码器」降级为「决定码率」。)
    浮点整数值(JSON 里 `1200.0`,或前端数字框提交的 Number)必须收成 `1200k` ——
    直接把 `1200.0` 交给 ffmpeg 会被拒。
    """
    if value in (None, "", 0):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    b = str(value).strip()
    if not b:
        return ""
    try:
        # "0" / "0k" / "0.0" 都是「不重编码」的写法,不能变成 -b:v 0k(ffmpeg 会拒)
        if float(b.rstrip("kKmM")) == 0:
            return ""
    except ValueError:
        pass
    if b.isdigit():
        b += "k"
    return b


def _video_mode(rec_cfg, bitrate_str):
    """解析实际编码路线 vt / sw / copy。

    · 非法/缺失一律回落 **sw**(libx264)—— 非法值透传给 ffmpeg 等于「起来就退出」,
      表现成「检测到开播却录不到文件」,所以这里必须挡;
      缺键回落 sw 而不是 vt 是为了「老配置/合成调用保持原行为」,真实配置由
      load_config 用 DEFAULT_CONFIG 补齐,线上默认仍是 vt。
    · 没有码率只能 copy:重编码必须有目标码率,否则体积/画质都不可控。
    """
    mode = str(rec_cfg.get("video_mode") or "").strip().lower()
    if mode not in VIDEO_MODES:
        mode = "sw"
    return "copy" if not bitrate_str else mode


def _build_ffmpeg_args(ffmpeg_path, stream_url, out_dir, name, fmt, rec_cfg, ts,
                       tier=None, progress_path=None):
    """按 recorder 配置构造 ffmpeg 参数,返回 (args, output_path)。

    三条编码路线(2026-10-22 硬编改造后):
      vt  : `-hwaccel videotoolbox` + `h264_videotoolbox -b:v` (+ `-allow_sw 0`)
      sw  : `libx264 -b:v` (+ `-preset`),老行为,回退用
      copy: `-c copy`,最省 CPU,但没有参数可调(也就没有码率/分辨率)

    分辨率档位(`tier`)对 vt / sw 都生效:缩放**统一走 CPU `scale`**。
    GPU 的 `scale_vt` 已被实测否决(它撑不住滤镜图重配置,源流参数一变就把
    整个进程打死,见 `quality.SCALE_VT_ENABLED` 的注释与崩溃归因报告)。
    **三条铁律在这里固化,任何一条改了都会静默毁掉整条链路**:
      ① 时间基由 `src_timestamps` 决定:True(默认)时**不加**
         `-use_wallclock_as_timestamps`,让 tag 保留源流时间戳(音视频同一坐标系,
         源 ts→墙钟的换算交给 subtitle.clock_offset);只有显式置 False 才加,
         加了会让每个包的 PTS 变成"读到它的墙钟",包成批到达时被压扁、断流处跳秒,
         音轨采样数与视轨脱钩(见 音画不同步-根因分析与修复方案.md)。
         不论哪种形态,该开关都必须在 `-i` **之前**;
      ② `-copyts` 必须在 `-i` 之后;分片 `-reset_timestamps 0`;
      ③ `-hwaccel` 也必须在 `-i` 之前,否则会被当成输出选项 → `Invalid argument`;
         配套的 `-hwaccel_output_format` **只在启用 GPU 缩放时才加** —— 加了它
         却又不用 scale_vt,滤镜图在源参数变化时会重建失败(2026-10-29 定案)。
    """
    bitrate_str = _norm_bitrate(rec_cfg.get("bitrate"))
    mode = _video_mode(rec_cfg, bitrate_str)
    tier = quality_mod.norm_tier(tier if tier is not None
                                 else rec_cfg.get("resolution"))
    hw_decode = bool(rec_cfg.get("hw_decode", True))
    need_scale = bool(quality_mod.scale_expr(tier))
    # 源时间戳模式(默认开)。缺键按 True 处理:线上默认必须是新行为,
    # 老配置也会被 load_config 用 DEFAULT_CONFIG 补齐。
    src_ts = bool(rec_cfg.get("src_timestamps", True))

    # 输入侧选项的顺序原则:wallclock 开关(若启用)**永远紧邻** `-i` 之前
    # (tests/test_ffmpeg_args.py 钉住的不变量),其余输入选项一律排在它前面。
    args = [ffmpeg_path, "-y", "-loglevel", "error"]
    if progress_path:
        # 跟随率数据源:0.5 s 覆盖写一个文件(不用管道,不存在阻塞风险)。
        # 看护/质量调度靠它算「实际输出时间 ÷ 墙钟」,判断这一路跟不跟得上。
        args += ["-progress", progress_path, "-stats_period", "0.5"]
    if mode == "vt" and hw_decode:
        args += ["-hwaccel", "videotoolbox"]
        if need_scale and quality_mod.SCALE_VT_ENABLED:
            # 只有启用 GPU 缩放时才让硬解帧直出:scale_vt 只吃 videotoolbox_vld,
            # 少了这行 → Conversion failed(实测)。
            # 默认(CPU 缩放)**绝不能加**:硬解帧直出会让滤镜图在源参数变化时
            # 重建失败,`Impossible to convert ... auto_scale_0` → 进程退出。
            args += ["-hwaccel_output_format", "videotoolbox_vld"]
    if not src_ts:
        # 回退路径才加墙钟打戳(逐包覆盖 PTS,音画同步的元凶)
        args += ["-use_wallclock_as_timestamps", "1"]
    args += [
        "-i", stream_url,
        # 输出保留绝对时间戳(copyts),分片不重置(跨分片连续)
        "-copyts"]

    if mode == "vt":
        args += ["-c:v", "h264_videotoolbox", "-b:v", bitrate_str]
        if rec_cfg.get("force_hw", True):
            # 硬件不可用时立即失败,而不是静默软编 —— 7 路静默软编会直接拖垮整机
            args += ["-allow_sw", "0"]
        vf = quality_mod.vf_filter(tier, hw=hw_decode)
        if vf:
            args += ["-vf", vf]
        # 硬编没有 preset 概念,`_norm_preset` 在这一支跳过(给了会被拒)
        args += ["-c:a", "aac", "-b:a", "128k"]
        if src_ts:
            args += ["-af", AUDIO_SYNC_FILTER]
    elif mode == "sw":
        args += ["-c:v", "libx264", "-b:v", bitrate_str]
        preset = _norm_preset(rec_cfg.get("preset"))
        if preset:
            args += ["-preset", preset]
        vf = quality_mod.vf_filter(tier, hw=False)
        if vf:
            args += ["-vf", vf]
        args += ["-c:a", "aac", "-b:a", "128k"]
        if src_ts:
            args += ["-af", AUDIO_SYNC_FILTER]
    else:
        # 流复制:音频也原样透传(加 -af 会强制重编码,失去 copy 的意义)。
        # 源时间戳模式下时间戳天生同步,不需要补偿;断流时音视频一同缺内容,
        # PTS 同步跳空,下游按时间戳补即可。
        args += ["-c", "copy"]

    seg = int(rec_cfg.get("segment_time") or 0) or 0
    if seg > 0:
        pattern = os.path.join(out_dir, f"{name}-{ts}-%03d.{fmt}")
        args += ["-f", "segment", "-segment_time", str(seg),
                 "-reset_timestamps", "0", pattern]
        output_path = pattern
    else:
        path = os.path.join(out_dir, f"{name}-{ts}.{fmt}")
        args.append(path)
        output_path = path

    return args, output_path


def _progress_path(name):
    """该主播本轮的 `-progress` 文件(每次起录覆盖写)。

    放 `state/` 而不是 `logs/`:它是**瞬时运行态**(0.5 s 一刷),不是审计材料,
    每天轮转的业务日志里塞这个只会把真正的信息冲掉。
    """
    d = os.path.join(BASE, "state", "progress")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return os.path.join(d, _safe_file_stem(name) + ".progress")


def _read_progress(path):
    """解析 `-progress` 文件 → {"frame": int, "out_us": int};读不到返回 None。

    ⚠️ `out_time_ms` 实际是**微秒**(与 `out_time_us` 同值,ffmpeg 历史命名遗留)。
    用它算「输出时间增量 ÷ 墙钟增量」才是真正的跟随率:跟着跟着落后时,
    out_time 会明显慢于墙钟,而「文件还在长」是看不出来的。
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    frame = out_us = None
    for line in text.replace("\r", "\n").splitlines():
        if line.startswith("frame="):
            try:
                frame = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                out_us = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    if frame is None and out_us is None:
        return None
    return {"frame": frame or 0, "out_us": out_us or 0}



def _safe_file_stem(name):
    """主播名 → 可做文件名的安全串(主播名里可能带全角标点、emoji 等)。"""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]", "_", str(name or ""))
    return (s.strip("._") or "unknown")[:60]


_ERR_START_MARK = "##### ffmpeg start "
# 单主播 err.log 上限。超过就滚动一次(只留一个 .1):
# 崩溃现场要能留几轮做对照,但绝不能像 2026-09-11 那样涨到 7.5MB。
_ERR_LOG_MAX = 2 * 1024 * 1024


def _ffmpeg_err_path(name):
    """本次录制的 ffmpeg stderr 落盘路径(按主播;**追加**写,不覆盖)。

    2026-10-31 从 "w" 改成追加:原先每次启动覆盖,于是一场里连续崩 4 次只剩
    最后一次的 stderr —— 而崩溃归因最需要的恰恰是"同样几条报错repeat了几次、
    中间有没有夹着别的错"。10-29 那次 scale_vt 归因就卡在这里,证据是被自己
    删掉的。读取侧靠 _ERR_START_MARK 切分,语义仍是"本次启动的输出"。
    """
    d = os.path.join(LOG_DIR, "ffmpeg")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return os.path.join(d, _safe_file_stem(name) + ".err.log")


def _rotate_err_log(path):
    """err.log 超过 _ERR_LOG_MAX 就滚动成 `.1`(覆盖旧的 .1)。失败不影响录制。"""
    try:
        if os.path.getsize(path) > _ERR_LOG_MAX:
            backup = path + ".1"
            try:
                os.remove(backup)
            except OSError:
                pass
            os.replace(path, backup)
    except OSError:
        pass


def _ffmpeg_err_tail(path, lines=6):
    """读 ffmpeg stderr 文件的末尾几行,拼成一行;读不到返回 ''。

    err.log 是追加写的,先定位**最后一次启动**的标记、只取它之后的内容:
    否则「起来了但一句话没说就退出」这一轮会把上一次的报错当成自己的归因,
    归因一错,画质调度就会照着 404 去降分辨率。
    """
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return ""
    rows = [r.strip() for r in content.splitlines() if r.strip()]
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].startswith(_ERR_START_MARK):
            rows = rows[i + 1:]
            break
    return " | ".join(rows[-lines:])


def _log_ffmpeg_exit(name, rec):
    """把 ffmpeg 的最后几行报错记进业务日志。

    为什么必须记:ffmpeg 的 stderr 原先被丢进 DEVNULL,于是「起来 20 秒就退出」
    在日志里只剩一句「已退出」,看不出任何原因 —— 2026-09-18 排查某主播时,
    只能手动用 detect 拿流地址再跑一遍 ffmpeg,才看到那条 `404 Not Found`
    (根因是已下播时 reflow 页仍带一个占位流地址)。
    留存最后几行的成本极低,但下一次同类故障能直接从日志定位。
    """
    tail = _ffmpeg_err_tail(rec.get("err_path"))
    if tail:
        log(f"[录制] {name} ffmpeg 最后输出: {tail[:400]}")


def _exit_cause(rec):
    """按 ffmpeg stderr 尾部给这次退出归因,供画质调度区分「资源」与「流侧」。

    err.log 是追加写的,`_ffmpeg_err_tail` 会自动切到**本次启动**那一段,所以
    必须在本轮重启之前读 —— 重启之后就多了一个启动标记,拿到的就是下一轮了。
    """
    try:
        return quality_mod.classify_exit(_ffmpeg_err_tail((rec or {}).get("err_path")))
    except Exception:
        return quality_mod.CRASH_UNKNOWN


def _hw_probe(ffmpeg_path=None, force=False):
    """硬件编码能力探针(结果缓存)。返回 (ok, reason)。

    `h264_videotoolbox` 未编入的 ffmpeg、或在非 Apple 机器上跑,都会让硬编路线
    起不来。**必须在起录前判断**,否则表现为「开播了却录不到文件」——
    与 preset 透传那个坑同型。探不到就整体回 copy:宁可变大,不能丢素材。
    """
    global _hw_ok, _hw_reason
    if _hw_ok is not None and not force:
        return _hw_ok, _hw_reason
    try:
        from preflight import probe_hw_encoder
        ok, reason = probe_hw_encoder(ffmpeg_path or find_ffmpeg())
    except Exception as e:                       # 探针自身异常不该阻断录制
        ok, reason = True, f"探针异常(按可用处理): {e}"
    _hw_ok, _hw_reason = bool(ok), str(reason or "")
    return _hw_ok, _hw_reason


def _active_level():
    """当前生效的降级档位(0/1/2)。质量调度没起来时一律 L0。"""
    gov = _quality_gov
    return int(getattr(gov, "level", 0) or 0) if gov else 0


def _active_encode(cfg, name=None):
    """当前该用哪一组编码参数 → (tier, bitrate, mode, level, why)。

    level 由质量调度给出;准入控制(在录路数 ≥ max_hw_streams)与硬编可用性
    在这一层决定要不要退化成 copy —— 这两条都会明确写进返回值,便于日志与
    前端 `/api/status.recordings[<主播>].mode` 暴露,不让它静默发生。
    """
    rec_cfg = cfg.get("recorder") or {}
    level = _active_level()
    tier_cfg = quality_mod.level_tier({"recorder": rec_cfg}, level)
    tier = int(tier_cfg["resolution"] or 0)
    bitrate = tier_cfg["bitrate"]
    mode = _video_mode(rec_cfg, _norm_bitrate(bitrate))
    why = ""
    if mode == "vt":
        ok, reason = _hw_probe()
        if not ok:
            mode, why = "copy", f"硬件编码不可用({reason})→ 回落流复制"
        else:
            cap = int(rec_cfg.get("max_hw_streams") or 8)
            active = _hw_stream_count(exclude=name)
            if active >= cap:
                mode = "copy"
                why = f"在录 {active} 路已到准入上限 {cap} → 本路改流复制"
    return tier, bitrate, mode, level, why


def _hw_stream_count(exclude=None):
    """在录的硬编路数(准入控制用)。copy 路不占引擎,故只数 vt 的。"""
    n = 0
    for nm, rec in list(_recordings.items()):
        if exclude and nm == exclude:
            continue
        if (rec or {}).get("video_mode") == "vt" and not (
                rec.get("proc") is not None and rec["proc"].poll() is not None):
            n += 1
    return n


def _learn_src_size(name, output_path, level=None):
    """从首个分片的 FLV onMetaData 学源分辨率(拿到了就记住,不再重复解析)。

    只用于引擎需求估算:`quality.pixels()` 需要「原生档到底多大」。
    缩放本身不依赖它 —— 由 `scale` 表达式在滤镜内保证「只缩不放」。

    **只在 L0(不缩放)档学**:分片里写的是**编码后**的尺寸,降档期间录出来的
    720×1280 根本不是源。把它当源会让 `gains()` 认为「降到 720 一个像素都没
    省」——2026-10-27 实测:`reason` 显示「L1 引擎 −0%」,而两路竖屏实际是
    1080×1920 → 720×1280、省了 55.6%。学不到时宁可不记,让 `ASSUMED_SRC`
    兜底,也不能记一个被缩放过的值。
    """
    if name in _src_size or not output_path:
        return _src_size.get(name)
    if level:
        return None
    try:
        path = output_path
        if "%03d" in path:
            import glob as _glob
            hits = sorted(_glob.glob(path.replace("%03d", "[0-9][0-9][0-9]")))
            if not hits:
                return None
            path = hits[0]
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return None
        size = subtitle_mod.flv_video_size(path)
        if size:
            _src_size[name] = (int(size[0]), int(size[1]))
        return _src_size.get(name)
    except Exception:
        return None


def _start_recording(state, name, stream_url, cfg, room_id=None):
    """用 ffmpeg 从 stream_url 拉流录制到 output_dir/name/。返回 proc 或 None。"""
    if not stream_url:
        return None
    rec_cfg = cfg.get("recorder") or {}
    if not rec_cfg.get("enabled", True):
        return None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log(f"[录制] {name} 未找到 ffmpeg,无法录制(请 brew install ffmpeg)")
        return None
    # 本机暂存目录与归档层级一致:<暂存根>/<主播>/<YYYY-MM-DD>/(场次归属=开播日)
    # 走 _session_out_dir 而不是每次重算:本场内(含崩溃/换房间/降档重启)
    # 目录必须钉死,否则中途改配置会把一场拆到两个暂存根。
    out_dir = _session_out_dir(name, cfg)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        # 暂存盘没插 / 路径写错时必须显式失败:**绝不回落到项目内**(那正是
        # 这次要根除的行为),也绝不静默留一行日志就返回 —— 前端会显示
        # 「开播却不录」,与本函数返回 None 的其他分支无法区分。
        log(f"[录制][!] {name} 无法创建暂存目录 {out_dir}: {e}")
        incidents_mod.record(
            "record.nodir", f"{name} 暂存目录不可写,本场无法录制",
            detail=f"{out_dir} · {e}", name=name, level=incidents_mod.LEVEL_ERROR)
        _session_dir_pin.pop(name, None)     # 目录都没建成,别把坏路径钉死
        return None

    fmt = rec_cfg.get("format", "flv") or "flv"
    ts = time.strftime("%Y%m%d-%H%M%S")
    # 档位 / 码率 / 编码路线:由质量调度档位 + 准入控制共同决定(见 _active_encode)
    tier, bitrate, mode, level, why = _active_encode(cfg, name)
    use_cfg = dict(rec_cfg)
    use_cfg["video_mode"] = mode
    use_cfg["bitrate"] = bitrate
    progress = _progress_path(name)
    args, path = _build_ffmpeg_args(ffmpeg, stream_url, out_dir, name, fmt, use_cfg, ts,
                                    tier=tier, progress_path=progress)
    if why:
        log(f"[录制] {name} {why}")
    # ffmpeg 的报错不再丢进 DEVNULL:写进 logs/ffmpeg/<主播>.err.log,
    # 退出时由 _log_ffmpeg_exit 摘最后几行进业务日志(见该函数说明)。
    # 追加 + 启动分隔标记:一场里反复退出时,每轮现场都留着,便于横向对照。
    err_path = _ffmpeg_err_path(name)
    errf = None
    try:
        if err_path:
            _rotate_err_log(err_path)
            errf = open(err_path, "a", encoding="utf-8", errors="replace")
            errf.write("%s%s tier=%s bitrate=%s mode=%s\n" % (
                _ERR_START_MARK, time.strftime("%Y-%m-%d %H:%M:%S"),
                tier, bitrate, mode))
            errf.flush()
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=errf or subprocess.DEVNULL)
    except Exception as e:
        log(f"[录制] {name} 启动失败: {e}")
        if _quality_gov:
            _quality_gov.note_crash("start_fail:%s" % name)
        return None
    finally:
        # 子进程已继承 fd,父进程这份可以立刻关(否则句柄泄漏)
        if errf:
            errf.close()
    started_at = time.time()
    _recordings[name] = {"proc": proc, "started_at": started_at,
                         "output_path": path, "stream_url": stream_url, "format": fmt,
                         "room_id": room_id, "err_path": err_path,
                         # 本场暂存日期目录(绝对路径)。显式存一份:很多下游
                         # (归档入队/弹幕 retarget/字幕收尾)要 dirname(output_path),
                         # 暂存根可切换后,反推不再等价于「当前配置的根」。
                         "out_dir": out_dir,
                         # 本轮实际生效的编码参数(重启/切档判定与前端展示都用它)
                         "video_mode": mode, "resolution": tier,
                         "bitrate": bitrate, "quality_level": level,
                         "progress_path": progress, "follow": 1.0}
    # 本场起点:restart 保留(同一场),下播/换房间后另起新值
    _session_start.setdefault(name, started_at)
    log(f"[录制] {name} 开始: {path}"
        + (f" (档位 L{level} · {quality_mod.TIERS.get(tier, tier)} · "
           f"{quality_mod.fmt_int(bitrate) if bitrate else 'copy'} kbps · {mode})"
           if mode != "copy" else " (流复制)"))
    _sync_recording_state(state, name)
    # S2 契约版本随数据走:日期目录 session.json 落 recording 态(旁路,失败仅日志)
    try:
        base = os.path.basename(path)
        stem = base.split("-%03d.")[0] if "-%03d." in base else os.path.splitext(base)[0]
        interop_mod.write_session_state(out_dir, "recording", [stem])
    except Exception as exc:
        log(f"[对接] session.json 写入失败(不影响录制): {exc}")
    # 延迟入史:不立即写 history,由看护循环在首次巡检(20s)时确认进程
    # 仍活着才补记 —— 404 秒退这类「起录即死」的尝试就不产生历史条目了。
    # (真实短直播 <20s 也不会入史;这么短的场次本来就无内容可看。)
    _recordings[name]["history_pending"] = True
    return proc


_SEG_TAIL = "%03d"          # 分片模板的序号占位(见 _build_ffmpeg_args)
_rec_carry = {}             # name -> {"segments": int, "bytes": int} 重启前已完成的段
_session_start = {}         # name -> 本场第一次起录的时间戳(give_up 回收残片时界定"本场")

# give_up 后的残片回收阈值:低于任一即视作「断流残渣」(没有可看的内容)。
# 数据停滞场景录了很久的正常条目(时长长/体积大)不受影响;
# 真实短直播不经过 give_up(正常下播),也不会被碰。
_JUNK_MAX_DURATION = 90.0        # 秒
_JUNK_MIN_BYTES = 1_048_576      # 1 MB


def _discard_junk_segments(state, name, session_start=None):
    """give_up 后清理「本场从未建立稳定流」留下的残渣条目与文件。

    成因(2026-09-21 排查):房间还挂着「直播中」但推流已被 CDN 切断,
    复检判在播 → 起录 → 约 1 分钟流死退出(`Stream ends prematurely`)→
    反复重启 4 次 → give_up。每次重启都 `_history_finish` + `_history_add`,
    一场失败循环就留下 4-5 条 ~1 分钟的历史条目和几十 MB 的残片文件 ——
    用户看到的就是「历史里总有多出 20 秒/1 分钟的视频残留」。

    只删同时满足的条目:属于本场 + (时长 < 90s 或 体积 < 1MB)。
    「或」是有意的:停滞场景空转产生的 0 字节分片墙钟可能超 90s,靠体积兜住;
    反复退出场景每条墙钟可能 80s 出头,靠时长兜住。
    `session_start` 由调用方在 `_stop_recording` **之前**取好传进来 ——
    stop 会清掉 `_session_start`,顺序反了就永远删不到东西。
    """
    start = session_start if session_start is not None else _session_start.get(name)
    if start is None:
        return
    keep, removed = [], []
    for e in _history:
        if (e.get("name") == name and e.get("status") == "done"
                and (e.get("started_at") or 0) >= start):
            dur = e.get("duration") or 0
            size = e.get("size") or 0
            if dur < _JUNK_MAX_DURATION or size < _JUNK_MIN_BYTES:
                removed.append(e)
                continue
        keep.append(e)
    if not removed:
        return
    for e in removed:
        _remove_session_files(e.get("output_path"))
    _history[:] = keep
    _commit_history()
    log(f"[录制] {name} 已清理 {len(removed)} 条断流残片记录"
        f"(本场反复退出,未能建立稳定流,文件一并移除)")


def _remove_session_files(template):
    """删掉一个场次(模板路径)的全部产物:分片 .flv / 同前缀 .ass / .meta 明细。"""
    if not template:
        return
    d = os.path.dirname(template)
    base = os.path.basename(template)
    if _SEG_TAIL in base:
        prefix = base.split(_SEG_TAIL)[0]
        ext = os.path.splitext(base)[1] or ".flv"
        pats = [os.path.join(d, prefix + "*" + ext),
                os.path.join(d, prefix + "*.ass"),
                os.path.join(d, ".meta", prefix + "*")]
    else:
        stem = os.path.splitext(base)[0]
        pats = [template, os.path.splitext(template)[0] + ".ass",
                os.path.join(d, ".meta", stem + ".*")]
    for pat in pats:
        for p in glob.glob(pat):
            try:
                os.remove(p)
            except OSError:
                pass


def _segment_stats(output_path):
    """统计当前这一路 ffmpeg 已落盘的分片。非分片模式返回 None。

    分片文件名形如 `<主播>-<YYYYmmdd-HHMMSS>-%03d.flv`(_build_ffmpeg_args)。
    匹配办法:把模板里的 `%03d` 去掉当作前缀,认 `-000/-001/...` 这些实际文件。

    注意**重开一场会用新的时间戳前缀**(ffmpeg 重启 → 新 ts),所以这里只统计
    「当前前缀」的段数;旧前缀的段数在 `_restart_recording` 里累加进 `_rec_carry`,
    界面上看到的 `segments` 是**本场总数**,不会因为重启而归零。
    """
    if not output_path or _SEG_TAIL not in os.path.basename(output_path):
        return None
    stem, ext = os.path.splitext(os.path.basename(output_path))
    if not stem.endswith(_SEG_TAIL):
        return None
    prefix = stem[: -len(_SEG_TAIL)]
    d = os.path.dirname(output_path)
    try:
        names = os.listdir(d)
    except OSError:
        return None
    pat = re.compile("^" + re.escape(prefix) + r"(\d{3,})" + re.escape(ext) + "$")
    total = n = 0
    top = (-1, None, 0)                      # (序号, 文件名, 字节)
    for fn in names:
        m = pat.match(fn)
        if not m:
            continue
        try:
            sz = os.path.getsize(os.path.join(d, fn))
        except OSError:
            continue
        n += 1
        total += sz
        idx = int(m.group(1))
        if idx > top[0]:
            top = (idx, fn, sz)
    if not n:
        return {"count": 0, "bytes": 0, "index": 0, "latest": None, "latest_bytes": 0}
    return {"count": n, "bytes": total, "index": top[0],
            "latest": top[1], "latest_bytes": top[2]}


def _carry_segments(name):
    """把「当前前缀已完成的段」累加进本场计数(ffmpeg 重启换新前缀前调用)。"""
    rec = _recordings.get(name)
    if not rec:
        return
    s = _segment_stats(rec.get("output_path") or "")
    if not s or not s["count"]:
        return
    c = _rec_carry.setdefault(name, {"segments": 0, "bytes": 0})
    c["segments"] += s["count"]
    c["bytes"] += s["bytes"]


def _sync_recording_state(state, name, stalled=False):
    rec = _recordings.get(name)
    if not rec:
        return
    # 录制中:时长一律用墙钟差值(与前端实时计算/历史页公式一致)。
    # 不再用 ffmpeg 解析正在写入的文件——媒体时长 < 墙钟(ffmpeg 启动/缓冲延迟),
    # 会导致主播页与历史页时长对不上;真实媒体时长仅在录制结束时(_history_finish)解析。
    h = _rec_health.get(name) or {}
    snap = {
        "recording": True,
        "started_at": rec["started_at"],
        "duration": round(time.time() - rec["started_at"], 1),
        "output_path": rec["output_path"],
        "stream_url": rec["stream_url"],
        "format": rec["format"],
        # 本轮实际生效的编码参数:回退链走到哪一段、现在是不是降级档,界面上要看得见
        # (静默回落是这套系统最危险的失败模式 —— 看着在录,其实参数不对)
        "mode": rec.get("video_mode") or "copy",
        "resolution": rec.get("resolution", 0),
        "bitrate": rec.get("bitrate"),
        "quality_level": rec.get("quality_level", 0),
        "follow": round(float(rec.get("follow") or 1.0), 3),
        # 健康度:文件增长停滞(流已断但 ffmpeg 未退出)= 假在线,前端据此告警
        "stalled": bool(stalled),
        "stall_seconds": round(max(0.0, time.time() - h.get("ts", time.time())), 1),
        "restarts": int(h.get("restarts") or 0),
    }
    # 礼物静默告警:带上本场当前的结论(前端据此标红/标橙)。
    # silent_min 用触发时刻外推刷新 —— 不能直接改 _gift_alerts 里的 at,
    # 那个时间戳是冷却去重的依据,动了会导致同一场反复打扰。
    ga = _gift_alerts.get(name)
    if ga:
        snap["gift_alert"] = dict(
            ga, silent_min=round(ga["silent_min"] + (time.time() - ga["at"]) / 60, 1))
    # 分片进度:长视频按 segment_time 切成多段,界面要能看到「已经分了几段」。
    # 只在分片模式下给 —— 单文件录制没有"段"的概念,给了反而误导。
    # 段数含重启前已完成的段(_rec_carry),所以是本场总数,不会因重开而归零。
    seg = _segment_stats(rec["output_path"])
    if seg is not None:
        carry = _rec_carry.get(name) or {}
        snap["segments"] = seg["count"] + int(carry.get("segments") or 0)
        snap["segments_bytes"] = seg["bytes"] + int(carry.get("bytes") or 0)
        snap["segment_index"] = seg["index"]
        snap["segment_file"] = seg["latest"]
        snap["segment_bytes"] = seg["latest_bytes"]
    state.set_recording(name, snap)


def _stop_recording(state, name):
    rec = _recordings.pop(name, None)
    _rec_health.pop(name, None)
    _rec_carry.pop(name, None)               # 本场结束,段数结转清零
    _session_start.pop(name, None)           # 本场结束,下次起录另起新场
    # 本场收尾(下播 / 手动停 / 放弃)同时结束流中断 episode:
    # 留着它会让下一次开播继承上一场的退避节奏与失败计数。
    _clear_stream_wait(name)
    # 本场结束 ⇒ 解除暂存目录钉死:下次起录才会读**当前**的 recorder.output_dir。
    # 这也是 next_session 策略的生效点(在录时改目录 → 本场不动 → 这里解锁)。
    _session_dir_pin.pop(name, None)
    if not rec:
        return
    try:
        rec["proc"].terminate()
        try:
            rec["proc"].wait(timeout=5)  # 等 ffmpeg flush 落盘
        except subprocess.TimeoutExpired:
            try:
                rec["proc"].kill()
            except Exception:
                pass
    except Exception:
        pass
    log(f"[录制] {name} 停止: {rec['output_path']}")
    # 弹幕字幕收尾:录制结束即固化本前缀的分片锚点与 .ass(弹幕会话随直播延续)
    if (state.get_config().get("danmaku") or {}).get("enabled", False):
        try:
            dm = danmaku_mod.get(name)
            subtitle_mod.finalize(rec["output_path"], state.get_config(),
                                  dm.clock_drift_ms if dm else 0.0)
        except Exception as e:
            log(f"[弹幕] {name} 字幕收尾失败: {e}")
    _history_finish(state, name)
    state.remove_recording(name)


def _stop_recording_manual(state, name):
    """手动停止录制:终止 ffmpeg 并标记暂停,主播直播中也不再自动重录。"""
    _manual_paused.add(name)
    _stop_recording(state, name)
    log(f"[录制] {name} 手动停止(暂停自动录制)")


def _start_recording_manual(state, name):
    """手动恢复录制:清除暂停标记与冷却,用缓存的最新流地址立即启动。返回是否成功。"""
    _manual_paused.discard(name)
    _clear_giveup(name)
    url = _last_stream_url.get(name)
    if not url:
        log(f"[录制] {name} 手动开始失败:暂无缓存流地址")
        return False
    _start_recording(state, name, url, state.get_config())
    return True


def _preview_url_provider(name):
    """实时预览的流地址提供者:仅当该主播正在录制且拉流进程存活时返回地址。

    预览(按需独立第二路拉流)依赖录制在场:未录制 → None → 预览会话结束。
    录制 watchdog 重启的瞬时空窗由 live_preview 的宽限期(NONE_GRACE)覆盖。
    """
    rec = _recordings.get(name)
    if not rec:
        return None
    proc = rec.get("proc")
    if proc is not None and proc.poll() is not None:
        return None
    return rec.get("stream_url") or None


_danmaku_paths = {}  # name -> 当前录制输出路径(弹幕会话跟踪,前缀变化时切换 jsonl)


def _ensure_danmaku(state, name, room_id, cfg):
    """直播中确保弹幕会话在跑(配置开启且拿到 room_id)。幂等,失败不影响录制。"""
    if not (cfg.get("danmaku") or {}).get("enabled", False) or not room_id:
        return
    rec = _recordings.get(name)
    out = (rec or {}).get("output_path") or _danmaku_paths.get(name)
    if not out:
        return
    _danmaku_paths[name] = out
    if danmaku_mod.get(name) is None:
        danmaku_mod.start_for(name, room_id, out, cfg)
    else:
        danmaku_mod.retarget_for(name, out)


def _stop_danmaku(name, cfg=None):
    """下播/移除主播时停止弹幕会话,并补一次字幕收尾。

    _stop_recording 的 finalize 可能早于 WS 停止,期间到达的最后几条弹幕
    会漏进 jsonl 但不在 .ass 里;此处先停 WS 再重生成一次(幂等)。"""
    out = _danmaku_paths.pop(name, None)
    rec = danmaku_mod.stop_for(name)
    if rec is not None and out and cfg and (cfg.get("danmaku") or {}).get("enabled", False):
        try:
            subtitle_mod.finalize(out, cfg, rec.clock_drift_ms)
        except Exception as e:
            log(f"[弹幕] {name} 停止后收尾失败: {e}")


_VIDEO_EXTS = (".flv", ".mp4", ".ts", ".mkv", ".mov", ".part")


def _rec_output_size(path):
    """当前录制输出已落盘字节数(分片模式取该前缀全部分片之和)。

    只统计视频类文件,排除 .ass/.jsonl 等随行文件(它们由弹幕模块独立写入,
    增长与否不能代表视频流是否正常)。目录不可读返回 None。
    """
    if not path:
        return None
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    stem = base.split("-%03d.")[0] if "-%03d." in base else os.path.splitext(base)[0]
    if not stem:
        return None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    total = 0
    for fn in names:
        if not fn.startswith(stem) or not fn.lower().endswith(_VIDEO_EXTS):
            continue
        try:
            total += os.path.getsize(os.path.join(d, fn))
        except OSError:
            pass
    return total


def _clear_giveup(name):
    """解除某主播的录制冷却(连原因一起清),避免界面残留陈旧的暂停说明。"""
    _rec_giveup_until.pop(name, None)
    _rec_giveup_reason.pop(name, None)


def _url_fingerprint(url):
    """拉流地址指纹 —— 只回答一个问题:这是不是**同一条**地址。

    为什么不能直接比字符串:地址里带 `t_id`(生成时刻)之类的每次请求都可能变,
    而真正决定「这条流还能不能拉」的是 stream id + 签名参数。也不能只看
    stream id:同一场直播重签后 id 不变、签名变,那是**新**地址,值得立刻重试。

    取不到已知参数(极少数裸路径地址)时退化成去掉 query 的路径,宁可粗一点,
    也不要因为指纹恒为空而把「同一条地址反复失败」误判成「换了新地址」。
    """
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        query = urllib.parse.urlsplit(s).query
        got = urllib.parse.parse_qs(query)
    except ValueError:
        return s
    parts = []
    for k in _FP_KEYS:
        v = got.get(k)
        if v and str(v[0]).strip():
            parts.append("%s=%s" % (k, v[0]))
    return "|".join(parts) if parts else s.split("?", 1)[0]


def _stream_backoff(fails):
    """流中断 episode 内第 n 次重试应等多久(秒)。超出表长按最后一档。"""
    try:
        i = max(1, int(fails)) - 1
    except (TypeError, ValueError):
        i = 0
    return _STREAM_BACKOFF[min(i, len(_STREAM_BACKOFF) - 1)]


def _clear_stream_wait(name):
    """结束流中断 episode(恢复成功 / 确认下播 / 已放弃本场)。"""
    _rec_stream_wait.pop(name, None)


def _give_up(state, name, cooldown, reason):
    """停止本场录制并进入冷却:短时间内反复救不回来就不再空转。

    冷却期内 _refresh_recording 直接返回,避免每轮巡检都重开一个空文件
    刷出一堆零字节历史条目;主播下播(或下次真正开播)时由检测循环清除。

    ⚠️ 冷却**不是**"end"。`_stop_recording` 会把主播从 `_recordings` 里摘掉,
    于是 `_recording_watchdog` 的上半段(只遍历 `_recordings`)再也看不到它 ——
    冷却到期后必须由看护循环的下半段(遍历"判定在播却未录制")主动救回,
    否则要等下一次检测,而 live_interval 是 1200s ⇒ 最长白等 20 分钟。
    """
    log(f"[录制][!] {name} {reason},停止本场录制并冷却 {cooldown}s"
        f"(请检查流地址/网络)")
    # 最严重的一类:这一场的后续内容彻底不录了,等冷却到期才可能救回。
    # 它原先只落一行日志,界面上完全不可见(historically 连"本场已重启 N 次"
    # 都只在 stalled 分支里显示)—— 必须进告警条。
    try:
        incidents_mod.record(
            "record.giveup", "%s 放弃本场录制(%s)" % (name, reason),
            detail="冷却 %ds 内不再尝试;断流残片已回收" % cooldown,
            name=name, level=incidents_mod.LEVEL_ERROR)
    except Exception:                           # noqa: BLE001
        pass
    session_start = _session_start.get(name)   # stop 会清掉,先取
    _stop_recording(state, name)               # 内部会 _history_finish(最后一条)
    _discard_junk_segments(state, name, session_start)   # 残渣回收(文件+条目)
    _rec_health.pop(name, None)
    _session_start.pop(name, None)           # 本场结束,下次起录另起新场
    _rec_giveup_until[name] = time.time() + cooldown
    _rec_giveup_reason[name] = reason
    _anomaly_probe_at.pop(name, None)   # 新一轮异常,复检节流重新计时
    # 流能被 CDN 提前切断,房间状态多半也变了(下播/重开)。主动请求一次复检:
    # 若判下播 → 状态立刻纠正,「判定在播却未录制」的抢救循环不会再起,
    # 而不是等 live_interval(1200s) 才发现。冷却闸仍在,复检不会立刻重开录制。
    try:
        state.request_refresh(name)
    except Exception:
        pass


def _restart_recording(state, name, stream_url, cfg, room_id, reason="", count_it=True,
                       force_new_dir=False):
    """停掉当前 ffmpeg 并重开,累计重启次数用于止损。

    先 _history_finish 关闭旧条目(保留已录到的内容),避免孤儿 recording。
    `count_it=False` 用于「档位切换」这类主动重启:那不是故障,不该消耗
    「反复失败就放弃本场」的预算,否则一次降档就能把可用次数吃掉一格。
    `force_new_dir=True` 只给「在录切换暂存目录」用:解钉后按新配置重开,
    本场就此切成两段(旧半场由调用方结算并归档)。
    """
    if force_new_dir:
        _session_dir_pin.pop(name, None)
    h = _rec_health.get(name) or {}
    restarts = int(h.get("restarts") or 0) + (1 if count_it else 0)
    cause = ""
    if count_it:
        # 退出归因必须在 _recordings.pop 之前取:err.log 是**本次启动**的输出,
        # 一旦重开就指向下一轮了。
        cause = _exit_cause(_recordings.get(name))
        if _quality_gov:
            # 崩溃信号:只有**本地资源类**才算「并发过多 / 引擎不够」的旁证。
            # 流地址失效(404/403)引发的退出一天几十次,算进去会让降档变成常态,
            # 更致命的是它会把「崩溃后 30 min 不恢复」的闸门无限推后 ——
            # 2026-10-27 就是这样卡在 720P 两小时二十分。
            _quality_gov.note_crash("restart:%s" % name, cause=cause)
    # 重开会换新的时间戳前缀,先把旧前缀已完成的段数结转,界面上的"本场段数"才连续
    _carry_segments(name)
    _recordings.pop(name, None)
    _history_finish(state, name)
    _start_recording(state, name, stream_url, cfg, room_id)
    _rec_health[name] = {"size": 0, "ts": time.time(), "restarts": restarts,
                         "room_id": room_id, "stalled": False}
    # 立刻把弹幕输出切到新前缀(2026-10-29 修复)。
    # 原先 retarget 只挂在 danmaku_patrol_loop(60s 一轮)里,于是 ffmpeg 崩溃
    # 重建到切换 jsonl 之间最长 60 秒的新事件仍写进**上一场**的文件:那些行
    # 既不在上一场的结算里(结算早已完成),也不在本场的文件里 —— 实测某主播
    # 19:05:11 换场、19:06:03 才切,期间 41 条轻礼物成了孤儿。
    # 这里重开 ffmpeg 后立即切一次,把窗口从「一个巡检周期」压到毫秒级。
    try:
        rec = _recordings.get(name)
        if rec and danmaku_mod.get(name) is not None:
            danmaku_mod.retarget_for(name, rec["output_path"])
            _danmaku_paths[name] = rec["output_path"]
    except Exception as exc:                    # noqa: BLE001
        # 切不动不能影响录制:巡检仍会在下一个 60s 周期兜住
        log(f"[弹幕] {name} 换场切换输出失败(不影响录制,巡检兜底): {exc}")
    log(f"[录制] {name} 已重启({reason or '未知原因'}),本场累计 {restarts} 次")
    try:
        if count_it:
            # 崩溃事件:重启这段窗口里的内容已经丢了,而健康面板**完全看不出来** ——
            # ffmpeg 是同一秒被拉起来的,心跳没断、文件也没停(2026-10-29 那 86 次
            # 崩溃就是这么隐形的)。事件流是唯一能留下痕迹的地方。
            #
            # 但流侧退出不在这里报红(2026-10-31):404 / Stream ends prematurely
            # 是主播下播或推流中断,不是我们的故障,而且抖音翻状态有 24~70s 延迟,
            # 退出当下根本分不清是哪一种。按 error 记等于把每天的正常下播都刷成
            # 红色 —— 用户看到的就是"天天在崩"。留 INFO 痕迹,既不隐形也不误报;
            # 真正需要人看的"在播却连续拉不到流"由 _await_stream 的等流窗升级。
            if cause == quality_mod.CRASH_EXTERNAL:
                incidents_mod.record(
                    "record.streamend", "%s 流中断(%s)" % (name, reason or "流侧"),
                    detail="归因 %s · 本场第 %d 次重启" % (
                        quality_mod.crash_cause_text(cause), restarts),
                    name=name, level=incidents_mod.LEVEL_INFO)
            else:
                incidents_mod.record(
                    "record.crash", "%s 录制中断并重启(%s)" % (name, reason or "未知原因"),
                    detail="归因 %s · 本场第 %d 次" % (quality_mod.crash_cause_text(cause),
                                                      restarts),
                    name=name, level=incidents_mod.LEVEL_ERROR)
        else:
            # 主动重启(换房间 / 档位切换)不是故障,但「这一路被重启过」必须留痕 ——
            # 事后查「这段为什么多了一个分片 / 为什么画质变了」时,这是唯一线索。
            # 档位切换记 warn:那是画质调度在自我保护,说明机器有压力;
            # 换房间记 info:纯粹是主播换了新的一场,属于正常生命周期。
            incidents_mod.record(
                "record.restart", "%s 录制重启(%s)" % (name, reason or "主动重启"),
                detail="主动重启,不消耗止损次数(本场第 %d 次重启)" % restarts,
                name=name,
                level=(incidents_mod.LEVEL_WARN if "档位" in (reason or "")
                       else incidents_mod.LEVEL_INFO))
    except Exception:                           # noqa: BLE001
        pass                                    # 记事件失败绝不影响录制


# --- 录制暂存目录切换(在录可切) ---
#
# 需求(2026-10-30):设置里能改「默认录制暂存目录」,并且**录制进行中**改完,
# 后续产生的暂存文件就写到新目录。两条落地路径:
#   · next_session(默认):在录的场次一动不动,本场结束(_stop_recording 解钉)
#     后下一场自然落新目录。零风险,一场仍是「一个日期目录」。
#   · now:逐路受控重启切过去 —— 本场被切成两段(两条历史、两次归档)。
#     这是唯一能在运行中真正换目录的方式:ffmpeg `-f segment` 的输出模板
#     是**启动参数**,运行中无法重定向;用符号链接转发倒是能不动进程,但一场
#     会跨两个物理目录,直接击穿「一场=一个日期目录=原子搬运」的归档不变式。

def _stage_dir_writable(path):
    """目录可建且可写?返回 (ok, err)。会顺手把目录建出来。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return False, f"无法创建目录:{e}"
    probe = os.path.join(path, ".douyin-write-probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return False, f"目录不可写:{e}"
    return True, None


def stage_switch_snapshot(cfg):
    """暂存目录现状(给 /api/status.storage 与设置页展示)。"""
    eff = _resolve_output_dir(cfg)
    used, total = 0, 0
    try:
        u = shutil.disk_usage(eff)
        used, total = u.used, u.total
    except Exception:                           # noqa: BLE001
        pass
    pend = _stage_switch
    return {"effective_dir": eff,
            "default_dir": DEFAULT_STAGE_DIR,
            "configured": ((cfg.get("recorder") or {}).get("output_dir") or ""),
            "switch_policy": ((cfg.get("recorder") or {}).get("output_dir_switch")
                              or "next_session"),
            "in_project": _under_project(eff),
            "meta_dir": RECORD_DIR,
            "pending": dict(pend) if pend else None,
            "active_dirs": sorted({r.get("out_dir") or os.path.dirname(
                r.get("output_path") or "") for r in _recordings.values()} - {""}),
            "disk": {"used": used, "total": total,
                     "free": max(0, total - used) if total else 0}}


def set_stage_dir(state, new_dir, now=False):
    """切换录制暂存根。返回 {ok, error, switched, pending, effective}。

    `now=False`(默认 next_session):只记 pending,在录场次不动。
    `now=True`:逐路 `_restart_recording(force_new_dir=True, count_it=False)`,
    旧半场就地结算并入队归档(此时该目录已无进程在写,「在录不归档」闸门
    不会误伤 —— 它只看 _recordings 的**当前**路径)。
    """
    global _stage_switch
    raw = (new_dir or "").strip()
    target = os.path.expanduser(raw) if raw else DEFAULT_STAGE_DIR
    if _under_project(target):
        return {"ok": False,
                "error": "暂存目录不能放在项目文件夹内(项目只存系统文件,不存录制视频)",
                "switched": [], "pending": [], "effective": None}
    real = os.path.realpath(target)
    ok, err = _stage_dir_writable(real)
    if not ok:
        return {"ok": False, "error": err, "switched": [], "pending": [],
                "effective": None}
    cfg = state.get_config() if state is not None else load_config()
    # 生效用配置:**以本次请求的目录为准**覆盖一层,而不是指望调用方已经存过配置。
    # 否则「专用端点只切不存」或「先切后存」这两种顺序里总有一种会切到旧根 ——
    # 单元测试就是这么抓到的(切完 output_path 还在老目录)。
    cfg = dict(cfg)
    cfg["recorder"] = dict(cfg.get("recorder") or {}, output_dir=raw)
    active = [n for n, r in list(_recordings.items())
              if r.get("proc") is not None and r.get("proc").poll() is None]
    if not active:
        # 没人在录:配置在保存后就已经生效,不需要任何延迟语义
        _stage_switch = None
        return {"ok": True, "error": None, "switched": [], "pending": [],
                "effective": real, "mode": "immediate"}
    if not now:
        _stage_switch = {"dir": real, "since": time.time()}
        log(f"[录制] 暂存目录已改为 {real};{len(active)} 路在录,"
            f"本场结束后生效({', '.join(active)})")
        return {"ok": True, "error": None, "switched": [], "pending": active,
                "effective": _resolve_output_dir(cfg), "mode": "next_session"}
    switched = []
    for name in active:
        rec = dict(_recordings.get(name) or {})
        old_dir = rec.get("out_dir") or os.path.dirname(rec.get("output_path") or "")
        url = rec.get("stream_url")
        if not url:
            continue
        _restart_recording(state, name, url, cfg, rec.get("room_id"),
                           reason="切换暂存目录", count_it=False, force_new_dir=True)
        new_rec = _recordings.get(name)
        if not new_rec:
            log(f"[录制][!] {name} 切换暂存目录后未能重新起录,请检查流地址/磁盘")
            continue
        new_dir_actual = new_rec.get("out_dir") or ""
        # 旧半场:进程已收,目录不再被写 ⇒ 立刻排队归档,别留成孤儿
        if old_dir and os.path.isdir(old_dir) and old_dir != new_dir_actual:
            try:
                if (cfg.get("nas") or {}).get("enabled", False):
                    nas_mod.enqueue({"streamer": name, "src_path": old_dir,
                                     "room_id": rec.get("room_id"), "start": None,
                                     "date": os.path.basename(old_dir)})
                    _mark_archive_state(old_dir, "pending")
            except Exception as exc:            # noqa: BLE001
                log(f"[归档] {name} 旧半场入队失败(不影响新目录录制): {exc}")
        switched.append({"name": name, "from": old_dir, "to": new_dir_actual})
    _stage_switch = None
    log(f"[录制] 暂存目录已切换为 {real},{len(switched)} 路已在新目录继续")
    return {"ok": True, "error": None, "switched": switched, "pending": [],
            "effective": real, "mode": "now"}


# --- 矛盾状态校验:判定在播却拿不到流地址 ---
#
# 这是两种故障的分岔口,靠 302 单通道分辨不出来:
#   · 真的在播,只是 reflow 页没解析出地址 → API 能给出流地址,把录制救回来;
#   · 302 假阳性(其实没开播) → API 判 status != 2,纠正状态、停止空转。
# 两种结果都很值钱,所以值得花 Cookie。但 Cookie 很贵,而且本函数的调用方
# _refresh_recording 既被检测循环(5~20 分钟一轮)调用、也被 20 秒一次的
# 健康巡检调用 —— 不节流的话,一场假阳性就能把日配额刷穿。
def _conflict_due(name, cfg, room_id):
    """是否该再发起一次矛盾校验。返回 (是否放行, 原因)。"""
    g = (cfg.get("detection") or {}).get("cookie_guard") or {}
    interval = float(g.get("conflict_probe_interval", 300))
    max_tries = int(g.get("conflict_probe_max", 3))
    now = time.time()
    st = _conflict_state.get(name)
    # 换房间 = 新的一场,计数从头开始
    if st is None or (room_id and st.get("room_id") != room_id):
        _conflict_state[name] = {"last": now, "tries": 1, "room_id": room_id}
        return True, "首次校验"
    if st["tries"] >= max_tries:
        return False, f"本场已校验 {st['tries']} 次,达上限 {max_tries}"
    if now - st["last"] < interval:
        return False, f"距上次校验仅 {int(now - st['last'])}s(< {int(interval)}s)"
    st["last"] = now
    st["tries"] += 1
    st["room_id"] = room_id or st.get("room_id")
    return True, f"第 {st['tries']}/{max_tries} 次校验"


def _web_rid_of(cfg, name):
    """取主播的直播间号(web_rid)—— API 通道唯一认的主键。

    Cookie 校验打的是 `webcast/room/web/enter/?web_rid=...`,传 reflow 页里的
    19 位 room_id 会静默拿到空数据(详见 detect.check_api 说明)。缺 web_rid 的
    主播只能用 302 通道,矛盾校验在这里就该明确放弃,而不是假装校验过。
    """
    for m in (cfg or {}).get("monitors", []) or []:
        if m.get("name") == name:
            rid = str(m.get("web_rid") or "").strip()
            if rid:
                return rid
    return ""


def _schedule_conflict_probe(state, name, cfg, room_id=None, web_rid=None):
    """在播却无流地址时,异步发起一次带 Cookie 的专项校验(不阻塞调用方)。"""
    if state is None:
        return
    due, why = _conflict_due(name, cfg, room_id)
    if not due:
        return
    log(f"[矛盾校验] {name} 触发({why})")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环(如单元测试同步调用),跳过
    loop.create_task(_conflict_probe(state, name, cfg, room_id, web_rid))


async def _conflict_probe(state, name, cfg, room_id=None, web_rid=None):
    """用 Cookie 校验「判定在播却拿不到流地址」这一矛盾状态。

    两种结果:
      · API 确认在播且给出流地址 → 补上地址并重新进入录制流程;
      · API 确认未开播 / 无数据 → 判定为 302 假阳性,记录并停止本场录制,
        避免 ffmpeg 反复空转刷历史(这正是某位主播此前 17.82 小时空壳的成因)。
    """
    global _http_client
    web_rid = web_rid or _web_rid_of(cfg, name)
    if not web_rid:
        log(f"[矛盾校验] {name} 无 web_rid(直播间号),跳过 Cookie 校验 —— "
            f"API 通道需要 web_rid,room_id 传进去只会拿到空数据")
        return
    if not room_id:
        return
    client = _http_client
    if client is None:
        try:
            client = httpx.AsyncClient(timeout=10, follow_redirects=False,
                                       trust_env=False)
        except Exception:
            return
        own = True
    else:
        own = False
    try:
        res, why = await confirm_with_cookie(
            client, room_id, cfg, guard=_cookie_guard,
            anchor=name, reason="conflict", web_rid=web_rid)
        if res is None:
            log(f"[矛盾校验] {name} 未能完成(room_id={room_id}):{why}")
            return
        status = res.get("status")
        su = res.get("stream_url") or ""
        if res.get("is_live") and su:
            log(f"[矛盾校验] {name} 确认在播且取到流地址,补录中")
            _last_stream_url[name] = su
            # 不清除节流:救回一次不等于本场不会再出状况,但 Cookie 经不起反复花。
            # 计数继续累加,本场用完 conflict_probe_max 次即止;换房间会自动重置。
            _refresh_recording(state, name, su, cfg, room_id)
        elif res.get("is_live"):
            log(f"[矛盾校验] {name} 确认在播,但 API 亦无流地址(status={status})")
        else:
            log(f"[矛盾校验] {name} API 判定未开播(status={status})"
                f" —— 302 为假阳性,停止本场空转")
            _stop_recording(state, name)
            _clear_giveup(name)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[矛盾校验] {name} 异常: {type(e).__name__}: {e}")
    finally:
        if own:
            try:
                await client.aclose()
            except Exception:
                pass


def _note_clock_offset(rec):
    """采一次「当前墙钟 − 当前媒体时刻的源时间戳」,交给 subtitle 取最小值。

    只在 src_timestamps 模式下需要:tag 不再是墙钟,字幕锚点要靠这个偏移把
    源流时间还原成绝对墙钟。失败静默跳过(下一轮巡检再采)。

    数据来源有两条,优先第一条:
      ① `-progress` 的 out_time_us(ffmpeg 自己报的"已 mux 媒体时长")+
         首分片首包源时间戳 → 当前媒体时刻的源 ts。**数值可信、无解析风险**,
         唯一误差是 progress 的刷新滞后(≤ stats_period 0.5s,取最小值可削掉);
      ② 回退:tag 回扫 `flv_last_ts_any`。录制中的文件末尾常压在半个 tag 上,
         严格版 `flv_last_ts` 大概率返回 None(实测连试 3 次全 None),回扫能读到
         "最近一包",但存在误匹配字节的可能,所以只当备胎。

    为什么取最小值:两条通路都只带"偏晚"的误差(滞后/回退到更早的包),
    最小值那一次最接近真实媒体时刻。
    """
    try:
        segs = [p for _idx, p in subtitle_mod.find_segments(rec["output_path"])]
        if not segs:
            return
        first = subtitle_mod.flv_first_av_ts(segs[0])
        if first is None or first >= subtitle_mod.RELATIVE_TS_MAX:
            return          # 旧形态(tag 已是墙钟)或读不到,无需采样
        now_ms = int(time.time() * 1000)
        prog = _read_progress(rec.get("progress_path"))
        if prog and prog.get("out_us") is not None:
            media_ts = first + prog["out_us"] // 1000
            sample = now_ms - media_ts
            if sample > 0:
                subtitle_mod.note_clock_offset(rec["output_path"], sample)
                return
        last = subtitle_mod.flv_last_ts(segs[-1])
        if last is None:
            last = subtitle_mod.flv_last_ts_any(segs[-1])
        if last is not None and last < subtitle_mod.RELATIVE_TS_MAX:
            sample = now_ms - last
            if sample > 0:
                subtitle_mod.note_clock_offset(rec["output_path"], sample)
    except Exception:
        pass                # 采样是旁路,绝不影响录制主链路


def _await_stream(state, name, stream_url, cfg, room_id, await_sec):
    """流侧退出后的「等流」episode:退避重连 + 等抖音表态,不消耗重启预算。

    这一步要同时满足三件互相冲突的事,所以不能靠"重试次数"决策:
      ① 最常见的断流抖一下就好(实测 262 次重启里 261 次一次即恢复)——
         必须**立刻**重连,不能先等一分钟;
      ② 真下播时,抖音要 24~70 秒才把状态翻成"已结束",这段窗口里拉不到流
         是**正常现象**,报红就是误报;
      ③ 主播只是临时下播十几分钟后回来,不该被 600s 冷却挡在门外白扔素材。

    判定因此改用两个外部信号,而不是本地计数:
      · 抖音是否还判在播(由等流期间主动 request_refresh 逼检测循环尽快给出);
        一旦判下播,检测循环会走正常下播分支 → _stop_recording → 本 episode 自动清空,
        **全程不产生任何 error 事件**;
      · 地址指纹是否变化(变了 = 重签的新地址,值得立刻试)。

    只有"抖音一直说在播、地址一直不变、连续拉到 15 分钟仍拉不到"才升级成
    `record.giveup`(error)—— 那才是真的需要人介入。
    """
    now = time.time()
    fp = _url_fingerprint(stream_url)
    w = _rec_stream_wait.get(name)

    if w is None:
        # episode 第一跳:先立刻重连(覆盖绝大多数"抖一下"的断流),不计止损次数。
        _rec_stream_wait[name] = {
            "fp": fp, "since": now, "fails": 1, "next_at": now,
            "url": str(stream_url or "")[:200]}
        log(f"[录制] {name} 流中断(流侧),立即重连(不消耗止损次数)")
        _request_stream_recheck(state, name)
        _restart_recording(state, name, stream_url, cfg, room_id,
                           reason="断流重连", count_it=False)
        return

    w["fails"] = int(w.get("fails") or 0) + 1
    waited = now - float(w.get("since") or now)

    if fp and fp != w.get("fp"):
        # 抖音重签了新地址 → 这一跳大概率对应"推流已恢复",立刻试。
        w["fp"] = fp
        w["url"] = str(stream_url or "")[:200]
        w["next_at"] = now
        log(f"[录制] {name} 流地址已更新,立即重连(已等 {int(waited)}s)")
        _restart_recording(state, name, stream_url, cfg, room_id,
                           reason="断流重连", count_it=False)
        return

    if waited >= await_sec:
        # 等流窗耗尽:抖音一直判在播、地址一直不变、就是拉不到 —— 这才是真故障。
        fails = w["fails"]
        _clear_stream_wait(name)
        _give_up(state, name, int((cfg.get("recorder") or {}).get("stall_cooldown") or 600),
                 f"流中断 {int(waited)}s 未恢复(重试 {fails} 次)")
        return

    if now < float(w.get("next_at") or 0):
        # 退避中:本轮什么都不做。**这不是放弃** —— 下一轮巡检会接着来。
        # 之所以允许"空转一轮",是要把抖音翻状态的那 24~70s 让出来:
        # 每 20s 对着同一条死地址重开一次,除了刷空文件没有任何收益。
        return

    wait = _stream_backoff(w["fails"])
    w["next_at"] = now + wait
    log(f"[录制] {name} 地址未变,等推流恢复(已等 {int(waited)}s,"
        f"第 {w['fails']} 次重试,{wait}s 后再试)")
    _request_stream_recheck(state, name)
    _restart_recording(state, name, stream_url, cfg, room_id,
                       reason="等推流恢复", count_it=False)


def _request_stream_recheck(state, name):
    """请检测循环尽快给这个主播判一次在播/下播。

    没有这一步,等流窗就是个瞎等:该主播的检测周期是 live_interval(最长 20 分钟),
    抖音早就翻成"已结束"了我们也不知道,最后会把一次**正常下播**升级成
    15 分钟后的红色故障。旁路失败不影响录制。
    """
    try:
        state.request_refresh(name)
    except Exception:                       # noqa: BLE001
        pass


def _refresh_recording(state, name, stream_url, cfg, room_id=None, web_rid=None):
    """直播中维持录制:未录则开始;进程退出 / 数据停滞 / 换房间则重启;否则刷新时长。

    「虚假在线」防护:ffmpeg 在拉流断开时可能既不退出、也不再写数据
    (进程存活但文件停止增长)。只看 proc.poll() 会一直记为「录制中」却录不到
    内容,历史里留下空壳条目。此处叠加三条止损:
      1. 文件增长停滞超过 stall_seconds(默认 90s)→ 重启;
      2. 换 room_id(主播重开/换号)→ 旧文件已不属于本场,强制重启新文件;
      3. 单场重启超过 stall_max_restarts(默认 4 次)仍无数据 → 停止本场录制,
         不再空转刷历史。
    """
    rec_cfg = cfg.get("recorder") or {}
    stall_sec = int(rec_cfg.get("stall_seconds") or 90)
    max_restarts = int(rec_cfg.get("stall_max_restarts") or 4)
    cooldown = int(rec_cfg.get("stall_cooldown") or 600)
    # 流中断「等流窗」:抖音判下播有 24~70s 延迟,这段时间里"拉不到流"是正常的,
    # 不该算故障。超过它(默认 15 分钟)还拉不到、而抖音仍说在播,才是真问题。
    await_sec = int(rec_cfg.get("stream_await_seconds") or 900)

    if not stream_url:
        if name in _recordings:
            return  # 已有录制继续;无流地址不打断
        log(f"[录制] {name} 检测到直播但未解析到拉流地址,无法录制")
        # 进告警条:这是「判定在播却录不到文件」的直接表现,原先只有一行日志。
        try:
            incidents_mod.record(
                "record.nostream", "%s 判定在播却拿不到拉流地址" % name,
                detail="已排一次 Cookie 专项校验(可能是 302 假阳性,或 reflow 页降级)",
                name=name, level=incidents_mod.LEVEL_WARN)
        except Exception:                       # noqa: BLE001
            pass
        # 矛盾状态:判定在播却拿不到流地址。要么是真在播要救回录制,
        # 要么是 302 假阳性要纠正 —— 用 Cookie 做一次专项校验(自带节流)。
        _schedule_conflict_probe(state, name, cfg, room_id, web_rid)
        return
    rec = _recordings.get(name)

    # 换房间:room_id 变了说明是另一场直播,旧文件不应再续写。
    # 同时解除上一场的冷却——上一场救不回来不代表这一场也录不了。
    if rec is not None and room_id and rec.get("room_id") and room_id != rec["room_id"]:
        _clear_giveup(name)
        _session_start.pop(name, None)   # 换房间 = 新的一场,残片回收的"本场"重新界定
        log(f"[录制] {name} room_id 变更 {rec['room_id']} → {room_id},重启为新场次")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="换房间")
        return

    # 冷却期:本场已被判定为「救不回来」,在冷却结束前不再尝试。
    # 没有这道闸,流地址失效时 ffmpeg 会一退出就被立刻重开(巡检 20s 一轮),
    # 几分钟内刷出几十条空历史条目。
    if time.time() < _rec_giveup_until.get(name, 0):
        return

    if rec is None:
        _start_recording(state, name, stream_url, cfg, room_id)
        _rec_health[name] = {"size": 0, "ts": time.time(), "restarts": 0,
                             "room_id": room_id, "stalled": False}
        return

    if rec["proc"].poll() is not None:
        # 进程退出分两类,止损预算也分两套(2026-10-31 重构,依据见
        # 《断流与下播判定-数据研究与报错逻辑重构》):
        #   · 流侧(404 / Stream ends prematurely):是主播下播或推流中断,**不是**
        #     我们的故障。抖音翻状态有 24~70s 延迟,退出当下分不清是哪一种 ——
        #     按最坏情况处理就会把"正常下播"报成红色崩溃,还会在 80 秒内烧光
        #     重启预算、白扔 600s 冷却(实测 8 天 18 次)。
        #   · 本地资源类(scale_vt / 编码器 / 内存):这才是要人看的故障,维持原止损。
        _log_ffmpeg_exit(name, rec)          # ← 先把退出原因落到日志,再决定去留
        h = _rec_health.setdefault(name, {"size": 0, "ts": time.time(),
                                          "restarts": 0, "room_id": room_id,
                                          "stalled": False})
        if _exit_cause(rec) == quality_mod.CRASH_EXTERNAL:
            _await_stream(state, name, stream_url, cfg, room_id, await_sec)
            return
        # 进程退出同样计入重启上限:本地故障下不限次会让巡检每轮重开一个空文件。
        if h["restarts"] >= max_restarts:
            _give_up(state, name, cooldown,
                     f"ffmpeg 反复退出,已重启 {h['restarts']} 次")
            return
        log(f"[录制] {name} ffmpeg 已退出,重启一次")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="进程退出")
        return

    # 数据存活检查:文件长时间不增长 = 假在线
    h = _rec_health.setdefault(name, {"size": 0, "ts": time.time(), "restarts": 0,
                                      "room_id": room_id, "stalled": False})
    size = _rec_output_size(rec["output_path"])
    now = time.time()
    if size is None or size > h["size"]:
        h["size"] = size or 0
        h["ts"] = now
    stalled_for = now - h["ts"]
    # 过半阈值即标记告警(前端显示「无数据流」),满阈值才动手重启,
    # 避免把 ffmpeg 正常的缓冲/网络抖动当成故障。
    h["stalled"] = stalled_for >= stall_sec * 0.5
    if stalled_for >= stall_sec:
        if h["restarts"] >= max_restarts:
            _give_up(state, name, cooldown,
                     f"数据停滞 {int(stalled_for)}s 且已重启 {h['restarts']} 次仍无数据")
            return
        log(f"[录制][!] {name} 数据停滞 {int(stalled_for)}s"
            f"(ffmpeg 存活但无写入),重启第 {h['restarts'] + 1}/{max_restarts} 次")
        _restart_recording(state, name, stream_url, cfg, room_id, reason="数据停滞")
        return
    # 时钟偏移采样(字幕锚点的地基,见 _note_clock_offset):
    # 每轮巡检顺带采一次,放在"确认进程活着且数据在长"之后,不采半截文件。
    _note_clock_offset(rec)
    # 数据在长 = 这次流中断 episode 已经结束,必须就地清账。
    # 不清的后果很硬:同一个 stream key 在一场里可以用几个小时,第二次断流的
    # 指纹会和第一次相同,而 `since` 还是第一次的时刻 —— 于是「已等 3 小时」
    # 直接顶穿等流窗,把一次普通抖动判成本场放弃。
    if _rec_stream_wait.get(name):
        _clear_stream_wait(name)
        log(f"[录制] {name} 流已恢复,流中断态清零")
    _sync_recording_state(state, name, stalled=h["stalled"])


QUALITY_STATE_PATH = os.path.join(BASE, quality_mod.STATUS_FILE)


def _update_follow(name, rec):
    """读 `-progress` 算跟随率(输出时间增量 ÷ 墙钟增量),写回 rec。

    为什么不能只看「文件在不在长」:落后时文件照样在长,只是内容追不上实时 ——
    等文件不长了再动手,那一段已经是废素材。跟随率 <1 就是这一路正在落后。
    平滑用非对称 EMA:下跌敏感(0.5)、回升迟缓(0.2),避免一次 IO 抖动就切档。
    """
    if not rec or not rec.get("progress_path"):
        return rec.get("follow", 1.0) if rec else 1.0
    now = time.time()
    data = _read_progress(rec.get("progress_path"))
    if not data:
        return rec.get("follow", 1.0)
    prev = rec.get("_prog")
    rec["_prog"] = {"frame": data["frame"], "out_us": data["out_us"], "ts": now}
    if not prev:
        return rec.get("follow", 1.0)
    dt_out = (data["out_us"] - prev["out_us"]) / 1e6
    dt_wall = now - prev["ts"]
    if dt_wall <= 0.2:
        return rec.get("follow", 1.0)
    inst = max(0.0, min(1.5, dt_out / dt_wall))
    if dt_out > 0:                     # 内容帧率(progress 的 fps 字段实测恒为 0.00)
        rec["src_fps"] = round((data["frame"] - prev["frame"]) / dt_out, 2)
    old = float(rec.get("follow") or 1.0)
    alpha = 0.5 if inst < old else 0.2
    rec["follow"] = round(max(0.0, min(1.5, old + alpha * (inst - old))), 3)
    return rec["follow"]


def _maybe_retier(state, name, rec, cfg, url):
    """档位变了要换一路参数 —— 但**只在值得付代价时**才动在录流。返回是否已重启。

    在录流默认不重启:重启必丢 1–2 秒素材,字幕锚点也跟着错位。唯一例外要同时
    满足两个条件:
      ① 这一路确实跟不上(`follow < 0.95`)—— 跟得上的路没必要为它丢素材;
      ② 刚跨过一片边界 —— 分片边界切换能保证单个文件内参数一致,否则一个 .flv
         里前后参数不同,后端按首包 onMetaData 解析出的分辨率就是错的。
    """
    want = _active_level()
    if int(rec.get("quality_level", 0) or 0) == want:
        rec.pop("retier_wait", None)
        return False
    if not url or rec.get("proc") is None or rec["proc"].poll() is not None:
        return False
    if float(rec.get("follow") or 1.0) >= 0.95:
        return False
    seg = _segment_stats(rec.get("output_path"))
    idx = int((seg or {}).get("index") or 0)
    if "retier_wait" not in rec:
        rec["retier_wait"] = idx       # 记下当前分片号,等它翻页
        return False
    if idx <= rec["retier_wait"]:
        return False                   # 还没到边界,继续等(宁可变慢也不打断素材)
    rec.pop("retier_wait", None)
    log(f"[录制] {name} 跟随率 {rec.get('follow')} 偏低,在分片边界切换档位 "
        f"L{int(rec.get('quality_level', 0) or 0)} → L{want}")
    _restart_recording(state, name, url, cfg, rec.get("room_id"),
                       reason="档位切换", count_it=False)
    return True


def _cpu_percent():
    """系统整体 CPU 利用率(0–100)。

    **刻意不用 load average**:编码线程多,实测 3 路时 load 26 而 CPU 只有 74%,
    用 load 判定会疯狂误降档。优先 psutil(一次采样,内部保存时间差);
    没装 psutil 时退到 `ps -A -o pcpu` 求和 ÷ 核数 —— 精度差(是进程启动以来的
    均值),但方向不会错,足以兜住「psutil 没装也不要瞎降档」。
    """
    try:
        import psutil
        return round(float(psutil.cpu_percent(None)), 1)
    except Exception:
        pass
    try:
        out = subprocess.run(["ps", "-A", "-o", "pcpu="], capture_output=True,
                             text=True, timeout=5).stdout
        total = sum(float(x) for x in out.split() if x.strip())
        return round(min(100.0, total / max(os.cpu_count() or 1, 1)), 1)
    except Exception:
        return 0.0


def _disk_ok_for_degrade(cfg, need_gb=5.0):
    """暂存盘余量是否还够「让体积变大」(第 3 层守卫的输入)。

    只探本机暂存目录:归档目标可能是 SMB,5 s 一轮的采样里做网络 IO 会拖住
    整个调度循环。暂存盘满了录制先挂,所以它才是要看的那个盘。
    """
    try:
        out = _resolve_output_dir(cfg)
        if not os.path.isdir(out):
            return True
        return shutil.disk_usage(out).free / 1e9 >= need_gb
    except Exception:
        return True


def _quality_streams():
    """在录流的 [(宽, 高, fps)] —— 引擎需求(Σ 像素率)的输入。"""
    out = []
    for name, rec in list(_recordings.items()):
        proc = rec.get("proc")
        if proc is None or proc.poll() is not None:
            continue
        src = _src_size.get(name)
        w, h = quality_mod.effective_size(rec.get("resolution", 0),
                                          *(src or (None, None)))
        out.append((w, h, float(rec.get("src_fps") or 20.0)))
    return out


def _src_hint():
    """引擎需求的**主导**源尺寸(降级收益估算用);没有则 (None, None)。

    取像素数最大的那一路,**不取众数**。降档是为了压住最吃引擎的那一路,
    而众数/元组最大值会被一路横屏小画面带偏 —— 2026-10-27 实测:两路竖屏
    1080×1920 已降到 720×1280(引擎 −55.6%),reason 却显示「L1 引擎 −0%」,
    正是因为估算被那路 1040×480 代表了。逐路明细见 `_streams_detail`。
    """
    sizes = [_src_size[n] for n in _recordings if n in _src_size]
    if not sizes:
        return None, None
    best = max(sizes, key=lambda s: int(s[0]) * int(s[1]))
    return int(best[0]), int(best[1])


def _streams_detail(cfg):
    """每一路在录流的「源 → 当前输出」明细(接口与排查用)。

    收益估算只取主导路(见 `_src_hint`),但「哪一路真的被缩了、哪一路源本来就
    比档位小」必须逐路给出:一路横屏小画面就足以让整条链路的结论失真,而
    这种失真在只给单值的面板上完全看不出来。
    """
    out = []
    for name, rec in list(_recordings.items()):
        src = _src_size.get(name)
        tier_now = rec.get("resolution") or 0
        try:
            w0, h0 = (int(src[0]), int(src[1])) if src else (None, None)
        except (TypeError, ValueError):
            w0, h0 = None, None
        try:
            ow, oh = quality_mod.effective_size(tier_now, w0, h0)
        except Exception:
            ow, oh = 0, 0
        out.append({
            "name": name,
            "src": [w0, h0] if w0 else None,
            "level": int(rec.get("quality_level", 0) or 0),
            "resolution": int(tier_now) if tier_now else 0,
            "out": [int(ow), int(oh)],
            # True=真的被缩了 / False=源短边本就不大于档位,只降了码率 /
            # None=源尺寸还没学到,无法判断(别把「不知道」写成「没缩」)
            "scaled": (bool(quality_mod.tier_size(tier_now, w0, h0))
                       if (w0 and h0) else None),
        })
    return out


def _tier_view(cfg, level):
    """某一档位真正生效的编码参数(含按源尺寸换算出的输出 w/h)。

    **档位 → 参数的唯一换算出口**:接口、日志、`set_active` 三处都走它。
    以前各写一份,结果 `set_active` 拿到的是 observe 之前的旧档位,日志于是
    把上一档的参数当成新档打印(off-by-one),排查时方向读反。
    """
    tier = quality_mod.level_tier(cfg, level)
    br = tier["bitrate"]
    w, h = quality_mod.effective_size(tier["resolution"], *_src_hint())
    return {"resolution": tier["resolution"],
            "bitrate": int(br) if float(br).is_integer() else br,
            "w": w, "h": h,
            "level": tier["level"], "label": tier["label"]}


def _quality_plan(cfg, disk_ok=None):
    """当前配置下降级可行性 + 生效档位(**保存校验、采样、接口三处共用的口径**)。"""
    sw, sh = _src_hint()
    if disk_ok is None:
        disk_ok = _disk_ok_for_degrade(cfg)
    plan = quality_mod.degrade_plan(cfg, sw, sh, disk_ok)
    plan["active_tier"] = _tier_view(cfg, _active_level())
    plan["streams_detail"] = _streams_detail(cfg)
    return plan


async def _quality_loop(state, interval=5):
    """画质调度:5 s 采样 CPU / 引擎占用 / 跟随率,按阈值切档位。

    为什么另起一个 5 s 的循环:watchdog 是 20 s 一轮,只够做「文件有没有长」
    这种**事后**止损;降档必须在越线后一分钟内发生,否则那 60 s 里已经在掉帧。
    两者互补 —— watchdog 管「已经坏了怎么救」,这里管「快要坏了先退一步」。

    单轮异常一律吞掉:调度协程挂掉不会有人发现(录制照旧),而它一旦把整个
    任务拖垮,连止损都没了。
    """
    global _quality_status
    gov = _quality_gov
    last_state = ""
    while True:
        try:
            health_mod.beat("quality")
            await asyncio.sleep(interval)
            cfg = state.get_config()
            rec_cfg = cfg.get("recorder") or {}
            if gov is None or not rec_cfg.get("enabled", True):
                continue
            gov.set_config(cfg)
            now = time.time()
            streams = _quality_streams()
            cpu = _cpu_percent()
            demand = quality_mod.demand_pct(streams)
            follows = [float((_recordings.get(n) or {}).get("follow") or 1.0)
                       for n in _recordings]
            follow_min = min(follows) if follows else 1.0
            disk_ok = _disk_ok_for_degrade(cfg)
            plan = _quality_plan(cfg, disk_ok)
            res = gov.observe(
                now, plan, cpu=cpu, demand=demand,
                engine=quality_mod.engine_util(demand, follow_min),
                follow_min=follow_min, streams=len(streams),
                disk_ok=disk_ok)
            # 切换发生的这一轮,active_tier 必须按**新**档位回填:用 observe 之前
            # 算好的 plan 会让面板滞后一轮(5 s),切换瞬间读到的仍是上一档参数。
            gov.set_active(_tier_view(cfg, res["level"]))
            # 状态变化才写一行日志(不是每 5 s 刷屏);档位切换一律留痕
            if res["state"] != last_state:
                last_state = res["state"]
                log(f"[画质] 状态 {res['state']}: {res['reason']}")
            if res["changed"]:
                # 日志里「切到哪一档」必须用**切换后**的档位重算:`plan` 是在
                # observe() 之前算的,直接引用会把上一档的参数写成新档 ——
                # 实测出现过「档位 2 生效(crash) → 720P / 700 kbps」,而 L2 是
                # 500 kbps,排查时极易读反方向。
                after = _tier_view(cfg, res["level"])
                log(f"[画质] 档位 {res['level']} 生效({res['trigger']}): "
                    f"{res['last_reason']} → "
                    f"{quality_mod.TIERS.get(after['resolution'], '原生')}"
                    f" / {quality_mod.fmt_int(after['bitrate'])} kbps"
                    f"({after['w']}×{after['h']})")
                quality_mod.save_state(QUALITY_STATE_PATH, gov.dump())
            snapshot = gov.snapshot(now)
            snapshot["plan"] = {"state": plan["state"], "reason": plan["reason"]}
            snapshot["streams_detail"] = plan.get("streams_detail") or []
            _quality_status = snapshot
            state.set_quality_status(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                health_mod.beat("quality", error=str(e))
            except Exception:
                pass
            log(f"[画质] 采样异常(不影响录制): {e}")


async def _recording_watchdog(state, interval=20):
    """录制健康巡检:高频检查在录文件是否仍在增长;并把「在播却没在录」救回来。

    为什么必须独立于状态检测周期:直播中的检测间隔可能长达 20 分钟
    (live_interval),只靠检测循环调用 _refresh_recording 的话,流断了要等 20 分钟才发现。

    两段各管一种情况(缺了第②段就会漏掉最要紧的那种):
      ① **正在录**的主播:文件停增长 → 重启(「假在线」防护)。
      ② **判定在播、却不在录制**的主播:主动请求提前复检,把它救回录制。
         这段是 2026-09-19 补的:_give_up() 会调 _stop_recording() 把主播从
         `_recordings` 里摘掉,于是第①段再也看不到它 —— 冷却(默认 600s)到期后
         没有任何机制主动重启,只能等下一次检测(live_interval=1200s),
         实测空窗达 **28.5 分钟**(某主播 23:38:53 冷却 → 00:07:23 才恢复)。
         顺带解决"陈旧直播态":主播其实已下播,界面却还挂着「直播中」——
         异常态会触发一次提前复检,`is_live` 很快被纠正(实测另有一例挂了 24 分钟)。
    """
    while True:
        try:
            health_mod.beat("watchdog")
            await asyncio.sleep(interval)
            cfg = state.get_config()
            if not (cfg.get("recorder") or {}).get("enabled", True):
                continue
            # ① 已在录的:查文件是否还在增长
            for name in list(_recordings.keys()):
                rec = _recordings.get(name)
                if not rec:
                    continue
                # 延迟入史:起录时先不写 history,活过首次巡检(20s)才补记。
                # 成因(2026-09-21):流地址已被 CDN 回收(下播后 404)时,ffmpeg
                # 起来秒退,却各自留下一条 ~20s 的历史条目(墙钟=巡检间隔),
                # 一场失败循环刷出 4-5 条 —— 用户看到的「历史里多出 20 秒的残留」。
                # 404 秒退根本不产出文件,补记前确认进程还活着即可从源头拦住。
                if rec.pop("history_pending", None):
                    if rec["proc"].poll() is None:
                        _history_add(state, name, rec["output_path"],
                                     rec["started_at"])
                    # 已退出:不入史,交给下面的 _refresh_recording 处理重启
                # 优先用检测循环缓存的最新流地址(旧地址可能已过期)
                url = _last_stream_url.get(name) or rec.get("stream_url")
                try:
                    _update_follow(name, rec)
                    _learn_src_size(name, rec.get("output_path"),
                                    rec.get("quality_level"))
                    if _maybe_retier(state, name, rec, cfg, url):
                        continue      # 刚按新档位重启,本轮不再重复处理
                    _refresh_recording(state, name, url, cfg, rec.get("room_id"),
                                      _web_rid_of(cfg, name))
                except Exception as e:
                    log(f"[巡检] {name} 健康检查异常: {e}")
            # ② 判定在播、却没在录的:请求提前复检(由检测循环带着**新流地址**重启录制)
            try:
                _rescue_missing_recordings(state, cfg)
            except Exception as e:
                log(f"[巡检] 未录制主播抢救异常: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[巡检] 录制健康检查异常: {e}")


def _rescue_missing_recordings(state, cfg):
    """把「已判定在播、却不在录制」的主播救回录制。

    触发一次**提前复检**而不是直接拿缓存的旧地址硬开:复检会刷新 stream_url
    (旧地址可能已过期,硬开只会立刻再退一次、把冷却次数耗光),同时也会纠正
    「其实早下播了」的陈旧直播态。检测循环每 0.5s 消费一次刷新请求 ⇒ 近乎立即生效。

    不该管的四种情况,各自明确跳过:
      - 已在录制(第①段负责);
      - 用户手动停过(`_manual_paused`,不能违背用户意图自动开);
      - 该主播关了自动录制(`auto_record=false`,只检测不录);
      - 还在冷却期(止损设计:到期后下一次巡检自然会走这里)。
    """
    streamers = state.get_streamers()
    now = time.time()
    for name, snap in streamers.items():
        if not (snap or {}).get("is_live"):
            continue
        if name in _recordings or name in _manual_paused:
            continue
        if not auto_record_enabled(cfg, name):
            continue
        if now < _rec_giveup_until.get(name, 0):
            continue                      # 冷却中:等它到期,这里下一轮就来救
        if now - _anomaly_probe_at.get(name, 0) < _ANOMALY_RECHECK_SEC:
            continue                      # 节流:同一异常别每 20s 请求一次检测
        _anomaly_probe_at[name] = now
        left = _rec_giveup_until.get(name)
        why = "冷却已过" if left else "无录制进程"
        log(f"[巡检] {name} 判定在播却未录制({why}) → 请求提前复检并重启录制")
        state.request_refresh(name)


def _preview_selected(cfg, name):
    """该主播是否已设置预览图。"""
    return bool(preview_mod.selected_preview(cfg, name))


async def _preview_capture_loop(state, name, ffmpeg):
    """直播中维持 shots 张备选截图:未设头像时,库存不足 shots 张就持续补充。

    用 _last_stream_url 取最新流地址(避免流过期);设置头像时 return,由 reconcile
    在「删头像后」重新拉起;下播/删除主播由 _cancel_preview_capture 取消。
    """
    cfg = state.get_config()
    pv = cfg.get("preview") or {}
    delay = max(1, int(pv.get("start_delay", 60)))
    interval = max(1, int(pv.get("interval", 30)))
    shots = max(1, int(pv.get("shots", 5)))
    out_dir = preview_mod.streamer_dir(cfg, name)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        return
    try:
        await asyncio.sleep(delay)
        while True:
            cfg = state.get_config()
            if _preview_selected(cfg, name):
                return  # 已设头像,停止截图
            stream_url = _last_stream_url.get(name)
            if not stream_url:
                await asyncio.sleep(interval)
                continue
            existing = preview_mod.list_images(cfg, name)
            if len(existing) < shots:
                ts = time.strftime("%Y%m%d-%H%M%S")
                out = os.path.join(out_dir, f"shot-{ts}-{len(existing) + 1}.jpg")
                ok = await asyncio.to_thread(preview_mod.capture_frame, ffmpeg, stream_url, out)
                if ok:
                    log(f"[预览] {name} 截图(库存 {len(existing) + 1}/{shots}): {out}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise


def _ensure_preview_capture(state, name):
    """确保「直播中且未设头像」的主播有截图任务在跑。幂等,供 once 与 reconcile 调用。"""
    if _preview_selected(state.get_config(), name):
        return
    if name in _preview_tasks and not _preview_tasks[name].done():
        return
    if not _last_stream_url.get(name):
        return
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return
    task = asyncio.get_running_loop().create_task(
        _preview_capture_loop(state, name, ffmpeg))
    _preview_tasks[name] = task


def _cancel_preview_capture(name):
    """取消某主播的截图任务(下播/删除/退出)。"""
    task = _preview_tasks.pop(name, None)
    if task and not task.done():
        task.cancel()


def _cleanup_recordings():
    """进程退出时终止所有录制子进程,避免 ffmpeg 残留为孤儿进程。"""
    for name in list(_recordings):
        rec = _recordings.pop(name, None)
        if not rec:
            continue
        try:
            rec["proc"].terminate()
            try:
                rec["proc"].wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    rec["proc"].kill()
                except Exception:
                    pass
        except Exception:
            pass
        log(f"[录制] {name} 退出清理: {rec['output_path']}")
        _history_finish(None, name)
    for name in list(_preview_tasks):
        _cancel_preview_capture(name)


def _handle_term(signum, frame):
    log(f"收到信号 {signum},清理录制后退出")
    try:
        _cleanup_recordings()
    finally:
        # 必须放在 finally 里:清理过程抛异常也不能让「干净退出」的标记丢,
        # 否则下次启动会把这次正常停止误报成「主进程异常退出」。
        try:
            incidents_mod.end_session()
        except Exception:                       # noqa: BLE001
            pass
    raise SystemExit(0)


def _register_signal_handlers():
    try:
        signal.signal(signal.SIGTERM, _handle_term)
        signal.signal(signal.SIGINT, _handle_term)
    except (ValueError, OSError):
        pass  # 非主线程或受限环境,忽略


def _recent_hotspot(store, anchor):
    """最近热点窗口:返回 {center_minute, last_hit} 或 None。"""
    ws = store.windows(anchor)
    if not ws:
        return None
    w = ws[0]
    return {"center_minute": w.get("center"), "last_hit": w.get("last_hit")}


def _snap(name, anchor, phase, store, last_check, next_in, result, probe=None):
    # 录制冷却:让界面能在「直播中」旁边说清「为什么没在录、何时自动恢复」。
    # 没有这两个字段时,用户看到的就是「显示在播却没在录」而毫无线索。
    until = _rec_giveup_until.get(name) or 0
    return {
        "name": name, "anchor": anchor, "probe": probe or anchor, "phase": phase,
        "windows": len(store.windows(anchor)),
        "hotspot": _recent_hotspot(store, anchor),
        "last_check": last_check,
        "next_in": round(next_in, 1) if next_in else None,
        "is_live": bool(result.get("is_live")) if result else False,
        "status": (result or {}).get("status"),
        "unknown": bool((result or {}).get("unknown")),
        "method": (result or {}).get("method", ""),
        "error": (result or {}).get("error", ""),
        "rec_paused_until": round(until, 1) if until > time.time() else None,
        "rec_pause_reason": (_rec_giveup_reason.get(name) or "") if until > time.time() else "",
    }


def _record_open(store, name, anchor, result, prev_meta, now, cfg):
    """记录一次开播(热点事件):三源分级取开播时间,写事件日志并更新热点。

    S1 create_time: 检测响应透传的真实开播时间(API/302 通道既有响应自带,零额外请求)。
    S2 witness_mid: 无 create_time 时取见证区间 [上次检测时刻, 本次检测时刻] 中点。
    S3 skip(防污染): 无 create_time 且(重启时已在播 / 见证区间过大)且主播已有热点数据
       → 不记录,避免重启时刻污染既有模式。新主播(无任何数据)豁免:首播必记录。
    返回记录使用的开播时间戳(未记录返回 None)。
    """
    sched = cfg.get("schedule") or {}
    has_data = store.has_data(anchor)
    ct = result.get("create_time")
    try:
        ct = float(ct) if ct is not None else None
    except (TypeError, ValueError):
        ct = None

    if ct and 0 < ct <= now + 60 and now - ct <= 86400:
        # S1:真实开播时间(拒绝未来值与超过 1 天的陈旧值)
        start_ts, source = ct, "create_time"
    else:
        # S2/S3:见证区间估计,配合防污染规则
        gap_max = float(sched.get("max_witness_gap", 7200))
        last_check = (prev_meta or {}).get("last_check_ts") or now
        gap = now - last_check
        resumed = bool((prev_meta or {}).get("was_live")) and \
            (prev_meta or {}).get("last_room_id") == result.get("room_id")
        if resumed and has_data:
            log(f"[热点] {name} 重启恢复会话且无 create_time,跳过本次记录(防污染)")
            return None
        if gap > gap_max and has_data:
            log(f"[热点] {name} 见证区间 {gap / 3600:.1f}h 过大且无 create_time,跳过记录")
            return None
        start_ts, source = last_check + gap / 2, "witness_mid"

    kind = store.record_open(anchor, start_ts, source, now)
    src_desc = "真实开播时间" if source == "create_time" else "见证区间估计"
    log(f"[热点] {name} 开播时刻 {time.strftime('%H:%M', time.localtime(start_ts))}"
        f"({src_desc}{',偏离过大已触发重分析' if kind == 'analyzed' else ''})")
    return start_ts


async def run_streamer(client, mon, state, store):
    name = mon.get("name", "?")
    anchor = str(mon.get("anchor", ""))
    recording = False
    # 会话状态恢复:重启后 last_room_id 不再丢失,
    # 「重启时已在播」由 _record_open 的防污染规则(S3)处理,不会被当新开播记录
    sess = store.get_session(anchor)
    last_room_id = (sess or {}).get("last_room_id")
    err_count = 0
    cooldown_until = 0.0
    warned_no_probe = False
    await asyncio.sleep(random.uniform(0, 60))  # 启动随机相位

    async def once(next_in_hint=None, force=False):
        """执行一次检测并更新状态;返回 (kind, value):
        ('ok', interval) 正常;('noprobe', 120) 缺 web_rid;('cooldown', 剩余秒);
        ('dormant', interval) 休眠中(关了自动录制),不检测。
        next_in_hint: 手动刷新时传入「到原计划检测点的剩余秒」,避免 next_in 被重置。
        force: 手动刷新触发 —— 休眠主播也会被唤醒检测这一次。"""
        nonlocal recording, last_room_id, err_count, cooldown_until, warned_no_probe
        cfg = state.get_config()
        now = time.time()
        # 健康心跳:面板据此判断「检测协程是否还活着」。放在这里而不是成功分支,
        # 是为了让「一直失败但协程没死」也能刷新心跳 —— 那种情况由网络层告警负责,
        # 面板不该把它显示成检测协程卡死。
        health_mod.beat("detect", count=len(cfg.get("monitors", [])))

        # 热重取最新 monitor 条目:UI 上补填 web_rid 无需重启协程,下一轮即生效
        cur = next((m for m in cfg.get("monitors", [])
                    if m.get("name") == name and str(m.get("anchor", "")) == anchor), mon)
        anchor_now = str(cur.get("anchor", ""))
        # 休眠判定放在缺 web_rid 之后:没填 web_rid 的主播仍需提示(那是配置没填全,
        # 不是用户主动休眠),否则关掉自动录制后这条提示也跟着没了、无从排查。
        probe, probe_err = probe_anchor(cur)
        if probe is None:
            # sec_uid 且无 web_rid:不误判开播/冷门,明确提示等待补充
            if not warned_no_probe:
                log(f"[!] {name}: {probe_err}")
                warned_no_probe = True
            state.set_streamer(name, _snap(name, anchor_now, "缺web_rid", store, now, 120,
                                           {"error": probe_err}, probe=""))
            return "noprobe", 120

        if is_dormant(cfg, name, name in _recordings) and not force:
            log_expiry_once(cfg, name)   # 到期只记一次业务日志(决策7A),不推企微
            dorm = float((cfg.get("schedule") or {}).get("cold_interval", 1800))
            state.set_streamer(name, _snap(name, anchor_now, "未监测", store, now, None,
                                           None, probe))
            return "dormant", dorm

        if now < cooldown_until:
            state.set_streamer(name, _snap(name, anchor_now, "冷却", store, None,
                                           cooldown_until - now, None, probe))
            return "cooldown", cooldown_until - now

        in_hot = store.in_hotspot(anchor_now, now)
        result = None
        try:
            # 把「当前是否处于热点窗口」传给检测层:Cookie 护栏据此决定
            # 这次是否值得动用真实账号的登录态(详见 cookie_guard.py)。
            result = await check(client, probe, cfg, last_room_id,
                                 in_hotspot=in_hot, guard=_cookie_guard)
        except Exception as e:
            result = {"is_live": False, "error": str(e)}

        _note_network_result(now, bool(result.get("error")))
        if result.get("error"):
            err_count += 1
            log(f"[!] {name} 异常 {result['error']} (连续 {err_count})")
            if err_count >= int(cfg["schedule"].get("circuit_errors", 5)):
                cooldown_until = now + int(cfg["schedule"].get("circuit_cooldown", 1800))
                log(f"[熔断] {name} 暂停 {cfg['schedule'].get('circuit_cooldown')}s")
                err_count = 0
            # 检测失败:只延长见证区间起点,不动房间/在播状态
            store.touch_session(anchor_now, now)
        else:
            err_count = 0
            unknown = bool(result.get("unknown"))
            if result.get("is_live"):
                room_id = result.get("room_id")
                if room_id and room_id != last_room_id:
                    last_room_id = room_id
                    # prev_meta 为「本次检测之前」的持久化会话状态(重启前/上一轮),
                    # 用于 S3 防污染判定与 S2 见证区间
                    _record_open(store, name, anchor_now, result,
                                 store.get_session(anchor_now), now, cfg)
                    log(f"[开播] {name} room_id={room_id} (via {result.get('method')})")
                    ctx = {
                        "name": name, "anchor": anchor_now, "web_rid": probe, "room_id": room_id,
                        "sec_uid": result.get("sec_uid", anchor_now),
                        "stream_url": result.get("stream_url") or "",
                        "room_url": f"https://live.douyin.com/{probe}",
                    }
                    run_command(state, cfg.get("on_live_command", ""), ctx)
                recording = True
                if result.get("stream_url"):
                    _last_stream_url[name] = result["stream_url"]
                # 自动录制开关:关掉的主播只检测、不自动录(需要时前端手动点
                # 「开始」)。仍在录的(手动开的)继续交给 _refresh_recording,
                # 这样断流自检/重启对手动录制同样生效。
                if name not in _manual_paused and (
                        auto_record_enabled(cfg, name) or name in _recordings):
                    _refresh_recording(state, name, result.get("stream_url"), cfg,
                                       room_id, probe)
                # 截图(头像)与录制是两件事:关掉自动录制也照常补头像。
                # 弹幕会话依附录制输出,没录自然起不来(_ensure_danmaku 内部判断)。
                _ensure_preview_capture(state, name)
                _ensure_danmaku(state, name, room_id, cfg)
            elif unknown:
                # 「看不清」不等于「已下播」:落地页是降级页时(Web 端不可观看)
                # 这一轮既不能证明在播,也不能证明下播。已经开始/开过场的场次一律
                # 不动 —— 此时若按常规下播流程走,会把正在跑的录制直接停掉。
                log(f"[未知] {name} 落地页不可判定(Web 端可能不可观看)"
                    + (" —— 本场状态保持不变" if (recording or last_room_id is not None)
                       else " —— 跳过本轮结论"))
            else:
                if recording or last_room_id is not None:
                    log(f"[下播] {name}")
                    output_dir = _resolve_output_dir(cfg)
                    ctx = {"name": name, "anchor": anchor_now, "web_rid": probe, "output_dir": output_dir}
                    run_command(state, cfg.get("on_offline_command", ""), ctx)
                    # NAS 启用时入队归档(on_offline_command 仍照常执行,作逃生通道)
                    if (cfg.get("nas") or {}).get("enabled", False):
                        # src = 本场次的日期目录(<暂存根>/<主播>/<YYYY-MM-DD>/),
                        # 优先用录制态里钉死的 out_dir —— 暂存根可以中途切换,
                        # 「按当前配置反推」会指到新根,把旧半场漏掉。
                        rec = _recordings.get(name) or {}
                        session_dir = rec.get("out_dir") or (
                            os.path.dirname(rec["output_path"])
                            if rec.get("output_path")
                            else _session_dir_pin.get(name)
                            or os.path.join(output_dir, name, time.strftime("%Y-%m-%d")))
                        # 无实际录制文件时目录不存在,直接跳过入队(否则重试 5 次后作废,污染队列)
                        if os.path.isdir(session_dir):
                            nas_mod.enqueue({"streamer": name, "src_path": session_dir,
                                             "room_id": last_room_id, "start": None,
                                             "date": os.path.basename(session_dir)})
                            _mark_archive_state(session_dir, "pending")
                        else:
                            log(f"[归档] {name} 本场无录制文件"
                                f"({os.path.basename(session_dir)}),跳过入队")
                    last_room_id = None
                recording = False
                _manual_paused.discard(name)  # 下播后清除暂停标记,下次开播恢复正常录制
                _clear_giveup(name)  # 下播即解除冷却,下次开播正常录
                _stop_recording(state, name)
                _stop_danmaku(name, cfg)
                _cancel_preview_capture(name)
            # 会话状态落盘:供重启后恢复(房间号/是否在播/本轮检测时刻)
            # 看不清的一轮(unknown)不能把"在播"抹成"下播",否则重启后会把
            # 正在进行的场次当成历史会话。
            store.set_session(anchor_now, last_room_id,
                              bool(result.get("is_live")) or (unknown and recording),
                              now)

        store.decay()
        interval = next_interval(recording, in_hot, cfg)
        phase = "直播中" if recording else ("热点" if in_hot else "冷门")
        ni = interval if next_in_hint is None else next_in_hint
        state.set_streamer(name, _snap(name, anchor_now, phase, store, now, ni, result, probe))
        return "ok", interval

    while True:
        kind, value = await once()
        if kind == "noprobe":
            await asyncio.sleep(value)
            continue
        if kind == "cooldown":
            await asyncio.sleep(min(value, 5))
            continue

        # 正常检测完成:等待到下次自动检测,期间响应手动刷新(不改变 deadline)
        deadline = time.time() + value
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            if state.take_refresh(name):
                # 手动刷新:立即检测一次,不重置 deadline,不影响下次自动检测时间。
                # force=True —— 休眠中的主播也要能被手动唤醒查一次(不然关掉自动
                # 录制后就再也查不到他在不在播了)。
                rk, _ = await once(next_in_hint=max(0.0, deadline - time.time()), force=True)
                if rk in ("noprobe", "cooldown"):
                    break  # 回到外层循环按特殊状态处理
                continue
            await asyncio.sleep(min(0.5, remain))


async def supervise(client, mon, state, store):
    """协程守护:捕获异常按指数退避重启,退避中上报「异常重启」。"""
    name = mon.get("name", "?")
    anchor = str(mon.get("anchor", ""))
    backoff = 60  # 1min
    # 立即上报初始状态,保证 UI 增删主播尽快可见
    state.set_streamer(name, _snap(name, anchor, "启动中", store, None, None, None))
    while True:
        try:
            await run_streamer(client, mon, state, store)
            backoff = 60  # 正常退出才复位(实际 run_streamer 为无限循环)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[守护] {name} 协程异常: {e},{backoff}s 后重启")
            state.set_streamer(name, _snap(name, anchor, "异常重启", store, None, backoff, {"error": str(e)}))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)  # 1→2→4→8→10min 上限


async def reconcile(client, state, store):
    """每 5s 比对运行任务集合与 config.monitors:增启删取消;顺带轮询命令退出码。"""
    tasks = {}  # (name, anchor) -> asyncio.Task
    while True:
        cfg = state.get_config()
        desired = {}
        for m in cfg.get("monitors", []):
            key = (m.get("name", "?"), str(m.get("anchor", "")))
            desired[key] = m

        # 删除不再需要的任务
        for key in list(tasks):
            if key not in desired:
                task = tasks.pop(key)
                name, anchor = key
                snap = state.get_status()["streamers"].get(name)
                if snap and snap.get("is_live"):
                    run_command(state, cfg.get("on_offline_command", ""), {"name": name, "anchor": anchor})
                task.cancel()
                _stop_recording(state, name)
                _stop_danmaku(name, cfg)
                _cancel_preview_capture(name)
                store.forget(anchor)  # 清理会话状态(窗口/事件保留,便于重新添加后延续)
                state.remove_streamer(name)
                log(f"[移除] 停止监控: {name} ({anchor})")

        # 新增任务
        for key, m in desired.items():
            if key not in tasks:
                tasks[key] = asyncio.create_task(supervise(client, m, state, store))
                log(f"[新增] 启动监控: {m.get('name')} ({m.get('anchor')})")

        poll_commands(state)
        # 每 5s 刷新录制时长 + 维持截图任务(直播中且未设头像时保证备选图不中断;
        # 覆盖「用户删除头像后重新截图」「截图任务结束后自动补」场景)
        for rn in list(_recordings):
            _sync_recording_state(state, rn)
            _ensure_preview_capture(state, rn)
        await asyncio.sleep(5)


async def daily_analysis_loop(store):
    """每日 analysis_hour(默认 05:00)全量分析:修剪事件日志 + 重算所有主播热点窗口。

    偏移触发的即时分析在 _record_open → record_open 内完成;
    这里兜底「缓慢漂移」——每次偏离 ≤ 阈值时实时层只做合并,不会触发即时分析,
    只有每日全量重算(近 3 天事件加权 ×2)才能把中心逐步拉到新时段。
    """
    last_date = None
    while True:
        try:
            health_mod.beat("analysis")
            hour = int(store.cfg.get("analysis_hour", 5))
            now = time.time()
            today = time.strftime("%Y-%m-%d")
            if time.localtime(now).tm_hour == hour and last_date != today:
                last_date = today
                store.prune_events(now)
                anchors = set(store.events.keys()) | {
                    a for a in store.data.keys() if a != "_meta"}
                n = 0
                for a in sorted(anchors):
                    if store.analyze(a, now):
                        n += 1
                log(f"[分析] 每日热点分析完成:检查 {len(anchors)} 个主播,重算 {n} 个")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[分析] 每日分析异常: {e}")
        await asyncio.sleep(60)


async def danmaku_patrol_loop(state):
    """每 60s 弹幕对齐巡检:锚定新分片、检测视频停滞/弹幕缺口、重生成 .ass。

    分片 ↔ 字幕一一对应(同目录同名 .ass),每片锚自己的首包墙钟,
    断链/缺失不会向后传播错位;异常写入该场次的 align.json。
    """
    while True:
        try:
            health_mod.beat("danmaku")
            cfg = state.get_config()
            if (cfg.get("danmaku") or {}).get("enabled", False):
                for name in list(_danmaku_paths):
                    dm = danmaku_mod.get(name)
                    if dm is None or dm.dead:
                        continue
                    rec = _recordings.get(name)
                    if rec:  # ffmpeg 重启产生新前缀 → 切换 jsonl
                        danmaku_mod.retarget_for(name, rec["output_path"])
                        _danmaku_paths[name] = rec["output_path"]
                    out = _danmaku_paths.get(name)
                    if not out:
                        continue
                    _, anomalies = subtitle_mod.patrol(
                        out, cfg, drift_ms=dm.clock_drift_ms,
                        last_msg_ts=dm.last_msg_ts)
                    for a in anomalies:
                        log(f"[弹幕] {name} 巡检: {a}(已记录到 align.json)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[弹幕] 巡检异常: {e}")
        await asyncio.sleep(60)


# ── 礼物健康度(轻通道完整性)──────────────────────────────────────────────
#
# 2026-10-29 定案:全主播礼物口径统一为**轻礼物通道**(WebcastLightGiftMessage)。
# 完整通道(WebcastGiftMessage)匿名侧 349 场实测恒 0 条,且与轻通道是事件级互斥
# 路由,同收会重复计数 —— 故它不再落盘、不参与任何统计。
#
# 健康度只回答一个问题:轻通道下发的每一条礼物,是不是都完整落进了 jsonl 那 4 个
# 承诺字段(ts / 礼物标识 / 单价 / 份数)。判定分两类:
#   · fault   :解析失败(proto 失配)、写盘失败(磁盘/权限) —— 数据真的丢了
#   · suspect :字段缺项、条数对不上 —— 数据在但不完整
#   · quiet   :本场未下发轻礼物 / 房间冷清 / 已收礼但暂时静默 —— 只落盘记录
#
# "弹幕热闹却零礼物"不再推手机。349 场实测已证伪:那 56 场是服务端整组关掉礼物
# 互动消息(轻礼物+彩蛋+互动分值同时为 0),重连/换参数/游客态四重证伪过。拿它
# 告警只会把人训练成无视告警;留给历史页 gift_integrity.light 与日报口径行解释。

# 严重度:数字越大越该打扰人
GIFT_LEVELS = {"fault": 2, "suspect": 1, "quiet": 0}


def _light_integrity_diag(dm):
    """轻通道完整性诊断 —— 礼物健康度的**唯一**判据(2026-10-29 定案)。

    口径:礼物数据只来自轻礼物通道(`WebcastLightGiftMessage`)。健康度问的不是
    "这场有没有人送礼"(那是主播的人气,不是系统的健康),而是:

        轻通道下发的每一条礼物,是不是都完整落进了 jsonl 的 4 个承诺字段?

    所以这里只对账三个环节,任何一环不齐都是真实的数据缺口:
      ① 解析:收到但解析失败 → proto 可能与线上失配(改动前的静默盲区)
      ② 落盘:解析成功但写不进去 → 磁盘满 / 句柄失效
      ③ 字段:落盘了但 ts/gift/diamond/count 缺项 → 解析分支漏取
    送礼人/礼物名不参与判定:轻通道协议本身不带(实测 100%,登录态同样没有),
    拿它算不完整等于每天报警。

    dm 为 DanmakuRecorder 或替身(用 getattr 容错,测试替身不必补齐所有字段)。
    返回 (code, level, detail) 或 None(无完整性缺口)。
    """
    if dm is None:
        return None
    parse_err = int(getattr(dm, "light_gift_errors", 0) or 0)
    write_err = int(getattr(dm, "light_write_errors", 0) or 0)
    recv = int(getattr(dm, "gift_light_recv", 0) or 0)
    kept = int(getattr(dm, "gift_light_count", 0) or 0)
    miss = getattr(dm, "light_field_miss", None) or {}
    try:
        miss = {k: int(v) for k, v in dict(miss).items() if int(v or 0) > 0}
    except Exception:
        miss = {}
    if parse_err:
        return ("LIGHT_GIFT_PARSE_ERR", "fault",
                f"轻礼物解析失败 {parse_err} 条(全是匿名侧唯一礼物来源,"
                f"proto 可能与线上协议失配)")
    if write_err:
        return ("LIGHT_GIFT_WRITE_ERR", "fault",
                f"轻礼物已解析但写盘失败 {write_err} 条"
                f"(检查磁盘空间 / .meta 目录权限)")
    if miss:
        tail = "、".join(f"{k} 缺 {v} 次" for k, v in sorted(miss.items()))
        return ("LIGHT_FIELD_MISS", "suspect",
                f"轻礼物已落盘 {kept} 条,但字段不齐:{tail}"
                f"(轻通道只承诺 ts/gift/diamond/count 四项)")
    # 对不上的账:收到了、既没解析失败也没写盘失败、却比落盘多
    lost = recv - kept - parse_err - write_err
    if lost > 0:
        return ("LIGHT_EVENT_LOST", "suspect",
                f"轻礼物收到 {recv} 条、落盘 {kept} 条,差 {lost} 条无法归因"
                f"(非解析失败、非写盘失败)")
    return None


def _gift_diagnose(dm, base_ts, silent, session_sec=0.0, busy_rate=2.0,
                   quiet_phase=True):
    """判断礼物通道此刻的状态。返回 (code, level, detail) 或 None(暂不结论)。

    分层顺序(先硬故障、再数据完整性、最后才是"有没有礼物"):
      1. 会话/连接层:DM_OFF / WS_DEAD / WS_NO_MSG / WS_SILENT —— 拿不到任何数据
         (只在 quiet_phase 下结论:开场几秒会话没建起来属正常,不该报故障)
      2. 轻通道完整性:LIGHT_GIFT_PARSE_ERR / LIGHT_GIFT_WRITE_ERR(fault)、
         LIGHT_FIELD_MISS / LIGHT_EVENT_LOST(suspect)—— 拿到了但没录全
      3. 通道态(仅在静默超阈值 quiet_phase=True 时才结论):
         LIGHT_CHAN_OFF(本场服务端未下发轻礼物,房间活跃)/ LIGHT_QUIET / LIGHT_OK
         —— 三者都是 quiet 级:只落盘记录,不推手机。

    quiet_phase=False 时只做第 2 层:完整性缺口不该等到"静默 30/45 分钟"才发现,
    每轮巡检都要能报;而"没人送礼"这类结论必须等静默达阈值才有意义。

    dm           DanmakuRecorder(可能为 None:弹幕没开或没启动)
    base_ts      静默计时起点(最后礼物时刻或开录时刻)
    silent       已静默秒数
    session_sec  本场已录秒数(算弹幕活跃速率用)
    busy_rate    「弹幕很热闹」的阈值(条/分钟)
    quiet_phase  是否已到「静默超阈值」,可以下通道态结论
    """
    if dm is None:
        # 开场几秒内弹幕会话可能还没建起来;只有静默达阈值后 DM_OFF 才有意义。
        if not quiet_phase:
            return None
        return ("DM_OFF", "fault", "弹幕会话未启动(弹幕功能关闭或 room_id 取不到)")
    if dm.dead:
        if not quiet_phase:
            return None
        return ("WS_DEAD", "fault", f"弹幕连接重试 {dm.retries} 次后已放弃")
    # ── 第 2 层:轻通道完整性(与"有没有礼物"无关,随时可判)──
    comp = _light_integrity_diag(dm)
    if comp is not None:
        return comp
    if not quiet_phase:
        return None
    if dm.msg_count == 0:
        return ("WS_NO_MSG", "fault", "会话启动后一条消息都没收到")
    gap = time.time() - (dm.last_msg_ts or base_ts)
    if gap > 600:   # 10 分钟一条消息都没有:连接基本已经断了
        return ("WS_SILENT", "fault", f"已 {int(gap)}s 没收到任何消息(连接可能已断)")
    # ── 第 3 层:通道态(解释"为什么这半小时没有礼物",一律 quiet)──
    # ⚠️ 这里不再像旧版那样把"弹幕热闹却零礼物"判成 suspect:2026-10-29 全量
    #    349 场实测证明,那 56 场是**服务端压根没下发轻礼物**(平台侧开关),
    #    重连/换参数/游客态四重证伪过 —— 拿它去推手机只会把人训练成无视告警。
    recv = int(getattr(dm, "gift_light_recv", 0) or 0)
    kept = int(getattr(dm, "gift_light_count", 0) or 0)
    rate = dm.msg_count / max(1.0, session_sec) * 60   # 条/分钟
    if recv == 0:
        if rate >= busy_rate:
            return ("LIGHT_CHAN_OFF", "quiet",
                    f"本场服务端未下发轻礼物(平台侧开关,房间并不冷清:"
                    f"{rate:.1f} 条/分钟),逐条礼物结构性不可见 —— 非录制故障")
        return ("LIGHT_QUIET", "quiet",
                f"本场未收到轻礼物且房间冷清({rate:.1f} 条/分钟),"
                f"无法区分「没人送」与「未下发」")
    return ("LIGHT_OK", "quiet",
            f"轻通道已收 {recv} 条、完整落盘 {kept} 条,"
            f"已静默 {silent / 60:.0f} 分钟 —— 属正常无礼物流")


def _giftwatch_jsonl_path(output_path):
    """由录制输出路径推导本场诊断快照文件路径 .meta/<基名>.giftwatch.jsonl。

    与弹幕 jsonl 同基名(剥掉 .danmaku.jsonl 换后缀),_gift_record 落盘和
    _history_finish 的收尾计数共用这一份推导,避免两处各写一套规则漂移。
    """
    meta = danmaku_mod.jsonl_for(output_path or "")
    if not meta:
        return ""
    suffix = ".danmaku.jsonl"
    return (meta[: -len(suffix)] if meta.endswith(suffix) else meta) + ".giftwatch.jsonl"


def _gift_diag_counts(output_path):
    """统计本场 giftwatch.jsonl 的诊断记录级别计数(历史行警示胶囊数据源)。

    返回 {"fault","suspect","muted","quiet","total"};文件不存在 → None
    (调用方不写字段,历史行不显示诊断胶囊);损坏行跳过不计。
    """
    path = _giftwatch_jsonl_path(output_path)
    if not path or not os.path.exists(path):
        return None
    counts = {"fault": 0, "suspect": 0, "muted": 0, "quiet": 0}
    total = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                lv = str(r.get("level") or "")
                counts[lv] = counts.get(lv, 0) + 1
                total += 1
    except OSError:
        return None
    counts["total"] = total
    return counts


def _gift_record(name, rec, item, dm):
    """诊断快照落盘到 .meta/<场次>.giftwatch.jsonl。

    一行日志说不清「那场为什么没礼物」,消息类型分布和各类错误计数才是证据 ——
    事后排查翻这个文件,一眼能看出是协议失配还是真没人送礼。追加写,
    结论升级会再记一条。
    """
    try:
        path = _giftwatch_jsonl_path(rec.get("output_path") or "")
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        snap = dict(item)
        snap["wall"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["at"]))
        if dm is not None:
            snap["snapshot"] = {
                "gift_count": dm.gift_count, "msg_count": dm.msg_count,
                "gift_errors": dm.gift_errors, "silent_errors": dm.silent_errors,
                "frame_errors": dm.frame_errors, "dead": dm.dead, "retries": dm.retries,
                "last_msg_ago": (round(time.time() - dm.last_msg_ts, 1)
                                 if dm.last_msg_ts else None),
                "method_top": [f"{m}={c}" for m, c in dm.method_counter.most_common(8)],
                # 轻通道三段账(2026-10-29):收到 / 落盘 / 各自的失败数。
                # 有这三行,事后翻快照就能判断"是通道没给"还是"给了没接住"。
                "light": {"recv": getattr(dm, "gift_light_recv", None),
                          "kept": getattr(dm, "gift_light_count", None),
                          "parse_errors": getattr(dm, "light_gift_errors", None),
                          "write_errors": getattr(dm, "light_write_errors", None),
                          "field_missing": getattr(dm, "light_field_miss", None),
                          "full_ignored": getattr(dm, "gift_full_count", None)},
            }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[礼物] {name} 诊断快照写盘失败: {e}")


def _gift_should_push(item, gw):
    """分级闸门。notify_level=fault 只推硬故障;suspect(默认)推故障 + 可疑。"""
    min_level = 2 if str(gw.get("notify_level") or "suspect").lower() == "fault" else 1
    return GIFT_LEVELS.get(item["level"], 0) >= min_level


def _gift_scan(cfg):
    """扫一遍在录场次,返回本轮需要「新告知」的告警(按严重度降序)。

    两段式(2026-10-29):
      · **完整性**每轮必检 —— 轻通道的解析/落盘/字段缺口,与"有没有礼物"无关,
        不该等到静默 30/45 分钟才被发现;
      · **通道态**只在静默达阈值后才结论,且一律 quiet(只落盘、不推送)。

    「零礼物」不设任何抑制闸:2026-10-30 起匿名可见性抽检已下线(命题被实测证伪,
    见《匿名礼物可见性抽检-下线说明》),没有任何档案能改变 quiet 的结论 ——
    能改变结论的只有轻通道完整性(真实故障)。
    """
    gw = cfg.get("gift_watch") or {}
    limit = float(gw.get("silent_minutes", 45) or 45) * 60
    cooldown = float(gw.get("cooldown_minutes", 60) or 60) * 60
    now = time.time()
    hits = []
    for name, rec in list(_recordings.items()):
        dm = danmaku_mod.get(name)
        # 基准 = 最后一次礼物时刻 与 本场开录时刻 中较晚者:
        #   送过礼 → 从最后一次送礼起算;一直没人送 → 从开录起算
        base = max((dm.last_gift_ts if dm else 0.0), float(rec.get("started_at") or now))
        silent = now - base
        diag = _gift_diagnose(
            dm, base, silent,
            session_sec=max(1.0, now - float(rec.get("started_at") or now)),
            busy_rate=float(gw.get("busy_chat_rate", 2) or 2),
            quiet_phase=silent >= limit)
        if diag is None:
            # 既没完整性缺口、也还没到静默阈值 → 撤下旧告警(收到礼物/新场次)
            _gift_alerts.pop(name, None)
            continue
        code, level, detail = diag
        prev = _gift_alerts.get(name)
        # 冷却:同场次同结论不重复打扰;结论升级(quiet→suspect/fault)立即再报
        if prev and now - prev["at"] < cooldown and (
                prev["code"] == code
                or GIFT_LEVELS.get(level, 0) <= GIFT_LEVELS.get(prev["level"], 0)):
            continue
        item = {"name": name, "code": code, "level": level, "detail": detail,
                "silent_min": round(silent / 60, 1), "at": now}
        _gift_alerts[name] = item
        _gift_record(name, rec, item, dm)
        hits.append(item)
    hits.sort(key=lambda x: (-GIFT_LEVELS.get(x["level"], 0), -x["silent_min"]))
    return hits


async def _gift_push(items, gw):
    """多路合并成一条推送 —— 逐条推会刷屏,也白吃额度。

    通道分发在 notify.resolve_channels / push_by_config(webui 的测试通知共用同一份,
    别在这里另写一份)。
    """
    tag = {"fault": "[故障]", "suspect": "[可疑]", "quiet": "[正常]"}
    lines = [f"{tag.get(i['level'], '·')} {i['name']} · {i['code']} · "
             f"静默 {i['silent_min']} 分钟\n    {i['detail']}" for i in items]
    body = "\n".join(lines) + "\n\n排查记录:场次目录 .meta/*.giftwatch.jsonl"
    await notify_mod.push_by_config(gw, f"礼物异常告警 · {len(items)} 路", body)


async def gift_watch_loop(state):
    """礼物静默巡检(60s):在录场次超阈值无礼物 → 诊断 + 分级告知。

    每一轮的结论都落盘,不管推不推送 —— 用户回头排查时靠的是这些记录,
    而不是当时有没有收到消息。
    """
    while True:
        try:
            health_mod.beat("giftwatch")
            cfg = state.get_config()
            gw = cfg.get("gift_watch") or {}
            if gw.get("enabled", True) and (cfg.get("danmaku") or {}).get("enabled", False):
                hits = _gift_scan(cfg)
                for h in hits:
                    log(f"[礼物][!] {h['name']} 静默 {h['silent_min']} 分钟 → "
                        f"{h['code']}({h['level']}): {h['detail']}")
                pushable = [h for h in hits if _gift_should_push(h, gw)]
                if pushable:
                    await _gift_push(pushable, gw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[礼物] 巡检异常: {e}")
        await asyncio.sleep(60)


# ── 每日质检日报(方案 A,2026-10-10)──────────────────────────────────
#
# 为什么要有它:巡检(patrol_loop)的结果只落 meta.json 和日志,用户永远看不到
# 「一切正常」的正反馈;登录态有效期到期也没有任何提前量,只能等失效后救火。
# 日报把每天 4 点的体检结果送到企微 —— 发得出去,机器人通路顺带验了;
# 发不出去,连接器授权过期这件事本身也暴露了。


def _expire_grade(days_left, dr):
    """登录态剩余天数分级。返回 (级别, 建议文案)。

    分级线可在 daily_report 里调:expire_warn_days(默认 14)/expire_alert_days(默认 7)。
    days_left 为 None 表示会话级 Cookie(无 expires 字段)——不报警,正常展示。
    """
    warn = float(dr.get("expire_warn_days", 14) or 14)
    alert = float(dr.get("expire_alert_days", 7) or 7)
    if days_left is None:
        return "ok", "会话级 Cookie(无固定有效期)"
    if days_left <= alert:
        return "alert", f"仅剩 {days_left:.0f} 天,请尽快重新扫码续期"
    if days_left <= warn:
        return "warn", f"剩 {days_left:.0f} 天,建议近期重新扫码续期"
    return "ok", f"剩 {days_left:.0f} 天"


def _seconds_until_next_run(hour, jitter_minutes, now=None, rng=None):
    """距下一次 hour 点(±抖动)的秒数。已过当天时刻则顺延到明天。

    抖动与 patrol_loop 同一个理由:固定时刻整点请求容易被识别为机器行为。
    抖动可能把时刻拉到 now 之前(比如 3:59 触发、抖 -90s),max(60, ...) 兜底。
    """
    now = time.time() if now is None else now
    rng = rng or random
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hour), 0, 0, 0, 0, -1))
    if target <= now:
        target += 86400
    jitter = rng.uniform(-jitter_minutes * 60, jitter_minutes * 60) if jitter_minutes > 0 else 0
    return max(60, target + jitter - now)


async def _cookie_check(state, cfg):
    """登录态自检。返回 {ok, state, reason, days_left, checked, last_age_h}。

    meta.last_check 在 8 小时内 → 复用最近巡检结果,不重复打 API(巡检 12h±2h
    一次,质量足够);超过 8 小时(或已报失效)→ 主动跑一次 verify 拿新鲜结论。
    """
    st = (auth_mod.status(cfg) if auth_mod else
          {"ok": False, "state": "unknown", "reason": "auth 模块不可用"})
    last = st.get("last_check") or 0
    age_h = max(0.0, (time.time() - last) / 3600)
    checked = False
    if (not st.get("ok") or age_h > 8) and (cfg.get("auth") or {}).get("enabled", True):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10, follow_redirects=False,
                                         trust_env=False) as client:
                r = await auth_mod.verify(client, cfg)
            st = dict(st, ok=r.get("ok"), state=r.get("state", st.get("state")),
                      reason=r.get("reason", ""), last_check=time.time())
            checked = True
        except Exception as e:
            st = dict(st, ok=False, state="unknown", reason=f"{type(e).__name__}: {e}"[:120])
            checked = True
    exp = st.get("expire_at") or 0
    days_left = max(0.0, (exp - time.time()) / 86400) if exp > 0 else None
    return {"ok": bool(st.get("ok")), "state": st.get("state", "unknown"),
            "reason": st.get("reason") or "", "days_left": days_left,
            "expire_at": exp, "checked": checked, "last_age_h": age_h}


def _daily_report_items(cookie, state, cfg):
    """跑完自检项,返回 (rows, force)。

    rows      [(图标, 标题, 明细)] —— 全部检查项,正常异常都在
    force     True = 这份报告必须发出去(有问题/红牌),无视 always_send
    """
    dr = cfg.get("daily_report") or {}
    rows, force = [], False

    # 登录态
    if cookie["ok"]:
        detail = ("刚实测通过" if cookie["checked"]
                  else f"校验通过({cookie['last_age_h']:.0f}h 前巡检)")
        rows.append(("✅", "登录态", detail))
    else:
        rows.append(("❌", "登录态", cookie["reason"] or cookie["state"]))
        force = True

    # 有效期分级
    grade, tip = _expire_grade(cookie["days_left"], dr)
    if grade == "alert":
        icon, force = "🔴", True
    elif grade == "warn":
        icon, force = "🟡", True
    else:
        icon = "✅"
    exp_at = cookie.get("expire_at") or 0
    when = time.strftime("%m-%d %H:%M", time.localtime(exp_at)) if exp_at > 0 else "未知"
    rows.append((icon, "登录态有效期", f"{tip}({when} 到期)"))

    # 缺号补号巡检:没待补主播时不占行;有就必须能在日报里看见"跑没跑、补到没"
    # —— 这条链路是后台自动跑浏览器,静默失效时用户没有任何别的线索。
    try:
        pend = rid_probe.pending_monitors(cfg)
        if pend:
            rst = _rid_state_load()
            norm_rb = rid_probe.normalize_cfg(cfg.get("rid_backfill") or {})
            n = len(pend)
            lr = rst.get("last_result") or {}
            stats = rst.get("stats") or {}
            reasons = stats.get("reasons") or {}
            top = "、".join(f"{k}×{v}" for k, v in
                           sorted(reasons.items(), key=lambda kv: -kv[1])[:3])
            cu = int((rst.get("circuit") or {}).get("global_until") or 0)
            nxt = int(rst.get("next_run_at") or 0)
            if rst.get("last_reason") == "window_expired":
                rows.append(("🟡", "补号巡检", f"{n} 位缺号,7 天窗口已到期,待人工续期"))
            elif cu > time.time():
                rows.append(("🟡", "补号巡检",
                             f"{n} 位缺号,冷却中至 {time.strftime('%m-%d %H:%M', time.localtime(cu))}"
                             f"({(rst.get('circuit') or {}).get('reason') or '-'})"))
            elif rst.get("last_reason") == "daily_cap":
                rows.append(("🟡", "补号巡检",
                             f"{n} 位缺号,今日已达上限 "
                             f"{rst.get('checks_today')}/{norm_rb.get('max_checks_per_day')}"))
            else:
                rows.append(("✅", "补号巡检",
                             f"{n} 位缺号,今日已查 {rst.get('checks_today') or 0}"
                             f"/{norm_rb.get('max_checks_per_day')} 次"
                             f",最近 {lr.get('name') or '-'}:{lr.get('verdict') or '-'}"
                             f",下次 {time.strftime('%H:%M', time.localtime(nxt)) if nxt else '-'}"
                             + (f",失败 {top}" if top else "")))
    except Exception:
        pass

    # 磁盘:主盘(录制目录所在卷) + 归档目标(外接盘/NAS 挂载点,配了才查)。
    # 路径不存在 ≠ 容量不足:外接盘没插/NAS 没挂载是独立故障,单独一句话说明。
    min_gb = float(dr.get("disk_min_gb", 10) or 10)
    targets = [("主盘", _resolve_output_dir(cfg))]
    arch = cfg.get("archive") or {}
    if arch.get("mode") == "external" and arch.get("external_dir"):
        targets.append(("归档盘", arch["external_dir"]))
    elif (cfg.get("nas") or {}).get("enabled") and (cfg.get("nas") or {}).get("mount_point"):
        targets.append(("NAS", os.path.expanduser(cfg["nas"]["mount_point"])))
    for label, path in targets:
        if not path or not os.path.isdir(path):
            rows.append(("⚠️", f"磁盘 · {label}", "路径不存在(未挂载/未插入或配置错误)"))
            force = True
            continue
        try:
            free = shutil.disk_usage(path).free / 2**30
        except Exception:
            rows.append(("⚠️", f"磁盘 · {label}", "容量查询失败,请手动确认"))
            force = True
            continue
        if free < min_gb:
            rows.append(("🔴", f"磁盘 · {label}", f"仅剩 {free:.0f}GB(<{min_gb:.0f}GB)"))
            force = True
        else:
            rows.append(("✅", f"磁盘 · {label}", f"剩 {free:.0f}GB"))

    # 健康面板:warn/down 的常驻协程(idle=启动中/未接入,不算问题)
    snap = health_mod.snapshot(cfg)
    bad = [(it.get("key"), it.get("detail") or it.get("status"))
           for g in snap.get("groups", []) for it in g.get("items", [])
           if it.get("status") in ("warn", "down") and it.get("key") != "dailyreport"]
    if bad:
        rows.append(("⚠️", "健康面板", "; ".join(f"{k}:{str(d)[:40]}" for k, d in bad[:4])))
        force = True
    else:
        rows.append(("✅", "健康面板", "全部协程正常"))

    # 归档:队列失败 + 历史失败(pending 是正常排队,不算)
    q = nas_mod.queue_status() or {}
    hist_fail = sum(1 for x in _history
                    if isinstance((x.get("archive") or {}), dict)
                    and (x.get("archive") or {}).get("state") == "failed")
    if q.get("failed") or hist_fail:
        rows.append(("⚠️", "归档", f"队列失败 {q.get('failed', 0)} · 历史失败 {hist_fail}"
                     f"(录制历史页可看明细)"))
        force = True
    else:
        rows.append(("✅", "归档", "无失败"))

    # 礼物口径(2026-10-29 定案轻通道):近 24h 收尾场次按 gift_channel 分类,
    # 并单独统计「轻通道完整性缺口」场次。前者解释"为什么没有礼物"(平台未下发 /
    # 冷清,都不是故障);后者才是系统问题(轻通道给了、我们没录全)——
    # 只有它亮 ⚠️,其余仅陈述。
    try:
        cut = time.time() - 86400
        tally = {"light": 0, "full_only": 0, "quiet": 0, "unknown": 0}
        partial = 0
        for e in _history:
            if not isinstance(e, dict):
                continue
            if float(e.get("started_at") or 0) < cut:
                continue
            gi = e.get("gift_integrity") or {}
            ch = gi.get("gift_channel")
            if ch in tally:
                tally[ch] += 1
            if ((gi.get("light") or {}).get("status")) == "partial":
                partial += 1
        total = sum(tally.values())
        if total:
            detail = (f"轻场 {tally['light']} · 轻通道未下发 {tally['full_only']}"
                      f" · 冷清 {tally['quiet']}")
            if tally["unknown"]:
                detail += f" · 未判定 {tally['unknown']}"
            if partial:
                detail += f" · 完整性缺口 {partial} 场"
            rows.append(("⚠️" if partial else "✅", "礼物口径", detail))
    except Exception:
        pass

    return rows, force


def _daily_report_text(rows, now=None):
    """组装日报正文(markdown);标题由调用方给。"""
    ts = time.strftime("%m-%d %H:%M", time.localtime(now if now is not None else time.time()))
    bad = sum(1 for i, _, _ in rows if i not in ("✅",))
    head = f"📋 **每日质检日报** · {ts}" + (f" · ⚠️ {bad} 项需关注" if bad else " · 全部正常")
    body = "\n".join(f"{i} **{t}**: {d}" for i, t, d in rows)
    return f"{head}\n\n{body}"


def _sync_gift_dict():
    """同步官方礼物字典（阻塞 IO，调用方要用 to_thread）。供日报协程每天调一次。"""
    try:
        import gift_dict as gift_dict_mod
        return gift_dict_mod.sync()
    except Exception:
        return None


async def daily_report_loop(state, log=print):
    """每天 hour 点(默认 4 点 ±30min)体检一次并发日报。异常自愈,绝不拖垮主循环。"""
    while True:
        try:
            health_mod.beat("dailyreport")
            cfg = state.get_config()
            dr = cfg.get("daily_report") or {}
            if not dr.get("enabled", True):
                await asyncio.sleep(3600)
                continue
            hour = int(dr.get("hour", 4) or 4)
            wait = _seconds_until_next_run(hour, float(dr.get("jitter_minutes", 30) or 0))
            log(f"[日报] 下次质检 {wait / 3600:.1f}h 后(hour={hour}±抖动)")
            await asyncio.sleep(wait)
            health_mod.beat("dailyreport")

            cfg = state.get_config()          # 睡醒后重取,配置可能已改
            dr = cfg.get("daily_report") or {}
            cookie = await _cookie_check(state, cfg)
            # 官方礼物字典每天刷一次：名称/单价只认官方 gift/list（匿名、1 请求）。
            # 失败不影响日报与录制（字典沿用旧快照，缺失时名称降级为 礼物#id）。
            try:
                gst = await asyncio.to_thread(_sync_gift_dict)
                if gst:
                    log(f"[礼物字典] 已刷新 {gst}")
            except Exception as exc:                     # noqa: BLE001
                log(f"[礼物字典] 刷新失败(沿用旧快照): {exc}")
            rows, force = _daily_report_items(cookie, state, cfg)
            gw = cfg.get("gift_watch") or {}
            # always_send=False 且没有强制事项 → 只落日志(安静模式)
            if not (dr.get("always_send", True) or force):
                log("[日报] 全部正常(安静模式,不发消息) — " +
                    "; ".join(t for _, t, _ in rows))
                continue
            r = await notify_mod.push_by_config(
                gw, "每日质检日报" + ("⚠️" if any(i != "✅" for i, _, _ in rows) else ""),
                _daily_report_text(rows), local=force)
            if r.get("ok"):
                log(f"[日报] 已发送(通道:{','.join(r.get('channels') or [])})")
            else:
                log(f"[日报][!] 发送失败: {r.get('error', '')} — 机器人通路请检查连接器授权")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[日报] 异常: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(1800)


async def retention_loop(state):
    """孤儿主播目录延迟清理:启动时先跑一次,之后每 6 小时一次。

    主播卡片删掉后不立刻清目录 —— 先登记进 spool/retention.json,满 30 天
    且确认内容已归档才真正删除;期间主播被加回来会自动撤销。只清理项目内的
    previews/ 与 recordings/,NAS 上的归档副本不动。
    """
    while True:
        try:
            health_mod.beat("retention")
            cfg = state.get_config()
            r = retention_mod.run_due(cfg, list(_history), log=log)
            m, s = r["mark"], r["sweep"]
            if m["added"]:
                log(f"[清理] 发现孤儿目录,已登记(满 {retention_mod.GRACE_DAYS} 天后清理): "
                    + "、".join(m["added"]))
            if m["revoked"]:
                log(f"[清理] 主播已重新添加,取消清理: " + "、".join(m["revoked"]))
            if s["deleted"]:
                log(f"[清理] 已清理到期孤儿目录: " + "、".join(s["deleted"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[清理] 孤儿目录巡检异常: {e}")
        await asyncio.sleep(retention_mod.CHECK_INTERVAL)


async def interop_feedback_loop(state):
    """S3.B 问题反馈消费(600s 轮询):转录端管线发现坏素材/坏对齐时写包到
    NAS _对接/问题反馈/,这里取走(移入已处理/),error 级即时企微告警。

    旁路铁律:NAS 未挂载静默跳过、坏包留下轮重试、任何异常只记日志 ——
    绝不影响检测/录制/归档主链路。字段语义见对端手册 §3.2。
    """
    while True:
        try:
            cfg = state.get_config()
            consumed, _problems = interop_mod.consume_pending(cfg, log=log)
            health_mod.beat("issue_feedback", count=len(consumed))
            for rec in consumed:
                if str(rec.get("severity")) == "error":
                    await notify_mod.push_by_config(
                        cfg.get("gift_watch") or {},
                        f"[问题反馈] {rec.get('summary') or '转录端报告问题'}",
                        f"session_key: {rec.get('session_key')}\n"
                        f"stage: {rec.get('stage')} | exit: {rec.get('exit_code')}"
                        f" | run_id: {rec.get('run_id')}\n"
                        f"hint: {rec.get('hint') or '-'}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[对接] 问题反馈轮询异常: {e}")
            health_mod.beat("issue_feedback", error=str(e))
        await asyncio.sleep(interop_mod.FEEDBACK_POLL_SEC)


# ── 缺号主播补号巡检(web_rid backfill) ─────────────────────────────────────
#
# 为什么要这套东西:添加主播必须有 web_rid 才能检测开播,而 web_rid 只在「直播间
# 链接」或**渲染后的主页**里出现 —— 抖音号/资料接口都给不出来(2026-10-26 实测)。
# 于是「先加、开播后自动补」成了唯一不打扰用户的路径。
#
# 三层分工,别搞混:
#   web_rid_probe.py    纯逻辑(何时跑/跑谁/算不算失败/熔断),零 IO、可穷举单测;
#   web_rid_browser.py  唯一碰 playwright 的地方,一次一进程,用完即关;
#   本段                把两者接进事件循环 + 落 state + 写回 config + 打日志。
#
# ⚠️ 浏览器调用一律 `asyncio.to_thread`:sync_playwright 会阻塞事件循环,
# 直接 await 它会把检测/录制/看门狗全部卡住(本项目对这类阻塞极其敏感)。


def _rid_state_load():
    """读巡检状态。文件缺失/损坏都返回初始空状态,绝不抛。"""
    now = int(time.time())
    try:
        with open(RID_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return rid_probe.empty_state(now)
    except (OSError, json.JSONDecodeError) as e:
        log(f"[补号][!] 状态文件损坏({type(e).__name__}),按空状态继续: {e}"[:200])
        return rid_probe.empty_state(now)
    return rid_probe.merge_state(raw, now)


def _rid_state_save(st):
    try:
        os.makedirs(os.path.dirname(RID_STATE_PATH), exist_ok=True)
        _atomic_write_json(RID_STATE_PATH, st)
    except OSError as e:
        log(f"[补号][!] 状态落盘失败: {e}"[:160])


def _rid_fail_audit(entry, cap=500):
    """失败追加到 jsonl 审计(只留最近 cap 条)。

    业务日志只留 8 份、跨天翻不到,而"某天为什么一个都没补到"恰恰要跨天复盘,
    所以失败必须单独留一份可 grep 的账。
    """
    try:
        os.makedirs(os.path.dirname(RID_FAILS_PATH), exist_ok=True)
        lines = []
        if os.path.exists(RID_FAILS_PATH):
            with open(RID_FAILS_PATH, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > cap:
            lines = lines[-cap:]
        tmp = RID_FAILS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, RID_FAILS_PATH)
    except OSError:
        pass


def _rid_apply_web_rid(name, anchor, rid, log=log):
    """把 rid 合并进 config.json:读最新 → 只补字段 → mtime 校验 → 原子写。

    为什么这么小心:`POST /api/config` 是 **monitors 整体替换**语义,后台写盘与
    前端保存一交叉就会丢主播(历史事故:10 位被覆盖成 1 位)。冲突时**放弃本轮**
    永远优于覆盖 —— 下一轮巡检还会再来一次。
    返回 (结果码, 实际写入的 cfg 或 None)。
    """
    for _attempt in (1, 2):
        try:
            st0 = os.stat(CONFIG_PATH)
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)          # 读原始文件,不做默认值合并(不替用户补键)
        except (OSError, json.JSONDecodeError) as e:
            return "read_failed", None
        new_cfg, code = rid_probe.merge_web_rid(raw, name, anchor, rid, time.time())
        if new_cfg is None:
            return code, None
        try:
            st1 = os.stat(CONFIG_PATH)
        except OSError:
            return "stat_failed", None
        if (st0.st_mtime_ns, st0.st_size) != (st1.st_mtime_ns, st1.st_size):
            log("[补号] config 在本次合并期间被改动(可能是前端保存),重读重试一次")
            continue
        _atomic_write_json(CONFIG_PATH, new_cfg)
        return "ok", new_cfg
    return "conflict", None


def _rid_idle_sleep(norm, st, now, why):
    """不能跑的时候睡多久再复查。返回秒数。

    原则:状态类原因(冷却/日上限)睡到解禁;结构性原因(没待补/已过期/被关掉)
    用固定 10 分钟复查即可 —— 用户随时可能在控制台补一个主播进来。
    """
    if why == "cooling":
        until = int((st.get("circuit") or {}).get("global_until") or 0)
        return max(60, min(600, until - now))
    if why == "daily_cap":
        return max(600, min(7200, rid_probe.next_midnight(now) - now))
    if why == "backoff":
        pend = []
        for m in (st.get("per_monitor") or {}).values():
            u = int(m.get("backoff_until") or 0)
            if u > now:
                pend.append(u)
        return max(60, min(900, min(pend) - now)) if pend else 600
    return 600


async def rid_backfill_loop(state, log=log):
    """缺号主播补号巡检:三档随机间隔开一次浏览器,开播就把 web_rid 落进 config。

    任何异常都收敛在本协程内(记日志 + 退避),绝不让它拖垮主循环。
    """
    loop_circuit = rid_probe.Circuit()
    await asyncio.sleep(20)          # 起服务先让检测/录制把摊子铺开,别抢启动峰值
    while True:
        sleep_s = 600
        try:
            health_mod.beat("ridbackfill")
            st = _rid_state_load()
            cfg = state.get_config()
            norm = rid_probe.normalize_cfg(cfg.get("rid_backfill") or {})
            now = int(time.time())
            if norm.get("_normalized"):
                log("[补号][!] rid_backfill 配置非法已回落默认(档位/权重检查),"
                    "详见 web_rid_probe.normalize_cfg")
            if loop_circuit.reset_day(st, now):
                log("[补号] 跨日:当天次数/比例计数已归零")
            pending = rid_probe.pending_monitors(cfg)
            ok, why = rid_probe.should_run(norm, st, pending, now)
            st["last_reason"] = why
            if not ok:
                sleep_s = _rid_idle_sleep(norm, st, now, why)
                st["next_run_at"] = now + sleep_s
                _rid_state_save(st)
                # no_pending 是常态,别刷日志;其它原因每次说一句,便于排查"怎么没跑"
                if why != "no_pending":
                    log(f"[补号] 本轮跳过:{why}({sleep_s // 60} 分钟后复查)")
                await asyncio.sleep(sleep_s)
                continue

            batch = rid_probe.task_batch(norm, st, pending, now=now)
            if not batch:
                sleep_s = 600
                st["next_run_at"] = now + sleep_s
                _rid_state_save(st)
                await asyncio.sleep(sleep_s)
                continue

            log(f"[补号] 开始巡检 {len(batch)} 位缺号主播"
                f"(待补共 {len(pending)} 位,窗口至 "
                f"{time.strftime('%m-%d %H:%M', time.localtime(int(st.get('window_until') or 0)))}）")
            unk = 0
            checked = 0
            t_round = time.time()
            for idx, m in enumerate(batch):
                if time.time() - t_round > norm.get("round_budget_sec", 180):
                    log("[补号] 本轮超出预算,剩余主播留到下一轮")
                    break
                name = str(m.get("name") or "")
                sec = str(m.get("anchor") or "")
                timeout = int(norm.get("timeout_sec", 45))
                # 录制中把页面预算收紧:宁可这轮记 timeout,也别跟拉流抢 CPU
                try:
                    if state.get_status().get("recordings"):
                        timeout = min(30, timeout)
                except Exception:
                    pass
                try:
                    res = await asyncio.to_thread(
                        rid_browser.fetch_web_rid, sec,
                        timeout_sec=timeout, user_data_dir=RID_BROWSER_DIR)
                except Exception as e:  # noqa: BLE001
                    res = {"verdict": "unknown", "rid": None, "reason": "browser_crash",
                           "detail": f"{type(e).__name__}: {e}"[:150], "elapsed_ms": 0}
                checked += 1
                st["checks_today"] = int(st.get("checks_today") or 0) + 1
                verdict = str(res.get("verdict") or "unknown")
                reason = res.get("reason")
                rid = res.get("rid")
                detail = str(res.get("detail") or "")[:160]
                elapsed = int((res.get("elapsed_ms") or 0) / 100) / 10.0
                now_i = int(time.time())

                if verdict == "unknown":
                    unk += 1

                if verdict == "live" and rid:
                    code, _cfg_new = _rid_apply_web_rid(name, sec, rid)
                    if code == "ok":
                        try:
                            state.request_refresh(name)
                        except Exception:
                            pass
                        st.setdefault("stats", {})
                        st["stats"]["filled_total"] = int(
                            st["stats"].get("filled_total") or 0) + 1
                        st["stats"]["last_filled_at"] = now_i
                        st["stats"]["last_filled_name"] = name
                        log(f"[补号] ✅ {name} 已补齐 web_rid={rid}"
                            f"(elapsed={elapsed}s),下一轮检测即生效")
                        nbf = norm.get("notify_on_fill", True)
                        if nbf:
                            try:
                                gw = state.get_config().get("gift_watch") or {}
                                await notify_mod.push_by_config(
                                    gw, "补号成功",
                                    f"主播「{name}」已自动补齐直播间号 {rid},现在开始正常检测开播。",
                                    local=False)
                            except Exception as e:  # noqa: BLE001
                                log(f"[补号] 通知发送失败(不影响补齐):{type(e).__name__}")
                    else:
                        verdict, reason = "unknown", "merge_" + code
                        log(f"[补号][!] {name} 补写 config 未成功:{code}"
                            f"(rid={rid},已有值竞态或主播已被删)")
                    act = loop_circuit.record(st, name, verdict, reason, now_i, rid=rid)
                else:
                    act = loop_circuit.record(st, name, verdict, reason, now_i)

                st["last_result"] = {"name": name, "verdict": verdict,
                                     "reason": reason, "at": now_i,
                                     "detail": detail}
                if verdict == "offline":
                    log(f"[补号] {name}:未开播(已确认,非失败) elapsed={elapsed}s {detail}")
                elif verdict == "unknown":
                    log(f"[补号] FAIL {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_i))} "
                        f"name={name} anchor={sec[:10]}… reason={reason} detail=\"{detail}\" "
                        f"fails={act.get('fails', '?')}/{rid_probe.SINGLE_FAILS} "
                        f"backoff={'%.0fh' % ((act.get('backoff_until', 0) - now_i) / 3600.0) if act.get('backoff_until', 0) > now_i else 'none'}")
                    _rid_fail_audit({"ts": now_i, "name": name, "anchor_tail": sec[-6:],
                                     "reason": reason, "detail": detail,
                                     "fails": act.get("fails"), "round": st.get("cursor")})
                    # 按原因码累计:日报/state 里能看到"今天到底是被什么拦住的"
                    try:
                        rr = st.setdefault("stats", {}).setdefault("reasons", {})
                        rr[str(reason)] = int(rr.get(str(reason)) or 0) + 1
                    except Exception:
                        pass
                    if act.get("action") == "global_cooldown":
                        _cu = int((st.get("circuit") or {}).get("global_until") or 0)
                        log(f"[补号][!] 触发全局冷却({reason}),暂停巡检至 "
                            f"{time.strftime('%H:%M', time.localtime(_cu))}")

                # 轮内间隔:串行 + 随机,别在同一秒连打两个主页
                if idx < len(batch) - 1:
                    gap = random.randint(int(norm.get("gap_sec_min", 25)),
                                         max(int(norm.get("gap_sec_min", 25)),
                                             int(norm.get("gap_sec_max", 45))))
                    await asyncio.sleep(gap)

            act = loop_circuit.record_round(st, unk, checked, int(time.time()))
            if act and act.get("action") == "day_stop":
                log(f"[补号][!] 当天异常比例过高/冷却过多({act.get('reason')}),"
                    f"巡检停到次日 00:00")
            plan = rid_probe.plan_next_run(norm, st, int(time.time()))
            st["last_run_at"] = int(time.time())
            st["next_run_at"] = plan["next_run_at"]
            _rid_state_save(st)
            sleep_s = max(30, plan["next_run_at"] - time.time())
            log(f"[补号] 本轮结束:检查 {checked} 位 / 未查到 {unk} 位 / "
                f"下次 {time.strftime('%m-%d %H:%M', time.localtime(plan['next_run_at']))}"
                f"(档位 T{plan['tier']},{plan['minutes']} 分钟)")
            await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"[补号] 巡检异常: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(600)


async def main():
    global _web_state, _cookie_guard, _http_client, _quality_gov
    _register_signal_handlers()
    # 崩溃事件登记:必须在任何协程起来之前 —— 它会读上一次的落盘,若发现上次
    # 没走正常退出(clean_exit 仍是 False),就地记一条「主进程异常退出」。
    # 这件事没人做的话,主进程崩溃后唯一的痕迹是 launchd.out.log 里少了一行。
    try:
        incidents_mod.begin_session()
        # 「服务启动」也留痕:用户要的是「任何一处重启都看得见」——主进程重启
        # (手动 restart 或 launchd 拉起)同样会打断全部录制,必须能事后回看。
        incidents_mod.note_start("PID %d" % os.getpid())
    except Exception as e:                       # noqa: BLE001
        log(f"[告警] 崩溃事件表初始化失败(不影响运行): {e}")
    config = load_config()
    store = HotspotStore(HOTSPOT_PATH, config.get("schedule", {}), HOTSPOT_EVENTS_PATH)
    state = State(CONFIG_PATH, config, store)
    # Cookie 护栏:全局唯一实例,规则随配置热更新(见 _sync_cookie_guard)
    _cookie_guard = CookieGuard(config)
    state.set_cookie_guard(_cookie_guard)
    # 注入前端 state 引用:此后 history 的每次变更都会同步给前端
    _web_state = state
    _load_history()
    state.set_history(_history)
    # 重新定位:把本机已不在(多为已归档)的历史条目指向归档目标中的实际位置
    try:
        n = _relocate_history(config)
        if n:
            log(f"[历史] 重新定位 {n} 条归档记录(已指向归档目标)")
    except Exception as e:
        log(f"[历史] 重新定位失败: {e}")
    state.set_history(_history)
    # 注入录制手动控制回调(供 webui 停止/开始录制)
    state.set_rec_ctrl({
        "stop": lambda name: _stop_recording_manual(state, name),
        "start": lambda name: _start_recording_manual(state, name),
    })
    # 注入画质调度的人工解锁入口(抖动锁定后只能人工恢复)
    def _unlock_quality():
        if not _quality_gov:
            return {}
        snap = _quality_gov.unlock()
        quality_mod.save_state(QUALITY_STATE_PATH, _quality_gov.dump())
        log("[画质] 人工解除抖动锁定,已回到 L0 并重新采样")
        return snap
    state.set_quality_ctrl({"unlock": _unlock_quality})
    # 实时预览(按需独立拉流):注入流地址提供者 —— 仅主播在录制时给出地址
    live_preview_mod.set_url_provider(_preview_url_provider)
    # 注入归档结果回调(供 nas 模块回写 history 的归档状态)
    nas_mod.set_archive_hook(_on_archive_result)
    # ── 画质调度器:档位状态跨重启沿用(崩溃后不抖) ──
    # boot_id 变化 + 上次没干净退出 = 崩过一次 → 记一笔崩溃信号,5 min 内不再
    # 试探性恢复(否则每次正常 `douyin restart` 也会被当成崩溃而误降档)。
    gov = quality_mod.Governor(config.get("recorder") or {})
    gov.boot_id = quality_mod.new_boot_id()
    prev = quality_mod.load_state(QUALITY_STATE_PATH)
    if prev:
        ok = gov.restore(prev, time.time())
        if not ok:
            log("[画质] 档位状态文件损坏,已回到 L0")
        elif prev.get("boot_id") and prev.get("boot_id") != gov.boot_id \
                and not prev.get("clean_exit", False):
            gov.note_crash("boot:%s" % prev.get("boot_id"))
            log(f"[画质] 上次进程未干净退出(档位 L{prev.get('level')}),"
                f"按崩溃处理,沿用档位且 30 分钟内不恢复")
    _quality_gov = gov
    if not (config.get("recorder") or {}).get("video_mode") == "vt":
        log("[画质] 当前编码路线非 vt(硬编),自动降级不生效")
    else:
        ok, reason = _hw_probe()
        log(f"[画质] 硬件编码探针: {'可用' if ok else '不可用 — ' + reason}")
    log(f"[画质] 档位 L{int(getattr(gov, 'level', 0) or 0)} · "
        f"{quality_mod.degrade_plan(config).get('reason', '')}")
    # 注入「正在录制的场次目录」探针:nas 归档时据此拒绝搬运正在写入的目录。
    # 铁律,2026-09-18 事故:日期目录被整目录搬走后 rmtree,ffmpeg 的写句柄落到
    # 已删除的 inode 上 —— 整段录制写进幽灵文件永久丢失,目标盘只剩截断的 .flv。
    nas_mod.set_active_probe(lambda: {
        os.path.dirname(r["output_path"]) for r in list(_recordings.values())
        if r.get("output_path")})
    host = config.get("webui", {}).get("host", "127.0.0.1")
    port = int(os.environ.get("DOUYIN_MONITOR_PORT", config.get("webui", {}).get("port", 8780)))
    start(state, host, port)
    log(f"Web UI: http://{host}:{port}")

    monitors = config.get("monitors", [])
    log(f"监控 {len(monitors)} 个主播")

    # 启动自检摘要(B2)
    for c in run_checks(config):
        if c["status"] != "ok":
            log(f"[自检] {c['status']}: {c['title']} — {c['detail']}")

    if PURGED_PROXY_VARS:
        log(f"[自检] 已清除 {PURGED_PROXY_VARS} 个环境代理变量"
            f"(抖音检测与拉流需直连;确需代理请设 DOUYIN_KEEP_ENV_PROXY=1)")

    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
                                 trust_env=False) as client:
        _http_client = client      # 供矛盾状态校验复用,避免每场都新建连接
        nas_task = asyncio.create_task(nas_mod.nas_watch(state))
        analysis_task = asyncio.create_task(daily_analysis_loop(store))
        patrol_task = asyncio.create_task(danmaku_patrol_loop(state))
        # 礼物静默巡检(60s):长时间没礼物 → 诊断原因并分级告知
        gift_task = asyncio.create_task(gift_watch_loop(state))
        # 每日质检日报(04:00±抖动):机器人通路/登录态/磁盘/归档体检并发日报
        report_task = asyncio.create_task(daily_report_loop(state, log))
        # 企微机器人双向交互(45s 轮询单聊指令,菜单模式)
        bot_task = (asyncio.create_task(bot_mod.poll_loop(state, log))
                    if bot_mod else None)
        retention_task = asyncio.create_task(retention_loop(state))
        # 跨项目问题反馈消费(600s, 旁路):取走转录端回传的坏素材/坏对齐报告
        interop_task = asyncio.create_task(interop_feedback_loop(state))
        # 录制健康巡检(20s):独立于检测周期,及时发现「进程存活但无数据」的假在线
        watchdog_task = asyncio.create_task(_recording_watchdog(state))
        # 画质调度(5s):过载前主动降档。与看门狗互补 —— 一个事前,一个事后。
        quality_task = asyncio.create_task(_quality_loop(state))
        # 登录态巡检(默认 12h ± 2h 抖动):失效只影响检测通道,录制不受影响
        # 注意:不能写 `noop = asyncio.sleep(0)` 再在三元里二选一 —— 未被选中的那个
        # 协程对象永远不会被 await,退出时会刷 "coroutine was never awaited" 警告。
        # 条件表达式只会求值其中一个分支,故直接内联。
        auth_task = asyncio.create_task(
            auth_mod.patrol_loop(state, log)
            if (auth_mod and hasattr(auth_mod, "patrol_loop"))
            else asyncio.sleep(0))
        # 缺号主播补号巡检:有待补主播才开窗、才启浏览器;没待补时本协程只是在睡觉
        rid_task = asyncio.create_task(rid_backfill_loop(state, log))
        try:
            await reconcile(client, state, store)
        except asyncio.CancelledError:
            nas_task.cancel()
            analysis_task.cancel()
            patrol_task.cancel()
            watchdog_task.cancel()
            quality_task.cancel()
            gift_task.cancel()
            report_task.cancel()
            if bot_task:
                bot_task.cancel()
            auth_task.cancel()
            retention_task.cancel()
            interop_task.cancel()
            rid_task.cancel()
            raise
        except Exception as e:
            # 顶层兜底:log 后 re-raise,交给 launchd 拉起
            log(f"[致命] 监控主循环异常: {e}")
            raise
        finally:
            # 干净退出标记:重启后据此区分「用户重启」与「崩溃被拉起」,
            # 前者不该触发降档(否则每次 douyin restart 都白降一档)。
            try:
                if _quality_gov:
                    data = _quality_gov.dump()
                    data["clean_exit"] = True
                    quality_mod.save_state(QUALITY_STATE_PATH, data)
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
