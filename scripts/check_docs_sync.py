#!/usr/bin/env python3
"""文档同步检查 —— 防止「代码改了、文档没改」。

背景：项目根目录有 6 份 md 文档（README、AI_AGENT_GUIDE、PROJECT_GUIDE、
项目框架说明书、项目整理与迁移方案 …）。它们记录了项目根路径、端口、模块清单
这些**会随代码变化**的事实。历史教训是迁移改了路径、服务改了判活逻辑之后，
文档仍停留在旧版本，接手的人（或另一个智能体）读到的是错的。

本脚本把「文档里写的关键事实」与「代码/配置的真实状态」做一次比对，
任何不一致都会报出来。已纳入单元测试（tests/test_docs_sync.py），
所以**改完代码跑一次全套测试就能发现文档漂移**。

用法：
    .venv/bin/python scripts/check_docs_sync.py          # 检查,有问题退出码 1
    .venv/bin/python scripts/check_docs_sync.py --list   # 顺便列出被检查的文档

设计原则：只报**确定的**不一致（路径、端口、文件存在性），不做语义判断，
避免误报把人训练成无视告警。
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 迁移前的旧目录名。除「记录历史」的文档外,任何文档里都不该再出现。
OLD_ROOT_MARK = "douyin-monitor-phase-d"
DOCS_ALLOWING_HISTORY = {"项目整理与迁移方案.md"}

# 这几份文档必须存在,缺失说明文档体系本身不完整
REQUIRED_DOCS = (
    "README.md",
    "AI_AGENT_GUIDE.md",
    "PROJECT_GUIDE.md",
    "项目框架说明书.md",
    "项目整理与迁移方案.md",
)

# 声明「项目根」的文档:里面写的路径必须等于真实 BASE
ROOT_DECLARING_DOCS = ("项目框架说明书.md", "AI_AGENT_GUIDE.md")

# 顶层 .py 中不需要写进说明书的(入口/临时/生成物)
MODULE_IGNORE = {"migrate.py", "setup.py"}

# 文档里描述接口模板时的占位写法(/api/xxx、/api/<主播>、/api/{id}),不当真实接口核对
EP_PLACEHOLDER = re.compile(r"(^|/)(x{3,}|y{3,}|z{3,}|foo|bar|baz|whatever"
                            r"|[<{*]|\.\.\.)", re.I)


def _read(rel):
    path = os.path.join(BASE, rel)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _docs():
    return sorted(f for f in os.listdir(BASE) if f.endswith(".md"))


def check_all():
    """返回问题列表;空列表表示文档与代码一致。"""
    issues = []

    # ① 必需文档齐全
    for d in REQUIRED_DOCS:
        if _read(d) is None:
            issues.append("缺少文档: %s" % d)

    # ② 旧项目根路径残留(历史记录类文档/历史说明行除外)
    history_ctx = ("迁移历史", "旧路径", "旧根", "历史记录", "原名")
    for f in _docs():
        if f in DOCS_ALLOWING_HISTORY:
            continue
        for lineno, line in enumerate((_read(f) or "").splitlines(), 1):
            if OLD_ROOT_MARK not in line:
                continue
            if any(k in line for k in history_ctx):
                continue  # 明确标注为历史的引用,允许保留
            issues.append(
                "%s:%d: 仍在引用迁移前的旧项目根(%s…),应改为 %s"
                % (f, lineno, OLD_ROOT_MARK, BASE))

    # ③ 声明的项目根必须等于真实 BASE
    for f in ROOT_DECLARING_DOCS:
        txt = _read(f)
        if txt is None:
            continue
        m = re.search(r"项目根[`]?\s*[:：]\s*`?([^\s`）)?\"，,]+)", txt)
        if not m:
            issues.append("%s: 未声明项目根路径(需写成「项目根：`/abs/path`」)" % f)
            continue
        declared = os.path.normpath(m.group(1).strip("`"))
        if declared != os.path.normpath(BASE):
            issues.append("%s: 声明的项目根是 %s,实际是 %s" % (f, declared, BASE))

    # ④ 文档里写的 Web 端口必须与 config.json 一致
    try:
        cfg = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
        port = int((cfg.get("webui") or {}).get("port"))
    except Exception:
        port = None
    if port:
        for f in _docs():
            txt = _read(f) or ""
            for p in set(re.findall(r"127\.0\.0\.1:(\d{4,5})", txt)):
                if int(p) != port:
                    issues.append("%s: 写了端口 %s,config.json 里是 %s" % (f, p, port))

    # ⑤ 说明书必须登记所有顶层模块(新增 .py 忘了写文档会在这里暴露)
    guide = _read("项目框架说明书.md")
    if guide:
        for f in sorted(os.listdir(BASE)):
            if not f.endswith(".py") or f in MODULE_IGNORE:
                continue
            if not os.path.isfile(os.path.join(BASE, f)):
                continue
            if f not in guide:
                issues.append("项目框架说明书.md: 未登记模块 %s" % f)

        # ⑥ 说明书里写到的模块文件必须真实存在(可能在 scripts/ tests/ 子目录下)
        for name in sorted(set(re.findall(r"`([A-Za-z0-9_/]+\.py)`", guide))):
            if os.path.exists(os.path.join(BASE, name)):
                continue
            if any(os.path.exists(os.path.join(BASE, d, os.path.basename(name)))
                   for d in ("scripts", "tests", "recorder")):
                continue
            issues.append("项目框架说明书.md: 登记了不存在的模块 %s" % name)

    # ⑦ 说明书里写到的 Web 接口必须真实存在(文档不能凭空写一个接口)
    #
    # 只查「文档 → 代码」这一个方向:说明书里出现的 `/api/xxx` 必须在 webui.py 的
    # 路由里能找到。反方向(代码新增了接口但文档没写)不查 —— 说明书是速查性质、
    # 并未逐一登记全部 POST 路由,反向硬查会把大量「故意没写」报成问题,
    # 误报多了这个脚本就会被无视,那才是真的失效。
    if guide:
        code = _read("webui.py") or ""
        # 路径里连字符是常态(/api/open-folder、/api/rec-start),字符类里必须带上,
        # 否则会被截成 /api/open 而误判成「存在」
        _EP = r"(/api/[A-Za-z0-9_/.<>{}\*-]+)"
        routes = set(re.findall(r'path == "%s"' % _EP, code))
        routes |= set(re.findall(r'path\.startswith\("%s"' % _EP, code))
        for endpoint in sorted(set(re.findall(_EP, guide))):
            # 写模板时顺手用的占位符(如「说明书里写到的 /api/xxx」),不是真接口
            if EP_PLACEHOLDER.search(endpoint):
                continue
            # 带占位符/以斜杠结尾的是前缀描述(`/api/previews/`),按前缀比对
            base = endpoint.rstrip("/")
            if any(r.rstrip("/") == base or r.rstrip("/").startswith(base)
                   for r in routes):
                continue
            if any(endpoint.startswith(r) for r in routes if r.endswith("/")):
                continue
            issues.append("项目框架说明书.md: 写了不存在的接口 %s" % endpoint)

    return issues


def main():
    args = sys.argv[1:]
    issues = check_all()
    if "--list" in args:
        print("项目根: %s" % BASE)
        print("被检查的文档:")
        for f in _docs():
            print("  - %s" % f)
    if issues:
        print("发现 %d 处文档与代码不一致:" % len(issues))
        for i in issues:
            print("  ✗ %s" % i)
        print("\n修复方式: 更新对应文档后重跑本脚本。")
        return 1
    print("✓ 文档与代码一致(%d 份文档已检查)" % len(_docs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
