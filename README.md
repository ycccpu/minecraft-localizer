# Minecraft 汉化补丁生成器

面向 Minecraft Java 版整合包的本地汉化补丁生成工具。它可以扫描模组语言文件、指南书、任务、脚本、配置说明、枪包、女仆模型包和光影语言文件，复用已有中文资源后通过用户自己的 OpenAI 兼容 API 补译，并输出可直接安装的实例覆盖目录。

## 主要功能

- 扫描目录及 JAR/ZIP 内的 `en_us.json`、`en_us.lang`，合并同路径语言文件；
- 生成标准 Minecraft 资源包与 Patchouli 汉化资源包；
- 支持 FTB Quests/SNBT、KubeJS、FancyMenu、配置注释及存档 `serverconfig`；
- 支持 TACZ、女仆自定义包和光影包语言文件；
- 自动识别 Minecraft 版本并匹配社区中文资源；
- API 聚合并发、失败拆分重试、占位符和格式代码校验；
- SQLite 本地翻译缓存、术语表、质量报告、安装备份和恢复；
- 独立的 JSON/TXT 自定义翻译功能（不读写 Minecraft 翻译缓存）。

## 运行

要求 Python 3.10 或更高版本。

```powershell
python run_gui.pyw
```

也可以安装命令行入口：

```powershell
python -m pip install -e .
mc-localizer-gui
```

程序使用用户自行提供的 OpenAI 兼容 API。请勿把包含真实密钥的 `api_config.json` 提交到仓库。

### API Key 安全

为了方便本地重复使用，图形界面会把 API Key 明文保存到程序旁的 `api_config.json`。该文件已加入 `.gitignore`，但仍请注意：

- 不要分享或提交 `api_config.json`；
- 不要把运行后的整个程序目录直接打包发布；
- 发布日志或截图前检查其中是否含 API 地址、路径或密钥；
- 怀疑密钥泄露时，应立即到 API 服务商处撤销并更换。

JSON/TXT 自定义翻译不会读取或写入 Minecraft 翻译缓存，但仍会使用当前填写的 API 配置发起请求。

## 测试与打包

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests

python -m pip install pyinstaller
python -m PyInstaller Minecraft汉化补丁生成器.spec --noconfirm --clean
```

## CFPA 中文资源包

本工具可以根据游戏版本，从 [CFPAOrg/Minecraft-Mod-Language-Package](https://github.com/CFPAOrg/Minecraft-Mod-Language-Package) 下载匹配的社区中文资源包，并将其作为翻译记忆优先复用。

CFPA 中文资源由 CFPAOrg 及其贡献者维护，采用 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/deed.zh-hans) 许可。本仓库不预先打包或重新授权这些翻译资源；自动下载的资源仍受其原许可约束。使用或再分发相关资源时，请保留署名、仅用于非商业目的，并以相同方式共享。

详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## AI 辅助开发声明

本项目的代码和文档由 OpenAI Codex 根据项目维护者提出的需求辅助生成、修改和整理，并由项目维护者进行测试、审阅与发布。AI 生成内容可能存在错误，欢迎通过 Issue 提交问题或改进建议。

## 项目许可

本仓库自行编写的程序代码采用 [MIT License](LICENSE) 许可。该许可不覆盖运行时下载的 CFPA 翻译资源、Minecraft、模组或整合包中的第三方内容。

Minecraft 是 Mojang Studios 的商标。本项目与 Mojang Studios、Microsoft、NeoForge 或 CFPAOrg 均无隶属或官方合作关系。
