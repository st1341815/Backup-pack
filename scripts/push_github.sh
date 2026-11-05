#!/bin/sh
# 发布到 GitHub：建仓库（可选）→ 推送 → 打 tag → 创建 Release → 上传 fpk。
#
# 用法：
#   sh scripts/push_github.sh <owner/repo>              # 仅推送（仓库需已存在，走 SSH）
#   GITHUB_TOKEN=ghp_xxx sh scripts/push_github.sh <owner/repo> [--create]
#                                                       # 建仓库 + 推送 + Release + 附件
#
# 说明：
# - SSH 走 ~/.ssh/config 里的 github.com（本机已验证可认证）；
# - Release 附件上传只能走 API，必须提供 token（repo 权限）；
# - 本机 curl 访问 github.com 需要 -k（存在自签证书代理）。
set -eu

REPO=${1:-}
MODE=${2:-}
[ -n "$REPO" ] || { echo "用法：sh scripts/push_github.sh <owner/repo> [--create]" >&2; exit 1; }

SRC=$(cd "$(dirname "$0")/.." && pwd)
cd "$SRC"

# 本机 ssh 用的是 passwd 里的 home（/home/<user>，实际不存在），不会读 $HOME，
# 所以必须显式 -F 指向真实 config，否则 push 报 Host key verification failed。
export HOME="${HOME:-/vol1/@appshare/octop-native/data}"
if [ -f "$HOME/.ssh/config" ]; then
    export GIT_SSH_COMMAND="ssh -F $HOME/.ssh/config -o BatchMode=yes"
fi

APPNAME=$(sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' manifest | head -1 | tr -d '"' | tr -d ' \r')
VER=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' manifest | head -1 | tr -d '"' | tr -d ' \r')
TAG="v$VER"
FPK="../$APPNAME-$VER.fpk"
SHA="$FPK.sha256"
NOTES="docs/RELEASE_$TAG.md"

echo "repo=$REPO  app=$APPNAME  version=$VER  tag=$TAG"

# ---------- 1) 可选：创建仓库 ----------
if [ "$MODE" = "--create" ]; then
    [ -n "${GITHUB_TOKEN:-}" ] || { echo "创建仓库需要 GITHUB_TOKEN" >&2; exit 1; }
    code=$(curl -sk -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO")
    if [ "$code" = "404" ]; then
        echo "==> 仓库不存在，创建中"
        curl -sk -X POST \
            -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/user/repos" \
            -d "{\"name\":\"${REPO#*/}\",\"description\":\"飞牛 fnOS 应用：把大量小文件目录打包为单个可备份包\",\"private\":false,\"has_issues\":true,\"has_wiki\":false}" \
            -o /dev/null -w '    创建结果 http=%{http_code}\n'
    else
        echo "==> 仓库已存在（http=$code）"
    fi
fi

# ---------- 2) 推送 ----------
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "git@github.com:$REPO.git"
else
    git remote add origin "git@github.com:$REPO.git"
fi
echo "==> 推送 main"
git branch -M main 2>/dev/null || true
git push -u origin main

echo "==> 打 tag $TAG"
git tag -f "$TAG" -m "backup-pack $VER" 2>/dev/null || true
git push -f origin "$TAG"

# ---------- 3) Release（需要 token） ----------
if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "==> 未提供 GITHUB_TOKEN：已推送代码与 tag，Release 需手动创建或补 token 后重跑"
    exit 0
fi

[ -f "$NOTES" ] || { echo "缺少 $NOTES" >&2; exit 1; }
python3 - "$NOTES" /tmp/release_body.json <<'PY'
import json, sys
body = open(sys.argv[1], encoding="utf-8").read()
json.dump({"body": body}, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY

echo "==> 创建 Release $TAG"
RESP=$(curl -sk -X POST \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/releases" \
    -d "{\"tag_name\":\"$TAG\",\"name\":\"$TAG\",\"body\":$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1],encoding='utf-8').read()))" "$NOTES"),\"draft\":false,\"prerelease\":false}")

UPLOAD=$(printf '%s' "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('upload_url','').split('{')[0])" 2>/dev/null || true)
if [ -z "$UPLOAD" ]; then
    echo "Release 创建失败，响应："; printf '%s\n' "$RESP" | head -20
    exit 1
fi
echo "    上传地址：$UPLOAD"

# ---------- 4) 上传附件 ----------
for f in "$FPK" "$SHA"; do
    [ -f "$f" ] || { echo "缺少 $f，跳过" >&2; continue; }
    name=$(basename "$f")
    echo "==> 上传 $name"
    curl -sk -X POST \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Content-Type: application/octet-stream" \
        --data-binary @"$f" \
        "$UPLOAD?name=$name" \
        -o /dev/null -w '    http=%{http_code}\n'
done

echo "完成：https://github.com/$REPO/releases/tag/$TAG"
