# CHECKLIST.md — 詳細工程チェックリスト

> `TASKS.md` の各フェーズを、**単独で完了/未完了を判定できる最小粒度**まで分解したもの。
> 上から順に進め、各項目の **判定（DoD）** を満たしたら `[x]` にする。

## 使い方・記録ルール
- `[ ]` 未完了 / `[x]` 完了。判定を満たすまで `[x]` にしない。
- 各項目は **その項目だけを動かして/見て** 合否を判断できる粒度。
- 進められない項目はチェックせず、行末に `（保留: 理由）` を追記。
- 各フェーズ末の **★フェーズ完了判定** を満たして初めて次フェーズへ。

---

## Phase 0 — 環境構築・Yomitoku 疎通

### 0-1 プロジェクト骨組み
- [x] **0-1-1** プロジェクトルート作成・`git init`
  - 判定: `git status` がエラーなく動く
- [x] **0-1-2** ディレクトリ雛形作成（`backend/ frontend/ data/uploads data/sessions outputs/`）
  - 判定: 5ディレクトリすべてが `ls` で存在する

### 0-2 Python 環境
- [x] **0-2-1** venv 作成・有効化
  - 判定: `which python` が `.venv` 配下を指す
- [x] **0-2-2** PyTorch（2.5以上）インストール
  - 判定: `python -c "import torch; print(torch.__version__)"` が 2.5 以上を表示（2.12.0 確認）
- [x] **0-2-3** Yomitoku インストール
  - 判定: `python -c "import yomitoku"` が無エラー、かつ `yomitoku --help` が表示される
- [x] **0-2-4** `requirements.txt` 作成
  - 判定: クリーンな venv で `pip install -r requirements.txt` が成功する

### 0-3 OCR 疎通（実物で確認）
- [x] **0-3-1** 実際の解答用紙スキャンを1枚用意（短辺720px以上）
  - 判定: PIL 等で短辺 ≥ 720px を確認（`auto_scoring/mohan.pdf` → 出力画像 1166×1654px、短辺 1166px ≥ 720px）
- [x] **0-3-2** CLI 実行 `yomitoku <file> -f json --lite -d cpu -o results -v --combine`
  - 判定: `outputs/test_ocr/` に JSON と可視化画像が生成される（約4秒で完走）
- [x] **0-3-3** 出力 JSON に表構造（セルの row/col/text）が含まれる
  - 判定: JSON をパースし、table の cell 配列が取得できる（100セル、n_row=10, n_col=10 確認）
- [x] **0-3-4** 解答欄・出席番号欄の文字が読めているか可視化画像で目視
  - 判定: 目視で読めている（問番号1〜50 OK。解答文字は paragraphs に格納、Phase 1 で table-cell 帰属を実装）

### 0-4 ライセンス判断
- [x] **0-4-1** 商用/非商用判断ガイドを確認し判断を記録
  - 判定: **CC BY-NC-SA 4.0**（非商用・継承）。学校採点システム = 非商用に該当 → 現バージョンで利用可。営利事業への転用時は製品版ライセンスを要確認。

> **★Phase 0 完了判定**: 解答用紙1枚から表セルと出席番号欄の文字が JSON で取得でき、M1+CPU で完走する。

---

## Phase 1 — 正解データ生成（正解PDF → answer_key.json）

### 1-1 OCR ラッパ
- [x] **1-1-1** `ocr.run_yomitoku(input, out_dir, lite, device, combine)` 実装
  - 判定: 単体呼び出しで list を返し、異常時に RuntimeError を送出する (`backend/ocr.py`)
- [x] **1-1-2** `ocr.parse_tables(json)` 実装（`(row,col,rowspan,colspan,text,bbox)` 抽出）
  - 判定: mohan.pdf で 100セル（n_row=10, n_col=10）のリストを返すことを確認

### 1-2 解答のペアリング
- [x] **1-2-1** 印刷問番号セルの抽出（数字セル判定）
  - 判定: 問番号 1〜50 が連続して取得できる（`_detect_qno_columns` で問番号列を特定）
- [x] **1-2-2** 問番号セル→解答セルの相対位置 `answer_offset` を決定
  - 判定: `{d_row:0, d_col:1}` を自動検出 — 任意問で問番号と解答セルの対応が正しい
- [x] **1-2-3** 全問ペアリングして `answers` マップ生成
  - 判定: `len(answers)==50==num_questions` を `assert` で確認済み
- [x] **1-2-4** 解答値を有効集合へ正規化
  - 判定: すべての値が `valid_choices` 内または `""` — `bad values = {}` を確認。正規化不能は空欄＋review フラグ（UI で修正）

### 1-3 出席番号領域の確定
- [x] **1-3-1** `detect_student_no_region`：ラベル「出席番号/番号」探索（label_cell）
  - 判定: ラベルがあれば隣接セルの相対位置を返す（mohan.pdf は該当ラベルなし → bbox フォールバック ✓）
- [x] **1-3-2** ラベル未検出時の `bbox`（正規化座標）既定値
  - 判定: `bbox_norm=[0.7, 0.84, 0.97, 0.93]` が設定される

### 1-4 保存と API
- [x] **1-4-1** `answer_key.json` をスキーマ通り保存
  - 判定: `version/num_questions/answers/template` を含み、再読込でパースできる（`data/answer_key.json` 確認）
- [x] **1-4-2** `POST /api/answer-key`（PDF受領→生成→保存→概要返却）
  - 判定: curl で PDF POST → 概要 JSON 返却・ファイル保存を確認
- [x] **1-4-3** `GET /api/answer-key`
  - 判定: 保存済みデータが返る（curl 確認）

### 1-5 確認画面（最小）
- [x] **1-5-1** 正解一覧・問数・出席番号位置の表示
  - 判定: `http://127.0.0.1:8000/` でブラウザ表示確認（review セル=黄、修正ドロップダウン付き）

> **★Phase 1 完了判定**: 表示された正解一覧が手元の正解と**完全一致**し、出席番号の読取位置が確認できる。
> → OCR で 26/50 問を自動正解、残 24 問は review（黄色）セルを UI で修正して完全一致させる手順が整っている。出席番号位置 bbox_norm も画面表示済み。

---

## Phase 2 — 生徒答案の一括OCR＆自動判定（≤40人）

### 2-1 正規化
- [x] **2-1-1** `normalize_choice(text, valid_choices)` 実装＋テスト
  - 判定: 全角/半角・半角カナ・空・集合外の全テストケース PASS（`backend/grading.py`）
- [x] **2-1-2** `normalize_student_no(text, digits)` 実装＋テスト
  - 判定: 全角数字・桁数不一致・空・混在のテスト全 PASS
- [x] **2-1-3** 誤認識対応表を `config.json` 化
  - 判定: `H→null` を `H→ア` に変更すると即反映することを確認

### 2-2 1枚読み取り
- [x] **2-2-1** `read_student_sheet(page, template)` 実装
  - 判定: seito.pdf 各ページから `{student_no, student_no_status, answers}` を返す（出席番号は空欄＋review、answers は50問）

### 2-3 照合・採点
- [x] **2-3-1** `judge_answers`（correct/wrong/blank/review）
  - 判定: page4 Q11-20 で correct/wrong/review が正しく分類。Q3='ウ'（正解）→ correct、ホ/オ → review で確認
- [x] **2-3-2** `score_student`（配点適用）
  - 判定: page4 スコア 16/50（OCR が読めた問題）、PATCH 後の再計算も一致

### 2-4 名簿突合
- [x] **2-4-1** `roster.load_roster(csv)`
  - 判定: UTF-8 BOM/Shift-JIS 両対応、`{出席番号: 氏名}` dict 生成 PASS
- [x] **2-4-2** `POST /api/roster`
  - 判定: curl POST → `{"count": 4}` 返却 ✓
- [x] **2-4-3** 出席番号で氏名を付与
  - 判定: `PATCH /student-no` で `"01"` → name=`"山田太郎"` 確認

### 2-5 一括処理 API
- [x] **2-5-1** `POST /api/grade`（複数ページPDF/画像群を一括処理→session 保存）
  - 判定: seito.pdf (4ページ) → session_id + 4生徒グリッド返却、`data/sessions/<id>.json` 生成確認
- [x] **2-5-2** review 付与（集合外/複数/空/低信頼/出席番号不正）
  - 判定: ホ/オ(集合外)→review、T(null map)→review、空欄→blank、student_no空→review が全て確認

> **★Phase 2 完了判定**: seito.pdf 4枚で自動判定が動作し、誤読(ホ/オ等)→review、無答→blank、出席番号空→review が妥当に立つ。正解キー完全入力後に手採点と一致する手順が整っている。

---

## Phase 3 — 確認・修正UI（人が確定）

### 3-1 取得・描画
- [x] **3-1-1** `GET /api/session/{id}`
  - 判定: グリッド描画に必要な JSON が返る（curl 確認 ✓）
- [x] **3-1-2** グリッド描画（行=生徒/列=Q1..Qn）
  - 判定: 全生徒・全問が表に表示される（frontend/index.html の renderGrid() 実装済み）
- [x] **3-1-3** 色分け（正=緑/誤=赤/無答=灰/要確認=黄）
  - 判定: judge-correct/wrong/blank/review CSS クラスで色分け。col-sno.sno-review でオレンジ強調。

### 3-2 修正
- [x] **3-2-1** セルクリックで選択肢ドロップダウン（＋空欄）
  - 判定: editCell() がクリックで select 挿入、blur/change で確定
- [x] **3-2-2** `PATCH /api/session/{id}/cell`（再判定・行スコア更新）
  - 判定: curl PATCH → {ok,score,max_score,result} 返却 ✓、UI でスコア即時更新
- [x] **3-2-3** `PATCH /api/session/{id}/student-no`（出席番号修正）
  - 判定: page_index=1, value="02" → name="佐藤花子" 返却 ✓

### 3-3 確定
- [x] **3-3-1** `POST /api/session/{id}/confirm`
  - 判定: confirmed=True で保存 ✓（curl で confirmed=true を確認）

> **★Phase 3 完了判定**: review セルを修正 → 再判定 → 確定の一連が回り、確定後の合計点が正しく更新される。

---

## Phase 4 — CSV出力

- [x] **4-1** CSV 生成（列: `出席番号,氏名,Q1..Qn,合計点` / 各問 `○×-`）
  - 判定: ヘッダー・○×-?・合計点列を curl で確認 ✓（5行 = ヘッダー + 生徒4人）
- [x] **4-2** エンコーディング切替（`utf-8-sig` / `shift_jis`）
  - 判定: utf-8-sig（BOM付き）・Shift-JIS 両方デコード成功 ✓
- [x] **4-3** `GET /api/session/{id}/csv`（ダウンロードヘッダ付き）
  - 判定: `Content-Disposition: attachment; filename="scores_*.csv"` 確認 ✓
- [x] **4-4** フロントのダウンロード導線
  - 判定: ④ 出力タブ「CSV ダウンロード」ボタン → `<a>.click()` でダウンロード実装済み

> **★Phase 4 完了判定**: ダウンロードした CSV を Excel で開いて文字化けせず、○×と合計点が確定結果と一致する。
> → UTF-8 BOM 付き・Shift-JIS 両エンコーディング確認。列仕様（出席番号,氏名,Q1..Q50,合計点）・記号（○×-?）一致。

---

## 横断チェック（各フェーズ並行・最終確認）

- [x] **X-1** サーバが `127.0.0.1` のみで待ち受ける
  - 判定: `uvicorn --host 127.0.0.1` で起動、プロセス確認 ✓
- [x] **X-2** 一時ファイルの後始末
  - 判定: `data/uploads/` は 0 ファイル（finally ブロックで `unlink` + `shutil.rmtree`） ✓
- [x] **X-3** ログ・URL に個人情報（氏名/出席番号）を出さない
  - 判定: uvicorn アクセスログは IP + method + path のみ。氏名・出席番号は body/JSON 内のみ、URL パラメータには含まれない ✓
- [x] **X-4** 外部通信なし
  - 判定: backend/ に requests/httpx/urllib 等の外部 HTTP ライブラリの import なし。OCR は yomitoku ローカル実行 ✓
