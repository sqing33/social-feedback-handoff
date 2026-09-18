from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PANEL_DIR = ROOT / "panel"
sys.path.insert(0, str(PANEL_DIR))

os.environ.setdefault("FORMAL_MODE", "1")
os.environ.setdefault("ALLOW_LEGACY_WRITE_API", "0")

import ai_service  # noqa: E402
import social_feedback_backend as backend  # noqa: E402


class AnalyticsPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = backend.DB_PATH
        backend.DB_PATH = Path(self.tmp.name) / "analytics.db"
        backend.init_db()
        self.client = backend.app.test_client()

    def tearDown(self) -> None:
        backend.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def snapshot(
        self,
        content_id: str,
        *,
        account: str = "账号甲",
        title: str = "真实内容",
        metrics: dict | None = None,
        captured_at: str | None = None,
        published_date: str | None = None,
        shares: int | None = None,
    ) -> dict:
        url = f"https://www.bilibili.com/video/{content_id}"
        return {
            "status": "fetched",
            "platform": "B站",
            "title": title,
            "description": f"{title}的公开摘要",
            "author": account,
            "url": url,
            "published_at": f"{published_date or datetime.now().date().isoformat()}T12:00:00",
            "published_date": published_date or datetime.now().date().isoformat(),
            "published_time": "12:00",
            "captured_at": captured_at or datetime.now().isoformat(timespec="seconds"),
            "metrics": metrics or {"views": 100, "likes": 10, "saves": 2, "comments": 1, "shares": shares},
            "extra_metrics": {"shares": shares} if shares is not None else {},
            "comments_data": [{"content": "真实公开评论", "like_count": 1, "create_time_text": "今天"}],
        }

    def test_account_totals_use_latest_feedback_and_preserve_shares(self) -> None:
        first = self.snapshot("BVfirst", title="第一条", metrics={"views": 100, "likes": 10, "saves": 2, "comments": 1, "shares": 0})
        second = self.snapshot("BVsecond", title="第二条", metrics={"views": 200, "likes": 20, "saves": 4, "comments": 2, "shares": 7})
        with sqlite3.connect(backend.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            first_id = backend.save_note_snapshot(conn, first["url"], first)
            backend.save_note_snapshot(conn, second["url"], second)
            updated_first = self.snapshot("BVfirst", title="第一条", metrics={"views": 150, "likes": 15, "saves": 3, "comments": 3, "shares": 5})
            backend.save_note_snapshot(conn, updated_first["url"], updated_first, source="refresh")
            conn.commit()
            backend.seed_accounts_from_feedback()
            response = self.client.get("/api/accounts/metrics")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        item = body["items"][0]
        self.assertEqual(item["notes_count"], 2)
        self.assertEqual(item["views"], 350)
        self.assertEqual(item["likes"], 35)
        self.assertEqual(item["saves"], 7)
        self.assertEqual(item["comments"], 5)
        self.assertEqual(item["shares"], 12)
        self.assertEqual(item["engagement"], 59)
        with sqlite3.connect(backend.DB_PATH) as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM metric_history").fetchone()[0], 2)

    def test_null_metric_is_not_converted_to_zero(self) -> None:
        item = self.snapshot("BVnull", metrics={"views": None, "likes": 5, "saves": None, "comments": 0, "shares": None})
        with sqlite3.connect(backend.DB_PATH) as conn:
            backend.save_note_snapshot(conn, item["url"], item)
            conn.commit()
            backend.seed_accounts_from_feedback()
        metrics = self.client.get("/api/accounts/metrics").get_json()["items"][0]
        self.assertIsNone(metrics["views"])
        self.assertIsNone(metrics["saves"])
        self.assertIsNone(metrics["shares"])
        self.assertEqual(metrics["likes"], 5)
        self.assertEqual(metrics["comments"], 0)
        self.assertEqual(metrics["metric_coverage"]["likes"]["notes_with_value"], 1)
        self.assertEqual(metrics["metric_coverage"]["shares"]["notes_with_value"], 0)

    def test_dashboard_trend_has_fixed_zero_filled_window(self) -> None:
        today = datetime.now().date().isoformat()
        item = self.snapshot("BVtrend", published_date=today)
        with sqlite3.connect(backend.DB_PATH) as conn:
            backend.save_note_snapshot(conn, item["url"], item)
            conn.commit()
        body = self.client.get("/api/dashboard/overview?days=14").get_json()
        self.assertEqual(len(body["trend"]), 14)
        self.assertEqual(body["trend"][-1]["new_notes"], 1)
        self.assertEqual(body["trend"][-1]["current_engagement"], 13)
        self.assertTrue(all(item["new_notes"] == 0 for item in body["trend"][:-1]))

    def test_high_growth_requires_two_real_samples(self) -> None:
        first_time = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        first = self.snapshot("BVgrowth", captured_at=first_time, metrics={"views": 100, "likes": 10, "saves": 2, "comments": 1, "shares": 0})
        second = self.snapshot("BVgrowth", metrics={"views": 180, "likes": 30, "saves": 8, "comments": 4, "shares": 6})
        with sqlite3.connect(backend.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            backend.save_note_snapshot(conn, first["url"], first)
            backend.save_note_snapshot(conn, second["url"], second, source="refresh")
            conn.commit()
        items = self.client.get("/api/dashboard/high-growth?days=14").get_json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["engagement_delta"], 35)
        self.assertIsNotNone(items[0]["engagement_growth_rate"])

    def test_auto_analysis_is_queued_only_for_new_imports(self) -> None:
        item = self.snapshot("BVauto")
        config = ai_service.AIConfig(True, "test/model", "key", "", 30, 50)
        with patch.dict(os.environ, {"LITELLM_MODEL": "test/model", "LITELLM_API_KEY": "key"}), \
             patch.object(backend, "capture_link_snapshot", return_value=item), \
             patch.object(backend, "attach_real_comments", side_effect=lambda value, *_args, **_kwargs: value):
            first = self.client.post("/api/notes/import", json={"urls": [item["url"]]})
            second = self.client.post("/api/notes/import", json={"urls": [item["url"]]})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        with sqlite3.connect(backend.DB_PATH) as conn:
            queued = conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE status = 'queued'").fetchone()[0]
            notes = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
        self.assertEqual(notes, 1)
        self.assertEqual(queued, 2)

    def test_topic_labels_require_confirmation_before_topic_analysis(self) -> None:
        first = self.snapshot("BVtopic1", title="选题一")
        second = self.snapshot("BVtopic2", title="选题二")
        with sqlite3.connect(backend.DB_PATH) as conn:
            first_id = backend.save_note_snapshot(conn, first["url"], first)
            second_id = backend.save_note_snapshot(conn, second["url"], second)
            conn.commit()
            topic_ids = []
            for archive_id in (first_id, second_id):
                topic_id = f"top_{archive_id}"
                conn.execute(
                    """
                    INSERT INTO content_topics(
                        id, archive_id, topic, normalized_topic, source, confidence, confirmed, created_at, updated_at
                    ) VALUES (?, ?, '人工智能', '人工智能', 'ai', .9, 0, ?, ?)
                    """,
                    (topic_id, archive_id, datetime.now().isoformat(), datetime.now().isoformat()),
                )
                topic_ids.append(topic_id)
            conn.commit()
        with patch.dict(os.environ, {"LITELLM_MODEL": "test/model", "LITELLM_API_KEY": "key"}):
            first_response = self.client.patch(f"/api/content-topics/{topic_ids[0]}", json={"confirmed": True})
            second_response = self.client.patch(f"/api/content-topics/{topic_ids[1]}", json={"confirmed": True})
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.get_json()["topic_analysis"]["reason"], "TOPIC_SAMPLE_TOO_SMALL")
        self.assertTrue(second_response.get_json()["topic_analysis"]["queued"])

    def test_ai_result_rejects_unknown_evidence_and_stores_unconfirmed_topics(self) -> None:
        payload = {"allowed_archive_ids": ["arc_ok"]}
        with self.assertRaisesRegex(ValueError, "EVIDENCE_MISMATCH"):
            ai_service.validate_result("note_virality", {
                "summary": "可能因为标题清晰",
                "drivers": [], "audience_signals": [], "risks_or_caveats": [],
                "next_actions": [], "topics": [], "evidence": [{"archive_id": "arc_fake"}],
            }, payload)
        config = ai_service.AIConfig(True, "test/model", "key", "", 30, 50)
        snapshot = self.snapshot("BVai")
        with sqlite3.connect(backend.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            archive_id = backend.save_note_snapshot(conn, snapshot["url"], snapshot)
            enqueue = ai_service.enqueue_job(
                conn,
                kind="note_virality",
                scope_type="archive",
                scope_id=archive_id,
                payload={"allowed_archive_ids": [archive_id]},
                config=config,
            )
            conn.commit()
            job = ai_service.claim_next_job(conn)
            result = {
                "summary": "标题钩子可能提高了点击意愿",
                "drivers": ["标题钩子"], "audience_signals": ["求链接"],
                "risks_or_caveats": ["只有单次采集"], "next_actions": ["继续采样"],
                "topics": ["人工智能"], "evidence": [{"archive_id": archive_id}], "confidence": .7,
            }
            with patch.object(ai_service, "call_litellm", return_value=result):
                ai_service.process_job(job, conn, config)
            conn.commit()
            insight = conn.execute("SELECT status FROM ai_jobs WHERE id = ?", (job["id"],)).fetchone()[0]
            topic = conn.execute("SELECT confirmed FROM content_topics WHERE archive_id = ?", (archive_id,)).fetchone()
        self.assertTrue(enqueue["queued"])
        self.assertEqual(insight, "success")
        self.assertEqual(topic[0], 0)

    def test_dashboard_filters_apply_to_accounts_and_confirmed_topics(self) -> None:
        first_time = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
        item = self.snapshot("BVfilter", captured_at=first_time, metrics={"views": 100, "likes": 10, "saves": 1, "comments": 1, "shares": 0})
        with sqlite3.connect(backend.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            archive_id = backend.save_note_snapshot(conn, item["url"], item)
            backend.save_note_snapshot(conn, item["url"], self.snapshot("BVfilter", metrics={"views": 200, "likes": 30, "saves": 5, "comments": 3, "shares": 2}), source="refresh")
            conn.commit()
            backend.seed_accounts_from_feedback()
            account = conn.execute("SELECT id FROM accounts WHERE account_name = '账号甲'").fetchone()
            conn.execute(
                """
                INSERT INTO content_topics(
                    id, archive_id, topic, normalized_topic, source, confidence, confirmed, created_at, updated_at
                ) VALUES (?, ?, '人工智能', '人工智能', 'manual', 1, 1, ?, ?)
                """,
                (f"top_filter_{archive_id}", archive_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            conn.commit()
        account_response = self.client.get(f"/api/dashboard/high-growth?account_id={account['id']}")
        topic_response = self.client.get("/api/dashboard/overview?topic=人工智能")
        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(topic_response.status_code, 200)
        self.assertEqual(len(account_response.get_json()["items"]), 1)
        self.assertEqual(topic_response.get_json()["kpi"]["monitored_accounts"], 1)

    def test_ai_error_text_redacts_keys_and_temporary_tokens(self) -> None:
        message = ai_service.safe_error_text(
            "Authorization: Bearer secret-value api_key=sk-abcdefgh123456 "
            "https://example.com?xsec_token=temporary-value&xsec_source=pc_search"
        )
        self.assertNotIn("secret-value", message)
        self.assertNotIn("sk-abcdefgh123456", message)
        self.assertNotIn("temporary-value", message)
        self.assertIn("[redacted]", message)

    def test_dashboard_html_and_javascript_are_present(self) -> None:
        html = (PANEL_DIR / "social-feedback-panel.html").read_text(encoding="utf-8")
        for marker in ('data-view="dashboard"', 'id="dashboardPanel"', 'renderTrendChart', 'loadDashboard', 'AI 洞察'):
            self.assertIn(marker, html)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "panel.js"
            script.write_text(html.split("<script>", 1)[1].split("</script>", 1)[0], encoding="utf-8")
            result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
