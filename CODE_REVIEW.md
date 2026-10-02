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

**残留风险（未验证）**：未联网确认真实 Pixiv 对已删除作品返回的确切文案。若真实文案不含任何关键词，该作品会被记为 `sync_error` 并反复重试——方向是保守的（不会误删作品），但可能表现为「删不掉」。关键词表已覆盖中英日常见文案。

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

**已知外观小瑕疵（未改）**：`GET /api/bookmark-subs/check` 的 405 响应带 `Allow: DELETE`——Starlette 用「第一个路径部分匹配」的路由填这个表头，而 `DELETE /api/bookmark-subs/{pixiv_user_id}` 注册在它之前。POST 仍会 FULL 匹配到正确路由，功能无影响；若要干净的 `Allow: POST`，把该 POST 路由移到 DELETE 之前即可。

### 本轮验证总账

| 验证脚本 | 覆盖 | 结果 |
|----------|------|------|
| 逻辑验证脚本（临时目录 + 临时库） | M2 9 项、M3 7 项、M5 9 项、M6 8 项 | **33/33 通过** |
| HTTP 验证脚本（真实服务，端口 6906） | M8 11 项、M4 12 项 | **23/23 通过** |

两个脚本均为一次性临时脚本，未纳入版本库（如需可整理成回归测试）。全部断言都在临时目录/临时库上执行：逻辑脚本通过改写 `app.database.DB_PATH` 隔离数据库，实测运行前后真实 `archive.db` 的大小与 mtime **完全未变**。

真实库在本轮前后清点一致：1310 作品 / 1890 图片 / 555 作者 / 1 已删除 / 1310 元数据 JSON / 1310 缩略图，元数据与作品 1:1（孤儿 0、缺 JSON 0）。

### 已知未处理项

- L1–L11 维持报告中的原状。
- M3 的真实删除文案未联网确认（见 M3 残留风险）。
- M8 的 `Allow: DELETE` 表头不精确（纯外观，见 M8 一节）。

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