#!/usr/bin/env python3
"""`nas.nas_watch` 的事件循环安全性守卫(2026-10-27 事故复盘)。

事故经过:`nas_watch` 在协程里**同步**调用 `process_queue`,逐字节搬 5.5 GB
到 SMB(实测 ~2.6 MB/s,约 35 分钟)。整条 asyncio 事件循环被独占,12 个检测
协程从 `create_task` 之后连第一次调度都没拿到 —— `supervise` 首行的
`set_streamer()` 从未执行,`/api/status.streamers` 恒为 `{}`,页面「热点窗口 /
上次开播」全部变成「—」。这期间任何主播开播都不会被发现、也不会开录。

所以这里守两件事:
  1. `_offload` 真的把阻塞调用交出去了,等待期间事件循环仍在推进;
  2. `nas_watch` 里凡是会碰 SMB 的函数,都不允许出现「裸的同步调用」——
     必须作为 `_offload` 的第一个参数出现。以后再有人改回去会直接报红。
"""
import ast
import asyncio
import os
import time
import unittest

import nas

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 会碰 SMB / 远端卷的同步函数:在协程里裸调就会阻塞事件循环
SMB_BOUND = {
    "process_queue", "resolve_target", "backfill_from_history",
    "_init_dirs", "_host_alive", "auto_recover_host", "_nas_status",
}


class TestOffload(unittest.TestCase):
    def test_offload_does_not_block_event_loop(self):
        """搬运期间事件循环必须继续推进(否则检测协程会被饿死)。"""
        ticks = []

        async def main():
            async def ticker():
                while True:
                    await asyncio.sleep(0.05)
                    ticks.append(1)
            task = asyncio.ensure_future(ticker())
            await nas._offload(time.sleep, 0.4)     # 模拟一次慢搬运
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(main())
        # 0.4s 的阻塞里,50ms 的心跳至少该跑过 3 轮;同步执行的话这里是 0
        self.assertGreaterEqual(len(ticks), 3,
                                "阻塞调用占住了事件循环:期间其他协程一次都没跑")

    def test_offload_returns_value_and_reraises(self):
        async def main():
            self.assertEqual(await nas._offload(lambda a, b: a + b, 2, b=3), 5)

            def boom():
                raise ValueError("搬运失败")
            with self.assertRaises(ValueError):
                await nas._offload(boom)

        asyncio.run(main())

    def test_offload_beats_during_long_task(self):
        """长搬运要持续补心跳,否则 archive 会被面板误判成卡死。"""
        beats = []
        orig = nas.health.beat

        def fake_beat(key, **kw):
            beats.append(key)
            return orig(key, **kw)

        async def main():
            nas.health.beat = fake_beat
            try:
                # 31s 的真实等待太慢:把补心跳间隔打桩成 0.05s
                import itertools
                real_sleep = asyncio.sleep
                calls = itertools.count()

                async def fast_sleep(t, *a, **kw):
                    if t == 30:                      # 只改 _offload 的心跳节拍
                        return await real_sleep(0.05)
                    return await real_sleep(t, *a, **kw)

                asyncio.sleep = fast_sleep
                try:
                    await nas._offload(time.sleep, 0.3, beat="archive")
                finally:
                    asyncio.sleep = real_sleep
            finally:
                nas.health.beat = orig

        asyncio.run(main())
        self.assertGreaterEqual(len(beats), 3,
                                "长搬运期间没有补心跳,面板会误报 archive 卡死")


class TestNasWatchNeverBlocksLoop(unittest.TestCase):
    """静态守卫:`nas_watch` 里不得出现裸的同步 SMB 调用。"""

    def _nas_watch_node(self):
        with open(os.path.join(BASE, "nas.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        nodes = [n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "nas_watch"]
        self.assertEqual(len(nodes), 1, "nas.py 里应当恰好有一个 nas_watch")
        return nodes[0]

    def test_no_bare_smb_call_in_nas_watch(self):
        fn = self._nas_watch_node()
        parents = {}
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        bare = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in SMB_BOUND:
                continue
            parent = parents.get(node)
            offloaded = (
                isinstance(parent, ast.Call)
                and (getattr(parent.func, "id", None) == "_offload")
                and parent.args and parent.args[0] is node
            )
            if not offloaded:
                bare.append(f"{name}() 第 {node.lineno} 行")
        self.assertEqual(
            bare, [],
            "nas_watch 里有裸的同步 SMB 调用(会阻塞事件循环、饿死检测协程):"
            + "、".join(bare) + " —— 请改成 await _offload(...)")

    def test_offload_helper_exists_and_is_awaited(self):
        fn = self._nas_watch_node()
        awaited = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Await)
                   and isinstance(n.value, ast.Call)
                   and getattr(n.value.func, "id", None) == "_offload"]
        self.assertGreaterEqual(len(awaited), 3,
                                "nas_watch 里 await _offload(...) 少得可疑")


if __name__ == "__main__":
    unittest.main()
