# 跨平台真实笔记监测面板：正式本地版交接

> 验收日期：2026-09-05  
> 部署范围：当前 Mac、本机单用户、仅监听 `127.0.0.1`  
> 核心原则：正式界面和正式数据库只保存真实平台抓取结果；禁止 mock、示例数据、生成评论和占位记录。

## 1. 当前结论

本地正式化工作已完成，当前正式服务使用电脑级持久 Chrome profile，并已完成以下运行态核验：

1. 小红书、抖音、快手在固定 profile 中均保持登录，服务以可见 Google Chrome 串行访问真实平台。
2. 面板已增加【账号信息】视图；账号主页导入的成功笔记按“平台 → 具体账号 → 笔记”归类，并按发布时间从新到旧处理。
3. 账号主页 URL 归档时去除查询参数；普通链接导入和关键词搜索不会仅凭作者名进入账号信息视图。
4. 面板和 scraper 均由 LaunchAgent 管理，面板只向用户暴露 `5060`，`8007` 仅作为内部服务。

抖音某个真实主页曾完成登录校验，但当前页面未发现可解析作品；系统如实返回“未发现作品”，不会伪造导入成功，也不会误报为登录失效。

## 2. 架构与端口

```text
Safari / 本机浏览器
  ↓
Waitress + Flask 面板  http://127.0.0.1:5060
  ├── 前端页面
  ├── 业务 API
  └── SQLite: panel/social_feedback.db
          ↓ X-API-Key 请求头
Uvicorn + FastAPI scraper  http://127.0.0.1:8007
  ├── 平台状态
  ├── 真实详情、评论、搜索
  ├── 只读真实数据探针
  └── Playwright 持久 Chrome profile
          ↓
真实平台公开页面 / 页面状态 / DOM / GraphQL
```

两个服务只绑定 loopback，不对局域网或公网开放。未来如需远程访问，必须另行增加反向代理、TLS、用户级认证和网络访问策略。

## 3. 正式安全默认值

首次启动或安装 launchd 时，`scripts/ensure-runtime-env.sh` 会自动生成：

```text
.runtime/formal.env
```

该文件权限为 `0600`，目录权限为 `0700`，包含本机随机 scraper API Key 和正式模式开关。它被 `.gitignore` 排除，不得打包、提交或展示内容。

正式默认值：

- `FORMAL_MODE=1`
- `ENABLE_LEGACY_API=0`
- `ENABLE_BROWSER_ADMIN=0`
- `ALLOW_LEGACY_WRITE_API=0`
- `ALLOW_GENERIC_PUBLIC_CAPTURE=0`
- scraper API Key 仅通过 `X-API-Key` 请求头传递，不接受 URL query 参数。

正式 scraper 不注册旧 auth、business、proxy、reload 和 HTTP bridge 兼容路由。危险浏览器管理接口默认返回 403；只保留状态和人工登录所需接口。

## 4. 数据真实性规则

1. 只有 scraper 成功返回真实标题的内容才允许入库。
2. 抓取失败必须返回错误，禁止降级为 mock 或普通网页猜测。
3. 缺失指标保存为 `NULL`，前端显示 `—`，不得用 `0` 代替。
4. 发布时间缺失时进入“日期待识别”，不得以抓取时间冒充。
5. 重复成功导入更新同一 archive，不重复建档。
6. 失败刷新不得覆盖旧数据、收藏状态或指标历史。
7. 评论相对时间保存在文本字段，不伪造 Unix 时间戳。
8. 小红书公开浏览量缺失时保持 `NULL`；快手公开收藏量缺失时保持 `NULL`。
9. 测试必须使用临时数据库和隔离 profile，不污染正式数据库。
10. 浏览器单步操作不得超过 30 秒，不盲目高频重试。

面板旧直写接口在正式模式下被阻止：

- `POST /api/archives` → 410
- `POST /api/ingest` → 410
- `PUT /api/archives/<archive_id>/feedback` → 410

## 5. 目录结构

```text
social-feedback-handoff-20260905/
├── PROJECT_HANDOFF.md
├── requirements.txt
├── requirements.lock.txt
├── start-panel.sh
├── start-scraper.sh
├── panel/
│   ├── social-feedback-panel.html
│   ├── social_feedback_backend.py
│   └── wsgi.py
├── scraper-backend/
│   ├── app/
│   ├── README.md
│   └── BROWSER_USAGE.md
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
└── tests/test_formal_mode.py
```

以下运行数据不会进入交付包：`.runtime/`、数据库、浏览器 profile、虚拟环境、日志、备份、缓存和秘密配置。

## 6. 安装与长期运行

要求 Python 3.10+，推荐 Python 3.11。

```bash
cd social-feedback-handoff-20260905
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
./scripts/install-launchd.sh
```

`install-launchd.sh` 会：

- 自动生成本机秘密运行配置（若不存在）；
- 安装并加载面板、scraper、watchdog、每日备份四个当前用户 LaunchAgent；
- 创建仅本机可读的日志和备份目录。

常用运维命令：

```bash
./scripts/service-status.sh
./scripts/restart-services.sh
./scripts/uninstall-launchd.sh
```

面板地址：

```text
http://127.0.0.1:5060/
```

前台临时运行也可以直接执行：

```bash
./start-scraper.sh
./start-panel.sh
```

## 7. launchd 与备份

安装的 LaunchAgent：

- `com.mavis.social-feedback.scraper`：scraper 主服务，异常退出自动恢复。
- `com.mavis.social-feedback.panel`：面板主服务，异常退出自动恢复。
- `com.mavis.social-feedback.watchdog`：每 60 秒检查两个服务，失联时 kickstart。
- `com.mavis.social-feedback.backup`：每日 03:15 使用 SQLite backup API 做一致性备份。

备份目录：`.runtime/backups/`，默认保留最近 14 份。计划型 Agent 执行完成后显示 `not running` 且 `last exit code = 0` 属正常状态。

## 8. 自动化测试与本机验收

运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

2026-09-07 最终结果：`.venv/bin/python -m unittest discover -s tests -v` 共 `26/26` 通过。包含账号主页导入来源、发布时间倒序、四平台请求节流、主页 URL 参数清理、正式模式安全边界和小红书导航回归。唯一提示是 Starlette 关于 `httpx` TestClient 的弃用 warning，不影响测试结果。

本机 live 验收结果：

- scraper 不带 API Key：HTTP 401，结构化错误。
- scraper 带正确 API Key：HTTP 200，版本 `1.0.0`。
- 面板能够携带密钥调用 scraper；小红书未登录时返回明确 `CREDENTIAL_EXPIRED`，不是 401。
- 失败抓取前后六张业务表计数保持一致且全部为 0：`archives`、`feedback`、`assets`、`metric_history`、`accounts`、`sync_runs`。
- Waitress 面板与 Uvicorn scraper 均由 launchd 管理并处于运行状态。
- 面板异常终止已验证可由 KeepAlive 恢复；scraper 停止已验证可由 watchdog 恢复。
- SQLite 一致性备份已实际生成并验读。

## 9. 当前平台状态

当前电脑级固定 Chrome profile：`~/.minimax/social-feedback/browser`，正式 scraper 使用可见 Google Chrome（`headless=false`）。最近一次运行态检查：

- 小红书：`healthy`，登录标记为 true，`xhs-real`
- 抖音：`healthy`，登录标记为 true，`douyin-real`
- 快手：`healthy`，登录标记为 true，`kuaishou-real`
- B站：`healthy`，`bilibili-real`；本轮未要求重新登录
- Twitter / YouTube / TikTok：`needs_login`

历史日志中的 `CREDENTIAL_EXPIRED` 不代表当前固定 profile 的登录状态；面板会以 scraper 当前状态为准。账号主页没有解析出作品时，系统会返回真实“未发现作品”，不会改写成登录失效或生成占位笔记。

## 10. 后续需要用户参与的两步

### 10.1 登录三平台

服务运行后分别执行：

```bash
./scripts/login-platform.sh xhs
./scripts/login-platform.sh douyin
./scripts/login-platform.sh kuaishou
```

在项目打开的真实 Chrome 窗口中自行完成登录。不要在聊天、代码、文档或数据库中传输密码、验证码、Cookie 或 API Key。

### 10.2 运行只读真实数据探针

准备三条当前有效真实链接；小红书必须使用含当前有效 `xsec_token` 的完整分享链接。然后执行：

```bash
export PROBE_XHS_URL='完整小红书分享链接'
export PROBE_DOUYIN_URL='完整抖音作品链接'
export PROBE_KUAISHOU_URL='完整快手作品链接'
./scripts/run-real-probes.sh
```

探针只读调用 scraper，不写面板数据库，并检查真实标题、发布时间、指标、评论、`*-real` backend，以及禁止 degraded/mock。小红书公开浏览量和快手公开收藏量必须保持 `NULL`。

## 11. 关键文件

- `panel/social_feedback_backend.py`：面板业务 API、SQLite、真实抓取写库边界。
- `panel/wsgi.py`：Waitress WSGI 入口。
- `scraper-backend/app/main.py`：正式路由注册与中间件。
- `scraper-backend/app/middleware.py`：API Key 与 Request ID。
- `scripts/real_probe.py`：三平台只读探针。
- `tests/test_formal_mode.py`：正式模式回归测试。

维护时优先保持本文件列出的真实性、安全边界和本机单用户部署范围。
