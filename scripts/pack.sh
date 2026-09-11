#!/usr/bin/env bash
# 产出搬迁包 dist/douyin-monitor-<version>.tar.gz
# 排除 .venv/ logs/ __pycache__/ spool/ .git/ dist/;config.json 脱敏为 config.example.json,原 config.json 不入包。
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

VERSION="$(git describe --tags --always 2>/dev/null || echo 'unknown')"
DIST="$HERE/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
rm -rf "$DIST"
mkdir -p "$DIST"

PKG="douyin-monitor-$VERSION"
mkdir -p "$STAGE/$PKG"

# 1) 复制工作树(排除运行时/构建产物)
rsync -a \
  --exclude '.venv' --exclude 'logs' --exclude '__pycache__' --exclude 'spool' \
  --exclude 'recordings' --exclude '.git' --exclude 'dist' \
  --exclude '*.pyc' --exclude '.DS_Store' --exclude 'hotspots.json' \
  "$HERE/" "$STAGE/$PKG/"

# 2) 隐私清理:config.json → config.example.json(清空 cookie/username/password_ref,补齐完整结构),原 config.json 不入包
if [ -f "$STAGE/$PKG/config.json" ]; then
  python3 - "$STAGE/$PKG/config.json" "$STAGE/$PKG/config.example.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    cfg = json.load(f)

# 补齐完整结构(契约 §4 默认值),保留已有值
cfg.setdefault("detection", {}).setdefault("cookie", "")
nas = cfg.setdefault("nas", {})
for k, v in {
    "enabled": False, "protocol": "smb", "host": "", "port": 445,
    "share": "", "username": "", "password_ref": "keychain:douyin-nas",
    "mount_point": "/Volumes/douyin-archive", "root_dir": "录播",
}.items():
    nas.setdefault(k, v)
cfg.setdefault("recorder", {}).setdefault("output_dir", "")
cfg["recorder"].setdefault("enabled", True)
cfg["recorder"].setdefault("format", "flv")

# 脱敏:清空 cookie/username,password_ref 重置为引用
cfg["detection"]["cookie"] = ""
cfg["nas"]["username"] = ""
cfg["nas"]["password_ref"] = "keychain:douyin-nas"

with open(dst, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PY
  rm -f "$STAGE/$PKG/config.json"
fi

# 3) 打包
TARBALL="$DIST/douyin-monitor-$VERSION.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$PKG"
echo "产出: $TARBALL"
