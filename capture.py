"""截图覆盖层 — 暗化遮罩 + 窗口吸附 + 选区 + PixPin 风格工具栏 + 标注。"""
import ctypes
import math
import os
import shutil
import tempfile
import time

import numpy as np
from ctypes import wintypes

from PySide6.QtCore import (QMimeData, QPoint, QRect, QRectF, QSize, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QImage,
                           QMovie, QPolygon, QRegion,
                           QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QTextEdit,
                               QToolButton, QVBoxLayout, QWidget,
                               QGraphicsDropShadowEffect)

from icons import icon

ACCENT = "#2E7CF6"
_COLORS = ["#F53F3F", "#00B42A", "#2E7CF6", "#FF7D00", "#1D2129", "#FFFFFF", "#F7BA1E", "#7B61FF"]
_DRAW_TOOLS = ("rect", "line", "arrow", "ellipse", "pen", "marker", "mosaic")

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi


# ---------------------------------------------------------------- 工具栏
class CaptureToolbar(QWidget):
    """PixPin 风格工具栏；选中绘图工具时在第二栏展开色板。"""

    action = Signal(str)
    _TOOLS_WITH_COLOR = ("rect", "line", "arrow", "ellipse", "pen", "marker",
                         "mosaic", "text", "serial")

    def __init__(self, parent=None, color="#2E7CF6"):
        super().__init__(parent)
        self.setObjectName("capToolbar")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._color = color
        col = QVBoxLayout(self)
        col.setContentsMargins(6, 4, 6, 4)
        col.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(2)
        self._tool_buttons = {}
        seq = ["rect", "line", "arrow", "ellipse", "pen", "marker", "mosaic",
               "text", "serial", "eraser", "sep",
               "save", "close", "longshot", "gif", "pin", "copy"]
        for name in seq:
            if name == "sep":
                line = QWidget()
                line.setFixedSize(1, 20)
                line.setStyleSheet("background:#E5E6EB;")
                row.addWidget(line)
                continue
            b = QToolButton()
            b.setFixedSize(30, 30)
            b.setIconSize(QSize(20, 20))
            b.setCursor(Qt.PointingHandCursor)
            tips = {"rect": "矩形", "line": "直线", "arrow": "箭头", "ellipse": "椭圆",
                    "pen": "画笔", "marker": "马克笔", "mosaic": "马赛克", "text": "文字",
                    "serial": "标记序号（单击放置，序号自动递增）",
                    "eraser": "橡皮擦", "save": "保存", "close": "关闭 (Esc)",
                    "longshot": "长截图（滚动拼接）", "gif": "GIF 录制（选区固定，10fps）",
                    "pin": "贴图 (Ctrl+D)", "copy": "截图并复制 (Enter)"}
            b.setToolTip(tips.get(name, name))
            b.setIcon(icon(name, "#4E5969", 20, 1.6))
            b.setProperty("tool", name)
            b.clicked.connect(lambda _=False, n=name: self.action.emit(n))
            row.addWidget(b)
            self._tool_buttons[name] = b
        col.addLayout(row)

        # 第二栏：色板（点绘图工具时展开）
        self.color_row = QWidget()
        crow = QHBoxLayout(self.color_row)
        crow.setContentsMargins(2, 0, 2, 0)
        crow.setSpacing(6)
        self._dots = {}
        for c in _COLORS[:8]:
            d = QToolButton()
            d.setFixedSize(16, 16)
            d.setCursor(Qt.PointingHandCursor)
            d.setToolTip("画笔颜色")
            d.clicked.connect(lambda _=False, cc=c: self._pick(cc))
            crow.addWidget(d)
            self._dots[c] = d
        crow.addStretch(1)
        col.addWidget(self.color_row)
        self.color_row.hide()
        self._update_dots()
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(18)
        eff.setOffset(0, 3)
        eff.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(eff)
        self.setStyleSheet(
            "#capToolbar{background:white;border-radius:8px;}"
            "QToolButton{border:none;border-radius:6px;padding:3px;background:white;}"
            "QToolButton:hover{background:#F2F3F5;}"
            "QToolButton:disabled{color:rgba(0,0,0,.35);}")
        self.adjustSize()

    def _pick(self, c):
        self._color = c
        self._update_dots()
        self.action.emit("color:" + c)

    def set_current_color(self, c):
        self._color = c
        self._update_dots()

    def _update_dots(self):
        for c, d in self._dots.items():
            ring = "#1D2129" if c == self._color else "transparent"
            d.setStyleSheet(
                f"QToolButton{{background:{c};border-radius:8px;"
                f"border:2px solid {ring};}}")

    def highlight_tool(self, name):
        for n, b in self._tool_buttons.items():
            b.setStyleSheet("background:#EAF2FF;" if n == name else "")
        self.color_row.setVisible(name in self._TOOLS_WITH_COLOR)
        self.adjustSize()

# ---------------------------------------------------------------- 长截图预览窗
class LongshotPreview(QWidget):
    """长截图右侧实时预览：高度随内容增长，到上限后整体等比缩小（带完成/取消）。"""

    MAX_CONTENT_H = 640          # 预览区最大高度；超过后缩略图等比缩小

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("SnapPin 长截图预览")
        self.on_finish = None
        self.on_cancel = None
        col = QVBoxLayout(self)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(5)
        self.lab = QLabel("在选区内滚动页面，\n开始拼接长图…")
        self.lab.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.lab.setMinimumHeight(40)
        self.lab.setStyleSheet(
            "background:#1E2126;color:#C9CDD4;font-size:11px;padding:2px;")
        col.addWidget(self.lab)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignHCenter)
        self.status.setStyleSheet("color:#C9CDD4;font-size:10px;background:transparent;")
        self.status.hide()
        col.addWidget(self.status)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btn_ok = QPushButton("完成")
        btn_cancel = QPushButton("取消")
        for b in (btn_ok, btn_cancel):
            b.setFixedHeight(28)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
        btn_ok.setToolTip("完成并复制到剪贴板 (Enter)")
        btn_cancel.setToolTip("取消长截图 (Esc)")
        btn_ok.setStyleSheet(
            "QPushButton{background:#2E7CF6;color:#FFFFFF;border:none;"
            "border-radius:7px;font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:#1E66E0;}"
            "QPushButton:pressed{background:#1857C4;}")
        btn_cancel.setStyleSheet(
            "QPushButton{background:#2B3038;color:#C9CDD4;border:1px solid #3A4049;"
            "border-radius:7px;font-size:12px;}"
            "QPushButton:hover{background:#363C46;color:#FFFFFF;}"
            "QPushButton:pressed{background:#242930;}")
        btn_ok.clicked.connect(lambda: self.on_finish and self.on_finish())
        btn_cancel.clicked.connect(lambda: self.on_cancel and self.on_cancel())
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)
        col.addLayout(btns)
        self.setStyleSheet(
            "LongshotPreview{background:#1E2126;border:2px solid #2E7CF6;"
            "border-radius:9px;}")
        self.setFixedWidth(196)
        self._apply_height(60)

    def _apply_height(self, content_h: int):
        """按预览内容高度调整窗口高度，并夹在当前屏幕内。"""
        total = content_h + 62                     # 边距6*2 + 间距5*2 + 状态14 + 按钮行28 + 余量
        scr = self.screen() or QApplication.primaryScreen()
        max_h = scr.availableGeometry().height() - 24 if scr else total
        self.setFixedHeight(max(96, min(total, max_h)))
        # 高度变化后可能超出屏幕底部 → 上移夹回
        g = scr.availableGeometry() if scr else None
        if g and self.y() + self.height() > g.bottom() - 4:
            self.move(self.x(), max(g.top() + 4, g.bottom() - self.height() - 4))

    def set_pix(self, pix: QPixmap):
        """内容越长预览越高，达到上限后整体等比缩小（不再一味变小变窄）。

        预览用 FastTransformation：每 300ms 对不断增长的大图做平滑缩放太卡，
        最终导出仍用原始数据，不受影响。
        """
        if pix.isNull():
            self.lab.setText("在选区内滚动页面，\n开始拼接长图…")
            self.status.hide()
            self._apply_height(60)
            return
        self.status.setText(f"{pix.width()} × {pix.height()} px")
        self.status.show()
        max_h = min(self.MAX_CONTENT_H, 60000)
        scaled = pix.scaled(184, max_h, Qt.KeepAspectRatio, Qt.FastTransformation)
        self.lab.setPixmap(scaled)
        self._apply_height(scaled.height() + 4)


# ---------------------------------------------------------------- GIF 录制状态条
class GifBar(QWidget):
    """GIF 录制状态条：深色卡片 + 红色主按钮，独立顶层窗口。"""

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("SnapPin GIF 录制")
        self.on_stop = None
        self.on_cancel = None
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 8, 8, 8)
        col.setSpacing(6)
        self.status = QLabel("● 准备录制…")
        self.status.setStyleSheet(
            "color:#F53F3F;font:12px 'Microsoft YaHei';background:transparent;")
        col.addWidget(self.status)
        row = QHBoxLayout()
        row.setSpacing(8)
        btn_stop = QPushButton("停止并保存")
        btn_cancel = QPushButton("取消")
        for b in (btn_stop, btn_cancel):
            b.setFixedHeight(28)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
        btn_stop.setStyleSheet(
            "QPushButton{background:#F53F3F;color:#FFFFFF;border:none;"
            "border-radius:7px;font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:#D93030;}"
            "QPushButton:pressed{background:#B92525;}")
        btn_cancel.setStyleSheet(
            "QPushButton{background:#2B3038;color:#C9CDD4;border:1px solid #3A4049;"
            "border-radius:7px;font-size:12px;}"
            "QPushButton:hover{background:#363C46;color:#FFFFFF;}"
            "QPushButton:pressed{background:#242930;}")
        btn_stop.clicked.connect(lambda: self.on_stop and self.on_stop())
        btn_cancel.clicked.connect(lambda: self.on_cancel and self.on_cancel())
        row.addWidget(btn_stop)
        row.addWidget(btn_cancel)
        col.addLayout(row)
        self.setStyleSheet(
            "GifBar{background:#1E2126;border:2px solid #F53F3F;border-radius:9px;}")
        self.setFixedWidth(210)

    def set_status(self, text: str):
        self.status.setText(text)


# ---------------------------------------------------------------- GIF 播放遮罩
class _GifPlayOverlay(QWidget):
    """覆盖在 GIF 上的播放按钮层：点击播放，按住可拖动预览窗。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor)
        self._press = None
        self._win_pos = None
        self._dragging = False

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 70))
        p.setRenderHint(QPainter.Antialiasing)
        c = self.rect().center()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawEllipse(c, 28, 28)
        p.setBrush(QColor("#1E2126"))
        p.drawPolygon(QPolygon([QPoint(c.x() - 8, c.y() - 13),
                                QPoint(c.x() - 8, c.y() + 13),
                                QPoint(c.x() + 13, c.y())]))

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._win_pos = self.window().pos()
            self._dragging = False

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            d = e.globalPosition().toPoint() - self._press
            if abs(d.x()) + abs(d.y()) > 3:
                self._dragging = True
                self.window().move(self._win_pos + d)

    def mouseReleaseEvent(self, e):
        if self._press is not None and not self._dragging:
            self.window()._play()
        self._press = None


# ---------------------------------------------------------------- GIF 结果预览
_GIF_RESULTS = []          # 顶层窗口无 parent，必须持有引用防 GC


class GifResult(QWidget):
    """GIF 结果：钉在桌面，点击画面播放/暂停，保存或复制后关闭。"""

    def __init__(self, path: str, pos: QPoint):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._path = path
        self._press = None
        self._win_pos = None
        self._dragging = False
        self.setWindowTitle("SnapPin GIF 预览")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        self.lab = QLabel()
        self.lab.setAlignment(Qt.AlignCenter)
        self.lab.setStyleSheet("background:#111317;border-radius:6px;")
        self.movie = QMovie(path)
        self.movie.jumpToFrame(0)
        img = self.movie.currentImage()
        w, h = max(1, img.width()), max(1, img.height())
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        avail = scr.availableGeometry()
        scale = min(1.0, max(1, avail.width() - 80) / w, max(1, avail.height() - 120) / h)
        self._disp = QSize(max(1, int(w * scale)), max(1, int(h * scale)))
        self.movie.setScaledSize(self._disp)
        self.lab.setFixedSize(self._disp)
        self.lab.setMovie(self.movie)
        lay.addWidget(self.lab)
        self.overlay = _GifPlayOverlay(self.lab)
        self.overlay.setGeometry(self.lab.rect())
        self.overlay.show()
        self.overlay.raise_()
        tip = QLabel("点击画面播放 / 暂停")
        tip.setAlignment(Qt.AlignHCenter)
        tip.setStyleSheet("color:#8A9099;font-size:10px;background:transparent;")
        lay.addWidget(tip)
        row = QHBoxLayout()
        row.setSpacing(8)
        btn_save = QPushButton("保存")
        btn_copy = QPushButton("复制")
        btn_close = QPushButton("关闭")
        for b in (btn_save, btn_copy, btn_close):
            b.setFixedHeight(28)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
        btn_save.setStyleSheet(
            "QPushButton{background:#F53F3F;color:#FFFFFF;border:none;border-radius:7px;"
            "font-size:12px;font-weight:600;}QPushButton:hover{background:#D93030;}"
            "QPushButton:pressed{background:#B92525;}")
        btn_copy.setStyleSheet(
            "QPushButton{background:#2E7CF6;color:#FFFFFF;border:none;border-radius:7px;"
            "font-size:12px;font-weight:600;}QPushButton:hover{background:#1E66E0;}"
            "QPushButton:pressed{background:#1857C4;}")
        btn_close.setStyleSheet(
            "QPushButton{background:#2B3038;color:#C9CDD4;border:1px solid #3A4049;"
            "border-radius:7px;font-size:12px;}QPushButton:hover{background:#363C46;"
            "color:#FFFFFF;}")
        btn_save.clicked.connect(self._save)
        btn_copy.clicked.connect(self._copy)
        btn_close.clicked.connect(self.close)
        row.addWidget(btn_save)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)
        lay.addLayout(row)
        self.setStyleSheet(
            "GifResult{background:#1E2126;border:2px solid #F53F3F;border-radius:9px;}")
        self.adjustSize()
        x = min(max(avail.left() + 8, pos.x()),
                max(avail.left() + 8, avail.right() - self.width() - 8))
        y = min(max(avail.top() + 8, pos.y()),
                max(avail.top() + 8, avail.bottom() - self.height() - 8))
        self.move(x, y)
        self.movie.start()
        self.movie.stop()            # 停在第 1 帧，点击后开始预览
        self.show()
        _GIF_RESULTS.append(self)

    def _play(self):
        self.movie.start()
        self.overlay.hide()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._win_pos = self.pos()
            self._dragging = False

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            d = e.globalPosition().toPoint() - self._press
            if abs(d.x()) + abs(d.y()) > 3:
                self._dragging = True
                self.move(self._win_pos + d)

    def mouseReleaseEvent(self, e):
        if self._press is not None and not self._dragging:
            if self.movie.state() == QMovie.Running:
                self.movie.setPaused(True)
                self.overlay.show()
                self.overlay.raise_()
            else:
                self._play()
        self._press = None

    def _save(self):
        d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
        os.makedirs(d, exist_ok=True)
        default = os.path.join(d, "GIF_" + time.strftime("%Y-%m-%d_%H%M%S") + ".gif")
        path, _ = QFileDialog.getSaveFileName(self, "保存 GIF", default, "GIF 动图 (*.gif)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".gif"
        try:
            shutil.copyfile(self._path, path)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.close()

    def _copy(self):
        d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
        os.makedirs(d, exist_ok=True)
        auto = os.path.join(d, "GIF_" + time.strftime("%Y-%m-%d_%H%M%S") + ".gif")
        try:
            shutil.copyfile(self._path, auto)
        except Exception as e:
            QMessageBox.warning(self, "复制失败", str(e))
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(auto)])
        md.setText(auto)
        QApplication.clipboard().setMimeData(md)
        self.close()

    def closeEvent(self, ev):
        self.movie.stop()
        self.movie.setFileName("")
        if self in _GIF_RESULTS:
            _GIF_RESULTS.remove(self)
        try:
            os.remove(self._path)
        except OSError:
            pass
        super().closeEvent(ev)


# ---------------------------------------------------------------- 覆盖层
class CaptureOverlay(QWidget):
    sig_pin = Signal(QPixmap, QRect)
    sig_saved = Signal(str)
    sig_cancelled = Signal()

    def __init__(self, screen, cfg, parent=None):
        super().__init__(parent)
        self._screen = screen
        self._cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

        geo = screen.geometry()
        self.setGeometry(geo)
        self._origin = geo.topLeft()
        base = screen.grabWindow(0)
        self._dpr = base.devicePixelRatio() or 1.0
        self._base = base
        # 预暗化底图：拖拽/悬停的每次重绘只剩两次位块拷贝（洞内回贴原屏），
        # 不再逐帧栅格化整屏路径填充
        self._dark = QPixmap(base)
        pd = QPainter(self._dark)
        pd.fillRect(self._dark.rect(), QColor(0, 0, 0, 90))
        pd.end()

        # 热键→覆盖层可见之间只保留抓屏这一步必须同步的操作：
        # 马赛克底图 / 工具栏 / 窗口枚举都改成惰性或显示后补做，消除触发卡顿
        self._mosaic_img = None
        self._toolbar = None

        self._sel = QRect()
        self._selecting = False
        self._drag_start = QPoint()
        self._move_mode = None
        self._hover_win = QRect()
        self._win_cache = []
        self._anns = []
        self._redo_stack = []
        self._drawing = None
        self._tool = None
        self._color = cfg.get("capture", {}).get("last_color", _COLORS[0])
        self._serial_no = 0
        self._press_pos = QPoint()
        self._text_edit = None
        self._longshot = False
        self._ls_prev = None
        self._ls_stitched = None
        self._ls_buf = None
        self._ls_len = 0
        self._ls_off = 0          # 长图首行在 _ls_buf 中的偏移（支持头部前插）
        self._ls_view = 0         # 参考帧视口顶部在长图中的行坐标（去重唯一事实）
        self._ls_cap = 0
        self._ls_full = False
        self._ls_ref_gray = None
        self._ls_preview_h = 0
        self._ls_preview_t = 0.0
        self._preview = None
        self._gif = False
        self._gif_frames = []
        self._gif_t0 = 0.0
        self._gif_bar = None
        self._gif_timer = None
        self._gif_start_timer = None
        self._gif_full = False
        self._gif_size = None               # 统一帧尺寸（超内存预算时等比缩小）
        self._gif_cursor = None
        self._gif_result = None
        self._gif_max_frames = 300          # 30s @ 10fps
        self._gif_max_bytes = 400 * 1024 * 1024

        self._tip = QLabel(self)
        self._tip.setStyleSheet(
            "background:rgba(30,33,41,.85);color:white;border-radius:4px;"
            "padding:3px 8px;font-size:12px;")
        self._tip.hide()

        self._hint = QLabel(self)
        self._hint.setStyleSheet(
            "background:rgba(30,33,41,.82);color:white;border-radius:8px;"
            "padding:10px 14px;font-size:13px;")
        self._hint.setText(
            "<b>拖拽</b> 框选区域&nbsp;&nbsp;|&nbsp;&nbsp;<b>单击</b> 选中窗口<br/>"
            "<b>双击空白</b> 完成并复制&nbsp;&nbsp;|&nbsp;&nbsp;<b>Enter</b> 复制&nbsp;&nbsp;"
            "<b>Ctrl+D</b> 贴图&nbsp;&nbsp;<b>Esc</b> 取消")
        self._hint.adjustSize()
        if cfg.get("capture", {}).get("show_hint", True):
            self._hint.move(24, self.height() - self._hint.height() - 24)
        else:
            self._hint.hide()

        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._on_esc)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._on_enter)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=lambda: self._finish("pin"))
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=lambda: self._on_tool_action("undo"))

    # ------------------------------------------------ 生命周期
    @property
    def mosaic_img(self):
        """马赛克笔刷底图，首次使用马赛克工具时才生成（整屏两次缩放不便宜）。"""
        if self._mosaic_img is None:
            base = self._base
            small = base.scaled(max(1, base.width() // 14), max(1, base.height() // 14),
                                Qt.IgnoreAspectRatio, Qt.FastTransformation)
            self._mosaic_img = small.scaled(base.width(), base.height(),
                                            Qt.IgnoreAspectRatio, Qt.FastTransformation)
        return self._mosaic_img

    @property
    def toolbar(self):
        """工具栏首次用到（选中区域/切工具）时才创建：17 个 SVG 图标渲染不便宜。"""
        if self._toolbar is None:
            self._toolbar = CaptureToolbar(self, color=self._color)
            self._toolbar.action.connect(self._on_tool_action)
            self._toolbar.hide()
        return self._toolbar

    def start(self):
        self.show()
        self.activateWindow()
        # 窗口枚举/悬停吸附延后一轮事件循环：覆盖层先画出来，热键响应零顿挫
        QTimer.singleShot(0, self._after_show)

    def _after_show(self):
        self._win_cache = self._enum_windows()
        self._detect_hover(QCursor.pos())

    def _on_esc(self):
        if getattr(self, "_gif", False):
            self._stop_gif(save=False)
            return
        if getattr(self, "_longshot", False):
            self._exit_longshot()
            return
        self._cancel()

    def _on_enter(self):
        if self._text_edit is not None:
            self._commit_text()
            return
        if getattr(self, "_gif", False):
            self._stop_gif(save=True)
            return
        if getattr(self, "_longshot", False):
            self._finish_longshot()
            return
        self._finish("copy")

    def _cancel(self):
        self._exit_gif()
        self._exit_longshot()
        self.sig_cancelled.emit()
        self.close()

    # ------------------------------------------------ 窗口吸附
    def _enum_windows(self):
        """截图开始时枚举可见顶层窗口（Z 序，排除自身进程），用于吸附命中测试。"""
        import os
        result = []
        pid_self = os.getpid()
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == pid_self:
                    return True
                # 跳过鼠标穿透的悬浮窗（GameViewer 等全屏透明层）
                ex = user32.GetWindowLongPtrW(hwnd, -20)
                if ex & 0x20:  # WS_EX_TRANSPARENT
                    return True
                cloaked = ctypes.c_int(0)
                dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
                if cloaked.value:
                    return True
                rect = wintypes.RECT()
                if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect),
                                                ctypes.sizeof(rect)) != 0:
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                r = QRect(rect.left, rect.top, rect.right - rect.left,
                          rect.bottom - rect.top)
                r.translate(-self._origin)
                if r.width() >= 8 and r.height() >= 8:
                    result.append((r, hwnd))
            except Exception:
                pass
            return True

        user32.EnumWindows(proto(cb), 0)
        return result

    def _window_at(self, global_pos):
        if not self._cfg.get("capture", {}).get("ui_detect", True):
            return QRect()
        p = global_pos - self._origin
        for entry in getattr(self, "_win_cache", []):
            if entry[0].contains(p):
                return entry[0]
        return QRect()

    def _window_hwnd_at(self, global_pos):
        """光标处应吸顶的目标窗口 hwnd（已排除自身与穿透层）。"""
        p = global_pos - self._origin
        for entry in getattr(self, "_win_cache", []):
            if entry[0].contains(p):
                return entry[1]
        return None

    # ------------------------------------------------ 鼠标
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.pos()
        self._press_pos = pos
        if self._longshot or getattr(self, "_gif", False):
            return
        # 标注工具进行中
        if not self._sel.isNull() and self._sel.contains(pos):
            if self._longshot:
                return
            if self._tool in (None, "text"):
                idx = self._text_ann_at(pos)
                if idx is not None:
                    ann = self._anns[idx]
                    origin = ann.get("rect", QRect(pos, pos)).topLeft()
                    self._begin_text(origin, edit_index=idx, initial=ann.get("text", ""))
                    return
                if self._tool == "text":
                    self._begin_text(pos)
                    return
            if self._tool in _DRAW_TOOLS:
                self._drawing = {"tool": self._tool, "pts": [pos], "color": self._color}
                return
            if self._tool == "text":
                self._begin_text(pos)
                return
            if self._tool == "serial":
                self._place_serial(pos)
                return
            if self._tool == "eraser":
                self._erase_at(pos)
                return
            # 移动/缩放选区
            self._move_mode = self._anchor_at(pos) or "move"
            return
        # 选区外：重新框选（保留已选工具）
        self._selecting = True
        self._drag_start = pos
        self._sel = QRect(pos, pos)
        if self._toolbar is not None:
            self._toolbar.hide()
        self.update()

    def mouseDoubleClickEvent(self, e):
        import os
        if os.environ.get('SNAPPIN_DEBUG'):
            print('DBLCLICK event, button=', e.button(), 'pos=', e.pos(), flush=True)
        if getattr(self, "_longshot", False):
            return  # 长截图进行中双击会误关覆盖层，孤儿预览窗再也无法关闭
        if e.button() != Qt.LeftButton or self._sel.isNull():
            return
        # PixPin 行为：选区内双击=确认复制；选区外双击=完成并复制
        inside = self._sel.contains(e.pos())
        if inside and self._tool is not None:
            return  # 标注工具状态下双击不结束
        self._finish("copy")

    def mouseMoveEvent(self, e):
        pos = e.pos()
        if self._selecting:
            self._sel = QRect(QPoint(min(self._drag_start.x(), pos.x()),
                                     min(self._drag_start.y(), pos.y())),
                              QPoint(max(self._drag_start.x(), pos.x()),
                                     max(self._drag_start.y(), pos.y()))).normalized()
            self._update_tip(self._sel)
            self.update()
            return
        if self._drawing is not None:
            self._drawing["pts"].append(pos)
            self.update()
            return
        if self._tool == "eraser" and e.buttons() & Qt.LeftButton \
                and not self._sel.isNull() and self._sel.contains(pos):
            self._erase_at(pos)
            return
        if self._move_mode and not self._sel.isNull() and e.buttons() & Qt.LeftButton:
            d = pos - self._press_pos
            self._press_pos = pos
            self._apply_move(d)
            return
        if self._sel.isNull():
            self._detect_hover(QCursor.pos())
        else:
            self._update_cursor(self._anchor_at(pos))

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self._selecting:
            self._selecting = False
            if self._sel.width() < 5 or self._sel.height() < 5:
                win = self._window_at(QCursor.pos())
                self._sel = win if not win.isNull() else QRect()
            if not self._sel.isNull():
                self._place_toolbar()
                self._update_tip(self._sel)
            else:
                self._tip.hide()
            self.update()
            return
        if self._drawing is not None:
            ann, self._drawing = self._drawing, None
            if len(ann.get("pts", [])) > 1 or ann["tool"] in ("rect", "ellipse"):
                self._redo_stack.clear()
                self._anns.append(ann)
            self.update()

    # ------------------------------------------------ 选区移动/锚点
    def _anchors(self):
        r, c = self._sel, self._sel.center()
        return {"nw": r.topLeft(), "n": QPoint(c.x(), r.top()), "ne": r.topRight(),
                "w": QPoint(r.left(), c.y()), "e": QPoint(r.right(), c.y()),
                "sw": r.bottomLeft(), "s": QPoint(c.x(), r.bottom()), "se": r.bottomRight()}

    def _anchor_at(self, pos):
        if self._sel.isNull():
            return None
        r, c = self._sel, 14
        if abs(pos.x() - r.left()) <= c and abs(pos.y() - r.top()) <= c:
            return "nw"
        if abs(pos.x() - r.right()) <= c and abs(pos.y() - r.top()) <= c:
            return "ne"
        if abs(pos.x() - r.left()) <= c and abs(pos.y() - r.bottom()) <= c:
            return "sw"
        if abs(pos.x() - r.right()) <= c and abs(pos.y() - r.bottom()) <= c:
            return "se"
        m = 7
        if (pos.x() <= r.left() + m or pos.x() >= r.right() - m) and                 r.top() + c < pos.y() < r.bottom() - c:
            return "w" if pos.x() <= r.left() + m else "e"
        if (pos.y() <= r.top() + m or pos.y() >= r.bottom() - m) and                 r.left() + c < pos.x() < r.right() - c:
            return "n" if pos.y() <= r.top() + m else "s"
        if r.contains(pos):
            return "move"
        return None

    def _apply_move(self, d):
        mode = self._move_mode
        if mode == "move":
            self._sel.translate(d)
        else:
            r = QRect(self._sel)
            if "w" in mode:
                r.setLeft(min(r.left() + d.x(), r.right() - 8))
            if "e" in mode:
                r.setRight(max(r.right() + d.x(), r.left() + 8))
            if "n" in mode:
                r.setTop(min(r.top() + d.y(), r.bottom() - 8))
            if "s" in mode:
                r.setBottom(max(r.bottom() + d.y(), r.top() + 8))
            self._sel = r.normalized()
        self._place_toolbar()
        self._update_tip(self._sel)
        self.update()

    def _update_cursor(self, anchor):
        shapes = {"nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
                  "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
                  "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
                  "w": Qt.SizeHorCursor, "e": Qt.SizeHorCursor}
        if anchor is None:
            self.setCursor(Qt.ArrowCursor if self._tool else Qt.CrossCursor)
        else:
            self.setCursor(shapes.get(anchor, Qt.ArrowCursor))

    def _detect_hover(self, global_pos):
        r = self._window_at(global_pos)
        if r != self._hover_win:
            self._hover_win = r
            self.update()
        if not r.isNull() and self._sel.isNull():
            self._update_tip(r, at_cursor=True)
        elif self._sel.isNull():
            self._tip.hide()

    def _update_tip(self, rect, at_cursor=False):
        if rect.isNull():
            self._tip.hide()
            return
        self._tip.setText(f"{rect.x() + self._origin.x()}, {rect.y() + self._origin.y()}  "
                          f"{rect.width()} × {rect.height()}")
        self._tip.adjustSize()
        if at_cursor:
            gp = QCursor.pos() - self._origin
            self._tip.move(gp.x() + 16, gp.y() + 18)
        else:
            x = rect.x()
            y = rect.y() - self._tip.height() - 6
            if y < 0:
                y = rect.y() + 6
            self._tip.move(max(0, min(x, self.width() - self._tip.width())), y)
        self._tip.show()

    def _place_toolbar(self):
        if self._sel.isNull():
            if self._toolbar is not None:
                self._toolbar.hide()
            return
        tb = self.toolbar
        tb.adjustSize()
        x = self._sel.right() - tb.width()
        y = self._sel.bottom() + 8
        if y + tb.height() > self.height() - 4:
            y = self._sel.y() - tb.height() - 8
        x = max(4, min(x, self.width() - tb.width() - 4))
        y = max(4, min(y, self.height() - tb.height() - 4))
        tb.move(x, y)
        tb.show()
        tb.raise_()
        import os
        if os.environ.get('SNAPPIN_DEBUG'):
            btns = [f"{b.property('tool')}:{b.x() + tb.x()},{b.y() + tb.y()}"
                    for b in tb.findChildren(QToolButton) if b.property('tool')]
            print('TOOLBAR', x, y, tb.width(), tb.height(), ' '.join(btns), flush=True)

    # ------------------------------------------------ 工具动作
    def _on_tool_action(self, name):
        if getattr(self, "_gif", False):
            if name == "gif":
                self._stop_gif(save=True)  # 录制中再点 GIF：停止并保存
            elif name == "close":
                self._stop_gif(save=False)
            return
        if getattr(self, "_longshot", False):
            if name == "save":
                self._save_longshot()      # 长截图另存为：选位置 → 保存 → 关闭
            elif name == "pin":
                self._pin_longshot()       # 长图直接钉到桌面（自动缩放适配屏幕）
            elif name == "close":
                self._cancel()
            return
        if name.startswith("color:"):
            self._color = name.split(":", 1)[1]
            self._cfg.setdefault("capture", {})["last_color"] = self._color
            import config
            config.save(self._cfg)
            # 刚画完的最后一个标注立即换色（画完框 → 点颜色 → 当次生效）
            if self._anns and "color" in self._anns[-1]:
                self._anns[-1]["color"] = self._color
                self.update()
            return
        if name in ("rect", "line", "arrow", "ellipse", "pen", "marker", "mosaic",
                    "text", "serial", "eraser"):
            self._tool = name
            self.toolbar.highlight_tool(name)
            self._place_toolbar()
            self.setCursor(Qt.PointingHandCursor if name == "eraser" else Qt.CrossCursor)
            return
        if name == "undo":
            if self._anns:
                self._redo_stack.append(self._anns.pop())
                self.update()
            return
        if name == "redo":
            if self._redo_stack:
                self._anns.append(self._redo_stack.pop())
                self.update()
            return
        if name == "pin":
            self._finish("pin")
        elif name == "copy":
            self._finish("copy")
        elif name == "save":
            self._finish("save")
        elif name == "close":
            self._cancel()
        elif name == "longshot":
            self._enter_longshot()
        elif name == "gif":
            self._enter_gif()

    # ------------------------------------------------ 标注
    def _begin_text(self, pos, edit_index=None, initial=""):
        if self._text_edit is not None:
            self._commit_text()
        ed = QTextEdit(self)
        ed.setFixedSize(240, 34)
        ed.setFont(QFont("Microsoft YaHei", 14))
        ed.setLineWrapMode(QTextEdit.NoWrap)
        ed.setStyleSheet(
            f"QTextEdit{{background:rgba(255,255,255,.94);color:{self._color};"
            f"border:1.5px dashed {ACCENT};border-radius:4px;padding:3px 6px;}}")
        if edit_index is not None and self._anns[edit_index].get("rect"):
            ed.move(self._anns[edit_index]["rect"].topLeft())
        else:
            ed.move(pos)
        if initial:
            ed.setPlainText(initial)
            cursor = ed.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            ed.setTextCursor(cursor)
        ed.show()
        ed.setFocus()
        self._text_edit = (ed, pos, edit_index)
        ed.focusOutEvent = lambda ev: self._commit_text()
        ed.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if self._text_edit and obj is self._text_edit[0] and \
                ev.type() == ev.Type.KeyPress and ev.key() in (Qt.Key_Escape,):
            self._text_edit[0].deleteLater()
            self._text_edit = None
            return True
        return super().eventFilter(obj, ev)

    def _commit_text(self):
        if not self._text_edit:
            return
        ed, pos, idx = self._text_edit
        text = ed.toPlainText().strip()
        rect = QRect(ed.geometry())
        ed.deleteLater()
        self._text_edit = None
        if not text:
            return
        ann = {"tool": "text", "pts": [rect.topLeft()], "text": text,
               "color": self._color, "rect": rect}
        if idx is None:
            self._anns.append(ann)
        else:
            self._anns[idx] = ann
        self._redo_stack.clear()
        self.update()

    def _text_ann_at(self, pos):
        for i in range(len(self._anns) - 1, -1, -1):
            ann = self._anns[i]
            if ann["tool"] == "text" and self._ann_bbox(ann).contains(pos):
                return i
        return None

    def _place_serial(self, pos):
        """放置序号标记：底色=当前色板色，文字白色，序号自动递增。"""
        self._serial_no += 1
        self._anns.append({"tool": "serial", "pts": [pos],
                           "num": self._serial_no, "color": self._color})
        self._redo_stack.clear()
        self.update()

    def _erase_at(self, pos):
        for i in range(len(self._anns) - 1, -1, -1):
            if self._ann_bbox(self._anns[i]).contains(pos):
                self._redo_stack.clear()
                self._anns.pop(i)
                self.update()
                return

    @staticmethod
    def _ann_bbox(ann):
        if ann.get("rect"):
            return QRect(ann["rect"])
        pts = ann.get("pts", [])
        if not pts:
            return QRect()
        r = QRect(pts[0], pts[0])
        for pt in pts[1:]:
            r = r.united(QRect(pt, pt))
        return r.adjusted(-14, -14, 14, 14)

    def _paint_annotations(self, p: QPainter):
        for ann in self._anns:
            self._draw_ann(p, ann)
        if self._drawing:
            self._draw_ann(p, self._drawing)

    def _draw_ann(self, p: QPainter, ann):
        tool = ann["tool"]
        pts = ann.get("pts", [])
        color = QColor(ann.get("color", self._color))
        p.setRenderHint(QPainter.Antialiasing, True)
        if tool in ("pen", "marker", "mosaic", "line") and len(pts) >= 2:
            pen = QPen()
            if tool == "pen":
                pen = QPen(color, 3)
            elif tool == "marker":
                c = QColor(color)
                c.setAlpha(110)
                pen = QPen(c, 14)
            elif tool == "mosaic":
                pen = QPen(QBrush(self.mosaic_img), 18)
            else:
                pen = QPen(color, 3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolyline(pts)
            if tool == "line":
                return
        if tool in ("pen", "marker", "mosaic"):
            return
        if tool == "rect":
            p.setPen(QPen(color, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRect(pts[0], pts[-1]).normalized())
        elif tool == "ellipse":
            p.setPen(QPen(color, 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRect(pts[0], pts[-1]).normalized())
        elif tool == "arrow" and len(pts) >= 2:
            p.setPen(QPen(color, 3))
            p.drawPolyline(pts)
            a, b = pts[0], pts[-1]
            ang = math.atan2(b.y() - a.y(), b.x() - a.x())
            L = 14
            for da in (math.pi * 5 / 6, -math.pi * 5 / 6):
                p.drawLine(b, QPoint(int(b.x() + L * math.cos(ang + da)),
                                     int(b.y() + L * math.sin(ang + da))))
        elif tool == "text":
            f = QFont("Microsoft YaHei", 14)
            p.setFont(f)
            p.setPen(QPen(color, 1))
            r = ann.get("rect")
            if r:
                p.drawText(QRectF(r.adjusted(2, 2, -2, -2)),
                           Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, ann.get("text", ""))
            else:
                p.drawText(QRect(pts[0], QSize(300, 300)), Qt.TextWordWrap, ann.get("text", ""))
        elif tool == "serial":
            f = QFont("Microsoft YaHei", 11, QFont.Bold)
            p.setFont(f)
            c = pts[0]
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(ann.get("color", ACCENT))))
            p.drawEllipse(c, 11, 11)
            p.setPen(QPen(Qt.white, 1))
            p.drawText(QRect(c - QPoint(11, 11), QSize(22, 22)), Qt.AlignCenter,
                       str(ann.get("num", 1)))

    # ------------------------------------------------ GIF 录制（选区固定，10fps）
    def _enter_gif(self):
        if self._sel.isNull() or self._longshot:
            return
        self._gif = True
        self._tool = None
        self.toolbar.highlight_tool("gif")
        self._gif_frames = []
        self._gif_full = False
        self._gif_size = None
        self._gif_t0 = time.monotonic()
        self._make_gif_cursor()
        self._make_gif_bar()
        # 录制期间背景解锁：只留选区边框和工具栏，选区外实时桌面
        ring = QRegion(self._sel.adjusted(-4, -4, 4, 4)) - QRegion(self._sel)
        ring += QRegion(self.toolbar.geometry())
        self.setMask(ring)
        if self._gif_timer is None:
            self._gif_timer = QTimer(self, interval=100)
            self._gif_timer.timeout.connect(self._gif_tick)
        if self._gif_start_timer is None:
            self._gif_start_timer = QTimer(self)
            self._gif_start_timer.setSingleShot(True)
            self._gif_start_timer.timeout.connect(self._gif_begin)
        self._gif_start_timer.start(100)      # 等镂空上屏，避免抓到自己
        self.activateWindow()
        self.setFocus()

    def _make_gif_bar(self):
        self._gif_bar = GifBar()
        r = self._sel.translated(self._origin)
        geo = self._screen.geometry()
        x = r.right() + 16
        if x + 220 > geo.right():
            x = r.left() - 230
        self._gif_bar.move(max(4, x), max(4, r.top()))
        self._gif_bar.on_stop = lambda: self._stop_gif(save=True)
        self._gif_bar.on_cancel = lambda: self._stop_gif(save=False)
        self._gif_bar.set_status("● 准备录制…")
        self._gif_bar.show()
        self._place_toolbar()

    def _gif_begin(self):
        if not self._gif:
            return
        self._gif_t0 = time.monotonic()
        self._gif_timer.start()
        self._gif_tick()

    def _make_gif_cursor(self):
        """缓存一个带白边的黑色箭头，录制时叠加到鼠标位置。"""
        pm = QPixmap(26, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.moveTo(1, 1)
        path.lineTo(1, 23)
        path.lineTo(6, 18)
        path.lineTo(10, 28)
        path.lineTo(14, 26)
        path.lineTo(10, 16)
        path.lineTo(18, 16)
        path.closeSubpath()
        p.setPen(QPen(QColor("#FFFFFF"), 2))
        p.setBrush(QColor("#000000"))
        p.drawPath(path)
        p.end()
        self._gif_cursor = pm

    def _draw_gif_cursor(self, frame, x, y):
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, w * 3, QImage.Format_RGB888)
        p = QPainter(img)
        p.drawPixmap(x - 1, y - 1, self._gif_cursor)
        p.end()

    def _prepare_gif_frame(self, cur):
        """裁掉选区边缘 2px（防边框抗锯齿渗色），并按 30s 内存预算缩放。"""
        if cur.shape[0] > 6 and cur.shape[1] > 6:
            cur = cur[2:-2, 2:-2]
        h, w = cur.shape[:2]
        if self._gif_size is None:
            budget = self._gif_max_bytes / (self._gif_max_frames * 3)
            scale = min(1.0, (budget / max(1, h * w)) ** 0.5)
            self._gif_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        if (w, h) != self._gif_size:
            from PIL import Image
            cur = np.asarray(Image.fromarray(cur).resize(self._gif_size, Image.BILINEAR))
        return np.ascontiguousarray(cur)

    def _gif_tick(self):
        if not self._gif or self._gif_full:
            return
        cur, _ = self._grab_region()
        orig_h, orig_w = cur.shape[:2]
        cur = self._prepare_gif_frame(cur)
        pos = QCursor.pos()
        g = self._sel.translated(self._origin)
        mx = int((pos.x() - g.x()) * self._dpr) - 2
        my = int((pos.y() - g.y()) * self._dpr) - 2
        if self._gif_size:
            h0, w0 = max(1, orig_h - 4), max(1, orig_w - 4)
            mx = int(mx * self._gif_size[0] / w0)
            my = int(my * self._gif_size[1] / h0)
        if 0 <= mx < cur.shape[1] and 0 <= my < cur.shape[0]:
            self._draw_gif_cursor(cur, mx, my)
        h, w = cur.shape[:2]
        n = len(self._gif_frames)
        if n >= self._gif_max_frames or (n + 1) * h * w * 3 > self._gif_max_bytes:
            self._gif_full = True
            self._gif_timer.stop()
            if self._gif_bar:
                self._gif_bar.set_status(f"● 已到上限 · {n} 帧，点停止保存")
            return
        self._gif_frames.append(cur)
        if self._gif_bar:
            el = int(time.monotonic() - self._gif_t0)
            self._gif_bar.set_status(f"● 录制中 {el // 60:02d}:{el % 60:02d} · {n + 1} 帧")

    @staticmethod
    def _encode_gif(frames, path):
        """Pillow 编码：10fps、无限循环。从全片采样构建统一 256 色调色板，
        避免逐帧独立量化造成的闪烁，也避免只取首帧导致后续偏色。"""
        from PIL import Image
        step = max(1, len(frames) // 12)
        sample = frames[::step][:12]
        tw, th = 80, 45
        cols = min(4, len(sample))
        rows = max(1, math.ceil(len(sample) / cols))
        canvas = Image.new("RGB", (tw * cols, th * rows), (0, 0, 0))
        for i, f in enumerate(sample):
            canvas.paste(Image.fromarray(f).resize((tw, th)),
                         ((i % cols) * tw, (i // cols) * th))
        base = canvas.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        imgs = [Image.fromarray(f).quantize(
                    palette=base, dither=Image.Dither.FLOYDSTEINBERG)
                for f in frames]
        imgs[0].save(path, save_all=True, append_images=imgs[1:],
                     duration=100, loop=0, optimize=True)

    def _stop_gif(self, save=True):
        if not getattr(self, "_gif", False):
            return
        if self._gif_timer:
            self._gif_timer.stop()
        if self._gif_start_timer:
            self._gif_start_timer.stop()
        frames = self._gif_frames
        if not save or not frames:
            self._exit_gif()
            return
        # 先生成到临时文件，再钉到桌面预览；保存/复制由预览窗处理
        temp_dir = os.path.join(tempfile.gettempdir(), "SnapPin")
        os.makedirs(temp_dir, exist_ok=True)
        path = os.path.join(temp_dir, "gif_" + time.strftime("%Y%m%d_%H%M%S") + ".gif")
        if self._gif_bar:
            self._gif_bar.set_status("● 正在生成 GIF…")
            QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._encode_gif(frames, path)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "GIF 生成失败", str(e))
            self._exit_gif()
            return
        QApplication.restoreOverrideCursor()
        glob = self._sel.translated(self._origin)
        self._exit_gif()
        self.close()
        try:
            self._gif_result = GifResult(path, glob.topLeft())
        except Exception as e:
            QMessageBox.warning(None, "GIF 预览失败", str(e))

    def _exit_gif(self):
        if getattr(self, "_gif", False):
            self._gif = False
            if self._gif_timer:
                self._gif_timer.stop()
            if self._gif_start_timer:
                self._gif_start_timer.stop()
            self.clearMask()
            if self._gif_bar:
                self._gif_bar.on_stop = None
                self._gif_bar.on_cancel = None
                self._gif_bar.close()
                self._gif_bar = None
            self._gif_frames = []
            self._gif_full = False
            self._gif_size = None

    # ------------------------------------------------ 长截图（透明洞 + 定时抓屏拼接）
    def _enter_longshot(self):
        if self._sel.isNull():
            return
        self._longshot = True
        self._tool = None
        self.toolbar.highlight_tool("longshot")
        self._ls_reset_state()
        self._make_preview()
        # 长截图期间整个覆盖层镂空，只留选区边框和工具栏：
        # 选区外也恢复实时桌面（不再顶着进入长截图时的暗化快照），
        # 滚动页面时四周不会出现“背景被钉死”的割裂感；框选本身固定不动。
        ring = QRegion(self._sel.adjusted(-4, -4, 4, 4)) - QRegion(self._sel)
        ring += QRegion(self.toolbar.geometry())
        self.setMask(ring)
        if getattr(self, "_ls_timer", None) is None:
            self._ls_timer = QTimer(self, interval=150)   # 快滚时单拍位移更小、易落栅格
            self._ls_timer.timeout.connect(self._ls_step)
        self._ls_timer.start()
        # setMask 是异步窗口系统操作：立即抓首帧会抓到覆盖层自己的暗化画面，
        # 之后所有对齐都拿这张脏参考帧当基准 → 用户实测「完全拼不上」。
        # 延后到镂空真正上屏后的稳定帧再取首帧。用有 parent 的定时器，
        # 覆盖层被 WA_DeleteOnClose 销毁后不会回调到已删除对象。
        if getattr(self, "_ls_start_timer", None) is None:
            self._ls_start_timer = QTimer(self)
            self._ls_start_timer.setSingleShot(True)
            self._ls_start_timer.timeout.connect(self._ls_begin)
        self._ls_start_timer.start(100)
        self.activateWindow()
        self.setFocus()

    def _ls_reset_state(self):
        self._ls_prev = None
        self._ls_prev_gray = None
        self._ls_ref_gray = None
        self._ls_stitched = None
        self._ls_buf = None
        self._ls_len = 0
        self._ls_off = 0
        self._ls_view = 0
        self._ls_cap = 0
        self._ls_full = False
        self._ls_preview_h = 0
        self._ls_preview_t = 0.0

    def _ls_begin(self):
        """首帧抓取：由 _enter_longshot 延迟触发，确保镂空已上屏。"""
        if not self._longshot or self._ls_stitched is not None:
            return
        prev, prev_gray = self._grab_region()
        # 预分配拼接缓冲：逐帧 vstack 全量拷贝是长图越拼越卡的元凶，改为倍增扩容
        w3 = max(1, prev.shape[1] * 3)
        self._ls_cap = max(1000, min(40000, 150 * 1024 * 1024 // w3))  # 彩色按字节量封顶
        H0, W0 = prev.shape[:2]
        self._ls_buf = np.empty((min(self._ls_cap, max(H0 * 2, 256)), W0, 3), np.uint8)
        self._ls_buf[:H0] = prev
        self._ls_off = 0
        self._ls_len = H0
        self._ls_stitched = self._ls_buf[:H0]
        self._ls_prev, self._ls_prev_gray = prev, prev_gray
        self._ls_ref_gray = prev_gray
        self._ls_view = 0
        self._update_preview(force=True)

    def _grab_region(self):
        """只抓选区洞（开销随选区而非全屏）。返回 (彩色RGB, 灰度)：
        拼接/导出用彩色；滚动位移比对用灰度（绿通道近似亮度）。
        np.array 强制拷贝：stride 恰好无 padding 时 ascontiguousarray 会原样
        返回视图，QImage 底层内存随局部 img 销毁，事后读到的是已释放内存。"""
        g = self._sel.translated(self._origin)
        img = self._screen.grabWindow(
            0, g.x(), g.y(), g.width(), g.height()
        ).toImage().convertToFormat(QImage.Format_RGB888)
        h, w = img.height(), img.width()
        arr = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(h, img.bytesPerLine())
        arr = arr[:, :w * 3].reshape(h, w, 3)
        color = np.array(arr)                            # 必须真拷贝（见上）
        return color, np.ascontiguousarray(arr[:, :, 1].astype(np.int16))

    # ---- 滚动对齐：锚定条带定位法 ----
    # 旧「实心一致行」方案在微信等“大面积空白+稀疏文本”场景大面积假阳性：
    # 空白行与空白行天然逐像素一致，任意位移都凑得出实心行数 → 错位拼接即
    # 用户所见重复/拖影；固定件通配在慢滚时近乎全帧通配，判定彻底失效。
    # 现方案：从当前帧顶部找第一块有纹理的条带（空白不可锚定），在参考帧里
    # 精确逐像素定位 → 位移；再验证整个重叠区 ≥75% 行一致才接受。空白区
    # 不参与任何判定，假匹配失去了滋生土壤；上滚对称处理，来回滚零重复。
    _STRIP = 32            # 锚定条带高度
    _STRIP_STD = 8.0       # 条带纹理度门槛（行间标准差，低于视为均匀不可锚定）
    _OV_RATIO = 0.75       # 重叠区一致行占比门槛（容纳悬浮按钮/表情动画）

    @staticmethod
    def _texture_cols(strip, cap=96):
        """锚定条带内自适应挑有纹理的列。固定采中段在「内容只占画面一侧」
        （侧栏/聊天气泡/窄栏文档）时全部落在空白上，整帧永远锚定不上。
        返回列索引（可能为空=条带内找不到可用列）。"""
        sd = strip.std(axis=0)
        edge = max(8, min(strip.shape[1] // 20, 24))   # 右侧滚动条会动，不参与
        if strip.shape[1] > 2 * edge:
            sd = sd.copy()
            sd[:2] = 0.0
            sd[-edge:] = 0.0
        mx = float(sd.max())
        if mx < 1.0:
            return np.zeros(0, dtype=int)
        idx = np.flatnonzero(sd >= max(2.0, mx * 0.2))
        if idx.size > cap:
            idx = idx[np.linspace(0, idx.size - 1, cap).astype(int)]
        return idx

    @staticmethod
    def _pick_strip(gray, r0=0):
        """从 r0 行向下找第一块有纹理的 K 行条带；整片均匀的画面无从锚定。
        返回 (条带起始行, 条带)；找不到返回 None。"""
        K = CaptureOverlay._STRIP
        H = gray.shape[0]
        while r0 + K <= H:
            s = gray[r0:r0 + K]
            if float(s.std()) >= CaptureOverlay._STRIP_STD:
                return r0, s
            r0 += K
        return None

    @staticmethod
    def _locate_block(block, hay, cols, cap=48, tol=2):
        """在 hay 里找与 block 逐像素一致（采样列，±2）的全部位置（最多 cap 个）。
        平滑渐变类内容在 ±2 容差下相邻位置也会命中，必须收集齐全交给重叠
        验证逐个裁决，不能只取首个。"""
        K = block.shape[0]
        if len(cols) == 0:
            return []
        hits = []
        for p0 in range(hay.shape[0] - K + 1):
            if np.abs(hay[p0:p0 + K, cols] - block[:, cols]).max() <= tol:
                hits.append(p0)
                if len(hits) >= cap:
                    break
        return hits

    @staticmethod
    def _overlap_ratio(a, b, dy, cols, tol=2):
        """验证位移 dy：只统计有内容（行内有纹理或与相邻行不同）的行，
        并剔除悬浮固定件在两侧遮挡的行——空白行的“一致”不提供证据，
        固定件的遮挡也不是错位。"""
        H = a.shape[0]
        ov = H - dy
        if ov <= 1 or len(cols) == 0:
            return 0.0
        rows = a[:ov, cols]
        sd = rows.std(axis=1)                                   # 行内纹理
        vd = np.abs(np.diff(rows, axis=0)).max(axis=1)          # 与下一行差异
        textured = np.zeros(ov, dtype=bool)
        textured[:-1] = vd > 1.0
        textured |= sd > 2.0
        diff = np.abs(rows - b[dy:dy + ov, cols]).max(axis=1)
        # 悬浮固定件（吸顶/吸底按钮）在 cur/ref 同位置逐像素相同、却与
        # 各自错开 dy 的自身不同；它不属于滚动内容。cur 侧会直接出现在
        # 重叠区，ref 侧会“顶替”重叠区里本该是内容的行，两侧都必须剔除，
        # 否则几十行按钮就能把正确位移的占比压到门槛以下。
        same_pos = (a == b).all(axis=1)
        if dy < H:
            moved_a = (a[dy:] != a[:H - dy]).any(axis=1)
            moved_b = (b[dy:] != b[:H - dy]).any(axis=1)
            cur_fixed = same_pos.copy()
            cur_fixed[:dy] = False
            cur_fixed[dy:] &= moved_a
            ref_fixed = same_pos.copy()
            ref_fixed[:dy] = False
            ref_fixed[dy:] &= moved_b
        else:
            cur_fixed = ref_fixed = np.zeros(H, dtype=bool)
        bad = cur_fixed[:ov] | ref_fixed[dy:dy + ov]
        textured &= ~bad
        n = int(textured.sum())
        if n < 4:                                               # 内容太少无从取证
            return 0.0
        return float((diff[textured] <= tol).mean())

    @classmethod
    def _down_align(cls, ref, cur, max_shift=1600, min_ratio=None, locate_tol=2):
        """下滚对齐：cur 顶部纹理条带在 ref 中定位，dy=位置差=滚动位移；
        多命中逐个做重叠验证取最优，多条带重试（顶部恰好均匀时向后换带）。
        返回 (dy, 验证占比)；失败 (0, 0)。"""
        H = cur.shape[0]
        if ref.shape[0] != H or H < 80:
            return 0, 0.0
        maxs = min(max_shift, H - 60)          # 至少留 60 行重叠可验证
        best = (0, 0.0)
        r0 = 0
        for _ in range(8):                     # 失败路径限次：顶部固定栏再长也够换带
            picked = cls._pick_strip(cur, r0)
            if picked is None:
                break                          # 整帧再也找不到纹理条带
            r0, strip = picked
            if r0 + cls._STRIP > H - 60:
                break                          # 条带已深入可能的“新增内容”区
            cols = cls._texture_cols(strip)    # 内容只占一侧时也能锚定
            for p0 in cls._locate_block(strip, ref, cols, tol=locate_tol):
                dy = p0 - r0
                if 1 <= dy <= maxs:
                    ratio = cls._overlap_ratio(cur, ref, dy, cols)
                    if ratio > best[1]:
                        best = (dy, ratio)
                        if ratio >= 0.999:     # 整段重叠逐像素一致：无需更优
                            return best
            r0 += cls._STRIP                 # 本条带验证不过：换下一块
        gate = cls._OV_RATIO if min_ratio is None else min_ratio
        return best if best[1] >= gate else (0, 0.0)

    @classmethod
    def _up_align(cls, cur, ref, max_shift=1600, min_ratio=None, locate_tol=2):
        """上滚对齐（对称）：ref 顶部纹理条带在 cur 中定位，δ=位置差=上滚量。
        ref[r]=cur[r+δ]，即 cur 内容在 ref 上方 δ 行 → 视口顶部在长图里
        上移 δ（由 _ls_place 决定裁尾/前插，已捕获行不动）。
        返回 (δ, 验证占比)；失败 (0, 0)。"""
        H = cur.shape[0]
        if ref.shape[0] != H or H < 80:
            return 0, 0.0
        maxs = min(max_shift, H - 60)
        best = (0, 0.0)
        r0 = 0
        for _ in range(8):
            picked = cls._pick_strip(ref, r0)
            if picked is None:
                break
            r0, strip = picked
            if r0 + cls._STRIP > H - 60:
                break
            cols = cls._texture_cols(strip)
            for p0 in cls._locate_block(strip, cur, cols, tol=locate_tol):
                delta = p0 - r0
                if 1 <= delta <= maxs:
                    ratio = cls._overlap_ratio(ref, cur, delta, cols)
                    if ratio > best[1]:
                        best = (delta, ratio)
                        if ratio >= 0.999:
                            return best
            r0 += cls._STRIP
        gate = cls._OV_RATIO if min_ratio is None else min_ratio
        return best if best[1] >= gate else (0, 0.0)

    def _ls_step(self):
        if not self._longshot or self._ls_stitched is None or self._ls_full:
            return
        cur, cur_gray = self._grab_region()   # 选区是透明洞，抓到实时画面
        prev_gray = self._ls_prev_gray
        ref_gray = self._ls_ref_gray
        if prev_gray is None or cur_gray.shape != ref_gray.shape:
            self._ls_prev, self._ls_prev_gray = cur, cur_gray
            return
        # 只认“真正停住”的帧：连续两帧逐像素完全一致（0.05% 采样点容差
        # 留给光标闪烁）。移动中的帧即使条带能对上，视口其他区域可能仍在
        # 平滑滚动/流式渲染，拼进长图就是用户反复看到的横向拖影。宁丢勿糊。
        d_prev = np.abs(cur_gray[::2, ::2] - prev_gray[::2, ::2])
        d_ref = np.abs(cur_gray[::2, ::2] - ref_gray[::2, ::2])
        settled = float((d_prev > 0).mean()) < 0.0005
        moved = float((d_ref > 2).mean()) >= 0.002
        if settled and moved:
            self._ls_align(cur, cur_gray)
        if os.environ.get('SNAPPIN_DEBUG'):
            print('LS_STEP settled=', settled, 'len=', self._ls_len, flush=True)
        self._ls_prev, self._ls_prev_gray = cur, cur_gray

    def _ls_align(self, cur, cur_gray):
        """当前帧 vs 参考帧的双向锚定对齐：向下滚视口顶下移 dy，向上滚上移
        delta。所有写入都以「视口顶部在长图中的坐标」为唯一事实——已捕获
        的行永不覆盖，只在长图头/尾之外扩展，因此上滚、下滚、来回滚动都
        不会重复。两方向都命中时按验证占比仲裁。
        只在沉降帧调用：锚定条带必须逐像素精确命中（tol=0），重叠区再做
        常规验证（±2 容忍悬浮件遮挡）。"""
        ref_gray = self._ls_ref_gray
        gate, ltol = 0.9, 0
        d_s, d_r = self._down_align(ref_gray, cur_gray, min_ratio=gate, locate_tol=ltol)
        u_s, u_r = self._up_align(cur_gray, ref_gray, min_ratio=gate, locate_tol=ltol)
        if d_s >= 1 and (u_s < 1 or d_r >= u_r):   # 平手归向下：不无端改向
            top = self._ls_view + d_s
            shift = d_s
        elif u_s >= 1:
            top = self._ls_view - u_s
            shift = -u_s
        else:
            return
        H = cur_gray.shape[0]
        fixed = np.zeros(H, dtype=bool)
        if abs(shift) >= 16:
            # 位移小于固定件最小高度时，固定件不可能进入本次新行（新增行
            # 只在视口最边缘），不检测也不填充；否则小位移下的纯色块会
            # 被整行误判并抹成拉丝（用户实测拖影）。
            fixed = self._static_fixed(cur_gray, ref_gray, shift)
        if shift > 0:                       # 只消除会进入本次新尾行的固定件
            fixed[:H - shift] = False
        else:                               # 只消除会进入本次新头行的固定件
            fixed[H + shift:] = False
        cur_out = cur
        if fixed.any():
            # 悬浮固定件（回到底部↓/吸顶栏等）钉在视口不动，直接拼会沿长图
            # 重复盖章：填充消除后再写入。已捕获行不动，只处理本帧新内容。
            cur_out = self._fill_static(cur.copy(), fixed)
        self._ls_place(top, cur_out, cur_gray)

    def _ls_place(self, top, cur, cur_gray):
        """把视口内容放到长图坐标 top 处：只补头/尾尚未捕获的行，中间已有
        行一律不改写（去重的唯一出口）。超出 150MB 折算容量后冻结。"""
        H = cur.shape[0]
        want = max(self._ls_len, top + H) - min(0, top)
        if want > self._ls_cap:
            self._ls_full = True
            return
        if top < 0:                           # 向上滚出长图头部：前插新内容
            self._ls_prepend(cur[:-top])
            top = 0
        end = top + H
        if end > self._ls_len:                # 向下滚出长图尾部：追加新内容
            self._ls_append(cur[H - (end - self._ls_len):])
        self._ls_view = top
        self._ls_ref_gray = cur_gray          # 只有定位确认才前进参考帧
        self._update_preview()

    def _ls_prepend(self, rows):
        """长图头部前插（向上滚动的新内容）。头部预留不足时重排并居中，
        保证之后上下两方向都有扩展余量；写入量为 O(新增行)。"""
        n = int(rows.shape[0])
        if n <= 0:
            return
        if self._ls_off < n:
            total = min(self._ls_cap,
                        max((self._ls_len + n) * 2,
                            self._ls_len + n + max(n, 1024)))
            off = min(max(n, total // 4), total - self._ls_len)
            buf = np.empty((total, self._ls_buf.shape[1], 3), np.uint8)
            buf[off:off + self._ls_len] = \
                self._ls_buf[self._ls_off:self._ls_off + self._ls_len]
            self._ls_buf, self._ls_off = buf, off
        self._ls_off -= n
        self._ls_buf[self._ls_off:self._ls_off + n] = rows
        self._ls_len += n
        self._ls_stitched = self._ls_buf[self._ls_off:self._ls_off + self._ls_len]

    def _ls_append(self, band):
        """尾部追加（向下滚动的新内容）。预分配缓冲按需倍增扩容，
        超出 150MB 折算行数后冻结，不再每帧全量拷贝。"""
        if self._ls_stitched is None:
            return
        n = int(band.shape[0])
        if n <= 0:
            return
        if self._ls_len + n > self._ls_cap:
            n = self._ls_cap - self._ls_len
            if n <= 0:
                self._ls_full = True
                return
            band = band[:n]
            self._ls_full = True
        need = self._ls_off + self._ls_len + n
        if need > self._ls_buf.shape[0]:
            total = min(self._ls_cap, max(self._ls_buf.shape[0] * 2, need))
            off = min(self._ls_off, max(0, total - self._ls_len - n))
            buf = np.empty((total, self._ls_buf.shape[1], 3), np.uint8)
            buf[off:off + self._ls_len] = \
                self._ls_buf[self._ls_off:self._ls_off + self._ls_len]
            self._ls_buf, self._ls_off = buf, off
        self._ls_buf[self._ls_off + self._ls_len:
                     self._ls_off + self._ls_len + n] = band
        self._ls_len += n
        self._ls_stitched = self._ls_buf[self._ls_off:self._ls_off + self._ls_len]

    @staticmethod
    def _static_fixed(cur_gray, ref_gray, shift):
        """检测悬浮固定件（回到底部↓/吸顶栏）。判据必须同时满足：
        1) 与参考帧同位置逐像素完全一致——固定件钉在视口；
        2) 与参考帧的滚动对应位置不一致——滚动内容会随位移移动。
        只看 1) 会把滚动中的纯色块（粗体文字/卡片）整行判成固定件，再被
        线性填充抹成竖向条纹，就是用户实测的拖影。固定件又只可能出现在
        吸顶/吸底边缘，中间行一律不参与。占比过大说明整页没动，放弃。
        shift>0 表示当前帧相对参考帧下移，<0 表示上移。"""
        H = cur_gray.shape[0]
        if cur_gray.shape != ref_gray.shape:
            return np.zeros(H, dtype=bool)
        same = (cur_gray[:, ::2] == ref_gray[:, ::2]).mean(axis=1)
        textured = cur_gray.std(axis=1) > 6.0
        edge = max(8, H // 5)
        zone = np.zeros(H, dtype=bool)
        zone[:edge] = True
        zone[H - edge:] = True
        fixed = (same > 0.999) & textured & zone
        ov = H - abs(int(shift))
        if shift > 0 and ov > 8:                  # 下滚：cur[:ov] 对应 ref[shift:]
            fixed[:ov] &= (cur_gray[:ov, ::2] != ref_gray[shift:shift + ov, ::2]
                           ).mean(axis=1) > 0.001
        elif shift < 0 and ov > 8:                # 上滚：cur[-ov:] 对应 ref[:ov]
            fixed[H - ov:] &= (cur_gray[H - ov:, ::2] != ref_gray[:ov, ::2]
                               ).mean(axis=1) > 0.001
        if fixed.any():                           # 固定件是连续元件：去掉孤立单行
            cnt = np.convolve(fixed.astype(np.uint8), np.ones(3, np.uint8), "same")
            fixed &= cnt >= 2
        if fixed.sum() > H * 0.2:                 # 成片“静止”= 页面没动，不是固定件
            return np.zeros(H, dtype=bool)
        return fixed

    @staticmethod
    def _fill_static(img, mask):
        """把 fixed 行用上下最近正常行线性过渡填充（上下文多为纯背景，过渡即还原）。"""
        m = np.convolve(mask.astype(np.uint8), np.ones(5, np.uint8), "same") > 0  # ±2 行膨胀盖住抗锯齿边
        H = img.shape[0]
        if not m.any() or m.sum() > H * 0.25:
            return img                                   # 占比过大必是误判：保守不动
        d = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
        for r0, r1 in zip(np.where(d == 1)[0], np.where(d == -1)[0]):
            if r1 - r0 > H * 0.4:
                continue                                 # 过长的“静止段”可能是误判，不碰
            top = img[r0 - 1] if r0 > 0 else img[r1]
            bot = img[r1] if r1 < H else top
            k = np.linspace(0.0, 1.0, r1 - r0 + 2)[1:-1][:, None, None]
            img[r0:r1] = ((1 - k) * top[None] + k * bot[None]).astype(np.uint8)
        return img

    def _make_preview(self):
        self._preview = LongshotPreview()
        r = self._sel.translated(self._origin)
        geo = self._screen.geometry()
        x = r.right() + 16
        if x + 210 > geo.right():
            x = r.left() - 226
        self._preview.move(max(4, x), max(4, r.top()))
        self._preview.on_finish = self._finish_longshot
        self._preview.on_cancel = self._on_esc
        self._update_preview()
        self._preview.show()
        # 工具栏强制放洞外（选区下方），避免被 mask 镂空
        tb = self.toolbar
        tb.adjustSize()
        tb.move(max(4, min(self._sel.x(), self.width() - tb.width() - 4)),
                min(self._sel.bottom() + 8, self.height() - tb.height() - 4))
        tb.show()

    def _update_preview(self, force=False):
        """节流防抖动：新增 ≥32 行 或 距上次渲染 ≥400ms 才重渲染（全图缩放
        随长图变贵；小幅拼接频繁重渲染会造成预览窗抖动，用户实测反馈）。"""
        if self._preview is None or self._ls_stitched is None:
            return
        now = time.monotonic()
        if not force and self._ls_preview_h and \
                self._ls_len - self._ls_preview_h < 32 and \
                now - self._ls_preview_t < 0.4:
            return
        self._ls_preview_h = self._ls_len
        self._ls_preview_t = now
        c = self._ls_stitched
        h, w = c.shape[:2]
        data = c.tobytes()        # 必须持有引用：QImage 不拥有内存，临时 bytes
        img = QImage(data, w, h, w * 3, QImage.Format_RGB888)
        self._preview.set_pix(QPixmap.fromImage(img))

    def _longshot_pixmap(self) -> QPixmap:
        c = self._ls_stitched
        h, w = c.shape[:2]
        data = c.tobytes()        # 必须持有引用：QImage 不拥有内存
        img = QImage(data, w, h, w * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(img)

    def _finish_longshot(self):
        if self._ls_stitched is None or self._ls_stitched.size == 0:
            self._exit_longshot()
            self.close()
            return
        pix = self._longshot_pixmap()
        QApplication.clipboard().setPixmap(pix)
        try:                                  # 诊断用：保留原始长图 PNG，便于核对
            path = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                                "SnapPin", "last_longshot.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pix.save(path, "PNG")
            print(f"LONGSHOT {pix.width()}x{pix.height()} -> {path}", flush=True)
        except Exception as e:
            print(f"LONGSHOT save failed: {e}", flush=True)
        self._exit_longshot()
        self.close()

    def _pin_longshot(self):
        """把当前长图钉到桌面：PinWindow 会按屏幕大小自动缩放，滚轮可再缩放。"""
        if self._ls_stitched is None or self._ls_stitched.size == 0:
            self._exit_longshot()
            self.close()
            return
        pix = self._longshot_pixmap()
        glob = self._sel.translated(self._origin)
        self._exit_longshot()
        self.sig_pin.emit(pix, glob)
        self.close()

    def _save_longshot(self):
        """另存长截图：用户选位置后写文件，成功即退出并关闭截图。"""
        if self._ls_stitched is None or self._ls_stitched.size == 0:
            self._exit_longshot()
            self.close()
            return
        pix = self._longshot_pixmap()
        d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
        os.makedirs(d, exist_ok=True)
        default = os.path.join(d, "长截图_" + time.strftime("%Y-%m-%d_%H%M%S") + ".png")
        path, _ = QFileDialog.getSaveFileName(
            self, "保存长截图", default, "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
        if not path:
            return                            # 用户取消：留在长截图模式
        if not os.path.splitext(path)[1]:
            path += ".png"
        if not pix.save(path):
            QMessageBox.warning(self, "保存失败", f"无法写入：\n{path}")
            return
        self.sig_saved.emit(path)
        self._exit_longshot()
        self.close()

    def _exit_longshot(self):
        if getattr(self, "_longshot", False):
            self._longshot = False
            if getattr(self, "_ls_timer", None):
                self._ls_timer.stop()
            if getattr(self, "_ls_start_timer", None):
                self._ls_start_timer.stop()
            self.clearMask()
            if self._preview:
                # 先摘回调再关窗：防覆盖层销毁后预览窗按钮仍触发旧槽
                self._preview.on_finish = None
                self._preview.on_cancel = None
                self._preview.close()
                self._preview = None
            # 释放拼接缓冲：长图可达百 MB 级，别等覆盖层销毁才回收
            self._ls_reset_state()

    # ------------------------------------------------ 完成/导出
    def _compose(self) -> QPixmap:
        r = self._sel & QRect(0, 0, self.width(), self.height())
        img = QImage(int(r.width() * self._dpr), int(r.height() * self._dpr),
                     QImage.Format_ARGB32)
        img.setDevicePixelRatio(self._dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        sp = self._base.copy(QRect(int(r.x() * self._dpr), int(r.y() * self._dpr),
                                   int(r.width() * self._dpr), int(r.height() * self._dpr)))
        p.drawPixmap(0, 0, sp)
        p.translate(-r.x(), -r.y())
        p.setClipRect(QRect(r.x(), r.y(), r.width(), r.height()))
        self._paint_annotations(p)
        p.end()
        radius = int(self._cfg.get("capture", {}).get("corner_radius", 0))
        if radius > 0 and img.width() > radius * 2 and img.height() > radius * 2:
            out = QImage(img.size(), QImage.Format_ARGB32)
            out.setDevicePixelRatio(self._dpr)
            out.fill(Qt.transparent)
            po = QPainter(out)
            po.setRenderHint(QPainter.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(0.0, 0.0, float(out.width()), float(out.height()),
                                float(radius), float(radius))
            po.setClipPath(clip)
            po.drawPixmap(0, 0, QPixmap.fromImage(img))
            po.end()
            img = out
        return QPixmap.fromImage(img)

    def _finish(self, action: str):
        try:
            self._finish_impl(action)
        except Exception:
            import traceback
            traceback.print_exc()

    def _finish_impl(self, action: str):
        self._commit_text()
        if self._sel.isNull() or self._sel.width() < 3:
            return
        pix = self._compose()
        glob = self._sel.translated(self._origin)
        if self._cfg.get("capture", {}).get("autosave"):
            import os
            import time
            d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime("%Y-%m-%d_%H%M%S") + ".png")
            pix.save(path, "PNG")
            self.sig_saved.emit(path)
        if action == "copy":
            QApplication.clipboard().setPixmap(pix)
            self.close()
        elif action == "pin":
            self.sig_pin.emit(pix, glob)
            self.close()
        elif action == "save":
            import os
            import time
            d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime("%Y-%m-%d_%H%M%S") + ".png")
            pix.save(path, "PNG")
            self.sig_saved.emit(path)
            self.close()

    def closeEvent(self, ev):
        # 任何关闭路径都要收尾长截图：停定时器、关预览窗——
        # 否则覆盖层(WA_DeleteOnClose)销毁后预览窗成孤儿，按钮回调全指向已删除对象
        self._exit_gif()
        self._exit_longshot()
        if self._toolbar is not None:
            self._toolbar.close()
        super().closeEvent(ev)

    # ------------------------------------------------ OCR
    def _run_ocr(self):
        if self._sel.isNull() or self._ocr is None or self._ocr.busy():
            return
        pix = self._compose()
        self._ocr.done.connect(self._ocr_done, Qt.UniqueConnection)
        self._ocr.failed.connect(self._ocr_failed, Qt.UniqueConnection)
        self._ocr.run(pix)

    def _ocr_unhook(self):
        try:
            self._ocr.done.disconnect(self._ocr_done)
            self._ocr.failed.disconnect(self._ocr_failed)
        except Exception:
            pass

    def _ocr_done(self, items):
        self._ocr_unhook()
        text = "\n".join(it["text"] for it in items)
        self._ocr_panel.view.setPlainText(text if text else "（未识别到文字）")
        self._show_ocr_panel()

    def _ocr_failed(self, msg):
        self._ocr_unhook()
        self._ocr_panel.view.setPlainText(f"OCR 失败：{msg}")
        self._show_ocr_panel()

    def _show_ocr_panel(self):
        self._ocr_panel.show()
        self._ocr_panel.adjustSize()
        self._ocr_panel.setFixedHeight(min(320, self._ocr_panel.sizeHint().height()))
        x = self._sel.x()
        y = self._sel.bottom() + self.toolbar.height() + 16
        if y + self._ocr_panel.height() > self.height():
            y = max(8, self._sel.y() - self._ocr_panel.height() - 16)
        self._ocr_panel.move(max(4, min(x, self.width() - self._ocr_panel.width() - 4)), y)
        self._ocr_panel.raise_()

    # ------------------------------------------------ 绘制
    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._dark)      # 预暗化底图：一次位块拷贝
        if getattr(self, "_longshot", False) or getattr(self, "_gif", False):
            # 边框画成紧贴洞口的实心色带（洞内被 mask 镂空，只有洞外可见）：
            # adjusted(-2)+宽4 → 恰好覆盖 [sel边缘, sel边缘+4]，滚动时始终可见
            color = "#F53F3F" if getattr(self, "_gif", False) else ACCENT
            p.setPen(QPen(QColor(color), 4))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._sel.adjusted(-2, -2, 2, 2))
            return
        hole = self._sel if not self._sel.isNull() else (self._hover_win if self._tool is None else QRect())
        if not hole.isNull():
            # 洞内回贴未暗化原屏（源坐标取位图像素）
            src = QRect(int(hole.x() * self._dpr), int(hole.y() * self._dpr),
                        int(hole.width() * self._dpr), int(hole.height() * self._dpr))
            p.drawPixmap(hole, self._base, src)
        if not self._sel.isNull():
            p.save()
            p.setClipRect(self._sel)
            self._paint_annotations(p)
            p.restore()
            bw = self._cfg.get("capture", {}).get("border_width", 3)
            p.setPen(QPen(QColor(ACCENT), bw))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._sel)
            for pt in self._anchors().values():
                p.setPen(QPen(QColor(ACCENT), 1))
                p.setBrush(QBrush(Qt.white))
                p.drawRect(QRect(pt.x() - 4, pt.y() - 4, 8, 8))
        elif not self._hover_win.isNull():
            p.setPen(QPen(QColor(ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._hover_win)


if __name__ == "__main__":
    # 离屏自测：QT_QPA_PLATFORM=offscreen python capture.py
    # 覆盖懒加载链路：马赛克底图/工具栏按需创建、窗口枚举独立于 show()
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import sys

    import config
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    ov = CaptureOverlay(app.primaryScreen(), config.load())
    assert ov._toolbar is None and ov._mosaic_img is None, "重活应保持惰性"
    assert ov._win_cache == [], "枚举应延后到显示后"

    ov._after_show()                                  # 窗口枚举 + 悬停检测可独立执行
    assert isinstance(ov._win_cache, list)

    ov._sel = QRect(20, 20, 240, 120)
    ov.toolbar.action.emit("rect")                    # 惰性工具栏 + 工具切换
    assert ov._tool == "rect" and ov._toolbar is not None
    ov._place_toolbar()

    img = ov.mosaic_img                               # 惰性马赛克底图
    assert ov._mosaic_img is not None

    ov._anns = [{"tool": "mosaic", "pts": [QPoint(30, 30), QPoint(120, 80)],
                 "color": "#000000"}]
    out = QImage(ov.size(), QImage.Format_ARGB32)
    out.fill(Qt.black)
    ov.render(out)                                    # 触发 paintEvent（预暗化底图+洞内回贴）

    ov._cancel()
    print("capture 自测: 基础链路 OK，继续长截图守卫…")

    # ---- 长截图崩溃链：双击/工具栏动作不得关层；任何 close 都必须收尾预览窗
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    ov2 = CaptureOverlay(app.primaryScreen(), config.load())
    ov2._sel = QRect(40, 40, 300, 200)
    finished = []
    ov2._finish = lambda action: finished.append(action)
    ov2._longshot = True
    dbl = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(10, 10), QPointF(10, 10),
                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    ov2.mouseDoubleClickEvent(dbl)
    ov2._on_tool_action("copy")
    ov2._on_tool_action("rect")
    assert finished == [] and ov2._longshot is True, "长截图进行中双击/复制/切工具应被忽略"

    ov2._enter_longshot()                             # 进入长截图（含预览窗+定时器）
    assert ov2._preview is not None
    # 长截图背景解锁：只留边框+工具栏，其余全部镂空（选区外实时桌面）
    assert not ov2.mask().contains(ov2._sel.center()), "选区内必须镂空（实时滚动）"
    assert not ov2.mask().contains(QPoint(2, 2)), "选区外背景必须镂空（背景解锁）"
    assert ov2.mask().contains(ov2._sel.topLeft() - QPoint(1, 1)), "选区边框必须保留"
    assert ov2.mask().contains(ov2._toolbar.geometry().center()), "工具栏必须保留"
    saved = []
    ov2._save_longshot = lambda: saved.append(1)
    ov2._on_tool_action("save")
    assert saved == [1], "长截图模式保存按钮必须触发另存为"
    pinned = []
    ov2.sig_pin.connect(lambda pix, r: pinned.append((pix.width(), r.width())))
    ov2._ls_stitched = np.zeros((10, 20, 3), np.uint8)
    ov2._on_tool_action("pin")
    assert pinned == [(20, ov2._sel.width())] and ov2._longshot is False, \
        "长截图模式贴图必须把长图钉出并退出长截图"
    ov2.close()                                       # 任意关闭路径 → 必须收尾
    assert ov2._longshot is False and ov2._preview is None, "close 必须关掉孤儿预览窗"
    assert not ov2._ls_timer.isActive(), "长截图定时器必须停止"

    # ---- 长截图必须是彩色：拼接数据 RGB888 进预览与导出（此前只留绿通道=黑白）
    ov3 = CaptureOverlay(app.primaryScreen(), config.load())
    ov3._sel = QRect(40, 40, 300, 200)
    col = np.zeros((20, 40, 3), np.uint8)
    col[:, :, 0] = 220                                # 红色内容
    col[:, :, 2] = 60
    ov3._ls_stitched = col
    ov3._make_preview()                               # 预览窗（不影响拼接数据）
    ov3._update_preview()
    img = ov3._preview.lab.pixmap().toImage()
    pc = img.pixelColor(10, 5)
    assert pc.red() > 150 and pc.blue() < 120, \
        f"长截图应保留颜色，实际 RGB=({pc.red()},{pc.green()},{pc.blue()})"
    ov3._longshot = True                              # 生产路径下导出必处于长截图态
    ov3._finish_longshot()                            # 导出彩色 → 剪贴板（离屏为存根）
    assert ov3._preview is None and ov3._longshot is False

    # ---- 悬浮固定件（回到底部↓/吸顶栏）：滚动中同位静止且有纹理的行才判悬浮件
    H, W = 200, 60
    prev = ((np.arange(H)[:, None] % 251) * np.ones((1, W), np.int16)).astype(np.int16)
    prev[30:36] = 77 + (np.arange(W)[None, :] % 5) * 20    # ↓按钮：静止且行内有纹理
    prev[90:96] = 245                                      # 静止空白行：一致但无纹理
    cur = np.roll(prev, -10, axis=0).copy()
    cur[30:36] = prev[30:36]
    cur[90:96] = 245
    fixed = CaptureOverlay._static_fixed(cur, prev, -10)
    assert fixed[30:36].all() and not fixed[90:96].any() and not fixed[100:110].any(), \
        "应识别按钮行；静止空白行与滚动正文行都不得误判"
    col = np.repeat(cur[:, :, None], 3, axis=2).astype(np.uint8)
    out = CaptureOverlay._fill_static(col.copy(), fixed)
    assert not (out[28:38] == 77).any(), "悬浮按钮应被填充消除"
    assert (out[5] == col[5]).all(), "正常内容行必须保持原样"

    # 平滑滚动动画中间帧：同行内容“看起来接近”但并非逐像素静止，
    # 绝不能整片判成固定件（填充即用户实测的横向拉丝拖影）
    arng = np.random.default_rng(5)
    page = arng.integers(0, 255, (H + 1, W), dtype=np.int16)
    f0 = page[:H]
    f1 = np.rint(0.6 * page[:H] + 0.4 * page[1:H + 1]).astype(np.int16)
    assert not CaptureOverlay._static_fixed(f1, f0, 1).any(), \
        "平滑滚动动画帧不得被判为悬浮固定件"
    assert not CaptureOverlay._static_fixed(f0, f0, 0).any(), \
        "整页静止时固定件掩码必须为空（成片静止=页面没动，不是固定件）"
    # 滚动中的纯色块：同行像素在 ±1px 位移下相同，但滚动对应位置也相同，
    # 属于内容而非固定件；只有块的边缘一行会“同位置不同滚动位”，须被连续段过滤
    blk0 = np.full((H, W), 250, np.int16)
    blk1 = np.full((H, W), 250, np.int16)
    blk0[170:184, 20:120] = 40
    blk1[171:185, 20:120] = 40
    assert not CaptureOverlay._static_fixed(blk1, blk0, 1).any(), \
        "滚动中的纯色块不得被判为悬浮固定件"

    # ---- 锚定条带对齐：慢滚 1px/大步长/动画帧拒绝/悬浮件容忍/大位移量程
    prof = ((np.arange(400, dtype=np.int16)[:, None] * 7 +
             np.arange(90, dtype=np.int16)[None, :] * 3) % 251).astype(np.int16)
    assert CaptureOverlay._down_align(prof, np.roll(prof, -1, axis=0))[0] == 1, \
        "慢速滚动 1px 也应被捕获"
    assert CaptureOverlay._down_align(prof, np.roll(prof, -300, axis=0))[0] == 300, \
        "大步长滚动应在量程内命中"
    blank = np.full((400, 90), 240, np.int16)
    assert CaptureOverlay._down_align(blank, np.roll(blank, -50, axis=0))[0] == 0, \
        "整帧均匀空白无从锚定条带，必须放弃本帧"
    rng = np.random.default_rng(7)
    tex = rng.integers(0, 255, (400, 90), dtype=np.int16)
    mid = (np.roll(tex, -7, axis=0) + np.roll(tex, -8, axis=0)) // 2   # 7.5px 中间态
    assert CaptureOverlay._down_align(tex, mid) == (0, 0.0), \
        "平滑滚动动画中间帧(分数像素错位)锚定不上，必须整帧拒绝——文字重影的根源"
    assert CaptureOverlay._down_align(tex, np.roll(tex, -15, axis=0))[0] == 15, \
        "沉降帧应按真实位移定位拼接"
    cur_fx = np.roll(tex, -20, axis=0).copy()
    cur_fx[375] = 123                                   # 悬浮按钮钉在视口原位
    d_fx, r_fx = CaptureOverlay._down_align(tex, cur_fx)
    assert d_fx == 20 and r_fx >= 0.99, \
        "悬浮件行只占重叠区极小占比，重叠验证按占比容忍"
    assert CaptureOverlay._down_align(tex, np.roll(tex, -339, axis=0))[0] == 339, \
        "快滚大位移应在 1600 量程内命中(重叠 61 行仍可验证)"

    # ---- 仿微信场景回归(用户实测翻车场景)：大面积空白+稀疏文本簇
    # 上滚帧绝不允许被拼成"下滚"(旧实心行方案在空白区大面积假阳性的根因)
    doc = np.full((900, 200), 245, np.int16)
    for r in range(20, 900, 60):
        doc[r:r + 8, 10:180] = rng.integers(30, 220, (8, 170))    # 文本簇
    refw = doc[200:600]                   # 参考帧：视口在 200
    curw = doc[170:570]                   # 上滚 30：视口 170
    assert CaptureOverlay._down_align(refw, curw) == (0, 0.0), \
        "上滚帧在空白背景区凑不出真下滚证据，绝不允许向下假拼接"
    assert CaptureOverlay._up_align(curw, refw)[0] == 30, \
        "上滚 30 应由参考帧顶部条带在当前帧定位命中并裁尾回退"
    curw2 = doc[260:660]                  # 下滚 60：视口 260
    assert CaptureOverlay._down_align(refw, curw2)[0] == 60, \
        "空白背景下滚 60 应由条带精确定位"

    # ---- 预分配拼接缓冲：倍增扩容/首帧保留/追加落位
    ov4 = CaptureOverlay(app.primaryScreen(), config.load())
    ov4._sel = QRect(40, 40, 300, 200)
    ov4._enter_longshot()
    ov4._ls_begin()                                   # 首帧生产路径延迟 100ms，测试同步取
    H0 = ov4._ls_prev.shape[0]
    band = np.full((50, ov4._ls_prev.shape[1], 3), 200, np.uint8)
    for _ in range(6):
        ov4._ls_append(band)
    assert ov4._ls_len == H0 + 300 and ov4._ls_buf.shape[0] >= H0 + 300, \
        "缓冲应按需倍增扩容"
    assert (ov4._ls_stitched[:H0] == ov4._ls_prev).all(), "拼接图首帧必须保留"
    assert (ov4._ls_stitched[-50:] == 200).all(), "追加行必须正确落位"

    # ---- 预览节流防抖动：无新增行不重渲染，新增 ≥32 行才刷新
    ov4._update_preview(force=True)                   # 同步预览水位到当前长度
    called = []
    ov4._preview.set_pix = lambda p: called.append(1)
    ov4._update_preview()
    assert len(called) == 0, "无新增行不应重渲染预览"
    ov4._ls_append(band[:32])
    ov4._update_preview()
    assert len(called) == 1, "新增行达到阈值应刷新预览"
    ov4._exit_longshot()

    # ---- 上限截断：保头部、丢溢出（与旧 vstack 语义一致），满后再拼整体丢弃
    ov5 = CaptureOverlay(app.primaryScreen(), config.load())
    ov5._sel = QRect(40, 40, 300, 200)
    ov5._enter_longshot()
    ov5._ls_begin()
    H0 = ov5._ls_prev.shape[0]
    ov5._ls_cap = H0 + 120
    for _ in range(4):
        ov5._ls_append(band)                          # H0→+50→+100→+120 截断
    assert ov5._ls_len == H0 + 120, "超出 150MB 折算行数应截断"
    assert (ov5._ls_stitched[:H0] == ov5._ls_prev).all(), "截断必须保留头部首帧"
    assert (ov5._ls_stitched[-1] == 200).all()
    ov5._ls_append(band)                              # 已满：继续追加应被丢弃
    assert ov5._ls_len == H0 + 120, "满缓冲继续拼接应被丢弃"
    ov5._exit_longshot()
    assert ov5._ls_stitched is None and not ov5._ls_timer.isActive(), \
        "退出应释放拼接缓冲并停止定时器"

    # ---- 多拍流程（长文档切片构造，np.roll 的环形回绕会伪造反向命中）：
    # 动画帧拒绝不丢内容，沉降后按真实位移补齐；上滚只回退视口不重拼、
    # 下滚回来零重复（已捕获行永不改写/重复追加）
    ov6 = CaptureOverlay(app.primaryScreen(), config.load())
    ov6._sel = QRect(40, 40, 300, 200)
    ov6._enter_longshot()
    ov6._ls_begin()
    H6, W6 = ov6._ls_prev.shape[:2]
    doc = rng.integers(0, 255, (H6 + 3000, W6), dtype=np.int16)
    frame_at = lambda p: doc[p:p + H6]
    ov6._ls_ref_gray = frame_at(0).copy()   # 换假参考帧（缓冲内容与本逻辑无关）
    seq = [
        ("mid", (frame_at(0) + frame_at(40)) // 2),   # 动画中间帧：拒绝
        ("B", frame_at(40)),                          # 沉降：滚 40
        ("C", frame_at(25)),                          # 上滚 15：裁尾
        ("B2", frame_at(40)),                         # 回到 40：重拼同一位置
        ("D", frame_at(70)),                          # 再滚 30
        ("frac", (frame_at(90) * 3 + frame_at(91)) // 4),  # 分数位移帧：拒绝
        ("far", frame_at(70 + H6)),                   # 超量程：等待
        ("far2", frame_at(70 + H6)),                  # 沉降仍超量程：继续等
        ("E", frame_at(110)),                         # 对齐恢复：+40
    ]
    holder = {"frame": seq[0][1]}
    ov6._grab_region = lambda: (np.stack([holder["frame"].astype(np.uint8)] * 3, axis=2),
                                holder["frame"].copy())
    n0 = ov6._ls_len
    for name, frame in seq:
        holder["frame"] = frame
        ov6._ls_step()                 # 滚动中：不拼
        ov6._ls_step()                 # 停住：沉降帧才拼
    assert ov6._ls_len == n0 + 110, f"序列终态应累计 +110, 实际 +{ov6._ls_len - n0}"
    assert (ov6._ls_ref_gray == frame_at(110)).all(), "参考帧应前进到 E"

    # 拼接内容逐段等于文档（防错位/重复）：头 H6 行是真实截屏基座，不校验
    stitched = ov6._ls_stitched[H6:].astype(np.int16)[:, :, 1]
    expect = doc[H6:H6 + (ov6._ls_len - H6)]
    assert (np.abs(stitched - expect).mean(axis=1) <= 2).all(), \
        "拼接区必须逐行等于源文档（零错位零重复）"

    # ---- 上滚回退(用户实测去重缺陷)：上滚 85 只回退视口，下滚 45 回到
    # 已捕获区，长图不缩不重复——长度恒定、内容逐行不变
    settled_len = ov6._ls_len
    for name, frame, want_view in (("U", frame_at(25), 25),
                                   ("D2", frame_at(70), 70),
                                   ("U", frame_at(25), 25),
                                   ("D2", frame_at(70), 70),
                                   ("U", frame_at(25), 25),
                                   ("D2", frame_at(70), 70)):
        holder["frame"] = frame
        ov6._ls_step()                 # 滚动中：上滚不处理（防插值帧污染参考）
        ov6._ls_step()                 # 停住：沉降帧处理回退/前插
        assert ov6._ls_len == settled_len, \
            f"{name} 拍后长图不得缩短/重复, 应为 {settled_len}, 实际 {ov6._ls_len}"
        assert ov6._ls_view == want_view, \
            f"{name} 拍后视口顶部应回到长图行 {want_view}, 实际 {ov6._ls_view}"
    stitched2 = ov6._ls_stitched[H6:].astype(np.int16)[:, :, 1]
    expect2 = doc[H6:H6 + (ov6._ls_len - H6)]
    assert (np.abs(stitched2 - expect2).mean(axis=1) <= 2).all(), \
        "来回滚动后拼接区仍必须逐行等于源文档（零重复）"

    ov6._exit_longshot()
    assert ov6._ls_stitched is None and ov6._ls_ref_gray is None, \
        "退出应同时释放参考帧"

    # ---- 上滚扩展（用户要求）：从文档中间进入，上滚把上方新内容前插到
    # 长图头部，下滚回原位置不重复，再下滚继续追加；双向都按真实文档坐标
    ov7 = CaptureOverlay(app.primaryScreen(), config.load())
    ov7._sel = QRect(40, 40, 300, 200)
    ov7._enter_longshot()
    ov7._ls_begin()
    H7, W7 = ov7._ls_prev.shape[:2]
    doc7 = rng.integers(0, 255, (H7 + 6000, W7), dtype=np.int16)
    at7 = lambda p: doc7[p:p + H7]
    ov7._ls_buf[:H7] = np.repeat(at7(500)[:, :, None], 3, axis=2)  # 长图头 = 文档 500
    ov7._ls_stitched = ov7._ls_buf[:H7]
    ov7._ls_ref_gray = at7(500).copy()
    holder7 = {"frame": at7(500)}
    ov7._grab_region = lambda: (np.stack([holder7["frame"].astype(np.uint8)] * 3, axis=2),
                                holder7["frame"].copy())

    def shot7(p, want_len, want_view):
        holder7["frame"] = at7(p)
        ov7._ls_step()                 # 滚动中
        ov7._ls_step()                 # 停住（上滚只在沉降帧处理）
        assert ov7._ls_len == want_len, f"位置 {p}: 长图应 {want_len} 行, 实际 {ov7._ls_len}"
        assert ov7._ls_view == want_view, f"位置 {p}: 视口应在长图 {want_view}, 实际 {ov7._ls_view}"

    shot7(400, H7 + 100, 0)        # 上滚 100：头部前插未捕获内容
    shot7(360, H7 + 140, 0)        # 再上滚 40：继续前插
    shot7(460, H7 + 140, 100)      # 下滚回已捕获区：不重复追加
    shot7(560, H7 + 200, 200)      # 继续下滚：尾部追加 60
    shot7(660, H7 + 300, 300)      # 再下滚：尾部追加 100
    shot7(560, H7 + 300, 200)      # 来回滚：不缩不重复
    shot7(660, H7 + 300, 300)
    final7 = ov7._ls_stitched.astype(np.int16)[:, :, 1]
    expect7 = doc7[360:360 + ov7._ls_len]
    assert (np.abs(final7 - expect7).mean(axis=1) <= 2).all(), \
        "上下滚扩展后长图必须逐行等于文档（零错位零重复）"
    ov7._exit_longshot()
    # ---- GIF 录制：选区固定 / 背景解锁 / 帧采集 / 取消 / 编码
    ov8 = CaptureOverlay(app.primaryScreen(), config.load())
    ov8._sel = QRect(40, 40, 120, 90)
    ov8._enter_gif()
    assert ov8._gif and ov8._gif_bar is not None, "进入 GIF 录制应创建状态条"
    assert not ov8.mask().contains(ov8._sel.center()), "GIF 录制选区必须镂空"
    assert not ov8.mask().contains(QPoint(2, 2)), "GIF 录制背景必须解锁"
    assert ov8._gif_cursor is not None, "录制应缓存鼠标指针"
    cframe = np.zeros((60, 80, 3), np.uint8)
    ov8._draw_gif_cursor(cframe, 10, 10)
    assert cframe.any(), "鼠标指针应绘制到帧上"
    ov8._grab_region = lambda: (np.full((90, 120, 3), 180, np.uint8),
                                np.zeros((90, 120), np.int16))
    ov8._gif_begin()
    assert len(ov8._gif_frames) == 1, "开始录制应立即抓首帧"
    ov8._gif_tick()
    assert len(ov8._gif_frames) == 2, "定时抓帧应追加"
    ov8._stop_gif(save=False)
    assert not ov8._gif and ov8._gif_bar is None and not ov8._gif_frames, \
        "取消录制应释放状态条与帧"
    # 停止并保存：先生成临时 GIF，再钉出预览窗（点击播放 / 保存 / 复制）
    ov8._gif = True
    ov8._gif_frames = [np.full((90, 120, 3), 100, np.uint8),
                       np.full((90, 120, 3), 200, np.uint8)]
    ov8._stop_gif(save=True)
    assert not ov8._gif and ov8._gif_result is not None, "停止保存应生成预览窗"
    res = ov8._gif_result
    assert res in _GIF_RESULTS, "预览窗必须被持有，否则会被 GC 关闭"
    assert res.overlay.isVisible(), "默认应显示播放遮罩"
    res._play()
    assert not res.overlay.isVisible(), "点击播放后遮罩应消失"
    res.movie.setPaused(True)
    res.overlay.show()
    res.close()
    assert res not in _GIF_RESULTS
    import tempfile
    gif_path = os.path.join(tempfile.gettempdir(), "snappin_gif_selftest.gif")
    ov8._encode_gif([np.full((30, 40, 3), i * 60, np.uint8) for i in range(3)], gif_path)
    from PIL import Image
    with Image.open(gif_path) as im:
        assert im.n_frames == 3, "GIF 应包含 3 帧"
        assert im.size == (40, 30)
    os.remove(gif_path)
    print("capture 自测通过: 惰性马赛克/工具栏/预暗化绘制/长截图守卫收尾/彩色拼接/"
          "悬浮件纹理判定/锚定条带对齐/仿微信回归/上下滚去重/缓冲扩容截断/预览防抖 全部 OK")
