# 契约变更记录(CHANGELOG)

- 格式:`## vX.Y.Z(YYYY-MM-DD)` + 条目;**每次升版本必须在这里加条目**(守卫强制联动)。
- 语义:patch = 澄清/笔误;minor = 向后兼容加法;major = 删改语义(**两阶段发布,deprecated 挂 ≥2 周**)。
- 快照:版本变更时 `scripts/sync_contract.py` 把本文件与 contract.json 原子快照到 NAS `_对接/contract/`。

## v1.7.0(2026-10-29)

- **礼物口径统一为轻通道(minor/向后兼容)**:全主播礼物数据源固定为抖音轻礼物通道
  (`WebcastLightGiftMessage`)。完整通道(`WebcastGiftMessage`)事件**不再落盘** ——
  它与轻通道是服务端事件级互斥路由,匿名侧实测 349 场恒 0 条,双收只会重复计数。
  因此 `.danmaku.jsonl` 里 `type=gift` 行的 `channel` 字段**取值恒为 `light`**,
  消费方按 `type=gift` 全量消费即可,不必再按 `channel` 过滤。
- **`capabilities.gift` 补口径声明 + 新增 `capabilities.gift_health`**:轻通道只承诺
  `ts / gift / diamond / count` 四个字段;送礼人昵称、送礼人 uid、礼物名**协议不下发**
  (`user`/`uid` 恒空、`gift` 可能形如 `礼物#<id>`),属协议边界而非采集缺陷。
- **礼物健康度改判据(仅本机历史,不进 NAS 产物)**:从「存在礼物信息的时间占比」
  改为「轻通道收到多少条 vs jsonl 落了多少条 + 4 个承诺字段齐不齐」的对账结果,
  落在 `history.gift_integrity.light`:
  `{recv, events, full_rows, lost, errors{parse,write}, field_missing, field_ratio, ratio, complete, status, reasons}`,
  `status ∈ complete | partial | field_only | channel_off`。顶层 `ratio`/`covered_sec`
  仍是老口径,保留兼容。
- **结论码改名(诊断记录 `.giftwatch.jsonl`)**:`GIFT_PARSE_ERR` → `LIGHT_GIFT_PARSE_ERR`、
  新增 `LIGHT_GIFT_WRITE_ERR` / `LIGHT_FIELD_MISS` / `LIGHT_EVENT_LOST` /
  `LIGHT_CHAN_OFF` / `LIGHT_QUIET` / `LIGHT_OK`;`NO_GIFT_WHILE_BUSY` / `HEALTHY_NO_GIFT`
  下线。**语义变化**:零礼物不再产生 suspect 级告警(349 场实测证明多为服务端未下发,
  平台侧能力边界),只落 `quiet` 记录;`fault` 仅用于真实丢数据(解析失败/写盘失败)与
  线路故障。`snapshot` 新增 `light` 三段账 `{recv, kept, parse_errors, write_errors, field_missing, full_ignored}`。
- `contract_version` 1.6.0 → 1.7.0。

## v1.6.0(2026-10-29)

- **礼物版面重排：行距贴合字号 + 槽位连续堆叠 + 2/5 高度硬上限**(规则参数变更，产物坐标随之变化)。
- **行距压紧**：`row_height` 54 → **38**(设计单位，1080×1920 下 1:1)，字号 `slow_font` 44 → **38**、`fast_font` 32 → **30**
  (`slow_font_min` 32、`fast_font_min` 26)。依据是**实帧像素实测**：38 号汉字的实际墨迹只有 23~25px，
  原行高 54 会在相邻两行之间留 22px 空隙（比字本身还高），压到 38 后净空隙 16px、行距 = 字号。
  单测双向夹住 `0.8×字号 ≤ row_height ≤ 字号+2`，既防"间隔太宽"也防行间压字。
- **槽数 8 → 14**：7~8 行同屏是常态需求，密集时最多可叠 14 行；**但总高受硬上限约束**——
  新增 `max_area_ratio=0.4`(画面高度 2/5)，`layout_of()` 按 `(0.4×画布高 − 顶边距) / 行高` 自动裁槽，
  渲染层 `gift_layout_portrait()` 再兜一次底(防缩放取整越界)。1080×1920 下 14 行占 y=60..554、礼物区底部 y=592(**30.8%**，上限 2/5=768)。
- 🔴 **槽位分配由"固定偏好槽"改为"连续堆叠"**：旧实现让慢项永远用 0/1 号槽、快项永远用 3 号以后，
  于是"一行慢 + 一行快"中间会空出两格(y=60 与 y=168 之间 108px 的洞)，观感就是行距特别宽。
  现在慢项取第一个空闲槽(永远贴顶)、快项紧接当前仍在显示的慢项之后(无慢项时从 0 起)，
  **同屏 N 项必然占满 slot 0..N-1，不留空洞**。
- `capabilities.gift_display` 相应更新并新增 `max_area_ratio`；`check_contract_sync.py` 第 ⑧ 条继续逐项比对代码默认值。
- 黄金样例 `contract/fixtures/gift_display_golden.json` 重建(槽位坐标随之变化)。
- **消费方注意**：`.giftdisplay.json` 里 `slot_y` 数组长度由 8 变 14、行距由 54 变 38，**一律以文件内的 `slot_y` 为准**，
  不要硬编码行数或行高；`items[].slot` 依旧只是索引。时间基、命名规则、就绪信号与其余文件清单均未变。
- `contract_version` 1.5.0 → 1.6.0。

## v1.5.0(2026-10-29)

- **新增可选产物 `.meta/<基名>.giftdisplay.json`(礼物展示计划,minor/向后兼容加法)**:与 `.ass` 里的礼物 `Dialogue` **来自同一份计划**,
  含 `rule_version` / `layout_profile` / `segments[]`(每片 `slot_y`、`items[]`)/ `tail_overflow[]` / `stats{}`。
  每个展示项含 `seg/slot/start_ms/end_ms/lane/priority/channel/merged/price_known/delay_ms/borrowed_slot/segments[]/source_event_indexes[]`。
- **礼物展示规则写进契约 `capabilities.gift_display`**:`slow_min_diamond=99`(98 快 / 99 慢)、`high_priority_diamond=1000`、`slots_total=8`、
  `slow_preferred_slots=2`、四档快车道停留(1.5/1.0/0.8/0.6s)与三档慢车道(5.0/3.0/2.5s)、`merge_window_sec=3`、`max_segments=6`、
  `slow_combo_max_sec=8`/`fast_combo_max_sec=3`、`anonymous_merge=false`。
- **布局仅覆盖竖屏 `portrait-v1`**:参考画布 1080×1920,`scale=min(w/1080,h/1920)`;1080×1920 下慢/快字号 44/32px、8 个等高槽位
  y=60/114/…/438、礼物区底部 y=492(占高约 25.6%)。**横屏/方屏走原有单横幅路径,不输出本能力坐标**。
- **`.ass` 侧新增 `GiftSlow` / `GiftFast` 两种样式**(左上锚点 `\an7`):慢车道粗体高亮、快车道常规;匿名轻礼物只显示 `礼物名 ×N`(无名),
  完整通道显示 `昵称 送出 礼物名 ×N(金额 抖币)`。
- **合并口径**:仅完整通道按 `(uid, gift_id)` 在 3 秒窗口内合并、数量取连击累计的**最大值**;匿名轻通道**永不合并**,每个事件独立一项。
- **完整性口径**:每个原始礼物事件只能处于"已合并进可见项 / 作为独立可见项排入某分片 / 进入 `tail_overflow`"三者之一,
  不允许把视频结束后的 ASS 行算作已展示。消费方可用 `stats.covered` 与 `tail_overflow` 审计。
- `danmaku.jsonl` 结构**未变**(不新增展示字段);实时预览接口的 gift item 新增 `display` 对象(文案/车道/合并键),前端不再自行判断价格阈值。
- 时间基、命名规则、就绪信号、诊断包结构与其余文件清单**均未变**;`contract_version` 1.4.0 → 1.5.0(minor,向后兼容加法)。

## v1.4.0(2026-10-29)

- **新增可选产物 `.meta/<基名>.giftrank.jsonl`(贡献榜快照,minor/向后兼容加法)**:每分钟最多一条,`type=rank`,`ranks[]` 含 `rank/uid/name/score/gap`。
  `score` 单位=抖币,且**等于该用户的累计送礼金额(实测 1:1 精确相等:单人 +120000 ↔ 御龙·凌霄 30000 + 穹顶之巅 30000×3;+1 ↔ 粉丝团灯牌 1)**。
- 为什么加它:服务端把礼物事件分两条通道下发,匿名观众身份只收得到轻礼物通道;只走完整通道(`WebcastGiftMessage`)的场次匿名侧逐条礼物**恒为 0**。
  而贡献榜对匿名身份照常下发,所以它是那类场次**唯一的礼物金额线索**。实测同一场窗口内,榜单把占完整通道金额 99.8% 的两笔大额分毫不差地拿了回来。
- **边界(消费方必读)**:①只覆盖前 20 名,是下界不是总额;②拿不到逐条事件/礼物名/精确时间(只有快照时刻);③空榜时整条快照不写,文件可能整场缺失;**④`type=rank` 只出现在本文件,`danmaku.jsonl` 的 type 集合仍是 chat/gift/member,未变**。
- `danmaku.jsonl` 的 gift 行**新增 `channel` 字段**(`light`=匿名可见的轻通道 / `full`=完整通道):向后兼容加法,消费方可忽略;不改变任何已有字段语义。
- 录制历史 `history.json` 的 `gift_integrity` 新增 `gift_channel`(`light`/`full_only`/`quiet`/`unknown`)与 `chat_events`,用于标注"本场礼物数据是全量还是结构性不可见"。属**录制端内部元数据**,不在消费方读取路径上。
- 时间基、命名规则、就绪信号、诊断包结构、文件清单其余项**均未变**;`contract_version` 1.3.0 → 1.4.0。

## v1.3.0(2026-10-27)

- **时间基修正(音画不同步根治)**:录制不再使用 `-use_wallclock_as_timestamps`(逐包把 PTS 换成"读到该包的墙钟")。该项在包成批到达时会把 PTS 压扁、断流处直接跳秒,导致**音轨采样数少于视轨** —— 对端实测 `-000` 文件少 8.43s,且缺口分布在片头(音频多 1.5~2.6s)与中途断流处,不是单一偏移能修好的。改为保留推流端时间戳后,音视频落在同一坐标系,实测「音频采样数 ÷ 采样率」与「视轨跨度」差 < 0.15s,视频也不再在 CFR 下丢帧(此前 60s 实测丢 38%)。
- 新增 `timestamps` 两条:①`align.json` 增加可选字段 `clock_offset_ms`(源流相对毫秒 → 绝对墙钟的校准偏移);②音画同步保证。**`anchors` 的语义与格式完全未变**,仍是"该分片首包墙钟毫秒",消费方取场次 0 点的算法不需要任何改动。
- **断流补静音**:断流超过 0.5s 时录制端自动补等长静音(`aresample=async=1:min_hard_comp=0.5`),让音轨时间轴与视轨严格对齐。消费方**不再需要**按片段猜着补静音;正常时段采样间隔偏差仅 0.44ms,不会被误伤。
- 新增 `scripts/check_av_sync.py` 与 `douyin checkav`:数音频采样数与视轨跨度,超阈值即报。容器时长走 PTS 看不出这个问题,**只有数采样才暴露**,故做成可随时复跑的一条命令。
- `quality_facts` 补充:旧时间基产出的历史文件仍带该缺陷(音频实际短 0~8s 不等),对存量场次的时间校正请以 `checkav` 输出为准。
- 数据结构、命名规则、文件清单、就绪信号、诊断包结构**均未变**;`contract_version` 1.2.0 → 1.3.0(minor,向后兼容加法)。

## v1.2.0(2026-10-22)

- 新增 `capabilities.video` 段(向后兼容加法):录制改为 **H.264(High)+ VideoToolbox 硬件编码**(默认),视频码率可配(默认 1200 kbps)、音频固定 AAC 128 kbps,分辨率 4 档(原生/1080P/864/720,只缩不放、横竖自动选向)。
- 新增自动降级说明:**过载时录制端会自动把「分辨率+码率」整组降档**,因此同一场次内不同分片的分辨率/码率**可能不同**。消费方必须按每个分片自身 onMetaData 取尺寸(字幕 PlayRes 已由录制端按分片写入),不得假设整场一致。
- `quality_facts` 增 2 条:同场次分辨率可能不同;硬编与软编在容器/时间基/锚点上等价(实测四档帧数与墙钟 PTS 一致),仅同码率画质略有差异。
- `files[].video.notes` 补编码格式说明。**数据结构、命名规则、时间戳口径、就绪信号、诊断包结构均未变** —— 消费侧无需改动。

## v1.1.0(2026-10-18)

- 新增 `producer_duties` 段(向后兼容加法):**产物变更通告义务** —— 录制端凡调整产物形态/语义(files/naming/timestamps/capabilities/diagnostics 任一声明),必须 ①升版本+CHANGELOG ②跑 `scripts/notify_backend.py` 写通告并投递契约三件套 ③守卫兜底拦截漏改。
- 新增变更通告通道 `_对接/contract/NOTICE.md`(最新置顶,与契约快照同批投递):消费方读素材/对接状态时顺带读顶部条目即可获知变更。
- `capabilities.interop_dirs` 更新:contract/ 目录含 NOTICE.md。

## v1.0.0(2026-10-17)

- 首发契约:文件清单×完成态判据(7 类,含就绪信号与 session.json)、目录命名规则、时间戳分类表 12 行、功能矩阵、数据质量事实(5 条)、诊断包规范、安全边界(7 条)。
- 通道:NAS 文件面 `_对接/`(与「直播回放」平级),两机零 HTTP。
- 诊断包哈希口径:视频分片按大小+mtime 对账,sidecar/字幕做 sha256 前 8 位(带宽取舍,已在 contract.diagnostics.files_hash_policy 声明)。
