"""全局热键 — RegisterHotKey + 独立消息循环线程。

一个功能可绑定多个组合：配置值是 "Ctrl+1, Ctrl+Alt+1" 这样的字符串
（也接受 list），由 split_combos() 拆分、HotkeyManager 逐组合注册。
"""
import ctypes
import re
from ctypes import wintypes

from PySide6.QtCore import QObject, QThread, Signal

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT, MOD_CTRL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT_THREAD = 0x0401  # 自定义退出消息

_VK_SPECIAL = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
    "ins": 0x2D, "del": 0x2E, "space": 0x20, "tab": 0x09,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}

# 主键盘符号键 → VK_OEM_*。字母/数字的 ord 恰好等于 VK 码，符号不是
# （如 ` 是 0x60=VK_NUMPAD0，真正的反引号是 VK_OEM_3=0xC0），必须查表。
_VK_PUNCT = {
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}


def parse_combo(text: str):
    """"Ctrl+Shift+2" -> (mods, vk)；解析失败返回 None。"""
    mods, key = 0, None
    for part in [p.strip().lower() for p in text.split("+") if p.strip()]:
        if part in ("ctrl", "control"):
            mods |= MOD_CTRL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "meta"):
            mods |= MOD_WIN
        else:
            key = part
    if key is None:
        return None
    if key in _VK_SPECIAL:
        vk = _VK_SPECIAL[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif len(key) == 1 and key in _VK_PUNCT:
        vk = _VK_PUNCT[key]
    else:
        return None
    return mods, vk


def split_combos(value) -> list:
    """配置值 → 组合列表：支持 "Ctrl+1"、"Ctrl+1, Ctrl+Alt+1" 或 list。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[,;，；\n]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def combo_key(text: str):
    """"ctrl+1" / "Ctrl+1" 归一化成 (mods, vk) 供去重比对；无法解析返回 None。"""
    return parse_combo(text or "")


class _HotkeyThread(QThread):
    fired = Signal(int)       # hotkey id
    failed = Signal(int)      # hotkey id 注册失败

    def __init__(self, combos, parent=None):
        """combos: list[(id, 'Ctrl+1')]"""
        super().__init__(parent)
        self.combos = combos
        self._thread_id = None

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        registered = []
        self._registered = registered
        for hk_id, combo in self.combos:
            parsed = parse_combo(combo)
            if not parsed:
                self.failed.emit(hk_id)
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                registered.append(hk_id)
            else:
                self.failed.emit(hk_id)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.fired.emit(msg.wParam)
            elif msg.message == WM_QUIT_THREAD:
                break

        for hk_id in registered:
            user32.UnregisterHotKey(None, hk_id)
        self._registered = []
        # 让线程自然结束

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT_THREAD, 0, 0)


_PENDING = []  # stop() 超时未退出的旧 HotkeyManager，防运行中 QThread 被析构


class HotkeyManager(QObject):
    """combos: {逻辑 id: "Ctrl+1" 或 "Ctrl+1, Ctrl+Alt+1"}。

    一个功能可绑多个组合：每个组合用独立的系统热键 id 注册，
    WM_HOTKEY 回来后统一翻回逻辑 id 再发 triggered()。
    """
    triggered = Signal(int)
    registration_failed = Signal(int, str)      # (逻辑 id, 注册失败的组合)

    _HK_ID_BASE = 100                           # 系统热键 id 起始值

    def __init__(self, combos, parent=None):
        super().__init__(parent)
        self._slots = {}                        # 系统热键 id → (逻辑 id, 组合)
        plan = []
        hk_id = self._HK_ID_BASE
        for lid, value in combos.items():
            seen = set()                        # 同一功能里重复填写的组合只注册一次
            for combo in split_combos(value):
                key = combo_key(combo)
                if key is not None and key in seen:
                    continue
                seen.add(key)
                self._slots[hk_id] = (lid, combo)
                plan.append((hk_id, combo))
                hk_id += 1
        self._thread = _HotkeyThread(plan)
        self._thread.fired.connect(self._on_fired)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_fired(self, hk_id):
        slot = self._slots.get(hk_id)
        if slot is not None:
            self.triggered.emit(slot[0])

    def _on_failed(self, hk_id):
        lid, combo = self._slots.get(hk_id, (hk_id, ""))
        self.registration_failed.emit(lid, combo)

    def stop(self):
        """请求退出并等待；线程刚启动尚未记录 thread_id 时先等它就绪再投递退出消息。"""
        t = self._thread
        for _ in range(50):                    # 最多等 0.5s 让 run() 记下 thread_id
            if t._thread_id or not t.isRunning():
                break
            QThread.msleep(10)
        t.stop()
        t.wait(2000)
        if t.isRunning():
            # 兜底：没等到退出的 QThread 不能随对象一起析构（Qt 会直接 abort），
            # 挂到全局池等 finished 后释放
            if self not in _PENDING:
                _PENDING.append(self)
            t.finished.connect(lambda m=self: _PENDING.remove(m) if m in _PENDING else None)


if __name__ == "__main__":
    # 离屏自测：QT_QPA_PLATFORM=offscreen python hotkeys.py
    # 会短暂注册真实的全局热键（Ctrl+Alt+J / Ctrl+Alt+K / Ctrl+Alt+`）
    import os
    import sys
    import time

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    # ---- parse_combo：符号键必须映射到正确的 VK_OEM_*（此前误用字符码 = 小键盘键）
    assert parse_combo("Ctrl+Shift+2") == (MOD_CTRL | MOD_SHIFT, 0x32)
    assert parse_combo("Alt+V") == (MOD_ALT, 0x56)
    assert parse_combo("F5") == (0, 0x74)
    assert parse_combo("Alt+`") == (MOD_ALT, 0xC0), parse_combo("Alt+`")
    assert parse_combo("Ctrl+-") == (MOD_CTRL, 0xBD)
    assert parse_combo("Shift+/") == (MOD_SHIFT, 0xBF)
    assert parse_combo("Ctrl+") is None
    assert parse_combo("nonsense") is None
    # 双快捷键：配置值拆分与归一化去重
    assert split_combos("Ctrl+1, Ctrl+Alt+1") == ["Ctrl+1", "Ctrl+Alt+1"]
    assert split_combos("Ctrl+1；Alt+V\n F5 ") == ["Ctrl+1", "Alt+V", "F5"]
    assert split_combos(["Ctrl+1", " "]) == ["Ctrl+1"]
    assert split_combos(None) == [] and split_combos("") == []
    assert combo_key("ctrl+1") == combo_key("Ctrl+1") != combo_key("Ctrl+Alt+1")

    app = QApplication(sys.argv)

    def wait_registered(mgr, want, timeout=2500):
        t0 = time.time()
        while time.time() - t0 < timeout / 1000:
            if getattr(mgr._thread, "_registered", None) == want:
                return True
            app.processEvents()
            QThread.msleep(10)
        return False

    # ---- 换热键的关键链路：stop() 后同组合必须能立即重新注册（= 旧线程确实注销了）
    combos = {11: "Ctrl+Alt+J, Ctrl+Alt+K", 12: "Ctrl+Alt+`"}
    m1 = HotkeyManager(combos)
    assert wait_registered(m1, [100, 101, 102]), "首次注册失败"
    # 一个功能的第二个组合也必须路由回同一逻辑 id（系统 id 101 → 功能 11）
    fired = []
    m1.triggered.connect(fired.append)
    m1._on_fired(101)
    m1._on_fired(102)
    assert fired == [11, 12], f"组合到功能的映射错误，实际 {fired}"
    m1.stop()
    m2 = HotkeyManager(combos)
    ok = wait_registered(m2, [100, 101, 102])
    m2.stop()
    assert ok, "stop() 后同组合重新注册失败：旧热键未被注销（换键不生效根因）"
    print("hotkeys 自测通过: 符号键 VK 映射 / 双快捷键路由 / stop 后重注册 全部 OK")
