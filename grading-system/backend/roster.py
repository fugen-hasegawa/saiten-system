"""名簿 CSV の読み込み"""
from __future__ import annotations

import csv
from pathlib import Path

_DATA_PATH = Path(__file__).parent.parent / "data" / "roster.csv"

_CLASS_KEYS = ("組", "くみ", "class", "Class")
_SNO_KEYS   = ("出席番号", "番号", "no", "No", "id", "ID")
_NAME_KEYS  = ("氏名", "名前", "name", "Name")


def _norm(text: str) -> str:
    """先頭ゼロを除いた数値文字列に正規化。例: '01' → '1'"""
    try:
        return str(int(text.strip()))
    except ValueError:
        return text.strip()


def load_roster(csv_path: str | Path | None = None) -> dict:
    """
    CSV から名簿を読み込む。

    対応フォーマット（組列は任意）:
        組,出席番号,氏名
        1,01,山田太郎

        出席番号,氏名       ← 組なし（従来形式）も可
        01,山田太郎

    Returns:
        {
            "has_class": bool,
            "data": {(class_no_str, sno_str): name}
        }
        class_no_str / sno_str は先頭ゼロなしの数値文字列。
        組列がない場合、class_no_str は "" 固定。
    """
    path = Path(csv_path) if csv_path else _DATA_PATH
    if not path.exists():
        return {"has_class": False, "data": {}}

    for enc in ("utf-8-sig", "shift_jis", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []

                class_key = next((k for k in fieldnames if k.strip() in _CLASS_KEYS), None)
                sno_key   = next((k for k in fieldnames if k.strip() in _SNO_KEYS),   None)
                name_key  = next((k for k in fieldnames if k.strip() in _NAME_KEYS),  None)

                if not sno_key or not name_key:
                    continue

                has_class = class_key is not None
                data: dict[tuple[str, str], str] = {}

                for row in reader:
                    sno  = _norm(row[sno_key])
                    name = row[name_key].strip()
                    cls  = _norm(row[class_key]) if has_class else ""
                    if sno:
                        data[(cls, sno)] = name

            return {"has_class": has_class, "data": data}
        except (UnicodeDecodeError, csv.Error):
            continue

    return {"has_class": False, "data": {}}


def roster_get(roster: dict, class_no: str, sno: str) -> str:
    """
    組と出席番号で氏名を検索する。先頭ゼロを無視して数値比較。

    - 組列ありの名簿: (組, 番号) で完全一致優先、なければ番号のみで検索
    - 組列なしの名簿: 番号のみで検索
    """
    if not sno:
        return ""

    data: dict = roster.get("data", {})
    has_class: bool = roster.get("has_class", False)
    sno_n = _norm(sno)

    if has_class and class_no:
        cls_n = _norm(class_no)
        if (cls_n, sno_n) in data:
            return data[(cls_n, sno_n)]

    # 番号のみでフォールバック検索
    for (_, s), name in data.items():
        if s == sno_n:
            return name
    return ""


def save_roster(csv_path: str | Path, content_bytes: bytes) -> Path:
    """アップロードされた CSV バイト列を data/roster.csv に保存する。"""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content_bytes)
    return path
