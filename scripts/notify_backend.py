#!/usr/bin/env python3
"""产物变更通告(S5,唯一入口):写 NOTICE.md → 投递契约三件套到 NAS。

为什么存在:契约快照(S1)解决「契约内容怎么过去」,本脚本解决「什么时候必须
过去、并留下一条人能读的变更记录」。契约 v1.1.0 起 producer_duties 段声明:
凡调整产出产物的形态/语义(files/naming/timestamps/capabilities/diagnostics
任一),调整的同时必须跑本脚本 —— 让对端(转录端 asrclip)能及时同步。

用法:
    python scripts/notify_backend.py "分片时长调整为 2400s" \
        --type 产物 --detail "影响: complete_when 与文件大小预估;契约 v1.1.1 已同步"

流程(一条命令完成「告知后端」全链路):
  ① append_notice(): 条目置顶写入本地 contract/NOTICE.md(原子写)
  ② sync_contract.sync(): 把 contract.json + CHANGELOG.md + NOTICE.md
     原子投递到 NAS _对接/contract/,逐文件 sha256 回读校验
  ③ 打印后端可见路径与建议动作
任何一步失败都报错退出(人工/CI 触发,不静默、不重试);NAS 未挂载先恢复再跑。
"""
import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import interop                      # noqa: E402
from sync_contract import sync      # noqa: E402  同目录脚本,投递逻辑唯一实现


def run_notice(summary, kind="产物", detail="", cfg=None, do_sync=True):
    """核心流程(ctl 的 douyin notice 复用):写通告 → 投递 → 报告。

    返回 (exit_code, 报告文本)。cfg 为 None 时读 config.json。
    """
    from sync_contract import check_only   # noqa: F401  (文档性引用:回读口径同源)
    cfg = cfg if cfg is not None else interop.load_cfg()
    lines = []
    ok, where = interop.append_notice(kind, summary, detail)
    if not ok:
        return 1, f"✗ {where}"
    lines.append(f"✓ 通告已写入: {where}")
    lines.append(f"  条目: [{kind}] {summary}")
    ver = interop.contract_version(default="未版本化")
    lines.append(f"  契约版本: v{ver}(若本次调整改了契约声明,确认版本已升、CHANGELOG 已写)")
    if not do_sync:
        lines.append("= 未投递(do_sync=False):契约三件套仍在本地真源,后端暂不可见")
        return 0, "\n".join(lines)
    rc = sync(cfg)
    if rc != 0:
        lines.append("✗ 投递失败:通告与契约仍在本地;NAS 恢复挂载后重跑本命令即可"
                     "(NOTICE.md 本地已含本条,重跑不会产生重复条目以外的副作用)")
        return 1, "\n".join(lines)
    lines.append("✓ 已投递:后端在 _对接/contract/ 读 NOTICE.md 顶部条目即为本次变更")
    lines.append("  建议:若影响消费逻辑,同步在录制端↔转录端周报(周日 21:00)里口头确认一次")
    return 0, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="产物变更通告:写 NOTICE.md 并投递契约三件套到 NAS(告知后端)")
    ap.add_argument("summary", help="变更一句话摘要(将作为通告标题)")
    ap.add_argument("--type", dest="kind", default="产物",
                    choices=interop.NOTICE_KINDS, help="变更类型(默认 产物)")
    ap.add_argument("--detail", default="",
                    help="补充说明(可多行;影响范围/迁移建议/生效版本等)")
    ap.add_argument("--no-sync", dest="do_sync", action="store_false",
                    help="只写本地通告不投递(极少用;默认必投递)")
    args = ap.parse_args()
    rc, report = run_notice(args.summary, kind=args.kind,
                            detail=args.detail, do_sync=args.do_sync)
    print(report)
    return rc


if __name__ == "__main__":
    sys.exit(main())
