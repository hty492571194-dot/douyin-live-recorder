#!/usr/bin/env bash
# 一键部署 / 重新装机:检测 python → 建 venv → 装依赖 →(可选)注册 launchd。
#
# 用法:
#   ./setup.sh                      # 普通部署(建 venv + 装依赖)
#   ./setup.sh --launchd            # 部署并注册开机自启(按当前路径生成 plist)
#   ./setup.sh --reinstall-plist    # 只重新生成并注册 plist(项目搬家用这个)
#   ./setup.sh --uninstall          # 卸载 launchd 服务(不删任何数据)
#   ./setup.sh --slim-export ~/Desktop/douyin-monitor-slim
#                                   # 导出「不含录制视频/字幕」的瘦身副本,用于拷贝到其他电脑
#
# 这个脚本里没有任何硬编码的绝对路径 —— 全部由 HERE(脚本所在目录)推导,
# 所以整个项目目录拷到任意位置 / 任意 Mac 上,跑一遍本脚本即可直接运行。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
LABEL="com.douyin.monitor"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

log() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }

# ── 生成 plist(路径全部用当前实际位置) ────────────────────────────────────
gen_plist() {
  mkdir -p logs "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${HERE}/.venv/bin/python</string>
    <string>${HERE}/monitor.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${HERE}</string>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <!-- 崩溃重启的最小间隔。默认 10 秒:端口被占时会变成每 10 秒 spawn 一次又立刻
       退出,把 err.log 刷到上百 MB、业务日志全被淹没。放宽到 60 秒仍能自动接管。 -->
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <!-- SIGTERM 后的收尾时间:要停掉所有 ffmpeg 并落历史,默认 20 秒偏紧 -->
  <key>ExitTimeOut</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${HERE}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${HERE}/logs/launchd.err.log</string>
</dict>
</plist>
EOF
  echo "已生成 $PLIST"
}

svc_bootout() { launchctl bootout "gui/${UID_N}/${LABEL}" 2>/dev/null || true; }
svc_start() {
  launchctl bootstrap "gui/${UID_N}" "$PLIST" 2>/dev/null \
    || launchctl kickstart -k "gui/${UID_N}/${LABEL}" 2>/dev/null || true
}

# ── 卸载 ──────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
  log "卸载 launchd 服务 ${LABEL}(数据不删)"
  svc_bootout
  rm -f "$PLIST"
  echo "已卸载。重新启动服务: ./setup.sh --launchd"
  exit 0
fi

# ── 瘦身导出 ──────────────────────────────────────────────────────────────
if [ "${1:-}" = "--slim-export" ]; then
  DEST="${2:-}"
  if [ -z "$DEST" ]; then echo "用法: ./setup.sh --slim-export <目标目录>"; exit 1; fi
  log "导出瘦身副本 → $DEST"
  mkdir -p "$DEST"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
      --exclude '.DS_Store' --exclude 'auth/profile/' \
      --exclude 'recordings/*/' --exclude 'recordings_backup_*/' \
      --exclude 'ass_backup_*/' \
      ./ "$DEST/"
  else
    warn "没找到 rsync,改用 cp(会带上录制大文件,建议先装 rsync)"
    cp -R . "$DEST/"
  fi
  # recordings/*/ 被整个排掉了,但 history.json 是数据不是视频,必须带上
  if [ -f recordings/history.json ]; then
    mkdir -p "$DEST/recordings"
    cp recordings/history.json "$DEST/recordings/"
  fi
  echo "导出完成。到目标目录后执行: cd $DEST && ./setup.sh --launchd"
  exit 0
fi

# ── 只重装 plist ──────────────────────────────────────────────────────────
if [ "${1:-}" = "--reinstall-plist" ]; then
  log "按当前路径 ${HERE} 重新注册服务"
  svc_bootout
  gen_plist
  svc_start
  sleep 2
  if curl -s --max-time 3 -o /dev/null "http://127.0.0.1:8780/api/status"; then
    echo "服务已就绪: http://127.0.0.1:8780"
  else
    warn "服务尚未响应,查看日志: tail -f logs/launchd.err.log"
  fi
  exit 0
fi

# ── ① 检测 python3 >= 3.11 ────────────────────────────────────────────────
PYBIN=""
for c in python3 python3.13 python3.12 python3.11; do
  if command -v "$c" >/dev/null 2>&1 \
     && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PYBIN="$c"; break
  fi
done
if [ -z "$PYBIN" ]; then
  echo "错误: 需要 Python >= 3.11。请先安装 Python 3.11+" >&2
  exit 1
fi
log "使用 $PYBIN ($("$PYBIN" --version 2>&1))"

# ── ② 建 venv + 装依赖 ────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  log "创建虚拟环境 .venv"
  "$PYBIN" -m venv .venv
fi
log "安装依赖 requirements.txt"
if ! .venv/bin/pip install -q -r requirements.txt; then
  warn "依赖安装失败(可能无网络)。稍后手动执行: .venv/bin/pip install -r requirements.txt"
fi
# 扫码登录需要 chromium;体积较大,失败不阻断部署
if grep -q playwright requirements.txt 2>/dev/null; then
  log "安装 playwright chromium(扫码登录用,体积较大)"
  .venv/bin/playwright install chromium >/dev/null 2>&1 \
    || warn "chromium 安装失败,扫码登录将不可用;可稍后执行: .venv/bin/playwright install chromium"
fi

# ── ③ 首次运行生成默认 config ─────────────────────────────────────────────
if [ ! -f config.json ]; then
  log "生成默认 config.json"
  .venv/bin/python -c "import monitor; monitor.load_config()" >/dev/null 2>&1 || true
fi

# ── ⑤ --launchd 注册服务 ──────────────────────────────────────────────────
if [ "${1:-}" = "--launchd" ]; then
  log "注册 launchd 开机自启"
  svc_bootout
  gen_plist
  svc_start
  sleep 2
  if curl -s --max-time 3 -o /dev/null "http://127.0.0.1:8780/api/status"; then
    echo "服务已就绪: http://127.0.0.1:8780"
  else
    warn "服务尚未响应,查看日志: tail -f logs/launchd.err.log"
  fi
fi

# ── ⑥ 换机检查清单 ────────────────────────────────────────────────────────
cat <<'EOF'

换机 / 搬家后还有 3 件事不随文件夹走,需手动确认:
  1. 登录态   — 钥匙串里的 Cookie 不会跟着拷。打开 Web 控制台,必要时重新扫码登录
  2. NAS 密码 — 钥匙串里的 NAS 密码同理,在「归档设置」里重填一次
  3. NAS 挂载 — 保证 SMB 仍挂载在 ~/DouyinArchive(归档设置里的挂载点可改)
若 history.json 仍指向旧目录,执行: .venv/bin/python migrate.py --apply
EOF
echo "下一步: 打开 http://127.0.0.1:8780"
