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

**关键数据文件**（元数据在项目根 / `recordings/`，**录制品在项目外的暂存根**）：

```
config.json            运行配置（见 §3 API 契约-配置段）
hotspots.json          热点窗口（含 _meta 会话状态）
hotspot_events.json    开播事件日志（分析层输入）
recordings/history.json  录制历史（output_path 为模板路径，含 %03d）—— **恒定在项目内**
<暂存根>/<主播昵称>/<YYYY-MM-DD>/<name>-<yyyyMMdd-HHmmss>[-%03d].flv   视频分片
<暂存根>/<主播昵称>/<YYYY-MM-DD>/<name>-<yyyyMMdd-HHmmss>[-%03d].ass    外挂字幕（同名同目录，播放器自动加载）
<暂存根>/<主播昵称>/<YYYY-MM-DD>/.meta/<name>-<ts>.danmaku.jsonl        弹幕明细（追加写，永久保留）
<暂存根>/<主播昵称>/<YYYY-MM-DD>/.meta/<name>-<ts>.align.json           对齐元数据（锚点/偏移/gaps/summary）
<暂存根>/<主播昵称>/<YYYY-MM-DD>/.meta/<name>-<ts>.giftrank.jsonl      贡献榜快照（可选，每分钟≤1条；盲场唯一的礼物金额线索）
<暂存根>/<主播昵称>/<YYYY-MM-DD>/.meta/<name>-<ts>.giftdisplay.json    礼物展示计划（可选；与 .ass 同一份计划，含槽位/分段/延迟/片尾溢出）
state/gift_dict.json                                     官方礼物字典（gift/list 快照，id→名称/单价；永不删条目）
spool/pending.json      NAS 待归档队列
logs/monitor-*.log     业务日志；logs/launchd.out.log 弹幕 print 日志（danmaku 用 print 非 log()）
```

⚠️ **暂存根(`<暂存根>`)默认 `~/Movies/DouyinRecordings`,由 `recorder.output_dir` 配置**
(2026-10-30 外迁:项目文件夹只放系统文件)。解析唯一真源 `monitor._resolve_output_dir`:
空串→默认根,**路径落在项目目录内一律拒绝并回落默认**。别在别处再拼一份路径 ——
暂存根可在录制中切换,「配置值」不等于「此刻在写哪儿」(看 `/api/status.storage`)。

---

## 2. 功能索引表

> 列：功能 | 一句话描述 | 文件 | 关键符号 | 依赖/接口

| # | 功能 | 描述 | 文件:符号 | 依赖/接口 |
|---|---|---|---|---|
| F1 | 主监控循环 | 按主播轮询检测→调度录制/下播→维护历史，4 个并发 asyncio 任务 | `monitor.py`:`main()`,`reconcile()`,`run_streamer()`,`supervise()` | F2,F3,F4,F5 |
| F2 | 双通道直播检测 | 302 轻量探测 + 房间状态 API 确认（mix 模式 API 失败回退 302），sec_uid 为主锚、web_rid 为探测锚点。**API 的参数是 `web_rid`，传 reflow 页的 19 位 `room_id` 会静默拿到 4001038 + 空 data**；降级落地页判 `unknown`（既不在播也不下播） | `detect.py`:`check()`,`check_302()`,`check_api()`,`probe_anchor()`,`is_degraded_page()` | live.douyin.com、webcast enter API、Cookie |
| F3 | 热点时段追踪 | 三层：实时开播窗口（5h 合并取较早）+ 每日分析（90min 环形聚类 Top2）+ 会话防重启污染 | `schedule.py`:`HotspotStore`(`record_open`/`analyze`/`decay`/`in_hotspot`)、`next_interval()` | hotspots.json、hotspot_events.json |
| F4 | 录制控制 | ffmpeg 启停/分片；时间基默认是**源流时间戳**（`-copyts`，音视频同一坐标系 ⇒ 音画同步）；只有 `recorder.src_timestamps=false` 才回退 `-use_wallclock_as_timestamps 1`（逐包墙钟，会压扁 PTS、让音轨采样数少于视轨） | `monitor.py`:`_start_recording()`,`_build_ffmpeg_args()`,`_stop_recording()`,`_refresh_recording()`,`_note_clock_offset()` | ffmpeg |
| F5 | 弹幕/礼物抓取 | 每主播一条 WS 长连接，protobuf 解析，追加写 jsonl；失败重试 3 次静默放弃（隔离录制） | `danmaku.py`:`DanmakuRecorder`、`start_for()`,`retarget_for()`,`stop_for()` | wss://webcast100-ws-web-lq.douyin.com、F6、scripts/danmaku_sign.js |
| F6 | protobuf 消息定义 | Webcast 协议消息类（betterproto，AGPL-3.0 来源注释） | `danmaku_proto.py`:`PushFrame`,`Response`,`ChatMessage`,`GiftMessage`,`MemberMessage` | betterproto |
| F5b | 礼物健康度（轻通道） | **2026-10-29 定案：全主播礼物口径固定轻通道**（`WebcastLightGiftMessage`）。完整通道（`WebcastGiftMessage`）事件**不再落盘** —— 匿名侧 349 场实测恒 0 条，且两通道是服务端事件级互斥路由，双收会把同一笔礼物重复计数。健康度只对账「轻通道收到 N 条 / jsonl 落 M 条 / 4 个承诺字段（ts、gift、diamond、count）齐不齐」，**不判「谁送的、送的是什么」**（协议不下发，登录态同样没有）。每 60s 两段判定：①完整性随时报 —— `LIGHT_GIFT_PARSE_ERR`/`LIGHT_GIFT_WRITE_ERR`(fault)、`LIGHT_FIELD_MISS`/`LIGHT_EVENT_LOST`(suspect)；②通道态等静默 `silent_minutes`(默认 45) 后结论且一律 quiet —— `LIGHT_CHAN_OFF`(本场未下发轻礼物)/`LIGHT_QUIET`/`LIGHT_OK`，**永不推送**。结论落 `.meta/*.giftwatch.jsonl`（`snapshot.light` = 收到/落盘/解析失败/写盘失败/字段缺项 三段账），量化落 `history.gift_integrity.light`（`danmaku.gift_channel_coverage` 统计 + `_history_finish` 收尾带实时计数）。**2026-10-16 礼物界面改版（二次瘦身定案）**：前端礼物界面全部在「录制历史」页——完整性胶囊（`giftVisOf` / `giftVisPill` / `_giftVisTip` / `renderGiftVisLegend`，档位表 `<script id="gift-vis-tiers">` JSON 为唯一真源；读 `gift_integrity.light.ratio`，老条目回退 `ratio`；**6 档按百分比**：无数据 / ≤25% / 26~50% / 51~75% / 76~94% / ≥95%，4 格=档位段、数字=精确完整率、末端圆点=已对账且 ≥95%、空心格=未对账、`r=0` 且非 `channel_off` 显示 0% 红；录制中不展示，点击弹 `#gift-vis-modal` 说明，图例由表渲染而非手写；守卫 `tests/test_gift_vis_tiers.py`）+ ▲诊断警示胶囊（`gift_diag`）+ 展开区诊断块（`GET /api/giftwatch?path=` 按场次取记录），礼物诊断独立页已删除；**具体礼物明细（gift_stats 份数/抖币/礼物榜、行内摘要条）已按用户反馈整体下线**；存量缺失字段（含只有时间占比、无 `light` 的老结构）由 `webui._backfill_gift_fields` 打开历史页时惰性补算（本机 ≤10 条/次、归档 ≤2 条/次，无需停服）**跨场归属（2026-10-29 二次修复）**：礼物归哪一场**看事件时间不看文件** —— ffmpeg 崩溃重建后 `_restart_recording` 立即 `retarget_for`（原先只挂在 60s 巡检里，换场后几十秒的新礼物会写进上一场已结算的文件，实测丢 41 条）；`retarget` 一并归零 `gift_count`/`last_gift_ts`/`msg_count`/`chat_count`（否则本场 0 条礼物的诊断快照仍显示上一场累计数）；结算用 `DanmakuRecorder.ledger_for(jsonl)` 按文件取账，并用 `monitor._sibling_danmaku_jsonls()` + `gift_channel_coverage(extra_paths=)` 从兄弟 jsonl 借回落在本场时间窗口内的迟到行（条数如实报在 `light.adopted`；无 `started_at` 一律不借） | `monitor.py`:`gift_watch_loop()`,`_gift_scan()`,`_gift_diagnose()`,`_light_integrity_diag()`,`_gift_record()`,`_gift_push()`,`_history_finish()`,`_gift_diag_counts()`,`_restart_recording()`,`_sibling_danmaku_jsonls()`；`webui.py`:`_backfill_gift_fields()`；`danmaku.py`:`gift_channel_coverage()`,`light_row_missing()`,`classify_gift_channel()`,`DanmakuRecorder.ledger_for()`/`retarget()`；`notify.py`:`push_async()` | F5（`DanmakuRecorder.gift_light_recv`/`gift_light_count`/`light_gift_errors`/`light_write_errors`/`light_field_miss`）、`spool/gift_calib.json`、企微群机器人 webhook |
| F7 | 字幕生成 | FLV 对齐（每分片独立锚定防错位传播）→ ASS 生成（queue 底部队列 / scroll 横滚）→ 偏移重生成 | `subtitle.py`:`generate_session()`,`generate_ass_for_segment()`,`anchor_segments()`,`patrol()`,`finalize()`,`effective_offset()` | F5 `jsonl_for()` |
| F8 | Web UI 控制台 | 状态/配置/主播管理/历史/预览/弹幕微调面板（主播卡片四列，段号与落盘路径在详情弹窗的「录制实况」「落盘与归档」两块；**历史行胶囊 = 时长/段数/体积/分辨率/实测码率/礼物完整性（轻通道对账）**，分辨率与码率取自 `history.json.media` 的落盘实测值，不是配置目标值；中途降级显示「首片→末片」） | `webui.py`:`State`,`make_handler()`,`_backfill_media_info()`；`static/index.html` | REST API（§3） |
| F9 | NAS 归档 | SMB 挂载、待归档队列、按主播分目录 + manifest 元信息 | `nas.py`:`ensure_mount()`,`enqueue()`,`process_queue()`,`nas_watch()` | mount_smbfs、spool/pending.json |
| F10 | 预览截图 | 直播中周期 ffmpeg 抽帧（库存 5 张轮换），Web 展示 | `preview.py`:`capture_frame()`,`list_images()`；`monitor.py`:`_preview_capture_loop()` | ffmpeg |
| F11 | 链接 / 抖音号解析 | 主播分享链接或抖音号 → sec_uid / 昵称 / 抖音号（直播间链接另得 web_rid） | `link_resolver.py`:`resolve()`、`resolve_by_douyin_id()`、`fetch_user_info()` | 页面 302 跟随、`iesdouyin/web/api/v2/user/info`（免签名） |
| F12 | 启动自检 | ffmpeg/Cookie/NAS/防休眠检查 | `preflight.py`:`run_checks()`,`find_ffmpeg()` | ffmpeg、systemsetup |
| F12b | **缺号补号巡检** | 缺 `web_rid` 的主播开播后，后台浏览器渲染主页取 `live.douyin.com/<rid>` 并写回 config（7 天窗口、三档随机、三层熔断；无缺号时浏览器零启动） | `web_rid_probe.py`（纯逻辑）、`web_rid_browser.py`:`fetch_web_rid()`、`monitor.py`:`rid_backfill_loop()`、`_rid_apply_web_rid()` | playwright headless chromium、`state/rid_backfill.json` |
| F13 | 配置管理 | 深合并 DEFAULT_CONFIG 与 config.json，原子写 | `monitor.py`:`load_config()`,`_deep_merge()`,`_atomic_write_json()` | config.json |
| F14 | 一键部署 | 检测 Python、建 venv、装依赖、可选 launchd 自启 | `setup.sh`；`一键启动.command` | pip、launchd |
| F15 | 跨项目对接 | 与转录端 asrclip 的 NAS 文件面互联（两机零 HTTP）：S1 契约快照（`_对接/contract/`）/ S2 session.json 随数据走 / S3.B 问题反馈消费（600s 协程，error 级即时企微，取走=移入已处理/）/ S3.A 诊断包（`douyin diagnose`）/ S5 产物变更通告（产调必告知:`douyin notice` 写 NOTICE.md 并投递契约三件套,义务=producer_duties 段）；UI 侧「系统健康 → 项目对接」页经 `mailbox_status()` 只读探测四通道 | `interop.py`:`nas_paths()`,`consume_pending()`,`locate_session()`,`build_diagnostic()`,`deliver_diagnostic()`,`mailbox_status()`,`append_notice()`；`monitor.py`:`interop_feedback_loop()`；`scripts/sync_contract.py`,`scripts/notify_backend.py`,`scripts/check_contract_sync.py` | NAS `_对接/`（与「直播回放」平级）、`contract/contract.json`、`notify.push_by_config` |

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
| 辅助 | `preview.py`、`link_resolver.py`、`preflight.py`、`web_rid_probe.py`、`web_rid_browser.py` | 截图 / 链接解析 / 自检 / 补号巡检（纯逻辑 + 浏览器抓取） | 各自独立，无内部耦合；`web_rid_browser` 是**全项目唯一** import playwright 的业务模块（另一个是 `auth.py` 的扫码浏览器，两者 profile 目录必须隔离） |

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
| GET | `/api/status` | 全量状态（config/monitors/recordings/commands/nas/quality）+ `record_windows`（录制期限快照：每主播 `{state, mode, until, days_left}` + 服务端 `today`，前端只渲染不自算）+ `storage`（暂存目录现状：`effective_dir` 生效根 / `default_dir` / `pending` 待切换 / `active_dirs` 在录场次目录 / `disk`；**前端显示落盘地址只读它**） |
| GET | `/api/config` | 当前配置 |
| GET | `/api/preflight` | 自检结果 |
| GET | `/api/nas` | NAS 状态/队列 |
| GET | `/api/history` | 录制历史（output_path 为模板）。**后端分页**：`?offset=0&limit=20`（limit 上限 200），响应含 `entries`（仅当前页，新到旧）、`total`、`offset`、`limit`。只探测当页的文件存在性；过期走 stale-while-revalidate |
| GET | `/api/danmaku/session?path=<模板路径>` | 弹幕面板数据（分片锚点/偏移/gaps/summary） |
| POST | `/api/config` | 合并更新配置（`update_config` 校验；改了 `recorder.output_dir` 会顺带切暂存目录，响应带 `stage_switch`） |
| POST | `/api/recorder/stage-dir` | 切录制暂存目录：`{dir, now}`。`now=false` 下一场生效（默认、不拆场），`now=true` 逐路受控重启立刻切（本场被切成两段）。内部复用 `update_config`，项目内路径直接拒 |
| POST | `/api/nas`、`/api/nas/test`、`/api/nas/shares` | NAS 配置/连通性/共享列表 |
| POST | `/api/streamers/<name>/refresh`、`/rec-stop`、`/rec-start` | 主播重检 / 手动停录 / 手动复录 |
| POST | `/api/danmaku/offset` | 应用全局/单分片偏移并重生成 .ass；弹幕组 `global_offset`/`per_segment`，礼物组 `gift_global_offset`/`gift_per_segment`（传 `null` = 清除，两组可独立提交） |
| POST | `/api/history/delete` | 删历史（`clear:true` 清空） |
| POST | `/api/open-folder`、`/api/resolve` | 打开目录 / 链接解析 |
| GET/POST | `/api/previews/...`、`/previews/...` | 预览截图管理/展示 |

**短期录制主播（`monitors[]` 期限字段，2026-10-22）**

- 字段：`record_mode`（`long`/`short`/`paused`）+ `record_days`（1..365，UI 只给 1/3/7）+ `record_until`（`YYYY-MM-DD`，**含当天**）。起算日 = `added_at` 的本地日期，不另存 `record_from`。
- 判定唯一真源：`monitor.record_state(m, today)` → `(ok|paused|expired|missing_until, until, days_left)`，**纯时间派生、零 IO、不写配置**；`auto_record_enabled()` 是它的薄封装，检测循环 / 异常巡检 / `is_dormant()` 三处自动生效。
- 口径：自然日。末日 23:59 前仍可录，次日 00:00 起不再开新场；**正在录的场次不中断**（决策 6A），录完自然收尾归档。
- 到期后：`is_dormant=True` 停止检测（决策 5A，省 Cookie 配额）、卡片红色「已到期」徽标、开关显示为关、归「暂停录制」筛选档；手动「开始」仍允许（决策 2A）。恢复路径：详情里续期 `+1/+3/+7`（`new_until = base + N`，`base = max(today, until)`，已过期按 `today-1`）或「改为长期」。
- `auto_record` 与 `record_mode` 同步写：开着 = `long`/`short`，关着 = `paused`（期限数据保留便于原样恢复）；后端以 `auto_record=False` 优先。
- 🔴 **手工改过 `config.json` 会留下矛盾组合**（`auto_record=false` 但 `record_mode` 还是 `long`/`short`）：`record_window()` 下发前把这种组合的 `mode` **收敛成 `paused`**（2026-10-27）。不收敛的话前端 `_recBucket` 只看 `mode`，会把「开关是关的」主播归进「长期录制」档、徽标写「长期」，而开关是关的 —— 页面自相矛盾。前端 `_recBucket` 另外也认 `w.state === "paused"`，用来兜旧进程/异常快照。**收敛只对 paused 生效**，到期/期限缺失的 `mode` 必须保留 `short`（否则徽标从「已到期」退化成「暂停录制」，丢掉续期信号）。
- 写入校验在 `webui.State._validate`：`record_mode` 白名单、`record_days` 1..365 整数、`record_until` 可解析且 `short` 必填。
- 前端「剩几天」只吃 `record_windows`（含服务端 `today`），**禁止用浏览器时钟自算**。

**配置段**（`config.json` 顶层，`monitor.py:DEFAULT_CONFIG` 定义默认值）：`detection`（mode/cookie/api_base）、`schedule`（13 个热点参数，如 hotspot_merge_hours/analysis_hour）、`webui`（host/port）、`monitors[]`（name/anchor/web_rid）、`nas`、`archive`、`preview`、`recorder`（output_dir/enabled/format/**video_mode/hw_decode/force_hw/resolution/bitrate/preset**/segment_time/stall_*/**stream_await_seconds**/**auto_degrade/degrade_l1_*/degrade_l2_*/degrade_cpu|engine|recover|hold|recover_hold|min_gap|max_restore|max_switches/min_gain/max_hw_streams**）、`danmaku`（enabled/offset_seconds/font_size/capture_member/**style: queue|scroll**/queue_lines/queue_seconds）、`on_live_command`/`on_offline_command`。

**跨模块注意点**：
- 弹幕 jsonl 统一收纳在 `<视频同目录>/.meta/`（`danmaku.py:jsonl_for` 推导，支持模板/单文件/实际分片三形态）；`.ass` 必须与分片**同名同目录**（播放器自动加载约定）。
- webui 不直接 import monitor 模块级录制函数——通过 `state.set_rec_ctrl({stop/start})` 回调（`monitor.py:main` 注入）。
- **流侧退出 ≠ 本地故障，别把两者混成一个重启预算**（2026-10-31）：`ffmpeg` 退出先用 `quality.classify_exit` 归因 —— `external`（404 / `Stream ends prematurely` / `input/output error`）走 `_await_stream`（L2493）等流窗：退避重连 20→60→120→240→480s，**不消耗 `stall_max_restarts`、不报 error**；`resource`（`scale_vt` / 编码器 / 内存）才走原三道止损并记 `record.crash`(error)。判断「下播」只信抖音翻状态（实测 404 后中位 24s、典型 67s），**不信我们的重试次数**——8 天实测 61 次放弃里 18 次在 603~621s（冷却刚满）就恢复、2~5 分钟区间 0 次，说明冷却闸门在拦而不是主播没回来。等流期间必须 `state.request_refresh()`，否则检测周期 `live_interval`(1200s) 会把一次正常下播拖成 15 分钟后的红色故障。守卫 `tests/test_record_health.py::TestStreamAwait` + `tests/test_incidents.py::test_stream_side_exit_is_info_not_error`。
- **`logs/ffmpeg/<主播>.err.log` 是追加写**（2026-10-31 起，原先 `"w"` 覆盖）：每次 `_start_recording` 先写一行 `##### ffmpeg start <ts> tier= bitrate= mode=`，`_ffmpeg_err_tail` 只取**最后一个标记之后**的内容（等价于「本次启动的输出」），超 2MB 滚成 `.1`。⚠️ 改这段别退回覆盖写 —— 一场里连崩 4 次只剩最后一次现场，2026-10-29 那次 `scale_vt` 归因就因此卡住过。守卫 `tests/test_ffmpeg_args.py::TestErrLogRolling`。
- **浏览器自动化导航一律用 `wait_until="commit"`，绝不用默认的 `load`**：抖音首页挂长连接/埋点/风控 iframe，`load` 事件几十秒都不触发，等它只会超时并把「页面其实已打开」误判成失败（`auth.open_home` 2026-10-21 因此 99 秒才返回、还退化成系统浏览器；守卫见 `tests/test_auth_home.py`）。
- **改 `auth._BROWSER_FLAGS` 必须把 `LAUNCH_ARGS_REV` +1**：Chrome 多数开关只在首次启动生效，不换实例新参数永远不生效（`cdp.json` 里记 `args_rev`）。⚠️ **2026-10-26 起「打开我的抖音」走 `kill_stale=False`，不再因为版本落后杀进程**——旧行为在 05:04 把用户正在浏览的整台专用浏览器（11 个进程）关掉过，根因是「10-22 加开关升到 v2、旧进程内存里还是 v1 写了 `args_rev:1`、服务重启后内存变 v2」这种跨天错位。新参数要生效只能由用户点「重开浏览器」（`auth.restart_browser` → `POST /api/auth/restart-browser`，扫码登录中会拒绝）；`cdp.json` 落后时日志打真实差异（`v1→v2`），不再冒充「参数已更新」。**正在扫码登录时禁止换实例**（`_can_reuse_running_instance()`），否则会杀掉用户正在扫的窗口。
- **启动 Chrome 必须走 `auth._spawn_browser()`,禁止直接 `subprocess.Popen`**:服务由 launchd 以 `ProcessType=Background` 运行(`~/Library/LaunchAgents/com.douyin.monitor.plist`),后台 QoS 会被子进程继承 —— 实测专用 Chrome 主进程 `pti_priority=4`,而正常用户进程/Safari 都是 31。WindowServer 随后以低优先级给它合成,BeginFrame 被压到 12~14Hz:同一实例里连 `about:blank` 空白页都只有 14fps,而同屏 Safari 75fps、系统 Chrome 155 换个 profile 起也是 77fps。此时 CPU 只占 5%、GPU 走 ANGLE Metal 正常、窗口 `normal/visible/hasFocus` 全为真、AppleScript activate 也无效 —— 从资源占用和窗口可见性上都查不出来。该属性在 `exec` 时固化,事后 `setpriority(PRIO_DARWIN_PROCESS, pid, 0)` 返回 0 但优先级不变(实测),**只能靠启动方式打断继承**:`_spawn_browser()` 在 macOS 上走 `open -n -b <bundle> --args`(LaunchServices 在 GUI 会话里创建进程)。排查「窗口很卡」先读 `auth._proc_priority(auth._chrome_main_pid())`,`open_home` 返回体的 `chrome_pri` < 20 就是中招了(守卫 `tests/test_auth_home.py::TestSpawnViaLaunchServices`)。**2026-10-29 补两条硬约束**:①🔴 `open` 的返回码不可信 —— 从 AI 会话的 shell 里 `open -n -b com.google.Chrome --args ...` 全部 rc=0,可 `open -W` 0.2 秒就返回、进程表无新进程、也没有崩溃报告(同样写法起 TextEdit 正常),所以冷启动必须传 `verify=_wait_profile_chrome` 校验进程真的出现,否则退回 `Popen`,只信 rc 会白等 12 秒 CDP 再退化成「系统默认浏览器打开」;②🔴 **别在 AI 会话里自己起专用 Chrome 验证** —— 起得来(CDP 都能就绪)但 6 秒后必被 `EXC_BREAKPOINT`(CHROME CHECK,`~/Library/Logs/DiagnosticReports/Google Chrome-*.ips`)打死,要验证就让**服务**起:`curl -X POST --noproxy '*' -d '{}' http://127.0.0.1:8780/api/auth/open-home`,返回体直接带 `chrome_pid`/`chrome_pri`(服务侧实测 `open -n -b` 走通、2.5 秒拿到 pri=31)。
- **抖音页面重量差一个量级**：`/jingxuan`（首页落点）2947 节点 / DCL 10.8s，`/user/self`（个人主页）1150 节点 / DCL 3.4s。要给人看页面就打开个人主页，别打开信息流首页。

---

## 4. 快速导航指南

### 4.1 目录树（关键路径）

```
./
├── monitor.py          主循环/录制/巡检编排（2608 行）
├── detect.py           双通道检测
├── schedule.py         热点调度
├── danmaku.py          弹幕 WS 抓取 + jsonl_for
├── danmaku_proto.py    protobuf 消息（vendored，勿改字段号）
├── notify.py           告警通道层（企微 webhook/连接器 → macOS 通知 → 日志，多级降级）
├── bot.py              企微机器人双向交互（菜单模式；ACTIONS 注册表 = 加功能唯一入口）
├── subtitle.py         FLV 画布探测 + ASS 生成（queue/scroll，PlayRes 按视频真实分辨率）
├── webui.py            HTTP 服务 + REST + State
├── static/index.html   前端（单文件）
├── nas.py              SMB 归档
├── interop.py          跨项目对接层(契约快照/session.json/问题反馈消费/诊断包/信箱探测/产物变更通告;全旁路,零 HTTP)
├── contract/           跨项目契约真源(contract.json + CHANGELOG.md)
├── preview.py / link_resolver.py / preflight.py
├── web_rid_probe.py / web_rid_browser.py   缺号补号巡检(纯逻辑 / 浏览器抓取)
├── scripts/danmaku_sign.js   WS 签名 JS（485KB vendored，勿改）
├── scripts/ctl.py            终端快捷指令（douyin status/start/stop/restart/help…；COMMANDS 表是命令清单唯一真源，出口 cheatsheet()）
├── scripts/install_cli.sh    安装/修复 ~/.local/bin/douyin 与 zsh 别名（幂等）
├── scripts/sync_github.py    月度同步到 GitHub（禁词集从 config.json 反推，命中即中止；走 Git Data API 推送）
├── scripts/sync_contract.py  契约快照同步到 NAS _对接/contract/(原子写 + sha256 回读校验;--check 只核对;含 NOTICE.md)
├── scripts/notify_backend.py 产物变更通告唯一入口(S5):写 NOTICE.md + 投递契约三件套;义务来源 contract.json producer_duties
├── scripts/check_contract_sync.py  契约↔代码双向对账守卫(纳入单测,含破坏性验证)
├── scripts/regen_subtitles.py 存量 .ass 按新画布重生成（默认干跑；--apply 先备份到 recordings/_ass_backup-<ts>.tar.gz；活跃录制场次自动跳过）
├── scripts/migrate_stage_dir.py 把项目内 recordings/ 的老场次搬到暂存根（默认干跑；跳过在录、断点续传、同步改写 history 的 output_path 前缀）
├── setup.sh / 一键启动.command  部署（启动器先做目录自检：入口文件齐备 / 代码同代 / 本目录是 launchd 认定的正式部署目录，任一不过就拒绝启动，非正式目录还可按 y 转交正式目录那份；随后判活——比对新实例身份而不只看 HTTP 200，起服务后「等就绪开浏览器」同样验身份；启动窗口会打印命令速查表，渲染自 scripts/ctl.py；启动后留作终端控制台，按键 o/s/r/q/l/h 打开控制台/状态/重启/关闭/日志/速查）
├── tests/              64 个测试文件（1524 用例）
├── config.json / hotspots.json / hotspot_events.json
├── recordings/         仅元数据（history.json + 字幕备份包）；录制视频在项目外暂存根
├── spool/pending.json  NAS 队列
└── logs/               运行日志（launchd.out.log 含弹幕 print；手动/一键启动窗口拉起的实例写 console-YYYY-MM-DD.log，窗口里按 l 可跟随）
```

### 4.2 关键词 → 代码直达表

| 关键词/功能 | 直达 | 说明 |
|---|---|---|
| 开播检测、is_live、stream_url | `detect.py:check` / `check_302` / `check_api` | 302+API 混合；三态：在播 / 已下播 / `unknown`（降级页） |
| 轮询间隔、冷/热/直播阶段 | `schedule.py:next_interval` | 冷30min/热5-6min/直播20min+抖动 |
| 热点窗口、开播时间记录 | `schedule.py:HotspotStore.record_open` | 三层：create_time>见证>防污染 |
| ffmpeg 参数、分片、时间基 | `monitor.py:_build_ffmpeg_args` | **三条路线** `vt`（默认硬编 `h264_videotoolbox -b:v -allow_sw 0`）/ `sw`（libx264+`preset`）/ `copy`（`bitrate=0` 自动）。时间基由 `recorder.src_timestamps` 决定：**默认 True 时不加墙钟开关**（tag = 推流端时间戳，音视频同步）；回退 False 时 `-use_wallclock_as_timestamps 1` 必须紧邻 `-i` 之前。`-hwaccel` 必须在 `-i` 之前；**`scale_vt` 与 `-hwaccel_output_format videotoolbox_vld` 都不许用**（2026-10-29 定案：它撑不住滤镜图重配置，源流改分辨率/位深就把进程打死，8 天 86 次崩溃全是这条），缩放一律 CPU `scale`，硬解硬编都保留。守 `tests/test_ffmpeg_args.py` + `tests/test_vt_args.py::TestScaleVtDisabled`，端到端 `scripts/verify_scale_reinit.py` |
| **分辨率档位 / 码率 / 自动降级** | `quality.py` + `monitor.py:_quality_loop` | 换算与判定**只在** `quality.py`（`tier_size`/`scale_expr`/`gains`/`usable`/`degrade_plan`/`Governor`），monitor 只注入上下文。唯一真源，别在别处再写一份尺寸/收益换算。**崩溃信号必须按 `classify_exit` 归因**：`resource`（编码器/内存/线程）才参与降档与恢复闸门，`external`（地址 404/403、断流）与 `unknown`（stderr 为空）只记进 `crashes_recent.ignored`。改档位参数换算走 `monitor.py:_tier_view`（唯一出口，日志/接口/`set_active` 共用）。守 `tests/test_quality_tiers.py` / `test_degrade_validity.py` / `test_quality_decide.py` / `test_quality_crash_cause.py` |
| 跟随率 / `-progress` | `monitor.py:_read_progress` / `_update_follow` | `out_time_ms` 实际是**微秒**；跟随率 = 输出时间增量 ÷ 墙钟增量（文件还在长≠跟得上） |
| 录制启停 | `monitor.py:_start_recording` / `_stop_recording` / `_stop_recording_manual` | 手动停录会暂停自动重录 |
| 崩溃/重启告警在哪看 | `incidents.py` + `webui` 的 `/api/incidents` | **左侧导航底部常驻告警条**(`#incident-bar`,在「服务运行中」下方)。事件流:录制崩溃(`record.crash`)/主动重启(`record.restart`)/放弃本场(`record.giveup`)/拉流地址缺失/主进程异常退出(`process.crash`)/服务启动(`process.start`)/弹幕会话异常;再加 health 里 down/warn 的组件(协程卡死/Cookie/NAS/硬编)。**3 秒感知**:摘要走 `/api/status.incidents`(主轮询),详情走 `/api/incidents`(20s + 展开时拉)。与 health 互补 —— 崩溃后同秒重启的那种只有事件流记得住。**已读口径(2026-10-31 修)**:`POST /api/incidents/dismiss` 只把 `read` 置 True(条目保留,供事后回看),**界面必须按 `read` 过滤** —— 未读区只渲染未读,已读整体收进「已读 N 条 · 展开」抽屉并降透明(`.is-read`);dismiss 后要**就地重绘**(乐观更新),不能等 20s 轮询。旧版把 `recent()` 返回的全量事件原样渲染、已读与未读无差别,于是「点过已读的告警不消失,新崩溃一来旧内容又全刷一遍」(用户报障原话)。守 `tests/test_incidents.py` |
| 弹幕 WS、心跳、签名 | `danmaku.py:DanmakuRecorder._run` / `_on_open` / `_sign` | **心跳须 OPCODE_BINARY** |
| 弹幕 jsonl 路径推导 | `danmaku.py:jsonl_for` | 统一收 `.meta/` |
| 时间对齐、钟差、32 位回绕 | `subtitle.py:anchor_segments` / `flv_first_av_ts_abs` / `_unwrap_ts` | 锚点=FLV首帧−钟差 |
| 字幕样式（队列/滚动） | `subtitle.py:_queue_lines` / `_scroll_lines` / `generate_ass_for_segment` | queue 为默认 |
| 字幕画布、竖屏适配 | `subtitle.py:Canvas` / `flv_video_size` / `_fit_font` | PlayRes 用视频真实分辨率；版式常量按 1920×1080 设计；`\move` 做行上移（libass 不支持 `\t` 改 `\pos`） |
| 存量 .ass 重生成 | `scripts/regen_subtitles.py` | 默认干跑，`--apply` 先备份；活跃场次自动跳过 |
| 弹幕/礼物偏移微调 | `subtitle.py:effective_offset` / `gift_extra_offset` + `webui.py:_danmaku_apply_offset` | 弹幕与礼物各一组全局+单分片，无损重生成 |
| **分片进度统计** | `monitor.py:_segment_stats` / `_carry_segments` / `_rec_carry` | 按模板前缀统计已落盘分片，给前端 `segments`/`segments_bytes`/`segment_file`；**单文件录制不给这些字段**。ffmpeg 重开换时间戳前缀 ⇒ 旧段数结转进 `_rec_carry`，`segments` 是本场总数；历史条目收尾时也写 `segments`（`_history_finish`） |
| **断流残渣防护** | `monitor.py:_discard_junk_segments` / `_remove_session_files` / `history_pending` 延迟入史 + `scripts/purge_ghost_history.py` | 房间「直播中」但推流已断时反复起录会堆 20~60s 残渣。三层：①延迟入史（活过首次巡检 20s 才补记）；②give_up 回收本场 `duration<90s 或 size<1MB` 条目；③存量清理脚本（判据含"本机与归档目标都无文件"） |
| 停滞/缺口巡检 | `subtitle.py:patrol` + `monitor.py:danmaku_patrol_loop` | 60s 一次，记 align.json gaps |
| **录制看护 / 冷却恢复** | `monitor.py:_recording_watchdog` / `_rescue_missing_recordings` / `_give_up` / `_clear_giveup` | 20s 一轮，两段：①在录的查文件停增长；②**「判定在播却未录制」请求提前复检救回**。⚠ `_give_up` 会把主播移出 `_recordings`，第②段缺失就会「冷却到期后没人重启」——实测空窗 28.5 分钟。冷却态经 `streamers[].rec_paused_until` / `rec_pause_reason` 给前端 |
| API 路由 | `webui.py:make_handler` | GET `do_GET`(L1794) / POST `do_POST`(L2029) |
| **前端轮询 / 定时刷新** | `static/index.html` 顶部 `pollJSON` / `guarded` / `fetchJSON` | 新增轮询**必须**走 `pollJSON(key, url, timeout)`：同 key 单飞 + `AbortController` 超时。返回值 `undefined`=本轮被闸门挡下（静默跳过，别动 UI）、`null`=失败/超时、其余=数据。⚠ 2026-10-26 事故：裸 `fetch` + `setInterval(fn, 5000)` 遇上 `/api/history` 单次 43 秒 → 请求无限堆积占满浏览器对同一 origin 的 **6 条连接** → 全站接口（连「添加主播」的解析按钮）一起卡死，而服务端其实是空闲的。守 `tests/test_history_dir_cache.py` |
| **历史页文件存在性 / NAS 目录列举** | `webui.py:_dir_listing` / `_dir_cache_clear` / `_entry_root` / `_prefetch_dirs` | 同一目录只 `scandir` 一次，进程级缓存 TTL 60s（491 条归档条目去重后只 78 个目录，2946 次 SMB → 78 次）。三态 `_DIR_OK`/`_DIR_MISSING`/`_DIR_UNKNOWN` 对应前端「正常 / ✗缺失 / ?无法确认」，🔴 **MISSING 绝不能退化成 UNKNOWN**（否则已清理的归档会显示成「未挂载」）。`/api/history` 结果再套 5s 缓存 + 单飞（`_history_payload`），删除历史后调 `_hist_cache_clear()`。守 `tests/test_history_dir_cache.py` |
| 配置段、默认值 | `monitor.py:DEFAULT_CONFIG` / `load_config` | deep_merge |
| NAS 归档、队列 | `nas.py:enqueue` / `process_queue` / `nas_watch` | SMB，按主播目录。🔴 **`nas_watch` 在事件循环线程里，碰 SMB 的同步调用必须走 `nas.py:_offload`**（转线程池 + 长任务每 30s 补 `archive` 心跳）。裸调 `process_queue` 会独占整条 asyncio 循环、饿死全部检测协程：2026-10-27 事故同步搬 5.5 GB 约 35 分钟，`/api/status` 的 `streamers` 字段一直是 `{}`，页面「热点窗口 / 上次开播」全变「—」，且期间开播不会被录。守 `tests/test_nas_watch_async.py`（静态 AST 守卫，裸调直接报红） |
| 跨项目对接(契约/反馈/诊断包/变更通告) | `interop.py:nas_paths` / `consume_pending` / `run_diagnose` / `mailbox_status` / `append_notice` + `monitor.py:interop_feedback_loop` | NAS `_对接/` 信箱;两机零 HTTP;session_key 只从 JSON 内容读;全旁路;UI 状态页 = 「系统健康 → 项目对接」(`GET /api/interop`,30s 缓存只读探测);**产调必告知**:`douyin notice "<摘要>"`(scripts/notify_backend.py)写通告并投递契约三件套,义务=contract.json `producer_duties` |
| 归档目标解析（NAS / 外接硬盘 / auto 降级） | `nas.py:resolve_target` | 返回 `(ok, target_dir, mode)`；**`external` 模式完全不碰 NAS**（不挂载、不网段探测——否则 NAS 不可达时每轮白跑 10~30s 扫描）；`nas_watch` 就绪 60s 一轮、不可用退避至 600s ⇒ 外接盘插回最坏 10 分钟才被发现 |
| 归档空间预检 / 断点续传 / 进度 | `nas.py:_scan_missing` / `_free_at` / `_usage_at` / `progress_status` | 搬前只算「还需传多少」，不足返回 `wait_space`（不消耗重试、不开口）；目标已有同名同大小文件跳过；进度快照经 `/api/nas` 的 `progress` 给前端。**进度用独立 `_plock`**（`_process_one` 在 `_qlock` 内被调用，共用会死锁） |
| **正在录制的场次不归档（`defer`）** | `nas.py:set_active_probe` / `active_dirs` / `is_recording_path` / `_Deferred` | 探针由 `monitor.py` 注入（`_recordings[].output_path` 的父目录）。三道闸：入口探测 → `rmtree` 前再探测 → 单文件长大兜底（mtime < 120s）。`defer` **不消耗重试、不改 history、绝不 rmtree**。⚠ 去掉这道闸会 `rmtree` 掉正在录制的目录，ffmpeg 写句柄落到已删除 inode → 数据永久丢失（2026-09-18 真实事故） |
| 预览截图 | `preview.py:capture_frame` | ffmpeg 抽帧 |
| 链接 / 抖音号解析 | `link_resolver.py:resolve` | 分享链接→sec_uid；抖音号→sec_uid+昵称。**web_rid 只能来自直播间链接**（主页链接/抖音号拿不到，抖音在主播未开播时不对外暴露） |
| **缺号补号巡检** | `web_rid_probe.py` / `web_rid_browser.py:fetch_web_rid` / `monitor.rid_backfill_loop` | 只对 `web_rid` 为空的主播放行：开 headless chromium 渲染主页 → DOM 出现 `live.douyin.com/<rid>` 即补进 config。**`offline` 是成功、`unknown` 才算失败**（把没开播当失败 → 三次巡检就全熔断）；浏览器调用必须 `asyncio.to_thread` |
| **前端弹窗层级 / 遮罩定位** | `static/index.html`：通用弹窗段（`.modal-mask` / `.modal-box`）+ 预览弹层段（`.modal`）+ `openMaskModal()` | 两族：①**通用弹窗**（启动自检 / 机器人告警 / 添加主播 / 礼物完整性说明）把 `.modal-mask` 直接当遮罩，位置**只能**靠基类 `position: fixed`，打开统一走 `openMaskModal(id)`（顺手回滚内容区）；②**预览 / 备注弹层**外层 `.modal` 自己 fixed 铺满视口，内层 `.modal-mask` 只当半透明背景。⚠ ② 的差异项必须写成 `.modal .modal-mask` 这种作用域形式——写成全局选择器会盖掉 ① 的 fixed，遮罩改以文档左上角为参照，用户滚到历史页深处点「礼物可见度」胶囊时弹窗弹到页面最顶端，必须往上滚才看得见（2026-10-26 修）。🔴 **② 的遮罩必须显式复位 `z-index: 0`、`.modal-box` 给 `z-index: 1`**：裸 `.modal-mask` 带着 `z-index: 90`，而它是 box 的**兄弟**节点，不复位就压在弹窗上面，整个预览图弹层「看得见点不到」（关闭/上传/删除全失效，elementFromPoint 命中的是 mask）；① 那族不受影响，因为 box 是 mask 的**子**节点。② 的点遮罩关闭必须绑**自己那个**遮罩（`#preview-modal .modal-mask`），裸 `querySelector(".modal-mask")` 命中的是 DOM 里第一个遮罩=添加主播的。**主播管理页能打开的四类弹窗 2026-10-26 起统一毛玻璃**：添加主播 / 备注 / 预览图 / 主播详情都带 `.glass`（遮罩 `blur(10px)` 糊掉身后卡片列表，面板 `rgba(26,32,46,.74)` + `blur(18px) saturate(140%)`）。`.glass` 必须同时支持**两种骨架**：`.modal.glass`（预览图/备注/详情，mask 是 `.modal` 的子节点）与 `.modal-mask.glass`（添加主播，遮罩自己就是根节点）——只写一族的结果是「一半毛玻璃一半不透明色块」。🔴 **glass 规则里不许写 `width`**：`.modal-mask.glass .modal-box` 是 3 个类的权重，写个宽度就会盖掉 `.add-modal-box`(620px) 与 `.modal .modal-box`(720/92vw)，添加主播表单会被挤成两行；详情那 760px 单独写成 `#detail-modal .modal-box`。详情不再是卡内展开：卡片高度恒定，期限按钮因此落在 `#streamer-list` 之外，处理函数 `_termAction(e)` 由卡片与弹窗两处共用。守 `tests/test_ui_modal_layer.py` + `tests/test_ui_guards.py:TestStreamerDetailModal` / `TestStreamerModalsGlass` |
| 测试 | `tests/`（`test_danmaku_subtitle.py` 等） | `./.venv/bin/python -m unittest discover -s tests` |

---

## 5. Token 优化原则（本报告已遵循，供智能体延续）

1. **要点式、符号优先**：全篇用 `文件:符号(行号)` 而非散文；描述一句话封顶，细节一律以代码为准。
2. **重复信息集中存放**：配置段、事件 schema、align schema、路由表各出现一次（§3），功能索引表只引用不重复展开。
3. **导航即答案**：§2 索引表、§4 关键词映射表可直接作为 grep 前的第一跳；定位到符号后再读局部代码，避免整文件读取（最大文件 `webui.py` 2965 行 / `monitor.py` 4207 行，`danmaku_proto.py` 861 行为 vendored 可跳过）。
4. **修改前先看契约**：跨文件改动先核对 §3.3 的 schema/路由/回调；弹幕链路改动优先验证 `subtitle.py` 测试（`tests/test_danmaku_subtitle.py`，15+ 用例覆盖对齐/偏移/样式）；**动字幕版式/字号/坐标先跑 `tests/test_subtitle_canvas.py`（画布与竖屏适配）**，它锁住「横屏 1080p 输出逐字节不变」这一兼容底线。
5. **环境事实**：依赖 4 个（见 §1）；改动后运行 `./.venv/bin/python -m unittest discover -s tests`（当前 **1442 项全过**）；服务启停优先用终端快捷指令 `douyin restart` / `douyin stop` / `douyin start`（实现在 `scripts/ctl.py`，与 Web 按钮共用 `webui._service_plan()` 的执行计划；`-n` 可干跑预览，命令清单敲 `douyin help`）。**增删命令只改 `scripts/ctl.py` 的 `COMMANDS` / `ZSH_ALIASES` 表**：argparse 帮助、`douyin help`（`--short` / `--json`）、`一键启动.command` 窗口、Web「系统健康 → 终端快捷指令」卡片（数据 `GET /api/shortcuts`，见 `webui._shortcut_help()`）都由它渲染，别在别处另抄一份（`tests/test_ctl.py::TestCheatsheet` 与 `tests/test_shortcuts_api.py` 会拦）。裸 `launchctl kickstart -k gui/$(id -u)/com.douyin.monitor` 等价于「无外来实例占用端口」时的 restart（注意会中断正在进行的录制，重启后自动恢复）。**若端口被非 launchd 实例占用，kickstart 会无效**（新实例一 bind 就退出、KeepAlive 又重拉，界面连的仍是旧进程），此时先 `lsof -tiTCP:8780 -sTCP:LISTEN` 找出占用者并结束它，或用 `douyin restart` / Web 上的「重启」（两者都已内置该处理）。**启停的最坏耗时是 90 秒**（plist `ExitTimeOut=30` + `ThrottleInterval=60`，`bootout`/`kickstart` 会同步等到进程真退出、新实例真 spawn），所以 `scripts/ctl.py` 用 `LAUNCHD_TIMEOUT=100` / `READY_TIMEOUT=120`；把这两个值改小会把一次成功的启动判成「命令超时」（2026-10-29 就是这么误报的）。另：`douyin start` 对已装载的 job 只发 `kickstart`（不带 `-k`），**在服务跑着时是空操作**，不会打断录制。
