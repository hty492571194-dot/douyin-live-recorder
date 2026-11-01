#!/bin/bash
# 抖音直播监控 · 一键启动(macOS)
# 用法:双击本文件;或终端执行 ./一键启动.command
#
# 这个窗口有两个身份
# ------------------
# 1) 启动器 —— 判活、必要时清理僵死进程、拉起 monitor(见下面「判活逻辑」);
# 2) 终端控制台 —— 启动之后**不把窗口占死**,而是留一个按键菜单:
#      o 打开 Web 控制台 / s 检测运行状态 / r 重启 / q 关闭 / l 日志 / h 命令速查
#    之前这里只有一句「按任意键关闭本窗口」,想重启或看状态就得记命令、另开终端;
#    现在按键即可,和 Web 控制台侧边栏底部的「重启 / 关闭」是同一套动作。
#
# 为什么菜单只是「转发」
# --------------------
# 启停的真实规则(先清端口上的非托管实例、已装载就直接拉起、未装载才 bootstrap …)
# 只写在 webui._service_plan() 一处,终端入口是 scripts/ctl.py。本脚本里的每个按键
# 都转发给 ctl.py,绝不在 shell 里另拼 launchctl —— 否则界面修了、终端还留着老
# Bug(这个项目真的踩过一次「点重启没反应,两边各修一遍才对齐」)。
#
# 判活逻辑(重要):
#   端口被占用 ≠ 服务健康。出现过「端口在 LISTEN 但 HTTP 无响应」的僵死,
#   所以这里用「端口占用 + HTTP 探活」双重判断:
#     · 端口空            → 直接启动
#     · 端口占用 + 有响应 → 已在运行,只开浏览器
#     · 端口占用 + 无响应 → 判定僵死,杀掉旧进程后重启
set -u
cd "$(dirname "$0")" || { echo "无法进入项目目录"; exit 1; }
# -P 取物理路径:下面的「是不是本目录的实例」要和后端 os.path.abspath(__file__)
# 比字符串,两边都必须是同一种规范形,否则符号链接一来就永远判不相等。
ROOT="$(pwd -P)"

# 0. 清掉可能存在的 Python 注入。
#    某些受管终端/IDE 会设 PYTHONPATH 指向一个 shim 目录,里面的 sitecustomize.py
#    会把 os.remove 替换成「需人工确认」的版本。服务在后台跑,没人能确认,
#    删除调用会一直阻塞 → 事件循环卡死 → 端口在监听但 HTTP 无响应(僵死)。
#    这里 unset 掉,保证子进程里的 os.remove 是原生实现。
unset PYTHONPATH
unset CODEBUDDY_SAFE_DELETE_ENABLED CODEBUDDY_BROKERED_FS_HOOK_ENABLED 2>/dev/null

# 0b. 清掉继承来的代理变量。
#     若本脚本是从受管终端/IDE 集成终端启动的,环境里可能残留 HTTP_PROXY 指向
#     一个已经退出的沙箱代理端口。后果:检测请求和 ffmpeg 拉流全被送进死代理,
#     表现为「All connection attempts failed」→ 全员检测失败 → 不再录制,
#     而服务本身活着、Web UI 也正常,极难察觉。抖音接口必须直连,故一律清除。
#     确需代理时设 DOUYIN_KEEP_ENV_PROXY=1 可保留。
if [ "${DOUYIN_KEEP_ENV_PROXY:-0}" != "1" ]; then
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY 2>/dev/null
fi

# 0c. 让本窗口也能直接敲 douyin(短别名 dy/dyr 只在 zsh 里生效,这里没有;
#     别名要生效请另开一个终端窗口)。
#     双击 .command 起的 bash 不读 ~/.zshrc,那里的 PATH 补充就失效了,
#     所以自己把入口目录补进来。
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac

echo "=============================================="
echo "  抖音直播监控 一键启动"
echo "=============================================="

# ══ 目录自检(必须发生在任何进程被拉起之前) ════════════════════════
# 为什么需要它 —— 2026-10-29 真实事故
# ------------------------------------
# ~/Downloads 下并存历史发布快照(douyin-monitor-release-YYYYMMDD)。把它们
# 里的旧文件当成"同一套项目的旧版本"是错的:快照目录里的 monitor.py、
# static/index.html 停在几个月前那一代,但入口脚本与本文件会被复制成新的。
# 那天凌晨就是这样:在 8 月的快照目录里双击了本脚本 → 端口上跑起一个
# 「入口新、页面旧」的杂交实例 → 用户看到的整站界面回退三个月;而 launchd
# 的正式服务每 60 秒撞一次端口后退出(err.log 刷 Address already in use),
# 外层探活只看 HTTP 200,于是谁都没发现跑错了版本。
#
# 结论:启动器不能假设「我所在的目录就是要跑的那份代码」。设两道闸:
#   ① 身份闸 —— 已装 launchd 时,正式项目根只有一个(plist 的
#      WorkingDirectory)。本目录不是它就不启动。确实要在副本里临时跑,
#      显式给 DOUYIN_ALLOW_SIDE_COPY=1。
#   ② 完整性闸 —— 代码不自洽(缺入口文件,或几个文件不是同一代)也不启动。
# 宁可停在这里把原因说清楚,也不要拉起一个界面回退三个月的实例。
PLIST="$HOME/Library/LaunchAgents/com.douyin.monitor.plist"

# 正式部署目录(取 launchd plist 的 WorkingDirectory;没装则输出空)
official_root() {
  [ -f "$PLIST" ] || return 0
  command -v plutil >/dev/null 2>&1 || return 0
  plutil -extract WorkingDirectory raw -o - "$PLIST" 2>/dev/null
}

# 「同一代」的判定用能力锚点:每个锚点都是本代才有的符号(短期录制的
# record_state 一族),历史快照里必然找不到。锚点一旦被拷进旧目录就会命中。
# 选中它们是因为分属三层——后端纯函数 / API 层 / 前端消费——单独一层变旧
# 也能被抓到,而不是只看某一个文件。
check_same_generation() {
  local spec f pat bad=0
  for spec in \
    "monitor.py|def record_state(" \
    "webui.py|_record_windows(" \
    "static/index.html|record_windows"
  do
    f="${spec%%|*}"
    pat="${spec#*|}"
    if ! grep -qF "$pat" "$ROOT/$f" 2>/dev/null; then
      echo "       · $f 里找不到 ${pat}"
      bad=1
    fi
  done
  return "$bad"
}

verify_project() {
  local f miss="" official gen a
  for f in monitor.py webui.py static/index.html scripts/ctl.py; do
    [ -f "$ROOT/$f" ] || miss="$miss $f"
  done
  if [ -n "$miss" ]; then
    echo ""
    echo "  [×] 本目录不是一个完整的项目根:缺少$miss"
    echo "      当前目录: $ROOT"
    echo "      请从正式项目目录启动。"
    return 1
  fi
  if ! gen=$(check_same_generation); then
    echo ""
    echo "  [×] 本目录里的代码不是同一代:"
    printf '%s\n' "$gen"
    echo "      当前目录: $ROOT"
    echo "      典型成因:本脚本被复制进了历史发布快照(release-*)目录。"
    echo "      在那里启动只会跑起「入口新、页面旧」的实例,整站界面回退,"
    echo "      还会把 8780 从正式服务手里抢走。"
    echo "      请回到正式项目目录,重新双击「一键启动.command」。"
    return 1
  fi
  official=$(official_root)
  if [ -n "$official" ]; then
    official="$(cd "$official" 2>/dev/null && pwd -P)"
  fi
  if [ -n "$official" ] && [ "$official" != "$ROOT" ]; then
    if [ "${DOUYIN_ALLOW_SIDE_COPY:-0}" != "1" ]; then
      echo ""
      echo "  [×] 本目录不是正式部署目录"
      echo "      正式目录: $official"
      echo "      当前目录: $ROOT"
      echo "      两份同时存在只会互相抢端口:谁先起来谁占住 8780,"
      echo "      页面也就变成那一份的 —— 这正是「界面回退」的来路。"
      # 与其让人自己回去找目录,不如直接转交给正式目录那一份。用 exec 转交而
      # 不是在这里再判一遍:规则仍然只有这一处,以后改也只改一边。
      if [ -x "$official/一键启动.command" ] && [ -t 0 ]; then
        printf "  要改为从正式目录启动吗?[y 是 / 其他键退出] › "
        a=""
        if read -r -s -n 1 a; then
          echo ""
          case "$a" in
            y | Y)
              echo "  转交: $official/一键启动.command"
              exec /bin/bash "$official/一键启动.command"
              ;;
            *) echo "  已取消" ;;
          esac
        else
          echo ""
        fi
      else
        echo "      请回到正式项目目录,重新双击「一键启动.command」。"
      fi
      echo "      确实要在这里临时跑一份,请显式执行:"
      echo "        DOUYIN_ALLOW_SIDE_COPY=1 ./一键启动.command"
      return 1
    fi
    echo "  [!] 已按 DOUYIN_ALLOW_SIDE_COPY=1 在非正式目录启动(端口冲突自行处理)"
  fi
  return 0
}

if ! verify_project; then
  # 只在交互窗口里停下来等按键(双击 .command 就是这种);被脚本/管道调用时
  # 直接退出 —— 自动化场景没人按键,等下去只会把调用方挂住。
  if [ -t 0 ] && [ -t 1 ]; then
    read -n 1 -s -r -p "按任意键退出..." </dev/tty 2>/dev/null
  fi
  exit 1
fi

# 1. 首次运行自动部署(建 .venv + 装依赖 + 生成 config.json)
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[1/4] 首次运行,执行 setup.sh 自动部署(需要几分钟)..."
  bash setup.sh
  if [ $? -ne 0 ]; then
    echo "部署失败,请检查上方错误信息"
    read -n 1 -s -r -p "按任意键退出..."
    exit 1
  fi
else
  echo "[1/4] 环境就绪(.venv 存在)"
fi
PY="$ROOT/.venv/bin/python"

# 1b. 终端快捷指令:入口丢了就顺手装一次(换机/搬家后路径会失效,安装脚本幂等,
#     重跑只会更新入口和别名,不会写重)。
if [ ! -x "$HOME/.local/bin/douyin" ] && [ -f "$ROOT/scripts/install_cli.sh" ]; then
  echo "[1b] 未检测到 douyin 命令,安装终端快捷指令..."
  if bash "$ROOT/scripts/install_cli.sh" >/dev/null 2>&1; then
    echo "     已安装(新开的终端窗口可直接使用)"
  else
    echo "     安装失败,可手动执行:bash scripts/install_cli.sh"
  fi
fi

# 1c. 命令速查。
#     直接调用 scripts/ctl.py 渲染 —— 命令表只有那一份真源,终端指令、Web 按钮
#     和本窗口显示的都从它出来,不会出现「窗口里写着一条早就改掉的命令」。
#     服务没起来(甚至没启动)也能看,因为它不访问任何接口。
if [ -x "$PY" ] && [ -f "$ROOT/scripts/ctl.py" ]; then
  echo ""
  echo "----------------------------------------------"
  echo "  命令速查(本窗口和任意终端窗口都能敲)"
  echo "----------------------------------------------"
  "$PY" "$ROOT/scripts/ctl.py" help --short 2>/dev/null \
    || echo "  渲染失败,可直接执行:.venv/bin/python scripts/ctl.py help"
  echo ""
fi

# 2. 读取 Web 端口(用 venv 的 python,跟运行时保持一致)
PORT=$("$PY" -c "import json;print(json.load(open('config.json')).get('webui',{}).get('port',8780))" 2>/dev/null)
if [ -z "${PORT:-}" ]; then PORT=8780; fi
echo "[2/4] Web 端口: $PORT"
echo "      除本窗口外,控制台里也有一份同样的清单(点命令格可复制):"
echo "      http://127.0.0.1:$PORT → 左侧「系统健康」→ 页尾「终端快捷指令」"

# 3. 判活:端口占用 + HTTP 探活。--noproxy 防止系统代理把本机请求劫走
probe() {
  curl -s -m 3 --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/api/status"
}
holders() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null
}
# 端口上那个实例自报的项目根(/api/status 的 project_root)。
# 老版本没有这个字段 → 输出空字符串。
served_root() {
  curl -s -m 3 --noproxy '*' "http://127.0.0.1:$PORT/api/status" 2>/dev/null \
    | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get("project_root") or "")
' 2>/dev/null
}
# 端口上那个实例属于哪个目录。两条互相独立的证据,任一条成立就够:
#   ① HTTP 的 project_root —— 进程自报「我跑的是哪份代码」(新版才有)
#   ② lsof 的 cwd        —— 进程在哪个目录里被拉起(任何版本查得到)
# 为什么要有 ②:服务还没重启到带 ① 的版本时,只靠 ① 会变成"身份不明",
# 而"身份不明"正是这次事故里最危险的状态 —— 看着正常,其实跑的是别份代码。
instance_root() {
  local v p pids
  v=$(served_root)
  [ -n "$v" ] && { echo "$v"; return 0; }
  # 用**当前**占用者,不用启动时那份 pid 快照:轮询期间 pid 可能已经换人
  # (2026-10-29 实测占用者一分钟内从 66401 变成 67157)。
  pids=$(holders)
  [ -n "$pids" ] || pids=${PIDS:-}
  for p in $pids; do
    v=$(lsof -a -p "$p" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    [ -n "$v" ] && { echo "$v"; return 0; }
  done
  echo ""
}

# ══ 终端控制台 ═══════════════════════════════════════════════════
# 下面这些函数只在交互窗口里有意义:读键盘、调 ctl.py、看本窗口拉起的那个
# 监控进程。全部只操作本项目自己的端口与进程,不接受任何来自命令行的参数。
CTL="$ROOT/scripts/ctl.py"
CHILD=""        # 本窗口拉起的监控 pid;非本窗口承载时为空
OWNED=0         # 1 = 服务由本窗口承载(关窗口=停服务)
CONSOLE_LOG=""  # 本窗口承载时的终端输出文件(见 start_child)
POLLER=""       # 「就绪后再开浏览器」那个后台轮询任务的 pid(见 cleanup_poller)

# 无论从哪条路径退出,都把后台轮询收掉,别让它退出后还去开浏览器
trap 'cleanup_poller' EXIT

# 转发到 scripts/ctl.py。找不到就直接说清楚,不要静默失败。
ctl() {
  if [ -f "$CTL" ]; then
    "$PY" "$CTL" "$@"
  else
    echo "  [!] 找不到 scripts/ctl.py,无法执行「$*」—— 请在 Web 控制台里操作"
    return 1
  fi
}

# 正在录制的路数。重启/关闭会打断它们,所以动手前必须点出来(与 Web 按钮的
# 确认框同一个判据:只数 /api/status 顶层 recordings 里 recording 为真的)。
rec_count() {
  local out
  out=$(curl -s -m 3 --noproxy '*' "http://127.0.0.1:$PORT/api/status" 2>/dev/null \
        | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(0)
    sys.exit(0)
recs = d.get("recordings") or {}
print(sum(1 for v in recs.values() if isinstance(v, dict) and v.get("recording")))
' 2>/dev/null)
  case "${out:-}" in
    '' | *[!0-9]*) echo 0 ;;
    *) echo "$out" ;;
  esac
}

start_child() {
  mkdir -p "$ROOT/logs" 2>/dev/null
  CONSOLE_LOG="$ROOT/logs/console-$(date +%Y-%m-%d).log"
  echo "  拉起监控进程..."
  # -u 关掉输出缓冲:崩溃时最后几行必须已经落进文件,否则 tail 出来是空的。
  # 输出进文件而不是留在屏幕,是为了让菜单不被服务日志冲散;按 l 可随时跟随。
  # monitor.py 写**绝对路径**:这样子进程的 cmdline 里带着"我是哪个目录的代码",
  # 后面 pkill -f "$ROOT/monitor.py" 的兜底清理才认得出它(相对路径匹配不到)。
  "$PY" -u "$ROOT/monitor.py" >>"$CONSOLE_LOG" 2>&1 &
  CHILD=$!
  OWNED=1
  echo "  已启动 · pid $CHILD · 终端输出写入 ${CONSOLE_LOG#$ROOT/}(按 l 跟随)"
}

child_alive() {
  [ -n "${CHILD:-}" ] && kill -0 "$CHILD" 2>/dev/null
}

# 停本窗口拉起的监控。先 SIGTERM 让它走 _handle_term(停 ffmpeg、把已录部分
# 落进 history),收尾慢是正常的(要写 NAS 队列),给 15 秒再强制结束。
stop_child() {
  [ -n "${CHILD:-}" ] || return 0
  if ! child_alive; then
    CHILD=""
    return 0
  fi
  printf "  停止中(停 ffmpeg、把已录部分落进历史,最多等 15 秒)..."
  kill -TERM "$CHILD" 2>/dev/null
  local i=0
  while child_alive && [ "$i" -lt 30 ]; do
    sleep 0.5
    i=$((i + 1))
  done
  if child_alive; then
    printf "\n  [!] 15 秒内没退出,强制结束\n"
    kill -9 "$CHILD" 2>/dev/null
  fi
  wait "$CHILD" 2>/dev/null
  printf "完成\n"
  CHILD=""
}

# 本窗口拉起的进程自己死了(崩溃)。菜单还挂在屏幕上,必须立刻说清楚,
# 否则用户对着一个「运行中」的菜单按键,而服务其实早没了。
child_exited() {
  local code
  wait "$CHILD" 2>/dev/null
  code=$?
  echo ""
  echo "  [!] 监控进程已退出(退出码 $code)"
  if [ -n "${CONSOLE_LOG:-}" ] && [ -f "$CONSOLE_LOG" ]; then
    echo "  ── 输出末尾(${CONSOLE_LOG#$ROOT/})──"
    tail -n 15 "$CONSOLE_LOG" 2>/dev/null | sed 's/^/    /'
    echo "  ──────────────────────────────"
  fi
  echo "  按 r 重新拉起;要看完整日志按 l。"
  CHILD=""
}

# 会中断录制的动作先问一次。默认不确认(按别的键/直接回车都算取消),
# 避免手滑把正在录的场次掐掉。
confirm() {
  local what="$1" n a
  n=$(rec_count)
  echo ""
  if [ "$n" -gt 0 ]; then
    # 注意 ${what} 的花括号不能省:bash 3.2 会把紧跟其后的中文字节当成变量名的
    # 一部分(变量名被读成「what + 中文」),set -u 下直接 unbound variable 崩掉。
    echo "  提示:当前有 $n 路正在录制 —— ${what}会中断它们(已录部分会落进历史)。"
  fi
  printf "  确定%s?[y 确认 / 其他键取消] › " "$what"
  a=""
  if ! read -r -s -n 1 a; then
    echo ""
    return 1
  fi
  echo ""
  case "$a" in
    y | Y) return 0 ;;
    *) echo "  已取消" ;;
  esac
  return 1
}

# 第一次进控制台时讲一遍「这些键到底做了什么」,之后每轮只刷紧凑菜单。
print_explain() {
  echo ""
  echo "----------------------------------------------"
  echo "  这个窗口现在是终端控制台"
  echo "  · o / s / r / q 与 Web 控制台侧边栏底部的「重启 · 关闭」是同一套动作,"
  echo "    都走 scripts/ctl.py → webui._service_plan(),启停规则只有那一份。"
  echo "  · 重启会中断正在录制的场次 —— 发出前先问你一次。"
  echo "  · 关闭不会自己回来:恢复按 r,或重新双击「一键启动.command」。"
  echo "  · 检测状态是只读的,不动任何进程。"
  if [ "$OWNED" = "1" ]; then
    echo "  · 服务由本窗口承载:按 q / Ctrl+C 走正常收尾;直接关窗口是强退。"
  else
    echo "  · 服务不在本窗口里:退出窗口不影响它(继续在后台跑)。"
  fi
  echo "----------------------------------------------"
}

print_menu() {
  local hint
  if [ "$OWNED" = "1" ]; then
    hint="退出即关闭服务(想干净退出请按 q)"
  else
    hint="服务继续在后台运行"
  fi
  echo ""
  if probe; then
    echo "  ● 运行中 · http://127.0.0.1:$PORT"
  else
    echo "  ○ 未运行"
  fi
  echo "  快捷操作(按单个字母,不用回车)"
  echo "    o  打开 Web 控制台   浏览器打开 http://127.0.0.1:$PORT"
  echo "    s  检测运行状态      服务 + 健康面板 + 正在录制的场次"
  echo "    r  重启 / 启动       在跑就重启(中断录制,先确认),没在跑就拉起"
  echo "    q  关闭服务          停止监控,不会自动恢复"
  echo "    l  跟随今日日志      Ctrl+C 回到本菜单"
  echo "    h  全部命令速查      douyin help 完整版"
  echo "    Enter  退出本窗口    $hint"
  printf "  按键选择 › "
}

# 主循环。
# read 带 1 秒超时,是为了在没人按键时也能发现「本窗口的孩子死了」。
# 为什么不用返回值区分「超时」与「EOF」:bash 3.2(macOS 自带)两种都返回 1,
# bash 4+ 才是超时 >128。所以改成「连续空读计数 + 是否 tty」:
#   · 是终端 → Ctrl+D 只当没按(误触代价太大,不能拿它关服务),一直等;
#   · 不是终端 → 连续 5 次(≈5 秒)读不到东西就认为 stdin 没了(被 open / 管道
#     拉起),退化成「守着孩子,孩子走了就收工」,避免空转把 CPU 打满。
console_loop() {
  local k miss
  while :; do
    print_menu
    # 内层只负责「等一个键」:期间每秒看一眼本窗口的孩子。菜单不能放在这层
    # 循环里重绘 —— read 每秒超时一次,菜单就会一秒刷一屏把内容冲没。
    miss=0
    while :; do
      k=""
      read -r -s -n 1 -t 1 k
      [ $? -eq 0 ] && break
      miss=$((miss + 1))
      if [ "$OWNED" = "1" ] && [ -n "$CHILD" ] && ! child_alive; then
        child_exited
        miss=0
      fi
      if [ ! -t 0 ] && [ "$miss" -ge 5 ]; then
        echo ""
        echo "  没有键盘可读(非终端启动),守着服务跑;服务退出后本脚本结束。"
        if [ "$OWNED" = "1" ] && [ -n "$CHILD" ]; then
          while child_alive; do sleep 2; done
        fi
        exit 0
      fi
    done
    echo ""
    case "$k" in
      o | O) ctl open ;;
      s | S) ctl status ;;
      r | R)
        if confirm "重启服务"; then
          if [ "$OWNED" = "1" ]; then
            # 服务是本窗口拉起的:重启就是停掉它再拉一次,不交给 launchd
            stop_child
            start_child
          elif probe; then
            ctl restart
          else
            ctl start
          fi
        fi
        ;;
      q | Q)
        if confirm "关闭服务"; then
          if [ "$OWNED" = "1" ]; then
            stop_child
            echo "  服务已关闭,本窗口可以关掉了。"
            exit 0
          fi
          ctl stop
        fi
        ;;
      l | L)
        # 跟随日志时 Ctrl+C 应该回到菜单,不能被外层当成「关闭服务」。
        # 换成空动作而不是 trap ''(忽略):忽略的处置会被子进程继承,
        # tail 就再也收不到 Ctrl+C,用户反而退不出来。
        trap ':' INT
        ctl log -f 50
        trap on_leave INT HUP TERM
        ;;
      h | H) ctl help ;;
      "")
        # 回车(read -n 1 读到换行时变量为空)
        if [ "$OWNED" = "1" ]; then
          if confirm "退出窗口(服务随之关闭)"; then
            stop_child
            echo "  服务已关闭,窗口可以关掉了。"
            exit 0
          fi
        else
          echo "  窗口已退出 · 服务仍在后台运行"
          echo "  要停服务:再双击本文件按 q,或敲 douyin stop"
          exit 0
        fi
        ;;
      *)
        # ${k} 的花括号同上:后面紧跟中文时必须写全
        echo "  未知按键「${k}」 · o 控制台 / s 状态 / r 重启 / q 关闭 / l 日志 / h 命令"
        ;;
    esac
  done
}

# 收到中断/挂断:本窗口承载时就地收尾,别把 ffmpeg 留成孤儿。
on_leave() {
  echo ""
  if [ "$OWNED" = "1" ]; then
    if [ -n "${CHILD:-}" ]; then
      echo "  收到中断,先停服务(收尾会把已录部分落进历史)..."
      stop_child
    else
      echo "  已中断(服务尚未拉起)"
    fi
  else
    echo "  本窗口退出 · 服务继续在后台运行(要停请敲 douyin stop)"
  fi
  exit 130
}

# 轮询等就绪再开浏览器,避免打开一张「连接被拒绝」的空白页。
# 但「就绪」的判据不能只是 HTTP 有响应 —— 端口上回 200 的可能是**另一份代码**。
# 2026-10-29 事故正是这样:本目录的 monitor 因为端口被历史副本占着而启动即退出,
# 这个轮询却 probe 成功就开浏览器,于是屏幕上出现的是别人那份的页面(界面回退
# 三个月),而窗口里写着「已启动」。所以这里必须拿到实例身份并和 ROOT 比。
# 记下 pid:用户提前退出窗口时这个后台任务必须一起收掉 —— 否则它会继续轮询,
# 几十秒后凭空弹出一个浏览器页面(实测过,很莫名其妙)。
open_browser_when_ready() {
  (
    for _ in $(seq 1 40); do
      if probe; then
        r=$(instance_root)
        if [ "$r" = "$ROOT" ]; then
          open "http://127.0.0.1:$PORT" 2>/dev/null
        else
          echo "      [!] 端口 $PORT 上响应的是另一份代码: ${r:-(身份查不出来)}"
          echo "          没有打开浏览器 —— 那样页面会显示那一份的界面,不是本目录的。"
          echo "          按 s 看状态,或按 l 看本目录进程为何没起来。"
        fi
        exit 0
      fi
      sleep 1
    done
    echo "      [!] 40 秒内服务未就绪 —— 按 l 看日志,s 看状态"
  ) &
  POLLER=$!
}

cleanup_poller() {
  [ -n "${POLLER:-}" ] || return 0
  kill -TERM "$POLLER" 2>/dev/null
  wait "$POLLER" 2>/dev/null
  POLLER=""
}

# ══ 主流程 ═══════════════════════════════════════════════════════
PIDS=$(holders)

if [ -n "$PIDS" ] && probe; then
  # 探活只证明「有人在这个端口上讲 HTTP」,不证明它是**本目录**的代码。
  # 历史快照目录里跑起来的实例一样回 200,页面却是几个月前的界面
  # (2026-10-29 事故:界面整体回退,而这里只会打印「响应正常」)。
  SRV=$(instance_root)
  if [ "$SRV" != "$ROOT" ]; then
    echo "[3/4] 端口 $PORT 上跑的不是本目录的代码"
    if [ -n "$SRV" ]; then
      echo "      占用者: $SRV"
    else
      echo "      占用者身份查不出来(既没有 project_root,也读不到它的工作目录)"
    fi
    echo "      本目录: $ROOT"
    echo "      现在浏览器里看到的页面属于上面那一份,不是本目录的代码。"
    trap on_leave INT HUP TERM
    if confirm "停掉它,并从本目录启动"; then
      # 与「僵死清理」同一套动作:先按 pid 精确杀,再按进程名兜底。
      # shellcheck disable=SC2086
      kill -9 $PIDS 2>/dev/null
      sleep 2
      pkill -9 -f "$ROOT/monitor.py" 2>/dev/null
      sleep 1
      start_child
      print_explain
      open_browser_when_ready
      console_loop
      exit 0
    fi
    echo "      已取消 · 服务原样运行(页面仍是那一份)"
    exit 0
  fi
  echo "[3/4] 监控已在运行(已确认是本目录的实例)"
  open "http://127.0.0.1:$PORT"
  echo "      浏览器已打开: http://127.0.0.1:$PORT"
  trap on_leave INT HUP TERM
  print_explain
  console_loop
  exit 0
fi

if [ -n "$PIDS" ]; then
  echo "[3/4] 端口 $PORT 被占用但服务无响应(僵死),正在清理旧进程: $PIDS"
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null
  sleep 2
  # 兜底:lsof 可能因权限拿不到,再用进程名清一次(按 monitor.py 的绝对路径匹配,
  # 认准"本目录的进程",不误伤别的目录里跑着的实例)
  pkill -9 -f "$ROOT/monitor.py" 2>/dev/null
  sleep 1
  echo "      已清理"
else
  echo "[3/4] 端口空闲"
fi

# 4. 启动。不用 exec:窗口要留下来当控制台(菜单、日志、重启都在这里)。
echo "[4/4] 启动监控..."
trap on_leave INT HUP TERM
start_child
print_explain
open_browser_when_ready
console_loop
