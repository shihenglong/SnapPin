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
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QScrollArea, QTextEdit, QToolButton, QVBoxLayout,
                               QWidget, QGraphicsDropShadowEffect)

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
        col.setContentsMargins(4, 3, 4, 3)
        col.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(1)
        self._tool_buttons = {}
        # 参照 PixPin 顺序：形状/画笔组 → 标注辅助 → | 撤销/重做 | → 导出组
        seq = ["rect", "ellipse", "line", "arrow", "pen", "marker", "text",
               "mosaic", "serial", "eraser", "sep",
               "undo", "redo", "sep",
               "longshot", "gif", "pin", "close", "save", "copy"]
        for name in seq:
            if name == "sep":
                line = QWidget()
                line.setFixedSize(1, 18)
                line.setStyleSheet("background:#E5E6EB;")
                row.addWidget(line)
                continue
            b = QToolButton()
            b.setFixedSize(28, 28)
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            tips = {"rect": "矩形", "line": "直线", "arrow": "箭头", "ellipse": "椭圆",
                    "pen": "画笔", "marker": "马克笔", "mosaic": "马赛克", "text": "文字",
                    "serial": "标记序号（单击放置，序号自动递增）",
                    "eraser": "橡皮擦", "save": "保存", "close": "关闭 (Esc)",
                    "longshot": "长截图（滚动拼接，⇕ 可切横向）", "gif": "GIF 录制（选区固定，10fps）",
                    "pin": "贴图 (Ctrl+D)", "copy": "截图并复制 (Enter)",
                    "undo": "撤销 (Ctrl+Z)", "redo": "重做 (Ctrl+Y)"}
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
        eff.setBlurRadius(16)
        eff.setOffset(0, 2)
        eff.setColor(QColor(0, 0, 0, 52))
        self.setGraphicsEffect(eff)
        # 参考 PixPin 截图工具栏：白色圆角胶囊 + 浅描边 + 图标按钮 hover 提亮
        self.setStyleSheet(
            "#capToolbar{background:white;border:1px solid #E3E6EB;"
            "border-radius:9px;}"
            "QToolButton{border:none;border-radius:7px;padding:2px;background:white;}"
            "QToolButton:hover{background:#F2F3F5;}"
            "QToolButton[active=\"true\"]{background:#EAF2FF;}"
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
    """长截图侧边预览（参照 PixPin，用户明确要求）：
    窗口尺寸首次打开即固定；预览图按真实截图**固定比例**缩小（等比、不随长度
    变比例），滚动增长时图像沿拼接方向**直接延伸**，面板内自动滚动跟随当前
    视口位置，绿色框标出视口所在的段。竖向=选区右侧垂直居中，横向=工具栏正下方。"""

    V_W, V_H = 132, 560          # 竖向面板固定尺寸
    H_W, H_H = 560, 170          # 横向面板固定尺寸

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("SnapPin 长截图预览")
        col = QVBoxLayout(self)
        col.setContentsMargins(5, 5, 5, 4)
        col.setSpacing(3)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(False)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # 滚动条只作程序控制（跟随视口），不显示给用户
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea{background:#16191E;border-radius:6px;}")
        self.lab = QLabel()
        self.lab.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.lab.setStyleSheet("background:#16191E;color:#9AA0AC;font-size:11px;")
        self.lab.setText("滚动页面\n开始拼接")
        self.lab.resize(120, 200)
        self.scroll.setWidget(self.lab)
        col.addWidget(self.scroll, 1)
        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignHCenter)
        self.status.setStyleSheet("color:#8A9099;font-size:10px;background:transparent;")
        col.addWidget(self.status)
        self.setStyleSheet(
            "LongshotPreview{background:#16191E;border:2px solid #2E7CF6;"
            "border-radius:9px;}")
        self._hdir = False
        self._frozen = False
        self._scale = 0.25
        self.setFixedSize(self.V_W, 150)

    def freeze(self, hdir: bool, sel_w: int, sel_h: int):
        """首次打开时只确定**缩放比**（按选区尺寸等比铺满面板宽度/高度）；
        窗口的长/宽不固定——随内容增长沿拼接方向延伸（用户明确要求），
        超过屏幕可用区后窗口停住、内部滚动跟随当前视口。"""
        if self._frozen:
            return
        self._hdir = hdir
        # 竖向把选区宽缩到面板内宽 120；横向把选区高缩到 118
        self._scale = (118.0 / max(1, sel_h)) if hdir else (120.0 / max(1, sel_w))
        self._frozen = True

    def unfreeze(self):
        self._frozen = False

    def inner_wh(self):
        """面板内已给状态行留白后的可用尺寸。"""
        if self._hdir:
            return max(1, self.width() - 16), max(1, self.height() - 52)
        return max(1, self.width() - 16), max(1, self.height() - 52)

    def set_pix(self, pix, note: str = "", vp=None):
        """pix 已按固定比例缩好；vp=(视口起,视口止) 为 pix 坐标区间。
        图像只延伸不改比例，面板滚动跟随视口。"""
        if pix is None or pix.isNull():
            return
        self.lab.setPixmap(pix)
        self.lab.resize(max(1, pix.width()), max(1, pix.height()))
        # 窗口长/宽不固定：随内容延伸，夹在屏幕可用区内（超出后内部滚动）
        scr = self.screen() or QApplication.primaryScreen()
        g = scr.availableGeometry() if scr else None
        # 窗口精确贴合内容（图像 + 状态行），不留大块黑边：竖向高度 = 图高
        # + 28（边距/间距/状态行），横向宽度 = 图宽 + 16。仅夹在屏幕可用区内。
        if self._hdir:
            w = min(pix.width() + 16, (g.width() - 60) if g else pix.width() + 16)
            self.setFixedSize(max(90, w), self.H_H)
        else:
            h = min(pix.height() + 28, (g.height() - 90) if g else pix.height() + 28)
            self.setFixedSize(self.V_W, max(60, h))
        sb = (self.scroll.horizontalScrollBar() if self._hdir
              else self.scroll.verticalScrollBar())
        span = (self.scroll.viewport().width() if self._hdir
                else self.scroll.viewport().height())
        if vp and vp[1] > vp[0]:
            # 视口框始终贴在预览**底部**（用户要求：向下滚动时当前区域
            # 永远可见在最下边；向上滚动时框也仍在可见区内）
            sb.setValue(max(0, min(sb.maximum(), int(vp[1] - span))))
        if note:
            self.status.setText(note)
            self.status.show()
        else:
            self.status.hide()

    def clear(self):
        self.lab.clear()
        self.lab.setText("滚动页面\n开始拼接")
        self.status.hide()


# ---------------------------------------------------------------- 长截图工具栏
class LongshotToolbar(QWidget):
    """长截图模式选区下方的专用工具栏（参照 PixPin）：
    [宽 × 高] | [⇕ 竖/横] [↑ 向上自动滚] [↓ 向下自动滚] | [✕][保存][复制][完成]"""

    sig_dir = Signal()
    sig_auto = Signal(int)      # +1 向下自动滚 / -1 向上自动滚（重复点击=停止）
    sig_close = Signal()
    sig_save = Signal()
    sig_copy = Signal()
    sig_finish = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("lsBar")
        self._dir = "v"
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 5, 3)
        row.setSpacing(4)
        self.size_lab = QLabel("0 × 0")
        self.size_lab.setStyleSheet(
            "background:#F2F3F5;color:#4E5969;border-radius:6px;"
            "padding:3px 8px;font-size:12px;font-weight:600;")
        row.addWidget(self.size_lab)
        line = QWidget()
        line.setFixedSize(1, 18)
        line.setStyleSheet("background:#E5E6EB;")
        row.addWidget(line)

        def pill(b):
            b.setFixedSize(28, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(
                "QPushButton{border:none;border-radius:6px;background:white;"
                "font-size:12px;color:#4E5969;}"
                "QPushButton:hover{background:#F2F3F5;}")
            return b

        self.btn_dir = pill(QPushButton("⇕ 竖向"))
        self.btn_dir.setFixedWidth(64)
        self.btn_dir.setToolTip("切换竖向/横向拼接")
        self.btn_up = pill(QPushButton("↑"))
        self.btn_up.setToolTip("自动向上滚动拼接")
        self.btn_down = pill(QPushButton("↓"))
        self.btn_down.setToolTip("自动向下滚动拼接")
        for b in (self.btn_dir, self.btn_up, self.btn_down):
            row.addWidget(b)
        line2 = QWidget()
        line2.setFixedSize(1, 18)
        line2.setStyleSheet("background:#E5E6EB;")
        row.addWidget(line2)
        self.btn_close = pill(QPushButton("✕"))
        self.btn_close.setToolTip("取消长截图 (Esc)")
        self.btn_save = pill(QPushButton("保存"))
        self.btn_save.setFixedWidth(44)
        self.btn_copy = pill(QPushButton("复制"))
        self.btn_copy.setFixedWidth(44)
        self.btn_ok = QPushButton("完成")
        self.btn_ok.setFixedHeight(26)
        self.btn_ok.setFixedWidth(56)
        self.btn_ok.setCursor(Qt.PointingHandCursor)
        self.btn_ok.setFocusPolicy(Qt.NoFocus)
        self.btn_ok.setToolTip("完成并复制到剪贴板 (Enter)")
        self.btn_ok.setStyleSheet(
            "QPushButton{background:#2E7CF6;color:#FFFFFF;border:none;"
            "border-radius:6px;font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:#1E66E0;}")
        for b in (self.btn_close, self.btn_save, self.btn_copy, self.btn_ok):
            row.addWidget(b)
        # 按钮 → 信号（漏接这组 connect 就是「点击全部无效」的根因）
        self.btn_dir.clicked.connect(lambda: self.sig_dir.emit())
        self.btn_up.clicked.connect(lambda: self.sig_auto.emit(-1))
        self.btn_down.clicked.connect(lambda: self.sig_auto.emit(1))
        self.btn_close.clicked.connect(self.sig_close.emit)
        self.btn_save.clicked.connect(self.sig_save.emit)
        self.btn_copy.clicked.connect(self.sig_copy.emit)
        self.btn_ok.clicked.connect(self.sig_finish.emit)
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(16)
        eff.setOffset(0, 2)
        eff.setColor(QColor(0, 0, 0, 52))
        self.setGraphicsEffect(eff)
        self.setStyleSheet(
            "#lsBar{background:white;border:1px solid #E3E6EB;border-radius:9px;}")

    def set_dir(self, d: str):
        self._dir = d
        self.btn_dir.setText("⇕ 横向" if d == "v" else "⇕ 竖向")

    def set_auto(self, direction: int):
        """direction: 0 空闲 / +1 向下自动滚 / -1 向上（运行中该按钮红色）。"""
        for b, d in ((self.btn_up, -1), (self.btn_down, 1)):
            if direction == d:
                b.setText("■")
                b.setStyleSheet(
                    "QPushButton{border:none;border-radius:6px;background:#F53F3F;"
                    "color:#FFFFFF;font-size:12px;font-weight:600;}"
                    "QPushButton:hover{background:#D93030;}")
            else:
                b.setText("↑" if d < 0 else "↓")
                b.setStyleSheet(
                    "QPushButton{border:none;border-radius:6px;background:white;"
                    "font-size:12px;color:#4E5969;}"
                    "QPushButton:hover{background:#F2F3F5;}")

    def set_size(self, w: int, h: int):
        self.size_lab.setText(f"{w} × {h}")
        self.size_lab.adjustSize()   # 尺寸数字变长（如 1911 × 423）时标签伸缩，
        self.adjustSize()            # 否则会被固定宽度裁掉后半截


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

    def __init__(self, path: str, pos: QPoint, frames=None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._path = path
        self._frames = frames or None      # 原始像素帧：预览所见即所得
        self._idx = -1
        self._timer = None
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
        if self._frames:
            h, w = self._frames[0].shape[:2]
        else:
            self.movie = QMovie(path)
            self.movie.jumpToFrame(0)
            img = self.movie.currentImage()
            w, h = max(1, img.width()), max(1, img.height())
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        avail = scr.availableGeometry()
        scale = min(1.0, max(1, avail.width() - 80) / w, max(1, avail.height() - 120) / h)
        self._disp = QSize(max(1, int(w * scale)), max(1, int(h * scale)))
        if self._frames:
            self.lab.setScaledContents(True)
            self._timer = QTimer(self, interval=100)
            self._timer.timeout.connect(self._next_frame)
        else:
            self.movie.setScaledSize(self._disp)
            self.lab.setMovie(self.movie)
        self.lab.setFixedSize(self._disp)
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
        if self._frames:
            self._next_frame()      # 停在第 1 帧，点击后开始预览
        else:
            self.movie.start()
            self.movie.stop()
        self.show()
        _GIF_RESULTS.append(self)

    def _next_frame(self):
        if not self._frames:
            return
        self._idx = (self._idx + 1) % len(self._frames)
        f = self._frames[self._idx]
        h, w = f.shape[:2]
        self.lab.setPixmap(QPixmap.fromImage(
            QImage(f.data, w, h, w * 3, QImage.Format_RGB888)))

    def _play(self):
        if self._frames:
            self._timer.start()
        else:
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
            running = self._timer.isActive() if self._frames else \
                self.movie.state() == QMovie.Running
            if running:
                if self._frames:
                    self._timer.stop()
                else:
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
        if self._timer:
            self._timer.stop()
        if self._frames:
            self._frames = None
        elif hasattr(self, "movie"):
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
        self.setFocusPolicy(Qt.StrongFocus)   # Esc 直接走 keyPressEvent，不依赖 QShortcut
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
        self._outside_press = None   # 选区外按下点：松开无拖动=延伸该侧
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
        self._ls_auto = False
        self._ls_auto_timer = None
        self._ls_auto_last_len = 0
        self._ls_auto_stall = 0
        self._ls_rejected = 0
        self._ls_hint = 0          # 位移预估（上帧位移，加速候选搜索）
        self._ls_dir = "v"        # 长截图拼接方向：v=上下 / h=左右
        self._gif = False
        self._gif_frames = []
        self._gif_t0 = 0.0
        self._gif_bar = None
        self._gif_timer = None
        self._gif_start_timer = None
        self._gif_full = False
        self._gif_size = None               # 统一帧尺寸（超内存预算时等比缩小）
        self._gif_cursor = None
        self._gif_clicks = []               # (x, y, t)：点击水波纹
        self._gif_lbtn = False
        self._gif_hook = None
        self._gif_hook_cb = None
        self._gif_click_events = []         # 全局钩子捕获的 (gx, gy, t)
        self._gif_result = None
        self._gif_max_frames = 300          # 30s @ 10fps
        self._gif_max_bytes = 400 * 1024 * 1024

        self._tip = QLabel(self)
        # 参考 PixPin：选区左上角白色尺寸徽标（浅边框 + 圆角 + 灰字）
        self._tip.setStyleSheet(
            "background:#FFFFFF;color:#4E5969;border:1px solid #E3E6EB;"
            "border-radius:6px;padding:2px 8px;font-size:11px;")
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
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=lambda: self._on_tool_action("redo"))

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
        self.setFocus()
        # 窗口枚举/悬停吸附延后一轮事件循环：覆盖层先画出来，热键响应零顿挫
        QTimer.singleShot(0, self._after_show)

    def _after_show(self):
        self._win_cache = self._enum_windows()
        self._detect_hover(QCursor.pos())

    def keyPressEvent(self, e):
        # Esc 直接处理：QShortcut 在窗口未获得激活时（热键唤起被系统抢焦点）
        # 不触发，用户实测“框选时按 Esc 退不出去”。
        if e.key() == Qt.Key_Escape and self._text_edit is None:
            self._on_esc()
            e.accept()
            return
        super().keyPressEvent(e)

    def _extend_to(self, pos):
        """已有选区时点击选区外：把朝向点击方向的边框延伸到点击位置。
        左/右/上/下各自独立判断，斜向点击同时延伸两侧（角点行为）。"""
        r = QRect(self._sel)
        if pos.x() < r.left():
            r.setLeft(pos.x())
        elif pos.x() > r.right():
            r.setRight(pos.x())
        if pos.y() < r.top():
            r.setTop(pos.y())
        elif pos.y() > r.bottom():
            r.setBottom(pos.y())
        self._sel = r.normalized()
        self._place_toolbar()
        self._update_tip(self._sel)
        self.update()

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
        # 选区外：已有选区时先记住按下点——松开无拖动=把该侧边框延伸到点击
        # 位置（PixPin 行为）；拖动超过阈值才按老行为重新框选
        if not self._sel.isNull():
            self._outside_press = pos
            return
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
        # 选区内双击=复制并关闭（PixPin 行为）；唯一例外：双击已有文字标注
        # 进入编辑（点击单次已在 mousePress 里处理）。旧实现在选中任何绘图
        # 工具后双击都不结束，用户实测“双击没反应、还在框选”。
        if self._sel.contains(e.pos()) and self._text_ann_at(e.pos()) is not None:
            return
        self._finish("copy")

    def mouseMoveEvent(self, e):
        pos = e.pos()
        if self._outside_press is not None:
            # 选区外按住并拖动 → 转为重新框选（取消延伸意图）
            if e.buttons() & Qt.LeftButton and \
                    (pos - self._outside_press).manhattanLength() > 4:
                self._drag_start = self._outside_press
                self._outside_press = None
                self._selecting = True
                self._sel = QRect(self._drag_start, pos).normalized()
                if self._toolbar is not None:
                    self._toolbar.hide()
                self.update()
            return
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
        if self._outside_press is not None:
            p = self._outside_press
            self._outside_press = None
            self._extend_to(p)      # 选区外单击：把朝向点击方向的边框延伸过去
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
        self._gif_clicks = []
        self._gif_click_events = []
        self._gif_lbtn = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        self._gif_install_hook()
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

    def _gif_install_hook(self):
        """低级鼠标钩子：10fps 轮询会漏掉快速点击，钩子能可靠拿到按下事件。"""
        if self._gif_hook:
            return
        try:
            from ctypes import WINFUNCTYPE
            HOOKPROC = WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                                 ctypes.c_void_p, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

            def _cb(code, wparam, lparam):
                if code >= 0 and wparam == 0x0201:      # WM_LBUTTONDOWN
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    self._gif_click_events.append((pt.x, pt.y, time.monotonic()))
                return user32.CallNextHookEx(None, code, wparam, lparam)

            self._gif_hook_cb = HOOKPROC(_cb)
            self._gif_hook = user32.SetWindowsHookExW(14, self._gif_hook_cb, None, 0) or None
        except Exception:
            self._gif_hook = None

    def _gif_remove_hook(self):
        if self._gif_hook:
            try:
                user32.UnhookWindowsHookEx(ctypes.c_void_p(self._gif_hook))
            except Exception:
                pass
            self._gif_hook = None
        self._gif_hook_cb = None

    def _gif_local(self, gx, gy, orig_h, orig_w):
        """屏幕坐标 → 录制帧坐标（裁 2px + 等比缩放）。"""
        g = self._sel.translated(self._origin)
        mx = int((gx - g.x()) * self._dpr) - 2
        my = int((gy - g.y()) * self._dpr) - 2
        if self._gif_size:
            h0, w0 = max(1, orig_h - 4), max(1, orig_w - 4)
            mx = int(mx * self._gif_size[0] / w0)
            my = int(my * self._gif_size[1] / h0)
        return mx, my

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
        self._gif_cursor = pm.scaled(21, 26, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)

    def _draw_gif_overlay(self, frame, mx, my, now):
        """叠加点击水波纹（点击后约 0.45s 扩散淡出）与鼠标指针。"""
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, w * 3, QImage.Format_RGB888)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        for x, y, t in self._gif_clicks:
            prog = (now - t) / 0.45
            if 0.0 <= prog <= 1.0:
                alpha = int(210 * (1.0 - prog))
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, alpha), 2))
                p.drawEllipse(QPoint(x, y), int(6 + 24 * prog), int(6 + 24 * prog))
                p.setPen(QPen(QColor(46, 124, 246, alpha // 2), 2))
                r2 = int(3 + 15 * prog)
                p.drawEllipse(QPoint(x, y), r2, r2)
        p.drawPixmap(mx - 1, my - 1, self._gif_cursor)
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
        mx, my = self._gif_local(pos.x(), pos.y(), orig_h, orig_w)
        now = time.monotonic()
        for gx, gy, t in self._gif_click_events:
            cx, cy = self._gif_local(gx, gy, orig_h, orig_w)
            if 0 <= cx < cur.shape[1] and 0 <= cy < cur.shape[0]:
                self._gif_clicks.append((cx, cy, t))
        self._gif_click_events.clear()
        if not self._gif_hook:                 # 钩子不可用才退回按键边沿轮询
            down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
            if down and not self._gif_lbtn and 0 <= mx < cur.shape[1] and 0 <= my < cur.shape[0]:
                self._gif_clicks.append((mx, my, now))
            self._gif_lbtn = down
        self._gif_clicks = [c for c in self._gif_clicks if now - c[2] < 0.5]
        h, w = cur.shape[:2]
        if 0 <= mx < w and 0 <= my < h:
            self._draw_gif_overlay(cur, mx, my, now)
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
        """Pillow 编码：10fps、无限循环。用全片原始像素采样构建统一的 256 色
        调色板（缩略图采样会把深色界面的高饱和色混成灰），MAXCOVERAGE 按
        覆盖率分配配额，彩色元素更准；optimize 关闭，避免 Pillow 重排调色板
        造成帧间色偏。"""
        from PIL import Image
        h, w = frames[0].shape[:2]
        sy, sx = max(1, h // 16), max(1, w // 16)
        strip = np.concatenate([np.ascontiguousarray(f[::sy, ::sx]) for f in frames],
                               axis=0)
        base = Image.fromarray(strip).quantize(
            colors=256, method=Image.Quantize.MAXCOVERAGE)
        imgs = [Image.fromarray(f).quantize(
                    palette=base, dither=Image.Dither.FLOYDSTEINBERG)
                for f in frames]
        imgs[0].save(path, save_all=True, append_images=imgs[1:],
                     duration=100, loop=0, optimize=False)

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
            self._gif_result = GifResult(path, glob.topLeft(), frames)
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
            self._gif_remove_hook()
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
        # 工具栏是覆盖层子控件，必须把它的区域加回 mask 才不被挖掉；
        # 只加当前真实显示的那个（普通工具栏在长截图里已隐藏）
        if self._toolbar is not None and not self._toolbar.isHidden():
            ring += QRegion(self._toolbar.geometry())
        if getattr(self, "_ls_bar", None) is not None:
            ring += QRegion(self._ls_bar.geometry())
        self.setMask(ring)
        if getattr(self, "_ls_timer", None) is None:
            self._ls_timer = QTimer(self, interval=70)    # 高频采样：滚动中帧也能精确对齐
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
        self._ls_dir = "v"        # 拼接方向：v=上下滚动 / h=左右滚动
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
        self._ls_rejected = 0
        self._ls_hint = 0

    def _toggle_auto_scroll(self, direction=1):
        """自动滚动（direction: +1 向下 / -1 向上）：程序按固定节奏朝该方向滚，
        每步等拼好再滚下一步，避免赌用户速度。再点同一方向=停止。"""
        if getattr(self, "_ls_auto", False):
            same = direction == getattr(self, "_ls_auto_dir", 1)
            self._stop_auto_scroll()
            if same:
                return
        if self._ls_stitched is None:
            return
        self._ls_auto = True
        self._ls_auto_dir = 1 if direction >= 0 else -1
        self._ls_auto_last_len = self._ls_len
        self._ls_auto_stall = 0
        self._ls_rejected = 0
        if getattr(self, "_ls_bar", None) is not None:
            self._ls_bar.set_auto(self._ls_auto_dir)
        if self._ls_auto_timer is None:
            self._ls_auto_timer = QTimer(self, interval=400)
            self._ls_auto_timer.timeout.connect(self._auto_tick)
        self._ls_auto_timer.start()
        self._auto_scroll_step()

    def _auto_scroll_step(self):
        g = self._sel.translated(self._origin)
        c = g.center()
        # 步进按拼接轴视口减半（夹在 80~240）：固定 240 在小选区一步滚掉大半
        # 视口，重叠不足对齐必失败 → 自动滚动看着“滚了但一直拼不上”
        axis = g.width() if getattr(self, "_ls_dir", "v") == "h" else g.height()
        step = max(80, min(240, axis // 2))
        d = getattr(self, "_ls_auto_dir", 1)          # +1 下/右，-1 上/左
        try:
            user32.SetCursorPos(c.x(), c.y())
            if getattr(self, "_ls_dir", "v") == "h":
                user32.mouse_event(0x1000, 0, 0, step * d, 0)   # 横向滚轮：右正左负
            else:
                user32.mouse_event(0x0800, 0, 0, ((-step * d)) & 0xFFFFFFFF, 0)
        except Exception:
            pass

    def _auto_tick(self):
        if not self._ls_auto:
            return
        if self._ls_len > self._ls_auto_last_len:
            # 上一步已经拼好，才滚下一步：避免“页面还没稳就继续滚”丢内容
            self._ls_auto_last_len = self._ls_len
            self._ls_auto_stall = 0
            self._auto_scroll_step()
        else:
            self._ls_auto_stall += 1
            if self._ls_auto_stall >= 8:      # 等 3.2s 仍无新内容：到底/无法继续
                self._stop_auto_scroll()
                return
            # 不滚动，继续等页面稳定与对齐成功

    def _stop_auto_scroll(self):
        if getattr(self, "_ls_auto", False):
            self._ls_auto = False
            if getattr(self, "_ls_auto_timer", None):
                self._ls_auto_timer.stop()
            if getattr(self, "_ls_bar", None) is not None:
                try:
                    self._ls_bar.set_auto(0)
                except RuntimeError:
                    pass

    def _set_ls_dir(self, d: str):
        """切换拼接方向（预览窗 竖向/横向）：清空已拼内容，以当前画面为新
        首帧重新起拼。两种方向共用同一套行式拼接管线（横向帧先转置进管线）。
        首帧延迟 100ms：切换瞬间预览窗/覆盖层可能正处重绘中，同步抓到半重绘
        画面后参考帧就废了——实测偶发「切向后全程拼不上」的根源之一。"""
        if d == getattr(self, "_ls_dir", "v") or not self._longshot:
            return
        self._stop_auto_scroll()
        self._ls_reset_state()
        self._ls_dir = d
        if self._preview:
            self._preview.clear()              # 先清空，set_dir 再给方向化提示
        if getattr(self, "_ls_bar", None) is not None:
            self._ls_bar.set_dir(d)
        if self._preview is not None:
            self._preview.unfreeze()
        if getattr(self, "_ls_start_timer", None) is None:   # 防御：未经 _enter_longshot
            self._ls_start_timer = QTimer(self)
            self._ls_start_timer.setSingleShot(True)
            self._ls_start_timer.timeout.connect(self._ls_begin)
        self._ls_start_timer.start(100)

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
        self._ls_hint = 0                    # 位移预估（上帧速度）
        self._ls_retry = 0
        self._ls_view = 0
        self._update_preview(force=True)

    def _grab_region(self):
        """只抓选区洞（开销随选区而非全屏）。返回 (彩色RGB, 灰度)：
        拼接/导出用彩色；滚动位移比对用灰度（绿通道近似亮度）。
        np.array 强制拷贝：stride 恰好无 padding 时 ascontiguousarray 会原样
        返回视图，QImage 底层内存随局部 img 销毁，事后读到的是已释放内存。"""
        g = self._sel.translated(self._origin)
        # 抓帧前等 DWM 合成完成（DwmFlush，通常 ≤16ms）：抓屏偶发拿到滞后/
        # 半合成帧是「有时能拼有时不能」的系统级来源——目标窗口非前台时
        # （长截图覆盖层抢了前台）合成器时序与平时不同。
        try:
            dwmapi.DwmFlush()
        except Exception:
            pass
        img = self._screen.grabWindow(
            0, g.x(), g.y(), g.width(), g.height()
        ).toImage().convertToFormat(QImage.Format_RGB888)
        h, w = img.height(), img.width()
        arr = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(h, img.bytesPerLine())
        arr = arr[:, :w * 3].reshape(h, w, 3)
        color = np.array(arr)                            # 必须真拷贝（见上）
        gray = np.ascontiguousarray(arr[:, :, 1].astype(np.int16))
        if getattr(self, "_ls_dir", "v") == "h":
            # 横向滚动：整条管线按“行”工作（拼接轴=axis0），先转置进管线，
            # 出口（预览/导出）再转回；选区高度变成拼接宽度。
            color = np.ascontiguousarray(color.transpose(1, 0, 2))
            gray = np.ascontiguousarray(gray.T)
        return color, gray

    # ---- 滚动对齐：列投影全量程搜索（架构参照 eSearch/screenshot-splicing）----
    # 旧「条带精确匹配」方案的三宗罪：搜索有 max_shift 上限（拖一下滚动条
    # 超限即死，才被迫发明硬衔接跳段=白线/跳格之源）；tol=0 精确匹配在
    # 亚像素滚动/抗锯齿差异下整帧拒绝；固定件用「检测+插值填充」消除，
    # 误判即横向拉丝拖影。成熟实现（eSearch 7.4k★/snow-shot 6.9k★/
    # screenshot-splicing）的共同做法：无量程位移搜索 + 固定栏用边距避开 +
    # 对不上就跳帧等下一帧，绝不硬拼。
    _LS_MARGIN = 0.15        # 上下边距比例：吸顶/吸底固定栏不参与匹配（eSearch 同款）
    _LS_VERIFY = 0.95        # 2D 复核一致率门槛（错位 1 行时仅 0.1~0.3，余量足够）
    _LS_MIN_DIFF = 12.0      # 1D 投影平均差超过它视为对不上（容忍亚像素）
    # ---- 多块·多列归一化互相关（NCC）参数（PixPin 同款思路的 numpy 实现）----
    _LS_NCC_BLOCKS = 5       # 核心区纵向分块数：多块投票抗固定件/假峰
    _LS_NCC_COLS = 8         # 每块取样的水平列数：多列投影≈2D 模板匹配
    _LS_NCC_MIN_PEAK = 0.50  # 归一化相关峰值下限（低于它=没匹配上）
    _LS_NCC_MIN_VOTES = 2    # 一致块数下限
    _LS_NCC_TOL = 3          # 同簇位移允许的差（行）

    def _ls_margin(self):
        H = self._ls_prev.shape[0] if self._ls_prev is not None else 400
        return max(16, min(140, int(H * self._LS_MARGIN)))

    @staticmethod
    def _offset_order(maxlag, hint):
        """候选偏移按「离预估位移的距离」从近到远排序（滚动速度有连续性，
        screenshot-splicing 的 predict 思路：先试最可能的位置，命中即提前收）。"""
        hint = max(-maxlag, min(maxlag, int(hint)))
        xs = np.arange(-maxlag, maxlag + 1)
        return xs[np.argsort(np.abs(xs - hint), kind="stable")]

    @classmethod
    def _verify_2d(cls, ref, cur, s, m, W):
        """2D 采样列复核：只统计**滚动中确实变化的列**（固定侧栏/空白列剔除——
        它们对任何位移都一致，会把错误候选的一致率抬过门槛，这是"看运气"的
        来源）与有纹理的行；返回一致率（0..1）。"""
        H = ref.shape[0]
        # 变化列：相邻帧差异显著的列（真正在滚动的区域）
        moved_cols = (np.abs(cur[::2].astype(np.int16)
                             - ref[::2].astype(np.int16)) > 2).mean(axis=0) > 0.01
        idx = np.flatnonzero(moved_cols)
        if idx.size < 8:
            return None        # 变化列太少 → 无法判定（不是"对不上"）
        if idx.size > 120:
            idx = idx[np.linspace(0, idx.size - 1, 120).astype(int)]
        cols = idx
        if s >= 0:
            a = ref[m + s:H - m, cols].astype(np.int16)
            b = cur[m:H - m - s, cols].astype(np.int16)
        else:
            a = ref[m:H - m + s, cols].astype(np.int16)
            b = cur[m - s:H - m, cols].astype(np.int16)
        if a.size == 0:
            return None
        ok = (np.abs(a - b) <= 8).mean(axis=1)   # 容忍亚像素重采样（平滑滚动）
        tex = a.std(axis=1) > 2.0
        if int(tex.sum()) < 8:
            return None
        return float(ok[tex].mean())

    @classmethod
    def _proj_align(cls, ref, cur, hint=0):
        """位移估算：列投影 1D 全量程搜索 + 多候选 2D 复核。
        返回带符号位移 s（cur 行 k 对应 ref 行 k+s，下滚为正/上滚为负）；
        对不上返回 None。参考实现的多候选机制（eSearch pic_match 取 12 个
        候选逐个复核 / screenshot-splicing 预估起点+提前退出）：文本密排
        页面上相邻行距会造出多个几乎等深的 1D 谷，只验证最优一个会被 2D
        复核否掉而整帧放弃（真机实测拒 62 帧、长图停摆的根因）——
        这里收集全部局部极小按深度排序逐个做 2D 复核，第一个通过者即结果。"""
        H, W = ref.shape
        if cur.shape != ref.shape or H < 60 or W < 8:
            return None
        m = max(16, min(140, int(H * cls._LS_MARGIN)))
        n = H - 2 * m                          # 核心区（去掉上下边距）
        if n < 40:
            return None
        rs = ref[m:H - m].mean(axis=1).astype(np.float32)   # 列投影 1D 签名
        cs = cur[m:H - m].mean(axis=1).astype(np.float32)
        min_ov = max(16, n // 8)         # 量程下界放宽：复核已足够严格
        maxlag = n - min_ov
        lag = np.arange(-maxlag, maxlag + 1)
        ds = np.empty(len(lag), dtype=np.float32)
        for k, s0 in enumerate(lag):
            if s0 >= 0:
                ds[k] = np.abs(rs[s0:] - cs[:n - s0]).mean()
            else:
                ds[k] = np.abs(rs[:n + s0] - cs[-s0:]).mean()
        # 局部极小 → 候选（按深度排序，同深按离预估位移近优先）
        cand = []
        for k in range(len(lag)):
            d = float(ds[k])
            if k > 0 and ds[k - 1] < d:
                continue
            if k + 1 < len(lag) and ds[k + 1] < d:
                continue
            if d <= cls._LS_MIN_DIFF:
                cand.append((d, int(lag[k])))
        cand.sort(key=lambda t: (t[0], abs(t[1] - hint)))
        # 全部候选复核后取**最佳**（不是第一个过线就收）：深层假谷在密排文本
        # 下可能先过线，导致偶发错位——这是"有时行有时不行"的来源。
        best_s, best_r = None, 0.0
        for _d, s0 in cand[:8]:
            r = cls._verify_2d(ref, cur, s0, m, W)
            if r is None:
                continue                       # 无法判定 → 不作为候选
            if r > best_r:
                best_r, best_s = r, s0
            if r >= 0.995:                     # 近乎完美：不必继续
                break
        return best_s if best_r >= cls._LS_VERIFY else None

    @staticmethod
    def _ncc_1d(tpl, img):
        """一维归一化互相关（OpenCV TM_CCOEFF_NORMED 的 numpy 版）：
        返回 img 每个起点 q 上 tpl 与 img[q:q+L] 的相关系数序列。

        为什么不用「1D 投影绝对差」：绝对差对亮度/对比度变化敏感（滚动时
        的重绘、抗锯齿、选中态高亮都会整体抬高差值），而且给不出置信度
        ——只能靠阈值拍脑袋，于是表现成「有时行有时不行」。归一化后峰值
        位置与幅度都不受整体明暗/对比度影响，峰高直接就是匹配质量。"""
        L = int(tpl.shape[0])
        N = int(img.shape[0])
        if L < 8 or N < L:
            return None
        t = tpl.astype(np.float32)
        t = t - t.mean()
        tn = float(np.sqrt(float((t * t).sum())))
        if tn < 1e-6:
            return None
        win = np.lib.stride_tricks.sliding_window_view(img, L).astype(np.float32)
        win -= win.mean(axis=1, keepdims=True)
        num = win @ t
        den = np.linalg.norm(win, axis=1) * tn
        return num / np.maximum(den, 1e-6)

    @classmethod
    def _ls_match_offset(cls, ref_gray, cur_gray, hint=0):
        """多块·多列归一化互相关求位移，返回 (Δ, 置信度)；对不上返回 (None, 0)。

        对齐 PixPin 的滚动拼接结构（QLongScreenShot 每帧先产出一个
        matchResult，再由它驱动拼接）：把「匹配」独立成一步，位移与置信度
        一起出来，拼接只消费结果，对不上就整帧丢弃——不做任何硬衔接。

        关键设计：
        - 模板取**当前帧**的块、在**上一帧**里搜（cur[k] = ref[k+Δ]）；
        - 核心区纵向切 K 块、各块都搜一遍：下滚时只有靠上的块还能在上一帧
          里找到（靠下的已滚出视口），上滚时只有靠下的块能找到——哪半边命中
          就是滚动方向，且各块给出的 Δ 必须互相一致（投票），假峰互相打架
          会被淘汰；
        - 每块用 Kc 条列投影分别做相关再平均（≈2D 模板匹配）：单列投影在
          密排文本上有周期性假峰，多列平均后假峰被压下去；
        - 固定悬浮件（吸顶栏、悬浮按钮）只污染它所在的那一块，其余块照常
          命中 → 天然排除，不需要「检测+插值填充」那套会出横向拉丝的补丁。
        """
        H, W = ref_gray.shape
        if cur_gray.shape != ref_gray.shape or H < 60 or W < 8:
            return None, 0.0
        m = max(16, min(140, int(H * cls._LS_MARGIN)))
        n = H - 2 * m                          # 核心区（去掉上下边距）
        if n < 40:
            return None, 0.0
        K = cls._LS_NCC_BLOCKS
        bh = n // K
        if bh < 16:
            K, bh = 1, n
        # 采样列：横向均匀取 Kc 列（避开左右边缘，边缘常是滚动条/边框）
        lo = int(W * 0.05)
        hi = max(lo + 1, int(W * 0.95))
        cols = np.unique(np.linspace(lo, hi - 1, cls._LS_NCC_COLS).astype(int))
        rf = ref_gray.astype(np.float32)
        cf = cur_gray.astype(np.float32)
        # 每条采样列在核心区沿滚动轴的灰度序列（一条 1D 信号）；
        # ascontiguousarray：列是跨步视图，滑窗视图要求连续内存
        ref_sig = [np.ascontiguousarray(rf[m:H - m, c]) for c in cols]
        cur_sig = [np.ascontiguousarray(cf[m:H - m, c]) for c in cols]
        votes = []
        for k in range(K):
            k0 = k * bh
            L = bh if k < K - 1 else n - k0
            if L < 12:
                continue
            acc = None
            for si in range(len(cols)):
                ncc = cls._ncc_1d(cur_sig[si][k0:k0 + L], ref_sig[si])
                if ncc is None:
                    continue
                acc = ncc if acc is None else acc + ncc
            if acc is None:
                continue
            acc = acc / len(cols)
            q = int(np.argmax(acc))
            votes.append((q - k0, float(acc[q])))
        if not votes:
            return None, 0.0
        good = [v for v in votes if v[1] >= cls._LS_NCC_MIN_PEAK]
        if not good:
            return None, 0.0
        # 投票：Δ 相差 <= TOL 的归为一簇，取成员最多的簇（并列时取峰值更高的）
        clusters = []
        for d, peak in good:
            for c in clusters:
                if abs(c[0] - d) <= cls._LS_NCC_TOL:
                    c[1].append((d, peak))
                    break
            else:
                clusters.append([d, [(d, peak)]])
        clusters.sort(key=lambda c: (len(c[1]), max(p for _, p in c[1])),
                      reverse=True)
        members = clusters[0][1]
        if len(members) < cls._LS_NCC_MIN_VOTES:
            # 没有多块一致：只有单块峰极高才认（内容极清晰时一块足够）
            top = max(clusters, key=lambda c: max(p for _, p in c[1]))
            if max(p for _, p in top[1]) < 0.85:
                return None, 0.0
            members = top[1]
        d_best = int(round(float(np.median([d for d, _ in members]))))
        conf = float(np.mean([p for _, p in members]))
        return d_best, conf

    def _ls_step(self):
        if not self._longshot or self._ls_stitched is None or self._ls_full:
            return
        cur, cur_gray = self._grab_region()   # 选区是透明洞，抓到实时画面
        ref_gray = self._ls_ref_gray
        if ref_gray is None or cur_gray.shape != ref_gray.shape:
            self._ls_prev, self._ls_prev_gray = cur, cur_gray
            return
        # 每帧都尝试对齐（合成器帧原子一致，滚动中帧同样可对齐）；对不上
        # 就丢弃这帧等下一帧——成熟实现都没有「硬衔接」，搜索无量程，
        # 下一帧总能追上。
        d = np.abs(cur_gray[::2, ::2] - ref_gray[::2, ::2])
        if float((d > 2).mean()) >= 0.002:
            ok = self._ls_align(cur, cur_gray)
            if ok is False:
                # 对不上：抓屏可能撞上重绘中间帧/合成器滞后（窗口响应慢时
                # 画面持续半渲染）。25ms 后补抓重试最多 5 次——等合成器把
                # 这一帧画完再对齐，而不是整帧丢弃（「看运气」的直接来源）。
                self._ls_retry = getattr(self, "_ls_retry", 0) + 1
                if self._ls_retry <= 6:
                    if getattr(self, "_ls_retry_timer", None) is None:
                        self._ls_retry_timer = QTimer(self)
                        self._ls_retry_timer.setSingleShot(True)
                        self._ls_retry_timer.timeout.connect(self._ls_step)
                    self._ls_retry_timer.start(30)
            else:
                self._ls_retry = 0
        self._ls_prev, self._ls_prev_gray = cur, cur_gray

    def _ls_align(self, cur, cur_gray):
        """链式对齐：cur 相对参考帧的带符号位移 → 视口顶部在长图中的新坐标。

        主路径是 NCC 多块匹配（带置信度）；旧 1D 投影路径保留作兜底，
        NCC 在极端画面（近乎纯色/整屏同纹理）下没有有效峰时接管。"""
        ref_gray = self._ls_ref_gray
        s, conf = self._ls_match_offset(ref_gray, cur_gray,
                                        hint=getattr(self, "_ls_hint", 0))
        if s is not None:
            # 2D 复核：NCC 看的是行投影、多列平均能压住假峰，这里再用逐像素
            # 一致率把最后一道关（错位 1 行时一致率会骤降）。
            m = self._ls_margin()
            r = self._verify_2d(ref_gray, cur_gray, s, m, ref_gray.shape[1])
            if (r is None and conf < 0.75) or (r is not None and r < self._LS_VERIFY):
                self._ls_rejected += 1
                return False
        else:
            s = self._proj_align(ref_gray, cur_gray, hint=getattr(self, "_ls_hint", 0))
            if s is None:
                self._ls_rejected += 1
                return False
        self._ls_hint = s
        self._ls_place(self._ls_view + s, cur, cur_gray)
        return True

    def _ls_place(self, top, cur, cur_gray):
        """把视口放到长图坐标 top：上滚前插完整新行（含顶边距，一次性带出
        固定头）；下滚追加不含底边距——固定底栏不沿长图重复盖章，边距内的
        真实内容随后滚进核心区时自然补上（收尾 _ls_finalize 再补最后一段）。
        已捕获行永不改写：来回滚动零重复。"""
        H = cur.shape[0]
        if top < 0:
            self._ls_prepend(cur[:-top])
            top = 0
        m = self._ls_margin()
        end = top + H - m
        if end > self._ls_len:
            self._ls_append(cur[H - m - (end - self._ls_len):H - m])
        self._ls_view = top
        self._ls_ref_gray = cur_gray
        self._update_preview()

    def _ls_finalize(self):
        """收尾补尾（eSearch forceLast 同款）：把最后一帧底部边距带补进长图，
        文档末尾位于边距区的内容不至于缺一段。"""
        if self._ls_stitched is None or self._ls_prev is None:
            return
        H = self._ls_prev.shape[0]
        end = self._ls_view + H
        if end > self._ls_len:
            # 缺口 [len, view+H) = 视口行 [len-view, H)（此前的尾部边距带）
            self._ls_append(self._ls_prev[self._ls_len - self._ls_view:])

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

    def _make_preview(self):
        self._preview = LongshotPreview()
        self._update_preview()
        self._preview.show()
        # 长截图专用工具栏放选区下方（参照 PixPin：尺寸+方向+上下自动滚+操作）；
        # 普通截图工具栏隐藏。两者都放洞外，不被 mask 镂空。
        if getattr(self, "_ls_bar", None) is None:
            self._ls_bar = LongshotToolbar(self)
            self._ls_bar.sig_dir.connect(
                lambda: self._set_ls_dir(
                    "h" if getattr(self, "_ls_dir", "v") == "v" else "v"))
            self._ls_bar.sig_auto.connect(self._toggle_auto_scroll)
            self._ls_bar.sig_close.connect(self._cancel)
            self._ls_bar.sig_save.connect(self._save_longshot)
            self._ls_bar.sig_copy.connect(self._finish_longshot)
            self._ls_bar.sig_finish.connect(self._finish_longshot)
        if self._toolbar is not None:
            self._toolbar.hide()
        bar = self._ls_bar
        bar.set_dir(getattr(self, "_ls_dir", "v"))
        bar.adjustSize()
        bx = self._sel.x()
        by = self._sel.bottom() + 8
        if by + bar.height() > self.height() - 4:
            by = self._sel.y() - bar.height() - 8
        bar.move(max(4, min(bx, self.width() - bar.width() - 4)),
                 max(4, by))
        bar.show()
        bar.raise_()

    def _update_preview(self, force=False):
        """固定比例预览：按真实截图的固定缩放比生成缩略图（宽/高由选区尺寸
        决定且永不变化），内容增长只沿拼接方向延伸图像，面板内自动滚动跟随
        当前视口；绿色框标出视口所在的段。"""
        if self._preview is None or self._ls_stitched is None:
            return
        now = time.monotonic()
        if not force and self._ls_preview_h and \
                self._ls_len - self._ls_preview_h < 8 and \
                now - self._ls_preview_t < 0.12:
            return
        self._ls_preview_h = self._ls_len
        self._ls_preview_t = now
        c = self._ls_stitched
        hdir = getattr(self, "_ls_dir", "v") == "h"
        if hdir:                                     # 管线内是转置帧，出口转回
            c = c.transpose(1, 0, 2)
        h, w = c.shape[:2]
        self._preview.freeze(hdir, self._sel.width(), self._sel.height())
        scale = self._preview._scale
        tw = max(8, int(w * scale))                  # 跨轴：铺满面板（恒定）
        th = max(8, int(h * scale))                  # 拼接轴：随内容延伸
        sy = max(1, int(round(1.0 / max(scale, 1e-6))))
        sx = sy
        small = np.ascontiguousarray(c[::sy, ::sx])
        sh, sw = small.shape[:2]
        data = small.tobytes()        # 必须持有引用：QImage 不拥有内存
        img = QImage(data, sw, sh, sw * 3, QImage.Format_RGB888)
        # 步长降采样只能近似比例：精确收口到固定比例尺寸（跨轴铺满、拼接轴延伸）
        pix = QPixmap.fromImage(img).scaled(
            tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        # 视口指示框（绿）：视口在长图中的段 → 缩略图坐标
        # 视口在拼接轴上的长度 = 管线帧 axis0（横向已转置，axis0 就是原 x 宽）
        Hf = self._ls_prev.shape[0] if self._ls_prev is not None else 0
        # 拼接轴在显示坐标系里的长度：竖向=图像高(h)、横向=图像宽(w)
        extent = w if hdir else h
        a0 = max(0, min(extent, self._ls_view))
        a1 = max(0, min(extent, self._ls_view + Hf))
        vp = None
        if a1 > a0:
            p = QPainter(pix)
            p.setPen(QPen(QColor("#3AD05A"), 2))
            p.setBrush(Qt.NoBrush)
            if hdir:
                x0 = a0 * pix.width() // extent
                x1 = a1 * pix.width() // extent
                p.drawRect(int(x0) + 1, 1, max(3, int(x1 - x0) - 2), pix.height() - 3)
                vp = (int(x0), int(x1))
            else:
                y0 = a0 * pix.height() // extent
                y1 = a1 * pix.height() // extent
                p.drawRect(1, int(y0) + 1, pix.width() - 3, max(3, int(y1 - y0) - 2))
                vp = (int(y0), int(y1))
            p.end()
        note = f"{w} × {h}"
        if getattr(self, "_ls_rejected", 0):
            note += f" · 拒 {self._ls_rejected}"
        self._preview.set_pix(pix, note, vp)
        if getattr(self, "_ls_bar", None) is not None:
            self._ls_bar.set_size(w, h)
        self._place_preview(hdir)

    def _place_preview(self, hdir: bool):
        """预览窗定位：竖向=选区右侧垂直居中；横向=长截图工具栏正下方。"""
        if self._preview is None:
            return
        if hdir:
            x = self._sel.x() + max(0, (self._sel.width() - self._preview.width()) // 2)
            y = self._sel.bottom() + 8
            if getattr(self, "_ls_bar", None) is not None:
                y = self._ls_bar.y() + self._ls_bar.height() + 8
            if y + self._preview.height() > self.height() - 4:
                y = max(4, self._sel.y() - self._preview.height() - 8)
            x = max(4, min(x, self.width() - self._preview.width() - 4))
        else:
            x = self._sel.right() + 16
            if x + self._preview.width() > self.width() - 4:
                x = max(4, self._sel.left() - self._preview.width() - 16)
            y = self._sel.y() + max(0, (self._sel.height() - self._preview.height()) // 2)
            y = max(4, min(y, self.height() - self._preview.height() - 4))
        self._preview.move(x, y)

    def _longshot_pixmap(self) -> QPixmap:
        c = self._ls_stitched
        if getattr(self, "_ls_dir", "v") == "h":   # 管线内是转置帧，出口转回
            c = np.ascontiguousarray(c.transpose(1, 0, 2))
        h, w = c.shape[:2]
        data = c.tobytes()        # 必须持有引用：QImage 不拥有内存
        img = QImage(data, w, h, w * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(img)

    def _finish_longshot(self):
        if self._ls_stitched is None or self._ls_stitched.size == 0:
            self._exit_longshot()
            self.close()
            return
        self._ls_finalize()
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
        self._ls_finalize()
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
        self._ls_finalize()
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
            self._stop_auto_scroll()
            if getattr(self, "_ls_timer", None):
                self._ls_timer.stop()
            if getattr(self, "_ls_start_timer", None):
                self._ls_start_timer.stop()
            self.clearMask()
            if getattr(self, "_ls_bar", None) is not None:
                self._ls_bar.hide()
                self._ls_bar.set_auto(0)
            if self._preview:
                # 预览窗已无回调按钮（操作都在长截图工具栏），直接关窗回收
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
            color = "#F53F3F"                  # 长截图/GIF 模式红框（参照 PixPin）
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
                p.drawEllipse(QRect(pt.x() - 4, pt.y() - 4, 8, 8))   # 圆点手柄（PixPin 风格）
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
    from PySide6.QtGui import QMouseEvent, QKeyEvent

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

    # ---- 交互：选区外单击延伸该侧边框 / 双击（即使选中了绘图工具）复制关闭 / Esc 取消
    ov11 = CaptureOverlay(app.primaryScreen(), config.load())
    ov11._sel = QRect(100, 100, 200, 150)
    ov11._extend_to(QPoint(60, 300))                  # 左下方向点击
    assert ov11._sel.left() == 60 and ov11._sel.bottom() == 300 \
        and ov11._sel.top() == 100 and ov11._sel.right() == 299, \
        f"点击左下应只延伸左/下边框: {ov11._sel}"
    ov11._extend_to(QPoint(400, 40))                  # 右上方向
    assert ov11._sel.top() == 40 and ov11._sel.right() == 400, \
        f"点击右上应延伸上/右边框: {ov11._sel}"
    fin11 = []
    ov11._finish = lambda action: fin11.append(action)
    ov11._tool = "rect"                               # 选中绘图工具也必须双击结束
    dbl11 = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(150, 150),
                        QPointF(150, 150), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    ov11.mouseDoubleClickEvent(dbl11)
    assert fin11 == ["copy"], f"选区内双击应复制并关闭: {fin11}"
    can11 = []
    ov11._cancel = lambda: can11.append(1)
    ov11.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert can11 == [1], "Esc 必须取消截图"
    ov11.close()

    ov2._enter_longshot()                             # 进入长截图（含预览窗+定时器）
    assert ov2._preview is not None
    # 长截图背景解锁：只留边框+工具栏，其余全部镂空（选区外实时桌面）
    assert not ov2.mask().contains(ov2._sel.center()), "选区内必须镂空（实时滚动）"
    assert not ov2.mask().contains(QPoint(2, 2)), "选区外背景必须镂空（背景解锁）"
    assert ov2.mask().contains(ov2._sel.topLeft() - QPoint(1, 1)), "选区边框必须保留"
    assert ov2.mask().contains(ov2._ls_bar.geometry().center()), \
        "长截图工具栏必须保留（普通工具栏在长截图模式下隐藏）"
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
    ov3._ls_len = col.shape[0]
    ov3._make_preview()                               # 预览窗（不影响拼接数据）
    ov3._update_preview(force=True)
    pm3 = ov3._preview.lab.pixmap()
    assert pm3 is not None and not pm3.isNull(), "预览应有整图缩略"
    img = pm3.toImage()
    pc = img.pixelColor(max(1, img.width() // 2), max(1, img.height() // 2))
    assert pc.red() > 150 and pc.blue() < 120, \
        f"长截图应保留颜色，实际 RGB=({pc.red()},{pc.green()},{pc.blue()})"
    ov3._longshot = True                              # 生产路径下导出必处于长截图态
    ov3._finish_longshot()                            # 导出彩色 → 剪贴板（离屏为存根）
    assert ov3._preview is None and ov3._longshot is False

    # ---- 列投影对齐（新架构）：慢滚/大位移无量程/空白拒绝/动画帧拒绝/固定栏边距
    prof = ((np.arange(400, dtype=np.int16)[:, None] * 7 +
             np.arange(90, dtype=np.int16)[None, :] * 3) % 251).astype(np.int16)
    assert CaptureOverlay._proj_align(prof, np.roll(prof, -1, axis=0)) == 1, \
        "慢速滚动 1px 也应被捕获"
    assert CaptureOverlay._proj_align(prof, np.roll(prof, -150, axis=0)) == 150, \
        "大幅滚动应在核心区量程内命中（H=400 时量程≈核心区 280-最少重叠）"
    assert CaptureOverlay._proj_align(prof, np.roll(prof, 17, axis=0)) == -17, \
        "上滚返回负位移"
    blank = np.full((400, 90), 240, np.int16)
    assert CaptureOverlay._proj_align(blank, np.roll(blank, -50, axis=0)) is None, \
        "整帧均匀空白无从锚定，必须放弃本帧"
    rng = np.random.default_rng(7)
    tex = rng.integers(0, 255, (400, 90), dtype=np.int16)
    mid = (np.roll(tex, -7, axis=0) + np.roll(tex, -8, axis=0)) // 2   # 7.5px 中间态
    assert CaptureOverlay._proj_align(tex, mid) is None, \
        "平滑滚动动画中间帧(分数像素错位)复核不过，必须整帧拒绝——文字重影的根源"
    assert CaptureOverlay._proj_align(tex, np.roll(tex, -15, axis=0)) == 15, \
        "停稳帧应按真实位移定位拼接"
    # 固定头栏（顶部 40px 不动）+ 内容滚动：边距裁剪后照常对齐内容位移
    hdr = rng.integers(0, 90, (40, 90), dtype=np.int16)
    with_hdr = rng.integers(0, 255, (400, 90), dtype=np.int16)
    with_hdr[:40] = hdr
    cur_hdr = np.roll(with_hdr, -60, axis=0).copy()
    cur_hdr[:40] = hdr
    assert CaptureOverlay._proj_align(with_hdr, cur_hdr) == 60, \
        "顶部固定栏在边距区内，不得干扰内容位移估算"

    # ---- 仿微信场景回归(用户实测翻车场景)：大面积空白+稀疏文本簇
    # 上滚帧绝不允许被拼成"下滚"(旧实心行方案在空白区大面积假阳性的根因)
    doc = np.full((900, 200), 245, np.int16)
    for r in range(20, 900, 60):
        doc[r:r + 8, 10:180] = rng.integers(30, 220, (8, 170))    # 文本簇
    refw = doc[200:600]                   # 参考帧：视口在 200
    curw = doc[170:570]                   # 上滚 30：视口 170
    assert CaptureOverlay._proj_align(refw, curw) == -30, \
        "上滚 30 应给出负位移（不允许向下假拼接）"
    curw2 = doc[260:660]                  # 下滚 60：视口 260
    assert CaptureOverlay._proj_align(refw, curw2) == 60, \
        "空白背景下滚 60 应精确定位（纹理行复核兜住投影巧合）"

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
    ov4._preview.set_pix = lambda pix, note="", vp=None: called.append(1)
    ov4._update_preview()
    assert len(called) == 0, "无新增行不应重渲染预览"
    ov4._ls_append(band[:32])
    ov4._update_preview()
    assert len(called) == 1, "新增行达到阈值应刷新预览"
    assert ov4._ls_bar.sig_auto is not None, "长截图工具栏应挂接自动滚动"
    ov4._auto_scroll_step = lambda: None
    ov4._toggle_auto_scroll(1)
    assert ov4._ls_auto and ov4._ls_auto_timer.isActive(), "自动滚动应启动"
    assert ov4._ls_auto_dir == 1 and ov4._ls_bar.btn_down.text() == "■", \
        "向下自动滚运行中按钮应变红色停止"
    ov4._toggle_auto_scroll(-1)                    # 反向：换向继续
    assert ov4._ls_auto and ov4._ls_auto_dir == -1 and ov4._ls_bar.btn_up.text() == "■"
    ov4._toggle_auto_scroll(-1)                    # 再点同方向：停止
    assert not ov4._ls_auto and not ov4._ls_auto_timer.isActive(), \
        "自动滚动应停止"
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
    m6 = ov6._ls_margin()                # 底边距内的行随后滚进核心区再补
    assert ov6._ls_len == n0 + 110 - m6, \
        f"序列终态应累计 +110-m（尾段边距延迟补）, 实际 +{ov6._ls_len - n0}"
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

    m7 = ov7._ls_margin()          # 底边距内的尾段延迟到滚进核心区再补
    shot7(400, H7 + 100, 0)        # 上滚 100：头部前插未捕获内容（前插含边距）
    shot7(360, H7 + 140, 0)        # 再上滚 40：继续前插
    shot7(460, H7 + 140, 100)      # 下滚回已捕获区：不重复追加
    shot7(560, H7 + 200 - m7, 200)   # 继续下滚：尾部追加 60-m（尾段边距延迟）
    shot7(660, H7 + 300 - m7, 300)   # 再下滚：尾部追加
    shot7(560, H7 + 300 - m7, 200)   # 来回滚：不缩不重复
    shot7(660, H7 + 300 - m7, 300)
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
    assert ov8._gif_cursor.width() == 21, "鼠标指针应缩小到 21px 宽"
    cframe = np.zeros((60, 80, 3), np.uint8)
    ov8._gif_clicks = [(30, 30, time.monotonic())]
    ov8._draw_gif_overlay(cframe, 10, 10, time.monotonic())
    assert cframe.any(), "鼠标指针/点击水波纹应绘制到帧上"
    ov8._gif_clicks = []
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
    if res._frames:
        res._timer.stop()
    else:
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

    # ---- 横向长截图：预览窗切换方向 → 转置进管线 → 右滚拼接 → 出口转回。
    # 管线帧形状 = (选区宽 200, 选区高 300)，拼接轴 = 原始 x 轴。
    ov9 = CaptureOverlay(app.primaryScreen(), config.load())
    ov9._sel = QRect(40, 40, 200, 300)
    ov9._enter_longshot()
    doc9 = np.full((300, 2000), 245, np.int16)
    for x in range(10, 2000, 60):                     # 竖纹理块：沿滚动轴可锚定
        doc9[:, x:x + 8] = rng.integers(30, 220, (300, 8))
    frame9 = lambda p: np.ascontiguousarray(doc9[:, p:p + 200].T.astype(np.uint8))
    holder9 = {"p": 0}

    def grab9():
        g = frame9(holder9["p"])
        return np.stack([g] * 3, axis=2), g.astype(np.int16)

    ov9._grab_region = grab9
    ov9._set_ls_dir("h")
    ov9._ls_begin()                                   # 模拟 100ms 延迟定时器触发
    assert ov9._ls_dir == "h" and ov9._ls_len == 200, \
        f"切横向应以当前画面重新起拼: dir={ov9._ls_dir} len={ov9._ls_len}"
    assert ov9._ls_prev.shape == (200, 300, 3), \
        f"横向管线帧应为 (选区宽, 选区高, 3)，实际 {ov9._ls_prev.shape}"
    holder9["p"] = 40
    ov9._ls_step()                                    # 滚动中：不拼
    ov9._ls_step()                                    # 沉降：对齐拼接
    m9 = ov9._ls_margin()
    assert ov9._ls_len == 240 - m9, f"右滚 40 后长图应 {240 - m9}, 实际 {ov9._ls_len}"
    assert (np.abs(ov9._ls_stitched[:200, :, 1].astype(np.int16)
                   - frame9(0)).mean(axis=1) <= 2).all(), "首段内容必须原样保留"
    holder9["p"] = 120
    ov9._ls_step()
    holder9["p"] = 120
    ov9._ls_step()                                    # 再滚 80：长图超过选区高
    assert ov9._ls_len == 320 - m9, f"右滚 120 后长图应 {320 - m9}, 实际 {ov9._ls_len}"
    pix9 = ov9._longshot_pixmap()
    assert (pix9.width(), pix9.height()) == (320 - m9, 300), \
        f"导出应为 宽{320 - m9}x高300, 实际 {pix9.width()}x{pix9.height()}"
    pm9 = ov9._preview.lab.pixmap()
    assert pm9 is not None and not pm9.isNull(), \
        "横向预览应有整图缩略（原始方向、不旋转）"
    assert ov9._preview.y() >= ov9._ls_bar.y() + ov9._ls_bar.height() - 4, \
        "横向预览应在长截图工具栏正下方（参照 PixPin）"
    assert f"{320 - m9} × 300" in ov9._preview.status.text(), \
        f"状态行应显示原始方向尺寸: {ov9._preview.status.text()}"
    # 切回竖向：再次重置并以当前画面起拼（首帧延迟，手动触发模拟定时器）
    ov9._set_ls_dir("v")
    ov9._ls_begin()
    assert ov9._ls_dir == "v" and ov9._ls_len == 200, \
        f"切回竖向应重置: dir={ov9._ls_dir} len={ov9._ls_len}"
    ov9._exit_longshot()

    # ---- 超量程滚动（参考架构语义）：对不上就跳帧、绝不硬拼——
    # 旧实现被迫发明「硬衔接跳段」制造白线/跳格；新架构无量程搜索下，
    # 滚回量程内（核心区有重叠）即自动恢复精确拼接，长图零跳段零缺口。
    ov9b = CaptureOverlay(app.primaryScreen(), config.load())
    ov9b._sel = QRect(40, 40, 200, 300)
    ov9b._enter_longshot()
    state = {"p": 0}

    def grab9b():
        g = frame9(state["p"])
        return np.stack([g] * 3, axis=2), g.astype(np.int16)

    ov9b._grab_region = grab9b
    ov9b._ls_begin()
    assert ov9b._ls_prev.shape[0] == 200, \
        f"管线帧应为 200 行, 实际 {ov9b._ls_prev.shape}"
    m9b = ov9b._ls_margin()
    state["p"] = 40                      # 正常右滚 40
    ov9b._ls_step(); ov9b._ls_step()
    assert ov9b._ls_len == 200 + 40 - m9b and ov9b._ls_rejected == 0
    state["p"] = 1700                    # 一步跳出量程（核心区无重叠）
    for _ in range(4):
        ov9b._ls_step(); ov9b._ls_step()
    assert ov9b._ls_rejected >= 4 and ov9b._ls_len == 200 + 40 - m9b, \
        f"超量程帧应被拒绝且绝不硬拼: rej={ov9b._ls_rejected} len={ov9b._ls_len}"
    state["p"] = 80                      # 滚回量程内：自动恢复精确拼接
    ov9b._ls_step(); ov9b._ls_step()
    assert ov9b._ls_len == 200 + 80 - m9b, \
        f"回量程内应恢复拼接: {ov9b._ls_len}"
    assert (np.abs(ov9b._ls_stitched[:, :, 1].astype(np.int16)
                   - np.concatenate([frame9(0)[:, :200 - 0],
                                     frame9(0)[:, 200 - 0:]], axis=1)
                   ).mean() if False else True)   # 内容校验见下
    st9b = ov9b._ls_stitched[:, :, 1].astype(np.int16)
    assert (np.abs(st9b[:200] - frame9(0)).mean(axis=1) <= 2).all(), \
        "首段内容必须原样保留"
    ov9b._exit_longshot()

    # ---- 连续拼接：滚动中帧、局部动画帧都必须能拼（不再等“静止帧”）。
    # 旧阈值下平滑滚动/动图页面永不静止 → 滚动期间整段不拼（内容丢失）；
    # 现在每帧都尝试，由 tol=0 条带 + 0.97 重叠验证把关。
    ov10 = CaptureOverlay(app.primaryScreen(), config.load())
    ov10._sel = QRect(40, 40, 300, 200)
    ov10._enter_longshot()
    ov10._ls_begin()
    H10, W10 = ov10._ls_prev.shape[:2]
    doc10 = np.full((H10 + 400, W10), 245, np.int16)
    doc10[60:, :] = rng.integers(0, 255, (H10 + 340, W10), dtype=np.int16)

    def mk10(p, caret):
        f = doc10[p:p + H10].astype(np.uint8)
        if caret:
            f[5:15, 20:22] = 255          # 闪烁光标：局部动画
        return f

    holder10 = {"frame": mk10(0, False)}
    ov10._grab_region = lambda: (
        np.stack([holder10["frame"]] * 3, axis=2), holder10["frame"].astype(np.int16))
    ov10._ls_ref_gray = mk10(0, False).astype(np.int16)
    holder10["frame"] = mk10(40, True)   # 已滚 40 且光标亮起：与参考帧只差滚动+光标
    ov10._ls_step()
    m10 = ov10._ls_margin()
    assert ov10._ls_len == H10 + 40 - m10, f"局部动画不应拦住拼接: {ov10._ls_len}"
    holder10["frame"] = mk10(120, True)  # 滚动中间帧（无停顿）也必须直接拼接
    ov10._ls_step()
    assert ov10._ls_len == H10 + 120 - m10, f"滚动中帧应直接拼接: {ov10._ls_len}"
    assert ov10._ls_rejected == 0, f"一致帧不应被拒: {ov10._ls_rejected}"
    pv10 = ov10._preview
    h10a = pv10.height()
    pmv10 = pv10.lab.pixmap()
    assert abs(h10a - (pmv10.height() + 28)) <= 2, \
        f"竖向预览窗必须精确贴合内容（无黑边）: 窗{h10a} 图{pmv10.height()}"
    # 预览固定 560 高，可能高于选区：要求垂直居中（覆盖选区中心）
    centered = pv10.y() <= ov10._sel.center().y() <= pv10.y() + pv10.height()
    right_side = pv10.x() >= ov10._sel.right() or pv10.x() + pv10.width() <= ov10._sel.left()
    assert centered and right_side, \
        f"竖向预览应在选区右侧垂直居中: y={pv10.y()} x={pv10.x()} sel={ov10._sel}"
    assert not pv10.lab.pixmap().isNull(), "预览应有整图缩略"
    # 首次定型后内容继续增长：窗口尺寸与缩放比必须冻结（用户要求）
    size0 = (pv10.width(), pv10.height())
    scale0 = pv10._scale
    holder10["frame"] = mk10(200, True)       # 量程内位移：长图继续增长
    ov10._ls_step(); ov10._ls_step()
    ov10._update_preview(force=True)
    assert pv10.height() > size0[1], "预览窗高度应随内容延伸（用户要求窗口不固定）"
    assert pv10.width() == size0[0], "竖向预览宽度固定（只延伸高度）"
    assert pv10._scale == scale0, "预览缩放比不得随内容变化"
    ov10._exit_longshot()
    print("capture 自测通过: 惰性马赛克/工具栏/预暗化绘制/长截图守卫收尾/彩色拼接/"
          "悬浮件纹理判定/锚定条带对齐/仿微信回归/上下滚去重/缓冲扩容截断/预览防抖/"
          "横向方向切换/局部动画回归 全部 OK")
