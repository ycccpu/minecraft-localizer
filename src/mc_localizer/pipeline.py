from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from .formats import chunks, load_locale, needs_translation, save_locale
from .runtime import data_dir
from .translator import IdentityTranslator, Translator
from .specialized import SpecializedProcessor


LOCALE_NAMES = {"en_us.json", "en_us.lang"}
SKIP_DIRECTORIES = {
    "versions", "libraries", "runtime", "runtimes", "natives", "logs", "screenshots",
    "backups", "crash-reports", "downloads", "webcache", "shaderpacks",
    "journeymap", ".git", "__pycache__",
}


def _archive_has_translatable_content(path_text: str) -> bool:
    """Process-safe archive inventory pass used to discard irrelevant jars early."""
    try:
        with zipfile.ZipFile(path_text) as archive:
            for info in archive.infolist():
                parts = [part.lower() for part in PurePosixPath(info.filename).parts]
                name = parts[-1] if parts else ""
                if name in LOCALE_NAMES:
                    return True
                if "patchouli_books" in parts and "en_us" in parts and name.endswith(".json"):
                    return True
        return False
    except (OSError, zipfile.BadZipFile):
        return True


@dataclass
class GenerationResult:
    scanned_archives: int = 0
    source_files: int = 0
    output_files: int = 0
    translated_entries: int = 0
    memory_hits: int = 0
    specialized_files: int = 0
    specialized_strings: int = 0
    warnings: list[str] = field(default_factory=list)
    resource_packs: list[str] = field(default_factory=list)
    instance_overlay: str = ""
    quality_report: dict = field(default_factory=dict)


@dataclass
class LocaleRecord:
    relative: Path
    source: dict
    bundled: dict | None = None


class PatchGenerator:
    def __init__(self, source: str | Path, output: str | Path, translator: Translator | None = None,
                 memory=None, progress: Callable[[str], None] | None = None, max_workers: int = 1,
                 process_workers: int | None = None, finalize_output: bool = False,
                 cancel_event=None, scopes=None):
        self.source = Path(source)
        self.output = Path(output)
        self.translator = translator or IdentityTranslator()
        self.memory = memory
        self.progress = progress
        self.max_workers = max(1, int(max_workers))
        self.process_workers = max(1, int(process_workers if process_workers is not None
                                          else min(32, max(1, self.max_workers // 2))))
        self.finalize_output = finalize_output
        self.cancel_event = cancel_event
        self.scopes = set(scopes or {"locale", "patchouli", "quests", "scripts", "config", "serverconfig", "shaders"})
        self.specialized = SpecializedProcessor(self.output, self.translator, self.memory, self.scopes)
        self.scan_manifest_path = data_dir() / "scan_manifest.json"

    def _context_cache_backend(self):
        backend = getattr(self.translator, "translator", self.translator)
        cache = getattr(backend, "cache", None)
        if cache is None or not hasattr(cache, "get_context"): return None
        return backend, cache

    def _context_get(self, kind: str, path: str, item_key: str, source: str) -> str | None:
        pair = self._context_cache_backend()
        if not pair: return None
        backend, cache = pair
        return cache.get_context(backend.model, backend.cache_namespace, kind, path, item_key, source)

    def _context_put(self, kind: str, path: str, item_key: str, source: str, value: str) -> None:
        if value == source or not value.strip(): return
        pair = self._context_cache_backend()
        if not pair: return
        backend, cache = pair
        cache.put_context(backend.model, backend.cache_namespace, kind, path, item_key, source, value)

    def _context_put_many(self, records: list[tuple[str, str, str, str, str]]) -> None:
        records = [record for record in records if record[4] != record[3] and record[4].strip()]
        if not records:
            return
        pair = self._context_cache_backend()
        if not pair:
            return
        backend, cache = pair
        bulk = getattr(cache, "put_context_many", None)
        if bulk:
            bulk(backend.model, backend.cache_namespace, records)
        else:
            for kind, path, item_key, source, value in records:
                cache.put_context(backend.model, backend.cache_namespace, kind, path, item_key, source, value)

    def _archive_signature(self, path: Path) -> str:
        stat = path.stat()
        return f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"

    def _load_scan_manifest(self) -> dict:
        try:
            data = json.loads(self.scan_manifest_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_scan_manifest(self, data: dict) -> None:
        temporary = self.scan_manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.scan_manifest_path)

    def _report(self, message: str) -> None:
        if self.progress: self.progress(message)

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RuntimeError("用户已停止任务")

    def _validate_paths(self) -> None:
        source = self.source.resolve()
        output = self.output.resolve()
        if source == output:
            raise ValueError("输出目录不能与源目录相同")
        if self.source.is_dir() and (output.is_relative_to(source) or source.is_relative_to(output)):
            raise ValueError("输出目录不能位于源目录内部，也不能包含源目录；请选择平级的独立目录")

    def _walk_files(self, root: Path):
        """Walk useful instance content while pruning game/runtime-only directories."""
        output = self.output.resolve()
        for current, directories, files in os.walk(root):
            current_path = Path(current)
            kept = []
            for name in directories:
                candidate = current_path / name
                try:
                    parts = tuple(part.lower() for part in candidate.relative_to(root).parts)
                except ValueError:
                    parts = ()
                # Enter saves only far enough to find <world>/serverconfig.  Region,
                # player and mod data remain excluded because copying them is unsafe.
                in_safe_save_config = (parts and parts[0] == "saves" and
                                       (len(parts) <= 2 or (len(parts) >= 3 and parts[2] == "serverconfig")))
                if (name.lower() in SKIP_DIRECTORIES and not in_safe_save_config) or candidate.resolve() == output:
                    continue
                if parts and parts[0] == "saves" and not in_safe_save_config:
                    continue
                kept.append(name)
            directories[:] = kept
            for name in files:
                yield current_path / name

    @staticmethod
    def _is_runtime_archive(path: Path) -> bool:
        """Identify Minecraft/loader runtime jars even when launchers place them at instance root."""
        name = path.name.lower()
        patterns = (
            r"^\d+(?:\.\d+){1,2}-(?:neo)?forge[_-].*\.jar$",
            r"^(?:neo)?forge-\d+(?:\.\d+)+(?:-universal|-client|-server)?\.jar$",
            r"^minecraft[_-]?(?:client|server)?[_-]?\d+(?:\.\d+){1,2}\.jar$",
        )
        return any(re.match(pattern, name) for pattern in patterns)

    @staticmethod
    def _safe_member(name: str) -> bool:
        path = PurePosixPath(name)
        return not path.is_absolute() and ".." not in path.parts

    def _translate(self, source: dict, existing: dict | None = None, memory_override: bool = False, context: str = "") -> tuple[dict, int, int]:
        result = dict(existing or {})
        # A blank bundled/community translation must not mask a translatable
        # non-empty English value.  Keep intentional blanks whose source is
        # also blank (and technical sentinels rejected by needs_translation).
        for key, value in source.items():
            if key in result and isinstance(result[key], str) and not result[key].strip() and needs_translation(value):
                result.pop(key)
        memory_hits = 0
        if self.memory and memory_override:
            items = getattr(self.memory, "get_override_items", None)
            if items:
                remembered_items = items(context)
                result.update(remembered_items)
                memory_hits += len(remembered_items)
            override = getattr(self.memory, "get_override_for", None)
            if override:
                for key in source:
                    remembered = override(context, key)
                    if remembered is not None and key not in result:
                        result[key] = remembered
                        memory_hits += 1
        if self.memory:
            for key, value in source.items():
                if key not in result and needs_translation(value):
                    contextual = getattr(self.memory, "get_for", None)
                    remembered = contextual(context, key) if contextual else self.memory.get(key)
                    if remembered is not None:
                        result[key] = remembered
                        memory_hits += 1
        pending = [(key, str(value)) for key, value in source.items() if key not in result and needs_translation(value)]
        translated_count = 0
        for batch in chunks(pending):
            values = [value for _, value in batch]
            translations = self.translator.translate_many(values)
            for (key, source_value), translation in zip(batch, translations):
                result[key] = translation
                translated_count += translation != source_value
        for key, value in source.items():
            result.setdefault(key, value)
        return result, translated_count, memory_hits

    def _emit_locale(self, source_path: Path, relative: Path, result: GenerationResult, bundled_zh: Path | None = None) -> None:
        target_relative = relative.with_name(relative.name.replace("en_us", "zh_cn"))
        target = self.output / target_relative
        try:
            source_data = load_locale(source_path)
            target_exists = target.exists()
            existing = load_locale(target) if target_exists else (load_locale(bundled_zh) if bundled_zh and bundled_zh.exists() else None)
            translated, count, memory_hits = self._translate(source_data, existing, memory_override=not target_exists, context=target_relative.as_posix())
            save_locale(target, translated)
            result.source_files += 1
            result.output_files += 1
            result.translated_entries += count
            result.memory_hits += memory_hits
            self._report(f"已处理语言文件 {result.source_files}：{relative}（新翻译 {count}，复用 {memory_hits}）")
        except Exception as exc:
            result.warnings.append(f"{source_path}: {exc}")
            self._report(f"警告：{source_path.name} 处理失败：{exc}")

    def _scan_directory(self, root: Path, result: GenerationResult) -> None:
        for path in self._walk_files(root):
            if path.name.lower() in LOCALE_NAMES:
                sibling = path.with_name(path.name.replace("en_us", "zh_cn"))
                self._emit_locale(path, path.relative_to(root), result, sibling if sibling.exists() else None)

    def _scan_archive(self, archive: Path, result: GenerationResult, archive_number: int | None = None) -> None:
        result.scanned_archives += 1
        number = archive_number or result.scanned_archives
        self._report(f"正在扫描压缩包 {number}：{archive.name}")
        try:
            with zipfile.ZipFile(archive) as zf, tempfile.TemporaryDirectory(prefix="mc-localizer-") as temp:
                temp_root = Path(temp)
                names = {info.filename for info in zf.infolist()}
                for info in zf.infolist():
                    if not self._safe_member(info.filename):
                        result.warnings.append(f"Skipped unsafe archive member: {archive}!{info.filename}")
                        continue
                    if PurePosixPath(info.filename).name.lower() not in LOCALE_NAMES:
                        continue
                    extracted = temp_root / Path(*PurePosixPath(info.filename).parts)
                    extracted.parent.mkdir(parents=True, exist_ok=True)
                    extracted.write_bytes(zf.read(info))
                    zh_name = info.filename.replace("en_us", "zh_cn")
                    bundled = None
                    if zh_name in names:
                        bundled = temp_root / Path(*PurePosixPath(zh_name).parts)
                        bundled.parent.mkdir(parents=True, exist_ok=True)
                        bundled.write_bytes(zf.read(zh_name))
                    self._emit_locale(extracted, Path(*PurePosixPath(info.filename).parts), result, bundled)
        except (OSError, zipfile.BadZipFile) as exc:
            result.warnings.append(f"{archive}: {exc}")
        special = self.specialized.archive_patchouli(archive)
        result.specialized_files += special.files
        result.specialized_strings += special.strings
        self._report(f"压缩包完成 {number}：{archive.name}")

    @staticmethod
    def _merge_result(target: GenerationResult, source: GenerationResult) -> None:
        for field_name in ("scanned_archives", "source_files", "output_files", "translated_entries",
                           "memory_hits", "specialized_files", "specialized_strings"):
            setattr(target, field_name, getattr(target, field_name) + getattr(source, field_name))
        target.warnings.extend(source.warnings)

    def _scan_archive_result(self, item: tuple[int, Path]) -> GenerationResult:
        number, archive = item
        local = GenerationResult()
        self._scan_archive(archive, local, number)
        return local

    def _collect_directory_records(self, root: Path, result: GenerationResult) -> list[LocaleRecord]:
        records = []
        for path in self._walk_files(root):
            if path.name.lower() not in LOCALE_NAMES: continue
            relative = path.relative_to(root)
            sibling = path.with_name(path.name.replace("en_us", "zh_cn"))
            try:
                records.append(LocaleRecord(relative, load_locale(path),
                                            load_locale(sibling) if sibling.exists() else None))
            except Exception as exc:
                result.warnings.append(f"{path}: {exc}")
        return records

    def _collect_archive_records(self, item: tuple[int, Path]) -> tuple[int, list[LocaleRecord], list[str]]:
        number, archive = item
        records, warnings = [], []
        try:
            with zipfile.ZipFile(archive) as zf, tempfile.TemporaryDirectory(prefix="mc-localizer-") as temp:
                temp_root = Path(temp)
                names = {info.filename for info in zf.infolist()}
                for info in zf.infolist():
                    if not self._safe_member(info.filename):
                        warnings.append(f"Skipped unsafe archive member: {archive}!{info.filename}")
                        continue
                    if PurePosixPath(info.filename).name.lower() not in LOCALE_NAMES: continue
                    relative = Path(*PurePosixPath(info.filename).parts)
                    extracted = temp_root / relative
                    extracted.parent.mkdir(parents=True, exist_ok=True)
                    extracted.write_bytes(zf.read(info))
                    bundled_data = None
                    zh_name = info.filename.replace("en_us", "zh_cn")
                    if zh_name in names:
                        bundled = temp_root / Path(*PurePosixPath(zh_name).parts)
                        bundled.parent.mkdir(parents=True, exist_ok=True)
                        bundled.write_bytes(zf.read(zh_name))
                        bundled_data = load_locale(bundled)
                    records.append(LocaleRecord(relative, load_locale(extracted), bundled_data))
        except Exception as exc:
            warnings.append(f"{archive}: {exc}")
        return number, records, warnings

    def _prepare_record(self, record: LocaleRecord) -> tuple[dict, list[tuple[str, str]], int]:
        target_relative = record.relative.with_name(record.relative.name.replace("en_us", "zh_cn"))
        target = self.output / target_relative
        target_exists = target.exists()
        existing = load_locale(target) if target_exists else record.bundled
        translated = dict(existing or {})
        # Treat empty existing values as missing when the English source is a
        # real translatable string, so they enter cache lookup/API batching.
        for key, value in record.source.items():
            if key in translated and isinstance(translated[key], str) and not translated[key].strip() and needs_translation(value):
                translated.pop(key)
        memory_hits = 0
        context = target_relative.as_posix()
        if self.memory and not target_exists:
            items = getattr(self.memory, "get_override_items", None)
            if items:
                remembered = items(context)
                translated.update(remembered); memory_hits += len(remembered)
            override = getattr(self.memory, "get_override_for", None)
            if override:
                for key in record.source:
                    value = override(context, key)
                    if value is not None and key not in translated:
                        translated[key] = value; memory_hits += 1
        if self.memory:
            contextual = getattr(self.memory, "get_for", None)
            for key, value in record.source.items():
                if key in translated or not needs_translation(value): continue
                remembered = contextual(context, key) if contextual else self.memory.get(key)
                if remembered is not None:
                    translated[key] = remembered; memory_hits += 1
        for key, value in record.source.items():
            if key in translated or not needs_translation(value): continue
            remembered = self._context_get("locale", context, str(key), str(value))
            if remembered is not None:
                translated[key] = remembered; memory_hits += 1
        pending = [(key, str(value)) for key, value in record.source.items()
                   if key not in translated and needs_translation(value)]
        return translated, pending, memory_hits

    @staticmethod
    def _global_batches(values: list[str], max_chars: int, max_items: int) -> list[list[str]]:
        batches, batch, size = [], [], 0
        for value in values:
            if batch and (size + len(value) > max_chars or len(batch) >= max_items):
                batches.append(batch); batch, size = [], 0
            batch.append(value); size += len(value)
        if batch: batches.append(batch)
        return batches

    def _translate_batch_resilient(self, backend, batch: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        """Bisect malformed batches, preserving successes and reporting failed leaves."""
        self._check_cancel()
        try:
            translated = backend.translate_many(batch)
            if len(translated) != len(batch):
                raise ValueError("Translation API returned an invalid item count")
            return translated, []
        except Exception as exc:
            if len(batch) <= 1:
                return list(batch), [(batch[0], f"{type(exc).__name__}: {exc}")]
            middle = len(batch) // 2
            left_values, left_failures = self._translate_batch_resilient(backend, batch[:middle])
            right_values, right_failures = self._translate_batch_resilient(backend, batch[middle:])
            return left_values + right_values, left_failures + right_failures

    @staticmethod
    def _record_leaf_failures(result: GenerationResult, label: str, number: int,
                              failures: list[tuple[str, str]]) -> None:
        for source, error in failures:
            preview = " ".join(source.splitlines())[:120]
            result.warnings.append(f"{label} {number} 单条失败：{preview!r}（{error}）")

    def _translate_records(self, records: list[LocaleRecord], result: GenerationResult) -> None:
        merged_records: dict[str, LocaleRecord] = {}
        duplicate_files = 0
        for record in records:
            target_relative = record.relative.with_name(record.relative.name.replace("en_us", "zh_cn"))
            identity = target_relative.as_posix().lower()
            current = merged_records.get(identity)
            if current is None:
                merged_records[identity] = LocaleRecord(record.relative, dict(record.source),
                                                        dict(record.bundled) if record.bundled else None)
                continue
            duplicate_files += 1
            current.source.update(record.source)
            if record.bundled:
                if current.bundled is None: current.bundled = {}
                current.bundled.update(record.bundled)
        if duplicate_files:
            self._report(f"同路径语言文件合并：合并 {duplicate_files} 个重复来源，避免后写文件覆盖前文")
        records = list(merged_records.values())
        prepared, locations = [], {}
        for index, record in enumerate(records):
            try:
                translated, pending, memory_hits = self._prepare_record(record)
            except Exception as exc:
                result.warnings.append(f"{record.relative}: {exc}")
                translated, pending, memory_hits = dict(record.source), [], 0
            prepared.append((translated, pending, memory_hits))
            for key, value in pending:
                locations.setdefault(value, []).append((index, key))
        unique_values = list(locations)
        max_chars = getattr(self.translator, "max_chars", 12000)
        max_items = getattr(self.translator, "max_items", 512)
        batches = self._global_batches(unique_values, max_chars, max_items)
        workers = max(1, int(getattr(self.translator, "workers", 1)))
        backend = getattr(self.translator, "translator", self.translator)
        self._report(f"全局收集完成：{len(records)} 个语言文件，{len(unique_values)} 条去重待翻译文本，分为 {len(batches)} 批，API 并发 {workers}")
        translated_values = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="global-api") as pool:
            futures = {pool.submit(self._translate_batch_resilient, backend, batch): (number, batch)
                       for number, batch in enumerate(batches, 1)}
            completed = 0
            for future in as_completed(futures):
                number, batch = futures[future]
                try:
                    outputs, failures = future.result()
                    translated_values.update(zip(batch, outputs))
                    self._record_leaf_failures(result, "全局 API 批次", number, failures)
                except Exception as exc:
                    result.warnings.append(f"全局 API 批次 {number}: {exc}")
                    translated_values.update((value, value) for value in batch)
                completed += 1
                remaining_texts = sum(len(futures[item][1]) for item in futures if not item.done())
                self._report(f"全局翻译进度：{completed}/{len(batches)} 批完成，剩余 {len(batches) - completed} 批 / {remaining_texts} 条文本")
        for source_value, targets in locations.items():
            target_value = translated_values.get(source_value, source_value)
            for record_index, key in targets:
                prepared[record_index][0][key] = target_value
        self._check_cancel()
        for index, record in enumerate(records):
            translated, pending, memory_hits = prepared[index]
            for key, value in record.source.items(): translated.setdefault(key, value)
            target_relative = record.relative.with_name(record.relative.name.replace("en_us", "zh_cn"))
            for key, source in pending:
                self._context_put("locale", target_relative.as_posix(), str(key), source,
                                  str(translated.get(key, source)))
            save_locale(self.output / target_relative, translated)
            count = sum(translated.get(key) != source for key, source in pending)
            result.source_files += 1; result.output_files += 1
            result.translated_entries += count; result.memory_hits += memory_hits
            self._report(f"写回语言文件 {index + 1}/{len(records)}：{record.relative}（新翻译 {count}，复用 {memory_hits}）")

    def _translate_structured_records(self, records: list[dict], result: GenerationResult) -> None:
        mappings, locations = [], {}
        for record_index, record in enumerate(records):
            translated = {}
            try: context_path = record["target"].relative_to(self.output).as_posix()
            except ValueError: context_path = record["namespace"]
            for key, value in record["pairs"]:
                remembered = self.memory.get(key) if self.memory else None
                if remembered is not None:
                    translated[key] = remembered
                else:
                    remembered = self._context_get("structured", context_path, key, value)
                    if remembered is not None: translated[key] = remembered
                    else: locations.setdefault(value, []).append((record_index, key))
            mappings.append(translated)
        unique_values = list(locations)
        max_chars = getattr(self.translator, "max_chars", 12000)
        max_items = getattr(self.translator, "max_items", 512)
        batches = self._global_batches(unique_values, max_chars, max_items)
        workers = max(1, int(getattr(self.translator, "workers", 1)))
        backend = getattr(self.translator, "translator", self.translator)
        self._report(f"结构化全局收集完成：{len(records)} 个文件，{len(unique_values)} 条去重文本，{len(batches)} 个 API 批次")
        translated_values = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="structured-api") as pool:
            futures = {pool.submit(self._translate_batch_resilient, backend, batch): (number, batch)
                       for number, batch in enumerate(batches, 1)}
            completed = 0
            for future in as_completed(futures):
                number, batch = futures[future]
                try:
                    outputs, failures = future.result()
                    translated_values.update(zip(batch, outputs))
                    self._record_leaf_failures(result, "结构化 API 批次", number, failures)
                except Exception as exc:
                    result.warnings.append(f"结构化 API 批次 {number}: {exc}")
                    translated_values.update((value, value) for value in batch)
                completed += 1
                remaining = sum(len(futures[item][1]) for item in futures if not item.done())
                self._report(f"结构化翻译进度：{completed}/{len(batches)} 批，剩余 {len(batches) - completed} 批 / {remaining} 条文本")
        for source_value, targets in locations.items():
            target_value = translated_values.get(source_value, source_value)
            for record_index, key in targets: mappings[record_index][key] = target_value
        self._check_cancel()
        cache_records = []
        for record, mapping in zip(records, mappings):
            try: context_path = record["target"].relative_to(self.output).as_posix()
            except ValueError: context_path = record["namespace"]
            sources = dict(record["pairs"])
            for key, value in mapping.items():
                if key in sources:
                    cache_records.append(("structured", context_path, key, sources[key], value))
        self._context_put_many(cache_records)
        if cache_records:
            self._report(f"结构化缓存批量写入完成：{len(cache_records)} 条")
        for index, (record, mapping) in enumerate(zip(records, mappings), 1):
            self.specialized.write_record(record, mapping)
            result.specialized_files += 1; result.specialized_strings += len(mapping)
            self._report(f"写回结构化文件 {index}/{len(records)}：{record['target']}")

    def _write_pack_metadata(self):
        metadata = self.output / "pack.mcmeta"
        if not metadata.exists():
            metadata.write_text(json.dumps({"pack": {"pack_format": 34, "description": "Minecraft 模组补充汉化"}}, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _version_from_value(value) -> str | None:
        if not isinstance(value, str): return None
        match = re.search(r"(?<!\d)(1\.\d+(?:\.\d+)?)(?!\d)", value)
        return match.group(1) if match else None

    def _minecraft_version_info(self) -> tuple[str, str]:
        if self.source.is_dir():
            preferred = [self.source / f"{self.source.name}.json", self.source / "manifest.json",
                         self.source / "minecraftinstance.json", self.source / "instance.json"]
            preferred.extend(path for path in self.source.glob("*.json") if path not in preferred and path.stat().st_size < 2_000_000)
            keys = ("minecraftVersion", "minecraft_version", "gameVersion", "inheritsFrom", "version", "id")
            for path in preferred:
                if not path.is_file(): continue
                try: data = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError): continue
                found = []
                def inspect(value):
                    if isinstance(value, dict):
                        for key in keys:
                            if key in value:
                                version = self._version_from_value(value[key])
                                if version: found.append(version)
                        for child in value.values(): inspect(child)
                    elif isinstance(value, list):
                        for child in value: inspect(child)
                inspect(data)
                if found: return found[0], f"版本元数据 {path.name}"
            for config_name in ("instance.cfg", "mmc-pack.json"):
                path=self.source/config_name
                if path.is_file():
                    try: version=self._version_from_value(path.read_text(encoding="utf-8",errors="ignore"))
                    except OSError: version=None
                    if version:return version,f"启动器配置 {config_name}"
        version = self._version_from_value(self.source.name)
        return (version, "目录/文件名") if version else ("unknown", "未识别")

    def _minecraft_version(self) -> str:
        return self._minecraft_version_info()[0]

    def _pack_format(self) -> int:
        version = self._minecraft_version()
        known = {"1.19.2": 9, "1.19.3": 12, "1.19.4": 13, "1.20": 15, "1.20.1": 15,
                 "1.20.2": 18, "1.20.3": 22, "1.20.4": 22, "1.20.5": 32, "1.20.6": 32,
                 "1.21": 34, "1.21.1": 34, "1.21.2": 42, "1.21.3": 42, "1.21.4": 46}
        if version in known: return known[version]
        if self.source.is_dir():
            candidates = [self.source / f"{self.source.name}.json", self.source / "manifest.json",
                          self.source / "minecraftinstance.json", self.source / "instance.json"]
            for path in candidates:
                if not path.is_file(): continue
                try: data=json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError,json.JSONDecodeError,UnicodeDecodeError): continue
                found=[]
                def inspect(value,key=""):
                    if isinstance(value,dict):
                        if "pack_version" in value and isinstance(value["pack_version"],dict):
                            resource=value["pack_version"].get("resource")
                            if isinstance(resource,int):found.append(resource)
                        for child_key,child in value.items(): inspect(child,str(child_key))
                    elif isinstance(value,int) and key in {"resourcePackVersion","resource_pack_version","pack_format"}:
                        found.append(value)
                inspect(data)
                if found:return found[0]
        if self.finalize_output:
            raise ValueError(f"无法确定 Minecraft {version} 的资源包格式；请使用受支持版本或提供包含 pack_version.resource 的版本元数据")
        return 34

    def build_resource_packs(self) -> list[Path]:
        """Create unpacked Minecraft resource-pack directories."""
        destination = self.output / "resourcepacks"
        version = self._minecraft_version()
        base_name = self.source.stem if self.source.is_file() else self.source.name
        standard_pack = destination / f"{base_name}-补汉材质包-{version}"
        patchouli_pack = destination / f"帕秋莉汉化材质包-{version}"
        metadata = lambda description: json.dumps(
            {"pack": {"pack_format": self._pack_format(), "description": description}}, ensure_ascii=False, indent=2).encode("utf-8")
        groups = ((standard_pack, "Minecraft 模组补充汉化", "standard"),
                  (patchouli_pack, "Patchouli 指南书汉化", "patchouli"))
        created = []
        for target, description, kind in groups:
            selected = []
            for path in self.output.rglob("*"):
                if not path.is_file() or destination in path.parents: continue
                relative = path.relative_to(self.output)
                parts = [part.lower() for part in relative.parts]
                if kind == "standard":
                    include = (parts and parts[0] == "assets" and "lang" in parts and
                               relative.name.lower() in {"zh_cn.json", "zh_cn.lang"})
                else:
                    include = (parts and parts[0] in {"assets", "data"} and "patchouli_books" in parts)
                if include: selected.append((path, relative))
            if not selected:
                self._report(f"未发现{description}源文件，跳过该资源包")
                continue
            target.mkdir(parents=True, exist_ok=True)
            (target / "pack.mcmeta").write_bytes(metadata(description))
            for path, relative in selected:
                output_path = target / relative
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output_path)
            created.append(target)
            self._report(f"已生成资源包：{target}")
        return created

    def build_instance_overlay(self) -> Path | None:
        """Keep overlay files directly in their game-relative locations."""
        candidates = []
        excluded_roots = {"assets", "data", "resourcepacks"}
        excluded_files = {"pack.mcmeta", "translation_cache.db", "使用说明.txt"}
        for path in self.output.rglob("*"):
            if not path.is_file(): continue
            relative = path.relative_to(self.output)
            if relative.parts[0].lower() in excluded_roots or relative.name.lower() in excluded_files:
                continue
            candidates.append((path, relative))
        if not candidates: return None
        self._report(f"已整理实例覆盖文件：{len(candidates)} 个")
        return self.output

    def cleanup_working_output(self, keep: set[Path]) -> None:
        for name in ("assets", "data"):
            path = self.output / name
            if path.exists(): shutil.rmtree(path)
        metadata = self.output / "pack.mcmeta"
        if metadata.exists(): metadata.unlink()
        guide = self.output / "使用说明.txt"
        guide.write_text(
            "本目录已按 Minecraft 实例目录结构整理。\n"
            "可点击软件中的“一键安装”，或将本目录中的 resourcepacks、config 等目录复制到游戏实例根目录。\n"
            "质量报告、使用说明和发布清单无需复制。\n",
            encoding="utf-8")
        self._report("已清理翻译过程文件，仅保留可使用的发布资源")

    def write_release_manifest(self, packs: list[Path]) -> Path:
        excluded = {"使用说明.txt", "质量报告.json", "发布清单.json"}
        files = [path.relative_to(self.output).as_posix() for path in self.output.rglob("*")
                 if path.is_file() and path.name not in excluded]
        manifest = {
            "格式版本": 1,
            "资源包": [path.relative_to(self.output).as_posix() for path in packs],
            "安装文件": sorted(files),
        }
        target = self.output / "发布清单.json"
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    def build_quality_report(self, result: GenerationResult) -> dict:
        json_errors = []
        empty_translations = 0
        empty_samples = []
        untranslated_config_comments = []
        for path in self.output.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"), strict=False)
                if path.name.lower() == "zh_cn.json" and isinstance(data, dict):
                    for key, value in data.items():
                        if isinstance(value, str) and not value.strip():
                            empty_translations += 1
                            if len(empty_samples) < 20:
                                empty_samples.append({"文件": str(path.relative_to(self.output)), "键": str(key)})
            except Exception as exc:
                json_errors.append(f"{path.relative_to(self.output)}: {exc}")
        config_changes = []
        config_suffixes = {".toml", ".cfg", ".properties", ".json5", ".yaml", ".yml"}
        is_comment = lambda line: line.lstrip().startswith(("#", ";", "//"))
        is_technical_comment = lambda text: (
            bool(re.fullmatch(r"\[@[^\]]+\]", text)) or
            bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:\-/]*", text)) or
            bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*=", text))
        )
        for path in self.output.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in config_suffixes: continue
            try: lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError: continue
            for number, line in enumerate(lines, 1):
                if not is_comment(line):
                    continue
                content = line.lstrip().lstrip("#;/").strip()
                if (re.search(r"[A-Za-z]", content) and not re.search(r"[\u3400-\u9fff]", content)
                        and not is_technical_comment(content)):
                    untranslated_config_comments.append({"文件": str(path.relative_to(self.output)),
                                                          "行": number, "文本": content[:160]})
        if self.source.is_dir():
            for target in self.output.rglob("*"):
                if not target.is_file() or target.suffix.lower() not in config_suffixes: continue
                relative = target.relative_to(self.output)
                source = self.source / relative
                if not source.exists(): continue
                try:
                    before = source.read_text(encoding="utf-8-sig").splitlines()
                    after = target.read_text(encoding="utf-8-sig").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                if [line for line in before if not is_comment(line)] != [line for line in after if not is_comment(line)]:
                    config_changes.append(str(relative))
        version, version_source = self._minecraft_version_info()
        report = {
            "游戏版本": version,
            "版本识别来源": version_source,
            "资源包格式": self._pack_format(),
            "JSON格式错误": len(json_errors),
            "非注释配置变化": len(config_changes),
            "空语言值": empty_translations,
            "未翻译配置注释": len(untranslated_config_comments),
            "警告数量": len(result.warnings),
            "是否可用": not json_errors and not config_changes,
            "结论": "结构检查通过，可以使用" if not json_errors and not config_changes else "发现结构风险，不建议安装",
        }
        if empty_samples: report["空语言值样本"] = empty_samples
        if untranslated_config_comments: report["未翻译配置注释样本"] = untranslated_config_comments[:20]
        if json_errors: report["JSON错误样本"] = json_errors[:10]
        if config_changes: report["配置变化样本"] = config_changes[:10]
        self._report("质量检查：" + json.dumps(report, ensure_ascii=False))
        return report

    def _generate_current_output(self) -> GenerationResult:
        self._check_cancel()
        if not self.source.exists():
            raise FileNotFoundError(self.source)
        self.output.mkdir(parents=True, exist_ok=True)
        result = GenerationResult()
        version, version_source = self._minecraft_version_info()
        self._report(f"游戏版本识别：{version}（{version_source}），资源包格式 {self._pack_format()}")
        self._report(f"开始扫描：{self.source}")
        if self.source.is_file():
            if self.source.suffix.lower() in {".jar", ".zip"}:
                if self._is_runtime_archive(self.source):
                    self._report(f"已跳过游戏/加载器文件：{self.source.name}")
                else:
                    self._scan_archive(self.source, result)
            elif self.source.name.lower() in LOCALE_NAMES:
                self._emit_locale(self.source, Path(self.source.name), result)
            else:
                raise ValueError(f"Unsupported source: {self.source}")
        else:
            self._report("阶段 1/3：收集所有语言文本")
            records = self._collect_directory_records(self.source, result) if "locale" in self.scopes else []
            self._check_cancel()
            archives = []
            for archive in self._walk_files(self.source):
                if archive.suffix.lower() not in {".jar", ".zip"}: continue
                if self._is_runtime_archive(archive):
                    self._report(f"已跳过游戏/加载器文件：{archive.name}")
                    continue
                archives.append(archive)
            if archives and self.process_workers > 1:
                manifest = self._load_scan_manifest(); useful_map = {}; pending = []
                for path in archives:
                    signature = self._archive_signature(path)
                    if signature in manifest: useful_map[path] = bool(manifest[signature])
                    else: pending.append((path, signature))
                self._report(f"CPU 多进程预检：增量命中 {len(archives)-len(pending)}，需检查 {len(pending)} 个压缩包")
                if pending:
                    with ProcessPoolExecutor(max_workers=self.process_workers) as processes:
                        checked = list(processes.map(_archive_has_translatable_content,
                                                     (str(path) for path, _ in pending), chunksize=8))
                    for (path, signature), keep in zip(pending, checked):
                        useful_map[path] = keep; manifest[signature] = keep
                    self._save_scan_manifest(manifest)
                useful = [useful_map[path] for path in archives]
                skipped = useful.count(False)
                archives = [path for path, keep in zip(archives, useful) if keep]
                self._report(f"多进程预检完成：跳过 {skipped} 个无可翻译内容的压缩包")
            self._report(f"发现 {len(archives)} 个待收集压缩包，使用 {self.max_workers} 个线程")
            archive_records = {}
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="mc-localizer") as pool:
                futures = [pool.submit(self._collect_archive_records, item)
                           for item in enumerate(archives, 1)] if "locale" in self.scopes else []
                completed = 0
                total = len(futures)
                for future in as_completed(futures):
                    number, collected, warnings = future.result()
                    archive_records[number] = collected
                    result.warnings.extend(warnings)
                    completed += 1
                    self._report(f"文本收集进度：{completed}/{total} 个压缩包，剩余 {total - completed}")
            for number in sorted(archive_records): records.extend(archive_records[number])
            result.scanned_archives = len(archives)
            if records:
                self._report("阶段 2/3：全局去重、缓存过滤与 API 翻译")
                self._translate_records(records, result)
            else:
                self._report("阶段 2/3：未发现语言文件，跳过语言翻译")
            self._check_cancel()
            self._report("阶段 3/3：处理任务书与结构化文本")
            structured_records = self.specialized.collect_directory_records(self.source, self._walk_files(self.source))
            structured_records.extend(self.specialized.collect_shader_records(self.source))
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="specialized") as pool:
                futures = {pool.submit(self.specialized.collect_archive_records, archive): archive for archive in archives}
                completed = 0
                for future in as_completed(futures):
                    structured_records.extend(future.result())
                    completed += 1
                    self._report(f"结构化收集：{completed}/{len(archives)}，剩余 {len(archives) - completed}")
            if structured_records:
                self._translate_structured_records(structured_records, result)
            else:
                self._report("未发现任务、魔改、配置、指南书或光影语言文件，跳过结构化翻译")
        self._check_cancel()
        self._write_pack_metadata()
        self._report("正在写入资源包元数据")
        packs = self.build_resource_packs()
        result.resource_packs = [str(path) for path in packs]
        if self.finalize_output:
            overlay = self.build_instance_overlay()
            result.instance_overlay = str(overlay) if overlay else ""
            result.quality_report = self.build_quality_report(result)
            keep = {self.output / "resourcepacks"}
            if overlay: keep.add(overlay)
            self.cleanup_working_output(keep)
            quality_path = self.output / "质量报告.json"
            quality_path.write_text(json.dumps(result.quality_report, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            self.write_release_manifest(packs)
            self._report(f"质量报告已保存：{quality_path}")
        return result

    def generate(self) -> GenerationResult:
        """Generate in staging and publish atomically when producing GUI releases."""
        self._validate_paths()
        if not self.finalize_output:
            return self._generate_current_output()
        destination = self.output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-building-", dir=destination.parent))
        original_output = self.output
        self.output = staging
        self.specialized.output = staging
        backup = destination.with_name(f".{destination.name}-previous-{os.getpid()}")
        try:
            result = self._generate_current_output()
            result.resource_packs = [str(destination / Path(path).relative_to(staging))
                                     for path in result.resource_packs]
            if result.instance_overlay:
                result.instance_overlay = str(destination / Path(result.instance_overlay).relative_to(staging))
            if backup.exists():
                shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
            if destination.exists():
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except Exception:
                if backup.exists() and not destination.exists():
                    os.replace(backup, destination)
                raise
            if backup.exists():
                shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
            return result
        finally:
            self.output = original_output
            self.specialized.output = original_output
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def make_zip(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        base = destination.with_suffix("")
        created = Path(shutil.make_archive(str(base), "zip", self.output))
        if created != destination:
            created.replace(destination)
        return destination


def write_result(path: str | Path, result: GenerationResult) -> None:
    Path(path).write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
