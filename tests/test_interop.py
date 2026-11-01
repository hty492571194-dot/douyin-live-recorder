"""interop(跨项目对接层)单测 —— 全部临时目录 + 合成值,不碰真实 NAS 与 history。

覆盖:
  - sanitize(文件名清洗,与对端 sanitize_key 同语义)
  - session.json 两态写入 / 前缀合并 / 归档兜底(ensure_session_state_finalized)
  - 问题反馈消费(取走语义:原名移入已处理/、.log 附件连带、坏包不动、撞名拒绝)
  - locate_session(日期目录级定位 + HHMMSS 尾匹配)
  - 诊断包组包与投递(zip 内容、哈希口径、原子落位、回读校验、幂等)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import interop  # noqa: E402


def _mk_session(dest, stem="主播A-20261013-184436"):
    """造一个最小但结构真实的归档日期目录(文件名/层级按契约)。"""
    os.makedirs(os.path.join(dest, ".meta"), exist_ok=True)
    with open(os.path.join(dest, stem + "-000.flv"), "wb") as f:
        f.write(b"\x00" * 2048)
    with open(os.path.join(dest, stem + "-000.ass"), "w", encoding="utf-8") as f:
        f.write("[Script Info]\n")
    with open(os.path.join(dest, ".meta", stem + ".danmaku.jsonl"), "w",
              encoding="utf-8") as f:
        f.write('{"ts": 1000, "type": "chat", "user": "用户甲", "content": "对"}\n')
    with open(os.path.join(dest, ".meta", stem + ".align.json"), "w",
              encoding="utf-8") as f:
        json.dump({"anchors": {"000": 1788000000000}, "drift_ms": 3.2,
                   "gaps": [], "summary": {"chat": 1, "gift": 0}}, f)
    return stem


def _mk_history(tmp, dest, name="主播A", date="2026-10-13",
                stem="主播A-20261013-184436", state="archived"):
    """最小 history.json:单条目指向 dest。"""
    hp = os.path.join(tmp, "history.json")
    entry = {
        "name": name,
        "output_path": os.path.join(tmp, "recordings", name, date,
                                    f"{name}-{date.replace('-', '')}-184436-%03d.flv"),
        "status": "done",
        "archive": {"state": state, "target": "nas", "dest": dest,
                    "at": 1792000000.0, "error": None},
    }
    with open(hp, "w", encoding="utf-8") as f:
        json.dump([entry], f, ensure_ascii=False)
    return hp


class TestSanitize(unittest.TestCase):
    def test_unsafe_runs_become_underscore(self):
        self.assertEqual(interop.sanitize("主播B🐰/2026-10-13"), "主播B_2026-10-13")
        self.assertEqual(interop.sanitize("a b/c"), "a_b_c")

    def test_safe_chars_kept(self):
        self.assertEqual(interop.sanitize("a-b.c_d"), "a-b.c_d")

    def test_matches_asrclip_example(self):
        # 对端手册 §3.1 真实对照:🐰 洗掉,其余保留
        self.assertEqual(interop.sanitize("主播A🐰/2026-10-13/x"), "主播A_2026-10-13_x")


class TestSessionState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="interop-ss-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_recording_then_finalized_keeps_prefixes(self):
        interop.write_session_state(self.dir, "recording", ["s1"])
        doc = interop.mark_finalized(self.dir)
        self.assertEqual(doc["state"], "finalized")
        self.assertEqual(doc["prefixes"], ["s1"])
        with open(os.path.join(self.dir, "session.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["state"], "finalized")

    def test_prefixes_merge_across_sessions(self):
        interop.write_session_state(self.dir, "recording", ["s1"])
        doc = interop.write_session_state(self.dir, "recording", ["s2"])
        self.assertEqual(doc["prefixes"], ["s1", "s2"])
        doc = interop.write_session_state(self.dir, "recording", ["s1"])  # 幂等
        self.assertEqual(doc["prefixes"], ["s1", "s2"])

    def test_ensure_finalized_backfills_and_is_idempotent(self):
        stem = _mk_session(self.dir)
        self.assertTrue(interop.ensure_session_state_finalized(self.dir))
        with open(os.path.join(self.dir, "session.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["state"], "finalized")
        self.assertEqual(doc["prefixes"], [stem])
        self.assertFalse(interop.ensure_session_state_finalized(self.dir))


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.fb = tempfile.mkdtemp(prefix="interop-fb-")
        self.done = os.path.join(self.fb, "已处理")
        self.addCleanup(shutil.rmtree, self.fb, ignore_errors=True)

    def _packet(self, name="问题-主播A_2026-10-13-123.json", **over):
        rec = {"session_key": "主播A/2026-10-13/主播A_2026-10-13_184436",
               "run_id": "abcd1234ef01", "ts": 1792000000,
               "severity": "warning", "stage": "g0", "exit_code": 0,
               "summary": "G0 体检 FAIL(默认放行继续跑)", "detail_ref": "",
               "suspect_files": [], "hint": ""}
        rec.update(over)
        p = os.path.join(self.fb, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        return p

    def test_consume_moves_original_name_into_done(self):
        p = self._packet()
        rec, moved = interop.consume_feedback_file(p, self.done)
        self.assertEqual(rec["severity"], "warning")
        self.assertEqual(moved, [os.path.basename(p)])
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(os.path.join(self.done, os.path.basename(p))))

    def test_consume_takes_detail_log_along(self):
        p = self._packet(detail_ref="问题-主播A_2026-10-13-123.log")
        with open(os.path.join(self.fb, "问题-主播A_2026-10-13-123.log"), "w") as f:
            f.write("log tail\n")
        _, moved = interop.consume_feedback_file(p, self.done)
        self.assertEqual(len(moved), 2)
        self.assertEqual(sorted(os.listdir(self.fb)), ["已处理"])

    def test_missing_log_is_not_fatal(self):
        p = self._packet(detail_ref="不存在的.log")
        _, moved = interop.consume_feedback_file(p, self.done)
        self.assertEqual(len(moved), 1)

    def test_bad_json_left_in_place(self):
        p = os.path.join(self.fb, "问题-半文件.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"session_key": "主播A/202')     # 半文件
        with self.assertRaises(Exception):
            interop.consume_feedback_file(p, self.done)
        self.assertTrue(os.path.exists(p))            # 原地留下轮重读,绝不删除

    def test_refuses_to_overwrite_done(self):
        p1 = self._packet()
        interop.consume_feedback_file(p1, self.done)
        p2 = self._packet()                            # 同名再来一包
        with self.assertRaises(OSError):
            interop.consume_feedback_file(p2, self.done)
        self.assertTrue(os.path.exists(p2))            # 拒绝覆盖且不丢包

    def test_list_feedback_skips_subdir_and_part(self):
        self._packet()
        with open(os.path.join(self.fb, "x.json.part"), "w") as f:
            f.write("{}")
        os.makedirs(os.path.join(self.fb, "已处理", "y.json"), exist_ok=True)
        got = interop.list_feedback(self.fb)
        self.assertEqual([os.path.basename(p) for p in got],
                         ["问题-主播A_2026-10-13-123.json"])


class TestLocateSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="interop-loc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = os.path.join(self.tmp, "直播回放", "主播A", "2026-10-13")
        os.makedirs(self.dest)
        self.stem = _mk_session(self.dest)
        self.hp = _mk_history(self.tmp, self.dest)

    def test_date_level_and_tail_match(self):
        loc = interop.locate_session("主播A/2026-10-13/主播A_2026-10-13_184436",
                                     history_path=self.hp)
        self.assertEqual(loc["dest"], self.dest)
        self.assertEqual(loc["picked"], [self.stem])   # HHMMSS 尾命中
        loc2 = interop.locate_session("主播A/2026-10-13", history_path=self.hp)
        self.assertEqual(loc2["picked"], [self.stem])  # 无 stem → 全量

    def test_unarchived_entry_rejected(self):
        hp = _mk_history(self.tmp, self.dest, state="pending")
        with self.assertRaises(ValueError):
            interop.locate_session("主播A/2026-10-13", history_path=hp)

    def test_malformed_key_rejected(self):
        with self.assertRaises(ValueError):
            interop.locate_session("只有主播名", history_path=self.hp)


class TestDiagnostic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="interop-diag-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dest = os.path.join(self.tmp, "直播回放", "主播A", "2026-10-13")
        os.makedirs(self.dest)
        self.stem = _mk_session(self.dest)
        self.hp = _mk_history(self.tmp, self.dest)
        self.wd = os.path.join(self.tmp, "work")
        os.makedirs(self.wd)

    def test_build_zip_contents_and_hash_policy(self):
        built = interop.build_diagnostic("主播A/2026-10-13/主播A_2026-10-13_184436",
                                         cfg={"webui": {"port": 59999}},
                                         workdir=self.wd, history_path=self.hp)
        self.assertEqual(built["manifest"]["session_key"],
                         "主播A/2026-10-13/主播A_2026-10-13_184436")
        with zipfile.ZipFile(built["zip_path"]) as z:
            names = set(z.namelist())
        self.assertEqual(names, {"manifest.json", "status.json",
                                 "ffmpeg-err-tail.log", "history-entry.json",
                                 "files.tsv", "align-key.json"})
        self.assertEqual(built["manifest"]["inner_files"][f".meta/{self.stem}.align.json"],
                         interop._sha256(os.path.join(
                             self.dest, ".meta", f"{self.stem}.align.json"))[:8])
        # 哈希口径:flv 不做哈希(留空),sidecar 做 sha8
        self.assertNotIn(f"{self.stem}-000.flv", built["manifest"]["inner_files"])

    def test_deliver_atomic_and_idempotent(self):
        built = interop.build_diagnostic("主播A/2026-10-13",
                                         cfg={"webui": {"port": 59999}},
                                         workdir=self.wd, history_path=self.hp)
        diag_dir = os.path.join(self.tmp, "_对接", "诊断包")
        ok, dst = interop.deliver_diagnostic(built["zip_path"],
                                             built["manifest_text"],
                                             built["manifest_name"], diag_dir)
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(dst))
        self.assertTrue(os.path.exists(os.path.join(
            diag_dir, built["manifest_name"])))
        # 回读校验:落位文件与源一致
        self.assertEqual(interop._sha256(dst), built["sha256"])
        # 幂等:再投不覆盖
        ok2, msg2 = interop.deliver_diagnostic(built["zip_path"],
                                               built["manifest_text"],
                                               built["manifest_name"], diag_dir)
        self.assertFalse(ok2)
        self.assertIn("已存在", msg2)
        # .part 不留残渣
        self.assertEqual([n for n in os.listdir(diag_dir) if n.endswith(".part")], [])


class TestMailboxStatus(unittest.TestCase):
    """mailbox_status(NAS 信箱四通道只读探测):全 mock NAS 面,临时目录造树。"""

    def setUp(self):
        interop._MAILBOX_CACHE.update(ts=0.0, data=None)   # 测试间缓存隔离
        self.tmp = tempfile.mkdtemp(prefix="interop-mb-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        replay = os.path.join(self.tmp, "直播回放")
        oppo = os.path.join(self.tmp, "_对接")
        for d in (os.path.join(replay, "主播A", "2026-10-13"),
                  os.path.join(replay, "主播A", "2026-10-14"),
                  os.path.join(oppo, "contract"),
                  os.path.join(oppo, "问题反馈", "已处理"),
                  os.path.join(oppo, "诊断包")):
            os.makedirs(d, exist_ok=True)
        self.replay, self.oppo = replay, oppo
        self.cfg = {"archive": {"mode": "nas"},
                    "nas": {"enabled": True, "root_dir": "直播回放"}}
        # NAS 面打桩:resolve_target 返回临时根,绝不碰真实挂载
        p = unittest.mock.patch("nas.resolve_target",
                                return_value=(True, self.tmp, "nas"))
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _put(path, text="x"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_ok_full_channels_and_contract_match(self):
        # 契约远端副本 = 复制本地真源 → sha256 对账一致
        shutil.copyfile(os.path.join(interop.CONTRACT_DIR, interop.CONTRACT_FILE),
                        os.path.join(self.oppo, "contract", interop.CONTRACT_FILE))
        self._put(os.path.join(self.oppo, "contract", interop.CHANGELOG_FILE))
        self._put(os.path.join(self.replay, "主播A", "2026-10-13.meta.json"), "{}")
        self._put(os.path.join(self.replay, "主播A", "2026-10-14.meta.json"), "{}")
        self._put(os.path.join(self.oppo, "问题反馈", "问题-主播A-x.json"), "{}")
        self._put(os.path.join(self.oppo, "问题反馈", "已处理", "问题-主播A-y.json"), "{}")
        self._put(os.path.join(self.oppo, "诊断包", "诊断包-主播A.zip"), "PK")
        d = interop.mailbox_status(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertEqual(d["nas_root"], self.tmp)
        self.assertTrue(d["contract"]["match"])
        self.assertEqual(d["contract"]["remote_version"],
                         d["contract"]["local_version"])
        self.assertTrue(d["contract"]["changelog"])
        self.assertEqual(d["ready"]["count"], 2)
        self.assertIsNotNone(d["ready"]["latest_at"])
        self.assertEqual(d["feedback"]["pending"], 1)
        self.assertEqual(d["feedback"]["done"], 1)
        self.assertEqual(d["diagnostic"]["count"], 1)

    def test_contract_mismatch_is_reported_not_raised(self):
        self._put(os.path.join(self.oppo, "contract", interop.CONTRACT_FILE),
                  '{"contract_version": "0.0.0"}')
        d = interop.mailbox_status(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertFalse(d["contract"]["match"])
        self.assertEqual(d["contract"]["remote_version"], "0.0.0")
        self.assertEqual(d["contract"]["local_version"],
                         interop.contract_version())

    def test_empty_mailbox_degrades_to_zeroes(self):
        d = interop.mailbox_status(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertFalse(d["contract"]["match"])
        self.assertIsNone(d["contract"]["delivered_at"])
        self.assertEqual(d["ready"]["count"], 0)
        self.assertEqual(d["feedback"]["pending"], 0)
        self.assertEqual(d["feedback"]["done"], 0)
        self.assertEqual(d["diagnostic"]["count"], 0)
        # 次要目录缺失也不炸:问题反馈/已处理 整个不存在
        shutil.rmtree(os.path.join(self.oppo, "问题反馈"))
        d2 = interop.mailbox_status(self.cfg, force=True)
        self.assertTrue(d2["ok"])
        self.assertEqual(d2["feedback"], {"pending": 0, "done": 0,
                                          "oldest_pending_at": None,
                                          "latest_done_at": None})

    def test_unreachable_nas_returns_reason(self):
        with unittest.mock.patch("nas.resolve_target",
                                 return_value=(False, None, "nas")):
            d = interop.mailbox_status(self.cfg, force=True)
        self.assertFalse(d["ok"])
        self.assertIn("nas", d["reason"])

    def test_cache_hit_until_ttl(self):
        self._put(os.path.join(self.oppo, "诊断包", "诊断包-x.zip"), "PK")
        first = interop.mailbox_status(self.cfg, force=True)
        self.assertFalse(first["cached"])
        # 不带 force:命中缓存(即便信箱内容此后变化)
        self._put(os.path.join(self.oppo, "诊断包", "诊断包-y.zip"), "PK")
        second = interop.mailbox_status(self.cfg)
        self.assertTrue(second["cached"])
        self.assertEqual(second["diagnostic"]["count"], 1)
        # force:重探拿到新状态
        third = interop.mailbox_status(self.cfg, force=True)
        self.assertFalse(third["cached"])
        self.assertEqual(third["diagnostic"]["count"], 2)


class TestNotice(unittest.TestCase):
    """S5 产物变更通告(append_notice):置顶追加、原子写、格式头。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="interop-notice-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _read(self):
        with open(os.path.join(self.dir, interop.NOTICE_FILE),
                  encoding="utf-8") as f:
            return f.read()

    def test_first_entry_creates_file_with_header(self):
        ok, path = interop.append_notice("产物", "分片时长调整为 2400s",
                                         "影响: complete_when", contract_dir=self.dir)
        self.assertTrue(ok)
        self.assertEqual(path, os.path.join(self.dir, interop.NOTICE_FILE))
        text = self._read()
        self.assertIn("# 产物变更通告", text)
        self.assertIn("— 分片时长调整为 2400s", text)
        self.assertIn("- 影响: complete_when", text)

    def test_new_entry_goes_on_top(self):
        interop.append_notice("产物", "第一条(旧)", contract_dir=self.dir)
        interop.append_notice("通道", "第二条(新)", contract_dir=self.dir)
        text = self._read()
        i_new = text.index("第二条(新)")
        i_old = text.index("第一条(旧)")
        self.assertLess(i_new, i_old)
        # 条目头格式可被周报解析:## [YYYY-MM-DD HH:MM] 类型 — 摘要
        import re as _re
        self.assertTrue(_re.search(
            r"^## \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] 通道 — 第二条\(新\)$",
            text, _re.M))

    def test_empty_summary_rejected(self):
        ok, reason = interop.append_notice("产物", "   ", contract_dir=self.dir)
        self.assertFalse(ok)
        self.assertIn("摘要", reason)
        self.assertFalse(os.path.exists(os.path.join(self.dir, interop.NOTICE_FILE)))

    def test_no_temp_file_left(self):
        interop.append_notice("停机", "维护窗口 02:00-03:00", contract_dir=self.dir)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_default_kind_and_multiline_detail(self):
        ok, _ = interop.append_notice("", "摘要", "行一\n行二", contract_dir=self.dir)
        self.assertTrue(ok)
        text = self._read()
        self.assertIn("— 摘要", text)
        self.assertIn("- 行一", text)
        self.assertIn("- 行二", text)


if __name__ == "__main__":
    unittest.main()
