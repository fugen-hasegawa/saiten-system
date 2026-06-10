# TASKS.md — 実装作業手順書

> 本書は `CLAUDE.md` を**実装レベルに詳細化した作業手順書**。Claude Code はフェーズ順に進め、各タスクの「完了条件」を満たしてから次へ進むこと。
> `CLAUDE.md` と齟齬がある場合は、本書のフェーズ手順を優先しつつ `CLAUDE.md` 側も更新する。
>
> **確定事項（CLAUDE.md からの更新）**: 出席番号は **解答用紙の固定位置を OCR で読み取る**（ページ順による識別は最終フォールバックのみ）。

---

## 0. 進め方のルール

- 1タスク = 1まとまり。**完了条件を満たすまで次に進まない**。各フェーズ末で一度動作を見せて確認する。
- まず**動くもの**を作る。OCR は Phase 0〜2 では **CLI 実行 → JSON パース**で確実に。速度最適化（Python API 化）は後回し。
- すべてローカル完結。`127.0.0.1` のみ bind。答案・氏名・点数を外部送信・外部ログしない。
- 不明点は推測で埋めず、公式ドキュメント（`https://kotaro-kinoshita.github.io/yomitoku/`）を確認する。

---

## 1. 共通の前提・ルール

| 項目 | 値 |
|---|---|
| Python | 3.10〜3.13 |
| OCR | Yomitoku（`--lite -d cpu`、表構造認識を使用） |
| Backend | FastAPI + uvicorn |
| Frontend | HTML + 素の JavaScript（fetch、ビルド不要） |
| 保存 | ローカル JSON ファイル（`data/`配下） |
| 有効選択肢 | 既定 `["ア","イ","ウ","エ"]`（設定で増減可） |
| 配点 | 既定 全問1点 |
| CSV | 既定 UTF-8(BOM)、Shift-JIS 切替可 |
| 入力 | 複数ページPDF（1人1ページ）または画像フォルダ。短辺720px以上 / 300dpi目安 |

---

## 2. 解答用紙レイアウトとOCRの考え方

### 2.1 基本方針
解答用紙は**表形式**。Yomitoku の表構造認識が返す各セルの `(row, col, rowspan, colspan, text, bbox)` を使い、
**印刷された問番号セル**と**その解答セル**を対応付ける。

- 問番号は印刷済み（活字）→ OCRで確実に読める。これを基準に「問番号セル → 解答セル」を相対位置でペアリングする。
- この方式なら、1〜25 / 26〜50 … のように**複数ブロックが横並び**でも、各ブロックの問番号を読んで解答とペアにできるため、座標を決め打ちしなくてよい。
- ペアの相対位置（例: 「解答セル＝問番号セルの右隣」）を **テンプレ** `answer_table.answer_offset` に保存し、生徒用紙も同じルールで読む。

### 2.2 出席番号の読み取り（固定位置OCR）
解答用紙の決まった場所から出席番号を OCR で読む。テンプレ `student_no` に方式を保存する。

- **method = `label_cell`（優先）**: 表内に「出席番号」または「番号」というラベルセルがあれば、その隣接セル（相対位置はテンプレに保存）を出席番号として読む。
- **method = `bbox`（汎用フォールバック）**: 用紙上の固定矩形を**正規化座標** `[x0,y0,x1,y1]`（0〜1）で指定し、その領域を切り出して OCR する。
- 読み取り後の正規化: 全角→半角、数字以外を除去。`digits`（桁数）と不一致・空・複数候補は **review** フラグ。
- Phase 1 で正解用紙を読み込んだ際に、`label_cell` 自動検出を試み、失敗時は `bbox` を画面で確認・調整して確定する。

### 2.3 選択肢の正規化（採点の肝）
OCR結果は必ず有効集合へスナップしてから比較する。

- 全角/半角・半角カナ（ｱ等）・前後空白を正規化。
- 誤認識対応表（例 `了→ア` 等）を**設定ファイル化**し実データで調整。
- 空欄 → `blank`（無答 `-`）。集合外/複数検出/低信頼 → `review`（要確認）。

---

## 3. データ構造（JSONスキーマ）

### 3.1 `data/answer_key.json`
```json
{
  "version": 1,
  "created_at": "ISO8601",
  "valid_choices": ["ア", "イ", "ウ", "エ"],
  "num_questions": 100,
  "template": {
    "page_size_hint": { "w": 2480, "h": 3508 },
    "student_no": {
      "method": "label_cell",
      "label": "出席番号",
      "neighbor_offset": { "d_row": 0, "d_col": 1 },
      "bbox_norm": [0.70, 0.03, 0.95, 0.07],
      "digits": 2
    },
    "answer_table": {
      "qno_is_printed": true,
      "answer_offset": { "d_row": 0, "d_col": 1 }
    }
  },
  "answers": { "1": "ウ", "2": "ア", "3": "エ" }
}
```

### 3.2 `data/sessions/<session_id>.json`
```json
{
  "session_id": "uuid",
  "created_at": "ISO8601",
  "confirmed": false,
  "points": { "default": 1, "overrides": { "5": 2 } },
  "students": [
    {
      "page_index": 0,
      "student_no": "12",
      "student_no_status": "ok",
      "name": "（名簿CSV突合・無ければ空）",
      "results": {
        "1": { "read": "ウ", "judge": "correct", "review": false },
        "2": { "read": "?",  "judge": "review",  "review": true  }
      },
      "score": 87,
      "max_score": 100
    }
  ]
}
```

- `judge` ∈ `correct | wrong | blank | review`
- `student_no_status` ∈ `ok | review`

### 3.3 名簿 `data/roster.csv`（任意）
```
出席番号,氏名
1,山田太郎
2,佐藤花子
```

---

## 4. API エンドポイント仕様（FastAPI）

| Method | Path | 役割 | 主な入出力 |
|---|---|---|---|
| GET | `/` | フロント配信 | `index.html` |
| POST | `/api/answer-key` | 正解PDFから正解＋テンプレ生成・保存 | in: PDF / out: 概要(問数, 正解プレビュー, student_no検出結果) |
| GET | `/api/answer-key` | 現在の正解データ取得 | out: `answer_key.json` |
| POST | `/api/roster` | 名簿CSV取り込み | in: CSV / out: 件数 |
| POST | `/api/grade` | 生徒答案を一括OCR＆判定しセッション生成 | in: PDF/画像群 / out: `session_id` + グリッドデータ |
| GET | `/api/session/{id}` | セッション取得（グリッド描画用） | out: session JSON |
| PATCH | `/api/session/{id}/cell` | セル1つ修正→再判定 | in: `{student_no, q, value}` / out: 更新後の行スコア |
| PATCH | `/api/session/{id}/student-no` | 出席番号の修正 | in: `{page_index, value}` |
| POST | `/api/session/{id}/confirm` | 確定 | out: ok |
| GET | `/api/session/{id}/csv?encoding=utf-8-sig\|shift_jis` | CSV ダウンロード | out: CSV |

- すべて `127.0.0.1` 前提。CORS は不要（同一オリジン配信）。

---

## 5. モジュール／関数の責務

### `backend/ocr.py`
- `run_yomitoku(input_path, out_dir, lite=True, device="cpu", combine=True) -> dict | list`
  CLI を subprocess 実行（`-f json`）し、出力 JSON を読み込んで返す。複数ページは `--combine`。
- `parse_tables(yomitoku_json) -> list[Table]`
  セル一覧 `(row, col, rowspan, colspan, text, bbox)` を抽出。
- `crop_region(page_image, bbox_norm) -> image` / `ocr_region(image) -> str`
  `bbox` 方式の出席番号用。

### `backend/answer_key.py`
- `build_answer_key(pdf_path, valid_choices, student_no_cfg) -> dict`
  OCR → 表抽出 → 印刷問番号と解答セルをペアリング → 正規化して `answers` 生成。`num_questions`・`template` も確定。
- `detect_student_no_region(tables, page_image) -> student_no_template`
  ラベル「出席番号/番号」を探索（`label_cell`）。無ければ `bbox` 既定値を返し、UI確認に回す。

### `backend/grading.py`
- `normalize_choice(text, valid_choices) -> (value|None, status)`  status ∈ `ok|blank|out_of_set|multiple|low_conf`
- `normalize_student_no(text, digits) -> (value|None, ok|review)`
- `read_student_sheet(page, template) -> {student_no, student_no_status, answers:{q:read}}`
- `judge_answers(read_answers, key_answers) -> {q: {read, judge, review}}`
- `score_student(results, points) -> (score, max_score)`

### `backend/roster.py`
- `load_roster(csv_path) -> dict[str,str]`（出席番号→氏名）

### `backend/main.py`
- FastAPI アプリ、静的配信、上記エンドポイント、`data/` への入出力。

---

## 6. フロントエンド仕様（`frontend/`）

4ステップを画面上部のタブで切替（素のJS）。

1. **正解登録**: 正解PDFをアップロード → `POST /api/answer-key` → 解析結果（問数・正解一覧・検出した出席番号位置）を表で表示し目視確認。出席番号領域が不正なら座標を調整して再実行。
2. **採点**: 生徒答案（PDF/画像群）＋任意で名簿CSVをアップロード → `POST /api/grade` → セッション生成。
3. **確認・修正（中核）**: 採点グリッド表示。
   - 行＝生徒（出席番号・氏名）、列＝Q1…Qn。
   - セル色分け: 正=緑 / 誤=赤 / 無答=灰 / 要確認=黄。出席番号も `review` なら強調。
   - セルクリックで有効選択肢のドロップダウン（＋空欄）で修正 → `PATCH .../cell` → 行スコア即時更新。
   - 出席番号セルは `PATCH .../student-no` で修正。
   - 「確定」ボタンで `POST .../confirm`。
4. **出力**: エンコーディング選択（UTF-8(BOM)/Shift-JIS）→ `GET .../csv` でダウンロード。

CSV 列: `出席番号, 氏名, Q1, …, Qn, 合計点`（各問 `○/×/-`）。

---

## 7. フェーズ別 作業チェックリスト

### Phase 0 — 環境構築・Yomitoku 疎通
- [ ] venv 作成、`pip install yomitoku`、PyTorch（2.5+）導入。`requirements.txt` 作成。
- [ ] ディレクトリ雛形作成（`backend/ frontend/ data/{uploads,sessions} outputs/`）。
- [ ] 実際の解答用紙スキャン1枚を `yomitoku <file> -f json --lite -d cpu -o results -v --combine` で解析。
- [ ] 出力 JSON に**表構造（セルのrow/col/text）**が含まれることを確認。
- [ ] ライセンス区分（非商用該当か）の判断を済ませる（`CLAUDE.md` §2）。
- **完了条件**: 解答用紙1枚から表セルのテキストと座標が JSON で取得でき、M1+CPU で完走する。

### Phase 1 — 正解データ生成（正解PDF → `answer_key.json`）
- [ ] `ocr.py`: `run_yomitoku` / `parse_tables` 実装。
- [ ] `answer_key.py`: 印刷問番号セル↔解答セルのペアリング → `answers` 生成。
- [ ] 出席番号領域の検出（`label_cell` 自動 → 失敗時 `bbox` 既定）と `template` 保存。
- [ ] `POST /api/answer-key` と確認画面（問数・正解一覧・出席番号位置の表示）。
- **完了条件**: 出力された正解一覧が**問数・内容とも想定通り**で目視一致。出席番号の読取位置が画面で確認できる。

### Phase 2 — 生徒答案の一括OCR＆自動判定（≤40人）
- [ ] `grading.py`: `normalize_choice` / `normalize_student_no` / `read_student_sheet` / `judge_answers` / `score_student`。
- [ ] 誤認識対応表を設定ファイル化（`backend/config.json` 等）。
- [ ] `roster.py` と `POST /api/roster`、出席番号で氏名突合。
- [ ] `POST /api/grade`: 複数ページPDF/画像群を一括処理 → セッション JSON 保存。
- [ ] 集合外・複数・空・低信頼・出席番号不正に `review` を付与。
- **完了条件**: 数人分で**自動判定が手採点と一致**。誤認識・無答・出席番号エラーに `review` が立つ。

### Phase 3 — 確認・修正UI（人が確定）
- [ ] `GET /api/session/{id}` でグリッドデータ取得。
- [ ] グリッド描画（色分け）、セル修正ドロップダウン、出席番号修正。
- [ ] `PATCH .../cell` / `PATCH .../student-no`（再判定・行スコア更新）。
- [ ] `POST .../confirm`。
- **完了条件**: 要確認セルを修正→再判定→確定の一連が回り、確定後に合計点が正しく更新される。

### Phase 4 — CSV出力
- [ ] `GET /api/session/{id}/csv`（UTF-8(BOM)/Shift-JIS 切替）。
- [ ] フロントのダウンロード導線。
- **完了条件**: ダウンロードCSVを **Excel で文字化けせず**開け、○×と合計点が確定結果と一致。

---

## 8. 動作確認・テスト

- 単体: `normalize_choice`（全角半角・半角カナ・空・複数・集合外）のテーブルテスト。`normalize_student_no`（桁数・全角数字）。
- 結合: 正解用紙1枚＋生徒3〜5枚で「正解登録→採点→修正→CSV」を通す。
- 既知の答えと突合した**正答率の手検算**を1回行う。
- スキャン品質（300dpi / 短辺720px）を満たさない画像を入れて `review` 多発を確認（早期に画質要件を運用へ反映）。

---

## 9. つまずきやすい点とフォールバック

- **表のセル数が想定問数と合わない**（罫線が薄い・かすれ）→ 警告を出して人手確認に回す。再スキャン or `bbox` 方式併用。
- **問番号ブロックが横並び**で読み順が乱れる → 問番号セルの**印刷テキスト値**を真とし、座標順ではなく番号でペアリング。
- **出席番号の誤読** → 桁数チェックで `review`。名簿CSVがあれば存在しない番号を警告。最終フォールバックとしてページ順での仮割当（要修正フラグ付き）。
- **OCRが遅い** → まず `--lite -d cpu` で運用。改善が必要なら Python API 化（`DocumentAnalyzer` を起動時1回だけ初期化して使い回す。正確な呼び出しは公式docsで確認）。
- **個人情報** → 一時ファイルは処理後に掃除。ログ・URLに氏名/番号を出さない。
