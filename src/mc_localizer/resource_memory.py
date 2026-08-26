from __future__ import annotations

import json
import zipfile
from pathlib import Path, PurePosixPath

from .formats import load_locale


class ResourcePackMemory:
    """Use non-empty translations from completed Chinese resource packs."""

    def __init__(self, paths):
        self.values: dict[str, str] = {}
        self.by_path: dict[tuple[str, str], str] = {}
        for item in paths:
            self._load(Path(item))

    @staticmethod
    def _normalize_path(path: str) -> str:
        return path.replace("\\", "/").lower().lstrip("./")

    def _add(self, data, path: str):
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                self.values[key] = value
                self.by_path[(self._normalize_path(path), key)] = value

    def _load(self, path: Path):
        if path.is_dir():
            for locale in path.rglob("zh_cn.*"):
                if locale.suffix.lower() in {".json", ".lang"}:
                    self._add(load_locale(locale), locale.relative_to(path).as_posix())
            return
        if path.suffix.lower() not in {".zip", ".jar"}:
            raise ValueError(f"Unsupported memory pack: {path}")
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                member = PurePosixPath(name)
                if member.name.lower() not in {"zh_cn.json", "zh_cn.lang"}:
                    continue
                raw = archive.read(name).decode("utf-8-sig", errors="replace")
                if member.suffix.lower() == ".json":
                    data = json.loads(raw)
                else:
                    data = {}
                    for line in raw.splitlines():
                        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            data[key.strip()] = value
                if isinstance(data, dict):
                    self._add(data, name)

    def get(self, lang_key: str) -> str | None:
        return self.values.get(lang_key)

    def get_override(self, lang_key: str) -> str | None:
        return self.get(lang_key)

    def get_for(self, path: str, lang_key: str) -> str | None:
        return self.by_path.get((self._normalize_path(path), lang_key), self.get(lang_key))

    def get_override_for(self, path: str, lang_key: str) -> str | None:
        return self.get_for(path, lang_key)

    def get_override_items(self, path: str) -> dict[str, str]:
        normalized = self._normalize_path(path)
        return {key: value for (stored_path, key), value in self.by_path.items() if stored_path == normalized}

    def count(self) -> int:
        return len(self.values)


class CompositeMemory:
    def __init__(self, *memories):
        self.memories = [memory for memory in memories if memory is not None]

    def get(self, lang_key: str) -> str | None:
        for memory in self.memories:
            value = memory.get(lang_key)
            if value is not None:
                return value
        return None

    def get_override(self, lang_key: str) -> str | None:
        for memory in self.memories:
            method = getattr(memory, "get_override", None)
            if method:
                value = method(lang_key)
                if value is not None:
                    return value
        return None

    def get_for(self, path: str, lang_key: str) -> str | None:
        for memory in self.memories:
            method = getattr(memory, "get_for", None)
            value = method(path, lang_key) if method else memory.get(lang_key)
            if value is not None:
                return value
        return None

    def get_override_for(self, path: str, lang_key: str) -> str | None:
        for memory in self.memories:
            method = getattr(memory, "get_override_for", None)
            if method:
                value = method(path, lang_key)
                if value is not None:
                    return value
        return None

    def get_override_items(self, path: str) -> dict[str, str]:
        result = {}
        # Later lower-priority memories are applied first; earlier memories win.
        for memory in reversed(self.memories):
            method = getattr(memory, "get_override_items", None)
            if method:
                result.update(method(path))
        return result
