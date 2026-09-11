#!/usr/bin/env python3
"""NAS 挂载管理与归档队列。

模块导入面(契约 §5):ensure_mount / get_password / nas_watch / enqueue / queue_status。
另提供 set_password / test_connection 供 webui 的 /api/nas 与 /api/nas/test 使用(实现细节)。
"""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import quote

import health

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
    except OSError:
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

    nas_ready = False
    if nas_cfg.get("enabled", False):
        nas_ready = ensure_mount(cfg)

    ext_dir = os.path.expanduser((archive.get("external_dir") or "").strip())
    ext_ready = bool(ext_dir) and os.path.isdir(ext_dir) and _probe_writable(ext_dir)

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


def queue_status():
    """返回 {depth, failed, last_archive}。"""
    with _qlock:
        depth = sum(1 for e in _pending if e.get("status") != "failed")
        failed = sum(1 for e in _pending if e.get("status") == "failed")
        return {"depth": depth, "failed": failed, "last_archive": _last_archive}


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


def _process_one(entry, nas_cfg, target_dir=None):
    """搬运单个条目到归档目标。返回 ("done"|"retry", error)。

    target_dir 为生效归档根目录(mount_point 或 external_dir);为空时回退 nas 挂载点。
    目录层级:<target>/<root_dir>/<主播>/<YYYY-MM-DD>/<文件> —— NAS/外置硬盘共用此逻辑。
    """
    src = entry.get("src_path", "")
    if not src or not os.path.exists(src):
        return "retry", "源不存在"
    # 防御:旧版条目 src 指向录制根目录(recordings),会误搬全部主播,直接标失败
    if os.path.basename(os.path.normpath(src)) == "recordings":
        return "failed", "旧版错误条目(src=录制根目录),已拦截"
    mp = target_dir if target_dir else _mount_point(nas_cfg)
    root = nas_cfg.get("root_dir", "直播回放")
    streamer = entry.get("streamer", "?")
    dest_dir = os.path.join(mp, root, streamer, _archive_date(entry))
    fname = os.path.basename(os.path.normpath(src)) or "recording"
    is_dir = os.path.isdir(src)
    try:
        if is_dir:
            _notify_archive(src, "archiving", dest_dir, None, None)
            # 目录(场次日期目录):把**内容**搬进 <target>/<root>/<主播>/<date>/,
            # 不再嵌套一层同名目录;逐项复制校验,全部成功后删除源目录。
            os.makedirs(dest_dir, exist_ok=True)
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
                else:
                    shutil.copy2(s, part)
                    if os.path.getsize(part) != os.path.getsize(s):
                        raise OSError(f"复制后大小不一致: {item}")
                    if os.path.exists(d):
                        os.remove(d)
                    os.rename(part, d)
            shutil.rmtree(src, ignore_errors=True)
            _write_meta(dest_dir, entry, time.time())
            _notify_archive(src, "archived", dest_dir, None, None)
            return "done", ""
        dest = os.path.join(dest_dir, fname)
        part = dest + ".part"
        os.makedirs(dest_dir, exist_ok=True)
        _notify_archive(src, "archiving", dest_dir, None, None)
        # 单文件:copy2 到 .part → 校验大小 → rename
        shutil.copy2(src, part)
        if os.path.getsize(part) != os.path.getsize(src):
            raise OSError("复制后大小不一致")
        os.rename(part, dest)
        _write_meta(dest, entry, time.time())
        os.remove(src)
        _notify_archive(src, "archived", dest_dir, None, None)
        return "done", ""
    except Exception as e:
        try:
            if os.path.isdir(part):
                shutil.rmtree(part, ignore_errors=True)
            elif os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        return "retry", str(e)


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
    with _qlock:
        keep = []
        for entry in _pending:
            if entry.get("status") == "failed":
                keep.append(entry)
                continue
            status, err = _process_one(entry, nas_cfg, target_dir)
            if status == "done":
                _last_archive = time.time()
                changed = True  # 移除:不入 keep
            elif status == "failed":
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


def _nas_status(mounted, backoff_s, mode="auto"):
    qs = queue_status()
    return {
        "mounted": mounted,
        "mode": mode,
        "backoff_s": backoff_s,
        "queue_depth": qs["depth"],
        "failed": qs["failed"],
        "last_archive": qs["last_archive"],
    }


async def nas_watch(state):
    """归档目标健康监控 + 队列消化(monitor 主循环接入)。

    支持三种归档模式(auto/nas/external);外接硬盘热插拔(拔出→目录消失→入队等待,
    插回→目录恢复→自动补传)由同一轮询节奏自然覆盖,无需额外定时器。
    """
    import asyncio
    global _last_host_check
    backoff = 60
    first_ready = False
    while True:
        health.beat("archive")   # 健康面板:归档协程存活心跳
        cfg = state.get_config()
        nas_cfg = cfg.get("nas") or {}
        archive = cfg.get("archive") or {}
        has_nas = bool(nas_cfg.get("enabled", False))
        has_ext = bool((archive.get("external_dir") or "").strip())
        if not has_nas and not has_ext:
            # 未配置任何归档目标:只空转 300s,不做挂载/目录探测
            state.set_nas_status(_nas_status(False, 0))
            await asyncio.sleep(300)
            continue
        # 周期预检(默认 24h,可配 nas.host_check_hours):轻量探测当前 host 可达性,
        # 发现 IP 漂移即自动识别并更新 config,避免归档静默失效 1-3 天才发现。
        now = time.time()
        if not DRY_RUN and has_nas and nas_cfg.get("auto_discover", True) \
                and now - _last_host_check >= float(nas_cfg.get("host_check_hours", 24)) * 3600:
            _last_host_check = now
            _host = clean_host(nas_cfg.get("host", ""))
            _port = int(nas_cfg.get("port", 445) or 445)
            if _host and not _host_alive(_host, _port):
                await asyncio.get_event_loop().run_in_executor(
                    None, auto_recover_host, cfg)
        ok, target_dir, mode = resolve_target(cfg)
        if ok:
            backoff = 60
            if not first_ready:
                _init_dirs(target_dir, nas_cfg.get("root_dir", "直播回放"), cfg.get("monitors", []))
                first_ready = True
            process_queue(cfg, target_dir, mode)
            state.set_nas_status(_nas_status(True, 0, mode))
            await asyncio.sleep(60)
        else:
            state.set_nas_status(_nas_status(False, backoff, mode))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)


# 启动时加载 pending.json 续传
_load_pending()
