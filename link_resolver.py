#!/usr/bin/env python3
"""抖音分享链接 / 抖音号解析:→ sec_uid / web_rid / nickname / douyin_id。

由 douyin-link-parser 项目的解析逻辑移植,改用 httpx(本项目已有依赖)。
同步实现,供 webui 的 POST /api/resolve 直接调用。

三条入口(2026-10-26 扩):
1. 链接:v.douyin.com / live.douyin.com / 主页链接 / 带链接的整段分享文案
   → 跟随 302 抽 sec_uid(直播间链接顺带抽 web_rid);
2. 抖音号:纯数字或字母数字(`12345678` / `anchor_99` / `a.b`)反查,拿到 sec_uid + 昵称;
3. 主页链接进来的,再补一次 `user/info?sec_uid=` 拿昵称和抖音号 —— 因为
   www.douyin.com/user/* 和 iesdouyin/share/user/* 现在返回的都是 72KB 的
   JS 空壳(VM 混淆脚本,body 里没有 og:title,也没有 room 数据),HTML 里
   已经刮不出昵称了(实测 2026-10-26)。

⚠️ **web_rid 只能从「直播间链接」里拿**,主页链接 / 抖音号都拿不到。实测 2026-10-26:
主播在播时 user/info 也不返回任何 room 字段;reflow/info 只认 room_id、
enter API 只认 web_rid,把 sec_uid 传进去一律 10011(params error);
`live.douyin.com/{sec_uid}` 即使在播也返回 200 而非 302。
web_rid 对同一主播长期稳定,所以离线添加时先空着,等开播时粘一次直播间
分享链接补上即可(monitor 每轮热读 config,补完无需重启)。
"""
import html
import re
import urllib.parse

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://live.douyin.com/",
}

# 用户资料接口(旧版 iesdouyin,免签名、免 Cookie,2026-10-26 实测可用)。
# sec_uid 和 unique_id 两种入参都认,但**只返回昵称/抖音号,没有直播间信息**。
USER_INFO_API = "https://www.iesdouyin.com/web/api/v2/user/info/"

_SEC_UID_RE = re.compile(r"MS4wLjAB[A-Za-z0-9_\-]{10,}")
# 抖音号:纯数字、或字母数字下划线点。排除 URL 形态与过短/过长输入。
_DOUYIN_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")
_URLISH = ("http", "www.", "v.douyin", "live.douyin", "iesdouyin")
# 手抄抖音号时常带标签(「抖音号:12345678」「抖音ID 12345678」),先剥掉再判形态,
# 否则会被当成「无法识别的链接格式」——那句提示对拿抖音号的人毫无帮助。
_ID_LABEL_RE = re.compile(r"^(抖音号|抖音\s*ID|douyin\s*id)\s*[:：\-—]?\s*", re.I)


def _extract_web_rid(url):
    m = re.search(r"live\.douyin\.com/(\d+)", url)
    return m.group(1) if m else None


def _extract_sec_uid(url):
    m = re.search(r"sec_user_id=([^&\s]+)", url)
    if m:
        v = urllib.parse.unquote(m.group(1))
        if v.startswith("MS4wLjAB"):
            return v
    m = re.search(r"/(?:share/)?user/([^/?#]+)", url)
    if m:
        v = urllib.parse.unquote(m.group(1))
        if v.startswith("MS4wLjAB"):
            return v
    return None


def _extract_sec_uid_from_html(html_text):
    m = re.search(r"MS4wLjAB[A-Za-z0-9_\-]{20,}", html_text)
    return m.group(0) if m else None


def _extract_nickname(html_text):
    m = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html_text)
    if not m:
        return None
    t = html.unescape(m.group(1)).strip()
    for suf in ("的主页", "的抖音号", "的抖音"):
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
    return t or None


def _extract_redirect_from_body(html_text):
    m = re.search(
        r'<meta[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^"\']*url=([^"\'\s]+)',
        html_text, re.I)
    if m:
        return m.group(1)
    m = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", html_text)
    return m.group(1) if m else None


def _extract_first_url(text):
    text = (text or "").strip()
    m = re.search(r"(https?://[^\s\u4e00-\u9fff]+)", text)
    if m:
        return m.group(1).rstrip("，。！!；;、")
    m = re.search(
        r"(live\.douyin\.com/\d+|v\.douyin\.com/[A-Za-z0-9_\-]+/?|www\.douyin\.com/\S+)",
        text)
    if m:
        return m.group(0)
    return text


def strip_id_label(text):
    """剥掉「抖音号:」这类前缀标签,便于把带标签手抄的号当抖音号处理。"""
    return _ID_LABEL_RE.sub("", (text or "").strip()).strip()


def looks_like_douyin_id(text):
    """输入是不是一个「抖音号」(而不是链接/一段分享文案)。

    抖音号允许纯数字(11 位等)、字母数字、带点(`a.bc`)。这里只做形态判断,
    真正是否存在的由接口说话 —— 宁可多问一次接口,也不要因为形态误判就说
    「无法识别的链接格式」(那个提示对只拿到抖音号的人毫无帮助)。
    """
    t = (text or "").strip()
    if not t or not _DOUYIN_ID_RE.match(t):
        return False
    return not t.lower().startswith(_URLISH)


def fetch_user_info(client, sec_uid=None, unique_id=None):
    """按 sec_uid 或抖音号查用户资料,返回 (nickname, douyin_id, sec_uid)。

    抖音号取值规则:优先 `unique_id`;为空或 "0" 时退回 `short_id`
    (实测 2026-10-26:两种形态都存在 —— 有的号 `unique_id` 为空、抖音号只落在
    `short_id`;有的号反过来 `short_id` 为 "0")。两者都是 "0" 视为没有。
    查不到时三项都返回 None —— 调用方据此决定是报错还是「只缺这一项」。
    """
    params = {}
    if sec_uid:
        params["sec_uid"] = sec_uid
    elif unique_id:
        params["unique_id"] = unique_id
    else:
        return None, None, None
    resp = client.get(USER_INFO_API, params=params, headers=HEADERS)
    if resp.status_code != 200:
        return None, None, None
    user = (resp.json() or {}).get("user_info") or {}
    got_sec = (user.get("sec_uid") or "").strip() or None
    nick = (user.get("nickname") or "").strip() or None
    did = ""
    for key in ("unique_id", "short_id"):
        v = str(user.get(key) or "").strip()
        if v and v != "0":
            did = v
            break
    return nick, (did or None), got_sec


def _ok(sec_uid, web_rid, nickname, douyin_id, final_url):
    return {
        "ok": True,
        "sec_uid": sec_uid,
        "web_rid": web_rid,
        "nickname": nickname,
        "douyin_id": douyin_id,
        "final_url": final_url,
    }


def resolve_by_douyin_id(douyin_id):
    """抖音号 → sec_uid + 昵称。离线主播也能用(不依赖开播状态)。"""
    try:
        with httpx.Client(timeout=12, trust_env=False, headers=HEADERS) as client:
            nick, did, sec_uid = fetch_user_info(client, unique_id=douyin_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"网络请求失败: {e}"}
    if not sec_uid:
        return {"ok": False,
                "error": f"没查到抖音号 {douyin_id}:请确认是「抖音号」而不是昵称,"
                         f"或直接在 App 里复制主页链接粘过来"}
    return _ok(sec_uid, None, nick, did or douyin_id,
               f"https://www.douyin.com/user/{sec_uid}")


def resolve(raw_text):
    """解析分享链接 / 抖音号,返回 {ok, sec_uid, web_rid, nickname, douyin_id, final_url}。

    失败返回 {ok: False, error}。web_rid 只有直播间链接才给得出来(见模块头说明)。
    """
    text = (raw_text or "").strip()
    if not text:
        return {"ok": False, "error": "请输入链接或抖音号"}

    # 先按抖音号判一次:纯形态输入走资料接口,省掉「无法识别的链接格式」这种
    # 对只拿到抖音号的人毫无意义的报错。带「抖音号:」标签的写法先剥标签。
    bare = strip_id_label(text)
    if looks_like_douyin_id(bare):
        return resolve_by_douyin_id(bare)
    if looks_like_douyin_id(text):
        return resolve_by_douyin_id(text)

    link = _extract_first_url(text)
    if not link:
        return {"ok": False, "error": "请输入链接或抖音号"}
    if not re.match(r"^https?://", link):
        if link.startswith(("v.douyin.com", "live.douyin.com", "www.douyin.com")):
            link = "https://" + link
        else:
            return {"ok": False, "error": "无法识别的链接格式"}

    web_rid = None
    sec_uid = None
    nickname = None
    douyin_id = None
    final_url = link
    body = ""
    current = link

    try:
        with httpx.Client(timeout=12, follow_redirects=False, headers=HEADERS,
                          trust_env=False) as client:
            for _ in range(8):
                resp = client.get(current)
                status = resp.status_code
                loc = resp.headers.get("location")
                body = resp.text
                web_rid = web_rid or _extract_web_rid(current)
                sec_uid = sec_uid or _extract_sec_uid(current)
                if nickname is None:
                    nickname = _extract_nickname(body)
                if status in (301, 302, 303, 307, 308) and loc:
                    current = urllib.parse.urljoin(current, loc)
                    final_url = current
                    continue
                nxt = _extract_redirect_from_body(body)
                if nxt:
                    current = urllib.parse.urljoin(current, nxt)
                    final_url = current
                    continue
                break

            if web_rid and not sec_uid:
                sec_uid = _extract_sec_uid_from_html(body)

            if not web_rid and not sec_uid:
                return {"ok": False, "error": "未解析到 sec_uid / web_rid,请确认是直播间或主页分享链接"}

            # 主页链接只能给出 sec_uid(页面已是 JS 空壳,og:title 抓不到昵称),
            # 昵称和抖音号要靠资料接口补齐。
            try:
                nick2, did2, sec2 = fetch_user_info(client, sec_uid=sec_uid)
                nickname = nickname or nick2
                douyin_id = did2
                sec_uid = sec_uid or sec2
            except Exception:  # noqa: BLE001
                pass  # 补资料失败不影响主流程,前端照样能填 sec_uid
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"网络请求失败: {e}"}

    return _ok(sec_uid, web_rid, nickname, douyin_id, final_url)
