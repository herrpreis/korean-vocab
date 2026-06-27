from fastmcp import FastMCP
import sqlite3
import json
import os
from datetime import date, datetime, timezone, timedelta

mcp = FastMCP("Korean Vocab Bot")
DB_PATH = "korean.db"

BASE_IMAGE_URL = "https://raw.githubusercontent.com/herrpreis/korean-vocab/main/images/"

# Mapping: korean word -> github image number
IMAGE_MAP = {
    "약속": "30", "복잡하다": "12", "북": "18", "동서남북": "42",
    "지금": "48", "만": "20", "주문하다": "53", "열심히": "9",
    "조카": "52", "딸": "40", "놀다": "17", "수업": "7",
    "중": "47", "요즘": "21", "어렵다": "28", "맑다": "38",
    "조심하다": "63", "그럼": "3", "가족": "1", "손수건": "57",
    "외출하다": "22", "읽다": "19", "과자": "62", "기저귀": "27",
    "자전거": "50", "등산하다": "43", "심심하다": "36", "스물": "58",
    "서른": "8", "장난감": "56", "주무스다": "35", "대화": "5",
    "댁": "26", "친절하다": "2", "드리다": "44", "기린": "54",
    "개벽": "46", "독서실": "13", "찾다": "32", "보통": "37",
    "만두": "34", "담배 피우다": "49", "사무실": "51", "왜냐하면": "59",
    "다른": "23", "알아보다": "11", "걸리다": "29", "정류장": "0",
    "추천하다": "14", "얇다": "60", "유행이다": "24", "상사": "55",
    "계획하다": "4", "준비하다": "39", "유치원": "15", "놀이터": "6",
    "바꾸다": "16", "거실": "41", "뻥튀기": "61", "돌잔치": "25",
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    # Add image_url column if it doesn't exist
    try:
        db.execute("ALTER TABLE words ADD COLUMN image_url TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass  # Column already exists

    # Populate image_url for known words
    for korean, num in IMAGE_MAP.items():
        url = BASE_IMAGE_URL + num
        db.execute(
            "UPDATE words SET image_url = ? WHERE korean = ? AND (image_url IS NULL OR image_url = '')",
            (url, korean)
        )
    db.commit()

init_db()

@mcp.tool()
def get_due_cards(limit: int = 5) -> list[dict]:
    """Returns cards due for review today, oldest due first."""
    db = get_db()
    rows = db.execute("""
        SELECT w.id, w.korean, w.english, w.type, w.topic, w.example, w.image_url
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
             topic: str = "", example: str = "", image_url: str = "") -> dict:
    """Adds a new word or grammar point to the database."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO words (korean, english, type, topic, example, image_url) VALUES (?, ?, ?, ?, ?, ?)",
        (korean, english, type, topic, example, image_url)
    )
    word_id = cur.lastrowid
    db.execute(
        "INSERT INTO card_state (word_id, due_date) VALUES (?, ?)",
        (word_id, date.today().isoformat())
    )
    db.commit()
    return {"id": word_id, "korean": korean, "english": english}

@mcp.tool()
def get_deck_summary() -> dict:
    """Returns a summary of how many cards exist per type and topic."""
    db = get_db()
    by_type = db.execute(
        "SELECT type, COUNT(*) as count FROM words GROUP BY type"
    ).fetchall()
    by_topic = db.execute(
        "SELECT topic, COUNT(*) as count FROM words WHERE topic != '' GROUP BY topic ORDER BY count DESC"
    ).fetchall()
    return {
        "by_type": [dict(r) for r in by_type],
        "by_topic": [dict(r) for r in by_topic]
    }

@mcp.tool()
def get_cards_by_type(type: str, limit: int = 10) -> list[dict]:
    """Returns cards filtered by type: 'vocab', 'grammar', or 'phrase'."""
    db = get_db()
    rows = db.execute(
        "SELECT id, korean, english, type, topic, example, image_url FROM words WHERE type = ? LIMIT ?",
        (type, limit)
    ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def get_cards_by_topic(topic: str, limit: int = 10) -> list[dict]:
    """Returns cards filtered by topic (e.g. 'Restaurant', 'weather', 'Shop')."""
    db = get_db()
    rows = db.execute(
        "SELECT id, korean, english, type, topic, example, image_url FROM words WHERE topic LIKE ? LIMIT ?",
        (f"%{topic}%", limit)
    ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def search_cards(query: str, limit: int = 10) -> list[dict]:
    """Searches cards by keyword in Korean or English fields."""
    db = get_db()
    rows = db.execute(
        """SELECT id, korean, english, type, topic, example, image_url FROM words
           WHERE korean LIKE ? OR english LIKE ? LIMIT ?""",
        (f"%{query}%", f"%{query}%", limit)
    ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def generate_test(type: str = "vocab", topic: str = "", limit: int = 5) -> dict:
    """
    Returns a mixed set of cards for a custom test.
    Fetches words/phrases of the given type (and optional topic),
    plus up to 3 grammar cards to combine into exercises.
    """
    db = get_db()
    if topic:
        words = db.execute(
            "SELECT id, korean, english, type, topic, example, image_url FROM words WHERE type = ? AND topic LIKE ? LIMIT ?",
            (type, f"%{topic}%", limit)
        ).fetchall()
    else:
        words = db.execute(
            "SELECT id, korean, english, type, topic, example, image_url FROM words WHERE type = ? LIMIT ?",
            (type, limit)
        ).fetchall()
    grammar = db.execute(
        "SELECT id, korean, english, type, topic, example, image_url FROM words WHERE type = 'grammar' LIMIT 3"
    ).fetchall()
    return {
        "words": [dict(r) for r in words],
        "grammar": [dict(r) for r in grammar]
    }

if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
