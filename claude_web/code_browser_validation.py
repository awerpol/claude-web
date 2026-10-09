from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlparse


RUN_STATUSES = {"queued", "running", "passed", "failed", "skipped", "cancelled"}
TERMINAL_RUN_STATUSES = {"passed", "failed", "skipped", "cancelled"}
RUN_TRANSITIONS = {
    "queued": {"running", "skipped", "cancelled"},
    "running": {"passed", "failed", "skipped", "cancelled"},
}
STEP_ACTIONS = {
    "navigate", "click", "type", "press", "select", "wait_for",
    "scroll", "hover", "screenshot",
}
ASSERTION_TYPES = {
    "visible", "hidden", "text", "value", "url", "count", "attribute",
    "no_console_errors", "network_status",
}
RESULT_STATUSES = {"passed", "failed", "skipped"}
SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

MAX_RECIPE_STEPS = 50
MAX_RECIPE_ASSERTIONS = 50
MAX_SCREENSHOTS = 20
MAX_CONSOLE_ENTRIES = 100
MAX_NETWORK_ENTRIES = 100
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


class CodeBrowserValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CodeBrowserValidationRegistry:
    """Persistent recipes and evidence; browser control remains external."""

    def __init__(self, db_path: Path, *, evidence_root: Optional[Path] = None) -> None:
        self.db_path = Path(db_path)
        self.evidence_root = Path(evidence_root).resolve() if evidence_root else None

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS code_browser_validation_recipes (
                    id TEXT PRIMARY KEY,
                    owner_session_id TEXT NOT NULL,
                    canonical_cwd TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    viewport_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    assertions_json TEXT NOT NULL,
                    server_command_suggestion TEXT NOT NULL DEFAULT '',
                    change_set_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS code_browser_validation_runs (
                    id TEXT PRIMARY KEY,
                    recipe_id TEXT NOT NULL,
                    owner_session_id TEXT NOT NULL,
                    canonical_cwd TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    reason_code TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    change_set_id TEXT NOT NULL DEFAULT '',
                    revision INTEGER,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS code_browser_validation_evidence (
                    run_id TEXT PRIMARY KEY,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_validation_recipe_owner "
                "ON code_browser_validation_recipes (owner_session_id, canonical_cwd, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_browser_validation_run_owner "
                "ON code_browser_validation_runs (owner_session_id, canonical_cwd, updated_at)"
            )

    def create_recipe(
        self,
        session_id: str,
        *,
        name: str,
        url: str,
        viewport: Dict[str, object],
        steps: Sequence[Dict[str, object]],
        assertions: Sequence[Dict[str, object]],
        server_command_suggestion: str = "",
        change_set_id: str = "",
        revision: Optional[int] = None,
    ) -> Dict[str, object]:
        root = self._code_session_root(session_id)
        recipe_id = uuid.uuid4().hex
        normalized = {
            "name": self._text(name, "recipe name", 120, required=True),
            "url": self._url(url),
            "viewport": self._viewport(viewport),
            "steps": self._steps(steps),
            "assertions": self._assertions(assertions),
            "server_command_suggestion": self._text(
                server_command_suggestion, "server command suggestion", 1000
            ),
            "change_set_id": self._text(change_set_id, "change set ID", 160),
            "revision": self._revision(revision),
        }
        now = time.time()
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO code_browser_validation_recipes (
                    id, owner_session_id, canonical_cwd, name, url, viewport_json,
                    steps_json, assertions_json, server_command_suggestion,
                    change_set_id, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recipe_id, session_id, str(root), normalized["name"], normalized["url"],
                    json.dumps(normalized["viewport"], ensure_ascii=False),
                    json.dumps(normalized["steps"], ensure_ascii=False),
                    json.dumps(normalized["assertions"], ensure_ascii=False),
                    normalized["server_command_suggestion"], normalized["change_set_id"],
                    normalized["revision"], now, now,
                ),
            )
        return self.get_recipe(session_id, recipe_id)

    def list_recipes(self, session_id: str) -> List[Dict[str, object]]:
        root = self._code_session_root(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM code_browser_validation_recipes
                WHERE owner_session_id = ? AND canonical_cwd = ?
                ORDER BY updated_at DESC
                """,
                (session_id, str(root)),
            ).fetchall()
        return [self._recipe_payload(dict(row)) for row in rows]

    def get_recipe(self, session_id: str, recipe_id: str) -> Dict[str, object]:
        root = self._code_session_root(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM code_browser_validation_recipes WHERE id = ?",
                (recipe_id,),
            ).fetchone()
        if row is None:
            raise CodeBrowserValidationError("recipe_not_found", "Recipe приёмочного теста браузера не существует")
        self._require_owner(session_id, root, row)
        return self._recipe_payload(dict(row))

    def create_run(
        self,
        session_id: str,
        recipe_id: str,
        *,
        change_set_id: Optional[str] = None,
        revision: Optional[int] = None,
    ) -> Dict[str, object]:
        recipe = self.get_recipe(session_id, recipe_id)
        root = self._code_session_root(session_id)
        run_id = uuid.uuid4().hex
        resolved_change_set = (
            recipe["change_set_id"]
            if change_set_id is None
            else self._text(change_set_id, "change set ID", 160)
        )
        resolved_revision = recipe["revision"] if revision is None else self._revision(revision)
        now = time.time()
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO code_browser_validation_runs (
                    id, recipe_id, owner_session_id, canonical_cwd, status,
                    change_set_id, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id, recipe_id, session_id, str(root), resolved_change_set,
                    resolved_revision, now, now,
                ),
            )
        return self.get_run(session_id, run_id)

    def list_runs(self, session_id: str, *, recipe_id: str = "") -> List[Dict[str, object]]:
        root = self._code_session_root(session_id)
        query = (
            "SELECT * FROM code_browser_validation_runs "
            "WHERE owner_session_id = ? AND canonical_cwd = ?"
        )
        params: List[object] = [session_id, str(root)]
        if recipe_id:
            query += " AND recipe_id = ?"
            params.append(recipe_id)
        query += " ORDER BY created_at DESC"
        with self.connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._run_payload(dict(row), self._evidence_for(str(row["id"]))) for row in rows]

    def get_run(self, session_id: str, run_id: str) -> Dict[str, object]:
        root = self._code_session_root(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM code_browser_validation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise CodeBrowserValidationError("run_not_found", "Запуск приёмочного теста браузера не существует")
        self._require_owner(session_id, root, row)
        return self._run_payload(dict(row), self._evidence_for(run_id))

    def transition_run(
        self,
        session_id: str,
        run_id: str,
        status: str,
        *,
        reason_code: str = "",
        reason: str = "",
    ) -> Dict[str, object]:
        run = self.get_run(session_id, run_id)
        target = str(status or "").strip()
        if target not in RUN_STATUSES or target not in RUN_TRANSITIONS.get(str(run["status"]), set()):
            raise CodeBrowserValidationError("invalid_run_transition", "Недействительный переход состояния запуска приёмочного теста браузера")
        normalized_reason_code = self._text(reason_code, "reason code", 80)
        normalized_reason = self._text(reason, "reason", 1000)
        if target == "failed" and normalized_reason_code in {
            "browser_unavailable", "extension_unavailable", "server_unavailable",
        }:
            raise CodeBrowserValidationError(
                "unavailable_must_skip", "При недоступности среды проверки необходимо записать как skipped"
            )
        if target == "passed":
            evidence = run.get("evidence")
            if not evidence:
                raise CodeBrowserValidationError("evidence_required", "Перед прохождением проверки необходимо записать доказательства")
            recipe = self.get_recipe(session_id, str(run["recipe_id"]))
            failed = any(
                item.get("status") == "failed"
                for item in [
                    *(evidence.get("step_results") or []),
                    *(evidence.get("assertion_results") or []),
                ]
            )
            if failed:
                raise CodeBrowserValidationError("failed_evidence", "Есть доказательства сбоя, нельзя отметить как passed")
            expected_assertions = {item["id"] for item in recipe["assertions"]}
            passed_assertions = {
                item["assertion_id"]
                for item in evidence.get("assertion_results") or []
                if item.get("status") == "passed"
            }
            if not expected_assertions.issubset(passed_assertions):
                raise CodeBrowserValidationError(
                    "assertions_incomplete", "Не все утверждения Recipe пройдены"
                )
        now = time.time()
        started_at = now if target == "running" and not run.get("started_at") else run.get("started_at")
        completed_at = now if target in TERMINAL_RUN_STATUSES else None
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                UPDATE code_browser_validation_runs
                SET status = ?, reason_code = ?, reason = ?, started_at = ?,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target, normalized_reason_code, normalized_reason,
                    started_at, completed_at, now, run_id,
                ),
            )
        return self.get_run(session_id, run_id)

    def mark_unavailable(self, session_id: str, run_id: str, reason: str) -> Dict[str, object]:
        return self.transition_run(
            session_id,
            run_id,
            "skipped",
            reason_code="browser_unavailable",
            reason=reason,
        )

    def record_evidence(
        self,
        session_id: str,
        run_id: str,
        *,
        step_results: Sequence[Dict[str, object]],
        assertion_results: Sequence[Dict[str, object]],
        screenshots: Sequence[str] = (),
        console_summary: Sequence[Dict[str, object]] = (),
        network_summary: Sequence[Dict[str, object]] = (),
    ) -> Dict[str, object]:
        run = self.get_run(session_id, run_id)
        if run["status"] != "running":
            raise CodeBrowserValidationError("run_not_recording", "Текущее состояние запуска не позволяет записывать доказательства проверки")
        root = self._code_session_root(session_id)
        recipe = self.get_recipe(session_id, str(run["recipe_id"]))
        normalized_steps = self._results(step_results, "step")
        normalized_assertions = self._results(assertion_results, "assertion")
        known_step_ids = {item["id"] for item in recipe["steps"]}
        known_assertion_ids = {item["id"] for item in recipe["assertions"]}
        if any(item["step_id"] not in known_step_ids for item in normalized_steps):
            raise CodeBrowserValidationError("invalid_evidence", "Доказательство ссылается на неизвестный шаг Recipe")
        if any(item["assertion_id"] not in known_assertion_ids for item in normalized_assertions):
            raise CodeBrowserValidationError("invalid_evidence", "Доказательство ссылается на неизвестное утверждение Recipe")
        evidence = {
            "step_results": normalized_steps,
            "assertion_results": normalized_assertions,
            "screenshots": self._screenshots(root, screenshots),
            "console_summary": self._console(console_summary),
            "network_summary": self._network(network_summary),
        }
        try:
            encoded = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CodeBrowserValidationError("invalid_evidence", "Доказательства приёмочного теста браузера должны быть сериализуемыми") from exc
        if len(encoded.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise CodeBrowserValidationError("evidence_too_large", "Доказательства приёмочного теста браузера превышают ограничение размера")
        now = time.time()
        with self.connect(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO code_browser_validation_evidence
                    (run_id, evidence_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (run_id, encoded, now, now),
            )
        return self.get_run(session_id, run_id)

    def _code_session_root(self, session_id: str) -> Path:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, cwd, workspace_mode FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise CodeBrowserValidationError("session_not_found", "Сессия Code не существует")
        if str(row["workspace_mode"] or "chat") != "code":
            raise CodeBrowserValidationError("code_session_required", "Приёмочный тест браузера поддерживается только в сессиях Code")
        raw_cwd = str(row["cwd"] or "").strip()
        if not raw_cwd:
            raise CodeBrowserValidationError("project_required", "Сессия Code ещё не привязана к каталогу проекта")
        root = Path(os.path.expanduser(raw_cwd)).resolve()
        if not root.is_dir() or root in {Path.home().resolve(), Path(root.anchor)}:
            raise CodeBrowserValidationError("project_required", "Каталог проекта сессии Code недоступен")
        return root

    @staticmethod
    def _require_owner(session_id: str, root: Path, row: sqlite3.Row) -> None:
        if str(row["owner_session_id"]) != session_id:
            raise CodeBrowserValidationError("validation_forbidden", "Запись проверки не принадлежит текущей сессии Code")
        if str(row["canonical_cwd"]) != str(root):
            raise CodeBrowserValidationError("workspace_changed", "Каталог проекта сессии Code изменился")

    @staticmethod
    def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
        text = str(value or "").strip()
        if required and not text:
            raise CodeBrowserValidationError("invalid_recipe", f"{field} не может быть пустым")
        if len(text) > limit:
            raise CodeBrowserValidationError("value_too_large", f"{field} превышает ограничение длины")
        return text

    @classmethod
    def _url(cls, value: object) -> str:
        text = cls._text(value, "URL", 2048, required=True)
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CodeBrowserValidationError("invalid_url", "URL проверки поддерживает только http/https")
        if parsed.username or parsed.password:
            raise CodeBrowserValidationError("invalid_url", "URL проверки не может содержать встроенные учётные данные")
        return text

    @staticmethod
    def _viewport(value: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(value, dict) or set(value) - {"width", "height", "device_scale_factor"}:
            raise CodeBrowserValidationError("invalid_viewport", "Недействительное поле viewport")
        try:
            width = int(value.get("width") or 0)
            height = int(value.get("height") or 0)
            scale = float(value.get("device_scale_factor") or 1)
        except (TypeError, ValueError) as exc:
            raise CodeBrowserValidationError("invalid_viewport", "Недействительное значение viewport") from exc
        if not 320 <= width <= 3840 or not 320 <= height <= 2160 or not 0.5 <= scale <= 4:
            raise CodeBrowserValidationError("invalid_viewport", "viewport вне допустимого диапазона")
        return {"width": width, "height": height, "device_scale_factor": scale}

    @classmethod
    def _steps(cls, values: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_RECIPE_STEPS:
            raise CodeBrowserValidationError("invalid_steps", "Recipe должен содержать от 1 до 50 шагов")
        allowed = {"id", "title", "action", "target", "value", "timeout_ms"}
        result = []
        for index, item in enumerate(values, 1):
            if not isinstance(item, dict) or set(item) - allowed:
                raise CodeBrowserValidationError("invalid_steps", "Недействительное поле шага Recipe")
            action = cls._text(item.get("action"), "step action", 40, required=True)
            if action not in STEP_ACTIONS:
                raise CodeBrowserValidationError("invalid_steps", "Недействительное действие шага Recipe")
            try:
                timeout_ms = int(item.get("timeout_ms") or 10_000)
            except (TypeError, ValueError) as exc:
                raise CodeBrowserValidationError("invalid_steps", "Недействительный таймаут шага Recipe") from exc
            if not 0 <= timeout_ms <= 120_000:
                raise CodeBrowserValidationError("invalid_steps", "Недействительный таймаут шага Recipe")
            result.append({
                "id": cls._text(item.get("id") or f"step-{index}", "step ID", 80, required=True),
                "title": cls._text(item.get("title"), "step title", 160),
                "action": action,
                "target": cls._text(item.get("target"), "step target", 1000),
                "value": cls._text(item.get("value"), "step value", 4000),
                "timeout_ms": timeout_ms,
            })
        if len({item["id"] for item in result}) != len(result):
            raise CodeBrowserValidationError("invalid_steps", "ID шагов Recipe не должны повторяться")
        return result

    @classmethod
    def _assertions(cls, values: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        if not isinstance(values, (list, tuple)) or len(values) > MAX_RECIPE_ASSERTIONS:
            raise CodeBrowserValidationError("invalid_assertions", "Recipe может содержать не более 50 утверждений")
        allowed = {"id", "title", "type", "target", "expected", "timeout_ms"}
        result = []
        for index, item in enumerate(values, 1):
            if not isinstance(item, dict) or set(item) - allowed:
                raise CodeBrowserValidationError("invalid_assertions", "Недействительное поле утверждения Recipe")
            assertion_type = cls._text(item.get("type"), "assertion type", 40, required=True)
            if assertion_type not in ASSERTION_TYPES:
                raise CodeBrowserValidationError("invalid_assertions", "Недействительный тип утверждения Recipe")
            try:
                timeout_ms = int(item.get("timeout_ms") or 10_000)
            except (TypeError, ValueError) as exc:
                raise CodeBrowserValidationError("invalid_assertions", "Недействительный таймаут утверждения Recipe") from exc
            if not 0 <= timeout_ms <= 120_000:
                raise CodeBrowserValidationError("invalid_assertions", "Недействительный таймаут утверждения Recipe")
            expected = item.get("expected")
            try:
                expected_json = json.dumps(expected, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise CodeBrowserValidationError("invalid_assertions", "Недействительное ожидаемое значение утверждения Recipe") from exc
            if len(expected_json) > 4000:
                raise CodeBrowserValidationError("invalid_assertions", "Ожидаемое значение утверждения Recipe слишком велико")
            result.append({
                "id": cls._text(item.get("id") or f"assertion-{index}", "assertion ID", 80, required=True),
                "title": cls._text(item.get("title"), "assertion title", 160),
                "type": assertion_type,
                "target": cls._text(item.get("target"), "assertion target", 1000),
                "expected": expected,
                "timeout_ms": timeout_ms,
            })
        if len({item["id"] for item in result}) != len(result):
            raise CodeBrowserValidationError("invalid_assertions", "ID утверждений Recipe не должны повторяться")
        return result

    @staticmethod
    def _revision(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        try:
            revision = int(value)
        except (TypeError, ValueError) as exc:
            raise CodeBrowserValidationError("invalid_revision", "revision должен быть целым числом") from exc
        if revision < 0:
            raise CodeBrowserValidationError("invalid_revision", "revision не может быть отрицательным")
        return revision

    @classmethod
    def _results(cls, values: Sequence[Dict[str, object]], kind: str) -> List[Dict[str, object]]:
        limit = MAX_RECIPE_STEPS if kind == "step" else MAX_RECIPE_ASSERTIONS
        if not isinstance(values, (list, tuple)) or len(values) > limit:
            raise CodeBrowserValidationError("invalid_evidence", f"Количество результатов {kind} превышает ограничение")
        allowed = {f"{kind}_id", "status", "duration_ms", "detail", "actual", "expected"}
        result = []
        for item in values:
            if not isinstance(item, dict) or set(item) - allowed:
                raise CodeBrowserValidationError("invalid_evidence", f"Недействительное поле результата {kind}")
            item_id = cls._text(item.get(f"{kind}_id"), f"{kind} result ID", 80, required=True)
            status = cls._text(item.get("status"), f"{kind} result status", 20, required=True)
            if status not in RESULT_STATUSES:
                raise CodeBrowserValidationError("invalid_evidence", f"Недействительное состояние результата {kind}")
            try:
                duration_ms = int(item.get("duration_ms") or 0)
            except (TypeError, ValueError) as exc:
                raise CodeBrowserValidationError("invalid_evidence", f"Недействительное время выполнения результата {kind}") from exc
            if not 0 <= duration_ms <= 3_600_000:
                raise CodeBrowserValidationError("invalid_evidence", f"Недействительное время выполнения результата {kind}")
            payload = {
                f"{kind}_id": item_id,
                "status": status,
                "duration_ms": duration_ms,
                "detail": cls._text(item.get("detail"), f"{kind} result detail", 4000),
            }
            if kind == "assertion":
                payload["actual"] = item.get("actual")
                payload["expected"] = item.get("expected")
            result.append(payload)
        id_field = f"{kind}_id"
        if len({item[id_field] for item in result}) != len(result):
            raise CodeBrowserValidationError("invalid_evidence", f"ID результатов {kind} не должны повторяться")
        return result

    def _screenshots(self, root: Path, values: Sequence[str]) -> List[str]:
        if not isinstance(values, (list, tuple)) or len(values) > MAX_SCREENSHOTS:
            raise CodeBrowserValidationError("invalid_evidence", "Количество скриншотов превышает ограничение")
        allowed_roots = [root]
        if self.evidence_root:
            allowed_roots.append(self.evidence_root)
        result = []
        for value in values:
            raw = Path(os.path.expanduser(str(value or "")))
            target = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
            if target.suffix.casefold() not in SCREENSHOT_SUFFIXES:
                raise CodeBrowserValidationError("invalid_screenshot", "Недействительный тип файла скриншота")
            if not any(self._is_within(target, allowed) for allowed in allowed_roots):
                raise CodeBrowserValidationError("invalid_screenshot", "Скриншот должен находиться в проекте или указанном каталоге доказательств")
            if not target.is_file() or target.stat().st_size > MAX_SCREENSHOT_BYTES:
                raise CodeBrowserValidationError("invalid_screenshot", "Скриншот не существует или превышает ограничение размера")
            result.append(str(target))
        return list(dict.fromkeys(result))

    @classmethod
    def _console(cls, values: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        if not isinstance(values, (list, tuple)) or len(values) > MAX_CONSOLE_ENTRIES:
            raise CodeBrowserValidationError("invalid_evidence", "Количество сводок Console превышает ограничение")
        result = []
        for item in values:
            if not isinstance(item, dict) or set(item) - {"level", "message", "source"}:
                raise CodeBrowserValidationError("invalid_evidence", "Недействительное поле сводки Console")
            result.append({
                "level": cls._text(item.get("level"), "console level", 20, required=True),
                "message": cls._text(item.get("message"), "console message", 2000, required=True),
                "source": cls._text(item.get("source"), "console source", 1000),
            })
        return result

    @classmethod
    def _network(cls, values: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        if not isinstance(values, (list, tuple)) or len(values) > MAX_NETWORK_ENTRIES:
            raise CodeBrowserValidationError("invalid_evidence", "Количество сводок Network превышает ограничение")
        result = []
        for item in values:
            if not isinstance(item, dict) or set(item) - {"method", "url", "status", "summary"}:
                raise CodeBrowserValidationError("invalid_evidence", "Недействительное поле сводки Network")
            try:
                status = int(item.get("status") or 0)
            except (TypeError, ValueError) as exc:
                raise CodeBrowserValidationError("invalid_evidence", "Недействительный код состояния Network") from exc
            if not 0 <= status <= 599:
                raise CodeBrowserValidationError("invalid_evidence", "Недействительный код состояния Network")
            result.append({
                "method": cls._text(item.get("method") or "GET", "network method", 16, required=True),
                "url": cls._text(item.get("url"), "network URL", 2048, required=True),
                "status": status,
                "summary": cls._text(item.get("summary"), "network summary", 2000),
            })
        return result

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _recipe_payload(row: Dict[str, object]) -> Dict[str, object]:
        return {
            "id": row["id"],
            "owner_session_id": row["owner_session_id"],
            "canonical_cwd": row["canonical_cwd"],
            "name": row["name"],
            "url": row["url"],
            "viewport": json.loads(str(row["viewport_json"])),
            "steps": json.loads(str(row["steps_json"])),
            "assertions": json.loads(str(row["assertions_json"])),
            "server_command_suggestion": row["server_command_suggestion"],
            "change_set_id": row["change_set_id"],
            "revision": int(row["revision"]) if row["revision"] is not None else None,
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _run_payload(row: Dict[str, object], evidence: Optional[Dict[str, object]]) -> Dict[str, object]:
        return {
            "id": row["id"],
            "recipe_id": row["recipe_id"],
            "owner_session_id": row["owner_session_id"],
            "canonical_cwd": row["canonical_cwd"],
            "status": row["status"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "change_set_id": row["change_set_id"],
            "revision": int(row["revision"]) if row["revision"] is not None else None,
            "created_at": float(row["created_at"]),
            "started_at": float(row["started_at"]) if row["started_at"] is not None else None,
            "completed_at": float(row["completed_at"]) if row["completed_at"] is not None else None,
            "updated_at": float(row["updated_at"]),
            "evidence": evidence,
        }

    def _evidence_for(self, run_id: str) -> Optional[Dict[str, object]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT evidence_json FROM code_browser_validation_evidence WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(str(row["evidence_json"])) if row else None
