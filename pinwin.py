"""贴图窗口 v3 — 贴图即取字（拖拽选字/点击复制）+ 分块并发翻译 + 译文按块替换。"""
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QIcon, QImage,
                           QMouseEvent, QPainter, QPen, QPixmap, QTextLayout,
                           QTextOption)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                               QGraphicsScene, QGraphicsView, QLabel, QMenu,
                               QWidget)

from icons import icon, APP_ICON_SVG
from ocrtext_layer import OcrTextItemLayer

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


class _TextLayerView(QGraphicsView):
    """PixPin 文字图层的显示与交互宿主。

    透明无边框叠在图片上：命中文字 → 交给图层做「字符区间」选字；
    未命中 → 把事件翻译回窗口坐标再转发给 PinWindow，保持「空白拖拽移动
    贴图」的原手感（QGraphicsView 默认会吞掉所有鼠标事件）。
    """

    def __init__(self, win, scene):
        super().__init__(scene, win)
        self._win = win
        self.setStyleSheet("background:transparent;border:none;")
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.IBeamCursor)

    def _scene_pos(self, e) -> QPointF:
        return self.mapToScene(e.position().toPoint())

    def _forward(self, e, handler):
        gp = e.globalPosition().toPoint()
        ev = QMouseEvent(e.type(), QPointF(self._win.mapFromGlobal(gp)),
                         e.globalPosition(), e.button(), e.buttons(),
                         e.modifiers())
        handler(ev)

    def mousePressEvent(self, e):
        layer = self._win._text_layer
        pos = self._scene_pos(e)
        if e.button() == Qt.LeftButton and layer.hit_test(pos) is not None:
            layer.begin_selection(pos)
            self._win._selecting = True
            e.accept()
            return
        e.ignore()
        self._forward(e, self._win.mousePressEvent)

    def mouseMoveEvent(self, e):
        layer = self._win._text_layer
        if self._win._selecting:
            layer.drag_selection(self._scene_pos(e))
            e.accept()
            return
        pos = self._scene_pos(e)
        self.setCursor(Qt.IBeamCursor if layer.hit_test(pos) is not None
                       else Qt.SizeAllCursor)
        e.ignore()
        self._forward(e, self._win.mouseMoveEvent)

    def mouseReleaseEvent(self, e):
        if self._win._selecting:
            self._win._text_layer.end_selection()
            self._win._selecting = False
            self._win._after_select()
            e.accept()
            return
        e.ignore()
        self._forward(e, self._win.mouseReleaseEvent)

    def mouseDoubleClickEvent(self, e):
        e.ignore()
        self._forward(e, self._win.mouseDoubleClickEvent)

    def wheelEvent(self, e):
        e.ignore()
        self._forward(e, self._win.wheelEvent)


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
        self._selecting = False       # 正在拖拽选字（PixPin 文字图层接管）
        self._band_start = None
        self._band = QRect()
        self._press_mode = None       # 'move'（选字已交给文字图层）
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

        # PixPin 文字图层（OcrTextItemLayer / OcrGraphicsTextItem 复刻）：
        # 每个识别块是一个 QGraphicsItem，选中态是字符区间
        self._scene = QGraphicsScene(self)
        self._text_layer = OcrTextItemLayer()
        self._scene.addItem(self._text_layer)
        self._view = _TextLayerView(self, self._scene)
        self._view.raise_()

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
        # 文字图层：scene 用图像原始像素坐标，缩放交给 view 的 transform，
        # 这样 item 内部（字符插值、二分命中）完全不用管 zoom
        pm = self._current_pix()
        iw = max(1, pm.width())
        ih = max(1, pm.height())
        self._scene.setSceneRect(0, 0, iw, ih)
        self._view.resetTransform()
        self._view.scale(size.width() / iw, size.height() / ih)
        self._view.setGeometry(MARGIN, MARGIN, size.width(), size.height())
        self._view.raise_()
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
        self._press_global = e.globalPosition().toPoint()
        self._moved = False
        if e.modifiers() & Qt.ShiftModifier:
            self._band_start = pos
            self._band = QRect()
            self._press_mode = None
            return
        # 选字已交给 PixPin 文字图层：_TextLayerView 命中文字时自行处理，
        # 只有未命中才把事件转发到这里 → 这里只剩「移动贴图」
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
            if self._press_mode == "move":
                gp = e.globalPosition().toPoint()
                if (gp - self._press_global).manhattanLength() > 5:
                    self._moved = True
                self.move(gp - self._drag_at)
                return
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
        self._press_mode = None

    def _after_select(self):
        """选字结束（文字图层回调）：提示选中字数。"""
        n = self._text_layer.selection_count()
        if n:
            self._toast_text(f"已选中 {n} 字 · Ctrl+C 复制", 1800)

    def mouseDoubleClickEvent(self, e):
        # 双击 = 复制贴图 → 自动进入剪贴板历史；不关闭（防误触）。
        # 关闭只走双击 Esc / 右键菜单 / Alt+2
        QApplication.clipboard().setPixmap(self._current_pix())
        self._toast_text("已复制贴图 · 已进剪贴板历史 · 双击 Esc 关闭", 2200)
        e.accept()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self._text_layer.has_selection():
                self._text_layer.clear_selection()
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
            if self._text_layer.has_selection():
                self._copy_selection()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------ 选字/复制（文字图层接管）
    def _to_orig_rect(self, widget_rect: QRect) -> QRect:
        inner = widget_rect.adjusted(-MARGIN, -MARGIN, -MARGIN, -MARGIN)
        return QRect(int(inner.x() / self._scale), int(inner.y() / self._scale),
                     int(inner.width() / self._scale), int(inner.height() / self._scale))

    def _to_widget_rect(self, orig: QRect) -> QRect:
        return QRect(MARGIN + int(orig.x() * self._scale), MARGIN + int(orig.y() * self._scale),
                     int(orig.width() * self._scale), int(orig.height() * self._scale))

    def _copy_selection(self, _checked=False):
        """复制选中文字：文本由文字图层按「字符区间」回收，块间用 `_join_text`
        衔接（中日韩不加空格、西文加空格）。"""
        text = self._text_layer.selection_text(_join_text)
        if text.strip():
            n = self._text_layer.selection_count()
            QApplication.clipboard().setText(text)
            self._toast_text(f"已复制 {n} 字：{text[:40]}")

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
                        [d["text"] for d in items],
                        [d["box"] for d in items])
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
                        [d["text"] for d in items],
                        [[[p[0] + ox, p[1] + oy] for p in d["box"]]
                         for d in items])
        self._toast_text(f"已更新 {len(items)} 段文字")

    def _set_boxes(self, rects, texts, quads=None):
        """把识别结果同时喂给两处：

        - `_ocr_boxes`：给翻译链（`_blocks` / `_build_replaced`）用，结构不变
        - PixPin 文字图层：四角 + 文本 + 置信度，负责选字与高亮

        `quads` 传 OCR 的原始四点（可能是斜的），图层据此做四边形插值；
        没有时退化为矩形四点。
        """
        items = []
        for i, (r, t) in enumerate(zip(rects, texts)):
            if not t:
                continue
            items.append({"box": r, "text": t,
                          "quad": quads[i] if quads and i < len(quads) else None})
        items.sort(key=lambda it: (it["box"].y() // 12, it["box"].x()))
        self._ocr_boxes = items
        self._text_layer.set_boxes([
            {"quad": it["quad"] if it["quad"] else
                [[it["box"].x(), it["box"].y()],
                 [it["box"].right() + 1, it["box"].y()],
                 [it["box"].right() + 1, it["box"].bottom() + 1],
                 [it["box"].x(), it["box"].bottom() + 1]],
             "text": it["text"], "score": it.get("score", 0.0)}
            for it in items])
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
            # 空/未变的译文：不擦底不重画，原样保留原文像素——绝不因翻译丢内容
            if not trans or trans == blk["text"]:
                continue
            rect = blk["rect"].adjusted(-2, -2, 2, 2)
            fit = self._fit_trans(p, trans, rect, blk["h"])
            if fit is None:
                # 译文缩到字号下限仍放不下：整块保留原文。旧实现照擦原文、
                # 只画放得下的几行，译文一长字段内容就“消失”（用户实测丢字段）
                continue
            bg = self._sample_bg(img, rect)
            fg = self._sample_fg(img, rect, bg)
            jobs.append((rect, bg, fg, fit))
        # 两遍绘制：先全部铺底色、再统一写译文。紧凑排版截图里 OCR 块上下紧挨
        # （行距小于 ±2px 外扩），单块顺序画会让下一块的底色矩形擦掉上一块刚写的译文。
        p.setPen(Qt.NoPen)
        for rect, bg, _fg, _fit in jobs:
            p.setBrush(QBrush(bg))
            p.drawRect(rect)
        for rect, _bg, fg, fit in jobs:
            self._draw_trans_text(p, rect, fg, fit)
        p.end()
        pm.setDevicePixelRatio(dpr)   # 恢复 DPR，贴图显示尺寸不变
        return pm

    def _fit_trans(self, p: QPainter, trans: str, rect: QRect, h0: int):
        """译文排版预算：像素字号按原行高 0.72 起、逐 px 缩到下限 8，行数×
        行进距必须完全放进 rect 才算适配（度量取画笔所在设备，与绘制同源）。
        放不下返回 None，由调用方保留原文——这是“宁可不翻，不能丢内容”的出口。
        """
        w = max(8, rect.width())
        size = max(8, int(round(h0 * 0.72)))
        while True:
            f = QFont(_tr_family())
            f.setPixelSize(size)
            p.setFont(f)
            fm = p.fontMetrics()
            advance = fm.height() + max(1, size // 8)
            lines = self._wrap_lines(trans, f, w)
            if len(lines) * advance <= rect.height():
                return (f, lines, advance, fm.height())
            if size <= 8:
                return None
            size -= 1

    def _draw_trans_text(self, p: QPainter, rect: QRect, fg: QColor, fit):
        """按 _fit_trans 预算好的 (字体, 行, 行进距) 逐行显式排版绘制：
        行位置自算、几何保证不重叠（不同机器字体解析差异下依然成立）。
        适配检查已在 _fit_trans 完成，这里整段照画，不再截断行。"""
        f, lines, advance, fh = fit
        p.setFont(f)
        p.setPen(QPen(fg, 1))
        p.setClipRect(rect)
        w = max(8, rect.width())
        y = rect.top()
        for ln in lines:
            p.drawText(QRect(rect.left(), y, w, fh),
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
        if self._text_layer.has_selection():
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
        """只画贴图外框；OCR 文字块与选中高亮由 PixPin 文字图层
        （OcrTextItemLayer 的 QGraphicsItem）绘制，避免两套坐标系各画一遍。"""
        p = QPainter(layer)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = layer.width(), layer.height()
        # 亮色边框（任何底色都可见）
        p.setPen(QPen(QColor(46, 124, 246), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)

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

    # ---- 翻译不丢内容回归：放不下的译文整块保留原文；译文=原文不动像素
    pw_t = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    # 极矮块 + 必换行的超长译文：任何字号都放不下 → 原文必须原样保留
    tiny = [{"rect": QRect(20, 60, 260, 10), "text": "Second line here", "h": 10}]
    out_t = pw_t._build_replaced(
        tiny, ["这是一段长得根本放不下必须要换行好几行的超长译文必须整块保留原文不能擦掉原来的任何内容"])
    out_t.setDevicePixelRatio(1.0)
    it = out_t.toImage()
    orig = _make_pix(1.0).toImage()
    dark_before = sum(1 for x in range(22, 278) for y in range(62, 76)
                      if orig.pixelColor(x, y).lightness() < 128)
    dark_after = sum(1 for x in range(22, 278) for y in range(62, 76)
                     if it.pixelColor(x, y).lightness() < 128)
    assert dark_before > 0 and dark_after == dark_before, \
        f"放不下的译文必须保留原文墨迹: {dark_before} -> {dark_after}"
    # 译文与原文相同：不擦不画（混合语块引擎原样返回时不丢像素）
    out_e = pw_t._build_replaced(tiny, ["Second line here"])
    out_e.setDevicePixelRatio(1.0)
    assert out_e.toImage() == orig, "译文=原文时输出必须与原图逐像素一致"
    pw_t.close_me()
    print("pinwin 自测: 翻译不丢内容 OK，继续翻译守卫与译文选字…")

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
    # PixPin 文字图层：选中态是字符区间，高亮由 QGraphicsItem 自己绘制
    pw._set_boxes([QRect(20, 20, 260, 22)], ["The quick brown fox"])
    item = pw._text_layer.items[0]
    item.set_selection(4, 8)
    assert item.selected_text() == "quick", item.selected_text()
    canvas = QImage(max(1, int(pw._scene.width())),
                    max(1, int(pw._scene.height())), QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    pp = QPainter(canvas)
    pw._scene.render(pp)
    pp.end()
    assert canvas.pixelColor(100, 28).alpha() > 0, "译文模式下选中字符应有高亮"
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
    pw3._set_boxes([QRect(20, 20, 260, 22)], ["Hello world"])
    pw3._text_layer.begin_selection(QPointF(25, 30))
    pw3._text_layer.drag_selection(QPointF(200, 30))
    pw3._text_layer.end_selection()
    assert pw3._text_layer.has_selection(), "应已选中文字"
    pw3.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not pw3._text_layer.has_selection() and pw3 in PinWindow.instances, \
        "有选字时 Esc 应先清选字"
    pw3.close_me()

    # ---- 文字图层跨块流式选择（PixPin 字符区间语义）
    pw4 = PinWindow(_make_pix(1.0), QPoint(30, 30), config.load(), None, None)
    pw4._set_boxes([QRect(20, 20, 260, 22), QRect(20, 60, 260, 22)],
                   ["The quick brown fox", "Second line here"])
    tl = pw4._text_layer
    assert len(tl.items) == 2, len(tl.items)
    tl.begin_selection(QPointF(100, 31))          # 第一行中部
    tl.drag_selection(QPointF(150, 71))           # 第二行中部
    ranges = tl.selection_ranges()
    assert len(ranges) == 2, ranges
    assert ranges[0][2] == 18, f"第一行应从锚点延伸到行尾: {ranges[0]}"
    assert ranges[1][1] == 0, f"第二行必须从首字开始: {ranges[1]}"
    assert ranges[1][2] < 15, f"当前列之后的字符不应选中: {ranges[1]}"
    tl.end_selection()
    # 反向：从第二行划回第一行
    tl.clear_selection()
    tl.begin_selection(QPointF(150, 71))
    tl.drag_selection(QPointF(100, 31))
    r2 = tl.selection_ranges()
    assert len(r2) == 2 and r2[1][1] == 0, r2
    # 同行只波及一块
    tl.clear_selection()
    tl.begin_selection(QPointF(30, 31))
    tl.drag_selection(QPointF(120, 31))
    assert len(tl.selection_ranges()) == 1, tl.selection_ranges()
    # 原地按下：只选 1 个字符（字符区间语义，不是整块）
    tl.clear_selection()
    tl.begin_selection(QPointF(30, 31))
    assert tl.selection_count() == 1, tl.selection_count()
    # 复制：字符区间回收文本
    assert tl.selection_text(), tl.selection_text()
    pw4.close_me()
    print("pinwin 自测通过: 翻译/文字图层选字/跨块流式/双击禁用/双击Esc关闭 全部 OK")
