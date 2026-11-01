"""抖音登录态管理 —— 仅服务「检测通道」,严格限定作用域。

安全边界(修改本模块前必读)
-------------------------
本模块对配置的**唯一**写入点是 ``detection.cookie``(经钥匙串注入),
绝不触碰弹幕 WebSocket、录制、归档、预览等任何链路。

为什么登录 Cookie 不能用于弹幕 WebSocket:
    抖音限制「同一账号同时只能进入一个直播间」—— 若账号进入第二个直播间,
    第一个会被强制踢出,导致正在录制的场次中断。本项目需要 4~5 路并发,
    而账号数量有限,一旦把登录态带进弹幕连接就会互相挤占。

    因此弹幕采集(danmaku.py)继续使用**匿名 ttwid + 随机 user_unique_id**,
    每路连接等价于一个独立游客身份,无并发上限。这是社区验证的最佳实践,
    礼物数据已实测跑通。登录 Cookie 只用于 HTTP 房间状态 API,
    该接口不占直播间名额,一个账号可服务全部主播的检测。

存储策略
--------
* Cookie 明文:macOS 钥匙串(service ``douyin-cookie``),不落 config.json
* 浏览器登录态:``auth/profile/``(Playwright 持久化 profile,供续期/免扫码)
* 元数据:``auth/meta.json``(昵称、校验时间、过期时间 —— 不含 Cookie 明文)
"""

import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser

import health

BASE = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.path.join(BASE, "auth")
PROFILE_DIR = os.path.join(AUTH_DIR, "profile")
META_PATH = os.path.join(AUTH_DIR, "meta.json")
KEYCHAIN_SERVICE = "douyin-cookie"

# 扫码登录入口。二维码只在点击「登录」后由 JS 动态渲染,
# 直接访问首页只会落到 /jingxuan 信息流页,页面上根本没有二维码。
LOGIN_URL = "https://www.douyin.com/"

# 登录态里标识"已登录"的关键 Cookie。
#
# ⚠️ passport_csrf_token 绝不能出现在这里:它是抖音给**匿名访客**也下发的
# CSRF 令牌,只要打开过页面就一定存在。曾把它算作登录信号,导致页面一加载
# 就被判定"已登录" → 立即结束等待并关闭浏览器窗口(用户看到的就是
# 「窗口弹出几秒后自动消失,且从未出现二维码」),同时把一串匿名 Cookie
# 写进钥匙串,status() 查不到 sessionid 又显示"未登录"。
_SESSION_KEYS = ("sessionid", "sessionid_ss", "sid_guard")

# 过期时间统计用的字段集合(含会话级字段,不影响登录判定)
_EXPIRE_KEYS = _SESSION_KEYS + ("passport_csrf_token",)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 规避基础自动化特征(抖音会检测 navigator.webdriver 等)
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'chrome', { get: () => ({ runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} }) });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
Object.defineProperty(window, 'chrome', { get: () => ({ runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} }) });
"""

# 抖音首页在首次访问新 profile 时,会先弹「验证码中间页」,需要用户在浏览器里
# 完成滑块后才能进入真正的首页。
# 两组特征分开:标题关键词用于识别中间页,URL 关键词只在页面空白时才采信。
_CAPTCHA_TITLES = ("验证码中间页", "环境异常", "请完成验证", "安全验证")
_CAPTCHA_URLS = ("verifycenter", "captcha", "verify_mtn")

# 登录期间要屏蔽的媒体资源。
#
# 抖音首页/直播广场默认会自动播放多路 1080×1920 视频。实测 live.douyin.com
# 要拉取约 32MB、8 个 video 元素持续解码,www.douyin.com 约 10MB。这些流量
# 与解码会吃满带宽和 CPU,表现为:
#   * 页面交互明显发涩(用户反馈的"浏览非常卡顿")
#   * 登录面板/二维码 iframe 要排队等到视频加载完才渲染
# 扫码登录只需要 DOM 与 sso 子 frame,不需要任何视频,所以登录期间直接在
# 浏览器侧阻断媒体请求(CDP Network.setBlockedURLs,不产生 Python 侧往返)。
#
# 只屏蔽视频域名与视频扩展名 —— 图片 CDN(byteimg/douyinpic)不能屏蔽,
# 二维码图片有可能走它们。
_MEDIA_BLOCK = [
    "*douyinvod.com*",
    "*amemv.com*",
    "*.mp4", "*.flv", "*.m3u8", "*.ts", "*.webm",
    "*.m4s", "*.hevc", "*.mov",
]

# 扫码登录的目标域。必须是 www 主站 —— live.douyin.com / creator.douyin.com
# 等子域虽然同属抖音,但不会自动弹出登录面板,拿它们当"已在登录页"复用
# 会导致一直找不到登录入口(用户感知就是"二维码迟迟不出来")。
_LOGIN_HOST = "www.douyin.com"

_lock = threading.Lock()
_login_thread = None          # 登录进行中时非 None(防止重复拉起浏览器)
_last_check = 0.0             # 最近一次真实校验时间戳
_cancel_event = None          # 当前登录流程的取消信号
_login_info = {"phase": "", "browser": "", "error": ""}   # 供前端展示的进度


# ── 钥匙串读写 ──────────────────────────────────────────────
def _keychain_get():
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _keychain_set(value):
    """写入钥匙串。返回 (是否成功, 错误信息)。"""
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", "default",
             "-s", KEYCHAIN_SERVICE, "-w", value],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or "").strip()[:200]
    except Exception as e:
        return False, str(e)[:200]


# ── 元数据 ──────────────────────────────────────────────────
def _load_meta():
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(meta):
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        tmp = META_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, META_PATH)
    except Exception:
        pass


# ── Cookie 组装 ─────────────────────────────────────────────
def _cookie_string(cookies):
    """Playwright cookies 列表 → 请求头用的 Cookie 字符串(保留全部字段)。

    不过滤字段:抖音要求 Cookie 完整(通常 60+ 个字段),缺失会返回
    200 但内容为空,或被判定为异常环境。
    """
    parts = []
    for c in cookies or []:
        name = c.get("name") if isinstance(c, dict) else None
        value = c.get("value") if isinstance(c, dict) else None
        # 过滤空值:空字段会拼出 "name=" 这样的无效片段
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _has_login(cookies):
    """是否**真正**登录:sessionid / sessionid_ss / sid_guard 任一有值。

    只认会话型字段。passport_csrf_token 之类的防刷字段匿名访客也有,
    算进来会在页面刚打开时就误判为已登录。
    """
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") in _SESSION_KEYS and c.get("value"):
            return True
    return False


def _expire_at(cookies):
    """取登录态 Cookie 里最早的过期时间(秒);-1 表示会话级 Cookie。"""
    exp = None
    for c in cookies or []:
        if not isinstance(c, dict) or c.get("name") not in _EXPIRE_KEYS:
            continue
        if not c.get("value"):
            continue
        e = c.get("expires")
        if e is None or e == -1:
            continue
        exp = e if exp is None else min(exp, e)
    return exp


# ── 对外:注入到配置 ─────────────────────────────────────────
def resolve_cookie(det_cfg):
    """解析检测通道要用的 Cookie。

    ``detection.cookie_ref == "keychain:douyin-cookie"`` 时从钥匙串取,
    否则回退配置里的明文 ``detection.cookie``(兼容手动填写)。
    """
    ref = (det_cfg or {}).get("cookie_ref", "")
    if ref == f"keychain:{KEYCHAIN_SERVICE}":
        ck = _keychain_get()
        if ck:
            # 钥匙串里有内容但没有 sessionid,说明存的是匿名 Cookie
            # (旧版本把 passport_csrf_token 误判为已登录时就会写下这种垃圾)。
            # 宁可返回空让检测走纯 302,也不拿废 Cookie 去打 API。
            # 注意:钥匙串为**空**时不在此列,那种情况继续回退明文配置。
            return ck if "sessionid=" in ck else ""
    return (det_cfg or {}).get("cookie", "") or ""


def inject(cfg):
    """把钥匙串里的 Cookie 注入 cfg["detection"]["cookie"](内存,不落盘)。

    安全边界:只改这一个字段。
    """
    try:
        det = cfg.setdefault("detection", {})
        det["cookie"] = resolve_cookie(det)
    except Exception:
        pass
    return cfg


# ── 对外:状态(供前端展示) ──────────────────────────────────
def status(cfg=None):
    """登录态概览。不含任何 Cookie 明文。"""
    meta = _load_meta()
    ck = _keychain_get()
    det = (cfg or {}).get("detection") or {}
    using_keychain = det.get("cookie_ref") == f"keychain:{KEYCHAIN_SERVICE}"
    logged_in = bool(ck) and ("sessionid=" in ck)
    state = "none"
    if logged_in:
        state = "expired" if meta.get("expired") else (
            "ok" if meta.get("last_ok") else "unknown")
    return {
        "ok": True,
        "logged_in": logged_in,
        "state": state,                       # none/ok/unknown/expired
        "nickname": meta.get("nickname") or "",
        "last_check": meta.get("last_check"),      # 最近一次真实校验
        "last_ok": meta.get("last_ok"),            # 最近一次校验通过
        "expire_at": meta.get("expire_at"),
        "error": meta.get("error") or "",
        "using_keychain": using_keychain,
        "manual_cookie": bool(det.get("cookie")) and not using_keychain,
        "logging_in": _login_thread is not None and _login_thread.is_alive(),
        "login_phase": _login_info.get("phase", ""),   # starting/waiting/manual/done/failed/cancelled
        # 已登录成功时必须让位:这个标志只是"上次没能自动读到 Cookie"的
        # 残留状态(进程内变量,重启才清),否则前端会一直挂着手动粘贴框
        "manual_required": (not logged_in) and _login_info.get("phase") == "manual",
        "browser": _login_info.get("browser", ""),
        "browser_path": find_browser()[0],
        "browser_name": find_browser()[1] or "未检测到 Chrome",
        "check_interval_hours": (cfg or {}).get("auth", {}).get(
            "check_interval_hours", 12) if cfg else 12,
    }


# ── 系统浏览器探测 ──────────────────────────────────────────
def _browser_candidates():
    """按平台返回候选浏览器 [(绝对路径, 显示名)],优先级从高到低。

    优先系统安装的 Chrome,而不是 Playwright 自带的 Chromium:
    自带 Chromium 缺专有解码器与部分站点兼容逻辑,抖音前端对它的
    环境指纹也更敏感,历史上出现过二维码区域渲染不出来的情况。
    """
    sysname = platform.system()
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    la = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")

    if sysname == "Darwin":
        paths = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
            (f"{home}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "Chromium"),
            ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "Brave"),
        ]
    elif sysname == "Windows":
        paths = [
            (os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"), "Google Chrome"),
            (os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"), "Google Chrome"),
            (os.path.join(la, "Google", "Chrome", "Application", "chrome.exe"), "Google Chrome"),
            (os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"), "Microsoft Edge"),
            (os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"), "Microsoft Edge"),
        ]
    else:  # Linux 及其它
        paths = [
            ("/usr/bin/google-chrome", "Google Chrome"),
            ("/usr/bin/google-chrome-stable", "Google Chrome"),
            ("/usr/bin/chromium", "Chromium"),
            ("/usr/bin/chromium-browser", "Chromium"),
            ("/snap/bin/chromium", "Chromium"),
        ]

    # PATH 兜底:覆盖自定义安装位置
    for exe, name in (("google-chrome", "Google Chrome"),
                      ("google-chrome-stable", "Google Chrome"),
                      ("chromium", "Chromium"),
                      ("chromium-browser", "Chromium"),
                      ("microsoft-edge", "Microsoft Edge")):
        w = shutil.which(exe)
        if w:
            paths.append((w, name))
    return paths


def find_browser():
    """返回 (可执行路径, 显示名);一个都没装时返回 ("", "")。"""
    for p, name in _browser_candidates():
        try:
            if p and os.path.exists(p) and os.access(p, os.X_OK):
                return p, name
        except OSError:
            continue
    return "", ""


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# —— 启动 Chrome 必须走 LaunchServices,不能直接 Popen ——
#
# 服务由 launchd 以 ``ProcessType=Background`` 运行(见 com.douyin.monitor.plist),
# 后台 QoS 会**被子进程继承**。实测同一个 Chrome:
#   * 从服务里 Popen 出来的主进程   pti_priority = 4
#   * 用户双击启动的 Safari         pti_priority = 31
#   * 从普通 shell 起的 Chromium    pti_priority = 31
# 掉到 4 之后 WindowServer 以低优先级给它合成,Chrome 的 BeginFrame 被压到
# 12~14Hz —— 连 ``about:blank`` 空白页都只有 14fps(同一时刻同一块屏 Safari
# 75fps、系统 Chrome 155 用别的 profile 起也是 77fps),用户感知就是「弹出来的
# 窗口很卡,滚动一顿一顿」。此时 CPU 只占 5%、GPU 走 ANGLE Metal 正常、
# 窗口 normal/visible/hasFocus 全为真、AppleScript activate 也无效 ——
# 所以从资源占用或窗口可见性上都查不出问题。
#
# 这个属性在 exec 那一刻就固化了:事后 ``setpriority(PRIO_DARWIN_PROCESS, pid, 0)``
# 返回 0(成功)但 pti_priority 仍是 4(实测),**补救不了**,只能在启动时打断继承。
# ``open -n -b <bundle> --args`` 由 LaunchServices 在 GUI 会话里创建进程,
# QoS 正常,是本机唯一可靠的方式。
_CHROME_BUNDLE_HINT = ".app/Contents/MacOS/"

_libproc = None
_libproc_tried = False


def _bundle_id_for(bin_path):
    """从可执行路径反查 macOS bundle id(给 ``open -b`` 用)。

    只认 ``<X>.app/Contents/MacOS/<exe>`` 这种布局;读不到 Info.plist 返回 "",
    调用方据此退回直接 Popen。
    """
    if platform.system() != "Darwin":
        return ""
    i = (bin_path or "").find(_CHROME_BUNDLE_HINT)
    if i < 0:
        return ""
    app = bin_path[:i + len(".app")]
    try:
        import plistlib
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            bid = (plistlib.load(f) or {}).get("CFBundleIdentifier") or ""
        return bid if isinstance(bid, str) else ""
    except Exception:
        return ""


def _proc_priority(pid):
    """读进程调度优先级(macOS ``proc_taskinfo.pti_priority``)。

    正常用户进程 31,继承了 launchd 后台 QoS 的是 4 —— 这就是「窗口很卡」的
    指纹。读不到(非 macOS / 无权限 / 进程已退出)返回 None,只当诊断信息。
    """
    global _libproc, _libproc_tried
    if platform.system() != "Darwin" or not pid:
        return None
    try:
        import ctypes
        if not _libproc_tried:
            _libproc_tried = True
            try:
                import ctypes.util
                _libproc = ctypes.CDLL(ctypes.util.find_library("proc")
                                       or "/usr/lib/libproc.dylib")
            except Exception:
                _libproc = None
        if _libproc is None:
            return None

        class _TaskInfo(ctypes.Structure):
            _fields_ = [
                ("pti_virtual_size", ctypes.c_uint64),
                ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64),
                ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64),
                ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32),
                ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32),
                ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32),
                ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32),
                ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32),
                ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32),
                ("pti_priority", ctypes.c_int32),
            ]

        info = _TaskInfo()
        n = _libproc.proc_pidinfo(int(pid), 4, 0, ctypes.byref(info),
                                  ctypes.sizeof(info))
        return int(info.pti_priority) if n == ctypes.sizeof(info) else None
    except Exception:
        return None


def _chrome_main_pid():
    """专用 profile 的 Chrome **主**进程 pid(helper 进程带 ``--type=``,排除)。

    只用于诊断(读优先级),拿不到返回 0。
    """
    prof = os.path.realpath(PROFILE_DIR)
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0
    for line in out.splitlines():
        if f"--user-data-dir={prof}" not in line or "--type=" in line:
            continue
        try:
            return int(line.split()[0])
        except Exception:
            continue
    return 0


def _spawn_browser(bin_path, args, log=print, verify=None):
    """启动 Chrome,返回实际用的方式("open" / "popen" / "")。

    ``args`` 是传给 Chrome 的参数(不含可执行文件本身)。macOS 上优先
    ``open -n -b <bundle> --args`` —— 由 LaunchServices 在 GUI 会话里创建进程,
    **不继承服务的后台 QoS**(根因见本段上方的长注释)。

    ``-n`` 是必须的:不带它时,只要系统里任何 Chrome 在跑,``open`` 只会把旧
    窗口激活、参数全丢。

    🔴 ``open`` 的返回码**不可信**:实测在某些上下文里(例如从 AI 会话的 shell
    里调)``open -n -b com.google.Chrome`` 会 rc=0 却什么都没拉起 —— ``open -W``
    0.2 秒就返回、进程表里没有新进程、也没有崩溃报告。同一条命令起 TextEdit
    却是正常的,所以这是 Chrome 专有行为。给了 ``verify`` 回调就必须等它返回
    True 才算成功,否则照样退回 Popen(宁可 QoS 低,也不能一个浏览器都没有)。
    调用方(冷启动路径)传 ``_wait_profile_chrome``;其余场景保持 ``None``。
    """
    if platform.system() == "Darwin":
        bid = _bundle_id_for(bin_path)
        if bid:
            try:
                r = subprocess.run(["open", "-n", "-b", bid, "--args"] + list(args),
                                   capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and (verify is None or verify()):
                    return "open"
                if r.returncode == 0:
                    log("[启动] LaunchServices 返回成功但浏览器没出现(rc=0 不可信),"
                        "改用直接 spawn")
                else:
                    log("[启动] LaunchServices 启动失败(%s),改用直接 spawn"
                        % ((r.stderr or "").strip()[:80] or f"rc={r.returncode}"))
            except Exception as e:
                log(f"[启动] LaunchServices 启动失败({type(e).__name__}),改用直接 spawn")
    try:
        subprocess.Popen(
            [bin_path] + list(args),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),   # 脱离本进程组,服务重启不影响它
        )
        return "popen"
    except Exception as e:
        log(f"[启动] 直接 spawn 失败({type(e).__name__})")
        return ""


def _wait_profile_chrome(timeout=3.0):
    """等一台**使用本项目 profile 的 Chrome 真的出现在进程表里**。

    只给 ``_spawn_browser(verify=...)`` 和单元测试用:用来戳破「``open`` 返回 0
    但其实什么都没起来」。调用前必须确保没有旧实例在跑(冷启动路径天然满足),
    否则任何活着的旧进程都会让校验直接通过。
    """
    deadline = time.time() + timeout
    while True:
        if _profile_chrome_pids():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


# 专用浏览器固定启动参数。改这个元组必须把 LAUNCH_ARGS_REV +1,否则已在跑的
# 旧实例会被继续复用,新参数永远不生效(Chrome 多数开关只在首次启动生效)。
_BROWSER_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    # 这三条专治「弹出来的页面加载慢 + 滚动卡」:窗口被别的窗口遮挡(或切到
    # 别的桌面)时,Chrome 默认会把渲染进程后台化 → rAF 掉到个位数 fps、
    # 资源加载优先级也被降低。实测同一个抖音页:遮挡时 5~13fps / DCL 10.8s,
    # 前台 75fps / DCL 1.2s。这个 profile 只用来给人看页面,不需要省电。
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)
LAUNCH_ARGS_REV = 2


def _activate_browser(name, log=print):
    """把浏览器窗口抬到最前(best-effort)。

    CDP 的 ``Page.bringToFront`` 只激活**标签页**,不保证 macOS 把应用窗口
    提到其他应用之上;窗口留在后面就会命中上一条说的遮挡节流,用户感知就是
    「打开了但很卡」。这里补一次 AppleScript activate,失败不影响主流程。
    """
    app = (name or "").strip()
    if not app or app == "系统默认浏览器":
        return False
    try:
        r = subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            return True
        log(f"[主页] 激活 {app} 窗口失败(非致命): {(r.stderr or '').strip()[:80]}")
    except Exception as e:
        log(f"[主页] 激活 {app} 窗口失败(非致命): {type(e).__name__}")
    return False


def _cdp_state_path():
    return os.path.join(AUTH_DIR, "cdp.json")


def _port_alive(port, timeout=0.4):
    """TCP 快探:本机端口是否在听。用于跳过已失效的 CDP 端口记录。

    cdp.json 里的历史端口多半早已退出(实例被关掉/机器重启),旧实现对着
    每个死端口白等 ``_wait_cdp`` 的 3 秒超时,一次「打开主页」光是探端口
    就要多花 6 秒以上。先做 0.4 秒的 TCP 连接探测,连不上直接跳过。
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(("127.0.0.1", int(port)))
        return True
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def _cdp_state():
    """读 cdp.json(端口记录 + 启动参数版本)。坏文件一律当空。"""
    try:
        with open(_cdp_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cdp_ports():
    """近期用过的调试端口,按「最近优先」排列。

    只记一个端口是不够的:那份记录一旦过期(实例已退出、或被别的流程覆盖),
    就会连不回**仍在运行且已登录**的那个 Chrome,只能干等新端口就绪。
    """
    d = _cdp_state()
    out = []
    for p in [d.get("port")] + list(d.get("recent") or []):
        try:
            p = int(p)
        except Exception:
            continue
        if p and p not in out:
            out.append(p)
    return out[:8]


def _save_cdp_port(port):
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        recent = [port] + [p for p in _cdp_ports() if p != port]
        tmp = _cdp_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "recent": recent[:8],
                       "args_rev": LAUNCH_ARGS_REV}, f)
        os.replace(tmp, _cdp_state_path())
    except Exception:
        pass


def _profile_chrome_pids():
    """列出正在使用本模块专用 profile 的 Chrome 进程(含 helper)。"""
    prof = os.path.realpath(PROFILE_DIR)
    out = ""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        try:
            out = subprocess.run(["pgrep", "-fl", "Chrome"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return []
    pids = []
    for line in out.splitlines():
        if "Chrome" not in line or f"--user-data-dir={prof}" not in line:
            continue
        try:
            pids.append(int(line.split()[0]))
        except Exception:
            continue
    return pids


def _clear_profile_singleton():
    """删掉 profile 里 Chrome 的单例锁残留(仅在确认没有活实例时调用)。

    不删的后果实测过:旧实例被 SIGTERM 后 SingletonLock 还在,紧接着新拉起的
    Chrome 会认为「已有实例在跑」,把 URL 转发过去然后自己退出 —— 调试端口
    **刚监听上就消失**,调用方随后拿到 TargetClosedError,一次「打开主页」
    就退化成了系统浏览器。
    """
    for fn in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(PROFILE_DIR, fn)
        try:
            if os.path.lexists(p):
                os.unlink(p)
        except Exception:
            pass


def _kill_profile_chrome(wait=True):
    """结束占用登录 profile 的 Chrome 进程,返回被结束的进程数。

    Chrome 对同一个 user-data-dir 只允许一个实例:后启动的进程只会把 URL
    转发给已有实例然后自己退出,于是新传的 ``--remote-debugging-port``
    永远不会监听。所以只要记录的端口失了效,就会卡死在
    「端口未就绪 → 转手动粘贴」,浏览器里明明已登录却读不到 Cookie。
    这里主动接管那个实例,让新端口能真正生效。

    ``wait=True`` 时会等到进程真的退出(最多 8 秒,超时补 SIGKILL)并清掉
    单例锁残留 —— 做不到这一点,调用方紧接着拉起的 Chrome 会自己退出。
    """
    pids = _profile_chrome_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if not wait or not pids:
        return len(pids)
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _profile_chrome_pids():
            break
        time.sleep(0.3)
    left = _profile_chrome_pids()
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    if left:
        time.sleep(0.5)
    if not _profile_chrome_pids():
        _clear_profile_singleton()
    return len(pids)


def _keychain_delete():
    """删除钥匙串项。

    单独抽成函数,是为了让测试能把它和 get/set 一起替换掉 —— 之前 logout()
    里直接跑 security 命令,单测的桩只替换了 get/set,结果跑一次测试就把
    用户真实保存的登录态删掉了。
    """
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _open_tab_via_cdp(ws, url):
    """在已运行的 Chrome 里新开一个标签页。失败不影响主流程。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(ws)
            try:
                ctx = b.contexts[0] if b.contexts else None
                if ctx is None:
                    return False
                page = ctx.new_page()
                page.goto(url, timeout=45000)
                page.bring_to_front()
                return True
            finally:
                pass          # 不断开浏览器
    except Exception:
        return False


def _can_reuse_running_instance(kill_stale=True):
    """能否直接复用正在跑的专用浏览器。返回 (可复用, 原因)。

    参数版本落后默认不能复用:Chrome 多数开关只在**首次启动**生效,不换实例,
    新加的开关永远不起作用(踩过 ``--remote-debugging-port``)。

    ``kill_stale=False`` 时**永不因为版本落后而换实例**:调用方(一键打开主页)
    的唯一诉求是「别动我已经开着的页面」,新参数晚点生效无所谓 —— 换实例的入口
    收敛到用户显式点的「重开浏览器」按钮(``restart_browser``)。

    ⚠️ 2026-10-26 事故:10-22 加反节流开关把版本升到 v2,但在跑的旧进程内存里
    还是 v1,02:30 冷启动时把 ``args_rev:1`` 写进 cdp.json;服务当天重启后内存
    变成 v2 —— 四天后第一次点「打开我的抖音」就被判成「参数已更新」,把用户正在
    浏览的整个 Chrome(11 个进程)杀掉重开。所以 ``why`` 现在必须带真实差异
    (旧版本→新版本),旧实现固定打印「启动参数已更新」,从日志根本看不出真因。

    例外:正在扫码登录时宁可继续用旧参数的实例 —— 杀掉会让用户正在扫的
    二维码窗口消失,那是**影响原有功能**,比新参数晚点生效严重得多。
    """
    rev = _cdp_state().get("args_rev")
    if rev == LAUNCH_ARGS_REV:
        return True, ""
    if _login_thread is not None and _login_thread.is_alive():
        return True, "正在扫码登录,本轮先复用旧实例(不打断登录)"
    delta = (f"v{rev}→v{LAUNCH_ARGS_REV}" if rev is not None
             else f"旧记录缺失→v{LAUNCH_ARGS_REV}")
    if not kill_stale:
        return True, f"启动参数{delta},本次先复用旧实例(新参数等下次冷启动生效)"
    return False, f"启动参数{delta}"


def _live_cdp_ws():
    """在记录的端口里找一个**真的能用**的 CDP 地址,返回 (ws, port)。

    只探活:不杀进程、不新建实例。记录可能过期(实例已退出或被别的流程覆盖),
    所以近期端口逐个试;先用 0.4s 的 TCP 快探跳过死端口,再等 CDP 握手。
    """
    for prev_port in _cdp_ports():
        if not _port_alive(prev_port):
            continue              # 死端口别白等 3 秒(见 _port_alive 说明)
        ws = _wait_cdp(prev_port, timeout=3)
        if ws:
            return ws, prev_port
    return "", 0


def launch_browser(url, log=print, kill_stale=True, open_tab=True):
    """用系统浏览器打开登录页,并返回 CDP 地址用于回读 Cookie。

    返回 (ws_url, browser_name, mode, error):
      mode = "cdp"    —— 已用系统 Chrome 打开,且可以通过 CDP 读取 Cookie
      mode = "system" —— 已用系统默认浏览器打开,但无法自动回读,需用户手动粘贴
    失败时 ws_url 为 ""。

    同一个 profile 已经开着 Chrome 时,再次传 --remote-debugging-port 会被
    Chrome 忽略(该开关只在首次启动生效),所以这里先复用上次记录的端口:
    复用成功就在那个 Chrome 里直接新开标签页 —— 这正是期望的扫码体验。

    ``kill_stale``:参数版本落后时是否结束旧实例。扫码登录等「新参数要立刻生效」
    的场景用 True(默认);「打开我的抖音」用 False —— 那个入口的底线是**绝不
    关掉用户已经开着的页面**(2026-10-26 事故,见 _can_reuse_running_instance)。

    ``open_tab``:复用已有实例时是否顺手在 ``url`` 上开个标签页。调用方自己会
    新开窗口时传 False,免得每次点击都攒一个空白标签。
    """
    bin_path, name = find_browser()
    if not bin_path:
        # 回退:系统默认浏览器(无法控制,只能靠用户手动粘贴 Cookie)
        try:
            opened = webbrowser.open(url)
        except Exception as e:
            return "", "", "", f"启动默认浏览器失败: {type(e).__name__}: {e}"
        if not opened:
            return "", "", "", "未能启动任何浏览器(未检测到 Chrome,且系统默认浏览器打开失败)"
        log("[登录] 未检测到 Chrome,已用系统默认浏览器打开登录页(需手动粘贴 Cookie)")
        return "", "系统默认浏览器", "system", ""

    # 复用上一次的 Chrome:端口还活着就直接在里面新开标签页。
    ws = ""
    reuse, why = _can_reuse_running_instance(kill_stale)
    if not reuse:
        killed = _kill_profile_chrome()
        if killed:
            log(f"[登录] {why},结束旧的 {name}({killed} 个进程)以便生效")
    else:
        if why:
            log(f"[登录] {why}")
        ws, prev_port = _live_cdp_ws()
        if ws:
            if open_tab:
                log(f"[登录] 复用已打开的 {name}(调试端口 {prev_port}),在新标签页中打开登录页")
                _open_tab_via_cdp(ws, url)
            else:
                log(f"[登录] 复用已打开的 {name}(调试端口 {prev_port}),不新开标签")
            return ws, name, "cdp", ""
        if not kill_stale and _profile_chrome_pids():
            # 专用 Chrome 活着但拿不到调试端口。这时再 spawn 一个只会被 Chrome
            # 单例转发、给用户白弹一个空白窗口 —— kill_stale=False 的语义就是
            # 「绝不碰它」,把「怎么把页面开出来」交给调用方(见 _forward_to_existing)。
            log(f"[登录] {name} 正在运行但调试端口不可用,本次不重启它")
            return "", name, "system", ""

    port = _free_port()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    if not _profile_chrome_pids():
        # 没有活实例却留着单例锁(上次被 SIGKILL / 异常退出)→ 新进程会认为
        # 「已有实例」而转发 URL 后自杀,表现为「打开主页退化系统浏览器」。
        _clear_profile_singleton()
    # 启动参数全部使用 Chrome 官方支持的开关,任何 Blink 特性开关(如
    # --disable-blink-features=AutomationControlled)都会触发 Chrome 的
    # 「不受支持的命令行标记」警告条(见 bad_flags_prompt.cc)。
    # 反自动化效果改由 CDP 的 addScriptToEvaluateOnNewDocument 注入。
    args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
    ] + list(_BROWSER_FLAGS) + [
        "--new-window",
        url,
    ]

    def _spawn():
        # 走 _spawn_browser:macOS 上用 LaunchServices 起,避免继承服务的
        # 后台 QoS(否则窗口只有 12~14fps,见 _spawn_browser 上方说明)。
        # verify 不能省:open 的 rc=0 不代表真的拉起了进程(见 _spawn_browser),
        # 走到这里时已确认没有旧实例,所以「进程表里出现本 profile 的 Chrome」
        # 就是成功的判据。
        return bool(_spawn_browser(bin_path, args, log,
                                   verify=_wait_profile_chrome))

    if not _spawn():
        return "", name, "", f"启动 {name} 失败"

    ws = _wait_cdp(port, timeout=12)
    unstable = False
    if ws and not _wait_cdp(port, timeout=2):
        # 起来了又立刻没了:典型是 profile 单例锁冲突(旧实例刚被结束、锁还在),
        # 新进程把 URL 转发过去就退出,端口刚监听上就消失。此时若把 ws 交出去,
        # 调用方只会拿到 TargetClosedError,不如走下面的「接管旧实例再拉起」。
        log(f"[登录] {name} 端口 {port} 就绪后立即消失(单例锁冲突?),按旧实例接管处理")
        ws, unstable = "", True
    if not ws:
        # 走到这里,几乎总是「同 profile 的 Chrome 还活着」:新进程把 URL 转发
        # 给它后就退出了,新端口永远不会监听。先接管旧实例再重新拉起,
        # 否则只能退化成手动粘贴 —— 浏览器里明明已登录却读不到 Cookie。
        # kill_stale=False(一键打开主页)时**不许接管**,宁可这次拿不到 CDP。
        killed = _kill_profile_chrome() if kill_stale else 0
        if killed:
            log(f"[登录] 已结束占用登录配置的旧 {name} 进程({killed} 个),重新启动")
        if killed or unstable:
            if _spawn():
                ws = _wait_cdp(port, timeout=25)
    if not ws:
        # 浏览器起来了但调试端口不通 —— 页面仍可扫码,只是读不到 Cookie
        log(f"[登录] {name} 已打开,但调试端口未就绪({port}),将转为手动粘贴模式")
        return "", name, "system", ""
    _save_cdp_port(port)
    log(f"[登录] 已用 {name} 打开登录页(调试端口 {port})")
    return ws, name, "cdp", ""


def _wait_cdp(port, timeout=30):
    """轮询 Chrome 的 /json/version 直到就绪,返回 webSocketDebuggerUrl。"""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                info = json.loads(r.read().decode("utf-8", "replace"))
            ws = info.get("webSocketDebuggerUrl") or ""
            if ws:
                return ws
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def _is_captcha(page):
    """是否真的卡在「验证码中间页」,而不是"页面上碰巧有风控 iframe"。

    抖音对新 profile 首次访问会先返回「验证码中间页」,此时页面上没有任何
    「登录」入口,需要先让用户在浏览器里完成滑块,才会跳转到正常首页。

    ⚠️ 不能只凭 captcha iframe 判定:抖音在**正常首页**(甚至二维码已显示
    之后)也会异步注入 rmc-nocaptcha 风控 iframe。若一律判为验证码页,
    会让已经能扫码的页面白白进入 60 秒等待。实测区分点:
      真·验证码中间页 → title="验证码中间页", body 文本为空
      正常首页         → title="抖音精选电脑版…", body 有内容(仅后台挂了风控 iframe)
    """
    try:
        title = (page.title() or "")
    except Exception:
        title = ""
    if any(h in title for h in _CAPTCHA_TITLES):
        return True
    # 页面已有实质内容 → 只是后台安全组件,不是中间页
    try:
        if (page.inner_text("body") or "").strip():
            return False
    except Exception:
        pass
    # 页面空白 + 存在验证 iframe → 确实卡在中间页
    for f in (page.frames or []):
        u = f.url or ""
        if any(h in u for h in _CAPTCHA_URLS):
            return True
    return False


def _page_text(page, max_len=400):
    try:
        return (page.inner_text("body") or "")[:max_len]
    except Exception:
        return ""


def _has_login_entry(page):
    """宽松判定页面上是否有「登录/登陆」入口。
    抖音首页的「登录」按钮不一定在 <button> 里,可能是 <p>/<div>/<span>
    或者一个 id 含 login 的容器 —— 老代码只认 button,所以经常漏。"""
    try:
        locs = [
            page.locator('button:has-text("登录")'),
            page.locator('button:has-text("登陆")'),
            page.locator('p:has-text("登录")'),
            page.locator('[id*="login" i]'),
            page.get_by_text("登录", exact=True),
            page.get_by_text("登陆", exact=True),
        ]
        for loc in locs:
            try:
                if loc.count() > 0:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    # 兜底:body 文本里出现「登录」且页面处于已渲染状态
    txt = _page_text(page)
    return "登录" in txt or "登陆" in txt


def _is_login_host(url):
    """是否为扫码登录的目标域(www.douyin.com)。

    注意不能用 ``"douyin.com" in url`` 这种包含判断:live.douyin.com、
    creator.douyin.com 等同属抖音的子域都会被误判成"已经在登录页",
    从而被复用而不做跳转 —— 而这些页面并不会自动弹出登录面板。
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        host = ""
    return host == _LOGIN_HOST or host == "douyin.com"


def _setup_media_block(page):
    """登录期间阻断视频流量。返回 CDP session(用于解除),失败返回 None。

    这是"页面卡顿"与"二维码出得慢"的直接对策:抖音首页/直播广场会
    自动播放多路高清视频,把带宽和 CPU 占满,登录面板要排队等它们加载完。
    """
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.setBlockedURLs", {"urls": list(_MEDIA_BLOCK)})
        return cdp
    except Exception:
        return None


def _clear_media_block(cdp):
    """解除视频阻断,让用户在登录完成后恢复正常浏览。"""
    if cdp is None:
        return
    try:
        cdp.send("Network.setBlockedURLs", {"urls": []})
    except Exception:
        pass


def _wait_page_ready(page, timeout=20, log=print):
    """等待页面真正渲染出来:body 有文本、没有验证码、可能有登录入口。
    返回 ('ready'|'captcha'|'error', info)。"""
    deadline = time.time() + timeout
    last_note = ""
    while time.time() < deadline:
        try:
            if _is_captcha(page):
                return "captcha", "等待用户完成滑块验证"
            txt = _page_text(page, max_len=80)
            if txt and ("抖音" in txt or "抖音精选" in txt or len(txt) > 30):
                return "ready", f"页面已就绪({len(txt)}字)"
        except Exception as e:
            last_note = f"{type(e).__name__}: {e}"[:80]
        time.sleep(1)
    return "error", f"等待页面渲染超时({last_note or '页面无内容'})"


def _wait_captcha_clear(page, timeout=90, cancel=None, log=print):
    """等待用户把验证码中间页过去,期间每 3 秒打日志,方便用户在浏览器里跟进。
    返回 True 表示已通过,False 表示超时/取消。"""
    deadline = time.time() + timeout
    last_tick = 0
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            return False
        if not _is_captcha(page):
            return True
        now = time.time()
        if now - last_tick >= 3:
            remain = int(deadline - now)
            log(f"[登录] 仍在验证码中间页,请在浏览器里完成滑块 (剩余 {remain}s)")
            last_tick = now
        time.sleep(0.5)
    return False


# ── 一键打开已登录抖音主页(WebUI 按钮)────────────────────
# 「打开我的抖音」打开的**不是** douyin.com 首页:首页会 302 到 /jingxuan
# (抖音精选视频信息流),实测 2947 个节点、250 个资源、DCL 10.8s / load 15.9s
# ——全站最重的页面之一,而且不是「我的」。个人主页 /user/self 只有 1150 个
# 节点、DCL 3.4s / load 6.0s(快 2.6~3 倍),语义也对。
_MY_HOME_URL = "https://www.douyin.com/user/self"
_MY_HOME_PATH = "/user/self"       # 复用判定只认这个路径(见 _focus_existing_home)
_HOME_URL = "https://www.douyin.com/"      # 回退:未登录/个人主页打不开时
_HOME_HOST_MARK = "douyin.com"

# 同一时刻只允许一次「打开主页」在跑:专用 profile 冷启动要十几秒,
# 连点两下会各自去抢同一个 user-data-dir(Chrome 只认首个实例,后一个
# 把 URL 转发过去就退出),互相把对方刚拉起的窗口杀掉,观感就是「点了没反应」。
_home_lock = threading.Lock()


def _load_login_cookies():
    """取登录 cookie 列表(Playwright add_cookies 格式),返回 (cookies, 来源)。

    优先 storage_state.json(扫码/手动登录时落盘,domain/expires/httpOnly/
    sameSite 字段最全);缺失或无登录态时回退钥匙串 cookie 字符串逐字段解析
    (域统一挂 .douyin.com,会丢 host-only 细节但保住 sessionid 等登录关键字段)。
    都没有返回 ([], "")。
    """
    try:
        with open(os.path.join(AUTH_DIR, "storage_state.json"), "r",
                  encoding="utf-8") as f:
            cks = (json.load(f) or {}).get("cookies") or []
        if _has_login(cks):
            return [c for c in cks if isinstance(c, dict)], "storage_state"
    except Exception:
        pass
    raw = _keychain_get()
    if "sessionid=" in raw:
        out = []
        for pair in raw.split(";"):
            k, _, v = pair.strip().partition("=")
            if k and v:
                out.append({"name": k, "value": v, "domain": ".douyin.com",
                            "path": "/"})
        if _has_login(out):
            return out, "keychain"
    return [], ""


def _goto_home(page, log=print, timeout=15000, url=None):
    """把标签页导航到指定抖音页面(默认个人主页),返回 (ok, err)。

    ⚠️ 绝不能用默认的 ``wait_until="load"``:抖音首页挂着长连接、埋点与
    风控 iframe,``load`` 事件经常几十秒都不触发。旧实现由此在 45 秒后抛
    ``Timeout 45000ms exceeded``,把一次**其实已经打开成功**的导航判成失败
    (实测同一时刻页面标题早已是「抖音精选电脑版…」),再去退化系统浏览器
    —— 用户感知就是「点了没反应,过一分半才冒出一个没登录的抖音」。

    改为 ``commit``(导航一提交就返回),超时后仍以地址栏是否已落到抖音域
    作为兜底判据:页面继续在浏览器里加载,不需要这里同步等它 load 完。
    """
    target = url or _MY_HOME_URL
    try:
        page.goto(target, wait_until="commit", timeout=timeout)
        return True, ""
    except Exception as e:
        cur = ""
        try:
            cur = page.url or ""
        except Exception:
            pass
        if _HOME_HOST_MARK in cur:
            log(f"[主页] 导航事件超时({type(e).__name__}),但地址已是 {cur},按成功处理")
            return True, ""
        return False, f"{type(e).__name__}: {e}"[:160]


def _focus_existing_home(ctx):
    """已有「我的抖音」页面就复用它(切到前台),避免连点堆出一堆窗口。

    只认 ``/user/self``。旧实现匹配任意 ``douyin.com``:用户正在看别人的主页或
    信息流时,点「打开我的抖音」只会把那一页顶到前台,**自己的主页根本没打开**,
    语义完全错位(2026-10-26 修)。
    """
    for pg in ctx.pages:
        try:
            if _MY_HOME_PATH in (pg.url or ""):
                return pg
        except Exception:
            continue
    return None


def _new_window_via_cdp(b, url):
    """在已运行的 Chrome 里新开一个**窗口**(不是标签页),返回 targetId(空=失败)。

    为什么要绕开 Playwright:``context.new_page()`` 只能往现有窗口里塞标签,会把
    用户窗口的标签栏改掉;能开窗口的只有浏览器级 ``Target.createTarget`` 的
    ``newWindow`` 参数。

    🔴 实测(Chrome 155,2026-10-26)这样建出来的 target **不会被 Playwright
    自动接管**:``connect_over_cdp`` 只在连接时枚举已存在的 target,事后新建的
    raw target 不挂 auto-attach。所以这里拿不到 Page 对象 —— 也正因此 URL 必须
    直接交给 createTarget,不能事后 goto。下一次点击会重新 connect,那时它才看得
    见这个页面(复用判定就靠这个,见 _focus_existing_home)。
    """
    try:
        sess = b.new_browser_cdp_session()
        r = sess.send("Target.createTarget",
                      {"url": url, "newWindow": True, "background": False})
        tid = (r or {}).get("targetId") or ""
        if tid:
            return tid
    except Exception as e:
        return ""      # 交给调用方退回新标签页
    return ""


def _forward_to_existing(url, log=print):
    """把 URL 交给专用 profile 的 Chrome(单例转发),在它里面开一个新窗口。

    只用于「进程活着但拿不到调试端口」的降级路径。以前这里会退化成
    ``webbrowser.open`` —— 那是**系统默认浏览器**(另一个 profile、没有登录态),
    等于弹出一个陌生的未登录抖音;而专用实例其实好好开着,只是读不到 CDP。

    带上 ``--remote-debugging-port``:如果其实是新起实例,端口就真的生效,顺手
    记进 cdp.json,下次点击能回到完整的 CDP 路径。
    """
    bin_path, name = find_browser()
    if not bin_path:
        return False
    port = _free_port()
    how = _spawn_browser(
        bin_path,
        [f"--remote-debugging-port={port}",
         f"--user-data-dir={PROFILE_DIR}"] + list(_BROWSER_FLAGS) +
        ["--new-window", url],
        log,
    )
    if not how:
        log("[主页] 转发到专用浏览器失败,改用系统浏览器")
        return False
    if _wait_cdp(port, timeout=3):
        _save_cdp_port(port)
    log(f"[主页] 已让 {name} 在专用 profile 中转发打开 {url}(本次无 CDP,未注入登录态)")
    return True


def open_home(log=print):
    """一键打开已登录抖音主页(监控页「打开我的抖音」按钮)。

    **底线:绝不关掉用户已经开着的页面。** 2026-10-26 之前这个按钮走
    ``launch_browser`` 的「参数版本落后 → 杀掉整个专用 Chrome 重开」逻辑,
    用户正在浏览的两三个抖音页会跟着一起消失。现在:
    1. 已有实例在跑 → 注入登录 cookie(幂等覆盖),**不杀进程、不换实例**;
    2. 已经开着「我的抖音」页面 → 直接切前台(不新开、**不刷新**,刷新等于把
       十几秒的重页面重载一遍);
    3. 有实例但没有可复用页面 → 用 CDP ``Target.createTarget(newWindow)``
       新开一个**窗口**,现有标签页一个都不动;
    4. 无实例 → 专用 profile 冷启动,复用它的空白页导航(不多开窗口);
    5. 拿不到 CDP → 让 Chrome 自己把 URL 转发进原实例开新窗口(只是不注入),
       **绝不**退化系统默认浏览器 —— 那是另一个 profile,等于弹出未登录的抖音。

    打开的页面:有登录态 → ``/user/self`` 个人主页(轻,且才是「我的」);
    无登录态 → 首页。首页那条会 302 到 /jingxuan 视频信息流,是全站最重的
    页面之一(实测 2947 节点 / DCL 10.8s),这就是「弹出来很卡」的主要来源。

    并发保护:同一时刻只跑一次,重复点击立刻返回 ``busy``,不排队不叠窗口。
    返回 {"ok", "mode"(cdp/forward/system), "injected", "cookie_src", "elapsed",
    "url"/"detail"/"error"}(busy 时带 "busy": True)。
    """
    if not _home_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "mode": "", "injected": False,
                "cookie_src": "",
                "error": "正在打开中,请稍候(首次启动浏览器约需 10~30 秒)"}
    t0 = time.time()
    try:
        r = _open_home(log)
    finally:
        _home_lock.release()
    r["elapsed"] = round(time.time() - t0, 1)
    # 诊断:浏览器主进程的调度优先级。继承了 launchd 后台 QoS 的实例是 4
    # (正常 31),表现为窗口只有十几帧且运行期无法补救 —— 与其让用户凭
    # 「感觉卡」反馈,不如把这条硬指标直接带回去。
    pid = _chrome_main_pid()
    if pid:
        pri = _proc_priority(pid)
        if pri is not None:
            r["chrome_pid"] = pid
            r["chrome_pri"] = pri
            if pri < 20:
                r["degraded"] = True
                note = (f"浏览器优先级只有 {pri}(正常 31),说明它仍继承了服务的后台 "
                        f"QoS,窗口会被压到十几帧;点「重开浏览器」重建实例即可恢复正常")
                r["detail"] = ("；".join(x for x in (r.get("detail"), note) if x))
                log(f"[主页] 警告:浏览器优先级 {pri}(正常 31),仍继承了后台 QoS,"
                    f"页面会被压到十几帧;点「重开浏览器」可重建实例")
    return r


def _open_home(log=print):
    cookies, src = _load_login_cookies()
    if not cookies:
        log("[主页] 无可用登录态(storage_state/钥匙串均无 sessionid),将以未登录态打开")
    bin_path, _name = find_browser()
    if not bin_path:
        # 无 Chrome 就无法注入登录态,不能让默认浏览器白开一个 about:blank
        try:
            if webbrowser.open(_HOME_URL):
                return {"ok": True, "mode": "system", "injected": False,
                        "cookie_src": "",
                        "detail": "未检测到 Chrome,已用系统默认浏览器打开(无登录态);如需登录态请先扫码登录"}
            return {"ok": False, "mode": "system", "injected": False,
                    "cookie_src": "", "error": "未能启动任何浏览器"}
        except Exception as e:
            return {"ok": False, "mode": "system", "injected": False,
                    "cookie_src": "", "error": f"启动默认浏览器失败: {e}"}

    # kill_stale=False:参数版本落后也不换实例(不关用户页面);open_tab=False:
    # 不要在复用路径上替他开空白标签,新窗口由下面自己开。
    ws, name, mode, err = launch_browser("about:blank", log,
                                         kill_stale=False, open_tab=False)
    # 有登录态 → 开个人主页(轻、且是「我的」);没有 → 首页
    target = _MY_HOME_URL if cookies else _HOME_URL
    if mode == "cdp" and ws:
        # 本地 CDP WebSocket 绝不能走代理:环境带 HTTP(S)_PROXY 时,playwright
        # 对 127.0.0.1 的 ws 会被代理劫持后 ECONNRESET(实测)。连接期间临时
        # 清掉代理变量,结束恢复,不影响进程其他网络行为。
        _proxy_keys = ("http_proxy", "https_proxy", "all_proxy",
                       "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        _saved = {k: os.environ.pop(k) for k in _proxy_keys if os.environ.get(k)}
        os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",127.0.0.1,localhost").lstrip(",")
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(ws)
                try:
                    ctx = b.contexts[0] if b.contexts else None
                    if ctx is None:
                        raise RuntimeError("浏览器无可用上下文")
                    if cookies:
                        try:
                            ctx.add_cookies(cookies)   # 幂等:同名字段直接覆盖
                        except Exception as e:
                            log(f"[主页] Cookie 注入失败({type(e).__name__}),按现有状态打开")
                    # ① 已经开着「我的抖音」→ 只切前台,**不刷新**(刷新等于把
                    # 这个重页面 250 个资源重新加载一遍,用户要等十几秒)。
                    page = _focus_existing_home(ctx)
                    if page is not None:
                        page.bring_to_front()
                        _activate_browser(name, log)
                        log(f"[主页] 复用已打开的「我的抖音」页面: {page.url}")
                        return {"ok": True, "mode": "cdp", "injected": bool(cookies),
                                "cookie_src": src, "url": page.url,
                                "reused": True,
                                "detail": f"已切到 {name} 里开着的「我的抖音」页面"
                                          + (f"(登录态来自{src})" if cookies else "")}
                    # ② 冷启动留下的空白页 → 直接导航,不再多开一个窗口
                    page = next((pg for pg in ctx.pages
                                 if (pg.url or "").startswith("about:blank")), None)
                    if page is not None:
                        ok, gerr = _goto_home(page, log, url=target)
                        if not ok:
                            raise RuntimeError(f"打开 {target} 失败: {gerr}")
                        page.bring_to_front()
                        _activate_browser(name, log)
                        return {"ok": True, "mode": "cdp", "injected": bool(cookies),
                                "cookie_src": src, "url": page.url, "reused": False,
                                "detail": f"已在 {name} 中打开{'我的抖音主页' if cookies else '抖音首页'}"
                                          + (f"(登录态来自{src})" if cookies else "")}
                    # ③ 有实例、但没有可复用页面 → 新开一个**窗口**,现有标签页不动
                    tid = _new_window_via_cdp(b, target)
                    if tid:
                        _activate_browser(name, log)
                        log(f"[主页] 已新开窗口打开 {target}(target {tid[:8]})")
                        return {"ok": True, "mode": "cdp", "injected": bool(cookies),
                                "cookie_src": src, "url": target,
                                "reused": False, "new_window": True,
                                "detail": f"已在 {name} 的新窗口打开{'我的抖音主页' if cookies else '抖音首页'}"
                                          + (f"(登录态来自{src})" if cookies else "")}
                    # ④ 兜底:新窗口开不出来(老 Chrome)才退回新标签页
                    log("[主页] 新窗口不可用,退回新标签页")
                    page = ctx.new_page()
                    ok, gerr = _goto_home(page, log, url=target)
                    if not ok:
                        raise RuntimeError(f"打开 {target} 失败: {gerr}")
                    page.bring_to_front()
                    _activate_browser(name, log)
                    return {"ok": True, "mode": "cdp", "injected": bool(cookies),
                            "cookie_src": src, "url": page.url, "reused": False,
                            "detail": f"已在 {name} 中打开{'我的抖音主页' if cookies else '抖音首页'}"
                                      + (f"(登录态来自{src})" if cookies else "")}
                finally:
                    pass          # 不断开浏览器
        except Exception as e:
            log(f"[主页] CDP 打开失败({e}),转发到原实例")
        finally:
            os.environ.update(_saved)

    # 拿不到 CDP(端口未就绪/注入失败):让 Chrome 把 URL 转发进原实例开新窗口。
    # **不能**退回 webbrowser.open —— 那是系统默认浏览器(另一个 profile、没有
    # 登录态),用户看到的是一个陌生的未登录抖音。
    if _forward_to_existing(target, log):
        return {"ok": True, "mode": "forward", "injected": False, "cookie_src": src,
                "url": target,
                "detail": "已在专用浏览器的新窗口打开"
                          + ("(本次未注入登录态;若页面不是登录态,点「重开浏览器」后重试)"
                             if cookies else "(未登录态)")}
    try:
        webbrowser.open(_HOME_URL)
    except Exception as e:
        return {"ok": False, "mode": "system", "injected": False,
                "cookie_src": src, "error": f"打开浏览器失败: {e}"}
    return {"ok": True, "mode": "system", "injected": False, "cookie_src": src,
            "detail": "已用系统默认浏览器打开"
                      + ("(无法注入登录态,可能未登录)" if cookies else "(未登录态)")
                      + (f";原错误: {err}" if err else "")}


# ── 登录 ────────────────────────────────────────────────────
def _run_browser_login(timeout, cancel=None, log=print):
    """打开登录页并等待扫码完成。返回 (cookies, error)。

    在独立线程里执行(阻塞式),不占用 asyncio 事件循环。
    浏览器窗口**不会被自动关闭** —— 由用户自己决定何时关。

    整体分四个阶段:
      A. 页面就绪:等待 douyin.com 首页真正渲染(避免一打开就找按钮)
      B. 验证码绕路:若命中验证码中间页,等用户在浏览器里手动过滑块
      C. 打开登录面板:点「登录」入口,弹出二维码
      D. 等待扫码:轮询 Cookie 是否出现 sessionid
    """
    ws, bname, mode, err = launch_browser(LOGIN_URL, log=log)
    if err:
        return None, err
    _login_info["browser"] = bname
    _login_info["phase"] = "waiting"
    if mode != "cdp":
        # 浏览器已打开但拿不到 CDP:交给前端提示用户手动粘贴 Cookie
        return None, "MANUAL_REQUIRED"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "MANUAL_REQUIRED"

    cookies = None
    cdp = None                 # 媒体屏蔽用的 CDP session(finally 里要引用)
    last_phase_log = 0.0
    try:
        with sync_playwright() as p:
            # 连到用户真实看到的系统 Chrome,而不是另起一个自动化 Chromium
            browser = p.chromium.connect_over_cdp(ws)
            try:
                ctx = browser.contexts[0] if browser.contexts else None
                if ctx is None:
                    return None, "CDP 已连接但没有可用浏览器上下文"

                # 隐身 JS 通过 init script 注入,等价于曾经的
                # --disable-blink-features=AutomationControlled,但不会触发
                # Chrome「不受支持的命令行标记」警告条。
                try:
                    ctx.add_init_script(_STEALTH_JS)
                except Exception as e:
                    log(f"[登录] 注入隐身脚本失败(可忽略): {type(e).__name__}")

                # 找可用标签页 —— 只认 www 主站,避免复用 live./creator. 等
                # 不会自动弹登录面板的子域(那会导致二维码迟迟不出)
                page = None
                for pg in ctx.pages:
                    if _is_login_host(pg.url):
                        page = pg
                        break
                if page is None:
                    page = ctx.new_page()
                # 无论复用还是新开,都强制回到登录目标页:
                # 复用页可能停在任意中间状态(信息流/个人页/直播页)
                try:
                    page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    log(f"[登录] 打开登录页失败: {type(e).__name__}: {str(e)[:120]}")
                try:
                    page.bring_to_front()
                except Exception:
                    pass

                # 阻断视频流量 —— 页面轻了,二维码与登录面板才能立刻渲染
                cdp = _setup_media_block(page)
                if cdp is not None:
                    log("[登录] 已临时屏蔽视频资源以加速登录页渲染")

                # —— A 阶段:等首页真正渲染出来 ——
                state, info = _wait_page_ready(page, timeout=15, log=log)
                if state == "captcha":
                    _login_info["phase"] = "captcha"
                    log(f"[登录] 命中验证码中间页({info}),请在浏览器里手动完成滑块")
                    cap_budget = max(30, min(60, timeout - 90))
                    if not _wait_captcha_clear(page, timeout=cap_budget,
                                                cancel=cancel, log=log):
                        if cancel is not None and cancel.is_set():
                            return None, "已取消"
                        return None, "验证码未在时限内通过,请重试"
                    # 滑块过后页面会跳到首页,等待渲染
                    state, info = _wait_page_ready(page, timeout=15, log=log)
                log(f"[登录] 页面阶段={state} | {info}")

                # —— C 阶段:找登录入口,打开登录面板 ——
                login_phase_done = False
                if state == "ready":
                    # 先看是否已经在登录面板里(刷新后的场景)
                    if not _qr_ready(page):
                        if _open_login_panel(page, log=log):
                            login_phase_done = True
                        else:
                            log("[登录] 未找到「登录」入口 —— 若二维码未出现,"
                                "请手动点击页面右上角「登录」")
                    else:
                        login_phase_done = True
                else:
                    log(f"[登录] 页面未就绪({info}),继续等待扫码...")

                # —— D 阶段:轮询 Cookie ——
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if cancel is not None and cancel.is_set():
                        return None, "已取消"
                    now = time.time()
                    # 每 10 秒打印一次状态,让用户知道后台还在等
                    if now - last_phase_log >= 10:
                        remain = int(deadline - now)
                        captcha_now = _is_captcha(page)
                        qr_now = _qr_ready(page)
                        log(f"[登录] 等待扫码完成 (剩余 {remain}s, "
                            f"验证码={captcha_now}, 二维码={qr_now})")
                        last_phase_log = now
                        # 用户可能手动点了登录,这里再补一次
                        if not login_phase_done and not captcha_now and not qr_now:
                            if _open_login_panel(page, log=log):
                                login_phase_done = True
                    try:
                        ck = ctx.cookies()
                    except Exception:
                        ck = None
                    if _has_login(ck):
                        cookies = ck
                        break
                    time.sleep(2)

                if cookies:
                    try:
                        ctx.storage_state(
                            path=os.path.join(AUTH_DIR, "storage_state.json"))
                    except Exception:
                        pass
                    # 登录完成 —— 立刻解除视频屏蔽,把流畅的窗口还给用户
                    _clear_media_block(cdp)
                    log("[登录] 已解除视频屏蔽,浏览器可正常浏览")
            finally:
                # 只断开连接,不关闭浏览器。窗口去留由用户决定 ——
                # 曾在这里 ctx.close() 强制关窗,用户看到的就是"几秒后自动消失"。
                # 超时/取消路径也要解除屏蔽,否则窗口会一直停在无视频的残缺状态。
                if not cookies:
                    _clear_media_block(cdp)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:300]

    if not cookies:
        return None, f"等待超时({timeout}s),未完成扫码"
    return cookies, ""


def _open_login_panel(page, log=print):
    """点击页面上的「登录」按钮,让二维码面板渲染出来。

    抖音首页默认只展示信息流,二维码是点击登录按钮后才由 JS 动态插入的;
    不点这一步,页面上唯一的 canvas 是隐藏的视频播放画布,扫不了。

    抖音的「登录」入口会按页面状态呈现成 <button> / <p> / <div> / <span>
    不同标签,且文本可能是「登录」或旧版「登陆」。这里按宽→窄顺序依次尝试。
    """
    selectors = (
        'button:has-text("登录")',
        'button:has-text("登陆")',
        'p:has-text("登录")',
        'p:has-text("登陆")',
        'span:has-text("登录")',
        'div:has-text("登录") >> visible=true',
        '[data-e2e="login-button"]',
        '[id*="login" i]:has-text("登录")',
    )
    # 第一轮:纯按可见的文本定位,Playwright 会自动等元素出现再操作
    try:
        loc = page.get_by_text("登录", exact=True).first
        if loc.count() and loc.is_visible():
            for kwargs in ({"timeout": 8000}, {"timeout": 6000, "force": True}):
                try:
                    loc.click(**kwargs)
                    page.wait_for_timeout(2500)
                    log("[登录] 已打开登录面板(文本),二维码应已显示")
                    return True
                except Exception:
                    continue
            # 最后一招:JS 触发点击
            try:
                h = loc.element_handle()
                if h is not None:
                    page.evaluate("el => el.click()", h)
                    page.wait_for_timeout(2500)
                    log("[登录] 已打开登录面板(JS 触发)")
                    return True
            except Exception:
                pass
    except Exception:
        pass

    # 兜底:逐个候选选择器再试一遍,覆盖结构变化
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count() or not loc.is_visible():
                continue
            for kwargs in ({"timeout": 6000}, {"timeout": 5000, "force": True}):
                try:
                    loc.click(**kwargs)
                    page.wait_for_timeout(2500)
                    log(f"[登录] 已打开登录面板({sel}),二维码应已显示")
                    return True
                except Exception:
                    continue
            try:
                h = loc.element_handle()
                if h is not None:
                    page.evaluate("el => el.click()", h)
                    page.wait_for_timeout(2500)
                    log(f"[登录] 已打开登录面板({sel}/JS)")
                    return True
            except Exception:
                continue
        except Exception:
            continue
    return False


def _qr_ready(page):
    """二维码是否已出现。**仅用于日志提示**,不参与任何流程判定 ——

    登录成功与否只认 Cookie 里的 sessionid,页面猜不来也不该猜。
    这里既看主页面文本,又遍历所有 frame 找 80×80 以上的 canvas/img/svg,
    以兼容抖音把二维码嵌进 SSO 子 frame 的场景。
    """
    try:
        txt = page.inner_text("body") or ""
        if any(k in txt for k in ("扫码登录", "扫码", "二维码")):
            return True
    except Exception:
        pass
    for f in page.frames:
        try:
            ftxt = (f.inner_text("body") if hasattr(f, "inner_text") else "") or ""
            if any(k in ftxt for k in ("扫码登录", "扫码", "二维码")):
                return True
        except Exception:
            pass
        for sel in ("canvas", "img", "svg"):
            try:
                loc = f.locator(sel).first
                if not loc.count() or not loc.is_visible():
                    continue
                bb = loc.bounding_box()
                if bb and bb.get("width", 0) >= 80 and bb.get("height", 0) >= 80:
                    return True
            except Exception:
                continue
    return False


def login(timeout=180, wait=False, log=print):
    """启动扫码登录。

    wait=False 时立即返回(前端轮询 status);wait=True 时阻塞等结果。
    同一时刻只允许一个登录流程。
    """
    global _login_thread, _cancel_event, _login_info
    with _lock:
        if _login_thread is not None and _login_thread.is_alive():
            return {"ok": False, "error": "登录流程已在进行中"}

    result = {}
    _cancel_event = threading.Event()
    cancel = _cancel_event
    _login_info = {"phase": "starting", "browser": "", "error": ""}

    def runner():
        cookies, err = _run_browser_login(timeout, cancel=cancel, log=log)
        if err == "MANUAL_REQUIRED":
            # 浏览器已打开,但拿不到 Cookie —— 让用户手动粘贴
            _login_info.update({"phase": "manual", "error": ""})
            result.update({"ok": False, "manual_required": True,
                           "error": "无法自动读取浏览器 Cookie,请手动粘贴"})
            return
        if err:
            log(f"[登录] 未完成: {err}")
            result["ok"] = False
            result["error"] = err
            meta = _load_meta()
            meta["error"] = err
            _save_meta(meta)
            # 用户主动取消不算失败,前端不再提示"出错了"
            if err == "已取消":
                _login_info.update({"phase": "cancelled", "error": ""})
            else:
                _login_info.update({"phase": "failed", "error": err})
            return
        ck = _cookie_string(cookies)
        ok, kerr = _keychain_set(ck)
        meta = _load_meta()
        meta.update({
            "last_ok": time.time(),
            "last_check": time.time(),
            "expire_at": _expire_at(cookies),
            "expired": False,
            "error": "" if ok else f"钥匙串写入失败: {kerr}",
            "fields": len(cookies),
        })
        _save_meta(meta)
        result.update({"ok": True, "error": "" if ok else meta["error"],
                       "fields": len(cookies)})
        _login_info.update({"phase": "done", "error": ""})
        if ok:
            log(f"[登录] 成功,已保存 {len(cookies)} 个 Cookie 字段到钥匙串")
        else:
            log(f"[登录] Cookie 已获取但写入钥匙串失败: {kerr}")

    t = threading.Thread(target=runner, daemon=True)
    with _lock:
        _login_thread = t
    t.start()
    if wait:
        t.join(timeout + 30)
        return result if result else {"ok": False, "error": "登录超时"}
    return {"ok": True, "started": True}


def cancel_login():
    """用户主动取消登录(不会关闭浏览器窗口,只停止等待)。"""
    global _cancel_event, _login_info
    if _cancel_event is not None:
        _cancel_event.set()
    _login_info["phase"] = "cancelled"
    return {"ok": True}


def set_manual_cookie(cookie_str, log=print):
    """手动粘贴 Cookie 兜底:浏览器无法自动回读时使用。

    只接受含 sessionid 的完整 Cookie 串,避免把匿名 Cookie 存进去。
    返回 (是否成功, 错误)。
    """
    ck = (cookie_str or "").strip()
    if not ck:
        return False, "Cookie 为空"
    if "sessionid=" not in ck:
        return False, "Cookie 中未找到 sessionid,请确认已登录后再完整复制"
    ok, kerr = _keychain_set(ck)
    if not ok:
        return False, f"钥匙串写入失败: {kerr}"
    meta = _load_meta()
    meta.update({"last_ok": time.time(), "last_check": time.time(),
                 "expired": False, "error": "", "manual": True})
    _save_meta(meta)
    log("[登录] 手动粘贴的 Cookie 已保存")
    return True, ""


def restart_browser(log=print):
    """显式关闭专用浏览器,让下次打开按当前启动参数重建(WebUI「重开浏览器」)。

    存在的理由:一键打开主页已经**永不杀进程**(见 ``_can_reuse_running_instance``
    的 ``kill_stale=False``),所以 ``LAUNCH_ARGS_REV`` 落后时不会再自动换实例 ——
    那正是 2026-10-26「点一下把用户页面全关掉」的根因。真要应用新的 Chrome 开关,
    只能由用户显式确认后关掉它,这就是那个入口。

    不顺手重开:开一个空白窗口对用户没价值,下次点「打开我的抖音」自然会带着新
    参数冷启动。扫码登录进行中直接拒绝(结束浏览器会打断扫码)。
    """
    if _login_thread is not None and _login_thread.is_alive():
        return {"ok": False, "killed": 0, "busy": True,
                "error": "正在扫码登录,结束浏览器会打断扫码,请稍后再试"}
    killed = _kill_profile_chrome()
    if killed:
        log(f"[主页] 按用户要求关闭专用浏览器({killed} 个进程),"
            f"下次打开按 v{LAUNCH_ARGS_REV} 参数重建")
        return {"ok": True, "killed": killed,
                "detail": f"已关闭专用浏览器({killed} 个进程);"
                          f"下次点「打开我的抖音」会按新的启动参数重建"}
    return {"ok": True, "killed": 0, "detail": "专用浏览器本来就没在运行"}


def logout():
    """清除登录态(Cookie + 元数据 + 浏览器 profile)。

    ⚠️ 必须**显式**结束专用浏览器:以前是靠「删掉 cdp.json → 下次启动判定参数
    版本不匹配 → 顺手杀进程」这个隐式副作用收尾的。一键打开主页改成「永不杀
    进程」之后那层保险没了,不显式杀就会留下一台前台还登录着的浏览器 —— 用户
    以为退出成功了,其实抖音还是登录态。
    """
    _keychain_delete()
    for p in (META_PATH, os.path.join(AUTH_DIR, "storage_state.json"),
              _cdp_state_path()):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    _kill_profile_chrome()
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    return {"ok": True}


# ── 判活与巡检 ──────────────────────────────────────────────
def _first_web_rid(cfg):
    for m in (cfg or {}).get("monitors", []) or []:
        rid = str(m.get("web_rid") or "").strip()
        if rid:
            return rid
    return ""


async def verify(client, cfg):
    """真实判活:用当前 Cookie 请求一次房间状态 API。

    不影响录制/弹幕 —— 只发一个业务读请求,不进入直播间。
    """
    det = (cfg or {}).get("detection") or {}
    cookie = resolve_cookie(det)
    meta = _load_meta()
    if not cookie:
        meta.update({"expired": True, "error": "未登录"})
        _save_meta(meta)
        return {"ok": False, "state": "none", "reason": "未登录"}

    exp = meta.get("expire_at")
    if exp and exp > 0 and time.time() > exp:
        meta.update({"expired": True, "error": "登录态已过期,请重新扫码"})
        _save_meta(meta)
        return {"ok": False, "state": "expired", "reason": "登录态已过期"}

    rid = _first_web_rid(cfg)
    if not rid:
        return {"ok": False, "state": "unknown", "reason": "无可用主播房间"}

    try:
        import detect as detect_mod
        r = await detect_mod.check_api(
            client, rid, cookie,
            det.get("api_base", "https://live.douyin.com/webcast/room/web/enter/"))
    except Exception as e:
        return {"ok": False, "state": "unknown", "reason": f"{type(e).__name__}: {e}"[:200]}

    # check_api 返回 error 字段通常意味着鉴权/参数异常
    err = r.get("error")
    if err and "HTTP 4" in str(err):
        meta.update({"expired": True, "error": f"登录态失效({err})",
                     "last_check": time.time()})
        _save_meta(meta)
        return {"ok": False, "state": "expired", "reason": str(err)}
    meta.update({"last_check": time.time(), "last_ok": time.time(),
                 "expired": False, "error": ""})
    _save_meta(meta)
    return {"ok": True, "state": "ok", "reason": ""}


async def _notify(cfg, title, detail):
    """失效告警 —— 统一走 notify 通道层(企微连接器/群机器人,由 gift_watch.notify_channel 决定)。

    旧版写死飞书 payload(msg_type/content.text)且依赖单独的 auth.webhook_url,
    与告警通道层双轨且配置分散 —— 已收编:全项目告警只认 notify.py 一套,
    gift_watch 的通道配置实际是「全局告警通道」。发不出只返回 False,绝不抛异常。
    """
    try:
        import notify as notify_mod
        gw = (cfg or {}).get("gift_watch") or {}
        r = await notify_mod.push_by_config(gw, f"[抖音监控] {title}", detail, local=True)
        return bool(r.get("ok"))
    except Exception:
        return False


async def patrol_loop(state, log=print):
    """登录态巡检:每 10~14 小时(默认 12h ± 2h 抖动)校验一次。

    抖动是为了避免固定时刻请求被识别为机器行为。
    失效时:写 meta + 可选 webhook 告警;检测通道自动退化为纯 302,录制不受影响。
    """
    import asyncio
    import random
    while True:
        try:
            health.beat("auth")   # 健康面板:登录态守护存活心跳
            cfg = state.get_config()
            auth_cfg = cfg.get("auth") or {}
            if not auth_cfg.get("enabled", True):
                await asyncio.sleep(3600)
                continue
            base_h = float(auth_cfg.get("check_interval_hours", 12) or 12)
            jitter = random.uniform(-2, 2)
            wait_s = max(600, (base_h + jitter) * 3600)
            await asyncio.sleep(wait_s)
            try:
                import httpx
                async with httpx.AsyncClient(
                        timeout=10, follow_redirects=False,
                        trust_env=False) as client:
                    r = await verify(client, cfg)
            except Exception as e:
                r = {"ok": False, "reason": str(e)[:200]}
            if r.get("ok"):
                log(f"[登录态] 校验通过(下次约 {base_h:.0f}h 后)")
            else:
                msg = f"{r.get('state', '?')}: {r.get('reason', '')}"
                log(f"[登录态] 校验失败 — {msg} — 检测已降级为纯 302,录制不受影响")
                await _notify(cfg, "抖音登录态失效",
                              f"{msg}\n请在 Web 面板「高级参数 → 抖音登录」重新扫码。")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[登录态] 巡检异常: {type(e).__name__}: {e}"[:200])
            await asyncio.sleep(1800)
