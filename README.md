# 备份打包（backup-pack）

把「几十万到上百万个小文件」的目录（典型场景：飞牛相册的缩略图目录）打包成**单个可校验的备份包**，
支持定时执行、保留 N 份、空间预检、挂载点守卫、产物四态与运行状态可见。

- 版本：`1.2.5`　平台：`x86`　形态：fnOS 原生 FPK + **常驻端口服务**（CGI 通道保留备用）
- 应用身份：`appname = backup-pack`　显示名：**备份打包**　桌面入口：`backup-pack.Application`
- 服务端口：`45182`（`manifest.service_port`，入口走 `protocol+port`）
- 运行时依赖：系统 `tar`、`sha256sum`、`crontab`；可选 `zstd`；`/usr/bin/python3`（3.11+，不依赖 `cgi` 模块）

> **本应用备份的是缩略图等派生数据，不含原图，不能替代原图备份。**
> 缩略图可重建，本工具的定位是「降低重建成本」，不是「数据保险」。

---

## 1. 为什么是端口服务而不是纯 CGI

1.2.1 及之前用「`app/ui/index.cgi` + `ui/config` 里 `url=/cgi/ThirdParty/...`」的纯 CGI 形态，
真机结果是：**应用卡片只有「卸载」、桌面无图标、无「打开」**。

原因是这版 fnOS 的桌面入口只在入口配置带 `protocol` + `port` 时才注册（本机
`fn-ups-manager` 走 51824 端口；它的 CGI 入口在 nginx 历史访问日志里从未被浏览器请求过）。
所以改为常驻 HTTP 服务，桌面入口指向 `http://<NAS-IP>:45182/`。

- `app/server/server.py`：标准库 `ThreadingHTTPServer`，只做 HTTP 外壳
- `app/ui/index.cgi`：把路由抽成纯函数 `route(method, params) -> (status, headers, body)`
- 两条通道调同一个 `route()`，行为与错误语义完全一致；CGI 通道保留，方便纯 CLI/无端口环境

代价：多一个常驻进程（空闲时 RSS 约十几 MB），且**端口服务不受 NAS 登录态保护**——
仅限可信内网，见第 7 节。

## 2. 真机落地形态

```text
/var/apps/backup-pack/                  # 应用根（fnOS 创建）
├── manifest  cmd/  config/  ICON*.PNG
├── target -> /vol1/@appcenter/backup-pack
│   ├── ui/                    # ← 源码 app/ui：入口配置 + CGI 备用入口
│   │   ├── config             # backup-pack.Application → http:45182 /
│   │   ├── index.cgi          # CGI 备用入口（route 宿主）
│   │   └── images/            # icon_64.png / icon_256.png
│   ├── server/server.py       # ← 源码 app/server：常驻 HTTP 服务
│   ├── lib/
│   │   ├── config.py          # 常量 / 配置 / 校验 / crontab / status
│   │   ├── backup.py          # 锁 / 预检 / 打包 / 校验 / 清理 / 日志
│   │   └── cron_run.py        # 系统 cron 入口
│   └── config/                # privilege / resource
└── var/ -> /vol1/@appdata/backup-pack
    ├── config.json  status.json  task.log  backup.lock
    └── server.pid  server.log
```

`app/` 对应安装后的 `target/`：**`lib/` 必须放在 `app/lib/`**，放在项目根不会被打进 `app.tgz`
（踩过一次，安装后「有界面没代码」）。

路径不写死：按自身位置推导（`<target>/lib`、`<target>/server`）→ `PHOTOS_BACKUP_APP_DIR`（自测）
→ `TRIM_APPDEST`（兜底）；数据目录用 `TRIM_PKGVAR`，自测时 `<APP_DIR>/var`。

## 3. 构建与安装

```bash
sh scripts/bump_version.sh 1.2.3    # 一次改到位：manifest / index.cgi / CHANGELOG(插条目)
# 补完 CHANGELOG →
sh scripts/build.sh                 # 三处版本校验 → 打包 → backup-pack-1.2.3.fpk + .sha256
```

**版本是硬约束**：`scripts/build.sh` 会比对 `manifest.version`、`app/ui/index.cgi` 的 `VERSION`、
`CHANGELOG.md` 首条标题，不一致直接拒绝打包；产物文件名必带版本，旧的无版本号产物会被清理。

安装：应用中心 → 手动安装 → 选 fpk。生命周期：

| 脚本 | 做什么 |
|---|---|
| `cmd/install_init` | 建 `var/` 并 `chown` 给包用户（不碰 crontab） |
| `cmd/install_callback` | 以包用户身份 `config.py --install-cron`，幂等同步定时行 |
| `cmd/main` | `start` 起 HTTP 服务（写 `server.pid`）、`stop` 停、`status` 用端口探测 |
| `cmd/upgrade_*` | 同上，保留配置、备份包与用户 cron 表达式 |
| `cmd/uninstall_callback` | 只清本应用 cron 行，配置/日志/备份包全部保留 |

## 4. 首次配置

1. 应用「设置」里把**源目录**与**备份目标目录**授权给本应用（fnOS 应用级 ACL，
   这里用的是 fnOS 自带的目录选择对话框）。
2. 打开「备份打包」→ 点路径右侧的**「选择…」**，在目录浏览器里逐级进入并「选此目录」；
   也可直接粘贴路径。
   - 首次成功备份会记录挂载点；之后挂载点变化（外接盘掉线）会拒绝写入。
   - 目标落在 `/` 时默认拒绝，除非勾选「允许写入系统盘」。
3. 容量：单包 `P ≈ 源大小 × 1.026`，需求 `D ≈ P × keep_versions × (1 + min_free_ratio)`。
   keep=3 时 60 GB 源约需 203 GB，**不要按源大小买盘**。
4. 定时：选频率（每天 / 每周 / 每月）+ 时间控件（每周可多选星期，每月选 1-28 日），
   下方实时显示将写入的表达式，如「将使用：`15 2 * * *`（每天 02:15）」；
   复杂表达式（`*/15 2-6 * * 1`）会落到「高级：手动填写 cron 表达式」，原值不被改写。
5. 点「立即备份」跑一次，确认列表出现「完整」与 `.sha256`。

## 4.1 目录选择：调用 fnOS 原生选择框

点路径右侧的「选择…」会弹出 **fnOS 原生目录选择框**（与「文件查重 → 新建任务」同一套能力），
选完自动回填并完成该目录对应用的授权。

实现要点：

- fnOS 桌面通过 penpal 桥向 iframe 应用暴露 `pickFile / pickUserFile / pickSharedFile /
  authorizeUserFile / openFileManager …`；本应用用 `pickSharedFile`（选目录 + 授权），
  `sidebarGroup` 开放 `myFiles / otherShare / external / remote / favorites / team`。
- SDK 放在 `app/ui/assets/fnnas-sdk.js`，来源与「上架时换官方分发版」的说明见同目录 `README.txt`；
  `server.py` 提供 `/assets/` 白名单静态服务。
- **回退**：SDK 加载失败或不在 fnOS 桌面环境（直接用浏览器开端口）时，按钮自动退回内置目录浏览器
  （只列可访问目录，仍受授权范围约束）。

## 4.2 应用鉴权：为什么 `config/resource` 必须声明 api-scope

fnOS 的目录选择器与文件访问**不是"能调就能用"**：宿主会校验调用方的应用鉴权。
校验失败时返回的是 `{code:-1, msg:"应用鉴权异常"}`，界面上表现为「按钮点了没反应」。

所以 `config/resource` 必须声明：

```json
{
  "api-scope": [
    "trim.file.sharedAccess",   // 共享目录 / 外接盘（典型：备份目标 /vol2）
    "trim.file.userAccess",     // 用户空间（典型：相册缩略图 /vol1/1000/...）
    "trim.file.userAcl",        // 用户目录 ACL 读取
    "trim.file.path"            // 任意路径访问（备份读写）
  ]
}
```

**安装时** fnOS 会依此生成鉴权并落库，可这样核对：

```bash
sudo -u postgres psql -d appcenter -tAc \
  "select app_name, length(token), api_scope from app_auth where app_name='backup-pack';"
sudo -u postgres psql -d appcenter -tAc \
  "select scope, status from app_scopes where app_name='backup-pack';"
```

没有 `app_auth` 记录 = 权限没生效，此时任何 `pick*` 调用都会被拒。

> 另一个坑：`code:-1` 的语义是**失败**，不是取消。宿主取消时 `msg` 是 `Cancelled`，
> 但「应用鉴权异常」「Operation failed」同样是 `code:-1`。判定取消必须结合 `msg`，
> 否则真实错误会被当成"用户取消"静默掉——这正是 1.2.10 之前"点了没反应"的直接原因。

## 5. 日常使用

- 状态区：上次成功/距今 N 天（超 `keep_versions` 天标红）、最近失败原因、锁持有者、目标挂载点。
- 四态列表：`完整` / `近似快照` / `未知` / `无效`（缺 `.sha256`，不计 keep、不自动删）。
- 校验只证明「文件读回一致」，不代表内容完整可恢复。
- 自救：「清除残留锁」（死进程立即回收）、「清理无效产物」（POST）。

## 6. 命令行排障

```bash
APP=/var/apps/backup-pack
python3 $APP/target/lib/backup.py --check       # 配置 / 扫源 / 空间预检（不动手）
python3 $APP/target/lib/backup.py --unlock      # 仅清除失效锁
python3 $APP/target/lib/backup.py               # 手动跑一次（与 Web/cron 同一把锁）
python3 $APP/target/lib/config.py --validate    # 只校验配置
crontab -l | grep backup-pack                   # 定时行（包用户的 crontab）
curl -s http://127.0.0.1:45182/?action=status   # 服务健康检查
tail -n 50 $APP/var/task.log                    # 没有「完成：」= 这次没成功
tail -n 50 $APP/var/server.log                  # 服务启动/请求日志
su - backup-pack -c '/usr/bin/python3 /var/apps/backup-pack/target/lib/backup.py --check'
```

## 7. 安全边界（必读）

- **端口 45182 无鉴权**：能连到该端口的人可改目标目录、触发打包。仅限可信内网；
  不要映射公网。需要外网访问时在反代层加认证。
- CGI 通道（`/cgi/ThirdParty/backup-pack/index.cgi/`）受 NAS 登录态保护，可作为受限替代入口。
- 入口 `allUsers=false`，只对管理员可见。

## 8. 自测（三套，沙箱，不碰真实 crontab）

```bash
mkdir -p /tmp/pbtest/{app,bin,dst,src,tmp}
cp -a app/. /tmp/pbtest/app/
python3 -c "import os
for i in range(500):
    d='/tmp/pbtest/src/sub%d'%(i%10); os.makedirs(d,exist_ok=True)
    open(d+'/f%03d.jpg'%i,'wb').write(b'x'*20480)"

cat > /tmp/pbtest/bin/crontab <<'EOF'
#!/bin/sh
F=/tmp/pbtest/crontab.txt
if [ "$1" = "-l" ]; then
  [ -f "$F" ] || { echo "no crontab for $(id -un)" >&2; exit 1; }
  cat "$F"; exit 0
fi
if [ "$1" = "-" ]; then cat > "$F"; exit 0; fi
exit 1
EOF
chmod +x /tmp/pbtest/bin/crontab

export PATH=/tmp/pbtest/bin:$PATH PHOTOS_BACKUP_APP_DIR=/tmp/pbtest/app
bash tests/acceptance.sh      # 45 项：命令行侧
python3 tests/cgi_test.py     # 30 项：CGI 通道
python3 tests/http_test.py    # 26 项：HTTP 通道（桌面入口实际走这条）
```

当前实测 **101 项断言全绿**（Python 3.11.2 / GNU tar 1.34 / zstd 可用）。

## 9. 版本规范与历史

- 每次改动必须升版本：`sh scripts/bump_version.sh X.Y.Z`
- 三处版本必须一致（`manifest` / `index.cgi` / `CHANGELOG` 首条），否则拒绝打包
- 产物固定 `<appname>-<version>.fpk`

| 版本 | 变更 |
|---|---|
| 1.2.5 | 目录选择改用 fnOS 原生选择框（SDK `pickSharedFile`），内置浏览器降为回退；`/assets/` 白名单静态服务 |
| 1.2.4 | 源/目标目录加「选择…」目录浏览器（`action=roots`/`ls`，越界拒绝）；定时改为频率 + 时间控件，复杂表达式落「高级」；修代理对导致的 HTTP 500 |
| 1.2.3 | `ICON.PNG` 提到 256×256（应用中心列表取 `icon.png`，之前 64×64 显示成默认图标）；PNG 补 sRGB/gAMA；`ui/images` 增 `icon.png` |
| 1.2.2 | 更名「备份打包」(backup-pack)；改端口服务形态（45182）修桌面无入口/无「打开」；路由抽成 `route()` 双通道共用；新增 HTTP 测试 |
| 1.2.1 | 入口 ID 改 `photos-backup.Application`（未解决纯 CGI 入口不被注册的问题） |
| 1.2.0 | 首个可交付版本（实现缺陷修复 + fnOS 形态适配） |
