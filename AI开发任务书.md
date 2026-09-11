# 抖音直播监控录制系统 — AI 智能体开发任务书

> 版本 v2.0 · 2026-08-16 · 交给 AI 编码智能体执行
> 目标：在本机（macOS）完成全部开发与验证，交付"可搬迁包"，使项目在 MacBook Air M4（Apple Silicon）上经 `setup.sh` + Web UI 简单配置即可运行。

---

## 0. 给智能体的执行规则（必读）

1. **按阶段顺序执行**：阶段 0 → A → B → C → D，阶段内按任务号顺序。每完成一个任务立即验证，通过后 git commit 一次，再进入下一任务。
2. **增量可验证**：每个任务都附带「验收标准」和「验证命令」。验证不通过不得进入下一任务。
3. **不引入新依赖**：项目只允许依赖 Python 3.11+ 标准库 + `httpx`。禁止引入 Web 框架、数据库、任务队列等任何第三方库。
4. **不改变已验证的行为**：双通道检测协议逻辑（`detect.py` 的请求头、判定规则）、热点调度参数语义、API 路径保持不变，除非任务书明确要求修改。
5. **每处改动保持向后兼容**：旧的 `config.json` / `hotspots.json` 在新代码下必须能直接读取，缺失的新字段用默认值补齐（启动时 merge 默认值）。
6. **错误处理优先**：所有新增 IO / 子进程 / 网络调用必须 try/except 并写日志，任何单点异常不得导致进程退出或其它监控协程终止。
7. **文件写入一律原子**：写临时文件 + `os.replace`，参照 `schedule.py:39-46` 的现有实现。
8. 完成全部任务后，按 §7 更新 `README.md` 与本文档的验收勾选框。

---

## 1. 项目背景（上下文）

对抖音平台 4~5 路主播进行开播自动监测，开播触发录制命令（实际拉流由外部 `DouyinLiveRecorder` 承担），下播触发归档命令，素材归档到 NAS。核心矛盾：反爬严格、流地址频繁变化，需在「录得全」与「不被风控」间平衡。

架构（现有，勿推翻）：

```
Web UI (static/index.html, 原生 HTML/JS, 暗色主题)
        │ GET/POST /api/*
        ▼
内嵌 HTTP 服务 (webui.py, 标准库 ThreadingHTTPServer + State 热更新)
        ▲ 状态快照 (线程锁保护)
监控主循环 (monitor.py, asyncio)
 ├─ 检测层 detect.py:  302 探测 + room status API 确认 (mode=mix 自动回退)
 ├─ 调度层 schedule.py: 热点窗口持久化 + 自适应间隔(冷30min/热5-6min/录制中20min)
 └─ 触发层: on_live_command / on_offline_command (shell 模板, subprocess.Popen)
```

当前文件：`monitor.py`（入口+主循环）、`detect.py`、`schedule.py`、`webui.py`、`static/index.html`、`config.json`、`hotspots.json`。

运行方式：`pip install httpx && python3 monitor.py`，控制台 `http://127.0.0.1:8780`。

---

## 2. 已知缺陷清单（已人工审阅定位，直接据此修复）

| # | 位置 | 缺陷 | 严重度 |
|---|---|---|---|
| B1 | `monitor.py:153` | `asyncio.gather` 裸跑，任一协程异常导致全部监控终止且进程不重启 | 高 |
| B2 | `webui.py:26-33` | `update_config` 无类型/范围校验，前端提交字符串型数值即可让 `schedule.py:96-105` 的 `float()` 抛异常，叠加 B1 = 全部崩溃 | 高 |
| B3 | `webui.py:35-40` | config.json 非原子写，写一半崩溃后 JSON 截断，重启 `load_config` 直接抛异常起不来 | 高 |
| B4 | `index.html:334-354` + `monitor.py:149-153` | 前端已有主播增删 UI（POST monitors），但后端协程在启动时一次性创建，增删**静默不生效**，必须重启 | 高（现有功能失效） |
| B5 | `schedule.py:58` | 热点合并 `(w["center"] + minute) // 2` 跨零点错误：23:50(1430) 与 00:10(10) 合并出 720（中午），窗口错乱 | 中 |
| B6 | `monitor.py:59` | `Popen(shell=True)` fire-and-forget，stdout/stderr 全丢弃，录制器启动失败不可见 | 中 |
| B7 | `monitor.py:46` | 日志仅 print，无落盘、无轮转 | 中 |
| B8 | `webui.py` | `webui.host/port` 可被热更新保存但不生效（服务器只启动一次），造成"已改"假象 | 低 |
| B9 | `detect.py:97` | cookie 为空时 API 通道休眠，mix 退化为纯 302（设计如此，但用户无感知，需在 UI 提示） | 低 |

---

## 3. 硬性约束

- **技术栈**：Python 3.11+（标准库 + httpx）、原生 HTML/JS 前端、JSON 持久化。禁止 Docker、禁止数据库、禁止 Node 构建链。
- **目标平台**：macOS 26 + Apple Silicon（M4）。子进程命令只用 macOS 自带工具（`mount_smbfs`、`umount`、`diskutil`、`security`、`caffeinate`、`launchctl`）。
- **代码风格**：与现有代码一致——中文 docstring/日志、单文件模块、无类框架化过度设计。
- **安全**：密码不得明文写入 config.json（走钥匙串，见 C4）；`subprocess` 调用用户配置的模板时维持现有 shell=True 语义，但新增代码中的外部命令一律用列表参数。

---

## 4. 阶段 0：版本控制基线

**任务 0.1** 在项目根目录 `git init`，创建 `.gitignore`（内容：`__pycache__/`、`.venv/`、`logs/`、`*.pyc`、`.DS_Store`），提交当前全部代码为基线 commit（message: `baseline: v1.0 现状`）。

验收：`git log --oneline` 有基线提交；`git status` 干净。

---

## 5. 阶段 A：稳定性加固

### A1 协程守护与自动重启（修复 B1）
- `monitor.py`：将 `run_streamer` 的调用包进 `supervise(mon)` 包装协程：内部 `while True: try: await run_streamer(...) except Exception: log 并按 1min→2min→4min→8min（上限 10min）指数退避后重启该主播协程`；退避中通过 State 上报该主播状态 phase="异常重启"。
- `main()` 中 gather 改为 `asyncio.gather(*[supervise(m) for m in ...], return_exceptions=False)`，并在外层再包一个顶层异常兜底（log 后 re-raise，交给 launchd 拉起）。

验收：临时在某协程内 `raise RuntimeError` 注入，其它主播监控不受影响，异常协程 1 分钟内自动重启并恢复正常状态。

### A2 配置校验 + monitors 动态生效（修复 B2、B4）
- `webui.py`：`update_config` 增加校验：
  - `schedule` 数值字段：必须是 number（拒绝字符串）、> 0，interval 类字段下限 30s，`hot_min <= hot_max`，`hotspot_max ∈ [1,5]`，`hotspot_decay_days >= 1`；
  - `detection.mode ∈ {302, api, mix}`；
  - 校验失败返回 400 + 具体字段错误信息（前端 toast 已能显示 `error`）。
- `webui.host/port` 变更时接受保存，但响应体加 `"restart_required": true`（前端可暂不处理，仅日志提示）。
- monitors 动态生效：`State` 增加 `monitors_version` 计数器，`update_config` 检测到 `monitors` 键变化时 +1 并保存；`monitor.py` 增加一个 `reconcile` 协程，每 5s 对比当前运行的任务集合（以 anchor+name 为 key）与配置中的 monitors：新增的启动 `supervise`，删除的优雅取消（`task.cancel()`，并触发一次 on_offline_command 兜底若在直播中）。

验收：运行中通过 UI 添加主播 → ≤10s 内 `/api/status` 出现该主播状态；删除主播 → 协程取消、状态消失；POST `{"schedule":{"cold_interval":"1800"}}`（字符串）→ 返回 400 且配置不变。

### A3 config 原子写（修复 B3）
- `webui.py` 的 `_save` 与 `monitor.py` 的 `load_config` 首次写入均改为 tmp + `os.replace`；`load_config` 读到损坏 JSON 时：备份为 `config.json.corrupt-<时间戳>`、用默认值重建、日志告警（而非崩溃）。

验收：手工把 config.json 截断成半截 JSON，启动进程 → 自动恢复默认配置且生成备份文件。

### A4 热点窗口跨零点修复（修复 B5）+ 自测
- `schedule.py` 合并逻辑：先把 `minute` 按环形距离解到 `w["center"]` 同侧（`if minute - w["center"] > 720: minute -= 1440`；`if w["center"] - minute > 720: minute += 1440`），平均后再 mod 1440。`in_hotspot` 已用环形距离，无需改。
- 新增 `tests/test_schedule.py`（纯标准库 `unittest`，可用 `python3 -m unittest tests.test_schedule` 运行）：用例覆盖 ① 23:50 与 00:10 合并后中心仍在两者附近（如 0 或 1439±1）② 普通 20:00/20:30 合并 ③ 上限 2 个淘汰最旧 ④ decay 淘汰。

验收：全部用例通过。

### A5 命令执行可观测（修复 B6）
- `monitor.py` `run_command`：Popen 后把 `(pid, name, cmd)` 记入内存 active 列表；每次主循环轮询 `poll()`，退出码非 0 时 log 告警 `命令异常退出 code=X`；同时保留 DEVNULL（避免管道阻塞）。active 列表加入 `/api/status` 的 `commands` 字段（webui.py 顺带暴露）。

验收：把 on_live_command 配成 `exit 3`，触发后日志出现退出码告警。

### A6 日志落盘（修复 B7）
- `monitor.py`：log 函数改为同时 print 与追加写 `logs/monitor-YYYY-MM-DD.log`；跨天自动切文件；启动时清理 7 天前的日志文件。写失败静默降级为仅 stdout。

验收：运行后 `logs/` 出现当日日志；伪造一个 8 天前的日志文件重启后被清理。

**阶段 A 验收门（全部满足才 commit 阶段标签 `git tag phase-a`）**：
1. `python3 -m unittest discover tests` 通过；
2. `python3 -m py_compile monitor.py detect.py schedule.py webui.py` 通过；
3. 启动进程，注入坏配置/异常，进程存活、UI 正常响应 /api/status；
4. 本机挂机 24h 无人工干预（挂机留给用户执行，智能体先完成 1-3）。

---

## 6. 阶段 B：开箱即用

### B1 {BASE} 占位符 + 自动探测
- `monitor.py` `run_command` 的 ctx 增加 `"BASE": BASE`（项目根目录），模板即可写 `python3 {BASE}/recorder/recorder.py ...`。
- 新增 `preflight.py`（见 B2）内提供 `which()` 探测：`ffmpeg`、`python3` 存在性写入 preflight 结果，供前端展示；`DEFAULT_CONFIG` 的命令模板保持 echo 示例不变（不猜测用户录制器路径）。

### B2 preflight 启动自检 + API
- 新增 `preflight.py`：函数 `run_checks(config) -> list[dict]`，每项 `{id, title, status: ok|warn|fail, detail, action}`：
  1. `httpx` 可导入（fail 时给出 `pip install httpx` 提示）；
  2. `on_live_command`/`on_offline_command` 中若含绝对路径引用（正则找 `/Users/` 或 `/home/`），提示改用 `{BASE}`（warn）；
  3. `ffmpeg` 是否在 PATH（warn，录制器依赖）；
  4. NAS 是否已配置（读 `config.nas.enabled`，未配置 → warn 引导到 NAS 面板）；
  5. `detection.cookie` 是否为空（warn：API 通道休眠，mix 退化为 302，修复 B9 的无感知问题）；
  6. 防休眠：检测进程父链中是否有 caffeinate 或 `pmset -g` 显示系统不会睡眠（warn + 给出 `caffeinate -dimsu` 建议命令）。
- `webui.py` 新增 `GET /api/preflight` 返回结果；`monitor.py` 启动时也执行一次并打印摘要。

### B3 前端配置横幅（复用现有组件体系）
- `static/index.html`：`<main>` 顶部插入 `#setup-banner` 卡片（复用 `.card` 样式，warn 黄 / fail 红左边框）：轮询 `/api/preflight`（复用现有 3s 状态轮询节奏即可），列出非 ok 项的 title+detail；fail 项置顶。有 fail 项时横幅常驻，全部 ok 时隐藏。点击某项可 `showView` 跳转对应设置页（如 cookie → advanced 视图）。
- 不引入新框架，沿用现有原生 JS 写法与 CSS 变量。

### B4 setup.sh + requirements.txt
- `requirements.txt`：仅 `httpx`。
- `setup.sh`（bash，`chmod +x`）：
  1. 检测 python3 ≥ 3.11；
  2. 创建 `.venv` 并 `pip install -r requirements.txt`；
  3. 生成默认 config.json（若不存在，直接运行 `.venv/bin/python -c "import monitor; monitor.load_config()"` 或等价逻辑）；
  4. `--launchd` 选项：生成 `~/Library/LaunchAgents/com.douyin.monitor.plist`（ProgramArguments 指向 `.venv/bin/python monitor.py`，WorkingDirectory 项目根，KeepAlive=true，StandardOutPath/ErrPath 指向 logs/，ProcessType=Background），`launchctl load` 并提示；
  5. 结束打印「下一步：打开 http://127.0.0.1:8780 完成配置向导」。
- `README.md` 增加「新机部署」一节：`./setup.sh --launchd` 两条路径（带/不带开机自启）。

**阶段 B 验收门（`git tag phase-b`）**：
1. 把项目 `rsync` 到临时目录（排除 .venv/logs/__pycache__），模拟新用户：`./setup.sh` → 启动成功；
2. Web UI 顶部横幅正确列出「NAS 未配置 / Cookie 为空」等警告，全部修复后横幅消失；
3. 命令模板使用 `{BASE}` 后在临时目录中能正确解析执行。

---

## 7. 阶段 C：NAS 模块

### C1 挂载管理 `nas.py`
- 新模块 `nas.py`，config.json 新增（默认值，向后兼容由 merge 补齐）：
  ```json
  "nas": { "enabled": false, "protocol": "smb", "host": "", "port": 445,
           "share": "", "username": "", "password_ref": "keychain:douyin-nas",
           "mount_point": "/Volumes/douyin-archive", "root_dir": "录播" }
  ```
- `ensure_mount() -> bool`：已挂载且可写（在挂载点写删探针文件）→ True；`mount` 输出中已有该挂载点但探针失败（僵尸挂载）→ `umount -f` 后重挂；未挂载 → `mkdir -p` 挂载点 + `mount_smbfs //user:pass@host/share <mount_point>`（列表参数，超时 15s，密码经钥匙串读取，不落日志）。
- 凭据读取 `get_password()`：`security find-generic-password -s douyin-nas -w`，失败返回 ""（并置 warn 状态）。
- 健康协程 `nas_watch()`（并入 monitor 主循环，60s 周期）：调用 ensure_mount，失败后指数退避（1min→2min→…上限 10min，成功后复位），退避不阻塞归档队列检查；状态写入 State 供 API 读取。
- 首次挂载成功后按 `config.monitors` 初始化 `<mount_point>/<root_dir>/<主播名>/` 目录树。

### C2 归档流水线（暂存队列）
- `spool/` 目录 + `spool/pending.json`（原子写）记录待归档条目：`{id, streamer, src_path, added_at, retries}`。
- 下播事件处理变更（`monitor.py`）：若 `nas.enabled`，将录制输出目录（新增配置 `recorder.output_dir`，默认 `{BASE}/recordings`，下播 ctx 增加该值）入队，由 `nas_watch` 协程在挂载健康时搬运：`mv` 到 NAS 主播目录 + 写 `<文件名>.meta.json`（streamer/room_id/start/end）；移动失败 retries+1，≥5 次标 failed 并告警。
- 保留 `on_offline_command` 作为逃生通道（内置归档与命令模板都执行）。
- 进程启动时加载 pending.json，自动消化历史队列。

### C3 API + 前端 NAS 面板
- `webui.py` 新增：`GET /api/nas`（配置+当前状态：挂载/退避/队列深度/最近归档）、`POST /api/nas`（更新配置，校验 host/share 非空、port 1-65535）、`POST /api/nas/test`（用**提交的参数**临时探测：临时目录挂载 → 写探针 → 读回 → 卸载，返回 `{ok, latency_ms, error}`，不落盘凭据）。
- 前端：侧边栏新增「NAS 归档」入口（nav-item + 新 view），复用 `.card/.field/.btn/.toast`：
  - 连接信息表单（协议/host/port/share/用户名/密码/挂载点/root_dir）+「测试连接」按钮（显示 ok/延迟或错误）+「保存并启用」；
  - 状态卡：挂载状态、队列深度、最近归档列表；
  - 保存时密码不回显，POST 后端写入钥匙串（`security add-generic-password -U -s douyin-nas -w <密码>`），config 只存 password_ref。

### C4 钥匙串集成
- `security add/find-generic-password` 封装进 `nas.py`；`security` 调用一律列表参数；失败（如无钥匙串环境/CI）降级为内存密码并 warn——保证非交互环境下功能可测。

**阶段 C 验收门（`git tag phase-c`）**：
1. 无 NAS 环境下：`/api/nas/test` 对不存在主机返回明确错误（超时受控 ≤15s）；
2. 模拟 NAS：本机另开一个 SMB 共享或用 `/tmp` 挂载替身（允许测试桩：`nas.py` 预留 `dry_run_mount` 钩子供单测注入）跑通 挂载→归档→队列→恢复 流程；单测覆盖：入队→搬运成功→队列清空；入队→搬运失败→retries 递增→≥5 标 failed；
3. 拔网线式模拟（test 钩子注入失败）：录制进行中归档失败不影响监控协程；恢复后自动重挂并消化队列；
4. `python3 -m unittest discover tests` 全绿。

---

## 8. 阶段 D：迁移交付物

1. `scripts/pack.sh`：`git archive -o douyin-monitor-<version>.tar.gz HEAD`（或 rsync 排除 .venv/logs/__pycache__/spool），产出搬迁包；
2. `README.md` 更新：M4 部署三步（解包 → `./setup.sh --launchd` → Web UI 向导填 NAS/Cookie/主播）；「M4 注意事项」：合盖休眠策略（caffeinate 或系统设置）、首次传输脚本 `xattr -d com.apple.quarantine`、热点历史 hotspots.json 随包携带说明；
3. `开发项目书.md` 勾选本文档 §9 验收框。

**阶段 D 验收门**：在另一台 Mac/另一目录解包演练，从零到监控运行 ≤15 分钟，Web UI 向导引导完成全部配置。

---

## 9. 总验收清单（交付时逐项勾选）

- [x] 单协程异常不影响其它主播，1 分钟内自愈重启
- [x] 坏配置 POST 返回 400，进程不崩
- [x] config.json 损坏可自愈恢复默认并备份
- [x] UI 增删主播 ≤10s 动态生效
- [x] 跨零点热点合并正确（单测覆盖）
- [x] 命令退出码告警入日志；日志按天落盘、保留 7 天
- [x] {BASE} 占位符可用；preflight 横幅引导新用户
- [x] setup.sh 一键部署（含 --launchd）
- [x] NAS：测试连接 / 自动挂载 / 健康重连 / 归档队列 / 钥匙串凭据
- [x] 搬迁包在新目录/新机器 15 分钟内可用
- [x] `python3 -m unittest discover tests` 全绿
- [x] git 标签 phase-a / phase-b / phase-c / phase-d 齐全

> 注：本机 24h 挂机稳定性验证（阶段 A 验收门第 4 条）留给用户执行。

## 10. 验证命令速查

```bash
python3 -m py_compile monitor.py detect.py schedule.py webui.py nas.py preflight.py
python3 -m unittest discover tests
python3 monitor.py                      # 冒烟：启动后另开终端 curl
curl -s http://127.0.0.1:8780/api/preflight | python3 -m json.tool
curl -s http://127.0.0.1:8780/api/status | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8780/api/config -d '{"schedule":{"cold_interval":"x"}}'  # 应 400
./setup.sh && .venv/bin/python monitor.py
```
