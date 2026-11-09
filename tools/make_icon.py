"""渲染 APP_ICON_SVG 为多尺寸 snappin.ico。用法: python tools/make_icon.py"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from icons import APP_ICON_SVG

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = [16, 24, 32, 48, 64, 128, 256]


def render_png(size: int) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(APP_ICON_SVG.encode())).render(p)
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "snappin.ico"
    pngs = [(s, render_png(s)) for s in SIZES]
    ico = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    body = b""
    for s, data in pngs:
        dim = s % 256  # 256 在 ICO 目录项里写作 0
        ico += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data
    out.write_bytes(ico + body)
    print(f"已写入 {out} ({out.stat().st_size} 字节, 尺寸: {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
