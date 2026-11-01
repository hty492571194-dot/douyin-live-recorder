#!/usr/bin/env python3
"""NAS 挂载管理与归档队列。

模块导入面(契约 §5):ensure_mount / get_password / nas_watch / enqueue / queue_status。
另提供 set_password / test_connection 供 webui 的 /api/nas 与 /api/nas/test 使用(实现细节)。
"""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

import health
import interop as interop_mod   # 跨项目对接层(S2:归档落位后兜底补 session.json;全旁路)

# 测试钩子:True 时 ensure_mount / test_connection 用本地目录模拟,不触发真实挂载
DRY_RUN = False

BASE = os.path.dirname(os.path.abspath(__file__))
SPOOL_DIR = os.path.join(BASE, "spool")
PENDING_PATH = os.path.join(SPOOL_DIR, "pending.json")
CONFIG_PATH = os.path.join(BASE, "config.json")
KEYCHAIN_SERVICE = "douyin-nas"
MAX_RETRIES = 5

_qlock = threading.Lock()
_pending = []        # [{id, streamer, src_path, added_at, retries, status, ...}]
_last_archive = None  # 最近一次成功归档的时间戳

# 归档结果回调:由 monitor 注入,用于把归档状态回写到录制历史
# 签名: hook(src_path, state, dest, target, error)
_archive_hook = None


def set_archive_hook(fn):
    """注入归档结果回调(monitor 调用),避免 nas → monitor 的循环 import。"""
    global _archive_hook
    _archive_hook = fn


def _notify_archive(src_path, state, dest=None, target=None, error=None):
    """通知归档状态变化(成功/失败/开始搬运)。异常不影响归档主流程。"""
    if not _archive_hook:
        return
    try:
        _archive_hook(src_path, state, dest, target, error)
    except Exception as e:
        _log(f"[归档] 状态回写失败: {e}")


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _atomic_write(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


# ── 钥匙串 ──
# 钥匙串不可用(无钥匙串环境/CI/授权被拒)时的内存降级密码,重启后丢失
_mem_password = ""


def get_password():
    """从钥匙串读取 NAS 密码;失败回退内存密码。"""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return _mem_password


def set_password(password):
    """写入钥匙串(注意 -a 参数必需);失败降级为内存密码并告警。返回是否成功。"""
    global _mem_password
    if not password:
        return True
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", "default",
             "-s", KEYCHAIN_SERVICE, "-w", password],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    # 钥匙串不可用(无钥匙串环境/CI/授权被拒):降级内存密码并告警
    _mem_password = password
    _log("[警告] 钥匙串写入失败,NAS 密码仅保存在内存(重启后需重新填写)")
    return True


# ── 挂载 ──
def _probe_writable(path):
    """在挂载点写探针文件,验证可写。只以「写入成功」为准。

    删除失败一律忽略(权限、占用、安全软件拦截都可能让它失败)。这一点很关键:
    上层 ensure_mount 一旦判定不可写,就会走「僵尸挂载」分支 umount -f 把 NAS
    强行卸载 —— 若只因一个 5 字节探针删不掉就卸掉正在归档的卷,代价完全不对等。
    探针残留也会被下次覆盖写,不影响后续判定。
    """
    probe = os.path.join(path, ".douyin-probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
    except OSError as e:
        # ENOSPC(28)= 盘满。目录本身**是可写的**(权限没问题),只是没空间了 ——
        # 这里必须判「可写」:否则盘满会被误报成「目标离线/未挂载」,用户根本
        # 看不出真正原因是空间不够(2026-09-18 就是这么被绕进去的)。
        # 空间够不够交给 _process_one 的搬前预检判断。
        if getattr(e, "errno", None) == 28:
            return True
        return False
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def _is_mounted(mount_point):
    """通过 mount 输出判断挂载点是否已挂载。"""
    try:
        r = subprocess.run(["mount"], capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            if f" on {mount_point}" in line:
                return True
    except Exception:
        pass
    return False


def clean_host(host):
    """规范化主机地址:去掉 smb:// 或 cifs:// 前缀与首尾斜杠(用户常整串粘贴)。"""
    h = (host or "").strip()
    low = h.lower()
    for p in ("smb://", "cifs://"):
        if low.startswith(p):
            h = h[len(p):]
            break
    return h.strip("/")


def clean_share(share):
    """规范化共享名:去掉首尾斜杠与空白。"""
    return (share or "").strip().strip("/")


def _mount_point(nas_cfg):
    """返回展开 ~ 后的挂载点。挂载点必须在用户可写目录(/Volumes 需 root,会失败)。"""
    mp = nas_cfg.get("mount_point", "") or ""
    return os.path.expanduser(mp) if mp else mp


def _smb_url(host, share, port, username, password):
    """构造 mount_smbfs 的 URL;用户名/密码/共享名 percent-encode 以兼容特殊字符。"""
    hp = f"{host}:{port}" if port and int(port) != 445 else host
    u = quote(username, safe="") if username else ""
    p = quote(password, safe="") if password else ""
    sh = quote(share, safe="")
    if u and p:
        return f"//{u}:{p}@{hp}/{sh}"
    if u:
        return f"//{u}@{hp}/{sh}"
    return f"//{hp}/{sh}"


def _mask(text, password):
    """把错误信息里的明文/编码密码替换为 ***,避免密码泄露到前端或日志。"""
    if not password or not text:
        return text
    return text.replace(quote(password, safe=""), "***").replace(password, "***")


def _do_mount(host, share, port, username, password, mount_point):
    """挂载 SMB,返回 (ok, error)。密码经 URL 传递(已 percent-encode)。
    残余风险:密码会短暂出现在进程列表(单用户 Mac 影响可控)。"""
    url = _smb_url(host, share, port, username, password)
    try:
        r = subprocess.run(["mount_smbfs", url, mount_point],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0, _mask((r.stderr or "").strip(), password)
    except Exception as e:
        return False, _mask(str(e), password)


def ensure_mount(cfg):
    """确保 NAS 已挂载且可写,返回 bool。DRY_RUN 下用本地目录模拟。"""
    nas = cfg.get("nas") or {}
    if not nas.get("enabled", False):
        return False
    mount_point = _mount_point(nas)

    if DRY_RUN:
        # 模拟:把 mount_point 当本地目录,可写即视为挂载成功
        if not mount_point:
            return False
        try:
            os.makedirs(mount_point, exist_ok=True)
            return _probe_writable(mount_point)
        except OSError:
            return False

    # 已挂载且可写
    if _is_mounted(mount_point) and _probe_writable(mount_point):
        return True

    # 僵尸挂载:挂载点存在但探针失败 → umount -f 后重挂
    if _is_mounted(mount_point):
        try:
            subprocess.run(["umount", "-f", mount_point], capture_output=True, timeout=15)
        except Exception:
            pass

    # 未挂载 → mkdir + mount_smbfs
    try:
        os.makedirs(mount_point, exist_ok=True)
    except OSError:
        return False

    host = clean_host(nas.get("host", ""))
    share = clean_share(nas.get("share", ""))
    if not host or not share:
        return False
    ok, _ = _do_mount(host, share, nas.get("port", 445),
                      nas.get("username", ""), get_password(), mount_point)
    if not ok and nas.get("auto_discover", True) \
            and not _host_alive(host, nas.get("port", 445)):
        # 挂载失败且主机不可达 → 多半是 DHCP 换址,自动识别新 IP 后重挂
        new_ip = auto_recover_host(cfg)
        if new_ip:
            ok, _ = _do_mount(new_ip, share, nas.get("port", 445),
                              nas.get("username", ""), get_password(), mount_point)
    return ok and _probe_writable(mount_point)


def resolve_target(cfg):
    """解析当前生效的归档目标根目录,返回 (ok, target_dir, mode)。

    mode 取值:
      - "nas": 仅 NAS(需 enabled 且 ensure_mount 通过)
      - "external": 仅外接硬盘(需 external_dir 存在且可写)
      - "auto": NAS 优先,失败降级外接硬盘;两者都不可用返回 (False, None, "auto")
    向后兼容:无 archive 键的旧 config 等价于纯 NAS 行为。
    """
    archive = cfg.get("archive") or {}
    mode = archive.get("mode", "auto") or "auto"
    nas_cfg = cfg.get("nas") or {}

    ext_dir = os.path.expanduser((archive.get("external_dir") or "").strip())
    ext_ready = bool(ext_dir) and os.path.isdir(ext_dir) and _probe_writable(ext_dir)

    # 仅外接硬盘:**完全不碰 NAS**。历史实现在这里也会 ensure_mount(),
    # 若 NAS 恰好不可达(换网段/关机),每轮都会触发 auto_recover_host 全网段
    # 自动识别(10~30s)才返回 —— 明明外接盘可用,归档轮询却被白白拖慢一半。
    # 选了 external 就表示不用 NAS,这里必须短路。
    if mode == "external":
        return (True, ext_dir, "external") if ext_ready else (False, None, "external")

    nas_ready = False
    if nas_cfg.get("enabled", False):
        nas_ready = ensure_mount(cfg)

    if mode == "nas":
        return (True, _mount_point(nas_cfg), "nas") if nas_ready else (False, None, "nas")
    if mode == "external":
        return (True, ext_dir, "external") if ext_ready else (False, None, "external")
    # auto:NAS 优先,失败降级外接硬盘
    if nas_ready:
        return True, _mount_point(nas_cfg), "nas"
    if ext_ready:
        return True, ext_dir, "external"
    return False, None, "auto"


# ── 目录选择框(macOS 原生) ──
PICK_TIMEOUT = 180          # 等用户点选的秒数(挂盘、翻目录都可能很慢)


def pick_directory(prompt="选择外接硬盘上的归档目录", timeout=PICK_TIMEOUT):
    """弹出 macOS 原生目录选择框,返回 (path|None, error|None)。

    为什么必须由服务端弹:浏览器的 <input type=file webkitdirectory> 出于安全
    **不暴露真实路径**,拿不到 `/Volumes/xxx`,所以只能起原生对话框。

    失败模式都要能优雅降级(让用户在文本框里手填),绝不能 500:
      - 用户点「取消」       → 退出码非 0,stderr 含 -128
      - 服务不在 GUI session → osascript 直接报错(如以 root 跑、ssh 起的服务)
      - 一直没点             → subprocess 超时
    弹窗期间本函数会阻塞,调用方必须放到线程/executor 里。
    """
    p = str(prompt or "选择目录").replace("\\", "\\\\").replace('"', '\\"')
    script = ('tell me to activate\n'
              'set f to choose folder with prompt "%s"\n'
              'return POSIX path of f' % p)
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "选择超时(%d 秒内未选择)" % timeout
    except FileNotFoundError:
        return None, "本机没有 osascript(该能力仅 macOS 可用)"
    except Exception as e:                      # noqa: BLE001
        return None, str(e)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        low = err.lower()
        if "-128" in err or "canceled" in low or "cancelled" in low:
            return None, "已取消"
        line = err.splitlines()[-1] if err else ""
        return None, line or "无法弹出选择框(服务可能不在图形会话中)"
    path = (r.stdout or "").strip()
    if not path:
        return None, "未返回路径"
    return path, None


def test_connection(nas_cfg):
    """用提交参数临时挂载→写探针→读回→卸载,返回 {ok, latency_ms, error}。DRY_RUN 下模拟。"""
    nas_cfg = nas_cfg or {}
    host = clean_host(nas_cfg.get("host", ""))
    share = clean_share(nas_cfg.get("share", ""))
    if not host or not share:
        return {"ok": False, "latency_ms": None, "error": "host/share 不能为空"}
    if "/" in share:
        return {"ok": False, "latency_ms": None,
                "error": "share 不能包含 /,子目录请填到「归档根目录」(root_dir)"}
    if DRY_RUN:
        return {"ok": True, "latency_ms": 1, "error": None}

    # 该共享已挂载:macOS 不允许同一共享重复挂载(会报 File exists),
    # 此时直接在已挂载点做写入探针,避免误导性报错。
    if _share_mounted(host, share):
        start0 = time.time()
        mp = _mount_point(nas_cfg) if nas_cfg.get("mount_point") else None
        probe_dir = mp if (mp and os.path.isdir(mp)) else None
        if not probe_dir:
            return {"ok": False, "latency_ms": None,
                    "error": f"共享已挂载,但挂载点不可用: {mp or '(未配置 mount_point)'}"}
        if _probe_writable(probe_dir):
            return {"ok": True, "latency_ms": int((time.time() - start0) * 1000),
                    "error": None, "note": "该共享已挂载,直接验证可写"}
        return {"ok": False, "latency_ms": None,
                "error": f"已挂载但写入失败: {probe_dir} 不可写(权限/只读/卷已满)"}

    import tempfile
    tmp = tempfile.mkdtemp(prefix="douyin-nas-test-")
    username = nas_cfg.get("username", "")
    password = nas_cfg.get("password", "") or get_password()
    start = time.time()
    try:
        ok, err = _do_mount(host, share, nas_cfg.get("port", 445), username, password, tmp)
        if not ok:
            return {"ok": False, "latency_ms": None, "error": err or "挂载失败"}
        # 只写 + 读回验证,不删探针:该临时挂载随即会被 umount,残留无意义;
        # 而 SMB 上 os.remove 可能因 NAS 回收站/文件占用/安全软件失败(详见 _probe_writable)
        probe = os.path.join(tmp, ".douyin-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        with open(probe, "r", encoding="utf-8") as f:
            if f.read() != "probe":
                return {"ok": False, "latency_ms": None, "error": "探针写入后读回不一致"}
        return {"ok": True, "latency_ms": int((time.time() - start) * 1000), "error": None}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": str(e)}
    finally:
        try:
            subprocess.run(["umount", "-f", tmp], capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


# ── 共享名探测 ──
def _parse_shares(stdout):
    """解析 smbutil view 输出中的共享名列表。"""
    shares = []
    lines = stdout.splitlines()
    type_col = -1
    started = False
    for line in lines:
        if not started:
            if "Type" in line:
                type_col = line.find("Type")
            elif line.strip().startswith("---"):
                started = True
            continue
        s = line.strip()
        if not s or "shares listed" in s.lower():
            continue
        if type_col > 0 and len(line) > type_col:
            name = line[:type_col].strip()
        else:
            name = s.split()[0] if s.split() else ""
        if name and name.lower() != "share":
            shares.append(name)
    return shares


def list_shares(host, username="", password=""):
    """列出主机上的 SMB 共享名(供前端「检测共享」下拉)。返回 (ok, shares, error)。"""
    host = clean_host(host)
    if not host:
        return False, [], "host 不能为空"
    if DRY_RUN:
        return True, ["share1", "share2"], None
    try:
        if username or password:
            url = f"//{quote(username, safe='')}:{quote(password, safe='')}@{host}"
        else:
            url = f"//{host}"  # 匿名/游客
        r = subprocess.run(["smbutil", "view", url], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False, [], _mask((r.stderr or "列出共享失败").strip(), password)
        return True, _parse_shares(r.stdout), None
    except Exception as e:
        return False, [], _mask(str(e), password)


# ── NAS IP 自动识别(防 DHCP 换址导致归档静默失效) ──────────────
_last_host_check = 0.0  # 上次主机可达性预检时间(进程内)


def _host_alive(host, port=445, timeout=1.5):
    """TCP 探测 SMB 端口可达性。"""
    try:
        with socket.create_connection((host, int(port) if port else 445), timeout=timeout):
            return True
    except OSError:
        return False


def _local_net_prefix():
    """本机所在 /24 网段前缀(如 '192.168.1');失败返回 None。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 不实际发包,仅取路由源地址
        ip = s.getsockname()[0]
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except OSError:
        pass
    finally:
        s.close()
    return None


def _smb_fingerprint(ip, nas_cfg):
    """用凭据列出共享,检查配置的 share 是否存在 —— 确认这就是配置中那台 NAS。"""
    user = nas_cfg.get("username", "")
    pw = get_password()
    if user or pw:
        url = f"//{quote(user, safe='')}:{quote(pw, safe='')}@{ip}"
    else:
        url = f"//{ip}"
    try:
        r = subprocess.run(["smbutil", "view", url],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return clean_share(nas_cfg.get("share", "")) in _parse_shares(r.stdout)
    except Exception:
        pass
    return False


def discover_nas_host(cfg):
    """扫描本机 /24 网段,对开放 445 的主机做共享名指纹验证,返回 NAS 当前 IP 或 None。

    指纹 = 用现有凭据能列出共享,且列表里包含 config 配置的 share 名,
    避免把局域网内其他开了文件共享的设备误认为 NAS。"""
    nas_cfg = cfg.get("nas") or {}
    prefix = _local_net_prefix()
    if not prefix:
        _log("[NAS] 无法确定本机网段,跳过自动识别")
        return None
    current = clean_host(nas_cfg.get("host", ""))
    open_hosts = []
    lock = threading.Lock()

    def probe(i):
        ip = f"{prefix}.{i}"
        if ip == current:
            return
        if _host_alive(ip):
            with lock:
                open_hosts.append(ip)

    threads = []
    for i in range(1, 255):
        t = threading.Thread(target=probe, args=(i,), daemon=True)
        t.start()
        threads.append(t)
        if len(threads) >= 64:  # 分批,避免瞬时 254 线程
            for x in threads:
                x.join()
            threads = []
    for x in threads:
        x.join()

    for ip in sorted(open_hosts):
        if _smb_fingerprint(ip, nas_cfg):
            _log(f"[NAS] 指纹匹配成功: {ip} 提供共享「{nas_cfg.get('share', '')}」")
            return ip
    return None


def _share_mounted(host, share):
    """该 SMB 共享是否已被挂载(macOS 不允许同一共享重复挂载,会报 File exists)。"""
    try:
        r = subprocess.run(["mount"], capture_output=True, text=True, timeout=15)
        host_k = clean_host(host)
        share_k = clean_share(share)
        enc = quote(share_k, safe="")
        for line in r.stdout.splitlines():
            if host_k in line and (share_k in line or enc in line):
                return True
    except Exception:
        pass
    return False


def persist_host(new_host):
    """把发现的新 host 原子写回 config.json(monitor 每轮热重取,下轮即生效)。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if (cfg.get("nas") or {}).get("host") == new_host:
            return True
        cfg.setdefault("nas", {})["host"] = new_host
        _atomic_write(CONFIG_PATH, cfg)
        _log(f"[NAS] 主机地址已自动更新: {new_host}")
        return True
    except Exception as e:
        _log(f"[NAS] 自动更新 host 失败: {e}")
        return False


def auto_recover_host(cfg):
    """主机不可达时的自动恢复入口:扫描 → 指纹验证 → 更新 config。返回新 IP 或 None。"""
    old = clean_host((cfg.get("nas") or {}).get("host", ""))
    _log(f"[NAS] 主机 {old} 不可达,开始全网段自动识别(约 10-30s)...")
    new_ip = discover_nas_host(cfg)
    if new_ip and persist_host(new_ip):
        return new_ip
    if not new_ip:
        _log("[NAS] 自动识别未找到 NAS(可能离线/换了网段),稍后重试")
    return None


# ── 归档队列 ──
def _load_pending():
    global _pending
    items = []
    if os.path.exists(PENDING_PATH):
        try:
            with open(PENDING_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, json.JSONDecodeError):
            items = []
    # 启动清理:failed 条目(不再重试,残留只会虚增失败计数)与
    # 旧格式条目(src=录制根目录)直接丢弃,避免失败计数越滚越大。
    valid = []
    for e in items:
        if e.get("status") == "failed":
            continue
        if os.path.basename(os.path.normpath(e.get("src_path", ""))) == "recordings":
            continue
        valid.append(e)
    _pending = valid
    if len(valid) != len(items):
        _log(f"[归档队列] 启动清理: 丢弃无效条目 {len(items) - len(valid)} 个,保留 {len(valid)} 个")
        _save_pending()  # 必须落盘,否则重启后无效条目又回来


def _save_pending():
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
    except OSError:
        pass
    _atomic_write(PENDING_PATH, _pending)


def enqueue(entry):
    """入队待归档条目。entry: {streamer, src_path, room_id?, start?, ...}。"""
    with _qlock:
        item = {
            "id": uuid.uuid4().hex,
            "streamer": entry.get("streamer", "?"),
            "src_path": entry.get("src_path", ""),
            "added_at": time.time(),
            "retries": 0,
            "status": "pending",
        }
        for k in ("room_id", "start", "end", "date"):
            if k in entry:
                item[k] = entry[k]
        _pending.append(item)
        _save_pending()


# ── 补归档:把历史里漏搬的场次重新入队 ──
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SESSION_STAMP_RE = re.compile(r"-(\d{4})(\d{2})(\d{2})-\d{6}")


def _session_dir(entry):
    """从 history 条目推导「场次日期目录」;推导不出返回 None。

    铁律:**绝不能退化成 `<主播>/` 这一层**。那是该主播所有场次的父目录,
    一旦当 src 入队就会把整个主播的素材一起搬走(老格式条目正有这个风险)。
    """
    p = os.path.expanduser(entry.get("output_path") or "")
    if not p:
        return None
    d = os.path.dirname(p)
    if not d:
        return None
    if _DATE_DIR_RE.match(os.path.basename(d)):
        return d
    # 老格式:output_path 无日期层,从文件名 <主播>-20260828-044846 里取日期
    m = _SESSION_STAMP_RE.search(os.path.basename(p))
    if m:
        cand = os.path.join(d, "-".join(m.groups()))
        return cand if os.path.isdir(cand) else None
    return None


def backfill_from_history(cfg, history_path=None):
    """把「已录完、未归档、本机文件还在」的场次重新入队,返回入队条数。

    为什么需要它:归档目标不可用时(NAS 关机 / 外接盘拔着),下播入队的条目的确
    会留在 pending 里等目标恢复 —— 但**历史上目标长期不可用时**,这些条目要么
    重试耗尽被标 failed、要么压根没入队(启动时队列从 spool 恢复但 history 里
    的 `archive.state` 仍是 none)。目标一恢复,这些素材不会自己跑过去。

    安全边界:
      - 只认**日期层目录**(`<主播>/<YYYY-MM-DD>/`),推导不出就跳过;
      - 目录必须存在且非空;
      - **正在录制的场次目录跳过**(否则会把 ffmpeg 正在写的文件搬走并删掉);
      - 已在 pending 里(非 failed)的同 src 不重复入队 ⇒ 可反复调用,幂等。
    """
    if history_path is None:
        # history.json **恒定**落在项目元数据目录,与录制暂存根无关。
        # 2026-10-30 暂存外迁后这里曾按 output_dir 拼路径 —— 用户一改暂存目录,
        # 补归档就彻底读不到历史(表现成「点了补归档,入队 0 条」,毫无报错)。
        history_path = os.path.join(BASE, "recordings", "history.json")
    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
    except (OSError, ValueError) as e:
        _log("[补归档] 读 history 失败: %s" % e)
        return 0
    if not isinstance(history, list):
        return 0

    with _qlock:
        known = {e.get("src_path") for e in _pending
                 if e.get("status") != "failed"}

    added = 0
    for e in history:
        if not isinstance(e, dict):
            continue
        if e.get("status") != "done":
            continue                                   # 仍在录/异常结束
        if ((e.get("archive") or {}).get("state")) == "archived":
            continue                                   # 已归档
        src = _session_dir(e)
        if not src or not os.path.isdir(src):
            continue
        if is_recording_path(src):
            continue                                   # 正在录:等录完再补
        try:
            if not os.listdir(src):
                continue                               # 空目录,没东西可搬
        except OSError:
            continue
        if src in known:
            continue
        known.add(src)
        name = e.get("name") or os.path.basename(os.path.dirname(src)) or "?"
        enqueue({"streamer": name, "src_path": src,
                 "start": e.get("started_at"), "end": e.get("ended_at"),
                 "date": os.path.basename(src)})
        try:
            if _archive_hook:
                _archive_hook(src, "pending")     # 回写 history:未归档 → 待归档
        except Exception:                         # noqa: BLE001
            pass
        added += 1
    if added:
        _log("[补归档] 目标恢复,重新入队 %d 个未归档场次" % added)
    return added


# ── 目标卷空间 / 传输进度 ──
SPACE_RESERVE_BYTES = 512 * 1024 * 1024   # 预留余量:别把目标盘顶到 0 字节

_plock = threading.Lock()      # 进度用**独立**锁:_process_one 是在 _qlock 内被调用的,
_progress = None              # 在那里再抢 _qlock 会死锁(threading.Lock 不可重入)


def _free_bytes(path):
    """目标卷剩余字节;拿不到返回 None(不因此阻断归档)。"""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _human(n):
    """字节数转可读字符串(日志/前端提示用)。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fTB" % n


def _free_at(path):
    """目标路径所在卷的剩余字节:路径还不存在时向上找第一个存在的父目录。"""
    p = path
    while p:
        if os.path.isdir(p):
            return _free_bytes(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return _free_bytes(path)


def _usage_at(path):
    """目标卷容量:{total, used, free, used_percent};拿不到返回 None。

    外接硬盘要看「占用了多少」、NAS 要看「还剩多少」——两个数都在这儿,
    展示成什么口径由前端按目标类型决定。
    """
    p = path
    while p:
        if os.path.isdir(p):
            try:
                total, used, free = shutil.disk_usage(p)
            except OSError:
                return None
            return {"total": total, "used": used, "free": free,
                    "used_percent": round(used * 100.0 / total, 1) if total else 0.0}
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def _is_space_error(e):
    """是不是「盘满」造成的失败(ENOSPC=28)。这类失败不该消耗重试次数,应等空间。"""
    return isinstance(e, OSError) and getattr(e, "errno", None) == 28


def _dir_size(path):
    """目录/文件的总字节数(读不到的大小忽略,不抛)。"""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root_, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root_, f))
            except OSError:
                pass
    return total


def _scan_missing(src, dest_dir):
    """对比源与目标,返回 (未传文件相对路径列表, 未传字节数)。

    判据:**目标不存在同名文件,或大小不一致**(上一次复制被中断)。
    这是「盘满中断后还剩什么没搬」的唯一真相来源 —— 目录级搬运不是原子的,
    中断时目标里会留下「部分文件」,光看 history 状态是看不出来的。
    """
    missing, total = [], 0
    if not os.path.isdir(src):
        return missing, total
    for root_, _dirs, files in os.walk(src):
        rel_dir = os.path.relpath(root_, src)
        for f in files:
            s = os.path.join(root_, f)
            d = os.path.join(dest_dir, f) if rel_dir == "." \
                else os.path.join(dest_dir, rel_dir, f)
            try:
                ssize = os.path.getsize(s)
            except OSError:
                continue
            same = False
            try:
                same = os.path.exists(d) and os.path.getsize(d) == ssize
            except OSError:
                same = False
            if not same:
                missing.append(os.path.relpath(s, src))
                total += ssize
    return missing, total


def _progress_begin(entry, src, dest_dir, total_bytes, total_files, resumed=0):
    global _progress
    with _plock:
        _progress = {
            "streamer": entry.get("streamer", "?"),
            "src": src, "dest": dest_dir,
            "total_bytes": total_bytes, "done_bytes": 0,
            "total_files": total_files, "done_files": resumed,
            "file": "", "resumed_files": resumed,
            "started_at": time.time(), "updated_at": time.time(),
            "speed_bps": 0.0, "eta_s": None, "error": None,
        }


def _progress_tick(name, size):
    """记一个文件传完。speed/eta 用累计值算,够准且不用额外计时器。"""
    with _plock:
        if not _progress:
            return
        _progress["done_bytes"] += size
        _progress["done_files"] += 1
        _progress["file"] = name
        now = time.time()
        _progress["updated_at"] = now
        elapsed = now - _progress["started_at"]
        if elapsed > 0.5:
            sp = _progress["done_bytes"] / elapsed
            _progress["speed_bps"] = sp
            left = _progress["total_bytes"] - _progress["done_bytes"]
            _progress["eta_s"] = int(left / sp) if sp > 0 else None


def _progress_end(error=None):
    global _progress
    with _plock:
        if _progress is not None:
            _progress["error"] = error
            _progress["updated_at"] = time.time()
        _progress = None


def progress_status():
    """当前搬运进度快照(无搬运时 None);附带 percent(0~100,已 clamp)。"""
    with _plock:
        if not _progress:
            return None
        p = dict(_progress)
    total = p.get("total_bytes") or 0
    done = p.get("done_bytes") or 0
    if total > 0:
        p["percent"] = round(min(100.0, done * 100.0 / total), 1)
    else:
        p["percent"] = 100.0 if done else 0.0
    return p


def queue_status():
    """返回 {depth, failed, waiting_space, deferred, last_archive, progress}。"""
    with _qlock:
        depth = sum(1 for e in _pending if e.get("status") not in ("failed",))
        failed = sum(1 for e in _pending if e.get("status") == "failed")
        waiting = sum(1 for e in _pending if e.get("status") == "waiting_space")
        deferred = sum(1 for e in _pending if e.get("status") == "deferred")
        base = {"depth": depth, "failed": failed, "waiting_space": waiting,
                "deferred": deferred, "last_archive": _last_archive}
    base["progress"] = progress_status()
    return base


def _write_meta(dest, entry, end_ts):
    meta = {
        "streamer": entry.get("streamer", ""),
        "room_id": entry.get("room_id", ""),
        "start": entry.get("start") or entry.get("added_at"),
        "end": end_ts,
    }
    _atomic_write(dest + ".meta.json", meta)


def _archive_date(entry):
    """归档日期层(YYYY-MM-DD,场次归属日):显式 date > start > 入队时间。"""
    d = str(entry.get("date") or "").strip()
    if d:
        return d
    ts = entry.get("start") or entry.get("added_at")
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return time.strftime("%Y-%m-%d")


# ── 正在录制的场次目录(由 monitor 注入探针) ──
# 铁律:**正在录制的场次目录绝不能搬走**。
# 归档单元是「日期目录」,而日期目录里可能正躺着 ffmpeg 正在写的 .flv;
# 一旦整目录搬完就会 rmtree(源目录),ffmpeg 的写句柄随即指向一个已删除的
# inode —— 数据全写进「幽灵文件」,进程退出时永久丢失,而目标盘上只留一个
# 被截断的 .flv。2026-09-18 真实发生过(见 _process_one 头部注释)。
_active_probe = None
_write_grace_sec = 120      # 源文件最近被写过 ⇒ 视为仍在写入(探针失效时的兜底判据)


def set_active_probe(fn):
    """注入「正在录制的场次目录」提供者(返回可迭代的绝对路径)。"""
    global _active_probe
    _active_probe = fn


def active_dirs():
    """当前正在录制的场次目录集合(规范化绝对路径);探针缺失/异常时返回空集。"""
    if _active_probe is None:
        return set()
    try:
        return {os.path.normpath(str(p)) for p in (_active_probe() or ()) if p}
    except Exception:                              # noqa: BLE001
        return set()


def is_recording_path(path):
    """path 自身、或其中任一项正在被录制 ⇒ 不可归档。"""
    if not path:
        return False
    p = os.path.normpath(path)
    for a in active_dirs():
        if a == p or a.startswith(p + os.sep):
            return True
    return False


def _recently_written(path, within=None):
    """文件最近是否被写过(探针拿不到时的兜底判据)。"""
    try:
        return (time.time() - os.path.getmtime(path)) < (within or _write_grace_sec)
    except OSError:
        return False


class _Deferred(Exception):
    """「暂不归档」信号:源仍在被写入(正在录制)。

    与「失败」严格区分:不消耗重试次数、不回退 history 状态、更不会 rmtree 源目录。
    下一轮轮询自动重试,录完自然就搬走了。
    """


_MEDIA_EXTS = (".flv", ".mp4", ".ts", ".mkv", ".mov")


def _dest_has_media(dest_dir):
    """目标目录里是否已有真正的媒体文件。

    只认媒体扩展名 —— 免得把一个只建了目录、或只写了 .meta 的空壳当成归档成功。
    exFAT 上会伴随生成 `._xxx` AppleDouble 文件,一并跳过。
    """
    try:
        for n in os.listdir(dest_dir):
            if n.startswith("._"):
                continue
            if n.lower().endswith(_MEDIA_EXTS):
                return True
    except OSError:
        return False
    return False


def _process_one(entry, nas_cfg, target_dir=None, mode=None):
    """搬运单个条目到归档目标。返回 ("done"|"retry"|"failed"|"wait_space"|"defer", error)。

    target_dir 为生效归档根目录(mount_point 或 external_dir);为空时回退 nas 挂载点。
    目录层级:<target>/<root_dir>/<主播>/<YYYY-MM-DD>/<文件> —— NAS/外置硬盘共用此逻辑。

    三条安全边界(2026-09-18 加,起因是外接盘写满导致大量半搬状态):
      ① **搬前预检空间**:只算「还需要传多少」(扣除目标已有的完整副本),
         不够就直接返回 wait_space —— 不开口就不会有半成品。wait_space 不算失败,
         不消耗重试次数,等空间腾出来下一轮自动继续。
      ② **断点续传**:目标已有同名同大小文件视为已传,跳过重传(盘满中断后重试很快)。
      ③ **失败回退状态**:搬挂了要把 history 从 archiving 退回 pending 并写上真实原因,
         不能让「归档中」无限挂着;同时记下「还剩哪些文件没传」。
      ④ **正在录制的场次绝不搬**(defer):归档单元是日期目录,里面可能正躺着
         ffmpeg 在写的 .flv。整目录搬完会 rmtree 源目录 ⇒ ffmpeg 的句柄指向
         已删除的 inode,数据写进「幽灵文件」永久丢失,目标盘只剩截断的 .flv。
         所以:入口探测 + 复制后、rmtree 前再探测 + 文件长大即视为仍在写入。
    """
    src = entry.get("src_path", "")
    if not src:
        return "failed", "条目缺少 src_path"
    # 防御:旧版条目 src 指向录制根目录(recordings),会误搬全部主播,直接标失败
    if os.path.basename(os.path.normpath(src)) == "recordings":
        return "failed", "旧版错误条目(src=录制根目录),已拦截"
    mp = target_dir if target_dir else _mount_point(nas_cfg)
    root = nas_cfg.get("root_dir", "直播回放")
    streamer = entry.get("streamer", "?")
    dest_dir = os.path.join(mp, root, streamer, _archive_date(entry))
    if not os.path.exists(src):
        # 源已消失。历史上这里返回 retry → 重试 5 次后被标 failed,并用「源不存在」
        # **覆写**掉本该是 archived 的 history 状态(素材其实已在目标盘上),
        # 于是历史页显示「归档失败」而磁盘上文件齐全。改为:
        #   目标已有媒体 ⇒ 认作已完成(顺手把 history 状态校正回 archived);
        #   目标也没有   ⇒ 永久失败,不再无限重试。
        if _dest_has_media(dest_dir):
            _notify_archive(src, "archived", dest_dir, mode, None)
            return "done", ""
        return "failed", "源已不存在,且目标无对应文件"
    # ★ 安全闸:正在录制的场次绝不能碰(见 _active_probe 段注释)
    if is_recording_path(src):
        return "defer", "该场次正在录制,暂不归档(录完自动继续)"
    fname = os.path.basename(os.path.normpath(src)) or "recording"
    is_dir = os.path.isdir(src)
    _set_missing(entry, None, None)          # 先清掉上一轮的残留标记

    # ① 空间预检:只算还需要传的部分
    if is_dir:
        missing, need = _scan_missing(src, dest_dir)
    else:
        d0 = os.path.join(dest_dir, fname)
        try:
            same = os.path.exists(d0) and os.path.getsize(d0) == os.path.getsize(src)
        except OSError:
            same = False
        missing, need = ([], 0) if same else ([fname], os.path.getsize(src))
    free = _free_at(dest_dir if os.path.isdir(dest_dir) else mp)
    if need and free is not None and need + SPACE_RESERVE_BYTES > free:
        _set_missing(entry, len(missing), need)
        return "wait_space", ("目标剩余空间不足:还需 %s,可用 %s"
                              % (_human(need), _human(free)))

    part = None
    try:
        if is_dir:
            _notify_archive(src, "archiving", dest_dir, mode, None)
            # 目录(场次日期目录):把**内容**搬进 <target>/<root>/<主播>/<date>/,
            # 不再嵌套一层同名目录;逐项复制校验,全部成功后删除源目录。
            os.makedirs(dest_dir, exist_ok=True)
            _progress_begin(entry, src, dest_dir, need, len(missing))
            for item in sorted(os.listdir(src)):
                s = os.path.join(src, item)
                d = os.path.join(dest_dir, item)
                part = d + ".part"
                if os.path.isdir(s):
                    if os.path.exists(part):
                        shutil.rmtree(part, ignore_errors=True)
                    shutil.copytree(s, part)
                    if os.path.exists(d):
                        shutil.rmtree(d, ignore_errors=True)
                    os.rename(part, d)
                    _progress_tick(item + "/", _dir_size(s))
                else:
                    size = os.path.getsize(s)
                    # ② 续传:目标已有完整副本(大小一致)就不重传
                    try:
                        if os.path.exists(d) and os.path.getsize(d) == size:
                            continue
                    except OSError:
                        pass
                    shutil.copy2(s, part)
                    if os.path.getsize(part) != size:
                        # 复制期间源长大了 ⇒ 该文件还在被写。这不是错误:
                        # 既不该算失败重试,更不能继续往下走到 rmtree ——
                        # 那会把正在录制的文件删掉(数据写进幽灵文件,永久丢失)。
                        if is_recording_path(src) or _recently_written(s):
                            raise _Deferred(f"{item} 仍在写入,暂不归档")
                        raise OSError(f"复制后大小不一致: {item}")
                    if os.path.exists(d):
                        os.remove(d)
                    os.rename(part, d)
                    _progress_tick(item, size)
            # ★ 最后一道闸:搬运期间该场次又开始录了 ⇒ 绝不能 rmtree。
            # 已复制的文件留在目标(下一轮按「同名同大小」跳过),源目录原样保留。
            if is_recording_path(src):
                raise _Deferred("本场次在搬运期间开始录制,已中止搬运")
            shutil.rmtree(src, ignore_errors=True)
            _write_meta(dest_dir, entry, time.time())
            # S2 契约版本随数据走:日期目录兜底补 finalized session.json。
            # 正常场次由 monitor 收尾写好、随档复制而来;老场次/异常缺失在这里
            # 按目录实况补写。旁路:失败只记日志,绝不影响归档结果。
            try:
                interop_mod.ensure_session_state_finalized(dest_dir)
            except Exception as e:
                _log(f"[对接] session.json 兜底写入失败(不影响归档): {e}")
            _progress_end()
            _notify_archive(src, "archived", dest_dir, mode, None)
            return "done", ""
        dest = os.path.join(dest_dir, fname)
        part = dest + ".part"
        os.makedirs(dest_dir, exist_ok=True)
        _notify_archive(src, "archiving", dest_dir, mode, None)
        _progress_begin(entry, src, dest_dir, need, 1)
        # 单文件:copy2 到 .part → 校验大小 → rename
        shutil.copy2(src, part)
        if os.path.getsize(part) != os.path.getsize(src):
            if is_recording_path(os.path.dirname(src)) or _recently_written(src):
                raise _Deferred("源文件仍在写入,暂不归档")
            raise OSError("复制后大小不一致")
        os.rename(part, dest)
        _progress_tick(fname, os.path.getsize(dest))
        _write_meta(dest, entry, time.time())
        os.remove(src)
        _progress_end()
        _notify_archive(src, "archived", dest_dir, mode, None)
        return "done", ""
    except Exception as e:
        # 清掉半成品 .part,别让它冒充已传文件(part 可能未赋值:见上一步预检)
        try:
            if part and os.path.isdir(part):
                shutil.rmtree(part, ignore_errors=True)
            elif part and os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        err = str(e) or e.__class__.__name__
        # 暂不归档(源仍在写入):不改 history 状态、不记缺失、不算失败
        if isinstance(e, _Deferred):
            _progress_end(err)
            return "defer", err
        # ③ 记下「还剩哪些没传」+ 把 history 从 archiving 退回 pending
        try:
            if is_dir:
                miss, need2 = _scan_missing(src, dest_dir)
                _set_missing(entry, len(miss), need2)
        except OSError:
            pass
        _progress_end(err)
        _notify_archive(src, "pending", dest_dir, mode, err)
        if _is_space_error(e):
            return "wait_space", ("目标空间不足,已传 %s 后中断:%s"
                                  % (_human(entry.get("missing_bytes") or 0), err))
        return "retry", err


def _set_missing(entry, count, nbytes):
    """记录「本轮还剩多少没传」(0/None 表示清空)。"""
    if count:
        entry["missing_files"] = count
        entry["missing_bytes"] = nbytes
    else:
        entry.pop("missing_files", None)
        entry.pop("missing_bytes", None)


def process_queue(cfg, target_dir=None, mode="auto"):
    """目标健康时消化队列:逐个搬运 pending 条目,失败重试,≥5 次标 failed。

    通过 resolve_target 决定当前生效目标(NAS / 外接硬盘 / auto 降级);
    目标不可用时直接返回,条目留在队列等待下次(指数退避)。
    target_dir 已由调用方解析时直接传入,避免重复 ensure_mount(mount_smbfs)。
    """
    global _pending, _last_archive
    nas_cfg = cfg.get("nas") or {}
    if target_dir is None:
        ok, target_dir, mode = resolve_target(cfg)
        if not ok:
            return
    changed = False
    stopped = False          # 空间不足 → 本轮不再处理后面的条目
    with _qlock:
        keep = []
        for entry in _pending:
            if entry.get("status") == "failed":
                keep.append(entry)
                continue
            if stopped:
                keep.append(entry)      # 空间不够,后面的原样留着下轮再说
                continue
            status, err = _process_one(entry, nas_cfg, target_dir, mode)
            if status == "done":
                _last_archive = time.time()
                changed = True          # 移除:不入 keep
                continue
            if status == "wait_space":
                # 目标空间不够:不是错误,不消耗重试次数,保留条目并**停止本轮**
                # (否则后面每个条目都要扫一遍目录算体积,白白磨磁盘)
                entry["status"] = "waiting_space"
                entry["error"] = err
                changed = True
                keep.append(entry)
                stopped = True
                _log(f"[归档等待] {entry.get('streamer')} {err}")
                continue
            if status == "defer":
                # 源还在被写入(正在录制):不是错误,不消耗重试、也不碰源目录。
                # 状态置 deferred 仅供界面展示;录完下一轮自动继续。
                if entry.get("status") != "deferred":       # 只记一次,避免每 60s 刷屏
                    _log(f"[归档暂缓] {entry.get('streamer')} {err}")
                entry["status"] = "deferred"
                entry["error"] = err
                changed = True
                keep.append(entry)
                continue
            if status == "failed":
                # 永久性失败(如旧版错误条目),不再重试
                entry["status"] = "failed"
                entry["error"] = err
                _log(f"[归档失败] {entry.get('streamer')} {err}")
                _notify_archive(entry.get("src_path", ""), "failed", None, None, err)
                changed = True
                keep.append(entry)
            else:
                entry["retries"] = entry.get("retries", 0) + 1
                entry["error"] = err  # 记录最近一次失败原因(重试耗尽后可查)
                if entry["retries"] >= MAX_RETRIES:
                    entry["status"] = "failed"
                    _log(f"[归档失败] {entry.get('streamer')} 重试 {entry['retries']} 次仍失败: {err}")
                    _notify_archive(entry.get("src_path", ""), "failed", None, None, err)
                changed = True
                keep.append(entry)
        if changed:
            _pending = keep
            _save_pending()

def _init_dirs(target_dir, root_dir, monitors):
    """目标就绪后按 monitors 初始化 <target>/<root_dir>/<主播名>/ 目录树。"""
    if not target_dir:
        return
    for m in monitors:
        d = os.path.join(target_dir, root_dir, m.get("name", "?"))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def _nas_status(mounted, backoff_s, mode="auto", target_dir=None):
    """归档状态快照(Web 状态卡 / 健康面板数据源)。

    除队列计数外还给「目标卷容量」「当前搬运进度」——盘满是 2026-09-18 那次
    批量归档失败的根因,但当时界面上只看得到「失败 N 条」,看不到为什么。
    mode 传的是 resolve_target 的**实际生效**类型(nas / external),不是配置里的 auto。
    """
    qs = queue_status()
    st = {
        "mounted": mounted,
        "mode": mode,
        "backoff_s": backoff_s,
        "queue_depth": qs["depth"],
        "failed": qs["failed"],
        "waiting_space": qs.get("waiting_space", 0),
        "deferred": qs.get("deferred", 0),
        "last_archive": qs["last_archive"],
        "progress": qs.get("progress"),
        "free_bytes": _free_at(target_dir) if target_dir else None,
    }
    if st["free_bytes"] is not None:
        st["free_human"] = _human(st["free_bytes"])
    usage = _usage_at(target_dir) if target_dir else None
    if usage:
        st["disk_total"] = usage["total"]
        st["disk_used"] = usage["used"]
        st["disk_free"] = usage["free"]
        st["disk_used_percent"] = usage["used_percent"]
    return st


async def _offload(fn, *args, beat=None, **kwargs):
    """把阻塞型同步函数丢到线程池执行,避免独占 asyncio 事件循环。

    为什么必须这样(2026-10-27 事故):`nas_watch` 里的 `process_queue` 原来是
    **直接同步调用**的,它在事件循环线程里逐字节搬 5.5 GB 到 SMB(实测 ~2.6 MB/s,
    约 35 分钟)。整条事件循环被独占,12 个检测协程从 `create_task` 之后连第一次
    调度都没拿到 —— `supervise` 首行的 `state.set_streamer()` 从未执行,于是
    `/api/status.streamers` 恒为 `{}`,页面「热点窗口 / 上次开播」全变成「—」。
    更严重的是这段时间任何主播开播都不会被发现、也不会开录。

    凡是从协程里碰 SMB 的同步调用(挂载、目录扫描、statvfs、搬运)都要走这里。

    beat: 传组件 key 时,等待期间每 30s 补一次心跳。一轮搬运远超 `archive` 的
          900s grace,不补会被健康面板误判成「归档协程卡死」。
    """
    loop = asyncio.get_running_loop()
    fut = asyncio.ensure_future(loop.run_in_executor(None, lambda: fn(*args, **kwargs)))
    if beat:
        try:
            while not fut.done():
                health.beat(beat)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            fut.cancel()      # 线程无法真正取消,仅尽早丢弃结果;退出路径上可接受
            raise
    return await fut


async def nas_watch(state):
    """归档目标健康监控 + 队列消化(monitor 主循环接入)。

    支持三种归档模式(auto/nas/external);外接硬盘热插拔(拔出→目录消失→入队等待,
    插回→目录恢复→自动补传)由同一轮询节奏自然覆盖,无需额外定时器。

    ⚠️ 本函数跑在 asyncio 事件循环线程里,凡会碰 SMB 的同步调用(挂载探测、
    目录扫描、statvfs、搬运)**一律经 `_offload` 转线程池**。直接同步调用会把
    整条循环连同 12 个检测协程一起饿死 —— 2026-10-27 事故,详见 `_offload`。
    """
    global _last_host_check
    backoff = 60
    first_ready = False
    # 初值 False 是**有意的**:进程重启会走一轮 backfill,兜住「目标长期不可用
    # 期间重试耗尽被标 failed / 压根没进过队列」的漏网场次(见函数 docstring)。
    # 这不是误判 —— 它会触发的风险只有一个「唤醒一轮搬运」,而那个风险(阻塞
    # 事件循环)已由 `_offload` 消除,不必为了绕开它牺牲补归档兜底。
    target_was_ok = False    # 上一轮目标是否可用 → 翻转时触发补归档
    while True:
        health.beat("archive")   # 健康面板:归档协程存活心跳
        cfg = state.get_config()
        nas_cfg = cfg.get("nas") or {}
        archive = cfg.get("archive") or {}
        has_nas = bool(nas_cfg.get("enabled", False))
        has_ext = bool((archive.get("external_dir") or "").strip())
        mode = archive.get("mode", "auto") or "auto"
        if not has_nas and not has_ext:
            # 未配置任何归档目标:只空转 300s,不做挂载/目录探测
            state.set_nas_status(await _offload(_nas_status, False, 0))
            await asyncio.sleep(300)
            continue
        # 周期预检(默认 24h,可配 nas.host_check_hours):轻量探测当前 host 可达性,
        # 发现 IP 漂移即自动识别并更新 config,避免归档静默失效 1-3 天才发现。
        now = time.time()
        # 选了「仅外接硬盘」就不做 NAS 网段探测(同上:不该为不用的目标付探测代价)
        if not DRY_RUN and mode != "external" and has_nas \
                and nas_cfg.get("auto_discover", True) \
                and now - _last_host_check >= float(nas_cfg.get("host_check_hours", 24)) * 3600:
            _last_host_check = now
            _host = clean_host(nas_cfg.get("host", ""))
            _port = int(nas_cfg.get("port", 445) or 445)
            if _host and not await _offload(_host_alive, _host, _port):
                await _offload(auto_recover_host, cfg)
        # 挂载探测:NAS 模式下可能要跑一次 mount_smbfs,同样不能占着事件循环
        ok, target_dir, mode = await _offload(resolve_target, cfg)
        if ok:
            backoff = 60
            if not first_ready:
                await _offload(_init_dirs, target_dir,
                               nas_cfg.get("root_dir", "直播回放"),
                               cfg.get("monitors", []))
                first_ready = True
            if not target_was_ok:
                # 目标刚从「不可用」恢复(插上外接盘 / NAS 重新上线):
                # 把历史上漏搬的场次补进队列,下一次 process_queue 就会带走。
                # 幂等,重复调用无副作用。
                try:
                    await _offload(backfill_from_history, cfg)
                except Exception as e:                 # noqa: BLE001
                    _log("[补归档] 异常: %s" % e)
            target_was_ok = True
            try:
                # 搬运走线程池 + 全程补心跳:一轮可能要搬几十 GB,远超 grace
                await _offload(process_queue, cfg, target_dir, mode, beat="archive")
            except Exception as e:                     # noqa: BLE001
                # 单轮搬运异常绝不能拖垮归档协程:协程一死,归档会永久静默停止,
                # 而进程还活着、界面还正常 —— 正是最难发现的那类故障。
                _log("[归档] 本轮搬运异常: %s" % e)
                _progress_end(str(e))
            st = await _offload(_nas_status, True, 0, mode, target_dir)
            state.set_nas_status(st)
            if st.get("waiting_space"):
                health.beat("archive", error="目标空间不足,归档暂停")
            await asyncio.sleep(60)
        else:
            target_was_ok = False
            state.set_nas_status(await _offload(_nas_status, False, backoff, mode))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)


# 启动时加载 pending.json 续传
_load_pending()
