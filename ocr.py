"""OCR 引擎 — PixPin 式三段式管线（检测→裁块→超分→识别）+ RapidOCR 回退。

主路径是**进程内推理**（ocr_engine.OcrPipeline，与 PixPin 的 ncnn 三段结构
一致）：检测与识别分离之后，每块文字可以单独决定要不要超分、用哪个识别
模型——长截图里字号混杂时这是关键差别。onnxruntime / opencv 缺失或推理
异常时自动回退 RapidOCR-json.exe（旧路径原样保留），行为与改造前一致。
"""
import json
import os

import numpy as np
from PySide6.QtCore import QObject, QProcess, QThread, Signal
from PySide6.QtGui import QImage

import config
import ocr_engine

_VENDOR = os.path.join(config.app_root(), "vendor", "plugins", "ocr")
_EXE = os.path.join(_VENDOR, "RapidOCR-json.exe")
_MODELS = os.path.join(_VENDOR, "models")


_lang_models = {  # 语种 -> (det, rec, keys)
    "zh": ("ch_PP-OCRv4_det_infer.onnx", "rec_ch_PP-OCRv4_infer.onnx", "dict_chinese.txt"),
    "en": ("ch_PP-OCRv3_det_infer.onnx", "rec_en_PP-OCRv3_infer.onnx", "dict_chinese.txt"),
    "cht": ("ch_PP-OCRv3_det_infer.onnx", "rec_chinese_cht_PP-OCRv3_infer.onnx", "dict_chinese_cht.txt"),
    "ja": ("ch_PP-OCRv3_det_infer.onnx", "rec_japan_PP-OCRv3_infer.onnx", "dict_japan.txt"),
    "ko": ("ch_PP-OCRv3_det_infer.onnx", "rec_korean_PP-OCRv3_infer.onnx", "dict_korean.txt"),
    "ru": ("ch_PP-OCRv3_det_infer.onnx", "rec_cyrillic_PP-OCRv3_infer.onnx", "dict_cyrillic.txt"),
}


def _pixmap_to_bgr(pixmap) -> np.ndarray:
    """QPixmap → BGR ndarray（推理输入）。必须在主线程调用：QPixmap 绑定
    GUI 线程资源，跨线程访问不安全。"""
    img = pixmap.toImage().convertToFormat(QImage.Format_RGB888)
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None
    arr = np.frombuffer(img.constBits(), dtype=np.uint8)
    arr = arr.reshape(h, img.bytesPerLine())[:, :w * 3].reshape(h, w, 3)
    return np.ascontiguousarray(arr[:, :, ::-1])          # RGB → BGR


class _OcrWorker(QObject):
    """工作线程：进程内跑「检测 → 裁块 → 超分 → 识别」。"""

    done = Signal(list)
    failed = Signal(str)

    def __init__(self, pipe, img_bgr):
        super().__init__()
        self._pipe = pipe
        self._img = img_bgr

    def run(self):
        try:
            self.done.emit(self._pipe.run(self._img))
        except Exception as e:
            self.failed.emit(f"OCR 失败: {e}")


class OcrRunner(QObject):
    """run(QPixmap) → done(list[{box, text, score}])；同一时刻只允许一个任务。

    分辨率适配：引擎 --maxSideLen 默认 1024，大图先被硬缩再检测，小字直接
    检测不到（“只能识别部分文字”的根因）。这里按图实际长边下传参数，
    ≤4096（含 4K 全屏）整图不缩放；超长图（长截图贴图取字）沿长边切块
    串行识别，box 平移回全局坐标后按重叠区去重合并。
    """
    done = Signal(list)
    failed = Signal(str)

    _MAX_SIDE = 4096        # 单次送检长边上限；防 det 内存/耗时爆炸
    _CHUNK_OVERLAP = 64     # 块间重叠：跨界文字行两侧都能完整检出，再去重

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._lang = "zh"
        self._chunks = []          # 待识别块 [(x, y, QPixmap)]
        self._chunk_items = []     # 已完成块的结果（已平移回全局坐标）
        self._thread = None        # 进程内推理的工作线程
        self._worker = None
        self._pipes = {}           # 语种 → OcrPipeline（ONNX session 复用）

    def set_lang(self, lang: str):
        if lang in _lang_models:
            self._lang = lang

    def busy(self) -> bool:
        return self._proc is not None or self._thread is not None

    @classmethod
    def _plan_chunks(cls, w: int, h: int) -> list:
        """切块方案 [(x, y, cw, ch)]。不超限 → 整图单块；否则沿长边切，
        相邻块重叠 _CHUNK_OVERLAP，跨界文字行至少在一块里完整出现。"""
        if max(w, h) <= cls._MAX_SIDE or min(w, h) < 32:
            return [(0, 0, w, h)]
        vertical = h >= w
        span = h if vertical else w
        step = cls._MAX_SIDE - cls._CHUNK_OVERLAP
        out, start = [], 0
        while True:
            size = min(cls._MAX_SIDE, span - start)
            out.append((0, start, w, size) if vertical else (start, 0, size, h))
            if start + size >= span:
                return out
            start += step

    def run(self, pixmap, tmp_path=None):
        if self.busy():
            self.failed.emit("OCR 正在进行中")
            return
        if ocr_engine.available():
            self._run_engine(pixmap)
            return
        # 回退路径：RapidOCR-json.exe（改造前的原实现，整段保留）
        self._chunks = [(x, y, pixmap.copy(x, y, cw, ch))
                        for x, y, cw, ch in
                        self._plan_chunks(pixmap.width(), pixmap.height())]
        self._chunk_items = []
        self._run_chunk(tmp_path)

    # ---- 进程内推理（PixPin 式三段管线）--------------------------
    def _pipeline(self):
        """按语种取管线；模型 session 只加载一次并复用。"""
        pipe = self._pipes.get(self._lang)
        if pipe is None and self._lang not in self._pipes:
            det, rec, keys = _lang_models[self._lang]
            try:
                pipe = ocr_engine.OcrPipeline(_MODELS, det, rec, keys)
            except Exception:
                pipe = None
            self._pipes[self._lang] = pipe
        return pipe

    def warmup(self):
        """预热：把当前语种的 ONNX session 与字典先加载好（首次约 1~2 秒），
        避免用户钉图后第一次取字等两秒。"""
        pipe = self._pipeline()
        if pipe is None:
            return False
        try:
            pipe.bank.session(pipe.det_name)
            pipe.bank.session(pipe.rec_name)
            pipe._chars()
        except Exception:
            return False
        return True

    def _run_engine(self, pixmap):
        try:
            img = _pixmap_to_bgr(pixmap)
        except Exception as e:
            self.failed.emit(f"图片转换失败: {e}")
            return
        if img is None:
            self.failed.emit("图片为空")
            return
        pipe = self._pipeline()
        if pipe is None:
            self.failed.emit("OCR 模型加载失败")
            return
        self._thread = QThread(self)
        self._worker = _OcrWorker(pipe, img)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._engine_done)
        self._worker.failed.connect(self._engine_failed)
        self._thread.start()

    def _engine_done(self, items):
        self._teardown_thread()
        # 与旧路径同构：切块重叠区会重复检出同一行，沿用同一套去重
        self.done.emit(self._dedup(items))

    def _engine_failed(self, msg):
        self._teardown_thread()
        self.failed.emit(msg)

    def _teardown_thread(self):
        if self._worker is not None:
            try:
                self._worker.done.disconnect()
                self._worker.failed.disconnect()
            except Exception:
                pass
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread.deleteLater()
            self._thread = None

    def _run_chunk(self, tmp_path=None):
        import tempfile
        _ox, _oy, pix = self._chunks[0]
        path = tmp_path or os.path.join(tempfile.gettempdir(), "snappin_ocr.png")
        if not pix.save(path, "PNG"):
            self.failed.emit("图片保存失败")
            self._chunks = []
            return
        det, rec, keys = _lang_models[self._lang]
        side = max(1024, min(self._MAX_SIDE, max(pix.width(), pix.height())))
        if os.environ.get('SNAPPIN_DEBUG'):
            print('OCR run:', path, f'{pix.width()}x{pix.height()}',
                  'side=', side, flush=True)
        self._proc = QProcess(self)
        self._proc.setWorkingDirectory(_VENDOR)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(lambda _: self._emit_failed("OCR 进程错误"))
        args = ["--models", "models", "--det", det, "--cls",
                "ch_ppocr_mobile_v2.0_cls_infer.onnx", "--rec", rec, "--keys", keys,
                "--ensureAscii=0", "--ensureLogger=0",
                f"--maxSideLen={side}", f"--image_path={path}"]
        self._proc.start(_EXE, args)

    def _emit_failed(self, msg):
        self._cleanup()
        self._chunks = []
        self.failed.emit(msg)

    def _cleanup(self):
        if self._proc:
            self._proc.deleteLater()
            self._proc = None

    def _on_finished(self, code, status):
        proc = self._proc
        self._cleanup()
        if not proc:
            return
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        if os.environ.get('SNAPPIN_DEBUG'):
            print('OCR out head:', out[:150], flush=True)
        result = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('{"code"'):
                try:
                    result = json.loads(line)
                except Exception:
                    pass
        if not result:
            self.failed.emit("OCR 无输出")
            self._chunks = []
            return
        if result.get("code") != 100:
            self.failed.emit(f"OCR 失败: {result.get('data', '未知错误')}")
            self._chunks = []
            return
        ox, oy, _pix = self._chunks.pop(0)
        self._chunk_items.extend(
            [{"box": [[p[0] + ox, p[1] + oy] for p in d["box"]],
              "text": d["text"], "score": d.get("score", 0)}
             for d in result.get("data", []) if d.get("text")])
        if self._chunks:
            self._run_chunk()
            return
        self.done.emit(self._dedup(self._chunk_items))

    @staticmethod
    def _dedup(items: list) -> list:
        """重叠区同一行会被相邻两块各检出一次：外接矩形交叠超过较小块面积
        60% 视为重复，保留先出现（前一块）的。"""
        def bbox(it):
            xs = [p[0] for p in it["box"]]
            ys = [p[1] for p in it["box"]]
            return min(xs), min(ys), max(xs), max(ys)

        out, kept = [], []
        for it in items:
            x0, y0, x1, y1 = bbox(it)
            area = (x1 - x0) * (y1 - y0)
            dup = False
            for kx0, ky0, kx1, ky1 in kept:
                ix = max(0, min(x1, kx1) - max(x0, kx0))
                iy = max(0, min(y1, ky1) - max(y0, ky0))
                if ix * iy > 0.6 * min(area, (kx1 - kx0) * (ky1 - ky0)):
                    dup = True
                    break
            if not dup:
                out.append(it)
                kept.append((x0, y0, x1, y1))
        return out


def available() -> bool:
    """进程内管线可用即算可用；否则看 RapidOCR-json.exe 是否在位。"""
    return ocr_engine.available() or os.path.exists(_EXE)


if __name__ == "__main__":
    # 切块方案：不超限整图单块；超长图沿长边切，覆盖完整、相邻重叠 64
    assert OcrRunner._plan_chunks(800, 600) == [(0, 0, 800, 600)]
    chunks = OcrRunner._plan_chunks(800, 10000)
    assert chunks[0] == (0, 0, 800, 4096) and len(chunks) == 3, chunks
    assert chunks[-1][1] + chunks[-1][3] == 10000, "最后一块必须贴到图底"
    for a, b in zip(chunks, chunks[1:]):
        assert b[1] - a[1] == OcrRunner._MAX_SIDE - OcrRunner._CHUNK_OVERLAP, \
            "相邻块重叠必须为 64px"
    wide = OcrRunner._plan_chunks(12000, 300)      # 横向长图沿 x 切
    assert wide[0] == (0, 0, 4096, 300) and wide[-1][0] + wide[-1][2] == 12000
    # 去重：重叠区同一行被检出两次只留一个；不同行全保留
    def _line(x, y):
        return {"box": [[x, y], [x + 100, y], [x + 100, y + 20], [x, y + 20]],
                "text": "行", "score": 1}
    dedup = OcrRunner._dedup([_line(10, 10), _line(12, 11), _line(10, 200)])
    assert len(dedup) == 2, f"重叠检出应去重, 实际 {len(dedup)}"
    print("ocr 自测通过: 切块方案/覆盖完整/重叠去重 全部 OK")
