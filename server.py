from fastmcp import FastMCP
import sqlite3
import json
import os
from datetime import date, datetime, timezone, timedelta

mcp = FastMCP("Korean Vocab Bot")
DB_PATH = "korean.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@mcp.tool()
def get_due_cards(limit: int = 5) -> list[dict]:
    """Returns cards due for review today, oldest due first."""
    db = get_db()
    rows = db.execute("""
        SELECT w.id, w.korean, w.english, w.type, w.topic, w.example
        FROM words w
        JOIN card_state cs ON w.id = cs.word_id
        WHERE cs.due_date <= ?
        ORDER BY cs.due_date ASC
        LIMIT ?
    """, (date.today().isoformat(), limit)).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def get_stats() -> dict:
    """Returns study statistics."""
    db = get_db()
    total = db.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    due   = db.execute(
        "SELECT COUNT(*) FROM card_state WHERE due_date <= ?",
        (date.today().isoformat(),)
    ).fetchone()[0]
    reviewed_today = db.execute(
        "SELECT COUNT(*) FROM reviews WHERE reviewed_at >= ?",
        (date.today().isoformat(),)
    ).fetchone()[0]
    return {"total_cards": total, "due_today": due, "reviewed_today": reviewed_today}

@mcp.tool()
def record_review(word_id: int, rating: int) -> dict:
    """
    Records a review result and updates scheduling using FSRS.
    Rating: 1=again (don't know), 2=hard, 3=good, 4=easy (know very well)
    """
    from fsrs import Scheduler, Card, Rating

    RATING_MAP = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}

    db = get_db()
    row = db.execute(
        "SELECT card_data FROM card_state WHERE word_id = ?", (word_id,)
    ).fetchone()

    card = Card()
    if row and row[0]:
        try:
            card = Card.from_dict(json.loads(row[0]))
        except Exception:
            card = Card()

    scheduler = Scheduler()
    card, _ = scheduler.review_card(card, RATING_MAP[rating])

    db.execute("""
        INSERT INTO card_state (word_id, due_date, card_data)
        VALUES (?, ?, ?)
        ON CONFLICT(word_id) DO UPDATE SET
            due_date  = excluded.due_date,
            card_data = excluded.card_data
    """, (word_id, card.due.date().isoformat(), json.dumps(card.to_dict())))

    db.execute(
        "INSERT INTO reviews (word_id, rating, reviewed_at) VALUES (?, ?, ?)",
        (word_id, rating, datetime.now(timezone.utc).isoformat())
    )
    db.commit()

    return {
        "word_id":    word_id,
        "next_due":   card.due.date().isoformat(),
        "stability":  round(card.stability, 2),
        "difficulty": round(card.difficulty, 2),
        "state":      card.state.name
    }

@mcp.tool()
def add_word(korean: str, english: str, type: str = "vocab",
             topic: str = "", example: str = "") -> dict:
    """Adds a new word or grammar point to the database."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO words (korean, english, type, topic, example) VALUES (?, ?, ?, ?, ?)",
        (korean, english, type, topic, example)
    )
    word_id = cur.lastrowid
    db.execute(
        "INSERT INTO card_state (word_id, due_date) VALUES (?, ?)",
        (word_id, date.today().isoformat())
    )
    db.commit()
    return {"id": word_id, "korean": korean, "english": english}

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
