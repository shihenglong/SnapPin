"""生成 README 界面截图 — python tools/make_readme_shots.py（真平台，非 offscreen）

不拍用户真实桌面：截图层的背景用合成的假桌面图像替换。
产出 docs/img/：capture.png / pin_ocr.png / pin_translate.png / clip_history.png / settings.png
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import tempfile

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import QApplication

OUT = os.path.join(ROOT, "docs", "img")
os.makedirs(OUT, exist_ok=True)

app = QApplication.instance() or QApplication(sys.argv)

import config
from clip import ClipStore, ClipMonitor
from pwman import PasswordStore

CFG = config.load()
CFG["capture"]["show_hint"] = True

TMP = tempfile.mkdtemp(prefix="snappin_shots_")


def save(img_or_pix, name):
    path = os.path.join(OUT, name)
    img_or_pix.save(path)
    print(name, os.path.getsize(path) // 1024, "KB")


def wait_until(fn, timeout_ms=30000, what=""):
    loop = QEventLoop()
    state = {"ok": False}

    def _poll():
        if fn():
            state["ok"] = True
            loop.quit()

    t = QTimer()
    t.setSingleShot(True)
    t.timeout.connect(loop.quit)
    poll = QTimer()
    poll.setInterval(150)
    poll.timeout.connect(_poll)
    poll.start()
    t.start(timeout_ms)
    _poll()
    if not state["ok"]:
        loop.exec()
    poll.stop()
    if not state["ok"] and not fn():
        print(f"  [warn] 等待超时: {what}")
    return state["ok"]


# ================================================================ 假桌面
def fake_desktop(w=1280, h=800) -> QImage:
    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    # 壁纸
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor("#2B3440"))
    g.setColorAt(1, QColor("#1D242C"))
    p.fillRect(0, 0, w, h, QBrush(g))

    def window(x, y, ww, hh, title):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawRoundedRect(x + 4, y + 6, ww, hh, 10, 10)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(x, y, ww, hh, 10, 10)
        p.setBrush(QColor("#F2F3F5"))
        p.drawRoundedRect(x, y, ww, 34, 10, 10)
        p.setBrush(QColor("#FFFFFF"))
        p.drawRect(x, y + 24, ww, 10)
        for i, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
            p.setBrush(QColor(c))
            p.drawEllipse(x + 14 + i * 20, y + 12, 11, 11)
        f = QFont("Microsoft YaHei UI", 9)
        p.setFont(f)
        p.setPen(QColor("#4E5969"))
        p.drawText(QRect(x + 84, y, ww - 100, 34), Qt.AlignVCenter | Qt.AlignLeft, title)

    # 假编辑器窗口（左侧）
    ex, ey, ew, eh = 70, 90, 660, 470
    window(ex, ey, ew, eh, "main.py — SnapPin")
    p.setBrush(QColor("#FAFBFC"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(ex + 10, ey + 44, 150, eh - 54, 6, 6)
    p.setFont(QFont("Microsoft YaHei UI", 9))
    for i, name in enumerate(("main.py", "capture.py", "pinwin.py", "clipwin.py", "winfront.py")):
        p.setPen(QColor("#1D2129" if i == 0 else "#86909C"))
        p.drawText(ex + 24, ey + 68 + i * 24, name)
    # 代码行（彩色 token 矩形近似）
    code_colors = ["#6B7FDB", "#A272D6", "#448AFF", "#26A69A", "#8A93A2"]
    import random
    rnd = random.Random(7)
    for r in range(16):
        x = ex + 190
        y = ey + 62 + r * 24
        for seg in range(rnd.randint(2, 5)):
            wseg = rnd.randint(24, 90)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(code_colors[rnd.randrange(len(code_colors))]))
            p.setBrush(QColor(code_colors[rnd.randrange(4)]).lighter(160 if seg % 2 else 130))
            p.drawRoundedRect(x, y, wseg, 9, 3, 3)
            x += wseg + 10
    # 行号
    p.setFont(QFont("Consolas", 8))
    p.setPen(QColor("#C9CDD4"))
    for r in range(16):
        p.drawText(ex + 172, ey + 60 + r * 24, str(r + 1))

    # 假浏览器窗口（右上）
    bx, by, bw, bh = 790, 70, 440, 300
    window(bx, by, bw, bh, "SnapPin — Releases")
    p.setBrush(QColor("#F5F6F7"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(bx + 12, by + 44, bw - 24, 26, 13, 13)
    p.setFont(QFont("Segoe UI", 9))
    p.setPen(QColor("#86909C"))
    p.drawText(bx + 28, by + 50, "github.com/shihenglong/SnapPin")
    for r in range(6):
        p.setBrush(QColor("#E5E6EB"))
        p.drawRoundedRect(bx + 20, by + 92 + r * 30, (bw - 60) * (0.9 if r % 2 else 0.7), 10, 4, 4)

    # 假卡片（右下）
    cx, cy, cw, ch = 790, 420, 440, 250
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 90))
    p.drawRoundedRect(cx + 4, cy + 6, cw, ch, 10, 10)
    g2 = QLinearGradient(cx, cy, cx + cw, cy + ch)
    g2.setColorAt(0, QColor("#3D8BFF"))
    g2.setColorAt(1, QColor("#7B5CFF"))
    p.setBrush(QBrush(g2))
    p.drawRoundedRect(cx, cy, cw, ch, 10, 10)
    p.setPen(QColor("white"))
    p.setFont(QFont("Microsoft YaHei UI", 15, QFont.Bold))
    p.drawText(cx + 28, cy + 52, "剪贴板历史 Alt+V")
    p.setFont(QFont("Segoe UI", 10))
    p.setPen(QColor(255, 255, 255, 190))
    p.drawText(cx + 28, cy + 88, "复制即记录 · 点哪行粘哪行")
    for r in range(4):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))
        p.drawRoundedRect(cx + 24, cy + 116 + r * 28, cw - 48, 18, 5, 5)
    p.end()
    return img


def darken(base: QPixmap, alpha=90) -> QPixmap:
    pm = QPixmap(base)
    pd = QPainter(pm)
    pd.fillRect(pm.rect(), QColor(0, 0, 0, alpha))
    pd.end()
    return pm


def flatten(pix: QPixmap, bg="#FFFFFF") -> QImage:
    """贴到白底上（grab 的半透明投影在白背景渲染更自然，避免查看器显示黑边）。"""
    out = QImage(pix.size(), QImage.Format_ARGB32)
    out.fill(QColor(bg))
    pd = QPainter(out)
    pd.drawPixmap(0, 0, pix)
    pd.end()
    return out


# ================================================================ 1. 截图层
def shot_capture():
    from capture import CaptureOverlay
    ov = CaptureOverlay(app.primaryScreen(), CFG)
    base = QPixmap.fromImage(fake_desktop())
    ov.setGeometry(0, 0, base.width(), base.height())
    ov._origin = QPoint(0, 0)
    ov._dpr = 1.0
    ov._base = base
    ov._dark = darken(base)
    ov._hint.move(24, ov.height() - ov._hint.height() - 24)
    ov._sel = QRect(255, 160, 440, 335)
    # 标注画在选区内（paintEvent 以选区为裁剪区，选区外不显示）
    ov._anns = [{"tool": "arrow", "pts": [QPoint(310, 470), QPoint(520, 300)],
                 "color": "#E5484D"}]
    _ = ov.toolbar
    ov._place_toolbar()
    # 尺寸提示条（拖拽时的 W × H）
    ov._tip.setText("440 × 335")
    ov._tip.adjustSize()
    ov._tip.move(ov._sel.right() - ov._tip.width() + 8, ov._sel.bottom() + 8)
    ov._tip.show()
    ov._tip.raise_()
    out = QImage(ov.size(), QImage.Format_ARGB32)
    out.fill(Qt.white)
    ov.render(out)
    save(out, "capture.png")
    ov._tip.hide()
    ov._cancel()


# ================================================================ 贴图底图：英文文章卡片
ART_W, ART_H = 880, 440
BODY = [
    ("SnapPin is a lightweight desktop toolbox for Windows, built with "
     "Python and PySide6.", 0),
    ("It recreates the daily workflow of PixPin: full screen capture with "
     "window snapping,", 0),
    ("always-on-top pinned snapshots, instant OCR text picking and inline "
     "translation that", 0),
    ("paints the translated text right back onto the image. A Ditto style "
     "clipboard history", 0),
    ("and a DPAPI encrypted password vault complete the picture — everything "
     "is one hotkey", 0),
    ("away, and every byte of the OCR engine runs locally on your machine.", 0),
]


def article_pix() -> QPixmap:
    pm = QPixmap(ART_W, ART_H)
    pm.fill(QColor("#FFFFFF"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    # 顶栏
    p.fillRect(0, 0, ART_W, 54, QColor("#F7F8FA"))
    p.setPen(QColor("#C9CDD4"))
    p.drawLine(0, 54, ART_W, 54)
    p.setPen(QColor("#1D2129"))
    p.setFont(QFont("Segoe UI", 15, QFont.Bold))
    p.drawText(36, 36, "SnapPin 0.4 — Release Notes")
    p.setPen(QColor("#86909C"))
    p.setFont(QFont("Segoe UI", 9))
    p.drawText(ART_W - 300, 36, "docs · 2026-11-09 · 2 min read")
    # 正文
    y = 108
    p.setFont(QFont("Georgia", 13))
    p.setPen(QColor("#26292E"))
    for line, _ in BODY:
        p.drawText(36, y, line)
        y += 30
    # 代码块
    y += 10
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#F5F6F7"))
    p.drawRoundedRect(30, y, ART_W - 60, 64, 8, 8)
    p.setPen(QColor("#A272D6"))
    p.setFont(QFont("Consolas", 11))
    p.drawText(48, y + 26, "pip install SnapPin   # or grab the portable zip")
    p.drawText(48, y + 50, "SnapPin.exe           # tray icon appears, press Ctrl+1")
    y += 92
    p.setPen(QColor("#26292E"))
    p.setFont(QFont("Georgia", 13))
    p.drawText(36, y, "Press Alt+V any time to search everything you have copied.")
    p.end()
    return pm


# ================================================================ 2/3. 钉图选字 + 翻译替换
def make_pin(engine):
    from ocr import OcrRunner, available as ocr_available
    from translate import TranslateClient
    from pinwin import PinWindow
    assert ocr_available(), "vendor OCR 缺失"
    cfg = config.load()
    cfg["translate"]["engine"] = engine
    cfg["capture"]["autosave"] = False
    win = PinWindow(article_pix(), QPoint(200, 160), cfg, OcrRunner(),
                    TranslateClient(cfg))
    return win


def shot_pin_ocr():
    win = make_pin("online")
    ok = wait_until(lambda: bool(win._ocr_boxes), 60000, "自动取字")
    if not ok:
        return False
    # 选中正文第一段前 34 个字符（展示拖拽选字高亮 + 复制反馈 toast）
    sel = set()
    count = 0
    for bi, it in enumerate(win._ocr_boxes):
        for ci in range(len(it["chars"])):
            sel.add((bi, ci))
            count += 1
            if count >= 34:
                break
        if count >= 34:
            break
    win._selected = sel
    win._hover_i = 0
    win._toast_text(f"已复制 {count} 字：{win._ocr_boxes[0]['text'][:34]}…", 60000)
    app.processEvents()
    save(flatten(win.grab()), "pin_ocr.png")
    return True


def shot_pin_translate(win):
    # 自动取字是 60ms singleShot：必须等它先出框，否则 blocks 为空直接返回
    wait_until(lambda: bool(win._ocr_boxes), 60000, "取字")
    win.translate_replace()
    ok = wait_until(lambda: win._show_translated and win._translated_pix is not None,
                    45000, "翻译")
    if not ok:
        return False
    app.processEvents()
    save(flatten(win.grab()), "pin_translate.png")
    return True


# ================================================================ 4. 剪贴板历史
def shot_clip():
    import hashlib
    import sqlite3

    db = os.path.join(TMP, "clip.db")
    st = ClipStore(db)
    rows = [
        {"text": "SnapPin — PixPin 风格截图 / 贴图 / OCR / 翻译 / 剪贴板历史 开源复刻",
         "source_app": "README.md"},
        {"text": "https://github.com/shihenglong/SnapPin/releases/tag/v0.4.0",
         "source_app": "Chrome"},
        {"text": "def force_front(widget) -> bool:\n    hwnd = int(widget.winId())\n    u32.SetForegroundWindow(hwnd)",
         "source_app": "VS Code"},
        {"text": "会议纪要：周四发布 v0.4.1，修复托盘弹窗沉底；截图文档补 README 预览图。",
         "source_app": "飞书"},
        {"text": "REG add HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run /v SnapPin /d SnapPin.exe",
         "source_app": "终端"},
        {"text": "The quick brown fox jumps over the lazy dog.",
         "source_app": "Notepad"},
        {"text": "password = dpapi.decrypt(blob)  # 30s 后自动清空剪贴板",
         "source_app": "VS Code"},
        {"text": "git push origin main --tags   # 发布 v0.4.1",
         "source_app": "终端"},
        {"text": "译文已替换原图 · 在线直连 · 1.9s · 右键显示原文",
         "source_app": "SnapPin"},
        {"text": "QTimer.singleShot(12_000, self._check_updates_silent)",
         "source_app": "VS Code"},
        {"text": "日落时间 17:23 · 明日多云转晴 · 12~19°C",
         "source_app": "天气"},
        {"text": "pip install PySide6 numpy pyinstaller",
         "source_app": "终端"},
    ]
    for r in rows:
        st.add({"formats": {}, "text": r["text"], "source_app": r["source_app"],
                "hash": hashlib.sha256(r["text"].encode()).hexdigest()})
    # 一条图片记录
    img = QImage(180, 110, QImage.Format_ARGB32)
    gp = QPainter(img)
    g = QLinearGradient(0, 0, 180, 110)
    g.setColorAt(0, QColor("#FB923C"))
    g.setColorAt(1, QColor("#EA580C"))
    gp.fillRect(0, 0, 180, 110, QBrush(g))
    gp.setPen(QColor("white"))
    gp.setFont(QFont("Microsoft YaHei UI", 12, QFont.Bold))
    gp.drawText(img.rect(), Qt.AlignCenter, "界面预览图.png")
    gp.end()
    from PySide6.QtCore import QBuffer
    ba = QBuffer()
    ba.open(QBuffer.ReadWrite)
    img.save(ba, "PNG")
    st.add({"formats": {"image/png": bytes(ba.data())}, "text": "",
            "source_app": "截图", "hash": hashlib.sha256(bytes(ba.data())).hexdigest()})
    # 第一条设为常用 📌
    con = sqlite3.connect(db)
    con.execute("UPDATE clips SET pinned=1 WHERE id=(SELECT MIN(id) FROM clips)")
    con.commit()
    con.close()

    pw = PasswordStore(os.path.join(TMP, "pw.db"))
    pw.add("GitHub", "shihenglong", "ghp_demo_not_a_real_token")

    from clipwin import ClipWindow
    win = ClipWindow(st, CFG, ClipMonitor(st, enabled=False),
                     on_settings=lambda: None,
                     pw_provider=lambda: pw, on_pwman=lambda: None)
    if hasattr(win, "refresh"):
        win.refresh()
    app.processEvents()
    save(flatten(win.grab(), "#F5F6F7"), "clip_history.png")
    try:
        pw.close()
    except Exception:
        pass


# ================================================================ 6. 长截图预览窗
def shot_longshot_preview():
    """长截图右侧预览窗：长图缩略图 + 自动滚动 / 完成 / 取消。"""
    from capture import LongshotPreview
    pm = QPixmap(232, 520)
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.fillRect(pm.rect(), QColor("#FFFFFF"))
    p.setPen(QColor("#1D2129"))
    p.setFont(QFont("Microsoft YaHei UI", 12, QFont.Bold))
    p.drawText(16, 30, "滚动拼接中的长图")
    p.setPen(QColor("#86909C"))
    p.setFont(QFont("Segoe UI", 8))
    p.drawText(16, 46, "live preview · scroll stitching")
    p.setPen(QColor("#4E5969"))
    p.setFont(QFont("Segoe UI", 8))
    for i, t in enumerate(("选区固定，向上下双向拼接，", "回滚不重复，停止后预览更新", "", "自动滚动：程序定速下滚，", "拼好一段才滚下一段", "", "固定悬浮件自动消除，", "正文零涂抹、零重影")):
        p.drawText(16, 76 + i * 16, t)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#F5F6F7"))
    p.drawRoundedRect(16, 226, 200, 54, 6, 6)
    p.setPen(QColor("#A272D6"))
    p.setFont(QFont("Consolas", 8))
    p.drawText(26, 248, "$ snappin --longshot")
    p.drawText(26, 266, "  stitch 60 rows / frame")
    p.setBrush(QColor("#E8F3FF"))
    p.drawRoundedRect(16, 296, 200, 76, 6, 6)
    p.setPen(QColor("#1D2129"))
    p.setFont(QFont("Segoe UI", 8))
    for i in range(6):
        p.drawText(26, 314 + i * 12, "· stitched line " * 2)
    p.end()
    pv = LongshotPreview()
    pv.set_pix(pm)
    app.processEvents()
    save(flatten(pv.grab(), "#FFFFFF"), "longshot.png")
    pv.close()


# ================================================================ 7. GIF 录制结果
def shot_gif_result():
    """GIF 录制结果：钉在桌面的预览窗（播放遮罩 + 保存/复制/关闭）。"""
    import numpy as np
    from capture import CaptureOverlay, GifResult
    W, H = 320, 210
    frames = []
    for i in range(10):
        yy, xx = np.mgrid[0:H, 0:W]
        r = (xx * 255 // W).astype(np.uint8)
        g = (yy * 255 // H).astype(np.uint8)
        b = ((i * 26 + xx) % 256).astype(np.uint8)
        f = np.stack([r, g, b], axis=2)
        f[36:92, 18:92] = np.array([255, 59, 48], np.uint8)
        f[104:160, 150:236] = np.array([52, 199, 89], np.uint8)
        f[120:168, 40:140] = np.array([10, 132, 255], np.uint8)
        frames.append(f)
    path = os.path.join(TMP, "readme_demo.gif")
    CaptureOverlay._encode_gif(frames, path)
    res = GifResult(path, QPoint(40, 40), frames)
    app.processEvents()
    save(flatten(res.grab(), "#FFFFFF"), "gif_record.png")
    res.close()


# ================================================================ 5. 设置窗口
def shot_settings():
    from settings_ui import SettingsDialog
    dlg = SettingsDialog(config.load(), lambda: None)
    dlg.nav.setCurrentRow(0)
    app.processEvents()
    save(dlg.grab(), "settings.png")


if __name__ == "__main__":
    print("== 1/5 截图层 ==")
    shot_capture()
    print("== 2/5 钉图取字 ==")
    if shot_pin_ocr():
        print("== 3/5 翻译替换 ==")
        ok = shot_pin_translate(make_pin("online"))
        if not ok:                      # 在线直连不可达 → 本地离线引擎兜底
            print("   在线引擎不可用，改用离线引擎…")
            shot_pin_translate(make_pin("local"))
    print("== 4/5 剪贴板历史 ==")
    shot_clip()
    print("== 5/7 设置窗口 ==")
    shot_settings()
    print("== 6/7 长截图预览 ==")
    shot_longshot_preview()
    print("== 7/7 GIF 录制结果 ==")
    shot_gif_result()
    print("done ->", OUT)
