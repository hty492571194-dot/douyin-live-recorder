"""登录态模块测试。

重点覆盖安全边界:本模块只允许写 detection.cookie,
绝不能触碰弹幕(danmaku)、录制(recorder)、归档(nas)等任何配置。
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth


def _cookies():
    """模拟 Playwright 返回的 cookies 列表(含登录态字段与干扰字段)。"""
    now = time.time()
    return [
        {"name": "ttwid", "value": "1|abc", "expires": now + 86400 * 30},
        {"name": "sessionid", "value": "sess-abc123", "expires": now + 86400 * 7},
        {"name": "sid_guard", "value": "guard-xyz", "expires": now + 86400 * 14},
        {"name": "msToken", "value": "ms-token", "expires": -1},
        {"name": "emptyv", "value": "", "expires": now + 100},
    ]


class TestAuthCookie(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 完整记录并还原:这些是模块级全局,漏还原会污染后续用例,
        # 其中 _keychain_delete 漏桩会直接删掉用户真实的登录态
        self.orig = {k: getattr(auth, k) for k in (
            "AUTH_DIR", "META_PATH", "PROFILE_DIR",
            "_keychain_get", "_keychain_set", "_keychain_delete")}
        auth.AUTH_DIR = os.path.join(self.tmp, "auth")
        auth.META_PATH = os.path.join(auth.AUTH_DIR, "meta.json")
        auth.PROFILE_DIR = os.path.join(auth.AUTH_DIR, "profile")
        self.store = {}
        auth._keychain_get = lambda: self.store.get("ck", "")
        auth._keychain_set = lambda v: (self.store.__setitem__("ck", v), True, "")[1:]
        auth._keychain_delete = lambda: (self.store.pop("ck", None), True)[1]

    def tearDown(self):
        for k, v in self.orig.items():
            setattr(auth, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 基础组装 ──
    def test_cookie_string_joins_all_fields(self):
        s = auth._cookie_string(_cookies())
        self.assertIn("sessionid=sess-abc123", s)
        self.assertIn("ttwid=1|abc", s)
        self.assertIn("; ", s)  # 多字段以分号分隔

    def test_cookie_string_skips_empty_value(self):
        s = auth._cookie_string(_cookies())
        self.assertNotIn("emptyv", s)

    def test_has_login_detects_sessionid(self):
        self.assertTrue(auth._has_login(_cookies()))
        self.assertFalse(auth._has_login([{"name": "ttwid", "value": "x"}]))
        self.assertFalse(auth._has_login([{"name": "sessionid", "value": ""}]))

    def test_expire_at_takes_earliest_login_cookie(self):
        e = auth._expire_at(_cookies())
        self.assertIsNotNone(e)
        # sessionid(7天)早于 sid_guard(14天),应取 7 天那个
        self.assertLess(abs(e - (time.time() + 86400 * 7)), 5)

    # ── 解析优先级 ──
    def test_resolve_prefers_keychain_when_ref_set(self):
        self.store["ck"] = "sessionid=from-keychain"
        det = {"cookie_ref": "keychain:douyin-cookie", "cookie": "plain=old"}
        self.assertEqual(auth.resolve_cookie(det), "sessionid=from-keychain")

    def test_resolve_falls_back_to_plain_when_keychain_empty(self):
        det = {"cookie_ref": "keychain:douyin-cookie", "cookie": "plain=fallback"}
        self.assertEqual(auth.resolve_cookie(det), "plain=fallback")

    def test_resolve_plain_when_no_ref(self):
        self.store["ck"] = "sessionid=ignored"
        det = {"cookie": "plain=used"}
        self.assertEqual(auth.resolve_cookie(det), "plain=used")

    # ── 安全边界(核心) ──
    def test_inject_only_touches_detection_cookie(self):
        self.store["ck"] = "sessionid=abc"
        cfg = {
            "detection": {"mode": "mix", "cookie": "", "cookie_ref": "keychain:douyin-cookie"},
            "danmaku": {"enabled": True, "offset_seconds": 0, "style": "queue"},
            "recorder": {"format": "flv", "segment_time": 2300},
            "nas": {"enabled": True, "host": "192.168.1.10"},
            "monitors": [{"name": "A", "web_rid": "123"}],
        }
        before = copy.deepcopy(cfg)
        auth.inject(cfg)
        # 唯一允许的改动
        self.assertEqual(cfg["detection"]["cookie"], "sessionid=abc")
        # 其余全部原样
        for k in ("danmaku", "recorder", "nas", "monitors"):
            self.assertEqual(cfg[k], before[k], f"{k} 不应被修改")
        self.assertEqual(cfg["detection"]["mode"], "mix")
        self.assertEqual(cfg["detection"]["cookie_ref"], "keychain:douyin-cookie")

    def test_inject_never_creates_unexpected_keys(self):
        cfg = {"detection": {"cookie": ""}}
        auth.inject(cfg)
        self.assertEqual(set(cfg.keys()), {"detection"})

    def test_status_exposes_no_cookie_plaintext(self):
        self.store["ck"] = "sessionid=SUPER-SECRET-VALUE"
        blob = json.dumps(auth.status(), ensure_ascii=False)
        self.assertNotIn("SUPER-SECRET-VALUE", blob)

    # ── 状态机 ──
    def test_status_none_when_not_logged_in(self):
        st = auth.status()
        self.assertFalse(st["logged_in"])
        self.assertEqual(st["state"], "none")

    def test_status_ok_after_successful_login(self):
        auth._keychain_set(auth._cookie_string(_cookies()))
        auth._save_meta({"last_ok": time.time(), "last_check": time.time(),
                         "expire_at": time.time() + 86400, "nickname": "测试"})
        st = auth.status()
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["state"], "ok")
        self.assertEqual(st["nickname"], "测试")

    def test_status_expired_when_meta_flagged(self):
        auth._keychain_set(auth._cookie_string(_cookies()))
        auth._save_meta({"expired": True, "error": "登录态已过期"})
        st = auth.status()
        self.assertEqual(st["state"], "expired")
        self.assertEqual(st["error"], "登录态已过期")

    # ── 清理 ──
    def test_logout_clears_state(self):
        auth._keychain_set("sessionid=abc")
        auth._save_meta({"last_ok": time.time()})
        # 钥匙串删除在 CI/沙箱可能失败,只要不抛异常且 meta 被清即可
        auth.logout()
        self.assertFalse(os.path.exists(auth.META_PATH))

    def test_meta_roundtrip(self):
        auth._save_meta({"nickname": "甲", "last_ok": 123.0})
        self.assertEqual(auth._load_meta().get("nickname"), "甲")


if __name__ == "__main__":
    unittest.main()
