"""採点コアロジック: 正規化・読取・照合・得点計算"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from .ocr import Cell, parse_tables


# ─── 設定読み込み ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.json"
    import json
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


# ─── 選択肢の正規化 ────────────────────────────────────────────────────────────

def normalize_choice(
    text: str,
    valid_choices: list[str],
    correction_map: dict | None = None,
) -> tuple[str | None, str]:
    """
    OCR テキストを有効選択肢へ正規化する。

    Returns:
        (value, status)  status ∈ "ok" | "blank" | "out_of_set" | "review"
    """
    if correction_map is None:
        correction_map = _load_config().get("ocr_correction_map", {})

    if not text or not text.strip():
        return None, "blank"

    t = unicodedata.normalize("NFKC", text.strip())

    if t in correction_map:
        mapped = correction_map[t]
        if mapped is None:
            return None, "review"
        t = mapped

    if t in valid_choices:
        return t, "ok"

    # 空白除去後に再チェック
    t2 = t.replace(" ", "").replace("　", "")
    if t2 in valid_choices:
        return t2, "ok"

    # 日本語文字（ひらがな・カタカナ・漢字）を含まない＝英数字の誤読
    if t2 and not any('぀' <= c <= 'ヿ' or '一' <= c <= '鿿' for c in t2):
        return None, "review"

    return None, "out_of_set"


# ─── 出席番号の正規化 ──────────────────────────────────────────────────────────

def normalize_student_no(text: str, digits: int = 2) -> tuple[str, str]:
    """
    出席番号テキストを正規化する。

    Returns:
        (value, status)  status ∈ "ok" | "review"
    """
    if not text or not text.strip():
        return "", "review"

    # 全角数字 → 半角、数字以外を除去
    t = unicodedata.normalize("NFKC", text.strip())
    t = re.sub(r"\D", "", t)

    if not t:
        return "", "review"

    # 先頭ゼロ除去後に桁数チェック
    stripped = t.lstrip("0") or "0"
    if len(stripped) > digits:
        return "", "review"

    # 0埋めで digits 桁に統一
    normalized = stripped.zfill(digits)
    if len(normalized) != digits:
        return "", "review"

    return normalized, "ok"


# ─── 問番号判定ユーティリティ ─────────────────────────────────────────────────

def _is_question_number(text: str) -> int | None:
    """純粋な整数テキストなら int、違えば None。"""
    m = re.fullmatch(r"\d+", text.strip())
    return int(m.group()) if m else None


def detect_qno_columns(cells: list[Cell]) -> set[int]:
    """
    問番号（連続整数 1〜N）が3個以上含まれる列を返す。
    """
    col_ints: dict[int, list[int]] = defaultdict(list)
    for c in cells:
        qno = _is_question_number(c.text)
        if qno is not None:
            col_ints[c.col].append(qno)

    return {col for col, nums in col_ints.items() if len(nums) >= 3}


# ─── 出席番号の読取 ─────────────────────────────────────────────────────────────

def _estimate_page_size(page_data: dict) -> tuple[int, int]:
    """words と table cells の最大座標からページサイズを推定する。"""
    all_xs, all_ys = [], []
    for w in page_data.get("words", []):
        for p in w["points"]:
            all_xs.append(p[0])
            all_ys.append(p[1])
    for t in page_data.get("tables", []):
        for c in t.get("cells", []):
            b = c["box"]
            all_xs.extend([b[0], b[2]])
            all_ys.extend([b[1], b[3]])
    return (max(all_xs) + 50 if all_xs else 1200,
            max(all_ys) + 80 if all_ys else 1600)


def _read_student_no_bbox(
    page_data: dict,
    bbox_norm: list[float],
    digits: int,
) -> tuple[str, str]:
    """bbox_norm 領域内の数字ワードから出席番号を読む。"""
    pw, ph = _estimate_page_size(page_data)
    rx0, ry0, rx1, ry1 = bbox_norm
    px0, py0, px1, py1 = rx0 * pw, ry0 * ph, rx1 * pw, ry1 * ph

    candidates = []
    for word in page_data.get("words", []):
        pts = word["points"]
        wx0 = min(p[0] for p in pts)
        wy0 = min(p[1] for p in pts)
        wx1 = max(p[0] for p in pts)
        wy1 = max(p[1] for p in pts)
        # bbox 内に中心点があるか
        cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
        if px0 <= cx <= px1 and py0 <= cy <= py1:
            content = word["content"].strip()
            if re.search(r"\d", content):
                candidates.append(word)

    if not candidates:
        return "", "review"

    candidates.sort(key=lambda w: w.get("rec_score", 0), reverse=True)
    raw = candidates[0]["content"]
    return normalize_student_no(raw, digits)


def _read_student_no_label(
    page_data: dict,
    label: str,
    digits: int,
) -> tuple[str, str]:
    """ラベルワードを探し、右隣の数字ワードを出席番号として返す。"""
    for word in page_data.get("words", []):
        if label not in word.get("content", ""):
            continue
        pts = word["points"]
        wx1 = max(p[0] for p in pts)
        wy0 = min(p[1] for p in pts)
        wy1 = max(p[1] for p in pts)
        wy_mid = (wy0 + wy1) / 2

        neighbors = []
        for w2 in page_data.get("words", []):
            if w2 is word:
                continue
            p2 = w2["points"]
            w2x0 = min(p[0] for p in p2)
            w2y0 = min(p[1] for p in p2)
            w2y1 = max(p[1] for p in p2)
            w2_mid = (w2y0 + w2y1) / 2
            # 右にあって y 方向が近い
            if w2x0 > wx1 and abs(w2_mid - wy_mid) < (wy1 - wy0):
                content = w2["content"].strip()
                if re.search(r"\d", content):
                    neighbors.append(w2)

        if neighbors:
            neighbors.sort(key=lambda w: min(p[0] for p in w["points"]))
            raw = neighbors[0]["content"]
            return normalize_student_no(raw, digits)

    return "", "review"


def read_student_no(page_data: dict, sno_template: dict) -> tuple[str, str]:
    """テンプレートに従って出席番号を読む。"""
    method = sno_template.get("method", "bbox")
    digits = sno_template.get("digits", 2)

    if method == "label_cell":
        label = sno_template.get("label", "出席番号")
        val, status = _read_student_no_label(page_data, label, digits)
        if status == "ok":
            return val, status
        # label 検出失敗 → bbox fallback

    bbox_norm = sno_template.get("bbox_norm", [0.85, 0.87, 1.0, 0.97])
    return _read_student_no_bbox(page_data, bbox_norm, digits)


# ─── 1ページ読取 ───────────────────────────────────────────────────────────────

def read_student_sheet(
    page_data: dict,
    template: dict,
    valid_choices: list[str],
    correction_map: dict,
) -> dict:
    """
    OCR 1ページから生徒解答データを抽出する。

    Returns:
        {
            "student_no": str,
            "student_no_status": "ok" | "review",
            "answers": {q_str: raw_ocr_text}
        }
    """
    # テーブル解析
    tables = parse_tables([page_data])
    answers: dict[str, str] = {}

    if tables:
        cells = tables[0]
        cell_map = {(c.row, c.col): c for c in cells}
        qno_cols = detect_qno_columns(cells)

        at = template.get("answer_table", {})
        dr = at.get("answer_offset", {}).get("d_row", 0)
        dc = at.get("answer_offset", {}).get("d_col", 1)

        for cell in cells:
            if cell.col not in qno_cols:
                continue
            qno = _is_question_number(cell.text)
            if qno is None:
                continue
            ans_cell = cell_map.get((cell.row + dr, cell.col + dc))
            if ans_cell:
                answers[str(qno)] = ans_cell.text

    # 出席番号読取
    sno_template = template.get("student_no", {})
    student_no, sno_status = read_student_no(page_data, sno_template)

    return {
        "student_no": student_no,
        "student_no_status": sno_status,
        "answers": answers,
    }


# ─── 照合・採点 ────────────────────────────────────────────────────────────────

def judge_answers(
    read_answers: dict[str, str],
    key_answers: dict[str, str],
    valid_choices: list[str],
    correction_map: dict,
) -> dict[str, dict]:
    """
    生徒の読取答案と正解を比較する。

    Returns:
        {q: {"read": str, "judge": str, "review": bool}}
        judge ∈ "correct" | "wrong" | "blank" | "review"
    """
    results: dict[str, dict] = {}
    for q, correct in key_answers.items():
        raw = read_answers.get(q, "")
        normalized, status = normalize_choice(raw, valid_choices, correction_map)

        def _has_japanese(s: str) -> bool:
            return any('぀' <= c <= 'ヿ' or '一' <= c <= '鿿' for c in s)

        if status == "blank":
            judge = "blank"
            review = False
            display = ""
        elif status in ("review", "out_of_set"):
            judge = "review"
            review = True
            # 英数字の誤読は表示しない（オレンジ空セルで要確認を示す）
            display = normalized or (raw if _has_japanese(raw) else "") or ""
        elif not correct:
            # 正解が未設定（review中の正解キー）
            judge = "review"
            review = True
            display = normalized or ""
        elif normalized == correct:
            judge = "correct"
            review = False
            display = normalized
        else:
            judge = "wrong"
            review = False
            display = normalized or ""

        results[q] = {"read": display, "judge": judge, "review": review}

    return results


def score_student(results: dict[str, dict], points: dict) -> tuple[int, int]:
    """
    配点を適用してスコアを計算する。

    Returns:
        (score, max_score)
    """
    default_pt = points.get("default", 1)
    overrides = points.get("overrides", {})
    score = 0
    max_score = 0
    for q, r in results.items():
        pt = int(overrides.get(str(q), default_pt))
        max_score += pt
        if r["judge"] == "correct":
            score += pt
    return score, max_score
