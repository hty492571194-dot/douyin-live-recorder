"""项目指引(目录跳转)的单测。"""
import os
import subprocess
import unittest

import webui


class TestGuideFolders(unittest.TestCase):
    BASE = webui.BASE  # 测试运行环境自带 BASE,这里只断言"路径基于 BASE 拼"这件事。

    def test_groups_and_base(self):
        """清单必须非空,每个分组都有至少一条;返回结构含 base。"""
        out = webui._guide_folders({})
        self.assertEqual(out["base"], self.BASE)
        self.assertGreaterEqual(len(out["groups"]), 1)
        for g in out["groups"]:
            self.assertIn("group", g)
            self.assertGreaterEqual(len(g["items"]), 1)
            for it in g["items"]:
                self.assertIn("key", it)
                self.assertIn("name", it)
                self.assertIn("path", it)
                self.assertIn("exists", it)

    def test_rel_path_uses_base(self):
        """rel 类条目的路径 = BASE + rel,项目搬家后自动跟随 —— 这是迁移友好的关键。"""
        out = webui._guide_folders({})
        rel_items = [it for g in out["groups"]
                     for it in g["items"] if it["path"].startswith(self.BASE)]
        # 至少有 root / logs / previews / recordings 这几个标准目录项
        names = {it["key"] for it in rel_items}
        self.assertIn("root", names)
        self.assertIn("logs", names)
        self.assertIn("previews", names)
        self.assertIn("recordings", names)
        # 绝对路径必须用 realpath,与 BASE 等价(去 symlink)
        for it in rel_items:
            self.assertTrue(
                it["path"] == os.path.realpath(self.BASE)
                or it["path"].startswith(os.path.realpath(self.BASE) + os.sep),
                it,
            )

    def test_nas_root_reads_config(self):
        """nas_root 走 dynamic:mount_point + root_dir,配置变路径就变。"""
        cfg = {"nas": {"mount_point": "~/DouyinArchive", "root_dir": "直播回放"}}
        out = webui._guide_folders(cfg)
        nas = next(it for g in out["groups"] for it in g["items"]
                   if it["key"] == "nas_root")
        self.assertTrue(nas["path"].endswith(os.path.join("DouyinArchive", "直播回放")))
        # root_dir 为空时只到挂载点
        cfg2 = {"nas": {"mount_point": "~/DouyinArchive", "root_dir": ""}}
        out2 = webui._guide_folders(cfg2)
        nas2 = next(it for g in out2["groups"] for it in g["items"]
                    if it["key"] == "nas_root")
        self.assertTrue(nas2["path"].endswith("DouyinArchive"))
        self.assertFalse(nas2["path"].endswith("直播回放"))

    def test_launchagents_dynamic(self):
        out = webui._guide_folders({})
        la = next(it for g in out["groups"] for it in g["items"]
                  if it["key"] == "launchagents")
        self.assertTrue(la["path"].endswith("LaunchAgents"))

    def test_unknown_key_rejected(self):
        """未知 key 必须拒绝,不允许任意路径从外部塞进来。"""
        calls = []
        orig = subprocess.Popen
        subprocess.Popen = lambda *a, **kw: calls.append(a)
        try:
            res = webui._open_guide_folder("__nonexistent_key__", {})
            self.assertFalse(res["ok"])
            self.assertIn("未知", res["error"])
            self.assertEqual(calls, [], "未知 key 不应触发任何进程")
        finally:
            subprocess.Popen = orig


if __name__ == "__main__":
    unittest.main()