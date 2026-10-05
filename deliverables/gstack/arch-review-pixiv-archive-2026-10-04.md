# Pixiv-Archive 项目整体梳理与评审报告

**日期**：2026-10-04
**场景**：全流程交付 —— 结构梳理 + 架构/代码质量评审 + 安全审计
**审查对象**：`D:\Little Tool\Pixiv-Archive`（README 标注 v1.2.1）
**参与成员**：🔧 调查员（gstack-investigator）+ 🔍 产品评审员（gstack-product-reviewer）+ 🛡️ 安全官（gstack-security-officer）
**审查方式**：通读全部自研源码（`app/` 13 个模块 + `run.py` + 10 个模板/片段 + `tests/`，约 8.4k 行，不含 `venv/` 与图库数据），逐项回到源码核对并标注 `文件:行号`。
**环境限制（重要）**：本环境沙箱禁止派生子进程（`python`/`node`/`git` 均被 SIGTERM），因此**未能实机起服务、跑测试或用浏览器复现**；所有结论为**源码级确认**，安全审计中的运行时复现项已逐条标注置信度。

---

## 📌 TL;DR

- **整体结论：🟡 有条件通过** —— 高完成度的小工具，架构干净、无循环依赖，常见高危缺陷（SQL 注入 / 路径穿越 / 命令注入 / 反序列化）均已正确规避；历史 `CODE_REVIEW.md` 的 22 条修复经复核 **21 条真实落地、1 条（L8）为有意的部分完成**，没有"假修复"。
- **但有三处真伤，均为历史报告未覆盖 / 覆盖不全**：① 前端转义范式缺陷（`escapeAttr` 被用于内联 `onclick` 的 JS 字符串，可被实体解码击穿 → 存储型 XSS）；② 本机 CSRF 与 DNS rebinding（全站无 Host/Origin 校验）；③ `api_add_bookmark_sub` 用 `async def` 做阻塞网络调用，会卡死事件循环。
- **阻塞项数量**：3 条 P0（1 高 XSS + 1 高访问控制 + 1 高并发），另有 1 条"文档与实现不符"（DoH 引导未校验，比 README 声明更严重）。
- **下一步**：先修 3 条 P0（约半天量级），再补"删数据路径"的测试网，最后才谈 `main.py` 拆分。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 条件 Go（可继续自用；对外分发 / 多人环境前需先修 P0） |
| 安全评分 | **C**（Critical 0 · High 2 · Medium 3 · Low 4 · Info 3） |
| 严重度分布 | 🔴 0 / 🟠 5 / 🟡 11 / 🟢 6（合并去重后共 25 条） |
| 关键行动项 | 8 条（P0×3、P1×5） |
| 最大架构问题 | `app/main.py` 1218 行"上帝模块"（路由 + SQL + 业务编排混住） |
| 最大测试缺口 | `scanner`/`sync`/`download` 三条"写/删数据"路径几乎无测试；在线用例硬编码开发者真实库 PID |
| 建议负责人 | 前端转义/事件委托 → 前端；Host/Origin 中间件 + async 修复 → 后端 |

---

## 0. 项目速览（结构 · 技术栈 · 模块 · 依赖 · 流程）

### 0.1 一句话定位

纯本地运行的 Pixiv 图库归档与浏览工具（**不含在线浏览**）：扫描本地已下载图片 → 解析 Pixiv ID 建库（SQLite）+ 生成缩略图 → 本地网页浏览 / 搜索 / 整理 → 可选联网同步元数据、订阅他人公开收藏并增量下载。服务固定绑定 `127.0.0.1`，单实例。

### 0.2 技术栈

| 层 | 选型 | 承担 |
|---|---|---|
| 语言 | Python 3.11（要求 3.10+） | 全部后端 |
| Web 框架 | FastAPI | 路由、参数校验、静态/模板挂载 |
| 请求体模型 | pydantic v2 | JSON body 校验（注意：Model 类散落在各路由函数体内） |
| ASGI 服务器 | hypercorn | 真正监听 HTTP（keep-alive / 流式 / 连接错误） |
| 数据库 | SQLite（stdlib，WAL，`foreign_keys=ON`，`timeout=30`） | 全部持久化，8 张表 |
| 模板 | Jinja2 | 服务端渲染页面外壳，数据由内联 JS 异步拉取 |
| 前端 | **零框架、零构建**；原生 DOM + 内联 `<script>`；CSS 单文件 2439 行；SVG 精灵图 | 交互 / 视图切换 / 轮询 / 弹窗 / 查看器 |
| HTTP 客户端 | requests + urllib3 | Pixiv API、图片 / 头像下载、自定义直连适配器 |
| Pixiv SDK | pixivpy3（AppPixivAPI） | 认证、作品详情、用户收藏、用户详情 |
| 图片处理 | Pillow | 缩略图（统一重编码 JPEG q85） |
| 配置 | python-dotenv + 自研 `envfile.py` | `.env` 读取与就地更新 |
| 网络绕行 | 自研 `direct_connect.py`（DoH + IP 覆盖 + 免 SNI TLS） | `PIXIV_MODE=direct/auto` 下在被阻断网络可用 |

### 0.3 模块清单与规模

| 模块 | 行数 | 职责 | 评价 |
|------|-----:|------|------|
| `app/main.py` | **1218** | 全部路由（页面 + API，33 个端点）、R18 过滤 SQL、收藏订阅业务编排、通用工具 | ⚠️ 过载 |
| `app/pixiv.py` | 427 | Pixiv API 客户端：认证、direct/proxy/auto、镜像、错误分类 | 偏大但内聚 |
| `app/direct_connect.py` | 246 | 免 SNI 直连 + DoH + IP 校验 | 内聚 |
| `app/download.py` | 242 | 收藏订阅增量下载（游标 / 限速 / `.part` 原子改名） | 内聚 |
| `app/scanner.py` | 220 | 目录扫描、文件名解析、SHA-256 去重、缺失清理 | 内聚、复杂 |
| `app/sync.py` | 214 | 元数据同步（错误分类 / 已删除判定 / 重试） | 内聚 |
| `app/database.py` | 153 | 连接、建表、索引、3 个幂等迁移 | 内聚 |
| `app/jobs.py` | 119 | 内存后台任务（全局单任务 + 取消） | ✅ 质量高 |
| `app/envfile.py` | 86 | `.env` 就地更新（原子写 + 锁） | ✅ 质量最高 |
| `app/thumbnails.py` | 75 | 缩略图生成 | 内聚 |
| `app/paths.py` | 74 | 集中路径 + 作品边车文件删除 | ✅ 单一实现 |
| `app/models.py` | 50 | 4 个 dataclass | ❌ **全库零 import（死代码）** |
| `app/config.py` | 7 | `DEFAULT_PORT` 唯一来源 | ✅ 极简 |
| `run.py` | 191 | 启动 + 单实例锁（独占锁文件 + PID 存活检测）+ hypercorn | 内聚 |
| `templates/index.html` | 2146 | 单页 5 视图 + 约 1750 行内联 JS（独立文档，不 extends base） | ⚠️ 单体 |
| `templates/`（其余 9） | — | `base.html` + 4 个 partial + 3 个子页 + `icons.html` | ✅ 已去重 |
| `static/css/style.css` | 2439 | 双主题样式单文件（`static/js/` 为空目录） | — |
| `tests/` | 10 文件 | 自研 harness（无 pytest），256 项检查 | 见 §2 |

### 0.4 模块依赖关系（**无循环依赖**）

分层依赖（箭头 = 依赖方向）：

```
[入口层]  Start.bat → run.py → app.main:app
                 └→ app.paths / app.config
[路由层]  app/main.py → jobs, envfile, database, scanner, sync, download, pixiv
                      (+ 函数内延迟 import: thumbnails, pixiv 内部函数, refresh_direct_ips)
[领域层]  scanner  → database, paths
          sync     → pixiv, database, paths
          download → scanner, pixiv, paths (+ 延迟 database)
[基础层]  pixiv          → direct_connect, paths
          direct_connect → envfile, paths
          thumbnails     → paths (+ 延迟 database)
          database       → paths
          叶子: envfile / jobs / models / config / paths
```

- **单向 DAG，无环**：`download → scanner`（取 `IMAGE_EXTENSIONS`）不回流；几处潜在环由"函数内延迟 import"化解。
- **两个隐患**：① import 期副作用（`main.py` 模块加载即 `init_db()`、建目录、`load_dotenv`），跑测试 import 就会触碰真实 `archive.db`；② 全局可变单例（`pixiv._pixiv_client`、`direct_connect` 的 `socket.getaddrinfo` monkeypatch、`database.DB_PATH`），测试改写后**无恢复机制**。

### 0.5 关键执行流程（6 条）

1. **启动与单实例锁**：`run.py` 加载 `.env` → 解析端口 → import `app.main`（触发 `init_db`）→ `acquire_single_instance()`（`O_CREAT|O_EXCL` 建 `.instance.lock` 写 PID；已存在则用 `OpenProcess` + `GetExitCodeProcess` 判持有者存活，死亡则接管）→ hypercorn `serve` 绑 `127.0.0.1`；端口占用 / 已有实例打印提示并 `return 1`；`finally` 删锁文件。
2. **本地扫描入库**：`POST /api/scan` → `jobs.start` → `scan_directory`：`os.walk` 收集 → `extract_pixiv_info` 正则解析 `(\d{7,10})_p(\d+)` 等 → 按 PID 分组 upsert `artworks` → 每文件 `compute_sha256` 去重写 `images` → `_prune_duplicates`（删同 sha 的较大 id）→ `_prune_missing`（删源目录已不存在的行 + `remove_side_files` 清缩略图 / JSON + 重算 `page_count`）→ 有新增则 `generate_all_thumbnails`。
3. **元数据同步**：`POST /api/sync` → `sync_metadata` → `_select_pending_rows`（`title/last_synced IS NULL OR sync_error IS NOT NULL OR (deleted AND 超 30 天)`）→ 逐行限速调用 `get_illust_detail`（`_detail_request` 自己发请求以保住 HTTP 状态码）→ 错误分类：`PixivAuthError` 中止整批、`PixivNetworkError` 记 `sync_error` 下轮重试、`PixivDeletedError`（**状态码 404 优先 + 文案 / 空错误体兜底**）标 `deleted` → 成功则 upsert 作者 / 标签 + 落 `metadata/{pid}.json`。
4. **收藏订阅增量下载**：`POST /api/bookmark-subs/check` → `_run_bookmark_check`（**四步串联**）→ `check_bookmark_subscription`：读已知 PID 集合 → 翻页（≤15 页）以 `last_pid` 为游标（遇 ≤ 阈值或整页已知即停）→ 逐件 `download_single_illust`（`_work_exists` 精确判完成度、`.part` 临时文件 + `os.replace` 原子改名、按 `SYNC_DELAY_MS` 限速）→ 写回游标（**有失败则回退不推进**）→ 再串 `扫描入库 → 缩略图 → 元数据同步`。
5. **后台任务模型**：`jobs.start` 在**同一把锁**内完成 `is_busy()` 检查与注册（避免并发起两个任务写库）；任务跑 daemon 线程；前端每 500ms 轮询 `GET /api/jobs/{id}`；取消走 `threading.Event`；`_MAX_JOBS=50` 防字典膨胀。**这部分实现干净。**
6. **直连模式**：`enable_direct()` 幂等替换 `socket.getaddrinfo` 把 pixiv 域名指向 `.env` 记录的 IP + `DirectAdapter` 用 `check_hostname=False` / `CERT_NONE` / 不发 SNI 的 SSL context；`/api/settings/refresh-ips` → `refresh_ips()` 经 DoH 查最新 IP、`_validate_ip` 校验后写回 `.env`。**proxy 分支走标准 SNI + 完整证书校验。**

### 0.6 数据库 schema（8 张表）

- `authors`(id, pixiv_user_id UNIQUE, name, profile_image)
- `artworks`(id, pixiv_id UNIQUE, title, description, author_id→authors, author_name, create_date, page_count, width, height, pixiv_status['active'|'deleted'], first_seen, last_synced, local_path, sync_error)
- `tags`(id, name UNIQUE, translated_name)
- `artwork_tags`(artwork_id→artworks CASCADE, tag_id→tags CASCADE) — 多对多
- `images`(id, artwork_id→artworks CASCADE, page, path, width, height, sha256, phash[预留未用])
- `favorites`(id, name UNIQUE, create_date)
- `favorite_artworks`(favorite_id→favorites CASCADE, artwork_id→artworks CASCADE, added_date) — 多对多
- `bookmark_subs`(pixiv_user_id PK, name, last_pid 游标, auto_download, last_checked, last_result, created_at) — 独立表，与 authors 逻辑关联（非外键）

外键级联：`artwork_tags` / `images` / `favorite_artworks` 均 `ON DELETE CASCADE`；`artworks.author_id` 无级联。索引 10 个；**缺 `create_date` / `first_seen` 索引**（见 §2 #18）。迁移 3 个（幂等）：补 `sync_error`、删旧 `subscriptions`、`last_bid→last_pid` 改名。

---

## 1. 各成员核心结论

### 🔧 调查员（结构梳理）
- **核心判断**：结构清晰，`run → main → 领域 → 基础设施` 单向分层，**无循环依赖**；`jobs.py`（单任务锁）、`envfile.py`（原子写）、`paths.py`（边车文件唯一实现）三个模块质量突出。
- **关键发现**：`app/models.py` 全库零 import（死代码）；`app/static/js/` 为空目录（JS 全内联）；`escapeHtml/escapeAttr` 在 `common_js.html` 与 `index.html` 各有一份；`init_db()` 在启动期被调用两次；排序白名单含 `create_date`/`first_seen` 但表上无对应索引。

### 🔍 产品评审员（架构 + 代码质量 + 修改计划）
- **核心判断**：历史 22 条修复 **21 条真实落地、L3 误报判定正确、无"假修复"**；但 `main.py` 重新膨胀到 1218 行，是最大架构问题；`api_artworks`/`api_search` 近 80 行重复（L4 修复曾因此改错地方）。
- **关键发现（历史未覆盖）**：`author.html`/`tag.html` 的 `alt` 未转义（L5 漏改）；`index.html:666` 内联 onclick 的 JS 字符串逃逸；`api_add_bookmark_sub` async 阻塞事件循环；在线测试硬编码真实库 PID；`scanner`/`sync`/`download` 三条数据路径几乎无测试。
- **前端工程化结论**：**不值得引入构建链**，保持"零构建 + 抽公共 JS + node 单测"，把 `index.html` 内联 JS 外移以便测试。

### 🛡️ 安全官（STRIDE + OWASP）
- **核心判断**：安全姿态**尚可**——路径穿越、SQL 注入、命令注入、反序列化**均无缺陷**；主要风险集中在两点：前端转义范式缺陷、本地服务无 Host/Origin 校验。
- **关键发现**：F-001 存储型 XSS（9 处内联事件处理器）；F-002 直连 TLS 全关**且 DoH 引导自身也未校验**（比 README 声明更严重）；F-003 无 Host/Origin 校验 → 本机 CSRF + DNS rebinding；F-004 头像抓取受限 SSRF；F-005 token 前 8 字符回显。
- **明确区分**：直连关 TLS / 全站无鉴权 / 无 access log 属**设计取舍**；DoH 未校验、XSS 范式、无 Host/Origin 校验、token 回显属**真实缺陷**。

---

## 2. 综合审查发现（去重合并，按严重度排序）

> 严重度：🔴 严重 · 🟠 高 · 🟡 中 · 🟢 低。来源列标注成员；`F-xxx`/`N-xx`/`P-x` 为各成员原始编号。

| # | 严重度 | 类别 | 位置 | 问题 | 建议 | 来源 |
|---|--------|------|------|------|------|------|
| 1 | 🟠 | XSS 注入 | `index.html:666,865,950-955,1065,1282,1295,1301`（共 9 处） | `escapeAttr()` 把 `'`→`&#39;`，但被用于**内联 onclick 的 JS 字符串**；浏览器先解码实体再编译 JS，字符串闭合被击穿 → 存储型 XSS。**最严重的一条。** | 改用 `data-*` + 事件委托，或 `createElement`+`addEventListener`+`textContent`（参考已有正确范式 `index.html:1985 showCtxMenu`）；补 JS 回归单测 | 安全 F-001 / 评审 N2 |
| 2 | 🟠 | 访问控制 | `app/main.py`（全站无中间件） | 无 Host/Origin 校验、无 CSRF token、无 CORS → 无 body 的 POST 可跨站触发；**DNS rebinding** 可升级为同源读写（读全库、删本地文件、改设置） | 加中间件校验 `Host ∈ {127.0.0.1:PORT, localhost:PORT}` + 写接口 `Origin` 校验；或给写接口统一要求自定义头 | 安全 F-003 |
| 3 | 🟠 | 并发/阻塞 | `main.py:925-942`（阻塞点 `:935`） | `api_add_bookmark_sub` 是 `async def` 却做同步阻塞 HTTP（最长 30s），**卡死整个 asyncio 事件循环** | 改为同步 `def`（与 `api_preview_bookmark_sub` 一致，改 1 行）或 `await asyncio.to_thread(...)` | 评审 N3 |
| 4 | 🟠 | 传输安全 | `direct_connect.py:99-109`、`138-150`、`84` | 直连关证书 / 主机名校验（**设计取舍**），但 **DoH 引导查询 `_doh_query` 也未校验** → "信任根 = DoH 应答"不成立，MITM 可伪造 DoH 应答并固化攻击者 IP（**真实缺陷**） | DoH 查询恢复 SNI + `CERT_REQUIRED`；pixiv 主机尝试证书 pinning；README 补"DoH 亦未校验" | 安全 F-002 |
| 5 | 🟠 | XSS 注入 | `author.html:72`、`tag.html:60` | `alt="${title}"` **未转义**（同文件文本节点却用了 `escapeHtml`，属 L5 漏改） | 改 `alt="${escapeAttr(title)}"`；测试补断言 | 评审 N1 |
| 6 | 🟡 | SSRF | `main.py:724-745`、`pixiv.py:163-188` | 头像抓取对 DB 中的 URL 直接请求（源自 Pixiv API，可被 MITM 篡改）→ 受限 SSRF（探测内网） | 加 host 白名单（`*.pximg.net` + 配置镜像）+ 仅 https + 拒私网 / 环回 | 安全 F-004 |
| 7 | 🟡 | 信息暴露 | `main.py:1100` | `/api/settings` 回显 token 前 8 字符 | 只回 `has_token` 布尔（或 `token[:4]+"…"`） | 安全 F-005 |
| 8 | 🟡 | XSS（次要） | `index.html:1287`、`artwork.html:111` | `create_date` 未转义插入 `innerHTML`（MITM 可注入） | 非数值字段统一 `escapeHtml()` 或 `textContent` | 安全 F-009 |
| 9 | 🟡 | 安全配置 | `app/main.py` | 无安全响应头（CSP / nosniff / X-Frame-Options / Referrer-Policy） | 先加 `nosniff`/`DENY`/`no-referrer`；F-001 修完后逐步收紧 CSP | 安全 F-006 |
| 10 | 🟡 | 架构/可维护 | `app/main.py`（1218 行） | "上帝模块"：路由 + SQL 构造 + 业务编排（`_run_bookmark_check`）+ 通用工具混住；`_run_bookmark_check` 穿透访问 pixiv 私有成员 | 渐进拆分：先抽 `queries.py`，再抽 `routers/`（APIRouter），业务编排移 `services.py`；**注意保留 `bookmark-subs/check` 在 `delete {id}` 之前的相对顺序** | 评审 P1-5 |
| 11 | 🟡 | 重复代码 | `main.py:138-234` vs `:397-513` | `api_artworks`/`api_search` 近 80 行重复（where / count / 排序 / 组装），改一处必须改两处 | 抽 `_gallery_query(...)` 统一分页；两函数只做参数翻译 | 评审 P1-6 |
| 12 | 🟡 | 可观测性 | 全局；`README.md:185` | **全项目零 `logging`**；`jobs` 的 traceback 只存内存；README 承诺的 `pixiv_archive.log` 根本不会生成 | 配置 `logging`（RotatingFileHandler 写 `DATA_DIR/pixiv_archive.log`）+ 关键"吞异常"处记日志；**或**改 README 删该承诺（二选一） | 评审 P1-7 / N5 |
| 13 | 🟡 | 事务一致性 | `main.py:322-327`、`:363-367`、`scanner.py:196-200` | `os.remove` 发生在 `get_db()` 提交之前；commit 失败则"库回滚、文件已删" | 改为"先改库 → 提交 → 再删文件"或"待删清单"两阶段 | 评审 P1-9 |
| 14 | 🟡 | 并发 | `database.py`、`scanner.py:75`、`sync.py:56` | 长事务（整次扫描一个写事务）+ 30s timeout → 后台任务与 UI 写并发时偶发 `database is locked` → 500 | 扫描 / 同步分批提交；UI 写失败有限重试 + 可读提示 | 安全 F-010 |
| 15 | 🟡 | 卫生 | 仓库根 `.qa-tmp/`；`.gitignore` | `.qa-tmp/`（含 app 源码副本、acl jsonl、测试图）未被 `.gitignore` 排除，有误提交风险 | 删除 `.qa-tmp/` 并加入 `.gitignore` | 评审 N6 / 安全 F-012 |
| 16 | 🟡 | 测试可信度 | `test_http_smoke.py:21`、`test_pages.py:15` | 在线用例硬编码开发者真实库 PID（`148494017`/`author/1`），换机 / 空库必红或退化为"只测页面壳" | 仿 `test_http_smoke.py:52-57` 动态取真实 id，空库则 skip | 评审 N10 |
| 17 | 🟡 | 测试缺口 | `tests/` | `scanner.scan_directory`/`_prune_missing`、`sync_metadata` 各分支、`download` 游标、`jobs` 并发、`api_search`/`api_update_settings` 均无测试 | 优先补 `test_scanner.py`/`test_sync_branches.py`/`test_jobs.py` | 评审 P2-13 |
| 18 | 🟢 | 性能 | `database.py:116-125` | 排序白名单含 `create_date`/`first_seen` 但无索引 → 全表排序 | 加两个 `CREATE INDEX IF NOT EXISTS`（幂等） | 评审 P2-16 / 调查补注 A |
| 19 | 🟢 | 代码质量 | `main.py:340,380,578,609,645,750,1112`；`:29` vs `sync.py:53` | pydantic Model 在函数体内重复定义 7 次（每请求重建）；同步间隔默认 `800` 两处硬编码 | Model 上提模块级 / `schemas.py`；默认值移 `config.py` | 评审 P1-8 |
| 20 | 🟢 | 死代码 | `app/models.py` | 50 行 dataclass 零 import | 删除或真正用于 DB row→对象映射 | 评审 N7 / 调查 §7 |
| 21 | 🟢 | 注释漂移 | `main.py:75-91` | 注释称"自定义 ASGI 不触发 lifespan"（已过时，现用 hypercorn）；`on_event("startup")` 已弃用；`init_db()` 启动期执行两次 | 改用 `lifespan=` 或删 startup；更新注释 | 评审 N4 / 调查补注 B |
| 22 | 🟢 | 功能瑕疵 | `main.py:768` | `["explorer","/select,",path]` 把 `/select,` 与路径拆成两个参数，可能只打开文件夹不选中文件 | 改单参数 `f"/select,{path}"`（需 Windows 实机确认） | 评审 N8 |
| 23 | 🟢 | 供应链 | `requirements.txt`；`install.bat:38-39` | 依赖无 lock / 哈希；`--trusted-host` 关闭了 PyPI TLS 校验 | 提供 `requirements.lock`（哈希）+ `--require-hashes`；移除 `--trusted-host` | 安全 F-013 |
| 24 | 🟢 | 注入（防御性） | `tag.html:27` | Jinja 变量内联进 `<script>` 的 JS 字符串（当前因 script 不解析实体而安全，属依赖巧合） | 改用 `{{ tag_name \| tojson }}` | 安全 F-008 |
| 25 | 🟢 | 并发（低概率） | `run.py:92-111` | 单实例锁"判陈旧 → 删除 → 重建"存在 TOCTOU 窗口 | 接管时 `os.rename` 到临时名后再独占重建 | 安全 F-011 |

**验证为"良好"、无需过度修复的项** ✅：路径穿越（`/image` 走 `images.path` 精确白名单，覆盖编码变形 / 绝对路径 / 盘符）、SQL 注入（全参数化，动态片段仅来自代码内常量）、命令注入（`subprocess` list 形式无 shell）、反序列化（不读 `metadata/*.json`）、`.env` 写入（按键就地 + 原子替换 + 锁）、`sanitizeHtml` 白名单、单实例锁设计、`_json_body`+ValidationError、删除失败上报。

---

## 3. 后续修改计划（P0 / P1 / P2 / P3）

### P0 — 阻塞项，建议优先（3 条，约半天量级）

| 编号 | 行动 | 位置 | 预期收益 | 风险 |
|------|------|------|----------|------|
| **P0-1** | 重构前端动态渲染：9 处内联 onclick 改为 `data-*` + 事件委托（或 `createElement`+`addEventListener`+`textContent`），彻底离开字符串拼接 | `index.html` 9 处 | 消除存储型 XSS 一整类 | 中（前端无运行时测试，改完需人工点验或先补 node 单测） |
| **P0-2** | 加 Host/Origin 校验中间件（`Host ∈ {127.0.0.1:PORT, localhost:PORT}`），写接口校验 `Origin`；或统一要求自定义头 | `app/main.py` | 挡 DNS rebinding 与本机 CSRF | 低 |
| **P0-3** | `api_add_bookmark_sub` 改同步 `def`（或 `asyncio.to_thread`），不再阻塞事件循环 | `main.py:925-942` | 消除全局卡顿 | 极低（改 1 行） |

### P1 — 正确性与可维护性（5 条）

| 编号 | 行动 | 位置 |
|------|------|------|
| P1-4 | 补 `author.html:72`/`tag.html:60` 的 `alt` 转义；测试补断言 | 两个模板 |
| P1-5 | 拆分 `app/main.py`（先抽 `queries.py`，再抽 `routers/`，业务编排移 `services.py`） | `main.py` |
| P1-6 | 消除 `api_artworks`/`api_search` 重复（抽 `_gallery_query`） | `main.py` |
| P1-7 | 引入统一日志 + 修正 README（或删承诺）；DoH 查询恢复 SNI + 证书校验 | 全局 / `direct_connect.py` |
| P1-8 | `fetch_profile_image` 加 host 白名单；`/api/settings` 只回 `has_token`；`create_date` 等补转义 | `pixiv.py` / `main.py` / 模板 |

### P2 — 工程整洁（含"可直接做"的低风险项）

- 补 `scanner`/`sync`/`download` 的核心数据路径测试（**最高优先，见 §2 #17**）
- 在线测试去硬编码（动态取真实 id，空库 skip）
- 为 `create_date`/`first_seen` 补索引（§2 #18，低风险可直接做）
- Model 上提模块级 + 同步默认值收敛到 `config.py`
- 删除 `models.py` 死代码；清理 `.qa-tmp/` 并加入 `.gitignore`
- 前端：把 `index.html` 内联 JS 外移到 `static/js/*.js`（仍零构建），使 node 单测可加载——为 P0-1 提供回归网
- `explorer /select,` 单参数修正（需实机确认）

### P3 — 纵深防御与卫生

- 加安全响应头（nosniff / X-Frame-Options / Referrer-Policy），F-001 完成后逐步收紧 CSP
- 缩小 `getaddrinfo` monkeypatch 范围至 adapter 内部
- 扫描 / 同步分批提交，缓解写竞争
- 单实例锁接管原子化；`requirements.lock` + 哈希；移除 `--trusted-host`

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 前端 9 处内联 onclick 改事件委托 / `data-*`（修存储型 XSS） | 前端 | **P0** | 本迭代 |
| 2 | 加 Host/Origin 校验中间件（挡 rebinding / CSRF） | 后端 | **P0** | 本迭代 |
| 3 | `api_add_bookmark_sub` 改同步 `def` | 后端 | **P0** | 立即（1 行） |
| 4 | 补 `author`/`tag` 的 `alt` 转义 + 测试断言 | 前端 | P1 | 本迭代 |
| 5 | DoH 查询恢复 SNI + 证书校验；README 补说明 | 后端 | P1 | 本迭代 |
| 6 | `/api/settings` 只回 `has_token`；`create_date` 补转义；头像加 host 白名单 | 后端 | P1 | 本迭代 |
| 7 | 补 `scanner`/`sync`/`download` 测试 + 在线测试去硬编码 | QA | P1 | 下迭代 |
| 8 | 拆分 `main.py` + 消除 `api_*` 重复（**先织测试网再动刀**） | 后端 | P2 | 下迭代 |

---

## ⚠️ 待完善 / 已知局限

- **本环境无法实机验证**：沙箱禁止派生子进程（`python`/`node`/`git` 均被 SIGTERM），因此**未能起服务、跑 `tests/`、用浏览器复现 XSS、联网复现直连 / SSRF**；所有结论为**源码级确认**，安全发现已逐条标注置信度（F-001 为 9/10，F-011 竞态为 5/10）。
- **前端运行时零覆盖**：`index.html` 约 1750 行内联 JS 无任何运行时测试（历史报告已记录 `bsk` CLI 缺失导致浏览器工具不可用），P0-1 改造后**必须人工在浏览器点验**或先补 node 单测。
- **F-011 锁接管竞态**未运行时复现，仅静态推导，属低概率。
- **历史 `CODE_REVIEW.md` 可信**：22 条修复经独立复核 21 条真实落地，L3 误报判定正确；但存在"修得不够宽"（L5 只改 `artwork.html`，漏了 `author`/`tag` 的 `alt`），且**测试断言范围与修复范围一起漏了同一处**。
- 本报告为**只读评审**，未修改任何代码。

---

## 📚 成员产出索引

| 成员 | 原始产出 | 关键交付 |
|------|----------|----------|
| 🔧 gstack-investigator（调查员） | 结构分析报告（Task #1）+ 2 条补注 | 模块清单、无环依赖图、6 条执行链路、8 表 schema、前端结构 |
| 🔍 gstack-product-reviewer（产品评审员） | 架构与代码质量评审（Task #2）+ P2-16 补充 | 架构评价、10 条新问题（N1–N10）、与历史报告逐条对照、P0/P1/P2 计划 |
| 🛡️ gstack-security-officer（安全官） | 安全审计报告（Task #3） | STRIDE 威胁模型、OWASP Top 10 映射、14 项发现（F-001–F-014）、安全评分 C |

---

> 本报告由软件工坊 AI 协作生成（主理人汇编三位专家产出），关键决策请由工程负责人复核。安全与架构结论均为源码级确认，落地上线前建议补一次实机运行时验证。
