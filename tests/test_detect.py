#!/usr/bin/env python3
"""detect.py:sec_uid 识别与探测锚点解析。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detect

SEC = "MS4wLjABAAAAwCQqbMRDtGCOY2wY30OHNHl4fbJT8_jM04QAUodzasE"


class TestAnchor(unittest.TestCase):
    def test_is_sec_uid(self):
        self.assertTrue(detect.is_sec_uid(SEC))
        self.assertTrue(detect.is_sec_uid("  " + SEC + "  "))
        self.assertFalse(detect.is_sec_uid("388770149407"))
        self.assertFalse(detect.is_sec_uid(""))
        self.assertFalse(detect.is_sec_uid("随便什么"))

    def test_probe_anchor_web_rid_field(self):
        """sec_uid 锚点 + web_rid 字段 → 用 web_rid 探测(修复核心)。"""
        mon = {"name": "林知夏", "anchor": SEC, "web_rid": "388770149407"}
        probe, err = detect.probe_anchor(mon)
        self.assertEqual(probe, "388770149407")
        self.assertIsNone(err)

    def test_probe_anchor_digit_anchor(self):
        """纯数字锚点直接用。"""
        probe, err = detect.probe_anchor({"name": "x", "anchor": "388770149407"})
        self.assertEqual(probe, "388770149407")
        self.assertIsNone(err)

    def test_probe_anchor_sec_uid_only(self):
        """只有 sec_uid → 明确提示,而不是静默误判未开播。"""
        probe, err = detect.probe_anchor({"name": "林知夏", "anchor": SEC})
        self.assertIsNone(probe)
        self.assertIn("web_rid", err)
        self.assertIn("sec_uid", err)

    def test_probe_anchor_web_rid_precedence(self):
        """web_rid 字段优先于 anchor。"""
        probe, _ = detect.probe_anchor({"name": "x", "anchor": SEC, "web_rid": "111111"})
        self.assertEqual(probe, "111111")


class TestRoomStatus(unittest.TestCase):
    def test_live(self):
        html = r'...\"idStr\":\"7675261780737821478\",\"status\":2,\"ownerUserId\":756...'
        self.assertEqual(detect.extract_room_status(html), 2)

    def test_ended(self):
        html = r'...\"idStr\":\"7675086685574646538\",\"status\":4,\"ownerUserId\":105...'
        self.assertEqual(detect.extract_room_status(html), 4)

    def test_ignores_user_status(self):
        """user 资料的 status 后跟 createTime,不应被当作 room.status。"""
        html = r'...\"city\":\"\",\"status\":1,\"createTime\":0,\"secret\":0...'
        self.assertIsNone(detect.extract_room_status(html))

    def test_plain_json(self):
        """兼容不带转义的原始 JSON。"""
        html = '..."status":2,"ownerUserId":123...'
        self.assertEqual(detect.extract_room_status(html), 2)


class TestStreamUrl(unittest.TestCase):
    def test_audio_stream_excluded(self):
        """纯音频流(only_audio=1,无清晰度后缀)必须排除,返回视频流。"""
        html = (
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-408168216488050827.flv'
            r'?expire=6a8d42d3\u0026sign=abc\u0026only_audio=1"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-408168216488050827_or4.flv'
            r'?expire=6a8d42d3\u0026sign=xyz"...'
        )
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("_or4.flv", url)
        self.assertNotIn("only_audio=1", url)
        self.assertNotIn("\\u0026", url)  # 转义已清理
        self.assertIn("&sign=", url)

    def test_or4_preferred(self):
        """多个视频流时优先原画 _or4(忽略出现顺序)。"""
        html = (
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_Stage0T000ld.flv?expire=x\u0026sign=a"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_or4.flv?expire=x\u0026sign=b"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_Stage0T000hd.flv?expire=x\u0026sign=c"...'
        )
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("_or4.flv", url)

    def test_amp_entity_cleaned(self):
        """HTML 实体 &amp; 也要清理。"""
        html = 'url="http://pull-flv-l26.douyincdn.com/stage/stream-1.flv?expire=x&amp;sign=y"'
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("&sign=y", url)
        self.assertNotIn("&amp;", url)

    def test_fallback_hls(self):
        """无 flv 时回退 m3u8。"""
        html = '..."hls":"http://pull-hls-l26.douyincdn.com/stage/stream-1_or4.m3u8?expire=x\u0026sign=y"...'
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "hls")
        self.assertIn(".m3u8", url)

    def test_none(self):
        self.assertEqual(detect.extract_stream_url("no stream here"), (None, None))


if __name__ == "__main__":
    unittest.main()


# ── 下播不得返回流地址(2026-09-18) ──
#
# 背景:302 通道跟到 reflow 落地页后,即使 room.status=4(已结束),页面里**仍然带着
# 一个流地址**,它带 rtm_expr_tag=reflow_room_info,是占位地址 —— 实测 ffmpeg 拉它
# 直接 `Server returned 404 Not Found`。一旦上层拿它启动录制,就会
# 「起来→404 秒退→重启 4 次→冷却 600s」无限循环,而界面上因为 is_live 还显示着
# 「直播中」。这里锁住:不直播就不给地址。
class _FakeResp:
    def __init__(self, status_code=302, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """只实现 check_302/check_api 用到的那一次 get。"""

    def __init__(self, resp):
        self._resp = resp

    async def get(self, *a, **k):
        return self._resp


REFLOW_LOC = "https://live.douyin.com/reflow/1234567890123456789?sec_user_id=" + SEC
PLACEHOLDER_URL = ("http://pull-flv-l11.douyincdn.com/stage/stream-1_or4.flv"
                   "?rtm_expr_tag=reflow_room_info")


class TestOfflineGivesNoStreamUrl(unittest.TestCase):
    def setUp(self):
        self.orig = detect._fetch_room_info

    def tearDown(self):
        detect._fetch_room_info = self.orig

    def _run_302(self, status, url, degraded=False):
        import asyncio

        async def fake_fetch(client, loc):
            return (status, url, None, degraded)

        detect._fetch_room_info = fake_fetch
        client = _FakeClient(_FakeResp(302, {"location": REFLOW_LOC}))
        return asyncio.run(detect.check_302(client, "388770149407"))

    def test_offline_status4_drops_stream_url(self):
        """已下播(status=4)必须清空流地址 —— 那是 reflow 页的占位地址,拉它必 404。"""
        r = self._run_302(4, PLACEHOLDER_URL)
        self.assertFalse(r["is_live"])
        self.assertIsNone(r["stream_url"], "已下播仍给地址,录制会去拉一个 404 地址")

    def test_live_status2_keeps_stream_url(self):
        """直播中照常给地址,别把正常路径也堵了。"""
        r = self._run_302(2, "http://pull-flv-f1.douyinliving.com/x.flv")
        self.assertTrue(r["is_live"])
        self.assertEqual(r["stream_url"], "http://pull-flv-f1.douyinliving.com/x.flv")

    def test_unknown_status_keeps_url_conservatively(self):
        """status 解析失败(None)→ 保守判开播,**不能顺手把地址也丢了**(那会漏录)。"""
        r = self._run_302(None, "http://x/y.flv")
        self.assertTrue(r["is_live"])
        self.assertEqual(r["stream_url"], "http://x/y.flv")

    def test_room_id_still_extracted_when_offline(self):
        """清了地址但 room_id 要留着 —— 矛盾校验/状态纠错都要用它。"""
        r = self._run_302(4, PLACEHOLDER_URL)
        self.assertEqual(r["room_id"], "1234567890123456789")


DEGRADED_HTML = (
    '<!doctype html><html lang="en"><head></head><body>'
    '抖音 打开APP, 发现更多内容 暂时无法在该页面观看 尝试在 抖音 内观看 打开抖音'
    '</body></html>'
)
NORMAL_SMALL_HTML = '\\"status\\":2,\\"ownerUserId\\":123' + '.flv' * 3


class TestDegradedReflowPage(unittest.TestCase):
    """降级落地页必须判「无法判定」,而不是「在播」(2026-10-13 补)。

    背景:某主播直播间连续约 3 小时被判在播却拿不到流地址,巡检每 20s 复检一次
    空转。根因之一就是 reflow 页解析不出 status 时无条件保守判开播 —— 对降级页
    来说解析不出是**预期结果**(抖音压根不给网页端看),不是解析失手。
    """

    def setUp(self):
        self.orig = detect._fetch_room_info

    def tearDown(self):
        detect._fetch_room_info = self.orig

    def _run_302(self, status, url, degraded):
        import asyncio

        async def fake_fetch(client, loc):
            return (status, url, None, degraded)

        detect._fetch_room_info = fake_fetch
        client = _FakeClient(_FakeResp(302, {"location": REFLOW_LOC}))
        return asyncio.run(detect.check_302(client, "388770149407"))

    def test_marker_page_is_degraded(self):
        """带「暂时无法在该页面观看」的页面即降级页。"""
        self.assertTrue(detect.is_degraded_page(DEGRADED_HTML))

    def test_normal_page_not_degraded(self):
        """正常页(有房间对象 + 流地址后缀)不能被误判成降级。"""
        self.assertFalse(detect.is_degraded_page(NORMAL_SMALL_HTML))

    def test_empty_page_is_degraded(self):
        self.assertTrue(detect.is_degraded_page(""))

    def test_degraded_page_not_live_but_unknown(self):
        """降级页:既不判在播,也要标 unknown(上层才知道这不是「已下播」)。"""
        r = self._run_302(None, None, degraded=True)
        self.assertFalse(r["is_live"], "降级页不能判在播 —— 那是拿不到流地址的永久假阳性")
        self.assertTrue(r.get("unknown"), "必须标 unknown,否则上层会当成真下播处理")
        self.assertIsNone(r["stream_url"])

    def test_unknown_status_keeps_url_conservatively(self):
        """非降级原因导致的解析失败 → 沿用旧的保守判开播,不该被 unknown 吞掉。"""
        r = self._run_302(None, "http://x/y.flv", degraded=False)
        self.assertTrue(r["is_live"])
        self.assertFalse(r.get("unknown"))
        self.assertEqual(r["stream_url"], "http://x/y.flv")

    def test_status2_wins_over_degraded_flag(self):
        """能解析出 status 时以 status 为准,降级标记只用于「解析不出」的场景。"""
        r = self._run_302(2, "http://x/y.flv", degraded=True)
        self.assertTrue(r["is_live"])
        self.assertFalse(r.get("unknown"))


class TestMixRescuesDegradedPage(unittest.TestCase):
    """mix 模式下,降级页必须让位给 API(2026-10-13 补)。

    降级页自己给不出任何结论,而 API 能给出 status 甚至可用流地址 —— 这一轮
    恰恰最值得花一次 Cookie。`unknown` 因此必须算进 `need`,不能只看 is_live。
    """

    CFG = {"detection": {"mode": "mix", "cookie": "a=b",
                         "api_base": "https://api/"}}

    def _run(self, api_result, stream302=None):
        import asyncio
        seen = {}

        async def fake302(client, anchor):
            return {"is_live": False, "unknown": True, "status": None,
                    "stream_url": stream302, "room_id": "7695832358775212800",
                    "sec_uid": "MS4w", "method": "302"}

        async def fakeapi(client, web_rid, cookie, api_base, room_id=None):
            seen.update({"web_rid": web_rid, "room_id": room_id})
            return dict(api_result)

        orig302, origapi = detect.check_302, detect.check_api
        detect.check_302, detect.check_api = fake302, fakeapi
        try:
            out = asyncio.run(detect.check(None, "36000000000", self.CFG))
        finally:
            detect.check_302, detect.check_api = orig302, origapi
        return out, seen

    def test_api_live_rescues_degraded_302(self):
        """API 判在播并给了地址 → 以 API 为准,带上可用流地址。"""
        out, seen = self._run({"is_live": True, "status": 2, "method": "api",
                               "stream_url": "http://pull/x_or4.flv"})
        self.assertEqual(out["method"], "api")
        self.assertTrue(out["is_live"])
        self.assertEqual(out["stream_url"], "http://pull/x_or4.flv")
        self.assertEqual(seen["web_rid"], "36000000000")
        self.assertEqual(seen["room_id"], "7695832358775212800")

    def test_api_offline_clears_the_false_positive(self):
        """API 判未开播 → 假阳性就此纠正,不再是「在播却没流地址」。"""
        out, _ = self._run({"is_live": False, "status": 4, "method": "api",
                            "stream_url": None})
        self.assertFalse(out["is_live"])
        self.assertIsNone(out["stream_url"])

    def test_api_live_without_url_keeps_302_url(self):
        """API 判在播却没给地址时,不许把 302 已有的地址丢掉。"""
        out, _ = self._run({"is_live": True, "status": 2, "method": "api",
                            "stream_url": None},
                           stream302="http://pull/from302_or4.flv")
        self.assertEqual(out["stream_url"], "http://pull/from302_or4.flv")


class TestCheckApiUsesWebRid(unittest.TestCase):
    """check_api 必须把第一个参数当 web_rid 用(2026-10-13 补)。

    曾经调用方把 reflow 页里的 19 位 room_id 传进来,抖音回 HTTP 200 +
    status_code=4001038 + data.data=[] —— 看起来像房间不存在,实际是参数错位,
    导致三次矛盾校验全部假失败、假阳性直播态纠正不掉。
    """

    def _run_api(self, room={"status": 2}, capture=None):
        import asyncio

        payload = {"data": {"data": [dict(room)]}}

        class _CapClient(_FakeClient):
            async def get(self, url, params=None, headers=None):
                if capture is not None:
                    capture.update(params or {})
                return _FakeResp(200, payload=payload)

        return asyncio.run(detect.check_api(_CapClient(None), "36000000000",
                                            "cookie=1", "https://api/"))

    def test_web_rid_goes_to_web_rid_param(self):
        cap = {}
        self._run_api(capture=cap)
        self.assertEqual(cap.get("web_rid"), "36000000000")

    def test_room_id_is_extra_param_not_replacement(self):
        """room_id 只能作为附加参数带上,不能顶替 web_rid。"""
        import asyncio

        payload = {"data": {"data": [{"status": 2}]}}
        cap = {}

        class _CapClient(_FakeClient):
            async def get(self, url, params=None, headers=None):
                cap.update(params or {})
                return _FakeResp(200, payload=payload)

        asyncio.run(detect.check_api(_CapClient(None), "36000000000", "cookie=1",
                                     "https://api/", room_id="7695832358775212800"))
        self.assertEqual(cap.get("web_rid"), "36000000000")
        self.assertEqual(cap.get("room_id"), "7695832358775212800")


class TestConfirmWithCookieNeedsWebRid(unittest.TestCase):
    """矛盾校验拿不到 web_rid 时必须明确放弃,不许退回拿 room_id 冒充。"""

    def _cfg(self):
        return {"detection": {"cookie": "a=b",
                              "api_base": "https://api/"}}

    def _run(self, web_rid):
        import asyncio
        return asyncio.run(detect.confirm_with_cookie(
            _FakeClient(_FakeResp(200, payload={"data": {"data": [{"status": 2}]}})),
            "7695832358775212800", self._cfg(), web_rid=web_rid))

    def test_missing_web_rid_is_reported(self):
        res, why = self._run(None)
        self.assertIsNone(res)
        self.assertEqual(why, "no_web_rid", "缺 web_rid 必须明确报原因,不能静默失败")

    def test_with_web_rid_returns_room(self):
        res, why = self._run("36000000000")
        self.assertEqual(why, "ok")
        self.assertEqual(res.get("status"), 2)


class TestCheckApiOfflineGivesNoStreamUrl(unittest.TestCase):
    def _run_api(self, status, su):
        import asyncio
        payload = {"data": {"data": [{"status": status, "stream_url": {"flv_pull_url": {"FULL_HD1": {"main": {"flv": su}}}}}]}}
        return asyncio.run(detect.check_api(_FakeClient(_FakeResp(200, payload=payload)),
                                            "1234567890123456789", "cookie=1", "https://api/"))

    def test_offline_drops_stream_url(self):
        r = self._run_api(4, "http://x/y.flv")
        self.assertFalse(r["is_live"])
        self.assertIsNone(r["stream_url"])

    def test_live_keeps_stream_url(self):
        r = self._run_api(2, "http://x/y.flv")
        self.assertTrue(r["is_live"])
        self.assertEqual(r["stream_url"], "http://x/y.flv")
