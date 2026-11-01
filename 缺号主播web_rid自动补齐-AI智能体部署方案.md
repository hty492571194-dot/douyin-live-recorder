# 缺号主播 web_rid 自动补齐 — AI 智能体部署方案

> 版本 v1.0 · 2026-10-26 · 交给 AI 编码智能体执行
> 上游设计文档：`缺号主播web_rid自动补齐-实施方案A.md`（讲"为什么这么做"）；本文件讲"**怎么做、什么顺序、怎么验**"。
> 两者冲突时**以本文件为准**；本文件没写的细节回上游文档找。

---

## 0. 执行规则（必读，违反即返工）

1. **顺序执行** P1 → P2 → P3 → P4，阶段内按任务号顺序。每个任务自带「验收标准」和「验证命令」，不通过不得进入下一任务；每任务一个 commit，message 前缀 `R1:` / `R2:` …（**不要 push**，本项目按月同步）。
2. **环境红线**：
   - **不要重启服务**。本环境 launchd 不可用，`douyin restart` 只能由用户在终端执行；在这里重启会把服务打掉。
   - **8780 上的线上实例一律不许碰**（不 restart、不 kill、不写它的 config）。
   - 端到端验证只能起**独立端口**的临时实例，用完即关。
3. **不引新依赖**：`playwright>=1.55` 已在 `requirements.txt`，chromium 已装（`~/Library/Caches/ms-playwright/chromium-1234`）。禁止为此新增任何库。
4. **禁改区**：`detect.py` 的请求头与判定语义、`schedule.py` 的现有参数语义、`contract/`（asrclip 契约）、`auth.py` 的浏览器实例与 `user-data-dir`。补号链路必须与它们**零耦合**。
5. **异常不得逃逸**：所有新增的 IO / 网络 / 子进程调用必须 `try/except` 并落日志；任何单点异常不得终止主循环或其他协程。
6. **写盘一律原子**（复用 `monitor._atomic_write_json`）；**写 `config.json` 前必须重新读盘**，禁止用内存快照回写（见 §4.3 与 §7.4）。
7. **敏感词门**：代码注释、文档、测试夹具一律用合成值（`主播甲` / `12345678` / `MS4wLjABAAAA…fixture`）。**真实主播名、真实抖音号、真实 sec_uid 禁入仓库** —— 一旦进入，月度 `scripts/sync_github.py` 会直接中止。
8. **每阶段收尾三件套**：`scripts/check_docs_sync.py` 通过 + 全量 `unittest discover -s tests` 全绿（基线 1133 项）+ 相关文档同步。
9. **不做任务书以外的顺手优化**。发现别的问题记到交付说明，不改。

---

## 1. 背景与已确认事实（不要重复调研，直接采信）

### 1.1 要解决什么

添加主播时 `name` + `anchor`(sec_uid) 必填，`web_rid` 决定能否检测开播。抖音号 / 主页链接只能给出 `sec_uid` + 昵称 + 抖音号（`link_resolver.py` 已实现），**拿不到 `web_rid`**。本任务给系统加一条后台能力：**主播开播后自动把 `web_rid` 补进 config**，然后回到现有检测链路。

### 1.2 2026-10-26 实测结论（实现依据）

| 事实 | 数据 |
|---|---|
| headless Chromium 未登录打开 `https://www.douyin.com/user/<sec_uid>`，页面「直播中」入口 href 即 `live.douyin.com/<web_rid>` | 在播主播实测抓到；用 `live.douyin.com/<rid>`（**移动 UA**）302 落地 `sec_user_id` 比对确认归属 |
| 单次判定耗时 | 在播 1.3s / 1.4s；离线 5.0s / 5.2s |
| 离线主播页面特征 | 无「直播中」、DOM 无任何 `live.douyin.com/<数字>`、profile XHR `room_id_str="0"` |
| profile 接口字段 | `live_status` / `room_id` / `room_data.status` 有；**`web_rid` 为 null** → 纯 HTTP 方案补不出 web_rid |
| `live.douyin.com/<rid>` 的 302 | **只有移动 UA 才 302**（桌面 UA 返回 200 SPA） |
| `live.douyin.com/<sec_uid>` | 即使在播也返回 200，**不能当探测锚点** |

### 1.3 可复用的现有资产

| 能力 | 位置 |
|---|---|
| 原子写 JSON | `monitor._atomic_write_json(path, data)` |
| 读配置（含默认值深合并） | `monitor.load_config()` |
| 状态类 / 前端轮询 | `webui.State`、`/api/status` |
| 健康心跳登记 | `health.SPECS`（现有 21 项）、`health.beat(key)` |
| 通知（企微/本机） | `notify.push_by_config(...)`，失败不抛异常 |
| 立即复检某主播 | `state.request_refresh(name)` |
| 抖音号/链接解析 | `link_resolver.py`（与本任务无关，互不影响） |

---

## 2. 代码契约（先落地签名，再填实现；签名不许改）

### 2.1 新文件 `web_rid_probe.py` —— 纯逻辑

**硬约束：不 import playwright、不联网、不做文件 IO。** 这样它能在单测里被穷举，也让 1133 项基线测试不被浏览器拖慢。

```python
TIERS_DEFAULT   = ((40, 50), (80, 90), (110, 120))   # 分钟，闭区间
WEIGHTS_DEFAULT = (0.5, 0.3, 0.2)                    # 与 T0/T1/T2 对应，均值≈71min

def normalize_cfg(cfg: dict) -> dict:
    """校验 interval_tiers_min / tier_weights；非法(空、倒置、重叠、权重≤0 或全 0)
    回落默认并在返回值里带 normalized=False + reason='config_invalid'，绝不抛异常。"""

def pending_monitors(cfg: dict) -> list[dict]:
    """返回 web_rid 为空的主播列表（保持 config 中顺序）。"""

def should_run(cfg, state, now) -> tuple[bool, str]:
    """能否开跑。原因码：ok / disabled / no_pending / window_expired /
    cooling / daily_cap / backoff(全部主播都在退避)"""

def plan_next_run(cfg, state, now, rng=None) -> dict:
    """两段随机。返回 {tier, minutes, interval_sec, next_run_at, normalized}
    · 第一段：按权重选档（avoid_same_tier_twice 时排除 state['last_tier']）
    · 第二段：档内 randint(lo, hi)
    · 与上一轮同一分钟 → 档内平移一格，端点回绕
    · second_jitter 时叠加 randint(0,59) 秒
    · next_run_at = now(本轮结束时刻) + interval_sec"""

def decide_verdict(signals: dict) -> tuple[str, str | None, str | None]:
    """signals 由浏览器层填：{dom_rids: list[str], profile_room_id_str: str|None,
    http_status: int, page_bytes: int, saw_login_wall: bool, saw_captcha: bool,
    xhr_status: int|None, nav_error: str|None}
    → (verdict, rid, reason)；verdict ∈ live/offline/unknown"""

def merge_web_rid(cfg_now, name, anchor, rid, now) -> tuple[dict | None, str]:
    """只写 web_rid / web_rid_source='auto' / web_rid_filled_at。
    返回 (新 cfg 或 None, 结果码)：ok / not_found / already_has_rid / rid_conflict"""

class Circuit:
    """三层熔断状态机。offline 视为成功→清零；unknown 才计数。"""
    def record(self, name, verdict, reason, now) -> dict   # 返回本轮产生的动作
    def blocked(self, name, now) -> int | None             # 该主播冷却到期时间
    def global_blocked(self, now) -> int | None
    def reset_day(self, day) -> None

def task_batch(cfg, state, limit: int) -> list[dict]:
    """按 cursor 轮转取本轮任务，跳过退避中的主播。"""
```

### 2.2 新文件 `web_rid_browser.py` —— 唯一 import playwright 的地方

```python
def fetch_web_rid(sec_uid: str, *, timeout_sec: int = 45,
                  profile_dir: str = "", user_data_dir: str = "") -> dict:
    """同步函数（由 asyncio.to_thread 调用）。
    返回 {"verdict","rid","reason","detail","elapsed_ms"}；
    任何异常都必须被吞掉并转成 {"verdict":"unknown","reason":"browser_crash"}。"""
```

实现要求（逐条对应反爬设计）：

| 要求 | 做法 |
|---|---|
| 持久化身份 | `launch_persistent_context(user_data_dir=state/browser/rid, ...)`，与 `auth.py` 完全隔离 |
| 固定指纹 | 真实 Chrome UA（与内核版本匹配）+ `locale=zh-CN` + `timezone_id=Asia/Shanghai` + `viewport 1440×900` |
| 抹自动化痕迹 | args 加 `--disable-blink-features=AutomationControlled`、`--no-proxy-server`；add_init_script 覆写 `navigator.webdriver`、补 `window.chrome`、`navigator.languages` |
| 只等判定 | `goto(..., wait_until="domcontentloaded")` + 轮询 DOM/拦截 profile XHR，**判定即返回**，不滚动、不点击 |
| 资源瘦身 | `route` 阻断 `image/media/font`，**放行 document/script/xhr/fetch** |
| 不碰登录态 | 绝不注入 `detection.cookie`；不读写 `auth.py` 的目录 |
| 必定收工 | `try/finally` 关闭 page 与 context；总耗时受 `timeout_sec` 约束 |

### 2.3 `monitor.py` 新增

```python
async def rid_backfill_loop(cfg_getter, state, stop_event): ...
    # 常驻协程：等 next_run_at → should_run 判定 → task_batch → 逐个
    #   结果 = await asyncio.to_thread(web_rid_browser.fetch_web_rid, sec_uid, ...)
    # 严禁直接调 sync_playwright（会阻塞事件循环，把检测/录制协程一起卡死）
```

`health.SPECS` 新增第 22 项 `ridbackfill`：`stale_sec = 180*60`（按最长档 120min×1.5，**不要按最短档**，否则长档间隔会被误报成卡死）。

### 2.4 `webui.py`：`/api/status` 新增段

```jsonc
"rid_backfill": {
  "enabled": "auto",            // 配置原值
  "running": true,              // 窗口是否有效
  "reason": "ok",               // should_run 的原因码，便于解释"为什么没跑"
  "window_until": 1793567271,   // null 表示未开窗
  "pending": ["主播甲"],         // 缺 web_rid 的主播名
  "checks_today": 12, "max_checks_per_day": 100,
  "last_run_at": 1792962471, "next_run_at": 1792965800,
  "last_result": {"name": "主播甲", "verdict": "offline", "reason": null,
                  "at": 1792962471},
  "circuit": {"global_until": 0, "reason": "", "cooldown_today": 0}
}
```

---

## 3. 任务分解（每个任务都有验收与命令）

### P1 纯逻辑 + 单测（不联网、不起浏览器）

| 任务 | 内容 | 验收标准 | 验证命令 |
|---|---|---|---|
| **P1-1** | `normalize_cfg` / `pending_monitors` / `should_run` | 六种原因码全覆盖；非法配置回落默认且 `normalized=False`；空 monitors 时 `no_pending` | `python -m unittest tests.test_web_rid_probe -v` |
| **P1-2** | `plan_next_run` 两段随机 | 抽样 1000 次：三档都出现、档内取值不越界、权重偏差 <±3pp；`avoid_same_tier_twice` 无连续同档；相邻两轮分钟值不同；`next_run_at` 秒数非 0；`next_run_at` = 传入 `now` + interval | 同上 |
| **P1-3** | `decide_verdict` + 原因码映射 | 有 rid→`live`；`profile_room_id_str=="0"` 且无 rid→`offline`；`captcha/login_wall/http_429/http_403/xhr_blocked/empty_shell/nav_error/timeout`→对应原因码的 `unknown`；**`offline` 不算失败** | 同上 |
| **P1-4** | `merge_web_rid` | 只改 3 个字段，其余逐字节不变；`already_has_rid` 不覆盖；`not_found`（主播已删/改名）返回 None | 同上 |
| **P1-5** | `Circuit` 三层熔断 | **`offline` 连打 10 轮不熔断**（最关键）；unknown 连 3 轮触发单主播 6h 退避且第 4 轮零访问；风控类触发全局冷却、纯网络类不触发；日级 >50% 停到次日 | 同上 |

### P2 抓取器 + 命令行（真机验证）

| 任务 | 内容 | 验收标准 | 验证命令 |
|---|---|---|---|
| **P2-1** | `web_rid_browser.fetch_web_rid` | 对一位**在播**主播返回 `live` + 10–11 位 rid，耗时 <10s；对一位**离线**主播返回 `offline`、`rid=None`；连续跑 3 次不泄漏进程（`pgrep -f headless_shell` 归零） | `.venv/bin/python scripts/probe_web_rid.py <sec_uid>` |
| **P2-2** | rid 归属校验（移动 UA 302 → `sec_user_id` 比对） | 校验通过才 `live`，不一致→`unknown` + `rid_mismatch`；桌面 UA 不被误用（这条要写进注释，防止后人"优化"成桌面 UA） | 同上（故意传错 rid 应得 `rid_mismatch`） |

### P3 接入服务

| 任务 | 内容 | 验收标准 | 验证命令 |
|---|---|---|---|
| **P3-1** | `state/rid_backfill.json` 读写（原子） | 文件缺失时用默认空状态；结构见 §4.2；写入用 `os.replace`；损坏 JSON 自动重置并记日志 | 单测 + 手工删文件跑一次 |
| **P3-2** | `rid_backfill_loop` 接入 `monitor` + health 第 22 项 | 主循环心跳不被拉长（巡检期间 `main` 心跳间隔仍 < 正常值 ×1.2）；`/api/status` 出现 `rid_backfill` 段；`health` 快照里能看到 `ridbackfill` | 临时实例 + `curl /api/status`、`curl /api/health` |
| **P3-3** | 失败日志 + 审计文件 + 日报一行 | 每次失败一行业务日志（格式见 §4.4）；`state/rid_backfill_fails.jsonl` 上限 500 行；日报追加 `补号巡检：今日 N 次 / 补到 M 位 / unknown K 次（按原因码）/ 熔断 X 次` | grep 日志 + 触发一次日报函数 |

### P4 前端 + 文档 + 端到端

| 任务 | 内容 | 验收标准 | 验证命令 |
|---|---|---|---|
| **P4-1** | 前端展示 + 高级参数组 | 「缺web_rid」徽标 tooltip 显示巡检状态；详情区显示窗口剩余天数/上次/下次；高级参数组含三档区间、权重、日上限，改完即时生效；**折叠只切 `.fg.open` 不摘 DOM**（否则保存会静默清空配置） | Playwright 截图 + 改参数后 `curl /api/config` 核对 |
| **P4-2** | 文档 5 处 + 全量测试 + 端到端 | `项目框架说明书.md`（模块 + 行数 + 接口 + 符号行号）、`PROJECT_GUIDE.md`、`AI_AGENT_GUIDE.md`、`README.md`、`主播开播状态监控.md` 全部同步；`check_docs_sync` 绿；全量测试绿；临时实例端到端通过 | `scripts/check_docs_sync.py` + `unittest discover -s tests` |

---

## 4. 数据格式（照抄，别自创）

### 4.1 `config.json` 新增段（默认值）

```jsonc
"rid_backfill": {
  "enabled": "auto",                        // "auto" | true | false
  "window_days": 7,
  "interval_tiers_min": [[40, 50], [80, 90], [110, 120]],
  "tier_weights": [0.5, 0.3, 0.2],
  "avoid_same_tier_twice": true,
  "second_jitter": true,
  "max_per_round": 3,
  "gap_sec_min": 25,
  "gap_sec_max": 45,
  "timeout_sec": 45,
  "round_budget_sec": 180,
  "max_checks_per_day": 100,
  "quiet_hours": []
}
```

### 4.2 `state/rid_backfill.json`

```jsonc
{
  "window_start": 0, "window_until": 0,
  "last_run_at": 0, "next_run_at": 0,
  "last_tier": null, "last_interval_sec": 0,
  "cursor": 0,
  "day": "2026-10-26", "checks_today": 0,
  "per_monitor": {"主播甲": {"last_check": 0, "verdict": "offline", "rid": null,
                            "fails": 0, "last_fail_at": 0, "last_reason": "",
                            "backoff_until": 0, "rid_blacklist": []}},
  "circuit": {"global_until": 0, "reason": "", "cooldown_today": 0,
              "unknown_today": 0, "checks_today_circuit": 0}
}
```

### 4.3 写 `config.json` 的铁律

1. 重新 `monitor.load_config()` 读盘（**不用内存快照**）；
2. 按 `name` + `anchor` 双匹配定位；
3. 已有 `web_rid` → 不写；
4. 只写 `web_rid` / `web_rid_source` / `web_rid_filled_at`；
5. 写前比对读取时的 mtime+size，不一致 → 重读重试 1 次，仍不一致 → **放弃本轮**；
6. `_atomic_write_json` 落盘 → `state.request_refresh(name)` → 日志。

### 4.4 日志与审计格式

```
[rid-backfill] FAIL 2026-10-26 15:20:31 name=主播甲 anchor=MS4w…fixture reason=xhr_blocked detail="profile XHR status=412" fails=2/3 backoff=none next=2026-10-26 16:05:12
[rid-backfill] OK   2026-10-26 15:20:29 name=主播乙 anchor=MS4w…fixture verdict=live rid=388770149407 elapsed=1.3s
[rid-backfill] SKIP 2026-10-26 15:20:31 reason=cooling until=2026-10-26 18:20:31
```

`state/rid_backfill_fails.jsonl`（一行一条，最多 500 行）：

```json
{"ts": 1792962471, "name": "主播甲", "anchor_tail": "…fixture", "reason": "xhr_blocked", "detail": "profile XHR status=412", "fails": 2, "round": 17}
```

---

## 5. 端到端验收（独立端口，绝不碰 8780）

1. 拷一份 config 到临时目录，端口改 `8799`，`monitors` 只留两条**测试用**记录（一条在播、一条离线，可用合成 sec_uid + 真实 sec_uid 但**不入库**）。
2. `PYTHONPATH=` 清空、清掉全部 `*_proxy` 环境变量后起临时实例。
3. 断言：
   - `/api/status.rid_backfill.running == true`、`pending` 长度正确；
   - 等在播那条被补上：`config.json` 出现 `web_rid`，且 `web_rid_source == "auto"`；
   - 下一轮检测该主播 `is_live == true`；
   - 离线那条 config 完全未变（`md5` 前后一致）；
   - 强制把 `should_run` 打到 `daily_cap`，确认不再开浏览器（`pgrep` 无新进程）；
   - 冲突演练：补写前手工改一次 config（改别人的 `note`），断言本轮放弃且不覆盖。
4. 关掉临时实例；确认 8780 上的线上实例**始终 200**（`curl --noproxy '*'`）。

---

## 6. 交付清单

- [ ] `web_rid_probe.py` + `tests/test_web_rid_probe.py`
- [ ] `web_rid_browser.py` + `scripts/probe_web_rid.py`
- [ ] `monitor.py`（`rid_backfill_loop` + 默认配置段）、`health.py`（第 22 项）
- [ ] `webui.py`（`rid_backfill` 状态下发 + 高级参数）、`static/index.html`（徽标/详情/参数组）
- [ ] `state/rid_backfill.json` 读写 + `state/rid_backfill_fails.jsonl` 审计
- [ ] 日报追加一行（复用 `daily_report_loop`）
- [ ] 5 份文档同步 + `check_docs_sync` 绿 + 全量测试绿
- [ ] 交付说明：端到端截图/日志、真实耗时数据、`unknown` 原因分布、回滚方法

---

## 7. 已知坑（都是本项目踩过的，别重踩）

1. **`sync_playwright` 阻塞事件循环**：直接调会让检测/录制协程一起停摆。一律 `asyncio.to_thread`，并在测试里断言主心跳没被拉长。
2. **移动 UA 才 302**：`live.douyin.com/<rid>` 用桌面 UA 返回 200 SPA，归属校验会静默失效。
3. **代理变量**：启动前必须清 `*_proxy`，chromium 也要 `--no-proxy-server`；项目曾因代理静默全停两天半。
4. **`monitors` 整体替换**：后台写盘必须"重读→比对 mtime→只改字段"，冲突就放弃。历史上丢过 9 位主播。
5. **health 不登记 = 看不见**：新协程必须 `health.beat()` + `SPECS` 登记，否则健康面板全绿而实际已死。
6. **`check_docs_sync` 会查三样东西**：说明书里登记的所有顶层 `.py`、`xxx.py (N 行)` 的真实行数、`符号(L123)` 的真实定义行号。改完代码不更新会直接报红。
7. **前端 5s 重绘**：跨秒异步状态必须存模块变量，别塞进会被重绘的 DOM 节点。
8. **录制中禁止重启**：重启前必须查 `/api/status` 的 `recordings` 为空。
9. **敏感词门**：`.py` / `.md` 都扫；真实主播名/抖音号会让月度 GitHub 同步直接中止。
10. **别把补号失败混进主播状态**：`unknown` 只影响 `rid_backfill` 那一段，不改主播的「未监测/在播」徽标。

---

## 8. 回滚与降级

| 场景 | 动作 |
|---|---|
| 想立刻停 | `rid_backfill.enabled=false`（改配置即生效，不动代码） |
| 想重置窗口/熔断 | 删 `state/rid_backfill.json` |
| P2 真机验证发现抖音改版（DOM 不再有 rid） | **停在 P2**，不接协程，保留 CLI 等修；主功能不受影响 |
| playwright/chromium 被误删 | 巡检自动降级为"不可用"：`should_run → disabled(reason=no_browser)`，卡片提示"缺浏览器组件" |
| 触发验证页 | 三层熔断接管 → 冷却期零访问 → 到期自动恢复；必要时改 `enabled=false` 人工介入 |

---

## 9. 验收口径（DoD）

1. 缺号主播开播后，**最迟 120 分钟内**（最长档 + 单轮耗时）`web_rid` 自动落盘并开始检测；
2. 无缺号主播时：浏览器零启动、`/api/status.rid_backfill.running == false`、`reason == "no_pending"`；
3. 连续 4–5 天无人值守：无崩溃、无进程/内存泄漏、验证页触发次数为 0（或触发后自动冷却自愈）、日报有数据、审计文件可复盘；
4. 单日主页访问次数 ≤100，单轮 ≤3 次；
5. 全量测试绿（基线 1133 项 + 新增）、`check_docs_sync` 绿、5 份文档已同步；
6. 未修改任何禁改区文件（`detect.py` 判定语义 / `schedule.py` 参数 / `contract/` / `auth.py` 浏览器目录）。

---

## 10. 实施结果（2026-10-26，P1–P4 全部完成）

| 阶段 | 结果 |
|---|---|
| P1 | `web_rid_probe.py`（577 行）+ `tests/test_web_rid_probe.py`（54 项，全绿） |
| P2 | `web_rid_browser.py`（278 行）+ `scripts/probe_web_rid.py`；真机验证：在播 1714ms 取到 rid、与 config 既有值一致、归属校验 `same`；离线 4126ms 判 `offline`；连续调用后无残留 headless 进程 |
| P3 | `monitor.rid_backfill_loop` / `_rid_apply_web_rid` / state / 失败审计 / 日报一行 / `health.SPECS` 第 22 项；`tests/test_web_rid_loop.py`（15 项，含「offline 连打不熔断」「mtime 冲突放弃」「daily_cap 零访问」） |
| P4 | `/api/status.rid_backfill` + 前端徽标/详情/高级参数组；5 份文档同步；全量 **1235 项测试绿**、`check_docs_sync` 通过、敏感词门通过 |

踩到并修掉的两个真实 bug（记在这里防止回退）：
1. `task_batch` 在「待补人数 < 单轮上限」时会把人**重复塞进同一轮**（`picked += avail[:limit-len(picked)]`），代价是白烧浏览器配额与日志说谎 —— 已改为 `take = min(limit, len(avail))` 并加守卫测试。
2. `should_run` 里调 `task_batch` 会**推进 cursor 两次**（每轮少查一个人）—— 改为只读的 `available_monitors()`。
