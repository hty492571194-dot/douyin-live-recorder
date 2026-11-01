#!/usr/bin/env python3
"""link_resolver:抖音号形态判定 + 资料接口取值规则 + web_rid 来源边界。

夹具一律合成值(主播甲 / 12345678 / MS4wLjAB…),不写真实主播名与真实抖音号,
守 TestRealRepoIsClean 那条敏感词门。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import link_resolver

SEC = "MS4wLjABAAAAsynthetic_fixture_000000000000000"


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload


class _Client:
    """最小 httpx.Client 替身:按 URL 片段分派预置响应,顺带记录入参。"""

    def __init__(self, user_info=None):
        self.user_info = user_info or {}
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        if "user/info" in url:
            return _Resp({"status_code": 0, "user_info": self.user_info})
        raise AssertionError(f"未预置的 URL: {url}")


def _user(**kw):
    base = {"nickname": "主播甲", "sec_uid": SEC, "unique_id": "12345678", "short_id": "0"}
    base.update(kw)
    return base


class TestLooksLikeDouyinId(unittest.TestCase):
    def test_accepts_id_forms(self):
        """纯数字、字母数字、带点的抖音号都算 —— 抖音号的真实形态就有这三种。"""
        for t in ("12345678", "anchor_99", "a.bc", " 12345678 "):
            self.assertTrue(link_resolver.looks_like_douyin_id(t), t)

    def test_rejects_urls_and_prose(self):
        """链接、带中文的整段分享文案、空串都不能被当成抖音号(否则会白发一次请求)。"""
        for t in ("https://v.douyin.com/abcdefg/", "www.douyin.com/user/x",
                  "长按复制此条消息,打开抖音搜索", "", "  ", "a" * 40):
            self.assertFalse(link_resolver.looks_like_douyin_id(t), t)

    def test_strip_id_label(self):
        """手抄时常见的「抖音号:xxx」要先剥标签,否则会被判成「无法识别的链接格式」。"""
        for raw, want in (("抖音号:12345678", "12345678"),
                          ("抖音号 12345678", "12345678"),
                          ("抖音ID：anchor_99", "anchor_99"),
                          ("douyin id: a.bc", "a.bc"),
                          ("12345678", "12345678")):
            self.assertEqual(link_resolver.strip_id_label(raw), want)


class TestFetchUserInfo(unittest.TestCase):
    def test_prefers_unique_id(self):
        c = _Client(_user())
        nick, did, sec = link_resolver.fetch_user_info(c, unique_id="12345678")
        self.assertEqual((nick, did, sec), ("主播甲", "12345678", SEC))
        self.assertEqual(c.calls[0][0], link_resolver.USER_INFO_API)
        self.assertEqual(c.calls[0][1], {"unique_id": "12345678"})

    def test_falls_back_to_short_id(self):
        """unique_id 为空时抖音号在 short_id 里(实测形态之一),不能判成「没有抖音号」。"""
        c = _Client(_user(unique_id="", short_id="87654321"))
        _, did, _ = link_resolver.fetch_user_info(c, sec_uid=SEC)
        self.assertEqual(did, "87654321")

    def test_zeros_mean_no_id(self):
        """两个字段都是 "0" = 这个号没有可用抖音号,返回 None 而不是字面量 "0"。"""
        c = _Client(_user(unique_id="0", short_id="0"))
        nick, did, sec = link_resolver.fetch_user_info(c, sec_uid=SEC)
        self.assertIsNone(did)
        self.assertEqual(nick, "主播甲")
        self.assertEqual(sec, SEC)

    def test_no_param_no_request(self):
        """既不传 sec_uid 也不传 unique_id 时直接返回空,不发请求。"""
        c = _Client(_user())
        self.assertEqual(link_resolver.fetch_user_info(c), (None, None, None))
        self.assertEqual(c.calls, [])

    def test_http_error_is_silent(self):
        """接口挂掉不能把「解析」整条链路带崩,只需返回空让调用方降级。"""
        class _Bad(_Client):
            def get(self, url, params=None, headers=None):
                return _Resp({}, status=500)

        self.assertEqual(link_resolver.fetch_user_info(_Bad(), sec_uid=SEC), (None, None, None))


class TestWebRidBoundary(unittest.TestCase):
    def test_web_rid_only_from_live_link(self):
        """web_rid 只存在于直播间链接里 —— 主页链接/资料接口都给不出(2026-10-26 实测)。"""
        self.assertEqual(link_resolver._extract_web_rid("https://live.douyin.com/388770149407"),
                         "388770149407")
        self.assertIsNone(link_resolver._extract_web_rid(f"https://www.douyin.com/user/{SEC}"))
        self.assertIsNone(link_resolver._extract_web_rid("https://v.douyin.com/abcdefg/"))

    def test_sec_uid_from_live_redirect(self):
        """直播间 302 落地的 reflow 地址里带 sec_user_id,顺带就能拿到 sec_uid。"""
        loc = f"https://webcast.amemv.com/douyin/webcast/reflow/7?sec_user_id={SEC}&from=x"
        self.assertEqual(link_resolver._extract_sec_uid(loc), SEC)


class TestResolveGuards(unittest.TestCase):
    def test_empty_input(self):
        r = link_resolver.resolve("")
        self.assertFalse(r["ok"])
        self.assertIn("抖音号", r["error"])

    def test_unrecognized_text(self):
        """既不是链接也不是抖音号形态 → 明确报错,且不联网。"""
        r = link_resolver.resolve("这不是链接也不是抖音号吗")
        self.assertFalse(r["ok"])
        self.assertIn("无法识别", r["error"])


if __name__ == "__main__":
    unittest.main()
