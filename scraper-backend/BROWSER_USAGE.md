# 正式浏览器登录与抓取指南

## 当前架构

```text
Waitress 面板 :5060 → FastAPI scraper :8007 → Playwright + Google Chrome → 真实平台
                                              ↓
                         ~/.minimax/social-feedback/browser/
```

服务只监听 `127.0.0.1`。正式模式使用可见 Chrome 窗口和独立持久 profile；登录态只保存在本机 profile 中，不进入交付包。该目录是电脑级固定目录，不随项目工作区迁移；服务重启、任务结束和标签页清理都不得删除或重建它。

## 登录方式

只允许用户在真实 Chrome 窗口中手动登录：

```bash
./scripts/login-platform.sh xhs
./scripts/login-platform.sh douyin
./scripts/login-platform.sh kuaishou
```

登录完成后查看状态：

```bash
./scripts/service-status.sh
```

不要把密码、验证码、Cookie 或 API Key 复制到命令行参数、代码、文档、数据库或聊天记录。

## 禁止的旧方式

正式模式禁用以下浏览器管理接口：

- `/api/browser/inject-cookie`
- `/api/browser/reset`
- `/api/browser/eval/{platform}`
- `/api/browser/goto/{platform}`
- `/api/browser/screenshot/{platform}`
- `/api/browser/click/{platform}`

它们默认返回 403。不要为了方便而开启 `ENABLE_BROWSER_ADMIN`；这会扩大本机攻击面，也容易泄露登录态。

## 抓取规则

- 小红书通常需要已登录 profile；详情链接还需要当前有效 `xsec_token`。
- 抖音详情与评论当前可公开读取，搜索需要登录。
- 快手需要登录；频繁导航可能触发验证码。
- B站当前无需登录即可使用真实 backend。
- Twitter、YouTube、TikTok 的可用性取决于登录态或平台配置。
- 任何浏览器单步操作不得超过 30 秒。
- 遇到验证码、空页或平台限制时停止高频重试，先检查登录态与页面状态。

## 运行配置

首次启动会自动创建项目根目录 `.runtime/formal.env`，其中包含随机 API Key。辅助脚本会自行读取该文件，不需要用户复制密钥。

常用环境变量：

| 变量 | 正式默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | scraper 监听地址 |
| `PORT` | `8007` | scraper 端口 |
| `BROWSER_USER_DATA_DIR` | `~/.minimax/social-feedback/browser` | 电脑级固定持久登录目录；除更换电脑外不主动删除或重建 |
| `BROWSER_HEADLESS` | `false` | 显示真实登录窗口 |
| `BROWSER_EXECUTABLE_PATH` | 自动检测 Google Chrome | 浏览器可执行文件 |
| `FORMAL_MODE` | `1` | 正式模式 |
| `ENABLE_LEGACY_API` | `0` | 禁止旧兼容路由 |
| `ENABLE_BROWSER_ADMIN` | `0` | 禁止危险浏览器管理接口 |

## 三平台只读探针

登录完成并准备有效链接后：

```bash
export PROBE_XHS_URL='完整且含当前 xsec_token 的小红书分享链接'
export PROBE_DOUYIN_URL='完整抖音作品链接'
export PROBE_KUAISHOU_URL='完整快手作品链接'
./scripts/run-real-probes.sh
```

探针不写面板数据库。没有真实链接或登录态时，不应将“路由可用”描述成“真实抓取已通过”。
