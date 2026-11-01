#!/usr/bin/env python3
"""测试不得写进正式业务日志(由 tests/__init__.py 保证)。

为什么要专门守这条:2026-09-18 排查「服务反复重启」时,把日志里测试产生的
`[历史] 重新定位` / `[热点] A 开播时刻` 当成了真实的服务启动痕迹,
得出完全错误的结论(实际 launchctl 只跑过 2 次)。
根因就是测试与正式服务共用了同一个 `logs/monitor-YYYY-MM-DD.log`。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import webui  # noqa: E402

REAL_LOG_DIR = os.path.join(webui.BASE, "logs")


class TestLogIsolation(unittest.TestCase):
    def test_log_dir_is_not_production(self):
        """测试进程里的日志目录必须**不是**项目内 logs/。"""
        self.assertNotEqual(
            os.path.realpath(monitor.LOG_DIR), os.path.realpath(REAL_LOG_DIR),
            "测试在往正式业务日志里写 —— 见 tests/__init__.py")

    def test_log_dir_exists(self):
        self.assertTrue(os.path.isdir(monitor.LOG_DIR), monitor.LOG_DIR)

    def test_log_writes_go_to_isolated_dir(self):
        """真调一次 log(),确认落在隔离目录而不是项目内。"""
        monitor.log("__isolation_probe__")
        found = False
        for fname in os.listdir(monitor.LOG_DIR):
            if not fname.startswith("monitor-"):
                continue
            with open(os.path.join(monitor.LOG_DIR, fname),
                      encoding="utf-8", errors="replace") as f:
                if "__isolation_probe__" in f.read():
                    found = True
                    break
        self.assertTrue(found, "log() 没有写进隔离目录 %s" % monitor.LOG_DIR)


if __name__ == "__main__":
    unittest.main()
