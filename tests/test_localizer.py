from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mc_localizer.formats import load_lang, placeholders_match
from mc_localizer.custom_json import custom_tokens_match, dictionary_candidates, translate_dictionary_file, translate_text_file
from mc_localizer.cfpa import asset_name, package_version
from mc_localizer.pipeline import PatchGenerator
from mc_localizer.resource_memory import ResourcePackMemory, CompositeMemory
from mc_localizer.translator import CoalescingTranslator, OpenAITranslator, TranslationCache
from mc_localizer.gui import App


class FakeTranslator:
    def translate_many(self, values):
        return [f"中:{value}" for value in values]


class LocalizerTests(unittest.TestCase):
    def test_custom_dictionary_translates_only_unfinished_text_without_cache(self):
        data = {"コンティニュー": "コンティニュー", "称号": "称号", "ja": "ja",
                "防御崩し": "防御崩溃。", "St.リフレクト": "St.リフレクト"}
        self.assertEqual([key for key, _ in dictionary_candidates(data)],
                         ["コンティニュー", "St.リフレクト"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.json"; output = root / "output.json"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            result = translate_dictionary_file(source, output, FakeTranslator(), workers=2)
            translated = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(translated["コンティニュー"], "中:コンティニュー")
            self.assertEqual(translated["称号"], "称号")
            self.assertEqual(translated["ja"], "ja")
            self.assertEqual(result["待翻译"], 2)

    def test_custom_dictionary_preserves_rpg_maker_control_codes(self):
        self.assertTrue(custom_tokens_match(r"\N[1] uses \C[2]Fire\C[0]", r"\N[1]使用\C[2]火焰\C[0]"))
        self.assertFalse(custom_tokens_match(r"\N[1] uses \V[3]", "使用变量"))

    def test_custom_txt_preserves_layout_and_assignment_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.txt"; output = root / "output.txt"
            source.write_bytes("title=New Game\r\n  コンティニュー  \r\nhttps://example.com/file\r\nid_only\r\n\r\n".encode("utf-8"))
            result = translate_text_file(source, output, FakeTranslator(), workers=2)
            with output.open("r", encoding="utf-8", newline="") as stream:
                translated = stream.read()
            self.assertIn("title=中:New Game\r\n", translated)
            self.assertIn("  中:コンティニュー  \r\n", translated)
            self.assertIn("https://example.com/file\r\n", translated)
            self.assertIn("id_only\r\n", translated)
            self.assertEqual(result["待翻译"], 2)

    def test_cfpa_asset_matches_game_version_and_loader(self):
        self.assertEqual(package_version("1.21.1"), "1.21")
        self.assertEqual(asset_name("1.21.1"), "Minecraft-Mod-Language-Modpack-1-21.zip")
        self.assertEqual(asset_name("1.20.1", True), "Minecraft-Mod-Language-Modpack-1-20-fabric.zip")
        self.assertIsNone(asset_name("1.22.0"))
    def test_version_metadata_beats_folder_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"自定义实例"; root.mkdir()
            (root/"manifest.json").write_text(json.dumps({"minecraft":{"version":"1.20.4"}}),encoding="utf-8")
            generator=PatchGenerator(root,Path(temp)/"out")
            self.assertEqual(generator._minecraft_version_info(),("1.20.4","版本元数据 manifest.json"))
            self.assertEqual(generator._pack_format(),22)

    def test_glossary_loader_cleans_empty_terms(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"glossary.json"
            path.write_text(json.dumps({" Mana ":" 魔力 ","":"无效","Empty":" "}),encoding="utf-8")
            self.assertEqual(App.load_glossary(path), {"Mana":"魔力"})

    def test_glossary_affects_prompt_and_cache_namespace(self):
        one = OpenAITranslator("key", "https://example.test/v1", "model", glossary={"Mana": "魔力"})
        two = OpenAITranslator("key", "https://example.test/v1", "model", glossary={"Mana": "法力"})
        self.assertIn("Mana => 魔力", one._build_payload(["Mana"])["messages"][0]["content"])
        self.assertNotEqual(one.cache_namespace, two.cache_namespace)

    def test_pack_format_tracks_minecraft_version(self):
        self.assertEqual(PatchGenerator("instance-1.21.1", "out")._pack_format(), 34)
        self.assertEqual(PatchGenerator("instance-1.20.1", "out")._pack_format(), 15)
        with self.assertRaises(ValueError):
            PatchGenerator("instance-9.9.9", "out", finalize_output=True)._pack_format()

    def test_overlapping_output_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/"instance"; source.mkdir()
            with self.assertRaises(ValueError):
                PatchGenerator(source,source,finalize_output=True).generate()
            nested=source/"output"
            with self.assertRaises(ValueError):
                PatchGenerator(source,nested,finalize_output=True).generate()

    def test_scopes_can_disable_config_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/"instance"; config=source/"config/demo.toml"
            config.parent.mkdir(parents=True); config.write_text("# Explain option\nenabled=true\n",encoding="utf-8")
            PatchGenerator(source, root/"out", FakeTranslator(), scopes={"locale"}).generate()
            self.assertFalse((root/"out/config/demo.toml").exists())

    def test_atomic_generation_preserves_previous_release_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input"; source.mkdir()
            output = root / "release"; output.mkdir()
            (output / "previous.txt").write_text("keep", encoding="utf-8")
            class BrokenGenerator(PatchGenerator):
                def _generate_current_output(self):
                    (self.output / "partial.txt").write_text("partial", encoding="utf-8")
                    raise RuntimeError("simulated failure")
            with self.assertRaises(RuntimeError):
                BrokenGenerator(source, output, FakeTranslator(), finalize_output=True).generate()
            self.assertEqual((output / "previous.txt").read_text(encoding="utf-8"), "keep")
            self.assertFalse((output / "partial.txt").exists())

    def test_translation_cache_namespaces_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TranslationCache(Path(temp) / "translation_cache.db")
            cache.put("same-model", "Hello", "服务一", "provider-one|prompt-v1")
            cache.put("same-model", "Hello", "服务二", "provider-two|prompt-v1")
            self.assertEqual(cache.get("same-model", "Hello", "provider-one|prompt-v1"), "服务一")
            self.assertEqual(cache.get("same-model", "Hello", "provider-two|prompt-v1"), "服务二")
            cache.put("same-model", "Legacy", "旧译文")
            self.assertIsNone(cache.get("same-model", "Legacy", "provider-one|prompt-v1"))

    def test_translation_cache_can_edit_and_delete_records(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TranslationCache(Path(temp) / "translation_cache.db")
            cache.put("model", "Hello", "你好", "provider")
            cache.put("model", "World", "世界", "provider")
            self.assertEqual(cache.count(), 2)
            self.assertEqual(len(cache.detailed_entries(limit=1, offset=1)), 1)
            key = next(row[0] for row in cache.detailed_entries() if row[2] == "Hello")
            cache.set_locked([key], True)
            self.assertTrue(next(row[4] for row in cache.detailed_entries() if row[0] == key))
            self.assertEqual(cache.delete([key]), 0)
            cache.put("model", "Hello", "API覆盖", "provider")
            self.assertEqual(cache.get("model", "Hello", "provider"), "你好")
            cache.set_locked([key], False)
            cache.update(key, "您好")
            self.assertEqual(cache.get("model", "Hello", "provider"), "您好")
            self.assertEqual(cache.delete([key]), 1)

    def test_translation_cache_context_uses_path_key_and_source(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TranslationCache(Path(temp) / "translation_cache.db")
            cache.put_context("model", "namespace", "locale", "assets/demo/lang/zh_cn.json",
                              "gui.demo.title", "Machine Settings", "机器设置")
            self.assertEqual(cache.get_context("model", "namespace", "locale",
                                               "assets/demo/lang/zh_cn.json", "gui.demo.title",
                                               "Machine Settings"), "机器设置")
            self.assertIsNone(cache.get_context("model", "namespace", "locale",
                                                "assets/demo/lang/zh_cn.json", "gui.demo.title",
                                                "New Machine Settings"))
            self.assertIsNone(cache.get_context("model", "namespace", "locale",
                                                "assets/other/lang/zh_cn.json", "gui.demo.title",
                                                "Machine Settings"))
            self.assertIsNone(cache.get("model", "Hello", "provider"))

    def test_translation_cache_bulk_context_write(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TranslationCache(Path(temp) / "translation_cache.db")
            cache.put_context_many("model", "namespace", [
                ("structured", "config/one.toml", "comment.1", "First", "第一"),
                ("structured", "config/two.toml", "comment.2", "Second", "第二"),
            ])
            self.assertEqual(cache.get_context("model", "namespace", "structured",
                                               "config/one.toml", "comment.1", "First"), "第一")
            self.assertEqual(cache.get_context("model", "namespace", "structured",
                                               "config/two.toml", "comment.2", "Second"), "第二")

    def test_plain_single_item_fallback_validation(self):
        self.assertEqual(OpenAITranslator._parse_plain_translation('"启用较低细节层级"', "Enable lower detail levels"),
                         "启用较低细节层级")
        self.assertEqual(OpenAITranslator._parse_plain_translation("翻译：你好 %s", "Hello %s"), "你好 %s")
        with self.assertRaises(ValueError):
            OpenAITranslator._parse_plain_translation("你好", "Hello %s")

    def test_safe_config_comments_and_world_serverconfig_are_translated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "instance"
            config = source / "config/demo.toml"; config.parent.mkdir(parents=True)
            config.write_text('# Enable the magic feature\nenabled = true\n', encoding="utf-8")
            server = source / "saves/测试世界/serverconfig/demo.snbt"; server.parent.mkdir(parents=True)
            server.write_text('title: "Server options"\ndifficulty: "hard"\n', encoding="utf-8")
            player = source / "saves/测试世界/playerdata/user.snbt"; player.parent.mkdir(parents=True)
            player.write_text('title: "Must not be copied"\n', encoding="utf-8")
            result = PatchGenerator(source, root / "out", FakeTranslator()).generate()
            translated_config = (root / "out/config/demo.toml").read_text(encoding="utf-8")
            self.assertIn('# 中:Enable the magic feature', translated_config)
            self.assertIn('enabled = true', translated_config)
            translated_server = (root / "out/saves/测试世界/serverconfig/demo.snbt").read_text(encoding="utf-8")
            self.assertIn('title: "中:Server options"', translated_server)
            self.assertIn('difficulty: "hard"', translated_server)
            self.assertFalse((root / "out/saves/测试世界/playerdata/user.snbt").exists())
            self.assertGreaterEqual(result.specialized_files, 2)

    def test_shader_folder_and_zip_locales_are_translated_without_touching_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); shaders = root / "input/shaderpacks"; shaders.mkdir(parents=True)
            folder_lang = shaders / "FolderPack/shaders/lang/en_us.lang"
            folder_lang.parent.mkdir(parents=True)
            folder_lang.write_text("option.shadow=Shadow Quality\nscreen.lighting=Lighting\n", encoding="utf-8")
            with zipfile.ZipFile(shaders / "ZipPack.zip", "w") as archive:
                archive.writestr("shaders/lang/en_us.lang", "option.water=Water Quality\n")
                archive.writestr("shaders/program/test.glsl", "void main() {}")
            PatchGenerator(root / "input", root / "out", FakeTranslator()).generate()
            folder_zh = (root / "out/shaderpacks/FolderPack/shaders/lang/zh_cn.lang").read_text(encoding="utf-8")
            self.assertIn("option.shadow=中:Shadow Quality", folder_zh)
            with zipfile.ZipFile(root / "out/shaderpacks/ZipPack.zip") as archive:
                self.assertIn("option.water=中:Water Quality", archive.read("shaders/lang/zh_cn.lang").decode())
                self.assertEqual(archive.read("shaders/program/test.glsl"), b"void main() {}")

    def test_config_comment_match_never_consumes_section_or_newline(self):
        from mc_localizer.structured import CONFIG_COMMENT_RE
        text = '#\n[UI]\n# Explain this option\nenabled = true\n\ngeneral {\n'
        values = [match.group("value") for match in CONFIG_COMMENT_RE.finditer(text)]
        self.assertEqual(values, ["Explain this option"])

    def test_failed_large_api_batch_is_bisected(self):
        class SizeLimitedTranslator:
            def translate_many(self, values):
                if len(values) > 2:
                    raise ValueError("missing IDs")
                return [f"译:{value}" for value in values]
        values = [str(index) for index in range(7)]
        translated, failures = PatchGenerator(".", "out")._translate_batch_resilient(SizeLimitedTranslator(), values)
        self.assertEqual(translated, [f"译:{value}" for value in values])
        self.assertEqual(failures, [])

    def test_failed_single_item_does_not_discard_successful_siblings(self):
        class SelectiveTranslator:
            def translate_many(self, values):
                if "bad" in values:
                    raise ValueError("invalid JSON")
                return [f"译:{value}" for value in values]
        translated, failures = PatchGenerator(".", "out")._translate_batch_resilient(
            SelectiveTranslator(), ["good-1", "bad", "good-2"])
        self.assertEqual(translated, ["译:good-1", "bad", "译:good-2"])
        self.assertEqual(failures[0][0], "bad")

    def test_translation_cache_keeps_readable_source_model_and_search(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = TranslationCache(Path(temp) / "translation_cache.db")
            cache.put("demo-model", "Hello world", "你好，世界")
            self.assertEqual(cache.get("demo-model", "Hello world"), "你好，世界")
            self.assertEqual(cache.entries("world"), [("demo-model", "Hello world", "你好，世界")])
            self.assertEqual(cache.entries("不存在"), [])

    def test_resource_pack_matching_ignores_loader_version(self):
        packs = [Path("1.20.1.zip"), Path("1.21.1.zip")]
        source = r"F:\MC\versions\1.21.1-NeoForge_21.1.248"
        self.assertEqual(App._matching_pack(packs, source), Path("1.21.1.zip"))

    def test_deepseek_reasoning_effort_payload(self):
        disabled = OpenAITranslator("key", "https://api.deepseek.com/v1", "deepseek-v4-flash",
                                    reasoning_effort="disabled")._build_payload(["Hello"])
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", disabled)
        high = OpenAITranslator("key", "https://api.deepseek.com/v1", "deepseek-v4-flash",
                                reasoning_effort="high")._build_payload(["Hello"])
        self.assertEqual(high["thinking"], {"type": "enabled"})
        self.assertEqual(high["reasoning_effort"], "high")
        openai = OpenAITranslator("key", "https://api.openai.com/v1", "gpt-4.1-mini",
                                  reasoning_effort="high")._build_payload(["Hello"])
        self.assertNotIn("thinking", openai)

    def test_translation_ids_restore_original_order_and_reject_gaps(self):
        content = json.dumps({"2": "第三", "0": "第一", "1": "第二"}, ensure_ascii=False)
        self.assertEqual(OpenAITranslator._parse_translations(content, 3), ["第一", "第二", "第三"])
        with self.assertRaises(ValueError):
            OpenAITranslator._parse_translations(json.dumps({"0": "第一", "2": "第三"}), 3)

    def test_batch_sizing_tracks_concurrency_and_never_splits_text(self):
        class RecordingTranslator:
            def __init__(self): self.received = []
            def translate_many(self, values):
                self.received.extend(values)
                return list(values)
        low = CoalescingTranslator(RecordingTranslator(), workers=4)
        high = CoalescingTranslator(RecordingTranslator(), workers=32)
        self.assertGreater(low.max_chars, high.max_chars)
        low.close(); high.close()
        recording = RecordingTranslator()
        translator = CoalescingTranslator(recording, workers=2, max_chars=10, max_items=2)
        long_text = "A" * 5000
        try:
            self.assertEqual(translator.translate_many([long_text]), [long_text])
        finally:
            translator.close()
        self.assertEqual(recording.received, [long_text])

    def test_concurrent_translation_jobs_are_coalesced(self):
        class CountingTranslator:
            def __init__(self): self.calls = []
            def translate_many(self, values):
                self.calls.append(list(values))
                return [f"中:{value}" for value in values]
        underlying = CountingTranslator()
        translator = CoalescingTranslator(underlying, workers=2, gather_seconds=0.1)
        try:
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(lambda i: translator.translate_many([f"Text {i}"]), range(12)))
        finally:
            translator.close()
        self.assertEqual(results[5], ["中:Text 5"])
        self.assertLess(len(underlying.calls), 12)
        self.assertEqual(sum(map(len, underlying.calls)), 12)

    def test_placeholder_validation(self):
        self.assertTrue(placeholders_match("Hello %s {name}", "你好 %s {name}"))
        self.assertFalse(placeholders_match("Hello %s", "你好"))

    def test_directory_generation_and_existing_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lang = root / "input/assets/demo/lang"
            lang.mkdir(parents=True)
            (lang / "en_us.json").write_text(json.dumps({"a": "Hello %s", "b": "World"}), encoding="utf-8")
            output_existing = root / "out/assets/demo/lang/zh_cn.json"
            output_existing.parent.mkdir(parents=True)
            output_existing.write_text(json.dumps({"b": "世界"}), encoding="utf-8")
            result = PatchGenerator(root / "input", root / "out", FakeTranslator()).generate()
            data = json.loads(output_existing.read_text(encoding="utf-8"))
            self.assertEqual(data["a"], "中:Hello %s")
            self.assertEqual(data["b"], "世界")
            self.assertEqual(result.output_files, 1)

    def test_jar_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jar = root / "demo.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/demo/lang/en_us.lang", "hello=Hello\n")
            result = PatchGenerator(jar, root / "out", FakeTranslator()).generate()
            self.assertEqual(load_lang(root / "out/assets/demo/lang/zh_cn.lang")["hello"], "中:Hello")
            self.assertEqual(result.scanned_archives, 1)

    def test_game_runtime_directories_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_jar = root / "input/versions/1.21.1-NeoForge_21.1.248/1.21.1-NeoForge_21.1.248.jar"
            version_jar.parent.mkdir(parents=True)
            with zipfile.ZipFile(version_jar, "w") as archive:
                archive.writestr("assets/game/lang/en_us.json", json.dumps({"skip": "Skip me"}))
            mod_jar = root / "input/mods/demo.jar"
            mod_jar.parent.mkdir(parents=True)
            with zipfile.ZipFile(mod_jar, "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({"keep": "Translate me"}))
            result = PatchGenerator(root / "input", root / "out", FakeTranslator()).generate()
            self.assertEqual(result.scanned_archives, 1)
            self.assertTrue((root / "out/assets/demo/lang/zh_cn.json").exists())
            self.assertFalse((root / "out/assets/game/lang/zh_cn.json").exists())

    def test_root_neoforge_runtime_jar_is_skipped_by_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "input/1.21.1-NeoForge_21.1.248.jar"
            runtime.parent.mkdir(parents=True)
            with zipfile.ZipFile(runtime, "w") as archive:
                archive.writestr("assets/game/lang/en_us.json", json.dumps({"skip": "Skip me"}))
            result = PatchGenerator(root / "input", root / "out", FakeTranslator()).generate()
            self.assertEqual(result.scanned_archives, 0)
            self.assertFalse((root / "out/assets/game/lang/zh_cn.json").exists())

    def test_archives_can_be_scanned_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mods = root / "input/mods"
            mods.mkdir(parents=True)
            for index in range(8):
                with zipfile.ZipFile(mods / f"mod-{index}.jar", "w") as archive:
                    archive.writestr(f"assets/mod{index}/lang/en_us.json", json.dumps({"name": f"Mod {index}"}))
            progress = []
            result = PatchGenerator(root / "input", root / "out", FakeTranslator(),
                                    progress=progress.append, max_workers=4).generate()
            self.assertEqual(result.scanned_archives, 8)
            self.assertEqual(result.output_files, 8)
            for index in range(8):
                self.assertTrue((root / f"out/assets/mod{index}/lang/zh_cn.json").exists())
            self.assertTrue(any("剩余 0" in message for message in progress))

    def test_global_collection_deduplicates_and_writes_back_to_each_mod(self):
        class RecordingTranslator:
            def __init__(self): self.values = []
            def translate_many(self, values):
                self.values.extend(values)
                return [f"译:{value}" for value in values]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); mods = root / "input/mods"; mods.mkdir(parents=True)
            with zipfile.ZipFile(mods / "one.jar", "w") as archive:
                archive.writestr("assets/one/lang/en_us.json", json.dumps({"shared": "Shared", "one": "First"}))
            with zipfile.ZipFile(mods / "two.jar", "w") as archive:
                archive.writestr("assets/two/lang/en_us.json", json.dumps({"shared": "Shared", "two": "Second"}))
            translator = RecordingTranslator()
            PatchGenerator(root / "input", root / "out", translator, max_workers=2).generate()
            one = json.loads((root / "out/assets/one/lang/zh_cn.json").read_text(encoding="utf-8"))
            two = json.loads((root / "out/assets/two/lang/zh_cn.json").read_text(encoding="utf-8"))
            self.assertEqual(translator.values.count("Shared"), 1)
            self.assertEqual(one, {"shared": "译:Shared", "one": "译:First"})
            self.assertEqual(two, {"shared": "译:Shared", "two": "译:Second"})

    def test_duplicate_locale_paths_are_merged_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); mods = root / "input/mods"; mods.mkdir(parents=True)
            with zipfile.ZipFile(mods / "base.jar", "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({"menu.main": "Main", "menu.second": "Second"}))
            with zipfile.ZipFile(mods / "addon.jar", "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({"menu.addon": "Addon"}))
            PatchGenerator(root / "input", root / "out", FakeTranslator(), max_workers=2).generate()
            data = json.loads((root / "out/assets/demo/lang/zh_cn.json").read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"menu.main", "menu.second", "menu.addon"})

    def test_structured_text_is_globally_deduplicated_and_restored(self):
        class RecordingTranslator:
            def __init__(self): self.values = []
            def translate_many(self, values):
                self.values.extend(values)
                return [f"译:{value}" for value in values]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); mods = root / "input/mods"; mods.mkdir(parents=True)
            for namespace in ("one", "two"):
                with zipfile.ZipFile(mods / f"{namespace}.jar", "w") as archive:
                    archive.writestr(
                        f"assets/{namespace}/patchouli_books/guide/en_us/entries/start.json",
                        json.dumps({"name": "Shared Chapter", "pages": [{"type": "patchouli:text", "text": "Shared Body"}]}))
            translator = RecordingTranslator()
            result = PatchGenerator(root / "input", root / "out", translator, max_workers=2).generate()
            self.assertEqual(translator.values.count("Shared Chapter"), 1)
            self.assertEqual(translator.values.count("Shared Body"), 1)
            for namespace in ("one", "two"):
                target = root / f"out/assets/{namespace}/patchouli_books/guide/zh_cn/entries/start.json"
                data = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(data["name"], "译:Shared Chapter")
                self.assertEqual(data["pages"][0]["text"], "译:Shared Body")
            self.assertEqual(result.specialized_files, 2)

    def test_distribution_creates_two_minecraft_resource_packs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); mods = root / "input-1.21.1/mods"; mods.mkdir(parents=True)
            with zipfile.ZipFile(mods / "demo.jar", "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({"name": "Demo"}))
                archive.writestr("assets/demo/patchouli_books/guide/en_us/entries/start.json",
                                 json.dumps({"name": "Guide"}))
            result = PatchGenerator(root / "input-1.21.1", root / "out", FakeTranslator()).generate()
            self.assertEqual(len(result.resource_packs), 2)
            standard = next(Path(path) for path in result.resource_packs if "补汉材质包" in path)
            patchouli = next(Path(path) for path in result.resource_packs if "帕秋莉" in path)
            self.assertTrue((standard / "assets/demo/lang/zh_cn.json").is_file())
            self.assertFalse(any("patchouli_books" in path.as_posix() for path in standard.rglob("*")))
            self.assertTrue((patchouli / "assets/demo/patchouli_books/guide/zh_cn/entries/start.json").is_file())
            self.assertFalse(any("/lang/" in path.as_posix() for path in patchouli.rglob("*")))

    def test_absent_content_types_do_not_create_empty_resource_packs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input-1.21.1"
            config = source / "config/demo.toml"; config.parent.mkdir(parents=True)
            config.write_text("# Enable this feature\nenabled=true\n", encoding="utf-8")
            result = PatchGenerator(source, root / "out", FakeTranslator(), finalize_output=True).generate()
            self.assertEqual(result.resource_packs, [])
            self.assertFalse((root / "out/resourcepacks").exists())
            self.assertTrue((root / "out/config/demo.toml").is_file())
            manifest = json.loads((root / "out/发布清单.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["资源包"], [])

    def test_finalized_output_keeps_only_ready_to_use_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input-1.21.1"
            lang = source / "assets/demo/lang"; lang.mkdir(parents=True)
            (lang / "en_us.json").write_text(json.dumps({"name": "Demo"}), encoding="utf-8")
            quest = source / "config/ftbquests/quests/chapter.snbt"; quest.parent.mkdir(parents=True)
            quest.write_text('title: "Chapter"\n', encoding="utf-8")
            result = PatchGenerator(source, root / "out", FakeTranslator(), finalize_output=True).generate()
            children = {path.name for path in (root / "out").iterdir()}
            self.assertEqual(children, {"resourcepacks", "config", "使用说明.txt", "质量报告.json", "发布清单.json"})
            self.assertFalse((root / "out/assets").exists())
            self.assertTrue(result.quality_report["是否可用"])
            saved_quality = json.loads((root / "out/质量报告.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_quality, result.quality_report)
            self.assertTrue((root / "out/config/ftbquests/quests/chapter.snbt").is_file())
            manifest = json.loads((root / "out/发布清单.json").read_text(encoding="utf-8"))
            self.assertIn("config/ftbquests/quests/chapter.snbt", manifest["安装文件"])
            self.assertEqual(len(manifest["资源包"]), 1)

    def test_bundled_chinese_has_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jar = root / "demo.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({"hello": "Hello", "new": "New"}))
                archive.writestr("assets/demo/lang/zh_cn.json", json.dumps({"hello": "官方中文"}))
            PatchGenerator(jar, root / "out", FakeTranslator()).generate()
            data = json.loads((root / "out/assets/demo/lang/zh_cn.json").read_text(encoding="utf-8"))
            self.assertEqual(data["hello"], "官方中文")
            self.assertEqual(data["new"], "中:New")

    def test_blank_bundled_chinese_is_automatically_translated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jar = root / "demo.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/demo/lang/en_us.json", json.dumps({
                    "blank": "Trapezoid", "intentional": ""
                }))
                archive.writestr("assets/demo/lang/zh_cn.json", json.dumps({
                    "blank": "", "intentional": ""
                }))
            PatchGenerator(jar, root / "out", FakeTranslator()).generate()
            data = json.loads((root / "out/assets/demo/lang/zh_cn.json").read_text(encoding="utf-8"))
            self.assertEqual(data["blank"], "中:Trapezoid")
            self.assertEqual(data["intentional"], "")

    def test_quality_report_only_counts_actual_config_comments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input"
            config = source / "config/demo.toml"
            config.parent.mkdir(parents=True)
            config.write_text('[server_scheduler]\nThreadCount = 0\n# English comment\n', encoding="utf-8")
            result = PatchGenerator(source, root / "out", FakeTranslator()).generate()
            samples = result.quality_report.get("未翻译配置注释样本", [])
            self.assertEqual(result.quality_report.get("未翻译配置注释", 0), 0)
            self.assertFalse(any("ThreadCount" in item["文本"] for item in samples))

    def test_resource_pack_memory_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "translated.zip"
            with zipfile.ZipFile(pack, "w") as archive:
                archive.writestr("assets/demo/lang/zh_cn.json", json.dumps({"item.demo.name": "补丁译名"}))
            pack_memory = ResourcePackMemory([pack])
            class LowerPriority:
                def get(self, key): return "旧缓存译名"
            memory = CompositeMemory(pack_memory, LowerPriority())
            self.assertEqual(memory.get("item.demo.name"), "补丁译名")

    def test_empty_resource_pack_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pack = root / "translated.zip"
            with zipfile.ZipFile(pack, "w") as archive:
                archive.writestr("assets/demo/lang/zh_cn.json", json.dumps({
                    "item.demo.name": "资源包译名", "empty": "", "blank": "   "
                }))
            pack_memory = ResourcePackMemory([pack])
            self.assertEqual(pack_memory.get("item.demo.name"), "资源包译名")
            self.assertIsNone(pack_memory.get("empty"))
            self.assertIsNone(pack_memory.get("blank"))

    def test_resource_memory_can_fill_empty_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input/assets/demo/lang/en_us.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({"subtitle.demo": ""}), encoding="utf-8")
            pack = root / "memory.zip"
            with zipfile.ZipFile(pack, "w") as archive:
                archive.writestr("assets/demo/lang/zh_cn.json", json.dumps({"subtitle.demo": "补全字幕"}))
            result = PatchGenerator(root / "input", root / "out", FakeTranslator(), CompositeMemory(ResourcePackMemory([pack]))).generate()
            data = json.loads((root / "out/assets/demo/lang/zh_cn.json").read_text(encoding="utf-8"))
            self.assertEqual(data["subtitle.demo"], "补全字幕")
            self.assertEqual(result.memory_hits, 1)

    def test_patchouli_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jar = root / "book.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/demo/patchouli_books/guide/en_us/entries/start.json",
                                 json.dumps({"name": "Getting Started", "category": "demo:start", "pages": [{"type": "patchouli:text", "text": "Hello %s"}]}))
            result = PatchGenerator(jar, root / "out", FakeTranslator()).generate()
            target = root / "out/assets/demo/patchouli_books/guide/zh_cn/entries/start.json"
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["name"], "中:Getting Started")
            self.assertEqual(data["pages"][0]["text"], "中:Hello %s")
            self.assertEqual(data["category"], "demo:start")
            self.assertEqual(result.specialized_files, 1)

    def test_quest_snbt_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            quest = root / "input/config/ftbquests/quests/chapter.snbt"
            quest.parent.mkdir(parents=True)
            quest.write_text('title: "First Chapter"\ndescription: ["Hello Adventurer"]\nid: "ABC123"\n', encoding="utf-8")
            result = PatchGenerator(root / "input", root / "out", FakeTranslator()).generate()
            output = root / "out/config/ftbquests/quests/chapter.snbt"
            text = output.read_text(encoding="utf-8")
            self.assertIn("中:First Chapter", text)
            self.assertIn('id: "ABC123"', text)
            self.assertEqual(result.specialized_files, 1)


if __name__ == "__main__":
    unittest.main()
