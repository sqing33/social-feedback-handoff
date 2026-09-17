from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PANEL_DIR = ROOT / "panel"
SCRAPER_DIR = ROOT / "scraper-backend"
sys.path.insert(0, str(PANEL_DIR))
sys.path.insert(0, str(SCRAPER_DIR))

os.environ.setdefault("FORMAL_MODE", "1")
os.environ.setdefault("ENABLE_LEGACY_API", "0")
os.environ.setdefault("ENABLE_BROWSER_ADMIN", "0")
os.environ.setdefault("ALLOW_LEGACY_WRITE_API", "0")
os.environ.setdefault("ALLOW_GENERIC_PUBLIC_CAPTURE", "0")
os.environ.pop("SCRAPER_API_KEY", None)

import social_feedback_backend as panel_backend  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.browser.pool import _parse_macos_proxy_config, _safe_url_for_error  # noqa: E402
from app.main import app as scraper_app  # noqa: E402
from app.middleware import APIKeyMiddleware, RequestIDMiddleware  # noqa: E402
from app.models import MetaInfo  # noqa: E402
from app.scrapers import xhs as xhs_module  # noqa: E402
from app.xhs_bridge.protocol import sanitize_result, validate_bridge_url, validate_method, validate_params  # noqa: E402
from app.xhs_bridge.runtime import prepare_extension, reset_for_tests as reset_bridge_runtime  # noqa: E402


class FormalPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        panel_backend.DB_PATH = Path(self.tmp.name) / "formal-test.db"
        panel_backend.init_db()
        self.client = panel_backend.app.test_client()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(panel_backend.DB_PATH) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("archives", "feedback", "assets", "metric_history")
            }

    def test_direct_write_routes_are_disabled(self) -> None:
        archive = {"topic": "fake", "archive_date": "2026-09-05", "archive_time": "12:00"}
        self.assertEqual(self.client.post("/api/archives", json=archive).status_code, 410)
        self.assertEqual(self.client.post("/api/ingest", json={"items": [archive]}).status_code, 410)
        self.assertEqual(self.counts(), {"archives": 0, "feedback": 0, "assets": 0, "metric_history": 0})

    def test_generic_and_mismatched_domains_are_rejected_before_network(self) -> None:
        with self.assertRaisesRegex(ValueError, "只允许已支持平台"):
            panel_backend.capture_link_snapshot("http://127.0.0.1:9999/private")
        with self.assertRaisesRegex(ValueError, "域名与所选平台不匹配"):
            panel_backend.capture_link_snapshot("https://example.com/note", "小红书")
        with patch.object(panel_backend, "resolve_supported_detail_url", return_value="https://v.douyin.com/share"):
            with self.assertRaisesRegex(ValueError, "无法从平台链接识别真实作品编号"):
                panel_backend.capture_link_snapshot("https://v.douyin.com/share")

    def test_failed_real_capture_never_writes(self) -> None:
        failure = {
            "status": "error",
            "platform": "抖音",
            "title": "",
            "error": "real scraper failed",
        }
        with patch.object(panel_backend, "capture_link_snapshot", return_value=failure):
            response = self.client.post(
                "/api/notes/import",
                json={"urls": ["https://www.douyin.com/video/7674892845588827435"]},
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["items"], [])
        self.assertEqual(len(body["failures"]), 1)
        self.assertEqual(self.counts(), {"archives": 0, "feedback": 0, "assets": 0, "metric_history": 0})

    def test_metric_columns_allow_null(self) -> None:
        with sqlite3.connect(panel_backend.DB_PATH) as conn:
            info = {row[1]: row for row in conn.execute("PRAGMA table_info(feedback)")}
        for column in ("views", "likes", "saves", "comments"):
            self.assertEqual(info[column][3], 0, column)

    def test_xhs_snapshot_drops_temporary_access_parameters(self) -> None:
        note_id = "69f981ff000000003802270b"
        temporary_url = (
            f"https://www.xiaohongshu.com/explore/{note_id}"
            "?xsec_token=temporary-test-value&xsec_source=pc_note"
        )
        snapshot = {
            "status": "fetched",
            "platform": "小红书",
            "title": "真实图文标题",
            "url": temporary_url,
            "metrics": {"views": None, "likes": 81000, "saves": 13000, "comments": 2749},
            "comments_data": [{"content": "真实公开评论", "profile_url": temporary_url}],
            "comments_error": (
                "nested failure https://www.xiaohongshu.com/explore/"
                f"{note_id}?xsec_token=temporary-test-value&xsec_source=pc_note"
            ),
            "content_context": {
                "content_id": note_id,
                "xsec_token": "temporary-test-value",
                "xsec_source": "pc_note",
            },
        }
        stable_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        with sqlite3.connect(panel_backend.DB_PATH) as conn:
            archive_id = panel_backend.save_note_snapshot(conn, temporary_url, snapshot)
            conn.commit()
            row = conn.execute(
                "SELECT published_url, published_snapshot_json FROM archives WHERE id = ?",
                (archive_id,),
            ).fetchone()
        stored = json.loads(row[1])
        serialized = json.dumps(stored, ensure_ascii=False)
        self.assertEqual(row[0], stable_url)
        self.assertEqual(stored["url"], stable_url)
        self.assertEqual(stored["comments_data"][0]["profile_url"], stable_url)
        self.assertNotIn("xsec_token", serialized)
        self.assertNotIn("xsec_source", serialized)
        self.assertNotIn("temporary-test-value", serialized)

    def test_search_import_sorts_by_likes_and_caps_limit_at_50(self) -> None:
        candidates = [
            {"content_id": "BVlow", "url": "https://www.bilibili.com/video/BVlow", "stats": {"like": 10}},
            {"content_id": "BVhigh", "url": "https://www.bilibili.com/video/BVhigh", "stats": {"like": 900}},
        ]

        def snapshot(url: str, *args, **kwargs) -> dict:
            title = "高赞" if "BVhigh" in url else "低赞"
            return {
                "status": "fetched", "platform": "B站", "title": title, "url": url,
                "metrics": {"views": None, "likes": 900 if title == "高赞" else 10, "saves": None, "comments": None},
                "comments_data": [],
            }

        with patch.object(panel_backend, "scraper_service_search", return_value=(candidates, {})) as search_mock, \
             patch.object(panel_backend, "capture_link_snapshot", side_effect=snapshot), \
             patch.object(panel_backend, "attach_real_comments", side_effect=lambda item, *_args, **_kwargs: item):
            response = self.client.post(
                "/api/notes/search-import",
                json={"platform": "B站", "keyword": "测试", "limit": 99},
            )
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["limit"], 50)
        self.assertEqual([item["topic"] for item in body["items"]], ["高赞", "低赞"])
        self.assertEqual(search_mock.call_args.kwargs["limit"], 50)

    def test_account_import_caps_limit_at_50(self) -> None:
        progress = {
            "status": "completed",
            "successful_count": 0,
            "remaining_count": 0,
            "pause_reason": "",
            "resumable": False,
        }
        with patch.object(panel_backend, "import_account_results", return_value=([], [], progress)) as import_mock:
            response = self.client.post(
                "/api/notes/account-import",
                json={"platform": "小红书", "profile_url": "https://www.xiaohongshu.com/user/profile/example", "limit": 88},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["limit"], 50)
        self.assertEqual(import_mock.call_args.args[3], 50)

    def test_account_view_is_present_in_panel(self) -> None:
        html = (ROOT / "panel" / "social-feedback-panel.html").read_text(encoding="utf-8")
        self.assertIn('data-view="accounts"', html)
        self.assertIn("账号信息", html)
        self.assertIn("item.isAccountImport", html)
        self.assertIn("accountGroups", html)
        self.assertIn("stableProfileUrl", html)

        panel_backend.FETCH_STATE["last_xiaohongshu_fetch_at"] = 100.0
        with patch.object(panel_backend.random, "uniform", return_value=7.0), \
             patch.object(panel_backend.time, "time", side_effect=[103.0, 107.0]), \
             patch.object(panel_backend.time, "sleep") as sleep_mock:
            panel_backend.xiaohongshu_fetch_pause()
        sleep_mock.assert_called_once_with(4.0)
        self.assertEqual(panel_backend.FETCH_STATE["last_xiaohongshu_fetch_at"], 107.0)
        self.assertEqual(panel_backend.XIAOHONGSHU_FETCH_POLICY["min_interval_seconds"], 5.0)
        self.assertEqual(panel_backend.XIAOHONGSHU_FETCH_POLICY["max_interval_seconds"], 10.0)

    def test_four_platform_login_manager_is_present_in_panel(self) -> None:
        html = (ROOT / "panel" / "social-feedback-panel.html").read_text(encoding="utf-8")
        self.assertIn("平台登录确认", html)
        self.assertIn("确认已登录", html)
        self.assertIn('id="loginSignal"', html)
        self.assertIn('data-login-action="confirm"', html)
        for platform in ("xhs", "douyin", "kuaishou", "bilibili"):
            self.assertIn(f"key: '{platform}'", html)

    def test_login_confirmation_requires_real_scraper_login_state(self) -> None:
        with patch.object(panel_backend, "scraper_browser_action", side_effect=RuntimeError("CREDENTIAL_EXPIRED: no active login")):
            response = self.client.post("/api/login/xhs/confirm", json={})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["confirmed"])
        with sqlite3.connect(panel_backend.DB_PATH) as conn:
            confirmed = conn.execute(
                "SELECT confirmed FROM platform_login_state WHERE platform = 'xhs'"
            ).fetchone()[0]
        self.assertEqual(confirmed, 0)

    def test_login_confirmation_persists_without_exposing_cookies(self) -> None:
        live_flags = {key: True for key in panel_backend.LOGIN_PLATFORM_KEYS}
        with patch.object(
            panel_backend,
            "scraper_browser_action",
            return_value={"success": True, "logged_in": True, "cookie_count": 7},
        ), patch.object(
            panel_backend,
            "scraper_browser_status",
            return_value={"ok": True, "logged_in": live_flags, "user_data_dir": "/private/profile"},
        ):
            responses = [
                self.client.post(f"/api/login/{platform}/confirm", json={})
                for platform in ("xhs", "douyin", "kuaishou", "bilibili")
            ]
            status_response = self.client.get("/api/login-status")
        self.assertTrue(all(response.status_code == 200 for response in responses))
        final_body = responses[-1].get_json()
        self.assertTrue(final_body["all_ready"])
        self.assertEqual(final_body["signal"], "ALL_PLATFORM_LOGINS_CONFIRMED")
        status_body = status_response.get_json()
        self.assertTrue(status_body["all_ready"])
        serialized = json.dumps({"confirmation": final_body, "status": status_body})
        self.assertNotIn("cookie", serialized.lower())
        self.assertNotIn("private/profile", serialized)

    def test_account_profile_url_summary_drops_query_parameters(self) -> None:
        snapshot = {
            "status": "fetched",
            "platform": "B站",
            "title": "账号主页笔记",
            "author": "账号甲",
            "url": "https://www.bilibili.com/video/BVprofile",
            "import_context": {
                "source": "account_import",
                "account_name": "账号甲",
                "account_profile_url": "https://space.bilibili.com/123?from_tab_name=main#posts",
            },
            "metrics": {"views": None, "likes": None, "saves": None, "comments": None},
            "comments_data": [],
        }
        with sqlite3.connect(panel_backend.DB_PATH) as conn:
            archive_id = panel_backend.save_note_snapshot(
                conn,
                snapshot["url"],
                snapshot,
                favorite=False,
                source="account_import",
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
            summary = panel_backend.archive_summary(row, conn)
        self.assertEqual(summary["account_profile_url"], "https://space.bilibili.com/123")

        for platform_key in panel_backend.FETCH_INTERVAL_PLATFORMS:
            with self.subTest(platform=platform_key):
                state_key = panel_backend.FETCH_STATE_KEYS[platform_key]
                panel_backend.FETCH_STATE[state_key] = 100.0
                with patch.object(panel_backend.random, "uniform", return_value=7.0), \
                     patch.object(panel_backend.time, "time", side_effect=[103.0, 107.0]), \
                     patch.object(panel_backend.time, "sleep") as sleep_mock:
                    panel_backend.platform_fetch_pause(platform_key)
                sleep_mock.assert_called_once_with(4.0)
                self.assertEqual(panel_backend.FETCH_STATE[state_key], 107.0)
                policy = panel_backend.PLATFORM_FETCH_POLICY[platform_key]
                self.assertEqual((policy["min_interval_seconds"], policy["max_interval_seconds"]), (5.0, 10.0))

    def test_scraper_service_entrypoints_throttle_all_four_platforms(self) -> None:
        response = {"note_info": {}, "comments": [], "results": [], "result": []}
        with patch.object(panel_backend, "platform_fetch_pause") as pause_mock, \
             patch.object(panel_backend, "scraper_service_request", return_value=(response, {})):
            for platform_key in panel_backend.FETCH_INTERVAL_PLATFORMS:
                panel_backend.scraper_service_info(platform_key, "content")
                panel_backend.scraper_service_comments(platform_key, "content")
                panel_backend.scraper_service_search(platform_key, "keyword")
                panel_backend.scraper_service_call(platform_key, "get_info")
        self.assertEqual(pause_mock.call_count, 16)
        self.assertEqual(
            [call.args[0] for call in pause_mock.call_args_list],
            [platform_key for platform_key in panel_backend.FETCH_INTERVAL_PLATFORMS for _ in range(4)],
        )

    def test_account_import_sorts_newest_first(self) -> None:
        candidates = [
            {"content_id": "BVold", "url": "https://www.bilibili.com/video/BVold", "publish_time": 100},
            {"content_id": "BVnew", "url": "https://www.bilibili.com/video/BVnew", "publish_time": 200},
        ]

        def snapshot(url: str, *args, **kwargs) -> dict:
            title = "最新" if "BVnew" in url else "较早"
            return {
                "status": "fetched", "platform": "B站", "title": title, "author": "账号甲", "url": url,
                "metrics": {"views": None, "likes": None, "saves": None, "comments": None},
                "comments_data": [],
            }

        profile_url = "https://space.bilibili.com/example?from_tab_name=main"
        with sqlite3.connect(panel_backend.DB_PATH) as conn, \
             patch.object(panel_backend, "scraper_service_account_contents", return_value=(candidates, {})), \
             patch.object(panel_backend, "capture_link_snapshot", side_effect=snapshot), \
             patch.object(panel_backend, "attach_real_comments", side_effect=lambda item, *_args, **_kwargs: item):
            conn.row_factory = sqlite3.Row
            items, failures, progress = panel_backend.import_account_results(
                conn, "B站", profile_url, 20
            )
        self.assertEqual(failures, [])
        self.assertEqual(progress["status"], "completed")
        self.assertEqual([item["topic"] for item in items], ["最新", "较早"])
        self.assertTrue(all(item["import_source"] == "account_import" for item in items))
        self.assertTrue(all(item["account_name"] == "账号甲" for item in items))
        self.assertTrue(all(item["account_profile_url"] == "https://space.bilibili.com/example" for item in items))

    def test_xhs_account_import_stops_on_first_verification(self) -> None:
        note_ids = [
            "69f981ff0000000038022701",
            "69f981ff0000000038022702",
            "69f981ff0000000038022703",
        ]
        candidates = [
            {"content_id": note_id, "url": f"https://www.xiaohongshu.com/explore/{note_id}", "author": "账号甲"}
            for note_id in note_ids
        ]

        def snapshot(url: str, *args, **kwargs) -> dict:
            self.assertTrue(kwargs.get("account_import"))
            if note_ids[0] in url:
                return {
                    "status": "fetched",
                    "platform": "小红书",
                    "title": "第一条真实作品",
                    "author": "账号甲",
                    "url": url,
                    "content_id": note_ids[0],
                    "content_context": {"content_id": note_ids[0]},
                    "metrics": {"views": None, "likes": 1, "saves": None, "comments": None},
                    "comments_data": [],
                }
            return {
                "status": "error",
                "platform": "小红书",
                "error": "RATE_LIMITED: xhs temporarily requires interactive verification",
            }

        profile_url = (
            "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0"
            "?xsec_token=temporary-test-value&xsec_source=pc_search"
        )
        with sqlite3.connect(panel_backend.DB_PATH) as conn, \
             patch.object(panel_backend, "scraper_service_account_contents", return_value=(candidates, {})), \
             patch.object(panel_backend, "capture_link_snapshot", side_effect=snapshot) as capture_mock, \
             patch.object(panel_backend, "attach_real_comments", side_effect=lambda item, *_args, **_kwargs: item):
            conn.row_factory = sqlite3.Row
            items, failures, progress = panel_backend.import_account_results(
                conn, "小红书", profile_url, 20
            )
        self.assertEqual(capture_mock.call_count, 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(progress["status"], "paused")
        self.assertEqual(progress["successful_count"], 1)
        self.assertEqual(progress["remaining_count"], 2)
        self.assertTrue(progress["resumable"])
        serialized = json.dumps({"items": items, "failures": failures, "progress": progress}, ensure_ascii=False)
        self.assertNotIn("temporary-test-value", serialized)
        self.assertNotIn("xsec_token", serialized)

    def test_xhs_profile_discovery_rate_limit_returns_paused_progress(self) -> None:
        profile_url = (
            "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0"
            "?xsec_token=temporary-test-value&xsec_source=pc_search"
        )
        with sqlite3.connect(panel_backend.DB_PATH) as conn, patch.object(
            panel_backend,
            "scraper_service_account_contents",
            side_effect=RuntimeError(
                "RATE_LIMITED: verification_wall "
                "?xsec_token=temporary-test-value&xsec_source=pc_search"
            ),
        ):
            items, failures, progress = panel_backend.import_account_results(
                conn, "小红书", profile_url, 7
            )
        self.assertEqual(items, [])
        self.assertEqual(progress["status"], "paused")
        self.assertEqual(progress["successful_count"], 0)
        self.assertEqual(progress["remaining_count"], 7)
        self.assertTrue(progress["resumable"])
        serialized = json.dumps({"failures": failures, "progress": progress}, ensure_ascii=False)
        self.assertNotIn("temporary-test-value", serialized)
        self.assertNotIn("xsec_token", serialized)
        self.assertNotIn("xsec_source", serialized)
        self.assertEqual(
            failures[0]["url"],
            "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0",
        )

    def test_temporary_access_values_are_redacted_from_logs(self) -> None:
        text = "failed /explore/example?xsec_token=secret-value&xsec_source=pc_search"
        redacted = panel_backend.redact_temporary_access_text(text)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("xsec_token", redacted)
        self.assertNotIn("xsec_source", redacted)

    def test_security_headers_do_not_enable_wildcard_cors(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")


class XhsNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_profile_url_returns_only_stable_card_identifiers(self) -> None:
        note_id = "69f981ff000000003802270b"
        profile_url = (
            "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0"
            "?xsec_token=temporary-test-value&xsec_source=pc_search"
        )
        raw_profile = {
            "profile_author": "账号甲",
            "profile_user_id": "5fdc23ba00000000010025d0",
            "items": [{
                "note_id": note_id,
                "author": "账号甲",
                "user_id": "5fdc23ba00000000010025d0",
                "detail_url": f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=temporary-test-value",
                "xsec_token": "temporary-test-value",
                "xsec_source": "pc_user",
            }],
        }
        scraper = xhs_module.XhsScraper()
        with patch.object(xhs_module, "_check_login_or_raise", new=AsyncMock()), patch.object(
            xhs_module, "_navigate_xhs_page", new=AsyncMock()
        ) as navigate_mock, patch.object(xhs_module.pool, "wait_for_state", new=AsyncMock()), patch.object(
            xhs_module.pool,
            "evaluate",
            new=AsyncMock(side_effect=[{"path": "/user/profile/5fdc23ba00000000010025d0"}, raw_profile]),
        ):
            results = await scraper.get_user_contents(profile_url, limit=1)
        self.assertEqual(len(results), 1)
        self.assertFalse(navigate_mock.call_args.kwargs["allow_js_fallback"])
        self.assertEqual(results[0]["content_id"], note_id)
        self.assertEqual(results[0]["url"], f"https://www.xiaohongshu.com/explore/{note_id}")
        self.assertEqual(
            results[0]["profile_url"],
            "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0",
        )
        serialized = json.dumps(results, ensure_ascii=False)
        self.assertNotIn("temporary-test-value", serialized)
        self.assertNotIn("xsec_token", serialized)

    async def test_profile_verification_wall_stops_before_card_extraction(self) -> None:
        scraper = xhs_module.XhsScraper()
        profile_url = "https://www.xiaohongshu.com/user/profile/5fdc23ba00000000010025d0"
        with patch.object(xhs_module, "_check_login_or_raise", new=AsyncMock()), patch.object(
            xhs_module, "_navigate_xhs_page", new=AsyncMock()
        ), patch.object(xhs_module.pool, "wait_for_state", new=AsyncMock()), patch.object(
            xhs_module.pool,
            "evaluate",
            new=AsyncMock(return_value={
                "path": "/user/profile/5fdc23ba00000000010025d0",
                "verification_wall": True,
            }),
        ) as evaluate_mock:
            with self.assertRaises(xhs_module.ScraperError) as raised:
                await scraper.get_user_contents(profile_url, limit=1)
        self.assertEqual(raised.exception.code, xhs_module.ErrorCode.RATE_LIMITED)
        self.assertEqual(raised.exception.capability, "account-contents")
        evaluate_mock.assert_awaited_once()

    async def test_native_card_click_matches_href_and_uses_visible_bounding_box(self) -> None:
        note_id = "69f981ff000000003802270b"
        page = MagicMock()
        anchors = MagicMock()
        wrong_anchor = MagicMock()
        target_anchor = MagicMock()
        anchors.count = AsyncMock(return_value=2)
        anchors.nth.side_effect = [wrong_anchor, target_anchor]
        wrong_anchor.get_attribute = AsyncMock(return_value="/explore/not-the-target")
        target_anchor.get_attribute = AsyncMock(
            return_value=f"/explore/{note_id}?xsec_source=pc_user"
        )
        target_anchor.is_visible = AsyncMock(return_value=True)
        target_anchor.scroll_into_view_if_needed = AsyncMock()
        target_anchor.bounding_box = AsyncMock(
            return_value={"x": 10.0, "y": 20.0, "width": 100.0, "height": 80.0}
        )
        page.locator.return_value = anchors
        page.mouse.click = AsyncMock()
        with patch.object(xhs_module.pool, "_get_page", new=AsyncMock(return_value=page)):
            clicked = await xhs_module.pool.click_xhs_note_card(note_id)
        self.assertTrue(clicked)
        target_anchor.is_visible.assert_awaited_once()
        target_anchor.bounding_box.assert_awaited_once()
        page.mouse.click.assert_awaited_once_with(60.0, 60.0)

    async def test_native_card_click_uses_visible_media_box_when_anchor_has_no_box(self) -> None:
        note_id = "69f981ff000000003802270b"
        page = MagicMock()
        anchors = MagicMock()
        anchor = MagicMock()
        media = MagicMock()
        anchors.count = AsyncMock(return_value=1)
        anchors.nth.return_value = anchor
        anchor.get_attribute = AsyncMock(return_value=f"/explore/{note_id}")
        anchor.is_visible = AsyncMock(return_value=True)
        anchor.scroll_into_view_if_needed = AsyncMock()
        anchor.bounding_box = AsyncMock(return_value=None)
        anchor.locator.return_value.first = media
        media.is_visible = AsyncMock(return_value=True)
        media.scroll_into_view_if_needed = AsyncMock()
        media.bounding_box = AsyncMock(return_value={"x": 20.0, "y": 30.0, "width": 80.0, "height": 60.0})
        page.locator.return_value = anchors
        page.mouse.click = AsyncMock()
        with patch.object(xhs_module.pool, "_get_xhs_profile_page", new=AsyncMock(return_value=page)):
            clicked = await xhs_module.pool.click_xhs_note_card(note_id)
        self.assertTrue(clicked)
        anchor.locator.assert_any_call("img, video, picture")
        page.mouse.click.assert_awaited_once_with(60.0, 60.0)

    async def test_native_card_click_uses_visible_card_container_for_hidden_href_anchor(self) -> None:
        note_id = "69f981ff000000003802270b"
        page = MagicMock()
        anchors = MagicMock()
        anchor = MagicMock()
        media = MagicMock()
        card = MagicMock()
        anchors.count = AsyncMock(return_value=1)
        anchors.nth.return_value = anchor
        anchor.get_attribute = AsyncMock(return_value=f"/explore/{note_id}")
        anchor.is_visible = AsyncMock(return_value=False)
        media.is_visible = AsyncMock(return_value=False)
        card.is_visible = AsyncMock(return_value=True)
        card.scroll_into_view_if_needed = AsyncMock()
        card.bounding_box = AsyncMock(return_value={"x": 40.0, "y": 50.0, "width": 120.0, "height": 100.0})

        def locate(selector: str):
            located = MagicMock()
            located.first = media if selector == "img, video, picture" else card
            return located

        anchor.locator.side_effect = locate
        page.locator.return_value = anchors
        page.mouse.click = AsyncMock()
        with patch.object(xhs_module.pool, "_get_xhs_profile_page", new=AsyncMock(return_value=page)):
            clicked = await xhs_module.pool.click_xhs_note_card(note_id)
        self.assertTrue(clicked)
        card.scroll_into_view_if_needed.assert_awaited_once()
        page.mouse.click.assert_awaited_once_with(100.0, 100.0)

    async def test_account_native_click_captures_popup_and_restores_profile_page(self) -> None:
        note_id = "69f981ff000000003802270b"
        profile_page = MagicMock()
        anchors = MagicMock()
        anchor = MagicMock()
        popup = MagicMock()
        anchors.count = AsyncMock(return_value=1)
        anchors.nth.return_value = anchor
        anchor.get_attribute = AsyncMock(return_value=f"/explore/{note_id}")
        anchor.is_visible = AsyncMock(return_value=True)
        anchor.scroll_into_view_if_needed = AsyncMock()
        anchor.bounding_box = AsyncMock(return_value={"x": 10.0, "y": 20.0, "width": 100.0, "height": 80.0})
        profile_page.locator.return_value = anchors
        profile_page.mouse.click = AsyncMock()
        popup.is_closed.return_value = False
        popup.wait_for_load_state = AsyncMock()
        popup.close = AsyncMock()

        class PopupWaiter:
            def __init__(self, value) -> None:
                self.value = value

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        popup_future = asyncio.get_running_loop().create_future()
        popup_future.set_result(popup)
        waiter = PopupWaiter(popup_future)
        profile_page.expect_popup = MagicMock(return_value=waiter)

        with patch.object(xhs_module.pool, "_get_xhs_profile_page", new=AsyncMock(return_value=profile_page)):
            clicked = await xhs_module.pool.click_xhs_note_card(
                note_id,
                timeout_ms=5000,
                capture_popup=True,
            )
        self.assertTrue(clicked)
        profile_page.mouse.click.assert_awaited_once_with(60.0, 60.0)
        popup.wait_for_load_state.assert_awaited_once()
        self.assertIs(xhs_module.pool._xhs_active_page, popup)

        await xhs_module.pool.restore_xhs_profile_page()
        popup.close.assert_awaited_once()
        self.assertIsNone(xhs_module.pool._xhs_active_page)

    async def test_restore_profile_page_uses_browser_history_after_same_tab_detail(self) -> None:
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://www.xiaohongshu.com/explore/69f981ff000000003802270b"
        page.go_back = AsyncMock(return_value=MagicMock())
        previous_page = xhs_module.pool._pages.get("xhs")
        previous_active = xhs_module.pool._xhs_active_page
        xhs_module.pool._pages["xhs"] = page
        xhs_module.pool._xhs_active_page = None
        try:
            await xhs_module.pool.restore_xhs_profile_page()
        finally:
            if previous_page is None:
                xhs_module.pool._pages.pop("xhs", None)
            else:
                xhs_module.pool._pages["xhs"] = previous_page
            xhs_module.pool._xhs_active_page = previous_active
        page.go_back.assert_awaited_once_with(wait_until="domcontentloaded", timeout=15000)

    async def test_account_import_enables_popup_capture_only_for_account_mode(self) -> None:
        note_id = "69f981ff000000003802270b"
        ready = {"target_match": True, "detail_ready": True}
        with patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(side_effect=[{"path": "/user/profile/example"}, ready]),
        ), patch.object(
            xhs_module.pool,
            "click_xhs_note_card",
            new=AsyncMock(return_value=True),
        ) as click_mock, patch.object(
            xhs_module.pool,
            "wait_for_state",
            new=AsyncMock(return_value=True),
        ):
            state = await xhs_module._open_detail_page(
                note_id,
                "",
                account_import=True,
            )
        self.assertTrue(state["detail_ready"])
        click_mock.assert_awaited_once_with(note_id, timeout_ms=5000, capture_popup=True)

    async def test_account_info_does_not_construct_detail_url(self) -> None:
        note_id = "69f981ff000000003802270b"
        ready = {
            "note_id": note_id,
            "title": "来自真实卡片的标题",
            "cover": "https://img.example/cover.jpg",
            "images": ["https://img.example/cover.jpg"],
            "media_type": "image",
            "user": {"nickname": "账号甲"},
        }
        scraper = xhs_module.XhsScraper()
        with patch.object(xhs_module, "_check_login_or_raise", new=AsyncMock()), patch.object(
            xhs_module,
            "_open_detail_page",
            new=AsyncMock(return_value={"target_match": True, "detail_ready": True}),
        ) as open_mock, patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=ready)):
            result = await scraper.get_info(note_id, account_import=True)
        self.assertEqual(result["content_id"], note_id)
        self.assertEqual(open_mock.call_args.args[1], "")

    async def test_comments_close_account_popup_after_reuse(self) -> None:
        note_id = "69f981ff000000003802270b"
        popup = MagicMock()
        popup.is_closed.return_value = False
        popup.close = AsyncMock()
        xhs_module.pool._xhs_active_page = popup
        scraper = xhs_module.XhsScraper()
        with patch.object(xhs_module, "_USE_EXISTING_CHROME_TAB", False), patch.object(
            xhs_module, "_check_login_or_raise", new=AsyncMock()
        ), patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(return_value={"target_match": True, "detail_ready": True}),
        ), patch.object(xhs_module, "_raise_for_detail_page_state", new=AsyncMock()), patch.object(
            xhs_module.pool,
            "evaluate",
            new=AsyncMock(side_effect=[True, []]),
        ), patch.object(xhs_module.pool, "wait_for_state", new=AsyncMock()):
            comments = await scraper.get_comments(note_id, reuse_current_page=True, account_import=True)
        self.assertEqual(comments, [])
        popup.close.assert_awaited_once()
        self.assertIsNone(xhs_module.pool._xhs_active_page)

    async def test_account_import_never_uses_script_or_direct_navigation_fallback(self) -> None:
        note_id = "69f981ff000000003802270b"
        bridge = MagicMock()
        bridge.click_note_card = AsyncMock(return_value=True)
        with patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(return_value={"path": "/user/profile/example"}),
        ), patch.object(
            xhs_module.pool, "click_xhs_note_card", new=AsyncMock(return_value=False)
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock()) as evaluate_mock, patch.object(
            xhs_module, "_SAFE_BRIDGE", bridge
        ), patch.object(
            xhs_module, "_navigate_xhs_page", new=AsyncMock()
        ) as navigate_mock:
            with self.assertRaises(xhs_module.ScraperError) as raised:
                await xhs_module._open_detail_page(
                    note_id,
                    f"https://www.xiaohongshu.com/explore/{note_id}",
                    account_import=True,
                )
        self.assertEqual(raised.exception.capability, "account-card-navigation")
        bridge.click_note_card.assert_not_awaited()
        evaluate_mock.assert_not_awaited()
        navigate_mock.assert_not_awaited()

    async def test_account_info_preserves_card_navigation_error(self) -> None:
        note_id = "69f981ff000000003802270b"
        card_error = xhs_module.ScraperError(
            "xhs account import could not open the visible href-matched note card",
            code=xhs_module.ErrorCode.CONTENT_NOT_FOUND,
            status_code=409,
            retryable=True,
            capability="account-card-navigation",
        )
        scraper = xhs_module.XhsScraper()
        with patch.object(xhs_module, "_check_login_or_raise", new=AsyncMock()), patch.object(
            xhs_module, "_open_detail_page", new=AsyncMock(side_effect=card_error)
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock()) as evaluate_mock:
            with self.assertRaises(xhs_module.ScraperError) as raised:
                await scraper.get_info(note_id, account_import=True)
        self.assertEqual(raised.exception.capability, "account-card-navigation")
        evaluate_mock.assert_not_awaited()

    async def test_single_link_mode_keeps_direct_navigation_fallback(self) -> None:
        note_id = "69f981ff000000003802270b"
        ready = {"target_match": True, "detail_ready": True}
        with patch.object(
            xhs_module, "_detail_page_state", new=AsyncMock(return_value={"path": "/search_result"})
        ), patch.object(
            xhs_module.pool, "click_xhs_note_card", new=AsyncMock(return_value=False)
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=False)), patch.object(
            xhs_module, "_SAFE_BRIDGE", None
        ), patch.object(xhs_module, "_navigate_xhs_page", new=AsyncMock()) as navigate_mock, patch.object(
            xhs_module, "_wait_for_detail_state", new=AsyncMock(return_value=ready)
        ):
            state = await xhs_module._open_detail_page(
                note_id, f"https://www.xiaohongshu.com/explore/{note_id}"
            )
        self.assertTrue(state["detail_ready"])
        navigate_mock.assert_awaited_once()

    async def test_profile_navigation_disables_location_assign_fallback(self) -> None:
        timeout_error = xhs_module.ScraperError(
            "profile navigation timeout",
            code=xhs_module.ErrorCode.UPSTREAM_TIMEOUT,
            status_code=504,
        )
        with patch.object(
            xhs_module, "_navigate_marked_chrome_tab", new=AsyncMock(return_value=False)
        ), patch.object(
            xhs_module.pool, "navigate", new=AsyncMock(side_effect=timeout_error)
        ), patch.object(
            xhs_module.pool,
            "current_url",
            new=AsyncMock(return_value="https://www.xiaohongshu.com/"),
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=True)) as evaluate_mock:
            with self.assertRaises(xhs_module.ScraperError):
                await xhs_module._navigate_xhs_page(
                    "https://www.xiaohongshu.com/user/profile/example",
                    "/user/profile/example",
                    allow_js_fallback=False,
                )
        expressions = [str(call.args[1]) for call in evaluate_mock.call_args_list if len(call.args) > 1]
        self.assertFalse(any("location.assign" in expression for expression in expressions))

    async def test_comments_reuse_current_account_detail_without_reopening(self) -> None:
        note_id = "69f981ff000000003802270b"
        scraper = xhs_module.XhsScraper()
        with patch.object(xhs_module, "_USE_EXISTING_CHROME_TAB", False), patch.object(
            xhs_module, "_check_login_or_raise", new=AsyncMock()
        ), patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(return_value={"target_match": True, "detail_ready": True}),
        ), patch.object(xhs_module, "_open_detail_page", new=AsyncMock()) as open_mock, patch.object(
            xhs_module, "_raise_for_detail_page_state", new=AsyncMock()
        ), patch.object(xhs_module.pool, "wait_for_state", new=AsyncMock()), patch.object(
            xhs_module.pool, "evaluate", new=AsyncMock(side_effect=[True, []])
        ):
            comments = await scraper.get_comments(
                note_id,
                reuse_current_page=True,
                account_import=True,
            )
        self.assertEqual(comments, [])
        open_mock.assert_not_awaited()

    async def test_detail_open_prefers_live_discovery_card(self) -> None:
        note_id = "69f981ff000000003802270b"
        with patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(side_effect=[{"path": "/search_result"}, {"target_match": True, "detail_ready": True}]),
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=True)) as evaluate_mock, patch.object(
            xhs_module.pool, "wait_for_state", new=AsyncMock(return_value=True)
        ), patch.object(xhs_module.pool, "click_xhs_note_card", new=AsyncMock(return_value=False)), patch.object(
            xhs_module.pool, "navigate", new=AsyncMock()
        ) as navigate_mock:
            state = await xhs_module._open_detail_page(
                note_id,
                f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=temporary-test-value",
            )
        self.assertTrue(state["detail_ready"])
        self.assertIn(note_id, evaluate_mock.call_args.args)
        navigate_mock.assert_not_awaited()

    async def test_native_pool_click_is_used_before_page_evaluate(self) -> None:
        note_id = "69f981ff000000003802270b"
        with patch.object(
            xhs_module,
            "_detail_page_state",
            new=AsyncMock(side_effect=[{"path": "/user/profile/example"}, {"target_match": True, "detail_ready": True}]),
        ), patch.object(xhs_module.pool, "click_xhs_note_card", new=AsyncMock(return_value=True)) as click_mock, patch.object(
            xhs_module.pool, "wait_for_state", new=AsyncMock(return_value=True)
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=False)) as evaluate_mock:
            state = await xhs_module._open_detail_page(note_id, f"https://www.xiaohongshu.com/explore/{note_id}")
        self.assertTrue(state["detail_ready"])
        click_mock.assert_awaited_once_with(note_id, timeout_ms=5000)
        evaluate_mock.assert_not_awaited()

    async def test_slow_navigation_continues_when_expected_path_is_reached(self) -> None:
        timeout_error = xhs_module.ScraperError(
            "slow lifecycle",
            code=xhs_module.ErrorCode.UPSTREAM_TIMEOUT,
            status_code=504,
        )
        with patch.object(xhs_module, "_navigate_marked_chrome_tab", new=AsyncMock(return_value=False)), patch.object(
            xhs_module.pool, "navigate", new=AsyncMock(side_effect=timeout_error)
        ), patch.object(
            xhs_module.pool,
            "current_url",
            new=AsyncMock(return_value="https://www.xiaohongshu.com/search_result?keyword=test"),
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=True)) as evaluate_mock:
            await xhs_module._navigate_xhs_page(
                "https://www.xiaohongshu.com/search_result?keyword=test",
                "/search_result",
            )
        evaluate_mock.assert_awaited_once()

    async def test_native_chrome_navigation_avoids_playwright_goto(self) -> None:
        with patch.object(xhs_module, "_navigate_marked_chrome_tab", new=AsyncMock(return_value=True)), patch.object(
            xhs_module.pool, "current_url", new=AsyncMock(return_value="https://www.xiaohongshu.com/search_result/")
        ), patch.object(xhs_module.pool, "evaluate", new=AsyncMock(return_value=True)), patch.object(
            xhs_module.pool, "navigate", new=AsyncMock()
        ) as navigate_mock:
            await xhs_module._navigate_xhs_page(
                "https://www.xiaohongshu.com/search_result?keyword=test",
                "/search_result",
            )
        navigate_mock.assert_not_awaited()

    async def test_info_error_never_echoes_temporary_access_context(self) -> None:
        scraper = xhs_module.XhsScraper()
        identifier = "69f981ff000000003802270b?xsec_token=temporary-test-value&xsec_source=pc_search"
        with patch.object(xhs_module, "_check_login_or_raise", new=AsyncMock()), patch.object(
            xhs_module, "_open_detail_page", new=AsyncMock(return_value={})
        ), patch.object(xhs_module, "_raise_for_detail_page_state", new=AsyncMock()), patch.object(
            xhs_module.pool, "evaluate", new=AsyncMock(return_value={})
        ):
            with self.assertRaises(xhs_module.ScraperError) as raised:
                await scraper.get_info(identifier)
        self.assertNotIn("temporary-test-value", raised.exception.message)
        self.assertNotIn("xsec_token", raised.exception.message)

    async def test_comments_without_text_are_discarded(self) -> None:
        scraper = xhs_module.XhsScraper()
        raw_comments = [
            {"nickname": "只有作者", "content": "", "likeCount": 1},
            {"nickname": "真实用户", "content": "  真实公开评论  ", "likeCount": 2},
        ]
        with patch.object(xhs_module, "_USE_EXISTING_CHROME_TAB", True), patch.object(
            xhs_module,
            "_extract_existing_chrome_comments",
            new=AsyncMock(return_value=raw_comments),
        ):
            comments = await scraper.get_comments("69f981ff000000003802270b")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["content"].strip(), "真实公开评论")

    def test_macos_proxy_config_is_parsed_without_credentials(self) -> None:
        raw = """
        HTTPEnable : 1
        HTTPPort : 12639
        HTTPProxy : 127.0.0.1
        HTTPSEnable : 1
        HTTPSPort : 12639
        HTTPSProxy : 127.0.0.1
        """
        self.assertEqual(_parse_macos_proxy_config(raw), "http://127.0.0.1:12639")

    def test_browser_navigation_errors_redact_temporary_access_context(self) -> None:
        redacted = _safe_url_for_error(
            "https://www.xiaohongshu.com/explore/example?xsec_token=temporary-test-value&xsec_source=pc_search"
        )
        self.assertNotIn("temporary-test-value", redacted)
        self.assertIn("xsec_token=%5Bredacted%5D", redacted)


class XhsBridgeSecurityTests(unittest.TestCase):
    def test_bridge_is_loopback_only_and_query_free(self) -> None:
        self.assertEqual(validate_bridge_url("ws://127.0.0.1:9333"), "ws://127.0.0.1:9333")
        with self.assertRaises(ValueError):
            validate_bridge_url("ws://example.com:9333")
        with self.assertRaises(ValueError):
            validate_bridge_url("ws://127.0.0.1:9333?token=secret")

    def test_bridge_rejects_arbitrary_methods_and_parameters(self) -> None:
        with self.assertRaises(ValueError):
            validate_method("evaluate")
        with self.assertRaises(ValueError):
            validate_params("get_xhs_page_state", {"note_id": "ok", "cookie": "secret"})
        with self.assertRaises(ValueError):
            validate_params("get_xhs_page_state", {"note_id": "bad/id"})

    def test_bridge_result_rejects_unapproved_fields(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_result("get_xhs_page_state", {
                "path": "/explore/example",
                "detail_ready": True,
                "cookie": "secret",
            })

    def test_bridge_is_opt_in_and_runtime_extension_has_no_source_token(self) -> None:
        with patch.dict(os.environ, {"XHS_BRIDGE_ENABLED": "0"}, clear=False):
            self.assertEqual(prepare_extension(), "")

    def test_runtime_extension_copies_only_declared_safe_assets(self) -> None:
        with tempfile.TemporaryDirectory() as runtime_dir:
            reset_bridge_runtime()
            with patch.dict(
                os.environ,
                {
                    "XHS_BRIDGE_ENABLED": "1",
                    "XHS_BRIDGE_EXTENSION_RUNTIME_DIR": runtime_dir,
                    "XHS_BRIDGE_TOKEN": "unit-test-pairing-value",
                },
                clear=False,
            ):
                target = Path(prepare_extension())
                self.assertEqual(
                    {path.name for path in target.iterdir()},
                    {"manifest.json", "background.js", "popup.html", "popup.js"},
                )
                manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["action"]["default_popup"], "popup.html")
                self.assertEqual(manifest["permissions"], ["scripting", "tabs"])
                self.assertNotIn("__XHS_BRIDGE_TOKEN__", (target / "background.js").read_text(encoding="utf-8"))
            reset_bridge_runtime()


class XhsJavascriptSyntaxTests(unittest.TestCase):
    def test_all_embedded_xhs_javascript_is_valid(self) -> None:
        scripts = {
            name: value
            for name, value in vars(xhs_module).items()
            if name.endswith("_JS") and isinstance(value, str)
        }
        self.assertTrue(scripts)
        with tempfile.TemporaryDirectory() as tmp_dir:
            for name, source in sorted(scripts.items()):
                script_path = Path(tmp_dir) / f"{name.lower()}.js"
                script_path.write_text(
                    f'"use strict";\nconst candidate = (\n{source}\n);\n',
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    ["node", "--check", str(script_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{name} failed Node syntax validation:\n{completed.stderr}",
                )

    def test_profile_extraction_excludes_state_only_notes_without_dom_href(self) -> None:
        harness_prefix = r'''
"use strict";
const stateOnlyId = "state-only-note";
const domNoteId = "dom-linked-note";
const section = {querySelector: () => null};
const anchor = {
  href: `https://www.xiaohongshu.com/explore/${domNoteId}?xsec_token=temporary-test-value`,
  getAttribute: function () { return this.href; },
  closest: () => section,
  parentElement: section,
  querySelector: () => null,
};
global.location = {href: "https://www.xiaohongshu.com/user/profile/example"};
global.document = {
  querySelector: () => null,
  querySelectorAll: () => [anchor],
};
global.window = {
  __INITIAL_STATE__: {
    user: {
      userPageData: {basicInfo: {nickname: "账号甲", userId: "example"}},
      notes: [
        {id: stateOnlyId, noteCard: {displayTitle: "state only", user: {nickname: "账号甲"}}},
        {id: domNoteId, noteCard: {displayTitle: "dom linked", user: {nickname: "账号甲"}}},
      ],
    },
  },
};
const extractProfile = (
'''
        harness_suffix = r'''
);
process.stdout.write(JSON.stringify(extractProfile("example")));
'''
        with tempfile.TemporaryDirectory() as tmp_dir:
            script_path = Path(tmp_dir) / "profile-dom-filter.js"
            script_path.write_text(
                harness_prefix + xhs_module._EXTRACT_PROFILE_JS + harness_suffix,
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(script_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual([item["note_id"] for item in result["items"]], ["dom-linked-note"])


class FormalScraperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(scraper_app)

    def test_legacy_mock_routes_are_not_registered(self) -> None:
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code, 404)
        self.assertEqual(self.client.get("/api/dashboard/metrics").status_code, 404)
        self.assertEqual(self.client.post("/api/proxies/test", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/reload/xhs").status_code, 404)

    def test_browser_admin_routes_are_disabled(self) -> None:
        self.assertEqual(self.client.post("/api/browser/eval/xhs", json={"expression": "1+1"}).status_code, 403)
        self.assertEqual(self.client.post("/api/browser/goto/xhs", json={"url": "https://example.com"}).status_code, 403)
        self.assertEqual(self.client.post("/api/browser/reset").status_code, 403)
        self.assertEqual(self.client.post("/api/browser/inject-cookie", json={"platform": "xhs", "cookie": "a=b"}).status_code, 403)

    def test_root_reports_formal_mode(self) -> None:
        body = self.client.get("/").json()
        self.assertTrue(body["formal_mode"])
        self.assertFalse(body["legacy_api_enabled"])

    def test_default_meta_backend_is_not_mock(self) -> None:
        self.assertEqual(MetaInfo().backend, "unknown")

    def test_api_key_middleware_returns_structured_401(self) -> None:
        protected = FastAPI()
        protected.add_middleware(APIKeyMiddleware, api_key="expected-secret")
        protected.add_middleware(RequestIDMiddleware)

        @protected.get("/api/check")
        def check() -> dict:
            return {"ok": True}

        client = TestClient(protected)
        denied = client.get("/api/check")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["error"]["code"], "SERVICE_UNAUTHORIZED")
        self.assertTrue(denied.headers.get("X-Request-ID"))
        allowed = client.get("/api/check", headers={"X-API-Key": "expected-secret"})
        self.assertEqual(allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
