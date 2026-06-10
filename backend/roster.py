"""名簿 CSV の読み込み"""
from __future__ import annotations

import csv
from pathlib import Path

_DATA_PATH = Path(__file__).parent.parent / "data" / "roster.csv"


def load_roster(csv_path: str | Path | None = None) -> dict[str, str]:
    """
    CSV から {出席番号: 氏名} の dict を生成する。

    CSV 形式:
        出席番号,氏名
        01,山田太郎
        02,佐藤花子

    Returns:
        dict[student_no_str, name] — 出席番号は zfill 正規化なし（そのまま保持）
    """
    path = Path(csv_path) if csv_path else _DATA_PATH
    if not path.exists():
        return {}

    roster: dict[str, str] = {}
    # UTF-8 BOM と Shift-JIS の両方を試みる
    for enc in ("utf-8-sig", "shift_jis", "utf-8"):
        try:
            with open(path, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # ヘッダー候補: 出席番号, 番号, no, id
                    no_key = next(
                        (k for k in row if k.strip() in ("出席番号", "番号", "no", "No", "id", "ID")),
                        None,
                    )
                    name_key = next(
                        (k for k in row if k.strip() in ("氏名", "名前", "name", "Name")),
                        None,
                    )
                    if no_key and name_key:
                        roster[row[no_key].strip()] = row[name_key].strip()
            break
        except (UnicodeDecodeError, csv.Error):
            continue

    return roster


def save_roster(csv_path: str | Path, content_bytes: bytes) -> Path:
    """アップロードされた CSV バイト列を data/roster.csv に保存する。"""
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content_bytes)
    return path
