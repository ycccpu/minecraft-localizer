"""Pure-Python Minecraft localization patch generator."""

__version__ = "0.1.0"

from .pipeline import PatchGenerator, GenerationResult
from .translator import CoalescingTranslator, OpenAITranslator, IdentityTranslator, TranslationCache
from .resource_memory import ResourcePackMemory, CompositeMemory

__all__ = ["__version__", "PatchGenerator", "GenerationResult", "OpenAITranslator", "CoalescingTranslator", "IdentityTranslator", "TranslationCache", "ResourcePackMemory", "CompositeMemory"]
