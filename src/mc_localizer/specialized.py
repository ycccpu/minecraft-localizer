from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

from .formats import needs_translation
from .structured import CONFIG_COMMENT_RE, JS_TEXT_RE, StructuredStats, StructuredTranslator, patchouli_target, write_json


class SpecializedProcessor:
    def __init__(self, output: str | Path, translator, memory=None, scopes=None):
        self.output = Path(output)
        self.structured = StructuredTranslator(translator, memory)
        self.scopes = set(scopes or {"patchouli", "quests", "scripts", "config", "serverconfig", "shaders"})

    @staticmethod
    def _shader_locale(raw: bytes, suffix: str) -> dict[str, str]:
        text = raw.decode("utf-8-sig", errors="replace")
        if suffix == ".json":
            data = json.loads(text, strict=False)
            return {str(key): str(value) for key, value in data.items() if isinstance(value, str)} if isinstance(data, dict) else {}
        result = {}
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1); result[key.strip()] = value
        return result

    def collect_shader_records(self, source: Path) -> list[dict]:
        if "shaders" not in self.scopes: return []
        root = source / "shaderpacks"
        if not root.is_dir(): return []
        records = []
        for pack in root.iterdir():
            if pack.is_dir():
                for locale in pack.rglob("*"):
                    if not locale.is_file() or locale.name.lower() not in {"en_us.lang", "en_us.json"}: continue
                    try: data = self._shader_locale(locale.read_bytes(), locale.suffix.lower())
                    except (OSError, json.JSONDecodeError): continue
                    namespace = "shader." + pack.name + "." + locale.relative_to(pack).as_posix()
                    pairs = [(f"{namespace}.{key}", value) for key, value in data.items() if needs_translation(value)]
                    if pairs:
                        target = self.output / "shaderpacks" / pack.name / locale.relative_to(pack).with_name(
                            locale.name.lower().replace("en_us", "zh_cn"))
                        records.append({"kind": "shader-folder", "target": target, "payload": data,
                                        "namespace": namespace, "pairs": pairs, "suffix": locale.suffix.lower()})
            elif pack.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(pack) as archive:
                        locales = []
                        for name in archive.namelist():
                            member = PurePosixPath(name)
                            if member.name.lower() not in {"en_us.lang", "en_us.json"}: continue
                            try: data = self._shader_locale(archive.read(name), member.suffix.lower())
                            except (KeyError, json.JSONDecodeError): continue
                            target_member = member.with_name(member.name.lower().replace("en_us", "zh_cn")).as_posix()
                            locales.append({"source": name, "target": target_member, "data": data,
                                            "suffix": member.suffix.lower()})
                except (OSError, zipfile.BadZipFile): continue
                pairs = []; namespace = "shader." + pack.name
                for locale in locales:
                    pairs.extend((f"{namespace}.{locale['target']}.{key}", value)
                                 for key, value in locale["data"].items() if needs_translation(value))
                if pairs:
                    records.append({"kind": "shader-zip", "target": self.output / "shaderpacks" / pack.name,
                                    "payload": locales, "source_archive": pack, "namespace": namespace,
                                    "pairs": pairs})
        return records

    def archive_patchouli(self, archive_path: Path) -> StructuredStats:
        stats = StructuredStats()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for name in archive.namelist():
                    target_name = patchouli_target(name)
                    if not target_name or not name.lower().endswith(".json"):
                        continue
                    try:
                        data = json.loads(archive.read(name).decode("utf-8-sig"), strict=False)
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
                        continue
                    namespace = "patchouli." + name.replace("/", ".")
                    translated, count = self.structured.translate_json(data, namespace)
                    write_json(self.output / Path(*PurePosixPath(target_name).parts), translated)
                    stats.files += 1
                    stats.strings += count
        except (OSError, zipfile.BadZipFile):
            pass
        return stats

    def directory_specials(self, source: Path, files: Iterable[Path] | None = None) -> StructuredStats:
        stats = StructuredStats()
        for path in files if files is not None else (item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            lower_parts = [part.lower() for part in relative.parts]
            if "patchouli" in self.scopes and "patchouli_books" in lower_parts and "en_us" in lower_parts and path.suffix.lower() == ".json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"), strict=False)
                except (OSError, json.JSONDecodeError):
                    continue
                translated, count = self.structured.translate_json(data, "patchouli." + ".".join(relative.parts))
                target_parts = ["zh_cn" if part.lower() == "en_us" else part for part in relative.parts]
                write_json(self.output.joinpath(*target_parts), translated)
                stats.files += 1; stats.strings += count
            elif "quests" in self.scopes and path.suffix.lower() in {".snbt", ".nbt"} and any(name in lower_parts for name in ("ftbquests", "quests")):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                translated, count = self.structured.translate_pattern_text(text, "quests." + ".".join(relative.parts))
                target = self.output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(translated, encoding="utf-8")
                stats.files += 1; stats.strings += count
            elif "scripts" in self.scopes and path.suffix.lower() in {".js", ".txt"} and any(name in lower_parts for name in ("kubejs", "fancymenu")):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                translated, count = self.structured.translate_pattern_text(text, "script." + ".".join(relative.parts), JS_TEXT_RE)
                if count:
                    target = self.output / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(translated, encoding="utf-8")
                    stats.files += 1; stats.strings += count
        return stats

    def collect_archive_records(self, archive_path: Path) -> list[dict]:
        records = []
        if "patchouli" not in self.scopes: return records
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for name in archive.namelist():
                    target_name = patchouli_target(name)
                    if not target_name or not name.lower().endswith(".json"): continue
                    try:
                        data = json.loads(archive.read(name).decode("utf-8-sig"), strict=False)
                    except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
                        continue
                    namespace = "patchouli." + name.replace("/", ".")
                    pairs = self.structured.collect_json(data, namespace)
                    if pairs:
                        records.append({"kind": "json", "target": self.output / Path(*PurePosixPath(target_name).parts),
                                        "payload": data, "namespace": namespace, "pairs": pairs})
        except (OSError, zipfile.BadZipFile):
            pass
        return records

    def collect_directory_records(self, source: Path, files: Iterable[Path] | None = None) -> list[dict]:
        records = []
        for path in files if files is not None else (item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            lower_parts = [part.lower() for part in relative.parts]
            safe_config = (("config" in self.scopes and bool(lower_parts) and lower_parts[0] in {"config", "defaultconfigs"}) or
                           ("serverconfig" in self.scopes and bool(lower_parts) and lower_parts[0] == "saves" and "serverconfig" in lower_parts))
            if "patchouli" in self.scopes and "patchouli_books" in lower_parts and "en_us" in lower_parts and path.suffix.lower() == ".json":
                try: data = json.loads(path.read_text(encoding="utf-8-sig"), strict=False)
                except (OSError, json.JSONDecodeError): continue
                namespace = "patchouli." + ".".join(relative.parts)
                target_parts = ["zh_cn" if part.lower() == "en_us" else part for part in relative.parts]
                pairs = self.structured.collect_json(data, namespace)
                if pairs: records.append({"kind": "json", "target": self.output.joinpath(*target_parts),
                                          "payload": data, "namespace": namespace, "pairs": pairs})
            elif "quests" in self.scopes and path.suffix.lower() in {".snbt", ".nbt"} and any(name in lower_parts for name in ("ftbquests", "quests")):
                try: content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError): continue
                namespace = "quests." + ".".join(relative.parts)
                pairs = self.structured.collect_pattern_text(content, namespace)
                if pairs: records.append({"kind": "pattern", "target": self.output / relative,
                                          "payload": content, "namespace": namespace, "pairs": pairs, "pattern": "snbt"})
            elif safe_config and path.suffix.lower() in {".snbt"}:
                try: content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError): continue
                namespace = "config." + ".".join(relative.parts)
                pairs = self.structured.collect_pattern_text(content, namespace)
                if pairs: records.append({"kind": "pattern", "target": self.output / relative,
                                          "payload": content, "namespace": namespace, "pairs": pairs, "pattern": "snbt"})
            elif safe_config and path.suffix.lower() == ".json":
                try: data = json.loads(path.read_text(encoding="utf-8-sig"), strict=False)
                except (OSError, json.JSONDecodeError): continue
                namespace = "config." + ".".join(relative.parts)
                pairs = self.structured.collect_json(data, namespace)
                if pairs: records.append({"kind": "json", "target": self.output / relative,
                                          "payload": data, "namespace": namespace, "pairs": pairs})
            elif safe_config and path.suffix.lower() in {".toml", ".cfg", ".properties", ".json5", ".yaml", ".yml"}:
                try: content = path.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeDecodeError): continue
                namespace = "config-comment." + ".".join(relative.parts)
                pairs = self.structured.collect_pattern_text(content, namespace, CONFIG_COMMENT_RE)
                if pairs: records.append({"kind": "pattern", "target": self.output / relative,
                                          "payload": content, "namespace": namespace, "pairs": pairs,
                                          "pattern": "config-comment"})
            elif "scripts" in self.scopes and path.suffix.lower() in {".js", ".txt"} and any(name in lower_parts for name in ("kubejs", "fancymenu")):
                try: content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError): continue
                namespace = "script." + ".".join(relative.parts)
                pairs = self.structured.collect_pattern_text(content, namespace, JS_TEXT_RE)
                if pairs: records.append({"kind": "pattern", "target": self.output / relative,
                                          "payload": content, "namespace": namespace, "pairs": pairs, "pattern": "js"})
        return records

    def write_record(self, record: dict, translated: dict[str, str]) -> None:
        if record["kind"] == "shader-folder":
            data = {key: translated.get(f"{record['namespace']}.{key}", value)
                    for key, value in record["payload"].items()}
            record["target"].parent.mkdir(parents=True, exist_ok=True)
            if record["suffix"] == ".json": write_json(record["target"], data)
            else: record["target"].write_text("".join(f"{key}={value}\n" for key, value in data.items()), encoding="utf-8")
        elif record["kind"] == "shader-zip":
            target = record["target"]; target.parent.mkdir(parents=True, exist_ok=True)
            replacements = {}
            for locale in record["payload"]:
                prefix = f"{record['namespace']}.{locale['target']}."
                data = {key: translated.get(prefix + key, value) for key, value in locale["data"].items()}
                if locale["suffix"] == ".json": raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                else: raw = "".join(f"{key}={value}\n" for key, value in data.items()).encode("utf-8")
                replacements[locale["target"]] = raw
            temporary = target.with_suffix(".tmp")
            with zipfile.ZipFile(record["source_archive"]) as source, zipfile.ZipFile(temporary, "w") as output:
                existing = set(source.namelist())
                for info in source.infolist():
                    output.writestr(info, replacements.get(info.filename, source.read(info.filename)))
                for name, raw in replacements.items():
                    if name not in existing: output.writestr(name, raw)
            os.replace(temporary, target)
        elif record["kind"] == "json":
            data = self.structured.apply_json(record["payload"], record["namespace"], translated)
            write_json(record["target"], data)
        else:
            pattern_name = record.get("pattern")
            pattern = JS_TEXT_RE if pattern_name == "js" else (CONFIG_COMMENT_RE if pattern_name == "config-comment" else None)
            if pattern_name == "config-comment":
                # A model-generated newline would escape the comment marker and
                # could turn prose into active TOML/CFG syntax.
                translated = {key: " ".join(value.splitlines()).strip()
                              for key, value in translated.items()}
            if pattern is None:
                text = self.structured.apply_pattern_text(record["payload"], record["namespace"], translated)
            else:
                text = self.structured.apply_pattern_text(record["payload"], record["namespace"], translated, pattern)
            record["target"].parent.mkdir(parents=True, exist_ok=True)
            record["target"].write_text(text, encoding="utf-8")
