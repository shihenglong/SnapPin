"""剪贴板历史 — Ditto 风格：SQLite 存储 + 系统剪贴板监听（仅主线程使用，无需加锁）。"""
import hashlib
import json
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass, field

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QBuffer, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, hash TEXT, pinned INTEGER DEFAULT 0, text TEXT, html TEXT, files TEXT, source_app TEXT, size INTEGER, thumb_png BLOB);
CREATE TABLE IF NOT EXISTS clip_data(clip_id INTEGER, fmt TEXT, data BLOB, PRIMARY KEY(clip_id, fmt));
"""

# 图片类格式（按缩略图生成优先级排序）
_IMG_FMTS = ("image/png", "application/x-qt-image", "image/jpeg", "image/bmp", "image/gif")

# Windows API 签名（与 hotkeys.py 同款 ctypes 做法）
try:
    _user32, _kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
except Exception:  # 非 Windows 环境兜底
    _user32 = _kernel32 = None


def _is_img_fmt(fmt: str) -> bool:
    return fmt.startswith("image/") or fmt == "application/x-qt-image"


def _payload_hash(text: str, files: list, formats: dict) -> str:
    """sha256(文本 | 文件列表 | 最大图片格式字节)。"""
    if text:
        src = text.encode("utf-8")
    elif files:
        src = "\n".join(files).encode("utf-8")
    else:
        imgs = [d for k, d in formats.items() if _is_img_fmt(k)]
        src = max(imgs, key=len) if imgs else b""
    return hashlib.sha256(src).hexdigest()


def _make_thumb(formats: dict) -> bytes | None:
    """用首个可解码的图片格式生成最长边 192px 的 PNG 缩略图字节；失败返回 None。
    （192 覆盖行内 36px 图标与悬停预览 160px，放大后仍清晰）"""
    for fmt in _IMG_FMTS:
        data = formats.get(fmt)
        if not data:
            continue
        img = QImage()
        if not img.loadFromData(data) or img.isNull():
            continue
        if img.width() > 192 or img.height() > 192:
            img = img.scaled(192, 192, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QBuffer.WriteOnly)
        if img.save(buf, "PNG"):
            return bytes(buf.data())
    return None


def _uri_list_to_files(data: bytes) -> list:
    """text/uri-list → 本地文件路径列表（QUrl 解析；非 file: 链接保留原串）。"""
    out = []
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        u = QUrl(line)
        out.append(u.toLocalFile() if u.isLocalFile() else u.toString())
    return out


def _foreground_app() -> str:
    """前台窗口所属进程 exe 名（GetForegroundWindow→OpenProcess→QueryFullProcessImageNameW），失败给空串。"""
    if _user32 is None:
        return ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        h = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = wintypes.DWORD(512)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return os.path.basename(buf.value)
            return ""
        finally:
            _kernel32.CloseHandle(h)
    except Exception:
        return ""


@dataclass
class ClipItem:
    """一条剪贴板历史记录。"""
    id: int
    ts: float
    pinned: bool
    text: str = ""
    html: str = ""
    files: list = field(default_factory=list)
    source_app: str = ""
    size: int = 0
    has_image: bool = False
    thumb_png: bytes | None = None
    fmt_names: list = field(default_factory=list)


class ClipStore:
    """剪贴板历史 SQLite 存储（仅主线程使用，无需加锁）。"""
    retention_days = 30   # add() 写入前按这两个属性清理过期，调用方可直接覆盖
    max_entries = 500

    def __init__(self, db_path: str):
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._con.executescript(SQL_SCHEMA)
        self._con.commit()

    # ------------------------------------------------ 写入
    def add(self, payload: dict) -> int:
        """payload: {formats:{fmt:bytes}, text, html, files, source_app, hash} → clip_id。"""
        formats = {k: bytes(v) for k, v in (payload.get("formats") or {}).items() if v}
        text = payload.get("text") or ""
        html = payload.get("html") or ""
        files = list(payload.get("files") or [])
        h = payload.get("hash") or _payload_hash(text, files, formats)
        self.cleanup(self.retention_days, self.max_entries)  # 写入前先清理过期
        row = self._con.execute(
            "SELECT id, hash FROM clips ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
        if row and row[1] == h:  # dedupe：与最近一条相同 → 只更新 ts
            self._con.execute("UPDATE clips SET ts=? WHERE id=?", (time.time(), row[0]))
            self._con.commit()
            return row[0]
        cur = self._con.execute(
            "INSERT INTO clips(ts, hash, pinned, text, html, files, source_app, size, thumb_png) "
            "VALUES(?,?,0,?,?,?,?,?,?)",
            (time.time(), h, text, html,
             json.dumps(files, ensure_ascii=False) if files else None,
             payload.get("source_app") or "",
             sum(len(d) for d in formats.values()), _make_thumb(formats)))
        cid = cur.lastrowid
        for fmt, data in formats.items():
            self._con.execute("INSERT OR REPLACE INTO clip_data(clip_id, fmt, data) VALUES(?,?,?)",
                              (cid, fmt, data))
        self._con.commit()
        return cid

    def cleanup(self, days: int, max_entries: int) -> int:
        """清理非常用(pinned=0)记录：超过 days 天 / 超出 max_entries 条；<=0 表示该项不限。返回删除数。"""
        ids = set()
        if days and days > 0:
            cutoff = time.time() - days * 86400
            for r in self._con.execute("SELECT id FROM clips WHERE pinned=0 AND ts<?", (cutoff,)):
                ids.add(r[0])
        if max_entries and max_entries > 0:
            q = ("SELECT id FROM clips WHERE pinned=0 AND id NOT IN "
                 "(SELECT id FROM clips ORDER BY ts DESC LIMIT ?)")
            for r in self._con.execute(q, (max_entries,)):
                ids.add(r[0])
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        self._con.execute(f"DELETE FROM clips WHERE id IN ({ph})", list(ids))
        self._con.execute(f"DELETE FROM clip_data WHERE clip_id IN ({ph})", list(ids))
        self._con.commit()
        return len(ids)

    # ------------------------------------------------ 读取
    def query(self, search: str = "", limit: int = 200) -> list[ClipItem]:
        """常用置顶、其内按 ts 降序；search 对 text/html/files 大小写不敏感子串过滤。"""
        rows = self._con.execute(
            "SELECT c.id, c.ts, c.pinned, c.text, c.html, c.files, c.source_app, c.size, "
            "c.thumb_png, GROUP_CONCAT(d.fmt) FROM clips c "
            "LEFT JOIN clip_data d ON d.clip_id = c.id "
            "GROUP BY c.id ORDER BY c.pinned DESC, c.ts DESC").fetchall()
        s = (search or "").lower()
        out = []
        for cid, ts, pinned, text, html, files, app, size, thumb, fmts in rows:
            text, html = text or "", html or ""
            fl = json.loads(files) if files else []
            names = fmts.split(",") if fmts else []
            if s and s not in text.lower() and s not in html.lower() \
                    and s not in " ".join(fl).lower():
                continue
            out.append(ClipItem(
                cid, ts, bool(pinned), text, html, fl, app or "", size or 0,
                thumb is not None or any(_is_img_fmt(f) for f in names), thumb, names))
            if len(out) >= limit:
                break
        return out

    def get_payload(self, clip_id: int) -> dict:
        """→ {formats:{fmt:bytes}, text, html, files}，用于回放剪贴板。"""
        formats = {fmt: bytes(data) for fmt, data in self._con.execute(
            "SELECT fmt, data FROM clip_data WHERE clip_id=?", (clip_id,)) if data is not None}
        row = self._con.execute("SELECT text, html, files FROM clips WHERE id=?", (clip_id,)).fetchone()
        text, html, files = row if row else ("", "", None)
        return {"formats": formats, "text": text or "", "html": html or "",
                "files": json.loads(files) if files else []}

    # ------------------------------------------------ 管理
    def set_pinned(self, clip_id: int, pinned: bool) -> None:
        self._con.execute("UPDATE clips SET pinned=? WHERE id=?", (1 if pinned else 0, clip_id))
        self._con.commit()

    def delete(self, clip_id: int) -> None:
        self._con.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        self._con.execute("DELETE FROM clip_data WHERE clip_id=?", (clip_id,))
        self._con.commit()

    def clear(self, keep_pinned: bool = True) -> int:
        """清空历史（keep_pinned=True 时保留常用项），返回删除条数。"""
        where = " WHERE pinned=0" if keep_pinned else ""
        ids = [r[0] for r in self._con.execute(f"SELECT id FROM clips{where}")]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        self._con.execute(f"DELETE FROM clips WHERE id IN ({ph})", ids)
        self._con.execute(f"DELETE FROM clip_data WHERE clip_id IN ({ph})", ids)
        self._con.commit()
        return len(ids)


class ClipMonitor(QObject):
    """监听系统剪贴板 dataChanged → 50ms 合并后落库（仅主线程）。"""
    changed = Signal(int)  # 新条目 clip_id

    def __init__(self, store: ClipStore, enabled: bool = True):
        super().__init__()
        self.store = store
        self._enabled = enabled
        self._suppress = 0       # SnapPin 自己复制/回放时忽略的次数
        self._suppress_at = 0.0  # 抑制开始时间：超过 1s 的残留抑制自动作废
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._flush)
        QApplication.clipboard().dataChanged.connect(self._on_changed)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._timer.stop()

    def suppress_next(self, n: int = 1) -> None:
        """接下来 n 次 dataChanged 直接忽略（自己复制/回放时不记录）。

        残留抑制只在本应用自己复制后的 1 秒窗口内有效——超时作废，
        避免误吞随后来自截图贴图/外部程序的真实复制（历史丢条目的根因）。
        """
        self._suppress += n
        self._suppress_at = time.time()

    def _on_changed(self):
        if not self._enabled:
            return
        if self._suppress > 0:
            if time.time() - self._suppress_at < 1.0:
                self._suppress -= 1
                return
            self._suppress = 0  # 残留抑制已过期：这条是真实复制，正常记录
        self._timer.start()  # 单发重置 → 连续触发合并为一次落库

    def _flush(self):
        md = QApplication.clipboard().mimeData()
        if md is None:
            return
        formats = {}
        for fmt in md.formats():
            ba = md.data(fmt)
            if ba is not None and ba.size() > 0:
                formats[fmt] = bytes(ba)
        # 平台对 application/x-qt-image 可能给不出字节（延迟渲染/离屏等）：
        # 有图像却收集不到数据时，直接从剪贴板取 QImage 转 PNG，否则截图会被整条丢弃
        if md.hasImage() and not any(_is_img_fmt(k) for k in formats):
            img = QApplication.clipboard().image()
            if not img.isNull():
                buf = QBuffer()
                buf.open(QBuffer.WriteOnly)
                if img.save(buf, "PNG"):
                    formats["image/png"] = bytes(buf.data())
        text = md.text() if md.hasText() else (
            bytes(md.data("text/plain")).decode("utf-8", "replace") if md.hasFormat("text/plain") else "")
        html = md.html() if md.hasHtml() else ""
        files = _uri_list_to_files(bytes(md.data("text/uri-list")) if md.hasFormat("text/uri-list") else b"")
        if not formats and not text and not files:
            return  # 空数据跳过
        payload = {"formats": formats, "text": text, "html": html, "files": files,
                   "source_app": _foreground_app(),
                   "hash": _payload_hash(text, files, formats)}
        try:
            cid = self.store.add(payload)
        except Exception:
            traceback.print_exc()
            return
        if cid:
            self.changed.emit(cid)


if __name__ == "__main__":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    import tempfile

    app = QApplication([])  # 图片编解码插件需要
    db = os.path.join(tempfile.gettempdir(), "snappin_clip_selftest.db")
    if os.path.exists(db):
        os.remove(db)
    st = ClipStore(db)

    png = QImage(8, 6, QImage.Format_RGB32)
    png.fill(0xFF2E7CF6)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    assert png.save(buf, "PNG")
    png_bytes = bytes(buf.data())

    h_txt = hashlib.sha256("hello 世界".encode("utf-8")).hexdigest()
    a = st.add({"formats": {"text/plain": "hello 世界".encode("utf-8")}, "text": "hello 世界", "hash": h_txt})
    d = st.add({"formats": {"text/plain": "hello 世界".encode("utf-8")}, "text": "hello 世界", "hash": h_txt})
    assert a == d, "dedupe: 与最近一条相同应只更新 ts"
    b = st.add({"formats": {"image/png": png_bytes}, "text": "", "hash": hashlib.sha256(png_bytes).hexdigest()})
    c = st.add({"formats": {"text/html": b"<b>x</b>"}, "text": "", "html": "<b>x</b>",
                "files": ["C:/data/a.txt", "C:/data/b.jpg"], "hash": "files-hash"})

    items = st.query()
    assert [i.id for i in items] == [c, b, a], f"应按 ts 降序, 实际 {[i.id for i in items]}"
    assert items[0].files == ["C:/data/a.txt", "C:/data/b.jpg"]
    assert items[2].text == "hello 世界"
    img_item = [i for i in items if i.id == b][0]
    assert img_item.has_image and img_item.thumb_png and img_item.thumb_png.startswith(b"\x89PNG"), \
        "图片项应有 PNG 缩略图"

    st.set_pinned(a, True)
    assert st.query()[0].id == a, "pinned 应置顶"
    hits = st.query("b.jpg")
    assert [i.id for i in hits] == [c], "search 应命中 files"
    p = st.get_payload(b)
    assert p["formats"]["image/png"] == png_bytes and p["text"] == ""

    assert st.cleanup(0, 0) == 0, "days/max_entries 为 0 表示不清理"
    st.delete(b)
    assert all(i.id != b for i in st.query())
    st.set_pinned(a, False)
    n = st.cleanup(0, 1)  # 只保留最近 1 条
    left = st.query()
    assert len(left) == 1 and left[0].id == c, f"cleanup 后应只剩 1 条, 实际 {[i.id for i in left]}"
    # 缩略图上限 192px（悬停预览 160px 放大后仍清晰）
    _timg = QImage()
    assert _timg.loadFromData(img_item.thumb_png)
    assert _timg.width() <= 192 and _timg.height() <= 192, "缩略图应 ≤192px"

    # 抑制泄漏回归：残留抑制 1s 后过期，不吞外部复制（截图双击复制丢历史根因）
    mon = ClipMonitor(st, enabled=True)
    mon.suppress_next(2)
    QApplication.clipboard().setText("sup-test-1")
    t0 = time.time()
    while time.time() - t0 < 0.25:
        app.processEvents()
        time.sleep(0.01)
    assert not st.query("sup-test-1"), "抑制窗口内的自写复制不应入库"
    time.sleep(1.05)  # 超过 1s 抑制窗口
    QApplication.clipboard().setText("sup-test-2")
    t0 = time.time()
    while time.time() - t0 < 0.25:
        app.processEvents()
        time.sleep(0.01)
    hits = st.query("sup-test-2")
    assert hits and hits[0].text == "sup-test-2", "过期后的外部复制必须入库"

    # x-qt-image 无字节兜底：setPixmap 的截图必须入库并带缩略图
    from PySide6.QtGui import QPixmap as _QP, QColor as _QC
    _pm = _QP(300, 200)
    _pm.fill(_QC("#336699"))
    QApplication.clipboard().setPixmap(_pm)
    t0 = time.time()
    while time.time() - t0 < 0.3:
        app.processEvents()
        time.sleep(0.01)
    hits = [i for i in st.query() if i.has_image]
    assert hits and hits[0].thumb_png, "setPixmap 截图应兜底入库并带缩略图"
    mon.set_enabled(False)

    print(f"clip 自测通过: dedupe({a}=={d}) · thumb {len(img_item.thumb_png)}B · "
          f"search/pin/payload/delete/cleanup({n}) 全部 OK")
