#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""HTTP 通道自测（桌面入口实际走的就是这条）。

起一个 server.py 子进程，用标准库 urllib 打真实 HTTP，验证：
页面渲染、save/list/run/log/verify/purge/unlock 接口、GET 副作用拦截、UTF-8 往返。

前置：与 cgi_test.py 相同的沙箱（/tmp/pbtest），且 crontab 必须指向沙箱桩。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 45199
BASE = "http://127.0.0.1:%d" % PORT
APP_DIR = "/tmp/pbtest/app"
SERVER = os.path.join(APP_DIR, "server", "server.py")

_real = shutil.which("crontab") or ""
if not _real.startswith("/tmp/pbtest/bin/"):
    sys.exit("安全检查失败：crontab 未指向沙箱桩（实际 %r）。\n"
             "为了不污染真实 crontab，测试中止。请先 export PATH=/tmp/pbtest/bin:$PATH" % _real)

ENV = dict(os.environ, PHOTOS_BACKUP_APP_DIR=APP_DIR, TMPDIR="/tmp/pbtest/tmp",
           TRIM_DATA_ACCESSIBLE_PATHS="/tmp/pbtest")
ENV["PATH"] = "/tmp/pbtest/bin:" + ENV.get("PATH", "")

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  [PASS] %s %s" % (name, extra))
    else:
        fail += 1
        print("  [FAIL] %s %s" % (name, extra))


def call(path, method="GET", data=None):
    url = BASE + path
    body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def wait_ready(tries=40):
    for _ in range(tries):
        try:
            st, _h, _b = call("/?action=status")
            if st == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


proc = subprocess.Popen([sys.executable, SERVER, "--port", str(PORT), "--bind", "127.0.0.1"],
                        env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    if not wait_ready():
        print("服务未就绪，测试中止")
        sys.exit(1)

    print("HTTP-01 首页渲染")
    st, h, body = call("/")
    chk("状态 200", st == 200, str(st))
    chk("Content-Type text/html", "text/html" in h.get("Content-Type", ""))
    chk("含源目录输入框", 'name="src_dir"' in body)
    chk("含状态区", "上次成功" in body)
    chk("含清锁/清理按钮", "unlock" in body and "purge" in body)
    chk("页面标题正确", "备份打包" in body)

    print("HTTP-02 保存配置（含中文与特殊字符）")
    st, _h, body = call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src$KEEP", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "0 3 * * *", "enable_cron": "1",
        "allow_root_mount": "1", "max_files": "0",
    })
    chk("返回 ok", json.loads(body).get("status") == "ok", body[:60])
    _st, _h, page = call("/")
    chk("页面回显 $KEEP 原样", "/tmp/pbtest/src$KEEP" in page)

    st, _h, body = call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "99 99 99 99 99", "enable_cron": "1", "allow_root_mount": "1",
    })
    chk("非法 cron 被拒", json.loads(body).get("status") == "error", body[:60])

    st, _h, body = call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "15 2 * * *", "enable_cron": "1", "allow_root_mount": "1",
    })
    chk("合法保存 ok", json.loads(body).get("status") == "ok", body[:60])

    print("HTTP-03 副作用只接受 POST")
    st, _h, _b = call("/?action=run", "GET")
    chk("GET run → 405", st == 405, str(st))
    st, _h, _b = call("/?action=purge", "GET")
    chk("GET purge → 405", st == 405, str(st))

    print("HTTP-04 触发备份并出包")
    st, _h, body = call("/?action=run", "POST")
    chk("run 返回 ok", json.loads(body).get("status") == "ok", body[:60])
    time.sleep(4)
    st, _h, body = call("/?action=list")
    data = json.loads(body)
    chk("list 状态 ok", data.get("status") == "ok")
    chk("产生备份包", len(data.get("backups", [])) >= 1, str(len(data.get("backups", []))))
    if data.get("backups"):
        first = data["backups"][0]
        chk("含 quality 字段", first.get("quality") in ("complete", "approximate", "unknown", "invalid"),
            first.get("quality", ""))

    print("HTTP-05 校验与目录穿越")
    if data.get("backups"):
        name = data["backups"][0]["name"]
        st, _h, body = call("/?action=verify&name=" + urllib.parse.quote(name), "POST")
        chk("校验通过", json.loads(body).get("status") == "ok", body[:60])
        st, _h, body = call("/?action=verify&name=../../../etc/passwd", "POST")
        chk("穿越被拒", json.loads(body).get("status") == "error", body[:60])

    print("HTTP-06 日志与状态")
    st, h, body = call("/?action=log")
    chk("日志 text/plain", "text/plain" in h.get("Content-Type", ""))
    chk("日志非空", len(body.strip()) > 0, "%d 字节" % len(body))
    st, _h, body = call("/?action=status")
    d = json.loads(body)
    chk("status 含 lock", "lock" in d.get("state", {}))

    print("HTTP-07 清理无效产物")
    dst = "/tmp/pbtest/dst"
    os.makedirs(dst, exist_ok=True)
    stub = os.path.join(dst, "thumbnails_20991231_235959.tar")
    with open(stub, "wb") as fp:
        fp.write(b"x" * 512)
    st, _h, body = call("/?action=purge", "POST")
    chk("purge ok", json.loads(body).get("status") == "ok", body[:60])
    chk("无效产物被删", not os.path.exists(stub))

    print("HTTP-09 目录选择（roots / ls / 越界拒绝）")
    st, _h, body = call("/?action=roots")
    d = json.loads(body)
    chk("roots ok", d.get("status") == "ok", body[:80])
    chk("roots 含授权路径", any(r["path"].startswith("/tmp/pbtest") for r in d.get("roots", [])),
        str([r["path"] for r in d.get("roots", [])])[:80])
    st, _h, body = call("/?action=ls&path=" + urllib.parse.quote("/tmp/pbtest/src"))
    d = json.loads(body)
    chk("ls ok", d.get("status") == "ok", body[:80])
    names = [x["name"] for x in d.get("dirs", [])]
    chk("列出子目录", any(n.startswith("sub") for n in names), str(names[:5]))
    st, _h, body = call("/?action=ls&path=" + urllib.parse.quote("/etc"))
    chk("越界路径被拒", json.loads(body).get("status") == "error", body[:80])
    st, _h, body = call("/?action=ls&path=" + urllib.parse.quote("/tmp/pbtest/src/../../etc"))
    chk("穿越路径被拒", json.loads(body).get("status") == "error", body[:80])

    print("HTTP-09b fnOS SDK 静态资源（系统目录选择器依赖）")
    st, h, body = call("/assets/fnnas-sdk.js")
    chk("SDK 200", st == 200, str(st))
    chk("SDK 类型 js", "javascript" in h.get("Content-Type", ""), h.get("Content-Type", ""))
    chk("SDK 非空", len(body) > 10000, "%d 字节" % len(body))
    st, _h, _b = call("/assets/..%2findex.cgi")
    chk("静态目录不可穿越", st == 404, str(st))
    _st, _h, page = call("/")
    chk("页面引入 SDK", "fnnas-sdk.js" in page)
    chk("按钮走系统选择器", "pickDir(" in page)
    chk("保留内置浏览器回退", "openPicker(" in page)

    print("HTTP-10 定时控件（时间选择 + 表达式反解）")
    _st, _h, page = call("/")
    chk("页面含时间控件", 'type="time"' in page)
    chk("页面含目录选择按钮", "openPicker(" in page)
    chk("页面含频率下拉", 'id="freq"' in page)
    chk("页面无残留占位符", "$RUN_TIME" not in page and "$WD_JSON" not in page)
    call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "15 2 * * *", "enable_cron": "1", "allow_root_mount": "1",
    })
    _st, _h, page = call("/")
    chk("每天模式回显 02:15", 'value="02:15"' in page)
    chk("daily 选中", 'value="daily" selected' in page)
    call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "30 4 * * 1,3", "enable_cron": "1", "allow_root_mount": "1",
    })
    _st, _h, page = call("/")
    chk("每周模式回显 04:30", 'value="04:30"' in page)
    chk("weekly 选中", 'value="weekly" selected' in page)
    chk("周一周三预选（传给前端）", 'var WD_SEL = ["1", "3"]' in page,
        page[page.find("var WD_SEL"):page.find("var WD_SEL")+40])
    call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "0 3 15 * *", "enable_cron": "1", "allow_root_mount": "1",
    })
    _st, _h, page = call("/")
    chk("每月模式选中", 'value="monthly" selected' in page)
    chk("每月 15 日回显", "var DOM_SEL = 15;" in page,
        page[page.find("var DOM_SEL"):page.find("var DOM_SEL")+30])
    call("/?action=save", "POST", {
        "src_dir": "/tmp/pbtest/src", "dst_dir": "/tmp/pbtest/dst",
        "compress": "none", "keep_versions": "3", "log_retain_days": "7",
        "cron_expr": "*/15 2-6 * * 1", "enable_cron": "1", "allow_root_mount": "1",
    })
    _st, _h, page = call("/")
    chk("复杂表达式走高级模式", "高级：手动填写 cron 表达式" in page and 'id="adv" checked' in page)
    chk("高级模式保留原表达式", "*/15 2-6 * * 1" in page)

    print("HTTP-08 锁")
    lock = "/tmp/pbtest/app/var/backup.lock"
    with open(lock, "w") as fp:
        json.dump({"pid": 1, "task": "manual", "start": "x"}, fp)
    st, _h, body = call("/?action=unlock", "POST")
    chk("活锁拒绝清除", json.loads(body).get("status") == "error", body[:60])
    with open(lock, "w") as fp:
        json.dump({"pid": 999999, "task": "manual", "start": "x"}, fp)
    st, _h, body = call("/?action=unlock", "POST")
    chk("死锁可清除", json.loads(body).get("status") == "ok", body[:60])
    chk("锁文件消失", not os.path.exists(lock))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n===== 通过 %d / 失败 %d =====" % (ok, fail))
sys.exit(1 if fail else 0)
