from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .pipeline import PatchGenerator
from .translator import IdentityTranslator, OpenAITranslator, TranslationCache
from .resource_memory import ResourcePackMemory
from . import __version__


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="mc-localizer", description="Minecraft 汉化补丁生成器")
    command.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    command.add_argument("source", help="整合包目录、资源目录、JAR 或 ZIP")
    command.add_argument("output", help="输出资源包目录")
    command.add_argument("--api-key", default=os.getenv("MC_LOCALIZER_API_KEY"))
    command.add_argument("--base-url", default=os.getenv("MC_LOCALIZER_BASE_URL", "https://api.openai.com/v1"))
    command.add_argument("--model", default=os.getenv("MC_LOCALIZER_MODEL", "gpt-4.1-mini"))
    command.add_argument("--cache", default="translation_cache.db")
    command.add_argument("--memory-pack", action="append", default=[], help="已有中文资源包目录/ZIP，可重复指定")
    command.add_argument("--zip", dest="zip_path", help="额外生成 ZIP 补丁")
    return command


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    translator = IdentityTranslator()
    if args.api_key:
        translator = OpenAITranslator(args.api_key, args.base_url, args.model, TranslationCache(args.cache))
    memory = ResourcePackMemory(args.memory_pack) if args.memory_pack else None
    generator = PatchGenerator(args.source, args.output, translator, memory)
    result = generator.generate()
    if args.zip_path:
        generator.make_zip(args.zip_path)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if not result.warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())
