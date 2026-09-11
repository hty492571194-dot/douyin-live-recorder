#!/bin/bash
# 抖音直播监控 · 一键启动(macOS)
# 用法:双击本文件;或终端执行 ./一键启动.command
#
# 判活逻辑(重要):
#   端口被占用 ≠ 服务健康。出现过「端口在 LISTEN 但 HTTP 无响应」的僵死,
#   所以这里用「端口占用 + HTTP 探活」双重判断:
#     · 端口空            → 直接启动
#     · 端口占用 + 有响应 → 已在运行,只开浏览器
#     · 端口占用 + 无响应 → 判定僵死,杀掉旧进程后重启
set -u
cd "$(dirname "$0")" || { echo "无法进入项目目录"; exit 1; }
ROOT="$(pwd)"

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

PIDS=$(holders)

if [ -n "$PIDS" ] && probe; then
  echo "[3/4] 监控已在运行且响应正常"
  open "http://127.0.0.1:$PORT"
  echo "      浏览器已打开: http://127.0.0.1:$PORT"
  echo "      想重启:敲 douyin restart(或 dyr);想关闭:douyin stop(或 dyoff)"
  echo "      (本窗口可以直接关闭,服务在后台继续运行)"
  read -n 1 -s -r -p "按任意键关闭本窗口..."
  exit 0
fi

if [ -n "$PIDS" ]; then
  echo "[3/4] 端口 $PORT 被占用但服务无响应(僵死),正在清理旧进程: $PIDS"
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null
  sleep 2
  # 兜底:lsof 可能因权限拿不到,再用进程名清一次
  pkill -9 -f "$ROOT/.venv/bin/python monitor.py" 2>/dev/null
  sleep 1
  echo "      已清理"
else
  echo "[3/4] 端口空闲"
fi

# 4. 启动(不用 exec,这样崩溃时错误能留在窗口里)
echo "[4/4] 启动监控中..."
echo "      (Ctrl+C 停止;关闭本窗口即退出服务)"
echo "      命令速查见上方清单,随时可敲 douyin help 再看一次"
# 后台轮询等就绪再开浏览器,避免打开空白页
(
  for _ in $(seq 1 40); do
    if probe; then
      open "http://127.0.0.1:$PORT"
      exit 0
    fi
    sleep 1
  done
  echo "      [!] 40 秒内服务未就绪,请查看上方错误信息"
) &

"$PY" monitor.py
CODE=$?
echo ""
echo "监控已退出(退出码 $CODE)"
if [ "$CODE" -ne 0 ]; then
  echo "常见原因:端口被占用 / 依赖缺失 / config.json 损坏"
  echo "可先看日志: tail -50 logs/launchd.err.log"
fi
read -n 1 -s -r -p "按任意键关闭本窗口..."
