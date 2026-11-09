"""贴图窗口 v3 — 贴图即取字（拖拽选字/点击复制）+ 分块并发翻译 + 译文按块替换。"""
import time

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QIcon, QImage,
                           QPainter, QPen, QPixmap, QTextLayout, QTextOption)
from PySide6.QtWidgets import (QApplication, QLabel, QMenu, QWidget,
                               QGraphicsDropShadowEffect)

from icons import icon, APP_ICON_SVG

MARGIN = 14
ACCENT_PIN = "#2E7CF6"


def _app_icon() -> QIcon:
    from PySide6.QtSvg import QSvgRenderer
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    QSvgRenderer(APP_ICON_SVG.encode()).render(p)
    p.end()
    return QIcon(pm)


def _join_text(a: str, b: str) -> str:
    """相邻文字块拼接：中日韩之间不加空格，西文之间加空格。"""
    if not a:
        return b
    if not b:
        return a

    def cjk(ch):
        return '\u2e80' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' \
            or '\uff00' <= ch <= '\uffef'
    sep = "" if (cjk(a[-1]) or cjk(b[0])) else " "
    return a + sep + b


# 译文渲染字体候选：显式选第一个本机真实安装的家族。
# 硬编码 "Microsoft YaHei" 在没装雅黑/字体解析被替换的机器上，Qt 按各自规则
# 换字体，度量与实际字形容易对不齐，译文行间挤成重叠——这是换机才复现的根因。
_TR_FAMILIES = ("Microsoft YaHei", "Microsoft YaHei UI", "DengXian", "SimHei",
                "SimSun", "NSimSun", "Noto Sans CJK SC", "Source Han Sans SC",
                "Source Han Sans CN", "WenQuanYi Micro Hei", "Malgun Gothic",
                "Meiryo", "Yu Gothic UI", "MS Gothic")
_TR_FAMILY = None                     # 进程内缓存（字体表启动后不变）


def _tr_family() -> str:
    global _TR_FAMILY
    if _TR_FAMILY is None:
        installed = set(QFontDatabase.families())
        _TR_FAMILY = next((fam for fam in _TR_FAMILIES if fam in installed),
                          QFont().defaultFamily())
    return _TR_FAMILY


class BoxLayer(QWidget):
    """覆盖在图片上的绘制层：贴图边框 + OCR 文字块高亮。鼠标事件全部穿透。"""

    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _):
        self._win.paint_boxes(self)


class PinWindow(QWidget):
    """一张贴图：自动 OCR；文字上拖拽=选中（高亮）；Ctrl+C/右键复制；空白拖拽=移动。"""
    instances = []
    _id_seq = 0

    def __init__(self, pixmap: QPixmap, global_pos, cfg, ocr_runner, translator, parent=None):
        super().__init__(parent)
        PinWindow._id_seq += 1
        self.pin_id = PinWindow._id_seq
        self._cfg = cfg
        self._ocr = ocr_runner
        self._translator = translator
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowIcon(_app_icon())

        self._pix = pixmap
        self._scale = 1.0
        self._ocr_boxes = []          # [{'box': QRect(原始像素), 'text': str}]
        self._selected = set()
        self._band_start = None
        self._band = QRect()
        self._sel_rect = QRect()
        self._hover_i = -1
        self._press_mode = None       # 'select' / 'move'
        self._press_pos = QPoint()
        self._press_global = QPoint()
        self._moved = False
        self._translated_pix = None
        self._show_translated = False
        self._tr_blocks = []
        self._tr_results = []
        self._tr_wait = 0               # OCR 未完成时的自动重试轮次（防无限循环）
        self._esc_t0 = 0.0              # 上一次 Esc 时刻（双击 Esc 关闭用）
        self._toast_timer = QTimer(self, singleShot=True)
        self._toast_timer.timeout.connect(lambda: self._toast.hide())

        self._label = QLabel(self)
        self._label.setScaledContents(True)
        self._label.setMouseTracking(True)
        if cfg.get("pin", {}).get("shadow", True):
            eff = QGraphicsDropShadowEffect(self)
            eff.setBlurRadius(24)
            eff.setOffset(0, 4)
            eff.setColor(QColor(0, 0, 0, 70))
            self._label.setGraphicsEffect(eff)

        self._box_layer = BoxLayer(self)
        self._box_layer.raise_()

        self._toast = QLabel(self)
        self._toast.setStyleSheet(
            "background:rgba(30,33,41,.88);color:white;border-radius:4px;"
            "padding:4px 12px;font-size:12px;")
        self._toast.hide()

        geo = self._compute_geo(pixmap, global_pos)
        self.setGeometry(geo)
        self._apply_pix()
        self.setCursor(Qt.SizeAllCursor)
        PinWindow.instances.append(self)
        QTimer.singleShot(60, self._auto_ocr)

    # ------------------------------------------------ 几何
    def _current_pix(self) -> QPixmap:
        return self._translated_pix if (self._show_translated and self._translated_pix) \
            else self._pix

    def _content_size(self):
        pm = self._current_pix()
        w = int(pm.width() / (pm.devicePixelRatio() or 1.0) * self._scale)
        h = int(pm.height() / (pm.devicePixelRatio() or 1.0) * self._scale)
        return QSize(w, h)

    def _compute_geo(self, pix, global_pos):
        scr = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        avail = scr.availableGeometry()
        size = QSize(int(pix.width() / (pix.devicePixelRatio() or 1.0)),
                     int(pix.height() / (pix.devicePixelRatio() or 1.0)))
        max_w, max_h = int(avail.width() * 0.9), int(avail.height() * 0.9)
        self._scale = min(1.0, max_w / max(1, size.width()), max_h / max(1, size.height()))
        pos = QPoint(global_pos.x() - MARGIN, global_pos.y() - MARGIN)
        pos = QPoint(max(avail.left(), min(pos.x(), avail.right() - 120)),
                     max(avail.top(), min(pos.y(), avail.bottom() - 120)))
        return QRect(pos, QSize(size.width() + 2 * MARGIN, size.height() + 2 * MARGIN))

    def _apply_pix(self):
        size = self._content_size()
        self._label.setGeometry(MARGIN, MARGIN, size.width(), size.height())
        self._label.setPixmap(self._current_pix())
        self._box_layer.setGeometry(MARGIN, MARGIN, size.width(), size.height())
        self._box_layer.raise_()
        self.resize(size.width() + 2 * MARGIN, size.height() + 2 * MARGIN)
        self.update()

    # ------------------------------------------------ 缩放/透明度
    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if e.modifiers() & Qt.ControlModifier:
            op = self.windowOpacity() + (0.05 if dy > 0 else -0.05)
            self.setWindowOpacity(max(0.15, min(1.0, op)))
            self._toast_text(f"透明度 {int(self.windowOpacity() * 100)}%")
            return
        factor = 1.1 if dy > 0 else 1 / 1.1
        new_scale = max(0.1, min(8.0, self._scale * factor))
        if abs(new_scale - self._scale) < 1e-4:
            return
        rel = self._ev_pos(e) - QPoint(MARGIN, MARGIN)
        self._scale = new_scale
        self._apply_pix()
        self.move(self.x() + int(rel.x() * (1 - factor)),
                  self.y() + int(rel.y() * (1 - factor)))

    def _toast_text(self, text, ms=1600):
        self._toast.setText(text)
        self._toast.adjustSize()
        self._toast.move((self.width() - self._toast.width()) // 2,
                         max(0, self.height() - 40))
        self._toast.show()
        self._toast.raise_()
        self._toast_timer.start(ms)

    # ------------------------------------------------ 鼠标：文字上拖拽=选字，空白拖拽=移动
    def _ev_pos(self, e):
        """事件可能由 QLabel 冒泡而来（坐标是子控件局部的），统一映射回窗口坐标。"""
        return self.mapFromGlobal(e.globalPosition().toPoint())

    def mousePressEvent(self, e):
        self.setFocus()
        if e.button() != Qt.LeftButton:
            return
        pos = self._ev_pos(e)
        self._press_pos = pos
        self._press_global = e.globalPosition().toPoint()
        self._moved = False
        if e.modifiers() & Qt.ShiftModifier:
            self._band_start = pos
            self._band = QRect()
            self._press_mode = None
            return
        if self._box_at(pos) >= 0:
            self._press_mode = "select"
            self._sel_rect = QRect(pos, pos)
            self._select_chars(self._sel_rect)
        else:
            self._press_mode = "move"
            self.setCursor(Qt.SizeAllCursor)
            self._drag_at = self._press_global - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        pos = self._ev_pos(e)
        if e.buttons() & Qt.LeftButton:
            if self._band_start is not None:
                self._band = QRect(self._band_start, pos).normalized()
                self.update()
                return
            if self._press_mode == "select":
                self._sel_rect = QRect(self._press_pos, pos).normalized()
                self._select_chars(self._sel_rect)
                self.update()
                return
            if self._press_mode == "move":
                gp = e.globalPosition().toPoint()
                if (gp - self._press_global).manhattanLength() > 5:
                    self._moved = True
                self.move(gp - self._drag_at)
                return
        if self._ocr_boxes:
            i = self._box_at(pos)
            self.setCursor(Qt.IBeamCursor if i >= 0 else Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.SizeAllCursor)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._band_start is not None:
            band = self._band
            self._band_start = None
            self._band = QRect()
            if band.width() > 6 and band.height() > 6:
                self._ocr_region(band)
            self.update()
            return
        if self._press_mode == "select":
            if self._selected:
                self._toast_text(f"已选中 {len(self._selected)} 段 · Ctrl+C 复制", 1800)
            else:
                self._selected = set()
                self.update()
        self._press_mode = None

    def mouseDoubleClickEvent(self, e):
        # 双击 = 复制贴图 → 自动进入剪贴板历史；不关闭（防误触）。
        # 关闭只走双击 Esc / 右键菜单 / Alt+2
        QApplication.clipboard().setPixmap(self._current_pix())
        self._toast_text("已复制贴图 · 已进剪贴板历史 · 双击 Esc 关闭", 2200)
        e.accept()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self._selected:
                self._selected = set()
                self.update()
                self._esc_t0 = 0.0        # 清选字不算第一次 Esc
                return
            # 双击 Esc 才关闭：单击只提示，防误关
            now = time.time()
            if now - self._esc_t0 <= 0.6:
                self.close_me()
                return
            self._esc_t0 = now
            self._toast_text("再按一次 Esc 关闭贴图", 1500)
            return
        if e.key() == Qt.Key_C and e.modifiers() & Qt.ControlModifier:
            if self._selected:
                self._copy_selection()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------ 选字/复制
    def _box_at(self, pos) -> int:
        for i, it in enumerate(self._ocr_boxes):
            r = self._to_widget_rect(it["box"])
            if r.adjusted(-4, -4, 4, 4).contains(pos):
                return i
        return -1

    def _to_orig_rect(self, widget_rect: QRect) -> QRect:
        inner = widget_rect.adjusted(-MARGIN, -MARGIN, -MARGIN, -MARGIN)
        return QRect(int(inner.x() / self._scale), int(inner.y() / self._scale),
                     int(inner.width() / self._scale), int(inner.height() / self._scale))

    def _to_widget_rect(self, orig: QRect) -> QRect:
        return QRect(MARGIN + int(orig.x() * self._scale), MARGIN + int(orig.y() * self._scale),
                     int(orig.width() * self._scale), int(orig.height() * self._scale))

    def _char_widget_rect(self, bi: int, ci: int) -> QRect:
        """第 bi 块第 ci 个字符的窗口坐标矩形（字符矩形按当前缩放换算）。"""
        c = self._ocr_boxes[bi]["chars"][ci]
        return QRect(MARGIN + int(c.x() * self._scale), MARGIN + int(c.y() * self._scale),
                     max(2, int(c.width() * self._scale)), int(c.height() * self._scale))

    def _select_chars(self, widget_rect: QRect):
        """光标划过的字符（按字符矩形与拖拽范围相交）全部选中。"""
        sel = set()
        for bi, it in enumerate(self._ocr_boxes):
            box_r = self._to_widget_rect(it["box"])
            if not box_r.intersects(widget_rect):
                continue
            for ci, c in enumerate(it["chars"]):
                cr = QRect(MARGIN + int(c.x() * self._scale),
                           MARGIN + int(c.y() * self._scale),
                           max(2, int(c.width() * self._scale)),
                           int(c.height() * self._scale))
                if cr.intersects(widget_rect):
                    sel.add((bi, ci))
        if sel != self._selected:
            self._selected = sel
            self.update()

    def _copy_selection(self):
        if not self._selected:
            return
        # 按块分组：块内按字符下标拼接，块间按序衔接（旧实现每字符重复拼整块文本）
        blocks: dict[int, list[int]] = {}
        for bi, ci in self._selected:
            blocks.setdefault(bi, []).append(ci)
        text = ""
        for bi in sorted(blocks):
            seg = "".join(self._ocr_boxes[bi]["text"][c] for c in sorted(blocks[bi]))
            text = seg if not text else _join_text(text, seg)
        if text.strip():
            QApplication.clipboard().setText(text)
            self._toast_text(f"已复制 {len(self._selected)} 字：{text[:40]}")

    # ------------------------------------------------ OCR
    def _auto_ocr(self, retry=0):
        if self._ocr is None:
            return
        if self._ocr.busy():
            if retry < 6:
                QTimer.singleShot(600, lambda: self._auto_ocr(retry + 1))
            return
        self._ocr.done.connect(self._auto_ocr_done, Qt.UniqueConnection)
        self._ocr.failed.connect(self._auto_ocr_failed, Qt.UniqueConnection)
        self._toast_text("正在取字…", 4000)
        self._ocr.run(self._pix)

    def _unhook(self, done_slot, failed_slot):
        try:
            self._ocr.done.disconnect(done_slot)
            self._ocr.failed.disconnect(failed_slot)
        except Exception:
            pass

    def _auto_ocr_done(self, items):
        self._unhook(self._auto_ocr_done, self._auto_ocr_failed)
        self._set_boxes([QRect(d["box"][0][0], d["box"][0][1],
                               d["box"][1][0] - d["box"][0][0],
                               d["box"][3][1] - d["box"][0][1]) for d in items],
                        [d["text"] for d in items])
        if items:
            self._toast_text(f"识别到 {len(items)} 段 · 文字上拖拽选字 · Ctrl+C 复制 · Esc 关闭", 2600)

    def _auto_ocr_failed(self, msg):
        self._unhook(self._auto_ocr_done, self._auto_ocr_failed)
        if "正在进行" not in msg:
            self._toast_text(f"取字失败：{msg}")

    def _ocr_region(self, band: QRect):
        if self._ocr is None or self._ocr.busy():
            self._toast_text("OCR 进行中…")
            return
        orig = self._to_orig_rect(band)
        self._pending_offset = (orig.x(), orig.y())
        self._ocr.done.connect(self._region_ocr_done, Qt.UniqueConnection)
        self._ocr.failed.connect(self._auto_ocr_failed, Qt.UniqueConnection)
        self._ocr.run(self._pix.copy(orig))

    def _region_ocr_done(self, items):
        self._unhook(self._region_ocr_done, self._auto_ocr_failed)
        ox, oy = getattr(self, "_pending_offset", (0, 0))
        self._set_boxes([QRect(ox + d["box"][0][0], oy + d["box"][0][1],
                               d["box"][1][0] - d["box"][0][0],
                               d["box"][3][1] - d["box"][0][1]) for d in items],
                        [d["text"] for d in items])
        self._toast_text(f"已更新 {len(items)} 段文字")

    def _set_boxes(self, rects, texts):
        items = []
        for r, t in zip(rects, texts):
            if not t:
                continue
            n = len(t)
            cw = r.width() / max(1, n)
            chars = [QRect(int(r.x() + i * cw), r.y(), max(1, int(cw) + 1), r.height())
                     for i in range(n)]
            items.append({"box": r, "text": t, "chars": chars})
        items.sort(key=lambda it: (it["box"].y() // 12, it["box"].x()))
        self._ocr_boxes = items
        self._selected = set()   # {(框下标, 字符下标)}
        self.update()

    # ------------------------------------------------ 视觉行合并成块
    def _blocks(self):
        boxes = sorted(self._ocr_boxes, key=lambda it: (it["box"].y(), it["box"].x()))
        lines = []
        for it in boxes:
            b = it["box"]
            cy = b.center().y()
            placed = False
            for ln in lines:
                if abs(cy - ln["cy"]) <= max(12, 0.45 * min(b.height(), ln["h"])):
                    ln["items"].append(it)
                    ln["cy"] = sum(i["box"].center().y() for i in ln["items"]) / len(ln["items"])
                    ln["h"] = max(ln["h"], b.height())
                    placed = True
                    break
            if not placed:
                lines.append({"items": [it], "cy": cy, "h": b.height()})
        blocks = []
        for ln in lines:
            items = sorted(ln["items"], key=lambda i: i["box"].x())
            rect = items[0]["box"]
            text = ""
            for it in items:
                text = _join_text(text, it["text"])
            for it in items[1:]:
                rect = rect.united(it["box"])
            blocks.append({"rect": rect, "text": text, "h": ln["h"]})
        return blocks

    # ------------------------------------------------ 翻译：整批一次请求
    def translate_replace(self):
        if self._translator is None:
            return
        if self._show_translated:
            self.show_original()
            return
        if self._translator.busy():
            # 在途请求未完成就再点会重复发一批（两次 done 先后回包，译文来回跳）
            self._toast_text("上一批翻译还在进行中，请稍候…", 2000)
            return
        blocks = self._blocks()
        if not blocks:
            # 刚贴上就点翻译：自动取字还在跑，OCR 完成后自动重试——不用再手动点
            if self._ocr is not None and self._ocr.busy():
                self._tr_wait += 1
                if self._tr_wait <= 20:            # 最多等 ~12s
                    self._toast_text("正在取字…完成后自动开始翻译", 2500)
                    t = QTimer(self)               # 挂 self：窗口销毁计时器一并销毁
                    t.setSingleShot(True)
                    t.timeout.connect(self.translate_replace)
                    t.start(600)
                    return
            self._tr_wait = 0
            self._toast_text("未识别到文字", 2500)
            return
        self._tr_wait = 0
        target = self._cfg.get("translate", {}).get("target", "zh-CN")
        tgt_base = {"zh-CN": "zh", "zh-TW": "zh", "en": "en",
                    "ja": "ja", "ko": "ko"}.get(target, "zh")
        translatable = [b for b in blocks if self._block_lang(b["text"]) != tgt_base]
        skipped = len(blocks) - len(translatable)
        if not translatable:
            self._toast_text("原文与目标语言相同，无需翻译")
            return
        self._tr_blocks = translatable
        self._tr_results = [None] * len(translatable)
        self._tr_t0 = time.time()
        self._toast_text(f"翻译 {len(translatable)} 块中…" +
                         (f"（跳过 {skipped} 块同语言）" if skipped else ""), 3000)
        self._translator.done.connect(self._tr_done, Qt.UniqueConnection)
        self._translator.failed.connect(self._tr_failed, Qt.UniqueConnection)
        # 整批一次请求：done 按输入顺序回传等长 list，与 translatable 一一对应
        self._translator.translate([b["text"] for b in translatable], target, tag=0)

    def _tr_unhook(self):
        try:
            self._translator.done.disconnect(self._tr_done)
            self._translator.failed.disconnect(self._tr_failed)
        except Exception:
            pass

    def _tr_done(self, texts, tag):
        self._tr_unhook()
        if not isinstance(texts, list):              # 兼容旧的单串信号
            texts = [texts]
        self._tr_results = [t or "" for t in texts[:len(self._tr_blocks)]]
        while len(self._tr_results) < len(self._tr_blocks):
            self._tr_results.append("")
        self._translated_pix = self._build_replaced(self._tr_blocks, self._tr_results)
        self._show_translated = True
        self._apply_pix()
        elapsed = time.time() - getattr(self, "_tr_t0", time.time())
        # 显示实际生效引擎：降级到词典兜底时一眼可见，效果差有因可查
        eng = getattr(self._translator, "last_engine", "") or "翻译"
        self._toast_text(f"译文已替换原图 · {eng} · {elapsed:.1f}s · 右键显示原文", 2600)

    @staticmethod
    def _block_lang(t: str) -> str | None:
        """块语言粗判：纯文字块才判语言（ko/ja/zh），含拉丁字母的混合块返回 None。

        旧版按 CJK 占比 >30% 跳过，会把「Network 错误」这类混合块整体漏翻；
        现在与目标语言比对：混合块需要翻译，只有整块已是目标语言才跳过。
        """
        has_kana = any('ぁ' <= ch <= 'ヿ' for ch in t)    # 平假名 U+3040–309F + 片假名 U+30A0–30FF
        has_hangul = any('가' <= ch <= '힣' for ch in t)
        has_cjk = any('⺀' <= ch <= '鿿' for ch in t)
        has_latin = any(ch.isascii() and ch.isalpha() for ch in t)
        if has_hangul and not (has_latin or has_kana or has_cjk):
            return "ko"
        if has_kana and not has_latin:                     # 假名+汉字 = 纯日文
            return "ja"
        if has_cjk and not has_latin:
            return "zh"
        return None

    def _tr_failed(self, msg, tag):
        self._tr_unhook()
        self._toast_text(f"翻译失败：{msg}", 3000)

    def _build_replaced(self, blocks, translations) -> QPixmap:
        pm = self._pix.copy()
        dpr = pm.devicePixelRatio() or 1.0
        pm.setDevicePixelRatio(1.0)   # 画布统一到设备像素坐标（与 OCR 框一致），不受系统缩放干扰
        img = pm.toImage().convertToFormat(QImage.Format_RGB32)  # 采样只转一次
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        jobs = []
        for blk, trans in zip(blocks, translations):
            if not trans:
                continue
            rect = blk["rect"].adjusted(-2, -2, 2, 2)
            bg = self._sample_bg(img, rect)
            fg = self._sample_fg(img, rect, bg)
            jobs.append((rect, bg, fg, trans, blk["h"]))
        # 两遍绘制：先全部铺底色、再统一写译文。紧凑排版截图里 OCR 块上下紧挨
        # （行距小于 ±2px 外扩），单块顺序画会让下一块的底色矩形擦掉上一块刚写的译文。
        p.setPen(Qt.NoPen)
        for rect, bg, _fg, _trans, _h in jobs:
            p.setBrush(QBrush(bg))
            p.drawRect(rect)
        for rect, _bg, fg, trans, h in jobs:
            self._draw_trans_text(p, rect, fg, trans, h)
        p.end()
        pm.setDevicePixelRatio(dpr)   # 恢复 DPR，贴图显示尺寸不变
        return pm

    def _draw_trans_text(self, p: QPainter, rect: QRect, fg: QColor, trans: str, h0: int):
        """译文逐行显式排版：行进距 = 字高 + 间隙，任何机器/字体上都不重叠。

        旧实现一次 drawText 交由字体自身 lineSpacing 摆行：YaHei 部分字号
        leading≤0（行距≤字高），不重叠全凭字形没画满 em 框；字体解析不同的
        机器上行带直接相接，看起来就是重叠。现在行位置自己算，几何上保证间隙。

        纯像素字号（pointSize 受系统 DPI 影响，译文整体偏大约 1/3）：
        先按原行高给值，逐 px 缩到行数×行进距严格放得下。
        """
        w = max(8, rect.width())
        size = max(8, int(round(h0 * 0.75)))
        while True:
            f = QFont(_tr_family())
            f.setPixelSize(size)
            p.setFont(f)
            fm = p.fontMetrics()          # 度量取画笔所在设备，与实际绘制严格同源
            advance = fm.height() + max(1, size // 8)
            lines = self._wrap_lines(trans, f, w)
            if size <= 8 or len(lines) * advance <= rect.height():
                break
            size -= 1
        p.setPen(QPen(fg, 1))
        # 极小框缩到下限仍放不下时：只画放得下的整行，宁缺不压行（不再半截字）
        p.setClipRect(rect)
        y = rect.top()
        for ln in lines:
            if y + fm.height() > rect.bottom() + 1:
                break
            p.drawText(QRect(rect.left(), y, w, fm.height()),
                       Qt.AlignLeft | Qt.AlignTop, ln)
            y += advance
        p.setClipping(False)

    @staticmethod
    def _wrap_lines(text: str, font: QFont, w: int) -> list:
        """与 TextWordWrap 同款断行规则（QTextLayout），返回每行字符串。"""
        tl = QTextLayout(text, font)
        opt = tl.textOption()
        opt.setWrapMode(QTextOption.WrapMode.WordWrap)
        tl.setTextOption(opt)
        out = []
        tl.beginLayout()
        while True:
            ln = tl.createLine()
            if not ln.isValid():
                break
            ln.setLineWidth(w)
            out.append(text[ln.textStart():ln.textStart() + ln.textLength()])
        tl.endLayout()
        return out

    @staticmethod
    def _sample_bg(img: QImage, box: QRect) -> QColor:
        xs = [max(0, box.left() - 3), min(img.width() - 1, box.right() + 3)]
        ys = [max(0, box.top() - 3), min(img.height() - 1, box.bottom() + 3)]
        rs = gs = bs = n = 0
        for x in xs:
            for y in range(max(0, box.top()), min(img.height(), box.bottom() + 1), 4):
                c = img.pixelColor(x, y)
                rs += c.red(); gs += c.green(); bs += c.blue(); n += 1
        for y in ys:
            for x in range(max(0, box.left()), min(img.width(), box.right() + 1), 4):
                c = img.pixelColor(x, y)
                rs += c.red(); gs += c.green(); bs += c.blue(); n += 1
        if n == 0:
            return QColor(Qt.white)
        return QColor(rs // n, gs // n, bs // n)

    @staticmethod
    def _sample_fg(img: QImage, box: QRect, bg: QColor) -> QColor:
        darkest = QColor(0, 0, 0)
        min_lum = 1e9
        for y in range(box.top(), box.bottom() + 1, 3):
            for x in range(box.left(), box.right() + 1, 3):
                c = img.pixelColor(x, y)
                if c.lightness() < min_lum:
                    min_lum, darkest = c.lightness(), c
        if bg.lightness() < 110:
            return QColor(Qt.white)
        return darkest if min_lum < 140 else QColor(Qt.black)

    def show_original(self):
        self._show_translated = False
        self._apply_pix()

    def show_translated(self):
        if self._translated_pix:
            self._show_translated = True
            self._apply_pix()

    # ------------------------------------------------ 右键菜单
    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #E5E6EB;border-radius:8px;padding:4px;}"
            "QMenu::item{padding:6px 28px;border-radius:5px;font-size:13px;}"
            "QMenu::item:selected{background:#EAF2FF;color:#2E7CF6;}"
            "QMenu::separator{height:1px;background:#E5E6EB;margin:4px 8px;}")
        if self._show_translated:
            menu.addAction(icon("nav_look", "#4E5969"), "显示原文", self.show_original)
        else:
            menu.addAction(icon("nav_look", "#4E5969"), "翻译（替换原图）", self.translate_replace)
            if self._translated_pix:
                menu.addAction("显示译文", self.show_translated)
        if self._selected:
            menu.addAction(icon("copy", "#4E5969"), "复制选中文字", self._copy_selection)
        if self._ocr_boxes:
            menu.addAction("复制全部文字", self._copy_all_text)
        menu.addSeparator()
        menu.addAction(icon("copy", "#4E5969"), "复制贴图",
                       lambda: QApplication.clipboard().setPixmap(self._current_pix()))
        menu.addAction(icon("save", "#4E5969"), "保存", self._save)
        zoom = menu.addMenu("缩放")
        for pct in (50, 75, 100, 150, 200):
            zoom.addAction(f"{pct}%", lambda p=pct: self._set_zoom(pct / 100))
        op = menu.addMenu("透明度")
        for pct in (30, 50, 70, 100):
            op.addAction(f"{pct}%", lambda p=pct: self.setWindowOpacity(p / 100))
        menu.addSeparator()
        menu.addAction(icon("close", "#4E5969"), "关闭贴图", self.close_me)
        menu.addAction("关闭所有贴图", PinWindow.close_all)
        menu.exec(e.globalPos())

    def _copy_all_text(self):
        blocks = self._blocks()
        text = "\n".join(b["text"] for b in blocks) if blocks else \
            "\n".join(it["text"] for it in self._ocr_boxes)
        QApplication.clipboard().setText(text)
        self._toast_text("已复制全部文字")

    def _save(self):
        import os
        import time
        d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "pin_" + time.strftime("%Y%m%d_%H%M%S") + ".png")
        self._current_pix().save(path, "PNG")
        self._toast_text(f"已保存 {path}")

    def _set_zoom(self, s):
        self._scale = s
        self._apply_pix()

    # ------------------------------------------------ 生命周期
    def close_me(self):
        self.deleteLater()
        if self in PinWindow.instances:
            PinWindow.instances.remove(self)

    def closeEvent(self, ev):
        if self in PinWindow.instances:
            PinWindow.instances.remove(self)
        super().closeEvent(ev)

    # ------------------------------------------------ 绘制：在 BoxLayer 上
    def paint_boxes(self, layer):
        p = QPainter(layer)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = layer.width(), layer.height()
        # 亮色边框（任何底色都可见）
        p.setPen(QPen(QColor(46, 124, 246), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        # 选中字符高亮：译文模式同样绘制（OCR 框与替换底色同坐标系，高亮位置一致），
        # 此前译文模式直接 return，导致翻译后选字无任何视觉反馈、看起来失效
        for (bi, ci) in self._selected:
            if bi >= len(self._ocr_boxes):
                continue
            r = self._char_widget_rect(bi, ci).translated(-MARGIN, -MARGIN)
            p.setBrush(QBrush(QColor(46, 124, 246, 120)))
            p.setPen(Qt.NoPen)
            p.drawRect(r)

    # ------------------------------------------------ 类操作
    @classmethod
    def close_all(cls):
        for w in list(cls.instances):
            w.close_me()

    @classmethod
    def toggle_visible(cls):
        vis = [w for w in cls.instances if w.isVisible()]
        if vis:
            for w in cls.instances:
                w.hide()
        else:
            for w in cls.instances:
                w.show()


if __name__ == "__main__":
    # 离屏自测：QT_QPA_PLATFORM=offscreen python pinwin.py
    # 翻译替换：像素字号严格适配不越界 + DPR 保存/恢复（贴图显示尺寸不变）
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import sys

    import config
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)

    def _make_pix(dpr=1.0):
        pix = QPixmap(300, 120)
        pix.fill(Qt.white)
        pix.setDevicePixelRatio(dpr)
        p = QPainter(pix)
        p.setPen(QPen(Qt.black, 1))
        p.setFont(QFont("Microsoft YaHei", 10))
        p.drawText(QRect(20, 20, 260, 20), Qt.AlignLeft | Qt.AlignVCenter, "The quick brown fox")
        p.drawText(QRect(20, 60, 260, 20), Qt.AlignLeft | Qt.AlignVCenter, "Second line here")
        p.end()
        return pix

    blocks = [{"rect": QRect(20, 20, 260, 22), "text": "The quick brown fox", "h": 20},
              {"rect": QRect(20, 60, 260, 22), "text": "Second line here", "h": 20}]

    for dpr in (1.0, 2.0):
        pw = PinWindow(_make_pix(dpr), QPoint(30, 30), config.load(), None, None)
        out = pw._build_replaced(blocks, ["你好，世界。这是一段中文翻译。",
                                          "第二行也翻译成中文。"])
        assert out.width() == 300 and out.height() == 120, "设备像素尺寸不应改变"
        assert (out.devicePixelRatio() or 1.0) == dpr, "DPR 应原样恢复"
        pw.close_me()
    print("pinwin 自测: 翻译字号/DPR OK，继续译文行距硬保证…")

    # ---- 译文行距硬保证：逐行显式排版后墨迹带之间必有间隙（换机重叠的回归）
    # 机器上字体解析各不相同，这里直接校验渲染结果的墨迹几何：
    # 多行译文的相邻墨迹带之间必须至少有 1px 空隙，行带相接/合并即失败。
    def _ink_gaps(img, box):
        bands, cur = [], None
        for y in range(box.top(), box.bottom() + 1):
            ink = any(img.pixelColor(x, y).lightness() < 200
                      for x in range(box.left(), box.right() + 1))
            if ink and cur is None:
                cur = [y, y]
            elif ink:
                cur[1] = y
            elif cur is not None:
                bands.append(tuple(cur))
                cur = None
        if cur is not None:
            bands.append(tuple(cur))
        return [bands[i + 1][0] - bands[i][1] - 1 for i in range(len(bands) - 1)]

    pw = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    # 窄块 + 长译文 → 多行换行；块高足够 → 至少两行
    dense_blocks = [{"rect": QRect(20, 20, 120, 44), "text": "Original", "h": 44}]
    out = pw._build_replaced(dense_blocks,
                             ["这是一段比较长的中文翻译需要换行才能放下测试行距"])
    out.setDevicePixelRatio(1.0)
    gaps = _ink_gaps(out.toImage(), QRect(18, 18, 124, 48))
    assert len(gaps) >= 1, f"窄块长译文应换出多行，实际墨迹带 {gaps}"
    assert min(gaps) >= 1, f"译文行带相接/重叠: {gaps}"
    print(f"pinwin 自测: 译文行距硬保证 OK（行带间隙 {gaps}），继续两遍绘制回归…")

    # ---- 两遍绘制回归：紧凑双块（下块底色矩形会盖住上块译文尾部）不再互相擦字
    # 构造：A 块高、译文换出 4 行，末行墨迹压进 B 块的深红底色矩形。
    # A 原文黑色 → 译文近黑(max<100)；B 红底白字 → 填充红(max=150)、译文白(max=255)。
    # 旧的单块顺序下 B 的红色矩形会擦掉 A 的末行墨迹，近黑像素消失即回归失败。
    pix2 = QPixmap(300, 160)
    pix2.fill(Qt.white)
    p2 = QPainter(pix2)
    p2.setPen(QPen(Qt.black, 1))
    p2.setFont(QFont("Microsoft YaHei", 10))
    p2.drawText(QRect(20, 40, 120, 20), Qt.AlignLeft | Qt.AlignVCenter, "First line text")
    p2.fillRect(15, 60, 135, 30, QColor(150, 30, 30))
    p2.setPen(QPen(Qt.white, 1))
    p2.drawText(QRect(20, 66, 120, 12), Qt.AlignLeft | Qt.AlignVCenter, "Second line")
    p2.end()
    pw2 = PinWindow(pix2, QPoint(30, 30), config.load(), None, None)
    tight = [{"rect": QRect(20, 18, 120, 72), "text": "First line text", "h": 72},
             {"rect": QRect(20, 64, 120, 16), "text": "Second line", "h": 16}]
    out2 = pw2._build_replaced(
        tight,
        ["这是一段比较长的中文翻译需要换行显示完整测试多行排列效果",
         "第二行译文"])
    out2.setDevicePixelRatio(1.0)
    img2 = out2.toImage()
    tail = [img2.pixelColor(x, y) for x in range(20, 115) for y in range(71, 80)]
    assert any(max(c.red(), c.green(), c.blue()) < 100 for c in tail), \
        "下块铺底色不应擦掉上块译文（两遍绘制回归失败）"
    pw2.close_me()
    print("pinwin 自测: 两遍绘制 OK，继续翻译守卫与译文选字…")

    # ---- 翻译守卫：OCR 未完成不静默失败(自动重试)；在途请求去重
    from PySide6.QtCore import QObject, Signal as _Signal

    class _FakeOcr:
        def __init__(self, busy=True):
            self._b = busy

        def busy(self):
            return self._b

    class _FakeTr(QObject):
        done = _Signal(list, int)
        failed = _Signal(str, int)

        def __init__(self, busy=False):
            super().__init__()
            self._b = busy
            self.calls = []

        def busy(self):
            return self._b

        def translate(self, texts, target, source="auto", tag=0):
            self.calls.append(list(texts))

    pw = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    tr = _FakeTr()
    pw._translator = tr

    tr._b = True                                   # 上一批还在途 → 不重复发请求
    pw.translate_replace()
    assert tr.calls == [], "在途翻译时再点不应重复发请求"

    tr._b = False
    pw._ocr = _FakeOcr(busy=True)                  # 自动取字还在跑 → 等待重试，不报无文字
    pw.translate_replace()
    assert tr.calls == [], "OCR 进行中不应发请求"

    pw._ocr = _FakeOcr(busy=False)                 # 取字完成（模拟 OCR 回来）
    pw._set_boxes([QRect(20, 20, 260, 22)], ["The quick brown fox"])
    pw.translate_replace()
    assert tr.calls == [["The quick brown fox"]], f"应发起翻译: {tr.calls}"
    assert pw._tr_wait == 0, "正常路径应清零等待计数"

    # ---- 译文模式下选字高亮必须可见（此前直接 return，看起来选字失效）
    pw._translated_pix = pw._build_replaced(
        [{"rect": QRect(20, 20, 260, 22), "text": "The quick brown fox", "h": 20}],
        ["你好，世界。"])
    pw._show_translated = True
    pw._selected = {(0, 0)}
    layer = QPixmap(pw._label.size())
    layer.fill(Qt.transparent)
    pw.paint_boxes(layer)
    c = layer.toImage().pixelColor(30, 28)         # 第一个字符矩形内部
    assert c.alpha() > 0, "译文模式下选中字符应有高亮"
    pw.close_me()
    print("pinwin 自测: 翻译/选字 OK，继续双击禁用与双击 Esc 关闭…")

    # ---- 双击=复制不关闭（防误触）；关闭只走双击 Esc
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QKeyEvent, QMouseEvent

    pw2 = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    dbl = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(50, 50), QPointF(80, 80),
                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    pw2.mouseDoubleClickEvent(dbl)
    assert pw2 in PinWindow.instances, "双击不得关闭贴图"
    assert not QApplication.clipboard().pixmap().isNull(), "双击应复制贴图"

    esc = lambda: pw2.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                              Qt.NoModifier))
    esc()                                          # 第一击：仅提示
    assert pw2 in PinWindow.instances, "单击 Esc 不得关闭贴图"
    esc()                                          # 第二击（0.6s 窗口内）：关闭
    assert pw2 not in PinWindow.instances, "双击 Esc 应关闭贴图"

    pw3 = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    pw3._selected = {(0, 0)}
    pw3.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert pw3._selected == set() and pw3 in PinWindow.instances, \
        "有选字时 Esc 应先清选字"
    pw3.close_me()
    print("pinwin 自测通过: 翻译/选字/双击禁用/双击Esc关闭 全部 OK")
