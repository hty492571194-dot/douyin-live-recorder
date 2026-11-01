# ffmpeg 崩溃归因：`scale_vt` 滤镜重初始化失败（2026-10-29）

> 触发问题：主播D那场礼物统计对不上时，顺带发现日志里 ffmpeg 每隔几十分钟就退一次，
> 但页面上、手机上、告警里**一个字都看不到**。
>
> 结论先说：**8 天崩了 86 次，全是同一个错误；与并发路数、画幅、分辨率换档都无关，
> 只跟特定主播的流有关。画质调度 100% 兜不住它（因为这条错误被归成「原因未知」）。**
>
> **根因已在合成流上 100% 复现（§5），修复已实施（§8.1）**：缩放从 GPU `scale_vt` 换成
> CPU `scale`，硬解硬编都保留，代价 +0.17 核/路。升级 ffmpeg 无效 —— 上游从未修（§5b）。
>
> 本文的主播名按项目惯例用化名（主播A…主播K），对应关系见本机 `config.json`，不落文档。

---

## 1. 数量与分布

统计范围：`logs/monitor-2026-10-22.log` ~ `2026-10-29.log`（当前保留的 8 份）。

| 指标 | 数量 |
|---|---|
| ffmpeg 退出记录（含 `最后输出` 行） | 348 |
| 其中 **`scale_vt` filtergraph 重初始化失败** | **86**（24.7%） |
| 其中流损坏类（`missing picture` / `Stream ends prematurely`） | 15 |
| 其余（404 失效、主动停录、正常收尾等） | 247 |

按天：10-22 起几乎每天都有，单日 10-13 次（10-26 主播A 13 次 / 主播B 12 次最密）。

## 2. 错误签名（86 条完全一致，无一例外）

```
Impossible to convert between the formats supported by the filter 'Parsed_scale_vt_0'
  and the filter 'auto_scale_0'
[vf#0:0 @ 0x…] Error reinitializing filters!
[vf#0:0 @ 0x…] Task finished with error code: -78 (Function not implemented)
[vf#0:0 @ 0x…] Terminating thread with return code -78 (Function not implemented)
```

三个关键词：

- **`Error reinitializing filters!`** —— 不是启动即失败，是**运行中**滤镜图被要求重新初始化；
- **`auto_scale_0`** —— ffmpeg 自动插入的格式转换滤镜，说明 `scale_vt` 输出的格式与下游编码器
  期望的硬件帧格式对不上，没人能桥接；
- **`-78`** —— macOS 的 `ENOSYS`（Function not implemented）。即重初始化时调到了
  VideoToolbox 一条没实现的路径。`-78` 不是 OOM(-12)、不是 EIO(-5)，**不是资源耗尽**。

## 3. 已排除的假设（都做过数据对照）

| 假设 | 证伪方式 | 结果 |
|---|---|---|
| **并发路数太多、硬件会话耗尽** | `recordings/history.json` 495 场次区间做扫描线求并发 | ❌ 近 8 天**峰值只有 4 路**（全历史峰值 6 路，10-14）；1 路独占时崩溃率仍有 2.1 次/10h。`max_hw_streams=8` 的准入闸 8 天**从未触发过**（0 次），说明路数从来没到过上限 |
| **画幅特殊（横屏/异形分辨率）** | 按主播统计 `history.media` 的画幅分布 | ❌ 高发主播与零发主播都以 1088×1920 为主（主播B 69/99、主播G 82/90），分布无差异 |
| **中途分辨率换档（自动降级）** | 逐个分片读 FLV onMetaData | ❌ 主播A今日 23 片、主播D 11 片全程 1080×1920，一次没变。**但这不能排除源变过**：崩溃发生在第一帧新尺寸上，那帧来不及编码，产物只能记旧尺寸 —— 见 §5 |
| **流地址失效（404）** | 错误文本分类 | ❌ 那是另外 15 条 `missing picture` 类，与这 86 条**0 重叠** |

## 4. 真正强相关的因素：主播

| 主播 | 录制小时 | scale_vt 崩溃 | 每 10 小时 |
|---|---|---|---|
| 主播A | 48.2 | **29** | 6.02 |
| 主播B | 67.7 | **36** | 5.32 |
| 主播C | 89.2 | 17 | 1.91 |
| 主播D | 25.8 | 3 | 1.16 |
| 主播E | 17.6 | 1 | 0.57 |
| 主播F | 71.1 | 0 | 0 |
| 主播G | 69.2 | 0 | 0 |
| 主播H | 16.9 | 0 | 0 |
| 主播I / 主播J / 主播K | 13.0 | 0 | 0 |

**4 个主播吃掉 85/86 次；另外 7 个主播合计 160+ 小时，一次没崩过。**
画幅、码率配置完全相同 —— 所以差异只能来自**流本身**。

## 5. 机制（已实测复现，不再是推断）

**一句话**：源流在运行中改变帧参数 → ffmpeg 要求重建滤镜图 → `scale_vt` 重建失败 → 进程退出。

用合成流复现（2026-10-29，本机 ffmpeg 7.1）：

```bash
# 造 1088×1920 → 720×1280 两段，concat 成一条流（解码头之后参数变化）
ffmpeg -f lavfi -i "testsrc2=size=1088x1920:rate=30:duration=3" -c:v libx264 -g 15 a.flv
ffmpeg -f lavfi -i "testsrc2=size=720x1280:rate=30:duration=3"  -c:v libx264 -g 15 b.flv
printf "file 'a.flv'\nfile 'b.flv'\n" > list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy combo.flv

# 用线上参数跑 → 3 秒处必崩，报错与生产逐字一致
ffmpeg -hwaccel videotoolbox -hwaccel_output_format videotoolbox_vld -i combo.flv -copyts \
  -c:v h264_videotoolbox -b:v 1200k -allow_sw 0 \
  -vf "scale_vt=w='if(gte(iw,ih),min(iw,1920),min(iw,1080))':h='if(gte(iw,ih),min(ih,1080),min(ih,1920))'" \
  -an -f null -
```

复现日志比生产多一行（生产跑在 `-loglevel error`，这行是 verbose，所以从来没留下过）：

```
[vf#0:0] Reconfiguring filter graph because video parameters changed to
          videotoolbox_vld(unknown, unknown), 720x1280, hwaccel changed
Impossible to convert ... 'Parsed_scale_vt_0' ... 'auto_scale_0'
Error reinitializing filters! → error code -78 (Function not implemented)
```

**机制**：`scale_vt` 的输出与输入**共用同一个 VideoToolbox 帧池**（上游 commit `9b54388`
"fix output frame dimension … shares hardware frame context with input"）。输入分辨率一变，
帧池必须换，而 `scale_vt` 在重配置路径里没能换 → 它输出的 `videotoolbox_vld` 与 ffmpeg
自动插入的 `scale`（`auto_scale_0`）协商失败 → ENOSYS → vf 线程终止 → 进程退出。
`-78` 是 macOS 的 `ENOSYS`，不是 OOM、不是资源耗尽。

**三类触发都实测过**（都能复现，都被修复解决）：

| 触发 | 现网参数（`scale_vt`） | 修复后（CPU `scale`） |
|---|---|---|
| 分辨率 1088×1920 → 720×1280 | ✗ 崩 | ✓ 录完，输出跟随 720×1280 |
| 位深 8bit → 10bit | ✗ 崩 | ✓ 录完（自动转 nv12，该编码器只收 8bit） |
| 抖动后恢复 a → b → a | ✗ 在第一处就崩 | ✓ 两次重建都成功，尺寸 1080→720→1080 |

**为什么翻产物看不出源变过**：崩溃就发生在第一帧新尺寸上，那一帧根本来不及编码，所以
分片的 `onMetaData` 永远是旧尺寸；重启后如果源已经变回去，输出自然也还是旧尺寸。
**抖动（跳一下又变回）比稳定切换更常见** —— 这正是「4 个主播 86 次、另 7 个主播 0 次」
这个形状的由来。同理，`-reinit_filter 0` 也救不了：hw frames 变了 ffmpeg 会强制重建
（实测仍崩）。

## 5b. 上游现状：升级 ffmpeg 解决不了

`libavfilter/vf_scale_vt.c` 自 2023-07 引入（`c2c96c4`）至今，**没有任何一条提交处理滤镜图
重配置**。2026-04 的最新一条（`3f9b92b`）只是给 `ff_scale_adjust_dimensions()` 加了错误码
传播。所以这条路只有「换缩放后端」一条出口。

## 6. 为什么一点提示都没有（三处缺口，缺一不可）

### 6.1 前端：只在「数据停滞」时才提重启

`static/index.html` 里那句「本场已重启 N 次」写在 `if (rec.stalled)` 分支里 ——
`stalled` 的定义是**文件 90 秒不增长**。而 ffmpeg 崩溃后是**同一秒**就被拉起来的，
文件几乎不间断，`stalled` 永远不会置位 ⇒ 页面上连一个像素都不会变。

### 6.2 企微：录制链路根本没接告警

`monitor.py` 里 `notify.push_by_config` 只有 4 个调用点：

| 调用点 | 用途 |
|---|---|
| `_daily_report` | 每日质检日报 |
| `issue_feedback_loop` | 转录端问题反馈（error 级） |
| `_backfill_rid` | 补号成功 |
| `_gift_push` | 礼物异常告警 |

**`_restart_recording`（崩溃重启）和 `_give_up`（放弃本场 + 冷却）都只有 `log()`。**
最严重的那种 —— 反复崩 4 次后停止本场录制并冷却 10 分钟（主播A 20:29 就发生过一次）——
同样只落一行日志。

### 6.3 崩溃现场被自己覆盖了

`logs/ffmpeg/<主播>.err.log` 每次 `_start_recording` 都以 `"w"` 打开覆盖。崩溃后立刻重启，
**上一个进程的完整 stderr 当场没了**，业务日志里只留 `_log_ffmpeg_exit` 摘的**最后 6 行**。
所以「为什么会 reinit」永远查不到 —— 不是查不出来，是证据被自己删了。

## 7. 为什么画质调度兜不住（这条最关键）

`quality.classify_exit` 对这条错误返回 **`unknown`**：

```
Impossible to convert ... 'Parsed_scale_vt_0' ... auto_scale_0
→ 不含 _EXTERNAL_MARKS,也不含 _RESOURCE_MARKS 里的 "filter graph"/"conversion failed"/"videotoolbox"
→ unknown
```

而 `_crash_counts` 只认 `resource` 类。于是 **86 次崩溃没有一次参与降档判定** ——
`/api/status.quality` 现在仍是 `ARMED`、`switches_today = 0`，就是这个原因。

更麻烦的是：**即使归类修对了也照样救不了**。降档只改 `(分辨率, 码率)`，缩放后端仍是
`scale_vt` —— 同一个缺陷会继续触发。真正有效的兜底是**换缩放后端**（已实施，见 §8.1），
不是降档、也不是换编码器。

## 8. 修复与建议

### 8.1 已实施（2026-10-29）

| 改动 | 位置 | 说明 |
|---|---|---|
| **缩放改 CPU `scale`** | `quality.SCALE_VT_ENABLED=False`、`quality.vf_filter`、`monitor._build_ffmpeg_args` | 去掉 `-hwaccel_output_format videotoolbox_vld` 与 `scale_vt`；**硬解硬编都保留**，只是缩放落到 CPU |
| 预检不再强制要求 `scale_vt` | `preflight.probe_hw_encoder` | 老逻辑「缺 scale_vt 就让整机退回流复制」会把能硬编的机器白白降级 |
| 6 项守卫 | `tests/test_vt_args.py::TestScaleVtDisabled` 等 | 把 `SCALE_VT_ENABLED` 改回 True 立刻报红（已做破坏性验证） |
| 端到端验证脚本 | `scripts/verify_scale_reinit.py` | 造抖动流跑**真实参数** → 必须录完 270 帧；并带对照组：把 `scale_vt` 拼回去必须崩，否则说明验证环境已变、结论要重新确认 |

**代价**（本机 M4 实测，1080×1920@30、1200k、输出到 null）：

| 路线 | CPU |
|---|---|
| 硬解 + GPU `scale_vt`（旧） | **0.040 核/路** |
| 硬解 + CPU `scale`（新） | **0.213 核/路**（+0.17） |

4 路并发 = +0.7 核，`max_hw_streams=8` 全满 = +1.4 核。M4 是 10 核（4P+6E），录制时段
load ~3，可承受。**编码器、码率、分辨率档位全没变，产物参数完全一致**，不属于「产物
变更」，不需要 `douyin notice`；唯一差异是缩放算法（VT 硬件缩放 → swscale bicubic），
对 1088→1080 这种微缩肉眼无差。

### 8.2 仍未做（按性价比）

| # | 措施 | 成本 | 效果 |
|---|---|---|---|
| 1 | **崩溃现场留 3 份轮转**：崩溃时把 `err.log` 另存 `.crash-<ts>.log` | 十几行 | 这次靠合成流复现才拿到证据；下次任何新崩溃都会缺现场 |
| 2 | **`classify_exit` 增加 `vt_reinit` 类** | 几行 + 测试 | 面板能区分「滤镜缺陷」与「网络问题」 |
| 3 | **前端「本场重启 N 次」胶囊**（不依赖 `stalled`） | 小 | 崩溃即时可见 |
| 4 | **崩溃告警**：同场累计 ≥3 次或 `_give_up` 推企微 | 中等 | 补上现在完全空白的感知面 |

> 原建议里的「同场崩 ≥2 次就让该路下次重启走 `sw`」**已不需要**：根因不是资源，
> 降档/换编码器救不了；真正有效的是换缩放后端，且已全局生效。

---

## 附：复现本文所有数字的命令

```bash
cd ~/Downloads/douyin-monitor
# 崩溃分类计数（86/15/247）
.venv/bin/python - <<'PY'
import glob,re,collections
pat=re.compile(r'\[录制\] (\S+) ffmpeg 最后输出: (.*)')
c=collections.Counter()
for f in glob.glob('logs/monitor-*.log'):
    for l in open(f,encoding='utf-8',errors='replace'):
        m=pat.search(l)
        if m: c['scale_vt' if 'scale_vt' in m.group(2) else
               ('stream_broken' if 'missing picture' in m.group(2) else 'other')]+=1
print(c)
PY
# 真实并发峰值（4 路，不是 10 路）
.venv/bin/python -c "
import json;h=json.load(open('recordings/history.json'));es=h if isinstance(h,list) else h['entries']
ev=sorted([(e['started_at'],1) for e in es if e.get('ended_at')]+[(e['ended_at'],-1) for e in es if e.get('ended_at')])
c=p=0
for _,d in ev: c+=d; p=max(p,c)
print('峰值并发',p)"
```
