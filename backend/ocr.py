"""OCR ラッパ: yomitoku CLI を呼び出し、表構造を解析する。"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Cell:
    row: int
    col: int
    row_span: int
    col_span: int
    text: str
    bbox: list  # [x0, y0, x1, y1]


def _yomitoku_bin() -> str:
    """プロジェクトルートの .venv 内の yomitoku を返す。"""
    root = Path(__file__).parent.parent
    candidate = root / ".venv" / "bin" / "yomitoku"
    if candidate.exists():
        return str(candidate)
    return "yomitoku"  # PATH から探す


def run_yomitoku(
    input_path: str | Path,
    out_dir: str | Path,
    lite: bool = True,
    device: str = "cpu",
    combine: bool = True,
) -> list[dict]:
    """
    yomitoku CLI を subprocess で実行し、出力 JSON をパースして返す。

    Returns:
        list of page dicts, each with keys: figures, paragraphs, tables, words
    Raises:
        RuntimeError: yomitoku が非ゼロ終了した場合
        FileNotFoundError: JSON 出力が見つからない場合
    """
    cmd = [_yomitoku_bin(), str(input_path), "-f", "json", "-d", device, "-o", str(out_dir)]
    if lite:
        cmd.append("-l")
    if combine:
        cmd.append("--combine")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yomitoku failed (exit {result.returncode}):\n{result.stderr}")

    out_dir = Path(out_dir)
    json_files = sorted(out_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"yomitoku JSON not found in {out_dir}")

    with open(json_files[-1], encoding="utf-8") as f:
        data = json.load(f)

    return data if isinstance(data, list) else [data]


# ─── bbox ヘルパ ────────────────────────────────────────────────────────────

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


# ─── 表セルへの文字割り当て ────────────────────────────────────────────────

_MIN_SINGLE_CHAR_SCORE = 0.15  # 単文字ワードはこれ以上のスコアが必要


def _assign_words_to_cells(cells: list[Cell], words: list[dict]) -> None:
    """
    words を空の cell に割り当てる（in-place）。

    処理順序:
    1. 縦スパン多文字ワード（複数行にまたがる大きな手書き文字列）を先に処理
    2. 単文字・短ワードは残った空セルにのみ割り当てる（低スコアはスキップ）
    """
    # 列ごとに空セルをまとめる
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
                # 縦スパン word: 文字数で等分割し行に割り当てる
                n = len(text)
                char_h = word_h / n
                for i, ch in enumerate(text):
                    char_yc = wb[1] + (i + 0.5) * char_h
                    for cell in col_cells:
                        if cell.text:
                            continue
                        if cell.bbox[1] <= char_yc <= cell.bbox[3]:
                            cell.text = ch
                            break
            else:
                if empty_only:
                    # 単文字/短ワードは y 重なりが最大のセルに割り当てる
                    best = max(overlapping, key=lambda c: _overlap_y_ratio(c.bbox, wb))
                    if not best.text and _overlap_y_ratio(best.bbox, wb) > 0.2:
                        best.text = text

    # Pass 1: 縦スパン多文字ワードを優先（スコア不問）
    multi_words = [
        w for w in words
        if len(w.get("content", "").strip()) > 1
    ]
    for word in multi_words:
        _apply_word(word, empty_only=False)

    # Pass 2: 単文字ワードを残った空セルに（低スコアは除外）
    single_words = [
        w for w in words
        if len(w.get("content", "").strip()) <= 1
        and w.get("rec_score", 0.0) >= _MIN_SINGLE_CHAR_SCORE
    ]
    for word in single_words:
        _apply_word(word, empty_only=True)


def parse_tables(yomitoku_pages: list[dict]) -> list[list[Cell]]:
    """
    ページリストから全テーブルのセルリストを返す。

    Returns:
        list of tables; each table = list of Cells
        (row, col, row_span, col_span, text, bbox)
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
