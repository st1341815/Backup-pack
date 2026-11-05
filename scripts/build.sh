#!/bin/sh
# 构建 fpk：版本校验 → staging（排除 tests/scripts）→ fnpack build → 带版本号产物 + sha256。
#
# 版本规范（硬约束，三处必须完全一致，否则拒绝打包）：
#   manifest          version               = X.Y.Z
#   app/ui/index.cgi  VERSION = "X.Y.Z"
#   CHANGELOG.md      第一条标题             ## vX.Y.Z
# 产物命名：<appname>-<version>.fpk（附带同名 .sha256）
#
# 用法：sh scripts/build.sh
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
cd "$SRC"

APPNAME=$(sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' manifest | head -1 | tr -d '"' | tr -d ' \r')
MF_VER=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' manifest | head -1 | tr -d '"' | tr -d ' \r')
CGI_VER=$(sed -n 's/^VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' app/ui/index.cgi | head -1)
LOG_VER=$(grep -m1 '^## v' CHANGELOG.md | sed 's/^## v\([0-9][^（( ]*\).*/\1/')

[ -n "$APPNAME" ] || { echo "manifest 里读不到 appname" >&2; exit 1; }
[ -n "$MF_VER" ]  || { echo "manifest 里读不到 version" >&2; exit 1; }

echo "appname : $APPNAME"
echo "version : manifest=$MF_VER  index.cgi=$CGI_VER  CHANGELOG=$LOG_VER"

if [ "$MF_VER" != "$CGI_VER" ] || [ "$MF_VER" != "$LOG_VER" ]; then
    cat >&2 <<EOF

拒绝打包：三处版本号不一致。
  修正方式（必须同时改）：
    manifest           version = $MF_VER
    app/ui/index.cgi   VERSION = "$CGI_VER"
    CHANGELOG.md       首条 ## v$LOG_VER
  或直接执行：sh scripts/bump_version.sh <新版本号>
EOF
    exit 1
fi

# 同一版本重复构建会产生不同 sha256 的同名包，容易装错。显式提示。
if [ -f "../$APPNAME-$MF_VER.fpk" ]; then
    echo "提示：$APPNAME-$MF_VER.fpk 已存在，本次将覆盖它。若这是新的改动，请先升版本号：" >&2
    echo "      sh scripts/bump_version.sh $(echo "$MF_VER" | awk -F. '{printf \"%s.%s.%s\", $1, $2, $3+1}')" >&2
fi

if ! command -v fnpack >/dev/null 2>&1; then
    echo "未找到 fnpack：请先在 fnOS 构建环境安装。本脚本只做打包与校验，不代装。" >&2
    exit 1
fi

STAGE_ROOT=$(mktemp -d)
STAGE="$STAGE_ROOT/fnos-backup-pack"
mkdir -p "$STAGE"
cp -a "$SRC/." "$STAGE/"

# tests/ 与 scripts/ 不进 fpk（自测与构建工具，非运行时资产）
rm -rf "$STAGE/tests" "$STAGE/scripts" "$STAGE/.gitignore"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -name '*.pyc' -delete 2>/dev/null || true

# 权限：目录 755 / 文件 644，仅入口脚本与生命周期脚本 755。
# 本机源码目录是 700 (drwx------)，直接打包会让 fnOS 的 Web 进程读不到文件。
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/app/ui/index.cgi" "$STAGE"/cmd/*
find "$STAGE/app/lib" -name '*.py' -exec chmod 755 {} +

echo "staging: $STAGE"
cd "$STAGE_ROOT"
fnpack build -d fnos-backup-pack

FPK="$STAGE_ROOT/$APPNAME.fpk"
if [ ! -f "$FPK" ]; then
    echo "打包失败：未找到 $FPK" >&2
    ls -la "$STAGE_ROOT" >&2
    exit 1
fi

OUT="$SRC/../$APPNAME-$MF_VER.fpk"
cp -f "$FPK" "$OUT"
rm -rf "$STAGE_ROOT"

cd "$SRC/.."
rm -f "$APPNAME.fpk" "$APPNAME.fpk.sha256"        # 清理旧的无版本号产物，避免装错
sha256sum "$(basename "$OUT")" | tee "$APPNAME-$MF_VER.fpk.sha256"
echo "完成：$OUT"
echo "版本：$MF_VER（manifest / index.cgi / CHANGELOG 三处已校验一致）"
