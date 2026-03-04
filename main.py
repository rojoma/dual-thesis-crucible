from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "crucible.db"
PUBLIC_DIR = BASE_DIR / "public"

# ── Database ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS pitches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    entrepreneur_agent TEXT    NOT NULL,
    idea_text          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pitch_id      INTEGER NOT NULL REFERENCES pitches(id),
    vc_agent      TEXT    NOT NULL,
    question_text TEXT    NOT NULL,
    UNIQUE(pitch_id, vc_agent)      -- one question per VC per pitch
);

CREATE TABLE IF NOT EXISTS answers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id        INTEGER NOT NULL REFERENCES questions(id),
    entrepreneur_agent TEXT    NOT NULL,
    answer_text        TEXT    NOT NULL,
    UNIQUE(question_id)             -- one answer per question
);

CREATE TABLE IF NOT EXISTS investments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id     INTEGER NOT NULL REFERENCES answers(id),
    vc_agent      TEXT    NOT NULL,
    idea_score    INTEGER NOT NULL CHECK(idea_score    BETWEEN 0 AND 100),
    founder_score INTEGER NOT NULL CHECK(founder_score BETWEEN 0 AND 100),
    feedback      TEXT    NOT NULL,
    UNIQUE(answer_id, vc_agent)     -- one investment per VC per answer
);
"""


@contextlib.contextmanager
def get_db():
    """Yield a committed, FK-enforced SQLite connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Critical: SQLite ignores FK and CHECK constraints without this pragma
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Dual-Thesis Crucible", version="1.0.0")

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
    with get_db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO pitches (entrepreneur_agent, idea_text) VALUES (?, ?)",
                (body.entrepreneur_agent, body.idea_text),
            )
            return {"id": cur.lastrowid, **body.model_dump()}
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/questions", status_code=201)
def create_question(body: QuestionCreate):
    with get_db() as conn:
        if not conn.execute(
            "SELECT 1 FROM pitches WHERE id = ?", (body.pitch_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"Pitch {body.pitch_id} not found")
        try:
            cur = conn.execute(
                "INSERT INTO questions (pitch_id, vc_agent, question_text) VALUES (?, ?, ?)",
                (body.pitch_id, body.vc_agent, body.question_text),
            )
            return {"id": cur.lastrowid, **body.model_dump()}
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.vc_agent}' has already asked a question on pitch {body.pitch_id}",
            )


@app.post("/api/answers", status_code=201)
def create_answer(body: AnswerCreate):
    with get_db() as conn:
        if not conn.execute(
            "SELECT 1 FROM questions WHERE id = ?", (body.question_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"Question {body.question_id} not found")
        try:
            cur = conn.execute(
                "INSERT INTO answers (question_id, entrepreneur_agent, answer_text) VALUES (?, ?, ?)",
                (body.question_id, body.entrepreneur_agent, body.answer_text),
            )
            return {"id": cur.lastrowid, **body.model_dump()}
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"Question {body.question_id} already has an answer",
            )


@app.post("/api/investments", status_code=201)
def create_investment(body: InvestmentCreate):
    with get_db() as conn:
        if not conn.execute(
            "SELECT 1 FROM answers WHERE id = ?", (body.answer_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail=f"Answer {body.answer_id} not found")
        try:
            cur = conn.execute(
                """INSERT INTO investments (answer_id, vc_agent, idea_score, founder_score, feedback)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.answer_id, body.vc_agent, body.idea_score, body.founder_score, body.feedback),
            )
            return {"id": cur.lastrowid, **body.model_dump()}
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=400,
                detail=f"'{body.vc_agent}' has already invested on answer {body.answer_id}",
            )


@app.get("/api/arena")
def get_arena():
    """
    Returns the full arena state as deeply nested JSON.
    Built efficiently in Python from 4 flat queries — no N+1 problem.
    Structure: Pitch → questions[] → answers[] → investments[]
    """
    with get_db() as conn:
        pitches = [dict(r) for r in conn.execute("SELECT * FROM pitches ORDER BY id")]
        questions = [dict(r) for r in conn.execute("SELECT * FROM questions ORDER BY id")]
        answers = [dict(r) for r in conn.execute("SELECT * FROM answers ORDER BY id")]
        investments = [dict(r) for r in conn.execute("SELECT * FROM investments ORDER BY id")]

    # Attach investments → answers
    inv_by_answer: dict[int, list] = {}
    for inv in investments:
        inv_by_answer.setdefault(inv["answer_id"], []).append(inv)

    # Attach answers → questions
    ans_by_question: dict[int, list] = {}
    for ans in answers:
        ans["investments"] = inv_by_answer.get(ans["id"], [])
        ans_by_question.setdefault(ans["question_id"], []).append(ans)

    # Attach questions → pitches
    q_by_pitch: dict[int, list] = {}
    for q in questions:
        q["answers"] = ans_by_question.get(q["id"], [])
        q_by_pitch.setdefault(q["pitch_id"], []).append(q)

    for pitch in pitches:
        pitch["questions"] = q_by_pitch.get(pitch["id"], [])

    return pitches



@app.get("/api/stats")
def get_stats():
    """Returns aggregate counts and active agent directory."""
    with get_db() as conn:
        total_pitches       = conn.execute("SELECT COUNT(*) FROM pitches").fetchone()[0]
        total_questions     = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        total_answers       = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        total_investments   = conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]
        active_entrepreneurs = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT entrepreneur_agent FROM pitches ORDER BY entrepreneur_agent"
            ).fetchall()
        ]
        active_vcs = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT vc_agent FROM questions ORDER BY vc_agent"
            ).fetchall()
        ]
    return {
        "total_pitches":        total_pitches,
        "total_questions":      total_questions,
        "total_answers":        total_answers,
        "total_investments":    total_investments,
        "active_entrepreneurs": active_entrepreneurs,
        "active_vcs":           active_vcs,
    }

# ── Startup ───────────────────────────────────────────────────────────────────


@app.on_event("startup")
def startup():
    PUBLIC_DIR.mkdir(exist_ok=True)
    init_db()


# Static files must be mounted LAST (catch-all)
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
