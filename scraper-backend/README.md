# Social Feedback Real Scraper

这是跨平台真实笔记监测面板的 FastAPI scraper。正式模式只调用真实平台 scraper；失败直接返回结构化错误，不生成 mock、示例或占位数据。

完整安装、运维和验收说明见项目根目录 `PROJECT_HANDOFF.md`。

## 运行边界

- Python 3.10+，推荐 3.11。
- 默认监听 `127.0.0.1:8007`。
- 使用 Playwright 持久 Chrome profile：`~/.minimax/social-feedback/browser/`（电脑级固定目录；除更换电脑外不主动删除或重建）。
- 默认 `BROWSER_HEADLESS=false`，登录由用户在真实 Chrome 窗口完成。
- scraper API Key 只接受 `X-API-Key` 请求头。
- `.runtime/formal.env` 由根目录脚本首次启动时自动生成，权限为 `0600`，不得提交或打包。

## 启动

从项目根目录执行：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
./start-scraper.sh
```

macOS 启动脚本会优先使用已安装的 Google Chrome。也可以显式设置 `BROWSER_EXECUTABLE_PATH`。

正式长期运行推荐：

```bash
./scripts/install-launchd.sh
./scripts/service-status.sh
```

## 正式路由

正式模式注册：

- `GET /api/status`
- `POST /api/scrape/info`
- `POST /api/scrape/comments`
- `POST /api/scrape/search`
- 真实 scraper 自检和只读探针路由
- 真实凭证状态路由
- `GET /api/browser/status`
- `POST /api/browser/login/{platform}`

旧 auth、business、proxy、reload 和 HTTP bridge 路由默认不注册。危险浏览器管理接口（eval、goto、inject-cookie、reset、screenshot、click）在正式模式下默认返回 403。

## 登录

不要手工复制 API Key，也不要传输 Cookie。使用根目录辅助脚本：

```bash
./scripts/login-platform.sh xhs
./scripts/login-platform.sh douyin
./scripts/login-platform.sh kuaishou
```

在打开的 Chrome 窗口中自行完成登录。密码、验证码、Cookie 和浏览器 profile 不得写入代码、文档、数据库或聊天记录。

## 真实数据规则

- 详情、评论和搜索调用对应真实平台 scraper。
- 抓取失败不得回退到普通网页猜测或 mock。
- 缺失指标保持 `None / NULL`。
- 小红书公开浏览量缺失时保持 `NULL`。
- 快手没有公开收藏总数时保持 `NULL`。
- 评论相对时间保留原始文本，不伪造时间戳。
- 单次浏览器步骤不超过 30 秒；验证码或空页时停止高频重试。

## 只读探针

```bash
export PROBE_XHS_URL='完整且含当前 xsec_token 的小红书分享链接'
export PROBE_DOUYIN_URL='完整抖音作品链接'
export PROBE_KUAISHOU_URL='完整快手作品链接'
./scripts/run-real-probes.sh
```

探针验证真实标题、发布时间、指标、评论和 `*-real` backend，并拒绝 degraded/mock；它不会写面板数据库。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

正式数据库不得用于测试。测试代码使用临时数据库和隔离配置。
