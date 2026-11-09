# 离线翻译插件（CTranslate2 神经翻译 + OPUS-MT int8 模型）

主程序默认走免 Key 在线直连；本目录安装离线插件后，设置→翻译 引擎选
「离线插件（神经翻译）」即进入纯本地翻译模式（0 网络请求，CPU 单批 <0.5s）。

## 安装

- **应用内下载（推荐）**：设置→翻译→离线插件→「下载离线翻译插件」，
  自动从 GitHub Release 拉取引擎包与模型包并解压到本目录。
- **手动安装**：下载 `SnapPin-offline-engine-win64.zip` 与
  `SnapPin-offline-model-en-zh.zip`，解压到本目录即可，布局：

```text
plugins/translation/
├── README.md
├── engine/            # CTranslate2 引擎（onedir，translate-engine.exe）
│   ├── translate-engine.exe
│   └── _internal/
└── models/            # 语言对模型（每个 ~70MB）
    └── en-zh/         # 英语 → 简体中文
        ├── model.bin
        ├── sentencepiece.model
        └── shared_vocabulary.json
```

## 引擎接口（供自定义引擎替换参考）

    translate-engine.exe --from en --to zh --input <UTF-8 文本文件> --output <输出文件>

输入每行一段原文，输出与输入等长（每行对应译文）；模型查找 engine/ 上一级的
`models/<from>-<to>/`（`--from auto` 时取唯一已装语言对）。

## 模型来源与许可

- 模型：Argos Translate en_zh（OPUS-MT 系，int8 量化）
  https://www.argosopentech.com/ ，镜像 https://huggingface.co/TiberiuCristianLeon/Argostranslate
- 运行时：CTranslate2 https://github.com/OpenNMT/CTranslate2 （MIT）
- SentencePiece https://github.com/google/sentencepiece （Apache-2.0）
