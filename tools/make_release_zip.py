"""重建发布 zip(真 ZIP 格式)并三重验证 — 用法: python tools/make_release_zip.py"""
import hashlib
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import version

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
OUT = f"SnapPin-{version.APP_VERSION}-windows-x64.zip"
ROOT = sys.argv[1] if len(sys.argv) > 1 else "SnapPin"   # 可传暂存目录名

if os.path.exists(OUT):
    os.remove(OUT)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dirpath, _dirs, files in os.walk(ROOT):
        for f in files:
            p = os.path.join(dirpath, f)
            z.write(p, "SnapPin/" + os.path.relpath(p, ROOT).replace(os.sep, "/"))

with zipfile.ZipFile(OUT) as z:
    bad = z.testzip()
    names = z.namelist()
    print("testzip:", "OK" if bad is None else f"BAD:{bad}")
    print("entries:", len(names),
          "| exe:", "SnapPin/SnapPin.exe" in names,
          "| vendor files:", sum(1 for n in names if n.startswith("SnapPin/vendor/")))
    src = hashlib.md5(open(os.path.join(ROOT, "SnapPin.exe"), "rb").read()).hexdigest()
    dst = hashlib.md5(z.read("SnapPin/SnapPin.exe")).hexdigest()
    print("exe md5 一致:", src == dst)
with open(OUT, "rb") as f:
    magic = f.read(4)
print("魔数:", magic, "→ ZIP OK" if magic == b"PK\x03\x04" else "→ 仍不对!")
print("大小:", os.path.getsize(OUT))
sys.exit(0 if magic == b"PK\x03\x04" else 1)
