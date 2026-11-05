#!/bin/sh
# 本机一键部署（需要 sudo / root，可直接在 fnOS 上跑）。
#
# 背景：appcenter-cli 的 install-fpk 对「已安装」的应用只会打印
#       "[Info]Application [xxx] is installed." 然后**什么都不做**（不会升级），
#       应用中心日志里也没有任何记录。所以升级必须走「卸载 → 安装 → 启动」。
#
# 数据安全：实测卸载会删 /vol1/@appcenter/<app>（代码），但保留
#       /vol1/@appdata/<app>（var：配置/日志）；备份包落在用户目标目录，不受影响。
#       本脚本仍会先把 var 打包到 /tmp 兜底，装完再确认它还在，不在就恢复。
#
# 用法：sudo sh scripts/deploy.sh ../backup-pack-1.2.5.fpk
set -eu

FPK=${1:-}
APPNAME=$(sed -n 's/^appname[[:space:]]*=[[:space:]]*//p' manifest 2>/dev/null | head -1 | tr -d '"' | tr -d ' \r')
[ -n "$APPNAME" ] || APPNAME=backup-pack
[ -n "$FPK" ] || { echo "用法：sudo sh scripts/deploy.sh <fpk 路径>" >&2; exit 1; }
[ -f "$FPK" ] || { echo "找不到 fpk：$FPK" >&2; exit 1; }
command -v appcenter-cli >/dev/null 2>&1 || { echo "找不到 appcenter-cli" >&2; exit 1; }

VARBASE=/vol1/@appdata
BK="/tmp/$APPNAME-var-$(date +%Y%m%d%H%M%S).tgz"

echo "==> 1/5 备份 var：$BK"
if [ -d "$VARBASE/$APPNAME" ]; then
    tar czf "$BK" -C "$VARBASE" "$APPNAME" || true
    echo "    备份大小：$(du -h "$BK" | cut -f1)"
else
    echo "    （首次安装，无 var）"
fi

echo "==> 2/5 停止并卸载"
appcenter-cli stop "$APPNAME" >/dev/null 2>&1 || true
appcenter-cli uninstall "$APPNAME" 2>&1 | tr '\r' '\n' | grep -v '^[\\|/ -]*uninstalling' | tail -2

echo "==> 3/5 安装 $FPK"
appcenter-cli install-fpk "$FPK" 2>&1 | tr '\r' '\n' | grep -v '^[\\|/ -]*installing' | grep -v 'Verifying files' | tail -3

echo "==> 4/5 恢复 var（若被清空）"
if [ ! -d "$VARBASE/$APPNAME" ] && [ -f "$BK" ]; then
    tar xzf "$BK" -C "$VARBASE"
    echo "    已从备份恢复"
else
    echo "    var 保留，无需恢复"
fi

echo "==> 5/5 启动并自检"
appcenter-cli start "$APPNAME" >/dev/null 2>&1 || true
sleep 3
appcenter-cli list | grep "$APPNAME" || true
VER=$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' /var/apps/"$APPNAME"/manifest | head -1 | tr -d ' \r')
PORT=$(sed -n 's/^service_port[[:space:]]*=[[:space:]]*//p' /var/apps/"$APPNAME"/manifest | head -1 | tr -d ' \r')
echo "    版本=$VER 端口=$PORT"
if [ -n "$PORT" ]; then
    curl -sS --max-time 5 "http://127.0.0.1:$PORT/?action=status" >/dev/null 2>&1 \
        && echo "    HTTP 自检：OK" || echo "    HTTP 自检：失败（看 /vol1/@appdata/$APPNAME/server.log）"
fi
echo "完成。回滚用备份：$BK"
