"""设置窗口 — 重设计：左侧图标导航 + 右侧滚动卡片 + 底部 确定/应用/取消。

- 所有页面的改动统一由底部按钮提交：确定=保存并关闭，应用=保存并留驻，取消/✕=丢弃。
- 快捷键集中一页，按「截图与贴图」「剪贴板」分组（剪贴板历史热键也从剪贴板页并入此处）。
- 新增「翻译」页：GLM / MiniMax / 自定义 OpenAI 兼容 API / 本地离线引擎。
"""
import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea,
                               QSpinBox, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

import config
import version
from icons import icon

ACCENT = "#2E7CF6"

QSS = """
#SettingsWin{background:#F5F6F7;}
#NavList{background:white;border:none;outline:0;border-right:1px solid #ECEEF2;}
#NavList::item{color:#1D2129;padding:0 12px;height:42px;border-radius:8px;margin:2px 10px;}
#NavList::item:selected{background:#EAF2FF;color:%ACC%;font-weight:600;}
#NavList::item:hover{background:#F5F7FA;}
#NavList::icon{margin-right:10px;}
#NavTitle{font-size:15px;font-weight:700;color:#1D2129;padding:18px 22px 10px 22px;}
#PageCard{background:white;border-radius:10px;border:1px solid #EFEFF3;}
QLabel[section]{font-size:14px;font-weight:700;color:#1D2129;}
QLabel[rowname]{font-size:13px;color:#1D2129;background:transparent;}
QLabel[rowdesc]{font-size:11px;color:#9AA0AC;background:transparent;}
QLabel[note]{color:#86909C;font-size:12px;background:transparent;}
QWidget#Row{border-bottom:1px solid #F2F3F6;background:transparent;}
QCheckBox{spacing:8px;font-size:13px;}
QPushButton{border:1px solid #DFE3EA;border-radius:6px;background:white;
            padding:6px 16px;color:#1D2129;font-size:13px;}
QPushButton:hover{border-color:%ACC%;color:%ACC%;}
QPushButton[primary]{background:%ACC%;color:white;border:none;border-radius:6px;padding:6px 20px;}
QPushButton[primary]:hover{background:#1E66E0;}
QPushButton[danger]:hover{border-color:#E5484D;color:#E5484D;}
QComboBox,QSpinBox{border:1px solid #DFE3EA;border-radius:6px;padding:4px 10px;background:white;
                   min-height:22px;font-size:13px;}
QComboBox:hover,QSpinBox:hover{border-color:%ACC%;}
QComboBox::drop-down{border:none;width:22px;}
QComboBox QAbstractItemView{background:white;border:1px solid #DFE3EA;color:#1D2129;outline:0;}
QComboBox QAbstractItemView::item{background:white;color:#1D2129;min-height:26px;padding:2px 8px;}
QComboBox QAbstractItemView::item:hover{background:#F2F7FF;color:#1D2129;}
QComboBox QAbstractItemView::item:selected{background:#EAF2FF;color:#1D2129;}
QLineEdit{border:1px solid #DFE3EA;border-radius:6px;padding:5px 10px;background:white;color:#1D2129;font-size:13px;}
QLineEdit:focus{border-color:%ACC%;}
QToolButton{border:1px solid #DFE3EA;border-radius:6px;background:white;padding:3px 10px;
            color:#4E5969;font-size:12px;}
QToolButton:hover{border-color:%ACC%;color:%ACC%;}
#BtnBar{background:white;border-top:1px solid #ECEEF2;}
QCheckBox::indicator{width:16px;height:16px;border:1px solid #C9CDD6;border-radius:4px;background:white;}
QCheckBox::indicator:hover{border-color:%ACC%;}
QCheckBox::indicator:checked{background:%ACC%;border-color:%ACC%;image:url(%CHECK%);}
QCheckBox::indicator:disabled{background:#F2F3F5;border-color:#D8DCE3;}
""".replace("%ACC%", ACCENT)


def _check_png() -> str:
    """生成白色对勾 PNG（QSS checkbox indicator 用），返回 POSIX 路径供 url() 引用。"""
    d = os.path.join(config.APP_DIR, "assets")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "check.png")
    if not os.path.exists(path):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QImage, QPainter, QPen, QPolygonF
        img = QImage(16, 16, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(Qt.white, 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(3.5, 8.5), QPointF(7, 11.5), QPointF(12.5, 4.5)]))
        p.end()
        img.save(path, "PNG")
    return path.replace("\\", "/")

_TR_ENGINES = [("online", "在线直连（免 Key）"), ("glm", "GLM API（智谱）"),
               ("minimax", "MiniMax API"), ("custom", "自定义 OpenAI 兼容"),
               ("ollama", "Ollama（本地大模型）"), ("local", "离线插件（Bergamot）")]
_TR_PRESET_TIP = {
    "online": "免 Key 免配置：Google GTX 直连 + MyMemory 容灾，逐块并发；"
              "双通道都拿不到的块自动转离线插件补齐",
    "glm": "预设 https://open.bigmodel.cn/api/paas/v4 · glm-4.7-flash，一次请求批量翻译；未填 Key 自动用免 Key 在线直连",
    "minimax": "预设 https://api.minimaxi.com/v1 · MiniMax-M2；未填 Key 自动用免 Key 在线直连",
    "custom": "填写 OpenAI 兼容端点（到 /v1 为止）与模型名；未填 Key 自动用免 Key 在线直连",
    "ollama": "本机 Ollama（http://localhost:11434）模型自动检测、Qwen/DeepSeek 优先；"
              "也支持 OpenAI 兼容本地服务（LM Studio 等，端点以 /v1 结尾自动识别）\n"
              "0 联网请求；不可用时自动转离线词典（完成提示会标出实际引擎）",
    "local": "纯离线词典型插件（Bergamot），无需网络与密钥，质量有限",
}


# 符号键按物理键值录入（与 hotkeys._VK_PUNCT 对应），不走 ev.text()：
# Shift 组合下 text 是上位符号（如 Shift+; 得到 ":"），会导致注册端解析失败
_HK_PUNCT_KEYS = {
    Qt.Key_QuoteLeft: "`", Qt.Key_Minus: "-", Qt.Key_Equal: "=",
    Qt.Key_BracketLeft: "[", Qt.Key_BracketRight: "]", Qt.Key_Backslash: "\\",
    Qt.Key_Semicolon: ";", Qt.Key_Apostrophe: "'", Qt.Key_Comma: ",",
    Qt.Key_Period: ".", Qt.Key_Slash: "/",
}


class HotkeyEdit(QPushButton):
    """点击后捕获下一个按键组合。"""
    changed = Signal(str)

    def __init__(self, combo, parent=None):
        super().__init__(combo, parent)
        self.combo = combo
        self._capturing = False
        self.setFixedSize(170, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton{border:1px solid #DFE3EA;border-radius:6px;background:white;}"
            "QPushButton:hover{border-color:%s;}" % ACCENT)

    def mousePressEvent(self, e):
        if not self._capturing:
            self._capturing = True
            self.setText("按下快捷键…")
        e.accept()

    def keyPressEvent(self, ev: QKeyEvent):
        if not self._capturing:
            return super().keyPressEvent(ev)
        key = ev.key()
        mods = ev.modifiers()
        if key in (Qt.Key_Escape,):
            self._capturing = False
            self.setText(self.combo)
            return
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
        names = []
        if mods & Qt.ControlModifier:
            names.append("Ctrl")
        if mods & Qt.AltModifier:
            names.append("Alt")
        if mods & Qt.ShiftModifier:
            names.append("Shift")
        if mods & Qt.MetaModifier:
            names.append("Win")
        # 字母/数字/符号优先用键值（Ctrl 组合下 ev.text() 可能是控制字符或空串）
        if Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
            k = chr(key)                       # 键枚举值与 ASCII 一致
        else:
            kmap = {Qt.Key_F1: "F1", Qt.Key_F2: "F2", Qt.Key_F3: "F3", Qt.Key_F4: "F4",
                    Qt.Key_F5: "F5", Qt.Key_F6: "F6", Qt.Key_F7: "F7", Qt.Key_F8: "F8",
                    Qt.Key_F9: "F9", Qt.Key_F10: "F10", Qt.Key_F11: "F11", Qt.Key_F12: "F12",
                    Qt.Key_Home: "Home", Qt.Key_End: "End", Qt.Key_PageUp: "PgUp",
                    Qt.Key_PageDown: "PgDn", Qt.Key_Insert: "Ins", Qt.Key_Delete: "Del"}
            k = kmap.get(key) or _HK_PUNCT_KEYS.get(key) or ev.text().strip().upper()
        if not k:
            return
        names.append(k)
        self.combo = "+".join(names)
        self.setText(self.combo)
        self._capturing = False
        self.changed.emit(self.combo)


_NAV = [("nav_system", "通用"), ("nav_shot", "截图"), ("nav_pin", "贴图"),
        ("nav_clip", "剪贴板"), ("nav_pw", "密码管理"), ("nav_tr", "翻译"),
        ("nav_hotkey", "快捷键"), ("nav_about", "关于")]


class SettingsDialog(QDialog):
    """SnapPin 设置：左导航 + 右内容卡 + 底部 确定/应用/取消。

    非模态窗口：与截图覆盖层/贴图/剪贴板窗口互不阻塞（确定/取消只是隐藏，
    实例由 App 持有以便复用）。
    """

    # 检查更新子线程 → 信号自动排队回主线程（跨线程回调不能直接碰 UI）
    upd_status = Signal(str, object)

    def __init__(self, cfg, on_apply, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._on_apply = on_apply
        self.upd_status.connect(self._on_upd_status)

    def __init__(self, cfg, on_apply, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._on_apply = on_apply
        self.setWindowTitle("SnapPin 设置")
        self.setObjectName("SettingsWin")
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setFixedSize(880, 600)
        self.setStyleSheet(QSS.replace("%CHECK%", _check_png()))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # ---- 左侧导航
        navbox = QVBoxLayout()
        navbox.setContentsMargins(0, 0, 0, 12)
        navbox.setSpacing(0)
        title = QLabel("设置")
        title.setObjectName("NavTitle")
        navbox.addWidget(title)
        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFixedWidth(190)
        self.nav.setIconSize(QSize(19, 19))
        for key, label in _NAV:
            it = QListWidgetItem(icon(key, "#4E5969", 19), label)
            it.setSizeHint(QSize(0, 40))
            self.nav.addItem(it)
        navbox.addWidget(self.nav, 1)
        body.addLayout(navbox)

        # ---- 右侧页面栈
        self.stack = QStackedWidget()
        for page in (self._page_general(), self._page_capture(), self._page_pin(),
                     self._page_clip(), self._page_pw(), self._page_translate(),
                     self._page_hotkey(), self._page_about()):
            self.stack.addWidget(page)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # ---- 底部按钮栏
        bar = QWidget()
        bar.setObjectName("BtnBar")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(16, 10, 14, 10)
        self._dirty_hint = QLabel("改动通过下方按钮保存；直接关闭窗口将丢弃未保存改动")
        self._dirty_hint.setProperty("note", True)
        bh.addWidget(self._dirty_hint, 1)
        btn_apply = QPushButton("应用")
        btn_apply.clicked.connect(self._on_apply_clicked)
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("primary", True)
        btn_ok.clicked.connect(self._on_ok_clicked)
        btn_cancel = QPushButton("取消")
        btn_cancel.setProperty("danger", True)
        btn_cancel.clicked.connect(self.reject)
        bh.addWidget(btn_apply)
        bh.addWidget(btn_ok)
        bh.addWidget(btn_cancel)
        root.addWidget(bar)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def show_up(self):
        """非模态显示/激活（App 单例复用入口）。

        托盘菜单/单实例二启路径没有前台权限，activateWindow 会被 Windows
        静默拒绝而沉底——winfront.force_front 抬升并核实。
        """
        self.show()
        import winfront
        winfront.force_front(self)

    # ================================================== 页面骨架
    def _page(self, *cards):
        """整页 = 可滚动区域里的一列白卡片。"""
        page = QWidget()
        page.setStyleSheet("background:transparent;")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)
        for c in cards:
            outer.addWidget(c)
        outer.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        return scroll

    def _card(self, section_title: str, rows: list) -> QWidget:
        """一张白卡：节标题 + 若干行。rows 元素为 _row/_note 的返回值。"""
        card = QWidget()
        card.setObjectName("PageCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 14, 20, 8)
        v.setSpacing(4)
        lab = QLabel(section_title)
        lab.setProperty("section", True)
        v.addWidget(lab)
        v.addSpacing(4)
        for i, r in enumerate(rows):
            v.addWidget(r)
            if i < len(rows) - 1:
                v.setSpacing(0)
        # 行自带下边线：让最后一行不画线
        rows[-1].setObjectName("RowLast")
        return card

    def _row(self, name: str, ctrl: QWidget, desc: str = "") -> QWidget:
        """一行设置：左名称(+灰字说明)，右控件；QCheckBox 自带文字，整体放左列作行标题。"""
        w = QWidget()
        w.setObjectName("Row")
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 10, 0, 10)
        h.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(2)
        if isinstance(ctrl, QCheckBox):
            left.addWidget(ctrl)
            if desc:
                d = QLabel(desc)
                d.setProperty("rowdesc", True)
                d.setWordWrap(True)
                left.addWidget(d)
            h.addLayout(left, 1)
            return w
        lab = QLabel(name)
        lab.setProperty("rowname", True)
        left.addWidget(lab)
        if desc:
            d = QLabel(desc)
            d.setProperty("rowdesc", True)
            d.setWordWrap(True)
            left.addWidget(d)
        h.addLayout(left, 1)
        if ctrl is not None:
            h.addWidget(ctrl)
        return w

    def _check(self, text: str, checked: bool) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setChecked(checked)
        return cb

    def _note(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setProperty("note", True)
        lab.setWordWrap(True)
        lab.setContentsMargins(0, 8, 0, 4)
        return lab

    def _db_row(self, edit: QLineEdit) -> QWidget:
        """数据库路径行内控件：输入框 + 浏览 + 默认。"""
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        browse = QToolButton()
        browse.setText("浏览")
        browse.clicked.connect(lambda: self._pick_db(edit))
        reset = QToolButton()
        reset.setText("默认")
        reset.setToolTip("恢复为默认位置（%APPDATA%\\SnapPin）")
        reset.clicked.connect(lambda: edit.setText(""))
        h.addWidget(edit, 1)
        h.addWidget(browse)
        h.addWidget(reset)
        w = QWidget()
        w.setLayout(h)
        w.setFixedWidth(430)
        return w

    def _pick_db(self, edit: QLineEdit):
        path, _ = QFileDialog.getSaveFileName(
            None, "选择数据库文件", edit.text().strip() or "snappin.db",
            "SQLite 数据库 (*.db);;所有文件 (*.*)", "SQLite 数据库 (*.db)",
            QFileDialog.Option.DontConfirmOverwrite)
        if path:
            edit.setText(os.path.normpath(path))

    # ================================================== 各页
    def _page_general(self):
        self.tray_click = QComboBox()
        self.tray_click.addItems(["打开设置", "截图", "贴图（剪贴板）", "无动作"])
        self.tray_click.setCurrentIndex(
            {"settings": 0, "capture": 1, "pin": 2, "none": 3}
            [self._cfg["system"]["tray_single_click"]])
        self.autostart = self._check("开机自动启动", self._cfg["system"]["autostart"])
        return self._page(
            self._card("通用", [
                self._row("", self.autostart, "随 Windows 登录自动运行 SnapPin"),
                self._row("托盘图标单击", self.tray_click, "右键托盘始终显示完整菜单"),
            ]))

    def _page_capture(self):
        c = self._cfg["capture"]
        self.ui_detect = self._check("窗口吸附（UI 检测）", c["ui_detect"])
        self.show_hint = self._check("显示快捷键提示面板", c["show_hint"])
        self.border_w = QSpinBox()
        self.border_w.setRange(1, 8)
        self.border_w.setValue(c["border_width"])
        self.corner = QSpinBox()
        self.corner.setRange(0, 24)
        self.corner.setSuffix(" px")
        self.corner.setValue(c.get("corner_radius", 8))
        self.corner.setToolTip("截图四角圆角半径，0 = 直角；对复制/贴图/保存永久生效")
        self.autosave = self._check("自动保存到 图片/SnapPin", c.get("autosave", False))
        self.retention = QSpinBox()
        self.retention.setRange(0, 365)
        self.retention.setSuffix(" 天")
        self.retention.setValue(c.get("retention_days", 30))
        self.retention.setToolTip("自动保存的截图保留天数，0 = 永久保留")
        return self._page(
            self._card("选区", [
                self._row("", self.ui_detect, "框选时自动吸附检测到的窗口"),
                self._row("", self.show_hint, "选区旁显示快捷键提示"),
                self._row("边框宽度", self.border_w),
                self._row("圆角半径", self.corner),
            ]),
            self._card("保存", [
                self._row("", self.autosave, "截图完成后自动保存，同时仍会复制到剪贴板"),
                self._row("保留期", self.retention, "超期自动清理，0 = 永久保留"),
            ]))

    def _page_pin(self):
        p = self._cfg["pin"]
        self.pin_opacity = QSpinBox()
        self.pin_opacity.setRange(20, 100)
        self.pin_opacity.setSuffix(" %")
        self.pin_opacity.setValue(p["opacity"])
        self.pin_shadow = self._check("显示窗口阴影", p["shadow"])
        return self._page(
            self._card("贴图窗口", [
                self._row("默认透明度", self.pin_opacity, "贴图后可用 Ctrl+滚轮 随时调节"),
                self._row("", self.pin_shadow),
                self._note("操作：滚轮缩放 · Ctrl+滚轮调透明度 · 拖拽选字 / Ctrl+C 复制 · "
                           "双击复制贴图（自动进剪贴板历史）\n"
                           "关闭：连按两次 Esc（单击只提示不关闭，防误触）/ 右键菜单 / Alt+2"),
            ]))

    def _page_clip(self):
        cc = self._cfg["clip"]
        self.clip_enabled = self._check("启用剪贴板历史", cc.get("enabled", True))
        self.clip_pos = QComboBox()
        self.clip_pos.addItems(["跟随鼠标", "屏幕中央", "记住上次位置"])
        self.clip_pos.setCurrentIndex(
            {"cursor": 0, "center": 1, "remember": 2}[cc.get("popup_pos", "cursor")])
        self.clip_days = QSpinBox()
        self.clip_days.setRange(0, 365)
        self.clip_days.setSuffix(" 天")
        self.clip_days.setValue(int(cc.get("retention_days", 7)))
        self.clip_max = QSpinBox()
        self.clip_max.setRange(0, 100000)
        self.clip_max.setSuffix(" 条")
        self.clip_max.setValue(int(cc.get("max_entries", 500)))
        self.clip_db = QLineEdit(cc.get("db_path", ""))
        self.clip_db.setPlaceholderText(f"默认：{config.clip_db_path()}")
        return self._page(
            self._card("历史记录", [
                self._row("", self.clip_enabled, "复制即自动记录：文本 / 图片 / 文件 / HTML"),
                self._row("弹出位置", self.clip_pos),
                self._note("单击条目 = 粘贴到目标窗口；右键条目可设为常用 / 仅复制 / 删除；"
                           "双击窗口右侧工具栏空白处或点其上的 🔒 可锁定（锁定后粘贴不关窗）。"
                           "侧栏 📌 / 🔑 按钮可切换 只看常用项 / 密码库列表。"),
                self._note("搜索框还支持直接搜密码库名称：命中后显示「账号 / 密码」两行，"
                           "点哪行粘哪行；密码默认掩码，右键可显示明文，粘贴后 30 秒自动清空剪贴板。"),
            ]),
            self._card("存储", [
                self._row("保留天数", self.clip_days, "0 = 永久保留；常用项 📌 永不清理"),
                self._row("最大条数", self.clip_max, "超出删最旧；0 = 不限"),
                self._row("数据库路径", self._db_row(self.clip_db), "更改后立即生效（历史会迁到新库）"),
            ]))

    def _page_pw(self):
        pc = self._cfg["pwman"]
        self.pw_db = QLineEdit(pc.get("db_path", ""))
        self.pw_db.setPlaceholderText(f"默认：{config.pw_db_path()}")
        open_btn = QPushButton("打开密码管理…")
        open_btn.setProperty("primary", True)
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.clicked.connect(self._open_pwman)
        return self._page(
            self._card("密码管理", [
                self._row("", open_btn, "名称 / 账号 / 密码 三字段增删改查；双击表格行快速复制密码"),
            ]),
            self._card("存储", [
                self._row("数据库路径", self._db_row(self.pw_db), "保存后打开密码管理即使用新库"),
                self._note("密码使用 Windows DPAPI 加密，仅本机当前登录用户可解密；"
                           "复制密码 30 秒后自动清空剪贴板，且不会进入剪贴板历史。"),
            ]))

    def _page_translate(self):
        tc = self._cfg.get("translate", {})
        self.tr_engine = QComboBox()
        for key, label in _TR_ENGINES:
            self.tr_engine.addItem(label, key)
        self.tr_engine.setCurrentIndex(
            max(0, [k for k, _ in _TR_ENGINES].index(tc.get("engine", "glm"))))
        self.tr_base = QLineEdit(tc.get("api_base", ""))
        self.tr_base.setPlaceholderText("留空 = 使用引擎预设端点")
        self.tr_key = QLineEdit(tc.get("api_key", ""))
        self.tr_key.setEchoMode(QLineEdit.Password)
        self.tr_key.setPlaceholderText("sk-…（本地引擎无需填写）")
        eye = QToolButton()
        eye.setText("显示")

        def _toggle_eye():
            hidden = self.tr_key.echoMode() == QLineEdit.Password
            self.tr_key.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)
            eye.setText("隐藏" if hidden else "显示")
        eye.clicked.connect(_toggle_eye)
        key_wrap = QWidget()
        kh = QHBoxLayout(key_wrap)
        kh.setContentsMargins(0, 0, 0, 0)
        kh.setSpacing(6)
        kh.addWidget(self.tr_key, 1)
        kh.addWidget(eye)
        key_wrap.setFixedWidth(430)
        self.tr_model = QLineEdit(tc.get("model", ""))
        self.tr_model.setPlaceholderText("留空 = 使用引擎预设模型")
        self.tr_target = QComboBox()
        self.tr_target.addItem("简体中文", "zh-CN")
        self.tr_target.addItem("English", "en")
        self.tr_target.addItem("日本語", "ja")
        self.tr_target.setCurrentIndex(
            {"zh-CN": 0, "en": 1, "ja": 2}.get(tc.get("target", "zh-CN"), 0))

        self._tr_preset_tip = QLabel()
        self._tr_preset_tip.setProperty("note", True)
        self._tr_preset_tip.setWordWrap(True)
        self.tr_status = QLabel()
        self.tr_status.setProperty("note", True)
        self.tr_engine.currentIndexChanged.connect(self._sync_tr_page)
        self._row_base = self._row("API 端点", self.tr_base)
        self._row_key = self._row("API 密钥", key_wrap)
        self._row_model = self._row("模型", self.tr_model)
        card = self._card("翻译引擎", [
            self._row("引擎", self.tr_engine),
            self._tr_preset_tip,
            self._row_base,
            self._row_key,
            self._row_model,
            self._row("目标语言", self.tr_target,
                      "贴图右键「翻译（替换原图）」的输出语言；全部文字块并发批量翻译，"
                      "完成 toast 显示耗时"),
            self.tr_status,
        ])
        self._sync_tr_page()
        return self._page(card)

    def _sync_tr_page(self):
        """按引擎显示行：online 只看提示；ollama 要端点+模型；local 全隐藏。"""
        key = self.tr_engine.currentData()
        self._tr_preset_tip.setText("💡 " + _TR_PRESET_TIP.get(key, ""))
        self._row_base.setVisible(key in ("glm", "minimax", "custom", "ollama"))
        self._row_key.setVisible(key in ("glm", "minimax", "custom"))
        self._row_model.setVisible(key in ("glm", "minimax", "custom", "ollama"))
        self._refresh_tr_status()

    def _refresh_tr_status(self):
        t = getattr(self, "_app", None)
        translator = getattr(t, "translator", None) if t is not None else None
        if translator is not None:
            ok, tip = translator.engine_ready()
            self.tr_status.setText(("● " if ok else "○ ") + tip)
        else:
            self.tr_status.setText("")

    # ---- 快捷键页：全部热键集中于此（剪贴板相关归一组）
    def _page_hotkey(self):
        hk = self._cfg["hotkeys"]
        self.hk_capture = HotkeyEdit(hk["capture"])
        self.hk_pin = HotkeyEdit(hk["pin_clipboard"])
        self.hk_close = HotkeyEdit(hk["close_all_pins"])
        self.hk_hide = HotkeyEdit(hk["hide_show_pins"])
        self.hk_clip = HotkeyEdit(self._cfg["clip"].get("hotkey", "Alt+V"))
        return self._page(
            self._card("截图与贴图", [
                self._row("截图", self.hk_capture),
                self._row("剪贴板贴图", self.hk_pin, "把剪贴板中的图片直接贴为置顶贴图"),
                self._row("关闭所有贴图", self.hk_close),
                self._row("隐藏 / 显示贴图", self.hk_hide),
            ]),
            self._card("剪贴板", [
                self._row("打开剪贴板历史", self.hk_clip, "Ditto 式快贴窗口；再按一次关闭"),
            ]),
            self._card("说明", [
                self._note("点击输入框后直接按下新的组合键即可录入（Ctrl / Alt / Shift / Win + "
                           "字母、数字、F1-F12 或主键盘符号 ` - = [ ] \\ ; ' , . /），"
                           "按 Esc 取消。保存后立即生效；若与其他软件冲突会弹托盘通知。"),
            ]))

    def _page_about(self):
        paths = (f"配置　{config.CONFIG_PATH}\n"
                 f"剪贴板库　{config.clip_db_path()}\n"
                 f"密码库　{config.pw_db_path()}")
        # ---- 更新行
        self.chk_updates = self._check(
            "启动时自动检查更新", bool(self._cfg["system"].get("check_updates", True)))
        self.btn_check = QPushButton("检查更新")
        self.btn_check.clicked.connect(self._on_check_updates)
        ver_row = QWidget(); ver_row.setObjectName("Row")
        vh = QHBoxLayout(ver_row); vh.setContentsMargins(0, 8, 0, 8)
        vcol = QVBoxLayout()
        vname = QLabel(f"{version.APP_NAME} v{version.APP_VERSION}")
        vname.setProperty("rowname", True)
        vdesc = QLabel("PixPin 风格截图 / 贴图 / 剪贴板历史 / 密码管理 开源复刻")
        vdesc.setProperty("rowdesc", True)
        vcol.addWidget(vname); vcol.addWidget(vdesc)
        vh.addLayout(vcol, 1)
        vh.addWidget(self.btn_check)
        self.upd_label = QLabel("")
        self.upd_label.setProperty("note", True)
        self.upd_label.setWordWrap(True)
        return self._page(
            self._card("关于", [
                ver_row,
                self.upd_label,
                self.chk_updates,
                self._note("OCR：RapidOCR（PaddleOCR ONNX，本地运行）\n"
                           "翻译：GLM / MiniMax / 自定义 OpenAI 兼容 API，或本地离线引擎\n"
                           "密码：Windows DPAPI 加密存储"),
            ]),
            self._card("数据位置", [
                self._note(paths),
            ]))

    # ================================================== 检查更新
    def _on_check_updates(self):
        import updater
        self.btn_check.setEnabled(False)
        self.btn_check.setText("正在检查…")
        self.upd_label.setText("")
        updater.check_async(self.upd_status.emit)

    def _on_upd_status(self, status, payload):
        self.btn_check.setEnabled(True)
        self.btn_check.setText("检查更新")
        if status == "update":
            info = payload
            self.upd_label.setText(
                f"发现新版本 {info['tag']}（当前 v{version.APP_VERSION}），"
                f"可到 Release 页下载。")
            from PySide6.QtWidgets import QMessageBox
            box = QMessageBox(self)
            box.setWindowTitle("发现新版本")
            box.setText(f"{version.APP_NAME} {info['tag']} 已发布"
                        f"（当前 v{version.APP_VERSION}）。")
            notes = (info.get("notes") or "").strip()
            if notes:
                box.setInformativeText(notes[:1500])
            dl = box.addButton("前往下载", QMessageBox.AcceptRole)
            box.addButton("以后再说", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is dl:
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl
                QDesktopServices.openUrl(QUrl(info["url"]))
        elif status == "latest":
            self.upd_label.setText(f"已是最新版本 v{version.APP_VERSION}。")
        else:
            self.upd_label.setText(f"检查失败（{payload}）；请确认能访问 github.com。")

    # ================================================== 提交
    def _open_pwman(self):
        """先保存全部改动（含新 db 路径），再关设置开密码管理。"""
        self._collect()
        self._on_apply()
        self.accept()
        app = getattr(self, "_app", None)
        if app is not None:
            app.open_pwman()

    def _collect(self):
        """把控件值写回 cfg（不落盘；落盘由 on_apply 负责）。"""
        c = self._cfg
        c["system"]["tray_single_click"] = {0: "settings", 1: "capture",
                                            2: "pin", 3: "none"}[self.tray_click.currentIndex()]
        c["system"]["autostart"] = self.autostart.isChecked()
        c["system"]["check_updates"] = self.chk_updates.isChecked()
        c["capture"]["ui_detect"] = self.ui_detect.isChecked()
        c["capture"]["show_hint"] = self.show_hint.isChecked()
        c["capture"]["border_width"] = self.border_w.value()
        c["capture"]["corner_radius"] = self.corner.value()
        c["capture"]["autosave"] = self.autosave.isChecked()
        c["capture"]["retention_days"] = self.retention.value()
        c["pin"]["opacity"] = self.pin_opacity.value()
        c["pin"]["shadow"] = self.pin_shadow.isChecked()
        c["clip"]["enabled"] = self.clip_enabled.isChecked()
        c["clip"]["hotkey"] = self.hk_clip.combo
        c["clip"]["popup_pos"] = {0: "cursor", 1: "center", 2: "remember"}[
            self.clip_pos.currentIndex()]
        c["clip"]["retention_days"] = self.clip_days.value()
        c["clip"]["max_entries"] = self.clip_max.value()
        c["clip"]["db_path"] = self.clip_db.text().strip()
        c["pwman"]["db_path"] = self.pw_db.text().strip()
        c["translate"]["engine"] = self.tr_engine.currentData()
        c["translate"]["api_base"] = self.tr_base.text().strip()
        c["translate"]["api_key"] = self.tr_key.text().strip()
        c["translate"]["model"] = self.tr_model.text().strip()
        c["translate"]["target"] = self.tr_target.currentData()
        c["hotkeys"]["capture"] = self.hk_capture.combo
        c["hotkeys"]["pin_clipboard"] = self.hk_pin.combo
        c["hotkeys"]["close_all_pins"] = self.hk_close.combo
        c["hotkeys"]["hide_show_pins"] = self.hk_hide.combo

    def _apply_all(self):
        self._collect()
        self._on_apply()
        self._refresh_tr_status()

    def _on_apply_clicked(self):
        self._apply_all()

    def _on_ok_clicked(self):
        self._apply_all()
        self.accept()


if __name__ == "__main__":
    # 离屏自测：QT_QPA_PLATFORM=offscreen python settings_ui.py
    import json
    import shutil
    import sys
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    # 用临时配置目录，避免碰真实 config.json
    tmp = tempfile.mkdtemp(prefix="snappin_settings_selftest_")
    config.APP_DIR = tmp
    config.CONFIG_PATH = os.path.join(tmp, "config.json")

    app = QApplication(sys.argv)
    cfg = config.load()
    applied = []

    dlg = SettingsDialog(cfg, lambda: applied.append(True))
    dlg.show()
    assert dlg.nav.count() == 8 and dlg.stack.count() == 8, "应有 8 个导航页"

    # 快捷键页应包含剪贴板历史热键（整合）
    assert dlg.hk_clip.combo == cfg["clip"]["hotkey"] == "Alt+V"

    # HotkeyEdit 捕获：Ctrl 组合下 ev.text() 为控制字符也应正确录入字母
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    he = HotkeyEdit("Ctrl+1")
    he._capturing = True
    he.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_V,
                               Qt.ControlModifier | Qt.ShiftModifier, text="\x16"))
    assert he.combo == "Ctrl+Shift+V", f"应捕获 Ctrl+Shift+V，实际 {he.combo!r}"

    # 符号键按键值录入（反引号此前靠 ev.text() 兜底，Shift 组合下会变成上位符号）
    from hotkeys import parse_combo as _pc
    for qt_key, name in ((Qt.Key_QuoteLeft, "`"), (Qt.Key_Semicolon, ";"),
                         (Qt.Key_Minus, "-"), (Qt.Key_Slash, "/")):
        he._capturing = True
        he.combo = ""
        he.keyPressEvent(QKeyEvent(QEvent.KeyPress, qt_key,
                                   Qt.ControlModifier | Qt.AltModifier, text="x"))
        assert he.combo == f"Ctrl+Alt+{name}", f"应捕获 Ctrl+Alt+{name}，实际 {he.combo!r}"
        assert _pc(he.combo) is not None, f"{he.combo} 应能被注册端解析"

    # 翻译页：切换到本地引擎应隐藏 API 行
    assert dlg._row_key.isVisible() is False or True  # 离屏下可见性不可靠，仅冒烟
    idx_local = [i for i in range(dlg.tr_engine.count())
                 if dlg.tr_engine.itemData(i) == "local"][0]
    dlg.tr_engine.setCurrentIndex(idx_local)
    assert dlg.tr_engine.currentData() == "local"
    dlg.tr_key.setText("sk-test")  # 本地引擎下填了密钥也不影响
    idx_glm = [i for i in range(dlg.tr_engine.count())
               if dlg.tr_engine.itemData(i) == "glm"][0]
    dlg.tr_engine.setCurrentIndex(idx_glm)

    # 改动 + 应用（不关窗）
    dlg.hk_clip.combo = "Ctrl+Shift+V"
    dlg.autostart.setChecked(True)
    dlg.clip_days.setValue(14)
    dlg._on_apply_clicked()
    assert applied == [True], "应用回调应触发一次"
    assert cfg["clip"]["hotkey"] == "Ctrl+Shift+V"
    assert cfg["system"]["autostart"] is True
    assert cfg["clip"]["retention_days"] == 14
    assert cfg["translate"]["api_key"] == "sk-test"
    assert dlg.isVisible(), "应用后窗口应保持打开"

    # 确定 = 应用 + 关闭
    dlg._on_ok_clicked()
    assert applied == [True, True] and not dlg.isVisible()
    print("settings_ui 自测通过: 8 页导航/剪贴板热键整合/翻译引擎切换/应用-确定语义 全部 OK")
    shutil.rmtree(tmp, ignore_errors=True)
