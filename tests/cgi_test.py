#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CGI 层自测：模拟 fnOS 调用 index.cgi 的环境变量，做端到端检查。"""
import json
import os
import subprocess
import sys

CGI = "/tmp/pbtest/app/ui/index.cgi"
import shutil as _sh
_real = _sh.which("crontab") or ""
if not _real.startswith("/tmp/pbtest/bin/"):
    sys.exit("安全检查失败：crontab 未指向沙箱桩（实际 %r）。\n"
             "为了不污染真实 crontab，测试中止。请先 export PATH=/tmp/pbtest/bin:$PATH" % _real)

ENV = dict(os.environ, PHOTOS_BACKUP_APP_DIR="/tmp/pbtest/app",
           TMPDIR="/tmp/pbtest/tmp")
# 必须让 CGI 子进程使用沙箱里的 crontab 桩，否则会写真实 crontab
ENV["PATH"] = "/tmp/pbtest/bin:" + ENV.get("PATH", "")


def call(query="", method="GET", body=""):
    e = dict(ENV)
    e["REQUEST_METHOD"] = method
    e["QUERY_STRING"] = query
    if method == "POST":
        e["CONTENT_TYPE"] = "application/x-www-form-urlencoded"
        e["CONTENT_LENGTH"] = str(len(body.encode()))
    r = subprocess.run([sys.executable, CGI], input=body, capture_output=True,
                       text=True, env=e)
    return r.stdout, r.stderr, r.returncode


def split(out):
    if "\r\n\r\n" in out:
        head, _, rest = out.partition("\r\n\r\n")
    else:
        head, _, rest = out.partition("\n\n")
    return head, rest


def as_json(out):
    _, body = split(out)
    return json.loads(body)


ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s %s" % (name, extra))
    else:
        fail += 1
        print("  [FAIL] %s %s" % (name, extra))


print("AC-05 GET 首页渲染")
out, err, rc = call()
head, body = split(out)
chk("Content-Type 头", "text/html" in head)
chk("退出码 0", rc == 0, str(rc))
chk("页面含源目录输入框", 'name="src_dir"' in body)
chk("页面含状态区（上次成功）", "上次成功" in body)
chk("页面含清锁按钮", "unlock" in body)
chk("stderr 无 traceback", "Traceback" not in err, err[:80])

print("AC-42 占位符不二次替换（$ 语法）")
special = "/tmp/pbtest/src$KEEP"
os.makedirs(special, exist_ok=True)
call("action=save", "POST",
     "src_dir=%2Ftmp%2Fpbtest%2Fsrc%24KEEP&dst_dir=/tmp/pbtest/dst&"
     "compress=none&keep_versions=3&log_retain_days=7&"
     "cron_expr=0+3+%2A+%2A+%2A&enable_cron=1&allow_root_mount=1")
_, body2 = split(call()[0])
chk("目录名里的 $KEEP 原样显示", "/tmp/pbtest/src$KEEP" in body2, "（未被替换成数字）")
call("action=save", "POST",
     "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
     "keep_versions=3&log_retain_days=7&cron_expr=0+3+%2A+%2A+%2A&"
     "enable_cron=1&allow_root_mount=1")

print("AC-20 非法 cron 拒绝保存且不改 crontab")
before = open("/tmp/pbtest/crontab.txt", encoding="utf-8").read() if os.path.exists("/tmp/pbtest/crontab.txt") else ""
r = as_json(call("action=save", "POST",
                 "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
                 "keep_versions=3&log_retain_days=7&cron_expr=99+99+99+99+99&"
                 "enable_cron=1&allow_root_mount=1")[0])
after = open("/tmp/pbtest/crontab.txt", encoding="utf-8").read() if os.path.exists("/tmp/pbtest/crontab.txt") else ""
chk("返回 error", r["status"] == "error", r.get("message", "")[:40])
chk("crontab 未被改动", before == after)

print("AC-21 合法保存 → crontab 只有 1 条本应用行")
r = as_json(call("action=save", "POST",
                 "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
                 "keep_versions=3&log_retain_days=7&cron_expr=15+2+%2A+%2A+%2A&"
                 "enable_cron=1&allow_root_mount=1")[0])
chk("返回 ok", r["status"] == "ok", r.get("message", ""))
txt = open("/tmp/pbtest/crontab.txt", encoding="utf-8").read()
chk("行数=1", sum(1 for l in txt.splitlines() if "cron_run.py" in l) == 1)
chk("表达式为 15 2", "15 2 * * *" in txt)

print("AC-16 GET 触发 run 应被拒（405）")
head, body = split(call("action=run", "GET")[0])
chk("GET 不允许触发备份", "405" in head or "Method" in body, head.strip()[:40])

print("AC-06 POST run 可触发")
r = as_json(call("action=run", "POST")[0])
chk("返回 ok", r["status"] == "ok", r.get("message", "")[:40])

print("AC-04/18 列表四态")
import time
time.sleep(3)
r = as_json(call("action=list")[0])
chk("status ok", r["status"] == "ok")
if r.get("backups"):
    b = r["backups"][0]
    print("  首条:", b)
    chk("含 quality 字段", "quality" in b)
    chk("quality 取值合法", b["quality"] in ("complete", "approximate", "invalid", "unknown"))

print("AC-19/38 校验")
if r.get("backups"):
    name = r["backups"][0]["name"]
    v = as_json(call("action=verify&name=" + name, "POST")[0])
    chk("校验通过", v["status"] == "ok", v.get("message", "")[:40])
    v2 = as_json(call("action=verify&name=../../../etc/passwd", "POST")[0])
    chk("目录穿越被拒", v2["status"] == "error")

print("AC-33 日志尾读")
head, body = split(call("action=log")[0])
chk("返回 text/plain", "text/plain" in head)
chk("有内容", len(body.strip()) > 0, "%d 字节" % len(body))

print("AC-46 purge 必须 POST，且能清掉无效产物")
dst = "/tmp/pbtest/dst"
os.makedirs(dst, exist_ok=True)
stub = os.path.join(dst, "thumbnails_20991231_235959.tar")
with open(stub, "wb") as fp:
    fp.write(b"x" * 1024)
head, body = split(call("action=purge", "GET")[0])
chk("GET purge 被拒（405）", "405" in head or "Method" in body, head.strip()[:40])
r = as_json(call("action=purge", "POST")[0])
chk("purge 返回 ok", r["status"] == "ok", r.get("message", "")[:40])
chk("无效产物已删除", not os.path.exists(stub))

print("AC-45 max_files 保存与回显")
r = as_json(call("action=save", "POST",
                 "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
                 "keep_versions=3&log_retain_days=7&cron_expr=0+3+%2A+%2A+%2A&"
                 "enable_cron=1&allow_root_mount=1&max_files=1234")[0])
chk("保存 ok", r["status"] == "ok", r.get("message", "")[:40])
_, body3 = split(call()[0])
chk("页面回显 max_files", 'value="1234"' in body3)
r = as_json(call("action=save", "POST",
                 "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
                 "keep_versions=3&log_retain_days=7&cron_expr=0+3+%2A+%2A+%2A&"
                 "enable_cron=1&allow_root_mount=1&max_files=")[0])
chk("空 max_files 回退 0（不清空即失败）", r["status"] == "ok", r.get("message", "")[:40])
call("action=save", "POST",
     "src_dir=/tmp/pbtest/src&dst_dir=/tmp/pbtest/dst&compress=none&"
     "keep_versions=3&log_retain_days=7&cron_expr=0+3+%2A+%2A+%2A&"
     "enable_cron=1&allow_root_mount=1&max_files=0")

print("AC-09 锁状态与清除")
lock = "/tmp/pbtest/app/var/backup.lock"
json.dump({"pid": 1, "task": "manual", "start": "x", "start_epoch": 0}, open(lock, "w"))
r = as_json(call("action=unlock", "POST")[0])
chk("活锁拒绝清除", r["status"] == "error", r.get("message", "")[:30])
json.dump({"pid": 999999, "task": "manual", "start": "x", "start_epoch": 0}, open(lock, "w"))
r = as_json(call("action=unlock", "POST")[0])
chk("死锁可清除", r["status"] == "ok", r.get("message", "")[:30])
chk("锁文件已消失", not os.path.exists(lock))

print("\n===== 通过 %d / 失败 %d =====" % (ok, fail))
sys.exit(1 if fail else 0)
