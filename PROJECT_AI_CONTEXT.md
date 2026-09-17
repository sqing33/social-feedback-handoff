# 跨平台真实社交媒体笔记监测项目

## 给后续 AI 的项目上下文与交接说明

> 本文件用于帮助第二个 AI 快速理解项目目标、代码结构、运行边界、已完成工作和当前阻塞点。  
> 本文件不包含密码、验证码、Cookie、API Key、Token、浏览器认证资料或带临时访问参数的真实平台链接。

---

## 1. 项目概览

项目名称：`social-feedback-handoff-20260905`

项目类型：本机单用户、正式模式的跨平台真实社交媒体笔记监测系统。

主要覆盖平台：

- 小红书（XHS）
- 抖音（Douyin）
- 快手（Kuaishou）
- B 站（Bilibili）

核心能力：

1. 使用可见 Google Chrome 访问真实平台页面。
2. 通过 Playwright 持久化浏览器 Profile 保留用户本人完成的登录状态。
3. 获取真实作品详情、评论、搜索结果以及账号主页作品。
4. 通过本地面板查看和管理真实导入结果。
5. 将成功抓取的真实内容写入 SQLite 数据库。
6. 对平台验证、登录失效、空页面和详情不可访问状态返回真实错误。
7. 正式模式禁止用 mock、示例、截图或猜测内容伪造记录。

项目不是公网 SaaS，也不是绕过平台风控的工具。默认只监听本机 `127.0.0.1`，登录和安全验证必须由用户本人在可见 Chrome 窗口中完成。

---

## 2. 关于刚才的两个 ZIP 文件

### 2.1 截图中的文件

截图显示的是：

```text
xhs-safe-bridge-poc.zip
```

这是早期生成的“小红书安全 Bridge PoC 源码归档”，原位置在项目的：

```text
.runtime/xhs-safe-bridge-poc.zip
```

### 2.2 刚才交付的完整项目包

刚才交付的完整项目包是本机下载目录下的 `social-feedback-handoff-20260905.zip`（绝对路径仅在本地记录，不随仓库公开）。

结论：

- 截图中的 `xhs-safe-bridge-poc.zip` **没有作为一个嵌套 ZIP 文件包含在完整项目包中**。
- 原因是完整项目包按照安全规则排除了整个 `.runtime/` 目录以及所有 `*.zip` 文件。
- 但是，截图 ZIP 中的核心源代码已经包含在完整项目包中，主要对应：

```text
scraper-backend/app/xhs_bridge/
scraper-backend/xhs-safe-extension/
```

其中包括 Bridge 的协议、客户端、服务端、运行时管理，以及安全精简版 Chrome 扩展。

### 2.3 完整项目包的内容

完整项目包已验证：

- 包含 91 个项目源码、脚本、测试和安全扩展文件。
- 压缩包大小约 224.2 KiB。
- ZIP 完整性测试通过。
- 不包含数据库、日志、浏览器 Profile、运行时密钥、PID 文件、虚拟环境或 `.runtime/` 数据。

---

## 3. 目录结构

```text
social-feedback-handoff-20260905/
├── PROJECT_HANDOFF.md
├── PROJECT_AI_CONTEXT.md
├── AI_HANDOFF_PROGRESS.md
├── requirements.txt
├── requirements.lock.txt
├── start-panel.sh
├── start-scraper.sh
├── panel/
│   ├── social-feedback-panel.html
│   ├── social_feedback_backend.py
│   └── wsgi.py
├── scraper-backend/
│   ├── README.md
│   ├── BROWSER_USAGE.md
│   ├── pyproject.toml
│   ├── run.sh
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── middleware.py
│   │   ├── models.py
│   │   ├── platforms.py
│   │   ├── errors.py
│   │   ├── browser/
│   │   ├── routers/
│   │   ├── scrapers/
│   │   ├── store/
│   │   ├── sign/
│   │   ├── xhs_bridge/
│   │   └── xhs_skills/
│   ├── scripts/
│   └── xhs-safe-extension/
├── scripts/
│   ├── ensure-runtime-env.sh
│   ├── install-launchd.sh
│   ├── uninstall-launchd.sh
│   ├── restart-services.sh
│   ├── service-status.sh
│   ├── watchdog.sh
│   ├── backup_db.py
│   ├── login-platform.sh
│   ├── real_probe.py
│   └── run-real-probes.sh
└── tests/
    └── test_formal_mode.py
```

以下内容属于本机运行态，不应从另一台机器复制，也不在交付 ZIP 中：

```text
.runtime/
.venv/
浏览器持久化 Profile
panel/social_feedback.db
日志和备份
scraper-backend/data/
```

---

## 4. 系统架构

```text
用户本人
  │
  │ 在可见 Google Chrome 中完成登录、扫码和安全验证
  ▼
持久化 Chrome Profile
  │
  │ Playwright，headless=false
  ▼
FastAPI Scraper：127.0.0.1:8007
  │
  ├── 平台状态查询
  ├── 真实作品详情抓取
  ├── 真实评论抓取
  ├── 真实搜索
  ├── 账号主页作品导入
  ├── 可选的小红书 Safe Bridge
  └── API Key 中间件
  │
  ▼
Flask + Waitress 面板：127.0.0.1:5060
  │
  ├── 前端面板
  ├── 账号信息视图
  ├── 真实导入结果
  ├── 失败提示
  └── SQLite 数据库
```

临时调试面板曾使用 `5090` 或 `5061`，正式长期运行以交接文档中的 `5060` 为准。Scraper 内部端口是 `8007`，不应暴露到公网或局域网。

小红书 Safe Bridge 使用本机 loopback：

```text
127.0.0.1:9333
```

Bridge 仅在 `XHS_BRIDGE_ENABLED=1` 时启用；未连接或调用失败时，scraper 会回退到 Playwright 主路径，不应因此拖垮其他平台抓取。

---

## 5. 关键文件说明

### 5.1 面板

```text
panel/social_feedback_backend.py
```

职责：

- Flask 面板后端。
- 调用本机 scraper API。
- 维护 SQLite 数据库。
- 负责真实抓取结果入库边界。
- 提供账号信息、归档、反馈和同步状态相关接口。

```text
panel/social-feedback-panel.html
```

职责：

- 面板前端页面。
- 展示平台状态、账号信息、真实导入结果和错误信息。
- 不应把 Cookie、Token、API Key 或临时访问参数展示给用户。

```text
panel/wsgi.py
```

职责：

- Waitress/WSGI 启动入口。

### 5.2 Scraper 主服务

```text
scraper-backend/app/main.py
```

职责：

- FastAPI 应用入口。
- 注册正式模式路由。
- 初始化浏览器池和可选 Bridge。
- 管理应用生命周期。

```text
scraper-backend/app/middleware.py
```

职责：

- 校验 `X-API-Key` 请求头。
- 生成和传递 Request ID。
- 正式模式拒绝通过 URL query 参数传递密钥。

```text
scraper-backend/app/browser/pool.py
```

职责：

- 管理可见 Google Chrome。
- 使用持久化 Profile。
- 默认 `headless=False`。
- 保持各平台访问串行。
- 控制单步浏览器操作时长。
- 在启用 Safe Bridge 时加载运行时扩展。

```text
scraper-backend/app/scrapers/xhs.py
```

职责：

- 小红书真实详情、主页、搜索和评论链路。
- 识别登录状态、验证页、`/404`、不可访问页。
- 在无法获得真实详情时返回结构化错误，不伪造记录。
- 详情打开顺序大致为：Playwright 原生卡片点击、Safe Bridge 受控点击回退、页面内已有脚本回退、必要时直接详情导航。

```text
scraper-backend/app/scrapers/douyin.py
scraper-backend/app/scrapers/kuaishou.py
scraper-backend/app/scrapers/bilibili.py
```

职责：

- 对应平台的真实数据读取。
- 不应在正式模式中回退到 mock 数据。

### 5.3 小红书 Safe Bridge

```text
scraper-backend/app/xhs_bridge/protocol.py
```

定义 Bridge 的固定命令、请求和响应结构、参数校验以及返回字段边界。

```text
scraper-backend/app/xhs_bridge/client.py
```

scraper 侧客户端。连接本机 Bridge 服务，发送固定白名单命令，不暴露配对值。

```text
scraper-backend/app/xhs_bridge/server.py
```

本机 loopback Bridge 服务。负责握手、认证、命令分发和返回数据过滤。

```text
scraper-backend/app/xhs_bridge/runtime.py
```

负责扩展副本生成、Bridge 生命周期状态和与 scraper 的集成。

```text
scraper-backend/xhs-safe-extension/manifest.json
scraper-backend/xhs-safe-extension/background.js
```

安全精简版 Chrome 扩展，仅允许：

```text
scripting
tabs
```

host 权限仅覆盖：

```text
https://xiaohongshu.com/*
https://www.xiaohongshu.com/*
```

固定允许的 Bridge 命令：

```text
ping_server
get_runtime_status
get_xhs_page_state
click_xhs_note_card
```

明确禁止接入：

- `chrome.cookies`
- `webRequest`
- `debugger`
- 任意页面级 evaluate 接口
- 请求体读取
- 响应体读取
- `set-cookie` 采集
- Cookie 注入
- Token 读取或自动刷新
- 登录自动化
- 文件上传
- 自动验证码处理
- 高频 Token 重试循环

Safe Bridge 的定位是“受控读取页面状态和受控点击当前页面上真实存在的作品卡片”，不是风控绕过器。

### 5.4 测试与运维

```text
tests/test_formal_mode.py
```

覆盖正式模式安全边界、路由、账号主页导入来源、平台访问节流、URL 参数清理、小红书导航回归等行为。

```text
scripts/ensure-runtime-env.sh
```

首次运行时创建本机正式运行配置。该配置包含密钥，权限应为 `0600`，不应复制或打包。

```text
scripts/install-launchd.sh
scripts/uninstall-launchd.sh
scripts/restart-services.sh
scripts/service-status.sh
scripts/watchdog.sh
```

负责 macOS LaunchAgent 安装、卸载、重启、状态查询和故障恢复。

```text
scripts/login-platform.sh
```

打开指定平台的可见 Chrome 登录流程。密码、验证码和扫码动作必须由用户本人完成。

```text
scripts/real_probe.py
scripts/run-real-probes.sh
```

只读真实数据探针。探针不写面板数据库，用于验证标题、发布时间、指标、评论、真实 backend 和禁止 degraded/mock。

---

## 6. 正式模式安全配置

正式模式要求保持以下语义：

```text
FORMAL_MODE=1
ENABLE_LEGACY_API=0
ENABLE_BROWSER_ADMIN=0
ALLOW_LEGACY_WRITE_API=0
ALLOW_GENERIC_PUBLIC_CAPTURE=0
```

这些值可以在本机运行配置中存在，但不要把实际 API Key、配对值或完整 `.runtime/formal.env` 内容写入聊天、文档或新的压缩包。

正式模式下：

- scraper API Key 只通过 `X-API-Key` 请求头传递。
- 不接受 URL query 参数中的 API Key。
- 旧 auth、business、proxy、reload 和兼容 HTTP bridge 路由默认不注册。
- 危险浏览器管理接口默认返回 `403`。
- 面板旧直写接口被阻止，典型返回 `410`：
  - `POST /api/archives`
  - `POST /api/ingest`
  - `PUT /api/archives/<archive_id>/feedback`
- 两个服务只监听 loopback。
- 不应为了“方便调试”打开公网监听或关闭正式模式。

---

## 7. 数据真实性规则

这是本项目最重要的业务约束，后续 AI 修改代码时必须保持：

1. 只有 scraper 成功取得真实标题的内容才允许入库。
2. 抓取失败必须返回结构化错误，禁止改写为成功。
3. 禁止用 mock、示例数据、截图 OCR 猜测或作者名拼接生成笔记。
4. 缺失指标保存为 `NULL`，前端显示 `—`，不得用 `0` 代替。
5. 缺失发布时间时进入“日期待识别”，不得用抓取时间冒充发布时间。
6. 重复成功导入更新同一 archive，不重复建档。
7. 失败刷新不得覆盖旧数据、收藏状态或指标历史。
8. 评论相对时间保留原始文本，不伪造 Unix 时间戳。
9. 小红书公开浏览量缺失时保持 `NULL`。
10. 快手公开收藏量缺失时保持 `NULL`。
11. 账号信息视图必须依据真实导入来源：

```text
import_source === "account_import"
```

12. 四个平台访问保持串行，并使用约 5–10 秒随机间隔。
13. 单步浏览器操作不得超过 30 秒；出现验证或空页时停止高频重试。
14. 测试必须使用临时数据库和隔离浏览器 Profile，不污染正式数据。

---

## 8. 当前已经完成的工作

已完成：

- 正式模式面板和 scraper 的基本架构。
- 四个平台真实数据链路。
- 可见 Google Chrome + 持久化 Profile。
- 账号主页导入和账号信息视图。
- 小红书安全 Bridge 模块。
- 安全精简版 XHS Chrome 扩展。
- Bridge 与 scraper 的可选集成。
- Bridge 未连接时的 Playwright 回退路径。
- 正式模式安全边界测试。
- 扩展启动参数修复：启用扩展时移除 Playwright 默认的 `--disable-extensions` 影响。
- Bridge 端口 `9333` 冲突时不拖垮 scraper 的回退行为。
- Bridge 实际握手验证。
- 源码语法和 JSON/Manifest 校验。
- 测试套件通过。
- 完整源码归档生成并通过 ZIP 完整性测试。

最近一次已知测试结果：

```text
Ran 34 tests
OK
```

已验证的 Bridge 状态：

```text
extension_connected: true
generation: 1
```

---

## 9. 当前小红书问题与真实状态

当前小红书问题不是“扩展没有安装”，也不是“Bridge 没有连接”。已知状态：

```text
xhs_logged_in: true
xhs_safe_bridge.enabled: true
xhs_safe_bridge.extension_connected: true
```

主页作品发现阶段曾经成功，但逐条打开作品详情时，小红书页面级风控返回交互验证和不可访问状态：

```text
path: /404
verification: true
inaccessible: true
note_detail_count: 0
```

因此 5 条作品被真实记录为失败：

```text
RATE_LIMITED: xhs temporarily requires interactive verification; retry after completing it in the persistent browser
```

正确解释：

```text
主页作品可以发现
→ 进入详情时触发小红书交互验证
→ 页面进入 /404 或不可访问状态
→ scraper 返回 RATE_LIMITED
→ 面板显示失败，不把卡片标题或封面冒充完整详情
```

Safe Bridge 不会也不应该：

- 伪造 `navigator.webdriver`。
- 伪造页面可见性。
- 修改或注入 Cookie。
- 读取、刷新或重放 Token。
- 拦截请求头、请求体或响应体。
- 自动提交验证码。
- 高频重复访问详情页。
- 把 `RATE_LIMITED` 改成成功。

后续若要继续验证，正确流程是：

1. 用户本人在可见、持久化的 Chrome 中完成小红书页面要求的交互验证。
2. 等待页面回到正常可访问状态。
3. 只测试一条当前页面上真实存在的作品。
4. 检查是否能取得真实正文和媒体，而不是只看到主页卡片。
5. 如果仍然进入 `/404`，保留真实失败结果，不做绕过。

不要在聊天或文档中复制包含临时访问参数的完整小红书链接。

---

## 10. 安装和启动

要求：

- macOS。
- Python 3.10+，推荐 Python 3.11。
- 已安装 Google Chrome。
- Playwright 浏览器依赖按本机环境安装。

从项目根目录执行：

```bash
cd social-feedback-handoff-20260905
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

前台启动 scraper：

```bash
./start-scraper.sh
```

前台启动面板：

```bash
./start-panel.sh
```

长期运行：

```bash
./scripts/install-launchd.sh
./scripts/service-status.sh
```

重启服务：

```bash
./scripts/restart-services.sh
```

卸载 LaunchAgent：

```bash
./scripts/uninstall-launchd.sh
```

正式面板：

```text
http://127.0.0.1:5060/
```

内部 scraper：

```text
http://127.0.0.1:8007/
```

不要把这些服务改成 `0.0.0.0`，除非用户明确要求并同时重新设计认证、TLS、反向代理和网络边界。

---

## 11. 测试与验证

运行正式模式测试：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test*.py'
```

Python 语法检查：

```bash
.venv/bin/python -m py_compile \
  scraper-backend/app/xhs_bridge/*.py \
  scraper-backend/app/browser/pool.py \
  scraper-backend/app/main.py \
  scraper-backend/app/scrapers/xhs.py \
  tests/test_formal_mode.py
```

扩展 JavaScript 检查：

```bash
node --check scraper-backend/xhs-safe-extension/background.js
```

Manifest 检查：

```bash
.venv/bin/python -m json.tool \
  scraper-backend/xhs-safe-extension/manifest.json
```

真实数据探针使用环境变量传入真实链接，环境变量只在本机使用，不要把真实链接、临时参数、API Key 或浏览器认证信息写进文档：

```bash
export PROBE_XHS_URL='当前有效的小红书分享链接'
export PROBE_DOUYIN_URL='当前有效的抖音作品链接'
export PROBE_KUAISHOU_URL='当前有效的快手作品链接'
./scripts/run-real-probes.sh
```

真实探针只读调用 scraper，不写面板数据库。

---

## 12. 后续 AI 的工作规则

第二个 AI 继续处理本项目时，必须遵守：

### 必须先做

1. 先阅读本文件和 `PROJECT_HANDOFF.md`。
2. 先检查项目当前文件状态，不要假定旧日志仍代表当前运行状态。
3. 先确认当前服务、端口、测试结果和数据库状态，再做修改。
4. 优先使用项目已有模式和接口，不要重写整个抓取链路。
5. 修改后运行针对性的测试和语法检查。

### 禁止做

1. 不读取、复制、输出或传输密码、验证码、Cookie、Token、API Key、临时访问参数或浏览器认证资料。
2. 不删除、清空或重建用户现有 Chrome 登录 Profile。
3. 不把原始旧扩展直接接入正式环境。
4. 不启用 `chrome.cookies`、`webRequest`、`debugger` 或任意请求/响应拦截能力。
5. 不使用 headless 浏览器替代正式可见 Chrome。
6. 不绕过平台验证，不修改反检测字段，不重放请求，不自动处理验证码。
7. 不把失败改写成成功，不使用 mock 或占位数据填充真实业务表。
8. 不为了调试把密钥写入日志或返回给前端。
9. 不进行高频自动重试。
10. 不把带临时参数的真实平台 URL 写入 Markdown、代码提交或回复。

### 修改后必须报告

- 改了哪些文件。
- 为什么要改。
- 是否改变正式模式安全边界。
- 跑了哪些测试。
- 哪些测试通过、失败或未执行。
- 当前仍有什么真实阻塞。

---

## 13. 交付文件与阅读顺序

建议第二个 AI 按以下顺序阅读：

1. `PROJECT_AI_CONTEXT.md`：本文件，快速获得全局上下文。
2. `PROJECT_HANDOFF.md`：较完整的部署、验收和运维交接说明。
3. `scraper-backend/README.md`：scraper 服务使用说明。
4. `scraper-backend/BROWSER_USAGE.md`：浏览器使用边界。
5. `scraper-backend/app/main.py`：正式路由和生命周期。
6. `scraper-backend/app/browser/pool.py`：浏览器池和扩展加载。
7. `scraper-backend/app/scrapers/xhs.py`：当前小红书链路。
8. `scraper-backend/app/xhs_bridge/`：安全 Bridge 实现。
9. `scraper-backend/xhs-safe-extension/`：安全精简版扩展。
10. `tests/test_formal_mode.py`：正式模式回归测试。

刚才交付的完整源码包：

```text
social-feedback-handoff-20260905.zip
```

本文件保存位置：

```text
social-feedback-handoff-20260905/PROJECT_AI_CONTEXT.md
```

如果需要把本文件也放进新的 ZIP，应重新打包项目；不要把 `.runtime/`、数据库、日志、浏览器 Profile 或正式运行配置加入归档。
