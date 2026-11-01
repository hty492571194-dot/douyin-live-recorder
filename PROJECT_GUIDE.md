# 抖音直播监控录制系统 · 项目指南

> 本文件面向 **AI 智能体 / 后续维护者**。在另一台设备上运行遇到 bug 时，先读本文件，能快速定位「改哪里、怎么改、有哪些历史坑」。

---

## 1. 项目概述

一个跑在 macOS 上的抖音直播监控 + 自动录制系统：

- **检测**：监控主播是否开播（无需登录/Cookie，纯 302 + 页面解析）
- **录制**：检测到开播后，用 `ffmpeg` 从直播流地址拉流录制到 **暂存根**（`recorder.output_dir`，空 = `~/Movies/DouyinRecordings`）。项目内 `recordings/` 只留元数据（history.json + 字幕备份包）——2026-10-30 外迁，项目文件夹不再存视频
- **归档**：下播后可自动挂载 SMB（NAS）网络盘并搬运录制文件
- **前端**：内嵌 Web UI（`http://127.0.0.1:8780`），管理主播、看录制状态/历史、配 NAS

技术栈：**Python 3.11+（asyncio + httpx）** + 原生 `http.server` Web UI + `ffmpeg`（录制）+ `smbutil`/`mount_smbfs`（NAS）。无第三方 Web 框架、无数据库，全部状态用 JSON 文件持久化。

---

## 2. 目录结构

```
douyin-monitor/
├── monitor.py            # 主入口:协程调度、录制管理(含手动停止/开始)、录制历史、孤儿清理、信号处理、开播时间三源分级
├── detect.py             # 开播检测:302 探测 + reflow 页解析(状态/流地址/create_time 真实开播时间)
├── link_resolver.py      # 链接 / 抖音号解析:分享链接或抖音号 → sec_uid + 昵称 + 抖音号(httpx 同步;web_rid 仅直播间链接可得)
├── web_rid_probe.py      # ★缺号补号巡检纯逻辑:7天窗口/三档两段随机/三态判定/写回合并/三层熔断(零 IO,可穷举单测)
├── web_rid_browser.py    # ★缺号补号抓取器:唯一 import playwright 处(持久化 profile + 固定指纹 + DOM 取 rid + 移动UA归属校验)
├── schedule.py           # 热点三层追踪:实时层(窗口合并) + 分析层(事件日志环形聚类) + 会话层(_meta 防重启污染)
├── webui.py              # HTTP 服务 + State(共享状态:streamers/recordings/history/nas/rec_ctrl)
├── nas.py                # NAS 挂载(_mount_point 支持 ~)/归档队列/钥匙串凭据/SMB 共享列表
├── interop.py            # 跨项目对接层(消费方 asrclip):契约快照/session.json/问题反馈消费(600s 协程)/诊断包(douyin diagnose)/信箱只读探测(mailbox_status)/产物变更通告(append_notice);全走 NAS _对接/,两机零 HTTP,全旁路
├── preflight.py          # 启动自检 + find_ffmpeg(非标准 Homebrew 路径兜底)
├── preview.py            # 录制预览截图(供前端缩略图)
├── static/index.html     # 前端单页(主播管理/链接解析/调度/NAS/录制历史)
├── contract/             # 跨项目契约真源(contract.json + CHANGELOG.md + NOTICE.md 变更通告;版本变更时 scripts/sync_contract.py 快照到 NAS _对接/contract/)
├── config.json           # 运行时配置(主播列表/检测/调度/NAS/录制器)——首次运行自动生成
├── hotspots.json         # 热点窗口 + _meta 会话状态(运行时生成;迁移时建议随包带走以保留已学习的热点)
├── hotspot_events.json   # 开播事件日志(运行时生成,7 天保留;迁移时建议随包带走)
├── recordings/           # 元数据目录(不存视频):history.json + _ass_backup-*.tar.gz
│   └── history.json      # 录制历史(启动时自动清理孤儿 recording 记录)
└── ~/Movies/DouyinRecordings/  # ★录制暂存根(项目外,可配):<主播>/<日期>/<主播>-<时间>-%03d.flv
├── logs/                 # 日志(按天,保留 7 天):monitor-YYYY-MM-DD.log
├── spool/                # 运行时队列与档案:NAS 归档待搬运队列(pending.json)、孤儿清理台账(retention.json)、礼物可见性抽检档案(gift_calib.json,2026-10-30 起为只读历史存档)
├── tests/                # 单测(63 个文件/1483 项):检测/调度/录制/归档/服务控制/健康面板/快捷指令卡片/字段表分组结构/月度同步/字幕画布与播放器兼容/存量字幕重生成/零礼物一律 quiet 的守卫/礼物完整性（轻通道收到/落盘/字段对账）与诊断计数惰性补算/礼物界面并入历史页守卫/礼物完整性胶囊(6 档百分比 + 满分圆点 + 空心=未对账，档位表守卫)/弹窗层级与遮罩定位守卫/跨项目对接(契约对账+interop+信箱探测+变更通告)/缺号补号(纯逻辑+循环编排+写回冲突保护)
├── scripts/
│   ├── verify.sh         # 机械验收门(分 phase 累积执行:verify.sh a/b/c/d)
│   ├── pack.sh           # 打包迁移脚本(脱敏 config → tar.gz)
│   ├── ctl.py            # 终端快捷指令实现(douyin status/start/stop/restart…);COMMANDS 表=命令清单唯一真源
│   ├── install_cli.sh    # 安装/修复 ~/.local/bin/douyin 与 zsh 别名(幂等)
│   ├── sync_github.py    # 月度同步到 GitHub(扫描敏感内容→提交→走 Git Data API 推送)
│   ├── regen_subtitles.py # 存量 .ass 按新画布重生成(默认干跑;--apply 先备份;活跃场次跳过)
│   ├── sync_contract.py   # 契约快照同步到 NAS _对接/contract/(原子写 + sha256 回读校验,含 NOTICE.md)
│   ├── notify_backend.py  # 产物变更通告唯一入口(S5):写 NOTICE.md + 投递契约三件套;义务=producer_duties
│   └── check_contract_sync.py # 契约↔代码双向对账守卫(纳入单测,含破坏性验证)
├── setup.sh              # 一键部署(venv + 依赖 + 可选 --launchd 开机自启)
├── 一键启动.command        # macOS 双击即用(首次自动部署 + 启动后自动打开浏览器)
├── requirements.txt      # 唯一依赖:httpx
├── README.md             # 面向用户的使用说明
├── PROJECT_GUIDE.md      # 本文件(交付对接书)
└── agents/ + *.md         # 原始项目书/任务书(历史文档,运行时无需)
```

运行时产物（不入库）：`.venv/` `logs/` `recordings/` `spool/` `previews/` `__pycache__/` `dist/`；
录制品在**项目外**的暂存根（默认 `~/Movies/DouyinRecordings`，可用 `recorder.output_dir` 改到任意盘）。
`hotspots.json` / `hotspot_events.json` 是**已学习的热点数据**，迁移时建议随包带走（不带也能跑，新机重新积累）。

---

## 3. 核心模块详解

### 3.1 monitor.py（主循环）
- `DEFAULT_CONFIG`：所有配置默认值。**新增配置项必须同时在这里和 `webui.py::_SCHEDULE_RULES`（校验规则）登记**。当前默认:NAS 挂载点 `~/DouyinArchive`、归档根目录「直播回放」、录制码率 0(复制原流)。
- `run_streamer()` → 内嵌 `once()`：单主播的检测循环。**`once()` 是核心**，一次完整检测：热重取配置 → `probe_anchor` 解析探测锚点 → `check` → 开播/下播分支 → 录制管理 → 状态上报。
- 调度节奏：用 `deadline` 绝对时间 + 手动刷新标志，**手动刷新立即检测一次但不重置 deadline**（`next_in_hint` 传剩余秒）。
- 录制管理：`_recordings` dict + `_start_recording/_stop_recording/_refresh_recording/_sync_recording_state`。`_build_ffmpeg_args` 按配置构造参数(码率→重编码/0→-c copy;分片→-f segment)。
  退出先归因再决策(2026-10-31)：`quality.classify_exit` 判为 `external`(404/流被切断) 走 `_await_stream` 等流窗——退避重连且**不消耗** `stall_max_restarts`、不报 error；`resource`(编码器/内存/scale_vt) 才走原三道止损并记 `record.crash`。判「下播」只信抖音翻状态(404 后中位 24s)，不信重试次数。判据与实测数据见框架说明书 §5.1。
- **录制手动控制**:`_manual_paused` 集合 + `_last_stream_url` 缓存;`_stop_recording_manual`(标记暂停+停)/`_start_recording_manual`(清暂停+用缓存流启动);once() 直播中若 paused 则不自动重录,下播时 discard 暂停标记。main() 注入 `state.set_rec_ctrl({stop,start})`(**回调注入,避免 webui↔monitor 循环依赖**)。
- 录制历史:`_history` + `recordings/history.json`,`_history_add/_history_finish`。**`_load_history` 启动时自动清理孤儿 recording 记录**(status=recording 且文件不存在且不含 %03d),根治 mock 测试/进程崩溃残留。
- **历史条目的实测画质 `media`(2026-10-28)**:`_history_finish` 收尾时调 `_media_info()` 写入 `{w,h,kbps[,tail]}` —— 尺寸读首片 FLV 头部 onMetaData(`subtitle.flv_video_size`,纯 Python 不解码,`_DIM_CACHE` 缓存成功结果但**不缓存失败**,否则录制中 onMetaData 还没落盘的那一场会永远读不到),码率 = `体积×8÷媒体时长`。末片尺寸与首片不同则写 `tail`(中途自动降级)。存量条目由 `webui._backfill_media_info` 惰性补算,归档侧 `tail=False` 只读首片省一次 SMB open。
- `_recent_hotspot(store, anchor)`:最近热点窗口,`_snap` 加 hotspot 字段供前端显示「最近开播时间」。
- **开播时间三源分级 `_record_open`(monitor.py:566)**:检测到开播时决定「记什么时间进热点」——
  - **S1** `create_time`(检测响应里抖音返回的真实开播时间,最准);
  - **S2** 见证区间中点(上次检测不在播、这次在播 → 取两次检测时刻中点;区间 >2h 熔断期则不记录);
  - **S3** 跳过(重启后已在播、房间号与 `_meta` 会话记录相同、无 create_time → 防止「重启时刻」污染热点)。
  - 新主播(无任何热点数据)检测到开播必记录;S1 拒绝未来值与超 1 天的陈旧值。
- **`daily_analysis_loop`(monitor.py:798)**:每日 05:00 对全部主播跑一遍分析层重算,兜底「每次偏离 ≤1h、实时层永不触发即时分析」的缓慢漂移。
- **会话状态持久化**:每轮检测后把 `last_room_id / was_live / last_check_ts` 写入 `hotspots.json` 的 `_meta` 段,重启恢复——根治重启后「当前时刻被当开播时刻」的污染。
- `_cleanup_recordings` + `_register_signal_handlers`：SIGTERM/SIGINT 时 terminate + wait(timeout) 等 ffmpeg flush 落盘,避免孤儿进程。
- `reconcile()`：每 5s 比对 config.monitors 与实际任务，增启删停（移除主播时同步清理 `_meta` 会话态）；**同时每 5s 同步录制时长**。

### 3.2 detect.py（检测）
- `check_302()`：`GET live.douyin.com/{web_rid}` → 302 则再抓 reflow 落地页解析。**返回 dict 含 `is_live / status / stream_url / room_id / sec_uid / create_time / method / unknown`。**
  三态而非两态：`status=2` 判在播、`status=4` 判已结束、**降级页**（`is_degraded_page()`：页面带「暂时无法在该页面观看」等标记、无 `ownerUserId`、无任何 flv/m3u8，实测 26KB vs 正常 70~82KB）解析不出 status 时判 `unknown=True` + `is_live=False` —— 这是「这台机器看不清」，不是「已下播」；其余解析失败仍沿用旧的保守判开播（宁可多查一次也不漏录）。
- `extract_room_status(html)`：从 reflow 页解析 `room.status`（2=直播中，4=已结束）。**注意页面是转义 JSON，`room.status` 后紧跟 `ownerUserId`（user 资料里的 status 后跟 createTime，要避开）**。
- `extract_create_time(html)`（detect.py:50）：从 reflow 页解析 `room.create_time`（真实开播时间，秒级时间戳），供 S1 分级使用。**零新增 API 调用**——只从既有检测响应顺手解析，避免触发抖音风控。
- `extract_stream_url(html)`：提取拉流地址，**过滤 `only_audio=1` 纯音频流**（无清晰度后缀的 `stream-{id}.flv` 是音频流、只有声音没画面），优先原画 `_or4.flv`，fallback 任意视频 flv → m3u8。**正则 `[^"\s]` 不能排除反斜杠，否则 `\u0026` 被截断丢 sign 签名**；末尾 `rstrip("\\")` 去转义引号残留。
- `probe_anchor(mon)`：解析探测锚点——优先 `web_rid` 字段 → anchor 纯数字 → 仅 sec_uid 时返回明确错误。
- `check_api(client, web_rid, cookie, api_base, room_id=None)`：room status API（需 Cookie），无 Cookie 时整条链路退化为纯 302。**第一个参数必须是主播的 `web_rid`（直播间号），不是 reflow 页里的 19 位 `room_id`** —— 传错不报错，只回 `status_code=4001038` + 空 `data`，看着像房间不存在（2026-10-13 排雷：`room_id` 冒充 `web_rid` 让三次矛盾校验全部假失败）。`room_id` 只能作为附加参数带上。

### 3.3 schedule.py（热点三层追踪）

`HotspotStore`（schedule.py:47）内部三层，回答「主播大概什么时段开播」：

| 层 | 数据 | 职责 | 关键方法 |
|---|---|---|---|
| **实时层** | `hotspots.json` 每主播窗口数组 `{center, half_width, strength}` | 检测中即时合并：新开播时刻与现有窗口中心环形距离 ≤5h → 合并并**取较早时刻**(防晚开播拖走中心)；否则新开槽，槽位上限 `hotspot_max`(默认2) | `upsert()` `windows()` `decay()` |
| **分析层** | `hotspot_events.json` 原始开播事件 `{ts, src}`(7 天保留) | 重算模式：90min 环形聚类取 Top-2 **覆盖**实时层；近 3 天事件权重 ×2(主播换时段时新模式快速上位)；**7 天事件 ≥5 才分析**，不足则保留现槽位防偶发覆盖稳定模式 | `record_event()` `prune_events()` `analyze()` |
| **会话层** | `hotspots.json` 的 `_meta` 段 | 持久化 `last_room_id/was_live/last_check_ts`，重启恢复，配合 monitor 的 S3 分级防重启污染 | `get_meta()` `set_meta()` |

触发重算的两条路径：① **偏移触发**——新开播时刻与现窗口中心环形距离 >`deviation_trigger_minutes`(默认60min) 时立即对该主播 `analyze()`(事件不足则回退实时层合并，不丢槽位)；② **定时兜底**——`daily_analysis_loop` 每日 05:00 全量重算。

- `next_interval(recording, in_hot, cfg)`：返回下次检测间隔（冷 30min / 热 5-6min / 直播中 20min）。
- 跨零点一律用**环形距离** `_minute_distance`（历史坑：线性距离在 23:50→00:10 会算错）。
- 新配置参数(在 config.schedule 下,高级菜单可调)：`hotspot_merge_hours`(5,替代已废弃的 hotspot_merge 秒)、`analysis_hour`(5)、`analysis_cluster_min`(90)、`analysis_min_events`(5)、`deviation_trigger_minutes`(60)、`recency_weight`(2)、`event_retention_days`(7)、`max_witness_gap`(7200,见证区间上限)。

### 3.4 webui.py（HTTP + State）
- `State` 类：线程安全共享状态。`streamers`/`recordings`/`history`/`nas_status`/`refresh_pending`/`rec_ctrl`。
- `update_config()`：合并配置 + 校验 + 原子写（tmp + os.replace）。
- 路由：`GET /api/status` `/api/config` `/api/history` `/api/preflight` `/api/nas`；`POST /api/config` `/api/nas` `/api/nas/test` `/api/nas/shares` `/api/streamers/{name}/refresh` `/api/streamers/{name}/rec-stop|rec-start` `/api/open-folder` `/api/resolve`。
- `_open_folder`：open 命令打开文件(默认播放器)/目录(Finder)/占位符父目录;**realpath 安全校验必须落在录制目录内**。
- **`do_POST` 对空 body 容错**（`n==0` 时读 `"{}"`）。
- **`/api/history` 走 `_history_payload(state, offset, limit)`**（2026-10-26 建，**2026-10-31 重写为后端分页**）：只返回当前页（默认 20 条），**只探测当前页要用的目录**；三层缓存是 `_dir_listing`（同目录只 `scandir` 一次，TTL 300s）→ `_history_files_cached`（条目级结论，TTL 300s）→ `_HIST_CACHE`（按 (offset,limit)，5s）；过期走 stale-while-revalidate（先返回旧值、后台线程刷新）。另有 `_HIST_PROBE_BUDGET`（默认 12s）作为**单轮真实 SMB 探测的硬上限**，超出的条目文件状态返回 `None`（前端显示「? 无法确认」）而不是无限等待。**不要再改回「每轮全量探测 500 条」** —— 498 条在 SMB 上、去重 204 个目录、smbfs 真冷约 2 目录/秒，实测单次请求 165s/194s，前端 20s 超时必然失败，用户感受就是「每次点历史都要转半天」（§5 第 24 条）。

### 3.5 nas.py（NAS）
- `ensure_mount` / `test_connection` / `list_shares`（`smbutil view`）/ 归档队列（`spool/pending.json`）。
- **`_mount_point(nas_cfg)`：expanduser 展开挂载点**(默认 `~/DouyinArchive`,用户目录可写)。**切勿用 `/Volumes/xxx`**(系统目录,普通用户 mkdir 会 PermissionError)。
- `_init_dirs`：首次挂载成功后按 monitors 建 `<mount>/<root_dir>/<主播名>/` 目录树(root_dir 默认「直播回放」)。
- 🔴 **`_offload(fn, *args, beat=None)`：`nas_watch` 里一切碰 SMB 的同步调用都必须经它转线程池**（`resolve_target` / `_host_alive` / `auto_recover_host` / `_init_dirs` / `backfill_from_history` / `process_queue` / `_nas_status`）。`nas_watch` 跑在 asyncio 事件循环线程里，裸调会把整条循环独占、饿死 12 个检测协程——2026-10-27 事故：同步搬 5.5 GB 到 SMB 约 35 分钟，`supervise` 首行的 `set_streamer()` 一次没执行，`/api/status` 的 `streamers` 字段恒为 `{}`，页面「热点窗口 / 上次开播」全变「—」，期间开播也不会被录。`beat="archive"` 让长搬运每 30s 补心跳（一轮远超 900s grace，不补会被面板误判成卡死）。守 `tests/test_nas_watch_async.py`。
- 凭据：密码只进钥匙串（`security add-generic-password`，**必须带 `-a default`**），config 只存 `password_ref`。
- `clean_host`/`clean_share`：剥离 `smb://` 前缀、首尾斜杠。
- `_smb_url`：用户名/密码/共享名 percent-encode。

### 3.6 preflight.py（自检 + ffmpeg 定位）
- `find_ffmpeg()`：**which 失败后扫常见 Homebrew 前缀**(/opt/homebrew、/usr/local、~/.homebrew、~/homebrew、~/bin)兜底返回绝对路径。用户可能用非标准 Homebrew prefix,运行时 PATH 不含其 bin,必须用绝对路径。
- 6 项检查，返回 `[{id,title,status,detail,action}]`，前端横幅 3s 轮询展示。

### 3.7 link_resolver.py（链接 / 抖音号解析）
- `resolve(raw_text)`：粘贴 `v.douyin.com`/`live.douyin.com`/主页链接/**抖音号** → 返回 `{ok, sec_uid, web_rid, nickname, douyin_id, final_url}`。httpx 同步（follow_redirects=False 手动循环）。由 douyin-link-parser 项目移植。
- **三种输入**：① 直播间链接 → `web_rid` + `sec_uid`；② 主页链接 / 带链接的分享文案 → `sec_uid`（再补一次资料接口拿昵称与抖音号）；③ 纯抖音号（数字号 / 字母号 / 带点号都认）→ `resolve_by_douyin_id()` 走 `USER_INFO_API`（`iesdouyin/web/api/v2/user/info/?unique_id=`，免签名免 Cookie）反查 `sec_uid` + 昵称。**昵称不能靠 HTML 了**：`www.douyin.com/user/*` 与 `iesdouyin/share/user/*` 现在返回 72KB 的 JS 空壳，`og:title` 抓不到。
- ⚠️ **web_rid 只有直播间链接给得出来**。实测 2026-10-26：主播在播时资料接口也不返回任何 room 字段；`reflow/info` 只认 `room_id`、enter API 只认 `web_rid`，传 `sec_uid` 一律 `10011`；`live.douyin.com/{sec_uid}` 即使在播也返回 200 而非 302。所以「离线主播」只能先加 `sec_uid`（卡片标「缺web_rid」、不检测），等开播时粘一次直播间分享链接补上——补完 `monitor` 下一轮热读 config 即生效，无需重启。
- `fetch_user_info(client, sec_uid=, unique_id=)`：抖音号取值 `unique_id` 优先、为空或 `"0"` 退回 `short_id`，两者都 `"0"` 视为没有。接口失败只返回 `(None,None,None)`，不打断解析主链。

### 3.7.1 web_rid_probe.py + web_rid_browser.py（缺号补号巡检，2026-10-26 新增）
- 背景：`web_rid` 只在**渲染后的主页**里出现（DOM 中「直播中」入口 href = `live.douyin.com/<rid>`），抖音号/资料接口都给不出来。于是有了「先加主播、开播后后台自动补号」这条旁路。
- `web_rid_probe.py`（**纯逻辑，零 IO，不 import playwright**）：`normalize_cfg`（档位/权重校验 + `enabled` 三态归一）、`pending_monitors`、`ensure_window`（7 天窗口，**到期不自动续期**）、`should_run`（原因码：`ok/disabled/no_pending/window_expired/cooling/daily_cap/backoff`）、`plan_next_run`（两段随机 + 秒抖动 + 不连续同档/不同值）、`decide_verdict`（三态 + 10 个原因码）、`merge_web_rid`（只写三个字段）、`Circuit`（单主播 3 连败→6h；风控类→全局 3h→12h；日级 >50% unknown 或冷却 ≥3 次→停到次日）、`task_batch`（cursor 轮转，**不足上限时不重复塞人**）。
- `web_rid_browser.py`（唯一 import playwright）：`fetch_web_rid(sec_uid, timeout_sec=, user_data_dir=)` → `{verdict, rid, reason, detail, elapsed_ms}`。反爬要点：持久化 `state/browser/rid/`、UA 由内核版本派生（HeadlessChrome→Chrome）、抹 `navigator.webdriver`、阻断 image/media/font 但放行 XHR、判定即返回（不滚动不点击）、**不注入登录 Cookie**、`--no-proxy-server`；`verify_owner` 用**移动 UA** 走 302 比对 `sec_user_id`（桌面 UA 返回 200，校验会静默失效）。
- `monitor.rid_backfill_loop`：唯一把两者接起来的地方；**浏览器调用一律 `asyncio.to_thread`**（`sync_playwright` 会阻塞事件循环）。写回走 `_rid_apply_web_rid`：重新读盘 → 只补 `web_rid/web_rid_source/web_rid_filled_at` → mtime+size 复查 → 原子写，冲突**放弃本轮**。状态 `state/rid_backfill.json`，失败审计 `state/rid_backfill_fails.jsonl`（留最近 500 条），health 第 22 项 `ridbackfill`（grace 按最长档 180min）。
- 实测（2026-10-26）：在播主播 1.3–1.7s 判定并取到 rid（与 config 里已有值逐位一致）；离线 4.1–5.2s 判 `offline`；`scripts/probe_web_rid.py` 可单独排障。


### 3.8 static/index.html（前端）
单页,无框架。`showView()` 切换视图;`loadStatus/loadHistory/loadNasStatus/loadPreflight/loadAuth/loadHealth/loadInterop/loadService` 轮询 —— **所有轮询一律走顶部的 `pollJSON(key, url, timeout)`**(同 key 单飞 + `AbortController` 超时,返回值 `undefined`=本轮被挡下、`null`=失败/超时),禁止裸 `fetch` + `setInterval` 叠加(2026-10-26 连接池被占满事故,见 §5 第 24 条)。历史列表每条有「打开」按钮。**添加主播是弹出悬浮框**（标题行右侧「＋ 添加主播」，2026-10-22）：分享链接解析框 + 名称/sec_uid/web_rid/抖音号 + **录制方式**（长期 / 短期 1/3/7 天，右侧实时显示到期日）；标题行右侧另有**录制方式筛选**（**全部主播** + 长期/短期/暂停，2026-10-27 起四态；「全部主播」排在「＋ 添加主播」正右方，点它 = 取消筛选，点已选中的档也回到全部；默认态即「全部主播」高亮；已到期归暂停档并亮红点）。卡片名字旁挂期限徽标（长期/短期·剩 N 天/已到期/暂停录制），到期开关显示为关、详情里可续期 +1/+3/+7 或改长期。「系统健康 → 项目对接」视图(#view-interop)内嵌 SVG 示意图(录制端↔NAS 信箱↔asrclip 四通道)+通道明细表,`loadInterop` 30s 轮询 `/api/interop` 动态刷新状态点与时间。

**主播管理页版式（2026-10-26 改版）**：侧栏 **148px**（原 220，导航项与图标同步收紧，品牌「直播监控 / 控制台」拆两行，折叠态 56px）；主播卡片 **四列网格**（1920 视口卡宽 ≈424px，断点 1459 / 1119 / 799 依次降为 3 / 2 / 1 列，一屏约 16 位）。卡片操作条在卡内底部且**全透明**（无底色、无分隔线、按钮无描边）：主操作「停止」或「开始」、「预览」在左，其**左缘与主播名文字的起点对齐**（内距 `calc(34% + 7px)`，即文字列左缘再让开「直播小圆点 8px + 间距 5px − 按钮左内距 8px」，按钮不再压在左侧头像图上），右侧图标 **▤ 截图 / ↻ 刷新 / ✕ 删除** 贴文字列右缘（内距 11px，与「详情」右缘同一条线），**「详情」入口移到名字行末尾**（`.s-dbtn`）。录制中的卡面只留一行「● 录制中 · 时长 · N 段 · 分辨率」+ 一行「体积 · 跟随率（<98% 才显示）」；时长**只出现一次**，段数不再单独占行。

**详情弹窗**（`#detail-modal`，宽 860px）新增两块：**录制实况**（本段进度条 + 第 N 段 + 本段已录/每段时长 + 已落盘体积 + 当前文件名 + 档位/跟随率；**总段数后端给不出，不画假的分段进度**）与**落盘与归档**（录制目录面包屑，可点开 Finder + 归档目标类型与根路径 + 场次开播时刻）。两块由 `_fillLiveBlocks()` 渲染，弹窗开着时随 3 秒轮询就地重绘；归档子路径不在前端拼（唯一真源是 `nas.py`）。

---

## 4. 关键数据流

```
添加主播(name, anchor=sec_uid, web_rid, record_mode/record_days/record_until)
        ↓
monitor.run_streamer → once()   [record_state() 判定期限:到期不开新场]
        ↓
detect.probe_anchor → web_rid
        ↓
detect.check_302 → live.douyin.com/{web_rid}
        ├─ 302 → 抓 reflow 页 → room.status + stream_url
        │        status==2 → 开播 → ffmpeg 录制 + 历史记录
        │        status==4 → 已下播
        └─ 200 → 未开播
        ↓
下播 → 停止录制 + 历史回填 + NAS 归档入队
```

配置流：`config.json` ↔ `State.config`（前端 POST /api/config 修改）→ `run_streamer` 每轮热重取。

热点学习流：开播检测 → `_record_open` 三源分级(S1/S2/S3) → `store.record_open`(写事件日志+实时层 upsert) → 偏移>1h 即时 `analyze` / 每日 05:00 定时 `analyze` → Top-2 窗口覆盖 → `next_interval` 按窗口决定冷/热间隔。

---

## 5. 已知问题与历史坑（重要）

以下是开发过程中踩过并已修复的坑，**改相关代码时务必不要重新引入**：

1. **302 ≠ 开播**。`live.douyin.com/{web_rid}` 下播后仍返回 302（跳转 reflow 页），只有 reflow 页里 `room.status`（2/4）才是真相。切勿只看 302 就判开播。
2. **sec_uid 无法直接探测**。`live.douyin.com/{sec_uid}` 返回 200 落地页，永不 302。检测必须用 `web_rid`；sec_uid 只作标识。
3. **流地址提取的转义**。reflow 页里 `\u0026`（=&）、`&amp;`（=&）、`\u002F`（=/）都要清理；URL 末尾的 `\"` 转义会残留一个反斜杠，需 `rstrip("\\")`。
4. **流地址有时效**（expire/sign 参数）。录制必须拿到地址后立即用；ffmpeg 因过期退出时用「最新一次检测」的流地址重启。
5. **NAS 共享名 ≠ 卷名**。绿联等 NAS 的卷名（如 `UGREEN-6ADD`）不是 SMB 共享名，用 `smbutil view //user@host` 才能看到真实共享名。
6. **NAS 共享名不能含 `/`**。带子目录要拆：共享名填卷/共享，子目录填 `root_dir`。
7. **NAS 主机字段**：用户可能整串粘贴 `smb://192.168.x.x`，需 `clean_host` 剥离前缀。
8. **NAS 密码特殊字符**：`@ : /` 等会破坏 `mount_smbfs` URL，需 percent-encode。
9. **钥匙串 `-a` 参数**：`security add-generic-password` 不带 `-a`（account）会直接报 Usage 错误。
10. **前端轮询覆盖输入**：表单填充与状态刷新必须解耦（历史 bug：NAS 表单被 3s 轮询清空）。原则：轮询只更新只读状态区，表单仅在初始化/提交成功后填充。
11. **手动刷新不影响下次检测**：用 `deadline` 绝对时间 + `next_in_hint`，不要重置调度。
12. **ffmpeg 孤儿进程**：主进程被杀时 ffmpeg 会残留，必须用 signal handler 清理。
13. **录制时长不实时**：直播中检测间隔长（默认 1200s），duration 需在 reconcile 里每 5s 同步。
14. **`do_POST` 空 body**：`json.loads("")` 会抛异常，需对空 body 容错。
15. **NAS 挂载点不能放 `/Volumes`**。`/Volumes` 是系统目录,普通用户 `os.makedirs` 创建子目录会 PermissionError,导致「测试连接成功但实际挂载失败、状态一直未挂载」。必须用用户可写目录(默认 `~/DouyinArchive`)。
16. **孤儿录制记录**。mock 测试/进程崩溃会留下 `status=recording` 但无文件无进程的记录,导致「一直录制中不停止」。`_load_history` 启动时自动清理(文件不存在且不含 %03d 的 recording 记录)。
17. **ffmpeg 非标准路径**。用户可能用非标准 Homebrew prefix(如 `/Users/xx/.homebrew`),运行时 PATH 不含其 bin,`subprocess.Popen(["ffmpeg"])` 会 FileNotFoundError 静默失败。必须用 `find_ffmpeg()` 返回的绝对路径。
18. **码率单位**。`recorder.bitrate` 是数字(Kbps),后端 `_build_ffmpeg_args` 自动拼 `Xk`;0=复制原流。1M=1000K(非 10K)。**2026-10-22 起码率是体积的唯一决定因素**:同码率下四个分辨率档位体积完全相同,降分辨率只省媒体引擎占用(7 路 71%→45%→32%),不省体积。
18b. **默认硬编,别用 load average 判过载**。`video_mode=vt` 走 VideoToolbox(单路 ~0.02 核);`-hwaccel` 必须在 `-i` 之前,但 **`-hwaccel_output_format videotoolbox_vld` 与 GPU `scale_vt` 都不许用**(见坑 18c),缩放一律走 CPU `scale`。CPU 判定必须用 psutil 的**系统利用率**——编码线程多,实测 3 路时 load 26 而 CPU 仅 74%,用 load 会疯狂误降档。
18c. **GPU 缩放 `scale_vt` 会打死整个 ffmpeg(2026-10-29 定案)**。它撑不住滤镜图重配置:直播源只要在运行中改变帧参数(分辨率、位深,**哪怕只跳一帧又变回去**),ffmpeg 就要重建滤镜图;重建时 `scale_vt` 输出的 `videotoolbox_vld` 与自动插入的 `scale` 协商失败 —— `Impossible to convert between the formats supported by the filter 'Parsed_scale_vt_0' and the filter 'auto_scale_0'` → `Error reinitializing filters!` → `error code -78 (Function not implemented)` → 进程退出。8 天 86 次录制崩溃**全是这一条**:与并发路数无关(用 history.json 画扫描线,真实峰值只有 4 路)、与画幅无关,只取决于源流稳不稳,所以集中在 4 个主播身上(另 7 个主播 160+ 小时零崩溃),曾被误判成"引擎不够"。上游 2023-07 引入至今未修,**升级 ffmpeg 解决不了**。修法:缩放改 CPU `scale`(`quality.SCALE_VT_ENABLED=False`),硬解硬编都保留,代价实测 0.040 → 0.213 核/路(+0.17)。三个连带教训:①**抖动比稳定切换更常见** —— 源跳一下又变回去,崩溃发生在第一帧新尺寸,那帧根本来不及编码,所以翻产物(分片 onMetaData)看不到尺寸变化,不能据此否定归因;②**`-reinit_filter 0` 救不了**,hw frames ctx 变了 ffmpeg 会强制重建;③**本文件 18b 的旧版本把这条错误记成"`-ss` 引起的"**,`-ss` 只是当时偶然撞上的触发点。守卫 `tests/test_vt_args.py::TestScaleVtDisabled`(把 `SCALE_VT_ENABLED` 改回 True 立刻红)+ 端到端 `scripts/verify_scale_reinit.py`(造抖动流跑真实参数,并验证对照组确实会崩)。
18d. **崩溃/重启必须能在界面上看见,别只写日志(2026-10-30 定案)**。教训来自 2026-10-22~29 的 86 次录制崩溃:ffmpeg 每次都在**同一秒**被拉起,文件没停、心跳没断,`/api/health` 全程 `ok`,页面上连一个像素都不变 —— 用户一周都不知道在丢数据。所以任何「一处出事/被重启」都要进 `incidents.py` 的事件流(`record()`),并在左侧导航底部告警条上体现。三条口径:①**崩溃**(`record.crash`)与**放弃本场**(`record.giveup`)记 error,会把告警条变红;②**主动重启**(换房间)记 info、**档位切换**记 warn —— 都要留痕但不许标成崩溃,否则正常生命周期天天误报;③**服务启动**(`process.start`,info)也要记,主进程重启会打断全部录制,是排查时的时间锚点。感知速度:摘要塞进 3s 的 `/api/status`,详情走 20s 的 `/api/incidents`(展开时补拉一次)。④**已读只是标记,过滤在前端(2026-10-31 用户报障)**：「全部已读」(`POST /api/incidents/dismiss`)只把 `read` 置 True,**条目一律保留**(崩溃记录的价值在事后回看),界面必须按 `read` 过滤 —— 未读区只渲染未读,已读收进「已读 N 条 · 展开」抽屉并降透明(`.is-read`);点完要就地重绘(乐观更新),不能等下一轮轮询。历史 bug:列表全量渲染 `recent()`(含已读)且已读无样式,于是「点过已读的告警不消失,别的告警一来之前已读的又全出来」;顺带提醒 —— health 有常驻故障(如 NAS 未挂载)时告警条本来就该一直亮,那是**当前故障**,不是没读掉的旧事件。别只 `log()` —— 日志没人翻,告警条才会被人看见。守 `tests/test_incidents.py`。
19. **只有声音没画面（严重）**。reflow 页里「无清晰度后缀的 `stream-{id}.flv`」带 `only_audio=1` 是**纯音频流**；视频流反而带清晰度后缀（`_or4`/`_hd`/`_md`/`_sd`/`_ld`）。`extract_stream_url` 必须过滤 `only_audio=1`、优先 `_or4.flv`，否则录出来的 flv 只有声音没有画面。
20. **录制中时长要用墙钟,不要解析媒体文件**。`_media_duration`(ffmpeg 解析)对「正在写入的文件」比真实墙钟短 5-15s(启动/缓冲延迟),且每次解析要起 ffmpeg 进程。直播中 `duration` 用 `now - started_at` 纯墙钟;只有历史回填(下播后)才用 `_media_duration`。
21. **前端 number 输入框静默丢弃非法内容**。用户输入「30分钟」/全角数字时,浏览器把 `input.value` 读成空串,`Number("")==0`,会**假保存成功**。收集表单时必须检查 `input.validity.badInput` 并报错拦截。
22. **前端 pathSet 必须逐级创建中间对象**。`t["recorder"]["segment_time"]=v` 在 `t["recorder"]` 为 undefined 时直接崩溃(`undefined is not an object`)。所有配置字段都是两级路径,任何保存必经 pathSet——改它要跑「两级/四级/空路径」用例。
23. **静态资源缓存**。改 `static/index.html` 后浏览器可能用启发式缓存旧版 JS,后端首页响应必须带 `Cache-Control: no-store`,否则「代码已改但前端行为不变」误导排查。
24. **轮询必须单飞 + 超时,别裸写 `setInterval(fn, 5000)`(2026-10-26 事故)**。`/api/history` 对 500 条历史逐条在 NAS 上做 6 次 `stat`(归档条目全在 NAS、每条又是不同日期目录),单次实测 **43 秒**;而前端 5 秒轮询一次且函数内没有并发守卫 → 请求无限堆积(净增 ≈ 0.18 个/秒,永不收敛),占满浏览器对同一 origin 的 **6 条连接** → 所有接口排队,连「添加主播」的解析按钮都永久停在「解析中…」,**而服务端完全空闲、日志没有任何报错**。排查提示:`curl --noproxy '*'` 直连接口能秒回、但在页面上卡住,基本就是连接池被占。修复:前端一律走 `pollJSON`(`static/index.html` 顶部,同 key 单飞 + `AbortController` 超时);后端 NAS 目录列举走 `_dir_listing` 缓存 + `_prefetch_dirs` 并发预热,`/api/history` 再加 5s 结果缓存。守 `tests/test_history_dir_cache.py`。
25. **NAS 上「读不了」和「不存在」必须分开(三态)**。`_dir_listing` 返回 `_DIR_OK` / `_DIR_MISSING` / `_DIR_UNKNOWN`,对应前端「正常 / ✗缺失 / ?无法确认」。把 MISSING 写成 UNKNOWN 会让已清理的归档显示成「未挂载」,反过来会让人以为文件被删了。同类教训见 `monitor.record_state` 的四态。
26. **🔴 「全站数据不刷新但接口还能通」= 进程被 SMB 卡死了(2026-10-26 事故,未根治)**。SMB 短暂失联时,碰挂载点的 `stat()`/`close()` 会掉进内核不可中断睡眠,服务器恢复后**永不返回**。典型表现:`/api/status` 的 `streamers` 为空、`/api/health` 一堆「从未产生心跳」、日志停住半小时,而 `/api/status`、`/api/health` **仍然秒回**(WebUI 每请求一线程,不碰 NAS 就正常),只有 `/api/history` 这类探 NAS 的接口超时。别去前端找 bug —— 用 `sample <pid> 2 -file /tmp/s.txt` 看是否大批线程卡在 `stat`/`close`,确认后**只能重启服务**。根治方向是给 NAS 探测加超时+熔断(见《项目框架说明书》§8 第 12 条)。
24. **重启污染热点数据**。重启后内存态(`last_room_id/last_interval`)清零,若主播恰在播,旧代码会把「重启时刻」当开播时刻写进热点。现在靠 `_meta` 会话持久化 + S3 分级跳过;改检测循环时**不要删掉这两处**,否则污染回归。
27. **🔴 画质降档的「崩溃信号」必须按原因分类(2026-10-27 事故)**。症状:压力明明很小(CPU 21%、引擎 11%,阈值 92%),画质却停在 720P 两个多小时不恢复。根因两层:①`_restart_recording` 把每次看护重启都记为崩溃,而当天 19 次退出**全部**是 `404 Not Found`(流地址带签名有时效,主播侧重连即失效),被当成「并发过多」一次降到底;②不恢复是因为「崩溃后 1800s 内不恢复」这道闸门被 404 每 10~30 分钟刷新一次,**静默期永远等不到**。修法:`quality.classify_exit` 把退出归成 `resource`(编码器/内存/线程)/`external`(404/403/断流)/`unknown`(stderr 为空),**只有 `resource` 刷新 `last_crash` 并参与 `crash_hit`**,其余只记进 `crashes_recent.ignored`。还有个容易读反的地方:档位只对**新开流**生效,在录流要 `follow<0.95` 且跨分片边界才换档 —— 所以「自动恢复到 L0」≠ 正在录的画面立刻回 1080,得等下一场或下一次重启。守 `tests/test_quality_crash_cause.py`。
28. **`state` 与 `mode` 是同一件事的两个说法,必须同源收敛(2026-10-27)**。`/api/status.record_windows` 每条同时下发 `state`(判定结论)和 `mode`(渲染档位),前端 `_recBucket` 先看 `state`(到期/期限缺失→暂停档)再看 `mode` —— 两个字段一矛盾,页面就自相矛盾。手工改过 `config.json` 会造出「`auto_record=false` 但 `record_mode=long`」:`record_mode` 本身是合法值,`record_window` 旧的兜底(只在字段**缺失**时才按 `auto_record` 派生)拦不住,于是下发「`state=paused` + `mode=long`」→ 卡片徽标写「长期」而开关是关的,筛选还把它算进「长期录制」档。修法:`record_window` 里 `if state == "paused": mode = "paused"`(手停优先),前端 `_recBucket` 再认一次 `w.state === "paused"` 兜旧进程/异常快照。**收敛只对 paused 生效** —— 到期/期限缺失的 `mode` 必须留着 `short`,否则徽标会从「已到期」退化成「暂停录制」,丢掉续期信号。守 `tests/test_record_mode.py::TestRecordWindows` + `test_ui_guards.py::test_bucket_logic_has_paused_state`。
29. **🔴 礼物统计「一会儿有一会儿清零」= 换场时序 + 计数口径两个 bug 叠在一起(2026-10-29)**。症状:同一场直播的礼物数在三个界面上显示成 101 / 60 / 0,用户以为「统计被清零了」。三层根因:①ffmpeg 崩溃重建会换场次前缀,而弹幕会话原先只在 `danmaku_patrol_loop`(60s)里 `retarget`,这几十秒的新礼物写进**上一场**的文件 —— 上一场早已 `_history_finish` 结算(读到 0 条),本场又只认自己的文件,实测 41 条谁的账都不算;②`retarget` 只归零轻通道三段账,漏了 `gift_count`/`last_gift_ts`/`msg_count`/`chat_count`,于是「本场 0 条礼物」的诊断快照仍显示上一场累计的 101 条,与同一行 `light.recv/kept=0` 并排自相矛盾(静默计时不归零还会让新场次一开播就误报礼物静默);③`_history_finish` 读**会话当前计数**,场次切换后就是「B 场的收到数对 A 场的落盘数」。修法:`_restart_recording` 重开录制后**立即** `retarget_for`;`retarget` 四项计数一并归零;结算走 `DanmakuRecorder.ledger_for(jsonl)` 按文件取账,再用 `_sibling_danmaku_jsonls()` + `gift_channel_coverage(extra_paths=)` 从兄弟 jsonl 借回落在本场时间窗口内的迟到行(报在 `light.adopted`;**没有 `started_at` 一律不借**,否则同一批礼物会被算进两个场次,比丢数据更糟)。守 `tests/test_gift_cross_session.py`。


---

## 6. AI 智能体阅读指引（遇到问题改哪里）

| 症状 | 排查入口 | 大概率改 |
|---|---|---|
| 检测不到开播/误判 | `detect.py::check_302` / `extract_room_status` | detect.py |
| 主播填了 sec_uid 但不工作 | `detect.py::probe_anchor` | detect.py / 前端提示 |
| 录制不开始/无流地址 | `detect.py::extract_stream_url` + `monitor.py::_refresh_recording` | detect.py / monitor.py |
| 录制文件损坏/不完整 | `monitor.py::_start_recording` 的 ffmpeg 参数 | monitor.py |
| 前端看不到录制状态 | `monitor.py::_sync_recording_state` + `webui.py::State.recordings` + `index.html` 的 `.s-rec` | 三处联动 |
| 录制历史不对 | `monitor.py::_history_add/_history_finish` + `webui.py::/api/history` | monitor.py / webui.py |
| NAS 挂载失败 | `nas.py` + `smbutil view //user@host` 先确认真实共享名 | nas.py |
| 配置改不动/报错 | `webui.py::_validate` / `update_config` | webui.py |
| 调度间隔不对/跨零点 | `schedule.py::next_interval` / 环形距离 | schedule.py |
| 热点时段不准/学习慢 | `schedule.py` 分析层 `analyze()` + `monitor.py::_record_open` 三源分级 | schedule.py / monitor.py |
| 重启后热点被污染 | `schedule.py` 会话层 `_meta` + `monitor.py::run_streamer` 恢复逻辑 | monitor.py |
| 前端保存配置报错/假成功 | `index.html` 的 `collectFields`(badInput 拦截) + `pathSet`(逐级建对象) | static/index.html |
| 改配置后不生效 | 需重启 monitor（后端是常驻进程，改 .py 不重启不生效） | — |

**上下文边界（AI 智能体省 token 指引）**：

| 要改什么 | 只需读 | 不用读 |
|---|---|---|
| 检测/流地址/反爬 | `detect.py`(230行) | 其余全部 |
| 调度节奏 | `schedule.py::next_interval` + monitor 的 deadline 逻辑 | detect/nas/webui |
| 热点学习算法 | `schedule.py`(300行) + 本文件 §3.3 | monitor 主体 |
| 录制/ffmpeg/历史 | `monitor.py` 的录制函数族(:277-540) | detect/schedule |
| NAS 归档 | `nas.py` | 其余全部 |
| 前端 UI/表单 | `static/index.html` 对应视图段 | 所有 .py |
| 配置校验 | `webui.py::_SCHEDULE_RULES` + `monitor.py::DEFAULT_CONFIG` | 其余 |

原则：**先读本文件的对应小节 → 只打开边界表指定的文件 → 改完跑 `./.venv/bin/python -m unittest discover tests`**。不要为单一问题通读全部源码。

**通用调试三板斧**：
1. 看日志 `logs/monitor-YYYY-MM-DD.log`（`[开播]/[下播]/[录制]/[归档]` 等关键行）。
2. 调 `/api/status` 看 streamers/recordings 实时状态。
3. 独立跑 `python3 -c "import detect; ..."` 或 `curl` 直接探测 `live.douyin.com/{web_rid}`，把「网络/抖音侧」和「代码」分开。

**修改后必做**：`python3 -m unittest discover tests`（全绿）+ `bash scripts/verify.sh d`（全量回归）+ 重启 monitor 进程。

---

## 7. 运行与部署

### 一键启动（macOS，双击）
双击 `一键启动.command`（首次自动 `setup.sh` 部署，之后直接启动）。
窗口里会**直接列出全部终端指令和它们的效果**（渲染自 `scripts/ctl.py`，与 `douyin help` 同源），
入口丢了会顺手补装 —— 不必记命令，打开就能看到有哪些选项。

**启动前先做目录自检**（`verify_project()`，在拉起任何进程之前）：入口文件齐备 → 目录里的代码
是同一代（三个能力锚点分属后端纯函数 / API 层 / 前端消费）→ 本目录就是 launchd plist 认定的
正式部署目录。任一不过就拒绝启动并说明原因；如果只是误点进了副本目录，脚本会问一句「要改为从
正式目录启动吗」，按 `y` 直接 `exec` 转交给正式目录那一份（不静默跨目录启动）。为什么需要这道闸：
`~/Downloads` 下并存历史发布快照目录，把新版启动脚本拷进旧快照里双击，会跑起「入口新、页面旧」
的实例 —— 整站界面回退好几个月，而端口探活仍是 HTTP 200，外层看着一切正常。确实要在副本里临时
跑一份，显式 `DOUYIN_ALLOW_SIDE_COPY=1 ./一键启动.command`。

判活同理不能只看 HTTP 200：脚本会读 `/api/status` 的 `project_root`（老进程没这个字段时退回
`lsof` 读进程工作目录）确认「端口上跑的就是本目录」，不是就打印对方路径并问是否停掉它重启。
**起服务之后也一样**：`open_browser_when_ready()` 要拿到身份且等于本目录才 `open`，否则本目录
进程因端口被占而启动即退出时，会打开别人那份的页面（窗口写着「已启动」，屏幕是旧界面）。

启动完成后窗口**留作终端控制台**，按单个字母即可操作（不用回车、不用另开终端）：
`o` 打开 Web 控制台 / `s` 检测运行状态 / `r` 重启（未运行时就是启动）/ `q` 关闭 /
`l` 跟随今日日志（`Ctrl+C` 回菜单）/ `h` 命令速查 / `Enter` 退出窗口。
按键只是转发给 `scripts/ctl.py`，与 Web 控制台侧边栏底部的「重启 · 关闭」共用
`webui._service_plan()` 那一份启停规则；会中断录制的动作（重启/关闭）先数一遍
`/api/status` 里正在录制的场次，有的话要求按 `y` 确认。

### 手动启动
```bash
cd douyin-monitor
./setup.sh                    # 首次:建 .venv + 装 httpx + 生成 config.json
.venv/bin/python monitor.py   # 启动,浏览器开 http://127.0.0.1:8780
```

### 终端快捷指令（日常最省事）
```bash
bash scripts/install_cli.sh   # 装一次:~/.local/bin/douyin + zsh 别名(可重复执行)
douyin help       # 命令速查(全部命令 + 别名 + 作用,不必记)
douyin            # 状态总览(服务 + 健康 + 直播)     别名 dy
douyin restart    # 重启(先清掉占用端口的非托管实例)   别名 dyr
douyin start      # 开启(已在跑时是空操作,不打断录制)  别名 dyon
douyin stop       # 关闭                             别名 dyoff
douyin log -f     # 跟随日志(dylog)  douyin health(健康面板, dyh)
douyin doctor     # 自检终端环境(PYTHONPATH/代理隐患, dydr)
```
启停规则与 Web 界面按钮共用一份 `webui._service_plan()`,不会两边不一致;`-n` 可干跑预览。
次序规则:**已装载的 job 只 `kickstart`(不带 `-k`)、未装载才 `bootstrap`,`start` 永不 `bootout`** ——
`bootout` 会给正在录制的进程发 SIGTERM,并把 launchd 的 60 秒节流窗口打开(2026-10-29 因此中断过 2 路录制)。
真正会打断录制的是 `restart` / `stop`,终端里执行前会先报一行「当前 N 路正在录制」。
命令清单只维护在 `scripts/ctl.py` 的 `COMMANDS` 表:argparse 帮助、`douyin help`、启动窗口里显示的表、
以及 Web 控制台「系统健康 → 终端快捷指令」卡片(数据 `GET /api/shortcuts`,点命令格可复制)都由它渲染。
项目搬家后重跑一次安装脚本即修复入口路径。

### 开机自启（无人值守）
```bash
./setup.sh --launchd          # 注册 ~/Library/LaunchAgents/com.douyin.monitor.plist
```

### 迁移到新设备（如 MacBook Air M4）

#### 打包（源机）
```bash
bash scripts/pack.sh          # 产出 dist/douyin-monitor-<版本>.tar.gz(已脱敏 config)
```
包内含代码 + 文档 + 启动脚本 + 单测;**不含** `.venv/` `recordings/` `logs/` `hotspots.json`(运行时产物,新机自动生成)。

#### 新机部署步骤
```bash
# 1. 拷贝 tar.gz 到新机,解包
tar xzf douyin-monitor-*.tar.gz && cd douyin-monitor-*

# 2. 去除隔离属性(从网盘/AirDrop 来的需要)
xattr -dr com.apple.quarantine .

# 3. 装 ffmpeg(录制必需,新机一般没有)
brew install ffmpeg

# 4. 双击启动(首次自动 setup.sh 建 venv + 装 httpx,之后直接启动并开浏览器)
open "一键启动.command"
# 或终端:./一键启动.command
```

#### 迁移后必须重新设置/检查的项

| 项 | 是否随包迁移 | 新机要做什么 |
|---|---|---|
| **NAS 密码** | ❌ 不迁移 | 钥匙串不随项目走,到「NAS 归档」页重新填密码保存(密码只存本机钥匙串) |
| **NAS 主机/共享名** | ✅ config 带走 | 一般不变;若新机网络环境不同(如不同子网),改 host |
| **ffmpeg** | ❌ 不迁移 | `brew install ffmpeg`;`find_ffmpeg` 会自动定位(支持非标准 Homebrew 路径) |
| **主播列表** | ✅ config 带走 | 自动带过去,无需重添;若想换主播在 UI 改 |
| **录制码率/分片/格式** | ✅ config 带走 | 自动带过去;在「高级参数→录制设置」可调 |
| **Python 环境** | ❌ 不迁移 | `一键启动.command` 首次自动 `setup.sh` 建 venv + 装 httpx |
| **挂载点 ~/DouyinArchive** | ✅ 自动 | 新机用户目录可写,`ensure_mount` 自动创建 |
| **归档根目录「直播回放」** | ✅ config 带走 | 挂载成功后 `_init_dirs` 自动在 NAS 建 |
| **端口 8780** | ✅ config 带走 | 一般不变;若冲突改 config.webui.port |
| **防休眠** | ❌ | 无人值守时 `caffeinate -dimsu &` 或 `setup.sh --launchd` |
| **录制历史/录制文件** | ❌ 不迁移 | 新机从空开始(recordings/ 自动建) |
| **热点窗口/开播事件数据** | ✅ 建议随包 | 本包已含 `hotspots.json`+`hotspot_events.json`,新机免重新积累;不想带可删,系统从空学习 |

#### 迁移后验证清单
1. 自检横幅全绿(ffmpeg 就绪、httpx 就绪)
2. 「主播管理」页能看到带过去的主播,点「刷新」能检测
3. 主播开播后「录制中」状态出现、recordings/ 有文件
4. NAS 页填密码保存后状态显示「已挂载」
5. 「录制历史」页能看记录、点「打开」能播放

### 依赖
- Python 3.11+、`httpx`（setup.sh 自动装）
- `ffmpeg`（录制必需，`brew install ffmpeg`）
- `smbutil`/`mount_smbfs`（NAS 归档，macOS 自带）
