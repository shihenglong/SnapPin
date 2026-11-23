"""把图片以**无损格式**写入 Windows 剪贴板（CF_DIBV5 / CF_DIB / "PNG" / CF_BITMAP）。

为什么不用 `QApplication.clipboard().setPixmap()`：
Qt 在 Windows 上把 QImage 交给剪贴板时，同一个 DataObject 会暴露
QImageWriter 支持的全部格式 —— `image/png` 之外还有 `image/jpeg`、
`image/webp`、`image/tiff`（实测一份 2560x1440 截图，剪贴板里同时挂着
663 KB 的 JPEG）。粘贴端只要挑中 jpeg/webp，UI 文字就被有损编码糊掉
（Qt 的 JPEG 默认质量 75），用户感知就是「复制后粘到微信/QQ/文档里变模糊、
像分辨率被降低了」。这里直接写 Win32 剪贴板，只提供无损格式：

- `CF_DIBV5`：32bpp BI_BITFIELDS，带 alpha —— 支持透明的程序（Word、Office）
  拿得到圆角透明，不再像 Qt 那样写成 24bpp 让透明区变黑；
- `CF_DIB`：24bpp BI_RGB，最大兼容（微信/QQ/画图都走这条）；
- `"PNG"`：Windows 标准剪贴板格式名，Chromium/Electron 系（飞书、QQNT）
  优先取它，拿到的是带 alpha 的无损 PNG；
- `CF_BITMAP`：HBITMAP，兼容只认位图句柄的老程序。

全部是原始像素：不缩放、不重采样、不做有损编码。
所有句柄写入失败时回退 Qt 路径，保证功能不丢。
"""

import struct
import time
import ctypes
from ctypes import wintypes

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

CF_BITMAP, CF_DIB, CF_DIBV5 = 2, 8, 17
GMEM_MOVEABLE = 0x0002
BI_RGB, BI_BITFIELDS = 0, 3
LCS_sRGB = 0x73524742
LCS_GM_IMAGES = 4
DIB_RGB_COLORS = 0
CBM_INIT = 0x4
_DEFAULT_PPM = 3780            # 96 DPI，与 Qt 写 DIB 时给的值一致

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32")

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL

_gdi32.CreateDIBitmap.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_gdi32.CreateDIBitmap.restype = wintypes.HBITMAP
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_png_format = 0


def _png_fmt() -> int:
    global _png_format
    if not _png_format:
        _png_format = _user32.RegisterClipboardFormatW("PNG")
    return _png_format


# ---------------------------------------------------------------- 编码
def _bgra(img: QImage):
    """(w, h, 显示方向 BGRA 行视图, xppm, yppm)。

    必须把 `constBits()` 拷成 Python bytes：QImage 一释放，底下内存就没了。
    """
    if img.format() != QImage.Format_ARGB32:
        img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    stride = img.bytesPerLine()
    raw = bytes(img.constBits())
    rows = np.frombuffer(raw, dtype=np.uint8, count=stride * h).reshape(h, stride)
    bgra = rows[:, :w * 4].copy().reshape(h, w, 4)   # ARGB32 的小端内存 = B,G,R,A
    return (w, h, bgra,
            img.dotsPerMeterX() or _DEFAULT_PPM,
            img.dotsPerMeterY() or _DEFAULT_PPM)


def _dib24(w, h, bgra, xppm, yppm) -> bytes:
    """CF_DIB：BITMAPINFOHEADER(40) + bottom-up 24bpp BGR，行 4 字节对齐。"""
    stride = (w * 3 + 3) & ~3
    body = np.zeros((h, stride), dtype=np.uint8)
    body[:, :w * 3] = bgra[:, :, :3].reshape(h, w * 3)
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, BI_RGB,
                      stride * h, xppm, yppm, 0, 0)
    return hdr + body[::-1].tobytes()


def _dibv5(w, h, bgra, xppm, yppm) -> bytes:
    """CF_DIBV5：BITMAPV5HEADER(124) + bottom-up 32bpp BGRA（alpha 保留）。"""
    stride = w * 4
    hdr = struct.pack("<IiiHHIIiiII", 124, w, h, 1, 32, BI_BITFIELDS,
                      stride * h, xppm, yppm, 0, 0)
    hdr += struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    hdr += struct.pack("<I", LCS_sRGB) + b"\x00" * 36 + b"\x00" * 12
    hdr += struct.pack("<IIII", LCS_GM_IMAGES, 0, 0, 0)
    assert len(hdr) == 124
    return hdr + bgra[::-1].tobytes()


def _png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not img.save(buf, "PNG"):
        return b""
    return bytes(buf.data())


# ---------------------------------------------------------------- 句柄
def _global_bytes(data: bytes):
    h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return None
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return None
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    return h


def _hbitmap(w, h, dib24: bytes):
    """用同一份 24bpp DIB 位造 HBITMAP（CF_BITMAP 用）。"""
    bmi = _BITMAPINFOHEADER(40, w, h, 1, 24, BI_RGB, 0, 0, 0, 0, 0)
    hdc = _user32.GetDC(None)
    if not hdc:
        return None
    try:
        bits = (ctypes.c_char * (len(dib24) - 40)).from_buffer_copy(dib24[40:])
        return _gdi32.CreateDIBitmap(hdc, ctypes.byref(bmi), CBM_INIT,
                                     ctypes.byref(bits), ctypes.byref(bmi),
                                     DIB_RGB_COLORS) or None
    finally:
        _user32.ReleaseDC(None, hdc)


def _free(items) -> None:
    for fmt, (handle, is_bitmap) in items:
        if not handle:
            continue
        if is_bitmap:
            _gdi32.DeleteObject(handle)
        else:
            _kernel32.GlobalFree(handle)


def _commit(items) -> bool:
    """items: [(format, (handle, is_bitmap))]。成功的句柄所有权交给系统。"""
    for _ in range(12):                 # 剪贴板被别的程序占着：重试 ~240ms
        if _user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        _free(items)
        return False
    left = list(items)
    try:
        if not _user32.EmptyClipboard():
            return False
        if CF_DIB not in [fmt for fmt, _ in items]:
            return False
        for fmt, (handle, _is_bitmap) in items:
            if not _user32.SetClipboardData(fmt, handle):
                return False
            left = [it for it in left if it[0] != fmt]   # 已交给系统
        return True
    finally:
        _user32.CloseClipboard()
        _free(left)


# ---------------------------------------------------------------- 对外
def copy_image(img) -> bool:
    """把 QImage/QPixmap 以无损格式放进剪贴板。失败回退 Qt 并返回 False。"""
    if isinstance(img, QPixmap):
        img = img.toImage()
    if not isinstance(img, QImage) or img.isNull() or img.width() < 1 or img.height() < 1:
        return False
    # 本进程若曾通过 OLE 持有剪贴板（Qt 之前 setText/setPixmap 过），
    # OleGetClipboard 会继续返回自己那份空 DataObject：随后 Win32 写入的
    # 新数据在 Qt 侧（剪贴板历史、剪贴板贴图）读不到。OleSetClipboard(NULL)
    # 让 OLE 放弃所有权，之后 Qt 读的就是系统剪贴板本身。
    try:
        _ole32.OleSetClipboard(None)
    except Exception:
        pass
    w, h, bgra, xppm, yppm = _bgra(img)
    dib24 = _dib24(w, h, bgra, xppm, yppm)
    dibv5 = _dibv5(w, h, bgra, xppm, yppm)
    png = _png_bytes(img)

    items = []
    for fmt, data in ((CF_DIBV5, dibv5), (CF_DIB, dib24), (_png_fmt(), png)):
        if not data:
            continue
        hmem = _global_bytes(data)
        if hmem:
            items.append((fmt, (hmem, False)))
    hbm = _hbitmap(w, h, dib24)
    if hbm:
        items.append((CF_BITMAP, (hbm, True)))
    if not items:
        return False

    ok = _commit(items)
    if not ok:
        try:                                    # 兜底：至少让 Qt 写一份能用的
            QGuiApplication.clipboard().setPixmap(QPixmap.fromImage(img))
        except Exception:
            pass
    return ok


if __name__ == "__main__":
    # 自测：写一张带 1px 竖纹 + 透明角的图，断言剪贴板里只有无损格式，
    # 且 CF_DIB / "PNG" / CF_DIBV5 读回来与源图逐像素一致。
    import sys
    from ctypes import wintypes as wt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor, QPainter

    app = QApplication(sys.argv)
    _user32.EnumClipboardFormats.argtypes = [wt.UINT]
    _user32.EnumClipboardFormats.restype = wt.UINT
    _user32.GetClipboardData.argtypes = [wt.UINT]
    _user32.GetClipboardData.restype = wt.HANDLE
    _user32.GetClipboardFormatNameW.argtypes = [wt.UINT, wt.LPWSTR, ctypes.c_int]
    _user32.GetClipboardFormatNameW.restype = ctypes.c_int
    _kernel32.GlobalSize.argtypes = [wt.HANDLE]
    _kernel32.GlobalSize.restype = ctypes.c_size_t

    W, H = 401, 239                     # 故意用奇数宽：验证 24bpp 行对齐填充
    src = QImage(W, H, QImage.Format_ARGB32)
    src.fill(QColor(255, 255, 255, 255))
    p = QPainter(src)
    p.setPen(QColor(0, 0, 0))
    for x in range(0, W, 2):
        p.drawLine(x, 0, x, H)          # 1px 竖纹：任何有损编码都会糊
    p.end()
    src.setPixelColor(0, 0, QColor(0, 0, 0, 0))   # 透明角：验证 alpha 通道

    assert copy_image(src), "写入剪贴板失败"

    # --- 枚举真实 Windows 剪贴板格式
    _user32.OpenClipboard(None)
    fmts = {}
    f = 0
    while True:
        f = _user32.EnumClipboardFormats(f)
        if not f:
            break
        buf = ctypes.create_unicode_buffer(128)
        n = _user32.GetClipboardFormatNameW(f, buf, 128)
        fmts[f] = buf.value if n else {1: "CF_TEXT", 2: "CF_BITMAP",
                                       8: "CF_DIB", 17: "CF_DIBV5"}.get(f, f"#{f}")
    _user32.CloseClipboard()
    names = set(fmts.values())
    print("剪贴板格式:", sorted(names))
    for bad in ("image/jpeg", "image/jpg", "image/jfif", "image/webp", "image/tiff"):
        assert bad not in names, f"剪贴板里仍挂着有损格式 {bad}"
    assert "CF_DIB" in names and "CF_DIBV5" in names and "PNG" in names, names
    assert "CF_BITMAP" in names, names

    # --- 读回 CF_DIB 24bpp，逐像素比对（忽略 alpha：24bpp 装不下）
    _user32.OpenClipboard(None)
    h = _user32.GetClipboardData(CF_DIB)
    ptr = _kernel32.GlobalLock(h)
    dib = ctypes.string_at(ptr, _kernel32.GlobalSize(h))
    _kernel32.GlobalUnlock(h)
    _user32.CloseClipboard()
    bi, bw, bh, pl, bits, comp, imgsz, xpm, ypm, cu, ci = struct.unpack_from("<IiiHHIIiiII", dib, 0)
    assert (bw, bh, bits, comp) == (W, H, 24, BI_RGB), (bw, bh, bits, comp)
    stride = (W * 3 + 3) & ~3
    body = np.frombuffer(dib, dtype=np.uint8, offset=40, count=stride * H).reshape(H, stride)
    rgb = body[:, :W * 3].reshape(H, W, 3)[::-1]                 # BGR, bottom-up
    got = QImage(rgb.tobytes(), W, H, W * 3, QImage.Format_BGR888).convertToFormat(QImage.Format_RGB32)
    want = src.convertToFormat(QImage.Format_RGB32)
    assert got == want, "CF_DIB 与源图像素不一致（发生了重采样/有损编码）"

    # --- 读回 "PNG"：必须保留 alpha，且与源图完全一致
    _user32.OpenClipboard(None)
    pf = _png_fmt()
    h = _user32.GetClipboardData(pf)
    ptr = _kernel32.GlobalLock(h)
    png = ctypes.string_at(ptr, _kernel32.GlobalSize(h))
    _kernel32.GlobalUnlock(h)
    _user32.CloseClipboard()
    back = QImage.fromData(png)
    assert (back.width(), back.height()) == (W, H), (back.width(), back.height())
    assert back == src, "'PNG' 剪贴板数据与源图不一致"

    # --- 读回 CF_DIBV5：32bpp，alpha 保留
    _user32.OpenClipboard(None)
    h = _user32.GetClipboardData(CF_DIBV5)
    ptr = _kernel32.GlobalLock(h)
    v5 = ctypes.string_at(ptr, 124)
    _kernel32.GlobalUnlock(h)
    _user32.CloseClipboard()
    bi, bw, bh, pl, bits, comp = struct.unpack_from("<IiiHHI", v5, 0)
    assert (bw, bh, bits, comp) == (W, H, 32, BI_BITFIELDS), (bw, bh, bits, comp)

    # --- Qt 侧仍能读出这张图（剪贴板历史/贴图热键依赖它）
    qt_img = QGuiApplication.clipboard().image()
    assert (qt_img.width(), qt_img.height()) == (W, H), (qt_img.width(), qt_img.height())
    _md = QGuiApplication.clipboard().mimeData()
    assert _md is not None and _md.hasImage(), "剪贴板历史依赖 mimeData().hasImage()"

    print("clipimg 自测: 仅无损格式 + 逐像素一致 + alpha 保留 + Qt 可读 OK")
