"""窗口强制置前 — 托盘菜单/单实例二启等「进程无前台权限」场景。

热键路径没这个问题：RegisterHotKey 触发时系统会给进程前台权限；
但托盘菜单点击、第二个实例启动等路径没有权限，Qt 的 raise_/activateWindow
映射的 SetForegroundWindow 会被 Windows 静默拒绝——窗口沉底、任务栏闪烁，
用户必须手动点任务栏才能看到（设置窗口沉底 bug 的根因）。

修法与 clipwin._focus_target 同源：AttachThreadInput 挂到前台线程输入队列
绕过限制，TOPMOST→NOTOPMOST 抬升兜底，最后以 GetForegroundWindow 核实。
"""
import ctypes


def force_front(widget) -> bool:
    """把 Qt widget 真正抬到前台并核实；失败返回 False（调用方可兜底）。

    必须在 widget.show() 之后调用（winId() 才是有效原生句柄）。
    """
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hwnd = int(widget.winId())
        if not hwnd or not u32.IsWindow(hwnd):
            return False
        if u32.GetForegroundWindow() == hwnd:
            return True
        # 第一轮：常规抬升（有前台权限时到这里就够了）
        widget.raise_()
        widget.activateWindow()
        if u32.GetForegroundWindow() == hwnd:
            return True
        # 第二轮：挂输入队列 + TOPMOST 抬升
        # 托盘菜单路径下菜单刚关闭、前台权限已过期，后台进程直接
        # SetForegroundWindow 会被前台锁静默拒绝——先模拟一次 ALT 击键，
        # 让系统认为有用户输入（窗口管理器通用的绕过手法）。
        u32.keybd_event(0x12, 0, 0, 0)          # ALT down
        u32.keybd_event(0x12, 0, 2, 0)          # ALT up（KEYEVENTF_KEYUP）
        this_tid = k32.GetCurrentThreadId()
        tids = []
        for w in (u32.GetForegroundWindow(), hwnd):
            tid = u32.GetWindowThreadProcessId(w, None)
            if tid and tid != this_tid and tid not in tids:
                tids.append(tid)
        attached = [tid for tid in tids if u32.AttachThreadInput(this_tid, tid, True)]
        try:
            SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x2, 0x1, 0x40
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            u32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            u32.SetFocus(hwnd)
        finally:
            for tid in attached:
                u32.AttachThreadInput(this_tid, tid, False)
        return u32.GetForegroundWindow() == hwnd
    except Exception:
        return False


if __name__ == "__main__":
    # 真桌面自测（offscreen 无 Z 序，必须真窗口）：QT 不设 offscreen 直接跑本文件。
    # 模拟用户场景：后台进程（终端在前台）由 QTimer 弹设置窗 → 应自动到最前。
    import sys

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication(sys.argv)
    results = {}

    def _open():
        dlg = QDialog()
        dlg.setWindowTitle("winfront 自测")
        dlg.show()
        results["ok"] = force_front(dlg)
        u32 = ctypes.windll.user32
        results["fg_is_mine"] = u32.GetForegroundWindow() == int(dlg.winId())
        dlg.close()
        app.quit()

    QTimer.singleShot(1500, _open)   # 此时终端在前台，本进程无前台权限
    app.exec()
    print("force_front:", results["ok"], "| GetForegroundWindow==自己:", results["fg_is_mine"])
    sys.exit(0 if (results.get("ok") and results.get("fg_is_mine")) else 1)
