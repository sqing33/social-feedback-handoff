"""LiteLLM-backed analysis jobs for the local social feedback panel."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

LOGGER = logging.getLogger("social-feedback-ai")
PROMPT_VERSION = "v1"
DEFAULT_MAX_PENDING_JOBS = 50
DEFAULT_TIMEOUT_SECONDS = 90


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AIConfig:
    enabled: bool
    model: str
    api_key: str
    api_base: str
    timeout_seconds: int
    max_pending_jobs: int

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.model)

    def public_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "model": self.model if self.configured else "",
            "prompt_version": PROMPT_VERSION,
            "max_pending_jobs": self.max_pending_jobs,
            "error": "" if self.configured else "AI_NOT_CONFIGURED",
        }


def load_config() -> AIConfig:
    return AIConfig(
        enabled=_env_bool("AI_ANALYSIS_ENABLED", True),
        model=os.getenv("LITELLM_MODEL", "").strip(),
        api_key=os.getenv("LITELLM_API_KEY", "").strip(),
        api_base=os.getenv("LITELLM_API_BASE", "").strip(),
        timeout_seconds=max(10, int(os.getenv("LITELLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))),
        max_pending_jobs=max(1, int(os.getenv("LITELLM_MAX_PENDING_JOBS", str(DEFAULT_MAX_PENDING_JOBS)))),
    )


def safe_error_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|authorization)[\s=:]+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(xsec_(?:token|source)(?:=|%3D))[^&\s]+", r"\1[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    return text[:500]


def input_fingerprint(payload: Dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_topic(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    scope_type: str,
    scope_id: str,
    payload: Dict[str, Any],
    config: Optional[AIConfig] = None,
) -> Dict[str, Any]:
    config = config or load_config()
    if not config.configured:
        return {"queued": False, "reason": "AI_NOT_CONFIGURED", "job_id": ""}
    fingerprint = input_fingerprint(payload)
    existing = conn.execute(
        """
        SELECT id, status FROM ai_jobs
        WHERE kind = ? AND scope_type = ? AND scope_id = ? AND input_fingerprint = ? AND prompt_version = ?
        ORDER BY queued_at DESC LIMIT 1
        """,
        (kind, scope_type, scope_id, fingerprint, PROMPT_VERSION),
    ).fetchone()
    if existing and existing["status"] in {"queued", "running", "success"}:
        return {"queued": False, "reason": "ALREADY_QUEUED", "job_id": existing["id"], "status": existing["status"]}
    existing_insight = conn.execute(
        """
        SELECT id, generated_at FROM ai_insights
        WHERE kind = ? AND scope_type = ? AND scope_id = ? AND input_fingerprint = ? AND prompt_version = ?
        LIMIT 1
        """,
        (kind, scope_type, scope_id, fingerprint, PROMPT_VERSION),
    ).fetchone()
    if existing_insight:
        return {
            "queued": False,
            "reason": "CACHED",
            "job_id": existing_insight["id"],
            "status": "success",
            "generated_at": existing_insight["generated_at"],
        }
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM ai_jobs WHERE status IN ('queued', 'running')"
    ).fetchone()["c"]
    if int(pending or 0) >= config.max_pending_jobs:
        return {"queued": False, "reason": "QUEUE_CAPACITY", "job_id": ""}
    job_id = f"aij_{uuid.uuid4().hex[:12]}"
    ts = datetime.now().isoformat(timespec="seconds")
    provider = config.model.split("/", 1)[0]
    conn.execute(
        """
        INSERT INTO ai_jobs(
            id, kind, scope_type, scope_id, status, input_fingerprint, attempts,
            provider, model, prompt_version, queued_at, payload_json
        ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            kind,
            scope_type,
            scope_id,
            fingerprint,
            provider,
            config.model,
            PROMPT_VERSION,
            ts,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return {"queued": True, "reason": "", "job_id": job_id, "status": "queued"}


def claim_next_job(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM ai_jobs
            WHERE status = 'queued'
            ORDER BY queued_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        ts = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE ai_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
            (ts, row["id"]),
        )
        conn.commit()
        claimed = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (row["id"],)).fetchone()
        return claimed
    except Exception:
        conn.rollback()
        raise


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    content = getattr(choices[0].message, "content", "")
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI_ANALYSIS_INVALID_JSON")
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("AI_ANALYSIS_INVALID_SCHEMA")
    return value


def _evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, dict):
        archive_id = value.get("archive_id")
        if archive_id:
            found.add(str(archive_id))
        for nested in value.values():
            found.update(_evidence_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_evidence_ids(nested))
    return found


def validate_result(kind: str, result: Dict[str, Any], payload: Dict[str, Any]) -> None:
    allowed_ids = {str(item) for item in payload.get("allowed_archive_ids", [])}
    evidence_ids = _evidence_ids(result.get("evidence", []))
    unknown = evidence_ids - allowed_ids
    if unknown:
        raise ValueError("AI_ANALYSIS_EVIDENCE_MISMATCH")
    if kind == "note_virality":
        if not str(result.get("summary") or "").strip():
            raise ValueError("AI_ANALYSIS_MISSING_SUMMARY")
        for key in ("drivers", "audience_signals", "risks_or_caveats", "next_actions", "topics"):
            if not isinstance(result.get(key), list):
                raise ValueError(f"AI_ANALYSIS_INVALID_FIELD:{key}")


def call_litellm(kind: str, payload: Dict[str, Any], config: Optional[AIConfig] = None) -> Dict[str, Any]:
    config = config or load_config()
    if not config.configured:
        raise RuntimeError("AI_NOT_CONFIGURED")
    try:
        import litellm
    except ImportError as exc:
        raise RuntimeError("AI_DEPENDENCY_MISSING") from exc

    system_prompt = (
        "你是严谨的社交媒体内容分析师。只使用输入中的事实，不得编造数字、URL、账号或引用。"
        "解释爆款时必须使用‘可能原因’，不得断言因果。指标缺失时明确说明样本不足。"
        "输出必须是一个 JSON 对象，不要输出 Markdown。"
    )
    if kind == "note_virality":
        system_prompt += (
            "字段必须包括 summary、drivers、audience_signals、growth_stage、risks_or_caveats、"
            "confidence、evidence、next_actions、topics。topics 最多 5 个，每个为简短中文赛道名。"
            "evidence 只能引用 allowed_archive_ids 中的 archive_id。"
        )
    elif kind == "account_strategy":
        system_prompt += "字段必须包括 summary、content_pillars、viral_patterns、risks、next_topics、evidence。"
    elif kind == "topic_competition":
        system_prompt += "字段必须包括 summary、common_patterns、content_gaps、next_content_ideas、evidence。"
    else:
        raise ValueError(f"AI_ANALYSIS_UNKNOWN_KIND:{kind}")

    kwargs: Dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 1800,
        "timeout": config.timeout_seconds,
    }
    if config.api_key:
        kwargs["api_key"] = config.api_key
    if config.api_base:
        kwargs["api_base"] = config.api_base
    response = litellm.completion(**kwargs)
    result = _parse_json_object(_response_text(response))
    validate_result(kind, result, payload)
    return result


def _store_topics(conn: sqlite3.Connection, archive_id: str, topics: Any) -> None:
    if not isinstance(topics, list):
        return
    ts = datetime.now().isoformat(timespec="seconds")
    for item in topics[:5]:
        topic = str(item if isinstance(item, str) else item.get("topic", "")).strip()
        normalized = normalize_topic(topic)
        confidence = 0.0
        if isinstance(item, dict):
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
            except (TypeError, ValueError):
                confidence = 0.0
        if not topic or not normalized:
            continue
        conn.execute(
            """
            INSERT INTO content_topics(
                id, archive_id, topic, normalized_topic, source, confidence, confirmed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'ai', ?, 0, ?, ?)
            ON CONFLICT(archive_id, normalized_topic) DO UPDATE SET
                topic = excluded.topic,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (f"top_{uuid.uuid4().hex[:12]}", archive_id, topic, normalized, confidence, ts, ts),
        )


def process_job(row: sqlite3.Row, conn: sqlite3.Connection, config: AIConfig) -> None:
    payload = json.loads(row["payload_json"] or "{}")
    result = call_litellm(row["kind"], payload, config)
    ts = datetime.now().isoformat(timespec="seconds")
    insight_id = f"ins_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO ai_insights(
            id, job_id, kind, scope_type, scope_id, input_fingerprint,
            input_snapshot_json, result_json, evidence_json, provider, model,
            prompt_version, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            insight_id,
            row["id"],
            row["kind"],
            row["scope_type"],
            row["scope_id"],
            row["input_fingerprint"],
            json.dumps(payload, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            json.dumps(result.get("evidence", []), ensure_ascii=False),
            row["provider"],
            row["model"],
            row["prompt_version"],
            ts,
        ),
    )
    if row["kind"] == "note_virality" and row["scope_type"] == "archive":
        _store_topics(conn, row["scope_id"], result.get("topics"))
    conn.execute(
        "UPDATE ai_jobs SET status = 'success', finished_at = ?, error = '' WHERE id = ?",
        (ts, row["id"]),
    )


def _worker_loop(connect: Callable[[], sqlite3.Connection], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            with connect() as conn:
                row = claim_next_job(conn)
            if not row:
                stop.wait(1.0)
                continue
            config = load_config()
            if not config.configured:
                with connect() as conn:
                    conn.execute(
                        "UPDATE ai_jobs SET status = 'failed', finished_at = ?, error = 'AI_NOT_CONFIGURED' WHERE id = ?",
                        (datetime.now().isoformat(timespec="seconds"), row["id"]),
                    )
                continue
            try:
                with connect() as conn:
                    process_job(row, conn, config)
            except Exception as exc:
                LOGGER.warning("AI job %s failed: %s", row["id"], type(exc).__name__)
                with connect() as conn:
                    conn.execute(
                        "UPDATE ai_jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                        (datetime.now().isoformat(timespec="seconds"), safe_error_text(exc), row["id"]),
                    )
        except Exception:
            LOGGER.exception("AI worker loop failed")
            stop.wait(2.0)


class AIWorker:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def start(self, connect: Callable[[], sqlite3.Connection]) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=_worker_loop,
                args=(connect, self._stop),
                name="social-feedback-ai",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def reset_for_tests(self) -> None:
        self.stop()
        self._stop = threading.Event()


WORKER = AIWorker()


def recover_pending_jobs(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE ai_jobs SET status = 'queued', started_at = '' WHERE status = 'running'"
    )
    conn.commit()


def start_worker(connect: Callable[[], sqlite3.Connection]) -> None:
    if not WORKER.is_alive():
        recover_pending_jobs(connect())
    WORKER.start(connect)
