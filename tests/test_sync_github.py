#!/usr/bin/env python3
"""月度 GitHub 同步脚本(scripts/sync_github.py)的单元测试。

三条底线：
  ① **禁词集必须从真实配置反推** —— 硬编码主播名/内网 IP 的话,用户加一个主播、
     换一次 NAS,扫描规则就悄悄失效了(公开仓库里泄露真值是不可逆的)。
  ② **短串不许参与拦截** —— NAS 用户名 `ggg`、主播名 `17` 这类 2~3 字符的串
     在代码里到处都是,一旦参与匹配,扫描会永久红着,然后被无视。
  ③ **当前入库文件集必须扫不出任何真值** —— 这条是真正的守门人:以后谁不小心
     把一个真实主播名写进代码或文档,跑测试就会红。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

_SPEC = importlib.util.spec_from_file_location(
    "sync_github", os.path.join(BASE, "scripts", "sync_github.py"))
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)

REAL_CFG = os.path.join(BASE, "config.json")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _has_real_config():
    return os.path.isfile(REAL_CFG)


class TestWorthBlocking(unittest.TestCase):
    """短值设定下限,否则误报会把扫描变成噪音。"""

    def test_rejects_short_values(self):
        for v in ("", "g", "gg", "ggg", "17", "12345"):
            self.assertFalse(sync.worth_blocking(v), "不该拦: %r" % v)

    def test_accepts_digits_only_when_long_enough(self):
        self.assertTrue(sync.worth_blocking("111111111111"))   # web_rid 12 位
        self.assertTrue(sync.worth_blocking("222222222222"))
        self.assertTrue(sync.worth_blocking("123456"))         # 下限就是 6 位
        self.assertFalse(sync.worth_blocking("12345"))         # 5 位仍算短串

    def test_accepts_ascii_when_at_least_4(self):
        self.assertTrue(sync.worth_blocking("198.51.100.7"))
        self.assertFalse(sync.worth_blocking("ggg"))

    def test_accepts_two_char_cjk(self):
        self.assertTrue(sync.worth_blocking("阿测"))

    def test_accepts_real_sec_uid(self):
        # 拼装构造:真实值的全貌绝不能落在文件里(否则会被自己的守门人测试抓到)
        uid = "MS4w" + "LjAB" + "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY2z"
        self.assertTrue(sync.worth_blocking(uid))


class TestPrivateValues(unittest.TestCase):
    """禁词集来自配置,不硬编码。"""

    CFG = {
        "monitors": [
            {"name": "示例主播", "anchor": "MS4wLjABAAAA" + "x" * 60,
             "web_rid": "111111111111"},
            {"name": "阿测", "anchor": "MS4wLjABAAAA" + "y" * 60,
             "web_rid": "222222222222"},
        ],
        "nas": {"host": "10.1.2.3", "share": "share_video_real",
                "username": "abc", "mount_point": "~/Archive"},
    }

    def test_collects_each_category(self):
        v = sync.private_values(self.CFG)
        self.assertIn("示例主播", v["主播昵称"])
        self.assertIn("111111111111", v["直播间号"])
        self.assertIn("10.1.2.3", v["NAS 主机"])
        self.assertIn("share_video_real", v["NAS 共享名"])

    def test_two_char_cjk_name_is_kept(self):
        """含中文的短名有辨识度,要拦。"""
        self.assertIn("阿测", sync.private_values(self.CFG)["主播昵称"])

    def test_short_digit_name_is_dropped(self):
        """纯数字短名(如配置里的 `17`)到处都能撞上,不能参与拦截。"""
        cfg = {"monitors": [{"name": "17", "anchor": "MS4wLjABAAAA" + "z" * 60,
                             "web_rid": "222222222222"}], "nas": {}}
        self.assertNotIn("主播昵称", sync.private_values(cfg))

    def test_short_ascii_username_not_blocked(self):
        self.assertNotIn("NAS 用户名", sync.private_values(self.CFG))

    def test_lan_prefix_derived_from_ip(self):
        self.assertIn("10.1.2.", sync.private_values(self.CFG)["内网网段"])

    def test_no_prefix_when_host_is_not_ip(self):
        cfg = dict(self.CFG, nas={"host": "nas.local", "share": "s_real"})
        self.assertNotIn("内网网段", sync.private_values(cfg))

    def test_hotspot_keys_become_forbidden(self):
        uid = "MS4wLjABAAAA" + "z" * 50
        v = sync.private_values(self.CFG, {uid: {"hours": []}})
        self.assertIn(uid, v["sec_uid"])

    def test_categories_without_values_are_dropped(self):
        v = sync.private_values({"monitors": [], "nas": {}})
        self.assertEqual(v, {})


class TestMaskAndSynthetic(unittest.TestCase):
    def test_mask_does_not_leak_full_value(self):
        secret = "MS4w" + "LjAB" + "aB3dE5fG7hJ9kL2mN4pQ6rS8tU"
        m = sync.mask(secret)
        self.assertNotIn(secret, m)
        self.assertIn("len=%d" % len(secret), m)

    def test_synthetic_markers_recognised(self):
        for v in ("SUPER-SECRET-VALUE", "sessionid_test123", "your_token_here"):
            self.assertTrue(sync.is_synthetic(v), v)

    def test_real_value_not_synthetic(self):
        self.assertFalse(sync.is_synthetic("MS4w" + "LjAB" + "aB3dE5fG7hJ9kL2m"))


class TestScanText(unittest.TestCase):
    VALUES = {"主播昵称": {"示例主播"}, "NAS 主机": {"10.1.2.3"}}

    def test_finds_value_with_line_number(self):
        text = "line1\nline2\n主播=示例主播\n"
        hits = sync.scan_text(text, self.VALUES)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "主播昵称")
        self.assertEqual(hits[0][2], 3)

    def test_reports_every_occurrence(self):
        hits = sync.scan_text("示例主播\n示例主播\n", self.VALUES)
        self.assertEqual(len(hits), 2)

    def test_synthetic_value_in_config_is_skipped(self):
        hits = sync.scan_text("x=SUPER-SECRET-VALUE\n", {"主播昵称": {"SUPER-SECRET-VALUE"}})
        self.assertEqual(hits, [])

    def test_generic_cookie_pattern(self):
        hits = sync.scan_text('sessionid="' + "a1b2c3d4" * 5 + '"', {})
        self.assertEqual([h[0] for h in hits], ["凭据明文"])

    def test_generic_cloud_key_pattern(self):
        hits = sync.scan_text("AWS key " + "AKIA" + "3XQZ7PLMN2RK8TWV" + " end", {})
        self.assertTrue(hits, "AKIA 形态的云厂商密钥应被拦")

    def test_documented_example_key_is_allowed(self):
        """AWS 官方文档里的 AKIAIOSFODNN7EXAMPLE 是示例,不该拦(否则误报永久红)。"""
        self.assertEqual(sync.scan_text("AKIA" + "IOSFODNN7EXAMPLE", {}), [])

    def test_field_name_constants_are_allowed(self):
        """`_SESSION_KEYS = ("sessionid", ...)` 这类常量不是泄露。"""
        hits = sync.scan_text('_SESSION_KEYS = ("sessionid", "ttwid")\n', {})
        self.assertEqual(hits, [])


class TestParseRemote(unittest.TestCase):
    def test_https_and_ssh_forms(self):
        want = ("someone", "repo-name")
        for url in ("https://github.com/someone/repo-name.git",
                    "https://github.com/someone/repo-name",
                    "git@github.com:someone/repo-name.git",
                    "https://token@github.com/someone/repo-name.git"):
            self.assertEqual(sync.parse_remote(url), want, url)

    def test_non_github_returns_none(self):
        self.assertEqual(sync.parse_remote("https://gitlab.com/a/b.git"),
                         (None, None))
        self.assertEqual(sync.parse_remote(""), (None, None))


class TestBuildMessage(unittest.TestCase):
    PAIRS = [("M", "a.py"), ("A", "b/c.md")]

    def test_auto_message_has_date_and_stats(self):
        msg = sync.build_message(self.PAIRS, 10, 3, "2026-10-01")
        self.assertIn("月度同步 2026-10-01", msg)
        self.assertIn("2 个文件：+10 −3", msg)
        self.assertIn("改  a.py", msg)
        self.assertIn("增  b/c.md", msg)

    def test_custom_message_wins_and_ends_with_newline(self):
        self.assertEqual(sync.build_message(self.PAIRS, 1, 1, "2026-10-01",
                                            custom="hello"), "hello\n")


class TestScanRepoWithTempDir(unittest.TestCase):
    """整条链路(读目录 → 读文件 → 命中),在临时目录里跑,不碰真仓库。"""

    def setUp(self):
        self._orig_base = sync.BASE
        self.tmp = tempfile.mkdtemp(prefix="sync-test-")
        sync.BASE = self.tmp
        self.values = {"主播昵称": {"示例主播"}, "内网网段": {"10.1.2."}}

    def tearDown(self):
        sync.BASE = self._orig_base
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_detects_leak_in_named_file(self):
        self._write("secret.md", "主播：示例主播\n")
        hits = sync.scan_repo(self.values, ["secret.md"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "secret.md")

    def test_clean_file_reports_nothing(self):
        self._write("clean.md", "主播：主播A\n")
        self.assertEqual(sync.scan_repo(self.values, ["clean.md"]), [])

    def test_binary_extensions_skipped(self):
        self._write("x.flv", "示例主播")
        self.assertEqual(sync.scan_repo(self.values, ["x.flv"]), [])

    def test_missing_file_does_not_raise(self):
        self.assertEqual(sync.scan_repo(self.values, ["nope.md"]), [])


class TestGitignoreProtection(unittest.TestCase):
    """待提交文件集里绝不能出现敏感/大体积路径 —— 这条错了就是账号失窃。"""

    FORBIDDEN = ("auth/", "config.json", "config.prev.json", "hotspots.json",
                 "recordings/", "logs/", "previews/", "spool/",
                 "recordings_backup_20260825/", ".venv/")

    def test_candidate_files_excludes_sensitive_paths(self):
        files = sync.candidate_files()
        self.assertTrue(files, "待提交文件集不该为空")
        for rel in files:
            for bad in self.FORBIDDEN:
                self.assertFalse(rel == bad or rel.startswith(bad),
                                 "待提交文件集里出现了 %s" % rel)

    def test_candidate_files_includes_known_tracked_files(self):
        files = set(sync.candidate_files())
        for rel in ("webui.py", "static/index.html", "scripts/ctl.py",
                    "scripts/sync_github.py", ".gitignore"):
            self.assertIn(rel, files)


@unittest.skipUnless(_has_real_config(), "本机没有 config.json，跳过真实仓库扫描")
class TestRealRepoIsClean(unittest.TestCase):
    """守门人:当前入库文件集里不得出现任何真实值。

    以后谁把真实主播名、sec_uid、NAS 地址写进代码或文档,跑测试就会红。
    """

    def test_no_real_value_in_committable_files(self):
        cfg = _load_json(REAL_CFG)
        try:
            hs = _load_json(os.path.join(BASE, "hotspots.json"))
        except Exception:
            hs = {}
        values = sync.private_values(cfg, hs)
        hits = sync.scan_repo(values)
        self.assertEqual(
            hits, [],
            "待提交文件里出现真实值:\n" +
            "\n".join("  %s:%s [%s] %s" % h for h in hits[:20]))


class TestScriptHygiene(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(BASE, "scripts", "sync_github.py"),
                  encoding="utf-8") as f:
            self.src = f.read()

    def test_push_script_path_is_configurable(self):
        self.assertIn("DEFAULT_PUSH_SCRIPT", self.src)
        self.assertIn("--push-script", self.src)
        self.assertIn("GITHUB_PUSH_SCRIPT", self.src)

    def test_uses_absolute_git_binary(self):
        """PATH 首位挂着 safe-delete shim，必须用 /usr/bin/git 绝对路径。"""
        self.assertIn('GIT = "/usr/bin/git"', self.src)

    def test_does_not_hardcode_real_values(self):
        try:
            cfg = _load_json(REAL_CFG)
        except Exception:
            self.skipTest("本机没有 config.json")
        for m in (cfg.get("monitors") or []):
            for key in ("name", "anchor"):
                v = str(m.get(key) or "")
                if sync.worth_blocking(v):
                    self.assertNotIn(v, self.src, "脚本里硬编码了真实值")
        host = str((cfg.get("nas") or {}).get("host") or "")
        if sync.worth_blocking(host):
            self.assertNotIn(host, self.src)

    def test_cleans_proxy_and_pythonpath_before_push(self):
        self.assertIn("NO_PROXY", self.src)
        self.assertIn("pythonpath", self.src)


if __name__ == "__main__":
    unittest.main()
