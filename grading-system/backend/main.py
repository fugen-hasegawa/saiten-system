"""FastAPI バックエンド (Phase 1〜4)"""
from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .answer_key import build_answer_key, load_answer_key, save_answer_key
from .grading import (
    judge_answers,
    normalize_choice,
    read_student_sheet,
    score_student,
)
from .roster import _DATA_PATH as _ROSTER_PATH, load_roster, save_roster

_ROOT = Path(__file__).parent.parent
_FRONTEND = _ROOT / "frontend"
_UPLOADS = _ROOT / "data" / "uploads"
_SESSIONS = _ROOT / "data" / "sessions"
_UPLOADS.mkdir(parents=True, exist_ok=True)
_SESSIONS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="自動採点システム")

app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")


@app.get("/")
def index():
    return FileResponse(str(_FRONTEND / "index.html"))


# ─── 正解データ ─────────────────────────────────────────────────────────────

@app.post("/api/answer-key")
async def post_answer_key(file: UploadFile = File(...)):
    suffix = Path(file.filename or "upload").suffix or ".pdf"
    tmp_pdf = _UPLOADS / f"{uuid.uuid4().hex}{suffix}"
    tmp_out = Path(tempfile.mkdtemp(prefix="yomi_"))

    try:
        tmp_pdf.write_bytes(await file.read())
        answer_key = build_answer_key(tmp_pdf, tmp_out)
        save_answer_key(answer_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(tmp_out, ignore_errors=True)
        tmp_pdf.unlink(missing_ok=True)

    review_count = sum(1 for v in answer_key.get("review", {}).values() if v)
    return JSONResponse({
        "num_questions": answer_key["num_questions"],
        "answers_preview": answer_key["answers"],
        "review_count": review_count,
        "student_no": answer_key["template"]["student_no"],
        "answer_offset": answer_key["template"]["answer_table"]["answer_offset"],
    })


@app.get("/api/answer-key")
def get_answer_key():
    data = load_answer_key()
    if data is None:
        raise HTTPException(status_code=404, detail="正解データが未登録です。")
    return JSONResponse(data)


@app.get("/api/answer-key/image")
def get_answer_key_image():
    img_path = _ROOT / "data" / "answer_key_page.jpg"
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="プレビュー画像がありません。正解 PDF を再登録してください。")
    return FileResponse(str(img_path), media_type="image/jpeg")


@app.patch("/api/answer-key/cell")
async def patch_answer_key_cell(body: dict):
    data = load_answer_key()
    if data is None:
        raise HTTPException(status_code=404, detail="正解データが未登録です。")
    q = str(body.get("q", ""))
    value = body.get("value", "")
    if q not in data["answers"]:
        raise HTTPException(status_code=400, detail=f"問{q}は存在しません。")
    data["answers"][q] = value
    valid = data.get("valid_choices", ["ア", "イ", "ウ", "エ"])
    data.setdefault("review", {})[q] = value not in valid
    save_answer_key(data)
    return {"ok": True, "q": q, "value": value}


# ─── 名簿 ────────────────────────────────────────────────────────────────────

@app.post("/api/roster")
async def post_roster(file: UploadFile = File(...)):
    """名簿 CSV を取り込む。"""
    content = await file.read()
    try:
        save_roster(_ROSTER_PATH, content)
        roster = load_roster(_ROSTER_PATH)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"count": len(roster)}


@app.get("/api/roster")
def get_roster():
    roster = load_roster()
    return JSONResponse(roster)


# ─── 採点 ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = _ROOT / "backend" / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/grade")
async def post_grade(file: UploadFile = File(...)):
    """
    生徒答案 PDF/画像を一括 OCR & 採点しセッションを生成する。
    Returns: {session_id, students (グリッドデータ)}
    """
    ak = load_answer_key()
    if ak is None:
        raise HTTPException(status_code=400, detail="先に正解データを登録してください。")

    suffix = Path(file.filename or "upload").suffix or ".pdf"
    tmp_pdf = _UPLOADS / f"{uuid.uuid4().hex}{suffix}"
    tmp_out = Path(tempfile.mkdtemp(prefix="grade_"))

    try:
        tmp_pdf.write_bytes(await file.read())

        from .ocr import run_yomitoku
        pages = run_yomitoku(tmp_pdf, tmp_out, lite=True, device="cpu", combine=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR エラー: {e}")
    finally:
        shutil.rmtree(tmp_out, ignore_errors=True)
        tmp_pdf.unlink(missing_ok=True)

    cfg = _load_config()
    valid_choices: list[str] = ak.get("valid_choices", cfg["valid_choices"])
    correction_map: dict = cfg.get("ocr_correction_map", {})
    key_answers: dict = ak.get("answers", {})
    template: dict = ak.get("template", {})
    points = {"default": 1, "overrides": {}}

    roster = load_roster()

    students = []
    for page_idx, page_data in enumerate(pages):
        sheet = read_student_sheet(page_data, template, valid_choices, correction_map)
        results = judge_answers(sheet["answers"], key_answers, valid_choices, correction_map)
        score, max_score = score_student(results, points)

        sno = sheet["student_no"]
        name = roster.get(sno, "") if sno else ""

        students.append({
            "page_index": page_idx,
            "student_no": sno,
            "student_no_status": sheet["student_no_status"],
            "name": name,
            "results": results,
            "score": score,
            "max_score": max_score,
        })

    session_id = uuid.uuid4().hex
    session = {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "confirmed": False,
        "points": points,
        "students": students,
    }
    session_path = _SESSIONS / f"{session_id}.json"
    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    return JSONResponse({"session_id": session_id, "students": students})


# ─── セッション ──────────────────────────────────────────────────────────────

def _load_session(session_id: str) -> dict:
    path = _SESSIONS / f"{session_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="セッションが見つかりません。")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_session(session: dict) -> None:
    path = _SESSIONS / f"{session['session_id']}.json"
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    return JSONResponse(_load_session(session_id))


@app.patch("/api/session/{session_id}/cell")
async def patch_session_cell(session_id: str, body: dict):
    """
    生徒1問の値を修正し再判定する。
    Body: {"page_index": 0, "q": "1", "value": "ア"}
    """
    session = _load_session(session_id)
    ak = load_answer_key()
    if ak is None:
        raise HTTPException(status_code=400, detail="正解データが未登録です。")

    page_index = int(body.get("page_index", 0))
    q = str(body.get("q", ""))
    value = str(body.get("value", ""))

    cfg = _load_config()
    valid_choices = ak.get("valid_choices", cfg["valid_choices"])
    correction_map = cfg.get("ocr_correction_map", {})
    key_answers = ak.get("answers", {})
    points = session.get("points", {"default": 1, "overrides": {}})

    student = next((s for s in session["students"] if s["page_index"] == page_index), None)
    if student is None:
        raise HTTPException(status_code=400, detail="page_index が見つかりません。")

    # 値を更新して再判定
    student["results"][q] = {"read": value, "judge": "review", "review": True}
    correct = key_answers.get(q, "")
    if value == correct and value in valid_choices:
        student["results"][q] = {"read": value, "judge": "correct", "review": False}
    elif not value:
        student["results"][q] = {"read": "", "judge": "blank", "review": False}
    elif value in valid_choices and correct:
        student["results"][q] = {"read": value, "judge": "wrong", "review": False}

    score, max_score = score_student(student["results"], points)
    student["score"] = score
    student["max_score"] = max_score

    _save_session(session)
    return {"ok": True, "score": score, "max_score": max_score, "result": student["results"][q]}


@app.patch("/api/session/{session_id}/student-no")
async def patch_student_no(session_id: str, body: dict):
    """出席番号を修正し名簿と再突合する。"""
    session = _load_session(session_id)
    page_index = int(body.get("page_index", 0))
    value = str(body.get("value", ""))

    student = next((s for s in session["students"] if s["page_index"] == page_index), None)
    if student is None:
        raise HTTPException(status_code=400, detail="page_index が見つかりません。")

    roster = load_roster()
    student["student_no"] = value
    student["student_no_status"] = "ok" if value else "review"
    student["name"] = roster.get(value, "")
    _save_session(session)
    return {"ok": True, "name": student["name"]}


@app.post("/api/session/{session_id}/confirm")
def confirm_session(session_id: str):
    session = _load_session(session_id)
    session["confirmed"] = True
    _save_session(session)
    return {"ok": True}


# ─── CSV 出力 ────────────────────────────────────────────────────────────────

@app.get("/api/session/{session_id}/csv")
def get_session_csv(
    session_id: str,
    encoding: str = Query(default="utf-8-sig", pattern="^(utf-8-sig|shift_jis)$"),
):
    """
    採点結果を CSV でダウンロードする。
    列: 出席番号, 氏名, Q1..Qn, 合計点
    各問: ○(correct) / ×(wrong) / -(blank) / ?(review)
    """
    session = _load_session(session_id)
    students = session.get("students", [])

    if not students:
        raise HTTPException(status_code=404, detail="採点データがありません。")

    qnos = sorted(students[0]["results"].keys(), key=lambda x: int(x))

    def judge_symbol(judge: str) -> str:
        return {"correct": "○", "wrong": "×", "blank": "-", "review": "?"}.get(judge, "?")

    buf = io.StringIO()
    writer = csv.writer(buf)

    header = ["出席番号", "氏名"] + [f"Q{q}" for q in qnos] + ["合計点"]
    writer.writerow(header)

    for s in sorted(students, key=lambda x: x["page_index"]):
        row = [
            s.get("student_no", ""),
            s.get("name", ""),
        ]
        for q in qnos:
            r = s["results"].get(q, {})
            row.append(judge_symbol(r.get("judge", "review")))
        row.append(s.get("score", 0))
        writer.writerow(row)

    csv_str = buf.getvalue()
    try:
        raw = csv_str.encode(encoding)
    except LookupError:
        raw = csv_str.encode("utf-8-sig")

    filename = f"scores_{session_id[:8]}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": f"text/csv; charset={encoding}",
    }
    return StreamingResponse(io.BytesIO(raw), headers=headers, media_type="text/csv")
