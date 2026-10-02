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
| **M1** | **直连模式完全关闭 TLS 校验**：`check_hostname=False`、`verify_mode=CERT_NONE`、`server_hostname=None`，且 `urllib3.disable_warnings` 全局静音告警。动机是绕开 SNI 阻断，但这使 HTTPS 的信任根退化为"相信 DoH 应答与 DNS"。属于**设计取舍**，但建议在 README 的隐私/安全章节显式声明，让用户知情。 | [app/direct_connect.py:101-102](app/direct_connect.py#L101-L102)、[138-139](app/direct_connect.py#L138-L139)、[10](app/direct_connect.py#L10) | **读码确认 · 设计取舍** |
| **M2** | **IP 可用性判定过宽**：`_validate_ip` 只要 `status_code < 500` 就算这个 IP 可用，因此 403 / 404（被墙返回的拒绝页）也会被采纳并写回 `.env`，把坏 IP 固化下来。 | [app/direct_connect.py:197](app/direct_connect.py#L197) | **读码确认** |
| **M3** | **非认证类错误被误判为"作品已删除"**：`_detail_once` 在非认证分支直接 `raise PixivDeletedError(msg)`，于是 429/5xx/解析异常都可能把作品错标为 `deleted`，前端随即把它归入"已删除"筛选。 | [app/pixiv.py:282](app/pixiv.py#L282) | **读码确认** |
| **M4** | **随机排序下分页必然重复与漏项**：`ORDER BY RANDOM()` 配 `LIMIT/OFFSET`，每页独立重排，翻页结果交叉重叠。 | [app/main.py:139](app/main.py#L139)、[394](app/main.py#L394) | **读码确认** |
| **M5** | **已删除作品每轮同步都会被重新请求**：筛选条件无条件包含 `pixiv_status = 'deleted'`，而 Pixiv 端已删除的作品每轮都会再次失败/再次请求，随时间线性增加无效请求。 | [app/sync.py:38-39](app/sync.py#L38-L39) | **读码确认** |
| **M6** | **多页作品按前缀判存在会误判**：`_work_exists` 用 `name.startswith(prefix)`，前缀 `..._123` 会命中 `..._1234`；多页作品只下到第 1 页也会被判为"已完成"。 | [app/download.py:217-223](app/download.py#L217-L223) | **读码确认** |
| **M7** | **依赖声明错配**：`hypercorn`、`aiofiles` 在 [requirements.txt](requirements.txt) 中声明却**全项目零 import**（实测 grep 仅命中 `venv/`）；反过来 `requests`、`urllib3`（direct_connect）、`pydantic`（main.py 多处）被直接 import 却没声明，只靠 `pixivpy3`/`fastapi` 的传递依赖，上游一旦调整就会断。 | [requirements.txt:2](requirements.txt#L2)、[7](requirements.txt#L7) | **已实测** |
| **M8** | **GET 触发写操作，缺少 CSRF 防护**：`/api/scan`、`/api/sync`、`/api/bookmark-subs/check` 用 GET 启动写任务，本机浏览器访问任意恶意页面即可用 `<img>`/`fetch` 触发。局域网模式移除后跨设备风险消失，但本机 CSRF 仍在。 | [app/main.py](app/main.py) 对应路由 | 读码确认 |

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

按你的要求，`launcher.py` 的 Windows 命名互斥体（`PixivArchive_SingleInstance`）迁移到 [run.py:30-47](run.py#L30-L47)，因此**重复双击 `Start.bat` 不会再起第二个服务**；非 Windows 或调用失败时返回放行值，不阻塞启动。同时新增 `--browser`（exe 通过 `PA_BROWSER=1` 默认开启）。

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

- `build.ps1` 含中文字面量，其解码取决于文件 BOM 与 PowerShell 版本，本次未验证 BOM。
- 打包（PyInstaller）链路未实际执行，`build-exe.bat` / `build.ps1` 的问题均为读码确认。
- `PixivArchive.spec` 加入 `excludes` 后的实际产物体积未验证（需真实构建一次）。
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

M1–M8 与 L1–L11 维持报告中的原状。建议继续按第八节优先级处理，其中 M7（依赖声明错配）、M4（随机排序分页重复）、M2/M3（直连 IP 校验过宽、非认证错误误判为已删除）收益最高。