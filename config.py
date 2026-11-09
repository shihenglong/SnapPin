"""SnapPin 配置持久化 — 参照 PixPin 的配置结构。"""
import json
import os
import sys


def app_root() -> str:
    """应用根目录：源码运行 = 本文件所在目录；打包运行 = exe 所在目录
    （vendor 引擎随包放在 exe 旁边，onefile 解包临时目录里没有）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _migrate_old_dir() -> None:
    r"""一次性迁移：%APPDATA%\PyPin → %APPDATA%\SnapPin（改名前老用户数据不丢）。"""
    old = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PyPin")
    if os.path.isdir(old) and not os.path.isdir(APP_DIR):
        try:
            os.rename(old, APP_DIR)
        except Exception:
            pass  # 目录被占用等情况：放弃迁移，SnapPin 会新建自己的目录


APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SnapPin")
_migrate_old_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "hotkeys": {
        "capture": "Ctrl+1",          # 截图
        "close_all_pins": "Alt+2",    # 关闭所有贴图窗口
        "hide_show_pins": "Ctrl+Shift+2",  # 隐藏/显示所有贴图
        "pin_clipboard": "Ctrl+Alt+V",     # 剪贴板贴图
    },
    "capture": {
        "ui_detect": True,          # 窗口吸附
        "border_width": 3,          # 选区边框宽度
        "show_hint": True,          # 显示快捷键提示面板
        "last_color": "#2E7CF6",    # 记住上次画笔颜色
        "autosave": False,          # 截图完成后自动保存
        "retention_days": 30,       # 自动保存保留天数（0=永久）
        "corner_radius": 8,         # 截图圆角半径（0=直角）
    },
    "pin": {
        "opacity": 100,             # 贴图窗口默认透明度
        "shadow": True,             # 显示窗口阴影
        "wheel_zoom_anchor": "topleft",
    },
    "system": {
        "tray_single_click": "settings",  # settings / capture / pin / none
        "autostart": False,
        "check_updates": True,     # 启动后静默检查新版本（GitHub Releases）
    },
    "translate": {
        "engine": "online",     # online / glm / minimax / custom / ollama / local
        "api_base": "",         # 空 = 引擎预设端点（Ollama: http://localhost:11434）
        "api_key": "",          # API 密钥（在线直连 / Ollama / 离线插件无需）
        "model": "",            # 空 = 引擎预设模型（Ollama 自动检测，Qwen/DeepSeek 优先）
        "target": "zh-CN",      # 翻译目标语言
    },
    # === clip/pwman 挂载点：剪贴板历史 + 密码管理 ===
    "clip": {
        "enabled": True,           # 复制即自动记录
        "hotkey": "Alt+V",         # 打开剪贴板历史
        "db_path": "",             # 空 = %APPDATA%/SnapPin/clip.db
        "popup_pos": "cursor",     # cursor / center / remember
        "pin_stay": False,         # 锁定状态：粘贴后窗口保持打开
        "retention_days": 7,       # 历史保留天数（0=永久，常用项不受影响）
        "max_entries": 500,        # 最大条数（0=不限）
    },
    "pwman": {
        "db_path": "",             # 空 = %APPDATA%/SnapPin/pw.db
    },
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    # 一次性迁移：托盘单击由「截图」改为「打开设置」（2026-11 起新默认）
    if cfg["system"].get("tray_single_click") == "capture":
        cfg["system"]["tray_single_click"] = "settings"
    return cfg


def clip_db_path() -> str:
    """剪贴板历史默认数据库路径。"""
    return os.path.join(APP_DIR, "clip.db")


def pw_db_path() -> str:
    """密码管理默认数据库路径。"""
    return os.path.join(APP_DIR, "pw.db")


def save(cfg: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def cleanup_old_shots(days: int) -> int:
    """删除超过保留天数的自动保存截图，返回删除数。"""
    import glob
    import time
    if not days or days <= 0:
        return 0
    d = os.path.join(os.path.expanduser("~"), "Pictures", "SnapPin")
    cutoff = time.time() - days * 86400
    removed = 0
    for f in glob.glob(os.path.join(d, "*.png")):
        try:
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
                removed += 1
        except Exception:
            pass
    return removed


def set_autostart(enable: bool) -> None:
    """开机自启 — 写 HKCU Run 键：打包运行启动 exe；源码运行用 pythonw 启动 main.py。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                python = sys.executable.replace("python.exe", "pythonw.exe")
                main_py = os.path.join(app_root(), "main.py")
                cmd = f'"{python}" "{main_py}"'
            winreg.SetValueEx(key, "SnapPin", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "SnapPin")
            except FileNotFoundError:
                pass
            try:  # 清掉改名前的旧键
                winreg.DeleteValue(key, "PyPin")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass
