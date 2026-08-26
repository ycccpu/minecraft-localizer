from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252")
PLACEHOLDER_RE = re.compile(r"%(?:\d+\$)?[a-zA-Z%]|\{[^{}]+\}|§[0-9a-fk-or]", re.I)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


def detect_encoding(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for encoding in ENCODINGS:
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            pass
    return "utf-8"


def read_text(path: str | Path) -> str:
    path = Path(path)
    return path.read_text(encoding=detect_encoding(path), errors="replace")


def load_json(path: str | Path) -> dict[str, Any]:
    # Some mods ship literal control characters in otherwise valid locale
    # JSON.  Minecraft accepts these files; strict=False mirrors that behavior.
    data = json.loads(read_text(path), strict=False)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_lang(path: str | Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value
    return result


def save_lang(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in data.items()), encoding="utf-8")


def load_locale(path: str | Path) -> dict[str, Any]:
    return load_json(path) if Path(path).suffix.lower() == ".json" else load_lang(path)


def save_locale(path: str | Path, data: dict[str, Any]) -> None:
    if Path(path).suffix.lower() == ".json":
        save_json(path, data)
    else:
        save_lang(path, data)


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            result.update(flatten(value, full_key))
        elif isinstance(value, (str, int, float, bool)):
            result[full_key] = str(value)
    return result


def placeholders(text: str) -> tuple[str, ...]:
    return tuple(PLACEHOLDER_RE.findall(text))


def placeholders_match(source: str, translated: str) -> bool:
    return sorted(placeholders(source)) == sorted(placeholders(translated))


def needs_translation(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or CHINESE_RE.search(value):
        return False
    return bool(re.search(r"[A-Za-z]", value))


def chunks(items: Iterable[tuple[str, str]], max_chars: int = 12000):
    batch: list[tuple[str, str]] = []
    size = 0
    for item in items:
        item_size = len(item[0]) + len(item[1])
        if batch and size + item_size > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch
