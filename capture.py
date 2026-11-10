"""截图覆盖层 — 暗化遮罩 + 窗口吸附 + 选区 + PixPin 风格工具栏 + 标注。"""
import ctypes
import math
import os

import numpy as np
from ctypes import wintypes

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QImage,
                           QRegion,
                           QKeySequence, QPainter, QPainterPath, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QTextEdit, QToolButton, QVBoxLayout,
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
                    "longshot": "长截图（滚动拼接）", "gif": "GIF 录制（后续版本）",
                    "pin": "贴图 (Ctrl+D)", "copy": "截图并复制 (Enter)"}
            b.setToolTip(tips.get(name, name))
            b.setIcon(icon(name, "#4E5969", 20, 1.6))
            b.setProperty("tool", name)
            if name == "gif":
                b.setEnabled(False)
            else:
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
        col.setContentsMargins(2, 2, 2, 2)
        col.setSpacing(4)
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
        btns.setSpacing(6)
        btn_ok = QPushButton("完成 (Enter)")
        btn_cancel = QPushButton("取消 (Esc)")
        for b in (btn_ok, btn_cancel):
            b.setFixedHeight(24)
            b.setCursor(Qt.PointingHandCursor)
        btn_ok.setStyleSheet("QPushButton{background:#2E7CF6;color:white;border:none;"
                             "border-radius:4px;}QPushButton:hover{background:#1E66E0;}")
        btn_cancel.setStyleSheet("QPushButton{background:#F2F3F5;color:#4E5969;border:none;"
                                 "border-radius:4px;}QPushButton:hover{background:#E5E6EB;}")
        btn_ok.clicked.connect(lambda: self.on_finish and self.on_finish())
        btn_cancel.clicked.connect(lambda: self.on_cancel and self.on_cancel())
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)
        col.addLayout(btns)
        self.setStyleSheet(
            "LongshotPreview{background:#2E7CF6;border-radius:4px;}")
        self.setFixedWidth(192)
        self._apply_height(60)

    def _apply_height(self, content_h: int):
        """按预览内容高度调整窗口高度，并夹在当前屏幕内。"""
        total = content_h + 50                     # 边距2*2 + 间距4*2 + 状态14 + 按钮行24 + 余量
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
        self._ls_cap = 0
        self._ls_ref_gray = None
        self._ls_fixed_hint = None
        self._ls_preview_h = 0
        self._ls_scheduled = False
        self._preview = None

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
        if getattr(self, "_longshot", False):
            self._exit_longshot()
            return
        self._cancel()

    def _on_enter(self):
        if self._text_edit is not None:
            self._commit_text()
            return
        if getattr(self, "_longshot", False):
            self._finish_longshot()
            return
        self._finish("copy")

    def _cancel(self):
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
        if self._longshot:
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
        if getattr(self, "_longshot", False) and name != "close":
            return  # 长截图进行中工具栏其余动作一律忽略（复制/保存会误关覆盖层）
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

    # ------------------------------------------------ 长截图（透明洞 + 定时抓屏拼接）
    def _enter_longshot(self):
        if self._sel.isNull():
            return
        self._longshot = True
        self._tool = None
        self.toolbar.highlight_tool("longshot")
        # 真实透明洞：选区镂空，底层窗口直接可见、可直接滚轮滚动
        hole = QRegion(self._sel)
        self.setMask(QRegion(self.rect()) - hole)
        self._ls_prev, self._ls_prev_gray = self._grab_region()
        # 预分配拼接缓冲：逐帧 vstack 全量拷贝是长图越拼越卡的元凶，改为倍增扩容
        w3 = max(1, self._ls_prev.shape[1] * 3)
        self._ls_cap = max(1000, min(40000, 150 * 1024 * 1024 // w3))  # 彩色按字节量封顶
        H0, W0 = self._ls_prev.shape[:2]
        self._ls_buf = np.empty((min(self._ls_cap, max(H0 * 2, 256)), W0, 3), np.uint8)
        self._ls_buf[:H0] = self._ls_prev
        self._ls_len = H0
        self._ls_stitched = self._ls_buf[:H0]
        self._ls_ref_gray = self._ls_prev_gray   # 拼接参考帧：只在拼接成功时前进
        self._ls_fixed_hint = None               # 移动阶段留下的悬浮固定件行提示
        self._ls_preview_h = 0
        self._make_preview()
        self._ls_timer = QTimer(self, interval=150)   # 快滚时单拍位移更小、易落栅格
        self._ls_timer.timeout.connect(self._ls_step)
        self._ls_timer.start()
        self.activateWindow()
        self.setFocus()

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

    @staticmethod
    def _projection_candidates(ref, cur, max_shift=1600, n=8):
        """行均值投影粗排：返回差异最小的 n 个互不相邻(>3px)候选，升序。"""
        H = cur.shape[0]
        maxs = min(max_shift, H - 60)
        pp = ref.mean(axis=1, dtype=np.float32)
        cp = cur.mean(axis=1, dtype=np.float32)
        diffs = np.empty(maxs, dtype=np.float32)
        for s in range(maxs):
            diffs[s] = np.abs(cp[:H - s] - pp[s:H]).mean()
        order = np.argsort(diffs)
        cand = []
        for s in order:
            if all(abs(int(s) - c0) > 3 for c0 in cand):
                cand.append(int(s))
            if len(cand) >= n:
                break
        return sorted(cand), maxs

    @staticmethod
    def _find_dy(ref, cur, fixed=None, max_shift=1600):
        """沉降帧精确对齐（参考 ShareX ScrollingCaptureManager.CombineImages 的
        「重叠区实心一致行」思路）：投影只负责粗排候选，判定靠「从重叠区底边
        向上数连续逐像素一致行」（±2 容差，悬浮固定件行通配），实心一致行不足
        的候选一律拒绝——平滑滚动的动画中间帧（分数像素位置渲染）凑不出实心
        一致行，天然被拒，文字重影与模糊由此消除。

        量程放宽到 1600px 且阈值随重叠区缩放：快速滚动（拍间隔内位移大）时
        重叠变小也能命中。返回滚动位移 dy（0 = 本帧不拼接）。"""
        return CaptureOverlay._find_dy2(ref, cur, fixed, max_shift)[0]

    @staticmethod
    def _find_dy2(ref, cur, fixed=None, max_shift=1600):
        """> _find_dy，同时返回实心一致行数作为方向仲裁的证据强度。"""
        H, W = cur.shape
        if ref.shape[0] != H or H < 80:
            return 0, 0
        if float(ref.std()) < 2.0:
            return 0, 0                    # 近匀质画面投影无处对齐，防随机错配
        cand, _maxs = CaptureOverlay._projection_candidates(ref, cur, max_shift)
        # 采样列带：避开左右边缘（滚动条/窗框），中段均匀取 4 带
        margin = max(8, min(W // 5, 60))
        span = max(1, W - 2 * margin)
        band = max(12, span // 8)
        starts = margin + span * np.arange(4) // 4
        cols = np.unique(np.concatenate(
            [np.arange(st, min(st + band, W - margin)) for st in starts]))
        best_s, best_run = 0, 0
        for s in cand:
            run = CaptureOverlay._match_run(cur, ref, s, cols, fixed)
            if run > best_run:
                best_run, best_s = run, s
                if run >= (H - s) * 3 // 4:    # 已证重叠区 3/4 实心一致，无需更优
                    break
        if best_s < 1 or best_run < max(10, (H - best_s) // 4):
            return 0, 0
        return best_s, best_run

    @staticmethod
    def _match_run(cur, ref, dy, cols, fixed=None):
        """重叠区底边向上数连续一致行：cur[r] 与 ref[r+dy] 在采样列带内全部
        逐像素一致（±2）才算一致行；fixed 行（悬浮固定件）按通配跳过。
        返回从底边起的实心一致行数。"""
        H = cur.shape[0]
        ov = H - dy                              # 重叠行数
        if ov <= 0:
            return 0
        eq = (np.abs(cur[:ov, cols] - ref[dy:H, cols]) <= 2).all(axis=1)
        if fixed is not None and fixed.shape[0] == H:
            eq = eq | fixed[:ov]
        mrev = eq[::-1]
        bad = np.flatnonzero(~mrev)
        return int(bad[0]) if bad.size else int(mrev.size)

    def _ls_step(self):
        if not self._longshot:
            return
        cur, cur_gray = self._grab_region()   # 选区是透明洞，抓到实时画面
        prev_gray = self._ls_prev_gray
        ref_gray = self._ls_ref_gray
        if cur_gray.shape != ref_gray.shape:
            self._ls_prev, self._ls_prev_gray = cur, cur_gray
            return
        settled = prev_gray is not None and prev_gray.shape == cur_gray.shape and \
            float(np.abs(cur_gray[::4, ::4] - prev_gray[::4, ::4]).mean()) < 0.3
        moved = float(np.abs(cur_gray[::4, ::4] - ref_gray[::4, ::4]).mean()) >= 0.3
        if settled:
            # PixPin 式节奏「停下来才合并」：页面停住后与参考帧实心对齐。
            # 快滚攒下的位移在沉降帧上一次补齐；对不齐（位移超量程/内容变化）
            # 就继续等，绝不硬拼。固定件掩码沿用移动阶段留下的提示——此时
            # cur==prev，现算会退化成全帧通配，错放分数位移帧。
            if moved:
                self._ls_align(cur, cur_gray, self._ls_fixed_hint)
        elif moved:
            # 滚动中：与参考帧实心可拼才动（离散跳变式快滚实时成段，实心对齐
            # 保证无拖影）；平滑滚动的动画帧（分数像素位置）拼不了就直接不算
            # ——宁丢勿糊，等沉降帧兜底
            self._ls_align(cur, cur_gray, self._static_fixed(cur_gray, prev_gray))
        if os.environ.get('SNAPPIN_DEBUG'):
            print('LS_STEP settled=', settled, flush=True)
        self._ls_prev, self._ls_prev_gray = cur, cur_gray

    def _ls_align(self, cur, cur_gray, fixed):
        """当前帧 vs 参考帧的双向实心对齐：向下滚拼尾带，向上滚裁尾回退
        （长图跟着视口缩回，再下滚重拼同一位置，来回滚动不产生重复）。
        两个方向都可能在对齐成功的门槛内找到候选（均匀背景区易出假匹配），
        按实心一致行数仲裁——真位移的整段实心证据必然压过假匹配。"""
        ref_gray = self._ls_ref_gray
        d_s, d_run = self._find_dy2(ref_gray, cur_gray, fixed=fixed)
        u_s, u_run = self._find_dy2(cur_gray, ref_gray, fixed=fixed)
        if d_run >= u_run:                    # 平手归向下：不无端缩回已拼内容
            if d_s >= 1:
                self._ls_commit(cur, cur_gray, d_s, fixed)
                return
        elif u_s >= 1:
            self._ls_trim(u_s, cur_gray)
            return
        if fixed is not None:
            self._ls_fixed_hint = fixed       # 给沉降合并的固定件提示

    def _ls_commit(self, cur, cur_gray, dy, fixed):
        """拼接确认：悬浮固定件消除 → 尾带入缓冲 → 前进参考帧 → 刷新预览。"""
        H = cur.shape[0]
        cur_out = cur
        if fixed is not None and fixed.any():
            # 悬浮固定件（回到底部↓/吸顶栏等）钉在视口不动，直接拼会沿长图
            # 重复盖章：填充消除后拼接，并同步擦掉首帧落在拼接图头部的盖章
            cur_out = self._fill_static(cur.copy(), fixed)
            if self._ls_len >= H and self._ls_buf is not None \
                    and self._ls_buf.shape[1] == cur.shape[1]:
                self._fill_static(self._ls_buf[:H], fixed)
        self._ls_append(cur_out[H - dy:])
        self._update_preview()
        self._ls_ref_gray = cur_gray          # 只有拼接成功才前进参考帧

    def _ls_trim(self, delta, cur_gray):
        """向上滚动回退：裁掉长图尾部 delta 行（参考帧内容=缓冲尾 H 行的
        不变式保持成立），参考帧换成当前帧——视口回到已拼接区，再下滚时
        重拼的是同一位置内容，来回滚动不会重复（用户实测去重缺陷的修复）。
        已到起点（上滚超出已捕获范围）时不裁不动，等滚回范围再续。"""
        H = cur_gray.shape[0]
        if delta > self._ls_len - H:
            return                            # 视口已高于捕获起点：等待回滚
        self._ls_len -= delta
        self._ls_stitched = self._ls_buf[:self._ls_len]
        self._ls_ref_gray = cur_gray
        self._update_preview(force=True)      # 长图变短，预览立即跟随

    def _ls_append(self, band):
        """新增行写入预分配缓冲（倍增扩容）；超出按 150MB 折算行数截断，
        保头部丢弃溢出——与旧 vstack 切断语义一致，但不再每帧全量拷贝。"""
        if self._ls_len >= self._ls_cap:
            return
        rows = self._ls_buf.shape[0]
        need = self._ls_len + band.shape[0]
        if need > rows:
            new_rows = min(self._ls_cap, max(rows * 2, need))
            buf = np.empty((new_rows, self._ls_buf.shape[1], 3), np.uint8)
            buf[:self._ls_len] = self._ls_buf[:self._ls_len]
            self._ls_buf = buf
        stop = min(need, self._ls_cap)
        self._ls_buf[self._ls_len:stop] = band[:stop - self._ls_len]
        self._ls_len = stop
        self._ls_stitched = self._ls_buf[:self._ls_len]

    @staticmethod
    def _static_fixed(cur_gray, prev_gray):
        """检测悬浮固定件：内容整体滚动时，与上一帧同位置几乎完全一致的行。
        （按钮/吸顶栏像素纹丝不动；滚动中的正文行不可能逐像素相同）"""
        if cur_gray.shape != prev_gray.shape:
            return np.zeros(cur_gray.shape[0], dtype=bool)
        same = (np.abs(cur_gray[:, ::2] - prev_gray[:, ::2]) <= 2).mean(axis=1)
        return same > 0.92

    @staticmethod
    def _fill_static(img, mask):
        """把 fixed 行用上下最近正常行线性过渡填充（上下文多为纯背景，过渡即还原）。"""
        m = np.convolve(mask.astype(np.uint8), np.ones(5, np.uint8), "same") > 0  # ±2 行膨胀盖住抗锯齿边
        H = img.shape[0]
        if not m.any() or m.sum() > H * 0.6:
            return img                                   # 全帧静止/占比过大：保守不动
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
        """节流：内容每长 ≥12 行才重渲染一次预览（全图缩放随长图越来越贵）。"""
        if self._preview is None or self._ls_stitched is None:
            return
        if not force and self._ls_preview_h and \
                self._ls_len - self._ls_preview_h < 12:
            return
        self._ls_preview_h = self._ls_len
        c = self._ls_stitched
        h, w = c.shape[:2]
        img = QImage(c.tobytes(), w, h, w * 3, QImage.Format_RGB888)
        self._preview.set_pix(QPixmap.fromImage(img))

    def _finish_longshot(self):
        if self._ls_stitched is None or self._ls_stitched.size == 0:
            self._exit_longshot()
            self.close()
            return
        c = self._ls_stitched
        h, w = c.shape[:2]
        img = QImage(c.tobytes(), w, h, w * 3, QImage.Format_RGB888)
        pix = QPixmap.fromImage(img)          # 彩色长图
        QApplication.clipboard().setPixmap(pix)
        self._exit_longshot()
        self.close()

    def _exit_longshot(self):
        if getattr(self, "_longshot", False):
            self._longshot = False
            if getattr(self, "_ls_timer", None):
                self._ls_timer.stop()
            self.clearMask()
            if self._preview:
                # 先摘回调再关窗：防覆盖层销毁后预览窗按钮仍触发旧槽
                self._preview.on_finish = None
                self._preview.on_cancel = None
                self._preview.close()
                self._preview = None
            # 释放拼接缓冲：长图可达百 MB 级，别等覆盖层销毁才回收
            self._ls_buf = None
            self._ls_stitched = None
            self._ls_len = 0
            self._ls_ref_gray = None
            self._ls_fixed_hint = None

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
        if getattr(self, "_longshot", False):
            # 边框画成紧贴洞口的实心色带（洞内被 mask 镂空，只有洞外可见）：
            # adjusted(-2)+宽4 → 恰好覆盖 [sel边缘, sel边缘+4]，滚动时始终可见
            p.setPen(QPen(QColor(ACCENT), 4))
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

    # ---- 悬浮固定件（回到底部↓/吸顶栏）：滚动中同位静止行应被填充,不再重复盖章
    H, W = 200, 60
    prev = ((np.arange(H)[:, None] % 251) * np.ones((1, W), np.int16)).astype(np.int16)
    prev[30:36] = 77                                  # ↓按钮：两帧同位置纹丝不动
    cur = np.roll(prev, -10, axis=0).copy()
    cur[30:36] = 77
    assert CaptureOverlay._find_dy(prev, cur) == 10, "存在固定件不应干扰位移检测"
    fixed = CaptureOverlay._static_fixed(cur, prev)
    assert fixed[30:36].all() and not fixed[100:110].any(), "应识别出按钮行、放过滚动正文行"
    col = np.repeat(cur[:, :, None], 3, axis=2).astype(np.uint8)
    out = CaptureOverlay._fill_static(col.copy(), fixed)
    assert not (out[28:38] == 77).any(), "悬浮按钮应被填充消除"
    assert (out[5] == col[5]).all(), "正常内容行必须保持原样"

    # ---- 沉降帧精确对齐：动画中间帧拒绝/沉降帧实心拼接/固定件通配/匀质防错配
    prof = np.tile(np.arange(400, dtype=np.int16)[:, None], (1, 90))
    assert CaptureOverlay._find_dy(prof, np.roll(prof, -1, axis=0)) == 1, \
        "慢速滚动 1px 也应被捕获"
    assert CaptureOverlay._find_dy(prof, np.roll(prof, -300, axis=0)) == 300, \
        "大步长滚动应在新量程内命中"
    blank = np.full((400, 90), 240, np.int16)
    assert CaptureOverlay._find_dy(blank, np.roll(blank, -50, axis=0)) == 0, \
        "匀质空白区投影无处对齐,不应随机错配污染长图"
    rng = np.random.default_rng(7)
    tex = rng.integers(0, 255, (400, 90), dtype=np.int16)
    mid = (np.roll(tex, -7, axis=0) + np.roll(tex, -8, axis=0)) // 2   # 7.5px 中间态
    assert CaptureOverlay._find_dy(tex, mid) == 0, \
        "平滑滚动动画中间帧(分数像素错位)必须整帧拒绝——文字重影的根源"
    assert CaptureOverlay._find_dy(tex, np.roll(tex, -15, axis=0)) == 15, \
        "沉降帧应按真实位移实心拼接"
    cur_fx = np.roll(tex, -20, axis=0).copy()
    cur_fx[375] = 123                                   # 悬浮按钮钉在视口原位
    assert CaptureOverlay._find_dy(tex, cur_fx) == 0, \
        "固定件行阻断实心一致时应拒绝整帧(保守不拼)"
    fx = np.zeros(400, bool); fx[375] = True
    assert CaptureOverlay._find_dy(tex, cur_fx, fixed=fx) == 20, \
        "固定件行作通配后应按真实位移拼接"
    assert CaptureOverlay._find_dy(tex, np.roll(tex, -339, axis=0)) == 339, \
        "快滚大位移应在 1600 量程内实心命中(重叠 61 行仍可判定)"

    # ---- 预分配拼接缓冲：倍增扩容/首帧保留/追加落位
    ov4 = CaptureOverlay(app.primaryScreen(), config.load())
    ov4._sel = QRect(40, 40, 300, 200)
    ov4._enter_longshot()
    H0 = ov4._ls_prev.shape[0]
    band = np.full((50, ov4._ls_prev.shape[1], 3), 200, np.uint8)
    for _ in range(6):
        ov4._ls_append(band)
    assert ov4._ls_len == H0 + 300 and ov4._ls_buf.shape[0] >= H0 + 300, \
        "缓冲应按需倍增扩容"
    assert (ov4._ls_stitched[:H0] == ov4._ls_prev).all(), "拼接图首帧必须保留"
    assert (ov4._ls_stitched[-50:] == 200).all(), "追加行必须正确落位"

    # ---- 预览节流：无新增行不重渲染，新增 ≥24 行才刷新
    ov4._update_preview(force=True)                   # 同步预览水位到当前长度
    called = []
    ov4._preview.set_pix = lambda p: called.append(1)
    ov4._update_preview()
    assert len(called) == 0, "无新增行不应重渲染预览"
    ov4._ls_append(band[:30])
    ov4._update_preview()
    assert len(called) == 1, "新增行达到阈值应刷新预览"
    ov4._exit_longshot()

    # ---- 上限截断：保头部、丢溢出（与旧 vstack 语义一致），满后再拼整体丢弃
    ov5 = CaptureOverlay(app.primaryScreen(), config.load())
    ov5._sel = QRect(40, 40, 300, 200)
    ov5._enter_longshot()
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

    # ---- 多拍流程：参考帧持久化——动画帧拒绝不丢内容，沉降后按真实位移补齐
    ov6 = CaptureOverlay(app.primaryScreen(), config.load())
    ov6._sel = QRect(40, 40, 300, 200)
    ov6._enter_longshot()
    H6, W6 = ov6._ls_prev.shape[:2]
    base = rng.integers(0, 255, (H6, W6), dtype=np.int16)
    ov6._ls_ref_gray = base.copy()        # 换假参考帧（缓冲内容与本逻辑无关）
    seq = {"mid": (base + np.roll(base, -10, axis=0)) // 2,   # 动画中间帧
           "B": np.roll(base, -40, axis=0),                    # 沉降：滚 40
           "C": np.roll(base, -25, axis=0),                    # 回滚 15（应忽略）
           "D": np.roll(base, -70, axis=0)}                    # 再滚 30（B→D）
    holder = {"frame": seq["mid"]}
    ov6._grab_region = lambda: (np.stack([holder["frame"].astype(np.uint8)] * 3, axis=2),
                                holder["frame"].copy())
    n0 = ov6._ls_len
    ov6._ls_step()                        # 动画帧：拒绝
    assert ov6._ls_len == n0 and (ov6._ls_ref_gray == base).all(), \
        "动画中间帧应被拒绝且参考帧不前进"
    holder["frame"] = seq["B"]
    ov6._ls_step()                        # 沉降帧：一次补齐 40 行
    assert ov6._ls_len == n0 + 40, f"沉降帧应一次补齐 40 行, 实际 +{ov6._ls_len - n0}"
    assert (ov6._ls_ref_gray == seq["B"]).all(), "拼接成功后参考帧应前进到 B"
    holder["frame"] = seq["C"]
    ov6._ls_step()                        # 回滚 15：裁尾回退（新语义，不再忽略）
    assert ov6._ls_len == n0 + 25 and (ov6._ls_ref_gray == seq["C"]).all(), \
        "向上回滚应裁尾回退"
    holder["frame"] = seq["B"]
    ov6._ls_step()                        # 回到 40：重拼同一位置
    assert ov6._ls_len == n0 + 40, "下滚重拼不得产生重复内容"
    holder["frame"] = seq["D"]
    ov6._ls_step()                        # 再滚 30 行
    assert ov6._ls_len == n0 + 70, f"再次滚动应续拼 30 行, 实际 +{ov6._ls_len - n0 - 40}"
    # PixPin 式「宁丢勿糊」：分数位移动画帧拼不了就直接不算，等沉降帧兜底
    fracF = (np.roll(base, -90, axis=0) * 3 + np.roll(base, -91, axis=0)) // 4
    holder["frame"] = fracF
    ov6._ls_step()
    assert ov6._ls_len == n0 + 70, "分数位移动画帧应直接丢弃(不硬拼防拖影)"
    # 位移超量程的沉降帧：等待不硬拼；可对齐的帧一到立刻恢复续拼
    far = np.roll(base, -(70 + H6), axis=0)
    holder["frame"] = far
    ov6._ls_step()                        # 第一拍：移动分支，超量程丢弃
    ov6._ls_step()                        # 第二拍：settled，仍对不齐 → 继续等
    assert ov6._ls_len == n0 + 70, "超量程沉降帧应等待而非硬拼"
    holder["frame"] = np.roll(base, -110, axis=0)
    ov6._ls_step()                        # 对齐恢复：D→+40
    assert ov6._ls_len == n0 + 110, "可对齐帧到达应恢复续拼"

    # ---- 上滚回退(用户实测去重缺陷)：上滚 85 裁尾，下滚 45 重拼同一位置
    U = np.roll(base, -25, axis=0)        # 视口 25，参考帧在 110 → 上滚 85
    holder["frame"] = U
    ov6._ls_step()
    assert ov6._ls_len == n0 + 25 and (ov6._ls_ref_gray == U).all(), \
        "上滚应裁掉长图尾部并前进参考帧，而非重复拼接"
    D2 = np.roll(base, -70, axis=0)       # 下滚到 70：相对 U(25) 位移 45
    holder["frame"] = D2
    ov6._ls_step()
    assert ov6._ls_len == n0 + 70 and (ov6._ls_ref_gray == D2).all(), \
        "上滚后下滚应重拼同一位置"
    snap = ov6._ls_stitched[H6:].copy()
    for _ in range(3):                    # 反复上下滚：长度与内容必须纹丝不动
        holder["frame"] = U
        ov6._ls_step()
        assert ov6._ls_len == n0 + 25, "上滚必须回退而非重复拼接"
        holder["frame"] = D2
        ov6._ls_step()
        assert ov6._ls_len == n0 + 70, "下滚重拼不得增长出重复内容"
    assert (ov6._ls_stitched[H6:] == snap).all(), "来回滚动不得改变已拼内容"

    ov6._exit_longshot()
    assert ov6._ls_stitched is None and ov6._ls_ref_gray is None, \
        "退出应同时释放参考帧"
    print("capture 自测通过: 惰性马赛克/工具栏/预暗化绘制/长截图守卫收尾/彩色拼接/"
          "固定件消除/沉降帧对齐/PixPin式宁丢勿糊/缓冲扩容截断/预览节流 全部 OK")
