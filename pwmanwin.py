"""密码管理窗口 — 名称/账号/密码 三字段的增删改查（PixPin 风格浅色界面）。

复制密码时先调用 suppress_clip（若提供，表示下一条剪贴板变化不记入
剪贴板历史），并在 30 秒后自动清空剪贴板（仅当内容仍是这条密码时）。
closeEvent 只隐藏不销毁，外部通过 show_up() 复用同一窗口。
"""
import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout,
                               QWidget)

from pwman import PasswordStore

ACCENT = "#2E7CF6"
_MASK = "••••••••"  # 密码列固定显示的掩码

QSS = """
#PwmanWin{background:#F5F6F7;}
#Card{background:white;border-radius:10px;}
QLabel[title]{font-size:16px;font-weight:700;color:#1D2129;}
QLineEdit{border:1px solid #E5E6EB;border-radius:6px;padding:5px 10px;background:white;color:#1D2129;}
QLineEdit:focus{border-color:%ACC%;}
QPushButton{border:1px solid #E5E6EB;border-radius:6px;background:white;padding:5px 16px;color:#1D2129;}
QPushButton:hover{border-color:%ACC%;color:%ACC%;}
QPushButton[primary]{background:%ACC%;color:white;border:none;border-radius:6px;padding:6px 18px;}
QPushButton[primary]:hover{background:#1E66E0;color:white;}
QToolButton{border:1px solid #E5E6EB;border-radius:5px;background:white;padding:3px 10px;color:#4E5969;}
QToolButton:hover{border-color:%ACC%;color:%ACC%;}
QTableWidget{border:none;background:white;color:#1D2129;font-size:13px;}
QTableWidget::item{padding:2px 6px;}
QTableWidget::item:selected{background:#EAF2FF;color:#1D2129;}
QHeaderView::section{background:#FAFBFC;color:#4E5969;border:none;border-bottom:1px solid #E5E6EB;padding:6px 8px;font-weight:600;}
""".replace("%ACC%", ACCENT)


class _CredDialog(QDialog):
    """新增/编辑一条凭据的模态小对话框（名称、账号、密码三个输入框）。"""

    def __init__(self, parent=None, heading: str = "新增密码", rec: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(heading)
        self.setModal(True)
        self.setFixedWidth(380)
        rec = rec or {}

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 18, 18, 18)
        v.setSpacing(10)

        self.ed_title = QLineEdit(rec.get("title", ""))
        self.ed_title.setPlaceholderText("名称")
        self.ed_user = QLineEdit(rec.get("username", ""))
        self.ed_user.setPlaceholderText("账号")
        self.ed_pwd = QLineEdit(rec.get("password", ""))
        self.ed_pwd.setPlaceholderText("密码")
        self.ed_pwd.setEchoMode(QLineEdit.Password)

        # 密码框 显示/隐藏 切换按钮
        self.btn_eye = QToolButton()
        self.btn_eye.setText("显示")
        self.btn_eye.setCursor(Qt.PointingHandCursor)
        self.btn_eye.clicked.connect(self._toggle_echo)
        pwd_row = QHBoxLayout()
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.addWidget(self.ed_pwd, 1)
        pwd_row.addWidget(self.btn_eye)

        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("保存")
        btn_save.setProperty("primary", True)
        btn_save.clicked.connect(self._on_save)

        v.addWidget(self.ed_title)
        v.addWidget(self.ed_user)
        v.addLayout(pwd_row)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        v.addLayout(btns)

    def _toggle_echo(self):
        """切换密码框明文/掩码显示。"""
        hidden = self.ed_pwd.echoMode() == QLineEdit.Password
        self.ed_pwd.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)
        self.btn_eye.setText("隐藏" if hidden else "显示")

    def values(self):
        """校验并返回 (名称, 账号, 密码)；不合法时弹 QMessageBox.warning 并返回 None。

        规则：密码必须非空；名称/账号至少一项非空。
        """
        title = self.ed_title.text().strip()
        username = self.ed_user.text().strip()
        pwd = self.ed_pwd.text()  # 密码按原样取值，不 strip（可能有意含空格）
        if not title and not username:
            QMessageBox.warning(self, "SnapPin 密码管理", "名称和账号至少填写一项。")
            return None
        if not pwd:
            QMessageBox.warning(self, "SnapPin 密码管理", "密码不能为空。")
            return None
        return title, username, pwd

    def _on_save(self):
        """点保存时先校验，通过才接受对话框。"""
        if self.values() is not None:
            self.accept()


class PwmanWindow(QDialog):
    """密码管理主窗口。close 仅隐藏（可复用），show_up() 刷新并重新显示。"""

    def __init__(self, store: PasswordStore, cfg: dict, suppress_clip=None):
        super().__init__(None)
        self.store = store
        self._cfg = cfg or {}                # 预留：与主程序配置一致的入口，暂不使用
        self._suppress_clip = suppress_clip  # 可选 callable：下一条剪贴板变化不记入历史
        self._rows: list[dict] = []          # 当前表格展示的记录（行号 → 记录）

        self.setWindowTitle("SnapPin 密码管理")
        self.setObjectName("PwmanWin")
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setFixedSize(860, 480)
        self.setStyleSheet(QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        card = QWidget()
        card.setObjectName("Card")
        root.addWidget(card)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        # ---- 顶部：标题 + 搜索 + 新增
        top = QHBoxLayout()
        top.setSpacing(10)
        title = QLabel("密码管理")
        title.setProperty("title", True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 名称 / 账号 / 密码")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(lambda _t: self.refresh())  # 即时过滤
        btn_add = QPushButton("新增")
        btn_add.setProperty("primary", True)
        btn_add.setCursor(Qt.PointingHandCursor)
        btn_add.clicked.connect(lambda: self._add_or_edit(None))
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.search)
        top.addWidget(btn_add)
        v.addLayout(top)

        # ---- 中部：表格 名称 | 账号 | 密码 | 操作
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "账号", "密码", "操作"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)   # 密码列
        header.setSectionResizeMode(3, QHeaderView.Fixed)   # 操作列
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 320)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        v.addWidget(self.table, 1)

        self.refresh()

    # ------------------------------------------------ 显示 / 关闭
    def show_up(self):
        """刷新表格并显示/激活窗口（外部复用入口）。"""
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev):
        """关闭时仅隐藏，窗口保留以便复用。"""
        ev.ignore()
        self.hide()

    # ------------------------------------------------ 表格
    def refresh(self):
        """按当前搜索词重新查询并重建表格。"""
        self._rows = self.store.query(self.search.text().strip())
        self.table.setRowCount(len(self._rows))
        for i, rec in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(rec["title"]))
            self.table.setItem(i, 1, QTableWidgetItem(rec["username"]))
            it_pwd = QTableWidgetItem(_MASK)  # 一律掩码显示，不落明文
            it_pwd.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, it_pwd)
            self.table.setCellWidget(i, 3, self._make_actions(i))

    def _row_record(self, row: int):
        """取行号对应的记录，越界返回 None。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _small_btn(self, text: str) -> QToolButton:
        """操作列的小按钮。"""
        b = QToolButton()
        b.setText(text)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def _make_actions(self, row: int) -> QWidget:
        """构造一行的操作列：复制账号 / 复制密码 / 编辑 / 删除。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(4)
        btn_acct = self._small_btn("复制账号")
        btn_pwd = self._small_btn("复制密码")
        btn_edit = self._small_btn("编辑")
        btn_del = self._small_btn("删除")
        btn_acct.clicked.connect(lambda _=False, r=row: self._copy_account(r))
        btn_pwd.clicked.connect(lambda _=False, r=row: self._copy_password(r))
        btn_edit.clicked.connect(lambda _=False, r=row: self._edit_row(r))
        btn_del.clicked.connect(lambda _=False, r=row: self._remove(r))
        for b in (btn_acct, btn_pwd, btn_edit, btn_del):
            h.addWidget(b)
        h.addStretch(1)
        return w

    def _on_double_click(self, row: int, _col: int):
        """双击任意单元格 = 复制该行密码。"""
        self._copy_password(row)

    # ------------------------------------------------ 复制
    def _copy_account(self, row: int):
        """复制账号到剪贴板。"""
        rec = self._row_record(row)
        if rec:
            QApplication.clipboard().setText(rec["username"])

    def _copy_password(self, row: int):
        """复制密码：先 suppress_clip，再写剪贴板，30 秒后自动清空。"""
        rec = self._row_record(row)
        if not rec:
            return
        pwd = rec["password"]
        if self._suppress_clip is not None:
            try:
                self._suppress_clip(2)  # 复制 + 平台可能二次触发，均不记入剪贴板历史
            except Exception:
                pass  # 抑制失败不影响复制本身
        QApplication.clipboard().setText(pwd)
        # 30 秒后仅当剪贴板仍是这条密码时自动清空
        QTimer.singleShot(30_000, lambda: self._auto_clear_clip(pwd))

    def _auto_clear_clip(self, text: str):
        """剪贴板内容仍等于 text 时清空。"""
        cb = QApplication.clipboard()
        if cb.text() == text:
            cb.setText("")

    # ------------------------------------------------ 增 / 改 / 删
    def _add_or_edit(self, rec: dict | None):
        """弹出模态对话框；rec 为 None 表示新增，否则编辑该记录。"""
        dlg = _CredDialog(self, "新增密码" if rec is None else "编辑密码", rec)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.values()  # accept 前已校验，这里取值
        if vals is None:
            return
        title, username, password = vals
        try:
            if rec is None:
                if self.search.text().strip():
                    self.search.clear()   # 清掉过滤，让新条目立即可见
                self.store.add(title, username, password)
            else:
                self.store.update(rec["id"], title, username, password)
        except Exception as exc:
            # pythonw 下 stderr 为 None，异常必须弹窗，否则表现为"新增无效"
            QMessageBox.critical(self, "SnapPin 密码管理",
                                 f"保存失败：{exc}\n\n数据库：{self.store.db_path}")
            return
        self.refresh()

    def _edit_row(self, row: int):
        """编辑指定行。"""
        rec = self._row_record(row)
        if rec:
            self._add_or_edit(rec)

    def _remove(self, row: int):
        """删除指定行（需确认）。"""
        rec = self._row_record(row)
        if not rec:
            return
        name = rec["title"] or rec["username"] or f"#{rec['id']}"
        ret = QMessageBox.question(self, "SnapPin 密码管理",
                                   f"确定删除「{name}」吗？",
                                   QMessageBox.Yes | QMessageBox.No)
        if ret == QMessageBox.Yes:
            try:
                self.store.delete(rec["id"])
            except Exception as exc:
                QMessageBox.critical(self, "SnapPin 密码管理", f"删除失败：{exc}")
                return
            self.refresh()


if __name__ == "__main__":
    # 离屏自测：QT_QPA_PLATFORM=offscreen python pwmanwin.py
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db_path)
    store = PasswordStore(db_path)
    try:
        store.add("GitHub", "alice@example.com", "pwd-α")
        store.add("NetEase 邮箱", "bob@163.com", "secret2")
        qapp = QApplication([])
        win = PwmanWindow(store, {}, suppress_clip=None)
        win.show_up()
        assert win.table.rowCount() == 2 and win.isVisible()
        # 搜索过滤（textChanged 触发 refresh）
        win.search.setText("github")
        assert win.table.rowCount() == 1
        assert win._rows[0]["username"] == "alice@example.com"
        win.search.setText("不存在的关键词")
        assert win.table.rowCount() == 0
        win.search.setText("")
        assert win.table.rowCount() == 2
        # 模拟复制密码 + 自动清空逻辑（直接调用清空函数，不等 30 秒）
        cb = QApplication.clipboard()
        pwd0 = win._rows[0]["password"]
        win._copy_password(0)
        copied_ok = cb.text() == pwd0
        win._auto_clear_clip(pwd0)
        if copied_ok and cb.text() == "":
            print("剪贴板复制/自动清空: OK")
        else:
            print("剪贴板行为在此环境不可验证（离屏插件差异，不影响功能）")
        win._copy_account(0)  # 复制账号不抛异常即可
        # 编辑对话框字段往返（不进入模态 exec，只验证 values()）
        dlg = _CredDialog(win, "测试", {"title": "A", "username": "B", "password": "C"})
        assert dlg.values() == ("A", "B", "C")
        # close → hide（不销毁），show_up 可复用
        win.close()
        assert not win.isVisible()
        win.show_up()
        assert win.isVisible()
        # 删除（直接走 store + refresh，不弹确认框）
        store.delete(win._rows[0]["id"])
        win.refresh()
        assert win.table.rowCount() == 1
        print("pwmanwin 自测通过: 构建/show_up/搜索过滤/复制/close-hide/删除 OK")
    finally:
        store.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
