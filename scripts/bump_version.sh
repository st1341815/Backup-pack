#!/bin/sh
# 升版本号：一次改到位（manifest / app/ui/index.cgi / CHANGELOG.md），
# 并在 CHANGELOG 顶部插入新条目骨架。改完再跑 scripts/build.sh 出包。
#
# 用法：sh scripts/bump_version.sh 1.2.2
set -eu

NEW=${1:-}
if ! printf '%s' "$NEW" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "用法：sh scripts/bump_version.sh <X.Y.Z>   例：1.2.2" >&2
    exit 1
fi

SRC=$(cd "$(dirname "$0")/.." && pwd)
cd "$SRC"

OLD=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' manifest | head -1 | tr -d '"' | tr -d ' \r')
TODAY=$(date +%Y-%m-%d)

sed -i "s/^version[[:space:]]*=.*/version               = $NEW/" manifest
sed -i "s/^VERSION[[:space:]]*=[[:space:]]*\"[^\"]*\"/VERSION = \"$NEW\"/" app/ui/index.cgi

python3 - "$NEW" "$TODAY" <<'PY'
import sys
new, today = sys.argv[1], sys.argv[2]
path = "CHANGELOG.md"
lines = open(path, encoding="utf-8").read().splitlines()
entry = ["## v%s（%s）" % (new, today), "",
         "（待填写：这一版改了什么、为什么改、影响面）", ""]
out, done = [], False
for line in lines:
    out.append(line)
    if not done and line.strip() == "# CHANGELOG":
        out.append("")
        out.extend(entry)
        done = True
open(path, "w", encoding="utf-8").write("\n".join(out).rstrip("\n") + "\n")
print("CHANGELOG 顶部已插入 v%s 条目，记得补内容" % new)
PY

echo "版本：$OLD -> $NEW"
echo "已同步：manifest、app/ui/index.cgi、CHANGELOG.md"
echo "下一步：补 CHANGELOG 内容 → sh scripts/build.sh"
