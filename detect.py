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


# ── 降级 reflow 页识别 ────────────────────────────────────────
#
# 抖音会对部分直播间返回一份「网页端不可观看」的降级落地页(实测 2026-10-13
# 某主播):可见文案是「暂时无法在该页面观看 / 尝试在抖音内观看 / 打开APP」,
# 页面里既没有 room.status、也没有任何 flv/m3u8 地址,体积约 26KB;而正常
# 落地页 70~82KB 且两者齐全。成因在抖音侧(主播设置/房间限制),本地拉不到流,
# 属「这台机器无从录制」,不是解析代码失手。
#
# 必须把它和「网络截断、偶发结构变化导致解析失败」区分对待:
#   · 降级页 → 解析不出 status 是**预期结果**,标 unknown,不能判在播;
#   · 其它异常 → 沿用旧的保守判开播(宁可多查一次,也不漏一场)。
# 不区分的代价实测过一次:判定在播却没有流地址 → 巡检每 20s 复检、
# 每 2min 重检测,连续空转约 3 小时,界面还一直显示「在播」。
_VIDEO_EXT_RE = (".flv", ".m3u8")
_DEGRADED_MARKERS = (
    "暂时无法在该页面观看",
    "请在抖音APP内观看",
    "请在抖音 App 内观看",
)
_DEGRADED_MAX_LEN = 40960  # 正常落地页实测 ≥70KB,降级页约 26KB


def is_degraded_page(html: str) -> bool:
    """reflow 落地页是否为「网页端不可观看」的降级空壳页。"""
    if not html:
        return True
    if any(m in html for m in _DEGRADED_MARKERS):
        return True
    # 结构兜底:既没有房间对象(ownerUserId)、也没有任何拉流后缀,且体积小得反常
    return (len(html) < _DEGRADED_MAX_LEN
            and "ownerUserId" not in html
            and not any(ext in html for ext in _VIDEO_EXT_RE))


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
    """跟随 302 的 location 抓 reflow 落地页,提取 (status, stream_url, create_time, degraded)。
    失败返回 (None, None, None, False)。create_time 顺手从同一响应解析,不产生额外请求。

    末位的 degraded 表示该页是「抖音明确不给网页端看」的降级页(见 is_degraded_page)。
    请求异常时返回 False,让上层沿用旧的保守判开播 —— 降级与否只有在拿到页面时才知道,
    拿不到页面就当未知处理,不该顺手否掉一场可能是真直播的场次。"""
    try:
        resp = await client.get(loc, headers=HEADERS_302)
        if resp.status_code == 200:
            html = resp.text
            return (extract_room_status(html), extract_stream_url(html)[0],
                    extract_create_time(html), is_degraded_page(html))
    except Exception:
        pass
    return None, None, None, False


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
        status, stream_url, create_time, degraded = await _fetch_room_info(client, loc)
        if status is not None:
            is_live = (status == 2)
        elif degraded:
            # 降级页:解析不出 status 是**预期结果**(抖音压根不给网页端看),
            # 不是解析失手。此时判"无法判定"(unknown)而不是"在播" ——
            # 否则会得到「在播却没有流地址」的永久假阳性:录制起不来,
            # 巡检每隔 20s 复检一次空转(2026-10-13 实测连续空转约 3 小时)。
            is_live = False
        else:
            is_live = True  # 解析失败保守判开播(避免漏报)
        unknown = status is None and degraded
        # 已下播(status=4)时 reflow 落地页里**仍然带着一个流地址**,但它带
        # rtm_expr_tag=reflow_room_info,是占位地址 —— 实测 ffmpeg 拉它直接
        # `Server returned 404 Not Found`。留着它很危险:上层一旦拿它启动录制,
        # 就会「起来→404 秒退→重启 4 次→冷却 600s」无限循环,而界面上还因为
        # is_live 显示着「直播中」(2026-09-18 某主播那次就是这么绕进去的)。
        # 结论:不直播就不给地址,让上层没有机会去拉一个注定失败的东西。
        if not is_live:
            stream_url = None
        out = {
            "is_live": is_live,
            "status": status,
            "stream_url": stream_url,
            "room_id": extract_room_id(loc),
            "sec_uid": extract_sec_uid(loc) or str(anchor),
            "method": "302",
        }
        if unknown:
            # unknown 与 is_live=False 同时出现 = 「这轮没看清」,而不是「已下播」。
            # 上层据此不动正在进行的状态(详见 monitor.py 检测循环的 unknown 分支)。
            out["unknown"] = True
        if is_live and create_time:
            out["create_time"] = create_time  # 真实开播时间(仅直播中有意义)
        return out
    if resp.status_code == 200:
        return {"is_live": False, "method": "302"}
    return {"is_live": False, "error": f"HTTP {resp.status_code}", "method": "302"}


async def check_api(client, web_rid: str, cookie: str, api_base: str,
                    room_id=None) -> dict:
    """room status API 确认,返回 status(2=直播)+ stream_url。

    ⚠️ 第一个参数是 **web_rid**(`live.douyin.com/{数字}` 那串直播间号),
    不是 reflow 落地页里那个 19 位 room_id。两者混用不会报错,只会**静默失效**:
    实测把 room_id 当 web_rid 传,抖音返回 HTTP 200 + `status_code=4001038` +
    `data.data=[]` —— 表现得像"房间不存在",实际是参数错位。2026-10-13 的代价是
    三次矛盾校验全部误判为 no room data,一个假阳性直播态连续空转约 3 小时没能纠正。
    room_id 只在有值时作为附加参数带上(实测 web_rid + room_id 组合可用)。
    """
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
        "web_rid": web_rid,
    }
    if room_id:
        params["room_id"] = str(room_id)
    headers = {
        "User-Agent": UA_PC,
        "Referer": f"https://live.douyin.com/{web_rid}",
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
    live = r0.get("status") == 2
    return {
        "is_live": live,
        "status": r0.get("status"),
        # 与 check_302 同一原则:**不直播就不给地址**。下播后 CDN 地址已失效,
        # 留着它只会让录制端拉一个注定 404 的地址。
        "stream_url": _normalize_stream_url(r0.get("stream_url")) if live else None,
        "create_time": ct if live else None,
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
                              reason="conflict", in_hotspot=False, web_rid=None):
    """强制用 Cookie 做一次 API 确认(供矛盾状态校验等场景调用)。

    与 check() 的区别:check() 是常规检测流程里的自动确认,这里是**主动发起**的
    专项校验,仍然要走同一道护栏 —— 场景特殊只豁免「热点门」,频率和配额照扣。

    web_rid:主播的直播间号,API 唯一认的主键(详见 check_api 的说明)。缺它就
    拿不到房间数据,调用方应先把 web_rid 补齐;这里宁可明确报 no_web_rid,
    也不要退回拿 room_id 冒充 —— 那会重演「三次校验全部假失败」的老问题。

    返回 (result_dict, 护栏原因码);被拒绝或请求失败时 result 为 None。
    """
    det = cfg.get("detection", {})
    cookie = det.get("cookie", "")
    try:
        from auth import resolve_cookie as _resolve_cookie
        cookie = _resolve_cookie(det) or cookie
    except Exception:
        pass
    if not cookie:
        return None, "no_cookie"
    if not web_rid:
        return None, "no_web_rid"

    if guard is not None:
        ok, why = guard.allow(anchor or web_rid, reason=reason,
                              in_hotspot=in_hotspot, has_cookie=True)
        if not ok:
            return None, why
        guard.record(anchor or web_rid)

    try:
        r = await check_api(client, web_rid, cookie, det.get("api_base", ""),
                            room_id=room_id)
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
    # API 通道认的是 web_rid(直播间号),不是 reflow 页里的 room_id ——
    # 传错只会静默拿到 status_code=4001038 + 空 data(详见 check_api 说明)。
    # check() 的 anchor 由 probe_anchor() 产出,正常情况下就是 web_rid。
    web_rid = str(anchor or "").strip()
    if not web_rid.isdigit() or not cookie:
        return r302

    # unknown(降级页)也算「需要问一句」:302 侧看不清的时候,恰恰最值得花一次
    # Cookie 去问个明白 —— API 可能直接给出 status 与可拉流地址,顺带把录制救回来。
    need = mode == "api" or (mode == "mix" and (
        r302.get("is_live") or r302.get("error") or r302.get("unknown")))
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
        r_api = await check_api(client, web_rid, cookie, det.get("api_base", ""),
                                room_id=room_id)
    except Exception:
        return r302

    if r_api.get("error"):
        return r302

    # API 判在播却没给地址时,别把 302 通道已经拿到的地址扔了 —— 那等于自己掐断自己的流
    if r_api.get("is_live") and not r_api.get("stream_url") and r302.get("stream_url"):
        r_api["stream_url"] = r302["stream_url"]

    r_api["room_id"] = room_id
    r_api["sec_uid"] = r302.get("sec_uid") or str(anchor)
    return r_api
