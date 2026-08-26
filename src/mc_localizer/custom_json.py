from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .formats import placeholders_match


# Exclude Japanese punctuation such as U+30FB ``・``; Chinese translations
# commonly retain it in names and must not be mistaken for unfinished kana.
KANA_RE = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fd-\u30ff]")
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
RPG_CODE_RE = re.compile(r"\\(?:[A-Za-z]+)\[[^\]]*\]|\\[{}.$|!><^]|%(?:\d+\$)?[A-Za-z%]")
ASSIGNMENT_RE = re.compile(r"^(?P<prefix>\s*[A-Za-z_][A-Za-z0-9_.-]*\s*(?:=|:)\s*)(?P<text>.*?)(?P<suffix>\s*)$")


def custom_tokens_match(source: str, target: str) -> bool:
    return (placeholders_match(source, target) and
            sorted(RPG_CODE_RE.findall(source)) == sorted(RPG_CODE_RE.findall(target)))


def dictionary_candidates(data: dict) -> list[tuple[str, str]]:
    """Return ``(dictionary key, source text)`` for unfinished dictionary rows."""
    result = []
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        # Kana is an unambiguous Japanese signal.  For Latin-only dictionaries,
        # require sentence-like whitespace so IDs such as ``ja``/``HP`` are not translated.
        source_has_text = bool(KANA_RE.search(key) or (LATIN_RE.search(key) and re.search(r"\s", key)))
        unfinished = value == key or bool(KANA_RE.search(value))
        if source_has_text and unfinished and not (value == key and CHINESE_RE.search(key) and not KANA_RE.search(key)):
            result.append((key, key))
    return result


def _batches(values: list[str], max_chars: int = 24000, max_items: int = 128) -> list[list[str]]:
    batches, current, size = [], [], 0
    for value in values:
        if current and (len(current) >= max_items or size + len(value) > max_chars):
            batches.append(current); current, size = [], 0
        current.append(value); size += len(value)
    if current:
        batches.append(current)
    return batches


def _translate_values(values: list[str], translator, workers: int,
                      progress: Callable[[str], None] | None, label: str) -> tuple[dict[str, str], list[str]]:
    unique = list(dict.fromkeys(values)); batches = _batches(unique)
    translated, failures = {}, []
    if progress:
        progress(f"{label}已提取：{len(unique)} 条待翻译，{len(batches)} 个批次")
    with ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="custom-api") as pool:
        futures = {pool.submit(translator.translate_many, batch): (number, batch)
                   for number, batch in enumerate(batches, 1)}
        completed = 0
        for future in as_completed(futures):
            number, batch = futures[future]
            try:
                outputs = future.result()
                if len(outputs) != len(batch):
                    raise ValueError("API 返回数量不正确")
                for original, target in zip(batch, outputs):
                    translated[original] = target if target.strip() and custom_tokens_match(original, target) else original
            except Exception as exc:
                failures.append(f"批次 {number}: {type(exc).__name__}: {exc}")
            completed += 1
            if progress:
                progress(f"自定义翻译进度：{completed}/{len(batches)} 批，剩余 {len(batches)-completed} 批")
    return translated, failures


def translate_dictionary_file(source: str | Path, output: str | Path, translator,
                              workers: int = 1, progress: Callable[[str], None] | None = None) -> dict:
    source, output = Path(source), Path(output)
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        raise ValueError("自定义翻译文件必须是 JSON 对象（原文到译文的映射）")
    candidates = dictionary_candidates(data)
    locations: dict[str, list[str]] = {}
    for key, text in candidates:
        locations.setdefault(text, []).append(key)
    translated, failures = _translate_values(list(locations), translator, workers, progress,
                                              f"自定义 JSON（总记录 {len(data)}）")
    changed = 0
    for original, keys in locations.items():
        target = translated.get(original)
        if target is None:
            continue
        for key in keys:
            if data[key] != target:
                data[key] = target; changed += 1
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    temporary.replace(output)
    return {"总记录": len(data), "待翻译": len(locations), "已写回": changed,
            "失败批次": len(failures), "失败样本": failures[:10], "输出文件": str(output)}


def _text_segment(line: str) -> tuple[str, str, str] | None:
    """Return prefix/text/suffix while conservatively preserving unknown TXT syntax."""
    body = line.rstrip("\r\n")
    newline = line[len(body):]
    if not body.strip():
        return None
    match = ASSIGNMENT_RE.match(body)
    if match:
        prefix, text, suffix = match.group("prefix", "text", "suffix")
    else:
        leading = body[:len(body) - len(body.lstrip())]
        trailing = body[len(body.rstrip()):]
        prefix, text, suffix = leading, body.strip(), trailing
    candidate = text.strip()
    if not candidate or re.fullmatch(r"[\d\W_]+", candidate):
        return None
    if re.fullmatch(r"(?:https?://|file:/|[A-Za-z]:[\\/]).+", candidate, re.I):
        return None
    has_kana = bool(KANA_RE.search(candidate))
    natural_latin = bool(LATIN_RE.search(candidate) and re.search(r"\s", candidate))
    if not has_kana and not natural_latin:
        return None
    if CHINESE_RE.search(candidate) and not has_kana:
        return None
    return prefix, candidate, suffix + newline


def translate_text_file(source: str | Path, output: str | Path, translator,
                        workers: int = 1, progress: Callable[[str], None] | None = None) -> dict:
    source, output = Path(source), Path(output)
    raw = source.read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        content = raw.decode(encoding)
    except UnicodeDecodeError:
        try:
            content = raw.decode("cp932")
        except UnicodeDecodeError:
            content = raw.decode("gb18030")
    lines = content.splitlines(keepends=True)
    segments, values = [], []
    for index, line in enumerate(lines):
        segment = _text_segment(line)
        if segment:
            segments.append((index, *segment)); values.append(segment[1])
    translated, failures = _translate_values(values, translator, workers, progress,
                                              f"自定义 TXT（总行数 {len(lines)}）")
    changed = 0
    for index, prefix, text, suffix in segments:
        target = translated.get(text, text)
        lines[index] = prefix + target + suffix
        changed += target != text
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    # Write bytes so Windows does not expand preserved CRLF into CRCRLF.
    temporary.write_bytes("".join(lines).encode("utf-8"))
    temporary.replace(output)
    return {"总行数": len(lines), "待翻译": len(set(values)), "已写回": changed,
            "失败批次": len(failures), "失败样本": failures[:10], "输出编码": "UTF-8",
            "输出文件": str(output)}
