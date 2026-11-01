# 缺号主播 web_rid 自动补齐（A 方案：浏览器巡检）实施方案

> 目标读者：后续实现者（人或智能体）。本文只描述「怎么做、为什么这么做、边界在哪」，不含代码。
> 实测依据见 2026-10-26 调研结论：headless Chromium 未登录打开 `https://www.douyin.com/user/<sec_uid>`，页面「直播中」入口 href 即 `live.douyin.com/<web_rid>`；离线主播页面不含该入口，且资料接口 `room_id_str="0"`。单次判定成本：在播 1.3–1.4s，离线 5.0–5.2s。

## 1. 要解决的问题与硬约束

- 现状：添加主播必须有 `web_rid` 才能检测开播；而 `web_rid` 只在「直播间链接」里暴露，只有抖音号/主页链接时补不上。用户已经能拿到 `name / sec_uid / douyin_id`，只缺 `web_rid`。
- 目标：添加后系统**自动**在主播开播时把 `web_rid` 补进 `config.json`，补齐后立刻回到现有检测/录制链路（不引入第二套检测）。
- 硬约束：
  1. 巡检间隔 **三档随机**（40–50 / 80–90 / 110–120 分钟），两段随机选值，见 §4。
  2. **必须扛住 4–5 天无人值守**：不崩、不漏内存、不把 IP 打到触发验证、失败要能被看见。
  3. 开关策略：**有主播缺 `web_rid` → 自动开一个 7 天窗口；没有缺号主播 → 默认关闭**（连浏览器都不启动）。
  4. 不碰登录态：绝不把 `detection.cookie` 注入这个浏览器，绝不与 `auth.py` 的扫码登录浏览器共用实例/目录。
  5. 写 `config.json` 必须防「整体替换丢主播」事故（本项目历史上丢过 9 位）。

## 2. 核心思路

```
每轮结束 → 随机间隔（三档：40–50 / 80–90 / 110–120 分钟，两段随机）
      │
      ├─ 窗口不可用（无缺号主播 / 7 天已过期 / 全局冷却中 / 当日上限已到）→ 什么都不做（不起浏览器）
      │
      └─ 取本轮任务（缺 web_rid 的主播，最多 3 个，轮转游标）
             │
             └─ 逐个串行：开一次主页 → 判定 live / offline / unknown
                     ├─ live   → 校验 rid 归属（可跳过，见 §6.3）→ 读最新 config、只补 web_rid → 原子写 → 触发该主播 refresh
                     ├─ offline → 只更新「上次检查时间」，不写 config
                     └─ unknown → 不写、记失败计数、按熔断规则退避
```

关键取舍：**巡检只负责「把 web_rid 弄到手」这一件事**。一旦补上，后续检测完全走现有的 `detect.probe_anchor → check_302/check_api`，不新增常驻开销。

## 3. 生命周期：自动开关 + 7 天窗口

### 3.1 配置项（新增 `config.json` 顶层段）

```jsonc
"rid_backfill": {
  "enabled": "auto",        // "auto" | true | false。auto=按「有没有缺号主播」自动开关
  "window_days": 7,         // 自动开窗时长
  // 三档间隔（分钟，闭区间）。两段随机：先随机档位、再档位内随机取值，见 §4
  "interval_tiers_min": [[40, 50], [80, 90], [110, 120]],
  "tier_weights": [0.5, 0.3, 0.2],       // 与档位一一对应，权重和自动归一化（均值≈71 分钟）
  "avoid_same_tier_twice": true,        // 连续两轮不落同一档
  "second_jitter": true,                // 再叠加 0–59 秒抖动，避免整分撞点
  "max_per_round": 3,       // 单轮最多查几个主播（控总量）
  "gap_sec_min": 25,        // 轮内两个主播之间的随机间隔
  "gap_sec_max": 45,
  "timeout_sec": 45,        // 单个主播的页面预算
  "round_budget_sec": 180,  // 单轮总预算
  "max_checks_per_day": 100 // 日级硬上限（按「实际打开主页次数」计），后期可在此调
}
```

### 3.2 窗口状态（不入 config，避免高频写盘）

落 `state/rid_backfill.json`（gitignore）：

```jsonc
{
  "window_start": 1792962471,
  "window_until": 1793567271,
  "last_run_at": 1792962471,
  "next_run_at": 1792965800,
  "last_tier": 1,                    // 上一轮命中的档位下标（防连续同档 / 重启后不清零）
  "last_interval_sec": 4920,         // 上一轮实际间隔，用于「不与上次完全相同」
  "cursor": 0,                       // 轮转游标
  "checks_today": 5, "day": "2026-10-26",
  "per_monitor": { "<name>": {"last_check": 0, "verdict": "offline",
                              "rid": null, "fails": 0, "last_fail_at": 0,
                              "last_reason": "", "backoff_until": 0} },
  "circuit": {"fails": 0, "until": 0, "reason": "", "cooldown_today": 0}
}
```

### 3.3 开/关规则（每轮开始时评估，纯函数可测）

| 条件 | 动作 |
|---|---|
| `enabled=false` | 永不巡检（人工强制关） |
| `enabled=true` | 无视窗口，一直巡检（人工强制开） |
| `auto` 且 `pending = [m for m in monitors if not m.web_rid]` 为空 | **关窗**（`window_until=null`）、关闭浏览器、`next_run_at=null` |
| `auto` 且 pending 非空、无窗口或窗口已过期 | **开 7 天窗口**（`window_until=now+7d`），日志一行 + 可选企微通知 |
| `auto` 且窗口有效、但全局冷却中 | 本轮跳过（只更新 `next_run_at`） |

- **7 天到期后不自动续期**：pending 仍非空 → 状态置 `expired`，卡片提示「补号巡检已到期，仍有 N 位缺号」，发一次通知；用户可在控制台点「续 7 天」，或新增主播（pending 变化）时自动重新开窗。理由：无限期自动巡检是最容易把 IP 打黑的做法，必须有一个必须人工确认的闸门。
- 窗口 7 天 > 无人值守 4–5 天，覆盖需求且留 2 天缓冲。

## 4. 调度节奏（控总量是反爬的第一手段）

### 4.1 两段随机：先选档位，再选分钟

三档（闭区间，单位分钟，整体单调、互不重叠）：

| 档位 | 范围 | 命中概率（默认权重） | 该档均值 |
|---|---|---|---|
| T0 短 | 40–50 | 50% | 45 |
| T1 中 | 80–90 | 30% | 85 |
| T2 长 | 110–120 | 20% | 115 |

- **第一段**：按 `tier_weights` 随机选档位（默认 `[0.5, 0.3, 0.2]`，权重和自动归一化）。
- **第二段**：在选中档位内**均匀取整数分钟** `m = randint(lo, hi)`（含两端）。
- **第三段（可选，默认开）**：叠加 `s = randint(0, 59)` 秒 → `interval_sec = m*60 + s`。
- `next_run_at = 本轮结束时刻 + interval_sec`（**从轮结束算起**，不是从轮开始，避免慢轮把间隔压缩、出现"贴背"的两次访问）。

伪代码：

```
def plan_next_interval(cfg, state):
    tiers, weights = cfg.interval_tiers_min, cfg.tier_weights
    idx = weighted_choice(weights, exclude=state.last_tier if cfg.avoid_same_tier_twice else None)
    lo, hi = tiers[idx]
    m = randint(lo, hi)
    if state.last_interval_sec and m*60 <= state.last_interval_sec < (m+1)*60:
        m = lo if m == hi else m + 1          # 撞上次具体值 → 档内平移一格（边界回绕）
    sec = m*60 + (randint(0, 59) if cfg.second_jitter else 0)
    state.last_tier, state.last_interval_sec = idx, sec
    return idx, sec
```

### 4.2 防重复

- **不连续同档**：`avoid_same_tier_twice=true` 时从「除上一档以外的两档」里加权抽取；只有 3 档，永远至少剩 2 个候选，不会抽空。
- **不与上次同值**：若算出的分钟数与上一轮落在同一分钟区间，档内平移一格（到端点则反向回绕），保证连续两轮的**具体分钟值不同**。
- **不落整点**：秒级抖动 0–59s，使 `next_run_at` 永远不是 `mm:00`，避免"整点密集"这种一眼可见的机器特征。
- **重启不清零**：`last_tier` / `last_interval_sec` / `next_run_at` 都落在 `state/rid_backfill.json`，服务重启后延续同一随机序列，不会因重启变成"每轮都从 T0 开始"。
- **权重与档位可配**：后期想加密/放宽只改 `interval_tiers_min` + `tier_weights`，不改代码。

### 4.3 边界与异常处理

| 边界 | 处理 |
|---|---|
| 档位配置非法（空 / 上下界反了 / 档间重叠 / 权重全 0） | 启动时校验，回落到默认三档并在日志报一次 `config_invalid`，不崩 |
| 机器休眠 / 服务重启导致 `next_run_at` 已过期 | **只跑一轮**（不补跑积压轮次），并在开跑前加 120–300s 随机暖场延迟，避免"一开机就连打三次" |
| 单轮超出 `round_budget_sec` | 立即收尾，剩余主播留到下一轮（下一轮仍从 `cursor` 继续） |
| 上一轮结束时正在录制高负载 | 不改间隔，但把本轮的 `timeout_sec` 收紧到 30s；抓不到就记 `timeout`，等下轮 |
| 时钟回拨 / 系统时间被改 | `now < last_run_at` 视作异常：重置 `last_run_at=now`，并按 T1 档重排下一轮 |
| 日上限已到 | 不再计算间隔，`next_run_at` 置为次日 00:00 + 0–10 分钟随机 |

### 4.4 平均间隔与代价（必须知情）

权重 `[0.5, 0.3, 0.2]` 时**均值约 71 分钟**（0.5×45+0.3×85+0.2×115；三档等权为 82 分钟，旧方案 55 分钟）。这意味着：

- ✅ 更像人：间隔分布呈多峰、无固定周期，比窄幅抖动更难被周期性识别；同时把重心压在 T0（50%），漏场概率低于等权方案。
- ⚠️ 代价：仍有 20% 的轮次落在 110–120 分钟——**30 分钟以内的短场依然可能整场漏掉**（两次巡检之间"开播→下播"全落在里面，这一场就没了；但下一轮仍能补到 `web_rid`，只是那一场没录）。
- 缓解：① 一旦补到 `web_rid`，该主播立刻回到现有检测链路（热点 5–6 分钟、冷门 30 分钟），后续场次不再受影响；② 若实测漏场仍偏多，把 `tier_weights` 调成 `[0.6, 0.3, 0.1]`（均值≈64 分钟）即可，不用改代码。

### 4.5 单轮与日上限

- 单轮 **串行**，主播之间随机 `25–45s`；单轮最多 3 个（`max_per_round`），用 `cursor` 轮转，避免永远先查同一个人。
- 单主播页面预算 45s，单轮总预算 180s。
- **日上限 `max_checks_per_day = 100`（初期值，后期可调）**：按「**实际打开主页的次数**」计，不按主播数计；每成功发起一次页面加载即 `checks_today += 1`。达到上限后本轮起停止，直到次日 00:00 归零。参数改小立即生效（下一轮就用新值判定），改大不补做当天已跳过的轮次。
- **日上限与档位的真实关系（别算错）**：
  - 常态（均值 71 分钟、3 位待补）：约 20 轮/天 × 3 ≈ **60 次/天，碰不到 100**；
  - 极端（连续抽中 T0，40 分钟一轮）：36 轮 × 3 = **108 次 > 100 → 日上限先卡住**，约 33 轮即停；
  - 只有 1 位待补：36 次/天，远低于上限。
  - 结论：**100 是安全阀而不是日常约束**——它在"T0 连续命中 + 多人待补"这种最坏情况下兜底。按上限反推：**日均最多 100/24 ≈ 4.2 次主页访问/小时**，任一时刻单轮 ≤3 次。
- 夜间不做特殊限制（无人值守要连续），但预留 `quiet_hours` 字段（默认空）以备后续收紧。

## 5. 反爬设计（四层）

### 5.1 身份层：让浏览器看起来是一个持续存在的普通访客

- **持久化 `user-data-dir`**：`state/browser/rid/`。同一目录反复用，`ttwid`/`msToken`/`UIFID` 自然累积并延续，第 2 天起就不再是"每次全新访客"。**与 `auth.py` 的扫码浏览器完全隔离**（不同目录、不同实例）。
- 固定指纹：真实 Chrome UA（版本与内核匹配，不伪造）＋ `locale=zh-CN`＋`timezone=Asia/Shanghai`＋`viewport 1440×900`＋`deviceScaleFactor=1`。
- 抹自动化痕迹：`--disable-blink-features=AutomationControlled`；init script 覆写 `navigator.webdriver`；补齐 `window.chrome`、`navigator.languages`。
- 代理：显式 `--no-proxy-server`（本项目历史上有代理变量把服务静默打停的事故；playwright 默认继承系统代理）。
- **不注入任何登录 Cookie**（保护真实账号，也不消耗 `cookie_guard` 配额）。

### 5.2 节奏层：频率与随机性

- 见 §4：间隔三档两段随机、轮内 25–45s 随机、**单轮 ≤3 次、日均上限 100 次**（≈≤4.2 次/小时）。
- 页面**只驻留到判定完成即关**（在播 1.3s / 离线 5s），不滚动、不点击、不触发视频播放（播放行为是最容易被记的特征）。
- 每轮首次访问前若距上次 >2 小时（机器休眠过/服务重启），先访问一次 `https://www.douyin.com/` 做暖场拿 Cookie，再访目标主页。

### 5.3 行为层：少做事

- 只做 `goto` + 等 DOM/XHR，不模拟滚动与点赞等"伪人类行为"（伪行为反而更容易被识别为脚本）。
- 用 `route` 阻断 `image/media/font` 类资源，只保留 `document/script/xhr/fetch`：更快出 DOM，流量更低；**不得阻断 XHR**（判定依赖 profile 接口）。

### 5.4 「失败」的定义（先定清楚，再谈熔断）

**结论：查不到 ≠ 没开播。`offline` 是成功，不是失败。**

| 判定 | 含义 | 计不计失败 | 写 config |
|---|---|---|---|
| `live` | 拿到 `web_rid` 并校验通过 | ❌ 成功（连败计数清零） | ✅ 写入 |
| `offline` | **正常查到了**：profile 接口 200 + `room_id_str="0"` + DOM 无直播入口 → 主播此刻没开播 | ❌ **成功**（连败清零） | ❌ |
| `unknown` | **没能查到**：被拦、超时、异常、数据缺失 | ✅ 失败 | ❌ |

把 `offline` 当失败会是灾难：待补主播本来就没开播，按"连续 3 轮"的规则，三次巡检（最快 2 小时）就会把所有待补主播全熔断掉，巡检形同废掉。所以熔断只统计**「无法完成查询」**。

`unknown` 必须带**原因码**（决定退避策略与后期诊断）：

| 原因码 | 触发条件 | 归类 |
|---|---|---|
| `timeout` | 单主播页面预算（45s）内没判定出结果 | 网络类 |
| `nav_error` | DNS/连接失败、`net::ERR_*`、导航异常 | 网络类 |
| `empty_shell` | DOM < 20KB 且 profile XHR 无响应，页面没渲染出内容 | 风控/异常 |
| `login_wall` | 出现登录遮罩/二维码，且 profile XHR 未返回 | 风控类 |
| `captcha` | 滑块/验证页（`captcha`/`verify`）、跳到 passport/verify 域名 | 风控类 |
| `http_429` | 页面 HTTP 429（限流） | 风控类 |
| `http_403` | 页面 HTTP 403 | 风控类 |
| `xhr_blocked` | profile XHR 返回 412/403 或空 body | 风控类 |
| `browser_crash` | playwright/chromium 异常、进程崩溃、上下文被回收 | 异常类 |
| `rid_mismatch` | 抓到 rid 但 302 归属校验指向别人（防串号） | 数据类 |

**日志要求（便于后期诊断）**：每次失败写一行业务日志，字段固定、可直接 grep：

```
[rid-backfill] FAIL 2026-10-26 15:20:31 name=主播甲 anchor=MS4w…VTDZ reason=xhr_blocked
               detail="profile XHR status=412" fails=2/3 backoff=none next=2026-10-26 16:05:12
```

同时追加到审计文件 `state/rid_backfill_fails.jsonl`（每行一条 JSON，含 `ts/reason/detail/name/anchor_tail/fails/round_no`，超过 500 行自动截断保留最近 500 条），用于复盘"某天为什么一个都没补到"——业务日志只有 8 份滚动、跨天就翻不到了。日报里按 `reason` 分类汇总（如「unknown 5 次：xhr_blocked×4、timeout×1」）。

### 5.5 三层熔断（只看 `unknown`，不看 `offline`）

1. **单主播**：**连续 3 个轮次**都判 `unknown`（同一轮内的多次重试不重复计数）→ 该主播退避 6 小时，`backoff_until` 落 state。
   - 期间任何一次得到 `offline` 或 `live` 都**立即清零**连败计数。
   - 网络类原因（`timeout`/`nav_error`）退避 6h；`rid_mismatch` 不退避，只记录并把该 rid 加入本地黑名单。
2. **全局**：单轮出现 ≥2 个 `unknown`，或出现**风控类**原因 1 次（`captcha`/`login_wall`/`http_429`/`http_403`/`xhr_blocked`）→ 全局冷却 3 小时；冷却结束后再触发 → 12 小时。
   - 纯网络类（本机断网、DNS 挂了）**不触发全局冷却**——那不是抖音在拦我们，冷却也没用，只在健康面板标黄。
3. **日级**：当天 `unknown` 比例 >50%，或当天冷却次数 ≥3 → 停到次日 00:00，**发企微通知 + 健康面板亮红**。

失败不影响该主播在业务层的状态徽标（别让"补号失败"污染「未监测/在播」显示）；补号相关状态只出现在 `rid_backfill` 那一行。

冷却期内**绝不重试**。这条比"尽快补上"重要得多：一次滑块可能导致这台机器几天都拿不到页面。

## 6. 判定与写回

### 6.1 三态判定见 §5.4

### 6.2 写回 config（防覆盖是重点）

严格「读—验—改—写」：

1. **重新读盘** `monitor.load_config()`（不是用内存快照）。
2. 按 `name` + `anchor`(sec_uid) 双匹配定位条目；找不到 → 放弃（主播已被删/改名）。
3. 该条已有 `web_rid` → 什么都不做（可能人工已补）。
4. **只写**三个字段：`web_rid`、`web_rid_source="auto"`、`web_rid_filled_at=<ts>`；其余字段原样。
5. **写前比对读取时的 mtime+size**；不一致 → 重读重试 1 次；仍不一致 → 本轮放弃（`unknown`，下轮再来）。
6. `monitor._atomic_write_json(CONFIG_PATH, cfg)` 原子写；巡检进程内同一时刻只允许一个写者。
7. 成功后 `state.request_refresh(name)` 立刻检测一次 → 日志「已自动补齐 web_rid=xxx」→ 可选通知。

> 为什么这么啰嗦：`POST /api/config` 是 **monitors 整体替换**语义，后台写盘与前端保存一旦交叉就会丢条目（历史事故：9 位主播消失）。冲突时**放弃本轮**永远优于覆盖。

### 6.3 归属校验（可选但推荐）

把抓到的 rid 用移动 UA 请求 `https://live.douyin.com/<rid>`，跟随 302 拿 `location` 里的 `sec_user_id`，与目标主播 `anchor` 比对（注意：**桌面 UA 返回 200 不 302**，必须用移动 UA）。不一致 → 视为 `unknown`，不写。成本一次 HTTP（几毫秒），能挡住"页面推荐位串号"这类最坏的错补。

## 7. 系统接缝与改动清单

| 文件 | 改动 |
|---|---|
| **`web_rid_probe.py`（新）** | 纯逻辑，零 IO、不 import playwright：`pending_monitors()`、`should_open_window()`、`window_state()`、`plan_next_run()`、`decide_verdict()`、`merge_web_rid()`、`Circuit` 状态机 |
| **`web_rid_browser.py`（新）** | 唯一 import playwright 的地方：`fetch_web_rid(sec_uid)`（同步，返回 `{verdict, rid, reason}`），内部负责持久化 profile、资源阻断、超时、`finally` 关闭 |
| `monitor.py` | 新增 `rid_backfill_loop()` 协程（与既有 loop 并列）+ `health.beat("ridbackfill")` + 默认配置段；**浏览器调用必须 `asyncio.to_thread` 包起来**（`sync_playwright` 会阻塞事件循环，直接调会把检测/录制协程一起卡住） |
| `health.py` | `SPECS` 新增第 22 项 `ridbackfill`：stale 阈值按**最长档**取 120min×1.5≈180 分钟（按最短档会误报"卡死"）；熔断/连续失败记为 error |
| `webui.py` | `State` 读 `state/rid_backfill.json`，`/api/status` 增加 `rid_backfill` 段（enabled/window_until/pending/上次/下次/最近结果）；可选 3 个 POST：立即检查一次 / 续 7 天 / 暂停 |
| `static/index.html` | 「缺web_rid」徽标加 tooltip（巡检中·上次 05:12·下次 06:07）；详情区一行显示窗口剩余天数与结果；高级参数加一组「缺号补齐」 |
| `scripts/probe_web_rid.py`（新） | 手动一次性 CLI：`probe_web_rid.py <sec_uid>`，便于上线前后单独验证抓取器（不依赖服务） |
| 文档 5 份 | `项目框架说明书.md`（模块 + 行数 + 接口）、`PROJECT_GUIDE.md`、`AI_AGENT_GUIDE.md`、`README.md`（添加主播那节的自动补齐说明）、`主播开播状态监控.md` |
| 测试 | `tests/test_web_rid_probe.py`（纯逻辑）、`tests/test_web_rid_loop.py`（假 fetcher + 注入时钟跑虚拟 7 天） |

不影响 asrclip 契约（`contract/` 不动）。

## 8. 观测与「4–5 天无人值守」保障

- `/api/status.rid_backfill` + 健康面板第 22 项：能看到「上次几点查的、下次几点、最近结果与原因码、是否冷却、今日已用 N/100」。
- 每日 04:00 质检日报追加一行：`补号巡检：今日 N 次 / 补到 M 位 / unknown K 次（xhr_blocked×a、timeout×b）/ 熔断 X 次`。**失败必须能被看见**（本项目有过「企微通道死了三个月没人知道」的教训）。
- 日志：每次巡检一行业务日志（成功/失败都写，格式见 §5.4），走既有 `logs/` 8 份滚动机制；失败另存 `state/rid_backfill_fails.jsonl`（保留最近 500 条，跨天仍可复盘）。
- 进程健壮性：浏览器异常一律 `finally` 关闭；chromium 崩溃单轮最多重拉 2 次；连续崩溃 → 本轮 `unknown` 收尾，不拖死宿主协程。
- 资源：单次 1–5s、峰值 CPU 短促；与 7 路硬编录制（0.06 核/路）无冲突。空闲 20 分钟自动关浏览器，避免常驻内存。
- 幂等：已有的 `web_rid` 不重写；同一 rid 重复抓到不会产生多条写入。

## 9. 测试与验收

单元（不联网、不起浏览器）：
- 窗口：pending 空→关；pending 非空→开 7 天；过期仍 pending→expired 不自动续；
- 节奏：连续 1000 次抽样每个档位都命中、取值落在本档区间内、权重偏差在容差内；`avoid_same_tier_twice` 下无连续同档；相邻两轮分钟值不相同；`next_run_at` 永不为整分；配置非法（空/倒置/重叠/权重全 0）回落默认并报 `config_invalid`；`next_run_at` 已过期只补跑一轮；日上限达 100 后 `next_run_at` 指向次日；
- 判定：DOM 有 rid→live；接口正常 `room_id_str="0"`→offline；验证页/空壳/超时/429/XHR 412→unknown **且原因码正确**；**offline 不计失败、连败计数清零**；
- 熔断：`offline` 连打 10 轮**不触发**熔断（最关键的一条，防把"没开播"当失败）；unknown 连 3 轮触发单主播退避、第 4 轮零访问；风控类触发全局冷却、纯网络类不触发；冷却期内零访问；日级 >50% 停到次日；
- 写回：mtime 冲突→放弃；主播已删→放弃；已有 rid→不动；**除三个字段外其余内容逐字节不变**。

破坏性验证：把写回冲突保护改成"直接覆盖" → 测试必须报红（照本项目既有习惯做一次反向验证）。

端到端（独立端口临时实例 + 真浏览器，清代理，不碰 8780）：
- 拿一位**在播**主播：预期 `live` + rid 落盘 + 下一轮检测判在播；
- 拿一位**离线**主播：预期 `offline`、config 不动；
- 频率合规自检：从日志统计 24h 内目标主页请求数 ≤100（且任一小时 ≤6 次）。

Definition of Done（对外口径）：
1. 缺号主播开播后，**最迟 60 分钟**内 `web_rid` 自动落盘并开始检测；
2. 无缺号主播时，浏览器零启动、`/api/status` 显示「已关闭」；
3. 连续运行 4–5 天：无崩溃、无内存线性增长、无验证页触发（或触发后能自动冷却自愈）、日报有数据；
4. 全量测试（当前 1133 项）绿 + `check_docs_sync` 通过。

## 10. 实施顺序

| 阶段 | 内容 | 产出 |
|---|---|---|
| P1 | `web_rid_probe.py` + 单测（窗口/节奏/熔断/写回合并） | 纯逻辑全绿，无网络 |
| P2 | `web_rid_browser.py` + `scripts/probe_web_rid.py` | 命令行能对指定 sec_uid 输出 live/offline/unknown 与 rid |
| P3 | 接入 `monitor.rid_backfill_loop` + state 持久化 + health + 日志 | 服务内自动跑，`/api/status` 可见 |
| P4 | 前端展示 + 高级参数 + 文档 5 份 + 全量测试 + 端到端 | 可交付 |
| P5 | 4–5 天无人值守观察（只读日志，不改代码） | 观察报告：unknown 比例、补号成功率、是否触发验证 |

## 11. 风险与回滚

| 风险 | 影响 | 兜底 |
|---|---|---|
| 抖音改版，DOM 不再出现 `live.douyin.com/<rid>` | 永久 unknown | 判 unknown 不误补；日报 + 健康面板显性化；退回手动「粘直播间链接」 |
| 触发滑块/登录墙 | 几天拿不到页面 | 三层熔断 + 冷却期零访问 + 企微通知；冷却结束自动恢复 |
| 与 `auth.py` 扫码登录抢资源/风控关联 | 登录失败 | 独立 user-data-dir；auth 处于登录流程时本轮直接跳过 |
| 与前端保存 config 交叉写 | 丢主播 | §6.2 的 mtime 校验 + 冲突放弃 + 单项写入 |
| `sync_playwright` 阻塞事件循环 | 录制/检测卡住 | 一律 `asyncio.to_thread`，并在测试里断言主循环心跳不被拉长 |
| playwright/chromium 缺失（重建 venv 未装浏览器） | 巡检不可用 | 启动自检提示、卡片显示「缺浏览器组件」，主功能不受影响 |

回滚：把 `rid_backfill.enabled` 设为 `false` 即完全停止（无需改代码）；删 `state/rid_backfill.json` 重置窗口与熔断计数。

## 12. 待确认（实现前需要拍板）

1. 控制台要不要那三个按钮（立即检查一次 / 续 7 天 / 暂停）——要则加 3 个 POST 接口与前端交互，工作量 +1 小块。
2. 补到 `web_rid` 后要不要发企微通知（「主播甲 已自动补齐直播间号，开始检测」）。
3. `auth.py` 正在扫码登录时，巡检是「跳过本轮」还是「照跑」——建议跳过（同机两个 chromium 同时访问抖音，风控关联概率更高）。

---

## 13. 实施状态（2026-10-26 已落地）

P1–P4 已按本文与 `缺号主播web_rid自动补齐-AI智能体部署方案.md` 实现并验证：

- 新增 `web_rid_probe.py`（纯逻辑，54 项单测）、`web_rid_browser.py`（浏览器抓取）、`scripts/probe_web_rid.py`（排障 CLI）、`tests/test_web_rid_loop.py`（15 项编排/写回测试）。
- `monitor.py`：`rid_backfill_loop` + `_rid_apply_web_rid` + 配置段；`health.SPECS` 第 22 项；日报新增「补号巡检」一行。
- `webui.py`：`/api/status.rid_backfill` 段；前端徽标 tooltip + 详情「补号」行 + 高级参数组「缺号补齐(web_rid)」。
- 与方案的两处偏差：① 实际权重 `[0.5,0.3,0.2]`、日上限 `100`（按用户定稿）；② 浏览器**每次用完即关**（不常驻空闲回收）—— cookie 连续性靠 user-data-dir，常驻进程在无人值守期是内存/僵尸风险。
- 实测：在播主播 1.3–1.7s 取到 rid 且与 config 既有值逐位一致；离线 4.1–5.2s 判 offline；无残留 chromium 进程。
