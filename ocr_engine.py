"""PixPin 式 OCR 管线：检测 → 裁块 → 超分 → 识别。

为什么不用现成的 RapidOCR-json.exe 黑盒：PixPin 的做法是把 OCR 拆成
**detect（找文本框）→ sr（超分）→ rec（识别）** 三段，检测与识别分离
之后，每一块文字才能单独决定「要不要放大」「用哪个识别模型」——长截图
里字号混杂、有的字清晰有的字极小，黑盒一把梭只能整体用一个尺度。

本模块按同样的三段结构自己跑：

- 检测（det）：DB 概率图 → 文本四边形。做了标准 DB 后处理
  （二值化 → 轮廓 → minAreaRect → 外扩 unclip → 概率均值打分）。
- 超分（sr）：**小字块先放大再送识别**，这是小字号识别率的主要提升点。
  PixPin 用 ncnn 跑自带的 sr.caffemodel；这里默认用 LANCZOS 放大
  （无额外模型依赖），接口留了可插拔的模型超分位置。
- 识别（rec）：按宽高比缩放到高 48、宽 ≤ 320，CTC 贪心解码，逐块独立，
  可按语种切换模型。

依赖 onnxruntime + opencv（headless）。两者任一缺失时上层
（ocr.OcrRunner）自动回退到 RapidOCR-json.exe，行为与改造前一致。

纯 numpy/cv2 计算，不依赖 Qt，可单独跑 `python ocr_engine.py` 自测。
"""
import os
import threading

import numpy as np

try:
    import cv2
    _HAVE_CV2 = True
except Exception:                                   # pragma: no cover
    _HAVE_CV2 = False

try:
    import onnxruntime as ort
    _HAVE_ORT = True
except Exception:                                   # pragma: no cover
    _HAVE_ORT = False


# ---------------------------------------------------------------- 可用性
def available() -> bool:
    return _HAVE_CV2 and _HAVE_ORT


# ---------------------------------------------------------------- 模型管理
class _ModelBank:
    """按需加载 ONNX session + 字典（懒加载 + 缓存，跨线程安全）。"""

    def __init__(self, root: str):
        self.root = root
        self._lock = threading.Lock()
        self._sess = {}
        self._keys = {}

    def session(self, name: str):
        with self._lock:
            if name not in self._sess:
                path = os.path.join(self.root, name)
                so = ort.SessionOptions()
                so.intra_op_num_threads = 2
                so.log_severity_level = 3
                self._sess[name] = ort.InferenceSession(
                    path, sess_options=so, providers=["CPUExecutionProvider"])
            return self._sess[name]

    def keys(self, name: str) -> list:
        """字典：保留全部行（第一行通常是 'blank'，对应 CTC 空类）。"""
        with self._lock:
            if name not in self._keys:
                path = os.path.join(self.root, name)
                with open(path, "rb") as f:
                    lines = f.read().decode("utf-8").splitlines()
                self._keys[name] = lines
            return self._keys[name]


# ---------------------------------------------------------------- 预处理
# DB 检测的归一化常量：ImageNet 均值/方差（PixPin 侧实测取值为
# mean{0.485,0.456,0.406}、norm{1/58.395,1/57.12,1/57.375}，等价 (x-mean)/std）。
# 注意这与**识别**用的 (x/255-0.5)/0.5 不是同一套——混用会明显拉低检测框质量。
_DET_MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
_DET_STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)


def _det_preprocess(img_bgr: np.ndarray, max_side: int):
    """PaddleOCR det 预处理：只缩不放、长边限幅、补到 32 的倍数、
    按 ImageNet 均值方差归一化。返回 (张量, 有效高宽)。

    通道保持调用方传入的 BGR 序不动：PaddleOCR 的检测链路本身就是
    「cv2 读出的 BGR 像素 + RGB 顺序的均值」，这里与之一致，否则会因为
    通道与均值错配而掉精度。"""
    h, w = img_bgr.shape[:2]
    ratio = min(max_side / float(max(h, w)), 1.0)
    rh = max(1, int(round(h * ratio)))
    rw = max(1, int(round(w * ratio)))
    im = cv2.resize(img_bgr, (rw, rh), interpolation=cv2.INTER_LINEAR) \
        if (rw, rh) != (w, h) else img_bgr
    ph = max(32, int(np.ceil(rh / 32)) * 32)
    pw = max(32, int(np.ceil(rw / 32)) * 32)
    canvas = np.zeros((ph, pw, 3), np.float32)
    canvas[:rh, :rw] = im.astype(np.float32)
    canvas = canvas.transpose(2, 0, 1)[None] / 255.0
    canvas = (canvas - _DET_MEAN) / _DET_STD
    return canvas, (rh, rw)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """把 4 个角点排成固定顺时针序：左上、右上、右下、左下。

    cv2.boxPoints 返回的起点随矩形角度变化，直接用会让下游拿到「负宽高」
    的框（pinwin 的 _set_boxes 直接按点序算宽高），所以这里统一排一次。"""
    p = np.asarray(pts, dtype=np.float32)
    s = p.sum(axis=1)
    d = np.diff(p, axis=1).ravel()
    return np.array([p[np.argmin(s)], p[np.argmin(d)],
                     p[np.argmax(s)], p[np.argmax(d)]], dtype=np.float32)


def _crop_poly(img: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """按四边形做透视变换裁出文本条。"""
    pts = _order_quad(pts)
    w = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
    h = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
    w = max(w, 8)
    h = max(h, 8)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    m = cv2.getPerspectiveTransform(pts.astype(np.float32), dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


_REC_MAX_W = 2048        # 长行宽度上限


def _rec_prepare(crop: np.ndarray, img_h: int = 48, img_w: int = None):
    """识别预处理：等比缩放到高 img_h，宽按宽高比自适应。

    宽度不能封顶到 320：PaddleOCR 的 rec 输入宽度随宽高比伸缩，硬压到
    320 会把一行长文本横向挤扁（实测 40 个 A 的窄条只解出 2 个字）。
    这里按 48×宽高比算目标宽，上限 2048，批次内再统一补零。"""
    h, w = crop.shape[:2]
    tw_need = int(np.ceil(img_h * w / float(max(h, 1))))
    tw_need = max(img_h, min(tw_need, _REC_MAX_W))
    th = img_h
    canvas_w = int(img_w) if img_w else tw_need
    canvas_w = max(canvas_w, tw_need)
    im = cv2.resize(crop, (tw_need, th), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((img_h, canvas_w, 3), np.float32)
    canvas[:th, :tw_need] = im.astype(np.float32)
    canvas = canvas.transpose(2, 0, 1) / 255.0
    canvas = (canvas - 0.5) / 0.5
    return canvas, (th, tw_need)


# ---------------------------------------------------------------- 管线
class OcrPipeline:
    """detect → crop → sr(超分) → rec。线程内调用，一次一张图。"""

    MAX_SIDE = 2048          # 送检长边上限（实测 1280/2048/4096 精度相当，
                             # 2048 兼顾小字与耗时；更大的图按此值切块）
    CHUNK_OVERLAP = 64       # 超长图切块的重叠：跨界行至少在一块里完整
    BOX_THRESH = 0.30        # DB 二值化阈值
    UNCLIP = 1.60            # 文本框外扩系数
    SR_MIN_H = 24            # 裁块高度小于它就当「小字」，先放大
    SR_MAX_FACTOR = 4

    def __init__(self, models_dir: str, det: str, rec: str, keys: str,
                 cls: str = "ch_ppocr_mobile_v2.0_cls_infer.onnx"):
        self.bank = _ModelBank(models_dir)
        self.det_name = det
        self.rec_name = rec
        self.keys_name = keys
        self.cls_name = cls
        self._char_list = None
        self._class_map = {}
        self._blank = 0

    # -- 字典 ------------------------------------------------------
    def _chars(self):
        """建立「类别号 → 字符」映射表。

        模型输出类数 = 1(blank) + 字典字符数 + 1(空格)：字典首行是
        'blank' 时字符从类 1 开始，否则类 0 当 blank、类 1 起是首字符；
        多出来的末尾一类映射成空格（否则 'Hello World' 会粘成
        'HelloWorld'）。"""
        if self._char_list is None:
            ks = self.bank.keys(self.keys_name)
            self._blank = 0
            chars = ks[1:] if ks and ks[0] == "blank" else ks
            self._char_list = chars
            self._class_map = {i + 1: ch for i, ch in enumerate(chars)}
            self._class_map[len(chars) + 1] = " "
        return self._char_list

    # -- 检测 ------------------------------------------------------
    def _detect(self, img_bgr: np.ndarray):
        sess = self.bank.session(self.det_name)
        h, w = img_bgr.shape[:2]
        x, (rh, rw) = _det_preprocess(img_bgr, self.MAX_SIDE)
        prob = sess.run(None, {sess.get_inputs()[0].name: x})[0]
        prob = prob[0, 0]
        binary = (prob > self.BOX_THRESH).astype(np.uint8)
        contours, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / float(rw), h / float(rh)      # 有效区 → 原图
        out = []
        for cnt in contours:
            rect = cv2.minAreaRect(cnt)
            bw, bh = rect[1]
            if bw < 3 or bh < 3:
                continue
            pts = cv2.boxPoints(rect)
            x0 = int(max(0, np.floor(pts[:, 0].min())))
            x1 = int(min(prob.shape[1], np.ceil(pts[:, 0].max())))
            y0 = int(max(0, np.floor(pts[:, 1].min())))
            y1 = int(min(prob.shape[0], np.ceil(pts[:, 1].max())))
            if x1 <= x0 or y1 <= y0:
                continue
            score = float(prob[y0:y1, x0:x1].mean())
            if score < self.BOX_THRESH:
                continue
            cx, cy = pts.mean(axis=0)
            pts = (pts - [cx, cy]) * self.UNCLIP + [cx, cy]   # 外扩
            pts = pts * [sx, sy]                             # 回原图
            out.append((_order_quad(np.clip(pts, [0, 0], [w - 1, h - 1])),
                        score))
        return out

    # -- 方向 ------------------------------------------------------
    def _orientation(self, crop: np.ndarray) -> int:
        """0=正，1=转 180°（分类器不可用时一律按正处理）。"""
        try:
            sess = self.bank.session(self.cls_name)
        except Exception:
            return 0
        try:
            h, w = crop.shape[:2]
            ratio = 192 / 48.0
            tw = max(1, min(192, int(round(48 * w / float(max(h, 1))))))
            im = cv2.resize(crop, (tw, 48), interpolation=cv2.INTER_LINEAR)
            canvas = np.zeros((48, 192, 3), np.float32)
            canvas[:, :tw] = im.astype(np.float32)
            x = canvas.transpose(2, 0, 1)[None] / 255.0
            x = (x - 0.5) / 0.5
            out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
            return int(out[0].argmax())
        except Exception:
            return 0

    # -- 超分（PixPin sr 阶段的等价物）----------------------------
    def _upscale(self, crop: np.ndarray) -> np.ndarray:
        """小字块先放大再识别。没有 SR 模型时用 LANCZOS 近似放大；
        若要接模型超分，替换本函数即可（输入小图、返回大图）。"""
        h = crop.shape[0]
        if h >= self.SR_MIN_H:
            return crop
        factor = min(self.SR_MAX_FACTOR, max(2, int(round(48.0 / max(h, 1)))))
        return cv2.resize(crop, (crop.shape[1] * factor, h * factor),
                          interpolation=cv2.INTER_CUBIC)

    # -- 识别 ------------------------------------------------------
    def _recognize(self, crops: list):
        """批量识别 [(crop, pts, score)] → [(text, score, pts)]。"""
        if not crops:
            return []
        sess = self.bank.session(self.rec_name)
        in_name = sess.get_inputs()[0].name
        chars = self._chars()
        prepped = []
        for crop, _pts, _sc in crops:
            x, (th, tw) = _rec_prepare(crop)
            prepped.append(x)
        # 按有效宽度分桶，减少 padding 浪费
        order = sorted(range(len(prepped)),
                       key=lambda i: prepped[i].shape[2])
        results = [None] * len(prepped)
        batch = 8
        for s in range(0, len(order), batch):
            idxs = order[s:s + batch]
            wmax = max(prepped[i].shape[2] for i in idxs)
            stack = np.zeros((len(idxs), 3, 48, wmax), np.float32)
            for j, i in enumerate(idxs):
                stack[j, :, :, :prepped[i].shape[2]] = prepped[i]
            out = sess.run(None, {in_name: stack})[0]      # (N,T,C)
            for j, i in enumerate(idxs):
                results[i] = self._ctc_decode(out[j], chars)
        out_items = []
        for (crop, pts, sc), res in zip(crops, results):
            text, cscore = res
            out_items.append((text, float(cscore * sc), pts))
        return out_items

    def _ctc_decode(self, logits: np.ndarray, chars: list):
        """CTC 贪心解码：argmax → 去连续重复 → 去 blank → 取字符 + 置信度。"""
        idx = logits.argmax(axis=1)
        conf = logits.max(axis=1)
        out, scores = [], []
        prev = -1
        for k, c in enumerate(idx):
            c = int(c)
            if c != prev:
                ch = self._class_map.get(c)
                if ch is not None:
                    out.append(ch)
                    scores.append(float(conf[k]))
            prev = c
        text = "".join(out).strip()
        score = float(np.mean(scores)) if scores else 0.0
        return text, score

    # -- 整图 ------------------------------------------------------
    def run(self, img_bgr: np.ndarray) -> list:
        """整图识别 → [{'box': [[x,y]×4], 'text': str, 'score': float}]。"""
        items = []
        for ox, oy, sub in self._chunks(img_bgr):
            boxes = self._detect(sub)
            crops = []
            for pts, sc in boxes:
                crop = _crop_poly(sub, pts)
                if self._orientation(crop) == 1:
                    crop = cv2.rotate(crop, cv2.ROTATE_180)
                crop = self._upscale(crop)
                crops.append((crop, pts, sc))
            for text, sc, pts in self._recognize(crops):
                if not text.strip():
                    continue
                items.append({"box": [[float(p[0] + ox), float(p[1] + oy)]
                                      for p in pts],
                              "text": text, "score": sc})
        return items

    def _chunks(self, img_bgr: np.ndarray):
        """超长图沿长边切块（带重叠），yield (ox, oy, 子图)。"""
        h, w = img_bgr.shape[:2]
        if max(h, w) <= self.MAX_SIDE or min(h, w) < 32:
            yield 0, 0, img_bgr
            return
        vertical = h >= w
        span = h if vertical else w
        step = self.MAX_SIDE - self.CHUNK_OVERLAP
        start = 0
        while True:
            size = min(self.MAX_SIDE, span - start)
            if vertical:
                yield 0, start, img_bgr[start:start + size]
            else:
                yield start, 0, img_bgr[:, start:start + size]
            if start + size >= span:
                return
            start += step


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":                                  # pragma: no cover
    import tempfile
    from PIL import Image, ImageDraw, ImageFont

    if not available():
        print("缺依赖：需要 onnxruntime + opencv")
        raise SystemExit(1)

    root = r"C:\Workspace\SystemWorkSpace\pixditto\vendor\plugins\ocr\models"
    pipe = OcrPipeline(root, "ch_PP-OCRv4_det_infer.onnx",
                       "rec_ch_PP-OCRv4_infer.onnx", "dict_chinese.txt")

    img = Image.new("RGB", (900, 260), (255, 255, 255))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 34)
    except Exception:
        font = None
    d.text((30, 30), "PixPin 滚动截图 OCR 识别测试", fill=(0, 0, 0), font=font)
    d.text((30, 110), "Hello World 12345", fill=(20, 20, 20), font=font)
    small = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 13) if font else None
    d.text((30, 190), "小字号文字也要能认出来 tiny text", fill=(60, 60, 60),
           font=small)
    arr = np.array(img)[:, :, ::-1].copy()                 # RGB → BGR

    t0 = __import__("time").perf_counter()
    out = pipe.run(arr)
    dt = __import__("time").perf_counter() - t0
    for it in out:
        print(f"  {it['score']:.3f}  {it['text']!r}")
    print(f"共 {len(out)} 块，耗时 {dt*1000:.0f} ms")
