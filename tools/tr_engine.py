# -*- coding: utf-8 -*-
"""SnapPin 离线翻译引擎 — CTranslate2 + SentencePiece（Argos/OPUS-MT int8 模型）。

进程隔离 IPC（契约与主程序 translate.py 约定， RapidOCR-json 同款按需起进程）:

    translate-engine.exe --from en --to zh --input <UTF-8 文本文件> --output <输出文件>

- 输入文件每行一段原文（行内换行已被主程序替换为空格）；
- 输出文件与输入等长，每行对应一段译文；空行原样透传；
- 模型查找：exe 同级 models/<from>-<to>/ 目录（--from auto 时取唯一已装语言对）；
- 失败退出码非 0，错误信息写 stderr。

打包: python -m PyInstaller --noconfirm tools/tr_engine.spec（onedir，与模型分包发布）。
自测: 把 argosmodel 解包到本文件同级 models/en-zh/ 后 `python tools/tr_engine.py`。
"""
import os
import re
import sys


def _models_root() -> str:
    """模型目录：exe 旁的 models/（开发态则脚本旁）。"""
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                           else __file__))
    if getattr(sys, "frozen", False):
        base = os.path.dirname(base)          # engine/ 的上一级 = 插件根
    return os.path.join(base, "models")


def _resolve_model(from_lang: str, to_lang: str) -> str:
    """--from auto → 唯一已装语言对；否则精确匹配 <from>-<to>。"""
    root = _models_root()
    exact = os.path.join(root, f"{from_lang}-{to_lang}")
    if from_lang != "auto" and os.path.isdir(exact):
        return exact
    pairs = [d for d in (os.listdir(root) if os.path.isdir(root) else [])
             if os.path.isdir(os.path.join(root, d)) and d.endswith(f"-{to_lang}")]
    if len(pairs) == 1:
        return os.path.join(root, pairs[0])
    if not pairs:
        raise FileNotFoundError(f"未找到 --to {to_lang} 的离线模型（models/ 下无匹配语言对）")
    raise FileNotFoundError(f"已装多个语言对 {pairs}，--from auto 无法唯一确定，请显式指定 --from")


_CJK = "\u2e80-\u9fff\u3000-\u303f\u3040-\u30ff\uac00-\ud7af\uff00-\uffef"


def _cleanup(dst: str) -> str:
    """词片解码后的清尾：▁ 残留折空格收敛；CJK（含标点）相邻悬挂空格去掉。"""
    dst = re.sub(r"\s+", " ", dst.replace("▁", " ")).strip()
    return re.sub(rf"([{_CJK}]) +(?=[{_CJK}])", r"\1", dst)


def translate_lines(lines, from_lang: str, to_lang: str):
    import ctranslate2
    import sentencepiece as spm_pkg

    model_dir = _resolve_model(from_lang, to_lang)
    sp = spm_pkg.SentencePieceProcessor(os.path.join(model_dir, "sentencepiece.model"))
    tr = ctranslate2.Translator(model_dir, device="cpu",
                                intra_threads=min(4, os.cpu_count() or 4))
    idx = [i for i, t in enumerate(lines) if t.strip()]
    pieces = [sp.encode(lines[i], out_type=str) for i in idx]
    out = tr.translate_batch(pieces, beam_size=2, max_decoding_length=256,
                             batch_type="examples")
    result = {idx[j]: _cleanup(sp.decode(r.hypotheses[0])) for j, r in enumerate(out)}
    return [result.get(i, "") for i in range(len(lines))]


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    opts = {}
    for flag in ("--from", "--to", "--input", "--output"):
        if flag in argv:
            opts[flag[2:]] = argv[argv.index(flag) + 1]
    missing = [k for k in ("from", "to", "input", "output") if not opts.get(k)]
    if missing:
        print(f"缺少参数: {missing}", file=sys.stderr)
        return 2
    with open(opts["input"], "r", encoding="utf-8") as f:
        lines = f.read().split("\n")
    out = translate_lines(lines, opts["from"], opts["to"])
    with open(opts["output"], "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            sys.exit(main(sys.argv[1:]))
        except Exception as e:                # noqa: BLE001 — 引擎顶层兜底，错误进 stderr
            print(f"offline engine error: {e}", file=sys.stderr)
            sys.exit(1)
    # 自测：models/en-zh/ 就位于本文件旁时跑真实翻译（无模型则跳过）
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(here, "models")):
        print("无本地 models/，跳过引擎自测")
        sys.exit(0)
    sample = ["Save changes", "", "An unexpected error occurred. Please try again later."]
    got = translate_lines(sample, "auto", "zh")
    assert len(got) == 3, f"输出应与输入等长: {got}"
    assert got[0] and "▁" not in got[0] and got[2], f"译文异常: {got}"
    assert got[1] == "", "空行应原样透传"
    print("tr_engine 自测通过:", got)
