#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""表现层：配置读写、任务触发、状态/日志/备份列表/校验。

FIX-11  模板改用 string.Template.safe_substitute，杜绝占位符二次替换污染
FIX-12  取日志从文件尾读，不再全量 readlines
FIX-15  去掉 cgi/cgitb 依赖，手动解析 QUERY_STRING 与 stdin（兼容 Python 3.13+）
FIX-16  「立即备份 / 清除锁」只接受 POST，GET 不得产生副作用
FIX-09  页面展示上次成功时间、距今天数、最近失败原因、锁持有者
FIX-01  备份列表区分 完整 / 近似快照 / 未知 / 残缺 四态
FIX-04  提供「清除残留锁」入口，配合领域层的失效判定
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from string import Template

# FIX-13：库路径不写死。真实形态是 <TRIM_APPDEST>/ui/index.cgi（fnOS 把 app/ui 的内容
# 放到 target/ui 下），源码树是 <root>/app/ui/index.cgi，两种都能靠"向上找 lib/config.py"
# 定位；PHOTOS_BACKUP_APP_DIR 只给离线自测用。
def _detect_app_dir():
    env = os.environ.get("PHOTOS_BACKUP_APP_DIR") or os.environ.get("TRIM_APPDEST")
    if env and os.path.isdir(env):
        return env
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        if os.path.exists(os.path.join(d, "lib", "config.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return "/var/apps/backup-pack/target"


_APP_DIR = _detect_app_dir()
sys.path.insert(0, os.path.join(_APP_DIR, "lib"))

# CGI 进程的 locale 往往是 C/POSIX，默认 stdout 编码是 ASCII——直接 print 中文 HTML
# 会抛 UnicodeEncodeError，浏览器只能拿到空响应。这里显式钉死 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# CGI 的 PATH 可能很短，补齐标准目录，保证 tar / sha256sum / crontab / zstd 可执行
_PATH = os.environ.get("PATH", "")
for _p in ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"):
    if _p not in _PATH.split(":"):
        _PATH = _p if not _PATH else _PATH + ":" + _p
os.environ["PATH"] = _PATH

import config
import backup

VERSION = "1.2.12"

QUALITY_LABEL = {"complete": "完整", "approximate": "近似快照",
                 "unknown": "未知", "invalid": "残缺"}


# ---------------------------------------------------------------- 请求解析
def parse_request():
    """FIX-15：不依赖 cgi 模块。"""
    params = {}
    # 某些网关会在 QUERY_STRING 前面带上 "?"，这里做容错剥离
    qs = (os.environ.get("QUERY_STRING") or "").lstrip("?")
    if qs:
        for k, v in urllib.parse.parse_qs(qs, keep_blank_values=True).items():
            params[k] = v[-1]
    method = (os.environ.get("REQUEST_METHOD") or "GET").upper()
    if method == "POST":
        try:
            length = int(os.environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = sys.stdin.read(length) if length > 0 else ""
        ctype = os.environ.get("CONTENT_TYPE", "")
        if "application/x-www-form-urlencoded" in ctype and body:
            for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items():
                params[k] = v[-1]
    return method, params


_REASON = {200: "OK", 400: "Bad Request", 405: "Method Not Allowed",
           500: "Internal Server Error"}


def send(status, headers, body):
    """按 CGI/HTTP 规范输出：状态行（非 200 时）+ 头部 CRLF + 空行 + 体。"""
    lines = ["%s: %s" % (k, v) for k, v in headers]
    if status != 200:
        lines.insert(0, "Status: %d %s" % (status, _REASON.get(status, "Error")))
    sys.stdout.write("\r\n".join(lines) + "\r\n\r\n" + body)
    sys.stdout.flush()


def _json(status, obj):
    """路由辅助：返回 (status, headers, body)，不碰 stdout。"""
    return status, [("Content-Type", "application/json; charset=utf-8"),
                    ("Cache-Control", "no-store")], json.dumps(obj, ensure_ascii=False)


def _text(body, status=200):
    return status, [("Content-Type", "text/plain; charset=utf-8"),
                    ("Cache-Control", "no-store")], body


def _html(body, status=200):
    return status, [("Content-Type", "text/html; charset=utf-8"),
                    ("Cache-Control", "no-store")], body


# ---------------------------------------------------------------- 业务动作
def run_once():
    state = backup.inspect_lock()
    if state["held"] and not state["stale"]:
        info = state["info"]
        return {"status": "error",
                "message": "任务正在运行中（pid=%s，已运行 %d 分钟）"
                           % (info.get("pid", "?"), state["age"] // 60)}
    if state["held"] and state["stale"]:            # FIX-04 自愈，无需用户手工清理
        backup.release_lock(force=True)
        backup.log("页面触发时自动回收失效锁（pid=%s）" % state["info"].get("pid"), "WARN")

    cfg = config.normalize(config.load())
    ok, errors = config.validate(cfg)
    if not ok:
        return {"status": "error", "message": "；".join(errors)}

    os.makedirs(config.VAR_DIR, exist_ok=True)
    log_fh = open(config.LOG_PATH, "a", encoding="utf-8", errors="replace")
    try:
        subprocess.Popen([sys.executable, config.BACKUP_PY, "--manual"],
                         stdout=log_fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    finally:
        log_fh.close()                              # 子进程已继承 fd，父进程不再持有
    return {"status": "ok", "message": "备份任务已启动，可点「刷新日志」查看进度"}


def unlock():
    state = backup.inspect_lock()
    if not state["held"]:
        return {"status": "ok", "message": "当前没有锁"}
    if not state["stale"]:
        return {"status": "error", "message": "任务确实在运行（pid=%s），拒绝清除"
                                             % state["info"].get("pid")}
    backup.release_lock(force=True)
    backup.log("页面手动清除残留锁（pid=%s）" % state["info"].get("pid"), "WARN")
    return {"status": "ok", "message": "残留锁已清除"}


def purge_invalid():
    """FIX-01 / FR-10：无校验文件的产物不自动删，由用户显式确认后回收空间。"""
    cfg = config.normalize(config.load())
    dst = (cfg.get("dst_dir") or "").strip()
    if not dst or not os.path.isdir(dst):
        return {"status": "error", "message": "目标目录无效"}
    try:
        n = backup.purge_invalid(dst)
    except OSError as e:
        return {"status": "error", "message": "清理失败：%s" % e}
    if not n:
        return {"status": "ok", "message": "没有需要清理的无效产物"}
    return {"status": "ok", "message": "已清理 %d 个无效产物（缺校验文件）" % n}


def list_backups():
    cfg = config.normalize(config.load())
    dst = (cfg.get("dst_dir") or "").strip()
    if not dst or not os.path.isdir(dst):
        return {"status": "error", "message": "目标目录无效"}
    valid, residue = backup.collect_packages(dst)
    st = config.load_status()
    out = []
    for f in sorted(valid + residue, reverse=True):
        try:
            info = os.stat(f)
        except OSError:
            continue
        has_sha = os.path.exists(f + ".sha256")
        if not has_sha:
            quality = "invalid"
        elif st.get("last_package") == os.path.basename(f):
            quality = st.get("last_quality") or "unknown"
        else:
            quality = "unknown"
        out.append({"name": os.path.basename(f), "size": info.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.st_mtime)),
                    "quality": quality, "label": QUALITY_LABEL.get(quality, quality),
                    "has_sha256": has_sha})
    return {"status": "ok", "backups": out, "dst": dst}


def verify_backup(name):
    cfg = config.normalize(config.load())
    dst = (cfg.get("dst_dir") or "").strip()
    if not dst:
        return {"status": "error", "message": "目标目录未配置"}
    base = os.path.basename(name or "")            # FIX-08 只认 basename
    if not base.startswith("thumbnails_") or base != name:
        return {"status": "error", "message": "非法文件名"}
    path = os.path.join(dst, base)
    if not os.path.exists(path) or not os.path.exists(path + ".sha256"):
        return {"status": "error", "message": "备份包或校验文件不存在"}
    try:
        ok, detail = backup.verify(path)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "校验超时（包过大），请改用命令行校验"}
    return {"status": "ok" if ok else "error",
            "message": ("文件读回一致（sha256 通过）。注意：这只能证明文件未损坏，"
                        "不代表内容完整可恢复，请配合恢复演练。" if ok
                        else "校验失败：" + detail)}


def tail_log(max_lines=200, max_bytes=256 * 1024):
    """FIX-12：从文件尾读取，不把整个日志读进内存。"""
    path = config.LOG_PATH
    if not os.path.exists(path):
        return "暂无日志"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError as e:
        return "读取日志失败：%s" % e
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]                           # 丢弃被截断的首行
    return "\n".join(lines[-max_lines:]) or "暂无日志"


# ---------------------------------------------------------------- 模板
TPL = Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="app-base" content="$BASE">
<title>备份打包</title>
<style>
body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#222}
h2{margin:0 0 4px}
.sub{color:#888;font-size:13px;margin-bottom:18px}
.card{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin-bottom:16px}
.card h3{margin:0 0 10px;font-size:15px}
label{display:block;margin:8px 0 2px;font-size:13px;color:#555}
input[type=text],input[type=number],select{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid #ccc;border-radius:5px;font-size:13px}
button{padding:7px 16px;margin:6px 8px 0 0;cursor:pointer;border:1px solid #ccc;background:#fafafa;border-radius:5px;font-size:13px}
button.primary{background:#1a73e8;color:#fff;border-color:#1a73e8}
button.pick{margin:0;white-space:nowrap;padding:6px 12px}
pre{background:#f6f7f9;padding:12px;border-radius:6px;max-height:280px;overflow:auto;font-size:12px;line-height:1.5}
.hint{color:#b00;font-size:12px}
.hint2{color:#888;font-size:12px}
.ok{color:#0a7a35}.warn{color:#b06a00}.err{color:#b00020}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;font-size:13px}
.k{color:#777}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid #eee;white-space:nowrap}
.pathrow{display:flex;gap:8px;align-items:center}
.pathrow input{flex:1;min-width:0}
.cronrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cronrow select{width:auto}
.cronrow input[type=time]{width:auto;padding:6px 8px;border:1px solid #ccc;border-radius:5px;font-size:13px}
.wd{display:inline-flex;align-items:center;gap:3px;margin:0 8px 0 0;font-size:12px;color:#444}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:99}
.modal-box{background:#fff;border-radius:10px;width:min(700px,94vw);max-height:82vh;display:flex;flex-direction:column;box-shadow:0 8px 30px rgba(0,0,0,.25)}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #eee}
.modal-head button{margin:0;padding:2px 10px}
.modal-bar{display:flex;gap:8px;align-items:center;padding:8px 16px;border-bottom:1px solid #f0f0f0;font-size:12px;flex-wrap:wrap}
.modal-bar button{margin:0;padding:4px 10px;font-size:12px}
.modal-list{overflow:auto;padding:8px 16px;flex:1;min-height:200px}
.item{padding:7px 8px;border-radius:5px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;align-items:center}
.item:hover{background:#f2f6fd}
.item .tag{color:#999;font-size:11px}
.modal-foot{display:flex;gap:8px;justify-content:flex-end;align-items:center;padding:10px 16px;border-top:1px solid #eee}
.modal-foot .hint2{margin-right:auto}
</style>
</head>
<body>
<h2>备份打包</h2>
<div class="sub">版本 $VERSION ｜ 把大量小文件目录（如相册缩略图）打成单个备份包 ｜ 源目录只读，不会删除源文件</div>

<div id="diag" style="display:none;background:#fff4f4;border:1px solid #f3c2c2;color:#b00020;border-radius:6px;padding:8px 12px;font-size:12px;margin-bottom:14px;line-height:1.6"></div>

<div class="card">
<h3>运行状态</h3>
<div class="grid">$STATUS</div>
</div>

<form method="post" action="?" id="cfg">
<div class="card">
<h3>配置</h3>
<label>源目录（相册缩略图目录；需在「应用设置 → 授权目录」里授予访问权限）</label>
<div class="pathrow">
<input type="text" id="src_dir" name="src_dir" value="$SRC" placeholder="/vol1/1000/.../thumbnail">
<button type="button" class="pick" onclick="pickDir('src_dir','user')">选择…</button>
</div>
<label>目标目录（建议不同物理盘；同样需要在授权目录内）</label>
<div class="pathrow">
<input type="text" id="dst_dir" name="dst_dir" value="$DST" placeholder="/vol2/backup/photos">
<button type="button" class="pick" onclick="pickDir('dst_dir','shared')">选择…</button>
</div>
<label>压缩方式</label>
<select name="compress">
<option value="none"$SEL_NONE>不压缩（最快，推荐）</option>
<option value="gzip"$SEL_GZIP>gzip</option>
<option value="zstd"$SEL_ZSTD>zstd $ZSTD_HINT</option>
</select>
<div class="cronrow" style="margin-top:10px">
<div>
<label>保留份数（1-90）</label>
<input type="number" name="keep_versions" value="$KEEP" min="1" max="90">
</div>
<div>
<label>日志保留天数（1-365）</label>
<input type="number" name="log_retain_days" value="$RETAIN" min="1" max="365">
</div>
<div>
<label>空间安全余量（0-0.5）</label>
<input type="number" name="min_free_ratio" value="$MARGIN" step="0.01" min="0" max="0.5">
</div>
</div>
<label>源文件数上限（0=不限制；防止误把整个卷当缩略图目录打包）</label>
<input type="number" name="max_files" value="$MAXFILES" min="0" step="1000">

<label style="margin-top:14px">定时执行</label>
<div class="cronrow">
<select id="freq">
<option value="daily"$FREQ_DAILY>每天</option>
<option value="weekly"$FREQ_WEEKLY>每周</option>
<option value="monthly"$FREQ_MONTHLY>每月</option>
</select>
<span id="freq-extra"></span>
<input type="time" id="run_time" value="$RUN_TIME">
<span class="hint2">（NAS 本地时间）</span>
</div>
<div class="hint2" id="cron-desc" style="margin-top:4px">$CRON_DESC</div>
<label style="margin-top:8px"><input type="checkbox" id="adv"$ADV_CHK> 高级：手动填写 cron 表达式</label>
<div id="adv-box" style="display:none;margin-top:4px">
<input type="text" id="cron_raw" value="$CRON" placeholder="0 3 * * *">
<div class="hint2">分 时 日 月 周。支持 */n、a-b、a,b、MON-FRI。</div>
</div>
<input type="hidden" name="cron_expr" id="cron_expr" value="$CRON">

<label style="margin-top:10px"><input type="checkbox" name="enable_cron" value="1"$CHK> 启用定时任务</label>
<label><input type="checkbox" name="allow_root_mount" value="1"$ALLOWROOT> 允许写入系统盘（默认禁止，防止外接盘掉线时写满系统盘）</label>
<button type="submit" class="primary">保存配置</button>
<button type="button" onclick="post('run')">立即备份</button>
<button type="button" onclick="post('unlock')">清除残留锁</button>
<button type="button" onclick="post('purge')">清理无效产物</button>
</div>
</form>

<div class="card">
<h3>任务日志（尾部 200 行）</h3>
<button type="button" onclick="loadLog()">刷新日志</button>
<pre id="log">点击「刷新日志」查看</pre>
</div>

<div class="card">
<h3>备份列表</h3>
<button type="button" onclick="loadList()">刷新列表</button>
<pre id="list">点击「刷新列表」查看</pre>
</div>

<div class="modal" id="picker" style="display:none">
<div class="modal-box">
<div class="modal-head"><strong>选择目录</strong><button type="button" onclick="closePicker()">×</button></div>
<div class="modal-bar">
<button type="button" onclick="pkUp()">↑ 上一级</button>
<button type="button" onclick="pkRoots()">⌂ 起始位置</button>
<span id="pk-path" style="word-break:break-all"></span>
</div>
<div class="modal-list" id="pk-list"></div>
<div class="modal-foot">
<span class="hint2" id="pk-msg">只列出当前账号可访问的目录</span>
<button type="button" class="primary" onclick="pkChoose()">选此目录</button>
<button type="button" onclick="closePicker()">取消</button>
</div>
</div>
</div>

<script>
var PK = { target: null, path: "", roots: [] };

/* 前端打点：写进服务端 task.log，排障时按 [CLIENT] 过滤 */
function report(stage, msg){
  try{
    var q = "?action=diag&stage=" + encodeURIComponent(stage) + "&msg=" + encodeURIComponent(String(msg == null ? "" : msg).slice(0, 500));
    if(navigator.sendBeacon){ navigator.sendBeacon(q); }
    else { fetch(q, {method:"POST", keepalive:true}); }
  }catch(e){}
}
function diagShow(text){
  var el = document.getElementById("diag");
  if(!el) return;
  el.style.display = "block";
  el.textContent = text;
}
report("boot", JSON.stringify({
  href: location.href,
  origin: location.origin,
  inIframe: window.parent !== window,
  parentReadable: (function(){ try { window.parent.location.href; return true; } catch(e) { return false; } })(),
  ua: (navigator.userAgent || "").slice(0, 100)
}));

function post(action){
  fetch("?action="+action,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"}})
    .then(function(r){return r.json()})
    .then(function(d){alert(d.status+"\uFF1A"+d.message);if(d.status==="ok"){loadLog();}})
    .catch(function(e){alert("\u8BF7\u6C42\u5931\u8D25\uFF1A"+e)});
}
function loadLog(){
  fetch("?action=log").then(function(r){return r.text()})
    .then(function(t){document.getElementById("log").textContent=t});
}
function loadList(){
  fetch("?action=list").then(function(r){return r.json()}).then(function(d){
    var el=document.getElementById("list");
    if(d.status!=="ok"){el.textContent=d.message;return}
    if(!d.backups.length){el.textContent="\u76EE\u6807\u76EE\u5F55\u4E0B\u6682\u65E0\u5907\u4EFD\u5305";return}
    el.textContent=d.backups.map(function(b){
      return b.name+"  "+b.mtime+"  "+(b.size/1048576).toFixed(1)+"MB  ["+b.label+"]"
    }).join("\\n");
  });
}

/* ---------------- 目录选择：系统选择器优先 ---------------- */
async function pickDir(target, kind){
  report("pick-click", target + "/" + (kind || ""));
  if(typeof window.pickWithSystem === "function"){
    try {
      if(await window.pickWithSystem(target, kind || "user")) return;
    } catch(e){
      report("pick-error", (e && e.message) ? e.message : String(e));
    }
  } else {
    report("sdk-missing", window.__sdkLoadError || "pickWithSystem 未注册");
  }
  openPicker(target);
}

function openPicker(target){
  PK.target = target;
  var cur = document.getElementById(target).value.trim();
  document.getElementById("picker").style.display = "flex";
  pkRoots(cur || null);
}
function closePicker(){
  document.getElementById("picker").style.display = "none";
  PK.target = null;
}
function pkMsg(t){ document.getElementById("pk-msg").textContent = t; }
function pkRoots(startAt){
  fetch("?action=roots").then(function(r){return r.json()}).then(function(d){
    if(d.status!=="ok"){ pkMsg(d.message||"\u83B7\u53D6\u8D77\u59CB\u4F4D\u7F6E\u5931\u8D25"); return; }
    PK.roots = d.roots || [];
    var html = "", i;
    for(i=0;i<PK.roots.length;i++){
      var r = PK.roots[i];
      html += '<div class="item" data-path="'+esc(r.path)+'"><span>📁 '+esc(r.label||r.path)+'</span><span class="tag">'+esc(r.note||"")+'</span></div>';
    }
    if(!html){ html = '<div class="hint2" style="padding:12px 0">\u6CA1\u6709\u53EF\u8BBF\u95EE\u7684\u8D77\u59CB\u4F4D\u7F6E\u3002\u8BF7\u5148\u5230\u300C\u5E94\u7528\u8BBE\u7F6E \u2192 \u6388\u6743\u76EE\u5F55\u300D\u91CC\u6DFB\u52A0\u76EE\u5F55\u3002</div>'; }
    document.getElementById("pk-list").innerHTML = html;
    document.getElementById("pk-path").textContent = "\u8BF7\u9009\u62E9\u8D77\u59CB\u4F4D\u7F6E";
    pkMsg("\u9009\u62E9\u4E00\u4E2A\u8D77\u59CB\u4F4D\u7F6E\u540E\u9010\u7EA7\u8FDB\u5165");
    bindItems();
    if(startAt){ pkLoad(startAt); }
  });
}
function pkLoad(path){
  PK.path = path;
  document.getElementById("pk-path").textContent = path;
  fetch("?action=ls&path="+encodeURIComponent(path)).then(function(r){return r.json()}).then(function(d){
    var el = document.getElementById("pk-list");
    if(d.status!=="ok"){ el.innerHTML = '<div class="hint2" style="padding:12px 0">'+esc(d.message||"\u8BFB\u53D6\u5931\u8D25")+'</div>'; return; }
    var html = "", i;
    var parts = (d.path||path).split("/");
    var parent = parts.slice(0, -1).join("/") || "/";
    html += '<div class="item" data-path="'+esc(parent)+'"><span>\u2B06 .. \uFF08\u4E0A\u4E00\u7EA7\uFF09</span><span class="tag"></span></div>';
    for(i=0;i<(d.dirs||[]).length;i++){
      var it = d.dirs[i];
      html += '<div class="item" data-path="'+esc(it.path)+'"><span>📁 '+esc(it.name)+'</span><span class="tag">'+(it.denied?"\u65E0\u6743\u9650":'')+'</span></div>';
    }
    if((d.dirs||[]).length === 0){
      html += '<div class="hint2" style="padding:10px 0">\u8FD9\u91CC\u6CA1\u6709\u5B50\u76EE\u5F55\uFF0C\u53EF\u4EE5\u76F4\u63A5\u9009\u5F53\u524D\u76EE\u5F55</div>';
    }
    el.innerHTML = html;
    pkMsg(d.note || "\u70B9\u51FB\u76EE\u5F55\u8FDB\u5165\uFF0C\u6216\u76F4\u63A5\u300C\u9009\u6B64\u76EE\u5F55\u300D");
    bindItems();
  });
}
function bindItems(){
  var items = document.querySelectorAll("#pk-list .item");
  for(var i=0;i<items.length;i++){
    items[i].onclick = function(){ pkLoad(this.getAttribute("data-path")); };
  }
}
function pkUp(){
  if(!PK.path){ pkRoots(); return; }
  var parts = PK.path.split("/");
  var parent = parts.slice(0, -1).join("/") || "/";
  pkLoad(parent);
}
function pkChoose(){
  if(!PK.target) return;
  if(!PK.path){ pkMsg("\u8BF7\u5148\u8FDB\u5165\u4E00\u4E2A\u76EE\u5F55"); return; }
  document.getElementById(PK.target).value = PK.path;
  closePicker();
}
function esc(s){
  return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/* ---------------- 定时控件 ---------------- */
var WD = [["0","\u5468\u65E5"],["1","\u5468\u4E00"],["2","\u5468\u4E8C"],["3","\u5468\u4E09"],["4","\u5468\u56DB"],["5","\u5468\u4E94"],["6","\u5468\u516D"]];
var WD_SEL = $WD_JSON;
var DOM_SEL = $DOM_VALUE;

function renderFreqExtra(){
  var f = document.getElementById("freq").value, box = document.getElementById("freq-extra"), i, html="";
  if(f === "weekly"){
    for(i=0;i<WD.length;i++){
      var on = WD_SEL.indexOf(WD[i][0]) >= 0 ? " checked" : "";
      html += '<label class="wd"><input type="checkbox" class="wdbox" value="'+WD[i][0]+'"'+on+'>'+WD[i][1]+'</label>';
    }
  } else if(f === "monthly"){
    html = '<select id="dom" style="width:auto">';
    for(i=1;i<=28;i++){ html += '<option value="'+i+'"'+(String(DOM_SEL)===String(i)?" selected":"")+'>'+i+' \u65E5</option>'; }
    html += '</select>';
  }
  box.innerHTML = html;
  updateCronDesc();
}
function buildCron(){
  if(document.getElementById("adv").checked){
    return document.getElementById("cron_raw").value.trim();
  }
  var t = document.getElementById("run_time").value || "03:00";
  var p = t.split(":"), h = parseInt(p[0],10), m = parseInt(p[1],10);
  var f = document.getElementById("freq").value;
  if(f === "weekly"){
    var ds = [], boxes = document.querySelectorAll(".wdbox");
    for(var i=0;i<boxes.length;i++){ if(boxes[i].checked) ds.push(boxes[i].value); }
    if(!ds.length) ds = ["0"];
    return m+" "+h+" * * "+ds.join(",");
  }
  if(f === "monthly"){
    var sel = document.getElementById("dom");
    return m+" "+h+" "+(sel?sel.value:1)+" * *";
  }
  return m+" "+h+" * * *";
}
function updateCronDesc(){
  var expr = buildCron();
  var el = document.getElementById("cron-desc");
  var adv = document.getElementById("adv").checked;
  if(adv){ el.textContent = expr ? ("\u5C06\u4F7F\u7528\uFF1A"+expr) : "\u8BF7\u586B\u5199 cron \u8868\u8FBE\u5F0F"; return; }
  el.textContent = "\u5C06\u4F7F\u7528\uFF1A" + expr + "\uFF08" + describeCron(expr) + "\uFF09";
}
function describeCron(expr){
  var p = expr.split(" "), mm = parseInt(p[0],10), hh = parseInt(p[1],10);
  if(isNaN(mm) || isNaN(hh)) return "\u81EA\u5B9A\u4E49\u8868\u8FBE\u5F0F";
  var t = (hh<10?"0":"")+hh+":"+(mm<10?"0":"")+mm;
  if(p[2]==="*" && p[3]==="*" && p[4]==="*") return "\u6BCF\u5929 "+t;
  if(p[2]==="*" && p[3]==="*"){
    var names = [], ds = p[4].split(",");
    for(var i=0;i<ds.length;i++){
      for(var j=0;j<WD.length;j++){ if(WD[j][0]===ds[i]) names.push(WD[j][1]); }
    }
    return "\u6BCF\u5468 "+(names.join("\u3001")||p[4])+" "+t;
  }
  if(p[3]==="*" && p[4]==="*") return "\u6BCF\u6708 "+p[2]+" \u65E5 "+t;
  return "\u81EA\u5B9A\u4E49\u8868\u8FBE\u5F0F";
}
function syncAdv(){
  var adv = document.getElementById("adv").checked;
  document.getElementById("adv-box").style.display = adv ? "block" : "none";
  updateCronDesc();
}
document.getElementById("freq").onchange = function(){ WD_SEL = []; renderFreqExtra(); };
document.getElementById("run_time").onchange = updateCronDesc;
document.getElementById("adv").onchange = syncAdv;
document.getElementById("cron_raw").oninput = updateCronDesc;
document.getElementById("cfg").onsubmit = function(){
  document.getElementById("cron_expr").value = buildCron();
  return true;
};
renderFreqExtra();
syncAdv();
</script>
<script type="module">
/* ============ fnOS 原生目录选择器接入 ============
   1. 必须用绝对路径 import：ui/config 的 url 是 /app/backup-pack（无尾斜杠），
      相对路径 "./assets/x.js" 会被解析成 /app/assets/x.js，服务端回 HTML → MIME 错误。
   2. SDK 的 ready() 内部无超时，父窗口不回 SynAck 会永久 pending，外层必须加超时。
   3. 方法降级链：源目录 pickUserFile → 目标目录 pickSharedFile → 兜底 pickFile(directory)。
   4. 选到路径 != 有权限：再调 authorize*，并用服务端 probe 实测 os.access。
================================================ */
const BASE = (document.querySelector('meta[name="app-base"]') || {}).content || "/app/backup-pack";
const VERSION = "$VERSION";
const SDK_URL = BASE + "/assets/fnnas-sdk.js?v=" + encodeURIComponent(VERSION);

const T_IMPORT = 8000, T_READY = 4000, T_PICK = 180000, T_AUTH = 60000;
/* 侧栏分组（fnOS 枚举）：myFiles / team / otherShare / storage / external / remote / favorites。
   storage = 「存储空间」，管理员视角下才出现，是唯一能看到 /vol1、/vol2 以及
   @appdata（相册缩略图）、_sysbackup（系统备份）这类系统目录的分组。
   之前漏了 storage，导致管理员也选不到相册与备份所在的位置。 */
const SIDEBAR_ALL = ["myFiles", "team", "otherShare", "storage", "external", "remote"];

const S = { phase: "idle", app: null, appPromise: null, brokenUntil: 0 };
const el = function (id) { return document.getElementById(id); };

function mreport(stage, msg) {
  try {
    const q = BASE + "/?action=diag&v=" + encodeURIComponent(VERSION)
            + "&stage=" + encodeURIComponent(stage)
            + "&msg=" + encodeURIComponent(String(msg == null ? "" : msg).slice(0, 500));
    if (navigator.sendBeacon) navigator.sendBeacon(q);
    else fetch(q, { method: "POST", keepalive: true });
  } catch (e) {}
}
function errText(e) { return (e && (e.message || e.toString())) || "未知错误"; }
function withTimeout(p, ms, tag) {
  let timer;
  const guard = new Promise(function (_, rej) {
    timer = setTimeout(function () {
      rej(Object.assign(new Error(tag + " 超时(" + ms + "ms)"), { code: "timeout" }));
    }, ms);
  });
  return Promise.race([p, guard]).finally(function () { clearTimeout(timer); });
}
function diagShow(text) { const d = el("diag"); if (d) { d.style.display = "block"; d.textContent = text; } }
function diagClear() { const d = el("diag"); if (d) d.style.display = "none"; }

mreport("boot", JSON.stringify({
  v: VERSION, href: location.href, origin: location.origin,
  inIframe: window.parent !== window,
  parentReadable: (function () { try { window.parent.location.href; return true; } catch (e) { return false; } })(),
  ua: (navigator.userAgent || "").slice(0, 100)
}));

async function loadSdk() {
  const mod = await withTimeout(import(SDK_URL), T_IMPORT, "SDK 加载");
  if (!mod || typeof mod.s !== "function") throw new Error("SDK 导出缺少 s");
  return mod;
}
function getApp() {
  if (Date.now() < S.brokenUntil) return Promise.reject(new Error("SDK 刚失败过，稍后重试"));
  if (!S.appPromise) {
    S.appPromise = (async function () {
      mreport("sdk-import-start", SDK_URL);
      const mod = await loadSdk();
      mreport("sdk-import-ok", Object.keys(mod).join(","));
      const app = new mod.s({ debug: true });
      await withTimeout(app.ready(), T_READY, "SDK 握手");
      S.app = app;
      mreport("sdk-ready-ok", "standalone=" + app.isStandaloneWeb);
      return app;
    })().catch(function (e) {
      S.appPromise = null;
      S.brokenUntil = Date.now() + 15000;
      mreport("sdk-init-fail", errText(e));
      throw e;
    });
  }
  return S.appPromise;
}
getApp().catch(function () {});

/* 取消判定：覆盖三种方法的全部已知形态（RAWArchiver 实测：{code:-1,msg:'Cancelled',data:[]}） */
function isCancelResp(resp) {
  if (resp == null) return false;
  if (Array.isArray(resp)) return resp.length === 0;
  if (typeof resp !== "object") return false;

  if (resp.code === 0) {
    const d = Array.isArray(resp.data) ? resp.data : (resp.data ? [resp.data] : []);
    return d.filter(function (x) { return typeof x === "string" && x.indexOf("/") === 0; }).length === 0;
  }
  const m = String(resp.msg == null ? "" : resp.msg);
  // code:-1 的语义是「失败」，取消只是其中一种：宿主取消时 msg 为 Cancelled，
  // 而「应用鉴权异常」「Operation failed」这类必须按错误处理，否则会被静默吞掉。
  if (resp.code === -1) return /cancel|取消|abort|dismiss|close/i.test(m);
  if (Array.isArray(resp.data) && resp.data.length === 0) return true;
  if (!m) return true;
  return /cancel|取消|abort|dismiss|close/i.test(m);
}

function normalizePick(resp) {
  if (resp == null) return { ok: false, code: "empty", msg: "宿主没有返回结果" };

  if (Array.isArray(resp)) {
    const p = resp.filter(function (x) { return typeof x === "string" && x.indexOf("/") === 0; });
    return p.length ? { ok: true, paths: p } : { ok: false, code: "cancel", msg: "已取消" };
  }
  if (typeof resp === "string") {
    return resp.indexOf("/") === 0
      ? { ok: true, paths: [resp] }
      : { ok: false, code: "shape", msg: resp };
  }
  if (typeof resp === "object") {
    if (resp.code === 0) {
      const arr = Array.isArray(resp.data) ? resp.data : (resp.data ? [resp.data] : []);
      const p = arr.filter(function (x) { return typeof x === "string" && x.indexOf("/") === 0; });
      return p.length ? { ok: true, paths: p } : { ok: false, code: "cancel", msg: "已取消" };
    }
    if (isCancelResp(resp)) return { ok: false, code: "cancel", msg: resp.msg || "已取消" };
    return { ok: false, code: "bridge", msg: resp.msg || "目录选择失败" };
  }
  return { ok: false, code: "shape", msg: "无法识别的返回结构" };
}

function availableMethods(app) {
  try { return Object.keys(app.getWebMethods() || {}); } catch (e) { return []; }
}
function callPick(app, name, opts) {
  if (typeof app[name] !== "function") return Promise.reject(new Error(name + " 不存在"));
  return withTimeout(app[name](opts), T_PICK, name).then(normalizePick);
}
const RE_BRIDGE_DEAD = /Host bridge is not available|SDK 握手|SDK 加载|无法初始化飞牛开放平台|sdk-unavailable/i;
const RE_METHOD_GONE = /NotSupportedInExtensionHost|is not a function|不存在/i;

async function pickWithFallback(app, kind, userOpts, sharedOpts) {
  const has = availableMethods(app);
  mreport("methods", has.join(","));

  const chain = kind === "user"
    ? [["pickUserFile", userOpts],
       ["pickFile", { title: userOpts.title, okText: "选择", directory: true, disabledPaths: [] }],
       ["pickSharedFile", sharedOpts]]
    : [["pickSharedFile", sharedOpts],
       ["pickFile", { title: sharedOpts.title, okText: "选择", directory: true, disabledPaths: [] }],
       ["pickUserFile", userOpts]];

  let lastMsg = "";
  let tried = 0;
  let forcePickFile = false;      // 扩展宿主分支：只有 pickFile 是真实现

  for (let i = 0; i < chain.length; i++) {
    const name = chain[i][0], opts = chain[i][1];

    if (forcePickFile && name !== "pickFile") { mreport("skip-method", name + "(force)"); continue; }
    if (has.length && has.indexOf(name) === -1) { mreport("skip-method", name); continue; }

    tried++;
    try {
      mreport("call", name);
      const r = await callPick(app, name, opts);

      if (r.ok) return { status: "ok", paths: r.paths, method: name };

      if (r.code === "cancel") {                       // 取消：终止整条链
        mreport("pick-cancel", name + " || " + JSON.stringify(r).slice(0, 200));
        return { status: "cancel", method: name, msg: r.msg };
      }

      lastMsg = name + "：" + r.msg;
      if (r.code === "shape" || r.code === "empty") return { status: "error", method: name, msg: lastMsg };
    } catch (e) {
      const msg = errText(e);
      mreport("call-fail", name + " :: " + msg);

      if (RE_BRIDGE_DEAD.test(msg)) return { status: "unavailable", method: name, msg: msg };
      if (RE_METHOD_GONE.test(msg)) {
        lastMsg = name + "：" + msg;
        if (name !== "pickFile") { forcePickFile = true; continue; }
        return { status: "unavailable", method: name, msg: msg };
      }
      if (e && e.code === "timeout") { lastMsg = name + "：" + msg; continue; }
      return { status: "error", method: name, msg: msg };
    }
  }

  if (!tried) return { status: "unavailable", msg: "宿主没有暴露任何目录选择方法" };
  return { status: "error", msg: lastMsg || "目录选择失败" };
}

async function tryAuthorize(app, path, shared) {
  const name = shared ? "authorizeSharedFile" : "authorizeUserFile";
  if (typeof app[name] !== "function") return { ok: false, reason: "unsupported" };
  const args = [{ path: path }, path];
  for (let i = 0; i < args.length; i++) {
    try {
      await withTimeout(app[name](args[i]), T_AUTH, name);
      mreport("authorize-ok", name + " " + path);
      return { ok: true };
    } catch (e) { /* 换入参形态再试 */ }
  }
  mreport("authorize-fail", name + " " + path);
  return { ok: false, reason: "failed" };
}

async function probePath(path) {
  try {
    const r = await fetch(BASE + "/?action=probe&path=" + encodeURIComponent(path));
    return await r.json();
  } catch (e) { return { status: "error", message: errText(e) }; }
}

window.pickWithSystem = async function (target, kind) {
  const input = el(target);
  if (!input) return true;
  const before = input.value;                 // 取消/失败保持原值

  const shared = kind === "shared";
  /* allowToStorageRoot / allowToStorageSubDir：允许选中存储空间根（如 /vol2）与
     @appdata 之类的二级目录，否则备份目标只能选到子目录、相册缩略图目录点不进去。 */
  const userOpts = { title: "选择源目录（相册缩略图）", okText: "选择", directory: true,
                     sidebarGroup: SIDEBAR_ALL, disabledPaths: [],
                     allowToStorageRoot: true, allowToStorageSubDir: true };
  const sharedOpts = { title: shared ? "选择备份目标目录" : "选择目录", okText: "选择",
                       sidebarGroup: SIDEBAR_ALL, creatable: shared, disabledPaths: [],
                       allowToStorageRoot: true, allowToStorageSubDir: true };

  /* 回退点 ①：SDK 根本没起来 */
  let app;
  try {
    app = await getApp();
  } catch (e) {
    diagShow("fnOS 原生选择器不可用（" + errText(e) + "），已回退为内置浏览器");
    mreport("fallback", "sdk:" + errText(e));
    return false;
  }
  /* 回退点 ②：不在桌面 iframe */
  if (app.isStandaloneWeb) {
    diagShow("当前不在飞牛桌面窗口内，已回退为内置浏览器");
    mreport("fallback", "standalone");
    return false;
  }

  let r;
  try {
    r = await pickWithFallback(app, shared ? "shared" : "user", userOpts, sharedOpts);
  } catch (e) {
    r = { status: "error", msg: errText(e) };
  }

  if (r.status === "cancel") {                // 取消：安静结束，绝不弹第二个框
    diagClear();
    input.value = before;
    mreport("pick-cancel-done", r.method + " " + (r.msg || ""));
    return true;
  }
  /* 回退点 ③：桥明确不可用 */
  if (r.status === "unavailable") {
    diagShow("fnOS 原生选择器不可用（" + r.msg + "），已回退为内置浏览器");
    mreport("fallback", "unavailable:" + r.msg);
    return false;
  }
  if (r.status === "error") {                 // 其它失败：只留诊断条
    input.value = before;
    diagShow("目录选择失败：" + r.msg + "（可重试，或直接在输入框手动填写路径）");
    mreport("pick-error", r.method + " " + r.msg);
    return true;
  }

  let path = (r.paths[0] || "").replace(/\/+$/, "") || "/";
  input.value = path;

  const auth = await tryAuthorize(app, path, shared);
  const pr = await probePath(path);
  mreport("pick-done", r.method + " " + path + " auth=" + auth.ok + " probe=" + JSON.stringify(pr).slice(0, 160));

  if (pr.status !== "ok" || (!pr.readable && !pr.writable)) {
    diagShow("已选目录：" + path + "，但应用当前没有访问权限。请到「应用中心 → 备份打包 → 设置 → 授权目录」添加该目录。");
  } else {
    diagClear();
  }
  return true;
};

</script>
</body>
</html>
""")


_WD_NAMES = ("周日", "周一", "周二", "周三", "周四", "周五", "周六")


def parse_cron_simple(expr):
    """把「简单表达式」反解成界面控件初值。

    只反解三种形态：单值分 + 单值时 +（每天 | 每周指定星期 | 每月指定日）。
    其余（*/n、区间、跨字段组合等）标 advanced=True 走手写模式，绝不静默改写用户表达式。
    """
    expr = (expr or "").strip()
    info = {"freq": "daily", "time": "03:00", "days": [], "dom": 1,
            "advanced": False, "desc": ""}
    parts = expr.split()
    if (len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit()):
        info.update({"advanced": True, "desc": "自定义表达式：%s" % (expr or "未设置")})
        return info
    mm, hh = int(parts[0]), int(parts[1])
    if not (0 <= mm <= 59 and 0 <= hh <= 23):
        info.update({"advanced": True, "desc": "自定义表达式：%s" % expr})
        return info
    info["time"] = "%02d:%02d" % (hh, mm)
    if parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
        info.update({"freq": "daily", "desc": "每天 %s" % info["time"]})
        return info
    if parts[2] == "*" and parts[3] == "*" and re.fullmatch(r"[0-9,]+", parts[4]):
        days = []
        for d in parts[4].split(","):
            if d.isdigit() and 0 <= int(d) <= 7:
                days.append(str(int(d) % 7))
        if days:
            info.update({"freq": "weekly", "days": days,
                         "desc": "每周 %s %s" % ("、".join(_WD_NAMES[int(d)] for d in days),
                                                 info["time"])})
            return info
    if parts[3] == "*" and parts[4] == "*" and parts[2].isdigit() and 1 <= int(parts[2]) <= 31:
        info.update({"freq": "monthly", "dom": int(parts[2]),
                     "desc": "每月 %s 日 %s" % (parts[2], info["time"])})
        return info
    info.update({"advanced": True, "desc": "自定义表达式：%s" % expr})
    return info


# ---------------------------------------------------------------- 目录浏览
def _accessible_paths():
    """fnOS 注入的「用户授权目录」列表（: 分隔）。"""
    raw = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS") or ""
    return [p for p in raw.split(":") if p.strip()]


def _vol_roots():
    out = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"/vol[0-9]+", parts[1]):
                    out.append(parts[1])
    except OSError:
        pass
    return sorted(set(out))


def _browse_allowed():
    """允许浏览的根（全部 realpath 化，防符号链接逃逸）。"""
    roots = []
    for p in _accessible_paths() + _vol_roots() + [config.VAR_DIR, config.APP_DIR]:
        try:
            roots.append(os.path.realpath(p))
        except OSError:
            continue
    return roots


def browse_roots():
    """返回可浏览的起始位置。包用户读不到的目录不会出现（天然隔离）。"""
    roots, seen = [], set()

    def add(path, label, note):
        try:
            real = os.path.realpath(path)
        except OSError:
            return
        if real in seen or not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
            return
        seen.add(real)
        roots.append({"path": real, "label": label, "note": note})

    for p in _accessible_paths():
        add(p, p, "已授权")
    for v in _vol_roots():
        add(v, v, "存储卷")
    add(config.VAR_DIR, config.VAR_DIR, "应用数据目录")
    add(config.APP_DIR, config.APP_DIR, "应用目录")
    if not roots:
        return {"status": "error",
                "message": "没有可访问的目录。请先到「应用中心 → 备份打包 → 设置 → 授权目录」添加目录。"}
    return {"status": "ok", "roots": roots}


def browse_list(path):
    """列出某个目录下的子目录（只返回目录名，不读文件内容）。"""
    path = (path or "").strip()
    if not path:
        return {"status": "error", "message": "缺少 path 参数"}
    try:
        real = os.path.realpath(path)
    except OSError as exc:
        return {"status": "error", "message": "路径无效：%s" % exc}

    allowed = _browse_allowed()
    if not any(real == a or real.startswith(a.rstrip("/") + "/") for a in allowed):
        return {"status": "error",
                "message": "该路径不在可访问范围内。请先在「应用设置 → 授权目录」里授权。"}
    if not os.path.isdir(real):
        return {"status": "error", "message": "不是目录：%s" % real}

    dirs = []
    try:
        with os.scandir(real) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                dirs.append({"name": entry.name,
                             "path": os.path.join(real, entry.name)})
    except PermissionError:
        return {"status": "error", "message": "没有权限读取该目录，请先在应用设置里授权"}
    except OSError as exc:
        return {"status": "error", "message": "读取失败：%s" % exc}

    dirs.sort(key=lambda d: d["name"].lower())
    return {"status": "ok", "path": real, "dirs": dirs[:500],
            "note": "" if dirs else "该目录下没有子目录，可直接选当前目录"}


def render():
    cfg = config.normalize(config.load())
    st = config.load_status()
    lock = backup.inspect_lock()
    zstd_ok = bool(shutil.which("zstd"))

    def esc(v):
        return html.escape(str(v), quote=True)

    # ---- 状态区（全部经 escape） ----
    rows = []
    if st.get("last_success_epoch"):
        days = max(0.0, (time.time() - float(st["last_success_epoch"])) / 86400.0)
        cls = "ok" if days < 2 else ("warn" if days < 7 else "err")
        rows.append(("上次成功备份", '<span class="%s">%s（%.1f 天前）</span>'
                     % (cls, esc(st.get("last_success_at", "?")), days)))
        rows.append(("上次产物", esc(st.get("last_package", "-"))))
        rows.append(("上次文件数", esc(st.get("last_count", "-"))))
        rows.append(("产物质量", esc(QUALITY_LABEL.get(st.get("last_quality", ""),
                                                      st.get("last_quality", "-")))))
    else:
        rows.append(("上次成功备份", '<span class="err">从未成功备份</span>'))
    if st.get("last_error"):
        rows.append(("最近一次失败", '<span class="err">%s</span>' % esc(st["last_error"])))
    if lock["held"]:
        rows.append(("任务锁", '<span class="%s">持有中 pid=%s，已 %d 分钟%s</span>'
                     % ("warn" if lock["stale"] else "ok",
                        esc(lock["info"].get("pid", "?")), lock["age"] // 60,
                        "（已失效，可清除）" if lock["stale"] else "")))
    else:
        rows.append(("任务锁", "空闲"))
    rows.append(("目标挂载点", esc(cfg.get("dst_mountpoint") or "尚未记录（首次备份时写入）")))
    status_html = "".join('<div><span class="k">%s：</span>%s</div>' % (esc(k), v)
                          for k, v in rows)

    cron_info = parse_cron_simple(cfg.get("cron_expr", ""))
    page = TPL.safe_substitute(
        VERSION=esc(VERSION),
        BASE=esc(os.environ.get("BACKUP_PACK_BASE_PATH") or "/app/backup-pack"),
        STATUS=status_html,
        SRC=esc(cfg["src_dir"]),
        DST=esc(cfg["dst_dir"]),
        KEEP=esc(cfg["keep_versions"]),
        RETAIN=esc(cfg["log_retain_days"]),
        MARGIN=esc(cfg["min_free_ratio"]),
        MAXFILES=esc(cfg.get("max_files", 0)),
        CRON=esc(cfg["cron_expr"]),
        SEL_NONE=" selected" if cfg["compress"] == "none" else "",
        SEL_GZIP=" selected" if cfg["compress"] == "gzip" else "",
        SEL_ZSTD=" selected" if cfg["compress"] == "zstd" else "",
        CHK=" checked" if cfg["enable_cron"] else "",
        ALLOWROOT=" checked" if cfg.get("allow_root_mount") else "",
        ZSTD_HINT="" if zstd_ok else "（未检测到，选后打包会失败）",
        RUN_TIME=esc(cron_info["time"]),
        FREQ_DAILY=" selected" if cron_info["freq"] == "daily" else "",
        FREQ_WEEKLY=" selected" if cron_info["freq"] == "weekly" else "",
        FREQ_MONTHLY=" selected" if cron_info["freq"] == "monthly" else "",
        WD_JSON=json.dumps(cron_info["days"]),
        DOM_VALUE=esc(cron_info["dom"]),
        ADV_CHK=" checked" if cron_info["advanced"] else "",
        CRON_DESC=esc(cron_info["desc"]),
    )
    return page



# ---------------------------------------------------------------- 路由
def route(method, params):
    """纯路由：返回 (status, headers, body)。

    CGI 入口（app/ui/index.cgi）与常驻 HTTP 服务（app/server/server.py）共用这一份逻辑，
    因此两条通道的行为、鉴权边界、错误语义完全一致。
    """
    action = params.get("action", "")

    if action == "save":
        if method != "POST":
            return _json(400, {"status": "error", "message": "保存必须使用 POST"})
        cfg = config.load()
        cfg["src_dir"] = (params.get("src_dir") or "").strip()
        cfg["dst_dir"] = (params.get("dst_dir") or "").strip()
        cfg["compress"] = params.get("compress", "none")
        cfg["keep_versions"] = params.get("keep_versions", "3")
        cfg["cron_expr"] = (params.get("cron_expr") or "").strip()
        cfg["log_retain_days"] = params.get("log_retain_days", "7")
        cfg["min_free_ratio"] = params.get("min_free_ratio", "0.1")
        cfg["max_files"] = (params.get("max_files") or "0").strip()
        cfg["enable_cron"] = params.get("enable_cron") == "1"
        cfg["allow_root_mount"] = params.get("allow_root_mount") == "1"
        cfg = config.normalize(cfg)

        ok, errors = config.validate(cfg)
        if not ok:                                   # FIX-03 校验失败绝不改 crontab
            return _json(200, {"status": "error", "message": "；".join(errors)})
        try:
            mp = backup.real_mountpoint(cfg["dst_dir"])
        except OSError:
            mp = ""
        cfg["dst_mountpoint"] = mp if mp and mp != "/" else ""
        try:
            config.save(cfg)
            config.update_crontab(cfg)               # 只有校验通过才写
        except RuntimeError as e:
            return _json(200, {"status": "error", "message": str(e)})
        return _json(200, {"status": "ok",
                           "message": "配置已保存，定时任务已同步为 %s" % cfg["cron_expr"]})

    if action in ("run", "unlock", "purge"):
        if method != "POST":                         # FIX-16 拒绝 GET 触发副作用
            return (405,
                    [("Content-Type", "application/json; charset=utf-8"),
                     ("Allow", "POST"), ("Cache-Control", "no-store")],
                    json.dumps({"status": "error", "message": "该操作必须使用 POST"},
                               ensure_ascii=False))
        handler = {"run": run_once, "unlock": unlock, "purge": purge_invalid}[action]
        return _json(200, handler())

    if action == "probe":
        path = (params.get("path") or "").strip()
        if not path:
            return _json(400, {"status": "error", "message": "缺少 path"})
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            return _json(200, {"status": "error", "message": "不是目录或不存在：%s" % real})
        allowed = _browse_allowed()
        in_scope = any(real == a or real.startswith(a.rstrip("/") + "/") for a in allowed)
        return _json(200, {"status": "ok", "path": real, "in_scope": in_scope,
                           "readable": os.access(real, os.R_OK | os.X_OK),
                           "writable": os.access(real, os.W_OK)})
    if action == "diag":
        # 前端打点：把「页面在什么环境里、卡在哪一步」写进 task.log，便于真机排障
        stage = (params.get("stage") or "")[:40]
        msg = (params.get("msg") or "")[:600]
        try:
            backup.log("CLIENT %s %s" % (stage, msg), "INFO")
        except Exception:
            pass
        return _json(200, {"status": "ok"})
    if action == "roots":
        return _json(200, browse_roots())
    if action == "ls":
        return _json(200, browse_list(params.get("path", "")))
    if action == "list":
        return _json(200, list_backups())
    if action == "log":
        return _text(tail_log())
    if action == "verify":
        return _json(200, verify_backup(params.get("name", "")))
    if action == "status":
        st = config.load_status()
        st["lock"] = backup.inspect_lock()
        return _json(200, {"status": "ok", "state": st})

    return _html(render())


def main():
    """CGI 宿主：解析环境变量 → 路由 → 输出。"""
    method, params = parse_request()
    status, headers, body = route(method, params)
    send(status, headers, body)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:              # noqa: BLE001 - CGI 必须给出可读响应
        # CGI 里未捕获异常只会写 stderr，浏览器拿到空响应，排查成本极高。
        # 这里兜底输出 500 + JSON，让错误在页面上可见。
        try:
            backup.log("页面请求异常：%r" % (exc,), "ERROR")
        except Exception:
            pass
        try:
            sys.stdout.write("Status: 500 Internal Server Error\r\n"
                             "Content-Type: application/json; charset=utf-8\r\n"
                             "Cache-Control: no-store\r\n\r\n")
            sys.stdout.write(json.dumps({"status": "error",
                                         "message": "内部错误：%r" % (exc,)},
                                        ensure_ascii=False))
            sys.stdout.flush()
        except Exception:
            pass
        sys.exit(1)
