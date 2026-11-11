"""PixPin 滚动长截图引擎 — 行为复刻版（纯 numpy/cv2，不依赖 Qt）。

复刻依据：`local://pixpin_longshot.md`（REA 6.8.0 + Ghidra 12.1.4 逆向
PixPin.exe，每条结论带函数地址与伪码摘录）。目标是把 PixPin 的算法
**逐条对齐**，而不是另起一套更"现代"的实现。

对应关系（PixPin 地址 → 本模块）：

| PixPin | 地址 | 本模块 |
|---|---|---|
| `QLongScreenShot::doMatch` | `0x1400C7470` | `feed()` |
| `matcher->updateReference` | `0x1400CE340` | `update_reference()` |
| `matcher->findOffset` | `0x1400CE460`/`0x1400CE530` | `find_offset()` |
| `帧 → 行投影` | `0x1400CDE60` | `row_profile()` |
| `matcher->compose` | `0x1400CDFB0` / `0x1400CDA00` | `compose()` |

**已确证并对齐的量**（报告附录 B）：
- 行投影宽度 **32**、`INTER_AREA`；纵向 `boxFilter` 核高 **15**
- 匹配器 `cv::BFMatcher(NORM_HAMMING, crossCheck=false)`，`knnMatch(k=1)`
- 单候选误差阈值 `≤ max(30.0, 2 × 全体最小误差)`
- 位移投票 `unordered_map<int,int>`，冠军按票数
- 唯一候选 → 置信度 1.0；否则 `(top − runnerUp) / (top + runnerUp)`，须 **≥ 0.5**
- 搜索窗口 `[last_offset − block, last_offset + 2×block)`，`block` = 上一帧高
- 绝对位移 = 窗口起点 + 冠军 Δ（负值=上滚/左滚，保留原样）
- 失败码：`1` = 到底（画布无空间）、`2` = 未匹配
- 横向 = `cv::rotate` 90° 顺时针 → 复用纵向 → 拼完逆时针转回
- 抓帧周期 **100 ms**（由调用方定时器驱动，本模块不建定时器）

**推断实现（报告第 8 节列为未确证）**：
1. 描述子生成：二进制里只确证了 `cv::BFMatcher(NORM_HAMMING)`，没有
   ORB/SIFT/Feature2D 的 RTTI，说明是自研按行签名。这里把「行投影后的
   32 个灰度字节」直接当 256-bit 描述子——与汉明距离、30.0 的阈值量级
   自洽（同一行内容距离≈0，亮度微变时每像素仅差 1~2 bit）。
2. `m_ref.rows < 3 × block` 时 PixPin 会跳过（`goto LAB_1400ce69a`，该分支
   语义未确证）。这里改为**退化为全量程搜索**，否则第二帧永远无法匹配。
3. 拼接起点：PixPin 是 `(dst.rows − offset) / 2` 后逐行 `memcpy`，但
   offset 的绝对/相对语义报告第 8.4 条明确"未完全核实"，且尺寸不符会
   `exit(-1)`。这里按语义实现（新帧按绝对位移 s 对齐写入），只写新增段，
   避免每帧全图拷贝的 O(n²) 开销。
"""
import numpy as np

try:
    import cv2
except Exception:                                    # pragma: no cover
    cv2 = None

# ---- 报告附录 B：PixPin 的硬编码常量 ----
W_PROFILE = 32           # 行投影宽度（resize dsize.width）
K_BOX = 15               # 行投影纵向 boxFilter 核高
CONF_THRESH = 0.5        # 票数置信度阈值（inner +0x0C = 0x3F000000）
MAX_DIST = 30.0          # 单候选误差上限（double @0x140B6BE00）
INIT_OFFSET = 0x7FFFFFFF  # "上次位移"初值
FRAME_INTERVAL_MS = 100  # 抓帧定时器周期

ERR_OK = 0
ERR_BOTTOM = 1           # 滚到底：画布无空间可扩
ERR_NO_MATCH = 2         # 没匹配上


def row_profile(frame_rgb: np.ndarray) -> np.ndarray:
    """帧 → 行投影（`0x1400CDE60`）。

    PixPin：`cv::cvtColor(..., COLOR_RGB2GRAY)` → `cv::resize(dsize={32, rows},
    INTER_AREA)` → `cv::boxFilter(ksize={1,15}, anchor=(-1,-1), normalize=true)`。
    横向压缩到 32 宽（纵向不变），再纵向 15 像素滑动平均——平滑后同一内容
    行的汉明距离才足够小，投票才有区分度。
    """
    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    h = gray.shape[0]
    prof = cv2.resize(gray, (W_PROFILE, h), interpolation=cv2.INTER_AREA)
    prof = cv2.boxFilter(prof, -1, (1, K_BOX), normalize=True,
                         anchor=(-1, -1), borderType=cv2.BORDER_REFLECT_101)
    return np.ascontiguousarray(prof)


def _hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """逐行汉明距离矩阵，等价 `cv::BFMatcher(NORM_HAMMING)` 的成对距离。

    a: (N, 32) uint8 描述子；b: (M, 32) uint8 → 返回 (N, M) uint16。
    用 uint64 视图 + `np.bitwise_count`（C 实现的 popcount）向量化，
    比逐字节查表快一个量级。
    """
    a64 = np.ascontiguousarray(a).view(np.uint64).reshape(a.shape[0], -1)
    b64 = np.ascontiguousarray(b).view(np.uint64).reshape(b.shape[0], -1)
    out = np.empty((a64.shape[0], b64.shape[0]), np.uint16)
    # 分块：单块最多约 4M 个 uint64 的异或结果，避免大图爆内存
    step = max(1, 4_000_000 // max(1, b64.shape[0] * a64.shape[1]))
    for s in range(0, a64.shape[0], step):
        blk = a64[s:s + step]
        out[s:s + step] = np.bitwise_count(
            blk[:, None, :] ^ b64[None, :, :]).sum(axis=2)
    return out


class PixPinLongShot:
    """`QLongScreenShot` + 内层匹配器的行为复刻。

    mode: 0 = 纵向；1 = 横向（内部 rotate 90° 顺时针后复用纵向逻辑）。
    """

    def __init__(self, mode: int = 0):
        self.mode = 0 if mode != 1 else 1
        self.m_prof = None            # inner+0x10：参考帧行投影（会随长图增长）
        self.block = 0                # inner+0xB0：上一帧高
        self.last_offset = INIT_OFFSET  # inner+0xB4：上一次的绝对位移
        self.has_ref = False          # inner+0xB8
        self.canvas = None            # 长图（RGB，纵向模式；横向为转置态）
        self.status = ERR_OK
        self._prev_pipe = None        # 上一帧的管线态（固定带检测用）
        self._fixed = None            # 固定带 (a, b)，None = 未检测/无

    # ------------------------------------------------------------ 工具
    def _to_pipeline(self, frame_rgb: np.ndarray) -> np.ndarray:
        """横向模式：拼进管线前先 rotate 90° 顺时针（`cv::rotate(...,0)`）。"""
        if self.mode == 1:
            return np.ascontiguousarray(np.rot90(frame_rgb, k=-1))
        return frame_rgb

    def _from_pipeline(self, img: np.ndarray) -> np.ndarray:
        """横向模式：出口转回（`ROTATE_90_COUNTERCLOCKWISE` = 2）。"""
        if self.mode == 1:
            return np.ascontiguousarray(np.rot90(img, k=1))
        return img

    # ------------------------------------------------------------ 参考登记
    def update_reference(self, frame_rgb: np.ndarray):
        self.update_reference_pipe(self._to_pipeline(frame_rgb))

    def update_reference_pipe(self, pipe: np.ndarray):
        """`0x1400CE340`：首帧（或重建参考）时登记行投影与帧高（管线态）。"""
        prof = row_profile(pipe)
        self.m_prof = prof
        self.block = int(prof.shape[0])
        self.last_offset = INIT_OFFSET
        self.has_ref = True
        self.canvas = pipe.copy()
        self.status = ERR_OK

    # ------------------------------------------------------------ 位移搜索
    def find_offset(self, frame_rgb: np.ndarray):
        return self.find_offset_pipe(self._to_pipeline(frame_rgb))

    def find_offset_pipe(self, pipe: np.ndarray):
        """`0x1400CE530`：搜索窗口内 knnMatch(k=1) → 位移投票。

        返回 (status, offset, confidence)：
        - status 0 且 offset 为绝对位移（正=下滚/右滚，负=上滚/左滚）
        - status 1 = 到底（画布没有新空间）
        - status 2 = 未匹配（候选为空 / 置信度不足）
        """
        prof = row_profile(pipe)
        n = int(prof.shape[0])
        if self.m_prof is None or n < 8:
            return ERR_NO_MATCH, 0, 0.0
        rows_ref = int(self.m_prof.shape[0])

        # PixPin: last_offset 仍是初值（首帧之后第一次）时走 LAB_1400ce69a。
        # 该分支语义未确证；这里按「窗口取全量程」处理，否则第二帧必失败。
        if self.last_offset == INIT_OFFSET or rows_ref < self.block * 3:
            start, end = 0, rows_ref
        else:
            start = max(0, self.last_offset - self.block)
            end = min(rows_ref, self.last_offset + 2 * self.block)
        if end - start < 8:
            return ERR_NO_MATCH, 0, 0.0

        roi = self.m_prof[start:end]
        dist = _hamming(prof, roi)                    # (n, roi_rows)
        best = dist.argmin(axis=1).astype(np.int32)    # knnMatch k=1
        dmin = dist[np.arange(n), best]

        # 单候选误差阈值：max(30.0, 2 × 全体最小误差)
        thr = max(MAX_DIST, 2.0 * float(dmin.min()))
        keep = dmin.astype(np.float32) <= thr
        if not bool(keep.any()):
            return ERR_NO_MATCH, 0, 0.0

        # Δ = trainIdx − queryIdx（query=当前帧，train=参考窗口）
        deltas = best[keep] - np.arange(n, dtype=np.int32)[keep]
        vals, counts = np.unique(deltas, return_counts=True)
        if vals.size == 1:
            delta = int(vals[0])
            conf = 1.0                                  # 唯一候选：置信度 1.0
        else:
            order = np.argsort(-counts, kind="stable")
            top = int(counts[order[0]])
            second = int(counts[order[1]])
            conf = (top - second) / float(top + second)
            if conf < CONF_THRESH:
                return ERR_NO_MATCH, 0, float(conf)
            delta = int(vals[order[0]])

        # 绝对位移 = 窗口起点 + 冠军 Δ，**保留符号**。
        # 逐条对齐 0x1400CE530：
        #   *param_3 = iVar11;                    → 负值原样作为输出位移
        #   if (iVar11 < 0) { ...; iVar11 = 0; }  → 只是把搜索锚点归 0
        #   merge(dst, src, *param_3)             → 负 offset 走前插
        #   （merge 0x1400CDA00 开头 if (param_3 < 0) param_3 = -param_3）
        # 之前我把「锚点归 0」误读成「位移被丢弃」，才导致上滚/左滚无效。
        offset = start + delta
        self.last_offset = offset if offset >= 0 else 0
        if offset == 0:
            return ERR_BOTTOM, 0, float(conf)      # 位移为 0：没有新内容
        return ERR_OK, offset, float(conf)

    # ------------------------------------------------------------ 拼接
    def compose(self, frame_rgb: np.ndarray, offset: int):
        self.compose_pipe(self._to_pipeline(frame_rgb), offset)
        return self.canvas

    def compose_pipe(self, pipe: np.ndarray, offset: int):
        """把管线态当前帧按绝对位移 offset 写入长图（`0x1400CDFB0` + `0x1400CDA00`）。

        语义与 PixPin 的逐行 memcpy 一致：目标行 = 源行 + offset，重叠部分被
        新帧覆盖、没有单独的相似度去重；只是这里只写「新增段」，避免每帧
        整图拷贝。
        """
        img = pipe
        if self.canvas is None:
            self.canvas = img.copy()
            self.m_prof = row_profile(self.canvas)
            self.block = int(img.shape[0])
            return self.canvas

        h = int(img.shape[0])
        cur_len = int(self.canvas.shape[0])
        if offset < 0:                                   # 上滚：前插
            add = min(-offset, h)
            if add > 0:
                self.canvas = np.concatenate([img[:add], self.canvas], axis=0)
        else:                                            # 下滚：追加
            start = max(0, cur_len - offset)             # 已有内容起点
            if start < h:
                self.canvas = np.concatenate(
                    [self.canvas, img[start:]], axis=0)

        # 参考投影随长图一起长（PixPin 同样把当前帧投影并进 m_ref）
        self.m_prof = row_profile(self.canvas)
        self.block = h
        return self.canvas

    def result(self) -> np.ndarray:
        """取长图（横向模式转回正立方向）。"""
        if self.canvas is None:
            return None
        return self._from_pipeline(self.canvas)

    # -------------------------------------------------- 固定带（屏幕固定件）
    FIXED_MIN_ROWS = 24      # 连续不变的行数达到它才算固定带

    def _detect_fixed(self, prev_pipe, cur_pipe):
        """连续帧中几乎不变的连续行带（屏幕固定件：吸顶栏/固定右侧栏）。

        横向滚动时网页的固定右侧栏在每帧的相同屏幕位置，逐帧整帧拼接会让
        它在长图里反复出现（用户实测「黑色区域一直变宽」）。检测出来后在
        管线态里把它挖掉，拼接与预览就只剩内容区。"""
        if prev_pipe is None or prev_pipe.shape != cur_pipe.shape:
            return None
        d = np.abs(cur_pipe.astype(np.int16)
                   - prev_pipe.astype(np.int16)).mean(axis=(1, 2))
        quiet = d < 2.0
        best, bs, s = 0, None, None
        for i, q in enumerate(quiet):
            if q and s is None:
                s = i
            elif not q and s is not None:
                if i - s > best:
                    best, bs = i - s, s
                s = None
        if s is not None and len(quiet) - s > best:
            best, bs = len(quiet) - s, s
        # 全帧都不变（静止帧 prev==cur）时 best 会等于整帧：那不是固定件，
        # 而是没有新内容——返回 None，避免把帧裁成空。
        if best >= self.FIXED_MIN_ROWS and best < int(quiet.size) - self.FIXED_MIN_ROWS:
            return (bs, bs + best)
        return None

    def _strip_rows(self, img: np.ndarray, band):
        """从管线态图（拼接轴 = axis0）中挖掉固定带行 [a:b)。"""
        a, b = band
        return np.ascontiguousarray(np.concatenate([img[:a], img[b:]], axis=0))

    def feed(self, frame_rgb: np.ndarray):
        """PixPin 的 doMatch 全流程（`0x1400C7470`）：管线 → 裁固定带 →
        find_offset → compose。返回 (status, offset, conf)。

        status 0 时长图已更新；首帧走 update_reference_pipe 登记参考。"""
        pipe = self._to_pipeline(frame_rgb)
        if self._fixed is None and self.has_ref and self._prev_pipe is not None \
                and self._prev_pipe.shape == pipe.shape:
            band = self._detect_fixed(self._prev_pipe, pipe)
            if band:
                self._fixed = band
                # 一次性从画布与参考投影中挖掉固定带行（后续帧同样裁）
                self.canvas = self._strip_rows(self.canvas, band)
                self.m_prof = self._strip_rows(self.m_prof, band)
        pipe = self._strip_rows(pipe, self._fixed) if self._fixed else pipe
        self._prev_pipe = pipe
        if not self.has_ref:
            self.update_reference_pipe(pipe)
            return ERR_OK, 0, 1.0
        st, off, conf = self.find_offset_pipe(pipe)
        if st == ERR_OK:
            self.compose_pipe(pipe, off)
        self.status = st
        return st, off, conf


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":                               # pragma: no cover
    import sys

    if cv2 is None:
        print("需要 opencv")
        raise SystemExit(1)

    rng = np.random.default_rng(11)

    def make_page(h, w=800, dense=False):
        page = np.zeros((h, w, 3), np.uint8)
        page[:] = 250
        y = 0
        while y < h:
            bh = int(rng.integers(4, 10)) if dense else int(rng.integers(14, 30))
            if y + bh > h:
                break
            page[y:y + bh] = rng.integers(20, 230, (bh, w, 1)).astype(np.uint8)
            y += bh + (2 if dense else 12)
        return page

    def frame_of(page, off, h):
        return page[off:off + h].copy()

    H = 600
    page = make_page(3600)
    dense = make_page(3600, dense=True)

    print("=== 纵向：位移还原（PixPin 算法）===")
    ok = 0
    total = 0
    for off in (137, 212, 45, 333, 58, 90):
        eng = PixPinLongShot(0)
        eng.update_reference(frame_of(page, 300, H))
        st, s, conf = eng.find_offset(frame_of(page, 300 + off, H))
        good = (st == ERR_OK and abs(s - off) <= 2)
        ok += good
        total += 1
        print(f"  下滚 Δ={off:4d} -> status={st} offset={s:5d} conf={conf:.3f} "
              f"{'OK' if good else 'FAIL'}")

    for off in (-90, -150, -37):
        eng = PixPinLongShot(0)
        eng.update_reference(frame_of(page, 900, H))
        st, s, conf = eng.find_offset(frame_of(page, 900 + off, H))
        # 上滚：cur[k] = ref[k+off]，off<0 → 绝对位移保留负值原样返回
        # （`*param_3 = iVar11`），由调用方决定前插/忽略
        good = (st == ERR_OK and s == off)
        ok += good
        total += 1
        print(f"  上滚 Δ={off:4d} -> status={st} offset={s:5d} conf={conf:.3f} "
              f"{'OK(负位移保留)' if good else 'FAIL'}")

    print("=== 静止帧 ===")
    eng = PixPinLongShot(0)
    f = frame_of(page, 300, H)
    eng.update_reference(f)
    st, s, conf = eng.find_offset(f)
    print(f"  Δ=0 -> status={st} offset={s} conf={conf:.3f} "
          f"{'OK(无新内容)' if st in (ERR_BOTTOM, ERR_OK) and s == 0 else 'FAIL'}")

    print("=== 无匹配（纯色）===")
    eng = PixPinLongShot(0)
    flat = np.full((H, 800, 3), 200, np.uint8)
    eng.update_reference(flat)
    st, s, conf = eng.find_offset(flat)
    print(f"  纯色 -> status={st} {'OK(拒答)' if st == ERR_NO_MATCH else '注意: status=' + str(st)}")

    print("=== 横向（rotate 90° 复用纵向）===")
    wide = make_page(3600)                               # 高 3600 的页面
    wide_t = np.ascontiguousarray(np.rot90(wide, k=1))   # 变成宽 3600（模拟横向内容）
    eng = PixPinLongShot(1)
    # 横向滚动 = 视口沿列移动；引擎内部 rot90 后列轴就是行轴
    f0 = np.ascontiguousarray(wide_t[:, 300:300 + H]).copy()
    f1 = np.ascontiguousarray(wide_t[:, 420:420 + H]).copy()
    eng.update_reference(f0)
    st, s, conf = eng.find_offset(f1)
    good = (st == ERR_OK and abs(s - 120) <= 2)
    print(f"  Δ=120 -> status={st} offset={s} conf={conf:.3f} "
          f"{'OK' if good else 'FAIL'}")
    if st == ERR_OK:
        eng.compose(f1, s)
        print(f"  横向长图 {eng.result().shape[1]}x{eng.result().shape[0]}")

    print("=== 连续拼接（多帧累积）===")
    eng = PixPinLongShot(0)
    base = 200
    eng.update_reference(frame_of(page, base, H))
    for step in (150, 120, 200, 90):
        base += step
        st, s, conf = eng.find_offset(frame_of(page, base, H))
        if st == ERR_OK:
            eng.compose(frame_of(page, base, H), s)
    out = eng.result()
    print(f"  长图 {out.shape[1]}x{out.shape[0]}，期望高约 {H + 150 + 120 + 200 + 90}")

    print(f"\n纵向位移还原 {ok}/{total}")
