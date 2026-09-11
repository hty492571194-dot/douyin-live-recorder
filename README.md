# 抖音直播监控录制系统

双通道开播检测 + 自适应轮询调度 + 内嵌 Web UI 的抖音直播监控脚本（配合 `DouyinLiveRecorder` 使用）。

对抖音多个主播自动监测开播状态：开播即触发录制命令，下播触发归档命令。检测对反爬友好（302 轻量探测 + API 确认双通道、错峰轮询、自适应间隔、熔断退避）。

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
douyin open       # 在浏览器打开 Web 控制台
douyin restart -n # 干跑：只打印将要执行的步骤，不真执行
```

- 入口 `~/.local/bin/douyin` 会先清 `PYTHONPATH` 与代理变量再执行 —— 这两个坑都曾导致「服务看着活着但检测/录制全废」。
- 服务启停的次序规则（先清端口外来实例、`bootstrap` 前先 `bootout`）与 Web 界面按钮**共用一份** `webui._service_plan()`，不会两边行为不一致。
- 命令清单只维护在 `scripts/ctl.py` 的 `COMMANDS` 表里：argparse 帮助、`douyin help`、双击 `一键启动.command` 时窗口里打印的速查表、以及 Web 控制台「系统健康 → 终端快捷指令」那张卡片，全都由它渲染，四处不会各说各话。记不住命令时也可以直接看那张卡片（`GET /api/shortcuts`），点命令格即可复制。
- 项目搬家或换机后重跑 `bash scripts/install_cli.sh` 即自动修复入口路径；`--uninstall` 可整体卸掉。

## 文件结构

```
douyin-monitor/
├── monitor.py          # 入口 + 主循环(协程守护 + 动态 reconcile)
├── schedule.py         # 热点窗口 + 自适应间隔
├── detect.py           # 双通道检测(302 + API)
├── preflight.py        # 启动自检(依赖/ffmpeg/NAS/Cookie/防休眠)
├── webui.py            # 内嵌 HTTP 服务
├── scripts/ctl.py      # 终端快捷指令实现（douyin status/start/stop/restart…）
├── scripts/install_cli.sh  # 安装/修复终端入口与 zsh 别名
├── static/index.html   # 控制台页面(含自检横幅)
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

## 锚点获取

- `sec_uid`：抖音 App → 主播主页 → 复制链接 → `v.douyin.com/xxx` 解析出 `MS4wLjAB...`（最稳定）
- `web_rid`：直播间地址 `live.douyin.com/{数字}` 中的数字（较稳定）

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
> 双击后窗口里会**直接列出全部终端指令和它们的效果**（渲染自 `scripts/ctl.py`，与 `douyin help` 同源），入口丢了还会顺手补装；所以不必记命令。
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

## 说明

- 检测方式 `mode`：`302` / `api` / `mix`（默认）。`mix` 下 API 失败自动回退 302。
- API 通道需在 `detection.cookie` 填入有效 Cookie（部分接口还需签名），留空则退化为 302。
- 录制器（DouyinLiveRecorder）自行处理拉流与下播停止，本脚本负责监测与触发。
