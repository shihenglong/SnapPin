# -*- coding: utf-8 -*-
"""重建发布 zip(真 ZIP 格式)并三重验证。

用法:
  python tools/make_release_zip.py            # 标准版 SnapPin-{ver}-windows-x64.zip
  python tools/make_release_zip.py [暂存目录]  # 用指定暂存目录(运行中的 exe 锁文件时)
  python tools/make_release_zip.py --full     # full 版(内置离线翻译引擎+en-zh 模型)

full 版自动组装:复制 dist/SnapPin 为暂存 → 覆盖新 exe → 塞入
build/tr_engine_dist/translate-engine(引擎 onedir)与 tools/models/en-zh(模型)。
"""
import hashlib
import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import version

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT_DIR, "dist")
ENGINE_DIR = os.path.join(ROOT_DIR, "build", "tr_engine_dist", "translate-engine")
MODEL_DIR = os.path.join(ROOT_DIR, "tools", "models", "en-zh")


def build_zip(stage: str, out: str, extra_must: tuple = ()):
    """把 dist/<stage> 打成 zip（内部根目录 SnapPin/）并验证。"""
    stage = os.path.join(DIST, stage)
    out = os.path.join(DIST, out)
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, _dirs, files in os.walk(stage):
            for f in files:
                p = os.path.join(dirpath, f)
                z.write(p, "SnapPin/" + os.path.relpath(p, stage).replace(os.sep, "/"))
    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
        names = z.namelist()
        checks = {"exe": "SnapPin/SnapPin.exe" in names}
        src = hashlib.md5(open(os.path.join(stage, "SnapPin.exe"), "rb").read()).hexdigest()
        checks["exe md5 一致"] = src == hashlib.md5(z.read("SnapPin/SnapPin.exe")).hexdigest()
        for c in extra_must:
            checks[c] = any(n.replace("\\", "/").startswith(c) for n in names)
        ok_crc = bad is None
        print(f"{out}: {os.path.getsize(out)/1048576:.1f}MB 条目 {len(names)} | "
              f"testzip {'OK' if ok_crc else 'BAD:' + str(bad)} | "
              f"vendor {sum(1 for n in names if n.startswith('SnapPin/vendor/'))} 文件")
        for k, v in checks.items():
            print(f"  {k}: {'OK' if v else 'MISSING'}")
    magic = open(out, "rb").read(4)
    print("魔数:", magic, "→ ZIP OK" if magic == b"PK\x03\x04" else "→ 仍不对!")
    return magic == b"PK\x03\x04" and ok_crc and all(checks.values())


def main():
    full = "--full" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--full"]
    stage = args[0] if args else "SnapPin"
    out = f"SnapPin-{version.APP_VERSION}-windows-x64.zip"

    if not full:
        ok = build_zip(stage, out)
        return 0 if ok else 1

    # ---- full 版：组装暂存目录（标准目录 + 离线插件），exe 单独覆盖避免锁
    stage_full = "SnapPin_full_stage"
    dst = os.path.join(DIST, stage_full)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(os.path.join(DIST, stage), dst)
    shutil.copy2(os.path.join(DIST, "SnapPin.exe"), os.path.join(dst, "SnapPin.exe"))
    if not os.path.isdir(ENGINE_DIR) or not os.path.isfile(os.path.join(MODEL_DIR, "model.bin")):
        print("缺离线插件件（先打引擎 onedir 并解包模型到 tools/models/en-zh）")
        return 1
    tr_dir = os.path.join(dst, "vendor", "plugins", "translation")
    shutil.copytree(ENGINE_DIR, os.path.join(tr_dir, "engine"))
    shutil.copytree(MODEL_DIR, os.path.join(tr_dir, "models", "en-zh"))
    out_full = f"SnapPin-{version.APP_VERSION}-full-windows-x64.zip"
    ok = build_zip(stage_full, out_full,
                   extra_must=("SnapPin/vendor/plugins/translation/engine/translate-engine.exe",
                               "SnapPin/vendor/plugins/translation/models/en-zh/model.bin"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
