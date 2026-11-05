#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""常驻 HTTP 服务：桌面入口实际访问的通道。

为什么要它：这版 fnOS 的桌面入口只在入口配置带 `protocol`+`port` 时才会注册成可点项
（本机 fn-ups-manager 就是这么做的），纯 CGI 入口不会被登记——表现为「应用卡片只有
卸载、桌面无图标」。因此这里起一个标准库 HTTP 服务，由 cmd/main 负责 start/stop/status。

业务逻辑不重复实现：请求交给 app/ui/index.cgi 里的 route()，与 CGI 通道完全同一份代码，
两条通道的行为、错误语义一致，只是 IO 宿主不同。

安全：监听 0.0.0.0（与 fn-ups-manager 一致），应用自身不做鉴权，仅限可信内网使用，
不要把端口映射到公网。见 KNOWN_ISSUES 的 KNOWN-06。
"""

import argparse
import importlib.machinery
import importlib.util
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# fnOS 统一网关会把 /app/<appname>/* 直接转给 unix socket（前缀不剥离），
# 这个前缀同时决定页面里的相对路径解析，必须与 ui/config 的 gatewayPrefix 一致。
BASE_PATH = (os.environ.get("BACKUP_PACK_BASE_PATH") or "/app/backup-pack").rstrip("/")

HERE = os.path.dirname(os.path.abspath(__file__))          # <target>/server


def _detect_app_dir():
    """包内容目录（含 ui/ 与 lib/ 的那一层）。

    优先按自身位置推导（server.py 在 <target>/server/，上溯一级即 <target>），
    这是最可靠的一条；PHOTOS_BACKUP_APP_DIR 只给离线自测覆盖；
    最后才用 fnOS 的 TRIM_APPDEST——注意本机可能带着**其它应用**的 TRIM_* 变量，
    直接采信会把路径解析到别人的目录里。
    """
    here = os.path.dirname(HERE)
    if os.path.exists(os.path.join(here, "ui", "index.cgi")):
        return here
    env = os.environ.get("PHOTOS_BACKUP_APP_DIR")
    if env and os.path.exists(os.path.join(env, "ui", "index.cgi")):
        return env
    env = os.environ.get("TRIM_APPDEST")
    if env and os.path.exists(os.path.join(env, "ui", "index.cgi")):
        return env
    return here


APP_DIR = _detect_app_dir()
CGI_PATH = os.path.join(APP_DIR, "ui", "index.cgi")
sys.path.insert(0, os.path.join(APP_DIR, "lib"))


def load_webapp():
    """加载 CGI 入口模块，取它的 route() / 日志能力（模块级代码会设置路径与编码）。"""
    loader = importlib.machinery.SourceFileLoader("backup_pack_web", CGI_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ASSET_DIR = os.path.join(APP_DIR, "ui", "assets")
_ASSET_TYPES = {".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml"}


def strip_base(path):
    """去掉网关前缀：/app/backup-pack/foo -> /foo。"""
    p = path or "/"
    if BASE_PATH and (p == BASE_PATH or p.startswith(BASE_PATH + "/")):
        p = p[len(BASE_PATH):] or "/"
        if not p.startswith("/"):
            p = "/" + p
    return p


def serve_asset(path):
    """只服务 ui/assets 下的白名单静态文件（fnOS SDK 等），不碰其它路径。"""
    name = os.path.basename(path or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ASSET_TYPES or not name:
        return None
    full = os.path.join(ASSET_DIR, name)
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as f:
            return _ASSET_TYPES[ext], f.read()
    except OSError:
        return None


WEB = load_webapp()


class Handler(BaseHTTPRequestHandler):
    server_version = "backup-pack"
    protocol_version = "HTTP/1.1"

    def _params(self):
        u = urlparse(strip_base(self.path))
        params = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        if self.command in ("POST", "PUT"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
            ctype = self.headers.get("Content-Type") or ""
            if "application/x-www-form-urlencoded" in ctype and body:
                for k, v in parse_qs(body, keep_blank_values=True).items():
                    params[k] = v[-1]
        return params

    def _handle(self):
        clean = strip_base(self.path)
        if self.command in ("GET", "HEAD") and clean.startswith("/assets/"):
            got = serve_asset(urlparse(clean).path)
            if got:
                ctype, data = got
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            status, headers, body = WEB.route(self.command, self._params())
        except BaseException as exc:                 # noqa: BLE001 - 必须给出可读响应
            try:
                WEB.backup.log("HTTP 请求异常：%r" % (exc,), "ERROR")
            except Exception:
                pass
            status, headers = 500, []
            body = '{"status": "error", "message": "内部错误：%s"}' % str(exc).replace('"', "'")
        data = body.encode("utf-8", "replace")   # 兜底：不因单个脏字符整页 500
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_HEAD = _handle

    def log_message(self, fmt, *args):
        try:
            WEB.backup.log("http %s %s" % (self.address_string(), fmt % args))
        except Exception:
            pass


class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    """HTTP over unix socket：fnOS 统一网关按 socket 反代，iframe 与桌面同源。"""

    daemon_threads = True

    def get_request(self):
        request, _addr = super().get_request()
        return request, ("unix", 0)


def main():
    ap = argparse.ArgumentParser(description="飞牛备份打包 · 常驻 HTTP 服务")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TRIM_SERVICE_PORT") or 45182))
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--socket", default="", help="改为监听 unix socket（gateway 模式）")
    args = ap.parse_args()

    if args.socket:
        try:
            os.unlink(args.socket)
        except OSError:
            pass
        srv = UnixHTTPServer(args.socket, Handler)
        try:
            os.chmod(args.socket, 0o660)
        except OSError:
            pass
        try:
            WEB.backup.log("服务启动（gateway）：socket=%s base=%s" % (args.socket, BASE_PATH))
        except Exception:
            pass
    else:
        srv = ThreadingHTTPServer((args.bind, args.port), Handler)
        try:
            WEB.backup.log("HTTP 服务启动：%s:%d（代码目录 %s）" % (args.bind, args.port, APP_DIR))
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
