#!/usr/bin/env python3
"""preview.py 单元测试(临时目录,不依赖真实直播流/ffmpeg)。"""
import os
import tempfile
import unittest

import preview


class TestPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self):
        return {"preview": {"dir": self.base}, "monitors": []}

    def test_safe_join_path_traversal(self):
        """路径穿越与越界文件被拒绝,正常文件放行。"""
        cfg = self._cfg()
        self.assertIsNone(preview.safe_join(cfg, "s1", "../evil.jpg"))
        self.assertIsNone(preview.safe_join(cfg, "s1", "a/b.jpg"))
        self.assertIsNone(preview.safe_join(cfg, "s1", ""))
        self.assertIsNone(preview.safe_join(cfg, "s1", "..\\evil.jpg"))
        d = os.path.join(self.base, "s1")
        os.makedirs(d)
        with open(os.path.join(d, "x.jpg"), "w") as f:
            f.write("img")
        self.assertIsNotNone(preview.safe_join(cfg, "s1", "x.jpg"))

    def test_list_images_filters_ext(self):
        """只列出图片扩展名,排除其他文件。"""
        cfg = self._cfg()
        d = os.path.join(self.base, "s1")
        os.makedirs(d)
        for fn in ["a.jpg", "b.png", "c.txt", "d.webp", "e.jpeg"]:
            with open(os.path.join(d, fn), "w") as f:
                f.write("x")
        self.assertEqual(preview.list_images(cfg, "s1"),
                         ["a.jpg", "b.png", "d.webp", "e.jpeg"])

    def test_cleanup_others(self):
        """确认预览图后仅保留选中项,删除其余图片。"""
        cfg = self._cfg()
        d = os.path.join(self.base, "s1")
        os.makedirs(d)
        for fn in ["a.jpg", "b.jpg", "c.png"]:
            with open(os.path.join(d, fn), "w") as f:
                f.write("x")
        preview.cleanup_others(cfg, "s1", "a.jpg")
        self.assertEqual(preview.list_images(cfg, "s1"), ["a.jpg"])

    def test_selected_preview(self):
        """从 monitors 读取已选预览图文件名,未设置返回空串。"""
        cfg = {"monitors": [{"name": "s1", "preview": "a.jpg"}, {"name": "s2"}]}
        self.assertEqual(preview.selected_preview(cfg, "s1"), "a.jpg")
        self.assertEqual(preview.selected_preview(cfg, "s2"), "")

    def test_capture_frame_fake_ffmpeg(self):
        """假 ffmpeg 脚本写非空输出文件 → 判定成功。"""
        fake = os.path.join(self.base, "ffmpeg")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env python3\nimport sys\n"
                    "open(sys.argv[-1], 'wb').write(b'JPEGDATA')\n")
        os.chmod(fake, 0o755)
        out = os.path.join(self.base, "o.jpg")
        self.assertTrue(preview.capture_frame(fake, "http://x", out))
        self.assertTrue(os.path.getsize(out) > 0)

    def test_capture_frame_no_ffmpeg(self):
        """无 ffmpeg 或无流地址 → 直接失败,不抛异常。"""
        self.assertFalse(preview.capture_frame("", "http://x", "/tmp/x.jpg"))
        self.assertFalse(preview.capture_frame("/bin/true", "", "/tmp/x.jpg"))


if __name__ == "__main__":
    unittest.main()
