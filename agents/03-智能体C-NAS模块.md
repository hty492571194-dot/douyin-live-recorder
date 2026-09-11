# 子任务书 C — NAS 归档模块（阶段 C）

> 你是 NAS/文件系统智能体。只允许读：本文件、`agents/00-接口契约.md`、`webui.py`、`static/index.html`、`monitor.py` 的下播事件处理行段（grep `on_offline_command` 定位）。
> 预计上下文消耗：~70k tokens。基于 `phase-b` 开工，完成打 `phase-c`。**不依赖真实 NAS**，用测试桩验证。

## 任务

**C1 nas.py 挂载管理**：
- config merge 进 `nas`/`recorder` 段（契约 §4 默认值，容忍缺失）。
- `ensure_mount(cfg) -> bool`：① 探针文件写删验证已挂载可写 → True；② `mount` 输出含挂载点但探针失败（僵尸）→ `umount -f` 后重挂；③ 未挂载 → `mkdir -p` + `mount_smbfs //user:pass@host/share <mount_point>`（列表参数、15s 超时、密码不进日志）。
  - **密码传递安全**：URL 内嵌密码会短暂暴露在进程列表。实现时优先写临时 `~/Library/Preferences/nsmbrc`（0600 权限、用后删除）传递凭据；实现受限时允许 URL 方式，但须在代码注释标注此残余风险（单用户 Mac 影响可控）。
- `get_password()`：`security find-generic-password -s douyin-nas -w`，失败返回 ""。注意：**首次调用会弹一次钥匙串授权 GUI**（属预期，用户在场配置时点「始终允许」即可，此后 launchd 无人值守读取不再弹框）；读取失败的告警需提示用户这一原因。
- `nas_watch(state)` 协程（monitor 主循环接入）：**`nas.enabled=false` 时只做 300s 空转休眠，不执行任何挂载探测**；启用后 60s 周期 ensure_mount；失败指数退避 1→10min，成功复位；首次成功按 monitors 初始化 `<mount>/<root_dir>/<主播名>/`；状态写 State 供 `/api/status` 的 `nas` 键（契约 §3）。
- **测试钩子**：模块级 `DRY_RUN = False`；为 True 时 ensure_mount 用本地目录模拟成功/失败（单测用），生产路径不受影响。

**C2 归档队列**：
- `spool/pending.json`（原子写）：`[{id, streamer, src_path, added_at, retries}]`。
- 下播事件：`nas.enabled` 时把 `recorder.output_dir`（空串解析为 `{BASE}/recordings`）入队 `enqueue()`；`on_offline_command` 仍照常执行（逃生通道）。ctx 增加 `output_dir`。
- `nas_watch` 挂载健康时消化队列：**跨文件系统不能直接 mv**——先 `shutil.copy2` 到 NAS 目标 `<文件名>.part`，校验大小一致后 `os.rename` 为正式名，再删除本地源；任一步失败清理 `.part` 并 retries+1。搬运成功后写同名 `.meta.json`（streamer/room_id/时间）；retries ≥5 标 failed 告警。启动时加载 pending.json 续传。
- `queue_status() -> {depth, failed, last_archive}`。

**C3 API + 前端面板**：
- webui.py：`GET /api/nas`（配置脱敏：密码字段恒为空串）、`POST /api/nas`（host/share 非空、port 1-65535、密码非空时 `security add-generic-password -U -s douyin-nas -w` 入钥匙串且 config 只存 password_ref）、`POST /api/nas/test`（用提交参数临时目录挂载→写探针→读回→卸载，返回 `{ok, latency_ms, error}`，DRY_RUN 下模拟）。
- static/index.html：侧边栏加「NAS 归档」nav-item + 新 view（复用 .card/.field/.btn/.toast）：连接表单 + 测试连接按钮（显示 ok/延迟或错误）+ 保存并启用；状态卡（挂载状态/队列深度/最近归档，3s 轮询 `/api/nas`）。密码输入框 type=password，保存后不回显。
- 同步更新 `preflight.py` 第 4 项检查的 action 文案：由「待阶段C」改为「点击前往 NAS 归档页配置」。

**C4 单测**：`tests/test_nas.py`（DRY_RUN + 临时目录）覆盖：入队→搬运成功→队列清空；入队→失败→retries 递增→≥5 failed；启动加载 pending 续传；test 接口对空 host 返回错误。

## 验收（打 phase-c 前全部满足）

**先在 `scripts/verify.sh` 追加 `# --- phase-c ---` 检查块**（DRY_RUN 冒烟下 /api/nas 结构断言、空 host test 返回错误、NAS 单测全量），然后统一以 `bash scripts/verify.sh c` 退出码 0 为准。

```bash
python3 -m py_compile nas.py monitor.py webui.py
python3 -m unittest discover tests          # 含 A 阶段用例全绿
# 冒烟（DRY_RUN 启动）：
curl -s http://127.0.0.1:8780/api/nas | python3 -c "import sys,json; d=json.load(sys.stdin); assert {'mounted','queue_depth'} <= set(d)"
curl -s -X POST http://127.0.0.1:8780/api/nas/test -d '{"host":"","share":"x"}'   # 明确错误
```

## 禁止

改 detect/schedule/preflight 逻辑；真实网络挂载测试（一律 DRY_RUN）；把密码写入任何文件或日志。
