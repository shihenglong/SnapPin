"""重建发布 zip(真 ZIP 格式)并三重验证 — 用法: python tools/make_release_zip.py"""
import hashlib
import os
import sys
import zipfile

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dist"))
OUT = "SnapPin-0.4.0-windows-x64.zip"
ROOT = "SnapPin"

if os.path.exists(OUT):
    os.remove(OUT)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dirpath, _dirs, files in os.walk(ROOT):
        for f in files:
            p = os.path.join(dirpath, f)
            z.write(p, p.replace(os.sep, "/"))

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
