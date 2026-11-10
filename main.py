"""SnapPin — PixPin 风格截图贴图工具（开源复刻 MVP）。

用法: pythonw main.py
快捷键: Ctrl+1 截图 · Ctrl+Alt+V 剪贴板贴图 · Alt+2 关闭全部贴图 · Ctrl+Shift+2 隐藏/显示
"""
import os
import sys
import threading
import faulthandler
import config as _config

if sys.stderr is None:            # pythonw/打包 exe 无控制台：重定向到日志文件
    try:
        os.makedirs(_config.APP_DIR, exist_ok=True)   # 新机器首启目录还不存在
        _log_path = os.path.join(_config.APP_DIR, "snappin.log")
        sys.stderr = open(_log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr
    except Exception:
        sys.stderr = open(os.devnull, "w")   # 目录不可写也不能挡启动
        sys.stdout = sys.stderr
if sys.stderr is not None:
    faulthandler.enable()         # 崩溃栈也会写进日志

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QMenu, QMessageBox, QSystemTrayIcon,
                               QWidget)

import config
from icons import APP_ICON_SVG
from hotkeys import HotkeyManager
from ocr import OcrRunner, available as ocr_available
from translate import TranslateClient

HK_CAPTURE, HK_PIN, HK_CLOSE, HK_HIDE, HK_CLIP = 1, 2, 3, 4, 5


class _Relay(QObject):
    """App 非 QObject：更新检查子线程回调经此信号排队回主线程。"""
    fire = Signal(object)


def app_icon() -> QIcon:
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QPainter
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    QSvgRenderer(APP_ICON_SVG.encode()).render(p)
    p.end()
    return QIcon(pm)


class SingleInstance:
    """单实例守卫：命名互斥体判唯一性，本地套接字只负责「第二次启动时
    通知首个实例打开设置」。

    旧实现只靠 QLocalSocket 探测：`waitForConnected(300)` 在冷启动/杀毒
    扫描下会超时，于是第二进程误判自己是首个实例，还对**正在运行的实例**
    执行 QLocalServer.removeServer() —— 两个实例并存，抢热键、抢托盘，
    表现为「快捷键失效 + 托盘图标消失」。互斥体不受连接时序影响。
    """
    name = "SnapPin-Instance-7f2a"
    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, on_second):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        # handle 必须保活到进程结束：存成属性，交由 Python 对象生命周期持有
        self._mutex = k32.CreateMutexW(None, False, "Local\\" + self.name)
        already = (k32.GetLastError() == self._ERROR_ALREADY_EXISTS) \
            if self._mutex else False
        self.is_first = not already
        self.on_second = on_second
        if not self.is_first:
            self._notify_first()          # 已有实例：通知它打开设置后立刻退出
            return
        from PySide6.QtNetwork import QLocalServer
        QLocalServer.removeServer(self.name)   # 清理上次异常退出残留的管道
        self.server = QLocalServer()
        self.server.newConnection.connect(self._on_conn)
        self.server.listen(self.name)

    def _on_conn(self):
        c = self.server.nextPendingConnection()
        if c is not None:
            c.readAll()
            c.disconnectFromServer()
        if self.on_second:
            self.on_second()

    def _notify_first(self):
        from PySide6.QtNetwork import QLocalSocket
        s = QLocalSocket()
        s.connectToServer(self.name)
        if s.waitForConnected(1500):
            s.write(b"show")
            s.flush()
            s.waitForBytesWritten(300)
            s.disconnectFromServer()


class App:
    def __init__(self, qapp: QApplication):
        self.qapp = qapp
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setWindowIcon(app_icon())
        self.cfg = config.load()
        self._hk_fail_pending = []       # 热键失败提示聚合（防连发）
        self._hk_fail_timer = None
        self._settings_dlg = None        # 设置页单例（非模态，可与截图/贴图共存）
        self.ocr = OcrRunner() if ocr_available() else None
        self.translator = TranslateClient(self.cfg)
        self._pins_hidden = False

        self._make_tray()
        self._start_hotkeys()
        # === clip/pwman 挂载点：剪贴板历史 + 密码管理 ===
        self._pw_store = None
        self._pw_win = None
        self._pw_db = None
        self._init_clip()
        self._overlay = None
        self._warm_modules()             # 后台预热重依赖，首次热键不卡顿
        if self.cfg["capture"].get("autosave"):
            config.cleanup_old_shots(self.cfg["capture"].get("retention_days", 30))
        # 启动气泡：常驻应用每次启动给一次存在感提示（热键取实际配置）
        self.tray.showMessage("SnapPin 已启动",
                              f"{self.cfg['hotkeys']['capture']} 截图 · "
                              f"{self.cfg['clip'].get('hotkey', 'Alt+V')} 剪贴板历史 · "
                              f"双击托盘打开设置",
                              QSystemTrayIcon.Information, 2500)
        # 更新检查：启动 12s 后静默查一次 GitHub Releases（可在设置关闭）
        self._upd_url = None
        self._relay = _Relay()
        self._relay.fire.connect(self._on_update_status)
        if self.cfg["system"].get("check_updates", True):
            QTimer.singleShot(12_000, self._check_updates_silent)

    # ------------------------------------------------ 检查更新
    def _check_updates_silent(self):
        import updater
        updater.check_async(lambda s, p: self._relay.fire.emit((s, p)))

    def _on_update_status(self, pair):
        status, payload = pair
        if status != "update":
            return                            # 启动静默检查：仅发现新版才打扰
        info = payload
        self._upd_url = info["url"]
        self.tray.showMessage(f"SnapPin 发现新版本 {info['tag']}",
                              "点击此气泡打开下载页（也可在 设置→关于 手动检查）",
                              QSystemTrayIcon.Information, 8000)

    def _on_tray_message_clicked(self):
        if self._upd_url:
            QDesktopServices.openUrl(QUrl(self._upd_url))
            self._upd_url = None

    @staticmethod
    def _warm_modules():
        """后台线程预 import 截图/贴图模块（numpy 等大依赖），
        首次按热键时只剩抓屏一步同步开销。"""
        def _do():
            for name in ("capture", "pinwin"):
                try:
                    __import__(name)
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True, name="snappin-warmup").start()

    # ------------------------------------------------ 托盘
    def _make_tray(self):
        self.tray = QSystemTrayIcon(app_icon())
        self.tray.setToolTip("SnapPin — 截图 / 贴图 / 剪贴板历史 / 密码管理")
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu{background:white;border:1px solid #E5E6EB;border-radius:8px;padding:4px;}"
            "QMenu::item{padding:6px 30px;border-radius:5px;}"
            "QMenu::item:selected{background:#EAF2FF;color:#2E7CF6;}")
        act_shot = QAction(f"截图 ({self.cfg['hotkeys']['capture']})", menu)
        act_pin = QAction("贴图（剪贴板）", menu)
        act_hide = QAction("隐藏/显示贴图", menu)
        act_close = QAction("关闭所有贴图", menu)
        act_clip = QAction(
            f"剪贴板历史 ({self.cfg['clip'].get('hotkey', 'Alt+V')})", menu)
        self._act_shot, self._act_clip = act_shot, act_clip
        act_pw = QAction("密码管理", menu)
        act_settings = QAction("设置…", menu)
        act_quit = QAction("退出", menu)
        act_shot.triggered.connect(self.start_capture)
        act_pin.triggered.connect(self.pin_clipboard)
        act_hide.triggered.connect(self.toggle_pins)
        act_close.triggered.connect(self.close_pins)
        act_clip.triggered.connect(self.toggle_clip)
        act_pw.triggered.connect(self.open_pwman)
        act_settings.triggered.connect(self.open_settings)
        act_quit.triggered.connect(self.quit)
        for a in (act_shot, act_pin, act_hide, act_close, act_clip):
            menu.addAction(a)
        menu.addSeparator()
        self._menu = menu
        menu.addAction(act_pw)
        menu.addAction(act_settings)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.show()

    def _tray_activated(self, reason):
        act = self.cfg["system"].get("tray_single_click", "settings")
        if reason == QSystemTrayIcon.Trigger:
            if act == "settings":
                self.open_settings()
            elif act == "capture":
                self.start_capture()
            elif act == "pin":
                self.pin_clipboard()

    # ------------------------------------------------ 热键
    _HK_NAMES = {HK_CAPTURE: "截图", HK_PIN: "剪贴板贴图", HK_CLOSE: "关闭所有贴图",
                 HK_HIDE: "隐藏/显示贴图", HK_CLIP: "剪贴板历史"}

    def _start_hotkeys(self):
        hk = self.cfg["hotkeys"]
        combos = {HK_CAPTURE: hk["capture"], HK_PIN: hk["pin_clipboard"],
                  HK_CLOSE: hk["close_all_pins"], HK_HIDE: hk["hide_show_pins"],
                  HK_CLIP: self.cfg["clip"].get("hotkey", "Alt+V")}
        # 同一组合分给多个功能时，后注册的必然失败 → 提前剔除并提示
        seen = set()
        for hid in list(combos):
            c = combos[hid]
            if c in seen:
                self._queue_hk_fail(f"{self._HK_NAMES[hid]}（{c} 与其他功能重复）")
                del combos[hid]
            else:
                seen.add(c)
        self.hotkeys = HotkeyManager(combos)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.registration_failed.connect(self._hk_failed)

    def _hk_failed(self, hk_id):
        self._queue_hk_fail(
            f"{self._HK_NAMES.get(hk_id, f'#{hk_id}')} 注册失败（可能被其他软件占用），"
            f"可在 设置→快捷键 修改")

    def _queue_hk_fail(self, text: str):
        self._hk_fail_pending.append(text)
        if self._hk_fail_timer is None:
            self._hk_fail_timer = QTimer()  # App 非 QObject,不能作 parent,靠引用保活
            self._hk_fail_timer.setSingleShot(True)
            self._hk_fail_timer.setInterval(600)
            self._hk_fail_timer.timeout.connect(self._show_hk_failed)
        self._hk_fail_timer.start()

    def _show_hk_failed(self):
        if not self._hk_fail_pending:
            return
        self.tray.showMessage("SnapPin 快捷键", "\n".join(self._hk_fail_pending),
                              QSystemTrayIcon.Information, 4500)
        self._hk_fail_pending = []

    def _on_hotkey(self, hk_id):
        if hk_id == HK_CAPTURE:
            self.start_capture()
        elif hk_id == HK_PIN:
            self.pin_clipboard()
        elif hk_id == HK_CLOSE:
            self.close_pins()
        elif hk_id == HK_HIDE:
            self.toggle_pins()
        elif hk_id == HK_CLIP:
            self.toggle_clip()

    # ------------------------------------------------ 剪贴板历史/密码管理（clip/pwman 挂载点）
    def toggle_clip(self):
        if self.clip_win:
            self.clip_win.popup()

    def _ensure_pw_store(self, quiet: bool = False):
        """惰性创建/复用密码库连接；db 路径变更时重建（旧窗口一并关掉）。

        quiet=True 供剪贴板窗口搜索时调用：失败不弹窗只返回 None。
        """
        new_db = self.cfg["pwman"].get("db_path") or config.pw_db_path()
        if self._pw_store is not None and self._pw_db == new_db:
            return self._pw_store
        if self._pw_win is not None:      # 旧窗口指向旧库
            self._pw_win.hide()
            self._pw_win.deleteLater()
            self._pw_win = None
        if self._pw_store is not None:
            try:
                self._pw_store.close()
            except Exception:
                pass
        try:
            d = os.path.dirname(new_db)
            if d:
                os.makedirs(d, exist_ok=True)
            from pwman import PasswordStore
            self._pw_store = PasswordStore(new_db)
            self._pw_db = new_db
            return self._pw_store
        except Exception as exc:
            self._pw_store = None
            self._pw_db = None
            if not quiet:
                QMessageBox.critical(None, "SnapPin 密码管理",
                                     f"密码库打开失败：{exc}\n\n路径：{new_db}")
            return None

    def open_pwman(self):
        store = self._ensure_pw_store()
        if store is None:
            return
        if self._pw_win is None:
            from pwmanwin import PwmanWindow
            sup = self.clip_monitor.suppress_next if self.clip_monitor else None
            self._pw_win = PwmanWindow(store, self.cfg, sup)
        self._pw_win.show_up()

    def _init_clip(self):
        self.clip_store = None
        self.clip_monitor = None
        self.clip_win = None
        self.clip_sync = None
        self._clip_db = None
        cc = self.cfg["clip"]
        if not cc.get("enabled", True):
            return
        from clip import ClipStore, ClipMonitor
        from clipwin import ClipWindow
        self._clip_db = cc.get("db_path") or config.clip_db_path()
        d = os.path.dirname(self._clip_db)
        if d:
            os.makedirs(d, exist_ok=True)
        self.clip_store = ClipStore(self._clip_db)
        self.clip_monitor = ClipMonitor(self.clip_store, enabled=True)
        self.clip_win = ClipWindow(self.clip_store, self.cfg, self.clip_monitor,
                                   on_settings=self.open_settings,
                                   pw_provider=lambda: self._ensure_pw_store(quiet=True),
                                   on_pwman=self.open_pwman)
        # 局域网同步（Ditto「朋友」式）：本机新条目发给好友，收到的回放剪贴板
        from clipsync import ClipSync
        self.clip_sync = ClipSync(self.clip_store, self.cfg,
                                  suppress=self.clip_monitor.suppress_next)
        self.clip_monitor.changed.connect(self.clip_sync.send_clip)
        self.clip_sync.received.connect(self._on_clip_received)

    def _on_clip_received(self, _cid):
        """收到局域网条目：历史窗口开着就刷新，让远端条目立刻可见。"""
        if self.clip_win is not None and self.clip_win.isVisible():
            self.clip_win.refresh()

    def _teardown_clip(self):
        for attr in ("clip_monitor", "clip_win", "clip_sync"):
            obj = getattr(self, attr, None)
            if obj:
                try:
                    if hasattr(obj, "stop"):
                        obj.stop()
                    obj.hide()
                except Exception:
                    pass
                obj.deleteLater()
        self.clip_monitor = None
        self.clip_win = None
        self.clip_sync = None
        self.clip_store = None
        self._clip_db = None

    def _apply_clip_settings(self):
        """设置保存后同步剪贴板模块：开关/数据库路径变更时重建，保留策略即时生效。"""
        cc = self.cfg["clip"]
        enabled = cc.get("enabled", True)
        new_db = cc.get("db_path") or config.clip_db_path()
        on = self.clip_monitor is not None
        if enabled != on or (on and self._clip_db != new_db):
            self._teardown_clip()
            self._init_clip()
        elif self.clip_store:
            self.clip_store.retention_days = int(cc.get("retention_days", 7) or 0)
            self.clip_store.max_entries = int(cc.get("max_entries", 500) or 0)
            if self.clip_sync is not None:
                self.clip_sync.reload()       # 局域网同步开关/端口/好友即时生效
        err = getattr(self.clip_sync, "last_error", "") if self.clip_sync else ""
        if err:
            self.tray.showMessage("SnapPin 局域网同步", err,
                                  QSystemTrayIcon.Warning, 6000)

    # ------------------------------------------------ 截图
    def start_capture(self):
        if self._overlay is not None and self._overlay.isVisible():
            return
        from PySide6.QtGui import QCursor
        from capture import CaptureOverlay
        screen = self.qapp.screenAt(QCursor.pos()) or self.qapp.primaryScreen()
        self._overlay = CaptureOverlay(screen, self.cfg)
        self._overlay.setAttribute(Qt.WA_DeleteOnClose, True)
        self._overlay.sig_pin.connect(self._on_pin)
        self._overlay.sig_saved.connect(
            lambda p: self.tray.showMessage("SnapPin", f"已保存 {p}", QSystemTrayIcon.Information, 3000))
        self._overlay.sig_cancelled.connect(lambda: setattr(self, "_overlay", None))
        self._overlay.destroyed.connect(lambda: setattr(self, "_overlay", None))
        self._overlay.start()

    def _on_pin(self, pix: QPixmap, glob: QRect):
        try:
            from pinwin import PinWindow
            op = self.cfg["pin"].get("opacity", 100) / 100.0
            w = PinWindow(pix, glob.topLeft(), self.cfg, self.ocr, self.translator)
            w.show()
            w.setWindowOpacity(op)
            if self._pins_hidden:
                w.hide()
        except Exception:
            import traceback
            traceback.print_exc()

    def pin_clipboard(self):
        cb = QApplication.clipboard()
        pix = cb.pixmap()
        if pix.isNull():
            self.tray.showMessage("SnapPin", "剪贴板中没有图片", QSystemTrayIcon.Information, 2500)
            return
        from PySide6.QtGui import QCursor
        self._on_pin(pix, QRect(QCursor.pos(), pix.size()))

    # ------------------------------------------------ 贴图管理
    def close_pins(self):
        from pinwin import PinWindow
        PinWindow.close_all()

    def toggle_pins(self):
        from pinwin import PinWindow
        PinWindow.toggle_visible()
        self._pins_hidden = not self._pins_hidden

    # ------------------------------------------------ 设置
    def open_settings(self):
        """非模态单例：已打开则激活；不阻塞截图/贴图/热键等任何窗口。"""
        from settings_ui import SettingsDialog
        if self._settings_dlg is None:
            self._settings_dlg = SettingsDialog(self.cfg, self._apply_settings)
            self._settings_dlg._app = self  # 扩展设置页回调（打开密码管理等）
        self._settings_dlg.show_up()

    def _apply_settings(self):
        config.save(self.cfg)
        config.set_autostart(self.cfg["system"]["autostart"])
        if self.cfg["capture"].get("autosave"):
            n = config.cleanup_old_shots(self.cfg["capture"].get("retention_days", 30))
            if n:
                self.tray.showMessage("SnapPin", f"已清理 {n} 张过期截图",
                                      QSystemTrayIcon.Information, 2500)
        self.hotkeys.stop()
        self._start_hotkeys()
        self._apply_clip_settings()
        # 托盘菜单文案与换过的热键保持一致
        self._act_shot.setText(f"截图 ({self.cfg['hotkeys']['capture']})")
        self._act_clip.setText(
            f"剪贴板历史 ({self.cfg['clip'].get('hotkey', 'Alt+V')})")
        self.tray.showMessage("SnapPin", "设置已保存", QSystemTrayIcon.Information, 2000)

    def quit(self):
        self.hotkeys.stop()
        if getattr(self, "_pw_store", None):
            try:
                self._pw_store.close()
            except Exception:
                pass
        self.tray.hide()
        self.qapp.quit()


def main():
    # 防呆：环境里若带着 QT_QPA_PLATFORM=offscreen（CI / 某些 shell 会导出），
    # 主程序会静默进入离屏模式——没有托盘图标、窗口不可见、热键按下去也没
    # 反应，表现就是「启动了但什么都没有」。除显式要求外一律清掉。
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        os.environ.pop("QT_QPA_PLATFORM", None)
    qapp = QApplication(sys.argv)
    qapp.setApplicationName("SnapPin")
    # 强制浅色调色板：系统深色模式下，原生弹出层（下拉列表/tooltip）不再黑底白字失明
    from PySide6.QtGui import QPalette, QColor
    pal = qapp.palette()
    for role, color in ((QPalette.Window, "#F5F6F7"), (QPalette.WindowText, "#1D2129"),
                        (QPalette.Base, "#FFFFFF"), (QPalette.AlternateBase, "#F5F6F7"),
                        (QPalette.Text, "#1D2129"), (QPalette.Button, "#FFFFFF"),
                        (QPalette.ButtonText, "#1D2129"), (QPalette.ToolTipBase, "#FFFFFF"),
                        (QPalette.ToolTipText, "#1D2129"), (QPalette.Highlight, "#2E7CF6"),
                        (QPalette.HighlightedText, "#FFFFFF"),
                        (QPalette.PlaceholderText, "#9AA0AC")):
        pal.setColor(role, QColor(color))
    qapp.setPalette(pal)
    app_holder = {}

    def on_second():
        a = app_holder.get("app")
        if a:
            a.open_settings()

    si = SingleInstance(on_second)
    if not si.is_first:
        return 0

    app = App(qapp)
    app_holder["app"] = app
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
