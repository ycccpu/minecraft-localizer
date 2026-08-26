from __future__ import annotations

import html
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


REPOSITORY = "https://github.com/CFPAOrg/Minecraft-Mod-Language-Package"
RELEASE_FEED = REPOSITORY + "/releases.atom"
SUPPORTED = {"1.12.2", "1.16", "1.18", "1.19", "1.20", "1.21"}
FABRIC_SUPPORTED = {"1.16", "1.18", "1.20", "1.21"}


def package_version(game_version: str) -> str | None:
    if game_version == "1.12.2": return game_version
    match = re.match(r"^(1\.\d+)", game_version)
    value = match.group(1) if match else None
    return value if value in SUPPORTED else None


def detect_fabric(source: str | Path) -> bool:
    source = Path(source)
    clues = [source.name.lower()]
    if source.is_dir():
        for path in list(source.glob("*.json"))[:20]:
            if path.stat().st_size > 2_000_000: continue
            try: clues.append(path.read_text(encoding="utf-8-sig", errors="ignore").lower())
            except OSError: pass
    text = "\n".join(clues)
    return any(token in text for token in ("fabric-loader", "fabricloader", "quilt-loader", "quiltloader", "-fabric"))


def asset_name(game_version: str, fabric: bool = False) -> str | None:
    version = package_version(game_version)
    if not version: return None
    suffix = "-fabric" if fabric and version in FABRIC_SUPPORTED else ""
    return f"Minecraft-Mod-Language-Modpack-{version.replace('.', '-')}{suffix}.zip"


def _request(url: str, timeout: int = 30):
    request = urllib.request.Request(url, headers={"User-Agent": "Minecraft-Localizer/1.0"})
    return urllib.request.urlopen(request, timeout=timeout)


def _find_asset_url(filename: str) -> str:
    with _request(RELEASE_FEED) as response:
        root = ET.fromstring(response.read())
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("atom:entry", namespace):
        link = entry.find("atom:link", namespace)
        if link is None or not link.get("href"): continue
        tag = link.get("href", "").rstrip("/").rsplit("/", 1)[-1]
        with _request(f"{REPOSITORY}/releases/expanded_assets/{tag}") as response:
            page = response.read().decode("utf-8", errors="replace")
        pattern = rf'href="([^"]*/{re.escape(filename)})"'
        match = re.search(pattern, page, re.I)
        if match:
            return "https://github.com" + html.unescape(match.group(1))
    raise FileNotFoundError(f"CFPA 最近发布中没有 {filename}")


def _valid_pack(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return any(Path(name).name.lower() in {"zh_cn.json", "zh_cn.lang"}
                       for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def ensure_cfpa_pack(game_version: str, source: str | Path, cache_dir: str | Path,
                     max_age_hours: int = 24, report=None) -> Path | None:
    filename = asset_name(game_version, detect_fabric(source))
    if not filename:
        if report: report(f"CFPA 暂无 Minecraft {game_version} 对应的自动资源包")
        return None
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / filename
    fresh = target.is_file() and _valid_pack(target) and time.time() - target.stat().st_mtime < max_age_hours * 3600
    if fresh:
        if report: report(f"复用 CFPA 中文资源包缓存：{target.name}")
        return target
    if report: report(f"正在从 CFPA GitHub 获取适配资源包：{filename}")
    url = _find_asset_url(filename)
    temporary = target.with_suffix(".download")
    try:
        with _request(url, timeout=120) as response, temporary.open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block: break
                output.write(block)
        if not _valid_pack(temporary): raise ValueError("下载内容不是有效的中文资源包")
        os.replace(temporary, target)
        (cache_dir / "来源与许可.txt").write_text(
            "资源来源：https://github.com/CFPAOrg/Minecraft-Mod-Language-Package\n"
            "许可：CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享）\n",
            encoding="utf-8")
        if report: report(f"CFPA 中文资源包下载完成：{target}")
        return target
    finally:
        if temporary.exists(): temporary.unlink()
