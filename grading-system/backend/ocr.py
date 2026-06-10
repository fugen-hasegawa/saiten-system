"""OCR ラッパ: yomitoku Python API を使用してフルモデルで解析する。"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# ─── DocumentAnalyzer シングルトン ──────────────────────────────────────────

_analyzer = None
_analyzer_lock = threading.Lock()


def _get_analyzer(device: str = "cpu"):
    global _analyzer
    if _analyzer is None:
        with _analyzer_lock:
            if _analyzer is None:
                from yomitoku import DocumentAnalyzer
                # configs={} でデフォルト（フルモデル: parseq-large-v4.1）を使用
                # --lite の parseq-tiny より大幅に精度向上
                _analyzer = DocumentAnalyzer(
                    configs={},
                    device=device,
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

def _enhance_for_ocr(page_img):
    """
    BGR numpy 画像のコントラスト・シャープネスを強化して OCR 精度を上げる。
    CLAHE は使わない（表の罫線・矢印をノイズとして増幅するリスクがある）。
    """
    import numpy as np
    from PIL import Image as _PILImage, ImageEnhance

    pil = _PILImage.fromarray(page_img[:, :, ::-1])  # BGR → RGB
    pil = ImageEnhance.Contrast(pil).enhance(1.2)
    pil = ImageEnhance.Sharpness(pil).enhance(1.5)
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
    device: str = "cpu",
    save_first_page_to: str | Path | None = None,
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
        for page_img in pages_iter:
            if first and save_first_page_to:
                save_page_image(page_img, save_first_page_to)
            first = False
            enhanced = _enhance_for_ocr(page_img)
            schema, _, _ = analyzer(enhanced)
            result.append(_schema_to_dict(schema))

    # DocumentAnalyzer.__call__ が asyncio.run() を使うため別スレッドで起動
    t = threading.Thread(target=_process)
    t.start()
    t.join()

    if not result:
        raise RuntimeError("OCR 処理結果が空です。")
    return result


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

_MIN_SINGLE_CHAR_SCORE = 0.10  # フルモデルは精度が高いのでやや緩くする


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
    for word in words:
        content = word.get("content", "").strip()
        if len(content) <= 1 and word.get("rec_score", 0.0) >= _MIN_SINGLE_CHAR_SCORE:
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
