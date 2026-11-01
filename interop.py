#!/usr/bin/env python3
"""跨项目对接层:录制端(douyin-monitor) ↔ 转录端(asrclip, 跑在另一台 Mac)。

两机零 HTTP,通信全走 NAS 文件面 `<NAS根>/_对接/`(必须与「直播回放」平级——
放进「直播回放」里会被对端的主播发现当成主播目录,红线)。权威规范:对端
《跨项目对接-录制端实施手册-v1.0.md》;本端决策:工作区
《跨项目对接-局域网对接方式分析与录制端实施方案-v1.0.md》。

四个能力(全部旁路,任何失败都不许阻塞检测/录制/归档主链路):
  S1 契约快照   contract/ → _对接/contract/             scripts/sync_contract.py
  S2 随数据走   <日期>/session.json                      monitor 起录/收尾 + nas 归档兜底
  S3.B 问题反馈 _对接/问题反馈/ → 消费(移入已处理/)       monitor 常驻协程 600s
  S3.A 诊断包   douyin diagnose <key> → _对接/诊断包/     纯 CLI,不依赖服务在线
  S5 变更通告   产物调整 → _对接/contract/NOTICE.md       scripts/notify_backend.py(唯一入口;
                与契约快照同批投递,producer_duties 段声明了这条义务)

安全边界(违反即事故):
  - 对「直播回放」只读;只写 _对接/ 三个子目录;
  - session_key 只从 JSON 内容读,绝不解析反馈包文件名(对端文件名清洗不可逆);
  - 消费 = 原名同卷 rename 移入 已处理/,绝不删改对端写入的原始包;
  - 一切写入原子(临时文件 + rename);
  - 契约/文档/测试一律合成值(敏感词门 TestRealRepoIsClean)。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_DIR = os.path.join(BASE, "contract")
CONTRACT_FILE = "contract.json"
CHANGELOG_FILE = "CHANGELOG.md"
NOTICE_FILE = "NOTICE.md"               # S5 产物变更通告(与契约快照同批投递)
NOTICE_KINDS = ("产物", "通道", "停机", "其他")
OPPO_NAME = "_对接"                     # 与「直播回放」平级的信箱目录
SESSION_STATE_NAME = "session.json"
FEEDBACK_POLL_SEC = 600                 # S3.B 轮询周期(与对端手册 §3.3 约定一致)
FFMPEG_TAIL_LINES = 100                 # 诊断包附带的 ffmpeg 报错日志尾部行数
HASH_HEAD = 8                           # 文件清单里 sha256 只取前 8 位
VIDEO_EXTS = {".flv", ".mp4", ".ts", ".mkv"}   # 大文件不做哈希(大小+mtime 对账)
PRODUCER_HOST = socket.gethostname() or "recorder"

# 场次前缀:<主播名>-<YYYYmmdd-HHMMSS>(可带 -NNN 分片序号)。
# 形态学:文件名 = <前缀>[-<序号>],前缀本身以 -<8位日期>-<6位时刻> 结尾。
# 用 splitext 后的文件名匹配,.meta/ 目录与会话状态文件天然不命中。
_STEM_RE = re.compile(r"^(?P<stem>.*-\d{8}-\d{6})(?:-\d+)?$")
_STAMP_TAIL_RE = re.compile(r"(\d{6})$")   # asrclip stem 尾部的 HHMMSS


# ── 小工具 ──────────────────────────────────────────────────────────────
def sanitize(text):
    r"""与对端 sanitize_key 同语义:连续 [^\w\-.]+(unicode 字义)替换为 '_'。

    用于**本端生成**的文件名(诊断包)。反方向(解析对端文件名)被红线禁止——
    清洗不可逆,session_key 必须从 JSON 内容读。
    """
    return re.sub(r"[^\w\-.]+", "_", str(text), flags=re.UNICODE)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path, data):
    """JSON 原子写。真源是 nas._atomic_write(临时文件 + os.replace,SMB 已验证);
    函数内取模块避免 nas ↔ interop 循环 import。"""
    from nas import _atomic_write
    _atomic_write(path, data)


def _atomic_write_text(path, text):
    """文本原子写(契约 CHANGELOG / manifest.sha256 等非 JSON 内容)。
    与 nas._atomic_write 同一原语模式:同目录临时文件 + os.replace。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_cfg():
    """读 config.json(供 CLI/脚本用;服务内由调用方传入 cfg)。"""
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


# ── NAS 面路径解析 ──────────────────────────────────────────────────────
def nas_paths(cfg):
    """解析 _对接/ 全套路径。返回 (ok, info) —— 失败时 info 是原因字符串。

    info: {nas_root, replay_dir, oppo_dir, feedback_dir, done_dir,
           contract_dir, diag_dir}
    只有 NAS 归档模式可用(external / 未挂载 → False):_对接/ 通道只存在于
    NAS 面上,外接硬盘模式下对端看不见,调用方按旁路语义静默跳过。
    """
    import nas as nas_mod          # 函数内 import,避免循环
    ok, target, mode = nas_mod.resolve_target(cfg or {})
    if not ok or mode != "nas" or not target:
        return False, f"归档目标不可用(mode={mode})"
    root = str(((cfg or {}).get("nas") or {}).get("root_dir") or "直播回放").strip("/")
    replay = os.path.join(target, root)
    if not os.path.isdir(replay):
        return False, f"「{root}」目录不存在: {replay}"
    # _对接/ 与「直播回放」平级(取其父目录,兼容嵌套 root_dir)
    oppo = os.path.join(os.path.dirname(replay.rstrip(os.sep)), OPPO_NAME)
    return True, {
        "nas_root": target, "replay_dir": replay, "oppo_dir": oppo,
        "feedback_dir": os.path.join(oppo, "问题反馈"),
        "done_dir": os.path.join(oppo, "问题反馈", "已处理"),
        "contract_dir": os.path.join(oppo, "contract"),
        "diag_dir": os.path.join(oppo, "诊断包"),
    }


# ── S1 契约真源 ────────────────────────────────────────────────────────
def load_contract():
    with open(os.path.join(CONTRACT_DIR, CONTRACT_FILE), encoding="utf-8") as f:
        return json.load(f)


def contract_version(default=""):
    try:
        return str(load_contract().get("contract_version") or "") or default
    except Exception:
        return default


def contract_payload():
    """/api/contract(仅本机)的响应体。"""
    return {"ok": True, "version": contract_version(),
            "path": CONTRACT_DIR, "contract": load_contract()}


# ── S5 产物变更通告 ────────────────────────────────────────────────────
NOTICE_HEADER = (
    "# 产物变更通告(NOTICE)\n"
    "\n"
    "- 录制端产物形态/语义调整时由 `scripts/notify_backend.py` 写入;"
    "义务声明见 contract.json `producer_duties` 段(契约 v1.1.0 起)。\n"
    "- 最新条目在最上;条目头格式:`## [YYYY-MM-DD HH:MM] <类型> — <摘要>`,"
    "类型 = " + "|".join(NOTICE_KINDS) + "。\n"
    "- 消费方读素材/对接状态时顺带看顶部条目即可获知变更;"
    "详细内容见同目录 CHANGELOG.md 与 contract.json。\n"
)
_FIRST_ENTRY_MARK = "\n## ["


def append_notice(kind, summary, detail="", now=None, contract_dir=None):
    """写一条产物变更通告到 contract/NOTICE.md(本地真源;投递由调用方跑
    scripts/notify_backend.py 或 sync_contract.py 完成)。

    最新条目置顶(插在头部说明与旧条目之间);整文件原子写,不会出现半条
    通告被对端读到。返回 (ok, 路径或原因)。
    """
    kind = str(kind or "其他").strip() or "其他"
    summary = " ".join(str(summary or "").split())
    if not summary:
        return False, "变更摘要为空"
    detail = str(detail or "").strip()
    ts = time.localtime(now if now is not None else time.time())
    head = "## [%s] %s — %s" % (time.strftime("%Y-%m-%d %H:%M", ts), kind, summary)
    entry = head + "\n" + ("".join("- %s\n" % ln for ln in detail.splitlines() if ln.strip())
                           if detail else "")
    cdir = contract_dir or CONTRACT_DIR
    path = os.path.join(cdir, NOTICE_FILE)
    try:
        old = ""
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""
    if not old.startswith("# 产物变更通告"):
        old = NOTICE_HEADER + ("\n" if old and not old.startswith("\n") else "")
    # 插入点:头部说明之后、第一个旧条目之前;没有旧条目就追加在末尾
    idx = old.find(_FIRST_ENTRY_MARK)
    new = (old[:idx] + "\n" + entry + old[idx:]) if idx >= 0 else old.rstrip("\n") + "\n\n" + entry
    try:
        os.makedirs(cdir, exist_ok=True)
        _atomic_write_text(path, new.lstrip("\n"))
    except OSError as e:
        return False, f"NOTICE.md 写入失败: {e}"
    return True, path


# ── S2 session.json 随数据走 ───────────────────────────────────────────
def _merge_prefixes(old, add):
    out = list(old or [])
    for s in add or ():
        if s and s not in out:
            out.append(s)
    return out


def write_session_state(date_dir, state, prefixes=()):
    """日期目录内 session.json(recording / finalized 两态,多场次共用一份)。

    幂等合并 prefixes;原子写;OSError 由调用方按旁路兜(仅日志)。
    """
    path = os.path.join(date_dir, SESSION_STATE_NAME)
    old = {}
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass
    doc = {
        "contract_version": contract_version(),
        "producer_host": PRODUCER_HOST,
        "state": state,
        "prefixes": _merge_prefixes(old.get("prefixes"), prefixes),
        "updated_at": time.time(),
    }
    _atomic_write_json(path, doc)
    return doc


def mark_finalized(date_dir):
    """场次收尾:同目录保留 prefixes,状态翻 finalized。"""
    return write_session_state(date_dir, "finalized")


def stems_in_dir(date_dir):
    """从日期目录实际文件推导场次前缀清单(顶层,不递归 .meta)。"""
    try:
        names = os.listdir(date_dir)
    except OSError:
        return []
    return sorted({m.group("stem") for n in names
                   for m in [_STEM_RE.match(os.path.splitext(n)[0])] if m})


def ensure_session_state_finalized(dest_dir):
    """归档落位后的 NAS 兜底(nas.py 在 _write_meta 旁调用)。

    老场次没有本机 session.json 随档而来 → 补一份 finalized(前缀按目录实况推导);
    已是 finalized → 不动。返回 True 表示写了。
    """
    path = os.path.join(dest_dir, SESSION_STATE_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            if json.load(f).get("state") == "finalized":
                return False
    except Exception:
        pass
    write_session_state(dest_dir, "finalized", stems_in_dir(dest_dir))
    return True


# ── S3.B 问题反馈消费(asrclip → 录制端) ───────────────────────────────
def list_feedback(feedback_dir):
    """待取包清单:主目录下 *.json(跳过 已处理/ 子目录与 .part 临时文件)。"""
    try:
        names = os.listdir(feedback_dir)
    except OSError:
        return []
    out = []
    for n in sorted(names):
        if not n.endswith(".json") or n.endswith(".part"):
            continue
        p = os.path.join(feedback_dir, n)
        if os.path.isfile(p):
            out.append(p)
    return out


def parse_feedback(path):
    """解析反馈包。🔴 session_key 只从 JSON 内容读,绝不解析文件名。"""
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    if not isinstance(rec, dict) or not str(rec.get("session_key") or "").strip():
        raise ValueError("缺少 session_key(半文件或异构包)")
    return rec


def _detail_logs(path, rec):
    """detail_ref 指向的同目录 .log(存在则必须与 json 一起消费)。"""
    ref = str(rec.get("detail_ref") or "").strip()
    if not ref:
        return []
    p = os.path.join(os.path.dirname(path), os.path.basename(ref))
    return [p] if os.path.isfile(p) else []


def consume_feedback_file(path, done_dir):
    """取走一个反馈包:解析 → 原名同卷 rename 移入 已处理/(含 .log 附件)。

    返回 (record, moved_names)。解析失败不动文件(半文件下轮重读);
    已处理/ 撞名 → 报错拒绝(绝不覆盖对端原始包)。
    """
    rec = parse_feedback(path)
    os.makedirs(done_dir, exist_ok=True)
    moved = []
    for p in [path] + _detail_logs(path, rec):
        dst = os.path.join(done_dir, os.path.basename(p))
        if os.path.exists(dst):
            raise OSError(f"已处理/ 已存在同名,拒绝覆盖: {os.path.basename(dst)}")
        os.rename(p, dst)
        moved.append(os.path.basename(dst))
    return rec, moved


def consume_pending(cfg, log=None):
    """消费一轮全部待取包。返回 (consumed, problems)。

    problems: [(文件名, 错误)] —— 坏包/半文件留在原地等下轮,绝不删除;
    NAS 不可用 → ([], []) 静默跳过(旁路铁律)。
    """
    ok, info = nas_paths(cfg)
    if not ok:
        return [], []
    consumed, problems = [], []
    for p in list_feedback(info["feedback_dir"]):
        try:
            rec, moved = consume_feedback_file(p, info["done_dir"])
            consumed.append(rec)
            if log:
                log(f"[对接] 已消费问题反馈 → 已处理/: {moved[0]}"
                    f" (severity={rec.get('severity')} stage={rec.get('stage')})"
                    + (f" 附件x{len(moved) - 1}" if len(moved) > 1 else ""))
        except Exception as e:                     # noqa: BLE001  旁路:坏包不拖垮轮询
            problems.append((os.path.basename(p), str(e)))
            if log:
                log(f"[对接] 反馈包读取失败(下轮重试): {os.path.basename(p)} — {e}")
    return consumed, problems


# ── S3.A 诊断包(录制端 → asrclip) ─────────────────────────────────────
def locate_session(session_key, history_path=None):
    """session_key '<主播>/<日期>[/<场次stem>]' → 定位结果 dict。

    坐标优先级:history.json[].archive.dest(只认 archived)→ 归档目录实况。
    对端的 stem 是它自己切的场次时刻,与录制前缀不必逐字一致 → 按 HHMMSS 尾
    匹配;对不上就按日期目录全量出(诊断包本来就是日期目录级对账)。
    """
    parts = [p for p in str(session_key).strip().strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError('session_key 形如 "<主播>/<日期>[/<场次stem>]"')
    name, date = parts[0], parts[1]
    stem_hint = parts[2] if len(parts) > 2 else ""
    hp = history_path or os.path.join(BASE, "recordings", "history.json")
    with open(hp, encoding="utf-8") as f:
        hist = json.load(f)
    entries = [e for e in hist if e.get("name") == name
               and f"/{date}/" in str(e.get("output_path") or "")]
    if not entries:                                # 宽松兜底:主播名含特殊字符时
        entries = [e for e in hist if name in str(e.get("name") or "")
                   and f"/{date}/" in str(e.get("output_path") or "")]
        if entries:
            name = entries[0].get("name")
    dest = None
    for e in entries:
        arch = e.get("archive") or {}
        if arch.get("state") == "archived" and arch.get("dest"):
            dest = arch["dest"]
            break
    if not dest:
        raise ValueError(f"未找到已归档场次: {name}/{date}"
                         f"(history 命中 {len(entries)} 条,但均未归档或无 dest)")
    if not os.path.isdir(dest):
        raise ValueError(f"归档目录当前不可达: {dest}")
    stems = stems_in_dir(dest)
    picked = stems
    tail = _STAMP_TAIL_RE.search(stem_hint)
    if tail:
        cand = [s for s in stems if s.endswith("-" + tail.group(1))]
        if cand:
            picked = cand
    return {"name": name, "date": date, "dest": dest,
            "entries": entries, "stems": stems, "picked": picked}


def _fetch_status_snapshot(cfg):
    """本机 /api/status 快照。服务离线不报错 —— 诊断包照出,只注明 offline。"""
    try:
        port = int(((cfg or {}).get("webui") or {}).get("port") or 8780)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
            return json.load(r)
    except Exception as e:                         # noqa: BLE001
        return {"note": "服务不可达,已跳过实时快照", "error": str(e)}


def _file_rows(dest):
    """场次文件清单行:(rel, size, mtime, sha8)。

    哈希口径:视频分片不做哈希(单文件数百 MB,SMB 全量读太重)→ sha8 留空,
    按 大小+mtime 对账;sidecar(jsonl/align)与字幕做 sha256 前 8 位。
    该口径已写进契约 diagnostics.files_hash_policy。
    """
    rows = []
    for root, dirs, names in os.walk(dest):
        dirs.sort()
        for n in sorted(names):
            p = os.path.join(root, n)
            rel = os.path.relpath(p, dest)
            try:
                st = os.stat(p)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                continue
            sha8 = "" if os.path.splitext(n)[1].lower() in VIDEO_EXTS \
                else _sha256(p)[:HASH_HEAD]
            rows.append((rel, size, int(mtime), sha8))
    return sorted(rows)


def _ffmpeg_err_tail(name):
    p = os.path.join(BASE, "logs", "ffmpeg", f"{name}.err.log")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return "\n".join(lines[-FFMPEG_TAIL_LINES:])
    except OSError:
        return ""


def _align_key(dest, stems):
    """picked 场次的 align.json 对账关键字段(缺文件不报错,注明 missing)。"""
    out = {}
    for stem in stems:
        p = os.path.join(dest, ".meta", f"{stem}.align.json")
        try:
            with open(p, encoding="utf-8") as f:
                full = json.load(f)
            out[stem] = {k: full.get(k)
                         for k in ("anchors", "gaps", "drift_ms", "summary",
                                   "finished_at")}
        except Exception:                          # noqa: BLE001
            out[stem] = {"note": "missing"}
    return out


def build_diagnostic(session_key, cfg=None, workdir=None, history_path=None):
    """组诊断包(只落 workdir,不投递)。返回 dict(zip_path/sha256/manifest/...)。

    内容(契约 diagnostics.contents):manifest.json / status.json /
    ffmpeg-err-tail.log / history-entry.json / files.tsv / align-key.json。
    """
    loc = locate_session(session_key, history_path=history_path)
    wd = workdir or tempfile.mkdtemp(prefix="diag-")
    rows = _file_rows(loc["dest"])
    inner = {rel: sha8 for rel, _s, _m, sha8 in rows if sha8}
    manifest = {
        "session_key": str(session_key).strip().strip("/"),
        "resolved": {"name": loc["name"], "date": loc["date"],
                     "dest": loc["dest"], "stems": loc["stems"],
                     "picked": loc["picked"]},
        "contract_version": contract_version(),
        "produced_at": time.time(),
        "producer_host": PRODUCER_HOST,
        "contents": ["manifest.json", "status.json", "ffmpeg-err-tail.log",
                     "history-entry.json", "files.tsv", "align-key.json"],
        "inner_files": inner,
        "notes": [
            "files.tsv 哈希口径:视频分片=大小+mtime 对账(sha8 留空),"
            "sidecar/字幕=sha256 前 8 位",
        ],
    }
    status = _fetch_status_snapshot(cfg)
    err_tail = _ffmpeg_err_tail(loc["name"])

    ts = int(time.time())
    zip_name = f"诊断包-{sanitize(manifest['session_key'])}-{ts}.zip"
    zip_path = os.path.join(wd, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("status.json", json.dumps(status, ensure_ascii=False, indent=2))
        z.writestr("ffmpeg-err-tail.log", err_tail)
        z.writestr("history-entry.json",
                   json.dumps(loc["entries"], ensure_ascii=False, indent=2))
        tsv = "rel\tsize\tmtime\tsha8\n" + "\n".join(
            f"{r[0]}\t{r[1]}\t{r[2]}\t{r[3]}" for r in rows)
        z.writestr("files.tsv", tsv)
        z.writestr("align-key.json",
                   json.dumps(_align_key(loc["dest"], loc["picked"]),
                              ensure_ascii=False, indent=2))
    sha = _sha256(zip_path)
    return {"workdir": wd, "zip_path": zip_path, "zip_name": zip_name,
            "sha256": sha, "manifest": manifest, "loc": loc,
            "manifest_name": zip_name + ".manifest.sha256",
            "manifest_text": f"{sha}  {zip_name}\n"}


def deliver_diagnostic(zip_path, manifest_text, manifest_name, diag_dir,
                       force=False):
    """原子投递到 _对接/诊断包/:.part → rename → 回读 sha256 核对 → 写 manifest。

    返回 (ok, zip_dst 或 已存在说明)。撞名且未 --force → 不覆盖,视为已投递。
    """
    os.makedirs(diag_dir, exist_ok=True)
    dst = os.path.join(diag_dir, os.path.basename(zip_path))
    if os.path.exists(dst):
        if not force:
            return False, f"已存在(未覆盖): {dst}"
        os.remove(dst)
    part = dst + ".part"
    shutil.copyfile(zip_path, part)
    if _sha256(part) != _sha256(zip_path):
        try:
            os.remove(part)
        except OSError:
            pass
        raise OSError("投递校验失败:.part 与源 zip sha256 不一致")
    os.replace(part, dst)
    if _sha256(dst) != _sha256(zip_path):
        raise OSError("投递校验失败:落位后回读 sha256 不一致")
    _atomic_write_text(os.path.join(diag_dir, manifest_name), manifest_text)
    return True, dst


def run_diagnose(session_key, cfg=None, force=False, log=None):
    """CLI 一站式:定位 → 组包 → 投递。返回 (exit_code, 文本报告)。"""
    steps = []
    try:
        built = build_diagnostic(session_key, cfg=cfg)
    except Exception as e:                         # noqa: BLE001
        return 1, f"✗ 组包失败: {e}"
    steps.append(f"✓ 定位: {built['loc']['name']}/{built['loc']['date']}"
                 f" → {built['loc']['dest']}")
    steps.append(f"✓ 组包: {built['zip_name']} "
                 f"({os.path.getsize(built['zip_path']) / 1024:.0f} KB, "
                 f"{len(built['manifest']['inner_files'])} 个 sidecar 哈希)")
    ok, info = nas_paths(cfg or {})
    if not ok:
        return 1, "\n".join(steps + [f"✗ 投递取消: {info}(NAS 未挂载则稍后重试)"])
    try:
        delivered, dst = deliver_diagnostic(
            built["zip_path"], built["manifest_text"],
            built["manifest_name"], info["diag_dir"], force=force)
    except Exception as e:                         # noqa: BLE001
        return 1, "\n".join(steps + [f"✗ 投递失败: {e}"])
    if delivered:
        steps.append(f"✓ 投递: {dst}")
        steps.append(f"✓ 回读校验: sha256 一致,manifest 已写"
                     f"({built['manifest_name']})")
    else:
        steps.append(f"= {info}")
    return 0, "\n".join(steps)


# ── UI 通道探测(只读;/api/interop「项目对接」页数据源) ─────────────────
MAILBOX_CACHE_TTL = 30.0     # SMB 逐 stat 偏慢,与前端轮询同周期进程内缓存
_MAILBOX_CACHE = {"ts": 0.0, "data": None}


def _dir_json_files(dirpath):
    """目录下一层 *.json 绝对路径列表;目录不存在/不可读 → 空表(探测语义)。"""
    try:
        return sorted(os.path.join(dirpath, n) for n in os.listdir(dirpath)
                      if n.endswith(".json")
                      and os.path.isfile(os.path.join(dirpath, n)))
    except OSError:
        return []


def _latest_mtime(paths):
    """路径集合里最新的 mtime;全不可得 → None。"""
    latest = None
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return latest


def mailbox_status(cfg, force=False, now=None):
    """NAS _对接/ 信箱四通道只读探测(「项目对接」页 /api/interop 的数据源)。

    全只读、全兜底:任何一步失败都降级为该通道空值 + ok=False + reason,
    绝不抛异常(调用方是 HTTP 路由,探测挂了不能带挂 UI);不写任何文件、
    不移动任何包 —— 消费动作只发生在 consume_pending(消费协程)里,探测
    与消费互不干扰。进程内缓存 MAILBOX_CACHE_TTL 秒,避免 SMB 逐 stat 被
    前端轮询放大成 NAS 压力。

    返回(各通道子结构允许为 None = 探测不可得):
      ok           NAS 信箱可达
      reason       不可达原因(NAS 未挂载/归档模式非 nas 等)
      contract     契约快照对账: 本地真源 vs NAS 投递副本(sha256+版本)
      ready        就绪信号: 直播回放 <主播>/<日期>.meta.json 计数+最新时间
      feedback     问题反馈: 待处理/已消费计数 + 最旧待处理/最新消费时间
      diagnostic   诊断包: zip 计数 + 最新投递时间(按需 CLI,空≠故障)
    """
    now = time.time() if now is None else now
    cached = _MAILBOX_CACHE["data"]
    if not force and cached is not None and now - _MAILBOX_CACHE["ts"] < MAILBOX_CACHE_TTL:
        return dict(cached, cached=True)

    out = {"ok": False, "reason": None, "nas_root": None,
           "contract": None, "ready": None, "feedback": None,
           "diagnostic": None, "generated_at": now, "cached": False}
    ok, info = nas_paths(cfg)
    if not ok:
        out["reason"] = str(info)
        _MAILBOX_CACHE.update(ts=now, data=out)
        return out
    out["ok"] = True
    out["nas_root"] = info["nas_root"]

    # ① 契约快照:本地真源 vs NAS 投递副本 sha256 对账(两份都是几 KB,哈希廉价)
    c = {"local_version": contract_version(default=""), "local_sha": None,
         "remote_version": None, "remote_sha": None, "match": False,
         "delivered_at": None, "changelog": False}
    try:
        c["local_sha"] = _sha256(os.path.join(CONTRACT_DIR, CONTRACT_FILE))
    except OSError:
        pass
    remote = os.path.join(info["contract_dir"], CONTRACT_FILE)
    try:
        c["remote_sha"] = _sha256(remote)
        c["delivered_at"] = os.path.getmtime(remote)
        c["match"] = bool(c["local_sha"]) and c["remote_sha"] == c["local_sha"]
        with open(remote, encoding="utf-8") as f:
            c["remote_version"] = str((json.load(f) or {}).get("contract_version") or "")
    except (OSError, ValueError):
        pass
    c["changelog"] = os.path.isfile(os.path.join(info["contract_dir"], CHANGELOG_FILE))
    out["contract"] = c

    # ② 就绪信号:<主播>/<日期>.meta.json(归档成功原子写;对端据此判定场次完整)
    try:
        metas = [p for p in glob.glob(os.path.join(info["replay_dir"], "*", "*.meta.json"))
                 if os.path.isfile(p)]
    except OSError:
        metas = []
    out["ready"] = {"count": len(metas), "latest_at": _latest_mtime(metas)}

    # ③ 问题反馈:待处理(信箱根) / 已消费(已处理/)
    pending = _dir_json_files(info["feedback_dir"])
    done = _dir_json_files(info["done_dir"])
    pend_mt = [m for m in (_latest_mtime([p]) for p in pending) if m]
    out["feedback"] = {"pending": len(pending), "done": len(done),
                       "oldest_pending_at": min(pend_mt) if pend_mt else None,
                       "latest_done_at": _latest_mtime(done)}

    # ④ 诊断包:zip + manifest.sha256 成对投递;按需产生,空≠故障
    try:
        zips = [p for p in glob.glob(os.path.join(info["diag_dir"], "*.zip"))
                if os.path.isfile(p)]
    except OSError:
        zips = []
    out["diagnostic"] = {"count": len(zips), "latest_at": _latest_mtime(zips)}

    _MAILBOX_CACHE.update(ts=now, data=out)
    return dict(out)
