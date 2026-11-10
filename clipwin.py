"""Ditto 风格剪贴板历史快贴窗口 — 无边框置顶小窗（仿 Ditto 布局）。

结构：细标题栏（计数）/ 编号列表（常用项以图钉替代序号）/ 底部搜索栏（••• 菜单）/
右侧竖向工具栏（✕ 关闭 + 🔒 锁定 + 📌 常用 + 🔑 密码，空白双击锁定，底部锁定指示）。
键盘：输入即过滤 · ↓ 进列表 · Enter 粘贴当前行（密码行默认贴密码）· Del 删除 ·
1-9 快贴对应行 · Esc 关闭。锁定态下 Ctrl+V 发给弹出前的前台窗口，本窗口保持显示。
"""
import os
import time
import ctypes
from ctypes import wintypes

from PySide6.QtCore import (QByteArray, QEvent, QMimeData, QPoint, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QCursor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QToolButton,
                               QVBoxLayout, QWidget)

import config
from clip import ClipItem, ClipStore

ACCENT = "#2E7CF6"
_ELIDE_W = 230          # 预览文本省略宽度（窗口定宽，按可用宽度估算）

QSS = """
#ClipWin{background:white;border:1px solid #DADDE4;}
#ClipTop{background:white;border-bottom:1px solid #EFEFF2;}
#ClipTitle{color:#4E5969;font-size:12px;background:transparent;}
QToolButton#SideBtn{border:none;border-radius:6px;background:transparent;padding:2px;}
QToolButton#SideBtn:hover{background:#E9EDF4;}
QToolButton#SideBtn[active="true"]{background:#EAF2FF;}
#ClipList{background:white;border:none;outline:0;font-size:13px;}
#ClipList::item{border-radius:6px;margin:1px 4px;}
#ClipList::item:hover{background:#F2F7FF;}
#ClipList::item:selected{background:#EAF2FF;}
#ClipBottom{background:#FAFBFC;border-top:1px solid #E5E6EB;}
#ClipSearch{border:1px solid #E5E6EB;border-radius:14px;background:white;
            padding:4px 10px 4px 28px;font-size:12px;}
#ClipSearch:focus{border-color:%ACC%;}
QLabel[l1]{font-size:13px;color:#1D2129;background:transparent;}
QLabel[l2]{font-size:11px;color:#9AA0AC;background:transparent;}
QLabel[num]{font-size:11px;color:#A8AEBB;background:transparent;}
QLabel[pwline]{font-size:13px;color:#1D2129;background:transparent;
               border-radius:4px;padding:2px 6px;}
QLabel[pwline]:hover{background:#EAF2FF;}
QLabel[pwdim]{font-size:13px;color:#A8AEBB;background:transparent;
              border-radius:4px;padding:2px 6px;}
QToolButton#DotsBtn{border:none;border-radius:6px;background:transparent;padding:3px;}
QToolButton#DotsBtn:hover{background:#F2F3F5;}
""".replace("%ACC%", ACCENT)

_MENU_QSS = ("QMenu{background:white;border:1px solid #E5E6EB;border-radius:8px;padding:4px;}"
             "QMenu::item{padding:6px 30px;border-radius:5px;}"
             "QMenu::item:selected{background:#EAF2FF;color:%ACC%;}").replace("%ACC%", ACCENT)

# ------------------------------------------------ 线性 SVG 图标（与 icons.py 同风格）
_SVG_LOCKED = ('<rect x="5.5" y="10.5" width="13" height="9" rx="2"/>'
               '<path d="M8.5 10.5 V8 a3.5 3.5 0 0 1 7 0 v2.5 M12 14 v3"/>')
_SVG_UNLOCKED = ('<rect x="5.5" y="10.5" width="13" height="9" rx="2"/>'
                 '<path d="M8.5 10.5 V8 a3.5 3.5 0 0 1 7 -.8"/>')
_SVG_CLOSE = '<path d="M6 6 L18 18 M18 6 L6 18"/>'
_SVG_DOTS = '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>'
_SVG_SEARCH = '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15 L20 20"/>'
_SVG_IMG = ('<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/>'
            '<path d="M4.5 17 l4.5-4.5 3 3 3.5-3.5 4 4"/>')
_SVG_FILE = '<path d="M7 3.5 h7 l4 4 V20.5 H7 z M14 3.5 V7.5 h4"/>'
_SVG_TEXT = '<path d="M5 6 V4.5 h14 V6 M12 4.5 V20 M9 20 h6"/>'
_SVG_BOX = '<rect x="4.5" y="6.5" width="15" height="12" rx="1.5"/><path d="M4.5 10.5 h15 M9.5 6.5 v4"/>'
_SVG_PIN = ('<path d="M9 3 v6.5 L5.5 14 v1.5 h13 V14 L15 9.5 V3 M9 3 h6 M12 15.5 V21"/>')
_SVG_KEY = ('<circle cx="8" cy="12" r="3.5"/>'
            '<path d="M11.5 12 H20 M16.5 12 v3 M19.5 12 v2.5"/>')


def _svg_icon(body: str, color: str = "#4E5969", size: int = 18, stroke: float = 1.5) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
           f'stroke-linejoin="round">{body}</svg>')
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


# ------------------------------------------------ SendInput 模拟 Ctrl+V
_VK_CTRL, _VK_V, _KEYEVENTF_KEYUP = 0x11, 0x56, 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT))
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _U))


def _send_ctrl_v():
    """SendInput 模拟 Ctrl+V（按下/抬起严格配对）。"""
    try:
        seq = ((_VK_CTRL, 0), (_VK_V, 0), (_VK_V, _KEYEVENTF_KEYUP), (_VK_CTRL, _KEYEVENTF_KEYUP))
        arr = (_INPUT * len(seq))()
        for i, (vk, fl) in enumerate(seq):
            arr[i].type = 1  # INPUT_KEYBOARD
            arr[i].ki = _KEYBDINPUT(vk, 0, fl, 0, None)
        ctypes.windll.user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
    except Exception:
        pass


def _focus_target(hwnd: int) -> bool:
    """把目标窗口切到前台，并核实前台真的变成了它。

    SetForegroundWindow 会被系统静默拒绝（返回非零但前台未变），Ctrl+V
    就会发进自己的搜索框——锁定态“单击没反应”的根因。AttachThreadInput
    挂到目标线程输入队列可绕过前台限制，最后必须以 GetForegroundWindow 为准。
    """
    if not hwnd:
        return False
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        if not u32.IsWindow(hwnd) or u32.IsIconic(hwnd):
            return False
        if u32.GetForegroundWindow() == hwnd:
            return True
        this_tid = k32.GetCurrentThreadId()
        tids = []
        for w in (u32.GetForegroundWindow(), hwnd):
            tid = u32.GetWindowThreadProcessId(w, None)
            if tid and tid != this_tid and tid not in tids:
                tids.append(tid)
        attached = [tid for tid in tids if u32.AttachThreadInput(this_tid, tid, True)]
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            for tid in attached:
                u32.AttachThreadInput(this_tid, tid, False)
        return u32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def _sanitize(s: str) -> str:
    """预览文本清洗：去控制字符（乱码来源），空白折叠。"""
    s = (s or "").replace("\x00", "")
    out = []
    for ch in s:
        if ch in "\r\n\t":
            out.append(" ")
        elif ch.isprintable():
            out.append(ch)
    return "".join(out).strip()


def _trunc(s: str, n: int) -> str:
    s = _sanitize(s)
    return s[:n] + ("…" if len(s) > n else "")


def _dw_trunc(s: str, width: int = 42) -> str:
    """按显示宽度截断（CJK 记 2 列），避免密码行撑出横向滚动条。"""
    s = _sanitize(s)
    acc = 0
    for i, ch in enumerate(s):
        acc += 2 if ord(ch) > 0x2E80 else 1
        if acc > width:
            return s[:i] + "…"
    return s


class _SideBar(QWidget):
    """右侧竖向工具栏：✕ 关闭 / 🔒 锁定 / 📌 常用 / 🔑 密码 四键 + 空白双击锁定 + 底部状态指示。"""
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._locked = False
        self.setFixedWidth(36)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 8)
        v.setSpacing(4)

        def _btn(icon_body, color, tip):
            b = QToolButton()
            b.setObjectName("SideBtn")
            b.setIcon(_svg_icon(icon_body, color, 16))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(28, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(tip)
            return b

        self.btn_close = _btn(_SVG_CLOSE, "#4E5969", "关闭 (Esc)")
        self.btn_lock = _btn(_SVG_UNLOCKED, "#9AA0AC",
                             "锁定 / 解锁：锁定后单击粘贴时窗口保持打开\n（双击侧栏空白处同样可切换）")
        self.btn_pins = _btn(_SVG_PIN, "#9AA0AC", "只看常用项 📌（再点一次返回全部）")
        self.btn_pw = _btn(_SVG_KEY, "#9AA0AC", "密码库 🔑：列出全部条目，点行粘贴（再点一次返回）")
        v.addWidget(self.btn_close, 0, Qt.AlignHCenter)
        v.addWidget(self.btn_lock, 0, Qt.AlignHCenter)
        v.addWidget(self.btn_pins, 0, Qt.AlignHCenter)
        v.addWidget(self.btn_pw, 0, Qt.AlignHCenter)
        v.addStretch(1)

    def set_locked(self, locked: bool):
        self._locked = locked
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.double_clicked.emit()
            e.accept()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FAFBFC"))
        p.fillRect(0, 0, 1, self.height(), QColor("#ECEEF2"))  # 左分隔线
        # 底部锁定状态指示条（锁定 = 主题蓝）
        color = QColor(ACCENT) if self._locked else QColor("#D5DAE3")
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(self.width() // 2 - 2, self.height() - 56, 4, 46, 2, 2)


class _HintList(QListWidget):
    """空列表时绘制居中提示；键盘：Enter 粘贴 / Del 删除 / 1-9 快贴 / Esc 关闭。"""
    paste_row = Signal(int)
    delete_row = Signal(int)
    esc_pressed = Signal()
    focus_search = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hint = "暂无剪贴板记录"

    def keyPressEvent(self, e):
        k = e.key()
        row = self.currentRow()
        if k in (Qt.Key_Return, Qt.Key_Enter):
            if self.count():
                self.paste_row.emit(max(0, row))
            return
        if k == Qt.Key_Delete and row >= 0:
            self.delete_row.emit(row)
            return
        if Qt.Key_1 <= k <= Qt.Key_9 and (k - Qt.Key_1) < self.count():
            n = k - Qt.Key_1
            self.setCurrentRow(n)
            self.paste_row.emit(n)
            return
        if k == Qt.Key_Escape:
            self.esc_pressed.emit()
            return
        if k == Qt.Key_Up and row <= 0:
            self.focus_search.emit()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor("#A8AEBB"))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, self.hint)


class _PreviewTip(QLabel):
    """图片条目悬停缩略图浮窗（稍大于行内缩略图，无边框 tooltip 窗口）。"""

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setStyleSheet("QLabel{background:white;border:1px solid #DADDE4;"
                           "border-radius:6px;padding:3px;}")
        self.hide()


class _ClickLabel(QLabel):
    """可点击行标签 — 消费鼠标事件（不冒泡成 itemClicked），悬停高亮由 QSS 提供。"""
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
            e.accept()
        else:
            super().mousePressEvent(e)


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pw_line(prefix: str, value: str, clickable: bool = True) -> QLabel:
    """密码条目的一行：灰色前缀(账号/密码) + 值；clickable=False 用于空账号占位。"""
    lab = _ClickLabel() if clickable else QLabel()
    lab.setProperty("pwline" if clickable else "pwdim", True)
    lab.setTextFormat(Qt.RichText)
    lab.setText(f'<span style="color:#9AA0AC;font-size:11px;">{prefix}</span>'
                f'&nbsp;&nbsp;{_html_escape(_dw_trunc(value, 42))}')
    lab.setMinimumHeight(26)     # 保证两行完整显示（旧行高过矮导致文本被裁）
    return lab


class ClipWindow(QWidget):
    """剪贴板历史快贴窗口：单击条目回放剪贴板并模拟 Ctrl+V。

    搜索非空时，还会按 名称/账号 搜密码库（pw_provider 惰性返回 PasswordStore）：
    命中条目以「账号 / 密码」两行展示，点哪行粘哪行；密码默认掩码，右键可显示。
    """

    def __init__(self, store: ClipStore, cfg: dict, monitor=None, on_settings=None,
                 pw_provider=None, on_pwman=None):
        super().__init__(None)
        self._store = store
        self._cfg = cfg
        self._monitor = monitor
        self._on_settings = on_settings
        self._pw_provider = pw_provider   # callable() -> PasswordStore | None
        self._on_pwman = on_pwman         # callable() 打开密码管理窗口
        self._pw_revealed: set = set()    # 已显示明文的密码条目 id（刷新后仍保持）
        self._mode = "all"                # all / pins / pw（右侧栏切换）
        self._search = ""
        self._items: list[ClipItem] = []
        self._pw_items: list[dict] = []
        self._dims: dict = {}          # clip_id -> 图片原始尺寸缓存
        self._drag_off: QPoint | None = None
        self._last_paste = 0.0
        self._target_hwnd = 0          # popup 前的前台窗口：锁定态粘贴目标
        self._tip_item = None          # 悬停预览当前展示的行（防重复弹出闪烁）

        cc = cfg.setdefault("clip", {})  # config.py 默认没有 clip 段，就地补齐
        cc.setdefault("popup_pos", "cursor")
        cc.setdefault("pin_stay", False)
        cc.setdefault("retention_days", 30)
        cc.setdefault("max_entries", 500)
        store.retention_days = int(cc["retention_days"] or 0)  # store 清理参数跟随全局配置
        store.max_entries = int(cc["max_entries"] or 0)
        self._locked = bool(cc["pin_stay"])

        self.setObjectName("ClipWin")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setFixedSize(360, 500)
        self.setWindowTitle("剪贴板历史")
        self.setStyleSheet(QSS)

        # 根布局：主列 + 右侧锁定边条
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        root.addWidget(main, 1)
        self.edge = _SideBar()
        self.edge.double_clicked.connect(self._toggle_lock)
        self.edge.btn_lock.clicked.connect(self._toggle_lock)
        self.edge.btn_close.clicked.connect(self.hide)
        self.edge.btn_pins.clicked.connect(lambda: self._set_mode("pins"))
        self.edge.btn_pw.clicked.connect(lambda: self._set_mode("pw"))
        self.edge.btn_pw.setVisible(self._pw_provider is not None)
        root.addWidget(self.edge)

        col = QVBoxLayout(main)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # ---- 顶部细标题栏：标题计数（关闭/锁定按钮在右侧工具栏）
        top = QWidget()
        top.setObjectName("ClipTop")
        h = QHBoxLayout(top)
        h.setContentsMargins(10, 6, 8, 6)
        h.setSpacing(4)
        self.title = QLabel("剪贴板历史")
        self.title.setObjectName("ClipTitle")
        h.addWidget(self.title, 1)
        col.addWidget(top)

        # ---- 编号列表
        self.list = _HintList()
        self.list.setObjectName("ClipList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setIconSize(QSize(36, 36))
        self.list.setMouseTracking(True)  # 悬停预览需要 itemEntered/MouseMove
        self.list.itemClicked.connect(lambda it: self._paste(it.data(Qt.UserRole)))
        self.list.itemDoubleClicked.connect(lambda it: self._paste(it.data(Qt.UserRole)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.paste_row.connect(self._kb_paste)
        self.list.delete_row.connect(self._kb_delete)
        self.list.esc_pressed.connect(self.hide)
        self.list.focus_search.connect(lambda: self.search.setFocus())
        col.addWidget(self.list, 1)

        # ---- 底部搜索栏：🔍 搜索框 + ••• 菜单（Ditto 式置底）
        bottom = QWidget()
        bottom.setObjectName("ClipBottom")
        b = QHBoxLayout(bottom)
        b.setContentsMargins(10, 7, 6, 7)
        b.setSpacing(6)
        self.search = QLineEdit()
        self.search.setObjectName("ClipSearch")
        self.search.setPlaceholderText("搜索")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        self.search.installEventFilter(self)  # ↓/Enter 键盘导航
        self._tip = _PreviewTip()
        self.list.itemEntered.connect(self._on_item_entered)
        self.list.viewport().installEventFilter(self)  # 鼠标离开收起预览
        # 搜索框内嵌放大镜图标（左内边距已留 28px）
        self.search.addAction(_svg_icon(_SVG_SEARCH, "#9AA0AC", 14), QLineEdit.LeadingPosition)
        b.addWidget(self.search, 1)
        dots = QToolButton()
        dots.setObjectName("DotsBtn")
        dots.setIcon(_svg_icon(_SVG_DOTS, "#4E5969", 16))
        dots.setCursor(Qt.PointingHandCursor)
        dots.setFocusPolicy(Qt.NoFocus)
        dots.setToolTip("更多操作")
        dots.setMenu(self._build_menu())
        dots.setPopupMode(QToolButton.InstantPopup)
        b.addWidget(dots)
        col.addWidget(bottom)

        self._sync_lock_btn()
        self.refresh()

    # ------------------------------------------------ 弹出
    def popup(self):
        """热键 toggle：已显示则隐藏；否则按 popup_pos 定位并刷新列表。"""
        if self.isVisible():
            self.hide()
            return
        self.refresh()
        self.search.clear()
        mode = self._cfg["clip"].get("popup_pos", "cursor")
        if mode == "center":
            g = QApplication.primaryScreen().availableGeometry()
            self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2)
        elif mode == "remember" and self._cfg["clip"].get("last_pos"):
            p = QPoint(int(self._cfg["clip"]["last_pos"][0]), int(self._cfg["clip"]["last_pos"][1]))
            g = (QApplication.screenAt(p) or QApplication.primaryScreen()).availableGeometry()
            p.setX(min(max(p.x(), g.left()), max(g.left(), g.right() - self.width() + 1)))
            p.setY(min(max(p.y(), g.top()), max(g.top(), g.bottom() - self.height() + 1)))
            self.move(p)
        else:  # cursor：鼠标附近右下弹出，夹在屏内
            p = QCursor.pos()
            g = (QApplication.screenAt(p) or QApplication.primaryScreen()).availableGeometry()
            self.move(min(max(g.left(), p.x()), g.right() - self.width() + 1),
                      min(max(g.top(), p.y()), g.bottom() - self.height() + 1))
        try:  # 记下弹出前的前台窗口：锁定态粘贴时 Ctrl+V 发给它
            self._target_hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            self._target_hwnd = 0
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()

    # ------------------------------------------------ 列表
    def refresh(self):
        self._tip.hide()
        self._tip_item = None
        self.list.clear()
        self._pw_items = []
        self._items = []
        if self._mode == "pw":
            self._pw_items = self._query_pw(self._search, allow_all=True)
            for rec in self._pw_items:
                it, w = self._make_pw_item(rec)
                self.list.addItem(it)
                self.list.setItemWidget(it, w)
            self.title.setText(f"密码 · {len(self._pw_items)} 条")
            self.list.hint = "密码库为空" if not self._search else "无匹配的密码条目"
        else:
            self._items = self._store.query(self._search, limit=200)
            if self._mode == "pins":
                self._items = [ci for ci in self._items if ci.pinned]
            if self._mode == "all":
                self._pw_items = self._query_pw(self._search)
            for rec in self._pw_items:                 # 密码条目置顶（搜索时才有）
                it, w = self._make_pw_item(rec)
                self.list.addItem(it)
                self.list.setItemWidget(it, w)
            for i, ci in enumerate(self._items):
                it, w = self._make_item(ci, i + 1)
                self.list.addItem(it)  # it 不带文本：行内容全部由行 widget 绘制，杜绝重影
                self.list.setItemWidget(it, w)
            days = self._cfg["clip"].get("retention_days", 30)
            tip = "永久保留" if not days or days <= 0 else f"保留 {days} 天"
            if self._mode == "pins":
                head = f"常用项 · {len(self._items)} 条"
            else:
                head = f"剪贴板历史 · {len(self._items)} 条"
                if self._pw_items:
                    head += f" + 密码 {len(self._pw_items)} 条"
            self.title.setText(f"{head} · {tip}")
            if self._search:
                self.list.hint = "无匹配结果"
            elif self._mode == "pins":
                self.list.hint = "还没有常用项（右键条目 → 设为常用）"
            else:
                self.list.hint = "暂无剪贴板记录"
        self.list.scrollToTop()           # 每次刷新（含打开时）定位到最上面

    def _query_pw(self, search: str, allow_all: bool = False) -> list[dict]:
        """搜索词非空时查密码库（quick_search 仅解密命中行）；密码模式空搜索列出全部。"""
        kw = (search or "").strip()
        if self._pw_provider is None:
            return []
        if not kw and not allow_all:
            return []
        try:
            store = self._pw_provider()
        except Exception:
            return []
        if store is None:
            return []
        try:
            if kw:
                return store.quick_search(kw, limit=10)
            return store.query("")[:50]
        except Exception:
            return []

    def _on_search(self, text: str):
        self._search = text
        self.refresh()

    def _make_item(self, ci: ClipItem, num: int):
        """构造一行：序号(常用项→图钉) + 类型图标/缩略图 + 两行文本。"""
        fm = self.list.fontMetrics()
        if ci.files:  # 文件
            line1 = _trunc(", ".join(os.path.basename(f) for f in ci.files), 60)
            icon = _svg_icon(_SVG_FILE, "#6AA1F8", 36, 1.4)
        elif ci.has_image:  # 图片
            dims = self._image_dims(ci)
            line1 = f"图片 {dims[0]}x{dims[1]}" if dims else "图片"
            icon = None  # 用缩略图
        elif _sanitize(ci.text):  # 文本
            line1 = _trunc(ci.text.strip().splitlines()[0] if ci.text.strip() else "", 60)
            icon = _svg_icon(_SVG_TEXT, "#7BB89A", 36, 1.4)
        else:  # 其他格式
            line1 = _trunc("、".join(ci.fmt_names[:3]), 60)
            icon = _svg_icon(_SVG_BOX, "#B3BAC8", 36, 1.4)
        line2 = time.strftime("%m-%d %H:%M", time.localtime(ci.ts))
        if ci.source_app:
            line2 += f" · {ci.source_app}"

        it = QListWidgetItem()                     # 文本留空 → 只渲染行 widget，无重影
        it.setData(Qt.UserRole, ci)
        if ci.has_image and ci.thumb_png:          # 图片项用缩略图做图标（行内小图）
            img = QImage.fromData(QByteArray(ci.thumb_png))
            if not img.isNull():
                pm = QPixmap.fromImage(img).scaled(36, 36, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                icon = QIcon(pm)
                it.setData(Qt.UserRole + 1, bytes(ci.thumb_png))  # 悬停弹出稍大预览

        # 行 widget：序号/图钉 + 图标 + 两行文本
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(8, 6, 6, 6)
        h.setSpacing(8)
        num_lab = QLabel()
        num_lab.setProperty("num", True)
        num_lab.setFixedWidth(22)
        num_lab.setAlignment(Qt.AlignCenter)
        if ci.pinned:  # 常用项：图钉替代序号
            num_lab.setPixmap(_svg_icon(_SVG_PIN, ACCENT, 14, 1.7).pixmap(14, 14))
        else:
            num_lab.setText(str(num))
        h.addWidget(num_lab)
        if icon is not None:
            ic = QLabel()
            ic.setPixmap(icon.pixmap(36, 36))
            ic.setFixedSize(36, 36)
            ic.setAlignment(Qt.AlignCenter)
            h.addWidget(ic)
        v = QVBoxLayout()
        v.setSpacing(1)
        la = QLabel(fm.elidedText(line1, Qt.ElideRight, _ELIDE_W))
        la.setProperty("l1", True)
        lb = QLabel(fm.elidedText(line2, Qt.ElideRight, _ELIDE_W))
        lb.setProperty("l2", True)
        v.addWidget(la)
        v.addWidget(lb)
        h.addLayout(v, 1)
        it.setSizeHint(w.sizeHint())
        return it, w

    def _make_pw_item(self, rec: dict):
        """密码条目行：左侧 🔑 图标，右侧「账号 / 密码」两行，各自点击粘贴。"""
        it = QListWidgetItem()                     # 文本留空，杜绝重影
        it.setData(Qt.UserRole, ("pw", rec))
        it.setToolTip(f"{rec['title'] or rec['username']}\n点击「账号」行粘贴账号，点击「密码」行粘贴密码")

        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(8, 8, 6, 8)
        h.setSpacing(8)
        key = QLabel()
        key.setPixmap(_svg_icon(_SVG_KEY, ACCENT, 15, 1.7).pixmap(15, 15))
        key.setFixedSize(22, 58)
        key.setAlignment(Qt.AlignCenter)
        h.addWidget(key)
        v = QVBoxLayout()
        v.setSpacing(4)
        # 第一行：名称 · 账号 → 点击粘贴账号（无账号则灰显不可点）
        name = _sanitize(rec["title"])
        acct = _sanitize(rec["username"])
        line1_val = f"{name} · {acct}" if (name and acct) else (acct or name)
        if acct:
            la = _pw_line("账号", line1_val)
            la.clicked.connect(lambda r=rec: self._paste_pw(r, "username"))
        else:
            la = _pw_line("账号", "—", clickable=False)
        v.addWidget(la)
        # 第二行：密码（默认掩码，右键可显示明文）→ 点击粘贴密码
        shown = _sanitize(rec["password"]) if rec["id"] in self._pw_revealed \
            else "•" * 8
        lb = _pw_line("密码", shown or "—", clickable=bool(rec["password"]))
        if rec["password"]:
            lb.clicked.connect(lambda r=rec: self._paste_pw(r, "password"))
        v.addWidget(lb)
        h.addLayout(v, 1)
        it.setSizeHint(w.sizeHint())
        return it, w

    def _image_dims(self, ci: ClipItem):
        """图片原始尺寸（QImage 解码，带缓存；解不出返回 None）。"""
        if ci.id in self._dims:
            return self._dims[ci.id]
        dims = None
        imgs = sorted(((k, v) for k, v in self._store.get_payload(ci.id)["formats"].items()
                       if k.startswith("image/") or k == "application/x-qt-image"),
                      key=lambda kv: -len(kv[1]))
        for _, data in imgs:
            img = QImage.fromData(QByteArray(data))
            if not img.isNull():
                dims = (img.width(), img.height())
                break
        self._dims[ci.id] = dims
        return dims

    # ------------------------------------------------ 回放 / 粘贴
    def _replay(self, ci: ClipItem):
        """把条目回放到系统剪贴板（不粘贴、不隐藏）。"""
        if self._monitor is not None and hasattr(self._monitor, "suppress_next"):
            self._monitor.suppress_next(2)  # 自己写剪贴板不重新记录
        payload = self._store.get_payload(ci.id)
        md = QMimeData()
        for fmt, data in payload["formats"].items():
            if data:
                md.setData(fmt, QByteArray(bytes(data)))
        png = payload["formats"].get("image/png")
        if png:
            img = QImage.fromData(QByteArray(png))
            if not img.isNull():
                md.setImageData(img)
        QApplication.clipboard().setMimeData(md)

    def _paste(self, ci: ClipItem):
        if not isinstance(ci, ClipItem):
            return  # 密码条目：点击由行内 _ClickLabel 处理，不走整行回放
        now = time.time()
        if now - self._last_paste < 0.3:
            return  # 单击+双击防呆：短时间只回放一次
        self._last_paste = now
        self._replay(ci)
        self._send_paste()

    # ------------------------------------------------ 键盘操作
    def _kb_paste(self, row: int):
        """回车/数字快贴：粘贴指定行；密码条目默认贴密码。"""
        it = self.list.item(row)
        if it is None:
            return
        data = it.data(Qt.UserRole)
        if isinstance(data, tuple) and len(data) == 2 and data[0] == "pw":
            self._paste_pw(data[1], "password")
        else:
            self._paste(data)

    def _kb_delete(self, row: int):
        """Del：删除当前剪贴板条目（密码条目请在密码管理窗口删除）。"""
        it = self.list.item(row)
        if it is None:
            return
        data = it.data(Qt.UserRole)
        if isinstance(data, tuple) and len(data) == 2 and data[0] == "pw":
            return
        self._delete(data)

    def _on_item_entered(self, item):
        """悬停图片条目 → 光标附近弹出稍大的缩略图（≤160px，无大图窗）。"""
        self._show_tip(item)

    def _show_tip(self, item):
        png = item.data(Qt.UserRole + 1)
        if not png:
            self._tip.hide()
            self._tip_item = None
            return
        img = QImage.fromData(QByteArray(png))
        if img.isNull():
            self._tip.hide()
            self._tip_item = None
            return
        pm = QPixmap.fromImage(img).scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._tip.setPixmap(pm)
        self._tip.adjustSize()
        g = (QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()).availableGeometry()
        p = QCursor.pos()
        x, y = p.x() + 16, p.y() + 16
        if x + self._tip.width() > g.right() - 4:
            x = p.x() - self._tip.width() - 12
        if y + self._tip.height() > g.bottom() - 4:
            y = p.y() - self._tip.height() - 12
        self._tip.move(max(g.left() + 4, x), max(g.top() + 4, y))
        self._tip.show()
        self._tip_item = item

    def _hover_update(self, pos):
        """按视口坐标实时解析悬停行——itemEntered 只在鼠标划入新行时触发，
        滚轮让行在光标下移动、refresh 重建行后光标不动都触发不了，悬停预览
        「偶发弹不出来」的根因；MouseMove/Wheel 路径统一走这里。"""
        it = self.list.itemAt(pos)
        if it is None or it.data(Qt.UserRole + 1) is None:
            self._tip.hide()
            self._tip_item = None
            return
        if self._tip.isVisible():
            if it is self._tip_item:
                return                     # 同一行已展示，不动防闪烁
            if self._tip.geometry().contains(QCursor.pos()):
                self._tip.hide()           # 光标挪进了浮窗：先收起，移出再弹
                self._tip_item = None
                return
        self._show_tip(it)

    def hide(self):
        """隐藏窗口并收起悬停预览。"""
        self._tip.hide()
        self._tip_item = None
        super().hide()

    def eventFilter(self, obj, e):
        """搜索框：↓ 进列表 / 回车粘贴；列表视口：鼠标离开收起悬停预览，
        移动/滚轮按坐标实时刷新悬停预览（见 _hover_update）。"""
        if obj is self.list.viewport():
            t = e.type()
            if t == QEvent.Leave:
                self._tip.hide()
                self._tip_item = None
            elif t == QEvent.MouseMove:
                self._hover_update(e.position().toPoint())
            elif t == QEvent.Wheel:
                # 滚轮后行才挪位：等滚动生效再按光标当前位置解析
                QTimer.singleShot(0, lambda: self._hover_update(
                    self.list.viewport().mapFromGlobal(QCursor.pos())))
            return False
        if obj is self.search and e.type() == QEvent.KeyPress:
            k = e.key()
            if k == Qt.Key_Down and self.list.count():
                row = max(0, self.list.currentRow())
                self.list.setCurrentRow(row)
                self.list.setFocus()
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter) and self.list.count():
                self._kb_paste(max(0, self.list.currentRow()))
                return True
        return super().eventFilter(obj, e)

    def _copy_only(self, ci: ClipItem):
        self._replay(ci)  # 仅复制到剪贴板，不粘贴、不隐藏

    # ------------------------------------------------ 密码条目：粘贴 / 复制
    def _paste_pw(self, rec: dict, field: str):
        """粘贴账号/密码到目标窗口；密码不进历史且 30 秒后自动清空剪贴板。"""
        val = rec.get(field) or ""
        if not val:
            return
        now = time.time()
        if now - self._last_paste < 0.3:
            return
        self._last_paste = now
        self._copy_pw(rec, field)
        self._send_paste()

    def _send_paste(self):
        """把 Ctrl+V 发给目标窗口：未锁定 → 先隐藏本窗口再发；锁定 → 把弹出前
        的前台窗口强制切回前台并核实成功后发送，本窗口保持显示（Ditto 锁定行为）；
        切换失败（SetForegroundWindow 可能被系统静默拒绝）则退回隐藏-发送-弹回。"""
        if not self._locked:
            self.hide()  # 先隐藏，Ctrl+V 才发给目标窗口
            QTimer.singleShot(120, _send_ctrl_v)
            return
        hwnd = int(getattr(self, "_target_hwnd", 0) or 0)
        if _focus_target(hwnd):
            QTimer.singleShot(120, _send_ctrl_v)
        else:  # 无有效目标或切换被拒：短暂隐藏发送后弹回
            self.hide()
            QTimer.singleShot(120, _send_ctrl_v)
            QTimer.singleShot(350, self._reshow)

    def _reshow(self):
        if self._locked and not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
            self.search.setFocus()

    def _copy_pw(self, rec: dict, field: str):
        """把账号/密码写入剪贴板（不粘贴）。密码抑制历史记录并 30 秒自动清空。"""
        val = rec.get(field) or ""
        if not val:
            return
        if field == "password":
            if self._monitor is not None and hasattr(self._monitor, "suppress_next"):
                self._monitor.suppress_next(2)  # 密码绝不进入剪贴板历史
            QTimer.singleShot(30_000, lambda: self._auto_clear_clip(val))
        QApplication.clipboard().setText(val)

    def _auto_clear_clip(self, text: str):
        """剪贴板内容仍等于 text 时清空（与密码管理窗口同款策略）。"""
        cb = QApplication.clipboard()
        if cb.text() == text:
            cb.setText("")

    # ------------------------------------------------ 右键菜单 / ••• 菜单
    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        data = it.data(Qt.UserRole)
        gpos = self.list.viewport().mapToGlobal(pos)
        if isinstance(data, tuple) and len(data) == 2 and data[0] == "pw":
            self._pw_menu(data[1], gpos)
            return
        ci: ClipItem = data
        m = QMenu(self)
        m.setStyleSheet(_MENU_QSS)
        a_pin = m.addAction("取消常用" if ci.pinned else "设为常用")
        a_copy = m.addAction("仅复制到剪贴板")
        m.addSeparator()
        a_del = m.addAction("删除")
        act = m.exec(self.list.viewport().mapToGlobal(pos))
        if act is a_pin:
            self._set_pinned(ci, not ci.pinned)
        elif act is a_copy:
            self._copy_only(ci)
        elif act is a_del:
            self._delete(ci)

    def _pw_menu(self, rec: dict, gpos):
        """密码条目右键菜单：粘贴/复制、显示密码、打开密码管理。"""
        m = QMenu(self)
        m.setStyleSheet(_MENU_QSS)
        a_acct = m.addAction("粘贴账号")
        a_pwd = m.addAction("粘贴密码")
        m.addSeparator()
        a_ca = m.addAction("仅复制账号")
        a_cp = m.addAction("仅复制密码")
        rid = rec["id"]
        a_eye = m.addAction("隐藏密码" if rid in self._pw_revealed else "显示密码")
        a_open = None
        if self._on_pwman is not None:
            m.addSeparator()
            a_open = m.addAction("打开密码管理…")
        act = m.exec(gpos)
        if act is a_acct:
            self._paste_pw(rec, "username")
        elif act is a_pwd:
            self._paste_pw(rec, "password")
        elif act is a_ca:
            self._copy_pw(rec, "username")
        elif act is a_cp:
            self._copy_pw(rec, "password")
        elif act is a_eye:
            if rid in self._pw_revealed:
                self._pw_revealed.discard(rid)
            else:
                self._pw_revealed.add(rid)
            self.refresh()
        elif act is a_open and act is not None:
            self._on_pwman()

    def _build_menu(self) -> QMenu:
        m = QMenu(self)
        m.setStyleSheet(_MENU_QSS)
        m.addAction("刷新", self.refresh)
        m.addAction("清空历史（保留常用）", self._clear_history)
        if self._on_pwman is not None:
            m.addAction("密码管理…", self._on_pwman)
        if self._on_settings is not None:
            m.addAction("设置…", self._on_settings)
        return m

    def _clear_history(self):
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "SnapPin", "确定清空剪贴板历史吗？（常用项会保留）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._dims.clear()
            self._store.clear(keep_pinned=True)
            self.refresh()

    def _set_pinned(self, ci: ClipItem, pinned: bool):
        self._store.set_pinned(ci.id, pinned)
        self.refresh()

    def _delete(self, ci: ClipItem):
        self._store.delete(ci.id)
        self._dims.pop(ci.id, None)
        self.refresh()

    # ------------------------------------------------ 锁定
    def _toggle_lock(self):
        self._locked = not self._locked
        self._cfg["clip"]["pin_stay"] = self._locked
        config.save(self._cfg)
        self._sync_lock_btn()

    def _sync_lock_btn(self):
        self.edge.set_locked(self._locked)
        self.edge.btn_lock.setIcon(_svg_icon(
            _SVG_LOCKED if self._locked else _SVG_UNLOCKED,
            ACCENT if self._locked else "#9AA0AC", 16))

    # ------------------------------------------------ 侧栏模式：全部 / 常用 / 密码
    def _set_mode(self, mode: str):
        """切换列表内容；再点一次当前模式返回全部。"""
        self._mode = "all" if self._mode == mode else mode
        self._sync_mode()
        self.refresh()

    def _sync_mode(self):
        for b, m, body in ((self.edge.btn_pins, "pins", _SVG_PIN),
                           (self.edge.btn_pw, "pw", _SVG_KEY)):
            active = self._mode == m
            b.setProperty("active", active)
            b.style().unpolish(b)
            b.style().polish(b)
            b.setIcon(_svg_icon(body, ACCENT if active else "#9AA0AC", 16))

    # ------------------------------------------------ 关闭 / 拖动
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.WindowDeactivate and not self._locked:
            self.hide()  # 锁定时失焦不隐藏
        return super().event(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_off)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag_off is not None:
            self._drag_off = None
            self._cfg["clip"]["last_pos"] = [self.x(), self.y()]  # remember 模式下次 popup 用
            config.save(self._cfg)
        super().mouseReleaseEvent(e)


if __name__ == "__main__":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import sys
    import tempfile

    import config
    config.save = lambda c: None  # 自测不写真实配置文件

    app = QApplication([])
    db = os.path.join(tempfile.gettempdir(), "snappin_clipwin_selftest.db")
    if os.path.exists(db):
        os.remove(db)
    store = ClipStore(db)
    store.add({"formats": {"text/plain": "第一条 测试文本".encode("utf-8")},
               "text": "第一条 测试文本", "hash": "h1"})
    store.add({"formats": {"text/plain": b"second entry"}, "text": "second entry", "hash": "h2"})
    store.add({"formats": {"text/plain": "乱码\r\n控制\x00字符\x1b测试".encode("utf-8")},
               "text": "乱码\r\n控制\x00字符\x1b测试", "hash": "h3"})

    cfg = {"clip": {"popup_pos": "center", "pin_stay": False, "retention_days": 30, "max_entries": 100}}

    # 密码库(搜索集成自测用)
    from pwman import PasswordStore as _PWS
    pwdb = os.path.join(tempfile.gettempdir(), "snappin_clipwin_pw_selftest.db")
    if os.path.exists(pwdb):
        os.remove(pwdb)
    pws = _PWS(pwdb)
    pws.add("GitHub", "alice@example.com", "s3cret-pw")

    win = ClipWindow(store, cfg, pw_provider=lambda: pws)
    assert not win._locked, "pin_stay=False 应初始化为未锁定"

    win.popup()
    assert win.isVisible() and win.list.count() == 3, "popup 后应可见且有 3 条"
    # 重影修复：item 文本必须为空（行内容由 widget 渲染）
    assert all(win.list.item(i).text() == "" for i in range(win.list.count())), "item 文本应为空"
    # 乱码修复：控制字符应被清洗
    labels = win.list.itemWidget(win.list.item(0)).findChildren(QLabel)
    assert not any("\x00" in lb.text() or "\x1b" in lb.text() for lb in labels), "控制字符应被剔除"

    win.search.setText("second")
    assert win.list.count() == 1, "搜索应即时过滤"
    win.search.setText("不存在的词")
    assert win.list.count() == 0 and win.list.hint == "无匹配结果"
    win.search.setText("")
    assert win.list.count() == 3

    # 程序化触发单击回放路径（SendInput 换成探针，不真发按键）
    sent = []
    sys.modules[__name__]._send_ctrl_v = lambda: sent.append(True)
    win._paste(win.list.item(0).data(Qt.UserRole))
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert sent == [True], "回放后应触发一次 Ctrl+V"
    assert not win.isVisible(), "未锁定时回放后应隐藏"

    win.popup()
    assert win.isVisible()
    win._toggle_lock()   # 右侧边条双击 / 标题栏按钮共用入口
    assert win._locked and win.edge._locked, "锁定后边条应高亮"
    win._paste(win.list.item(0).data(Qt.UserRole))
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert sent == [True, True] and win.isVisible(), "锁定时回放后窗口应保持打开"
    win._toggle_lock()
    assert not win._locked

    # ---- 密码库搜索：名称 → 账号/密码两行，点哪行粘哪行
    win.popup()
    win.search.setText("github")
    assert win.list.count() == 1, "搜名称应命中密码条目"
    pwt = win.list.item(0).data(Qt.UserRole)
    assert isinstance(pwt, tuple) and pwt[0] == "pw", "密码条目 data 应带 pw 标记"
    pwrec = pwt[1]
    labs = win.list.itemWidget(win.list.item(0)).findChildren(QLabel)
    lab_txt = " ".join(lb.text() for lb in labs)
    assert "alice@example.com" in lab_txt, "账号行应显示账号"
    assert "•••" in lab_txt and "s3cret-pw" not in lab_txt, "密码默认应掩码"
    # 显示密码后明文可见
    win._pw_revealed.add(pwrec["id"])
    win.refresh()
    lab_txt = " ".join(lb.text() for lb in
                       win.list.itemWidget(win.list.item(0)).findChildren(QLabel))
    assert "s3cret-pw" in lab_txt, "显示密码后应见明文"
    win._pw_revealed.discard(pwrec["id"])
    win.refresh()
    # 粘贴密码：剪贴板=密码、窗口隐藏、30 秒清空逻辑可手动触发
    win._paste_pw(pwrec, "password")
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert QApplication.clipboard().text() == "s3cret-pw", "粘贴密码应写入剪贴板"
    assert not win.isVisible(), "未锁定时粘贴密码后窗口应隐藏"
    win._auto_clear_clip("s3cret-pw")
    assert QApplication.clipboard().text() == "", "自动清空应生效"
    # 粘贴账号
    win.popup()
    win._paste_pw(pwrec, "username")
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert QApplication.clipboard().text() == "alice@example.com"
    # 整行点击(itemClicked 路径)对密码条目应为 no-op,不回放、不崩溃
    win.popup()
    win._paste(pwt)
    assert QApplication.clipboard().text() == "alice@example.com"
    # 清空搜索 → 密码条目消失,普通条目恢复
    win.search.clear()
    assert win.list.count() == 3 and all(
        isinstance(win.list.item(i).data(Qt.UserRole), ClipItem)
        for i in range(win.list.count())), "空搜索不应显示密码条目"

    # 右侧工具栏：关闭/锁定按钮（原“关闭按钮”测试升级）
    win.show()
    assert win.isVisible()
    win.edge.btn_close.click()
    assert not win.isVisible(), "侧栏关闭按钮应能关窗"
    win.show()
    win.edge.btn_lock.click()
    assert win._locked and win.edge._locked, "侧栏锁定按钮应切换锁定"
    win.edge.btn_lock.click()
    assert not win._locked
    win.hide()
    assert not win.isVisible()

    # 锁定态单击条目：窗口保持显示且 Ctrl+V 照常发出（发给目标窗口）
    win.popup()
    win._toggle_lock()
    assert win._locked
    sent.clear()
    win._paste(win.list.item(0).data(Qt.UserRole))
    t0 = time.time()
    while time.time() - t0 < 0.5:
        app.processEvents()
        time.sleep(0.01)
    assert sent == [True], "锁定态单击应照常发出 Ctrl+V"
    assert win.isVisible() or not _focus_target(win._target_hwnd), \
        "锁定态且切前台成功时窗口应保持打开"
    win._toggle_lock()

    # _focus_target 核验逻辑：非法句柄必须返回 False（走隐藏兜底，不发向自己）
    assert _focus_target(0) is False
    assert _focus_target(0xDEADBEEF) is False

    # 侧栏模式切换：常用 / 密码（再点一次返回全部）
    win._set_pinned(win.list.item(0).data(Qt.UserRole), True)  # 先造一个常用项
    win._set_mode("pins")
    assert win._mode == "pins" and win.list.count() == 1, "常用模式只显示常用项"
    assert win.title.text().startswith("常用项")
    win._set_mode("pw")
    assert win._mode == "pw" and win.list.count() == 1, "密码模式空搜索应列出全部条目"
    assert win.title.text().startswith("密码")
    pw_labs = [lb for lb in win.list.itemWidget(win.list.item(0)).findChildren(QLabel)
               if lb.property("pwline") is not None or lb.property("pwdim") is not None]
    assert len(pw_labs) == 2 and all(lb.minimumHeight() >= 26 for lb in pw_labs), \
        "密码行最小高度 26，文本应完整显示"
    win._set_mode("pw")
    assert win._mode == "all", "再点一次当前模式应返回全部"

    # 清空历史（保留常用）
    ci0 = win.list.item(0).data(Qt.UserRole)
    win._set_pinned(ci0, True)
    n = store.clear(keep_pinned=True)
    win.refresh()
    assert n >= 2 and win.list.count() == 1, "清空后应只剩常用项"

    # ---- 键盘操作：Enter 粘贴 / Del 删除 / 数字快贴 / 搜索框回车
    store.add({"formats": {"text/plain": "kb 快捷条目".encode()},
               "text": "kb 快捷条目", "hash": "hkb"})
    win.popup()
    QApplication.clipboard().clear()
    sent.clear()
    expect0 = win.list.item(0).data(Qt.UserRole).text  # 行0 = 置顶常用项
    win._kb_paste(0)
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert sent == [True] and QApplication.clipboard().text() == expect0, \
        f"回车应粘贴当前行, 实际 {QApplication.clipboard().text()!r}"
    assert not win.isVisible(), "未锁定键盘粘贴后应隐藏"

    win.popup()
    win.list.paste_row.emit(0)  # 信号路由（列表回车/数字快贴共用）
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert sent == [True, True], "paste_row 信号应路由到粘贴"

    win.popup()
    QApplication.clipboard().clear()
    from PySide6.QtGui import QKeyEvent
    win.eventFilter(win.search, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert QApplication.clipboard().text() == expect0, "搜索框回车应粘贴首行"

    n0 = win.list.count()
    win._kb_delete(0)
    assert win.list.count() == n0 - 1, "Del 应删除当前行"

    # 密码行回车 = 贴密码
    win.popup()
    win._set_mode("pw")
    QApplication.clipboard().clear()
    win._kb_paste(0)
    t0 = time.time()
    while time.time() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)
    assert QApplication.clipboard().text() == "s3cret-pw", "密码行回车应贴密码"
    win._set_mode("pw")

    # ---- 图片行：行内小缩略图 + 悬停弹出稍大预览
    from PySide6.QtCore import QBuffer
    png_img = QImage(20, 14, QImage.Format_RGB32)
    png_img.fill(0xFF2E7CF6)
    _buf = QBuffer()
    _buf.open(QBuffer.WriteOnly)
    assert png_img.save(_buf, "PNG")
    store.add({"formats": {"image/png": bytes(_buf.data())}, "text": "", "hash": "himg"})
    win.popup()
    assert (win.width(), win.height()) == (360, 500), "窗口应为 360x500"
    img_it = next(win.list.item(i) for i in range(win.list.count())
                  if win.list.item(i).data(Qt.UserRole + 1) is not None)
    win._on_item_entered(img_it)
    assert win._tip.isVisible() and not win._tip.pixmap().isNull(), "悬停图片行应弹出预览"
    win.eventFilter(win.list.viewport(), QEvent(QEvent.Leave))
    assert not win._tip.isVisible(), "鼠标离开应收起预览"
    txt_it = next(win.list.item(i) for i in range(win.list.count())
                  if win.list.item(i).data(Qt.UserRole + 1) is None)
    win._on_item_entered(txt_it)
    assert not win._tip.isVisible(), "非图片行不应弹预览"

    # ---- 悬停预览增强：坐标驱动(_hover_update)+160px 大预览+滚轮路径
    big = QImage(400, 300, QImage.Format_RGB32)
    big.fill(0xFF6AA1F8)
    _buf2 = QBuffer()
    _buf2.open(QBuffer.WriteOnly)
    assert big.save(_buf2, "PNG")
    store.add({"formats": {"image/png": bytes(_buf2.data())}, "text": "", "hash": "himg2"})
    win.hide()
    win.popup()                           # popup 是 toggle：先隐藏才会走刷新分支
    # 缩略图入库已按 192px 框缩放：400x300 → 192x144，用它识别"大图"条目
    big_it = next(win.list.item(i) for i in range(win.list.count())
                  if (lambda d: QImage.fromData(QByteArray(d)).size() == QSize(192, 144)
                      if d else False)(win.list.item(i).data(Qt.UserRole + 1)))
    rc = win.list.visualItemRect(big_it)
    win._hover_update(rc.center())
    assert win._tip.isVisible() and win._tip_item is big_it, "坐标驱动悬停应弹出预览"
    assert win._tip.pixmap().width() == 160, "大图悬停预览应放大到 160px"
    win._hover_update(rc.center())        # 同一行重复悬停：不闪烁不重建
    assert win._tip_item is big_it and win._tip.isVisible()
    txt2 = next(win.list.item(i) for i in range(win.list.count())
                if win.list.item(i).data(Qt.UserRole + 1) is None)
    win._hover_update(win.list.visualItemRect(txt2).center())   # 非图片行：应收起
    assert not win._tip.isVisible() and win._tip_item is None
    win.eventFilter(win.list.viewport(), QEvent(QEvent.Wheel))  # 滚轮路径不崩即可
    t0 = time.time()
    while time.time() - t0 < 0.2:
        app.processEvents()
        time.sleep(0.01)

    # ---- 打开时滚动条置顶
    for i in range(10):
        store.add({"formats": {"text/plain": f"滚动置顶填充 {i}".encode()},
                   "text": f"滚动置顶填充 {i}", "hash": f"hroll{i}"})
    win.hide()
    win.popup()                           # 隐藏后再 popup 才走刷新分支
    sb = win.list.verticalScrollBar()
    sb.setValue(sb.maximum())
    assert sb.value() > 0, "前置：条目足够时应可滚动"
    win.hide()
    win.popup()
    assert win.list.verticalScrollBar().value() == 0, "再次打开应定位到最上边"

    pws.close()

    print("clipwin 自测通过: popup/搜索/无重影/无乱码/回放/锁定/边条/清空/键盘/"
          "悬停预览(坐标驱动+160px)/滚动置顶 全部 OK")
    win.hide()
    QApplication.clipboard().clear()  # 规避 offscreen 平台 QClipboard 析构段错误
    del win, store  # 先销毁窗口再销毁 app，避免退出时析构顺序问题
    del app
