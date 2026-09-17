#!/bin/bash
# 一键烟测：跑 10 条 curl 验证后端契约
# 用法：bash scripts/smoke.sh [base_url]  默认 http://127.0.0.1:8001
set -e
B=${1:-http://127.0.0.1:8001}
pass=0
fail=0

check() {
  local name=$1 expected_code=$2 actual_code=$3
  if [ "$actual_code" = "$expected_code" ]; then
    echo "  ✓ $name (HTTP $actual_code)"
    pass=$((pass+1))
  else
    echo "  ✗ $name expected $expected_code got $actual_code"
    fail=$((fail+1))
  fi
}

req() {
  # req METHOD PATH [json_body]
  local method=$1 path=$2 body=$3
  if [ -n "$body" ]; then
    curl -s -o /dev/null -w "%{http_code}" -X "$method" "$B$path" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -s -o /dev/null -w "%{http_code}" -X "$method" "$B$path"
  fi
}

echo ">>> smoke test against $B"

# 1. status
code=$(req GET /api/status)
check "GET /api/status" 200 $code

# 2. info bilibili
code=$(req POST /api/scrape/info '{"platform":"bilibili","content_id":"BV1xx"}')
check "POST /api/scrape/info bilibili" 200 $code

# 3. comments
code=$(req POST /api/scrape/comments '{"platform":"xhs","content_id":"abc123","kwargs":{"max_comments":5}}')
check "POST /api/scrape/comments xhs" 200 $code

# 4. search
code=$(req POST /api/scrape/search '{"platform":"douyin","keyword":"新品","limit":5}')
check "POST /api/scrape/search douyin" 200 $code

# 5. call
code=$(req POST /api/scrape/call '{"platform":"bilibili","method":"get_user_videos","args":["1"]}')
check "POST /api/scrape/call get_user_videos" 200 $code

# 6. invalid platform
code=$(req POST /api/scrape/info '{"platform":"unknown","content_id":"abc"}')
check "POST /api/scrape/info invalid platform" 400 $code

# 7. invalid content id
code=$(req POST /api/scrape/info '{"platform":"xhs","content_id":""}')
check "POST /api/scrape/info empty content_id" 400 $code

# 8. unknown method
code=$(req POST /api/scrape/call '{"platform":"xhs","method":"hack"}')
check "POST /api/scrape/call unknown method" 400 $code

# 9. reload
code=$(req POST /api/reload/xhs)
check "POST /api/reload/xhs" 200 $code

# 10. scraper test
code=$(req POST /api/scrapers/bilibili/test '{"keyword":"测试"}')
check "POST /api/scrapers/bilibili/test" 200 $code

# 11. credentials bilibili qr start
code=$(req POST /api/credentials/bilibili/qr/start '{}')
check "POST /api/credentials/bilibili/qr/start" 200 $code

# 12. credentials browser (xhs 故意 404)
code=$(req POST /api/credentials/xhs/browser)
check "POST /api/credentials/xhs/browser 404" 404 $code

# 13. credentials browser (bilibili 200)
code=$(req POST /api/credentials/bilibili/browser)
check "POST /api/credentials/bilibili/browser" 200 $code

# 14. reload unknown platform
code=$(req POST /api/reload/foo)
check "POST /api/reload/foo 503" 503 $code

echo
echo "passed: $pass, failed: $fail"
[ $fail -eq 0 ] && exit 0 || exit 1
