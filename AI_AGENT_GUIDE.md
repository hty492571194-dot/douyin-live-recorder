# 抖音直播监控系统 · AI 智能体导航报告

> 目标读者：运行在其他设备上的 AI 智能体（跨机器代码定位）。
> 本机项目根：`/Users/a/Downloads/douyin-monitor`（以下路径均为相对此根的相对路径；跨设备部署后路径结构不变）。
> 迁移历史：2026-09-07 由 `douyin-monitor-phase-d-19-g3fe4950` 改名并重写绝对路径（见 `项目整理与迁移方案.md` 第五章）。
> 生成日期：2026-08-27，最后校对 2026-09-07。

---

## 1. 项目概述

**一句话目标**：自部署的抖音直播自适应监控——检测主播开播 → 自动 ffmpeg 录制（分片）→ 同步抓取弹幕/礼物（WebSocket）→ 生成外挂 ASS 字幕（底部队列样式）→ NAS 归档，附带 Web 控制台。

**当前状态**：生产运行中（macOS 主录制节点，launchd 常驻 `com.douyin.monitor`）。弹幕链路已修复心跳 bug（应用层心跳须用二进制数据帧，否则 ~50s 被踢线），真实直播验证连接稳定。

**技术栈 / 运行环境**：

| 项 | 值 |
|---|---|
| 语言 | Python ≥ 3.11（async asyncio + 多线程）、单文件前端（原生 HTML/JS，无构建） |
| 服务 | 内嵌 HTTP server（`webui.py`，默认 `127.0.0.1:8780`，可用环境变量 `DOUYIN_MONITOR_PORT` 覆盖） |
| 常驻 | macOS launchd（`launchctl kickstart -k gui/$(id -u)/com.douyin.monitor` 重启） |
| 外部二进制 | `ffmpeg`（`preflight.find_ffmpeg` 定位；录制、预览截图、流地址都依赖） |

**Python 依赖**（已补全 `requirements.txt`；部署用 `./setup.sh` 自动建 `.venv` 安装）：

| 包 | 用途 | 关键注意 |
|---|---|---|
| `httpx` | 检测通道 HTTP / WS 前的 ttwid cookie 获取 | AsyncClient 复用 |
| `websocket-client` | 弹幕 WS 长连接 | 心跳须 `OPCODE_BINARY` 发 protobuf（`danmaku.py:196`） |
| `betterproto==2.0.0b6` | protobuf 消息解析（`danmaku_proto.py`） | 版本锁定 |
| `mini-racer==0.14.1` | 执行 `scripts/danmaku_sign.js` 的 `get_sign()` 计算 WS 签名 | **导入名是 `py_mini_racer`** |

**关键数据文件**（均在项目根或 `recordings/` 下，JSON 可读）：

```
config.json            运行配置（见 §3 API 契约-配置段）
hotspots.json          热点窗口（含 _meta 会话状态）
hotspot_events.json    开播事件日志（分析层输入）
recordings/history.json  录制历史（output_path 为模板路径，含 %03d）
recordings/<主播昵称>/<name>-<yyyyMMdd-HHmmss>[-%03d].flv   视频分片
recordings/<主播昵称>/<name>-<yyyyMMdd-HHmmss>[-%03d].ass    外挂字幕（与分片同名同目录，播放器自动加载）
recordings/<主播昵称>/.meta/<name>-<ts>.danmaku.jsonl        弹幕明细（追加写，永久保留）
recordings/<主播昵称>/.meta/<name>-<ts>.align.json           对齐元数据（锚点/偏移/gaps/summary）
spool/pending.json      NAS 待归档队列
logs/monitor-*.log     业务日志；logs/launchd.out.log 弹幕 print 日志（danmaku 用 print 非 log()）
```

---

## 2. 功能索引表

> 列：功能 | 一句话描述 | 文件 | 关键符号 | 依赖/接口

| # | 功能 | 描述 | 文件:符号 | 依赖/接口 |
|---|---|---|---|---|
| F1 | 主监控循环 | 按主播轮询检测→调度录制/下播→维护历史，4 个并发 asyncio 任务 | `monitor.py`:`main()`,`reconcile()`,`run_streamer()`,`supervise()` | F2,F3,F4,F5 |
| F2 | 双通道直播检测 | 302 轻量探测 + 房间状态 API 确认（mix 模式 API 失败回退 302），sec_uid 为主锚 | `detect.py`:`check()`,`check_302()`,`check_api()`,`probe_anchor()` | live.douyin.com、webcast enter API、Cookie |
| F3 | 热点时段追踪 | 三层：实时开播窗口（5h 合并取较早）+ 每日分析（90min 环形聚类 Top2）+ 会话防重启污染 | `schedule.py`:`HotspotStore`(`record_open`/`analyze`/`decay`/`in_hotspot`)、`next_interval()` | hotspots.json、hotspot_events.json |
| F4 | 录制控制 | ffmpeg 启停/分片；`-use_wallclock_as_timestamps 1`+`-copyts` 使 FLV tag 携带绝对毫秒（32 位回绕） | `monitor.py`:`_start_recording()`,`_build_ffmpeg_args()`,`_stop_recording()`,`_refresh_recording()` | ffmpeg |
| F5 | 弹幕/礼物抓取 | 每主播一条 WS 长连接，protobuf 解析，追加写 jsonl；失败重试 3 次静默放弃（隔离录制） | `danmaku.py`:`DanmakuRecorder`、`start_for()`,`retarget_for()`,`stop_for()` | wss://webcast100-ws-web-lq.douyin.com、F6、scripts/danmaku_sign.js |
| F6 | protobuf 消息定义 | Webcast 协议消息类（betterproto，AGPL-3.0 来源注释） | `danmaku_proto.py`:`PushFrame`,`Response`,`ChatMessage`,`GiftMessage`,`MemberMessage` | betterproto |
| F7 | 字幕生成 | FLV 对齐（每分片独立锚定防错位传播）→ ASS 生成（queue 底部队列 / scroll 横滚）→ 偏移重生成 | `subtitle.py`:`generate_session()`,`generate_ass_for_segment()`,`anchor_segments()`,`patrol()`,`finalize()`,`effective_offset()` | F5 `jsonl_for()` |
| F8 | Web UI 控制台 | 状态/配置/主播管理/历史/预览/弹幕微调面板 | `webui.py`:`State`,`make_handler()`；`static/index.html` | REST API（§3） |
| F9 | NAS 归档 | SMB 挂载、待归档队列、按主播分目录 + manifest 元信息 | `nas.py`:`ensure_mount()`,`enqueue()`,`process_queue()`,`nas_watch()` | mount_smbfs、spool/pending.json |
| F10 | 预览截图 | 直播中周期 ffmpeg 抽帧（库存 5 张轮换），Web 展示 | `preview.py`:`capture_frame()`,`list_images()`；`monitor.py`:`_preview_capture_loop()` | ffmpeg |
| F11 | 链接解析 | 主播分享链接 → sec_uid/房间号/昵称 | `link_resolver.py`:`resolve()` | 页面 HTML 抓取 |
| F12 | 启动自检 | ffmpeg/Cookie/NAS/防休眠检查 | `preflight.py`:`run_checks()`,`find_ffmpeg()` | ffmpeg、systemsetup |
| F13 | 配置管理 | 深合并 DEFAULT_CONFIG 与 config.json，原子写 | `monitor.py`:`load_config()`,`_deep_merge()`,`_atomic_write_json()` | config.json |
| F14 | 一键部署 | 检测 Python、建 venv、装依赖、可选 launchd 自启 | `setup.sh`；`一键启动.command` | pip、launchd |

---

## 3. 技术边界划分

### 3.1 子系统与职责（避免职责重叠）

| 子系统 | 文件 | 职责（只做） | 明确不做 |
|---|---|---|---|
| 检测层 | `detect.py` | 探测主播是否直播、返回流地址/房间号/开播时间 | 不调度、不录制 |
| 调度层 | `schedule.py` | 热点窗口建模、轮询间隔计算、开播事件记录 | 不碰网络探测 |
| 录制层 | `monitor.py`（录制部分）、`danmaku.py`、`subtitle.py` | 启停 ffmpeg；抓弹幕写 jsonl；从 jsonl+FLV 生成 .ass | danmaku/subtitle **互相解耦**：danmaku 只写 jsonl；subtitle 只读 jsonl+FLV，不启动/停止任何连接 |
| 展示层 | `webui.py`、`static/index.html` | 状态查询、配置读写、历史/弹幕面板、预览管理 | 不直接启停录制（经 `state.set_rec_ctrl` 回调注入 monitor） |
| 归档层 | `nas.py` | SMB 挂载、队列、按主播归档 | 不生成内容，只搬运已完成场次 |
| 辅助 | `preview.py`、`link_resolver.py`、`preflight.py` | 截图 / 链接解析 / 自检 | 各自独立，无内部耦合 |

### 3.2 主数据流（一次完整开播生命周期）

```
run_streamer (monitor.py:658, 每主播每轮检测)
  ├─ detect.check() ──→ {is_live, room_id, sec_uid, stream_url, create_time?}
  ├─ 开播分支: _record_open(store,...) 记录热点开播时间
  ├─ _start_recording() 启动 ffmpeg 分片录制
  └─ _ensure_danmaku() 启动 DanmakuRecorder(WS) ──追加写──> .meta/*.danmaku.jsonl
中间: danmaku_patrol_loop (monitor.py:881, 每60s)
  └─ subtitle.patrol() → 锚定新分片 + 停滞/弹幕缺口检测(记 align.json gaps) + 重生成 .ass
下播: _stop_recording() → subtitle.finalize()(固化 .ass+summary)
      _stop_danmaku()  → stop WS 后再 finalize 一次(补齐最后消息)
      nas.enqueue()    → nas_watch 异步 SMB 归档到 NAS/主播目录
```

### 3.3 关键接口契约

**detect.check 返回**（`detect.py:202`）：`{"is_live":bool, "room_id":str?, "sec_uid":str, "stream_url":str?, "create_time":int?, "error":str?}`；`mode` 取 `detection.mode`（302/api/mix）。

**弹幕事件 schema**（jsonl 每行，`danmaku.py:_dispatch`）：
```json
{"ts": 1787348725128, "ts_local": 1787348725008, "type": "chat|gift|member",
 "user": "昵称(已脱敏)", "uid": "...", "content": "...", 
 "gift": "礼物名", "count": 1, "diamond": 3000}
```
- `ts` = 服务器 `common.create_time*1000`（对齐主时钟）；`ts_local` = 本地接收时刻。
- `gift` 字段仅 gift 类型有；member 默认不采集（`danmaku.capture_member`）。

**align.json schema**（`subtitle.py:load_align/save_align`）：
```json
{"anchors": {"000": 1787348686231}, "drift_ms": -17.9, "config_offset": 0.0,
 "global_offset": 1.0?, "per_segment": {"001": -2.5}?,
 "gift_config_offset": 0.0, "gift_global_offset": 0.0?, "gift_per_segment": {...}?, "finished_at": ...,
 "summary": {"chat": 4, "gift": 0, "diamonds": 0, "gift_top": [{"gift":"嘉年华","count":3}]},
 "gaps": [{"type": "stall|danmaku_gap", "from": ..., "to": ..., "note": "..."}]}
```
- 锚点 = FLV 首数据帧绝对毫秒 − 钟差；每分片独立锚定 → 断链错位不传播。
- 偏移（弹幕）：生效值 = `(人工 global_offset 或配置 offset_seconds) + per_segment[idx]`，见 `subtitle.effective_offset`。
- 偏移（礼物）：**额外量** = `(人工 gift_global_offset 或配置 gift_offset_seconds) + gift_per_segment[idx]`，
  叠加在弹幕偏移之上，见 `subtitle.gift_extra_offset` / `effective_gift_offset`；默认 0 = 与弹幕同刻。

**WebUI REST API**（`webui.py:make_handler`，GET 于 :554，POST 于 :618）：

| 方法 | 路由 | 作用 |
|---|---|---|
| GET | `/api/status` | 全量状态（config/monitors/recordings/commands/nas） |
| GET | `/api/config` | 当前配置 |
| GET | `/api/preflight` | 自检结果 |
| GET | `/api/nas` | NAS 状态/队列 |
| GET | `/api/history` | 录制历史（output_path 为模板） |
| GET | `/api/danmaku/session?path=<模板路径>` | 弹幕面板数据（分片锚点/偏移/gaps/summary） |
| POST | `/api/config` | 合并更新配置（`update_config` 校验） |
| POST | `/api/nas`、`/api/nas/test`、`/api/nas/shares` | NAS 配置/连通性/共享列表 |
| POST | `/api/streamers/<name>/refresh`、`/rec-stop`、`/rec-start` | 主播重检 / 手动停录 / 手动复录 |
| POST | `/api/danmaku/offset` | 应用全局/单分片偏移并重生成 .ass；弹幕组 `global_offset`/`per_segment`，礼物组 `gift_global_offset`/`gift_per_segment`（传 `null` = 清除，两组可独立提交） |
| POST | `/api/history/delete` | 删历史（`clear:true` 清空） |
| POST | `/api/open-folder`、`/api/resolve` | 打开目录 / 链接解析 |
| GET/POST | `/api/previews/...`、`/previews/...` | 预览截图管理/展示 |

**配置段**（`config.json` 顶层，`monitor.py:DEFAULT_CONFIG` 定义默认值）：`detection`（mode/cookie/api_base）、`schedule`（13 个热点参数，如 hotspot_merge_hours/analysis_hour）、`webui`（host/port）、`monitors[]`（name/anchor/web_rid）、`nas`、`archive`、`preview`、`recorder`（output_dir/enabled/format/bitrate/segment_time）、`danmaku`（enabled/offset_seconds/font_size/capture_member/**style: queue|scroll**/queue_lines/queue_seconds）、`on_live_command`/`on_offline_command`。

**跨模块注意点**：
- 弹幕 jsonl 统一收纳在 `<视频同目录>/.meta/`（`danmaku.py:jsonl_for` 推导，支持模板/单文件/实际分片三形态）；`.ass` 必须与分片**同名同目录**（播放器自动加载约定）。
- webui 不直接 import monitor 模块级录制函数——通过 `state.set_rec_ctrl({stop/start})` 回调（`monitor.py:main` 注入）。

---

## 4. 快速导航指南

### 4.1 目录树（关键路径）

```
./
├── monitor.py          主循环/录制/巡检编排（960 行）
├── detect.py           双通道检测
├── schedule.py         热点调度
├── danmaku.py          弹幕 WS 抓取 + jsonl_for
├── danmaku_proto.py    protobuf 消息（vendored，勿改字段号）
├── subtitle.py         FLV 对齐 + ASS 生成（queue/scroll）
├── webui.py            HTTP 服务 + REST + State
├── static/index.html   前端（单文件）
├── nas.py              SMB 归档
├── preview.py / link_resolver.py / preflight.py
├── scripts/danmaku_sign.js   WS 签名 JS（485KB vendored，勿改）
├── scripts/ctl.py            终端快捷指令（douyin status/start/stop/restart/help…；COMMANDS 表是命令清单唯一真源，出口 cheatsheet()）
├── scripts/install_cli.sh    安装/修复 ~/.local/bin/douyin 与 zsh 别名（幂等）
├── scripts/sync_github.py    月度同步到 GitHub（禁词集从 config.json 反推，命中即中止；走 Git Data API 推送）
├── setup.sh / 一键启动.command  部署（启动窗口会打印命令速查表，渲染自 scripts/ctl.py）
├── tests/              23 个测试文件（456 用例）
├── config.json / hotspots.json / hotspot_events.json
├── recordings/         录制+字幕+ .meta 明细
├── spool/pending.json  NAS 队列
└── logs/               运行日志（launchd.out.log 含弹幕 print）
```

### 4.2 关键词 → 代码直达表

| 关键词/功能 | 直达 | 说明 |
|---|---|---|
| 开播检测、is_live、stream_url | `detect.py:check` / `check_302` / `check_api` | 302+API 混合 |
| 轮询间隔、冷/热/直播阶段 | `schedule.py:next_interval` | 冷30min/热5-6min/直播20min+抖动 |
| 热点窗口、开播时间记录 | `schedule.py:HotspotStore.record_open` | 三层：create_time>见证>防污染 |
| ffmpeg 参数、分片、墙钟时间戳 | `monitor.py:_build_ffmpeg_args` | `-use_wallclock_as_timestamps 1` 在 `-i` 前 |
| 录制启停 | `monitor.py:_start_recording` / `_stop_recording` / `_stop_recording_manual` | 手动停录会暂停自动重录 |
| 弹幕 WS、心跳、签名 | `danmaku.py:DanmakuRecorder._run` / `_on_open` / `_sign` | **心跳须 OPCODE_BINARY** |
| 弹幕 jsonl 路径推导 | `danmaku.py:jsonl_for` | 统一收 `.meta/` |
| 时间对齐、钟差、32 位回绕 | `subtitle.py:anchor_segments` / `flv_first_av_ts_abs` / `_unwrap_ts` | 锚点=FLV首帧−钟差 |
| 字幕样式（队列/滚动） | `subtitle.py:_queue_lines` / `_scroll_lines` / `generate_ass_for_segment` | queue 为默认 |
| 弹幕/礼物偏移微调 | `subtitle.py:effective_offset` / `gift_extra_offset` + `webui.py:_danmaku_apply_offset` | 弹幕与礼物各一组全局+单分片，无损重生成 |
| 停滞/缺口巡检 | `subtitle.py:patrol` + `monitor.py:danmaku_patrol_loop` | 60s 一次，记 align.json gaps |
| API 路由 | `webui.py:make_handler` | GET :554 / POST :618 |
| 配置段、默认值 | `monitor.py:DEFAULT_CONFIG` / `load_config` | deep_merge |
| NAS 归档、队列 | `nas.py:enqueue` / `process_queue` / `nas_watch` | SMB，按主播目录 |
| 预览截图 | `preview.py:capture_frame` | ffmpeg 抽帧 |
| 链接解析 | `link_resolver.py:resolve` | 分享链接→sec_uid |
| 测试 | `tests/`（`test_danmaku_subtitle.py` 等） | `./.venv/bin/python -m unittest discover -s tests` |

---

## 5. Token 优化原则（本报告已遵循，供智能体延续）

1. **要点式、符号优先**：全篇用 `文件:符号(行号)` 而非散文；描述一句话封顶，细节一律以代码为准。
2. **重复信息集中存放**：配置段、事件 schema、align schema、路由表各出现一次（§3），功能索引表只引用不重复展开。
3. **导航即答案**：§2 索引表、§4 关键词映射表可直接作为 grep 前的第一跳；定位到符号后再读局部代码，避免整文件读取（最大文件 `monitor.py` 仅 960 行，`danmaku_proto.py` 861 行为 vendored 可跳过）。
4. **修改前先看契约**：跨文件改动先核对 §3.3 的 schema/路由/回调；弹幕链路改动优先验证 `subtitle.py` 测试（`tests/test_danmaku_subtitle.py`，15+ 用例覆盖对齐/偏移/样式）。
5. **环境事实**：依赖 4 个（见 §1）；改动后运行 `./.venv/bin/python -m unittest discover -s tests`（当前 **456 项全过**）；服务启停优先用终端快捷指令 `douyin restart` / `douyin stop` / `douyin start`（实现在 `scripts/ctl.py`，与 Web 按钮共用 `webui._service_plan()` 的执行计划；`-n` 可干跑预览，命令清单敲 `douyin help`）。**增删命令只改 `scripts/ctl.py` 的 `COMMANDS` / `ZSH_ALIASES` 表**：argparse 帮助、`douyin help`（`--short` / `--json`）、`一键启动.command` 窗口、Web「系统健康 → 终端快捷指令」卡片（数据 `GET /api/shortcuts`，见 `webui._shortcut_help()`）都由它渲染，别在别处另抄一份（`tests/test_ctl.py::TestCheatsheet` 与 `tests/test_shortcuts_api.py` 会拦）。裸 `launchctl kickstart -k gui/$(id -u)/com.douyin.monitor` 等价于「无外来实例占用端口」时的 restart（注意会中断正在进行的录制，重启后自动恢复）。**若端口被非 launchd 实例占用，kickstart 会无效**（新实例一 bind 就退出、KeepAlive 又重拉，界面连的仍是旧进程），此时先 `lsof -tiTCP:8780 -sTCP:LISTEN` 找出占用者并结束它，或用 `douyin restart` / Web 上的「重启」（两者都已内置该处理）。
