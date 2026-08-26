from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from typing import Protocol

from .formats import placeholders_match


class Translator(Protocol):
    def translate_many(self, values: list[str]) -> list[str]: ...


class AdaptiveLimiter:
    def __init__(self, maximum: int):
        self.maximum = max(1, maximum); self.limit = self.maximum; self.active = 0; self.successes = 0
        self.condition = threading.Condition()
    def __enter__(self):
        with self.condition:
            while self.active >= self.limit: self.condition.wait()
            self.active += 1
        return self
    def __exit__(self, *_):
        with self.condition: self.active -= 1; self.condition.notify_all()
    def success(self):
        with self.condition:
            self.successes += 1
            if self.successes >= 20 and self.limit < self.maximum:
                self.limit += 1; self.successes = 0; self.condition.notify_all()
    def throttle(self):
        with self.condition:
            self.limit = max(1, self.limit // 2); self.successes = 0


class IdentityTranslator:
    """Offline translator used for extraction-only runs."""

    def translate_many(self, values: list[str]) -> list[str]:
        return list(values)


class TranslationCache:
    def __init__(self, path: str | Path = "translation_cache.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS translations (cache_key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(translations)")}
            if "model" not in columns:
                db.execute("ALTER TABLE translations ADD COLUMN model TEXT")
            if "source" not in columns:
                db.execute("ALTER TABLE translations ADD COLUMN source TEXT")
            if "namespace" not in columns:
                db.execute("ALTER TABLE translations ADD COLUMN namespace TEXT")
            if "locked" not in columns:
                db.execute("ALTER TABLE translations ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS contextual_translations (
                       cache_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
                       item_key TEXT NOT NULL, source TEXT NOT NULL, value TEXT NOT NULL,
                       model TEXT, namespace TEXT, locked INTEGER NOT NULL DEFAULT 0)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def key(model: str, text: str, namespace: str = "") -> str:
        return hashlib.sha256(f"{namespace}\0{model}\0{text}".encode()).hexdigest()

    def get(self, model: str, text: str, namespace: str = "") -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM translations WHERE cache_key=?",
                             (self.key(model, text, namespace),)).fetchone()
        return row[0] if row else None

    def put(self, model: str, text: str, value: str, namespace: str = "") -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO translations (cache_key, value, model, source, namespace)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, model=excluded.model,
                        source=excluded.source, namespace=excluded.namespace
                        WHERE translations.locked=0""",
                       (self.key(model, text, namespace), value, model, text, namespace))

    @staticmethod
    def context_key(model: str, namespace: str, kind: str, path: str, item_key: str, source: str) -> str:
        identity = "\0".join((namespace, model, kind, path.replace("\\", "/").lower(), item_key, source))
        return hashlib.sha256(identity.encode()).hexdigest()

    def get_context(self, model: str, namespace: str, kind: str, path: str,
                    item_key: str, source: str) -> str | None:
        key = self.context_key(model, namespace, kind, path, item_key, source)
        with self._connect() as db:
            row = db.execute("SELECT value FROM contextual_translations WHERE cache_key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_context(self, model: str, namespace: str, kind: str, path: str,
                    item_key: str, source: str, value: str) -> None:
        normalized = path.replace("\\", "/").lower()
        key = self.context_key(model, namespace, kind, normalized, item_key, source)
        with self._connect() as db:
            db.execute("""INSERT INTO contextual_translations
                       (cache_key, kind, path, item_key, source, value, model, namespace)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, model=excluded.model,
                       namespace=excluded.namespace WHERE contextual_translations.locked=0""",
                       (key, kind, normalized, item_key, source, value, model, namespace))

    def put_context_many(self, model: str, namespace: str,
                         records: list[tuple[str, str, str, str, str]]) -> None:
        """Store contextual translations in one transaction.

        Each record is ``(kind, path, item_key, source, value)``.  A single
        connection/commit avoids thousands of Windows SQLite fsyncs during
        structured-file writeback.
        """
        rows = []
        for kind, path, item_key, source, value in records:
            normalized = path.replace("\\", "/").lower()
            key = self.context_key(model, namespace, kind, normalized, item_key, source)
            rows.append((key, kind, normalized, item_key, source, value, model, namespace))
        if not rows:
            return
        with self._connect() as db:
            db.executemany("""INSERT INTO contextual_translations
                           (cache_key, kind, path, item_key, source, value, model, namespace)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, model=excluded.model,
                           namespace=excluded.namespace WHERE contextual_translations.locked=0""", rows)

    def entries(self, search: str = "", limit: int = 10000) -> list[tuple[str, str, str]]:
        """Return readable cache rows as ``(model, source, translation)`` tuples."""
        query = "SELECT COALESCE(model, ''), COALESCE(source, ''), value FROM translations"
        params: list[object] = []
        if search:
            query += " WHERE model LIKE ? OR source LIKE ? OR value LIKE ?"
            pattern = f"%{search}%"
            params.extend((pattern, pattern, pattern))
        query += " ORDER BY rowid DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as db:
            return [(str(model), str(source), str(value))
                    for model, source, value in db.execute(query, params)]

    def detailed_entries(self, search: str = "", limit: int = 500, offset: int = 0) -> list[tuple[str, str, str, str, bool]]:
        query = """SELECT ui_key, model, source_display, value, locked FROM (
                 SELECT rowid AS sort_id, cache_key AS ui_key, COALESCE(model, '') AS model,
                        COALESCE(source, '') AS source_display, value, locked FROM translations
                 UNION ALL
                 SELECT rowid AS sort_id, 'ctx:' || cache_key AS ui_key, COALESCE(model, '') AS model,
                        '[' || kind || '] ' || path || ' :: ' || item_key || char(10) || source AS source_display,
                        value, locked FROM contextual_translations)"""
        params: list[object] = []
        if search:
            query += " WHERE model LIKE ? OR source_display LIKE ? OR value LIKE ?"
            pattern = f"%{search}%"; params.extend((pattern, pattern, pattern))
        query += " ORDER BY sort_id DESC LIMIT ? OFFSET ?"; params.extend((max(1, int(limit)), max(0, int(offset))))
        with self._connect() as db:
            return [(str(key), str(model), str(source), str(value), bool(locked))
                    for key, model, source, value, locked in db.execute(query, params)]

    def count(self, search: str = "") -> int:
        query = """SELECT COUNT(*) FROM (
                 SELECT COALESCE(model, '') AS model, COALESCE(source, '') AS source_display, value FROM translations
                 UNION ALL
                 SELECT COALESCE(model, ''), '[' || kind || '] ' || path || ' :: ' || item_key || char(10) || source,
                        value FROM contextual_translations)"""; params = []
        if search:
            query += " WHERE model LIKE ? OR source_display LIKE ? OR value LIKE ?"
            pattern=f"%{search}%"; params=[pattern,pattern,pattern]
        with self._connect() as db:
            return int(db.execute(query, params).fetchone()[0])

    def delete(self, cache_keys: list[str]) -> int:
        if not cache_keys: return 0
        with self._connect() as db:
            before = db.total_changes
            regular = [(key,) for key in cache_keys if not key.startswith("ctx:")]
            contextual = [(key[4:],) for key in cache_keys if key.startswith("ctx:")]
            db.executemany("DELETE FROM translations WHERE cache_key=? AND locked=0", regular)
            db.executemany("DELETE FROM contextual_translations WHERE cache_key=? AND locked=0", contextual)
            return db.total_changes - before

    def update(self, cache_key: str, value: str) -> None:
        if not value.strip(): raise ValueError("译文不能为空")
        with self._connect() as db:
            if cache_key.startswith("ctx:"):
                db.execute("UPDATE contextual_translations SET value=? WHERE cache_key=?", (value, cache_key[4:]))
            else:
                db.execute("UPDATE translations SET value=? WHERE cache_key=?", (value, cache_key))

    def set_locked(self, cache_keys: list[str], locked: bool) -> int:
        if not cache_keys: return 0
        with self._connect() as db:
            before = db.total_changes
            regular = [(int(locked), key) for key in cache_keys if not key.startswith("ctx:")]
            contextual = [(int(locked), key[4:]) for key in cache_keys if key.startswith("ctx:")]
            db.executemany("UPDATE translations SET locked=? WHERE cache_key=?", regular)
            db.executemany("UPDATE contextual_translations SET locked=? WHERE cache_key=?", contextual)
            return db.total_changes - before


class OpenAITranslator:
    """Small dependency-free client for OpenAI-compatible chat-completions APIs."""

    def __init__(self, api_key: str, base_url: str, model: str, cache: TranslationCache | None = None,
                 timeout: int = 60, retries: int = 3, api_workers: int = 8,
                 reasoning_effort: str = "disabled", glossary: dict[str, str] | None = None,
                 system_prompt: str | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.api_workers = max(1, int(api_workers))
        self._api_slots = AdaptiveLimiter(self.api_workers)
        self.reasoning_effort = reasoning_effort
        self.system_prompt = system_prompt
        self.glossary = {str(k): str(v) for k, v in (glossary or {}).items() if str(k).strip() and str(v).strip()}
        glossary_hash = hashlib.sha256(json.dumps(self.glossary, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        self.cache_namespace = f"mc-localizer-v4|{self.base_url.lower()}|{reasoning_effort}|{glossary_hash}"

    def list_models(self) -> list[str]:
        """Return model IDs exposed by an OpenAI-compatible ``/models`` endpoint."""
        request = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Model API returned an invalid response")
        model_ids = sorted({item["id"] for item in items
                            if isinstance(item, dict) and isinstance(item.get("id"), str)})
        if not model_ids:
            raise ValueError("Model API returned no available models")
        return model_ids

    def _build_payload(self, values: list[str]) -> dict:
        numbered = "\n".join(f"{i}: {value}" for i, value in enumerate(values))
        system = self.system_prompt or "Translate Minecraft UI text to concise Simplified Chinese. Preserve placeholders, formatting codes, JSON escapes, and line count. Return only one JSON object whose keys are the exact numeric IDs from the input and whose values are translated strings. Do not omit, merge, split, or reorder IDs."
        if self.glossary:
            terms = "\n".join(f"{source} => {target}" for source, target in list(self.glossary.items())[:500])
            system += "\nUse this mandatory terminology glossary:\n" + terms
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": numbered},
            ],
        }
        if "deepseek.com" in self.base_url.lower():
            if self.reasoning_effort == "disabled":
                payload["thinking"] = {"type": "disabled"}
            elif self.reasoning_effort in {"low", "high", "max"}:
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = self.reasoning_effort
        return payload

    @staticmethod
    def _parse_translations(content: str, expected_count: int) -> list[str]:
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(content)
        if isinstance(result, dict):
            expected = {str(index) for index in range(expected_count)}
            if set(result) != expected:
                raise ValueError("Translation API returned missing or unexpected IDs")
            return [str(result[str(index)]) for index in range(expected_count)]
        # Backward compatibility for providers that insist on returning an array.
        if isinstance(result, list) and len(result) == expected_count:
            return [str(value) for value in result]
        raise ValueError("Translation API returned an invalid item count")

    def _request(self, values: list[str]) -> list[str]:
        payload = self._build_payload(values)
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            content = json.loads(response.read())["choices"][0]["message"]["content"].strip()
        return self._parse_translations(content, len(values))

    @staticmethod
    def _parse_plain_translation(content: str, source: str) -> str:
        """Validate the non-JSON last-resort response used for one failed item."""
        value = content.strip()
        if value.startswith("```") and value.endswith("```"):
            value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        if not value:
            raise ValueError("Plain translation API returned an empty response")
        if value.startswith(("翻译：", "翻译:")):
            value = value.split("：", 1)[-1] if "：" in value[:4] else value.split(":", 1)[-1]
            value = value.strip()
        if len(value) > max(500, len(source) * 4 + 100):
            raise ValueError("Plain translation API returned excessive text")
        if not placeholders_match(source, value):
            raise ValueError("Plain translation API damaged placeholders")
        return value

    def _request_plain(self, source: str) -> str:
        plain_system = ("Translate game text to natural Simplified Chinese. Preserve placeholders and formatting codes exactly. Return only the translated text, with no JSON, quotes, label, explanation, or Markdown."
                        if self.system_prompt else
                        "Translate the Minecraft text to concise Simplified Chinese. Preserve placeholders and formatting codes exactly. Return only the translated text, with no JSON, quotes, label, explanation, or Markdown.")
        if self.glossary:
            plain_system += "\nMandatory glossary:\n" + "\n".join(
                f"{key} => {value}" for key, value in list(self.glossary.items())[:500])
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": plain_system},
                {"role": "user", "content": source},
            ],
        }
        if "deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            content = json.loads(response.read())["choices"][0]["message"]["content"]
        return self._parse_plain_translation(content, source)

    def translate_many(self, values: list[str]) -> list[str]:
        result: list[str | None] = [None] * len(values)
        missing: list[tuple[int, str]] = []
        for index, value in enumerate(values):
            cached = self.cache.get(self.model, value, self.cache_namespace) if self.cache else None
            if cached is None:
                missing.append((index, value))
            else:
                result[index] = cached
        if missing:
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    with self._api_slots:
                        translated = self._request([value for _, value in missing])
                    self._api_slots.success()
                    for (index, source), target in zip(missing, translated):
                        if not placeholders_match(source, target):
                            target = source
                        result[index] = target
                        if self.cache:
                            self.cache.put(self.model, source, target, self.cache_namespace)
                    break
                except (ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
                    if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                        self._api_slots.throttle()
                    last_error = exc
                    time.sleep(2 ** attempt)
            else:
                if len(missing) == 1:
                    index, source = missing[0]
                    try:
                        target = self._request_plain(source)
                        result[index] = target
                        if self.cache:
                            self.cache.put(self.model, source, target, self.cache_namespace)
                    except Exception as fallback_error:
                        raise RuntimeError(f"Translation request failed: {last_error}; plain fallback failed: {fallback_error}")
                else:
                    raise RuntimeError(f"Translation request failed: {last_error}")
        return [value if value is not None else values[i] for i, value in enumerate(result)]


class CoalescingTranslator:
    """Combine small concurrent translation jobs into fewer, fuller API requests."""

    def __init__(self, translator: Translator, workers: int = 8, max_chars: int | None = None,
                 max_items: int | None = None, gather_seconds: float = 0.30, progress=None):
        self.translator = translator
        self.workers = max(1, int(workers))
        # Keep total in-flight payload near 768k characters while bounding request size.
        self.max_chars = max_chars or max(16000, min(48000, 768000 // self.workers))
        self.max_items = max_items or max(32, min(512, 4096 // self.workers))
        self.gather_seconds = gather_seconds
        self.progress = progress
        self.queue = Queue()
        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="api-batch")
        self.dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="api-batch-dispatch")
        self.closed = False
        self.batch_number = 0
        self.dispatcher.start()

    def translate_many(self, values: list[str]) -> list[str]:
        if not values: return []
        job = {"values": values, "event": threading.Event(), "result": None, "error": None}
        self.queue.put(job)
        job["event"].wait()
        if job["error"] is not None: raise job["error"]
        return job["result"]

    def _dispatch_loop(self):
        while True:
            first = self.queue.get()
            if first is None: break
            jobs = [first]
            size = sum(map(len, first["values"]))
            deadline = time.monotonic() + self.gather_seconds
            while time.monotonic() < deadline:
                try:
                    candidate = self.queue.get(timeout=max(0.001, deadline - time.monotonic()))
                except Empty:
                    break
                if candidate is None:
                    self.queue.put(None); break
                candidate_size = sum(map(len, candidate["values"]))
                item_count = sum(len(job["values"]) for job in jobs)
                if jobs and (size + candidate_size > self.max_chars or
                             item_count + len(candidate["values"]) > self.max_items):
                    self.queue.put(candidate); break
                jobs.append(candidate); size += candidate_size
            self.batch_number += 1
            self.executor.submit(self._run_batch, self.batch_number, jobs)

    def _run_batch(self, number, jobs):
        values = [value for job in jobs for value in job["values"]]
        if self.progress:
            chars = sum(map(len, values))
            self.progress(f"API 聚合批次 {number}：合并 {len(jobs)} 个任务，{len(values)} 条文本，{chars}/{self.max_chars} 字符")
        try:
            translated = self.translator.translate_many(values)
            cursor = 0
            for job in jobs:
                count = len(job["values"])
                job["result"] = translated[cursor:cursor + count]
                cursor += count
        except Exception as exc:
            for job in jobs: job["error"] = exc
        finally:
            for job in jobs: job["event"].set()

    def close(self):
        if self.closed: return
        self.closed = True
        self.queue.put(None)
        self.dispatcher.join()
        self.executor.shutdown(wait=True)
