# CHANGELOG

## v1.2.12（2026-11-05）

真机反馈（管理员账户）：选择框里看不到「管理员视角」的文件夹，而相册缩略图与系统备份
恰好都放在那些位置，选不到等于没用。

### 根因：`sidebarGroup` 漏了 `storage`

从 fnOS 前端 bundle 里挖到侧栏分组的枚举：

```
MyFiles=myFiles   Team=team   OtherShare=otherShare
Storage=storage   External=external   Remote=remote   Favorites=favorites
```

`storage`（存储空间）是**管理员视角才出现**的分组，也是唯一能看到 `/vol1`、`/vol2`
以及 `@appdata`（相册缩略图）、`_sysbackup`（系统备份）这类目录的分组。
我上一版只传了 `myFiles / otherShare / external / remote / favorites / team`，
所以即便用管理员账户，也只能在「我的文件 / 团队 / 外接 / 远程」里打转。

顺带发现两处相关限制：

- `allowToStorageRoot` / `allowToStorageSubDir`：不打开时，存储空间根与二级目录不可选
  （备份目标想选 `/vol2` 就会被拦）；
- `pickSharedFile` 在宿主里是**管理员专用**（源码 `j && !isAdmin → error`），
  非管理员账户本就不该走这个方法。

### 变更

1. `SIDEBAR_ALL` 改为 `["myFiles", "team", "otherShare", "storage", "external", "remote"]`
   （补 `storage`，补齐 `team`/`otherShare` 两种写法）；
2. 源目录与目标目录都开放全部六个分组；
3. 两个 pick 调用都加 `allowToStorageRoot: true`、`allowToStorageSubDir: true`，
   使 `/vol2`（存储空间根）与 `@appdata` 下的缩略图目录可选；
4. 注释写明 `storage` 与管理员视角的关系，避免以后又被"优化"掉。

### 验证

页面渲染确认 `SIDEBAR_ALL` 含 `storage`、`allowToStorageRoot: true` 出现 2 处；
三套回归仍 **127 项全绿**。


## v1.2.11（2026-11-05）

真机反馈：源目录按钮能用，**目标目录按钮点了完全没反应**。

日志（`[CLIENT]` 打点）把真相摊开了——两个按钮其实**都失败了**，只是被上一版的
「取消判定」静默吞掉：

```
pick-click src_dir/user   → pickUserFile   → {code:-1, msg:"Operation failed"}
pick-click dst_dir/shared → pickSharedFile → {code:-1, msg:"应用鉴权异常"}
```

### 根因一：缺少 api-scope 声明（fnOS 应用鉴权）

对比 `appcenter` 库：

| 表 | file.dedupe | backup-pack（修复前）|
|---|---|---|
| `app_auth` | 有 token（89 字节）+ 2 个 api_scope | **无记录** |
| `app_scopes` | `trim.file.sharedAccess`、`trim.file.path` | **无记录** |

而 file.dedupe 的 `config/resource` 是：

```json
{ "api-scope": ["trim.file.sharedAccess", "trim.file.path"] }
```

我们的 `config/resource` 是空对象 `{}` —— 没有声明文件访问权限，宿主校验时就报
「应用鉴权异常」，`pickSharedFile` / `pickUserFile` 全部失败。

**修复**：`config/resource` 声明四个 scope：

```json
{ "api-scope": [
    "trim.file.sharedAccess",   // 共享目录 / 外接盘（目标目录）
    "trim.file.userAccess",     // 用户空间（源目录，/vol1/1000/...）
    "trim.file.userAcl",        // 用户目录 ACL 读取
    "trim.file.path"            // 任意路径访问（备份读写）
] }
```

### 根因二：`code:-1` 一律当取消

RAWArchiver 的取消样本是 `{code:-1, msg:"Cancelled"}`，但 `code:-1` 的语义是「失败」，
取消只是其中一种。我把 `code:-1` 无条件判成 cancel，于是「应用鉴权异常」被当成用户取消
→ 静默 return true → 页面什么都不显示，看起来就是「按钮没反应」。

**修复**：`code:-1` 时看 `msg`——含 `cancel/取消/abort/dismiss/close` 才算取消，
否则按错误处理并显示诊断条。

### 测试

三套回归 **127 项全绿**；重新安装后 `app_auth` / `app_scopes` 应出现 backup-pack 记录
（安装时由 fnOS 依 api-scope 声明生成）。


## v1.2.10（2026-11-05）

真机反馈：原生选择框能弹出了，但**点取消/关闭后，页面又弹出了自己的内置浏览器**。

根因：把用户的「取消」当成了系统故障。取消被归一化成 `bridge`/`shape` 错误 →
降级链继续试下一个方法 → 最终 throw → 上层 `return false` → 回退内置浏览器。
在原生框已经弹过之后，再塞一个自绘弹窗既没价值又反直觉。

按「AI 编程实战导师」提供的 RAWArchiver（Raw 归档应用）实测结论重做了判定与回退策略。

### RAWArchiver 的实测取消样本

| 方法 | 取消时返回 |
|---|---|
| `pickSharedFile` | `{code:-1, msg:'Cancelled', data:[]}`（宿主 `K7` 归一化，code 固定 -1）|
| `pickUserFile` | 同上，或 `{code:0, data:[]}` |
| `pickFile` | 直接返回空数组 `[]` |

### 变更

1. **新增 `isCancelResp()`**：覆盖 `code:-1` / `code:0 && data 无非路径项` / 非 0 code + 空 data /
   空 msg / `msg` 含 cancel·取消·abort·dismiss·close 六种形态；
2. **降级链遇取消立即终止**（不再 `continue` 试下一个方法）——之前会因此再弹一次原生框；
3. **降级链错误分类**：
   - `Host bridge is not available` / 握手超时 / SDK 加载失败 → `unavailable`（唯一回退）
   - `NotSupportedInExtensionHost` / `is not a function` → 只保留 `pickFile` 再试一次（该分支只有它是真实现）
   - 其它报错 → 立即 `error`（换方法也不会好）
4. **回退点收敛为 3 个**：① SDK 加载/握手失败 ② `isStandaloneWeb` ③ 桥明确不可用。
   用户取消、单次调用失败、超时**一律不回退**，只留诊断条；
5. **取消/失败时输入框保持原值**（`before` 变量），不再写空；
6. 所有 pick 参数补 **`disabledPaths: []`**（宿主默认会把已授权目录置灰，不传会导致用户重选同一目录时点不动）；
7. `T_PICK` 由 120s 调整为 **180s**（与 file.dedupe 一致，避免用户翻外接盘时误超时）。

### 验收（按导师给的四条）

| # | 操作 | 期望 |
|---|---|---|
| 1 | 点「选择…」→ 原生框 → 取消 | 无第二个弹窗；输入框值不变；日志有 `pick-cancel` + `pick-cancel-done` |
| 2 | 连续取消 3 次 | 每次干净退出，不累积监听、不残留诊断条 |
| 3 | 直接浏览器打开（standalone） | 回退内置浏览器（回退点 ②，符合预期） |
| 4 | 改名 `fnnas-sdk.js` 模拟加载失败 | 回退内置浏览器 + 诊断条（回退点 ①） |

### 测试

页面 12 项结构检查全通过（isCancelResp / code:-1 / disabledPaths / T_PICK 180000 /
RE_BRIDGE_DEAD / RE_METHOD_GONE / forcePickFile / 三种 status / before 保原值），
classic 与 module 两段 JS `node --check` 通过；三套回归仍 **127 项全绿**。


## v1.2.9（2026-11-05）

按「AI 编程实战导师」的评审结论重做了原生目录选择器的接入。评审指出 4 个独立缺陷，
其中前两个各自都足以造成「点了没反应」：

1. **相对路径 import 打错 URL**：`ui/config` 的 `url` 是 `/app/backup-pack`（无尾斜杠），
   页面里 `import("./assets/fnnas-sdk.js")` 的 base 变成 `/app/`，实际请求
   `/app/assets/fnnas-sdk.js` → 落到 `route()` 默认分支返回整页 HTML（200 + text/html）
   → 浏览器报 MIME 错误，`mod=null`，`window.pickWithSystem` 压根没注册。
2. **`ready()` 永久 pending**：SDK 内部 `De({debug}).promise` 没有 timeout，
   父窗口不回 `SynAck` 就永不 settle，`await app.ready()` 一直挂着 → 既不弹窗也不报错。
3. **`appName` 写死 `file.dedupe`**：影响 standalone 回退与 `authorize*`。
4. **「选到路径」≠「有权限」**：`pick*` 只返回路径字符串，授权要靠 `authorize*`。

### 变更

- 页面 `<head>` 加 `<meta name="app-base" content="$BASE">`，SDK 改用**绝对路径** +
  `?v=<版本>` 动态 import，彻底规避相对路径解析问题；
- `getApp()` 给 `import()` 8s、`ready()` 4s、`pick*` 120s、`authorize*` 60s 四道超时；
  失败后 15s 内不再重试（避免每次点击都新建实例刷 console）；
- SDK 实例开 `debug: true`，握手过程进 console；
- **方法降级链**：源目录 `pickUserFile(directory)` → `pickFile(directory)` → `pickSharedFile`；
  目标目录 `pickSharedFile(creatable)` → `pickFile(directory)` → `pickUserFile`；
  并按 `getWebMethods()` 的结果跳过宿主没暴露的方法；
- 返回值归一化：兼容 `{code,data}` / 数组 / 字符串三种形态，区分「取消」与「失败」；
- 选完调 `authorizeUserFile` / `authorizeSharedFile`（两种入参形态各试一次）；
- 新增 `?action=probe&path=` 服务端实测（`os.access` + 授权范围校验），
  页面对没有权限的目录给出「去应用设置授权」的引导；
- 页面顶部诊断条 + `[CLIENT]` 打点保留（`boot` 里带 `inIframe` / `origin` / UA）。

### 测试

- 渲染检查：meta/绝对路径/按钮 kind/无残留占位符 全部通过；classic 与 module 两段 JS
  用 `node --check` 语法校验通过；
- `action=probe` 自测：范围内目录 `readable/writable=true`，`/etc` 返回 `in_scope=false`；
- 三套回归仍 **127 项全绿**（45 命令行 + 30 CGI + 52 HTTP）。


## v1.2.8（2026-11-05）

真机反馈：同环境下「文件查重」正常，我们不行 —— 这条对照把问题钉死了。

对比 `appcenter` 库的 `app_service` 表（入口注册的真相所在）：

| 字段 | backup-pack | file.dedupe |
|---|---|---|
| type | iframe | iframe |
| url | ://${host}/app/backup-pack | ://${host}/app/file-dedupe |
| gateway_socket | ✓ | ✓ |
| gateway_prefix | ✓ | ✓ |
| **is_admin** | **t** | **f** |

`is_admin = t` 会让入口变成「仅管理员」，FN Connect 通道下就报
「FN Connect 暂无权限访问该服务」。它的来源是 `app/ui/config` 里的 `allUsers: false`。

### 变更

- `app/ui/config`：`allUsers: false` → **`true`**（与 file.dedupe 对齐）；
- 其余不变（1.2.7 的前端打点保留，用于继续定位选择按钮问题）。

**安全提示**：`allUsers=true` 表示所有 NAS 用户都能打开该入口并修改配置。入口本身受 NAS 登录态保护，
但若只有管理员应能操作，后续可加「仅管理员可保存配置」的应用内判断。

1.2.7 的前端打点仍在：点一次「选择…」后
`sudo grep CLIENT /vol1/@appdata/backup-pack/task.log | tail -20` 能看到执行到哪一步。


## v1.2.7（2026-11-05）

真机反馈：应用中心里能打开页面，但「选择…」点了没反应；桌面图标打开提示
「FN Connect 暂无权限访问该服务」。

这一版先解决**可观测性**，把"点了没反应"变成"日志里有结论"：

1. 新增 `?action=diag` 接口：前端各阶段上报，服务端写进 `var/task.log`（前缀 `[CLIENT]`）；
2. 页面在 `boot` 阶段上报运行环境：`location.href` / `origin` / 是否在 iframe /
   `window.parent` 是否同源可读 / UA —— 用来判断是不是 iframe sandbox 或跨源问题；
3. `pickDir` 全链路打点：`pick-click` → `sdk-missing` / `pick-error` / `sdk-ready-fail` /
   `pick-resp`（SDK 实际返回体）→ 回退；
4. 页面顶部加一条**常驻诊断条**（只在出问题时显示），替代一次性 alert，便于截图反馈。

排障方式：点一次「选择…」，然后
`grep CLIENT /vol1/@appdata/backup-pack/task.log | tail -20`。

### 关于 FN Connect

桌面图标提示「FN Connect 暂无权限访问该服务」属于 fnOS 远程访问（trim-connect）的应用白名单
问题，不是应用内部报错：应用中心走局域网/会话直连能打开，FN Connect 通道未放行 `/app/backup-pack`。
该开关在系统侧（远程访问 → 应用权限），应用包内无法自行声明。

测试：三套回归仍 **127 项全绿**；`action=diag` 自测通过（写入 task.log 可检索）。


## v1.2.6（2026-11-05）

真机反馈：1.2.5 的「选择…」仍然不弹 fnOS 原生目录选择框。

### 根因

fnOS 桌面通过 penpal 桥向 iframe 应用提供 `pickFile / pickSharedFile / …`，但父端会校验
`originForReceiving`——**只接受同源 iframe**。1.2.5 走的是端口服务形态
（iframe src = `http://<nas>:45182/`），与桌面 `http://<nas>:5666` 不同源，握手必然失败，
SDK 随即回退到内置浏览器。`file.dedupe` 能用，是因为它走 **fnOS 统一网关**：
`url = /app/file-dedupe`，与桌面同源。

### 变更

1. **改用 gateway 形态**（与 `file.dedupe` 完全一致）：
   - `ui/config`：`protocol: ""`、`gatewaySocket: "backup-pack.sock"`、
     `gatewayPrefix: "/app/backup-pack"`、`url: "/app/backup-pack"`；
   - `manifest`：去掉 `service_port`，增加 `micro_app = true`；
   - `cmd/main`：启动 `server.py --socket <TRIM_APPDEST>/backup-pack.sock`，
     `status` 用「socket 能否连上」判断，`stop` 清理 socket 文件；
   - `server.py`：新增 `UnixHTTPServer`（HTTP over unix socket），并做网关前缀剥离
     （`BACKUP_PACK_BASE_PATH=/app/backup-pack`，前缀有没有都能正确路由）。
2. 页面改为**动态 import** SDK：加载失败也能捕获，`pickDir` 只在首次失败时提示原因并回退内置浏览器，
   提示文案带具体错误（便于现场反馈）。

### 为什么不干脆保留端口模式

端口模式虽能打开，但拿不到系统选择器，也拿不到 NAS 登录态保护；gateway 模式下页面与桌面同源，
既是调用原生选择器的前提，也顺带把「谁能访问」交回给 fnOS 的会话体系。

### 测试

socket 模式冒烟：无前缀 / 带 `/app/backup-pack` 前缀访问页面、`/assets/fnnas-sdk.js`、
`?action=status` 全部 200；三套回归 **127 项断言全绿**（45 命令行 + 30 CGI + 52 HTTP）。


## v1.2.5（2026-11-05）

按反馈，目录选择改为**调用 fnOS 原生目录选择框**（与「文件查重 → 新建任务」用的是同一套能力）。

### 怎么做到的

fnOS 桌面通过 penpal 桥向 iframe 应用暴露了这些方法：
`pickFile / pickUserFile / pickSharedFile / authorizeUserFile / authorizeSharedFile / openFileManager …`。
其中 `pickSharedFile` 就是「选目录 + 授权」，`file.dedupe` 用的正是它（其回调页里 method 为
`pickSharedFile` 时提示语是「目录授权」）。

- 新增 `app/ui/assets/fnnas-sdk.js`：fnOS 前端 SDK 构建产物（取自本机 `file.dedupe` 的
  `www/assets/fnnas-*.js`，来源与替换说明见 `app/ui/assets/README.txt`）；
- 页面用 `<script type="module">` 引入，点「选择…」直接调
  `pickSharedFile({title:'选择目录', okText:'选择', sidebarGroup:['myFiles','otherShare','external','remote','favorites','team'], creatable:false})`，
  弹出的是 fnOS 原生选择框，选完自动回填路径（并完成该目录的授权）；
- `app/server/server.py` 新增 `/assets/` 白名单静态服务（只服务 `ui/assets` 下的
  js/css/png/svg，`..%2f` 之类穿越一律 404）；
- **保留内置目录浏览器作为回退**：SDK 加载失败或不在 fnOS 桌面环境时，按钮自动退回上一版的自建浏览器，
  不会变成一个点不动的按钮。

### 测试

新增 7 项断言（SDK 静态资源 200/类型/大小、静态目录不可穿越、页面引入 SDK、按钮走 pickDir、
保留回退），合计 **127 项全绿**（45 命令行 + 30 CGI + 52 HTTP）。


## v1.2.4（2026-11-05）

按使用反馈改两处交互。

### 1) 目录选择（源目录 / 目标目录）

fnOS 的文件夹选择器是它自己的 React 内部组件（`filePicker.selectFolder`），只在桌面 SPA 内可用，
没有对第三方 iframe 暴露 API，也没有 postMessage 桥——第三方页面无法直接弹出它。
因此做等价实现，把「系统标准件」放在它真正生效的位置：

- 授权仍然走 **应用设置 → 授权目录**（fnOS 系统对话框），包用户只能访问授权范围内的路径；
- 页面里每个路径输入框右侧加「选择…」按钮，弹出目录浏览器：
  起始位置 = 已授权目录 + 存储卷 + 应用目录（只显示当前账号可读的），逐级进入，点「选此目录」回填；
- 新增后端接口 `action=roots` / `action=ls`：只列目录、不读文件内容，`realpath` 后校验前缀，
  越界（如 `/etc`）与 `../` 穿越一律拒绝，单目录最多返回 500 项。

### 2) 定时执行改为时间控件

不再让用户手写 cron：

- 频率下拉：每天 / 每周 / 每月；
- 时间控件：`<input type="time">`（浏览器原生时间选择）；
- 每周 → 勾选星期（可多选），每月 → 选 1-28 日；
- 实时显示「将使用：`15 2 * * *`（每天 02:15）」；
- 复杂表达式（`*/15 2-6 * * 1` 等）自动落到「高级」手写模式，原值原样保留，不会被静默改写。

### 3) 修复

JS 里用 UTF-16 代理对表示 emoji（`\uD83D\uDCC1`）被 Python 源码解析成孤立代理字符，
CGI 通道因 `errors="replace"` 掩盖，HTTP 通道严格编码直接 500。改为直接写字符，
并在 `server.py` 加编码兜底。

### 测试

`acceptance.sh` 45 + `cgi_test.py` 30 + `http_test.py` 45（新增目录选择、越界拒绝、定时反解、
高级模式保留等 19 项），合计 **120 项断言全绿**。


## v1.2.3（2026-11-05）

真机反馈：应用可正常访问、桌面图标正常，但**应用中心「已安装的应用」列表里显示的是系统默认图标**。

定位：fnOS 应用中心前端取图路径是 `/app-center-static/icon/<appname>/icon.png`（映射到应用根目录的
`ICON.PNG`），而桌面入口用的是 `ui/images/icon_{0}.png`。此前 `ICON.PNG` 只生成了 64x64，
与 `fn-ups-manager`（256x256）不一致。

### 变更

1. `ICON.PNG` 改为 **256x256**，与 `ICON_256.PNG` 同内容；
2. 生成 PNG 时补 `sRGB` + `gAMA` chunk（对齐 fnOS 官方图标，提升第三方解码器兼容性）；
3. `app/ui/images/` 额外产出 `icon.png`（256），作为入口图标的兜底命名；
4. `scripts/make_icons.py` 统一产出：根目录 2 张 + `ui/images` 3 张，避免再漏。

功能与数据格式无变化。


## v1.2.2（2026-11-05）

真机反馈：1.2.1 装完后**应用卡片仍只有「卸载」、桌面无图标、无「打开」**。
排查结论：这版 fnOS 的桌面入口只在入口配置带 `protocol`+`port` 时才会注册
（本机 fn-ups-manager 走 51824 端口；它的 CGI 入口在 nginx 历史访问日志里从未被浏览器请求过）。
纯 CGI 入口不会被登记成可点入口，因此换形态。

### 变更

1. **更名**：`appname` `photos-backup` → `backup-pack`，`display_name` → **备份打包**，
   避免与飞牛相册（trim.photos）混淆；安装路径随之变为 `/var/apps/backup-pack`。
2. **形态改为端口服务**：新增 `app/server/server.py`（标准库 `ThreadingHTTPServer`），
   `cmd/main` 实现 `start/stop/status`，manifest 声明 `service_port = 45182`、`ctl_stop = true`。
3. **两条通道共用一份逻辑**：`app/ui/index.cgi` 的 `main()` 拆为纯函数 `route(method, params)
   -> (status, headers, body)`，CGI 与 HTTP 都调它，行为与错误语义完全一致；CGI 通道保留备用。
4. **入口配置**：`backup-pack.Application`，`type=iframe`、`protocol=http`、`port=45182`、`url=/`。
5. **路径探测去环境变量依赖**：优先按自身位置推导（server 在 `<target>/server/`、lib 在
   `<target>/lib/`），`PHOTOS_BACKUP_APP_DIR` 仅自测覆盖，`TRIM_*` 降为兜底——
   本机执行环境带着别的应用的 `TRIM_PKGVAR/TRIM_APPDEST`，先前会把路径解析到别人的目录。
6. **cron 标记**改为 `# backup-pack`，同时识别并清理旧名 `photos-backup` 的遗留行（避免双份任务）。
7. 页面标题改为「备份打包」，副标题说明「把大量小文件目录（如相册缩略图）打成单个备份包」。

### 测试

- `tests/acceptance.sh` 45 项、`tests/cgi_test.py` 30 项、新增 `tests/http_test.py` 26 项
  （HTTP 通道：渲染 / save / run / list / verify / purge / unlock / 405 / UTF-8 往返），
  合计 **101 项断言全绿**。


## v1.2.1（2026-11-05）

真机反馈：应用中心里没有「打开」命令。对照本机所有第三方应用的入口 ID 后发现，
`desktop_applaunchname` / `.url` 的入口 key 统一为 `<appname>.Application`
（如 `fn-ups-manager.cgi.Application`、`octop-native.Application`），
只有官方应用 trim.photos 使用 `.main`。

修复：

1. 入口 ID `photos-backup.main` → `photos-backup.Application`（`app/ui/config`）；
2. `manifest.desktop_applaunchname` 同步改为 `photos-backup.Application`；
3. 版本号 1.2.0 → 1.2.1（fnOS 不允许同版本覆盖安装）。

功能与数据格式无变化，升级不影响已有 `config.json`、定时任务与备份包。


## v1.2.0（当前）

首个可交付版本，对应项目文档《飞牛相册备份压缩 · 项目文档 v1.2.0》。
以文档第三部分为准落地源码，并在验收过程中修复 4 处「文档承诺 ↔ 实现缺失」的偏差。

### 功能

- 三种打包格式：`.tar`（默认，最快）/ `.tar.gz` / `.tar.zst`。
- 前置校验：配置合法性、源目录非空、目标挂载点未变、空间预检（含 `min_free_ratio` 余量）。
- 单实例：`O_CREAT|O_EXCL` 原子锁 + PID 存活检测 + 60s 心跳；死进程立即回收，活进程不可抢。
- 产物四态：`complete` / `approximate`（tar rc=1）/ `unknown` / `invalid`（缺 `.sha256`）。
- 保留策略：只有带 `.sha256` 的包计入 `keep_versions`；残包失败即删；无校验包不自动删，由 UI 显式清理。
- 校验：`.sha256` 只记 basename，包改名/换机仍可校验（兼容旧绝对路径格式并就地归一）。
- 定时：5 字段真校验（字段数 / 范围 / 步长 / 区间方向 / 月份与星期名单），保存即生效、幂等；非法表达式不碰 crontab。
- 可观测：`status.json`（上次成功时间、失败原因、文件数、包体积、quality）+ UI 状态区 + 日志尾部定长读取。
- 安全：POST 才产生副作用（GET → 405）；模板 `string.Template.safe_substitute`；全部输出 `html.escape`；`verify` 只接受 basename。

### 本次开发修复（相对文档内嵌代码）

1. **`app/ui/index.cgi` 库路径写死**：`sys.path.insert(0, "/var/apps/photos-backup/lib")` 违反 FIX-13，
   改 `appname` / 离线自测即 `ModuleNotFoundError`。改为按脚本位置上溯推导，`PHOTOS_BACKUP_APP_DIR` 仅作自测覆盖。
2. **`max_files` 完全未实现**（文档 8.1、FIX-17、AC-45、RTM 均有承诺）：补配置字段、范围校验、
   扫源后超限拒绝、UI 输入框与回显。
3. **「清理无效产物」缺 UI 入口**：`backup.purge_invalid()` 已存在但无 `action=purge` 路由与按钮，
   FR-10 / AC-46 不可达。补处理函数、POST 路由（GET → 405）与按钮。
4. **压缩包被误判为残包**：`pack()` 用 `size < 10240`（tar 最小记录）判定「产物异常偏小」，
   对 gzip/zstd 同样生效，可压内容压缩后 < 10 KB 时正常包会被删除。改为只有 `compress=none` 使用该下限。

### 测试

- `tests/acceptance.sh`：45 项断言（原 34 项 + 补 AC-04 跨格式保留、AC-02 zstd、AC-45 max_files、AC-46 purge）。
- `tests/cgi_test.py`：29 项断言（原 24 项 + 补 purge 需 POST / 能清无效产物、max_files 保存与回显）。
- 合计 **74 项断言全绿**（Python 3.11.2 / GNU tar 1.34 / zstd 可用 / crontab 沙箱桩）。
- 修复测试脚手架自身缺陷：`setcfg` 在干净沙箱（无 `var/`）下不再崩溃。

### 真机（fnOS）适配修复

对照 fnOS 官方规范（developer.fnnas.com）与真机已安装应用的实际布局修正了打包形态：

1. **manifest 格式**：`platform: x86_64` → `platform = x86`（fnpack 1.2.4 直接拒绝前者）；
   补 `desktop_uidir=ui`、`desktop_applaunchname=photos-backup.main`、`maintainer_url`、`distributor`、
   `checkport=false`、`ctl_stop=false`，字段分隔改 `key = value`。
2. **补 `app/ui/config`**：CGI 入口 `/cgi/ThirdParty/photos-backup/index.cgi/`，`type=iframe`，`allUsers=false`。
3. **补 `app/ui/images/icon_64.png`、`icon_256.png`**：入口图标引用 `images/icon_{0}.png`。
4. **`config/privilege`、`config/resource` 改 JSON**：`{"defaults":{"run-as":"package"}}`；resource 为空对象。
5. **`lib/` 移到 `app/lib/`**：fnpack 只把 `app/` 打成 `app.tgz`，放在项目根的 `lib/` 不会进包——
   安装后会「有界面没代码」。
6. **路径解析**：`PHOTOS_BACKUP_APP_DIR`（自测）→ `TRIM_APPDEST`（真机）→ 上溯找 `lib/config.py`；
   数据目录用 `TRIM_PKGVAR`，且 `PHOTOS_BACKUP_APP_DIR` 存在时独占（否则在别的 fnOS 应用进程里跑自测
   会把数据写进那个应用的 var）。
7. **CGI 健壮性**：stdout/stdin 显式 UTF-8（C locale 下中文会 `UnicodeEncodeError` 变空响应）、
   响应头改 CRLF、补 500 兜底 JSON、PATH 补齐标准目录。
8. **生命周期脚本拆分**：`install_init` 只建 `var/` 并 `chown` 给包用户；`install_callback` 用
   `runuser -u $TRIM_USERNAME` 写 crontab（否则 root 与包用户各一份，出现双份任务）；
   新增 `upgrade_init/upgrade_callback/config_init/config_callback/uninstall_init`。
9. **构建脚本**：staging 排除 `tests/`、`scripts/`，统一目录 755 / 文件 644 / 入口 755，
   产物名取 `manifest.appname`（`photos-backup.fpk`）。

真机形态冒烟（`TRIM_APPDEST` + `TRIM_PKGVAR` 直接执行 `ui/index.cgi`）：save → run → list 全通，
crontab 行路径为 `<TRIM_APPDEST>/lib/cron_run.py`，数据落在 `<TRIM_PKGVAR>`。

### 已知限制

见 `KNOWN_ISSUES.md`（15 条）。发布门禁仍未完成的三项：百万文件实测（AC-32）、
第三方恢复演练（AC-35）、真机执行身份验证（AC-26）。

## v1.1.0

已撤回，未发布。
