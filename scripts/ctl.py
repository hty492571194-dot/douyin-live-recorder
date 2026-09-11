#!/usr/bin/env python3
"""抖音直播监控 · 终端快捷指令(启 / 停 / 重启 / 看状态)。

为什么要它
----------
Web 界面上的「重启」是**延迟 + 脱离进程组**执行的:后端必须先把自己杀掉,
所以命令只能甩出去、让前端轮询等待 —— 用户看到的是"点了一下、页面转圈"。
坐在终端前的时候不该这么别扭:这里逐步同步执行、每步等一等再校验,
最后直接把结果打出来。

和 Web 按钮的关系:两者共用 `webui._service_plan()` 里的同一份执行计划
(先清端口上的外来实例、bootstrap 前先 bootout 这些次序规则只写一遍),
本文件只负责「怎么执行、怎么等待、怎么展示」。

用法(装好后在任意目录都能用,见 README「终端快捷指令」一节;
完整清单直接敲 `douyin help`,同样由下面的 COMMANDS 表渲染)
    douyin              同 status
    douyin status       服务状态 + 健康面板摘要
    douyin start|on     启动(开机自启的 launchd job)
    douyin stop|off     关闭
    douyin restart|r    重启(会先清掉占用端口的非托管实例)
    douyin health       健康面板逐项明细
    douyin log [-f] [N] 业务日志(默认最近 30 行,-f 持续跟随)
    douyin open         打开 Web 控制台
    douyin doctor       自检终端环境(PYTHONPATH / 残留代理)
    douyin help         命令速查(「一键启动.command」窗口里显示的就是它)

安全边界:只操作本项目自己的固定 launchd label,不接受任何来自命令行的
路径/标签/端口参数,避免误伤或命令注入。restart 会中断正在进行的录制
(服务收到 SIGTERM 后停 ffmpeg、把已录部分落进 history)。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import webui  # noqa: E402

# 状态灯:终端渲染成彩色圆点,不支持颜色时自动退化为 ASCII(见 _paint)
DOTS = {"ok": "\u25cf", "warn": "\u25b2", "down": "\u2715", "idle": "\u25cb"}
COLORS = {"ok": "32", "warn": "33", "down": "31", "idle": "90"}
STATUS_CN = {"ok": "正常", "warn": "降级", "down": "故障", "idle": "空闲"}

SUCCESS, FAILURE, TIMEOUT = 0, 1, 2

# ── 命令清单(唯一真源) ────────────────────────────────────
# 「有哪些命令、各自什么效果」只写在这一处:argparse 的子命令与帮助文本、
# `douyin help` 的速查表、以及「一键启动.command」窗口里显示的那张表,
# 全部由它渲染出来。
#
# 为什么坚持表驱动:本项目踩过「两个入口各写一份规则」的坑 —— Web 界面按钮
# 与终端指令各自维护了一套启停规则,结果只修了一边,另一边留着老 Bug
# (表现为「点重启没反应」)。命令清单同理,写两遍迟早一边是过期的。
#
# 字段:命令名 / 别名 / 作用 / 终端示例(示例里带参的命令,参数写法即真实用法)
COMMANDS = (
    ("status", ("st", "s"), "服务状态 + 健康摘要 + 直播/录制(不带参数时的默认动作)",
     "douyin"),
    ("start", ("on", "up"), "启动服务并交给 launchd(开机自启、崩溃自动拉起)",
     "douyin start"),
    ("stop", ("off", "down"), "关闭服务(不会自己回来,需再 start)",
     "douyin stop"),
    ("restart", ("r", "reload"), "重启服务(先请走占用端口的非托管实例,再拉起)",
     "douyin restart"),
    ("health", (), "健康面板逐项明细:各组件状态与最后心跳",
     "douyin health"),
    ("log", (), "今日业务日志,默认最近 30 行",
     "douyin log [-f] [50]"),
    ("open", (), "在浏览器打开 Web 控制台",
     "douyin open"),
    ("doctor", (), "自检终端环境(PYTHONPATH / 残留代理隐患)",
     "douyin doctor"),
    ("help", (), "打印本页命令速查(--short 紧凑版,--json 结构化输出)",
     "douyin help"),
)

# 注册到 ~/.zshrc 的短别名。真身写在 scripts/install_cli.sh 的标记块里,
# tests/test_ctl.py 会比对两边,防止改了一边忘了另一边。
# 字段:别名 / 完整命令 / 说明
ZSH_ALIASES = (
    ("dy", "douyin", "状态总览"),
    ("dyr", "douyin restart", "重启"),
    ("dyon", "douyin start", "开启"),
    ("dyoff", "douyin stop", "关闭"),
    ("dyh", "douyin health", "健康明细"),
    ("dylog", "douyin log -f", "跟随日志"),
    ("dydr", "douyin doctor", "环境自检"),
)


# ── 小工具 ────────────────────────────────────────────────
def _use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text, status):
    if not _use_color():
        return text
    return "\033[%sm%s\033[0m" % (COLORS.get(status, "0"), text)


def _dot(status):
    return _paint(DOTS.get(status, "?"), status)


def _sh(cmd, timeout=20):
    """执行一条 shell 命令,返回 (returncode, 合并输出)。"""
    try:
        p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "命令超时:%s" % cmd
    except Exception as e:            # noqa: BLE001
        return 1, str(e)


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)          # 信号 0 = 只探测存在性,不打扰进程
        return True
    except OSError:
        return False


def _wait(check, timeout, step=0.5, on_tick=None):
    """轮询等待 check() 为真,超时返回 False。比固定 sleep 快得多也更准。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        if on_tick:
            on_tick()
        time.sleep(step)
    return check()


def _http(path, timeout=5):
    """直接问正在跑的监控进程要数据(绕开系统代理,否则会被劫走)。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:%d%s" % (webui._webui_port(), path)
    try:
        with opener.open(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                 # noqa: BLE001
        return None


def _port():
    return webui._webui_port()


def _probe_http():
    """服务是否真的在响应(只看端口会被僵死进程骗过去)。"""
    return _http("/api/service") is not None


# ── status ────────────────────────────────────────────────
def _print_service():
    st = webui._service_status()
    running, managed = st.get("running"), st.get("managed")
    src = st.get("source")
    port = _port()

    if running and src == "launchd":
        head = _dot("ok") + " 运行中 · launchd 托管(开机自启、崩溃自动拉起)"
    elif running:
        head = (_dot("warn") + " 运行中 · 但不是 launchd 启动的"
                               "(不会开机自启,建议 douyin restart 交回托管)")
    elif managed:
        head = _dot("down") + " 已装载但没在运行 —— 敲 douyin start 拉起"
    else:
        head = _dot("down") + " 未运行 —— 敲 douyin start 启动"

    print("服务   %s" % head)
    if st.get("pid"):
        # ps 在个别受限环境里取不到 etime,此时退而用服务自报的进程运行时长
        up_s = st.get("uptime")
        if up_s is None and running:
            h = _http("/api/health") or {}
            up_s = h.get("uptime_s")
        print("       进程 pid %s · 已运行 %s"
              % (st["pid"], webui._fmt_dur(up_s) if up_s else "?"))
    print("       控制台 http://127.0.0.1:%d" % port)
    if not st.get("plist_exists"):
        print("       " + _dot("warn") + " 缺少 launchd 配置:%s" % st.get("plist"))


def _print_health():
    h = _http("/api/health")
    if h is None:
        print("健康   %s 服务未响应,拿不到健康面板(先 douyin start)"
              % _dot("down"))
        return
    c = h.get("counts") or {}
    print("健康   %s 正常 %d · 降级 %d · 故障 %d · 空闲 %d"
          % (_dot("ok" if h.get("ok") else ("down" if h.get("failed") else "warn")),
             c.get("ok", 0), c.get("warn", 0), c.get("down", 0), c.get("idle", 0)))
    for g in h.get("groups") or []:
        bad = [i for i in g["items"] if i["status"] in ("down", "warn")]
        for i in bad:                 # 只列有问题的,正常项折叠成一行计数
            extra = " · %s" % i["detail"] if i.get("detail") else ""
            print("       %s %s%s" % (_dot(i["status"]), i["name"], extra))
    if h.get("ok"):
        print("       %d 项全部正常" % (h.get("total") or 0))


def _print_recording():
    st = _http("/api/status")
    if st is None:
        return
    streamers = st.get("streamers") or {}
    live = [v for v in streamers.values() if (v or {}).get("is_live")]
    recs = st.get("recordings") or {}
    active = [(k, v) for k, v in recs.items() if (v or {}).get("recording") is True]
    if active:
        head = _dot("ok") + " %d 路录制中" % len(active)
    elif live:
        head = _dot("warn") + " 有人在播但没有录制在进行 —— 看 douyin log 50"
    else:
        head = _dot("idle") + " 无人开播"
    print("直播   %s(%d 位主播,%d 位在播)" % (head, len(streamers), len(live)))
    for name, v in active[:6]:
        v = v or {}
        mark = " · 卡住告警" if v.get("stalled") else ""
        print("       · %s(已录 %s%s)"
              % (name, webui._fmt_dur(v.get("duration") or 0), mark))


def cmd_status(args):
    _print_service()
    _print_health()
    _print_recording()
    # 端口被非托管实例占着是这条链路上最容易埋雷的状态,status 里明确点出来
    outsider = webui._outsider_pid()
    if outsider:
        print("提醒   端口 %d 被非托管实例 pid %d 占用 —— "
              "douyin restart 会先清掉它再交回 launchd" % (_port(), outsider))
    return SUCCESS


# ── start / stop / restart ────────────────────────────────
def _run_action(action, dry_run=False):
    ok, message, steps = webui._service_plan(action)
    print("%s  %s%s" % (_dot("warn") if ok else _dot("down"),
                        "（干跑,不会真执行）" if dry_run else "", message))
    if not ok:
        return FAILURE
    if dry_run:
        # 会中断录制,所以先让人看清要做什么再点头
        print("  将要执行:")
        for st in steps:
            print("    [%s] %s\n         %s" % (st["kind"],
                                                st.get("label") or "", st["cmd"]))
        return SUCCESS

    for st in steps:
        kind, label = st["kind"], st.get("label") or st["cmd"]
        pid = st.get("pid")

        if kind == "pause":
            # 终端里不做固定傻等:每一步后面都按真实状态轮询(见下面 _wait)
            continue
        if kind == "kill" and pid and not _pid_alive(pid):
            continue                  # 已经自己退了,不必补刀

        print("       → %s" % label)
        rc, out = _sh(st["cmd"])
        if rc not in (0, 1) and out:  # 1 常是 kill 说「进程已不在」,不算失败
            print("         %s %s" % (_dot("warn"), out.splitlines()[0][:200]))

        if kind == "term" and pid:
            # 等它真的走 _handle_term:停 ffmpeg、把已录部分落进 history 再退出。
            # 收尾要写 NAS 队列,慢一些正常,给 10 秒;真僵住就交给下一步 SIGKILL。
            target = st.get("timeout_s", 10)
            if not _wait(lambda: not _pid_alive(pid), target):
                print("         %s 进程 %d 未在 %d 秒内退出(可能在收尾,下一步强制结束)"
                      % (_dot("warn"), pid, target))
        elif kind in ("bootout", "bootstrap"):
            time.sleep(0.5)           # launchctl 卸载/装载的落定时间

    return _verify(action)


def _verify(action):
    """动作发完后轮询确认结果 —— 只报「指令已发出」而不验证,等于没说。"""
    port = _port()
    if action == "stop":
        def gone():
            return webui._port_owner_pid(port) is None
        if _wait(gone, 20):
            print("%s  服务已停止(端口 %d 已释放)" % (_dot("ok"), port))
            return SUCCESS
        pid = webui._port_owner_pid(port)
        print("%s  20 秒后端口仍被 pid %s 占用 —— 该实例不是 launchd 拉的,"
              "需要 douyin restart 清掉" % (_dot("warn"), pid))
        return TIMEOUT

    ready = _wait(_probe_http, 60, step=1.0)
    st = webui._service_status()
    if ready:
        src = "launchd 托管" if st.get("source") == "launchd" else "非托管(建议 restart 交回)"
        print("%s  服务已就绪 · pid %s · %s · http://127.0.0.1:%d"
              % (_dot("ok"), st.get("pid"), src, port))
        return SUCCESS
    print("%s  60 秒内没有等到服务响应。排查顺序:" % _dot("down"))
    print("       1) tail -30 %s/logs/launchd.err.log" % BASE)
    print("       2) douyin log 50")
    print("       3) 端口被非托管实例占用时,launchctl 会 Address already in use")
    return TIMEOUT


def cmd_start(args):
    return _run_action("start", getattr(args, "dry_run", False))


def cmd_stop(args):
    return _run_action("stop", getattr(args, "dry_run", False))


def cmd_restart(args):
    return _run_action("restart", getattr(args, "dry_run", False))


# ── health / log / open ───────────────────────────────────
def cmd_health(args):
    h = _http("/api/health")
    if h is None:
        print("服务未响应,拿不到健康面板(先 douyin start)")
        return FAILURE
    print("系统健康 · 已运行 %s · 生成于 %s"
          % (webui._fmt_dur(h.get("uptime_s") or 0),
             time.strftime("%H:%M:%S", time.localtime(h.get("generated_at") or 0))))
    for g in h.get("groups") or []:
        print("\n[%s]" % g["name"])
        for i in g["items"]:
            age = ("最后心跳 %s 前" % webui._fmt_dur(i["age_s"])
                   if i.get("age_s") is not None else "无心跳")
            cnt = " ×%s" % i["count"] if i.get("count") else ""
            detail = i.get("detail") or ""
            print("  %s %-12s %s%s" % (_dot(i["status"]), i["name"],
                                       STATUS_CN.get(i["status"], i["status"]), cnt))
            print("     %s%s" % (age, (" · " + detail) if detail else ""))
    return SUCCESS if h.get("ok") else FAILURE


def _log_path():
    return os.path.join(BASE, "logs", "monitor-%s.log" % time.strftime("%Y-%m-%d"))


def cmd_log(args):
    path = _log_path()
    if not os.path.exists(path):
        print("今天的业务日志还没生成:%s" % path)
        return FAILURE
    if args.follow:
        print("跟随 %s(Ctrl+C 退出)" % path)
        try:
            return subprocess.call(["tail", "-n", str(args.lines), "-f", path])
        except KeyboardInterrupt:
            print("")
            return SUCCESS
    rc, out = _sh("tail -n %d %s" % (args.lines, path))
    print(out)
    return rc


def cmd_open(args):
    url = "http://127.0.0.1:%d" % _port()
    if not _probe_http():
        print("%s 服务未响应,先 douyin start" % _dot("warn"))
        return FAILURE
    subprocess.call(["open", url])
    print("已在浏览器打开 %s" % url)
    return SUCCESS


def cmd_doctor(args):
    """环境自检:这个项目的两次严重事故都出在「从 IDE/受管环境里拉起服务」。"""
    problems = []
    if os.environ.get("PYTHONPATH"):
        problems.append("PYTHONPATH 被设置(%s…):其中的 sitecustomize 会把 os.remove "
                        "换成需人工确认的版本,后台进程会卡死"
                        % os.environ["PYTHONPATH"][:60])
    for k in ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        v = os.environ.get(k)
        if v:
            problems.append("%s=%s 残留:检测请求与 ffmpeg 拉流会被送进代理,"
                            "表现为「全员检测失败」但服务看着是活的" % (k, v))
    if "WorkBuddy.app" in os.environ.get("PATH", ""):
        problems.append("PATH 里有 IDE/受管环境的 shim:建议在「你自己的终端」里"
                        "执行本命令,受管环境可能拦下 launchctl")
    if problems:
        print("%s  当前终端环境有 %d 处隐患:" % (_dot("warn"), len(problems)))
        for p in problems:
            print("   · %s" % p)
        print("\n  用官方入口(douyin 包装脚本 / 双击「一键启动.command」)会自动清掉这些")
        return FAILURE
    print("%s  环境干净", _dot("ok"))
    return SUCCESS


# ── help(命令速查) ────────────────────────────────────────
def _pad(text, width):
    """按**显示宽度**补齐:中日韩字符在终端里占 2 格,直接 ljust 会参差不齐。"""
    w = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)
    return text + " " * max(0, width - w)


def _alias_cmd(target):
    """短别名的目标是哪条子命令。`douyin` 单蹦 = 默认动作,即 status。"""
    words = target.split()
    return words[1] if len(words) > 1 else "status"


def cheatsheet():
    """命令速查的**结构化**版本 —— 命令清单对外的唯一出口。

    三个消费方都从这里取数据,谁也不许另写一份:
      · `douyin help` 的终端排版(见 cmd_help)
      · `douyin help --json`
      · Web「系统健康 → 终端快捷指令」卡片(经 webui 的 `GET /api/shortcuts`)

    只读 COMMANDS / ZSH_ALIASES,不访问服务 —— 服务没起来时照样能看。
    为什么非要一个结构化出口:本项目踩过「两个入口各写一份规则」的坑
    (Web 按钮与终端指令各自维护一套启停规则,只修了一边),命令清单同理。
    """
    rows = []
    for name, cli_aliases, effect, usage in COMMANDS:
        shorts = [short for short, target, _ in ZSH_ALIASES
                  if _alias_cmd(target) == name]
        rows.append({"command": name, "usage": usage,
                     "aliases": shorts + list(cli_aliases), "effect": effect})
    return {
        "rows": rows,
        "aliases": [{"alias": a, "target": t, "note": n}
                    for a, t, n in ZSH_ALIASES],
        # 下面几项一律用 list 而不是 tuple:这份数据要经 JSON 送到前端,
        # 元组虽然也能序列化成数组,但比较时类型对不上(测试里吃过一次亏)。
        "options": [
            {"flag": "-n, --dry-run",
             "note": "start / stop / restart 只打印将要执行的步骤,不真的执行"},
            {"flag": "-f, --follow",
             "note": "log 持续跟随(等价于 tail -f 今日业务日志)"},
        ],
        "notes": [
            "restart / stop 会中断正在录制的场次 —— 服务收到信号后先停 ffmpeg、"
            "把已录部分落进 history 再退出,不留孤儿录制进程。",
            "短别名由 scripts/install_cli.sh 写入 ~/.zshrc;新开终端窗口或执行一次 "
            "exec zsh 后生效。",
            "启停规则与 Web 界面上的按钮共用同一份(webui._service_plan()),"
            "不会两边不一致。",
        ],
        # 没装/搬家后入口失效时,一句话就能修回来(Web 卡片与终端都显示这行)
        "install": "bash scripts/install_cli.sh",
    }


def _cmd_rows():
    """终端排版用的行:(示例, 别名串, 作用)。别名列同时收 CLI 别名与 zsh 短别名。

    短别名要新开终端窗口才生效,这点在表下面单独说明。
    """
    return [(r["usage"], ", ".join(r["aliases"]) or "-", r["effect"])
            for r in cheatsheet()["rows"]]


def cmd_help(args):
    """命令速查。--short 是给「一键启动.command」窗口用的紧凑版,--json 给程序消费。

    全部内容来自 cheatsheet() —— 与 Web「系统健康 → 终端快捷指令」同源,
    不访问服务:服务没起来时也得能看。
    """
    if getattr(args, "json", False):
        print(json.dumps(cheatsheet(), ensure_ascii=False, indent=2))
        return SUCCESS

    sheet = cheatsheet()
    short = getattr(args, "short", False)
    print("抖音直播监控 · 终端快捷指令" +
          ("" if short else "(任意目录可用;与 Web 界面上的按钮同一套规则)"))
    print("  %s%s作用" % (_pad("命令", 26), _pad("别名", 20)))
    print("  %s" % ("─" * 66))
    for usage, aliases, effect in _cmd_rows():
        print("  %s%s%s" % (_pad(usage, 26), _pad(aliases, 20), effect))

    if short:
        print("  小贴士:restart / stop 会中断正在录制的场次,先加 -n 可干跑看清步骤;"
              "短别名在新开的终端窗口生效。")
        return SUCCESS

    print("")
    print("  开关:")
    for opt in sheet["options"]:
        print("    %s%s" % (_pad(opt["flag"], 16), opt["note"]))
    print("")
    print("  短别名(由 scripts/install_cli.sh 写入 ~/.zshrc;新开窗口或 exec zsh 生效):")
    for it in sheet["aliases"]:
        print("    %s%s%s" % (_pad(it["alias"], 8), _pad(it["target"], 22),
                              it["note"]))
    print("")
    for i, note in enumerate(sheet["notes"]):
        print("  注意:%s" % note if i == 0 else "        %s" % note)
    print("")
    print("  没装或搬家后失效:cd 到项目根执行 %s(幂等,可重复跑)" % sheet["install"])
    return SUCCESS


# ── 入口 ──────────────────────────────────────────────────
def build_parser():
    """子命令从 COMMANDS 表生成 —— 帮助文本、别名、速查表同源,不会漂移。"""
    p = argparse.ArgumentParser(
        prog="douyin", description="抖音直播监控 · 终端快捷指令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n  douyin            看状态\n  douyin restart    重启(先清掉占用端口的非托管实例)\n"
               "  douyin stop       关闭\n  douyin log -f     跟日志\n"
               "  douyin help       完整命令速查")
    sub = p.add_subparsers(dest="cmd")

    fns = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop,
           "restart": cmd_restart, "health": cmd_health, "log": cmd_log,
           "open": cmd_open, "doctor": cmd_doctor, "help": cmd_help}
    # 这几条会中断在录的场次,统一给一个「只看不做」的开关
    dry_run_able = {"start", "stop", "restart"}

    for name, aliases, effect, _usage in COMMANDS:
        s = sub.add_parser(name, help=effect, aliases=list(aliases))
        s.set_defaults(fn=fns[name])
        if name in dry_run_able:
            s.add_argument("-n", "--dry-run", action="store_true",
                           help="只打印将要执行的步骤,不真的执行")
        if name == "log":
            s.add_argument("lines", nargs="?", type=int, default=30,
                           help="输出行数(默认 30)")
            s.add_argument("-f", "--follow", action="store_true", help="持续跟随")
        if name == "help":
            # 一键启动窗口一屏要放得下,给它个紧凑版
            s.add_argument("-s", "--short", action="store_true",
                           help="紧凑输出(只列命令,供启动脚本显示)")
            # 结构化输出:Web 端(经 /api/shortcuts)与脚本都拿它当数据源,
            # 不必去解析排版后的文本
            s.add_argument("--json", action="store_true",
                           help="输出 JSON(含命令、别名、开关、注意事项)")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        args.fn = cmd_status       # 什么都不带 = 看状态,最常用
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
