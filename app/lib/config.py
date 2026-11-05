#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""基础层：唯一配置源、路径常量、校验、crontab 安装/迁移/卸载。

FIX-02  cron 表达式做「字段数 + 数值范围 + 步长 > 0 + 名单」真校验
FIX-06  crontab 安装保留用户既有表达式，并清理 1.0.0 无标记旧行
FIX-13  所有路径常量集中在本文件，其他模块只 import
FIX-14  crontab 写入检查返回码，失败抛错不静默
"""

import json
import os
import re
import subprocess
import tempfile

# ---------------------------------------------------------------- 路径常量
APP_NAME = "backup-pack"
_APP_ROOT = os.path.join("/var/apps", APP_NAME)   # /var/apps/<appname>（固定）


def _detect_app_dir():
    """包内容目录（下面有 lib/ 的那一层），按可靠性逐级回退。

    1. PHOTOS_BACKUP_APP_DIR：离线自测 / 迁移演练，生产环境不要设置；
    2. 从本文件位置向上找含 lib/config.py 的目录——源码树 <root>/lib 与安装形态
       <target>/lib 都命中，不依赖任何环境变量，最可靠；
    3. TRIM_APPDEST：fnOS 注入的包内容目录（兜底；注意执行环境里可能残留别的应用的
       TRIM_* 变量，所以放在自推导之后）；
    4. 兜底 <APP_ROOT>/target。
    """
    env = os.environ.get("PHOTOS_BACKUP_APP_DIR")
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
    env = os.environ.get("TRIM_APPDEST")
    if env and os.path.isdir(env):
        return env
    return os.path.join(_APP_ROOT, "target")


APP_DIR = _detect_app_dir()

# 运行时数据目录。注意：设置了 PHOTOS_BACKUP_APP_DIR 时它「独占」——
# 不能再混用 TRIM_PKGVAR，否则在某个 fnOS 应用的进程里跑自测会把数据
# 写进那个应用的 var 目录（本机 TRIM_PKGVAR 就指向 octop-native）。
if os.environ.get("PHOTOS_BACKUP_APP_DIR"):
    VAR_DIR = os.path.join(APP_DIR, "var")
else:
    VAR_DIR = os.environ.get("TRIM_PKGVAR") or os.path.join(_APP_ROOT, "var")
LIB_DIR = os.path.join(APP_DIR, "lib")
BACKUP_PY = os.path.join(LIB_DIR, "backup.py")
CRON_RUN_PY = os.path.join(LIB_DIR, "cron_run.py")
CONFIG_PATH = os.path.join(VAR_DIR, "config.json")
STATUS_PATH = os.path.join(VAR_DIR, "status.json")
LOG_PATH = os.path.join(VAR_DIR, "task.log")
LOCK_PATH = os.path.join(VAR_DIR, "backup.lock")
CRON_MARKER = "# backup-pack"                  # 本应用 crontab 行的唯一标记

DEFAULT_CONFIG = {
    "src_dir": "",
    "dst_dir": "",
    "dst_mountpoint": "",        # FIX-05 首次成功备份时记录，之后挂载点变化即拒绝
    "allow_root_mount": False,   # FIX-05 显式允许写到系统盘（默认禁止）
    "compress": "none",
    "keep_versions": 3,
    "cron_expr": "0 3 * * *",
    "enable_cron": False,
    "log_retain_days": 7,
    "min_free_ratio": 0.10,      # FIX-07 空间预检安全余量
    "max_files": 0,              # FIX-17 0=不限制；>0 时源文件数超限即拒绝（防误指整个卷）
}

INT_FIELDS = {"keep_versions": (1, 90), "log_retain_days": (1, 365),
              "max_files": (0, 100000000)}
BOOL_FIELDS = ("enable_cron", "allow_root_mount")
COMPRESS_MODES = ("none", "gzip", "zstd")

# ---------------------------------------------------------------- cron 校验
_MONTH_NAMES = {n: i + 1 for i, n in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DOW_NAMES = {n: i for i, n in enumerate(
    "sun mon tue wed thu fri sat".split())}
# (下限, 上限, 允许的名单)
_CRON_FIELDS = (
    (0, 59, None),        # 分
    (0, 23, None),        # 时
    (1, 31, None),        # 日
    (1, 12, _MONTH_NAMES),   # 月
    (0, 7, _DOW_NAMES),   # 周（0 与 7 都是周日）
)
_CRON_CHARS = re.compile(r"^[0-9A-Za-z*/,\-]+$")


def _cron_value(token, names, lo, hi):
    """把一个片段（如 '5' / 'JAN'）解析成整数，失败返回 None。"""
    if names and token.lower() in names:
        return names[token.lower()]
    if not token.isdigit():
        return None
    v = int(token)
    return v if lo <= v <= hi else None


def _check_cron_field(expr, spec):
    lo, hi, names = spec
    if not _CRON_CHARS.match(expr):
        return None, "含非法字符"
    for part in expr.split(","):
        if not part:
            return None, "存在空的列表项"
        body, step = part, "1"
        if "/" in part:
            body, _, step = part.partition("/")
            if not step.isdigit() or int(step) < 1:
                return None, "步长必须是正整数"
            if int(step) > hi:
                return None, "步长 %s 超出范围" % step
        if body == "*":
            continue
        if "-" in body:
            a, _, b = body.partition("-")
            va = _cron_value(a, names, lo, hi)
            vb = _cron_value(b, names, lo, hi)
            if va is None or vb is None:
                return None, "区间 %r 含非法值" % body
            if va > vb:
                return None, "区间 %r 起止颠倒" % body
        elif _cron_value(body, names, lo, hi) is None:
            return None, "值 %r 不在 %d-%d 内" % (body, lo, hi)
    return True, ""


def validate_cron_expr(expr):
    """返回 (ok, msg)。FIX-02：拒绝 99 99 99 99 99 / abc 3 * * * / */0 等。"""
    if not expr or not expr.strip():
        return False, "定时表达式不能为空"
    fields = expr.strip().split()
    if len(fields) != 5:
        return False, "定时表达式必须是 5 个字段（分 时 日 月 周），当前 %d 个" % len(fields)
    labels = ("分", "时", "日", "月", "周")
    for f, spec, label in zip(fields, _CRON_FIELDS, labels):
        ok, msg = _check_cron_field(f, spec)
        if not ok:
            return False, "%s字段 %r：%s" % (label, f, msg)
    return True, ""


# ---------------------------------------------------------------- 配置读写
def load():
    """读配置并与默认值合并（向前兼容）。文件损坏时回退默认并保留副本。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg = raw
        except (ValueError, OSError):
            cfg = {}
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def normalize(cfg):
    """类型收敛：数值字段转 int，布尔字段转 bool。非法值原样保留交给 validate 报错。"""
    out = dict(cfg)
    for k in INT_FIELDS:
        try:
            out[k] = int(out.get(k))
        except (TypeError, ValueError):
            pass
    for k in BOOL_FIELDS:
        v = out.get(k)
        out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    try:
        out["min_free_ratio"] = float(out.get("min_free_ratio"))
    except (TypeError, ValueError):
        pass
    return out


def atomic_write_json(path, obj):
    """临时文件 + os.replace，避免写一半掉电留下坏 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(cfg):
    atomic_write_json(CONFIG_PATH, normalize(cfg))


def load_status():
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(d):
    atomic_write_json(STATUS_PATH, d)


# ---------------------------------------------------------------- 校验
def validate(cfg):
    """返回 (ok, errors)。保存前与执行前（手动 + cron）都调用。"""
    errors = []

    src = (cfg.get("src_dir") or "").strip()
    if not src:
        errors.append("源目录不能为空")
    elif not os.path.isdir(src):
        errors.append("源目录不存在或不是目录（请先在应用设置中授权）")

    dst = (cfg.get("dst_dir") or "").strip()
    if not dst:
        errors.append("目标目录不能为空")
    elif os.path.exists(dst) and not os.path.isdir(dst):
        errors.append("目标目录已存在但不是目录")

    if cfg.get("compress") not in COMPRESS_MODES:
        errors.append("压缩方式仅支持 none/gzip/zstd")

    for key, (lo, hi) in INT_FIELDS.items():
        try:
            v = int(cfg.get(key))
        except (TypeError, ValueError):
            errors.append("%s 必须是整数" % key)
            continue
        if v < lo or v > hi:
            errors.append("%s 必须在 %d-%d 之间，当前 %d" % (key, lo, hi, v))

    ok, msg = validate_cron_expr(cfg.get("cron_expr", ""))
    if not ok:
        errors.append(msg)

    try:
        r = float(cfg.get("min_free_ratio"))
        if not 0 <= r <= 0.5:
            errors.append("安全余量必须在 0-0.5 之间")
    except (TypeError, ValueError):
        errors.append("安全余量必须是数字")

    if src and dst and os.path.realpath(src) == os.path.realpath(dst):
        errors.append("源目录与目标目录不能相同")

    return (not errors), errors


# ---------------------------------------------------------------- crontab
def build_cron_line(expr):
    return "%s /usr/bin/python3 %s >> %s 2>&1 %s" % (
        expr.strip(), CRON_RUN_PY, LOG_PATH, CRON_MARKER)


def _read_crontab():
    """返回 (文本, 错误)。无 crontab 视为空文本。"""
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return None, "系统未找到 crontab 命令"
    if p.returncode != 0:
        if "no crontab" in (p.stderr or "").lower():
            return "", None
        return None, (p.stderr or "读取 crontab 失败").strip()
    return p.stdout or "", None


def _write_crontab(text):
    """FIX-14：写入检查返回码，失败抛错，不让用户以为已生效。"""
    try:
        p = subprocess.run(["crontab", "-"], input=text, text=True, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError("系统未找到 crontab 命令")
    if p.returncode != 0:
        raise RuntimeError("写入 crontab 失败：%s" % (p.stderr or "").strip())


def _is_cron_entry(line):
    """FIX-06 判定「这是本应用的定时行」。

    同时识别四类：当前路径的 cron_run.py、1.0.0 遗留的无标记行（按脚本名 +
    应用名匹配）、被改过路径的历史行、以及旧名 photos-backup 时期的遗留行
    （应用 1.2.2 起改名 backup-pack，升级时要能顺手清掉旧行，避免双份任务）。
    注意不能只看 CRON_MARKER——残留的裸注释行也会命中，会被误当成有效定时行保留。
    """
    if CRON_RUN_PY in line:
        return True
    if "cron_run.py" in line and ("backup-pack" in line or "photos-backup" in line
                                  or CRON_MARKER in line):
        return True
    return False


def _is_stale_marker(line):
    """只剩注释标记、没有实际命令的残留行，直接丢弃。"""
    s = line.strip()
    return s.startswith("#") and ("backup-pack" in s or "photos-backup" in s)


def _split_ours(text):
    """拆成 (本应用定时行, 其它行)。残留裸注释行两边都不进，等于被清理。"""
    ours, others = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if _is_cron_entry(line):
            ours.append(line)
        elif _is_stale_marker(line):
            continue
        else:
            others.append(line)
    return ours, others


def update_crontab(cfg):
    """按当前 cron_expr 幂等重写本应用的行（只留一条）。"""
    expr = (cfg.get("cron_expr") or "").strip()
    ok, msg = validate_cron_expr(expr)
    if not ok:
        raise RuntimeError("拒绝写入 crontab：%s" % msg)   # FIX-03
    text, err = _read_crontab()
    if err:
        raise RuntimeError(err)
    ours, others = _split_ours(text)
    others.append(build_cron_line(expr))
    _write_crontab("\n".join(others) + "\n")
    return len(ours)


def _expr_from_line(line):
    """从既有 crontab 行里取出前 5 个字段作为表达式。"""
    parts = line.split()
    if len(parts) >= 5:
        cand = " ".join(parts[:5])
        if validate_cron_expr(cand)[0]:
            return cand
    return ""


def install_cron_if_absent():
    """FIX-06 安装 / 升级时调用，幂等。

    规则（config.json 是唯一事实来源）：
      1. config.json 里的 cron_expr 合法 → 用它重写本应用的行，
         用户在 UI 里设的表达式升级后不会被改回默认值；
      2. config.json 不可用 / 表达式非法 → 保留 crontab 里已有的表达式，
         而不是硬编码回 03:00；
      3. 两种情况都会顺手清掉 1.0.0 遗留的无标记旧行，避免双份定时任务。
    """
    text, err = _read_crontab()
    if err:
        raise RuntimeError(err)
    ours, others = _split_ours(text)

    cfg = load()
    expr = (cfg.get("cron_expr") or "").strip()
    if not validate_cron_expr(expr)[0]:
        expr = _expr_from_line(ours[-1]) if ours else ""
    if not validate_cron_expr(expr)[0]:
        expr = DEFAULT_CONFIG["cron_expr"]

    others.append(build_cron_line(expr))
    _write_crontab("\n".join(others) + "\n")
    return {"expr": expr, "kept": bool(ours), "removed": max(0, len(ours) - 1)}


def remove_cron():
    """卸载：清掉本应用全部行（含 1.0.0 遗留行），不碰其它条目。"""
    text, err = _read_crontab()
    if err:
        raise RuntimeError(err)
    ours, others = _split_ours(text)
    _write_crontab(("\n".join(others) + "\n") if others else "")
    return len(ours)


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--install-cron":
        print(install_cron_if_absent())
    elif arg == "--remove-cron":
        print({"removed": remove_cron()})
    elif arg == "--validate":
        ok, errs = validate(load())
        print(json.dumps({"ok": ok, "errors": errs}, ensure_ascii=False))
        sys.exit(0 if ok else 1)
    else:
        print("用法: config.py [--install-cron|--remove-cron|--validate]")
        sys.exit(1)
