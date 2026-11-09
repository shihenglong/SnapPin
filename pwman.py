"""本地密码管理 — SQLite 存储 + Windows DPAPI 加密。

仅提供 名称/账号/密码 三字段的手动增删改查；不做导入/同步/主密码功能。
密码经 DPAPI（CryptProtectData）加密后写入 enc 字段，只有当前 Windows
用户在本机能解密，数据库文件本身不含明文密码。
"""
import ctypes
import sqlite3
import sys
import time

_CRYPTPROTECT_UI_FORBIDDEN = 0x1  # DPAPI 全程不弹任何 UI

_C32 = None  # 缓存 crypt32 句柄


class _DATA_BLOB(ctypes.Structure):
    """DPAPI 的 DATA_BLOB 结构（长度 + 指针）。"""
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes):
    """构造 DATA_BLOB；返回 (blob, buf)，buf 须在调用期间保持引用防 GC。"""
    buf = ctypes.create_string_buffer(bytes(data) + b"\x00")  # 至少 1 字节，兼容空串
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _crypt32():
    """懒加载 crypt32 并设定返回值类型（仅 Windows 可用）。"""
    global _C32
    if _C32 is None:
        _C32 = ctypes.windll.crypt32
        _C32.CryptProtectData.restype = ctypes.c_int  # BOOL
        _C32.CryptUnprotectData.restype = ctypes.c_int
    return _C32


def dpapi_encrypt(data: bytes) -> bytes:
    """用 DPAPI CryptProtectData 加密字节串（CRYPTPROTECT_UI_FORBIDDEN），返回 blob。"""
    blob_in, _buf = _make_blob(data)
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok or not blob_out.pbData:
        raise OSError("CryptProtectData 加密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # 系统分配的内存须释放


def dpapi_decrypt(blob: bytes) -> bytes:
    """用 DPAPI CryptUnprotectData 解密 blob；失败抛 OSError，由调用方处理。"""
    blob_in, _buf = _make_blob(blob)
    descr = ctypes.c_wchar_p()  # 数据描述（不使用，但需释放）
    blob_out = _DATA_BLOB()
    ok = _crypt32().CryptUnprotectData(
        ctypes.byref(blob_in), ctypes.byref(descr), None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok or not blob_out.pbData:
        raise OSError("CryptUnprotectData 解密失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        if descr.value:
            ctypes.windll.kernel32.LocalFree(descr)


class PasswordStore:
    """本地密码库：SQLite 单表 + DPAPI 加密。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS creds("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, username TEXT, "
            "enc BLOB, updated REAL)")
        self._conn.commit()

    def add(self, title: str, username: str, password: str) -> int:
        """新增一条凭据，密码 DPAPI 加密后入库，返回新 id。"""
        enc = dpapi_encrypt(password.encode("utf-8"))
        cur = self._conn.execute(
            "INSERT INTO creds(title, username, enc, updated) VALUES(?,?,?,?)",
            (title, username, enc, time.time()))
        self._conn.commit()
        return int(cur.lastrowid)

    def update(self, id: int, title: str, username: str, password: str) -> None:
        """更新指定 id 的三个字段，并刷新 updated 时间戳。"""
        enc = dpapi_encrypt(password.encode("utf-8"))
        self._conn.execute(
            "UPDATE creds SET title=?, username=?, enc=?, updated=? WHERE id=?",
            (title, username, enc, time.time(), id))
        self._conn.commit()

    def delete(self, id: int) -> None:
        """删除指定 id 的记录。"""
        self._conn.execute("DELETE FROM creds WHERE id=?", (id,))
        self._conn.commit()

    def query(self, search: str = "") -> list[dict]:
        """返回全部（或按关键字过滤）的凭据列表，按 updated 降序。

        dict 含 id/title/username/password/updated；password 已解密为 utf-8
        文本，解密失败时给 ""（不抛异常）。search 对三个字段做大小写不敏感
        的子串匹配。密码需解密后才能匹配，故过滤在 Python 侧完成。
        """
        rows = self._conn.execute(
            "SELECT id, title, username, enc, updated FROM creds").fetchall()
        kw = (search or "").lower()
        out = []
        for rid, title, username, enc, updated in rows:
            try:
                pwd = dpapi_decrypt(bytes(enc)).decode("utf-8")
            except Exception:
                pwd = ""  # 换了系统用户 / 库被拷走等情况，宁可不显示明文
            if kw and (kw not in (title or "").lower()
                       and kw not in (username or "").lower()
                       and kw not in pwd.lower()):
                continue
            out.append({"id": rid, "title": title or "", "username": username or "",
                        "password": pwd, "updated": updated})
        out.sort(key=lambda r: (r["updated"], r["id"]), reverse=True)
        return out

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()

    def quick_search(self, search: str, limit: int = 10) -> list[dict]:
        """按 名称/账号 子串匹配（SQL 侧过滤，仅命中行解密密码）。

        给剪贴板快贴窗口的搜索用：不搜密码内容（避免全量解密），
        返回字段与 query() 相同，按 updated 降序。
        """
        kw = (search or "").strip().lower()
        if not kw:
            return []
        rows = self._conn.execute(
            "SELECT id, title, username, enc, updated FROM creds "
            "WHERE lower(title) LIKE ? OR lower(username) LIKE ? "
            "ORDER BY updated DESC LIMIT ?",
            (f"%{kw}%", f"%{kw}%", int(limit))).fetchall()
        out = []
        for rid, title, username, enc, updated in rows:
            try:
                pwd = dpapi_decrypt(bytes(enc)).decode("utf-8")
            except Exception:
                pwd = ""
            out.append({"id": rid, "title": title or "", "username": username or "",
                        "password": pwd, "updated": updated})
        return out


if __name__ == "__main__":
    # 自测：临时库走 add → query → update → delete 往返断言
    if sys.platform != "win32":
        print("非 Windows 平台，跳过 DPAPI 自测")
        sys.exit(0)
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # 让 sqlite 自己建库文件
    store = PasswordStore(path)
    try:
        id1 = store.add("GitHub", "alice@example.com", "s3cret-αβ")
        id2 = store.add("163 邮箱", "bob@163.com", "pw2")
        rows = store.query()
        assert [r["id"] for r in rows] == [id2, id1], rows  # updated 降序
        rows = store.query("github")  # 大小写不敏感
        assert len(rows) == 1 and rows[0]["username"] == "alice@example.com"
        rows = store.query("S3CRET")  # 按密码内容搜索
        assert len(rows) == 1 and rows[0]["id"] == id1
        rows = store.query("不存在")
        assert rows == []
        store.update(id2, "163 邮箱", "bob@163.com", "新密码★")
        by_id = {r["id"]: r for r in store.query()}
        assert by_id[id2]["password"] == "新密码★"  # 非 ASCII 往返
        assert by_id[id1]["password"] == "s3cret-αβ"
        store.delete(id1)
        rows = store.query()
        assert len(rows) == 1 and rows[0]["id"] == id2
        print("pwman 自测通过: add/query/update/delete 往返 OK, 临时库:", path)
    finally:
        store.close()
        try:
            os.remove(path)
        except OSError:
            pass
