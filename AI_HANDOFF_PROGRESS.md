# 跨平台真实笔记监测面板｜下一位 AI 交接说明

> 更新时间：2026-09-07 09:10（中国标准时间）  
> 项目目录：`social-feedback-handoff-20260905`  
> 当前运行地址：`http://127.0.0.1:5060/`  
> 内部 scraper：`http://127.0.0.1:8007/`（仅供面板内部调用）

## 1. 项目目标

这是一个本地单用户的跨平台社交媒体笔记监测面板，使用真实平台公开页面和真实可见 Chrome 持久会话获取数据，集中完成：

- 真实笔记链接导入；
- 平台关键词搜索并导入公开内容；
- 从账号主页发现并导入作品；
- 笔记归档、收藏、刷新和删除；
- 浏览量、点赞、收藏、评论等公开指标展示；
- 公开评论和指标历史展示；
- 按日期、平台、账号和来源进行组织与筛选。

正式支持的平台是：小红书、抖音、快手、B站。

## 2. 本轮已完成

### 2.1 【账号信息】视图

面板左侧“视图与日期”区域已新增【账号信息】视图和计数。

账号主页导入成功的笔记会按以下层级展示：

```text
账号信息
└── 社交媒体平台
    └── 具体账号
        └── 笔记列表
```

实现规则：

1. 只有 `import_source === "account_import"` 的记录进入账号信息视图。
2. 普通链接导入和关键词搜索不会因为作者名相同而被误归类。
3. 账号优先使用账号主页导入时记录的账号名。
4. 具体账号优先使用去除查询参数后的账号主页 URL 进行归组。
5. 没有主页 URL 时才按账号名归组。
6. 账号主页导入按发布时间从新到旧处理。
7. 导入成功后前端自动切换到账号信息视图。
8. 账号主页 URL 展示和“查看主页”链接都会去除 query 参数与 hash。
9. 日期筛选、搜索、收藏状态、详情展示继续对账号信息视图生效。

相关位置：

- `panel/social-feedback-panel.html`
  - 视图按钮：约第 480 行；
  - 记录归一化和来源识别：约第 573 行；
  - 日期/视图筛选：约第 655 行；
  - 平台→账号→笔记分组：约第 731 行；
  - 账号主页 URL 前端清理：`stableProfileUrl()`。
- `panel/social_feedback_backend.py`
  - 账号主页导入：`import_account_results()`；
  - 归档摘要：`archive_summary()`；
  - 主页 URL 后端清理：`stable_account_profile_url()`。

### 2.2 登录资料持久化与误判修复

正式 scraper 使用电脑级固定 Chrome profile（位于当前登录用户的 `~/.minimax/social-feedback/browser` 目录下，具体绝对路径因机器而异，不在文档中固定）：

当前约束：

- 使用可见 Google Chrome，不使用 headless；
- 服务重启、任务结束、普通抓取失败时不得删除或重建 profile；
- 不复制或输出 Cookie 值、密码、验证码、API Key；
- 不关闭普通用户 Chrome 页面，只管理 scraper 自己创建或恢复的标签页；
- 旧项目 profile 仍保留作回退，不主动删除：
  `scraper-backend/data/browser`；
- 面板只对外提供 `5060`，`8007` 仅为内部 scraper 服务；
- 平台访问保持串行和平台级随机 5–10 秒间隔。

最近一次运行态检查：

- 小红书：`healthy`，`xhs-real`，登录状态 true；
- 抖音：`healthy`，`douyin-real`，登录状态 true；
- 快手：`healthy`，`kuaishou-real`，登录状态 true；
- B站：`healthy`，`bilibili-real`；本轮未重新登录；
- Twitter / YouTube / TikTok：`needs_login`。

历史日志中出现过的 `CREDENTIAL_EXPIRED` 不应直接当作当前登录状态。应以带鉴权的 `/api/status` 返回为准，且不得在聊天或文档中写出 API Key。

### 2.3 抖音主页解析补充

抖音主页解析器已增加对以下页面自身数据源的只读扫描：

- 页面全局初始化对象；
- 常见路由/React Query 状态对象；
- `#root` 和 `document.body` 的 React props/fiber；
- 原有主页节点和 `/video/<id>` 链接仍保留；
- 继续使用 secUid 过滤，避免把其他账号作品混入。

相关文件：`scraper-backend/app/scrapers/douyin.py`。

## 3. 当前已验证结果

### 3.1 自动化测试

在项目 `.venv` 中运行：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：**26/26 通过**。

覆盖内容包括：

- 账号主页导入来源和账号名；
- 账号主页导入按发布时间倒序；
- B站账号主页归档字段；
- 四个平台统一 5–10 秒节流；
- 主页 URL query/hash 清理；
- 临时访问参数脱敏；
- 失败抓取不写入数据库；
- 正式模式旧写接口关闭；
- scraper API Key 中间件；
- 浏览器管理危险接口关闭；
- 小红书导航与 JavaScript 回归。

另已通过：

```text
Python py_compile：通过
前端内嵌 JavaScript node --check：通过
```

测试中出现的 Starlette/httpx 弃用 warning 不影响结果。

### 3.2 运行态

当前两个主服务已由 LaunchAgent 重启并处于运行状态：

- `com.mavis.social-feedback.scraper`
- `com.mavis.social-feedback.panel`

面板健康检查返回：

```json
{"ok":true,"service":"social-feedback-backend"}
```

面板已在 Safari 打开并加载：

```text
http://127.0.0.1:5060/
```

当前正式数据库统计：

- 已保存笔记：27 条；
- 账号主页导入：11 条；
- 当前已有账号主页归档记录来自小红书；
- 账号视图 HTML 和运行响应已确认包含按钮、计数、来源筛选、分组及“查看主页”逻辑。

## 4. 尚未闭环的问题

### 抖音真实账号主页暂无可解析作品

使用本轮用户提供的真实抖音主页执行账号主页导入时，结果为：

```text
未从该账号主页发现可导入的真实作品
```

已确认：

- 抖音登录状态是 true；
- 页面 `ready_state=complete`；
- 当前页面没有登录提示、验证码或不可访问标记；
- 解析器已增加全局状态和根容器扫描；
- 系统没有伪造作品，也没有把该结果错误显示成登录失效。

最新诊断显示该页面有极少量视频节点，但仍没有得到可归属到目标账号的有效作品对象。下一位 AI 如继续处理，应优先在真实可见 Chrome 页面中确认：

1. 主页是否实际展示作品列表，还是空主页/受限页面；
2. 视频节点是否属于推荐内容而不是主页作品；
3. 页面是否使用新的接口数据结构或延迟加载容器；
4. 是否需要增加新的、明确按目标账号过滤的 DOM/状态解析；
5. 不能通过伪造数据、复制 Cookie、注入 Cookie 或放宽到不可信的通用网页抓取来“修复”。

如果页面确实没有公开作品，应继续返回“未发现作品”。

## 5. 关键文件

```text
panel/social-feedback-panel.html       前端单文件面板
panel/social_feedback_backend.py       Flask 面板后端、SQLite 和导入边界
panel/wsgi.py                           Waitress 入口
scraper-backend/app/main.py             FastAPI scraper 入口
scraper-backend/app/browser/pool.py     持久 Chrome 浏览器池
scraper-backend/app/scrapers/douyin.py  抖音真实抓取与主页解析
scraper-backend/app/scrapers/xhs.py     小红书真实抓取
scraper-backend/app/scrapers/kuaishou.py 快手真实抓取
scraper-backend/app/scrapers/bilibili.py B站真实抓取
scripts/restart-services.sh             重启两个主服务
scripts/service-status.sh               查看健康状态
scripts/install-launchd.sh              安装 LaunchAgent
scripts/login-platform.sh               人工打开可见 Chrome 完成登录
scripts/run-real-probes.sh              只读真实数据探针
scripts/real_probe.py                   探针实现
tests/test_formal_mode.py               正式模式回归测试
PROJECT_HANDOFF.md                      正式运行和安全边界交接文档
```

## 6. 新环境启动方式

要求 Python 3.10+，推荐 Python 3.11：

```bash
cd social-feedback-handoff-20260905
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
./scripts/install-launchd.sh
./scripts/service-status.sh
```

常用命令：

```bash
./scripts/restart-services.sh
./scripts/service-status.sh
.venv/bin/python -m unittest discover -s tests -v
```

首次安装时，`scripts/ensure-runtime-env.sh` 会在 `.runtime/formal.env` 生成本机随机运行配置。该文件不在交付包中，不应手动把 API Key 写入代码、URL、日志或 Markdown。

## 7. 交付包排除项

为了不泄露登录资料和运行态数据，项目压缩包不包含：

```text
.runtime/
.venv/
.venv311/
.formal-browser-profile/
.formal-browser-profile-run-sh/
scraper-backend/data/
scraper-backend/data/
panel/social_feedback.db*
*.log
*.pid
__pycache__/
```

其中 `scraper-backend/data/browser` 和电脑级固定 profile 都包含浏览器登录资料，绝不能随项目包传播。用户原电脑上的固定 profile 不会因打包而被删除或重建。

## 8. 下一位 AI 的工作规则

1. 先阅读本文件和 `PROJECT_HANDOFF.md`，再改代码。
2. 先确认实际项目路径，不要在旧 session 副本或错误路径上修改。
3. 任何 config、settings、env、browser profile、数据库相关操作，先确认真实生效路径。
4. 不读取、保存或输出密码、验证码、Cookie 值、API Key 或临时访问参数。
5. 不删除或重建现有 Chrome 登录资料，除非用户明确同意。
6. 不把示例 URL、mock 数据或猜测的作品当作真实抓取成功。
7. 账号信息视图必须继续基于真实 `account_import` 来源，不能仅凭作者名判断。
8. 保持平台串行访问和 5–10 秒随机间隔。
9. 修改后至少运行 26 项正式回归测试、Python 编译检查和前端 JavaScript 语法检查。
10. 对抖音主页问题，应先获得真实页面证据，再做最小解析器改动。

## 9. 当前结论

项目的主要功能、账号信息视图、登录资料持久化和正式安全边界已经完成并通过回归。当前唯一已知业务限制是：指定的真实抖音账号主页目前没有返回可归属的公开作品节点，因此不能声称该主页已成功导入作品。
