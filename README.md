# Pixiv Archive

一个本地运行的 Pixiv 图库归档与浏览工具：扫描你本地已下载的 Pixiv 图片，构建可搜索的作品数据库并生成缩略图，然后通过本地网页界面进行浏览、搜索、按作者/标签/收藏夹整理，并可联网同步作品的元数据（标题、标签、作者信息等）。<br>
**注意：此工具不含任何在线浏览功能！！！**

---

## 本次变更：移除局域网模式、桌面窗口模式与 exe 打包链路

- **移除局域网访问（LAN 模式）**：服务固定只绑定 `127.0.0.1`，不再开放到局域网，访问令牌体系整体删除（`--lan` / `Start-LAN.bat` / `PA_HOST` / `PA_ACCESS_TOKEN` / `?token=` / Cookie / `X-Access-Token` / 前端 `pa_token.js`）。缩略图可被局域网枚举、令牌经 URL 泄漏等隐患随之消失。
- **移除桌面窗口模式**：不再内嵌 pywebview 原生窗口，`launcher.py` / `launcher.bat` 一并删除；启动方式统一为 `Start.bat`，用系统默认浏览器呈现界面。
- **单实例锁并入启动链路**：`run.py` 用数据目录下的独占锁文件（`.instance.lock`，含 PID 存活检测）保证同一时间只运行一个实例，重复双击 `Start.bat` 不会起第二个服务。原先的 Windows 命名互斥体在创建失败（同名内核对象属于别的安全上下文）时会静默放行，已据此替换。托盘与端口自动避让随窗口模式一并移除——端口被占用时直接启动失败，请改端口。
- 依赖中去掉 `pywebview` 与 `pystray`。
- **移除 exe 打包链路**：`build-exe.bat`、`build.ps1`、`PixivArchive.spec`、`requirements-build.txt`、`browser_entry.py`、`scripts/make_icon.py` 与 `assets/` 图标全部删除，`Release/`、`build/` 产物目录一并清掉。本工具**只从源码运行**（`install.bat` 初始化 + `Start.bat` 启动），不再提供单文件 exe。

---

## 版本 1.2.1 更新说明

**1. 新功能：收藏订阅**
- 订阅任意 Pixiv 用户的**收藏列表**：输入用户 ID（用户主页 URL 中的数字），首次订阅会自动下载该用户公开收藏的全部作品；之后每次点「订阅更新」只**增量检查**并下载上次检查后新收藏的作品，无需重新扫描整个列表。
- **全自动处理**：一次检查任务内依次完成「下载新收藏 → 扫描入库 → 生成缩略图 → 同步元数据」，结束后画廊直接可看，全程无需手动操作。
- 新增「收藏订阅」视图：订阅卡片显示当前进度与上次检查结果，支持单个「检查」、一键「退订」（已下载的作品会保留），每个订阅有独立的「自动下载」开关。
- 订阅前可**预览**：输入 ID 点「查询」即可看到用户名与收藏规模，再决定是否订阅。
- 收藏下载遵循「同步间隔」设置限速防风控；任务进度实时显示，可随时停止。
- 说明：受 Pixiv 隐私策略限制，仅能获取用户**公开**的收藏。

**2. 功能调整**
- 移除「画师订阅」与「按作者下载」，由更聚焦的「收藏订阅」取代；侧栏入口相应调整，浏览、搜索、收藏夹、元数据同步等其它功能不受影响。

**3. 界面与使用体验**
- 站内所有确认弹窗统一为应用自身风格：删除作品（列表 / 查看器 / 批量 / 详情页）、删除收藏夹、取消订阅均使用一致的面板与红色危险操作按钮，不再弹出浏览器原生对话框。
- 作品详情页的操作结果提示（在文件夹中打开、删除失败等）改为应用内 Toast，与主界面一致。
- 修复侧边栏收起后「安全模式」徽标显示异常的问题：收起时显示为居中的紧凑图标，悬停展开时恢复完整样式。

---

## 功能特性

- **本地扫描入库**：递归扫描本地 Pixiv 下载目录，从文件名解析作品中对应的 Pixiv ID（兼容 `12345678_p0` 等多页命名），自动去重（按 SHA-256）、识别缺失文件，并写入 SQLite 索引（`archive.db`）。
- **缩略图生成**：为库内作品自动生成统一尺寸的缩略图（JPG），供网格浏览快速加载。
- **网页浏览界面**：网格/排序浏览、多图页查看原图、作品详情页（标题、描述、标签、作者）。
- **多种检索方式**：关键词搜索（标题 / 描述 / 作者 / 标签 / Pixiv ID），按作者、标签、收藏夹筛选。
- **作者 / 标签体系**：自动聚合作者列表与标签列表，查看某作者或某标签下的全部作品。
- **收藏夹管理**：创建/删除收藏夹，将作品批量加入收藏夹。
- **收藏订阅**：订阅 Pixiv 用户的公开收藏列表，按增量游标自动下载新收藏的作品，一键完成 下载 → 入库 → 缩略图 → 元数据同步 全流程（v1.2.1 新增）。
- **元数据同步**：通过 Pixiv refresh token 联网同步作品的标题、标签、作者名、创建日期、尺寸等信息；作品已删除时自动标记（`deleted`）。
- **直连 / 代理自适应**：`PIXIV_MODE` 支持 `direct`（直连 IP 免 SNI）、`proxy`（走 Clash 类代理）、`auto`（自动尝试并回退）。
- **第三方图片镜像（可选）**：设置中可填写镜像域名，用第三方图站接管图片下载与头像获取（作品列表与认证仍走 Pixiv 官方 API）。
- **一键启动**：`.bat` 脚本自动查找 Python、创建 venv、安装依赖，`Start.bat` 直接启动本地服务。
- **本地单实例服务**：固定绑定 `127.0.0.1`，仅本机可访问；同一时间只允许一个实例运行。

---

## 目录结构

```
app/                 应用代码
  main.py             FastAPI 路由、页面与 API
  database.py         SQLite 连接与建表/迁移
  scanner.py          本地图片扫描与入库
  sync.py             Pixiv 元数据同步
  pixiv.py            Pixiv API 客户端（直连/代理）
  jobs.py             后台任务（扫描/同步）管理与进度
  thumbnails.py       缩略图生成
  templates/          页面模板（index.html、artwork.html、author.html、tag.html）
  static/             前端资源（css/style.css、js/、favicon.png）
run.py               启动本地 HTTP 服务（仅 127.0.0.1，含单实例锁）
Start.bat            启动服务（浏览器模式）
install.bat          一键初始化环境（创建 venv + 安装依赖 + 生成 .env）
tests/               回归测试套件（venv\Scripts\python.exe tests\run_all.py）
tools/               维护脚本（gen_requirements_lock.py：重新生成依赖锁）
requirements.txt     依赖清单（宽松版本约束）
requirements.lock    依赖锁定清单（含各平台轮子 sha256，供可复现安装）
.env.template        环境变量模板（复制为 .env 后填写）
```

---

## 环境要求

- **操作系统**：Windows
- **Python**：3.10+（开发与验证环境为 3.11）
- **网络**：同步元数据 / 刷新直连 IP 需要能访问 pixiv（必要时搭配代理）

依赖（`requirements.txt`）：`fastapi`、`pydantic`、`requests`、`urllib3`、`pixivpy3`、`Pillow`、`Jinja2`、`python-dotenv`、`hypercorn`。

### venv 里装的是什么？

`venv/` 只是普通 Python 虚拟环境（`python -m venv venv` 创建，再用 `venv\Scripts\python.exe -m pip install -r requirements.txt` 安装依赖）。里面没有任何个人数据，也不进版本库（`.gitignore` 已排除）；换台机器运行 `install.bat` 即可自行创建。

### 依赖锁定与哈希校验

`requirements.lock` 锁定了 `requirements.txt` 解析出的**完整依赖闭包**（含间接依赖），并附带每个包在 Windows + CPython 3.10–3.13 下各轮子的 `sha256`。用它安装可复现、可校验：

```
venv\Scripts\python.exe -m pip install -r requirements.lock --require-hashes
```

`install.bat` 默认走这条路；若本机 Python 版本不在覆盖范围内，pip 会拒绝，脚本会自动回退到 `requirements.txt`（此时没有哈希校验）。

> **为什么不再用 `--trusted-host`**：它会关闭 pip 对 PyPI 的 TLS 证书校验，让中间人可替换下载内容。现在默认走完整校验；若确需绕过自签证书的中间盒，请用 `PIP_CERT` 指向可信 CA，而不是长期关闭校验。

重新生成锁（需联网，会用到 `tools/gen_requirements_lock.py`）：

```
venv\Scripts\python.exe tools\gen_requirements_lock.py
```

---

## 使用教程

### 运行步骤

**第 1 步：初始化环境（仅首次）**

双击 `install.bat`，脚本会自动：
1. 查找系统 Python（`python` → `py`）；
2. 创建本地虚拟环境 `venv/`；
3. 安装依赖：优先用 `requirements.lock` 做带哈希校验的可复现安装；若本机解释器不在锁的覆盖范围（Windows + CPython 3.10–3.13）则自动回退到 `requirements.txt`；
4. 若不存在 `.env`，自动从 `.env.template` 复制生成。

> 没有 Python？先到 https://www.python.org/downloads/ 安装，安装时勾选 "Add python.exe to PATH"，再运行 `install.bat`。

**第 2 步：配置 `.env`**

编辑根目录的 `.env`，至少填写两项：

```ini
PIXIV_REFRESH_TOKEN=你的refresh_token
IMAGE_SOURCE_DIR=D:\Pixiv
```

**第 3 步：启动**

| 场景 | 操作 | 说明 |
|------|------|------|
| 本机使用 | 双击 `Start.bat` | 启动本地服务（仅绑定 127.0.0.1），随后用浏览器访问 http://127.0.0.1:6814 |
| 重复启动 | — | 已有实例运行时会提示并退出，不会起第二个服务 |

启动脚本会优先使用 `venv\Scripts\python.exe`，其次回退到 PATH 中已装好依赖的 `python` / `py`；找不到合适的 Python 时会给出提示。

**第 4 步：首次导入作品**

打开网页后，进入 **设置** 页确认 `本地图片目录`，然后点击 **扫描**。扫描会把该目录下所有 Pixiv 图片（文件名包含 7–10 位数字 ID）写入数据库并生成缩略图。

**第 5 步：同步元数据（可选）**

在 **设置** 填入 `PIXIV_REFRESH_TOKEN` 后点击 **同步**，为库内作品联网拉取标题、标签、作者等信息。

### 界面与常用操作

- **扫描**：把本地图片目录中识别出的作品加入库并生成缩略图（可设置取消/重置）。
- **同步**：联网为库内作品补充元数据；缺失/失效 token 会在界面给出明确提示。
- **搜索**：顶部搜索框按 标题/描述/作者/标签/Pixiv ID 全文检索；可按作者、标签、收藏夹组合筛选，支持多种排序。
- **收藏夹**：在作品详情或列表页批量加入/移除收藏夹。
- **作者 / 标签页**：`/author/<id>`、`/tag/<名称>` 页面聚合作者、标签下的全部作品。
- **R18 模式与已删除筛选**：顶栏「R18」循环切换 全部 / 隐藏R18 / 仅R18；「已删除」按钮快速只看被删作品。两者会同步作用于搜索、作者、标签、收藏夹与跨页浏览。
- **在文件夹中打开**：查看器与作品详情页的「在文件夹中打开」按钮，可在资源管理器中定位并选中图片文件。

### 环境变量一览（`.env`）

| 变量 | 说明 |
|------|------|
| `PIXIV_REFRESH_TOKEN` | Pixiv refresh token（同步元数据必需） |
| `IMAGE_SOURCE_DIR` | 本地 Pixiv 图片目录（扫描必需） |
| `PIXIV_MODE` | `direct` 直连 / `proxy` 代理 / `auto` 自动（默认 auto） |
| `PIXIV_PROXY` | 代理地址，如 `http://127.0.0.1:7890` |
| `THUMBNAIL_SIZE` | 缩略图边长，默认 400 |
| `THUMBNAIL_DIR` / `METADATA_DIR` | 缩略图 / 元数据存放目录名 |
| `PA_PORT` | 服务端口，默认 6814（固定绑定 `127.0.0.1`，不可改绑定地址） |
| `PIXIV_IP_*` | 直连模式下的 pixiv 域名 → IP 覆盖（网页设置中可刷新） |

### 直连模式的 TLS 校验：一个有意为之的取舍

`PIXIV_MODE=direct`（以及默认的 `auto` 在回退到直连时）为了绕开针对 SNI 的阻断，采用「域名 → IP 覆盖 + 握手时不发送 SNI」的方式连接。这个方案**必须关闭 TLS 证书校验与主机名校验**：不发送 SNI 时服务端不会返回与目标域名匹配的证书，一旦开启校验会直接把连接判失败。

代价需要说明白：此时 HTTPS 的信任根从「证书链 + 主机名」降级为「相信域名 → IP 的解析结果（DoH 应答）」。也就是说，**能篡改你 DoH 解析结果的中间人，可以把自己伪装成 pixiv**。这是为换取「在被 SNI 阻断的网络里仍然可用」而付出的代价，不是实现疏漏。

作用范围仅限直连会话：`app/direct_connect.py` 中专为直连构造的 SSL context，以及 `app/pixiv.py` 里非代理分支的 `session.verify = False`。**`proxy` 模式不受影响**，走标准 SNI 与完整证书校验（经代理隧道时不发送 SNI 会被 Cloudflare 直接掐断，所以那里本就必须用标准握手）。如果你的网络不需要直连，用 `PIXIV_MODE=proxy` 可以保留完整校验。

---

## 回归测试

改动后建议跑一遍测试套件（不需要 pytest，直接用项目 venv）：

```bat
venv\Scripts\python.exe tests\run_all.py             :: 全部：离线用例 + 真实服务用例
venv\Scripts\python.exe tests\run_all.py --offline   :: 只跑不需要起服务的用例
venv\Scripts\python.exe tests\run_all.py --only lock :: 只跑名字匹配的模块
```

用例会真实启动服务（默认端口 7801，可用 `--port` 改），只发只读请求；覆盖 `.env` 就地更新、单实例锁、扫描/同步的阶段性逻辑、删除失败上报、标签原名与译名匹配、页面片段完整性、HTTP 端点冒烟与错误响应、模板与资源一致性等。本机有 Node 时会顺带跑 `tests/js/` 下的前端单测。

---

## 数据与隐私

- 本仓库/产物**不包含任何个人数据**：不含 `.env`（token）、`archive.db`（作品记录）、图片、缩略图、元数据或日志。
- 运行后仅在你的机器上生成以下本地数据（都在项目根目录）：
  - `archive.db`：作品索引（SQLite）
  - `thumbnails/`、`metadata/`：缩略图与元数据 JSON
  - `pixiv_archive.log`：运行日志
  - `.env`：你的配置（token 等，仅本机保存）
- 服务固定只绑定 `127.0.0.1`，局域网内其它设备无法访问本工具，也不再有访问令牌。

---

## 常见问题（FAQ）

**启动时报 `ModuleNotFoundError`？**
说明当前 Python 缺少依赖。运行 `install.bat` 创建 venv 后，用 `Start.bat` 重新启动。

**服务启动但浏览器打不开 / 页面 404？**
确认访问地址端口与启动日志一致（默认 6814）。服务直接绑定设定端口，被占用即启动失败（不再自动避让）——请关闭占用端口的程序，或在设置 / `.env` 中改用其它 `PA_PORT`。

**扫描后没有缩略图？**
确认 `IMAGE_SOURCE_DIR` 目录存在且可读、文件名确实包含 Pixiv ID（7–10 位数字）、图片格式属于 jpg/jpeg/png/webp/gif/bmp。

**同步失败 / 认证失败？**
确认 `.env` 中 `PIXIV_REFRESH_TOKEN` 有效未过期，且网络可访问 pixiv；可在设置中切换直连 / 代理模式（`PIXIV_MODE` / `PIXIV_PROXY`）。

**局域网下其他设备无法访问？**
这是预期行为：服务固定只绑定 `127.0.0.1`，局域网访问支持已移除，仅本机可用。

---

## 命令行参数速查

| 参数 | 适用脚本 | 说明 |
|------|----------|------|
| `--port <端口>` | `run.py` / `Start.bat` | 指定端口（默认 6814；被占用即启动失败） |
| `--browser` | `run.py` | 启动后用系统默认浏览器自动打开页面（默认不自动打开，`Start.bat` 也不带） |

---

## 许可证

本项目以 **GNU GPL v3** 发布，见仓库根目录 [`LICENSE`](LICENSE)。

采用 GPL v3 的原因：本项目的直连模块（`app/direct_connect.py`，免 SNI / 直连 IP 方案）参考了开源项目 [PixEz](https://github.com/Notsfsssf/Pix-EzViewer)（及其 Flutter 版 [pixez-flutter](https://github.com/Notsfsssf/pixez-flutter)，GPL-3.0）的实现思路，为避免合规风险，整体以 GPL-3.0 发布其源码。在此感谢 PixEz 作者 **Notsfsssf** 提供的直连方案。

- 本仓库内的**代码**：GPL-3.0
- **依赖的第三方库**：各自许可证见 [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES)，与 GPL-3.0 兼容
- **图片版权**：作品版权归相应 Pixiv 作者所有；本工具仅作本地归档，请尊重原作者权利并遵守 Pixiv 使用条款