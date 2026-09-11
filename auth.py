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


def _cdp_state_path():
    return os.path.join(AUTH_DIR, "cdp.json")


def _cdp_ports():
    """近期用过的调试端口,按「最近优先」排列。

    只记一个端口是不够的:那份记录一旦过期(实例已退出、或被别的流程覆盖),
    就会连不回**仍在运行且已登录**的那个 Chrome,只能干等新端口就绪。
    """
    try:
        with open(_cdp_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return []
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
            json.dump({"port": port, "recent": recent[:8]}, f)
        os.replace(tmp, _cdp_state_path())
    except Exception:
        pass


def _kill_profile_chrome():
    """结束占用登录 profile 的 Chrome 进程,返回被结束的进程数。

    Chrome 对同一个 user-data-dir 只允许一个实例:后启动的进程只会把 URL
    转发给已有实例然后自己退出,于是新传的 ``--remote-debugging-port``
    永远不会监听。所以只要记录的端口失了效,就会卡死在
    「端口未就绪 → 转手动粘贴」,浏览器里明明已登录却读不到 Cookie。
    这里主动接管那个实例,让新端口能真正生效。
    """
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
            return 0
    pids = []
    for line in out.splitlines():
        if "Chrome" not in line or f"--user-data-dir={prof}" not in line:
            continue
        try:
            pids.append(int(line.split()[0]))
        except Exception:
            continue
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
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


def launch_browser(url, log=print):
    """用系统浏览器打开登录页,并返回 CDP 地址用于回读 Cookie。

    返回 (ws_url, browser_name, mode, error):
      mode = "cdp"    —— 已用系统 Chrome 打开,且可以通过 CDP 读取 Cookie
      mode = "system" —— 已用系统默认浏览器打开,但无法自动回读,需用户手动粘贴
    失败时 ws_url 为 ""。

    同一个 profile 已经开着 Chrome 时,再次传 --remote-debugging-port 会被
    Chrome 忽略(该开关只在首次启动生效),所以这里先复用上次记录的端口:
    复用成功就在那个 Chrome 里直接新开标签页 —— 这正是期望的扫码体验。
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
    # 记录可能过期(实例已退出或被别的流程覆盖),所以近期端口逐个试。
    for prev_port in _cdp_ports():
        ws = _wait_cdp(prev_port, timeout=3)
        if ws:
            log(f"[登录] 复用已打开的 {name}(调试端口 {prev_port}),在新标签页中打开登录页")
            _open_tab_via_cdp(ws, url)
            return ws, name, "cdp", ""

    port = _free_port()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # 启动参数全部使用 Chrome 官方支持的开关,任何 Blink 特性开关(如
    # --disable-blink-features=AutomationControlled)都会触发 Chrome 的
    # 「不受支持的命令行标记」警告条(见 bad_flags_prompt.cc)。
    # 反自动化效果改由 CDP 的 addScriptToEvaluateOnNewDocument 注入。
    args = [
        bin_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    def _spawn():
        try:
            subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),  # 脱离本进程组,服务重启不影响它
            )
            return True
        except Exception:
            return False

    if not _spawn():
        return "", name, "", f"启动 {name} 失败"

    ws = _wait_cdp(port, timeout=12)
    if not ws:
        # 走到这里,几乎总是「同 profile 的 Chrome 还活着」:新进程把 URL 转发
        # 给它后就退出了,新端口永远不会监听。先接管旧实例再重新拉起,
        # 否则只能退化成手动粘贴 —— 浏览器里明明已登录却读不到 Cookie。
        killed = _kill_profile_chrome()
        if killed:
            log(f"[登录] 已结束占用登录配置的旧 {name} 进程({killed} 个),重新启动")
            time.sleep(2.5)
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


def logout():
    """清除登录态(Cookie + 元数据 + 浏览器 profile)。"""
    _keychain_delete()
    for p in (META_PATH, os.path.join(AUTH_DIR, "storage_state.json"),
              _cdp_state_path()):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
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
    """失效告警。配置了 webhook 就推送,否则只落日志/meta。"""
    url = ((cfg or {}).get("auth") or {}).get("webhook_url", "").strip()
    if not url:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, trust_env=False) as c:
            await c.post(url, json={"msg_type": "text", "content": {
                "text": f"[抖音监控] {title}\n{detail}"}})
        return True
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
