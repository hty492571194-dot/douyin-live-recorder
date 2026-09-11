#!/usr/bin/env bash
# 机械验收门:bash scripts/verify.sh <phase>  退出码 0 = 通过
# 用法:bash scripts/verify.sh a|b|c|d
# 说明:脚本必须幂等;冒烟用随机端口,不依赖 8780;后续阶段累积执行前面所有检查(回归保护)。
set -u

PHASE="${1:-a}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

PY="python3"

# 累积执行到哪个阶段(后续阶段包含前面所有检查)
case "$PHASE" in
  a) RUN_A=1; RUN_B=0; RUN_C=0; RUN_D=0 ;;
  b) RUN_A=1; RUN_B=1; RUN_C=0; RUN_D=0 ;;
  c) RUN_A=1; RUN_B=1; RUN_C=1; RUN_D=0 ;;
  d) RUN_A=1; RUN_B=1; RUN_C=1; RUN_D=1 ;;
  *) echo "用法: bash scripts/verify.sh a|b|c|d"; exit 1 ;;
esac

# ── 公共:py_compile 全部 py + unittest 全量 ──
echo "== [公共] py_compile =="
$PY -m py_compile monitor.py schedule.py webui.py detect.py preflight.py nas.py || exit 1

echo "== [公共] unittest discover =="
$PY -m unittest discover tests || exit 1

# ── 冒烟:后台启动 monitor.py → 就绪等待 → curl 断言 → kill ──
PORT="${DOUYIN_SMOKE_PORT:-8780}"
if lsof -ti tcp:${PORT} >/dev/null 2>&1; then
  PORT=$((20000 + RANDOM % 20000))
fi
BASE_URL="http://127.0.0.1:${PORT}"

echo "== [冒烟] 启动 monitor.py (port=${PORT}) =="
DOUYIN_MONITOR_PORT=$PORT $PY monitor.py > /tmp/douyin-monitor-smoke.log 2>&1 &
PID=$!
cleanup() { kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; }
trap cleanup EXIT

ready=0
for _ in $(seq 1 40); do
  if curl -s "$BASE_URL/api/status" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" != "1" ]; then
  echo "FAIL: monitor 未在 20s 内就绪"
  cat /tmp/douyin-monitor-smoke.log
  exit 1
fi

# --- phase-a ---
if [ "$RUN_A" = "1" ]; then
echo "== [phase-a] POST 坏配置 → 400 =="
code=$(curl -s -o /tmp/douyin-smoke-resp.json -w '%{http_code}' -X POST "$BASE_URL/api/config" -d '{"schedule":{"cold_interval":"x"}}')
if [ "$code" != "400" ]; then
  echo "FAIL: 期望 400,得到 $code"
  cat /tmp/douyin-smoke-resp.json
  exit 1
fi

echo "== [phase-a] /api/status 含 commands 键 =="
curl -s "$BASE_URL/api/status" | $PY -c "import sys,json; d=json.load(sys.stdin); assert 'commands' in d, '缺少 commands 键'" || exit 1

echo "== [phase-a] config 损坏自愈 =="
if ! $PY - <<'PY'
import json, os, tempfile, monitor
d = tempfile.mkdtemp()
p = os.path.join(d, "config.json")
with open(p, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法的 JSON")
monitor.CONFIG_PATH = p
cfg = monitor.load_config()
assert isinstance(cfg, dict), "load_config 未返回 dict"
assert "schedule" in cfg, "默认配置缺失 schedule"
backups = [f for f in os.listdir(d) if f.startswith("config.json.corrupt-")]
assert backups, "未生成 .corrupt 备份"
print("  自愈 OK,备份:", backups[0])
PY
then
  echo "FAIL: config 损坏自愈"
  exit 1
fi
fi  # --- phase-a ---

# --- phase-b ---
if [ "$RUN_B" = "1" ]; then
echo "== [phase-b] /api/preflight 结构符合契约 =="
curl -s "$BASE_URL/api/preflight" | $PY -c "
import sys, json
d = json.load(sys.stdin)
assert isinstance(d, list), 'preflight 应为列表'
for x in d:
    assert {'id','title','status','detail','action'} <= set(x), f'缺少字段: {x}'
    assert x['status'] in ('ok','warn','fail'), f'非法 status: {x}'
print('  preflight 结构 OK, 共', len(d), '项')
" || exit 1

echo "== [phase-b] {BASE} 占位符替换 =="
if ! $PY - <<'PY'
import os, tempfile, time, monitor
from webui import State
d = tempfile.mkdtemp()
state = State(os.path.join(d, "c.json"), monitor.DEFAULT_CONFIG, None)
out = os.path.join(d, "out.txt")
monitor.run_command(state, f"echo '{{BASE}}' > {out}", {"name": "t"})
time.sleep(0.4)
monitor.poll_commands(state)
got = open(out, encoding="utf-8").read().strip()
assert got == monitor.BASE, f"{{BASE}} 替换失败: {got!r} != {monitor.BASE!r}"
print("  {BASE} 替换 OK =", got)
PY
then
  echo "FAIL: {BASE} 占位符替换"
  exit 1
fi

echo "== [phase-b] setup.sh 冒烟(/tmp 新目录) =="
TMP=$(mktemp -d)
rsync -a --exclude '.venv' --exclude 'logs' --exclude '__pycache__' --exclude '.git' --exclude 'spool' --exclude 'dist' "$HERE/" "$TMP/douyin-monitor/"
( cd "$TMP/douyin-monitor" && ./setup.sh > /tmp/douyin-setup-smoke.log 2>&1 )
if [ -d "$TMP/douyin-monitor/.venv" ] && [ -f "$TMP/douyin-monitor/config.json" ]; then
  echo "  setup.sh 冒烟 OK(.venv + config.json 已生成)"
else
  echo "FAIL: setup.sh 冒烟失败"
  cat /tmp/douyin-setup-smoke.log
  rm -rf "$TMP"
  exit 1
fi
rm -rf "$TMP"
fi  # --- phase-b ---

# --- phase-c ---
if [ "$RUN_C" = "1" ]; then
echo "== [phase-c] /api/nas 结构断言 =="
curl -s "$BASE_URL/api/nas" | $PY -c "
import sys, json
d = json.load(sys.stdin)
assert {'mounted', 'queue_depth', 'config'} <= set(d), f'缺少字段: {list(d.keys())}'
print('  /api/nas 结构 OK, mounted =', d['mounted'], ', queue_depth =', d['queue_depth'])
" || exit 1

echo "== [phase-c] /api/nas/test 空 host 返回错误 =="
curl -s -X POST "$BASE_URL/api/nas/test" -d '{"host":"","share":"x"}' | $PY -c "
import sys, json
d = json.load(sys.stdin)
assert d['ok'] is False, '空 host 应返回错误'
print('  空 host 错误 OK:', d.get('error'))
" || exit 1
fi  # --- phase-c ---

# --- phase-d ---
if [ "$RUN_D" = "1" ]; then
echo "== [phase-d] pack.sh 打包 =="
bash scripts/pack.sh > /tmp/douyin-pack.log 2>&1 || { echo "FAIL: pack.sh"; cat /tmp/douyin-pack.log; exit 1; }
TARBALL=$(ls dist/*.tar.gz 2>/dev/null | head -1)
[ -n "$TARBALL" ] || { echo "FAIL: 未产出 tarball"; exit 1; }
echo "  tarball: $TARBALL"

echo "== [phase-d] 产物内容断言(排除 .venv/logs/spool/.git) =="
if tar -tzf "$TARBALL" | grep -E '(\.venv/|/logs/|__pycache__|/spool/|\.git/)' | grep -q .; then
  echo "FAIL: 产物含排除项"
  tar -tzf "$TARBALL" | grep -E '(\.venv/|/logs/|__pycache__|/spool/|\.git/)' | head -10
  exit 1
fi
echo "  产物内容干净"

echo "== [phase-d] 解包演练 + 脱敏断言 =="
DRILL=$(mktemp -d)
tar xzf "$TARBALL" -C "$DRILL"
PKGDIR=$(find "$DRILL" -maxdepth 1 -type d -name 'douyin-monitor-*' | head -1)
[ -n "$PKGDIR" ] || { echo "FAIL: 解包后未找到 douyin-monitor-* 目录"; rm -rf "$DRILL"; exit 1; }
# config.example.json 存在且脱敏
[ -f "$PKGDIR/config.example.json" ] || { echo "FAIL: 缺少 config.example.json"; rm -rf "$DRILL"; exit 1; }
$PY -c "
import json
cfg = json.load(open('$PKGDIR/config.example.json', encoding='utf-8'))
assert cfg.get('detection', {}).get('cookie', '') == '', 'cookie 未清空'
assert cfg.get('nas', {}).get('username', '') == '', 'username 未清空'
assert 'nas' in cfg and 'recorder' in cfg, '缺少 nas/recorder 结构'
print('  config.example.json 脱敏 OK')
" || { rm -rf "$DRILL"; exit 1; }
# 原 config.json 不应入包
[ ! -f "$PKGDIR/config.json" ] || { echo "FAIL: 原 config.json 不应入包"; rm -rf "$DRILL"; exit 1; }
echo "  原 config.json 未入包"
# setup.sh 演练
( cd "$PKGDIR" && ./setup.sh > /tmp/douyin-drill.log 2>&1 )
if [ -d "$PKGDIR/.venv" ] && [ -f "$PKGDIR/config.json" ]; then
  echo "  解包演练 OK(.venv + config.json 已生成)"
else
  echo "FAIL: 解包演练失败"
  cat /tmp/douyin-drill.log
  rm -rf "$DRILL"
  exit 1
fi
rm -rf "$DRILL"
fi  # --- phase-d ---

echo "== 全部通过 (phase-${PHASE}) =="
