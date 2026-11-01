# 抖音直播监控录制系统

双通道开播检测 + 自适应轮询调度 + 内嵌 Web UI 的抖音直播监控脚本（配合 `DouyinLiveRecorder` 使用）。

对抖音多个主播自动监测开播状态：开播即触发录制命令，下播触发归档命令。检测对反爬友好（302 轻量探测 + API 确认双通道、错峰轮询、自适应间隔、熔断退避）。每个主播可设**录制期限**：添加时选长期录制或短期录制（1/3/7 天，从添加当天起算），到期自动停止录制、可随时续期或改长期（2026-10-22）。

## 快速开始

```bash
pip install httpx
python3 monitor.py
# 打开 http://127.0.0.1:8780
```

首次运行自动生成 `config.json`（带默认值）。热点窗口持久化到 `hotspots.json`。

## 终端快捷指令

装一次之后，在任意目录都能用（macOS 自带 zsh）：

```bash
bash scripts/install_cli.sh   # 写 ~/.local/bin/douyin + ~/.zshrc 别名（可重复执行）

douyin help       # ★命令速查：全部命令 + 别名 + 作用（不必记，敲这个就行）
douyin            # 状态总览：服务 + 健康面板摘要 + 直播/录制   (= dy)
douyin restart    # 重启（= dyr），会先清掉占用端口但不在 launchd 名下的实例
douyin start      # 开启（= dyon）
douyin stop       # 关闭（= dyoff）
douyin health     # 健康面板逐项（= dyh）      douyin log -f   # 跟随日志（= dylog）
douyin doctor     # 自检终端环境有无 PYTHONPATH / 代理隐患（= dydr）
douyin diagnose   # 跨项目诊断包：场次排查证据打包投递 NAS（纯 CLI，不依赖服务）
douyin notice     # 产物变更通告：写 NOTICE.md 并投递契约三件套，告知后端及时同步
douyin checkav    # 音画同步自检：数音频采样数与视轨跨度，超阈值即报（= avsync）
douyin open       # 在浏览器打开 Web 控制台
douyin restart -n # 干跑：只打印将要执行的步骤，不真执行
```

- 入口 `~/.local/bin/douyin` 会先清 `PYTHONPATH` 与代理变量再执行 —— 这两个坑都曾导致「服务看着活着但检测/录制全废」。
- 服务启停的次序规则（先清端口外来实例；**已装载就直接 `kickstart`，未装载才 `bootstrap`，`start` 永不 `bootout`**）与 Web 界面按钮**共用一份** `webui._service_plan()`，不会两边行为不一致。所以 `douyin start` 在服务已经跑着（哪怕正在录制）时是安全的空操作 —— 真正会中断录制的是 `restart` / `stop`。
- 命令清单只维护在 `scripts/ctl.py` 的 `COMMANDS` 表里：argparse 帮助、`douyin help`、双击 `一键启动.command` 时窗口里打印的速查表、以及 Web 控制台「系统健康 → 终端快捷指令」那张卡片，全都由它渲染，四处不会各说各话。记不住命令时也可以直接看那张卡片（`GET /api/shortcuts`），点命令格即可复制。
- 项目搬家或换机后重跑 `bash scripts/install_cli.sh` 即自动修复入口路径；`--uninstall` 可整体卸掉。

## 月度同步到 GitHub

本地改动按**月度**同步到公开仓库（每月 1 日 21:00 自动触发，手动同样可用）：

```bash
.venv/bin/python scripts/sync_github.py --check     # 只扫描:待提交文件里有没有真实值泄露
.venv/bin/python scripts/sync_github.py --dry-run   # 预览将提交哪些文件
.venv/bin/python scripts/sync_github.py             # 扫描 → 提交 → 推送
```

脚本先从 `config.json`（已被 `.gitignore` 排除）反推禁词集 —— 主播昵称、`sec_uid`、
直播间号、NAS 主机/共享名、内网网段 —— 命中任一项即**中止不推**。
`.gitignore` 已排除 `auth/`（登录态）、`config.json` 及备份、`hotspots*.json`、
`recordings*/`、`logs/`、`previews/`、`spool/`、`.venv/`。

> 受管/自动化环境里 `github.com` 不可达（只有 `api.github.com` 通），脚本因此走
> Git Data API 推送；在你自己的终端里 `git push` 同样可用。

## 文件结构

```
douyin-monitor/
├── monitor.py          # 入口 + 主循环(协程守护 + 动态 reconcile)
├── schedule.py         # 热点窗口 + 自适应间隔
├── detect.py           # 双通道检测(302 + API)
├── preflight.py        # 启动自检(依赖/ffmpeg/NAS/Cookie/防休眠)
├── quality.py          # 画质调度:分辨率档位换算/降级收益判定/自动降级状态机(纯逻辑)
├── webui.py            # 内嵌 HTTP 服务(历史接口带 NAS 目录列举缓存)
├── notify.py           # 告警通道层(企微群机器人 → macOS 通知 → 日志)
├── interop.py          # 跨项目对接层(契约快照/session.json/问题反馈消费/诊断包/信箱探测/产物变更通告,全旁路)
├── contract/           # 跨项目契约真源(contract.json + CHANGELOG.md)
├── scripts/ctl.py      # 终端快捷指令实现（douyin status/start/stop/restart…）
├── scripts/install_cli.sh  # 安装/修复终端入口与 zsh 别名
├── static/index.html   # 控制台页面(可折叠侧栏 148px;主播卡片四列;录制详情在详情弹窗;轮询统一走 pollJSON 单飞+超时)
├── config.json         # 可调参数(运行时热更新)
├── scripts/verify.sh   # 机械验收门
└── setup.sh            # 一键部署(可选 launchd 开机自启)
```

## 核心参数（config.json → schedule）

| 参数 | 默认 | 说明 |
|---|---|---|
| cold_interval | 1800s | 冷门时段轮询(30min) |
| hot_min / hot_max | 300 / 360s | 热点窗口轮询(5~6min) |
| live_interval | 1200s | 录制中兜底轮询(20min) |
| hotspot_max | 2 | 每主播热点上限 |
| hotspot_half_width | 1800s | 热点窗口半宽(±30min) |
| hotspot_merge | 2700s | 窗口合并阈值 |
| hotspot_decay_days | 14 | 热点衰减天数 |
| circuit_errors / circuit_cooldown | 5 / 1800s | 熔断阈值与冷却 |

## 录制画质（config.json → recorder）

2026-10-22 起默认走 **VideoToolbox 硬编**：7 路并发 CPU 从 7.7–11.2 核降到 0.3–0.4 核。

| 参数 | 默认 | 说明 |
|---|---|---|
| video_mode | `vt` | `vt` 硬编(默认) / `sw` libx264 软编 / `copy` 流复制 |
| resolution | `0` | 分辨率档位：`0` 原生 / `1080` / `864` / `720`（目标短边像素，只缩不放）。**只影响画质与引擎占用** |
| bitrate | `1200` | 视频码率 kbps。**体积的唯一决定因素**（同码率下各档位体积相同），38 分钟一场约 380 MB |
| auto_degrade | `true` | 过载自动降档：CPU≥92% 或引擎≥92% 持续 60s 降一级；**只有本地资源类**崩溃才直降最低档（流地址 404 等流侧故障只记账不判定）；恢复要回落 ≤72% 持续 600s、距上次切换 ≥900s、距上次**资源类**崩溃 ≥1800s、当日 ≤4 次 |
| degrade_l1/l2_* | `720`/`700`、`720`/`500` | 降级两档 = (分辨率, 码率)，录制设置页可改；主配置已不高于 L2 时降级自动失效 |
| max_hw_streams | `8` | 在录硬编路数上限，超出的新路直接流复制（不占媒体引擎） |

想省体积就降 `bitrate`；想省 CPU/引擎就降 `resolution` —— 两者互不替代。

## 礼物采集口径（2026-10-29 起：全主播统一轻通道）

抖音把礼物事件分两条通道下发，**匿名观众身份只能收到轻通道**，这不是抓取强度问题。
所以本项目把礼物口径**固定为轻通道**：`WebcastLightGiftMessage` 是唯一真源，
完整通道（`WebcastGiftMessage`）事件不再落盘 —— 它与轻通道是服务端事件级互斥路由，
匿名侧 349 场实测恒 0 条，两条都收只会把同一笔礼物重复计数。

| 场次类型 | 逐条礼物 | 金额 | 记录方式 |
|---|---|---|---|
| 轻场（`gift_channel=light`） | 与登录账号**逐条一致**（实测 7/7、42/42，价值 100%，时间差中位 34ms） | 精确 | `danmaku.jsonl` 的 `type=gift` 行（`diamond × count`），`channel` 恒为 `light` |
| 未下发场（`full_only`） | **拿不到**（服务端整组关闭礼物互动消息） | 只能靠榜单 | `.meta/*.giftrank.jsonl` 的 `type=rank` 行（前 20 名的**累计**送礼抖币，1:1 精确） |

- 同一场直播内会在**几分钟级切换**通道，所以每一场都会标注 `gift_channel`：`light` / `full_only` / `quiet` / `unknown`。
- **`full_only` 的 0 条礼物不代表没人送**，也不是录制故障 —— 属平台能力边界（重连/换参数/游客态四重证伪）。历史页与日报都单列，且**不再推告警**。
- 轻通道只承诺 4 个字段：**时间 / 礼物标识 / 单价 / 份数**。送礼人昵称、uid、礼物名协议不下发（登录态同样没有），所以礼物健康度**不**拿它们判完整性。

### 礼物健康度 = 轻通道对账（不是"有没有人送礼"）

每 60 秒对一次账，三个数放一起看：**收到多少条**（弹幕连接实时计数）、
**jsonl 落下多少条**、**每条 4 个字段齐不齐**。任何一环不齐都是真实缺口：

| 结论 | 级别 | 含义 |
|---|---|---|
| `LIGHT_GIFT_PARSE_ERR` / `LIGHT_GIFT_WRITE_ERR` | 🔴 推送 | 解析失败（proto 失配）/ 写盘失败（磁盘、权限）—— 数据真丢了 |
| `LIGHT_FIELD_MISS` / `LIGHT_EVENT_LOST` | 🟠 推送 | 落盘了但字段缺项 / 条数对不上 —— 数据在但不完整 |
| `LIGHT_CHAN_OFF` / `LIGHT_QUIET` / `LIGHT_OK` | ⚪ 不推送 | 本场未下发轻礼物 / 房间冷清 / 暂时没人送 —— 通道态说明 |

结果写进 `history.json` 的 `gift_integrity.light`（`status ∈ complete|partial|field_only|channel_off`），
历史行胶囊按完整率**百分比**分 6 档（无数据 / ≤25% / 26~50% / 51~75% / 76~94% / ≥95%）：
**4 格**表示档位段、**数字**是精确完整率、末端**圆点**表示「已对账且 ≥95%」、
**空心格**表示该场没对过账（补算场次 `field_only` / 旧口径时间占比）—— 未对账一律不发圆点；
诊断证据落 `.meta/*.giftwatch.jsonl`。

## 礼物展示（字幕横幅，2026-10-29 起）

礼物不再"一条事件一条横幅、全挤同一个位置"，改为**竖屏多行堆叠 + 快慢分道**：

| 项 | 规则 |
|---|---|
| 名称来源 | 官方 `gift/list` 字典（`state/gift_dict.json`，1314 条）；jsonl 里仍是 `礼物#id`，渲染时换成真名 |
| 快车道 | `≤98 抖币`：左上小字，停留 1.5s（拥挤时自动压到 1.0/0.8/0.6s） |
| 慢车道 | `≥99 抖币`：左上大字带金额，停留 5s（拥挤时 3.0/2.5s） |
| 合并 | 仅完整通道、同一人同一礼物 3 秒内合并成 `×N` 递增；**匿名轻礼物不合并**（平台不给送礼人） |
| 版面 | 竖屏 14 个等高槽位、行距 38px（1080×1920 下 y=60…554）；**连续堆叠不留空行**，同屏常规 7~8 行、密集最多 14 行；礼物区总高 ≤ 画面 2/5（超出自动裁槽）；横屏走旧版式 |
| 产物 | `.meta/*.giftdisplay.json`（可选）：与 `.ass` 同一份计划，实时预览与下游都从它取语义 |
| 回退 | `config.json → subtitle.gift_display.enabled=false` 立即回到旧的单横幅路径 |

## 锚点获取

控制台「添加主播」弹窗里粘一次就能自动填：**抖音号**、**主页分享链接**、**直播间分享链接**三种都行（`POST /api/resolve`）。

- `sec_uid`：抖音 App → 主播主页 → 复制链接 → 粘进「解析」，或直接粘**抖音号**（纯数字号与字母号都认）反查出 `MS4wLjAB...`（最稳定）
- `web_rid`：直播间地址 `live.douyin.com/{数字}` 中的数字（较稳定）。**只有直播间链接才带它**——主播没开播时抖音不对外暴露，此时可以先只填 `sec_uid` 添加（卡片标「缺web_rid」、暂不检测），等他开播时把直播间分享链接再粘一次「解析」补上即可，补完下一轮巡检自动生效。

### 缺 web_rid 的自动补齐（2026-10-26）

只填了 `sec_uid`/抖音号就加进来的主播，系统会在后台自动补直播间号：每 40–120 分钟（三档随机，均值≈76 分钟）用 headless Chromium 打开该主播主页，页面里出现 `live.douyin.com/<数字>` 就视为开播，校验归属后写回 `config.json` 并立即开始检测。

- **有缺号主播才启浏览器，没有就完全不跑**（`rid_backfill.enabled="auto"`）；自动窗口 7 天，到期不自动续期（避免长期自动访问被风控）。
- 想立刻停：把 `enabled` 改成 `false`；想马上试一次：`.venv/bin/python scripts/probe_web_rid.py <sec_uid或抖音号>`。
- 状态看「高级参数 → 缺号补齐」或 `GET /api/status` 的 `rid_backfill` 段；失败原因（登录墙/滑块/限流/空壳）落 `state/rid_backfill_fails.jsonl`，每日日报里也有一行。

## 命令模板占位符

开播/下播命令模板支持：`{name} {anchor} {room_id} {sec_uid} {room_url} {stream_url} {BASE}`。

其中 `{BASE}` 为项目根目录绝对路径，跨机器迁移时无需改绝对路径，例如：

```
python3 {BASE}/recorder/recorder.py --room {room_id}
```

## 新机部署

在全新 Mac 上两条路径二选一：

```bash
# 路径一：手动运行(前台)
./setup.sh
.venv/bin/python monitor.py

# 路径二：一键部署 + 开机自启(launchd)
./setup.sh --launchd
```

`setup.sh` 会自动：检测 Python ≥3.11 → 创建 `.venv` 并按 `requirements.txt` 安装全部依赖（`httpx`、`websocket-client`、`betterproto`、`playwright`、`imageio-ffmpeg` 等） → 生成默认 `config.json` → （可选）注册 `~/Library/LaunchAgents/com.douyin.monitor.plist` 开机自启。完成后打开 `http://127.0.0.1:8780` 完成配置向导（NAS 连接 / Cookie / 主播列表）。

> 日常使用直接双击 `一键启动.command` 即可（自动判活：已运行则开浏览器，端口被占但无响应会先清理再启动）。
> 启动前会先做**目录自检**：文件不齐、目录里的代码不是同一代（典型：把启动脚本拷进了 `douyin-monitor-release-YYYYMMDD` 之类的历史快照目录）、或者本目录不是 launchd 认定的正式部署目录，都会**拒绝启动并说明原因** —— 在旧快照里启动过会跑起「入口新、页面旧」的实例，整站界面回退好几个月，而外层探活只看 HTTP 200 还报"正常"。确实要在副本里临时跑一份，显式 `DOUYIN_ALLOW_SIDE_COPY=1 ./一键启动.command`；如果只是误点进了副本目录，脚本会问一句「要改为从正式目录启动吗」，按 `y` 直接转交给正式目录那一份。
> 判活也不只看 HTTP 200：会比对 `/api/status` 的 `project_root`（取不到则用 `lsof` 的进程工作目录兜底）确认「端口上跑的就是本目录」，不是就明说并问你是否停掉它 —— 连「起来之后等就绪再开浏览器」这一步也验身份，不会出现窗口写着「已启动」、屏幕上是旧版本界面的情况。
> 双击后窗口里会**直接列出全部终端指令和它们的效果**（渲染自 `scripts/ctl.py`，与 `douyin help` 同源），入口丢了还会顺手补装；所以不必记命令。
> 启动之后窗口会留作**终端控制台**（不占死、也不退场）：按单个字母即可 `o` 打开 Web 控制台、`s` 检测运行状态、`r` 重启（未运行时是启动）、`q` 关闭、`l` 跟随日志、`h` 命令速查、`Enter` 退出窗口。这几个动作与 Web 控制台侧边栏底部的「重启 / 关闭」是同一套（都走 `scripts/ctl.py` → `webui._service_plan()`），会中断录制的操作先要按 `y` 确认。
> 脚本启动时会清掉继承来的 `http_proxy` / `https_proxy` / `all_proxy` —— 抖音检测与 ffmpeg 拉流必须直连，走代理会全员 `All connection attempts failed`。确需代理设 `DOUYIN_KEEP_ENV_PROXY=1`。
> 改动代码后请同步更新根目录文档，并跑 `.venv/bin/python scripts/check_docs_sync.py` 确认没有漂移；完整架构说明见 `项目框架说明书.md`。

## M4 注意事项

1. **合盖休眠**：无人值守录制时，合盖会休眠导致监控中断。运行 `caffeinate -dimsu` 常驻，或在「系统设置 → 电池」关闭自动睡眠。
2. **隔空投送/下载的隔离属性**：通过隔空投送或浏览器下载的脚本会带 `com.apple.quarantine` 隔离属性，执行前先解除：
   ```bash
   xattr -dr com.apple.quarantine .
   ```
3. **热点历史**：`hotspots.json` 记录每个主播的开播时间窗口，随包携带即可在新机器上「开箱即用」，无需重新学习主播时间表。

## 迁移到新 Mac

把当前机器打包成搬迁包，在 M4 新机上三步完成迁移：

```bash
# 当前机器：产出搬迁包
bash scripts/pack.sh
# → 生成 dist/douyin-monitor-<version>.tar.gz

# 新机器：解包 → 一键部署
tar xzf douyin-monitor-<version>.tar.gz -C ~/
cd douyin-monitor-<version>
xattr -dr com.apple.quarantine .   # 隔空投送/下载的文件先解除隔离
./setup.sh --launchd               # 建 venv + 装依赖 + 注册开机自启
bash scripts/install_cli.sh        # 装终端快捷指令（douyin / dy / dyr / dyon / dyoff）
# 打开 http://127.0.0.1:8780 完成向导（NAS 连接 / Cookie / 主播列表）
```

`pack.sh` 会自动排除 `.venv/ logs/ __pycache__/ spool/ .git/`，并把 `config.json` 脱敏为 `config.example.json`（清空 Cookie / 用户名 / 钥匙串引用，不含个人隐私），原 `config.json` 不入包。

### 数据位置速查表

| 数据 | 路径 | 说明 |
|---|---|---|
| 配置 | `config.json` | 可调参数（运行时热更新） |
| 热点历史 | `hotspots.json` | 主播开播时间窗口，随包迁移即开即用 |
| 日志 | `logs/monitor-YYYY-MM-DD.log` | 按天切分，保留 7 天 |
| 归档暂存队列 | `spool/pending.json` | 待归档录制素材（下播后自动搬运到 NAS） |
| **录制暂存目录** | `~/Movies/DouyinRecordings`（默认，可配） | 录制视频写这里，**项目文件夹只放系统文件**；`recorder.output_dir` 可改到任意盘（项目内路径会被拒绝），录制中也能改：默认下一场生效，或选「立即切换」 |
| 元数据 | `recordings/` | 只放 `history.json` 与字幕备份包（KB 级），不含视频 |

## 说明

- 检测方式 `mode`：`302` / `api` / `mix`（默认）。`mix` 下 API 失败自动回退 302。
- API 通道需在 `detection.cookie` 填入有效 Cookie（部分接口还需签名），留空则退化为 302。
- 录制器（DouyinLiveRecorder）自行处理拉流与下播停止，本脚本负责监测与触发。
- 控制台「录制历史」页的文件徽标（正常 / ✗ 缺失 / ? 无法确认）**最多滞后 5 分钟**：为了不在 NAS 上逐条 `stat`（498 条归档历史全在 SMB 上、去重 204 个目录，逐条探测实测要 165~194 秒，会把整个页面拖死），目录内容是缓存读取的。归档或清理后稍等再看，或重启服务立即刷新。**这不影响打开速度** —— 页面先拿缓存内容渲染，后台再刷新；单轮真实 SMB 探测另有 12 秒硬上限，超出的条目显示「? 无法确认」，下一轮自动补。详见《项目框架说明书》§8 第 11 条。
- 「录制历史」行上的**分辨率与码率是落盘实测值**（`history.json` 的 `media` 字段：尺寸读 FLV 头部 onMetaData，码率 = 体积÷媒体时长），**不是**配置里的目标值 —— 自动降级、源本身低清、`copy` 不重编码都会让两者对不上，要核对真实画质请看行上的胶囊。存量条目打开历史页后会自动补齐（本机每轮 ≤20 条、NAS ≤16 条）。
