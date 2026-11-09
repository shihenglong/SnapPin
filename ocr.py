"""RapidOCR-json 封装 — RapidAI 开源引擎 (vendor/plugins/ocr)，单次调用模式。"""
import json
import os

from PySide6.QtCore import QObject, QProcess, Signal

import config

_VENDOR = os.path.join(config.app_root(), "vendor", "plugins", "ocr")
_EXE = os.path.join(_VENDOR, "RapidOCR-json.exe")
_MODELS = os.path.join(_VENDOR, "models")

_ARGS = [
    "--models", "models",
    "--det", "ch_PP-OCRv4_det_infer.onnx",
    "--cls", "ch_ppocr_mobile_v2.0_cls_infer.onnx",
    "--rec", "rec_ch_PP-OCRv4_infer.onnx",
    "--keys", "dict_chinese.txt",
    "--ensureAscii=0", "--ensureLogger=0",
]

_lang_models = {  # 语种 -> (det, rec, keys)
    "zh": ("ch_PP-OCRv4_det_infer.onnx", "rec_ch_PP-OCRv4_infer.onnx", "dict_chinese.txt"),
    "en": ("ch_PP-OCRv3_det_infer.onnx", "rec_en_PP-OCRv3_infer.onnx", "dict_chinese.txt"),
    "cht": ("ch_PP-OCRv3_det_infer.onnx", "rec_chinese_cht_PP-OCRv3_infer.onnx", "dict_chinese_cht.txt"),
    "ja": ("ch_PP-OCRv3_det_infer.onnx", "rec_japan_PP-OCRv3_infer.onnx", "dict_japan.txt"),
    "ko": ("ch_PP-OCRv3_det_infer.onnx", "rec_korean_PP-OCRv3_infer.onnx", "dict_korean.txt"),
    "ru": ("ch_PP-OCRv3_det_infer.onnx", "rec_cyrillic_PP-OCRv3_infer.onnx", "dict_cyrillic.txt"),
}


class OcrRunner(QObject):
    """run(QPixmap) → done(list[{box, text, score}])；同一时刻只允许一个任务。"""
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._lang = "zh"

    def set_lang(self, lang: str):
        if lang in _lang_models:
            self._lang = lang

    def busy(self) -> bool:
        return self._proc is not None

    def run(self, pixmap, tmp_path=None):
        if self._proc is not None:
            self.failed.emit("OCR 正在进行中")
            return
        import tempfile
        path = tmp_path or os.path.join(tempfile.gettempdir(), "snappin_ocr.png")
        if not pixmap.save(path, "PNG"):
            self.failed.emit("图片保存失败")
            return

        det, rec, keys = _lang_models[self._lang]
        if os.environ.get('SNAPPIN_DEBUG'):
            print('OCR run:', path, flush=True)
        self._proc = QProcess(self)
        self._proc.setWorkingDirectory(_VENDOR)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(lambda _: self._emit_failed("OCR 进程错误"))
        args = ["--models", "models", "--det", det, "--cls",
                "ch_ppocr_mobile_v2.0_cls_infer.onnx", "--rec", rec, "--keys", keys,
                "--ensureAscii=0", "--ensureLogger=0", f"--image_path={path}"]
        self._proc.start(_EXE, args)

    def _emit_failed(self, msg):
        self._cleanup()
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
            return
        if result.get("code") != 100:
            self.failed.emit(f"OCR 失败: {result.get('data', '未知错误')}")
            return
        items = [{"box": d["box"], "text": d["text"], "score": d.get("score", 0)}
                 for d in result.get("data", []) if d.get("text")]
        self.done.emit(items)


def available() -> bool:
    return os.path.exists(_EXE)
