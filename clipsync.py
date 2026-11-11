"""局域网剪贴板同步 — Ditto「朋友」式：好友列表 + 网络密码 + TCP 直传。

发送：本机新增剪贴板条目 → 逐个好友建立 TCP 连接推送一帧（daemon 线程，
失败静默，不阻塞 UI）。接收：QTcpServer 常驻监听，收满一帧后校验
「发送方 IP 必须在好友列表内」与「网络密码哈希一致」→ 入库并回放剪贴板
（抑制监听防回环）。

帧格式：8 字节魔数 SNPSYNC\\x01 + 4 字节头长(小端) + JSON 头 + 格式字节流。
JSON 头：{pwd, app, text, html, files, fmts:{fmt:len}}。
好友条目：`IP/主机名[:端口]`，逗号分隔；省略端口 = 用本机监听端口。
仅接收来自好友列表内机器的条目；文件类条目只同步路径引用，不传文件内容。
"""
import hashlib
import ipaddress
import json
import os
import socket
import struct
import threading
import time
import traceback

from PySide6.QtCore import QByteArray, QObject, QMimeData, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import QApplication

MAGIC = b"SNPSYNC\x01"
_HDR = 12                      # 8 魔数 + 4 头长
_MAX_HDR = 64 * 1024           # JSON 头上限
_MAX_TOTAL = 220 * 1024 * 1024  # 整帧上限（与拼接缓冲同级量级）
_TTL_CONN = 15_000             # 连接 15s 没收完一帧即断（挂起连接清理）


def pwd_hash(pwd: str) -> str:
    """网络密码哈希：传输与校验都只出现哈希，明文不进网络。"""
    return hashlib.sha256(("snappin-lan:" + (pwd or "")).encode("utf-8")).hexdigest()


class ClipSync(QObject):
    """剪贴板局域网同步：监听 + 发送（仅主线程 + 短命发送线程，无共享状态）。"""
    received = Signal(int)     # 收到并入库的 clip_id（接收方向）
    PORT_DEFAULT = 43110       # 避开 WinNAT/Hyper-V 常见动态保留段(49152+)

    def __init__(self, store, cfg: dict, suppress=None):
        super().__init__()
        self.store = store
        self.cfg = cfg
        self._suppress = suppress   # callable(n)：回放剪贴板前抑制本地监听防回环
        self._server = None
        self._bufs = {}             # socket -> bytearray（未收满的帧）
        self._friends_cache = (0.0, set())   # (时间, 已解析 IP 集) 5 分钟缓存
        self.last_error = ""        # 最近一次监听失败原因（空 = 正常），供托盘提示
        self.reload()

    # ------------------------------------------------ 配置
    def _conf(self):
        cc = self.cfg.get("clip", {}) if isinstance(self.cfg, dict) else {}
        return (bool(cc.get("sync_enabled", False)),
                int(cc.get("sync_port") or self.PORT_DEFAULT),
                str(cc.get("sync_friends") or ""),
                str(cc.get("sync_password") or ""))

    def reload(self):
        """设置保存后调用：按开关/端口重建监听，失效好友解析缓存。"""
        self._friends_cache = (0.0, set())
        enabled, port, _f, _p = self._conf()
        if self._server is not None and \
                (not enabled or self._server.serverPort() != port):
            self._server.close()
            self._server.deleteLater()
            self._server = None
        self.last_error = ""
        if enabled and self._server is None:
            srv = QTcpServer(self)
            if srv.listen(QHostAddress.AnyIPv4, port):
                srv.newConnection.connect(self._on_conn)
                self._server = srv
            else:                      # 端口被占/被系统保留：只发不收，托盘会提示
                self.last_error = f"端口 {port} 监听失败（{srv.errorString()}），" \
                                  f"局域网接收已停用；可在 设置→剪贴板 换个端口"
                print(f"clipsync: {self.last_error}", flush=True)
                srv.deleteLater()

    def stop(self):
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None

    # ------------------------------------------------ 好友列表解析
    def _friend_tokens(self):
        """→ ([(host, port_or_None)], 本机端口)；host 为 IP/主机名，逗号分隔，
        支持 host:port（省略端口 = 用本机监听端口）。"""
        _e, port, raw, _p = self._conf()
        out = []
        for tok in raw.replace("，", ",").replace(";", ",").replace("；", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            host, _, p = tok.rpartition(":")   # [IPv6]:端口 / IPv4:端口 / 纯主机名
            if not host or (p and not p.isdigit()):
                out.append((tok.strip("[]"), None))
            else:
                out.append((host.strip("[]"), int(p) if p else None))
        return out, port

    def _friend_ips(self):
        """好友主机名 → IPv4 集合（解析失败的条目本次跳过）；IP 条目原样收。"""
        now = time.time()
        if now - self._friends_cache[0] < 300:
            return self._friends_cache[1]
        toks, _p = self._friend_tokens()
        ips = set()
        for host, _port in toks:
            host = host.strip("[]")
            try:
                ipaddress.ip_address(host)
                ips.add(host)
                continue
            except ValueError:
                pass
            try:
                for inf in socket.getaddrinfo(host, None, socket.AF_INET):
                    ips.add(inf[4][0])
            except OSError:
                pass                       # 暂时解析不出：跳过，缓存过期后重试
        self._friends_cache = (now, ips)
        return ips

    # ------------------------------------------------ 发送
    def send_clip(self, clip_id: int):
        """把已入库条目推给全部好友（每好友一连接，daemon 线程，失败静默）。"""
        enabled, port, _raw, pwd = self._conf()
        toks, _p = self._friend_tokens()
        if not enabled or not toks:
            return
        try:
            payload = self.store.get_payload(clip_id)
        except Exception:
            traceback.print_exc()
            return
        frame = self._build_frame(payload, pwd)
        if frame is None:
            return
        for host, fport in toks:
            threading.Thread(target=self._push, args=(host, fport or port, frame),
                             daemon=True, name="snappin-clipsync").start()

    def _build_frame(self, payload: dict, pwd: str):
        formats = {k: bytes(v) for k, v in (payload.get("formats") or {}).items() if v}
        if not formats and not payload.get("text") and not payload.get("files"):
            return None
        total = sum(len(v) for v in formats.values())
        if total > _MAX_TOTAL - 4096:
            return None                   # 超大条目不同步（>200MB 的剪贴内容罕见）
        header = {"pwd": pwd_hash(pwd), "app": payload.get("source_app") or "",
                  "text": payload.get("text") or "", "html": payload.get("html") or "",
                  "files": list(payload.get("files") or []),
                  "fmts": {k: len(v) for k, v in formats.items()}}
        hb = json.dumps(header, ensure_ascii=False).encode("utf-8")
        return MAGIC + struct.pack("<I", len(hb)) + hb + b"".join(formats.values())

    @staticmethod
    def _push(host: str, port: int, frame: bytes):
        try:
            with socket.create_connection((host, port), timeout=3) as s:
                s.sendall(frame)
        except OSError:
            pass                          # 好友不在线/超时：静默，下次复制再试

    # ------------------------------------------------ 接收
    def _on_conn(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                continue
            sock.setReadBufferSize(_MAX_TOTAL + _HDR + 65536)
            self._bufs[sock] = bytearray()
            sock.readyRead.connect(self._on_read)
            sock.disconnected.connect(lambda s=sock: self._drop(s))
            sock.errorOccurred.connect(lambda _e, s=sock: self._drop(s))
            QTimer.singleShot(_TTL_CONN, lambda s=sock: self._timeout(s))

    def _timeout(self, sock):
        if sock in self._bufs:
            self._drop(sock)

    def _drop(self, sock):
        if sock in self._bufs:
            del self._bufs[sock]
        sock.abort()
        sock.deleteLater()

    def _on_read(self):
        sock = self.sender()
        buf = self._bufs.get(sock)
        if buf is None:
            return
        ba = sock.readAll()
        if not ba:
            return
        buf.extend(bytes(ba))
        # 魔数不对立即断（端口被其他程序撞上的快速识别）
        if len(buf) >= len(MAGIC) and bytes(buf[:len(MAGIC)]) != MAGIC:
            self._drop(sock)
            return
        if len(buf) < _HDR:
            return
        try:
            hlen = struct.unpack("<I", bytes(buf[8:_HDR]))[0]
        except struct.error:
            self._drop(sock)
            return
        if hlen > _MAX_HDR:
            self._drop(sock)
            return
        if len(buf) < _HDR + hlen:
            return
        try:
            header = json.loads(bytes(buf[_HDR:_HDR + hlen]).decode("utf-8"))
        except Exception:
            self._drop(sock)
            return
        total = _HDR + hlen + sum(int(n) for n in (header.get("fmts") or {}).values())
        if total > _MAX_TOTAL:
            self._drop(sock)
            return
        if len(buf) < total:
            return
        payload_bytes = bytes(buf[_HDR + hlen:total])
        self._bufs[sock] = bytearray()    # 单连接单帧：复位防重复摄取
        ok = self._ingest(sock, header, payload_bytes)
        sock.disconnectFromHost()
        if not ok:
            self._drop(sock)

    @staticmethod
    def _norm_ip(ip: str) -> str:
        """::ffff:192.168.1.2 → 192.168.1.2（IPv4-mapped IPv6 归一为 IPv4 比较）。"""
        if ip and ip.startswith("::ffff:") and "." in ip:
            return ip[7:]
        return ip

    def _ingest(self, sock, header: dict, payload_bytes: bytes) -> bool:
        """校验密码与好友来源 → 入库 → 回放剪贴板（抑制本地监听）。"""
        _e, _p, _raw, pwd = self._conf()
        if header.get("pwd") != pwd_hash(pwd):
            return False                  # 密码不一致：静默丢弃
        peer = self._norm_ip(sock.peerAddress().toString())
        if peer not in self._friend_ips():
            return False                  # 非好友来源：静默丢弃（Ditto 语义的收紧版）
        off = 0
        formats = {}
        for fmt, n in (header.get("fmts") or {}).items():
            n = int(n)
            if n < 0 or off + n > len(payload_bytes):
                return False
            formats[str(fmt)] = payload_bytes[off:off + n]
            off += n
        payload = {"formats": formats, "text": str(header.get("text") or ""),
                   "html": str(header.get("html") or ""),
                   "files": list(header.get("files") or []),
                   "source_app": str(header.get("app") or "")}
        if not formats and not payload["text"] and not payload["files"]:
            return False
        try:
            cid = self.store.add(payload)
        except Exception:
            traceback.print_exc()
            return False
        self._replay_clipboard(payload)
        if cid:
            self.received.emit(cid)
        return True

    def _replay_clipboard(self, payload: dict):
        """收到的条目回放到系统剪贴板；先抑制本地监听防回环，条目已手动入库。"""
        md = QMimeData()
        for fmt, data in payload["formats"].items():
            if data:
                md.setData(fmt, QByteArray(data))
        png = payload["formats"].get("image/png")
        if png:
            img = QImage.fromData(QByteArray(png))
            if not img.isNull():
                md.setImageData(img)
        if self._suppress is not None:
            self._suppress(2)
        QApplication.clipboard().setMimeData(md)


if __name__ == "__main__":
    # 离屏自测：同机双实例（127.0.0.1 互为好友）走真实 TCP，验证收发/密码/好友校验
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import os as _os
    import sys
    import tempfile


    from clip import ClipStore

    app = QApplication(sys.argv)

    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    dirA = tempfile.mkdtemp(prefix="snappin_sync_a_")
    dirB = tempfile.mkdtemp(prefix="snappin_sync_b_")
    storeA = ClipStore(_os.path.join(dirA, "a.db"))
    storeB = ClipStore(_os.path.join(dirB, "b.db"))
    portA, portB = _free_port(), _free_port()
    while portB == portA:
        portB = _free_port()

    def cfg(enabled=True, port=0, friends="", pwd="pw"):
        return {"clip": {"sync_enabled": enabled, "sync_port": port,
                         "sync_friends": friends, "sync_password": pwd}}

    gotA, recvB, supB = [], [], []
    syncA = ClipSync(storeA, cfg(port=portA, friends=f"127.0.0.1:{portB}"),
                     suppress=lambda n=0: None)
    syncB = ClipSync(storeB, cfg(port=portB, friends=f"127.0.0.1:{portA}"),
                     suppress=lambda n=0: supB.append("sup"))
    syncA.received.connect(lambda cid: gotA.append(cid))
    syncB.received.connect(lambda cid: recvB.append(cid))
    assert syncA._server is not None and syncB._server is not None, "启用后应有监听"

    def pump(sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            app.processEvents()
            time.sleep(0.02)

    # ---- 文本：A → B
    cid = storeA.add({"formats": {"text/plain": "局域网你好 A→B".encode("utf-8")},
                      "text": "局域网你好 A→B", "hash": "ha1"})
    syncA.send_clip(cid)
    pump(2.0)
    hits = storeB.query("局域网你好")
    assert hits and hits[0].text == "局域网你好 A→B", "B 库应收到 A 的文本条目"
    assert len(recvB) == 1, "B 应发出 received 信号"
    assert QApplication.clipboard().text() == "局域网你好 A→B", "收到后应回放剪贴板"
    assert supB == ["sup"], "回放前应抑制本地监听"

    # ---- 图片：B → A
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage as _QI
    pm = _QI(64, 48, _QI.Format_RGB32)
    pm.fill(0xFF2E7CF6)
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    assert pm.save(buf, "PNG")
    png = bytes(buf.data())
    cid2 = storeB.add({"formats": {"image/png": png}, "text": "", "hash": "hb1"})
    syncB.send_clip(cid2)
    pump(2.0)
    hits = [i for i in storeA.query() if i.has_image]
    assert hits, "A 库应收到 B 的图片条目"
    assert QApplication.clipboard().image().isNull() is False, "图片应回放剪贴板"

    # ---- 密码不一致：拒收
    n_before = len(storeB.query())
    storeA.add({"formats": {"text/plain": b"bad-pwd entry"}, "text": "bad-pwd entry",
                "hash": "ha2"})
    sync_bad = ClipSync(storeA, cfg(port=portA + 50, friends=f"127.0.0.1:{portB}",
                                    pwd="wrong"), suppress=lambda n=0: None)
    sync_bad.send_clip(storeA.query("bad-pwd")[0].id)
    pump(1.5)
    assert len(storeB.query()) == n_before, "密码不一致的发送应被 B 拒收"

    # ---- 非好友来源：拒收（B' 好友名单只有 10.9.9.9，来源 127.0.0.1 应被拒）
    storeB2 = ClipStore(_os.path.join(dirB, "b2.db"))
    syncB2 = ClipSync(storeB2, cfg(port=portB + 51, friends="10.9.9.9", pwd="pw"),
                      suppress=lambda n=0: None)
    sync_str = ClipSync(storeA, cfg(port=portA + 52, friends=f"127.0.0.1:{portB + 51}",
                                    pwd="pw"), suppress=lambda n=0: None)
    cid3 = storeA.add({"formats": {"text/plain": b"stranger entry"}, "text": "stranger",
                       "hash": "ha3"})
    sync_str.send_clip(cid3)          # 密码正确但不在好友名单 → B' 拒收
    pump(1.5)
    assert not storeB2.query("stranger"), "非好友来源应被拒收"

    # ---- 未启用：send 无动作、无监听
    sync_off = ClipSync(storeA, cfg(enabled=False, port=portA + 60,
                                    friends=f"127.0.0.1:{portB}"))
    assert sync_off._server is None, "未启用不应有监听"
    sync_off.send_clip(cid)
    pump(0.6)
    assert len(storeB.query()) == n_before, "未启用不应发出任何内容"

    # ---- reload 换端口：旧监听关、新监听起
    syncB.reload()
    syncB.cfg["clip"]["sync_port"] = portB + 70
    syncB.reload()
    assert syncB._server is not None and syncB._server.serverPort() == portB + 70, \
        "reload 后应换到新端口"

    syncA.stop(); syncB.stop(); sync_bad.stop(); syncB2.stop(); sync_off.stop()
    QApplication.clipboard().clear()  # 规避 offscreen 平台 QClipboard 析构段错误
    print("clipsync 自测通过: 文本收发/图片收发/回放抑制/密码拒收/非好友拒收/"
          "未启用/端口重载 全部 OK")
    del syncA, syncB, sync_bad, syncB2, sync_off
    del app
