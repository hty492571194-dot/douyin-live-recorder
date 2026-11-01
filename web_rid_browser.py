#!/usr/bin/env python3
"""缺号主播 web_rid 抓取器 —— 唯一 import playwright 的模块。

为什么要浏览器
--------------
实测(2026-10-26):`web_rid` 只在**渲染后的主页**里出现(DOM 中「直播中」入口的
href = `live.douyin.com/<web_rid>`)。抖音号/主页链接解析、以及页面自己发的
`aweme/v1/web/user/profile/other` 接口都拿不到它(接口里 `web_rid` 为 null)。
所以「开播后自动补 web_rid」绕不开一次真实浏览器渲染。

反爬要点(每一条都有代价,别随手改)
--------------------------------
1. **持久化 user-data-dir**(`state/browser/rid/`):ttwid/msToken 自然累积,当第二次
   起就不再是"全新访客"。与 `auth.py` 的扫码浏览器**完全不同目录**,互不干扰。
2. **固定指纹**:UA 由本机内核版本派生(HeadlessChrome → Chrome),locale/时区/viewport
   固定,`navigator.webdriver` 抹掉。
3. **只等判定**:`domcontentloaded` 后每 350ms 扫一次 DOM,判定出结果立刻返回
   (实测在播 1.3s / 离线 5s),不滚动、不点击、不触发视频播放。
4. **阻断重资源**:image/media/font 直接 abort,只放 document/script/xhr/fetch ——
   更快出 DOM、流量更低。**不许阻断 XHR**:判定要读 profile 接口。
5. **不注入登录 Cookie**:保护真实账号,也不占 cookie_guard 配额。
6. **归属校验**:拿到 rid 后用**移动 UA** 请求 `live.douyin.com/<rid>` 看 302 落地的
   `sec_user_id` 是否等于目标主播。桌面 UA 不会 302(返回 200 SPA),写错就静默失效。

与方案文档的一处偏差:方案里写的是"常驻浏览器 + 空闲 20 分钟关闭",实现改成
**每次用完即关**。理由:cookie 连续性靠 user-data-dir 而不是进程常驻,而常驻进程
在无人值守 4-5 天里是实打实的内存与僵尸进程风险;单次冷启动只多 ~0.3s。
"""

import os
import re
import time

import httpx

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

# 相对项目根;调用方(monitor)会传绝对路径,单测/CLI 用默认值也行
DEFAULT_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state", "browser", "rid")

BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
PROFILE_XHR_PATH = "/aweme/v1/web/user/profile/other/"
POLL_MS = 350
MIN_PAGE_BYTES = 20480        # 小于这个体积基本是空壳页(正常主页 ≥70KB)

# 扫描 DOM:一次性把「直播入口 rid / 页面体积 / 可见文本」取回来。
# 在页面内跑正则而不是把整页 HTML 传出来 —— 主页 outerHTML 上兆,回传会明显变慢。
_SCAN_JS = """
() => {
  const h = document.documentElement ? document.documentElement.outerHTML : "";
  const rids = [];
  const re = /live\\.douyin\\.com\\/(\\d+)/g;
  let m;
  while ((m = re.exec(h)) !== null) {
    if (rids.indexOf(m[1]) < 0) rids.push(m[1]);
    if (rids.length >= 5) break;
  }
  const body = document.body ? document.body.innerText : "";
  return {rids: rids, bytes: h.length, text: body.slice(0, 4000)};
}
"""

# 抹掉最显眼的自动化痕迹。navigator.webdriver 是 Playwright/CDP 默认会暴露的字段,
# 抖音前端是否据此判定不好说,但成本几乎为 0,没有理由留着。
_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
"""

_UA_CACHE = ""


def _launch_args():
    return [
        "--no-proxy-server",                       # 抖音必须直连;本项目有过代理把服务打停的事故
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]


def _resolve_ua(p, user_data_dir):
    """按本机内核版本派生一个"非 headless"的 UA(缓存,每进程只探一次)。

    直接用写死的版本号迟早会跟 playwright 升级脱节;UA 里带 HeadlessChrome 又等于
    自报家门。折中办法:起一次默认上下文读真实 UA,把 HeadlessChrome 换成 Chrome。
    """
    global _UA_CACHE
    if _UA_CACHE:
        return _UA_CACHE
    ctx = None
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir, headless=True, args=_launch_args(),
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ua = page.evaluate("navigator.userAgent")
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    _UA_CACHE = (ua or "").replace("HeadlessChrome", "Chrome")
    return _UA_CACHE


def verify_owner(rid, expect_sec_uid, timeout=8):
    """校验 rid 是否属于该主播:返回 ("same"|"other"|"unknown", detail)。

    必须用**移动 UA**:`live.douyin.com/<rid>` 桌面 UA 返回 200 SPA(不 302),
    用桌面 UA 会永远拿不到 location,静默失效。
    """
    rid = str(rid or "").strip()
    if not rid.isdigit():
        return "unknown", "rid 非数字"
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as c:
            resp = c.get(f"https://live.douyin.com/{rid}",
                         headers={"User-Agent": UA_MOBILE,
                                  "Referer": "https://live.douyin.com/"})
    except Exception as e:  # noqa: BLE001
        return "unknown", f"校验请求失败:{type(e).__name__}"
    loc = resp.headers.get("location", "") or ""
    m = re.search(r"sec_user_id=([^&\s]+)", loc)
    if not m:
        return "unknown", f"HTTP {resp.status_code} 无 302 落地(无法校验归属)"
    got = m.group(1)
    return ("same" if got == str(expect_sec_uid or "") else "other"), f"landed={got[:16]}…"


def fetch_web_rid(sec_uid, *, timeout_sec=45, user_data_dir="", verify=True):
    """打开主播主页,判定 live / offline / unknown 并尽可能带回 web_rid。

    返回:`{"verdict": "live"|"offline"|"unknown", "rid": str|None,
            "reason": str|None, "detail": str, "elapsed_ms": int}`

    同步函数 —— 调用方必须用 `asyncio.to_thread(...)` 包起来。`sync_playwright`
    会阻塞整个事件循环,直接在协程里调会把检测/录制一起卡死(本项目踩过同类坑)。
    """
    import web_rid_probe as probe        # 纯逻辑在同级模块;放函数内避免循环 import
    t0 = time.time()
    sec_uid = str(sec_uid or "").strip()

    def done(verdict, rid, reason, detail):
        return {"verdict": verdict, "rid": rid, "reason": reason, "detail": detail,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    if not re.match(r"^MS4wLjAB[A-Za-z0-9_\-]{10,}$", sec_uid):
        return done("unknown", None, "unknown", "sec_uid 形态非法")

    profile_dir = user_data_dir or DEFAULT_PROFILE_DIR
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except OSError as e:
        return done("unknown", None, "browser_crash", f"无法创建 profile 目录:{e}")

    deadline = t0 + max(10, timeout_sec)
    signals = {"profile_ok": False, "xhr_status": None, "profile_room_id_str": "",
               "page_bytes": 0, "see_text": "", "dom_rids": [],
               "http_status": 200, "nav_error": ""}
    playwright = None
    ctx = None
    try:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
        ua = _resolve_ua(playwright, profile_dir)
        ctx = playwright.chromium.launch_persistent_context(
            profile_dir, headless=True, args=_launch_args(),
            user_agent=ua or None,
            locale="zh-CN", timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        ctx.add_init_script(_INIT_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def _on_response(resp):
            if PROFILE_XHR_PATH not in resp.url:
                return
            try:
                signals["xhr_status"] = resp.status
                body = resp.json() or {}
                user = body.get("user") or {}
                signals["profile_ok"] = resp.status == 200 and bool(user)
                signals["profile_room_id_str"] = str(user.get("room_id_str") or "")
            except Exception:  # noqa: BLE001
                signals["xhr_status"] = getattr(resp, "status", None)

        def _route(route):
            try:
                if route.request.resource_type in BLOCK_RESOURCE_TYPES:
                    route.abort()
                else:
                    route.continue_()
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_response)
        page.route("**/*", _route)
        try:
            resp = page.goto(f"https://www.douyin.com/user/{sec_uid}",
                             wait_until="domcontentloaded",
                             timeout=int(min(30000, max(8000, (deadline - time.time()) * 1000))))
            if resp is not None:
                signals["http_status"] = resp.status
        except Exception as e:  # noqa: BLE001
            signals["nav_error"] = f"{type(e).__name__}: {str(e)[:120]}"

        # 轮询到判定出结果为止;超时按 timeout 记(不是 offline!)
        verdict = rid = reason = None
        while time.time() < deadline:
            try:
                sc = page.evaluate(_SCAN_JS) or {}
            except Exception as e:  # noqa: BLE001
                signals["nav_error"] = signals["nav_error"] or f"evaluate:{type(e).__name__}"
                sc = {}
            signals["dom_rids"] = sc.get("rids") or []
            signals["page_bytes"] = int(sc.get("bytes") or 0)
            signals["see_text"] = sc.get("text") or ""
            if "captcha" in page.url or "/verify" in page.url:
                signals["see_text"] = (signals["see_text"] or "") + " 请完成下列验证"
            verdict, rid, reason = probe.decide_verdict(signals)
            # offline 要再等一小会儿:直播入口的渲染可能晚于 profile 接口
            if verdict == "live":
                break
            if verdict == "offline" and time.time() - t0 > 3.0:
                break
            if verdict == "unknown" and reason in ("captcha", "login_wall",
                                                   "http_429", "http_403"):
                break
            page.wait_for_timeout(POLL_MS)

        if verdict is None:
            verdict, rid, reason = "unknown", None, "timeout"
        if verdict == "offline" and (signals["page_bytes"] or 0) < MIN_PAGE_BYTES:
            # 空壳页不能当 offline —— 那只是没渲染出来
            verdict, reason = "unknown", "empty_shell"
        if verdict == "timeout":
            verdict, rid, reason = "unknown", None, "timeout"

        detail = (f"bytes={signals['page_bytes']} xhr={signals['xhr_status']} "
                  f"room={signals['profile_room_id_str'] or '-'} "
                  f"http={signals['http_status']}")

        if verdict == "live" and rid:
            if verify:
                state, why = verify_owner(rid, sec_uid)
                detail += f" owner={state}({why})"
                if state == "other":
                    return done("unknown", rid, "rid_mismatch", detail)
                if state == "unknown":
                    # 校验不了不拦(网络抖动),但要在 detail 里留下痕迹
                    detail += " owner-unverified"
            return done("live", str(rid), None, detail)

        return done(verdict, None, reason, detail)

    except Exception as e:  # noqa: BLE001
        return done("unknown", None, "browser_crash", f"{type(e).__name__}: {str(e)[:150]}")
    finally:
        # 一定要收干净:chromium 留着不关就是内存泄漏 + 僵尸进程,无人值守几天会堆满
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass
