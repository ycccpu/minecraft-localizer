from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .formats import needs_translation, placeholders_match


TRANSLATABLE_FIELDS = {
    "title", "subtitle", "name", "description", "text", "tooltip", "message",
    "quest_desc", "quest_subtitle", "chapter", "label", "button_text", "display_name",
    "displayname", "warning", "info", "comment",
}
IDENTIFIER_FIELDS = {
    "id", "type", "item", "icon", "advancement", "parent", "category_id", "quest_id",
    "texture", "model", "recipe", "entity", "block", "fluid", "command", "url", "category",
}
SNBT_FIELD_RE = re.compile(
    r'(?P<prefix>\b(?:title|subtitle|description|text|name|quest_desc|quest_subtitle)\s*:\s*)'
    r'(?P<quote>["\'])(?P<value>(?:\\.|(?!\2).)*?)(?P=quote)', re.I | re.S
)
JS_TEXT_RE = re.compile(
    r'(?P<prefix>\b(?:text|title|label|tooltip|description)\s*(?:=|:)\s*)'
    r'(?P<quote>["\'])(?P<value>(?:\\.|(?!\2).)*?)(?P=quote)', re.I
)
CONFIG_COMMENT_RE = re.compile(
    r'(?m)^(?P<prefix>[ \t]*(?:#|;|//)[ \t]*)(?P<quote>)(?P<value>[^\r\n]*[A-Za-z][^\r\n]*)$'
)


@dataclass
class StructuredStats:
    files: int = 0
    strings: int = 0


class StructuredTranslator:
    def __init__(self, translator, memory=None):
        self.translator = translator
        self.memory = memory

    def translate_pairs(self, pairs: list[tuple[str, str]]) -> dict[str, str]:
        result: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        for key, value in pairs:
            remembered = self.memory.get(key) if self.memory else None
            if remembered is not None:
                result[key] = remembered
            elif needs_translation(value):
                pending.append((key, value))
        if pending:
            translated = self.translator.translate_many([value for _, value in pending])
            for (key, source), target in zip(pending, translated):
                result[key] = target if placeholders_match(source, target) else source
        return result

    def translate_json(self, data: Any, namespace: str) -> tuple[Any, int]:
        pairs = self.collect_json(data, namespace)
        translated = self.translate_pairs(pairs)
        return self.apply_json(data, namespace, translated), len(translated)

    def collect_json(self, data: Any, namespace: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        def collect(value, path=(), field=None):
            if isinstance(value, dict):
                for key, child in value.items():
                    collect(child, (*path, str(key)), str(key).lower())
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    collect(child, (*path, str(index)), field)
            elif isinstance(value, str) and field not in IDENTIFIER_FIELDS:
                if field in TRANSLATABLE_FIELDS and needs_translation(value):
                    pairs.append((f"{namespace}.{'/'.join(path)}", value))
        collect(data)
        return pairs

    def apply_json(self, data: Any, namespace: str, translated: dict[str, str]) -> Any:
        def replace(value, path=(), field=None):
            if isinstance(value, dict):
                return {key: replace(child, (*path, str(key)), str(key).lower()) for key, child in value.items()}
            if isinstance(value, list):
                return [replace(child, (*path, str(index)), field) for index, child in enumerate(value)]
            if isinstance(value, str):
                return translated.get(f"{namespace}.{'/'.join(path)}", value)
            return value
        return replace(data)

    def translate_pattern_text(self, text: str, namespace: str, pattern=SNBT_FIELD_RE) -> tuple[str, int]:
        pairs = self.collect_pattern_text(text, namespace, pattern)
        translated = self.translate_pairs(pairs)
        return self.apply_pattern_text(text, namespace, translated, pattern), len(translated)

    @staticmethod
    def collect_pattern_text(text: str, namespace: str, pattern=SNBT_FIELD_RE) -> list[tuple[str, str]]:
        matches = list(pattern.finditer(text))
        return [(f"{namespace}.{index}", match.group("value")) for index, match in enumerate(matches)]

    @staticmethod
    def apply_pattern_text(text: str, namespace: str, translated: dict[str, str], pattern=SNBT_FIELD_RE) -> str:
        matches = list(pattern.finditer(text))
        if not translated:
            return text
        output, cursor = [], 0
        for index, match in enumerate(matches):
            output.append(text[cursor:match.start("value")])
            value = translated.get(f"{namespace}.{index}", match.group("value"))
            quote = match.group("quote")
            if quote:
                value = value.replace("\\", "\\\\").replace(quote, "\\" + quote)
            output.append(value)
            cursor = match.end("value")
        output.append(text[cursor:])
        return "".join(output)


def patchouli_target(member: str) -> str | None:
    parts = list(PurePosixPath(member).parts)
    lowered = [part.lower() for part in parts]
    if "patchouli_books" not in lowered:
        return None
    for index, part in enumerate(lowered):
        if part == "en_us":
            parts[index] = "zh_cn"
            return PurePosixPath(*parts).as_posix()
    return None


def write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
