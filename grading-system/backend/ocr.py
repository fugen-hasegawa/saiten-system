"""OCR ラッパ: yomitoku Python API を使用してフルモデルで解析する。"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# ─── DocumentAnalyzer シングルトン ──────────────────────────────────────────

_analyzer = None
_analyzer_lock = threading.Lock()


def _resolve_device(device: str = "auto") -> str:
    """
    使用デバイスを解決する。

    "auto": MPS → CPU の順で自動選択
    "mps" / "cpu": 指定通り
    config.json の ocr_device が "auto" 以外なら config を優先する。
    """
    import json
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg_device = json.load(f).get("ocr_device", "auto")
    except Exception:
        cfg_device = "auto"

    target = cfg_device if cfg_device != "auto" else device
    if target == "auto":
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    return target


def _get_analyzer(device: str = "auto"):
    global _analyzer
    if _analyzer is None:
        with _analyzer_lock:
            if _analyzer is None:
                from yomitoku import DocumentAnalyzer
                resolved = _resolve_device(device)
                print(f"[OCR] device: {resolved}")
                _analyzer = DocumentAnalyzer(
                    configs={},
                    device=resolved,
                    visualize=False,
                )
    return _analyzer


# ─── データ型 ────────────────────────────────────────────────────────────────

@dataclass
class Cell:
    row: int
    col: int
    row_span: int
    col_span: int
    text: str
    bbox: list  # [x0, y0, x1, y1]


# ─── スキーマ → dict 変換 ─────────────────────────────────────────────────

def _schema_to_dict(schema) -> dict:
    """DocumentAnalyzerSchema を parse_tables が期待する dict 形式に変換する。"""
    tables = []
    for t in schema.tables:
        cells = []
        for c in t.cells:
            cells.append({
                "row":      c.row,
                "col":      c.col,
                "row_span": c.row_span,
                "col_span": c.col_span,
                "contents": (c.contents or "").strip(),
                "box":      list(c.box),
            })
        tables.append({"cells": cells})

    words = []
    for w in schema.words:
        pts = w.points
        words.append({
            "content":   (w.content or "").strip(),
            "rec_score": float(w.rec_score),
            "points":    [[int(p[0]), int(p[1])] for p in pts],
        })

    paragraphs = []
    for p in schema.paragraphs:
        paragraphs.append({
            "contents":  (p.contents or "").strip(),
            "box":       list(p.box),
            "direction": p.direction or "horizontal",
        })

    return {"tables": tables, "words": words, "paragraphs": paragraphs}


# ─── ページ画像保存（answer_key プレビュー用）────────────────────────────────

def _load_preprocess_cfg() -> dict:
    """config.json の ocr_preprocess を読む。"""
    import json
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f).get("ocr_preprocess", {})
    except Exception:
        return {}


def _enhance_for_ocr(page_img):
    """
    BGR numpy 画像を前処理して OCR 精度を上げる。
    パラメータは config.json の ocr_preprocess で調整可能。

    処理順:
      1. ノイズ除去（denoise=true のとき MedianFilter）
      2. コントラスト強化
      3. シャープネス強化
    CLAHE は罫線ノイズ増幅リスクがあるため不採用。
    """
    from PIL import Image as _PILImage, ImageEnhance, ImageFilter

    cfg      = _load_preprocess_cfg()
    contrast  = float(cfg.get("contrast",  1.4))
    sharpness = float(cfg.get("sharpness", 2.0))
    denoise   = bool(cfg.get("denoise",    True))

    pil = _PILImage.fromarray(page_img[:, :, ::-1])  # BGR → RGB

    if denoise:
        # MedianFilter(3): 孤立ノイズを除去しつつ文字エッジを保持
        pil = pil.filter(ImageFilter.MedianFilter(size=3))

    pil = ImageEnhance.Contrast(pil).enhance(contrast)
    pil = ImageEnhance.Sharpness(pil).enhance(sharpness)

    import numpy as np
    return np.array(pil)[:, :, ::-1]


def save_page_image(page_img, dest_path: str | Path) -> tuple[int, int]:
    """
    OCR 処理前の生ページ画像 (BGR numpy) を JPEG 保存する。
    Returns: (width, height) of the saved image
    """
    from PIL import Image as _PILImage
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pil = _PILImage.fromarray(page_img[:, :, ::-1])  # BGR → RGB
    pil.save(dest, quality=92)
    return pil.width, pil.height


# ─── メイン OCR 関数 ──────────────────────────────────────────────────────

def run_yomitoku(
    input_path: str | Path,
    out_dir: str | Path | None = None,  # 後方互換（使用しない）
    device: str = "auto",
    save_first_page_to: str | Path | None = None,
    save_pages_to_dir: str | Path | None = None,
    # 後方互換パラメータ（無視）
    lite: bool = False,
    combine: bool = True,
    viz: bool = False,
) -> list[dict]:
    """
    yomitoku Python API (フルモデル) で OCR を実行し、ページ dict リストを返す。

    DocumentAnalyzer は内部で asyncio.run() を使うため、別スレッドで実行する。

    Args:
        input_path: PDF または画像ファイルパス
        save_first_page_to: 最初のページ画像を JPEG 保存するパス（answer_key preview 用）
        save_pages_to_dir: 全ページを page_0.jpg ... として保存するディレクトリ
    Returns:
        list of page dicts: [{tables, words, paragraphs}, ...]
    """
    from yomitoku.data.functions import load_pdf, load_image

    path = Path(input_path)
    if path.suffix.lower() == ".pdf":
        pages_iter = load_pdf(str(path), dpi=200)
    else:
        pages_iter = load_image(str(path))

    result: list[dict] = []
    first = True

    def _process():
        nonlocal first
        analyzer = _get_analyzer(device)
        for idx, page_img in enumerate(pages_iter):
            if first and save_first_page_to:
                save_page_image(page_img, save_first_page_to)
            if save_pages_to_dir:
                save_page_image(page_img, Path(save_pages_to_dir) / f"page_{idx}.jpg")
            first = False
            enhanced = _enhance_for_ocr(page_img)
            schema, _, _ = analyzer(enhanced)
            page_dict = _schema_to_dict(schema)
            _reocr_empty_cells(page_dict, enhanced, analyzer)
            result.append(page_dict)

    # DocumentAnalyzer.__call__ が asyncio.run() を使うため別スレッドで起動
    t = threading.Thread(target=_process)
    t.start()
    t.join()

    if not result:
        raise RuntimeError("OCR 処理結果が空です。")
    return result


# ─── セル個別再OCR ────────────────────────────────────────────────────────────

def _cell_reocr_enabled() -> bool:
    import json
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return bool(json.load(f).get("ocr_cell_reocr", True))
    except Exception:
        return True


def _reocr_empty_cells(page_dict: dict, page_img_bgr, analyzer) -> None:
    """
    全ページOCR後も空のテーブルセルを個別クロップして再OCRで補完する。

    処理:
      1. セル bbox を切り出してパディング追加
      2. 短辺が 80px 未満なら拡大（最大4倍）
      3. 前処理 → analyzer で認識
      4. 最高スコアのワードを採用（スコア 0.25 以上）
    """
    if not _cell_reocr_enabled():
        return

    from PIL import Image as _PILImage
    import numpy as np

    ih, iw = page_img_bgr.shape[:2]
    pil_page = _PILImage.fromarray(page_img_bgr[:, :, ::-1])  # BGR→RGB

    reocr_count = 0
    filled_count = 0

    for table in page_dict.get("tables", []):
        for cell in table.get("cells", []):
            if cell["contents"].strip():
                continue  # すでに認識済みはスキップ

            b = cell["box"]
            x0, y0, x1, y1 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue

            pad = 5
            crop = pil_page.crop((
                max(0, x0 - pad), max(0, y0 - pad),
                min(iw, x1 + pad), min(ih, y1 + pad),
            ))

            # 短辺が 80px 未満なら拡大（最大4倍）
            cw, ch = crop.width, crop.height
            scale = max(1, min(4, 80 // max(1, min(cw, ch))))
            if scale > 1:
                crop = crop.resize((cw * scale, ch * scale), _PILImage.LANCZOS)

            arr = np.array(crop)[:, :, ::-1]  # RGB→BGR
            arr = _enhance_for_ocr(arr)

            reocr_count += 1
            try:
                sub_schema, _, _ = analyzer(arr)
                best_text, best_score = "", 0.0
                for w in sub_schema.words:
                    t = (w.content or "").strip()
                    s = float(w.rec_score)
                    if t and s > best_score:
                        best_text, best_score = t, s
                if best_text and best_score >= 0.25:
                    cell["contents"] = best_text
                    filled_count += 1
            except Exception:
                pass

    if reocr_count:
        print(f"[OCR] cell re-OCR: {reocr_count} 空セル → {filled_count} 補完")


# ─── bbox ヘルパ ─────────────────────────────────────────────────────────────

def _word_bbox(word: dict) -> list[int]:
    pts = word["points"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _overlap_x_ratio(cell_bbox: list, wb: list) -> float:
    ix0 = max(cell_bbox[0], wb[0])
    ix1 = min(cell_bbox[2], wb[2])
    if ix0 >= ix1:
        return 0.0
    cell_w = cell_bbox[2] - cell_bbox[0]
    return (ix1 - ix0) / cell_w if cell_w > 0 else 0.0


def _overlap_y_ratio(cell_bbox: list, wb: list) -> float:
    iy0 = max(cell_bbox[1], wb[1])
    iy1 = min(cell_bbox[3], wb[3])
    if iy0 >= iy1:
        return 0.0
    cell_h = cell_bbox[3] - cell_bbox[1]
    return (iy1 - iy0) / cell_h if cell_h > 0 else 0.0


# ─── 表セルへの文字割り当て ───────────────────────────────────────────────

def _get_min_score() -> float:
    """config.json の ocr_min_score を読む（デフォルト 0.40）。"""
    import json
    cfg_path = Path(__file__).parent / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return float(json.load(f).get("ocr_min_score", 0.40))
    except Exception:
        return 0.40


def _assign_words_to_cells(cells: list[Cell], words: list[dict]) -> None:
    """
    words を空の cell に割り当てる（in-place）。

    Pass 1: 縦スパン多文字ワード（手書き解答列）
      - セル中心 y 位置から比例マッピングで各文字を割り当て（改善版）
    Pass 2: 単文字ワードを残った空セルに（低スコア除外）
    """
    col_empty: dict[int, list[Cell]] = defaultdict(list)
    for cell in cells:
        if not cell.text:
            col_empty[cell.col].append(cell)
    for col in col_empty:
        col_empty[col].sort(key=lambda c: c.row)

    def _apply_word(word: dict, empty_only: bool) -> None:
        wb = _word_bbox(word)
        text = word.get("content", "").strip()
        if not text:
            return

        matching_cols = [
            col for col, col_cells in col_empty.items()
            if any(_overlap_x_ratio(c.bbox, wb) > 0.25 for c in col_cells)
        ]

        for col in matching_cols:
            col_cells = col_empty[col]
            overlapping = [c for c in col_cells if _overlap_x_ratio(c.bbox, wb) > 0.25]
            if not overlapping:
                continue

            avg_cell_h = sum(c.bbox[3] - c.bbox[1] for c in overlapping) / len(overlapping)
            word_h = wb[3] - wb[1]

            if word_h > avg_cell_h * 1.5 and len(text) > 1:
                # スペース区切り文字（例: "エ ウ ウ イ ア"）を正しく分割するため
                # スペースを除いた文字リストで比例マッピングする
                chars = [c for c in text if not c.isspace()]
                n = len(chars)
                if n == 0:
                    continue
                for cell in col_cells:
                    if cell.text:
                        continue
                    if _overlap_y_ratio(cell.bbox, wb) <= 0.05:
                        continue
                    cell_yc = (cell.bbox[1] + cell.bbox[3]) / 2
                    rel = max(0.0, min(1.0 - 1e-9, (cell_yc - wb[1]) / word_h))
                    cell.text = chars[int(rel * n)]
            else:
                if empty_only:
                    best = max(overlapping, key=lambda c: _overlap_y_ratio(c.bbox, wb))
                    if not best.text and _overlap_y_ratio(best.bbox, wb) > 0.2:
                        best.text = text

    # Pass 1: 縦スパン多文字ワードを優先（スコア不問）
    for word in words:
        if len(word.get("content", "").strip()) > 1:
            _apply_word(word, empty_only=False)

    # Pass 2: 単文字ワードを残った空セルに（低スコアは除外）
    min_score = _get_min_score()
    for word in words:
        content = word.get("content", "").strip()
        if len(content) <= 1 and word.get("rec_score", 0.0) >= min_score:
            _apply_word(word, empty_only=True)


def parse_tables(yomitoku_pages: list[dict]) -> list[list[Cell]]:
    """
    ページ dict リストから全テーブルのセルリストを返す。
    """
    result: list[list[Cell]] = []
    for page in yomitoku_pages:
        for table in page.get("tables", []):
            cells: list[Cell] = []
            for c in table["cells"]:
                cells.append(Cell(
                    row=c["row"],
                    col=c["col"],
                    row_span=c["row_span"],
                    col_span=c["col_span"],
                    text=c["contents"].strip(),
                    bbox=c["box"],
                ))
            _assign_words_to_cells(cells, page.get("words", []))
            result.append(cells)
    return result
