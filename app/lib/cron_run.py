#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""调度层：由系统 cron 调用。负责开关判断并把参数交给领域层。

FIX-10  与手动路径一样先过 config.validate()，配置坏了不硬跑
FIX-09  跳过/失败都写日志与 status，杜绝「静默不备份」
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import config
import backup


def main():
    cfg = config.normalize(config.load())

    if not cfg.get("enable_cron"):
        return 0                                  # 开关关闭：条目保留但不执行

    ok, errs = config.validate(cfg)
    if not ok:
        msg = "配置校验未通过，跳过本次定时备份：" + "；".join(errs)
        backup.log(msg, "ERROR")
        backup._record_failure(msg)
        return 2

    try:
        backup.run(cfg, task="cron")
        return 0
    except backup.BackupError as e:
        backup.log("定时备份失败：%s" % e, "ERROR")
        return 2
    except Exception as e:                        # 任何异常都必须留痕
        backup.log("定时备份出现未预期异常：%r" % (e,), "ERROR")
        backup._record_failure("未预期异常：%s" % e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
