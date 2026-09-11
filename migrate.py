# -*- coding: utf-8 -*-
"""项目搬家后的路径重写工具。

把整个项目目录搬到别处(或拷到另一台 Mac)后,`recordings/history.json` 里
355 条记录存的仍是**旧目录的绝对路径**,`config.json` 里也可能有指向旧位置的
输出目录。不修的话:录制历史页会全部显示"文件缺失",点开也找不到文件。

用法
----
    # 先试算,只看会改多少条、不落盘
    python migrate.py --from /旧/项目根 --dry-run

    # 确认无误后真正重写(自动备份原文件为 *.bak-migrate)
    python migrate.py --from /旧/项目根 --apply

    # 不写 --from 时,会从 history.json 的 output_path 里推断旧根目录
    python migrate.py --apply

    # 反向:把新路径改回旧路径(搬回去时用)
    python migrate.py --from /新/项目根 --to /旧/项目根 --apply

设计约束
--------
* **只改以旧根开头的路径**:NAS / 外接硬盘的归档目标(`archive.dest`)不受影响,
  它们本来就不在项目目录里。
* **默认 dry-run**,必须显式 `--apply` 才落盘。
* 落盘前把原文件另存 `*.bak-migrate`,可随时回滚。
* 幂等:重复跑,第二次没有任何匹配,输出"无需改动"。
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(BASE, "recordings", "history.json")
CONFIG = os.path.join(BASE, "config.json")

# 需要扫描重写的目标文件
TARGETS = [
    ("recordings/history.json", HISTORY),
    ("config.json", CONFIG),
]

# history.json 里录的都是 <root>/recordings/<主播>/<文件>,据此可反推旧根目录
_MARKERS = ("/recordings/", "/previews/", "/logs/", "/spool/")


def _infer_old_root(history):
    """从 history 的 output_path 里推断旧项目根目录。"""
    for e in (history or []):
        p = e.get("output_path") or ""
        for mk in _MARKERS:
            i = p.find(mk)
            if i > 0:
                return p[:i]
    return None


def _walk(obj, old, new, hit):
    """递归遍历 JSON 结构,把字符串里的 old 前缀替换成 new。"""
    if isinstance(obj, str):
        if obj.startswith(old):
            hit.append(obj)
            if obj == old:
                return new
            return new + obj[len(old):]
        return obj
    if isinstance(obj, list):
        return [_walk(x, old, new, hit) for x in obj]
    if isinstance(obj, dict):
        return {k: _walk(v, old, new, hit) for k, v in obj.items()}
    return obj


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path, data, backup=True):
    if backup and os.path.exists(path):
        bak = path + ".bak-migrate"
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="项目搬家后的路径重写")
    ap.add_argument("--from", dest="old", default=None,
                    help="旧项目根目录(省略则自动从 history.json 推断)")
    ap.add_argument("--to", dest="new", default=None,
                    help="新项目根目录(默认 = 本脚本所在目录)")
    ap.add_argument("--apply", action="store_true", help="真正写入(默认只试算)")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式声明只试算(默认行为,写出来更好读)")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak-migrate 备份")
    args = ap.parse_args()

    new = os.path.abspath(args.new or BASE)
    old = args.old

    if not os.path.exists(HISTORY):
        print("找不到 recordings/history.json,请先确认新目录拷贝完整")
        return 1

    history = _load(HISTORY)
    if not old:
        old = _infer_old_root(history)
        if not old:
            print("推断不出旧根目录,请用 --from 显式指定")
            return 1
        print(f"推断出旧根目录: {old}")

    old = old.rstrip("/")
    new = new.rstrip("/")
    if old == new:
        print("旧根目录与新根目录相同,无需迁移")
        return 0

    print(f"重写规则: {old}  ->  {new}")
    print(f"模式: {'落盘(--apply)' if args.apply else '试算(dry-run)'}")
    print("-" * 60)

    total = 0
    for label, path in TARGETS:
        if not os.path.exists(path):
            print(f"  {label}: 不存在,跳过")
            continue
        try:
            data = _load(path)
        except (OSError, ValueError) as e:
            print(f"  {label}: 读取失败 {e}")
            continue
        hit = []
        newdata = _walk(data, old, new, hit)
        uniq = sorted(set(hit))
        total += len(uniq)
        if not uniq:
            print(f"  {label}: 无需改动")
            continue
        print(f"  {label}: {len(uniq)} 条路径将被重写")
        for s in uniq[:3]:
            print(f"      {s}")
        if len(uniq) > 3:
            print(f"      … 其余 {len(uniq) - 3} 条")
        if args.apply:
            _save(path, newdata, backup=not args.no_backup)
            print(f"      -> 已写入(原文件备份为 {os.path.basename(path)}.bak-migrate)")

    print("-" * 60)
    if total == 0:
        print("没有需要重写的路径。")
    elif args.apply:
        print(f"完成,共重写 {total} 条路径。")
    else:
        print(f"试算完成,共 {total} 条路径待重写。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
