#!/bin/bash
# 验收自测脚本（对应文档第 21 章 AC 条目）
T=/tmp/pbtest; A=$T/app
export PATH=$T/bin:$PATH PHOTOS_BACKUP_APP_DIR=$A
# 安全护栏：必须使用沙箱 crontab 桩，否则会污染真实 crontab
if ! command -v crontab | grep -q "^/tmp/pbtest/bin/"; then
  echo "安全检查失败：crontab 未指向沙箱桩，为避免污染真实 crontab，测试中止。" >&2
  exit 99
fi
pass=0; fail=0
ok(){ echo "  [PASS] $1"; pass=$((pass+1)); }
ng(){ echo "  [FAIL] $1"; fail=$((fail+1)); }
chk(){ if [ "$2" = "$3" ]; then ok "$1 ($2)"; else ng "$1 期望[$3] 实际[$2]"; fi; }

setcfg(){ python3 - "$@" <<'PY'
import json,os,sys
p="/tmp/pbtest/app/var/config.json"
os.makedirs(os.path.dirname(p),exist_ok=True)   # 干净沙箱里 var/ 尚不存在（真实安装由 install_init 创建）
cfg={"src_dir":"/tmp/pbtest/src","dst_dir":"/tmp/pbtest/dst","compress":"none",
     "keep_versions":3,"cron_expr":"0 3 * * *","enable_cron":True,"log_retain_days":7,
     "min_free_ratio":0.10,"max_files":0,"allow_root_mount":True,"dst_mountpoint":""}
for kv in sys.argv[1:]:
    k,_,v=kv.partition("=")
    if v in ("True","False"): v=(v=="True")
    elif v.isdigit(): v=int(v)
    cfg[k]=v
json.dump(cfg,open(p,"w"),ensure_ascii=False,indent=2)
PY
}

echo "AC-03 保留份数"
setcfg; rm -rf $T/dst/*; rm -f $A/var/task.log $A/var/status.json
for i in 1 2 3 4 5; do python3 $A/lib/backup.py >/dev/null 2>&1; sleep 1; done
chk "包数=3" "$(ls $T/dst/*.tar 2>/dev/null|wc -l)" "3"
chk "校验数=3" "$(ls $T/dst/*.sha256 2>/dev/null|wc -l)" "3"

echo "AC-04 跨格式保留（tar 与 gzip 混跑，全格式统一计数）"
setcfg compress=none; rm -rf $T/dst/*; rm -f $A/var/status.json
python3 $A/lib/backup.py >/dev/null 2>&1; sleep 1
python3 $A/lib/backup.py >/dev/null 2>&1; sleep 1
setcfg compress=gzip
python3 $A/lib/backup.py >/dev/null 2>&1; sleep 1
python3 $A/lib/backup.py >/dev/null 2>&1
chk "混合格式包总数=3" "$(ls $T/dst/*.tar $T/dst/*.tar.gz 2>/dev/null|wc -l)" "3"
chk "校验文件数=3" "$(ls $T/dst/*.sha256 2>/dev/null|wc -l)" "3"
chk "最新两份是 gzip" "$(ls $T/dst/*.tar.gz 2>/dev/null|wc -l)" "2"
setcfg compress=none

echo "AC-15/18 无效产物不计 keep、不自动删、列表标 invalid"
head -c 300000 /dev/urandom > $T/dst/thumbnails_20990101_000000.tar
python3 $A/lib/backup.py >/dev/null 2>&1
chk "无效包仍存在(不误删)" "$([ -f $T/dst/thumbnails_20990101_000000.tar ] && echo yes || echo no)" "yes"
chk "有效包仍为3" "$(ls $T/dst/*.sha256|wc -l)" "3"
python3 - <<'PY'
import sys; sys.path.insert(0,"/tmp/pbtest/app/lib")
import backup
v,r=backup.collect_packages("/tmp/pbtest/dst")
print("  collect: valid=%d residue=%d"%(len(v),len(r)))
PY

echo "AC-46 清理无效产物（显式确认才删，且不碰有效包）"
python3 - <<'PY'
import sys; sys.path.insert(0,"/tmp/pbtest/app/lib")
import backup
v,r=backup.collect_packages("/tmp/pbtest/dst")
print("  清理前: valid=%d residue=%d"%(len(v),len(r)))
print("  purge 删除 %d 个"%backup.purge_invalid("/tmp/pbtest/dst"))
PY
chk "无效产物已清除" "$([ -f $T/dst/thumbnails_20990101_000000.tar ] && echo no || echo yes)" "yes"
chk "有效包未被误删" "$(ls $T/dst/*.sha256 2>/dev/null|wc -l)" "3"

echo "AC-15 打包失败不留残包"
setcfg; rm -rf $T/dst/*; rm -f $A/var/task.log
cp -a $T/src $T/src_bad 2>/dev/null
echo "secret" > $T/src_bad/f000.jpg; chmod 000 $T/src_bad/f000.jpg
setcfg src_dir=$T/src_bad
python3 $A/lib/backup.py >/dev/null 2>&1; rc=$?
chmod 644 $T/src_bad/f000.jpg
chk "失败退出码非0" "$([ $rc -ne 0 ] && echo yes || echo no)" "yes"
chk "失败后无残留包" "$(ls $T/dst/*.tar 2>/dev/null|wc -l)" "0"
grep -q "已丢弃无效产物" $A/var/task.log && ok "日志记录已丢弃" || ng "日志未记录"

echo "AC-16 空源目录拒绝"
setcfg src_dir=$T/src_empty; mkdir -p $T/src_empty; rm -rf $T/dst/*
python3 $A/lib/backup.py >/dev/null 2>&1; rc=$?
chk "空源退出码非0" "$([ $rc -ne 0 ] && echo yes || echo no)" "yes"
chk "空源未产包" "$(ls $T/dst/ 2>/dev/null|wc -l)" "0"

echo "AC-07 死锁立即自愈（不等 6 小时）"
setcfg; rm -rf $T/dst/*
python3 - <<'PY'
import json,os,time
json.dump({"pid":999999,"task":"manual","start":"x","start_epoch":time.time()},
          open("/tmp/pbtest/app/var/backup.lock","w"))
PY
start=$(date +%s)
python3 $A/lib/backup.py >/dev/null 2>&1; rc=$?
el=$(( $(date +%s) - start ))
chk "死锁后能立即备份(rc=0)" "$rc" "0"
chk "耗时<10s" "$([ $el -lt 10 ] && echo yes || echo no)" "yes"

echo "AC-06 活进程锁 → 第二次被拒"
python3 - <<'PY'
import json
json.dump({"pid":1,"task":"manual","start":"now","start_epoch":0},
          open("/tmp/pbtest/app/var/backup.lock","w"))
PY
python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
chk "活锁拒绝新任务" "$([ $r -ne 0 ] && echo yes || echo no)" "yes"
python3 $A/lib/backup.py --unlock >/dev/null 2>&1
python3 $A/lib/backup.py --unlock >/dev/null 2>&1
chk "活锁时 --unlock 拒绝" "$([ -f $A/var/backup.lock ] && echo kept || echo gone)" "kept"
rm -f $A/var/backup.lock

echo "AC-13 挂载点守卫"
rm -rf $T/dst/*
setcfg allow_root_mount=False dst_mountpoint=/vol9
python3 $A/lib/backup.py >/dev/null 2>&1; rc=$?
chk "挂载点变化被拒" "$([ $rc -ne 0 ] && echo yes || echo no)" "yes"
chk "拒绝时不产包" "$(ls $T/dst/*.tar 2>/dev/null|wc -l)" "0"

echo "AC-11 空间预检余量"
setcfg min_free_ratio=9999 dst_mountpoint=
python3 $A/lib/backup.py >/dev/null 2>&1; rc=$?
chk "余量不足被拒" "$([ $rc -ne 0 ] && echo yes || echo no)" "yes"

echo "AC-38 校验可移植（改名/搬目录后仍通过）"
setcfg; rm -rf $T/dst/*; python3 $A/lib/backup.py >/dev/null 2>&1
mkdir -p $T/moved && cp -a $T/dst/* $T/moved/ && mv $T/dst $T/dst_old && mv $T/moved $T/dst2
python3 - <<'PY'
import sys; sys.path.insert(0,"/tmp/pbtest/app/lib")
import backup,glob
f=glob.glob("/tmp/pbtest/dst2/*.tar")[0]
ok,msg=backup.verify(f)
print("  verify:",ok,msg[:80])
sys.exit(0 if ok else 1)
PY
chk "换路径后校验通过" "$?" "0"

echo "AC-20 cron 表达式真校验"
python3 - <<'PY'
import sys; sys.path.insert(0,"/tmp/pbtest/app/lib")
import config as c
bad=["99 99 99 99 99","abc 3 * * *","*/0 * * * *","0 3 * *","60 3 * * *","0 23-2 * * *"]
good=["0 3 * * *","*/15 2-6 1,15 * 0","0 3 * * MON-FRI"]
e=[x for x in bad if c.validate_cron_expr(x)[0]]+[x for x in good if not c.validate_cron_expr(x)[0]]
print("  误判:",e)
sys.exit(0 if not e else 1)
PY
chk "非法拒绝/合法通过" "$?" "0"

echo "AC-22/23 升级保留用户表达式 + 清理旧版/旧名遗留行"
# 三条本应用遗留行：旧名 photos-backup 的无标记行、旧路径行、当前名行；
# 升级后必须只剩 1 条（用户表达式），且不动别人的任务。
printf '# 用户自己的其它任务\n0 2 * * * /usr/bin/other-job\n0 3 * * * /usr/bin/python3 /var/apps/photos-backup/lib/cron_run.py >> /tmp/x.log 2>&1\n0 3 * * * /usr/bin/python3 /tmp/pbtest/app/lib/cron_run.py >> /tmp/y.log 2>&1\n' > /tmp/pbtest/crontab.txt
python3 - <<'PY'
import sys; sys.path.insert(0,"/tmp/pbtest/app/lib")
import config as c, json
cfg=c.load(); cfg["cron_expr"]="30 4 * * *"; c.save(cfg)
print("  install 结果:", c.install_cron_if_absent())
print("  crontab:"); print(open("/tmp/pbtest/crontab.txt").read())
PY
chk "只剩1条本应用行" "$(grep -c cron_run.py /tmp/pbtest/crontab.txt)" "1"
chk "保留用户表达式30 4" "$(grep -c '^30 4 \* \* \*' /tmp/pbtest/crontab.txt)" "1"

echo "AC-27 卸载清理"
python3 $A/lib/config.py --remove-cron >/dev/null
chk "卸载后无本应用行" "$(grep -c cron_run.py /tmp/pbtest/crontab.txt)" "0"
chk "配置文件保留" "$([ -f $A/var/config.json ] && echo yes || echo no)" "yes"

echo

# ---------------------------------------------------------------- 恢复与健壮性补充
echo "AC-01/36 小目录全链路 + 恢复一致性（含中文/空格/空目录）"
R=$T/r1; rm -rf $R; mkdir -p "$R/源 目录/子 层/空目录"
printf 'aaaa' > "$R/源 目录/中文 文件名.jpg"
printf 'bbbb' > "$R/源 目录/子 层/b.bin"
setcfg src_dir="$R/源 目录" dst_dir=$T/dst compress=none
rm -rf $T/dst/*; rm -f $A/var/*.log
python3 $A/lib/backup.py >/dev/null 2>&1
PKG=$(ls $T/dst/*.tar | head -1)
OUT=$T/restore; rm -rf $OUT; mkdir -p $OUT
tar xf "$PKG" -C $OUT 2>/dev/null
if diff -r "$R/源 目录" "$OUT" >/dev/null 2>&1; then ok "解压后与原目录 diff -r 无差异"; else ng "解压后与原目录有差异"; fi
chk "空目录被保留" "$([ -d "$OUT/子 层/空目录" ] && echo yes || echo no)" "yes"
chk "中文+空格文件名保留" "$([ -f "$OUT/中文 文件名.jpg" ] && echo yes || echo no)" "yes"
chk "源目录未被修改" "$([ -f "$R/源 目录/中文 文件名.jpg" ] && echo yes || echo no)" "yes"

echo "AC-02 gzip 往返"
setcfg src_dir=$T/src dst_dir=$T/dst compress=gzip
rm -rf $T/dst/*; rm -f $A/var/*.log
python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
chk "gzip 打包成功" "$r" "0"
chk "产物后缀 .tar.gz" "$(ls $T/dst/*.tar.gz 2>/dev/null | wc -l)" "1"
G=$(ls $T/dst/*.tar.gz | head -1)
chk "gzip 包可解压" "$(tar tzf "$G" >/dev/null 2>&1 && echo yes || echo no)" "yes"

echo "AC-02 zstd 往返（系统有 zstd 时）"
if command -v zstd >/dev/null 2>&1; then
  setcfg src_dir=$T/src dst_dir=$T/dst compress=zstd
  rm -rf $T/dst/*; rm -f $A/var/*.log
  python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
  chk "zstd 打包成功" "$r" "0"
  chk "产物后缀 .tar.zst" "$(ls $T/dst/*.tar.zst 2>/dev/null | wc -l)" "1"
  Z=$(ls $T/dst/*.tar.zst 2>/dev/null | head -1)
  chk "zstd 包可解压" "$(tar --zstd -tf "$Z" >/dev/null 2>&1 && echo yes || echo no)" "yes"
else
  echo "  [SKIP] 本机无 zstd"
fi

echo "AC-45 max_files 误指大目录保护"
setcfg src_dir=$T/src dst_dir=$T/dst compress=none max_files=100 dst_mountpoint=
rm -rf $T/dst/*; rm -f $A/var/*.log
python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
chk "文件数超限被拒" "$([ $r -ne 0 ] && echo yes || echo no)" "yes"
chk "超限时不产包" "$(ls $T/dst/*.tar 2>/dev/null | wc -l)" "0"
grep -q "超过上限 max_files" $A/var/task.log && ok "日志给出超限原因" || ng "日志未记录超限原因"
setcfg compress=none max_files=0

echo "AC-24 enable_cron=false 不执行"
setcfg src_dir=$T/src dst_dir=$T/dst compress=none enable_cron=false
rm -rf $T/dst/*; rm -f $A/var/*.log
python3 $A/lib/cron_run.py >/dev/null 2>&1; r=$?
chk "cron_run 开关关闭时退出0" "$r" "0"
chk "开关关闭时不产包" "$(ls $T/dst/*.tar 2>/dev/null | wc -l)" "0"

echo "AC-31 配置损坏不崩"
printf '{ 这不是合法 JSON' > $A/var/config.json
python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
chk "配置损坏时优雅失败(非 traceback 崩溃)" "$([ $r -ne 0 ] && echo yes || echo no)" "yes"
python3 -c "import sys;sys.path.insert(0,'$A/lib');import config;c=config.load();print('回退默认值:', c['keep_versions']==3 and c['cron_expr']=='0 3 * * *')"
setcfg src_dir=$T/src dst_dir=$T/dst compress=none enable_cron=true

echo "AC-41 写入范围约束（目标被换成普通文件）"
rm -rf $T/notadir; printf 'x' > $T/notadir
setcfg dst_dir=$T/notadir
python3 $A/lib/backup.py >/dev/null 2>&1; r=$?
chk "目标不是目录时拒绝" "$([ $r -ne 0 ] && echo yes || echo no)" "yes"
chk "未覆盖该文件" "$([ -f $T/notadir ] && echo yes || echo no)" "yes"
setcfg dst_dir=$T/dst

echo
echo "===== 通过 $pass / 失败 $fail ====="
exit $([ $fail -eq 0 ] && echo 0 || echo 1)
