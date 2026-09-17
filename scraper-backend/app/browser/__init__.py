"""浏览器自动化层。

- `BrowserPool` 是全局共享的 Playwright Chromium 实例
- 持久化 user data dir 让 cookie 在重启后保留
- 每个平台维护一个独立 page（tab），避免互相干扰
- 抓取器通过 `pool.navigate()` / `pool.evaluate()` 调用
"""
