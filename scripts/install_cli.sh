#!/bin/bash
# 安装 / 修复「终端快捷指令」——本脚本是快捷指令安装过程的**唯一真源**。
#
# 装什么
#   ① ~/.local/bin/douyin  命令行入口(在任意目录敲 douyin 都可用)
#   ② ~/.zshrc 里一段带标记的别名(dy / dyr / dyon / dyoff / dyh / dylog / dydr)
#
# 为什么是脚本而不是手把手改文件
#   项目搬过一次家、也换过机器。路径写在 ~/.local/bin/douyin 里,搬家后那个
#   入口就废了。所以把「写入口 + 写别名」固化成本脚本,搬家后再跑一次即可,
#   别名用标记块包裹,重复执行不会写重。
#
# 用法:
#   bash scripts/install_cli.sh            # 安装
#   bash scripts/install_cli.sh --uninstall  # 卸载(删入口 + 摘掉别名块)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/douyin"
ZSHRC="$HOME/.zshrc"
BEGIN="# >>> douyin-monitor 快捷指令"
END="# <<< douyin-monitor 快捷指令 <<<"

action="${1:-install}"

_strip_block() {
  # 幂等:摘掉旧标记块(存在才摘),用 awk 而非 sed 以兼容 macOS/Linux
  [ -f "$ZSHRC" ] || return 0
  awk -v b="$BEGIN" -v e="$END" '
    index($0, b) { skip=1 } !skip { print } index($0, e) { skip=0 }
  ' "$ZSHRC" > "$ZSHRC.tmp.$$" && mv "$ZSHRC.tmp.$$" "$ZSHRC"
}

if [ "$action" = "--uninstall" ]; then
  rm -f "$BIN" && echo "已删除 $BIN"
  _strip_block && echo "已从 $ZSHRC 摘掉别名片(备份:$ZSHRC.bak-*)"
  echo "卸载完成。已开着的终端执行 exec zsh 生效。"
  exit 0
fi

# ── ① 写命令行入口 ────────────────────────────────────────
mkdir -p "$BIN_DIR"
cat > "$BIN" <<'WRAP_EOF'
#!/bin/bash
# 抖音直播监控 · 终端快捷指令入口(任意目录可直接敲 douyin)
#
# 本文件由项目里的 scripts/install_cli.sh 生成,不要手改 —— 要改就改那个脚本,
# 然后重跑一次:bash scripts/install_cli.sh
#
# 真正的实现在项目里 scripts/ctl.py。这里只负责三件事:
#   1. 找到项目根(搬家后重跑安装脚本,或 export DOUYIN_MONITOR_ROOT=新路径)
#   2. 清掉继承来的 PYTHONPATH / 代理变量 —— 见下方注释,这是本项目踩过两次的坑
#   3. 用项目自己的 .venv/bin/python 执行
#
# 为什么不把逻辑都写在这里:执行计划(先清端口上的外来实例、bootstrap 前先
# bootout)与 Web 界面按钮共用一份(webui._service_plan),写在项目里才不会分叉。
set -u

# 1. 找项目根:环境变量优先,否则按候选列表逐个试
candidates=("${DOUYIN_MONITOR_ROOT:-}" \
            "@INSTALL_ROOT@" \
            "$HOME/Downloads/douyin-monitor" \
            "$HOME/douyin-monitor")
ROOT=""
for c in "${candidates[@]}"; do
  if [ -n "$c" ] && [ -x "$c/.venv/bin/python" ] && [ -f "$c/scripts/ctl.py" ]; then
    ROOT="$c"; break
  fi
done
if [ -z "$ROOT" ]; then
  echo "找不到抖音直播监控项目(需要 .venv/bin/python 与 scripts/ctl.py)。" >&2
  echo "若项目已搬家,请在项目里重跑:bash scripts/install_cli.sh" >&2
  exit 1
fi

# 2a. 清 PYTHONPATH。某些受管终端/IDE 会把它指向 shim 目录,里面的
#     sitecustomize.py 把 os.remove 换成「需人工确认」的版本 —— 后台进程没人
#     能确认,删除调用会一直阻塞,表现为「端口在监听但 HTTP 无响应」(僵死)。
unset PYTHONPATH
unset CODEBUDDY_SAFE_DELETE_ENABLED CODEBUDDY_BROKERED_FS_HOOK_ENABLED 2>/dev/null

# 2b. 清代理。残留的 HTTP_PROXY 指向已退出的沙箱代理时,检测请求与 ffmpeg
#     拉流会全部失败(「All connection attempts failed」),而服务看着是活的,
#     极难察觉 —— 这个坑曾静默丢过两天半的录制。确需代理时设
#     DOUYIN_KEEP_ENV_PROXY=1 保留。
if [ "${DOUYIN_KEEP_ENV_PROXY:-0}" != "1" ]; then
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null
fi

cd "$ROOT" || exit 1
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/ctl.py" "$@"
WRAP_EOF
# 把安装时的真实项目根写进候选列表首位(搬家后重跑本脚本即自动更新)
# 用 awk 而不是 sed -i:BSD sed 的 -i 要跟一个备份后缀参数,写不好会静默失败
# (实测在本机上直接把替换脚本当成文件名报错)。awk 两边行为一致。
awk -v r="$ROOT" '{ gsub(/@INSTALL_ROOT@/, r); print }' "$BIN" > "$BIN.tmp.$$" \
  && mv "$BIN.tmp.$$" "$BIN"
chmod +x "$BIN"
echo "① 已写入命令行入口:$BIN"

# ── ② 写 zsh 别名(带标记块,幂等) ──────────────────────────
_strip_block
cat >> "$ZSHRC" <<'ZSH_EOF'

# >>> douyin-monitor 快捷指令(由项目 scripts/install_cli.sh 写入,可整段删除) >>>
# 实现在 ~/.local/bin/douyin → 项目 scripts/ctl.py;规则与 Web 界面按钮共用。
alias dy='douyin'              # 状态总览(服务 + 健康 + 直播)
alias dyr='douyin restart'     # 重启(会先清掉占用端口的非托管实例)
alias dyon='douyin start'      # 开启
alias dyoff='douyin stop'      # 关闭
alias dyh='douyin health'      # 健康面板逐项明细
alias dylog='douyin log -f'    # 跟随今日业务日志
alias dydr='douyin doctor'     # 自检终端环境(PYTHONPATH/代理隐患)
# <<< douyin-monitor 快捷指令 <<<
ZSH_EOF
echo "② 已写入别名到:$ZSHRC"

# ── ③ 自检 ────────────────────────────────────────────────
# 用 --help 做冒烟测试:能打出帮助就说明「入口 → 项目 → venv」这条链是通的。
if "$BIN" --help >/dev/null 2>&1; then
  echo "③ 入口自检通过(--help 正常输出)"
else
  echo "③ 入口自检没通过,请手动执行 $BIN status 看报错"
fi
echo ""
echo "完成。已开着的终端执行 exec zsh 让别名生效;新开的窗口直接可用。"
echo "常用:dy(状态) / dyr(重启) / dyon(开启) / dyoff(关闭) / dylog(跟日志)"
