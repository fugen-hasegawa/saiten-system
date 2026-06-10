"""正解データ生成: 正解PDF → answer_key.json"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .ocr import Cell, run_yomitoku, parse_tables
from .grading import (
    normalize_choice,
    detect_qno_columns as _detect_qno_columns,
    _is_question_number,
)

_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _ROOT / "backend" / "config.json"
_DATA_PATH = _ROOT / "data" / "answer_key.json"


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _detect_answer_offset(cells: list[Cell], valid_choices: list[str]) -> dict | None:
    """
    問番号セルに対して解答セルがある方向を検出する。
    Returns {"d_row": 0, "d_col": 1} 形式、検出できなければ None。
    """
    cell_map = {(c.row, c.col): c for c in cells}
    directions = [
        {"d_row": 0, "d_col": 1},
        {"d_row": 0, "d_col": -1},
        {"d_row": 1, "d_col": 0},
        {"d_row": -1, "d_col": 0},
    ]
    hits: dict[tuple, int] = {}
    for cell in cells:
        if _is_question_number(cell.text) is None:
            continue
        for d in directions:
            neighbor = cell_map.get((cell.row + d["d_row"], cell.col + d["d_col"]))
            if neighbor is None:
                continue
            key = (d["d_row"], d["d_col"])
            hits[key] = hits.get(key, 0) + 1

    if not hits:
        return None
    best = max(hits, key=hits.get)
    return {"d_row": best[0], "d_col": best[1]}


# ─── 出席番号領域の検出 ────────────────────────────────────────────────────

def detect_student_no_region(
    pages: list[dict],
    page_w: int,
    page_h: int,
    digits: int = 2,
) -> dict:
    """
    yomitoku JSON ページリストから出席番号の読取方式テンプレを返す。

    優先度:
    1. words 内に「出席番号」「番号」ラベルがあれば label_cell 方式
    2. なければ bbox 既定値（右下エリア）
    """
    for page in pages:
        for word in page.get("words", []):
            content = word.get("content", "")
            if "出席番号" in content or content.strip() in ("番号", "番"):
                pts = word["points"]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                wx0, wy0, wx1, wy1 = min(xs), min(ys), max(xs), max(ys)
                # ラベルの右隣の bbox を正規化座標で記録
                margin = (wx1 - wx0) * 0.3
                nbx0 = (wx1 + margin) / page_w
                nby0 = wy0 / page_h
                nbx1 = min((wx1 + (wx1 - wx0) * 3) / page_w, 1.0)
                nby1 = wy1 / page_h
                return {
                    "method": "label_cell",
                    "label": content.strip(),
                    "neighbor_offset": {"d_row": 0, "d_col": 1},
                    "bbox_norm": [
                        round(nbx0, 3), round(nby0, 3),
                        round(nbx1, 3), round(nby1, 3),
                    ],
                    "digits": digits,
                }

    # フォールバック: 右下エリアの既定 bbox
    return {
        "method": "bbox",
        "label": None,
        "neighbor_offset": {"d_row": 0, "d_col": 1},
        "bbox_norm": [0.70, 0.84, 0.97, 0.93],
        "digits": digits,
    }


# ─── 正解キー生成 ──────────────────────────────────────────────────────────

def build_answer_key(
    pdf_path: str | Path,
    tmp_dir: str | Path,
    valid_choices: list[str] | None = None,
) -> dict:
    """
    正解PDF を OCR して answer_key dict を生成する。

    Returns:
        answer_key dict（TASKS.md §3.1 スキーマ準拠）
    Raises:
        ValueError: 表が検出できない場合
    """
    cfg = _load_config()
    if valid_choices is None:
        valid_choices = cfg["valid_choices"]
    correction_map = cfg.get("ocr_correction_map", {})
    digits = cfg.get("student_no", {}).get("digits", 2)

    pages = run_yomitoku(pdf_path, tmp_dir, lite=True, device="cpu", combine=True)
    all_tables = parse_tables(pages)

    if not all_tables:
        raise ValueError("OCR で表が検出できませんでした。")

    # ページサイズ推定（最初のテーブルの最大座標から）
    cells_all = all_tables[0]
    page_w = max(c.bbox[2] for c in cells_all) + 50
    page_h = max(c.bbox[3] for c in cells_all) + 200

    # 解答オフセット検出
    answer_offset = _detect_answer_offset(cells_all, valid_choices)
    if answer_offset is None:
        answer_offset = {"d_row": 0, "d_col": 1}

    cell_map = {(c.row, c.col): c for c in cells_all}
    dr, dc = answer_offset["d_row"], answer_offset["d_col"]

    answers: dict[str, str] = {}
    review_flags: dict[str, bool] = {}

    qno_cols = _detect_qno_columns(cells_all)

    for cell in cells_all:
        if cell.col not in qno_cols:
            continue
        qno = _is_question_number(cell.text)
        if qno is None:
            continue
        ans_cell = cell_map.get((cell.row + dr, cell.col + dc))
        if ans_cell is None:
            continue
        raw_text = ans_cell.text
        value, status = normalize_choice(raw_text, valid_choices, correction_map)
        if value:
            answers[str(qno)] = value
            review_flags[str(qno)] = (status != "ok")
        else:
            # 正規化できない値は空欄として review フラグを立てる
            answers[str(qno)] = ""
            review_flags[str(qno)] = True

    num_questions = len(answers)

    # ページサイズヒント（OCR 出力画像サイズ近似）
    page_size_hint = {"w": page_w, "h": page_h}

    student_no_template = detect_student_no_region(pages, page_w, page_h, digits)

    answer_key = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "valid_choices": valid_choices,
        "num_questions": num_questions,
        "template": {
            "page_size_hint": page_size_hint,
            "student_no": student_no_template,
            "answer_table": {
                "qno_is_printed": True,
                "answer_offset": answer_offset,
            },
        },
        "answers": answers,
        "review": review_flags,
    }
    return answer_key


def save_answer_key(answer_key: dict) -> Path:
    """answer_key を data/answer_key.json に保存する。"""
    _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(answer_key, f, ensure_ascii=False, indent=2)
    return _DATA_PATH


def load_answer_key() -> dict | None:
    """保存済みの answer_key を返す。なければ None。"""
    if not _DATA_PATH.exists():
        return None
    with open(_DATA_PATH, encoding="utf-8") as f:
        return json.load(f)
