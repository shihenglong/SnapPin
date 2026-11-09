"""翻译 — 参考 evan-ocr 的双轨方案：免 Key 在线直连（多通道容灾）+ 离线引擎。

在线直连（默认，开箱即用）：Google GTX 直连 + MyMemory 容灾，逐块并发、
  单块双通道都失败时自动转离线引擎补齐；
Ollama（本地大模型）：自动连接本机 Ollama（http://localhost:11434），
  模型自动检测（优先 Qwen/DeepSeek），不可用自动转离线引擎；
GLM / MiniMax / 自定义 OpenAI 兼容：一次请求批量翻译全部文字块；
离线插件：Bergamot translator-engine.exe（vendor/plugins/translation），
  零网络请求。

信号契约不变：done(list[str], int) 按输入顺序回传全部译文，failed(str, int)。
"""
import json
import os
import re
import tempfile
import urllib.parse

from PySide6.QtCore import QObject, QProcess, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

import config

# OpenAI 兼容 / Ollama 预设：base 不带 /chat/completions 后缀
PRESETS = {
    "online": {},
    "glm": {"base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.7-flash"},
    "minimax": {"base": "https://api.minimaxi.com/v1", "model": "MiniMax-M2"},
    "custom": {"base": "", "model": ""},
    "ollama": {"base": "http://localhost:11434", "model": ""},
    "local": {},
}

_LOCAL_EXE = os.path.join(config.app_root(),
                          "vendor", "plugins", "translation", "translator-engine.exe")
_LOCAL_LANG = {"zh": "zh", "zh-cn": "zh", "zh-tw": "zh", "en": "en",
               "ja": "ja", "ko": "ko"}

_OLLAMA_HOST = "http://localhost:11434"
_OLLAMA_PREFS = ("qwen", "deepseek", "glm")   # 模型自动检测优先级
_TIMEOUT_MS = 20_000
_ONLINE_TIMEOUT_MS = 8_000
_OLLAMA_TIMEOUT_MS = 120_000


def local_available() -> bool:
    return os.path.isfile(_LOCAL_EXE)


class TranslateClient(QObject):
    done = Signal(list, int)      # [译文, ...]（与输入等长）, tag
    failed = Signal(str, int)

    def __init__(self, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self._cfg = cfg if cfg is not None else {}
        self._nam = QNetworkAccessManager(self)
        self._pending = 0                      # 并发中的请求数
        self._online_state = {}                # tag -> {texts, results, pending, target}
        self.last_engine = ""                  # 最近一次成功翻译的实际引擎（完成提示用）

    # ------------------------------------------------ 入口
    def busy(self) -> bool:
        return self._pending > 0

    def engine_ready(self) -> tuple[bool, str]:
        """(可用, 提示) — 设置页状态行用。"""
        tc = self._cfg.get("translate", {})
        engine = tc.get("engine", "online")
        if engine == "online":
            return True, "在线直连 · 免 Key（GTX + MyMemory 多通道容灾）"
        if engine == "ollama":
            base = tc.get("api_base") or _OLLAMA_HOST
            return True, f"Ollama 本地大模型 · {base}（模型自动检测）"
        if engine == "local":
            return local_available(), ("本地离线插件就绪" if local_available()
                                       else "未找到离线插件 translator-engine.exe")
        if not tc.get("api_key"):
            return True, "未配置 API 密钥，自动改用免 Key 在线直连"
        return True, f"{engine.upper()} · {self._model_of(engine)}"

    def _model_of(self, engine: str) -> str:
        tc = self._cfg.get("translate", {})
        return tc.get("model") or PRESETS.get(engine, {}).get("model") or ""

    @staticmethod
    def _dst_name(target: str) -> str:
        return {"zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "英语",
                "ja": "日语", "ko": "韩语"}.get(target, target)

    def translate(self, texts, target: str = "zh-CN", source: str = "auto", tag: int = 0):
        """批量翻译：texts 为 str 或 list[str]；done 按输入顺序回传等长 list。"""
        if isinstance(texts, str):
            texts = [texts]
        texts = [t or "" for t in texts]
        if not texts or not any(t.strip() for t in texts):
            self.done.emit([""] * len(texts), tag)
            return

        tc = self._cfg.get("translate", {})
        engine = tc.get("engine", "online")
        if engine == "online":
            self._online_translate(texts, target, tag)
        elif engine == "ollama":
            self._ollama_translate(texts, target, tag)
        elif engine in ("glm", "minimax", "custom"):
            key = tc.get("api_key", "")
            base = tc.get("api_base") or PRESETS.get(engine, {}).get("base", "")
            model = self._model_of(engine)
            if key and base and model:
                self._api_translate(texts, base, key, model, target, source, tag,
                                    engine_label=engine.upper())
                return
            # 密钥/参数未配置 → 免 Key 在线直连(真翻译质量);词典插件只作最后兜底
            self._online_translate(texts, target, tag)
        else:
            if local_available():
                self._local_translate(texts, target, tag)
            else:
                self.failed.emit("无可用翻译引擎：请在 设置→翻译 配置", tag)

    # ------------------------------------------------ 在线直连（免 Key 多通道）
    def _online_translate(self, texts: list, target: str, tag: int):
        """逐块并发：Google GTX → MyMemory 双通道容灾；全败块转离线插件补齐。"""
        st = {"texts": list(texts), "results": ["" for _ in texts],
              "pending": len(texts), "target": target}
        self._online_state[tag] = st
        for i in range(len(texts)):
            self._online_req(st, tag, i, 0)

    def _online_req(self, st: dict, tag: int, i: int, stage: int):
        text = st["texts"][i]
        if stage == 0:      # 通道一：Google GTX 直连
            qs = urllib.parse.urlencode({"client": "gtx", "sl": "auto",
                                         "tl": st["target"], "dt": "t", "q": text})
            url = QUrl(f"https://translate.googleapis.com/translate_a/single?{qs}")
        else:               # 通道二：MyMemory 容灾
            src = self._guess_src(text)
            qs = urllib.parse.urlencode({"q": text, "langpair": f"{src}|{st['target']}"})
            url = QUrl(f"https://api.mymemory.translated.net/get?{qs}")
        req = QNetworkRequest(url)
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                         QNetworkRequest.NoLessSafeRedirectPolicy)
        req.setTransferTimeout(_ONLINE_TIMEOUT_MS)
        reply = self._nam.get(req)
        self._pending += 1

        def _fin(r=reply, s=stage):
            self._pending -= 1
            err = r.error()
            data = bytes(r.readAll()) if err == QNetworkReply.NoError else b""
            r.deleteLater()
            if err == QNetworkReply.NoError:
                out = self._gtx_parse(data) if s == 0 else self._mymemory_parse(data)
                if out:
                    st["results"][i] = out
            if not st["results"][i] and s == 0:
                self._online_req(st, tag, i, 1)     # GTX 失败 → MyMemory 容灾
                return
            st["pending"] -= 1
            if st["pending"] <= 0:
                self._online_done(st, tag)

        reply.finished.connect(_fin)

    def _online_done(self, st: dict, tag: int):
        self._online_state.pop(tag, None)
        missing = [i for i, r in enumerate(st["results"]) if not r.strip()]
        if missing and local_available():
            # 双通道都拿不到的块 → 离线插件补齐后按序合并
            self._local_translate([st["texts"][i] for i in missing], st["target"], tag,
                                  merge=(st["results"], missing))
        else:
            self.last_engine = "在线直连"
            self.done.emit(st["results"], tag)

    @staticmethod
    def _gtx_parse(data: bytes) -> str:
        """GTX 响应 [[["译","原",...],...], ...] → 拼接全部句段。"""
        try:
            obj = json.loads(data.decode("utf-8"))
            return "".join(s[0] for s in obj[0] if s and s[0]).strip()
        except Exception:
            return ""

    @staticmethod
    def _mymemory_parse(data: bytes) -> str:
        try:
            obj = json.loads(data.decode("utf-8"))
            if int(obj.get("responseStatus", -1)) == 200:
                return (obj.get("responseData") or {}).get("translatedText", "").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _guess_src(text: str) -> str:
        """MyMemory 需要显式源语言：按 CJK 占比粗判 zh-CN / en。"""
        def cjk(ch):
            return '⺀' <= ch <= '鿿' or '　' <= ch <= '〿'
        n = sum(1 for ch in text if cjk(ch))
        return "zh-CN" if n / max(1, len(text)) > 0.3 else "en"

    # ------------------------------------------------ Ollama（本地大模型）
    def _ollama_translate(self, texts: list, target: str, tag: int):
        tc = self._cfg.get("translate", {})
        base = (tc.get("api_base") or PRESETS["ollama"]["base"]).rstrip("/")
        if base.endswith(("/v1", "/v1/")):
            # OpenAI 兼容端点（LM Studio / llama.cpp / 各类本地代理）：
            # 按 Ollama 专有路径拼接必然 404 → 静默降级词典（“翻译效果很差”的根因）
            base = base.rstrip("/")
            if tc.get("model"):
                self._api_translate(texts, base, "", tc["model"], target, "auto", tag,
                                    engine_label="本地模型")
                return
            # 未填模型 → GET /v1/models 自动挑第一个
            req = QNetworkRequest(QUrl(base + "/models"))
            req.setTransferTimeout(_ONLINE_TIMEOUT_MS)
            reply = self._nam.get(req)
            self._pending += 1

            def _models(r=reply):
                self._pending -= 1
                data = bytes(r.readAll()) if r.error() == QNetworkReply.NoError else b""
                r.deleteLater()
                names = []
                if r.error() == QNetworkReply.NoError:
                    try:
                        names = [m.get("id", "") for m in
                                 json.loads(data.decode("utf-8")).get("data", [])]
                    except Exception:
                        names = []
                if names:
                    self._api_translate(texts, base, "", names[0], target, "auto", tag,
                                        engine_label="本地模型")
                else:
                    self._offline_fallback(texts, target, tag, "本地服务未返回可用模型")

            reply.finished.connect(_models)
            return
        if tc.get("model"):
            self._ollama_chat(base, tc["model"], texts, target, tag)
            return
        # 模型自动检测：GET /api/tags，优先 Qwen / DeepSeek
        req = QNetworkRequest(QUrl(base + "/api/tags"))
        req.setTransferTimeout(_ONLINE_TIMEOUT_MS)
        reply = self._nam.get(req)
        self._pending += 1

        def _tags(r=reply):
            self._pending -= 1
            data = bytes(r.readAll()) if r.error() == QNetworkReply.NoError else b""
            r.deleteLater()
            model = ""
            if r.error() == QNetworkReply.NoError:
                try:
                    names = [m.get("name", "") for m in
                             json.loads(data.decode("utf-8")).get("models", [])]
                    for pref in _OLLAMA_PREFS:
                        model = next((n for n in names if pref in n.lower()), "")
                        if model:
                            break
                    model = model or (names[0] if names else "")
                except Exception:
                    model = ""
            if model:
                self._ollama_chat(base, model, texts, target, tag)
            else:
                self._offline_fallback(texts, target, tag, "未检测到本机 Ollama 服务/模型")

        reply.finished.connect(_tags)

    def _ollama_chat(self, base: str, model: str, texts: list, target: str, tag: int):
        dst = self._dst_name(target)
        system = (f"你是精准的翻译引擎。把用户消息中的 JSON 数组里每段文本翻译成{dst}，"
                  f"保持术语准确。只输出一个与输入等长的 JSON 字符串数组（每个元素对应输入同下标文本），"
                  f"不要输出解释、代码块标记或其他任何内容。"
                  f'示例：["Hello world","Save changes"] → ["你好，世界","保存更改"]')
        body = json.dumps({"model": model, "stream": False,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user",
                                         "content": json.dumps(texts, ensure_ascii=False)}],
                           "temperature": 0.1})
        req = QNetworkRequest(QUrl(base + "/api/chat"))
        req.setTransferTimeout(_OLLAMA_TIMEOUT_MS)
        req.setRawHeader(b"Content-Type", b"application/json")
        reply = self._nam.post(req, body.encode("utf-8"))
        self._pending += 1

        def _chat(r=reply, n=len(texts)):
            self._pending -= 1
            data = bytes(r.readAll()) if r.error() == QNetworkReply.NoError else b""
            r.deleteLater()
            out = None
            if r.error() == QNetworkReply.NoError:
                try:
                    content = json.loads(data.decode("utf-8")).get("message", {}).get("content", "")
                    out = self._parse_batch(content, n)
                except Exception:
                    out = None
            if out is None:
                self._offline_fallback(texts, target, tag, "Ollama 推理失败")
                return
            self.last_engine = f"Ollama · {model}"
            self.done.emit(out, tag)

        reply.finished.connect(_chat)

    def _offline_fallback(self, texts: list, target: str, tag: int, reason: str):
        """所选引擎不可用 → 离线插件兜底；连插件都没有才报失败。"""
        if local_available():
            self._local_translate(texts, target, tag)
        else:
            self.failed.emit(f"{reason}，且未找到离线插件", tag)

    # ------------------------------------------------ OpenAI 兼容（GLM / MiniMax / 自定义 / 本地 /v1 端点）
    def _api_translate(self, texts: list, base: str, key: str, model: str,
                       target: str, source: str, tag: int, engine_label: str = ""):
        dst = self._dst_name(target)
        src = "原文语言" if source in ("auto", "", None) else source
        system = (f"你是精准的翻译引擎。把用户消息中的 JSON 数组里每段文本由{src}翻译成{dst}，"
                  f"保持术语准确。只输出一个与输入等长的 JSON 字符串数组（每个元素对应输入同下标文本），"
                  f"不要输出解释、代码块标记或其他任何内容。"
                  f'示例：["Hello world","Save changes"] → ["你好，世界","保存更改"]')
        body = json.dumps({"model": model,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user",
                                         "content": json.dumps(texts, ensure_ascii=False)}],
                           "temperature": 0.1})
        url = QUrl(base.rstrip("/") + "/chat/completions")
        req = QNetworkRequest(url)
        req.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                         QNetworkRequest.NoLessSafeRedirectPolicy)
        req.setTransferTimeout(_TIMEOUT_MS)
        req.setRawHeader(b"Content-Type", b"application/json")
        if key:
            req.setRawHeader(b"Authorization", "Bearer " + key.encode("utf-8"))
        reply = self._nam.post(req, body.encode("utf-8"))
        self._pending += 1

        def _fin(r=reply, t=tag, n=len(texts), tgt=target, m=model,
                 lb=engine_label or model):
            self._pending -= 1
            err = r.error()
            data = bytes(r.readAll()) if err == QNetworkReply.NoError else b""
            r.deleteLater()
            if err != QNetworkReply.NoError:
                self._offline_fallback(texts, tgt, t, f"网络错误 ({err})")
                return
            try:
                obj = json.loads(data.decode("utf-8"))
                content = (obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
            except Exception:
                content = data.decode("utf-8", "replace")
            out = self._parse_batch(content, n)
            if out is None:
                self._offline_fallback(texts, tgt, t, "译文解析失败")
            else:
                self.last_engine = lb
                self.done.emit(out, t)

        reply.finished.connect(_fin)

    @staticmethod
    def _parse_batch(content: str, n: int) -> list | None:
        """从模型回复里抠出等长 JSON 数组；容错代码块围栏/前后杂文。"""
        if not content:
            return None
        s = content.strip()
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()
        try:
            arr = json.loads(s)
        except Exception:
            i, j = s.find("["), s.rfind("]")
            if i < 0 or j <= i:
                return None
            try:
                arr = json.loads(s[i:j + 1])
            except Exception:
                return None
        if not isinstance(arr, list):
            return None
        out = ["" if a is None else str(a) for a in arr]
        if len(out) < n:                      # 模型少数行 → 按原文补齐
            out = out + ["" for _ in range(n - len(out))]
        elif len(out) > n:
            out = out[:n]
        return out

    # ------------------------------------------------ 离线插件（Bergamot，QProcess 文件 IPC）
    def _local_translate(self, texts: list, target: str, tag: int, merge=None):
        """merge=(base_results, indices)：只把结果写回指定下标后整体发出。"""
        to = _LOCAL_LANG.get((target or "zh-CN").lower(), "zh")
        fd_in, path_in = tempfile.mkstemp(suffix=".txt", prefix="snappin_tr_")
        fd_out, path_out = tempfile.mkstemp(suffix=".txt", prefix="snappin_tr_")
        os.close(fd_in)
        os.close(fd_out)
        try:
            with open(path_in, "w", encoding="utf-8") as f:
                f.write("\n".join(t.replace("\n", " ") for t in texts))
        except OSError:
            self.failed.emit("离线插件输入文件写入失败", tag)
            return
        self._pending += 1
        proc = QProcess(self)
        proc.setProgram(_LOCAL_EXE)
        proc.setArguments(["--from", "auto", "--to", to,
                           "--input", path_in, "--output", path_out])

        def _fin(code, status, p=proc, pi=path_in, po=path_out, n=len(texts), t=tag, m=merge):
            self._pending -= 1
            p.deleteLater()
            out = None
            try:
                if os.path.isfile(po) and os.path.getsize(po) > 0:
                    with open(po, "r", encoding="utf-8", errors="replace") as f:
                        out = f.read()
                elif code == 0:                       # 降级：解析 stdout 纯文本
                    out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
            except OSError:
                out = None
            for path in (pi, po):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if out is None:
                self.failed.emit("离线翻译插件执行失败", t)
                return
            lines = out.splitlines()
            lines = [ln.strip() for ln in lines][:n]
            while len(lines) < n:                     # 引擎少数行 → 原文兜底
                lines.append(texts[len(lines)])
            if m is not None:                         # 在线直连的缺口补齐模式
                base, indices = m
                for j, idx in enumerate(indices):
                    if lines[j]:
                        base[idx] = lines[j]
                self.last_engine = "在线直连+词典兜底"
                self.done.emit(base, t)
            else:
                self.last_engine = "离线词典兜底"
                self.done.emit(lines, t)

        proc.finished.connect(_fin)
        proc.errorOccurred.connect(
            lambda _e, p=proc, pi=path_in, po=path_out, t=tag: self._proc_err(p, pi, po, t))
        proc.start()

    def _proc_err(self, proc, path_in, path_out, tag):
        self._pending -= 1
        proc.deleteLater()
        for path in (path_in, path_out):
            try:
                os.remove(path)
            except OSError:
                pass
        self.failed.emit("离线翻译插件无法启动", tag)


if __name__ == "__main__":
    # 自测：解析容错 + 引擎选择/降级 + 在线直连真实请求（需网络）
    import sys
    from PySide6.QtCore import QCoreApplication, QTimer

    P = TranslateClient._parse_batch
    assert P('["你好","世界"]', 2) == ["你好", "世界"]
    assert P('```json\n["a","b","c"]\n```', 3) == ["a", "b", "c"]
    assert P('结果如下：\n["x","y"]\n以上。', 2) == ["x", "y"]
    assert P('["only"]', 2) == ["only", ""]
    assert P('["a","b","c"]', 2) == ["a", "b"]
    assert P("不是数组", 1) is None
    print("parse_batch 解析自测通过")

    gtx = TranslateClient._gtx_parse(
        b'[[["\\u4f60\\u597d","hello",null,null,10]],null,"en",[["hello",["\\u4f60\\u597d"],null,null,10]]]')
    assert gtx == "你好", gtx
    mm = TranslateClient._mymemory_parse(
        b'{"responseData":{"translatedText":"\\u4f60\\u597d"},"responseStatus":200}')
    assert mm == "你好", mm
    assert TranslateClient._mymemory_parse(b'{"responseStatus":403}') == ""
    assert TranslateClient._guess_src("hello world") == "en"
    assert TranslateClient._guess_src("你好世界") == "zh-CN"
    print("gtx / mymemory 解析与语种粗判自测通过")

    if not local_available():
        print("本地插件不存在，跳过降级自测")
        sys.exit(0)

    app = QCoreApplication(sys.argv)

    # Ollama 不可用（沙箱无服务）→ 自动降级离线插件
    cfg = {"translate": {"engine": "ollama", "api_base": "http://127.0.0.1:9", "model": ""}}
    client = TranslateClient(cfg)
    got, err = {}, []

    def on_done(texts, tag):
        got["r"] = texts

    def on_fail(msg, tag):
        err.append(msg)
        got["r"] = []

    client.done.connect(on_done)
    client.failed.connect(on_fail)
    client.translate(["hello world"], "zh-CN", tag=1)
    t0 = QTimer(); t0.setSingleShot(True); t0.timeout.connect(app.quit); t0.start(15_000)
    app.exec()
    assert not err, f"ollama 应降级离线插件: {err}"
    assert isinstance(got.get("r"), list) and len(got["r"]) == 1 and got["r"][0], got
    print("Ollama→离线插件降级链路 OK:", got["r"])

    # 无 Key 的付费引擎 → 必须自动改走免 Key 在线直连（用户“只翻出几个词”的根因回归）
    cfg3 = {"translate": {"engine": "glm", "api_key": ""}}
    c3 = TranslateClient(cfg3)
    online_called = []
    c3._online_translate = lambda texts, target, tag=0: online_called.append(list(texts))
    c3.translate(["hello world"], "zh-CN", tag=3)
    assert online_called == [["hello world"]], \
        f"无 Key 的 glm 应自动改走在线直连, 实际 {online_called}"

    # ollama 引擎 + /v1 结尾端点（LM Studio 等 OpenAI 兼容服务）→ 必须走 /chat/completions
    # （此前按 Ollama 专有路径拼接必然 404 → 静默降级词典，翻译效果极差的根因）
    cfg4 = {"translate": {"engine": "ollama", "api_base": "http://127.0.0.1:9/v1",
                          "model": "minimax-code/MiniMax-M3"}}
    c4 = TranslateClient(cfg4)
    api_calls = []

    def _fake_api(texts, base, key, model, target, source, tag, engine_label=""):
        api_calls.append((base, key, model, engine_label))
    c4._api_translate = _fake_api
    c4.translate(["hello"], "zh-CN", tag=4)
    assert api_calls == [("http://127.0.0.1:9/v1", "", "minimax-code/MiniMax-M3", "本地模型")], \
        f"/v1 端点应走 OpenAI 兼容且不带密钥: {api_calls}"

    # 真 Ollama（无 /v1）+ 显式模型 → 仍走 Ollama 专有 /api/chat
    cfg5 = {"translate": {"engine": "ollama", "api_base": "", "model": "qwen3"}}
    c5 = TranslateClient(cfg5)
    chat_calls = []
    c5._ollama_chat = lambda base, model, texts, target, tag: chat_calls.append((base, model))
    c5.translate(["hi"], "zh-CN", tag=5)
    assert chat_calls == [("http://localhost:11434", "qwen3")], chat_calls

    # 在线直连：真实网络请求（GTX 在本沙箱不可达 → MyMemory 容灾应命中）
    cfg2 = {"translate": {"engine": "online"}}
    c2 = TranslateClient(cfg2)
    got2, err2 = {}, []
    c2.done.connect(lambda texts, tag: got2.update(r=texts))
    c2.failed.connect(lambda msg, tag: err2.append(msg))
    c2.translate(["hello world", "save button"], "zh-CN", tag=2)
    t0 = QTimer(); t0.setSingleShot(True); t0.timeout.connect(app.quit); t0.start(25_000)
    app.exec()
    assert not err2, f"在线直连失败: {err2}"
    r2 = got2.get("r")
    assert isinstance(r2, list) and len(r2) == 2, r2
    print("在线直连多通道自测通过:", r2)

    ready, tip = TranslateClient({"translate": {"engine": "online"}}).engine_ready()
    assert ready and "免 Key" in tip
    print("engine_ready 自测通过:", tip)
    print("translate 自测全部通过")
