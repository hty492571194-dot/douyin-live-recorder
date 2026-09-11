#!/usr/bin/env python3
"""启动自检(preflight):依赖/命令模板/ffmpeg/NAS/Cookie/防休眠。

唯一导出 run_checks(config) -> list[dict],每项结构:
    {id, title, status: "ok"|"warn"|"fail", detail, action}
"""
import os
import re
import shutil
import subprocess


def _which(cmd):
    return shutil.which(cmd)


def find_ffmpeg():
    """定位 ffmpeg 绝对路径:which → 常见 Homebrew 前缀兜底。

    用户可能用非标准 Homebrew prefix(如 /Users/xx/.homebrew),运行时 PATH
    未必含其 bin 目录,shutil.which 会失败。这里兜底扫描常见路径。
    返回绝对路径或 None。
    """
    p = shutil.which("ffmpeg")
    if p:
        return p
    home = os.path.expanduser("~")
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        os.path.join(home, ".homebrew", "bin", "ffmpeg"),
        os.path.join(home, "homebrew", "bin", "ffmpeg"),
        os.path.join(home, "bin", "ffmpeg"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _sleep_check():
    """检测 caffeinate / pmset;解析失败按 ok 处理(不误报)。"""
    ok = {"id": "sleep", "title": "防休眠", "status": "ok", "action": ""}

    # 1) caffeinate 进程是否已在运行
    if _which("pgrep"):
        try:
            r = subprocess.run(["pgrep", "-x", "caffeinate"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return {**ok, "detail": "caffeinate 已在运行"}
        except Exception:
            pass

    # 2) pmset -g 显示系统是否会睡眠
    try:
        r = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5)
        m = re.search(r"\bsleep\s+(\d+)", r.stdout)
        if m:
            if int(m.group(1)) == 0:
                return {**ok, "detail": "系统已设为不睡眠"}
            return {"id": "sleep", "title": "防休眠", "status": "warn",
                    "detail": f"系统 sleep={m.group(1)} 分钟,无人值守可能休眠",
                    "action": "caffeinate -dimsu"}
    except Exception:
        pass

    # 解析失败按 ok,不误报
    return {**ok, "detail": "无法检测休眠设置(已跳过)"}


def run_checks(config):
    """按契约 §3 返回自检结果列表。"""
    checks = []

    # 1. httpx 可导入
    try:
        import httpx  # noqa: F401
        checks.append({"id": "httpx", "title": "httpx 依赖", "status": "ok",
                       "detail": "httpx 已安装", "action": ""})
    except ImportError:
        checks.append({"id": "httpx", "title": "httpx 依赖", "status": "fail",
                       "detail": "无法导入 httpx", "action": "pip install httpx"})

    # 2. 命令模板含绝对路径
    abs_refs = []
    for key in ("on_live_command", "on_offline_command"):
        tmpl = config.get(key, "") or ""
        if re.search(r"(/Users/|/home/)", tmpl):
            abs_refs.append(key)
    if abs_refs:
        checks.append({"id": "abs_path", "title": "命令模板路径", "status": "warn",
                       "detail": f"{', '.join(abs_refs)} 含绝对路径,换机后会失效",
                       "action": "建议改用 {BASE} 占位符"})
    else:
        checks.append({"id": "abs_path", "title": "命令模板路径", "status": "ok",
                       "detail": "命令模板未使用绝对路径", "action": ""})

    # 3. ffmpeg(定位绝对路径,兼容非标准 Homebrew prefix)
    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        checks.append({"id": "ffmpeg", "title": "ffmpeg", "status": "ok",
                       "detail": f"ffmpeg 已就绪({ffmpeg_path})", "action": ""})
    else:
        checks.append({"id": "ffmpeg", "title": "ffmpeg", "status": "warn",
                       "detail": "未找到 ffmpeg", "action": "录制器依赖 ffmpeg,可 brew install ffmpeg"})

    # 4. NAS 是否配置(键缺失视为未配置)
    nas_enabled = bool((config.get("nas") or {}).get("enabled", False))
    if nas_enabled:
        checks.append({"id": "nas", "title": "NAS 归档", "status": "ok",
                       "detail": "NAS 已启用", "action": ""})
    else:
        checks.append({"id": "nas", "title": "NAS 归档", "status": "warn",
                       "detail": "NAS 未配置,下播素材不会自动归档", "action": "点击前往 NAS 归档页配置"})

    # 5. Cookie(支持钥匙串登录态,配置里只存引用)
    det = config.get("detection") or {}
    cookie = det.get("cookie", "")
    try:
        from auth import resolve_cookie as _resolve_cookie
        cookie = _resolve_cookie(det) or cookie
    except Exception:
        pass
    if cookie:
        checks.append({"id": "cookie", "title": "API 通道 Cookie", "status": "ok",
                       "detail": "登录态可用(存于钥匙串,不落配置文件)", "action": ""})
    else:
        checks.append({"id": "cookie", "title": "API 通道 Cookie", "status": "warn",
                       "detail": "Cookie 为空,API 确认通道休眠,mix 退化为 302",
                       "action": "在「高级参数 → 抖音登录」扫码登录"})

    # 6. 防休眠
    checks.append(_sleep_check())

    # 7. 归档目标(启动轻量检查 NAS 挂载 + 外接硬盘,不主动 mount_smbfs)
    archive = config.get("archive") or {}
    nas = config.get("nas") or {}
    has_nas = bool(nas.get("enabled", False))
    has_ext = bool((archive.get("external_dir") or "").strip())
    if not has_nas and not has_ext:
        checks.append({"id": "archive", "title": "归档目标", "status": "warn",
                       "detail": "未配置归档目标(NAS/外接硬盘),下播素材仅保留在本机",
                       "action": "前往归档设置页配置"})
    else:
        mode = archive.get("mode", "auto") or "auto"
        nas_ok = ext_ok = False
        try:
            import nas as nas_mod
            if has_nas:
                mp = nas_mod._mount_point(nas)
                nas_ok = bool(mp) and nas_mod._is_mounted(mp) and nas_mod._probe_writable(mp)
            ext_dir = os.path.expanduser((archive.get("external_dir") or "").strip())
            ext_ok = bool(ext_dir) and os.path.isdir(ext_dir) and nas_mod._probe_writable(ext_dir)
        except Exception:
            pass
        ready = (mode == "nas" and nas_ok) or (mode == "external" and ext_ok) or \
                (mode == "auto" and (nas_ok or ext_ok))
        if ready:
            checks.append({"id": "archive", "title": "归档目标", "status": "ok",
                           "detail": f"归档就绪(mode={mode})", "action": ""})
        else:
            checks.append({"id": "archive", "title": "归档目标", "status": "fail",
                           "detail": "归档目标不可用:NAS 未挂载或外接硬盘未连接",
                           "action": "检查连接,或在归档设置页切换目标"})

    return checks


if __name__ == "__main__":
    import json
    print(json.dumps(run_checks({}), ensure_ascii=False, indent=2))
