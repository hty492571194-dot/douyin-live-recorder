"""环境代理隔离:防止继承来的死代理把检测请求全打飞。

背景:服务若从受管终端/IDE 集成终端拉起,常继承 HTTP_PROXY 等变量指向一个
已经退出的沙箱代理端口。httpx 默认 trust_env=True,ffmpeg 拉流同样读
http_proxy,于是所有请求 Connection refused —— 症状是全员「All connection
attempts failed」、录制停止、历史记录不再增长,而服务本身活着、UI 正常响应。
"""
import os
import unittest

import monitor

PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


class TestPurgeEnvProxy(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in PROXY_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _clear(self):
        for k in PROXY_KEYS:
            os.environ.pop(k, None)

    def test_purges_all_case_variants(self):
        """大小写变体都要清掉(httpx 两种都认)。"""
        self._clear()
        for k in PROXY_KEYS:
            os.environ[k] = "http://127.0.0.1:1"
        n = monitor._purge_env_proxy()
        self.assertEqual(n, len(PROXY_KEYS))
        for k in PROXY_KEYS:
            self.assertNotIn(k, os.environ)

    def test_keep_switch_preserves_proxy(self):
        """DOUYIN_KEEP_ENV_PROXY=1 是逃生开关,此时一个都不该动。"""
        self._clear()
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["DOUYIN_KEEP_ENV_PROXY"] = "1"
        try:
            self.assertEqual(monitor._purge_env_proxy(), 0)
            self.assertEqual(os.environ["http_proxy"], "http://127.0.0.1:1")
        finally:
            os.environ.pop("DOUYIN_KEEP_ENV_PROXY", None)

    def test_noop_when_absent(self):
        """环境干净时返回 0,不抛异常。"""
        self._clear()
        self.assertEqual(monitor._purge_env_proxy(), 0)

    def test_leaves_unrelated_vars_alone(self):
        """不能误伤 NO_PROXY 之外的其他变量。"""
        self._clear()
        os.environ["NO_PROXY"] = "localhost"
        os.environ["DOUYIN_SAMPLE"] = "keepme"
        try:
            monitor._purge_env_proxy()
            self.assertEqual(os.environ["DOUYIN_SAMPLE"], "keepme")
        finally:
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("DOUYIN_SAMPLE", None)

    def test_http_clients_disable_trust_env(self):
        """代码里所有 httpx 客户端必须显式 trust_env=False(双保险)。"""
        import re
        for fn in ("monitor.py", "auth.py", "link_resolver.py", "webui.py"):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), fn)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for m in re.finditer(r"httpx\.(?:Async)?Client\((?:[^()]|\([^()]*\))*\)",
                                 src):
                call = m.group(0)
                self.assertIn(
                    "trust_env=False", call,
                    "%s 中有 httpx 客户端未设 trust_env=False: %s"
                    % (fn, call.replace("\n", " ")[:90]))


class TestNetworkAlert(unittest.TestCase):
    """全员检测失败时必须有一条系统性告警，不能只刷单主播日志。"""

    def setUp(self):
        monitor._reset_network_stats()
        self._orig_log = monitor.log
        self.lines = []
        monitor.log = self.lines.append

    def tearDown(self):
        monitor.log = self._orig_log
        monitor._reset_network_stats()

    _T0 = 1789548000.0     # 真实量级的时间戳(告警冷却比较需要它 > 1800)

    def _feed(self, n, failed):
        for i in range(n):
            monitor._note_network_result(self._T0 + i, failed)

    def test_alerts_on_systemic_failure(self):
        self._feed(12, True)
        self.assertTrue(any("系统性故障" in s for s in self.lines),
                        "全员失败却没打网络告警: %s" % self.lines[-2:])

    def test_silent_when_healthy(self):
        self._feed(12, False)
        self.assertFalse(any("系统性故障" in s for s in self.lines))

    def test_silent_on_isolated_failures(self):
        """个别主播失败不算系统性故障。"""
        for i in range(30):
            monitor._note_network_result(self._T0 + i, i % 10 == 0)
        self.assertFalse(any("系统性故障" in s for s in self.lines))

    def test_no_spam(self):
        """同一窗口内不重复告警。"""
        self._feed(30, True)
        hits = [s for s in self.lines if "系统性故障" in s]
        self.assertEqual(len(hits), 1, "告警刷屏: %d 条" % len(hits))


if __name__ == "__main__":
    unittest.main()
