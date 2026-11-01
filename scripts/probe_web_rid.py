#!/usr/bin/env python3
"""一次性抓取 web_rid 的调试 CLI(不依赖服务)。

用途:上线前后单独验证抓取器 —— 服务里那条巡检链路出问题时,先用它确认
「浏览器这一层还能不能拿到 rid」,避免在浏览器、调度、写回三层之间瞎猜。

用法:
    .venv/bin/python scripts/probe_web_rid.py <sec_uid | 抖音号 | 主页链接>
    .venv/bin/python scripts/probe_web_rid.py --json 主播甲_sec_uid
    .venv/bin/python scripts/probe_web_rid.py --no-verify <sec_uid>

输出示例:
    verdict=live  rid=388770149407  reason=-  elapsed=1400ms
    detail: bytes=1231088 xhr=200 room=7700706410765568809 http=200 owner=same(landed=MS4w…)
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def resolve_sec(text):
    """把 sec_uid / 抖音号 / 分享链接统一成 sec_uid。"""
    t = (text or "").strip()
    if t.startswith("MS4wLjAB"):
        return t, None
    import link_resolver
    r = link_resolver.resolve(t)
    if not r.get("ok"):
        return None, r.get("error") or "无法解析"
    return r.get("sec_uid"), None


def main():
    ap = argparse.ArgumentParser(description="抓取某主播的 web_rid(调试验证用)")
    ap.add_argument("who", help="sec_uid / 抖音号 / 主页分享链接")
    ap.add_argument("--timeout", type=int, default=45, help="页面预算秒数(默认 45)")
    ap.add_argument("--no-verify", action="store_true", help="跳过 rid 归属校验")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    a = ap.parse_args()

    sec, err = resolve_sec(a.who)
    if not sec:
        print(f"解析失败:{err}", file=sys.stderr)
        return 2

    import web_rid_browser
    res = web_rid_browser.fetch_web_rid(sec, timeout_sec=a.timeout, verify=not a.no_verify)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"sec_uid: {sec}")
        print(f"verdict={res['verdict']}  rid={res.get('rid') or '-'}  "
              f"reason={res.get('reason') or '-'}  elapsed={res['elapsed_ms']}ms")
        print(f"detail: {res.get('detail', '')}")
    return 0 if res["verdict"] != "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
