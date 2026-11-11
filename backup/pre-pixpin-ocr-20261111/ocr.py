"""RapidOCR-json 封装 — RapidAI 开源引擎 (vendor/plugins/ocr)，单次调用模式。"""
import json
import os

from PySide6.QtCore import QObject, QProcess, Signal

import config

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

    def set_lang(self, lang: str):
        if lang in _lang_models:
            self._lang = lang

    def busy(self) -> bool:
        return self._proc is not None

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
        if self._proc is not None:
            self.failed.emit("OCR 正在进行中")
            return
        self._chunks = [(x, y, pixmap.copy(x, y, cw, ch))
                        for x, y, cw, ch in
                        self._plan_chunks(pixmap.width(), pixmap.height())]
        self._chunk_items = []
        self._run_chunk(tmp_path)

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
    return os.path.exists(_EXE)


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
