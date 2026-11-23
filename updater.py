"""检查 / 下载 / 安装更新 —— GitHub Releases，纯 stdlib。

一条链：检查 → 有新版本自动后台下载 → zip 校验 → 生成更新脚本。
事件统一经 on_event(kind, payload)，可能在子线程触发，UI 侧自行切回主线程：

    checking     None
    update       {"tag", "url", "notes", "asset"}    发现新版本（随后自动下载）
    latest       {"tag", ...}                        已是最新
    downloading  (received, total)                   total=0 表示长度未知
    downloaded   {"path", "tag", "size"}             下载并校验通过
    installing   script_path                         更新脚本已拉起，应退出程序
    error        str                                 网络 / 校验 / 资产缺失
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

import version

UPDATE_DIR = os.path.join(tempfile.gettempdir(), "SnapPin-update")
CHUNK = 256 * 1024
_PROGRESS_INTERVAL = 0.15        # 进度回调最小间隔（秒），避免刷爆 UI 队列


def parse_ver(s: str) -> tuple:
    """'v0.4.1' / '0.4' → (0,4,1)；解析失败返回 (0,)。"""
    parts = re.findall(r"\d+", (s or "").strip().lstrip("vV"))
    return tuple(int(p) for p in parts[:3]) or (0,)


def is_newer(remote: str, local: str) -> bool:
    return parse_ver(remote) > parse_ver(local)


def install_dir() -> str:
    """新版本要覆盖到哪个目录：打包版 = exe 所在目录，源码运行 = 项目目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def is_installed_full(root: str = "") -> bool:
    """当前安装是否带离线翻译引擎（full 包）——决定升级该下哪个资产。"""
    exe = os.path.join(root or install_dir(), "vendor", "plugins", "translation",
                       "engine", "translate-engine.exe")
    return os.path.exists(exe)


def fetch_latest(timeout: float = 8.0) -> dict:
    """拉取最新 release 信息；失败抛异常。"""
    req = urllib.request.Request(version.RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data or not data.get("tag_name"):     # 无任何 release
        raise ValueError("no releases")
    return {"tag": data["tag_name"],
            "url": data.get("html_url") or version.RELEASE_PAGE,
            "notes": (data.get("body") or "").strip(),
            "assets": data.get("assets") or []}


def pick_asset(info: dict, root: str = "") -> dict:
    """从 release 资产里挑 Windows 包：装的是 full 就升 full，否则 lite。

    lite 的文件是 full 的子集，所以「lite 用 lite、full 用 full」既不多下
    170MB 离线引擎，也不会把已有的离线能力降级掉。"""
    zips = [a for a in (info.get("assets") or [])
            if str(a.get("name", "")).lower().endswith(".zip")]
    win = [a for a in zips if "windows" in a["name"].lower()] or zips
    if not win:
        return {}
    if is_installed_full(root):
        for a in win:
            if "full" in a["name"].lower():
                return a
    for a in win:
        if "full" not in a["name"].lower():
            return a
    return win[0]


def _download(url: str, dest: str, on_progress=None, timeout: float = 30.0) -> int:
    """流式下载到 dest（先写 .part 再改名，断点残留不会污染成品）。"""
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{version.APP_NAME}/{version.APP_VERSION}",
    })
    got = 0
    last = 0.0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            now = time.time()
            if on_progress and (now - last >= _PROGRESS_INTERVAL or got == total):
                last = now
                on_progress(got, total)
    os.replace(part, dest)
    return got


def is_valid_package(path: str) -> bool:
    """zip 完整性 + 必须真的含有 SnapPin.exe（防止拿到 404 页面/半包）。"""
    try:
        with zipfile.ZipFile(path) as z:
            if "SnapPin/SnapPin.exe" not in z.namelist():
                return False
            return z.testzip() is None
    except Exception:
        return False


def build_script(package: str, dest: str, launch: str = "") -> str:
    """生成更新脚本：等旧 exe 释放 → 解包 → 覆盖 → 重启 → 自清理。

    用 `move` 试探代替 PID 轮询：Windows 上运行中的 exe 改名必然失败，
    成功即代表进程已退出、文件可写。"""
    os.makedirs(UPDATE_DIR, exist_ok=True)
    script = os.path.join(UPDATE_DIR, "apply_update.cmd")
    launch = launch or f'"{os.path.join(dest, "SnapPin.exe")}"'
    self_del = 'start "" cmd /c "del /q ""%~f0"""'
    body = f"""@echo off
setlocal EnableExtensions
set "LOG=%~dp0update.log"
set "DEST={dest}"
set "ZIP={package}"
set "TMP=%~dp0unpack"
echo [%DATE% %TIME%] update start>>"%LOG%"
set /a N=0
:wait
set /a N+=1
if %N% GTR 60 goto run
if not exist "%DEST%\\SnapPin.exe" goto run
move /y "%DEST%\\SnapPin.exe" "%DEST%\\SnapPin.exe.old" >nul 2>&1
if errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )
:run
if exist "%TMP%" rmdir /s /q "%TMP%"
mkdir "%TMP%"
tar -xf "%ZIP%" -C "%TMP%" >>"%LOG%" 2>&1
if not exist "%TMP%\\SnapPin\\SnapPin.exe" powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMP%' -Force" >>"%LOG%" 2>&1
if not exist "%TMP%\\SnapPin\\SnapPin.exe" goto failed
robocopy "%TMP%\\SnapPin" "%DEST%" /E /IS /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >>"%LOG%" 2>&1
if errorlevel 8 goto failed
if exist "%DEST%\\SnapPin.exe.old" del /q "%DEST%\\SnapPin.exe.old" >nul 2>&1
rmdir /s /q "%TMP%" >nul 2>&1
echo [%DATE% %TIME%] ok>>"%LOG%"
del /q "%ZIP%" >nul 2>&1
start "" {launch}
goto cleanup

:failed
echo [%DATE% %TIME%] FAILED>>"%LOG%"
if not exist "%DEST%\\SnapPin.exe" if exist "%DEST%\\SnapPin.exe.old" move /y "%DEST%\\SnapPin.exe.old" "%DEST%\\SnapPin.exe" >nul 2>&1
if exist "%DEST%\\SnapPin.exe" start "" {launch}
start "" notepad "%LOG%"

:cleanup
{self_del}
"""
    # 系统 ANSI 编码写 .cmd（中文安装路径必须如此，UTF-8 会被 cmd 解析错）
    with open(script, "w", encoding="mbcs", errors="replace", newline="\r\n") as f:
        f.write(body)
    return script


def launch_update(package: str, dest: str = "", launch: str = "") -> str:
    """拉起更新脚本（脱离本进程、无控制台窗口），返回脚本路径。"""
    script = build_script(package, dest or install_dir(), launch)
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(["cmd", "/c", script], close_fds=True,
                     creationflags=flags or 0)
    return script


class UpdateFlow:
    """检查 → 下载 → 安装的状态机；多个订阅者共享同一份进度/成品包。

    共享是必要的：主程序（启动静默检查）和设置页各自都要显示状态，但
    绝不能让两边同时往同一个 .part 里写。
    """

    def __init__(self, root: str = ""):
        self.root = root or install_dir()
        self.info = None          # 最新 release
        self.package = None       # 已下载并校验的 zip
        self.tag = None
        self._subs = []
        self._lock = threading.Lock()
        self._state = ("idle", None)
        self._active = False

    # ---- 订阅
    def subscribe(self, cb, replay: bool = True) -> None:
        """注册事件回调；replay=True 时立即补发最后一次状态（打开设置页
        时能直接看到「正在下载 43%」或「已下载完成」）。"""
        with self._lock:
            self._subs.append(cb)
            last = self._state
        if replay and last[0] != "idle":
            try:
                cb(*last)
            except Exception:
                pass

    def _emit(self, kind, payload=None):
        with self._lock:
            self._state = (kind, payload)
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(kind, payload)
            except Exception:
                pass

    # ---- 检查 + 自动下载
    def start(self, timeout: float = 8.0) -> bool:
        """后台跑完整条链；已有任务在跑时直接返回 False。"""
        with self._lock:
            if self._active:
                return False
            self._active = True
        threading.Thread(target=self._run, args=(timeout,), daemon=True,
                         name="snappin-update").start()
        return True

    def _run(self, timeout: float):
        try:
            self._emit("checking")
            try:
                info = fetch_latest(timeout)
            except Exception as exc:
                self._emit("error", f"网络错误：{exc}")
                return
            self.info = info
            self.tag = info["tag"]
            if not is_newer(info["tag"], version.APP_VERSION):
                self._emit("latest", info)
                return
            asset = pick_asset(info, self.root)
            if not asset:
                self._emit("error", f"{info['tag']} 没有可用的 Windows 安装包")
                return
            info = dict(info, asset=asset,
                        can_install=bool(getattr(sys, "frozen", False)))
            self.info = info
            self._emit("update", info)
            if not info["can_install"]:
                return       # 源码运行无法替换文件：只提示，不白下几百 MB
            self._download(info)
        finally:
            with self._lock:
                self._active = False

    def _download(self, info: dict):
        asset = info["asset"]
        name = asset["name"]
        dest = os.path.join(UPDATE_DIR, name)
        if os.path.exists(dest) and is_valid_package(dest):
            self.package = dest
            self._emit("downloaded", {"path": dest, "tag": info["tag"],
                                      "size": os.path.getsize(dest)})
            return
        try:
            size = _download(asset["browser_download_url"], dest,
                             lambda g, t: self._emit("downloading", (g, t)))
        except Exception as exc:
            self._emit("error", f"下载失败：{exc}")
            return
        if not is_valid_package(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
            self._emit("error", "安装包校验失败（下载不完整），请重试")
            return
        self.package = dest
        self._emit("downloaded", {"path": dest, "tag": info["tag"], "size": size})

    # ---- 安装
    def install(self) -> str:
        if not getattr(sys, "frozen", False):
            raise RuntimeError(
                "当前是源码运行，无法自动替换文件。\n"
                "自动更新只对打包后的 SnapPin.exe 生效；源码环境请手动拉取代码或"
                "下载发布包。")
        if not self.package or not os.path.exists(self.package):
            raise RuntimeError("还没有下载完成的安装包")
        script = launch_update(self.package, self.root)
        self._emit("installing", script)
        return script


if __name__ == "__main__":   # 离屏自测：python updater.py
    assert parse_ver("v0.4.1") == (0, 4, 1)
    assert parse_ver("0.4") == (0, 4)
    assert is_newer("v0.5.0", "0.4.0")
    assert not is_newer("v0.4.0", "0.4.0")
    assert not is_newer("v0.3.9", "0.4.0")
    flow = UpdateFlow()
    seen = []
    flow.subscribe(lambda k, p: seen.append(k), replay=False)
    flow.start(timeout=6)
    t0 = time.time()
    while time.time() - t0 < 12 and flow._active:
        time.sleep(0.2)
    print("当前版本:", version.APP_VERSION, "| 事件:", seen,
          "| tag:", flow.tag, "| 包:", flow.package)
    print("updater self-test ok")
