#!/usr/bin/env python3
"""抖音分享链接解析:粘贴链接 → sec_uid / web_rid / nickname。

由 douyin-link-parser 项目的解析逻辑移植,改用 httpx(本项目已有依赖)。
同步实现,供 webui 的 POST /api/resolve 直接调用。
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


def resolve(raw_text):
    """解析分享链接,返回 {ok, sec_uid, web_rid, nickname, final_url} 或 {ok:False, error}。"""
    link = _extract_first_url(raw_text)
    if not link:
        return {"ok": False, "error": "请输入链接"}
    if not re.match(r"^https?://", link):
        if link.startswith(("v.douyin.com", "live.douyin.com", "www.douyin.com")):
            link = "https://" + link
        else:
            return {"ok": False, "error": "无法识别的链接格式"}

    web_rid = None
    sec_uid = None
    nickname = None
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
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"网络请求失败: {e}"}

    if web_rid and not sec_uid:
        sec_uid = _extract_sec_uid_from_html(body)

    if not web_rid and not sec_uid:
        return {"ok": False, "error": "未解析到 sec_uid / web_rid,请确认是直播间或主页分享链接"}

    return {
        "ok": True,
        "sec_uid": sec_uid,
        "web_rid": web_rid,
        "nickname": nickname,
        "final_url": final_url,
    }
