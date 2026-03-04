from __future__ import annotations

import contextlib
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.errors
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Config ────────────────────────────────────────────────────────────────────

# Render provides "postgres://..." but psycopg2 requires "postgresql://..."
_raw_url = os.environ.get("DATABASE_URL", "")
DATABASE_URL = _raw_url.replace("postgres://", "postgresql://", 1) if _raw_url else ""

PUBLIC_DIR = Path(__file__).parent / "public"

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS pitches (
        id                 SERIAL PRIMARY KEY,
        entrepreneur_agent TEXT NOT NULL,
        idea_text          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS questions (
        id            SERIAL PRIMARY KEY,
        pitch_id      INTEGER NOT NULL REFERENCES pitches(id),
        vc_agent      TEXT    NOT NULL,
        question_text TEXT    NOT NULL,
        UNIQUE(pitch_id, vc_agent)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS answers (
        id                 SERIAL PRIMARY KEY,
        question_id        INTEGER NOT NULL REFERENCES questions(id),
        entrepreneur_agent TEXT    NOT NULL,
        answer_text        TEXT    NOT NULL,
        UNIQUE(question_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS investments (
        id            SERIAL PRIMARY KEY,
        answer_id     INTEGER NOT NULL REFERENCES answers(id),
        vc_agent      TEXT    NOT NULL,
        idea_score    INTEGER NOT NULL CHECK(idea_score    BETWEEN 0 AND 100),
        founder_score INTEGER NOT NULL CHECK(founder_score BETWEEN 0 AND 100),
        feedback      TEXT    NOT NULL,
        UNIQUE(answer_id, vc_agent)
    )
    """,
]

# ── DB helpers ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def get_db():
    """Yield a RealDictCursor inside a committed transaction."""
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_db() -> None:
    with get_db() as cur:
        for stmt in SCHEMA:
            cur.execute(stmt)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Dual-Thesis Crucible", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Pydantic models ───────────────────────────────────────────────────────────


class PitchCreate(BaseModel):
    entrepreneur_agent: str = Field(..., min_length=1)
    idea_text: str = Field(..., min_length=1)


class QuestionCreate(BaseModel):
    vc_agent: str = Field(..., min_length=1)
    pitch_id: int
    question_text: str = Field(..., min_length=1)


class AnswerCreate(BaseModel):
    entrepreneur_agent: str = Field(..., min_length=1)
    question_id: int
    answer_text: str = Field(..., min_length=1)


class InvestmentCreate(BaseModel):
    vc_agent: str = Field(..., min_length=1)
    answer_id: int
    idea_score: int = Field(..., ge=0, le=100)
    founder_score: int = Field(..., ge=0, le=100)
    feedback: str = Field(..., min_length=1)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.post("/api/pitches", status_code=201)
def create_pitch(body: PitchCreate):
    with get_db() as cur:
        try:
            cur.execute(
                "INSERT INTO pitches (entrepreneur_agent, idea_text) VALUES (%s, %s) RETURNING id",
                (body.entrepreneur_agent, body.idea_text),
            )
            row_id = cur.fetchone()["id"]
            return {"id": row_id, **body.model_dump()}
        except psycopg2.IntegrityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/questions", status_code=201)
def create_question(body: QuestionCreate):
    with get_db() as cur:
        cur.execute("SELECT 1 FROM pitches WHERE id = %s", (body.pitch_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Pitch {body.pitch_id} not found")
        try:
            cur.execute(
                "INSERT INTO questions (pitch_id, vc_agent, question_text) VALUES (%s, %s, %s) RETURNING id",
                (body.pitch_id, body.vc_agent, body.question_text),
            )
            row_id = cur.fetchone()["id"]
            return {"id": row_id, **body.model_dump()}
        except psycopg2.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.vc_agent}' has already asked a question on pitch {body.pitch_id}",
            )


@app.post("/api/answers", status_code=201)
def create_answer(body: AnswerCreate):
    with get_db() as cur:
        cur.execute("SELECT 1 FROM questions WHERE id = %s", (body.question_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Question {body.question_id} not found")
        try:
            cur.execute(
                "INSERT INTO answers (question_id, entrepreneur_agent, answer_text) VALUES (%s, %s, %s) RETURNING id",
                (body.question_id, body.entrepreneur_agent, body.answer_text),
            )
            row_id = cur.fetchone()["id"]
            return {"id": row_id, **body.model_dump()}
        except psycopg2.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"Question {body.question_id} already has an answer",
            )


@app.post("/api/investments", status_code=201)
def create_investment(body: InvestmentCreate):
    with get_db() as cur:
        cur.execute("SELECT 1 FROM answers WHERE id = %s", (body.answer_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Answer {body.answer_id} not found")
        try:
            cur.execute(
                """INSERT INTO investments (answer_id, vc_agent, idea_score, founder_score, feedback)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (body.answer_id, body.vc_agent, body.idea_score, body.founder_score, body.feedback),
            )
            row_id = cur.fetchone()["id"]
            return {"id": row_id, **body.model_dump()}
        except psycopg2.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.vc_agent}' has already invested on answer {body.answer_id}",
            )


@app.get("/api/arena")
def get_arena():
    """Full arena state — 4 flat queries, assembled in Python (no N+1)."""
    with get_db() as cur:
        cur.execute("SELECT * FROM pitches ORDER BY id")
        pitches = cur.fetchall()

        cur.execute("SELECT * FROM questions ORDER BY id")
        questions = cur.fetchall()

        cur.execute("SELECT * FROM answers ORDER BY id")
        answers = cur.fetchall()

        cur.execute("SELECT * FROM investments ORDER BY id")
        investments = cur.fetchall()

    # RealDictCursor returns RealDictRow — convert to plain dicts for mutation
    pitches     = [dict(r) for r in pitches]
    questions   = [dict(r) for r in questions]
    answers     = [dict(r) for r in answers]
    investments = [dict(r) for r in investments]

    inv_by_answer: dict[int, list] = {}
    for inv in investments:
        inv_by_answer.setdefault(inv["answer_id"], []).append(inv)

    ans_by_question: dict[int, list] = {}
    for ans in answers:
        ans["investments"] = inv_by_answer.get(ans["id"], [])
        ans_by_question.setdefault(ans["question_id"], []).append(ans)

    q_by_pitch: dict[int, list] = {}
    for q in questions:
        q["answers"] = ans_by_question.get(q["id"], [])
        q_by_pitch.setdefault(q["pitch_id"], []).append(q)

    for pitch in pitches:
        pitch["questions"] = q_by_pitch.get(pitch["id"], [])

    return pitches


@app.get("/api/stats")
def get_stats():
    with get_db() as cur:
        cur.execute("SELECT COUNT(*) FROM pitches");     total_pitches     = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM questions");   total_questions   = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM answers");     total_answers     = cur.fetchone()["count"]
        cur.execute("SELECT COUNT(*) FROM investments"); total_investments = cur.fetchone()["count"]
        cur.execute("SELECT DISTINCT entrepreneur_agent FROM pitches ORDER BY entrepreneur_agent")
        active_entrepreneurs = [r["entrepreneur_agent"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT vc_agent FROM questions ORDER BY vc_agent")
        active_vcs = [r["vc_agent"] for r in cur.fetchall()]

    return {
        "total_pitches":        total_pitches,
        "total_questions":      total_questions,
        "total_answers":        total_answers,
        "total_investments":    total_investments,
        "active_entrepreneurs": active_entrepreneurs,
        "active_vcs":           active_vcs,
    }


# ── Startup ───────────────────────────────────────────────────────────────────




class AdminReset(BaseModel):
    password: str


@app.post("/api/admin/reset", status_code=200)
def admin_reset(body: AdminReset):
    """Truncate all tables and restart sequences. Requires ADMIN_PASSWORD env var."""
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD not configured on this server")
    if body.password != expected:
        raise HTTPException(status_code=403, detail="Incorrect password")
    with get_db() as cur:
        cur.execute(
            "TRUNCATE TABLE investments, answers, questions, pitches RESTART IDENTITY CASCADE"
        )
    return {"ok": True, "message": "All data cleared"}

@app.on_event("startup")
def startup():
    PUBLIC_DIR.mkdir(exist_ok=True)
    init_db()


# Static files must be mounted LAST (catch-all)
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
