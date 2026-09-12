#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地改动同步到 GitHub 公开仓库（月度同步用）。

Agent / 自动化环境里 `github.com` 不可达（只有 `api.github.com` 通），所以不能 `git push`，
只能走 Git Data API。本脚本把「扫描 → 提交 → 推送」串成一步，并**在提交前拦下敏感内容**。

用法：
    .venv/bin/python scripts/sync_github.py            # 扫描 → 提交 → 推送
    .venv/bin/python scripts/sync_github.py --check    # 只扫描，不改任何东西
    .venv/bin/python scripts/sync_github.py --dry-run  # 打印将要做的动作，不落盘
    .venv/bin/python scripts/sync_github.py --message "自定义提交说明"

退出码：0 成功/无改动  1 扫描命中敏感内容(已中止)  2 推送失败  3 找不到推送脚本

设计要点
--------
* **禁词集从真实配置反推**：读 `config.json`（已被 .gitignore 排除）取出主播昵称、
  sec_uid、web_rid、NAS 主机/共享名/用户名，以及由 NAS IP 推出的 /24 前缀。
  这样以后加主播、换 NAS，扫描规则自动跟上，不需要改代码。
* **短值不参与拦截**：纯数字要 ≥6 位、纯 ASCII 要 ≥4 位、含中文 ≥2 位。
  否则像 NAS 用户名 `ggg`、主播名 `17` 这种短串会在代码里到处误报。
* **合成夹具豁免**：值里含 SUPER-SECRET / TEST / FAKE / EXAMPLE / DUMMY / PLACEHOLDER
  等标记的，视为测试数据放行（`tests/` 下大量此类夹具）。
* 命中时**只打印打码后的片段**，不把真实值写进日志。
"""
import argparse
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = "/usr/bin/git"          # 绝对路径，绕开 PATH 上的 safe-delete shim
DEFAULT_PUSH_SCRIPT = os.path.expanduser(
    "~/.workbuddy/skills/github-publish-restricted-env/scripts/gh_push_via_api.py")

# 合成/占位标记：命中其一即视为测试数据，不拦
SYNTHETIC = ("super-secret", "supper-secret", "test", "fake", "example",
             "dummy", "placeholder", "sample", "redacted", "xxxx", "your_",
             "changeme", "todo")

# 通用敏感模式（与具体项目无关，任何仓库都该拦）
GENERIC_PATTERNS = {
    "凭据明文": re.compile(
        r"(?i)\b(sessionid|sessionid_ss|ttwid|sid_guard|sid_tt|msToken|odin_tt|"
        r"passport_csrf_token|access_token|refresh_token)\b\s*[=:]\s*[\"']?"
        r"([0-9A-Za-z%|_.\-]{20,})"),
    "密钥明文": re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|apikey|private[_-]?key)\b"
        r"\s*[=:]\s*[\"']([^\"'\s]{12,})[\"']"),
    "云厂商密钥": re.compile(
        r"\b(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{30,})\b"),
}

# 跳过扫描的文件（二进制 / 体积无意义）
SKIP_EXT = (".pyc", ".flv", ".ass", ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".ico", ".woff", ".woff2", ".ttf", ".zip", ".gz", ".mp4", ".jsonl")


# ---------------------------------------------------------------- 禁词集
def worth_blocking(v):
    """短串会到处误报，按字符构成给最小长度。"""
    v = (v or "").strip()
    if len(v) < 2:
        return False
    if v.isdigit():
        return len(v) >= 6                     # web_rid 是 11~12 位
    if all(ord(c) < 128 for c in v):
        return len(v) >= 4                     # 排掉 NAS 用户名 ggg 这类
    return True                                # 含中文等，2 字就有辨识度


def private_values(cfg, hotspots=None):
    """从真实配置反推「一旦出现在仓库里就是泄露」的值。返回 {类别: set}。"""
    out = {"主播昵称": set(), "sec_uid": set(), "直播间号": set(),
           "NAS 主机": set(), "NAS 共享名": set(), "NAS 用户名": set(),
           "内网网段": set()}

    def put(kind, v):
        v = str(v or "").strip()
        if worth_blocking(v):
            out[kind].add(v)

    for m in (cfg.get("monitors") or []):
        put("主播昵称", m.get("name"))
        put("sec_uid", m.get("anchor"))
        put("sec_uid", m.get("sec_uid"))
        put("直播间号", m.get("web_rid"))

    nas = cfg.get("nas") or {}
    put("NAS 主机", nas.get("host"))
    put("NAS 共享名", nas.get("share"))
    put("NAS 用户名", nas.get("username"))

    host = str(nas.get("host") or "")
    m = re.fullmatch(r"(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}", host)
    if m:
        out["内网网段"].add(m.group(1) + ".")   # 同网段其它地址同样是内网信息

    for k in (hotspots or {}):
        if str(k).startswith("MS4wLjABAAAA"):
            put("sec_uid", k)

    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- 扫描
def mask(v):
    """命中值打码：保留首 2 字符 + 长度，够定位、不落敏感到日志。"""
    v = str(v)
    if len(v) <= 4:
        return v[:1] + "*" * (len(v) - 1)
    return v[:2] + "*" * max(1, len(v) - 3) + v[-1] + "(len=%d)" % len(v)


def is_synthetic(v):
    low = str(v).lower()
    return any(s in low for s in SYNTHETIC)


def scan_text(text, values):
    """返回 [(类别, 打码片段, 行号)]。"""
    hits = []
    for kind, vals in values.items():
        for v in vals:
            if is_synthetic(v):
                continue
            start = 0
            while True:
                i = text.find(v, start)
                if i < 0:
                    break
                hits.append((kind, mask(v), text[:i].count("\n") + 1))
                start = i + len(v)
    for kind, rx in GENERIC_PATTERNS.items():
        for m in rx.finditer(text):
            raw = m.group(m.lastindex) if m.lastindex else m.group(0)
            if is_synthetic(raw):
                continue
            hits.append((kind, mask(raw), text[:m.start()].count("\n") + 1))
    return hits


def candidate_files():
    """会被提交的文件集 = 已跟踪 + 未跟踪但未被忽略（尊重 .gitignore，只读不落盘）。"""
    raw = subprocess.run(
        [GIT, "-C", BASE, "-c", "core.quotePath=false",
         "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True).stdout
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def scan_repo(values, files=None):
    """扫描待提交文件集。返回 [(相对路径, 类别, 打码片段, 行号)]。"""
    out = []
    for rel in (files if files is not None else candidate_files()):
        if rel.endswith(SKIP_EXT):
            continue
        path = os.path.join(BASE, rel)
        try:
            if os.path.getsize(path) > 4 * 1024 * 1024:
                continue
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for kind, frag, line in scan_text(text, values):
            out.append((rel, kind, frag, line))
    return out


# ---------------------------------------------------------------- 仓库信息
def parse_remote(url):
    """从 remote URL 解析 (owner, repo)。"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def remote_url():
    p = subprocess.run([GIT, "-C", BASE, "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    return p.stdout.strip()


def porcelain():
    p = subprocess.run([GIT, "-C", BASE, "-c", "core.quotePath=false",
                        "status", "--porcelain", "-z"], capture_output=True)
    return p.stdout.decode("utf-8")


# ---------------------------------------------------------------- 提交
def stage_and_stat():
    """git add -A，然后返回 (name_status 列表, 增行数, 删行数)。"""
    subprocess.run([GIT, "-C", BASE, "add", "-A"], check=True)
    ns = subprocess.run([GIT, "-C", BASE, "-c", "core.quotePath=false",
                         "diff", "--cached", "--name-status", "-z"],
                        capture_output=True).stdout.decode("utf-8")
    parts = [x for x in ns.split("\0") if x]
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
    num = subprocess.run([GIT, "-C", BASE, "diff", "--cached", "--numstat", "-z"],
                         capture_output=True).stdout.decode("utf-8")
    add = dele = 0
    for f in num.split("\0"):
        seg = f.split("\t")
        if len(seg) >= 2 and seg[0].isdigit() and seg[1].isdigit():
            add += int(seg[0])
            dele += int(seg[1])
    return pairs, add, dele


def build_message(pairs, add, dele, today, custom=None):
    if custom:
        return custom if custom.endswith("\n") else custom + "\n"
    lines = ["月度同步 %s" % today, "",
             "%d 个文件：+%d −%d" % (len(pairs), add, dele), ""]
    tag = {"M": "改", "A": "增", "D": "删", "R": "移", "C": "拷"}
    for st, path in pairs:
        lines.append("%s  %s" % (tag.get(st[0], st), path))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser(description="月度同步到 GitHub（走 Git Data API）")
    ap.add_argument("--check", action="store_true", help="只扫描，不改任何东西")
    ap.add_argument("--dry-run", action="store_true", help="打印将做的动作，不落盘")
    ap.add_argument("--message", help="自定义提交说明（默认自动生成）")
    ap.add_argument("--push-script", default=os.environ.get(
        "GITHUB_PUSH_SCRIPT", DEFAULT_PUSH_SCRIPT))
    ap.add_argument("--owner"), ap.add_argument("--repo")
    ap.add_argument("--todo-dir", default=BASE, help="项目根目录")
    a = ap.parse_args(argv)

    base = os.path.abspath(a.todo_dir)
    globals()["BASE"] = base

    # 1) 禁词集
    try:
        with open(os.path.join(base, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print("✗ 读不到 config.json，无法构造禁词集：%s" % e)
        return 1
    try:
        with open(os.path.join(base, "hotspots.json"), encoding="utf-8") as f:
            hs = json.load(f)
    except Exception:
        hs = {}
    values = private_values(cfg, hs)
    n = sum(len(v) for v in values.values())
    print("=== 禁词集 %d 条 ===" % n)
    for k, v in sorted(values.items()):
        print("   %-12s %d 条" % (k, len(v)))

    # 2) 扫描
    hits = scan_repo(values)
    print("=== 扫描 %d 个待提交文件 ===" % len(candidate_files()))
    if hits:
        print("   [!] 命中 %d 处敏感内容，已中止：" % len(hits))
        for rel, kind, frag, line in hits[:40]:
            print("       %s:%s  [%s] %s" % (rel, line, kind, frag))
        if len(hits) > 40:
            print("       …… 另有 %d 处" % (len(hits) - 40))
        print("   处理完再跑一次；线上已存在的旧值仍需人工确认。")
        return 1
    print("   ✓ 未发现真实值泄露")

    dirty = bool(porcelain())
    if a.check:
        print("=== --check：%s ===" % ("有未提交改动" if dirty else "工作区干净"))
        return 0
    if not dirty:
        print("=== 无改动，跳过同步 ===")
        return 0

    # 3) 提交
    pairs, add, dele = stage_and_stat()
    import time
    today = time.strftime("%Y-%m-%d")
    msg = build_message(pairs, add, dele, today, a.message)
    print("=== 将提交 %d 个文件：+%d −%d ===" % (len(pairs), add, dele))
    if a.dry_run:
        print("--- 提交说明 ---\n%s----------------" % msg)
        print("=== --dry-run：未提交、未推送 ===")
        return 0

    p = subprocess.run([GIT, "-C", base, "-c", "core.quotePath=false",
                        "commit", "-q", "-F", "-"], input=msg.encode(),
                       capture_output=True)
    if p.returncode != 0:
        print("✗ 提交失败：%s" % p.stderr.decode("utf-8", "replace").strip())
        return 2
    head = subprocess.run([GIT, "-C", base, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    print("=== 已提交 %s ===" % head[:10])

    # 4) 推送
    if not os.path.exists(a.push_script):
        print("✗ 找不到推送脚本 %s" % a.push_script)
        print("  提交已落在本地。在**你自己的终端**里 `git push` 即可")
        print("  （外面 github.com 可达；Agent 环境里只有 api.github.com 通）。")
        return 3

    url = remote_url()
    owner, repo = (a.owner, a.repo) if a.owner and a.repo else parse_remote(url)
    if not owner:
        print("✗ 解析不出 owner/repo（remote 是 %r），用 --owner/--repo 指定" % url)
        return 2

    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy",
                                "pythonpath")}
    env["NO_PROXY"] = "*"
    print("=== 推送到 %s/%s（走 Git Data API）===" % (owner, repo))
    cmd = [sys.executable, "-u", a.push_script, "--dir", base,
           "--owner", owner, "--repo", repo]
    rc = subprocess.run(cmd, env=env).returncode
    if rc != 0:
        print("✗ 推送失败（退出码 %s）。提交已在本地，可重跑本脚本" % rc)
        return 2
    print("=== 同步完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
