# -*- coding: utf-8 -*-
"""组装离线翻译插件包（引擎 onedir + en-zh 模型）并三重验证。

前置：
  1) build/venv_ct2/Scripts/python -m PyInstaller --noconfirm --clean \
       --distpath build/tr_engine_dist --workpath build/tr_engine_work \
       --specpath build/tr_engine_work --name translate-engine \
       --collect-binaries ctranslate2 --collect-binaries sentencepiece \
       tools/tr_engine.py
  2) 模型解包在 tools/models/en-zh/（.argosmodel 即 zip，含 model/ 与 sentencepiece.model，
     平铺 model.bin + sentencepiece.model + shared_vocabulary.json 即可）

产出 dist/SnapPin-offline-engine-win64.zip、dist/SnapPin-offline-model-en-zh.zip。
zip 内部前缀 engine/、models/en-zh/ 对应 vendor/plugins/translation/ 布局，
用户/应用内下载后解压到插件目录即生效（translate.install_offline 校验结构）。
"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.join(ROOT, "build", "tr_engine_dist", "translate-engine")
MODEL_DIR = os.path.join(ROOT, "tools", "models", "en-zh")
DIST = os.path.join(ROOT, "dist")

JOBS = [(ENGINE_DIR, "engine", "SnapPin-offline-engine-win64.zip",
         "engine/translate-engine.exe"),
        (MODEL_DIR, "models/en-zh", "SnapPin-offline-model-en-zh.zip",
         "models/en-zh/model.bin")]

os.chdir(DIST)
bad = 0
for src, prefix, out, must in JOBS:
    if not os.path.isdir(src):
        print(f"缺目录 {src}（先完成前置步骤）")
        sys.exit(1)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dp, _d, fs in os.walk(src):
            for f in fs:
                p = os.path.join(dp, f)
                z.write(p, prefix + "/" + os.path.relpath(p, src).replace(os.sep, "/"))
    with zipfile.ZipFile(out) as z:
        crc = z.testzip()
        names = z.namelist()
        hit = any(n.replace("\\", "/").startswith(must) for n in names)
        magic = open(out, "rb").read(4)
        ok = crc is None and hit and magic == b"PK\x03\x04"
        bad += 0 if ok else 1
        print(f"{out}: {os.path.getsize(out)/1048576:.1f}MB 条目 {len(names)} "
              f"| CRC {'OK' if crc is None else 'BAD'} | {must} "
              f"{'OK' if hit else 'MISSING'} | 魔数 {magic} → {'OK' if ok else 'FAIL'}")
sys.exit(1 if bad else 0)
