#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""领域层：加锁、挂载点守卫、空间预检、打包、校验、保留策略、日志轮转、状态记录。

FIX-01  失败包绝不留成「有效备份」：残包即删、不计入 keep、列表四态
FIX-04  原子锁（O_EXCL）+ PID 存活检测 + 心跳，进程死了立刻可重跑
FIX-05  目标挂载点守卫：外接盘掉线/未挂载时拒绝写入，避免写满系统盘
FIX-07  空间预检覆盖全部压缩模式，含 tar 头开销与安全余量，并对 keep 份数告警
FIX-08  .sha256 只记录 basename，包换路径后仍可校验
FIX-09  每次运行写 status.json（上次成功/失败/文件数），供 UI 可观测
FIX-10  run() 自己先 validate，手动与 cron 两条路径都覆盖
FIX-12  日志按「行首时间戳」轮转，无时间戳行归属上一行；并有体积上限兜底
FIX-13  路径常量全部来自 config，本文件不再重复定义
FIX-17  源目录 0 文件时拒绝产出空包
"""

import glob
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfgmod

CONFIG_DIR = cfgmod.VAR_DIR
LOG_PATH = cfgmod.LOG_PATH
LOCK_PATH = cfgmod.LOCK_PATH
BACKUP_PY = cfgmod.BACKUP_PY

LOCK_HEARTBEAT = 60          # 心跳间隔（秒）
LOCK_STALE_AFTER = 600       # 无心跳超过该秒数且进程仍活着 → 判定卡死，允许回收
LOG_MAX_BYTES = 16 * 1024 * 1024
TAR_RECORD = 10240           # tar 结尾零块 + 记录对齐
PACKAGE_PATTERNS = ("thumbnails_*.tar", "thumbnails_*.tar.gz", "thumbnails_*.tar.zst")


class BackupError(Exception):
    """业务级失败：消息直接进日志 / UI。"""


# ---------------------------------------------------------------- 日志
def log(msg, level="INFO"):
    line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return line


def _trim_by_size():
    """FIX-12 兜底：日志超过体积上限时只留尾部 2MB。"""
    try:
        if os.path.getsize(LOG_PATH) <= LOG_MAX_BYTES:
            return
        with open(LOG_PATH, "rb") as f:
            f.seek(-(2 * 1024 * 1024), os.SEEK_END)
            tail = f.read()
        with open(LOG_PATH, "wb") as f:
            f.write("...(日志超限，早期内容已截断)...\n".encode("utf-8") + tail)
    except OSError:
        pass


def rotate_log(retain_days):
    """按行首时间戳清理过期行。无时间戳的行沿用上一行的时间戳（FIX-12）。"""
    if not os.path.exists(LOG_PATH) or retain_days < 1:
        return
    cutoff = time.time() - retain_days * 86400
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    kept, last_ts = [], None
    for l in lines:
        try:
            last_ts = time.mktime(time.strptime(l[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OSError):
            pass
        if last_ts is None or last_ts >= cutoff:
            kept.append(l)
    if len(kept) != len(lines):
        try:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(kept)
        except OSError:
            pass
    _trim_by_size()


# ---------------------------------------------------------------- 锁协议
def read_lock():
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但无权限发信号 → 视为存活
    except OSError:
        return False


def inspect_lock():
    """返回 {held, stale, info, age}，供 UI 展示与自愈判断。FIX-04"""
    if not os.path.exists(LOCK_PATH):
        return {"held": False, "stale": False, "info": {}, "age": 0}
    info = read_lock()
    try:
        age = time.time() - os.path.getmtime(LOCK_PATH)
    except OSError:
        return {"held": False, "stale": True, "info": info, "age": 0}
    pid = info.get("pid")
    if pid:
        stale = (not pid_alive(pid)) or age > LOCK_STALE_AFTER
    else:
        stale = age > LOCK_STALE_AFTER
    return {"held": True, "stale": stale, "info": info, "age": int(age)}


def acquire_lock(task="manual"):
    """原子创建锁文件。返回 (ok, 冲突信息)。FIX-04"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            state = inspect_lock()
            if state["stale"]:
                log("检测到失效锁（pid=%s, 已 %.0f 秒无心跳），自动回收"
                    % (state["info"].get("pid"), state["age"]), "WARN")
                release_lock(force=True)
                continue
            return False, state
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "task": task, "host": socket.gethostname(),
                       "start": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "start_epoch": time.time()}, f)
        return True, None
    return False, inspect_lock()


def heartbeat():
    try:
        os.utime(LOCK_PATH, None)
    except OSError:
        pass


def release_lock(force=False):
    if not os.path.exists(LOCK_PATH):
        return
    if not force:
        info = read_lock()
        if info.get("pid") and int(info["pid"]) != os.getpid():
            return           # 不是自己的锁，不动
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- 磁盘与规模
def real_mountpoint(path):
    """向上找到 path 所在挂载点。/ 表示系统盘。FIX-05

    目标目录可能尚不存在（首次使用 / 外接盘拔掉后目录消失），
    因此从最近的存在祖先开始向上找，而不是先建目录再判断——
    先建后判会在盘未挂载时把 60GB 写进系统盘。
    """
    p = os.path.realpath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    while True:
        try:
            if os.path.ismount(p):
                return p
        except OSError:
            pass
        parent = os.path.dirname(p)
        if parent == p:
            return p
        p = parent


def check_mountpoint(dst, cfg):
    """FIX-05：挂载点变化 = 外接盘掉线，拒绝写入。"""
    mp = real_mountpoint(dst)
    expected = (cfg.get("dst_mountpoint") or "").strip()
    if mp == "/" and not cfg.get("allow_root_mount"):
        raise BackupError(
            "目标目录落在系统盘（挂载点 /）。为防止写满系统盘已拒绝执行；"
            "确认要如此请勾选「允许写入系统盘」。")
    if expected and mp != expected:
        raise BackupError(
            "目标挂载点已变化：期望 %s，实际 %s。外接盘可能未挂载或已掉线，"
            "已拒绝写入以免写满系统盘。请检查磁盘挂载后重试。" % (expected, mp))
    return mp


def scan_source(src):
    """一次遍历同时得到文件数、源大小、tar 预估字节数。FIX-07 / FIX-17"""
    count = size = 0
    tar_bytes = 0
    problems = 0
    for dirpath, dirnames, filenames in os.walk(src, onerror=lambda e: None):
        tar_bytes += 512 * (1 + len(dirnames))     # 目录自身占一个头块
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                problems += 1
                continue
            count += 1
            if stat.S_ISLNK(st.st_mode):
                tar_bytes += 512                    # 符号链接只存链接体
            elif stat.S_ISREG(st.st_mode):
                size += st.st_size
                tar_bytes += 512 + ((st.st_size + 511) // 512) * 512
            else:
                tar_bytes += 512
    tar_bytes += TAR_RECORD
    return {"count": count, "size": size, "tar_bytes": tar_bytes, "unreadable": problems}


def precheck_space(dst, src_stat, cfg, keep):
    """FIX-07：所有压缩模式都预检；压缩比按「无收益」保守估算。"""
    st = os.statvfs(dst)
    free = st.f_bavail * st.f_frsize
    ratio = float(cfg.get("min_free_ratio", 0.10))
    need = int(src_stat["tar_bytes"] * (1 + ratio))
    if free < need:
        raise BackupError(
            "目标空间不足：预计需要 %.2f GiB（含 %.0f%% 安全余量），"
            "当前可用 %.2f GiB。" % (need / 2**30, ratio * 100, free / 2**30))
    if keep > 1 and free - need < need * (keep - 1):
        log("提示：可用空间 %.2f GiB，按保留 %d 份滚动需要约 %.2f GiB，"
            "旧包会被清理，但写入期间空间偏紧。" % (free / 2**30, keep, need * keep / 2**30),
            "WARN")
    return free, need


def collect_packages(dst):
    """返回 (valid, residue)。valid 必须同时存在 .sha256。FIX-01"""
    files = []
    for p in PACKAGE_PATTERNS:
        files.extend(glob.glob(os.path.join(dst, p)))
    files = sorted(set(files))
    valid, residue = [], []
    for f in files:
        (valid if os.path.exists(f + ".sha256") else residue).append(f)
    return valid, residue


# ---------------------------------------------------------------- 打包
def _run_with_heartbeat(cmd, logfile):
    """子进程运行期间持续 touch 锁文件，避免长任务被误判为卡死。FIX-04"""
    with open(logfile, "a", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        last = time.time()
        while proc.poll() is None:
            time.sleep(1)
            if time.time() - last >= LOCK_HEARTBEAT:
                heartbeat()
                last = time.time()
    return proc.returncode


def pack(src, dst, compress):
    os.makedirs(dst, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    if compress == "gzip":
        name, cmd = "thumbnails_%s.tar.gz" % ts, None
        cmd = ["tar", "czf"]
    elif compress == "zstd":
        if not shutil.which("zstd"):
            raise BackupError("系统未安装 zstd，请改用 gzip 或不压缩")
        name = "thumbnails_%s.tar.zst" % ts
        cmd = ["tar", "--zstd", "-cf"]
    else:
        name = "thumbnails_%s.tar" % ts
        cmd = ["tar", "cf"]
    tar_path = os.path.join(dst, name)
    cmd = cmd + [tar_path, "-C", src, "."]

    rc = _run_with_heartbeat(cmd, LOG_PATH)
    if rc >= 2:
        # FIX-01：失败即清残包，绝不让它进入备份列表
        discard(tar_path, "tar 返回码 %d" % rc)
        raise BackupError("打包失败：tar 返回码 %d（详见日志）" % rc)
    quality = "approximate" if rc == 1 else "complete"
    if rc == 1:
        log("打包期间有文件变化，产物为近似快照（成员可能不一致）", "WARN")
    # 大小下限只对未压缩 tar 有意义（最小合法 tar 是 10240 字节的结尾块）。
    # gzip/zstd 能把可压内容压到 10240 以下，用同一阈值会把正常压缩包误判成残包。
    size = os.path.getsize(tar_path) if os.path.exists(tar_path) else 0
    too_small = (size < TAR_RECORD) if compress == "none" else (size <= 0)
    if too_small:
        discard(tar_path, "产物异常偏小（%d 字节）" % size)
        raise BackupError("打包产物异常（文件缺失或过小），已丢弃")
    return tar_path, quality


def discard(tar_path, reason):
    """FIX-01：删除残包与残校验文件。"""
    for p in (tar_path, tar_path + ".sha256"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    log("已丢弃无效产物 %s（原因：%s）" % (os.path.basename(tar_path), reason), "ERROR")


def write_checksum(tar_path):
    """FIX-08：只记录 basename，包换路径后仍可校验。"""
    base = os.path.basename(tar_path)
    p = subprocess.run(["sha256sum", base], cwd=os.path.dirname(tar_path),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise BackupError("生成校验值失败：%s" % (p.stderr or "").strip())
    with open(tar_path + ".sha256", "w", encoding="utf-8") as f:
        f.write(p.stdout)


def normalize_checksum(tar_path):
    """兼容 1.1.0 生成的「绝对路径」校验文件，就地改写为 basename。"""
    path = tar_path + ".sha256"
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    out, changed = [], False
    for line in content.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.isabs(parts[1].strip()):
            out.append("%s  %s" % (parts[0], os.path.basename(parts[1].strip())))
            changed = True
        else:
            out.append(line)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    return changed


def verify(tar_path, timeout=7200):
    normalize_checksum(tar_path)
    p = subprocess.run(["sha256sum", "-c", os.path.basename(tar_path) + ".sha256"],
                       cwd=os.path.dirname(tar_path), capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def cleanup(dst, keep):
    """FIX-01：只有带 .sha256 的包才计入保留份数并参与淘汰。

    没有 .sha256 的包（历史残包 / 用户误删校验文件）一律标记为 invalid：
    不计入 keep、不参与淘汰、也不自动删除——避免误删用户唯一的数据。
    需要回收空间由用户在 UI 上点「清理无效产物」（purge_invalid）显式确认。
    """
    valid, residue = collect_packages(dst)
    if residue:
        names = "、".join(os.path.basename(f) for f in residue[:5])
        log(
            "发现 %d 个无校验文件的产物（%s%s），已标记为 invalid 并排除在保留份数之外"
            % (len(residue), names, "…" if len(residue) > 5 else ""),
            "WARN",
        )
    if len(valid) > keep:
        for f in valid[: len(valid) - keep]:
            for p in (f, f + ".sha256"):
                try:
                    os.remove(p)
                except OSError as e:
                    log("清理旧包失败 %s：%s" % (os.path.basename(f), e), "WARN")
            log("已清理旧包 %s" % os.path.basename(f))


def purge_invalid(dst):
    """用户显式确认后，删除所有无校验文件的产物。返回删除个数。"""
    _valid, residue = collect_packages(dst)
    n = 0
    for f in residue:
        try:
            os.remove(f)
            n += 1
            log("用户清除无效产物 %s" % os.path.basename(f), "WARN")
        except OSError as e:
            log("清除无效产物失败 %s：%s" % (os.path.basename(f), e), "ERROR")
    return n


# ---------------------------------------------------------------- 统一入口
def run(cfg=None, task="manual"):
    """手动与 cron 共用。锁、校验、预检、打包、校验值、清理、状态全包含。"""
    cfg = cfgmod.normalize(cfg or cfgmod.load())

    ok, errs = cfgmod.validate(cfg)              # FIX-10
    if not ok:
        msg = "配置校验未通过：" + "；".join(errs)
        log(msg, "ERROR")
        _record_failure(msg)
        raise BackupError(msg)

    src, dst = cfg["src_dir"].strip(), cfg["dst_dir"].strip()
    keep, days = int(cfg["keep_versions"]), int(cfg["log_retain_days"])
    compress = cfg["compress"]

    acquired, conflict = acquire_lock(task)
    if not acquired:
        info = (conflict or {}).get("info", {})
        msg = "已有备份任务正在运行（pid=%s，开始于 %s）" % (
            info.get("pid", "?"), info.get("start", "?"))
        raise BackupError(msg)

    try:
        rotate_log(days)
        log("开始备份：task=%s src=%s dst=%s compress=%s keep=%d"
            % (task, src, dst, compress, keep))

        mp = check_mountpoint(dst, cfg)          # FIX-05
        if not cfg.get("dst_mountpoint"):
            cfg["dst_mountpoint"] = mp
            try:
                cfgmod.save(cfg)
                log("已记录目标挂载点：%s" % mp)
            except OSError as e:
                log("记录挂载点失败：%s" % e, "WARN")

        src_stat = scan_source(src)              # FIX-07 / FIX-17
        if src_stat["count"] == 0:
            raise BackupError("源目录内没有文件，已拒绝产出空备份包")
        log("源目录扫描：%d 个文件 / %.2f GiB，预估包体积 %.2f GiB"
            % (src_stat["count"], src_stat["size"] / 2**30, src_stat["tar_bytes"] / 2**30))

        # FIX-17：max_files > 0 时做误指保护——源目录一旦填成整个卷，
        # 打包会先把目标盘写满再失败，这里在 tar 启动前就拒绝。
        try:
            max_files = int(cfg.get("max_files") or 0)
        except (TypeError, ValueError):
            max_files = 0
        if max_files > 0 and src_stat["count"] > max_files:
            raise BackupError(
                "源目录内文件数 %d 超过上限 max_files=%d，疑似误指到整个卷，已拒绝执行"
                "（确认无误请调大或置 0）" % (src_stat["count"], max_files))

        # 挂载点校验通过后才建目录：盘未挂载时绝不能先 makedirs，
        # 否则会在系统盘上创建一个空目录并把整个备份写进去。
        try:
            os.makedirs(dst, exist_ok=True)
        except OSError as e:
            raise BackupError("目标目录不存在且无法创建：%s" % e)
        precheck_space(dst, src_stat, cfg, keep)

        tar_path, quality = pack(src, dst, compress)
        write_checksum(tar_path)
        size = os.path.getsize(tar_path)
        cleanup(dst, keep)

        msg = "完成：%s（%.2f GiB，%d 个文件，质量=%s）" % (
            os.path.basename(tar_path), size / 2**30, src_stat["count"], quality)
        log(msg)
        _record_success(tar_path, quality, src_stat, size)
        return {"status": "ok", "package": tar_path, "quality": quality,
                "count": src_stat["count"], "bytes": size}
    except BackupError as e:
        log(str(e), "ERROR")
        _record_failure(str(e))
        raise
    except Exception as e:                        # 兜底：任何异常都要落日志+留痕
        log("未预期异常：%r" % (e,), "ERROR")
        _record_failure("未预期异常：%s" % e)
        raise
    finally:
        release_lock()


def _record_success(tar_path, quality, src_stat, size):
    st = cfgmod.load_status()                     # FIX-09
    now = time.time()
    st.update({
        "last_success_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "last_success_epoch": now,
        "last_package": os.path.basename(tar_path),
        "last_quality": quality,
        "last_count": src_stat["count"],
        "last_bytes": size,
        "runs": int(st.get("runs", 0)) + 1,
        "last_error": "",
    })
    try:
        cfgmod.save_status(st)
    except OSError:
        pass


def _record_failure(reason):
    st = cfgmod.load_status()
    st.update({"last_error": reason[:500],
               "last_failure_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        cfgmod.save_status(st)
    except OSError:
        pass


def main(argv):
    arg = argv[1] if len(argv) > 1 else ""
    if arg == "--unlock":
        state = inspect_lock()
        if not state["held"]:
            print("没有锁文件")
            return 0
        if not state["stale"]:
            print("任务确实在运行（pid=%s），拒绝清除" % state["info"].get("pid"))
            return 1
        release_lock(force=True)
        log("手动清除残留锁（pid=%s）" % state["info"].get("pid"), "WARN")
        print("残留锁已清除")
        return 0
    if arg == "--check":
        cfg = cfgmod.normalize(cfgmod.load())
        ok, errs = cfgmod.validate(cfg)
        print(json.dumps({"ok": ok, "errors": errs}, ensure_ascii=False, indent=2))
        if ok and os.path.isdir(cfg["dst_dir"]):
            s = scan_source(cfg["src_dir"])
            free, need = precheck_space(cfg["dst_dir"], s, cfg, int(cfg["keep_versions"]))
            print("文件数 %d，预估包 %.2f GiB，可用 %.2f GiB"
                  % (s["count"], need / 2**30, free / 2**30))
        return 0 if ok else 1
    try:
        run(task=arg.lstrip("-") or "manual")
        return 0
    except BackupError as e:
        print(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
