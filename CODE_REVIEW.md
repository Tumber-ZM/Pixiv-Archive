# Pixiv Archive 代码审查报告

| 项目 | 内容 |
|------|------|
| 审查对象 | `D:\Little Tool\Pixiv-Archive`（Pixiv 本地图库归档与浏览工具，README 标注版本 v1.2.1） |
| 技术栈 | Python 3.11.9 + FastAPI + SQLite + 原生 JS（零框架、零构建） |
| 审查方式 | 通读全部自研源码（约 8.4k 行，不含 `venv/` 与图库数据），并在本机实跑服务做端点与行为验证 |
| 代码行号基准 | 除特别注明外，均指本次重构后的状态（提交 `ca82b1a`）；被移除代码引用基线提交 `a14344a` |
| 审查时的数据 | 作品 1310、图片 1890、作者 555、标签 2812、标签关联 10695，全部已同步元数据 |

版本控制：审查开始时该目录**尚无 git**，已初始化仓库并留下两个提交：

- `a14344a` 基线版本（重构前的原始状态，含局域网模式与桌面窗口模式）
- `ca82b1a` 本次重构（移除上述两个功能，详见第六节）

---

## 一、结论摘要

1. **完成度较高的小工具**：模块职责分明（扫描 / 同步 / 下载 / 后台任务 / 缩略图各自独立），前端零依赖零构建，实机全部端点可用，图片接口的路径穿越防护经 6 种变形探测均有效拦截。
2. **最该修的三个问题都不是风格问题，而是会直接伤到用户的缺陷**：坏请求体会打掉整条连接（H1）、首次安装的 `.env` 生成路径是断的（H2）、删除作品会残留元数据文件（H3）。**三者均已修复并通过实测**，见第十节。
3. **直连模块是有意的安全取舍，但代价要说清楚**：为免 SNI 而同时关闭了证书校验与主机名校验（M1），整条 HTTPS 链路的信任根退化为"相信 DNS/DoH 的应答"。这对一个只读自己图库的工具是可接受的取舍，但必须在文档里明确告知，而不是只在代码注释里。
4. 本次已按要求移除**局域网模式**与**桌面窗口模式**，并把单实例锁并入启动链路：删除 4 个文件、净减 634 行（+98 / −732），重构后服务实跑与全部端点回归通过。

---

## 二、项目概况

一个纯本地运行的 Pixiv 图库归档与浏览工具，**不含任何在线浏览功能**。它扫描本地已下载的图片，按文件名解析 Pixiv ID 建库、生成缩略图，通过本地网页界面浏览/搜索/整理，并可选联网同步作品元数据。

### 模块规模

| 模块 | 行数 | 职责 |
|------|-----:|------|
| [app/main.py](app/main.py) | 1150 | 全部路由、页面渲染、错误包装（重构前 1288 行） |
| [app/pixiv.py](app/pixiv.py) | 338 | Pixiv API 客户端：认证、direct/proxy/auto 三模式、镜像 |
| [app/direct_connect.py](app/direct_connect.py) | 216 | 免 SNI 直连 + DoH 解析刷新 IP |
| [app/download.py](app/download.py) | 228 | 收藏订阅增量下载 |
| [app/scanner.py](app/scanner.py) | 234 | 目录扫描、文件名解析、SHA-256 去重、缺失清理 |
| [app/sync.py](app/sync.py) | 197 | 元数据同步（标题/标签/作者/尺寸） |
| [app/database.py](app/database.py) | 153 | 建表、索引与三个迁移函数 |
| [app/jobs.py](app/jobs.py) | 119 | 内存后台任务与取消（全局单任务） |
| [app/thumbnails.py](app/thumbnails.py) | 75 | 缩略图生成 |
| [app/templates/index.html](app/templates/index.html) | 2145 | 单页 5 视图主界面（全部内联 JS） |
| [app/static/css/style.css](app/static/css/style.css) | 2434 | 双主题样式单文件 |
| [run.py](run.py) | 184 | 本地服务入口（自研 ASGI HTTP 服务器） |

### 存储布局

`archive.db`（SQLite / WAL）、`thumbnails/{pixiv_id}.jpg`、`metadata/{pixiv_id}.json`、`metadata/avatars/*.jpg`、`.env`、`pixiv_archive.log`。

数据库 8 张表，`artworks` / `images` / `artwork_tags` / `favorite_artworks` 之间均为 `ON DELETE CASCADE` 且连接时开启 `PRAGMA foreign_keys=ON`，删除路径由数据库保证一致性（已实测验证）。

---

## 三、架构与数据流

### 两条主链路

**链路 A：本地扫描入库**

```
IMAGE_SOURCE_DIR → os.walk 收集图片 → 正则解析 PID(_pN) → 按 PID 分组
  → 逐个作品 upsert artworks → 每个文件写 images（SHA-256 去重）
  → 生成缩略图 → （可选）sync_metadata 联网补全
```

**链路 B：收藏订阅（v1.2.1 主功能）**

```
订阅用户 ID → list_user_bookmarks 分页拉公开收藏（按收藏时间倒序）
  → 以 last_pid 为增量游标，遇到 PID ≤ 阈值或整页已入库即停
  → 按画师建目录下载（.part 临时文件 + os.replace 原子改名，按 SYNC_DELAY_MS 限速）
  → 扫描入库 → 缩略图 → 元数据同步（一个任务内串完四步）
```

### 后台任务模型

单一全局任务：`jobs.is_busy()` 检查与 `jobs.start()` 注册在同一把锁内完成，任务跑在 daemon 线程，前端轮询 `/api/jobs/{id}`，取消通过 `threading.Event`。同时只允许一个扫描或同步任务，避免两个任务并发写库。

### 直连方案

`direct_connect.py` 用三件事绕开 SNI 阻断：monkeypatch `socket.getaddrinfo` 把 pixiv 域名解析到 `.env` 里记录的 IP、用自建 `ssl.SSLContext` 关闭证书与主机名校验、并通过 DoH 查询最新 IP 后回写 `.env`。

---

## 四、实机验证结果

以下结论均为**实际运行**得出，不是静态推断。

### 服务与端点

在 6899 端口实跑重构后的服务，全部核心端点返回 200：

| 端点 | 结果 |
|------|------|
| `GET /` | 200，90569 字节 |
| `GET /artwork/148494017` | 200，19207 字节 |
| `GET /api/settings` | 200，字段已无 `access_token` |
| `GET /api/artworks` / `/api/search` / `/api/tags` / `/api/authors` | 200 |
| `GET /api/favorites` / `/api/bookmark-subs` | 200（空） |
| `GET /thumbnails/{pid}.jpg` | 200，23794 字节 |
| `GET /static/css/style.css` | 200，49550 字节 |
| `GET /image/{真实路径}` | 200，2953863 字节（真实 PNG） |

### 安全相关的探测

| 探测 | 结果 |
|------|------|
| `/image/../../.env`、`..%2f..%2f`、`%2e%2e%2f` 编码变体 | **全部 404**，白名单机制有效 |
| `/image/D:/Windows/win.ini` | 404 |
| `/static/../.env`、`/thumbnails/..%2f.env` | 404 |
| 删除作品时的外键级联 | 实测 `images` / `artwork_tags` / `favorite_artworks` 残留均为 0 |
| 单实例锁 | 第二个实例被拒绝，退出码 1 |

### 数据一致性

| 项 | 数值 |
|----|-----:|
| 库内作品 | 1310 |
| 缩略图 | 1310（无缺失） |
| `metadata/*.json` | **1373**（其中 **63 个已无对应作品** → 见 H3） |
| `metadata/avatars/*.jpg` | 183 |

文本编码：数据库与 metadata JSON 的日文/表情均为合法 UTF-8，无乱码（此前控制台显示的乱码是终端编码问题，不是数据问题）。

---

## 五、问题清单

标注含义：**已复现** = 我实际触发了该问题；**已实测** = 通过运行/计数确认；**读码确认** = 逐行核对代码后确认；**设计取舍** = 有意为之但代价需说明。

### 高危

| ID | 问题 | 位置 | 状态 |
|----|------|------|------|
| **H1** | **坏请求体会打掉整条 TCP 连接，且不返回任何错误响应**。`await request.json()` 未包 try，非法 JSON 抛 `JSONDecodeError`；而 [run.py:152-153](run.py#L152-L153) 的自研 ASGI 服务器在异常时只打印 traceback 并 `writer.close()`，不发 500。浏览器侧表现为"连接被意外关闭"而非可读提示。pydantic 校验失败（如缺字段）走同一条路径。 | [app/main.py:277](app/main.py#L277)、[315](app/main.py#L315)、[507](app/main.py#L507)、[538](app/main.py#L538)、[574](app/main.py#L574)、[679](app/main.py#L679)、[851](app/main.py#L851)、[1055](app/main.py#L1055)；[run.py:152-153](run.py#L152-L153) | **已复现 → 已修复** |
| **H2** | **首次安装路径是断的**。[install.bat:47](install.bat#L47) 要复制 `.env.template`，但**仓库中不存在该文件**（全库 `*.template` 零命中），而 [README.md](README.md) 仍把它列为存在文件。更糟的是第 47 行用 `>nul` 吞掉了 copy 的报错、第 48 行无条件打印"Created .env"，新用户会拿到一个不存在的 `.env` 却被告知已创建，随后启动必然失败。 | [install.bat:46-52](install.bat#L46-L52) | **已实测**（文件不存在）→ **已修复** |
| **H3** | **删除作品会残留元数据文件**。删除处理只删图片与 `thumbnails/{pid}.jpg`，**从不删 `metadata/{pid}.json`**。实测已积累 63 个孤儿 JSON（1373 个 metadata 对应 1310 个作品）。这些文件虽不影响功能，但会随使用持续增长。 | [app/main.py:260](app/main.py#L260)、[299](app/main.py#L299) | **已实测 → 已修复** |

### 中等

| ID | 问题 | 位置 | 状态 |
|----|------|------|------|
| **M1** | **直连模式完全关闭 TLS 校验**：`check_hostname=False`、`verify_mode=CERT_NONE`、`server_hostname=None`，且 `urllib3.disable_warnings` 全局静音告警。动机是绕开 SNI 阻断，但这使 HTTPS 的信任根退化为"相信 DoH 应答与 DNS"。属于**设计取舍**，但建议在 README 的隐私/安全章节显式声明，让用户知情。 | [app/direct_connect.py:101-102](app/direct_connect.py#L101-L102)、[138-139](app/direct_connect.py#L138-L139)、[10](app/direct_connect.py#L10) | **读码确认 · 设计取舍 → 已处理** |
| **M2** | **IP 可用性判定过宽**：`_validate_ip` 只要 `status_code < 500` 就算这个 IP 可用，因此 403 / 404（被墙返回的拒绝页）也会被采纳并写回 `.env`，把坏 IP 固化下来。 | [app/direct_connect.py:197](app/direct_connect.py#L197) | **读码确认 → 已修复** |
| **M3** | **非认证类错误被误判为"作品已删除"**：`_detail_once` 在非认证分支直接 `raise PixivDeletedError(msg)`，于是 429/5xx/解析异常都可能把作品错标为 `deleted`，前端随即把它归入"已删除"筛选。 | [app/pixiv.py:282](app/pixiv.py#L282) | **读码确认 → 已修复** |
| **M4** | **随机排序下分页必然重复与漏项**：`ORDER BY RANDOM()` 配 `LIMIT/OFFSET`，每页独立重排，翻页结果交叉重叠。 | [app/main.py:139](app/main.py#L139)、[394](app/main.py#L394) | **读码确认 → 已修复** |
| **M5** | **已删除作品每轮同步都会被重新请求**：筛选条件无条件包含 `pixiv_status = 'deleted'`，而 Pixiv 端已删除的作品每轮都会再次失败/再次请求，随时间线性增加无效请求。 | [app/sync.py:38-39](app/sync.py#L38-L39) | **读码确认 → 已修复** |
| **M6** | **多页作品按前缀判存在会误判**：`_work_exists` 用 `name.startswith(prefix)`，前缀 `..._123` 会命中 `..._1234`；多页作品只下到第 1 页也会被判为"已完成"。 | [app/download.py:217-223](app/download.py#L217-L223) | **读码确认 → 已修复** |
| **M7** | **依赖声明错配**：`hypercorn`、`aiofiles` 在 [requirements.txt](requirements.txt) 中声明却**全项目零 import**（实测 grep 仅命中 `venv/`）；反过来 `requests`、`urllib3`（direct_connect）、`pydantic`（main.py 多处）被直接 import 却没声明，只靠 `pixivpy3`/`fastapi` 的传递依赖，上游一旦调整就会断。 | [requirements.txt:2](requirements.txt#L2)、[7](requirements.txt#L7) | **已实测 → 已修复** |
| **M8** | **GET 触发写操作，缺少 CSRF 防护**：`/api/scan`、`/api/sync`、`/api/bookmark-subs/check` 用 GET 启动写任务，本机浏览器访问任意恶意页面即可用 `<img>`/`fetch` 触发。局域网模式移除后跨设备风险消失，但本机 CSRF 仍在。 | [app/main.py](app/main.py) 对应路由 | 读码确认 → **已修复** |

### 轻微

| ID | 问题 | 位置 |
|----|------|------|
| L1 | `.env` 整体重写：`_write_env_file` 把 dict 全量覆盖写回，丢失注释、空行与原有顺序，且无并发保护。 | [app/main.py](app/main.py)、[app/direct_connect.py:56](app/direct_connect.py#L56) |
| L2 | 删除源文件失败被静默吞掉（`except Exception: pass`），接口仍回报"已删除"，用户会以为文件清理成功。 | [app/main.py:257](app/main.py#L257)、[296](app/main.py#L296) |
| L3 | 缩略图路径硬编码 `.jpg`，源图为 png/webp 时缓存与删除路径不匹配。 | [app/main.py:260](app/main.py#L260) |
| L4 | 标签筛选只匹配 `tags.name`，而关键词搜索同时匹配 `name` 与 `translated_name`，两处行为不一致。 | [app/main.py](app/main.py) |
| L5 | 前端 `innerHTML` 直接拼接未转义数据（作者名、标签名、作品标题），存在 XSS 面；同文件已有 `sanitizeHtml` 白名单却未在这几处使用。 | [app/templates/artwork.html](app/templates/artwork.html)、[author.html](app/templates/author.html)、[tag.html](app/templates/tag.html) |
| L6 | 模板间大量重复：主题初始化 IIFE、侧栏折叠、`escapeHtml`、`r18Param` 在 4 个模板里各写一遍；Jinja 无 `extends/block`。 | `app/templates/*` |
| L7 | 作者页/标签页/详情页的加载函数缺 `.catch`，请求失败会永久停在"加载中"并产生未处理 rejection。 | `app/templates/*` |
| L8 | 网格用 `per_page=100` 硬编码且无虚拟滚动，图库上万后首屏压力明显。 | [app/templates/index.html](app/templates/index.html) |
| L9 | 端口 `6814` 在 `run.py`、`app/main.py` 两处重复硬编码（重构前为三处），易漂移。 | [run.py](run.py)、[app/main.py](app/main.py) |
| L10 | 仓库无 `.gitignore` 之外的构建元数据；`build.ps1` 不创建 venv、不检查图标生成错误，打印的产物路径与实际 `onefile` 输出不符。 | [build.ps1](build.ps1) |
| L11 | `#i-external` SVG symbol 随"在外部浏览器中打开"按钮一起失去引用，可一并清理。 | [app/templates/icons.html:97](app/templates/icons.html#L97) |

### 因本次重构而消失的问题

以下问题在重构前的基线（`a14344a`）中存在，随局域网模式/桌面窗口模式移除而**不再适用**，无需再修：

- 局域网模式下 `/thumbnails/` 免鉴权，局域网内可按 pixiv_id 顺序枚举全部缩略图
- 访问令牌用 `==` 非常量时间比较；令牌经 URL / Cookie / localStorage 三处留存
- `lan_access_url()` 使用基准端口而非实际监听端口，避让后打印错误地址
- 端口避让会丢端口却失去托盘退出路径（托盘亦已移除）
- 依赖 `pywebview` / `pystray`，以及 spec 中"声称排除 webview 但 `excludes` 为空"的矛盾

---

## 六、本次重构：移除两个功能

### 改动范围

`16 files changed, 98 insertions(+), 732 deletions(-)`

**局域网模式（整体删除）**

- `app/main.py` 删除 `LANGuardMiddleware`（约 120 行）、`LAN_MODE` / `ACCESS_TOKEN` / `_PUBLIC_PREFIXES` / `_LOCAL_CLIENTS` / `is_local_client` / `get_lan_ip` / `lan_access_url`，以及设置接口里的令牌读写；`import secrets` / `socket` / `parse_qs` 随之移除（`json` / `unquote` 仍在使用，保留）。**1288 → 1150 行**
- 删除 `Start-LAN.bat`、`app/static/js/pa_token.js`
- 四个模板移除 `pa_token.js` 引用与 `paImg()` 包装；`index.html` 移除"访问令牌（局域网）"设置项与相关赋值/提交逻辑
- `run.py` 去掉 `--lan` / `--host` / `PA_HOST`，固定绑定 `127.0.0.1`

**桌面窗口模式（整体删除）**

- 删除 `launcher.py`（353 行）与 `launcher.bat`
- 移除 pywebview 原生窗口、托盘、端口自动避让、`--check` 自检
- `index.html` 移除 `openExternalBrowser()`、"在外部浏览器中打开"设置项与其显隐逻辑（`window.pywebview` 分支）
- `browser_entry.py` 改为 `import run` → `run.main()`，exe 仍自动打开浏览器
- `requirements.txt` 去掉 `pywebview`、`pystray`；`PixivArchive.spec` 把 `webview` / `clr` / `pythonnet` 真正写入 `excludes`（原先注释声称排除但 `excludes` 为空，实际仍会被静态分析打包）

**单实例锁并入启动链路（新增）**

按你的要求，`launcher.py` 的 Windows 命名互斥体（`PixivArchive_SingleInstance`）迁移到 [run.py](run.py)，因此**重复双击 `Start.bat` 不会再起第二个服务**；非 Windows 或调用失败时返回放行值，不阻塞启动。同时新增 `--browser`（exe 通过 `PA_BROWSER=1` 默认开启）。

> **后续更正（见第十二节）**：迁移过来时保留的互斥体实现存在结构缺陷——它只处理 `ERROR_ALREADY_EXISTS(183)`，把「创建失败（句柄为 NULL）」也当成了「拿到锁」。该缺陷在重构前的 `launcher.py` 中**已经存在**（`git show a14344a:launcher.py` 可核对），不是重构引入的；本轮做最终验收时它实际暴露（两个实例同时跑起来），已改为「独占锁文件 + PID 存活检测」。

### 行为变化（需要注意）

| 项 | 重构前 | 重构后 |
|----|--------|--------|
| 访问范围 | 可 `--lan` 开放到局域网 | **固定 `127.0.0.1`，仅本机** |
| 多实例 | `Start.bat` 可重复启动 | **只允许一个实例** |
| 端口被占用 | `launcher` 自动避让 +1（最多 20 次） | **直接启动失败**，需改端口 |
| 桌面窗口 | pywebview 可选，失败回退浏览器 | **始终系统浏览器** |
| 退出方式 | 托盘"退出" | **Ctrl+C / 关闭控制台窗口**（exe 由 `os._exit` 收尾） |

### 重构后的回归验证

- 全部端点 200（见第四节表格），页面渲染正常（首页 90569 字节、详情页 19207 字节，均含预期元素）
- 渲染后的 HTML 中 `pa_token` / `paImg` / `access_token` / `openExternalField` / `pywebview` / `tokenHint2` 出现次数**均为 0**
- `/static/js/pa_token.js` → 404（已删除）
- 单实例锁生效：第二个实例退出码 1
- 原图接口 200（2953863 字节），穿越探测仍全部 404
- `git grep` 全仓库扫描，除 README 中"说明该功能已移除"的描述外，无功能性残留

### 未随重构一并处理（留待你决定）

1. **`.env` 中的 `PA_ACCESS_TOKEN` 仍是死配置**。我没有改动它：该文件含你的真实 `PIXIV_REFRESH_TOKEN`，而沙箱不允许 shell 改写已存在的文件，用编辑器打开它会把 token 读进对话记录。该键现在**没有任何代码读取**，你可以自行删掉那一行。
2. `hypercorn` / `aiofiles` 无用声明、`requests`/`urllib3`/`pydantic` 未声明（M7）——属于依赖清理，未纳入本次功能移除范围。
3. H1 / H2 / H3 三个高危缺陷未修（也未要求修），见第八节建议。

---

## 七、Windows 文件权限修复记录

审查过程中发现工作区无法执行任何 shell 命令，DSH 报 `grantWrite(D:\Little Tool\Pixiv-Archive)` 失败。定位与处理如下。

**现象**：`app`、`app/templates` 等子目录对沙箱内进程完全不可写（新建/覆盖均被拒绝）；工作区**根目录可新建文件**；而 DSH 自带的文件工具可以正常读写全部路径。

**处理**：运行了工作区权限修复脚本两次，各改动一项，均经脚本回读验证：

| 对象 | 改动 | 验证 |
|------|------|------|
| `D:\Little Tool\Pixiv-Archive` | 为当前登录用户补"完全控制"权限 | `WRITE_OWNER` false → true，已验证 |
| `D:\Little Tool\Pixiv-Archive\app` | 同上 | `WRITE_OWNER` false → true，已验证 |

两次运行均未改动文件内容、未改动所有者、未删除任何拒绝项；子目录扫描 504 / 5 个对象，未发现外来应用包权限条目。**但需要如实说明**：第二项改动并未让沙箱内进程获得子目录写权限（实测仍被拒绝），因此本次所有子目录内的代码改动都是通过 DSH 的文件工具完成的。根目录的低完整性标签（低标签只打在根目录、子目录没有）是解释差异的最可能机制，这一条只是观察，未做进一步验证。

**撤销命令**（按需执行，顺序不限）：

```powershell
pwsh -NoProfile -File 'D:\Little Tool\acl-recovery\acl-backup-a304a60ae5ef49aa86ea55d99b9da290.json.ps1' -Path 'D:\Little Tool\Pixiv-Archive' -AllowRoot 'D:\Little Tool\Pixiv-Archive' -Restore 'D:\Little Tool\acl-recovery\acl-backup-a304a60ae5ef49aa86ea55d99b9da290.json'
pwsh -NoProfile -File 'D:\Little Tool\acl-recovery\acl-backup-de9778b822fc4b68900212bd7f0d95ba.json.ps1' -Path 'D:\Little Tool\Pixiv-Archive\app' -AllowRoot 'D:\Little Tool\Pixiv-Archive' -Restore 'D:\Little Tool\acl-recovery\acl-backup-de9778b822fc4b68900212bd7f0d95ba.json'
```

完整报告：`D:\Little Tool\acl-recovery\acl-report-31a64de0fc9d4004a5069d76d0b2a4f1.jsonl`、`acl-report-4700b4d66f1e41a1a33911cbeeaf52f0.jsonl`。

---

## 八、后续建议（按优先级）

**P0 — 修三个高危缺陷（✅ 已全部完成，见第十节）**

1. ✅ 给 `await request.json()` 统一加保护并注册 FastAPI 异常处理器，让坏请求返回 400；同时在 [run.py](run.py) 的 `handle()` 里捕获 ASGI 异常后**返回一个 500 响应**再关连接，而不是静默丢连接（H1）。
2. ✅ 补上 `.env.template`，并给 `install.bat` 加"模板缺失 / 复制失败"检查，失败即报错退出（H2）。
3. ✅ 删除作品时一并清理 `metadata/{pixiv_id}.json`；已清掉现存 63 个孤儿 JSON（H3）。

**P1 — 正确性与知情权**

4. 收紧 IP 校验（要求 2xx/3xx），避免把 403/404 固化成可用 IP（M2）。
5. 区分"作品已删除"与"请求失败"，后者写 `sync_error` 而不是标 `deleted`（M3）。
6. 随机排序改为"种子化随机"或改为按 ID 稳定的随机序，修分页重复（M4）。
7. 在 README 明确写出直连模式关闭 TLS 校验的取舍与后果（M1）。

**P2 — 工程整洁**

8. 清理依赖声明：删 `hypercorn`/`aiofiles`，补 `requests`/`urllib3`/`pydantic`（M7）。
9. 把 `run.py` 的自研 HTTP 服务器换成已在依赖里的 `hypercorn`，可一并解决 keep-alive、流式响应与异常处理问题。
10. 前端抽公共脚本（主题、侧栏、`escapeHtml`、请求封装），并把 `innerHTML` 拼接改为文本节点或统一走 `sanitizeHtml`（L5/L6）。
11. 删除作品这类不可逆操作，前端加"二次确认 + 明确列出将删除的文件数"，后端回报真实删除结果而非 `delete_files` 入参（L2）。

---

## 九、附录

### 审查期间产生的仓库变更

| 提交 | 内容 |
|------|------|
| `a14344a` | 基线：原样提交 v1.2.1 状态 |
| `ca82b1a` | 移除局域网模式与桌面窗口模式，单实例锁并入启动链路 |

`.gitignore` 已排除 `venv/`、`archive.db`、`thumbnails/`、`metadata/`、`.env`、`Release/`、`build/`、`dist/`、`__pycache__/`。已验证这五类机密/数据/依赖目录**均未进入版本库**。仓库本地身份为 `Lenovo <Lenovo@localhost>`（仅仓库级，未改全局配置），如需修改：

```powershell
git config user.name "你的名字"
git config user.email "你的邮箱"
```

### 关键验证命令

```powershell
# 端点冒烟
& venv\Scripts\python.exe run.py --port 6899     # 另开窗口
Invoke-WebRequest http://127.0.0.1:6899/api/artworks -UseBasicParsing

# 单实例锁（应打印"已在运行中"并返回 1）
& venv\Scripts\python.exe run.py --port 6900

# 数据一致性（缩略图/元数据缺失与孤儿）
& venv\Scripts\python.exe -c "import sqlite3,os;c=sqlite3.connect('archive.db');p={r[0] for r in c.execute('SELECT pixiv_id FROM artworks')};f={int(x[:-5]) for x in os.listdir('metadata') if x.endswith('.json') and x[:-5].isdigit()};print('作品',len(p),'JSON',len(f),'孤儿',len(f-p),'缺JSON',len(p-f))"
```

### 未验证/存疑事项

- `build.ps1` 含中文字面量，其解码取决于文件 BOM 与 PowerShell 版本——**已在第十四节末实测并修复**：原文件无 BOM，Windows PowerShell 5.1 按 ANSI 代码页解码后**脚本直接解析失败**，现已存为 UTF-8 with BOM。
- 打包（PyInstaller）链路未实际执行，`build-exe.bat` / `build.ps1` 的问题均为读码确认（`build.ps1` 已额外通过 PS 5.1 解析器校验，见第十四节末）。
- `PixivArchive.spec` 加入 `excludes` 后的实际产物体积未验证（需真实构建一次）。**后续：用户决定不再打包 exe，整条打包链路已在第十五节删除，本条不再适用。**
- 联网相关行为（元数据同步、收藏订阅下载、DoH 刷新 IP）未实测，避免消耗你的 Pixiv 配额与触发风控；相关结论均为读码确认。

---

## 十、高危问题修复记录

本节记录报告出具后的修复，代码状态基于提交 `ca82b1a`（功能移除后）。

### H1：坏请求不再打掉连接

| 改动 | 位置 |
|------|------|
| 新增 `_json_body()`：统一解析请求体，非法 JSON / 非对象一律返回 400 | [app/main.py:40-52](app/main.py#L40-L52) |
| 8 处 `await request.json()` 全部改走 `_json_body()` | [app/main.py](app/main.py) 各接口 |
| 新增 `ValidationError` 异常处理器：手工 `Model(**body)` 校验失败返回 422（原先属未处理异常 → 500） | [app/main.py:55-66](app/main.py#L55-L66) |
| `handle()` 在应用层抛异常时仍写出响应：已生成响应就发回（去掉旧 content-length 重算），否则补最小 500；不再静默 `writer.close()` | [run.py](run.py) |

修复后实测：

| 场景 | 结果 |
|------|------|
| 非法 JSON（`{bad json`、`not json at all`、空体） | **400** `{"detail":"请求体不是合法的 JSON"}`（修复前：连接被直接关闭，无任何响应） |
| 字段缺失 `{}` | **422** `{"detail":[{"type":"missing","loc":["artwork_ids"],...}]}` |
| 字段类型错 `{"artwork_ids":"abc"}` | **422** `list_type` |
| FastAPI 自身参数校验（`/api/artworks?page=0`） | 仍是 **422**，未被自定义处理器顶掉 |
| 连续触发上述错误后 | 服务仍正常响应全部端点 |

修复过程中的坑：用 `replace_all` 批量替换 `await request.json()` 时，把新增助手函数**自身**那一行也替换掉了，导致 `_json_body` 无限递归。该问题由单元测试当场捕获（否则会变成一个比原缺陷更严重的故障）。

### H2：首次安装不再"假成功"

- 新增 [.env.template](.env.template)：覆盖代码实际读取的全部环境变量，值为占位符、不含任何机密
- [install.bat](install.bat) 的 `.env` 段落改为 `goto` + 标签结构，并对"模板缺失 / 复制失败 / 复制后仍不存在"三种情况报错并 `exit /b 1`

实测（在临时目录中直接运行 install.bat 的真实段落）：

| 场景 | 结果 |
|------|------|
| 有模板、无 `.env` | 创建成功，退出码 **0**，内容含 `IMAGE_SOURCE_DIR` |
| `.env` 已存在 | 跳过且不覆盖（MARKER 保留），退出码 **0** |
| 模板也缺失 | 打印 `[ERROR]`，**退出码 1**，`.env` 未被创建 |

修复过程中的两个坑：

1. 最初写的是多层嵌套 `if` + `exit /b 1`。实测 cmd 在**双层嵌套 if** 内执行 `exit /b 1` 会返回 **0**，失败仍被当作成功——等于没修。改用 `goto` + 标签后正确返回 1。
2. 我一度在 `.bat` 里写了中文 `rem` 注释。install.bat 现为**纯 ASCII**（非 ASCII 字节数实测 = 0）：在 ANSI 代码页下读 UTF-8 中文注释可能吞掉换行，把后续命令并进注释行，使该行整条失效。

### H3：删除作品不再残留元数据

| 改动 | 位置 |
|------|------|
| 新增 `thumbnail_path()` / `metadata_path()` / `remove_side_files()`，作为"作品派生文件"的唯一实现 | [app/paths.py](app/paths.py) |
| 原先的私有 `_remove_side_files` 改为调用同一实现（消除重复） | [app/scanner.py](app/scanner.py) |
| 两个删除接口（单个 / 批量）都改调 `paths.remove_side_files()`：**缩略图与元数据 JSON 随作品记录一并清理**；原图仍只在 `delete_files=true` 时删除 | [app/main.py](app/main.py) |

行为变化：此前 `delete_files=false`（仅从库中移除、保留磁盘图片）时不删缩略图，会残留无用缩略图；现在与扫描时的缺失清理保持一致，边车文件一律清理。

实测（全部使用临时数据目录 / 临时数据库，未触碰真实图库）：

| 测试 | 结果 |
|------|------|
| `remove_side_files` 单元测试：两者都在 / 只存在一个 / 都不存在 / 无关文件 | **4/4 通过** |
| `scanner._prune_missing` 集成测试（临时库） | **7/7 通过**：作品行、图片行、缩略图、元数据 JSON 全删，无关 JSON 保留 |
| 两个删除接口（临时库） | **13/13 通过**：`delete_files=false` 边车删原图留；`true` 时原图也删；批量同样清理 |
| 遗留数据清理 | 删除 **63 个孤儿 JSON（63.1 KB）**；`metadata/` 现有 1310 个 JSON 与 1310 个作品一一对应，孤儿 0、缺失 0，作品总数不变 |

### 未纳入本次修复

L1–L11 维持报告中的原状。M1–M8 已全部处理完毕，见第十一节。

---

## 十一、中等（M 级）问题修复记录

本节记录 M1–M8 的修复，代码状态基于提交 `3ef11c4`（高危问题修复后）。

分工与写入范围（三个并行工作流 + Lead，文件互不重叠）：

| 工作流 | 负责问题 | 写入范围 |
|--------|----------|----------|
| A | M1（代码侧）、M2、M3 | [app/direct_connect.py](app/direct_connect.py)、[app/pixiv.py](app/pixiv.py) |
| B | M5、M6 | [app/sync.py](app/sync.py)、[app/download.py](app/download.py) |
| C | M4、M8 | [app/main.py](app/main.py)、[app/templates/index.html](app/templates/index.html) |
| D（Lead） | M1（文档侧）、M7 | [README.md](README.md)、[requirements.txt](requirements.txt)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |

### M1 — 直连 TLS 取舍：收窄告警范围 + 显式声明

- `urllib3.disable_warnings(...)` 原本在 **import 时**执行，是进程级全局副作用：只要 import 这个模块，其它模块真实的 TLS 告警也会被一起静音。现移入 `enable_direct()`，只有真正启用直连时才抑制。
- `_make_ssl_context()` / `_doh_query()` 补注释，写明为何必须 `check_hostname=False` + `CERT_NONE`（不发 SNI 时服务端证书无法匹配目标域名，开启校验会直接握手失败）及其代价。
- [README.md](README.md) 新增「直连模式的 TLS 校验：一个有意为之的取舍」小节：明确写出信任根退化为「相信 DoH 应答」，并说明该行为只作用于直连会话，`proxy` 模式仍走标准 SNI + 完整校验（[app/pixiv.py:112-121](app/pixiv.py#L112-L121)）。
- **刻意没有改变直连的产品行为**：未开启 `CERT_REQUIRED`、未新增开关。这类改动在本机无法验证，贸然收紧可能直接让直连失效。

### M2 — IP 可用性判定收窄

把 `_validate_ip` 里的 `resp.status_code < 500` 抽成可单测的 `_response_is_usable(resp)`：

| 响应 | 判定 |
|------|------|
| 451（法律封禁）、407（代理要求认证） | 不可用 |
| 403 且 Content-Type 含 `text/html`（典型中间盒拦截页） | 不可用 |
| 500 及以上 | 不可用 |
| 其余 < 500（含 Pixiv 对无鉴权 `/` 返回的 404 JSON） | 仍可用 |

**刻意没有收紧到 `< 400`**：Pixiv API 对无鉴权访问 `/` 返回 404 是正常现象，按 `< 400` 判定会把所有正常 IP 判死，直接让直连模式失效。验证 9/9 通过。

### M3 — 错误分类修正

`_detail_once` 原本在非认证分支**一律** `raise PixivDeletedError(msg)`。现在只有文案命中 `_DELETED_MSG_HINTS`（英文 not found / does not exist / deleted…，日文 見つかりません / 削除 / 存在しません）才判为已删除；其余错误改抛 `PixivNetworkError`，由 [app/sync.py](app/sync.py) 记入 `sync_error` 并在下轮重试（不再把作品错标 deleted）。

Lead 复核时补了一处：Pixiv 的 `reason` 字段可能是 `not_found` 这类下划线形式，直接子串匹配会漏掉，已把 `_` / `-` 归一化为空格后再比对。

验证 7/7：日文文案、英文 `Illust not found`、`reason=not_found` → `PixivDeletedError`；`rate limit`、`Internal Server Error` → `PixivNetworkError`；错误体为空但无 illust → `PixivDeletedError`；认证文案 + 重新认证失败 → `PixivAuthError`。

**残留风险（已在第十三节收尾）**：未联网确认真实 Pixiv 对已删除作品返回的确切文案。若真实文案不含任何关键词，该作品会被记为 `sync_error` 并反复重试。第十三节把判定信号从「文案」换成「HTTP 状态码优先」，该风险已大幅收窄。

### M4 — 随机排序分页稳定化

两处 `ORDER BY RANDOM()` 改为按 seed 确定的伪随机序，`api_artworks` 与 `api_search` 均新增 `seed` 查询参数（空 `q` 委托给 `api_artworks` 的路径也透传 seed）。

**一个重要更正**：本轮给执行方的初始公式是错的，被实测推翻——`((pixiv_id * 2654435761 + seed) % 2147483647)` 只是给每行加同一个常量，取模后绝大多数行的相对顺序不变；实测 seed=123 与 999 的第 1 页 **199/200 位完全相同**，等于 seed 没生效。最终实现把 seed 作为乘子混入行相关项：

```sql
ORDER BY (((a.pixiv_id * 2654435761) % 2147483647) * mult) % 2147483647, a.id
-- mult = 2 * (seed % 1073741823) + 1
```

两个中间值都 < 2³¹、乘积 < 2⁶²，与 `pixiv_id` 量级无关地不溢出；末尾 `a.id` 作确定性 tie-break。非 random 分支一字未动。

前端：新增全局 `_randomSeed`，**只有**用户重新选择「随机」时才换新种子；`_navCtx.seed` 随所有分页/筛选请求流转，由唯一的 URL 构造器 `buildGalleryUrl()` 统一追加（[index.html:624](app/templates/index.html#L624)），`/api/search` 与 `/api/artworks` 两个分支都经过它。

验证（真实服务、1310 件作品）：

| 检查 | 结果 |
|------|------|
| 固定 seed 全量翻页（per_page=100，14 页） | 1310 条、去重后仍 1310 → **零重复、零漏项** |
| 与确定性排序的条数对比 | random=1310，sequential=1310 |
| 同一 seed 同一页两次请求 | 完全一致 |
| seed 123 vs 999、123 vs 7 | 200 位中 199 位不同 → seed 确实生效 |
| 非 random 排序 | 不受 seed 影响 |
| `/api/search` 独立分支（标签 `R-18`，514 条） | 跨页无重复 |

### M5 — 已删除作品改为定期复查

筛选逻辑抽成可单测的 `_select_pending_rows()`：

```sql
WHERE title IS NULL OR last_synced IS NULL
   OR sync_error IS NOT NULL
   OR (pixiv_status = 'deleted' AND last_synced < datetime('now', ?))
-- 参数 '-30 days'；常量 _DELETED_RECHECK_DAYS = 30
```

未同步 / 缺标题 / 上次失败（仍每轮重试）的语义保持不变，`specific_pixiv_id` 分支无条件同步。已删除作品从「每轮都请求」变为「距上次确认超过 30 天才复查」，请求量不再随历史删除数线性增长。

验证 9/9（临时库 + 7 条构造数据）：昨天的 deleted 不入选、60 天前的 deleted 入选、`title IS NULL` / `last_synced IS NULL` / `sync_error IS NOT NULL` / 从未同步的 deleted 均保持入选、正常 active 不入选。

### M6 — 下载完成度精确判定

`_work_exists(dest_dir, prefix)` → `_work_exists(dest_dir, prefix, page_count)`，去掉 `startswith` 前缀匹配：

- 单页（`page_count <= 1`）：要求存在 `prefix + 扩展名` 的**精确**文件名
- 多页：要求 `prefix + str(p) + 扩展名` 对 `p = 0..page_count-1` **全部**存在

验证 8/8：单页精确命中判完成；单页仅因另一作品 ID 前缀相同（`123456` vs `1234567`）**不得**判完成；`.png` 也认；多页只有 p0 不得判完成；0..2 齐全判完成；缺中间一页不得判完成；缺页用别的扩展名补上也算完成；目录不存在返回 False。

### M7 — 依赖声明纠正

[requirements.txt](requirements.txt)：删掉 `hypercorn`、`aiofiles`（全项目零 import，HTTP 服务由 [run.py](run.py) 自带），补上被**直接 import 却未声明**的 `pydantic>=2.0.0`、`requests>=2.31.0`、`urllib3>=2.0.0`。同步更新 [README.md](README.md) 的运行依赖清单与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（后者原先也列着这两个已移除的包）。

验证 8/8：逐条解析 `requirements.txt`，用 `importlib.metadata` 取已安装版本做区间比较并实际 import 对应模块——8 条全部「已装版本 ≥ 声明下限」且可导入；README 清单与 `requirements.txt` 的名称集合完全一致。

### M8 — 写操作接口改 POST

三个会改数据的接口改为 POST：`/api/scan`、`/api/sync`、`/api/bookmark-subs/check`；前端 `startJob()` 统一传 `'POST'`。只读的 `GET /api/bookmark-subs`（列表）与已是 POST 的 `/api/bookmark-subs/{id}/check` 未动。

验证：三个接口 GET 一律 **405**，`/openapi.json` 中这三个路径只有 `post`；`/api/bookmark-subs`、`/api/artworks`、`/api/settings`、`/api/tags` 仍 200。全仓库 grep 确认没有其它以 GET 调用这三个 URL 的位置（无脚本/bat/文档调用）。

**已知外观小瑕疵（已在第十三节修复）**：`GET /api/bookmark-subs/check` 的 405 响应带 `Allow: DELETE`——Starlette 用「第一个路径部分匹配」的路由填这个表头，而 `DELETE /api/bookmark-subs/{pixiv_user_id}` 注册在它之前。POST 仍会 FULL 匹配到正确路由，功能无影响。

### 本轮验证总账

| 验证脚本 | 覆盖 | 结果 |
|----------|------|------|
| 逻辑验证脚本（临时目录 + 临时库） | M2 9 项、M3 7 项、M5 9 项、M6 8 项 | **33/33 通过** |
| HTTP 验证脚本（真实服务，端口 6906） | M8 11 项、M4 12 项 | **23/23 通过** |

两个脚本均为一次性临时脚本，未纳入版本库（如需可整理成回归测试）。全部断言都在临时目录/临时库上执行：逻辑脚本通过改写 `app.database.DB_PATH` 隔离数据库，实测运行前后真实 `archive.db` 的大小与 mtime **完全未变**。

真实库在本轮前后清点一致：1310 作品 / 1890 图片 / 555 作者 / 1 已删除 / 1310 元数据 JSON / 1310 缩略图，元数据与作品 1:1（孤儿 0、缺 JSON 0）。

### 已知未处理项

- L1–L11 已在**第十四节**逐条处理完毕（9 条修复、L3 证伪、L8 的虚拟滚动明确列为后续优化）。
- M3 的残留风险与 M8 的 `Allow` 表头瑕疵已在**第十三节**收尾修复。

---

## 十三、M3 与 M8 遗留项收尾

第十一节给 M3 留了「真实删除文案未确认」的风险，给 M8 留了一个 `Allow` 表头的外观瑕疵。本节把它们收掉。

### M3 — 判定信号从「文案」改为「状态码优先」

原判定链条是：错误文案命中 `_DELETED_MSG_HINTS` → 已删除，否则网络错误。这在两个方向上都不可靠：

- 真实文案若不含任何关键词，作品会**永远卡在 `sync_error`** 反复重试；
- `_fmt_error` 在 `message` / `user_message` / `reason` 全为空时只能返回 **dict 字面量**，任何关键词表都必然漏掉这种形态。

**先确认库的真实行为**（读本地源码，不联网）：pixivpy3 3.7.5 的 `illust_detail()` **完全不检查 HTTP 状态码**——`aapi.py:263-269` 只是发请求后 `parse_json()`，所以 404 的 JSON 错误体会被原样返回（`response.get("error")` 分支确实是活路径），但**状态码信息就此丢失**。

于是改成两层判定（[app/pixiv.py](app/pixiv.py)）：

1. **首选 HTTP 状态码**：新增 `_detail_request()`，自己发 `GET /v1/illust/detail` 拿到 `Response`，再用 `self.api.parse_result(res)` 解析，从而保住状态码。`404` 就是「作品不存在」的权威答案，不依赖任何文案。
2. **文案与空错误对象兜底**：新增 `_missing_illust()`，仅在拿不到状态码时（回退路径）才依赖关键词表，并额外把「error 对象存在但没有任何说明文字」判为不存在。

`_ERROR_MSG_KEYS` 同时纳入 `message_lang` / `user_message_lang`——原来只读 `message` / `user_message` / `reason`，若 Pixiv 把文案放在本地化字段里同样会漏。

| 场景 | 判定 |
|------|------|
| HTTP 404（含空 error 对象） | `PixivDeletedError` |
| 错误文案命中关键词（中 / 英 / 日） | `PixivDeletedError` |
| error 对象存在但全空，且状态码不是 429/5xx/401/403 | `PixivDeletedError` |
| 429 / 5xx（含空错误体） | `PixivNetworkError` → 记 `sync_error` 重试 |
| 401 / 403（含空错误体） | `PixivNetworkError` |
| 响应体不是 JSON（如 5xx 的 HTML 页） | `PixivNetworkError` |
| 200 但没有 illust | `PixivDeletedError` |

**绝不错标删除的红线**：429、5xx、401、403 无论错误体是否为空，一律判为可重试的网络错误，绝不标 deleted。

认证链路顺带修正：重新认证后**仍是认证类错误**才抛 `PixivAuthError`（由 sync 中断整批）；若变成 404 就按已删除处理。原实现是「重新认证后只要还有错误就抛 AuthError」，会把已删除的作品报成认证问题。

回退安全：若 pixivpy3 内部结构变化导致拿不到 `Response`（例如 `no_auth_requests_call` 不存在），自动退回公开的 `illust_detail()`，此时状态码为 `None`，由文案兜底——最坏情况只是丢掉状态码信号，功能不受影响；测试里专门覆盖了这条回退路径。

验证（离线假 API 对象，不联网、不碰真实库）**38/38 通过**，覆盖上表每一行、认证链路四条分支、回退路径，以及调用契约（方法、地址、参数与 pixivpy3 的 `illust_detail` 逐项一致，防止两边实现漂移）。

残余风险（已大幅收窄，如实记录）：若 Pixiv 对不存在的作品返回的状态码既不是 404、错误体又是不含关键词的文案，仍会落进重试分支。方向保守、不会误删，且即使误判，M5 的 30 天复查也会在作品其实仍在时把它同步回 active。

### M8 — 405 的 Allow 头改为 POST

根因定位到 Starlette：`Router` 在没有 FULL 匹配时，用**第一个路径部分匹配**的路由处理请求（`starlette/routing.py` 中 `partial = route`），而 `Route.handle` 用该路由的 `self.methods` 组装 405 的 `Allow` 头（同文件第 275 行）。`DELETE /api/bookmark-subs/{pixiv_user_id}` 与 `POST /api/bookmark-subs/check` 路径形状相同，DELETE 注册在前，于是 `GET /api/bookmark-subs/check` 拿到了 `Allow: DELETE`。

修复：把 `POST /api/bookmark-subs/check` 移到 DELETE 路由之前，并在 [app/main.py](app/main.py) 留注释说明这个顺序有意义、不要调换。POST 本来就是 FULL 匹配到正确路由，因此这只影响响应头，不改变任何接口行为。

验证（真实服务）**17/17 通过**：

- `GET /api/bookmark-subs/check` → 405 且 `Allow: POST`（修复点）
- `GET /api/bookmark-subs/12345` → 405 且 `Allow: DELETE`（原有行为未被破坏）
- `/openapi.json`：check 路径只有 `post`，`{pixiv_user_id}` 路径只有 `delete`
- 只读接口（列表 / artworks / settings）仍 200
- 真实 POST 一次确认能 FULL 匹配到处理器：返回前置校验错误 `NO_SUBS`、响应里没有 `job_id`。**带护栏**：仅当订阅数为 0 时才发这一次 POST，绝不触发联网任务
- 源码层面确认 `@app.post("/api/bookmark-subs/check")` 的行号在 `@app.delete(...)` 之前

### 本轮验证总账

| 脚本 | 覆盖 | 结果 |
|------|------|------|
| M3 离线套件 | 辅助函数 10、状态码判定 3、临时故障 7、兜底 4、成功与契约 2、回退路径 3、传输异常 2、认证链路 4、sync 契约 2（另含 1 项独立断言） | **38/38** |
| M8 套件（真实服务） | 路由顺序 3、405/Allow 5、只读回归 3、openapi 2、POST 可达性 3（另含就绪 1） | **17/17** |
| 综合最终验收 | M2 9、M5 6、M6 7、M4 10、端点冒烟 13、H1 回归 4、单实例锁 5 | **54/54** |

合计 **109 项全部通过**。单实例锁一并回归：第二个实例退出码 1、不占用端口、打印「已在运行中」，第一个实例持续服务；M4 在真实库上仍是全量 1310 件随机翻页零重复零漏项，seed 123 vs 999 第 1 页 100 位全不同。

三个脚本均为一次性临时脚本，未纳入版本库（如需可整理成回归测试）。

---

## 十二、额外发现并修复：单实例锁静默失效（严重度高于 M 级）

这一项不在原报告的 H1–H3 / M1–M8 里，是**本轮做最终验收时意外发现的**。

### 现象

第十一节的 M 级修复完成后，最后一次复测单实例锁，结果与前一次相反：**两个实例同时跑起来了**——6907 与 6908 同时有 HTTP 响应，锁完全没有拦住。

### 排查过程

1. 先怀疑 `ctypes.windll` 读 `GetLastError` 的经典坑。受控实验：同进程内连续两次 `CreateMutexW`，两种写法都正确返回 0 / 183 → **排除**。
2. 跨进程受控实验（holder 持锁 30 秒，另起两个进程探测）：子进程、孙进程都拿到 183 → **互斥体机制本身在该环境正常**。
3. 直接探测 `run.acquire_single_instance()` 的真实返回值：**三个进程（含正在持锁的第一个）全部返回 `0`**，即 `CreateMutexW` 返回了 NULL。
4. 换名字做对照，抓到真实错误码：

| 调用 | 结果 |
|------|------|
| `CreateMutexW(None, False, "PixivArchive_SingleInstance")` | **NULL，`GetLastError()=5`（ERROR_ACCESS_DENIED）** |
| `Local\PixivArchive_SingleInstance` | NULL，5 |
| `PixivArchive_SingleInstance_x` / `PixivArchive_ProbeAlpha` | 句柄 392 / 388，错误 0 |
| `Global\PixivArchive_SingleInstance` | 句柄 536，错误 0 |

即：同名内核对象在该命名空间里已存在、却**不属于当前安全上下文**，于是 `CreateMutexW` 直接失败。

### 根因

```python
handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
if kernel32.GetLastError() == 183:   # 只认「同名对象已存在」
    return None
return handle                        # ← 创建失败时 handle 是 NULL，却被当成「拿到锁」
```

判断只覆盖「对象已存在」，把**创建失败**当成了「成功获得锁」。于是单实例保护在最需要它的时候静默失效：重复双击 `Start.bat`、或快速连点两次，就可能同时跑起两个服务、并发写同一个 SQLite 库（WAL 模式下不必然立刻损坏，但这不是并发安全的用法；H1/H3 已修的删除路径还会连带操作同一批边车文件）。

附带一点：原写法用的是 `ctypes.windll` 的默认返回类型 `c_int`，会把 64 位 HANDLE 截断——本例句柄恰好都很小所以没暴露，但同样不严谨。

**归因**：该缺陷在重构前的 `launcher.py` 中已存在（`git show a14344a:launcher.py` 里是同一套判断），重构只是把它搬到了 `run.py`，不是重构引入的。

### 修复

把单实例保护从「Windows 命名互斥体」改为「**数据目录下的独占锁文件 + PID 存活检测**」（[run.py](run.py)）：

- `os.open(..., O_CREAT | O_EXCL)` 原子创建 `.instance.lock` 并写入自己的 PID；创建成功即获得锁
- 文件已存在时读出 PID，用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess` 判断持有者是否还活着：活着 → 返回 None（提示「已在运行中」，退出码 1）；已死 → 视为陈旧锁，删除后重试一次
- `OpenProcess` 打不开时区分错误：`ERROR_INVALID_PARAMETER(87)` 表示该 PID 不存在；其它错误（如拒绝访问）说明进程还在、只是查不了，按「存活」处理，**避免误抢运行中实例的锁**
- 「创建锁文件」与「写入 PID」之间被强杀会留下空文件：内容非法时，文件超过 10 秒即视为陈旧（`LOCK_FILE_GRACE_SECONDS`）
- 正常退出在 `finally` 里删除锁文件；被强杀留下的陈旧锁由 PID 检测自愈
- 数据目录不可写等异常不阻塞启动（保留原先的宽容语义）
- `.instance.lock` 已加入 [.gitignore](.gitignore)

**为什么不用「改用 `Global\` 命名空间」**：`Global\` 只是这次恰好可用，同类问题（同名对象属于别的安全上下文）依旧可能发生，而且它会让不同 Windows 用户的实例互相挡住。锁文件不依赖内核对象命名空间，且陈旧锁能自愈。

### 验证（端到端 30 项，全部通过）

| 用例 | 结果 |
|------|------|
| `_pid_is_alive`：自身 / 存活子进程 / 不存在的 PID | True / True / False |
| 第一实例启动后，锁文件里的 PID 真实存活 | 通过 |
| 第二实例 15 秒内退出、退出码 1、打印「已在运行中」、**不占用端口** | 通过 |
| 第二实例未改写锁文件，第一实例持续服务 | 通过 |
| 硬杀第一实例 → 陈旧锁（PID 已死）→ 新实例接管并服务 | 通过 |
| 锁文件指向存活 PID → 立即退出 1、不占用端口、不改写锁文件 | 通过 |
| 空锁文件：刚创建（宽限期内）拒绝启动；1 小时前的可接管 | 通过 |
| 正常退出路径（桩服务器）`main()` 返回 0 且锁文件被清理 | 通过 |

两点测试环境说明：本环境里 `Start-Process -PassThru` 与 `subprocess.Popen` 返回的 PID **并非子进程真实 PID**（存在进程包装层），因此测试校验的是「锁文件里的 PID 真实存活 / 未被改写」这类不变量，而不是与返回的 PID 做相等比较；另外沙箱禁止发送 Ctrl+Break，正常退出改用桩服务器直接验证 `main()` 的 `finally` 路径。

---

## 十四、轻微问题（L1–L11）清理

第一节「轻微」表里的 11 条，本轮逐条给结论：**9 条确认为真问题并已修，1 条（L3）实测为误报，1 条（L8）只完成可控部分**。

### 逐条结论

| ID | 结论 | 落地位置 |
|----|------|----------|
| L1 | 真问题 → **已修** | 新增 [app/envfile.py](app/envfile.py)：`read_env()` [:17](app/envfile.py#L17)、`update_env()` [:56](app/envfile.py#L56) |
| L2 | 真问题 → **已修** | [app/main.py:287](app/main.py#L287) `_remove_files()`；响应字段 [:322-334](app/main.py#L322-L334)、[:349-374](app/main.py#L349-L374)；前端 [index.html:795](app/templates/index.html#L795) |
| L3 | **误报** → 未改代码 | 见下 |
| L4 | 真问题 → **已修** | 标签筛选 [app/main.py:158-166](app/main.py#L158-L166)、标签搜索 [:523](app/main.py#L523) |
| L5 | 真问题 → **已修** | 三个模板 + [partials/common_js.html:9](app/templates/partials/common_js.html#L9) `escapeAttr()` |
| L6 | 真问题 → **已修** | [app/templates/base.html](app/templates/base.html) + [app/templates/partials/](app/templates/partials/)（4 个片段） |
| L7 | 真问题 → **已修** | [partials/common_js.html:23](app/templates/partials/common_js.html#L23) `showLoadError()`、[:36](app/templates/partials/common_js.html#L36) `fetchJson()` |
| L8 | **部分修** | [index.html:465](app/templates/index.html#L465) `PAGE_SIZE`；虚拟滚动**未做** |
| L9 | 真问题 → **已修** | 新增 [app/config.py:7](app/config.py#L7) `DEFAULT_PORT`；[run.py:22](run.py#L22)、[app/main.py:20](app/main.py#L20) 改为导入 |
| L10 | 真问题 → **已修** | [build.ps1](build.ps1) |
| L11 | 真问题 → **已修** | [app/templates/icons.html](app/templates/icons.html) |

### L3：缩略图 `.jpg` 是误报，没有代码要改

第一节写的是「源图为 png/webp 时缓存与删除路径不匹配」。实际读 [app/thumbnails.py:20-29](app/thumbnails.py#L20-L29)：缩略图是**无条件重编码**成 JPEG 的——`Image.open(src)` 之后 `img.convert("RGB").save(dest, "JPEG", ...)`。不管源图是 png、webp 还是别的格式，落盘的一定是 JPEG，所以 `{pixiv_id}.jpg` 与 `paths.thumbnail_path()`、模板里的 `/thumbnails/{id}.jpg` 三者本就一致。

真实库实测：`thumbnails/` 下 1310 个文件，扩展名分布 `{'.jpg': 1310}`，**没有一个其它扩展名**。因此这条判定为误报，未改任何代码。

### L1：`.env` 为什么不能只加把锁

原实现是把整个 `.env` 解析成 dict、再全量覆盖写回，丢注释、丢空行、重排顺序，也没有并发保护。修复没有停在「加锁」上，而是把写入改成**按键就地更新**（[app/envfile.py](app/envfile.py)）：

- `update_env()` 逐行扫描，命中目标键就替换该行（保留原有位置），未命中的行原样写回；新键追加到末尾；
- **换行风格跟随文件**：文件里出现过 CRLF 就继续用 CRLF（`readlines` 必须带 `newline=""`，否则通用换行模式会把 CRLF 归一成 LF，写回时把用户的换行风格悄悄改掉——这个坑是本轮验证脚本抓出来的，见下）；
- 写入走「同目录临时文件 + `os.replace()`」原子替换，失败时清理临时文件；`tempfile.mkstemp(dir=同目录)` 保证替换不跨卷；
- 模块级 `threading.Lock` 把同进程内的并发写串行化；`read_env()` / `update_env()` 是纯函数式的键值操作，不做任何静默的格式转换。

[app/main.py](app/main.py) 的 `_read_env_file` / `_write_env_file` 与 [app/direct_connect.py](app/direct_connect.py) 的 `_read_env` / `_write_env` 全部改走这一套，设置接口与直连 IP 覆盖不再有第二份写入实现。

### L2：删除失败不再被吞

两个删除接口原来各自维护一段 `try: os.remove(...) except Exception: pass`。现在收敛为一个 `_remove_files()`（[app/main.py:287](app/main.py#L287)），只捕 `OSError` 并返回 `(removed, failed)`，失败项带 `path` 与 `error` 文案；单条删除与批量删除都回报 `files_removed` / `files_failed`（单条另外保留原有的 `files_deleted` 字段），前端 toast 会把失败数量显示出来（[index.html:795](app/templates/index.html#L795)）。原先「文件没删掉却提示删除成功」的情况不再发生。

### L4：标签筛选与关键词搜索同口径

关键词搜索一直同时匹配 `tags.name` 与 `tags.translated_name`，而按标签筛选只匹配 `name`——用中文译名点进去会一个作品都筛不到。现在筛选改为 `t.name LIKE ? OR t.translated_name LIKE ?`（[app/main.py:158-166](app/main.py#L158-L166)），标签搜索接口 `/api/tags?q=` 同步改口径（[:523](app/main.py#L523)）。

需要如实说明一点：**本库当前带译名的标签数为 0**（`translated_name` 全空），所以这条修复在真实数据上观察不到差异。正确性由隔离临时库的用例保证（见验证一节），而不是靠真实库的观测。

### L5 / L7：前端转义与失败提示

- L5：作者名、标签名、作品标题拼进 `innerHTML` 时补上 `escapeHtml()`；`alt="..."` 这类属性值改用新增的 `escapeAttr()`（先 HTML 转义再处理 `"` / `'`），避免属性上下文被闭合。
- L7：三个页面原来请求失败会永久停在「加载中」并留下未处理的 promise rejection。现在统一走 `fetchJson()`：失败时收起「加载中」、把原因写进页面（`showLoadError()`），返回 `null` 让调用方直接 `return`。作者页里那次「结果从未被使用」的 `/api/artworks?author=` 并发请求也一并删掉了。

### L6：模板结构

原来是「4 个模板各写一遍」：主题初始化 IIFE、侧栏折叠脚本、`escapeHtml`、`r18Param`。现在：

| 文件 | 内容 |
|------|------|
| [app/templates/base.html](app/templates/base.html) | 页面骨架，提供 `title` / `sidebar` / `sidebar_js` / `content` 四个 block |
| [partials/head_boot.html](app/templates/partials/head_boot.html) | 首屏主题 / 安全模式 / 侧栏折叠状态 IIFE（4 个页面共用） |
| [partials/sidebar_simple.html](app/templates/partials/sidebar_simple.html) | 精简侧栏（`nav_active` 变量决定是否标「当前项」） |
| [partials/sidebar_js.html](app/templates/partials/sidebar_js.html) | `updateSidebarToggle` / `toggleSidebar` |
| [partials/common_js.html](app/templates/partials/common_js.html) | `escapeHtml` / `escapeAttr` / `r18Param` / `showLoadError` / `fetchJson` |

`author.html` / `tag.html` / `artwork.html` 改为 `{% extends 'base.html' %}`；`index.html` 布局特殊（自带一套侧栏与 helpers），只接入 `head_boot` 片段，不做强拆。

这次重构**只做去重、不改渲染结果**：重构前后的 `/`、`/author/1`、`/tag/R-18`、`/artwork/148494017` 四个页面做了逐行对照，侧栏、head 引导脚本、图标区完全一致，差异只有本节列出的那些有意改动（见验证一节）。

### L8：常量收敛了，虚拟滚动没做

`per_page=100` 在 `buildGalleryUrl()` 里出现两次，现在收敛为一个带说明的 `PAGE_SIZE` 常量（[index.html:465](app/templates/index.html#L465)）。**虚拟滚动没有实现**：它是独立的功能改造（需要重写网格渲染与滚动监听），本轮不夹带；当前状态是「每页 100 个瓦片 + 缩略图 `loading="lazy"`」，图库上万后翻页仍有压力。这一条按「部分完成」记录。**后续见第十五节 15.3：改用浏览器原生 `content-visibility` 拿到等效收益，并说明了为什么不手写窗口化。**

### L9 / L10 / L11

- L9：默认端口原本在 `run.py` 与 `app/main.py` 各写一遍，现在只有一个来源 [app/config.py:7](app/config.py#L7) `DEFAULT_PORT = 6814`，两处改为导入。
- L10：[build.ps1](build.ps1) 补上「缺 venv 就创建」（依次尝试 `python` / `py`，与 `install.bat` 同策略）、生成后校验 `assets/icon.ico` 存在、打包后校验产物存在，并按 `onefile` 的真实产物打印 `dist\PixivArchive.exe`（原脚本打印的是不存在的 `dist\PixivArchive\PixivArchive.exe`）。任一环节失败即以非零码退出。
- L11：删除已无任何引用的 `#i-external` symbol（全项目 0 引用）。

### 本轮验证

全部断言都在临时文件 / 临时库上执行，真实库前后清点一致。

| 验证 | 覆盖 | 结果 |
|------|------|------|
| 综合验证脚本 `_verify_L.py`（离线单元 + 真实服务） | L1 14 项、L2 5 项、L9 2 项、L10 5 项、L11 2 项、L4 4 项、L5/L6/L7/L8 与页面渲染 24 项、单实例锁 6 项、接口形状与冒烟 13 项、H1 4 项、M4 3 项、M8 2 项 | **93/93 通过** |
| 模板渲染前后对照（`_snap_before` vs `_snap_after`） | 4 个页面 | 侧栏 / head 引导 / 图标区逐字节一致，其余差异全部对应本节改动 |
| 单实例锁端到端（PowerShell 起第二个实例） | 锁检测与退出 | 打印「已在运行中」，未占用端口 |

真实库清点（本轮结束时）：1310 作品 / 1890 图片 / 555 作者 / 2812 标签 / 10695 作品-标签关系 / 0 收藏夹订阅 / 1 已删除；metadata JSON 1310 个与作品 **1:1**（孤儿 0、缺失 0），缩略图 1310 个且扩展名全部为 `.jpg`。

### 本轮自查发现并修掉的两个自身缺陷

如实记录，因为它们都属于「差一点就发布出去」的类型：

1. **`.env` 换行风格被破坏**：`update_env()` 最初用普通文本模式读文件，Python 的通用换行模式把 CRLF 归一成 LF，于是「保留原样」的行也被改成 LF 写回——用户 .env 的换行风格会被静默改掉，diff 里一片红。验证脚本里的 CRLF 断言当场把它抓出来，改为 `newline=""` 读写。
2. **L4 的修复没有落到 tag 筛选上**：第一轮改动加在了 `api_search` 的既有代码上（那里本来就已经匹配译名），而真正需要改的 `api_artworks` 标签筛选仍是 `t.name LIKE ?`。隔离库用例（用译名筛选返回 0 条）把它暴露出来，才补上正确的两处。

### 顺带发现：`build.ps1` 在 Windows PowerShell 5.1 下根本加载不了（已修）

L10 改完后用 PS 5.1 自带的解析器核对脚本，`ParseFile` 报出 3 个语法错误（含「字符串缺少终止符」）。原因不是语法写错，而是**文件编码**：

- `build.ps1` 里有 537 个非 ASCII 字节（中文 `Write-Host` 文案），但文件**没有 BOM**；
- Windows PowerShell 5.1 读取无 BOM 的 `.ps1` 时按系统 ANSI 代码页（本机 GBK）解码，UTF-8 的中文字节被拆成乱码，其中一段恰好"吃掉"了双引号字符串，整个脚本因此解析失败；
- **这不是本轮引入的**：把提交 `91a3ad6` 的旧版 `build.ps1` 原样导出成无 BOM 再解析，同样报「字符串缺少终止符」。也就是说 `build.ps1` 在本机默认代码页下一直是坏的，只是从没被真正跑过——而 Windows 上默认的 PowerShell 恰恰就是 5.1。

修复：`build.ps1` 改存为 **UTF-8 with BOM**，正文一字未改（改写前后对 UTF-8 正文算 SHA256 完全一致），随后 PS 5.1 的 `ParseFile` **错误数 3 → 0**，`Get-Content` 读出的中文文案也恢复正常。仓库里其余脚本（`install.bat` / `Start.bat` / `build-exe.bat`）非 ASCII 字节数均为 0，不存在同类问题。

仍需如实说明：**打包链路本身依旧没有真正执行过**（PyInstaller 构建要跑几分钟并写 `dist/`），L10 与这次编码修复都属于「读码 + 解析器验证」，不等于端到端构建验证。**后续见第十五节 15.5：这一轮真跑了一次打包（退出码 0），随后按用户要求把打包链路整体删除。**

### 测试环境说明（如实记录）

- 页面里的 JavaScript **没有在浏览器里真正执行过**：L6 的重构、L7 的 `fetchJson`、L5 的转义，验证手段是「服务端渲染结果逐行对照 + 静态断言」，不是运行时行为测试。要覆盖运行时行为需要引入 headless 浏览器，本轮没有做。
- 本机环境下从 Python 再启动子进程会被明显拖慢（空闲子进程实测 40–53 秒），一度表现为「第二个实例迟迟不退出」。改用 PowerShell 端到端 + 函数级断言（`acquire_single_instance()` 在真实实例持锁时返回 `None`）来验证，结论是锁本身工作正常。
- 验证脚本均为一次性脚本，未纳入版本库；如需要可以整理成常驻回归测试。**后续见第十五节 15.1：已整理为常驻套件 `tests/`。**

---

## 十五、收尾：常驻回归测试、换用 hypercorn、L8 现状，以及移除打包链路

本节记录这次收尾的四项工作，以及其中最后一项的范围变更（用户决定不再需要 exe）。

### 15.1 常驻回归测试套件（`tests/`）

把之前用过就丢的一次性验证脚本整理成可重复运行的套件。**不引入 pytest**（项目 venv 里没有、也不新增依赖）：

- 跑法：`venv\Scripts\python.exe tests\run_all.py`；另有 `--offline`（只跑离线用例）、`--only <关键字>`（只跑匹配模块）、`--port`（换端口）、`--no-js`。
- 结构：[harness.py](tests/harness.py)（断言计数、真实服务进程管理、HTTP 小工具）、[run_all.py](tests/run_all.py)（汇总 / 起停服务 / 调用 node）、各 `test_*.py`。
- 规模：**256 项检查，全部通过、0 跳过**（含真实服务用例）。
- 覆盖：`.env` 就地更新（注释 / 顺序 / CRLF / 原子写）、单实例锁（函数级 + 端到端双进程）、hypercorn 配置与端口占用提示、M2 IP 判定、M3 错误分类、M5 已删除复查、M6 下载完成度、L4 标签原名与译名匹配（隔离临时库）、L9/L10/L11 与模板一致性、L5/L6/L7/L8 的静态与渲染落地、HTTP 冒烟与错误响应。
- 安全边界：在线用例只发只读请求；写操作只用到 `POST /api/artworks/batch-delete` 的空数组与错误类型请求体，不触碰真实库数据。
- 前端：[tests/js/common_js.test.js](tests/js/common_js.test.js) 用 node 的 `vm` 直接加载 [partials/common_js.html](app/templates/partials/common_js.html) 里的真实代码，测 `escapeHtml` / `escapeAttr` / `r18Param` / `showLoadError` / `fetchJson` 共 25 项（其中 `escapeAttr` 必须比 `escapeHtml` 多转义引号，是 L5 防属性闭合的关键）。

### 15.2 `run.py` 的自研 ASGI 服务器换成 hypercorn

删掉手写的 `ASGIHTTPServer`（约 120 行：手动 HTTP 解析、状态文案、keep-alive），改用 `hypercorn.asyncio.serve` + `hypercorn.config.Config`：

- 行为保持不变：只绑定 `127.0.0.1`、端口来自 [app/config.py](app/config.py) 的 `DEFAULT_PORT`、单实例锁、启动横幅、`--browser` / `PA_BROWSER`。
- 端口被占用时打印 `启动失败：无法监听 127.0.0.1:<port> —— …` 并返回 1（不再抛 traceback）。
- `Config` 关掉 access log（保持原先安静的日志）、`graceful_timeout = 5s`。
- [requirements.txt](requirements.txt) 加回 `hypercorn>=0.17.0`；0.18 的运行时依赖只有 `h11` / `h2` / `priority` / `wsproto`，**不需要 `aiofiles`**。
- 曾在 [Start.bat](Start.bat) 的回退探测里补上过 `hypercorn`（原为 `fastapi/pixivpy3/dotenv/PIL`，见 15.7），**随后已改回原样**：收益不抵成本——回退分支选错解释器时 `run.py` 本来就会抛 ImportError，`Start.bat` 也会打印「dependencies missing → run install.bat」，而改动脚本会在本机触发安全软件的「无法验证开发者」提示。

验证：套件全绿，其中在线用例是**真实起停 hypercorn 服务**（第二实例持锁被拒、坏请求后服务存活、10 个端点冒烟、页面渲染）。

### 15.3 L8 的结论：不手写虚拟滚动，改用浏览器原生机制

- 图库 `.wall` 是 **CSS 多列瀑布流**（`columns` + `break-inside: avoid`），瓦片高度随图片比例变化，**行高不固定**；手写窗口化（spacer 行 / 绝对定位换算行号）在这种布局下必然错位，而本环境**没有可用的浏览器**（见 15.4），无法验证视觉结果。
- 每页本身就是 `PAGE_SIZE = 100`，DOM 节点数已有上限；虚拟滚动的主要收益其实是「屏外瓦片不参与布局与绘制」——这正是 `content-visibility: auto` 的语义。
- 因此改 [style.css](app/static/css/style.css)：`.tile` 加 `content-visibility: auto; contain-intrinsic-size: auto 320px;`（`auto` 记住上次渲染高度，避免滚动条跳动；不支持该属性的浏览器直接忽略，行为与改动前一致）。静态断言已纳入 `tests/test_static_assets.py`。
- 真要做 DOM 窗口化，得先把瀑布流改成等高网格（视觉会变）并在浏览器里逐项核对，属于独立改造，本轮不做。

### 15.4 前端运行时验证（如实记录）

- 本轮尝试用浏览器工具做真实 UI 验证，**失败**：browser 插件依赖的后端 CLI（`bsk`）未安装，工具直接返回 `the bsk CLI ("bsk") was not found`。因此**前端代码依旧没有在浏览器里真正跑过**——L6 重构、L7 `fetchJson`、L5 转义、L8 的 `content-visibility` 都只有服务端渲染断言、静态断言与 node 单测支撑。
- 要补上这一环，需要安装 BrowserSkill（`bsk`）或引入 headless 浏览器。

### 15.5 移除 exe 打包链路（用户决定不再打包）

用户明确要求「不需要打包了」，以下内容整体删除，不再维护：

| 删除项 | 原用途 |
|--------|--------|
| `build-exe.bat` / `build.ps1` | 一键打包（venv 引导、图标生成、产物校验） |
| `PixivArchive.spec` | PyInstaller onefile / 无控制台配置 |
| `requirements-build.txt` | 打包依赖（`pyinstaller`） |
| `browser_entry.py` | exe 专用入口（强制 `PA_BROWSER=1`） |
| `scripts/make_icon.py`、`assets/icon.ico`、`assets/icon.png` | 打包用图标（网页 favicon 是 `app/static/favicon.png`，不受影响） |
| `Release/`、`build/` | 产物与 PyInstaller 缓存（已构建出的 exe 一并删除） |

同步清理的引用：

- [README.md](README.md)：删掉「方式二：直接使用 exe」「构建方式」（一键脚本 / PowerShell 脚本 / 手动步骤 / spec 说明）两节、目录结构里的打包条目、`requirements-build.txt` 依赖说明、杀毒软件报毒 FAQ、命令行参数表里的 exe 列；依赖清单补上此前漏写的 `hypercorn`；新增「回归测试」一节。
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：删掉 Build time（PyInstaller）一节。
- [.gitignore](.gitignore)：删掉 `Release/` / `dist/` / `build/` 三条构建产物忽略规则。
- [tests/test_static_assets.py](tests/test_static_assets.py)：原 L10 断言（校验 `build.ps1` / spec）改为**反向断言**——打包文件必须不存在、文档（去掉「本次变更」小节后）不得再出现 `PyInstaller` / `Release\PixivArchive.exe` / 已删文件名；并断言 [Start.bat](Start.bat) 只调用 `run.py`、保持未被改动（原因见 15.7）。

保留未动的一处：`app/paths.py` 里 `getattr(sys, "frozen", False)` 的「冻结时用 exe 同级目录」分支。它是几行运行时路径决策，源码运行走的是同一个 `else` 分支——删掉对现状零影响、留着对将来零成本，所以按「运行时代码」而非「打包工具」处理。（若希望连它一起删，说一声即可。）

也要如实说明一件事：本轮**确实真实跑了一次打包**（`powershell -ExecutionPolicy Bypass -File .\build.ps1`，退出码 0，产出 `Release\PixivArchive.exe`，21.95 MB；包内 `app/templates`、`app/static/css/style.css` 等资源齐全；`--help` 退出码 0），但**把 exe 跑起来提供服务没有成功**：无控制台 exe 下 `sys.stdout` 是 `None`，[run.py](run.py) 打印启动横幅时会抛 `AttributeError`，服务起不来（这个缺陷在重构前的老代码里同样存在，只是从前没真跑过）。随后按你的要求把打包链路整体删掉，该缺陷随之不再有影响；如果将来重新引入 exe，第一件事就是给 `run.py` 加 `if sys.stdout is None: sys.stdout = open(os.devnull, "w")`。

### 15.6 相关提交

- `2135323` 新增常驻回归测试套件（235 项）＋ `run.py` 换 hypercorn ＋ `build.ps1` 产物路径修正。
- 本节改动（L8 ＋ 前端单测 ＋ 移除打包链路 ＋ 文档同步）见随后提交。

### 15.7 启动器 `Start.bat` 还原：本机安全软件的「无法验证开发者」只冲着被改动的脚本

用户反馈：双击 [Start.bat](Start.bat) 会弹「无法验证开发者」的风险提示，而本次会话前留存的副本（`Start1.bat`，内容就是改动前的 `Start.bat`）可以正常运行、没有提示。

事实对照（都实测过）：

| 项 | `Start.bat`（改动后） | `Start1.bat`（会话前留存） |
|----|----------------------|---------------------------|
| git blob | `0ba3c1c4…` | `8fffe404…` = `2ad47e5:Start.bat`，**与会话前完全一致** |
| 行尾 / BOM / 非 ASCII | LF / 无 / 0 | LF / 无 / 0 |
| `Zone.Identifier`（来自网络的标记） | 无 | 无 |
| 属性 / 创建时间 | Archive / 相同 | Archive / 相同 |
| 与另一份的内容差异 | — | **只差 15.2 里加的 11 字节（`hypercorn, `）与随之变化的修改时间** |

系统侧没有任何记录可查：`Microsoft-Windows-SmartScreen/Debug`、`Microsoft-Windows-SmartAppControl/Operational`、`Microsoft-Windows-AppLocker/EXE and DLL`、`Microsoft-Windows-AppLocker/MSI and Script` 都没有匹配事件，Defender 日志也是空的。也就是说这不是 Windows 的 SmartScreen / 智能应用控制拦截，而是本机第三方安全软件对「被改动过的脚本」的判定——同一内容、未改动的副本放行，改过的就提示。

处理方式（按「最小惊讶」）：

- `Start.bat` **还原为会话前字节**（blob 回到 `8fffe404…`），`fc /b` 与 `Start1.bat` 完全一致，长度 1013 字节，修改时间也改回 `2026-09-02 22:02:19`；
- [tests/test_static_assets.py](tests/test_static_assets.py) 的 L10 断言相应改为「`Start.bat` 只调用 `run.py`、不含额外探测」，防止以后又把它改回去；
- 代价是 `hypercorn` 探测被去掉。这个探测只影响「venv 不存在、PATH 解释器缺 hypercorn」这一种情况：真选错了，`run.py` 同样会抛 ImportError，`Start.bat` 也会打印「dependencies missing → run install.bat」。**若仍想要更清楚的提示，正确位置是 `run.py` 顶部对 hypercorn 的 import 做 try/except 给出可读信息，而不是改启动器**——启动器以后不再由我改动，确需改动会先征询。

用户留存的 `Start1.bat` 在版本库里是未跟踪文件（`git status` 为 `?? Start1.bat`），保留还是删除由用户决定，本轮没有动它。