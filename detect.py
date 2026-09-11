#!/usr/bin/env python3
"""双通道开播检测:302 轻量探测 + room status API 确认。"""
import re

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)
UA_PC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS_302 = {
    "User-Agent": UA_MOBILE,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://live.douyin.com/",
}


def extract_room_id(loc: str):
    m = re.search(r"/reflow/(\d+)", loc)
    return m.group(1) if m else None


def extract_sec_uid(loc: str):
    m = re.search(r"sec_user_id=([^&\s]+)", loc)
    return m.group(1) if m else None


# reflow 落地页里 room 对象的 status(转义 JSON)。room.status 后跟 ownerUserId,
# 用户资料里的 status 后跟 createTime,需据此区分。status 语义:2=直播中,4=已结束,其余视为未开播。
_ROOM_STATUS_RE = re.compile(r'\\"status\\":(\d+),\\"ownerUserId\\"')
_ROOM_STATUS_RE_PLAIN = re.compile(r'"status":(\d+),"ownerUserId"')


def extract_room_status(html: str):
    """从 reflow 落地页 HTML 提取直播间 status;解析失败返回 None。"""
    m = _ROOM_STATUS_RE.search(html) or _ROOM_STATUS_RE_PLAIN.search(html)
    return int(m.group(1)) if m else None


# 房间对象的 create_time(snake_case,秒级时间戳,≈开播时间)。
# 用户资料里的 createTime 是驼峰命名,不会误匹配。
_CREATE_TIME_RE = re.compile(r'\\"create_time\\":(\d{9,11})')
_CREATE_TIME_RE_PLAIN = re.compile(r'"create_time":(\d{9,11})')


def extract_create_time(html: str):
    """从 reflow 落地页提取直播间创建时间(≈开播时间);解析失败返回 None。"""
    m = _CREATE_TIME_RE.search(html) or _CREATE_TIME_RE_PLAIN.search(html)
    return int(m.group(1)) if m else None


def extract_stream_url(html: str):
    """从 reflow 落地页提取直播拉流地址,返回 (url, kind) 或 (None, None)。

    优先原画视频流(_or4),过滤纯音频流(only_audio=1 只有声音没有画面);
    fallback 任意 flv,再 fallback hls(m3u8)。
    地址带 expire/sign 有时效,须拿到后立即使用。"""
    def _clean(u):
        u = (u.replace("\\u0026", "&").replace("&amp;", "&")
               .replace("\\u002F", "/").replace("\\/", "/"))
        return u.rstrip("\\")  # 去掉 JSON 转义引号 \" 残留的反斜杠

    # 注意:不能排除反斜杠,否则 \u0026(& 转义)会被截断、丢失 sign 签名
    flvs = [_clean(u) for u in re.findall(r'https?://[^"\s]+?\.flv[^"\s]*', html)]
    m3u8s = [_clean(u) for u in re.findall(r'https?://[^"\s]+?\.m3u8[^"\s]*', html)]

    # 关键:抖音 reflow 页里「无清晰度后缀的 stream-{id}.flv」是纯音频流(only_audio=1),
    # 视频流反而带清晰度后缀(_or4/_hd/_md/_sd/_ld)。必须排除 only_audio=1,否则录出来只有声音。
    video_flvs = [u for u in flvs if "only_audio=1" not in u]

    if video_flvs:
        for u in video_flvs:  # 优先原画(_or4)
            name = u.split("?")[0].rsplit("/", 1)[-1]
            if name.endswith("_or4.flv"):
                return u, "flv"
        return video_flvs[0], "flv"
    if flvs:  # 极端:仅音频流时仍返回,避免完全无法录制
        return flvs[0], "flv"
    if m3u8s:
        return m3u8s[0], "hls"
    return None, None


_SEC_UID_RE = re.compile(r"^MS4wLjAB[A-Za-z0-9_-]{10,}$")


def is_sec_uid(anchor: str) -> bool:
    """是否为抖音 sec_uid(MS4wLjAB 开头)。"""
    return bool(_SEC_UID_RE.match((anchor or "").strip()))


def probe_anchor(mon: dict):
    """解析出用于 302 探测的锚点(必须 web_rid 纯数字)。

    sec_uid 只能作主播标识,`live.douyin.com/{sec_uid}` 不会返回 302,
    直接探测会把开播误判为未开播。返回 (probe, err):
    - 有 web_rid 字段(纯数字) → 用它探测
    - anchor 本身是纯数字(web_rid) → 用它探测
    - 仅 sec_uid → (None, 提示),由调用方明确告警而不是误判
    """
    anchor = str(mon.get("anchor", "")).strip()
    web_rid = str(mon.get("web_rid") or "").strip()
    if web_rid.isdigit():
        return web_rid, None
    if anchor.isdigit():
        return anchor, None
    if is_sec_uid(anchor):
        return None, "sec_uid 无法直接探测开播状态,请在主播信息中补充 web_rid(直播间号)"
    return None, f"锚点无效(需要数字直播间号或 sec_uid): {anchor!r}"


async def _fetch_room_info(client, loc: str):
    """跟随 302 的 location 抓 reflow 落地页,提取 (status, stream_url, create_time)。
    失败返回 (None, None, None)。create_time 顺手从同一响应解析,不产生额外请求。"""
    try:
        resp = await client.get(loc, headers=HEADERS_302)
        if resp.status_code == 200:
            html = resp.text
            return (extract_room_status(html), extract_stream_url(html)[0],
                    extract_create_time(html))
    except Exception:
        pass
    return None, None, None


async def check_302(client, anchor: str) -> dict:
    """302 只表示「存在直播间跳转」,下播后仍会 302 到 reflow 页。

    因此 302 后需再跟一次 reflow 落地页,用 room.status 判定真实状态:
    status==2 直播中,status==4 已结束;同时提取拉流地址 stream_url 供录制。
    reflow 解析失败时保守沿用 302=开播(避免漏报)。
    """
    url = f"https://live.douyin.com/{anchor}"
    resp = await client.get(url, headers=HEADERS_302)
    if resp.status_code in (301, 302):
        loc = resp.headers.get("location", "")
        status, stream_url, create_time = await _fetch_room_info(client, loc)
        is_live = (status == 2) if status is not None else True  # 解析失败保守判开播
        out = {
            "is_live": is_live,
            "status": status,
            "stream_url": stream_url,
            "room_id": extract_room_id(loc),
            "sec_uid": extract_sec_uid(loc) or str(anchor),
            "method": "302",
        }
        if is_live and create_time:
            out["create_time"] = create_time  # 真实开播时间(仅直播中有意义)
        return out
    if resp.status_code == 200:
        return {"is_live": False, "method": "302"}
    return {"is_live": False, "error": f"HTTP {resp.status_code}", "method": "302"}


async def check_api(client, room_id: str, cookie: str, api_base: str) -> dict:
    """room status API 确认,返回 status(2=直播)+ stream_url。"""
    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "enter_from": "web_live",
        "cookie_enabled": "true",
        "browser_language": "zh-CN",
        "browser_platform": "MacIntel",
        "browser_name": "Chrome",
        "browser_version": "126.0.0.0",
        "web_rid": room_id,
    }
    headers = {
        "User-Agent": UA_PC,
        "Referer": f"https://live.douyin.com/{room_id}",
        "Cookie": cookie,
    }
    resp = await client.get(api_base, params=params, headers=headers)
    if resp.status_code != 200:
        return {"is_live": False, "error": f"HTTP {resp.status_code}", "method": "api"}
    data = resp.json()
    rooms = (data.get("data") or {}).get("data") or []
    if not rooms:
        return {"is_live": False, "error": "no room data", "method": "api"}
    r0 = rooms[0]
    # create_time:房间创建时间 ≈ 开播时间(每场直播一个新房间),从既有响应透传,零额外请求
    try:
        ct = int(r0.get("create_time")) if r0.get("create_time") is not None else None
    except (TypeError, ValueError):
        ct = None
    return {
        "is_live": r0.get("status") == 2,
        "status": r0.get("status"),
        "stream_url": _normalize_stream_url(r0.get("stream_url")),
        "create_time": ct if r0.get("status") == 2 else None,
        "method": "api",
    }


# API 的 stream_url 是按清晰度分档的对象,形如
#   {"flv_pull_url": {"FULL_HD1": {"main": {"flv": "...", "hls": "..."}}, ...},
#    "hls_pull_url": {...}, ...}
# 而 302 通道直接给出地址字符串。两条通道的产出必须一致,否则录制端拿到 dict 会直接炸
# (ffmpeg 收到的会是一个 Python 对象的 str())。这里统一归一化成字符串。
_QUALITY_ORDER = ("FULL_HD1", "ORIGIN", "HD1", "SD1", "SD2", "LD", "AUDIO")


def _pick_stream(node):
    """从任意嵌套层级里挑出一个可用的拉流地址字符串。"""
    if isinstance(node, str):
        return node or None
    if isinstance(node, dict):
        # 先按清晰度优先级找,找不到再退化到遍历(兼容未知档位名)
        for q in _QUALITY_ORDER:
            if q in node:
                got = _pick_stream(node[q])
                if got:
                    return got
        for key in ("main", "flv", "hls", "ORIGIN", "FULL_HD1"):
            if key in node:
                got = _pick_stream(node[key])
                if got:
                    return got
        for v in node.values():
            got = _pick_stream(v)
            if got:
                return got
    elif isinstance(node, (list, tuple)):
        for v in node:
            got = _pick_stream(v)
            if got:
                return got
    return None


def _normalize_stream_url(su):
    """把 API 返回的 stream_url 归一化为可直接拉流的地址字符串。

    优先 flv(录制端对 flv 的处理最成熟),其次 hls;同一种封装内按清晰度从高到低。
    已经是字符串则原样返回(302 通道 / 已归一化的情况)。
    """
    if not su:
        return ""
    if isinstance(su, str):
        return su
    if isinstance(su, dict):
        for key in ("flv_pull_url", "hls_pull_url", "pull_url"):
            if key in su:
                got = _pick_stream(su[key])
                if got:
                    return got
        return _pick_stream(su) or ""
    return ""


async def confirm_with_cookie(client, room_id, cfg, guard=None, anchor=None,
                              reason="conflict", in_hotspot=False):
    """强制用 Cookie 做一次 API 确认(供矛盾状态校验等场景调用)。

    与 check() 的区别:check() 是常规检测流程里的自动确认,这里是**主动发起**的
    专项校验,仍然要走同一道护栏 —— 场景特殊只豁免「热点门」,频率和配额照扣。

    返回 (result_dict, 护栏原因码);被拒绝或请求失败时 result 为 None。
    """
    det = cfg.get("detection", {})
    cookie = det.get("cookie", "")
    try:
        from auth import resolve_cookie as _resolve_cookie
        cookie = _resolve_cookie(det) or cookie
    except Exception:
        pass
    if not room_id or not cookie:
        return None, "no_cookie"

    if guard is not None:
        ok, why = guard.allow(anchor or room_id, reason=reason,
                              in_hotspot=in_hotspot, has_cookie=True)
        if not ok:
            return None, why
        guard.record(anchor or room_id)

    try:
        r = await check_api(client, room_id, cookie, det.get("api_base", ""))
    except Exception as e:
        return None, f"error:{type(e).__name__}"
    if r.get("error"):
        return None, f"error:{r['error']}"
    r["room_id"] = room_id
    return r, "ok"


async def check(client, anchor: str, cfg: dict, last_room_id=None,
                in_hotspot=False, guard=None) -> dict:
    """按 detection.mode 混合两种通道,API 失败自动回退 302。"""
    det = cfg.get("detection", {})
    mode = det.get("mode", "mix")

    r302 = await check_302(client, anchor)
    if mode == "302":
        return r302

    room_id = r302.get("room_id") or last_room_id
    cookie = det.get("cookie", "")
    # 钥匙串登录态优先:config 只存引用(不落 Cookie 明文),实际值由 auth 模块解析
    try:
        from auth import resolve_cookie as _resolve_cookie
        cookie = _resolve_cookie(det) or cookie
    except Exception:
        pass
    if not room_id or not cookie:
        return r302

    need = mode == "api" or (mode == "mix" and (r302.get("is_live") or r302.get("error")))
    if not need:
        return r302

    # ── Cookie 护栏 ──
    # 真实账号的登录态是稀缺资源:用一次就消耗一次风控额度。这里先问后花,
    # 被拒时把原因挂在结果上(供日志与界面排查),并安静退回 302 结论。
    if guard is not None:
        ok, why = guard.allow(anchor,
                              reason="error" if r302.get("error") else "live",
                              in_hotspot=in_hotspot, has_cookie=True)
        if not ok:
            r302["cookie_guard"] = why
            return r302
        guard.record(anchor)

    try:
        r_api = await check_api(client, room_id, cookie, det.get("api_base", ""))
    except Exception:
        return r302

    if r_api.get("error"):
        return r302

    r_api["room_id"] = room_id
    r_api["sec_uid"] = r302.get("sec_uid") or str(anchor)
    return r_api
