# 抖音直播监控录制系统 · 项目指南

> 本文件面向 **AI 智能体 / 后续维护者**。在另一台设备上运行遇到 bug 时，先读本文件，能快速定位「改哪里、怎么改、有哪些历史坑」。

---

## 1. 项目概述

一个跑在 macOS 上的抖音直播监控 + 自动录制系统：

- **检测**：监控主播是否开播（无需登录/Cookie，纯 302 + 页面解析）
- **录制**：检测到开播后，用 `ffmpeg` 从直播流地址拉流录制到本地 `recordings/`
- **归档**：下播后可自动挂载 SMB（NAS）网络盘并搬运录制文件
- **前端**：内嵌 Web UI（`http://127.0.0.1:8780`），管理主播、看录制状态/历史、配 NAS

技术栈：**Python 3.11+（asyncio + httpx）** + 原生 `http.server` Web UI + `ffmpeg`（录制）+ `smbutil`/`mount_smbfs`（NAS）。无第三方 Web 框架、无数据库，全部状态用 JSON 文件持久化。

---

## 2. 目录结构

```
douyin-monitor/
├── monitor.py            # 主入口:协程调度、录制管理(含手动停止/开始)、录制历史、孤儿清理、信号处理、开播时间三源分级
├── detect.py             # 开播检测:302 探测 + reflow 页解析(状态/流地址/create_time 真实开播时间)
├── link_resolver.py      # 分享链接解析:粘贴 v.douyin.com 链接 → sec_uid/web_rid/昵称(httpx 同步)
├── schedule.py           # 热点三层追踪:实时层(窗口合并) + 分析层(事件日志环形聚类) + 会话层(_meta 防重启污染)
├── webui.py              # HTTP 服务 + State(共享状态:streamers/recordings/history/nas/rec_ctrl)
├── nas.py                # NAS 挂载(_mount_point 支持 ~)/归档队列/钥匙串凭据/SMB 共享列表
├── preflight.py          # 启动自检 + find_ffmpeg(非标准 Homebrew 路径兜底)
├── preview.py            # 录制预览截图(供前端缩略图)
├── static/index.html     # 前端单页(主播管理/链接解析/调度/NAS/录制历史)
├── config.json           # 运行时配置(主播列表/检测/调度/NAS/录制器)——首次运行自动生成
├── hotspots.json         # 热点窗口 + _meta 会话状态(运行时生成;迁移时建议随包带走以保留已学习的热点)
├── hotspot_events.json   # 开播事件日志(运行时生成,7 天保留;迁移时建议随包带走)
├── recordings/           # 录制输出(运行时生成):recordings/<主播>/<主播>-<时间>.flv
│   └── history.json      # 录制历史(启动时自动清理孤儿 recording 记录)
├── logs/                 # 日志(按天,保留 7 天):monitor-YYYY-MM-DD.log
├── spool/                # NAS 归档待搬运队列(运行时生成)
├── tests/                # 单测(21 个文件/380 项):检测/调度/录制/归档/服务控制/健康面板/快捷指令卡片
├── scripts/
│   ├── verify.sh         # 机械验收门(分 phase 累积执行:verify.sh a/b/c/d)
│   ├── pack.sh           # 打包迁移脚本(脱敏 config → tar.gz)
│   ├── ctl.py            # 终端快捷指令实现(douyin status/start/stop/restart…);COMMANDS 表=命令清单唯一真源
│   └── install_cli.sh    # 安装/修复 ~/.local/bin/douyin 与 zsh 别名(幂等)
├── setup.sh              # 一键部署(venv + 依赖 + 可选 --launchd 开机自启)
├── 一键启动.command        # macOS 双击即用(首次自动部署 + 启动后自动打开浏览器)
├── requirements.txt      # 唯一依赖:httpx
├── README.md             # 面向用户的使用说明
├── PROJECT_GUIDE.md      # 本文件(交付对接书)
└── agents/ + *.md         # 原始项目书/任务书(历史文档,运行时无需)
```

运行时产物（不入库）：`.venv/` `logs/` `recordings/` `spool/` `previews/` `__pycache__/` `dist/`。
`hotspots.json` / `hotspot_events.json` 是**已学习的热点数据**，迁移时建议随包带走（不带也能跑，新机重新积累）。

---

## 3. 核心模块详解

### 3.1 monitor.py（主循环）
- `DEFAULT_CONFIG`：所有配置默认值。**新增配置项必须同时在这里和 `webui.py::_SCHEDULE_RULES`（校验规则）登记**。当前默认:NAS 挂载点 `~/DouyinArchive`、归档根目录「直播回放」、录制码率 0(复制原流)。
- `run_streamer()` → 内嵌 `once()`：单主播的检测循环。**`once()` 是核心**，一次完整检测：热重取配置 → `probe_anchor` 解析探测锚点 → `check` → 开播/下播分支 → 录制管理 → 状态上报。
- 调度节奏：用 `deadline` 绝对时间 + 手动刷新标志，**手动刷新立即检测一次但不重置 deadline**（`next_in_hint` 传剩余秒）。
- 录制管理：`_recordings` dict + `_start_recording/_stop_recording/_refresh_recording/_sync_recording_state`。`_build_ffmpeg_args` 按配置构造参数(码率→重编码/0→-c copy;分片→-f segment)。
- **录制手动控制**:`_manual_paused` 集合 + `_last_stream_url` 缓存;`_stop_recording_manual`(标记暂停+停)/`_start_recording_manual`(清暂停+用缓存流启动);once() 直播中若 paused 则不自动重录,下播时 discard 暂停标记。main() 注入 `state.set_rec_ctrl({stop,start})`(**回调注入,避免 webui↔monitor 循环依赖**)。
- 录制历史:`_history` + `recordings/history.json`,`_history_add/_history_finish`。**`_load_history` 启动时自动清理孤儿 recording 记录**(status=recording 且文件不存在且不含 %03d),根治 mock 测试/进程崩溃残留。
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
- `check_302()`：`GET live.douyin.com/{web_rid}` → 302 则再抓 reflow 落地页解析。**返回 dict 含 `is_live / status / stream_url / room_id / sec_uid / create_time / method`**。
- `extract_room_status(html)`：从 reflow 页解析 `room.status`（2=直播中，4=已结束）。**注意页面是转义 JSON，`room.status` 后紧跟 `ownerUserId`（user 资料里的 status 后跟 createTime，要避开）**。
- `extract_create_time(html)`（detect.py:50）：从 reflow 页解析 `room.create_time`（真实开播时间，秒级时间戳），供 S1 分级使用。**零新增 API 调用**——只从既有检测响应顺手解析，避免触发抖音风控。
- `extract_stream_url(html)`：提取拉流地址，**过滤 `only_audio=1` 纯音频流**（无清晰度后缀的 `stream-{id}.flv` 是音频流、只有声音没画面），优先原画 `_or4.flv`，fallback 任意视频 flv → m3u8。**正则 `[^"\s]` 不能排除反斜杠，否则 `\u0026` 被截断丢 sign 签名**；末尾 `rstrip("\\")` 去转义引号残留。
- `probe_anchor(mon)`：解析探测锚点——优先 `web_rid` 字段 → anchor 纯数字 → 仅 sec_uid 时返回明确错误。
- `check_api()`：room status API（需 Cookie），无 Cookie 时整条链路退化为纯 302。

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

### 3.5 nas.py（NAS）
- `ensure_mount` / `test_connection` / `list_shares`（`smbutil view`）/ 归档队列（`spool/pending.json`）。
- **`_mount_point(nas_cfg)`：expanduser 展开挂载点**(默认 `~/DouyinArchive`,用户目录可写)。**切勿用 `/Volumes/xxx`**(系统目录,普通用户 mkdir 会 PermissionError)。
- `_init_dirs`：首次挂载成功后按 monitors 建 `<mount>/<root_dir>/<主播名>/` 目录树(root_dir 默认「直播回放」)。
- 凭据：密码只进钥匙串（`security add-generic-password`，**必须带 `-a default`**），config 只存 `password_ref`。
- `clean_host`/`clean_share`：剥离 `smb://` 前缀、首尾斜杠。
- `_smb_url`：用户名/密码/共享名 percent-encode。

### 3.6 preflight.py（自检 + ffmpeg 定位）
- `find_ffmpeg()`：**which 失败后扫常见 Homebrew 前缀**(/opt/homebrew、/usr/local、~/.homebrew、~/homebrew、~/bin)兜底返回绝对路径。用户可能用非标准 Homebrew prefix,运行时 PATH 不含其 bin,必须用绝对路径。
- 6 项检查，返回 `[{id,title,status,detail,action}]`，前端横幅 3s 轮询展示。

### 3.7 link_resolver.py（分享链接解析）
- `resolve(raw_text)`：粘贴 `v.douyin.com`/`live.douyin.com`/主页链接 → 跟随 302 + 解析 HTML → 返回 `{ok, sec_uid, web_rid, nickname}`。httpx 同步(follow_redirects=False 手动循环)。由 douyin-link-parser 项目移植。

### 3.8 static/index.html（前端）
单页,无框架。`showView()` 切换视图;`loadStatus/loadHistory/loadNasStatus/loadPreflight` 轮询。主播卡片有「刷新」「停止/开始录制」按钮、「最近开播时间」标签、录制中隐藏 id 显示录制详情;历史列表每条有「打开」按钮;添加主播处有「分享链接解析」框。

---

## 4. 关键数据流

```
添加主播(name, anchor=sec_uid, web_rid)
        ↓
monitor.run_streamer → once()
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
18. **码率单位**。`recorder.bitrate` 是数字(Kbps),后端 `_build_ffmpeg_args` 自动拼 `Xk`;0=复制原流。1M=1000K(非 10K)。
19. **只有声音没画面（严重）**。reflow 页里「无清晰度后缀的 `stream-{id}.flv`」带 `only_audio=1` 是**纯音频流**；视频流反而带清晰度后缀（`_or4`/`_hd`/`_md`/`_sd`/`_ld`）。`extract_stream_url` 必须过滤 `only_audio=1`、优先 `_or4.flv`，否则录出来的 flv 只有声音没有画面。
20. **录制中时长要用墙钟,不要解析媒体文件**。`_media_duration`(ffmpeg 解析)对「正在写入的文件」比真实墙钟短 5-15s(启动/缓冲延迟),且每次解析要起 ffmpeg 进程。直播中 `duration` 用 `now - started_at` 纯墙钟;只有历史回填(下播后)才用 `_media_duration`。
21. **前端 number 输入框静默丢弃非法内容**。用户输入「30分钟」/全角数字时,浏览器把 `input.value` 读成空串,`Number("")==0`,会**假保存成功**。收集表单时必须检查 `input.validity.badInput` 并报错拦截。
22. **前端 pathSet 必须逐级创建中间对象**。`t["recorder"]["segment_time"]=v` 在 `t["recorder"]` 为 undefined 时直接崩溃(`undefined is not an object`)。所有配置字段都是两级路径,任何保存必经 pathSet——改它要跑「两级/四级/空路径」用例。
23. **静态资源缓存**。改 `static/index.html` 后浏览器可能用启发式缓存旧版 JS,后端首页响应必须带 `Cache-Control: no-store`,否则「代码已改但前端行为不变」误导排查。
24. **重启污染热点数据**。重启后内存态(`last_room_id/last_interval`)清零,若主播恰在播,旧代码会把「重启时刻」当开播时刻写进热点。现在靠 `_meta` 会话持久化 + S3 分级跳过;改检测循环时**不要删掉这两处**,否则污染回归。

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
douyin start      # 开启                             别名 dyon
douyin stop       # 关闭                             别名 dyoff
douyin log -f     # 跟随日志(dylog)  douyin health(健康面板, dyh)
douyin doctor     # 自检终端环境(PYTHONPATH/代理隐患, dydr)
```
启停规则与 Web 界面按钮共用一份 `webui._service_plan()`,不会两边不一致;`-n` 可干跑预览。
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
