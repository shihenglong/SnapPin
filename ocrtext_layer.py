"""PixPin 文字图层复刻：`OcrTextItemLayer` + `OcrGraphicsTextItem`。

逆向依据：`local://pixpin_ocr.md` 第 6 节（地址级证据）。

| PixPin | 地址 | 本模块 |
|---|---|---|
| `OcrGraphicsTextItem` 构造 | `0x14005e160` | `OcrGraphicsTextItem.__init__` |
| 选字交互（返回模式码 1..7） | `0x14005f0f0` | `OcrTextItemLayer.hit_test()` |
| 设置字符区间 `(selStart, selEnd)` | `0x14005f8a0` | `OcrGraphicsTextItem.set_selection()` |
| 四边形上二分定位字符 | `0x14005f210` | `OcrGraphicsTextItem.char_at()` |
| 文本回收（遍历 childItems + 动态转换） | `0x140060100` | `OcrTextItemLayer.selection_ranges()` |
| 图层构造 | `0x14005fc90` | `OcrTextItemLayer.__init__` |

与旧实现的关键差别（也是"抄"的重点）：

1. **每个识别块是一个 QGraphicsItem**，自带四角坐标 + 文本 + 置信度
   （PixPin 的 +0x48 四角 / +0x68 文本 / +0x70 置信度 / +0x80 首边长度）；
2. 选中态是**字符索引区间**，不是逐字符矩形集合；
3. 字符位置由**四边形插值**算出（斜排/透视文本也准），不是"均分宽度"；
4. 命中字符用**二分**（字符位置沿文本方向单调递增），不是线性扫描；
5. 高亮色沿用 PixPin 实测值 `QColor(0x33,0x99,0xff,0x7f)`。

坐标系：所有 item 用**图像原始像素坐标**；显示缩放由 QGraphicsView 的
transform 负责，因此 item 内部完全不用管 zoom。
"""
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import QGraphicsItem

# PixPin 实测：+0x88 = QColor(0x33,0x99,0xff,0x7f)
HILITE = QColor(0x33, 0x99, 0xff, 0x7f)
FRAME_PEN = QPen(QColor(0x33, 0x99, 0xff, 0x9a), 1)


class OcrGraphicsTextItem(QGraphicsItem):
    """一个识别块：四角坐标 + 文本 + 置信度 + 字符区间选中态。"""

    def __init__(self, quad, text, score=0.0, parent=None):
        super().__init__(parent)
        # quad: [左上, 右上, 右下, 左下]（图像原始像素，顺序由 OCR 侧保证）
        self.quad = [QPointF(p[0], p[1]) for p in quad]
        self.text = text or ""
        self.score = float(score)
        # PixPin +0x80：首边长度（构造时算一次，选字/命中都用它做尺度）
        self.first_edge = QLineF(self.quad[0], self.quad[1]).length()
        self.sel_start = -1          # 字符区间（闭区间）；-1 = 未选中
        self.sel_end = -1
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)   # 同 PixPin 0x2000
        self.setAcceptedMouseButtons(Qt.NoButton)   # 命中由图层统一算

    # -------------------------------------------------- 几何
    def boundingRect(self) -> QRectF:
        xs = [p.x() for p in self.quad]
        ys = [p.y() for p in self.quad]
        # 外扩 2px：边框/高亮的半像素不越界，避免重绘残影
        return QRectF(min(xs) - 2, min(ys) - 2,
                      max(xs) - min(xs) + 4, max(ys) - min(ys) + 4)

    def _edge_point(self, t: float):
        """沿文本方向取 t∈[0,1] 处的（上边点, 下边点）。

        上边 = p0→p1，下边 = p3→p2；两点连线即该处的"字符列"。"""
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        top = QPointF(self.quad[0].x() + (self.quad[1].x() - self.quad[0].x()) * t,
                      self.quad[0].y() + (self.quad[1].y() - self.quad[0].y()) * t)
        bot = QPointF(self.quad[3].x() + (self.quad[2].x() - self.quad[3].x()) * t,
                      self.quad[3].y() + (self.quad[2].y() - self.quad[3].y()) * t)
        return top, bot

    def char_quad(self, i: int):
        """第 i 个字符的四边形（四边形插值，斜排文本也正确）。"""
        n = max(1, len(self.text))
        i = max(0, min(n - 1, i))
        t0, t1 = i / n, (i + 1) / n
        a0, b0 = self._edge_point(t0)
        a1, b1 = self._edge_point(t1)
        return [a0, a1, b1, b0]

    def t_of(self, pos: QPointF) -> float:
        """把图像坐标点投影到文本方向上 → t∈[0,1]（超出则夹住）。"""
        vx = self.quad[1].x() - self.quad[0].x()
        vy = self.quad[1].y() - self.quad[0].y()
        # 用"下边-上边"作为行高方向：把点先归到上边基线再投影，
        # 这样点在文本框内任意高度都能得到稳定的 t
        hx = self.quad[3].x() - self.quad[0].x()
        hy = self.quad[3].y() - self.quad[0].y()
        px = pos.x() - self.quad[0].x()
        py = pos.y() - self.quad[0].y()
        vv = vx * vx + vy * vy
        if vv < 1e-9:
            return 0.0
        hh = hx * hx + hy * hy
        if hh > 1e-9:
            k = (px * hx + py * hy) / hh
            px -= k * hx
            py -= k * hy
        t = (px * vx + py * vy) / vv
        return 0.0 if t < 0 else (1.0 if t > 1 else t)

    def char_at(self, pos: QPointF) -> int:
        """命中字符：**二分**定位（字符 t 区间沿文本方向单调）。

        等价 PixPin `0x14005f210` 的"四边形上二分定位字符"。边界用
        `t*n <= mid+0.5` 判左，避免"恰好在字符中心"被推到下一个字符。"""
        n = len(self.text)
        if n == 0:
            return -1
        t = self.t_of(pos) * n
        lo, hi = 0, n - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if t <= mid + 0.5:
                hi = mid
            else:
                lo = mid + 1
        return lo

    # -------------------------------------------------- 选中态
    def set_selection(self, start: int, end: int):
        """设置字符区间（闭区间）；等价 PixPin `0x14005f8a0(item, selStart, selEnd)`。"""
        n = len(self.text)
        if n == 0 or start < 0 or end < 0:
            self.clear_selection()
            return
        a, b = (start, end) if start <= end else (end, start)
        a = max(0, min(n - 1, a))
        b = max(0, min(n - 1, b))
        if (a, b) != (self.sel_start, self.sel_end):
            self.sel_start, self.sel_end = a, b
            self.update()

    def clear_selection(self):
        if self.sel_start != -1 or self.sel_end != -1:
            self.sel_start = self.sel_end = -1
            self.update()

    def has_selection(self) -> bool:
        return self.sel_start >= 0 and self.sel_end >= self.sel_start

    def selected_text(self) -> str:
        if not self.has_selection():
            return ""
        return self.text[self.sel_start:self.sel_end + 1]

    # -------------------------------------------------- 绘制
    def paint(self, p, option, widget=None):
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        # 块框：细蓝线（PixPin 的 OCR 图层态）
        p.setPen(FRAME_PEN)
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(self.quad)
        if not self.has_selection():
            return
        # 选中字符高亮：逐字符四边形填充
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(HILITE))
        for i in range(self.sel_start, self.sel_end + 1):
            q = self.char_quad(i)
            p.drawPolygon(q)


class OcrTextItemLayer(QGraphicsItem):
    """文字图层容器：持有所有 `OcrGraphicsTextItem`，负责跨块流式选择。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []                      # 按阅读序（y 带内 → x）
        self._anchor = None                  # (item, char) 选字锚点
        self._cursor = None                  # (item, char) 当前拖拽点

    # -------------------------------------------------- 构建
    def set_boxes(self, boxes, scale_ignored=None):
        """boxes: [{'quad': [[x,y]×4], 'text': str, 'score': float}]（原始像素）。

        `scale_ignored` 只为兼容旧调用签名——坐标缩放由 view 的 transform 负责。
        """
        for it in self.items:
            it.setParentItem(None)
            if it.scene():
                it.scene().removeItem(it)
        self.items = []
        for b in boxes:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            it = OcrGraphicsTextItem(b["quad"], text, b.get("score", 0.0), self)
            self.items.append(it)
        # 阅读序：先按行带（y//12）再按 x —— 与旧实现一致，保证跨块选择顺序正确
        self.items.sort(key=lambda it: (int(it.quad[0].y()) // 12, it.quad[0].x()))
        self.prepareGeometryChange()
        self.update()

    def clear(self):
        for it in self.items:
            it.setParentItem(None)
            if it.scene():
                it.scene().removeItem(it)
        self.items = []
        self._anchor = self._cursor = None
        self.update()

    # -------------------------------------------------- 几何
    def boundingRect(self) -> QRectF:
        if not self.items:
            return QRectF()
        r = QRectF()
        for it in self.items:
            r = it.boundingRect() if r.isNull() else r.united(it.boundingRect())
        return r

    def paint(self, p, option, widget=None):
        return          # 子项自己画

    # -------------------------------------------------- 命中
    def hit_test(self, pos: QPointF):
        """返回命中的 item（等价 PixPin 选字交互 `0x14005f0f0` 的模式判定）。"""
        for it in self.items:
            xs = [p.x() for p in it.quad]
            ys = [p.y() for p in it.quad]
            if min(xs) - 2 <= pos.x() <= max(xs) + 2 and \
                    min(ys) - 2 <= pos.y() <= max(ys) + 2:
                return it
        return None

    # -------------------------------------------------- 流式选择
    def begin_selection(self, pos: QPointF):
        it = self.hit_test(pos)
        if it is None:
            return False
        self._anchor = (it, it.char_at(pos))
        self._apply_selection(self._anchor, self._anchor)
        return True

    def drag_selection(self, pos: QPointF):
        if self._anchor is None:
            return
        it = self.hit_test(pos)
        if it is None:                       # 拖出文字区：夹到最近的块
            last = self.items[-1]
            it = last if pos.y() >= last.quad[0].y() else self.items[0]
            ci = 0 if it is self.items[0] else len(it.text) - 1
        else:
            ci = it.char_at(pos)
        self._cursor = (it, ci)
        self._apply_selection(self._anchor, self._cursor)

    def end_selection(self):
        self._anchor = self._cursor = None

    def clear_selection(self):
        for it in self.items:
            it.clear_selection()
        self._anchor = self._cursor = None

    def selection_ranges(self):
        """选中范围：[(item, start, end)]（按阅读序），供上层拼文本。"""
        return [(it, it.sel_start, it.sel_end)
                for it in self.items if it.has_selection()]

    def selection_text(self, joiner=None) -> str:
        parts = [it.selected_text() for it, _a, _b in self.selection_ranges()]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        if joiner is None:
            return "".join(parts)
        out = parts[0]
        for seg in parts[1:]:
            out = joiner(out, seg)
        return out

    def selection_count(self) -> int:
        return sum(b - a + 1 for _it, a, b in self.selection_ranges())

    def has_selection(self) -> bool:
        return bool(self.selection_ranges())

    def _apply_selection(self, a_t, b_t):
        """把 (item,char) 区间铺到各 item：同一块取两点间，跨块按阅读序。"""
        if a_t is None or b_t is None or not self.items:
            return
        ia = self.items.index(a_t[0])
        ib = self.items.index(b_t[0])
        ca, cb = a_t[1], b_t[1]
        if ia > ib or (ia == ib and ca > cb):
            ia, ib, ca, cb = ib, ia, cb, ca
        for k, it in enumerate(self.items):
            n = len(it.text)
            if n == 0:
                it.clear_selection()
                continue
            if ia == ib and k == ia:
                it.set_selection(ca, cb)
            elif k == ia:
                it.set_selection(ca, n - 1)
            elif k == ib:
                it.set_selection(0, cb)
            elif ia < k < ib:
                it.set_selection(0, n - 1)
            else:
                it.clear_selection()


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":                               # pragma: no cover
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import sys

    from PySide6.QtWidgets import QApplication, QGraphicsScene

    app = QApplication(sys.argv)

    def quad(x, y, w, h):
        return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

    # ---- 单块：字符区间 + 二分命中 + 四边形插值
    it = OcrGraphicsTextItem(quad(0, 0, 100, 20), "ABCDEFGHIJ", 0.9)
    assert abs(it.first_edge - 100) < 1e-6, "首边长度"
    # 字符 i 占据 [i/n,(i+1)/n]，取每字符中心应命中自己
    n = len(it.text)
    for i in range(n):
        cx = (i + 0.5) / n * 100
        got = it.char_at(QPointF(cx, 10))
        assert got == i, f"字符 {i} 中心命中 {got}"
    it.set_selection(2, 5)
    assert it.selected_text() == "CDEF", it.selected_text()
    it.set_selection(5, 2)                       # 反向传参也应归一
    assert it.selected_text() == "CDEF"
    it.clear_selection()
    assert not it.has_selection() and it.selected_text() == ""
    # 四边形插值：斜排（上边 y=0，下边 y=20；右边上移 → 斜切）
    sl = OcrGraphicsTextItem([[0, 10], [100, 0], [100, 20], [0, 30]], "ABCD", 0.8)
    cq = sl.char_quad(0)
    assert cq[0].x() == 0 and cq[1].x() == 25, [p.x() for p in cq]
    assert cq[0].y() == 10 and cq[3].y() == 30, "斜排下字符左边界应连到上下边"

    # ---- 图层：跨块流式选择
    scene = QGraphicsScene()
    layer = OcrTextItemLayer()
    scene.addItem(layer)
    layer.set_boxes([
        {"quad": quad(0, 0, 100, 20), "text": "Hello", "score": 0.9},
        {"quad": quad(0, 40, 100, 20), "text": "World", "score": 0.9},
        {"quad": quad(0, 80, 40, 20), "text": "end", "score": 0.9},
    ])
    assert len(layer.items) == 3
    assert layer.begin_selection(QPointF(20, 10))          # 第 1 块第 1 个字符
    layer.drag_selection(QPointF(50, 50))                  # 第 2 块 'r'（t=0.5）
    ranges = layer.selection_ranges()
    assert len(ranges) == 2, ranges
    assert ranges[0][1] == 1 and ranges[0][2] == 4, ranges[0]
    assert ranges[1][1] == 0 and ranges[1][2] == 2, ranges[1]
    assert layer.selection_text() == "ello" + "Wor", layer.selection_text()
    assert layer.selection_count() == 4 + 3, layer.selection_count()   # "ello"+"Wor"
    layer.end_selection()
    # 反向拖拽（从下往上）结果应一致
    layer.clear_selection()
    layer.begin_selection(QPointF(50, 50))
    layer.drag_selection(QPointF(20, 10))
    assert layer.selection_text() == "ello" + "Wor", layer.selection_text()
    # 同一块内
    layer.clear_selection()
    layer.begin_selection(QPointF(5, 10))
    layer.drag_selection(QPointF(95, 10))
    assert layer.selection_text() == "Hello", layer.selection_text()
    # 拖到最末块尾部
    layer.clear_selection()
    layer.begin_selection(QPointF(5, 10))
    layer.drag_selection(QPointF(200, 200))
    assert layer.selection_text().endswith("end"), layer.selection_text()
    print("ocrtext_layer 自测通过: 字符区间/二分命中/四边形插值/跨块流式选择 OK")
