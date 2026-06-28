from fastmcp import FastMCP
import sqlite3
import json
import os
import base64
import urllib.request
from datetime import date, datetime, timezone, timedelta

mcp = FastMCP("Korean Vocab Bot")
DB_PATH = "korean.db"

BASE_IMAGE_URL = "https://raw.githubusercontent.com/herrpreis/korean-vocab/main/images/"

# Mapping: korean word -> github image filename (with correct extension)
IMAGE_MAP = {
    "약속": "30.png",
    "복잡하다": "12.jpg",
    "북": "18.jpg",
    "동서남북": "42.png",
    "지금": "48.jpg",
    "만": "20.png",
    "주문하다": "53.jpg",
    "열심히": "9.jpg",
    "조카": "52.jpg",
    "딸": "40.jpg",
    "놀다": "17.jpg",
    "수업": "7.jpg",
    "중": "47.jpg",
    "요즘": "21.jpg",
    "어렵다": "28.jpg",
    "맑다": "38.jpg",
    "조심하다": "63.jpg",
    "그럼": "3.jpg",
    "가족": "1.jpg",
    "손수건": "57.jpg",
    "외출하다": "22.jpg",
    "읽다": "19.jpg",
    "과자": "62.jpg",
    "기저귀": "27.jpg",
    "자전거": "50.jpg",
    "등산하다": "43.jpg",
    "심심하다": "36.jpg",
    "스물": "58.jpg",
    "서른": "8.jpg",
    "장난감": "56.jpg",
    "주무스다": "35.jpg",
    "대화": "5.jpg",
    "댁": "26.jpg",
    "친절하다": "2.jpg",
    "드리다": "44.jpg",
    "기린": "54.jpg",
    "개벽": "46.jpg",
    "독서실": "13.jpg",
    "찾다": "32.jpg",
    "보통": "37.jpg",
    "만두": "34.jpg",
    "담배 피우다": "49.jpg",
    "사무실": "51.jpg",
    "왜냐하면": "59.jpg",
    "다른": "23.jpg",
    "알아보다": "11.jpg",
    "걸리다": "29.png",
    "정류장": "0.png",
    "추천하다": "14.jpg",
    "얇다": "60.png",
    "유행이다": "24.png",
    "상사": "55.jpg",
    "계획하다": "4.jpg",
    "준비하다": "39.png",
    "유치원": "15.jpg",
    "놀이터": "6.png",
    "바꾸다": "16.png",
    "거실": "41.png",
    "뻥튀기": "61.png",
    "돌잔치": "25.png",
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    try:
        db.execute("ALTER TABLE words ADD COLUMN image_url TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass

    for korean, filename in IMAGE_MAP.items():
        url = BASE_IMAGE_URL + filename
        db.execute(
            "UPDATE words SET image_url = ? WHERE korean = ?",
            (url, korean)
        )
    db.commit()

init_db()

@mcp.tool()
def upload_image(image_url: str, filename: str, korean: str = "") -> dict:
    """
    Fetches an image from a URL and uploads it to the GitHub images/ folder.
    Optionally updates the image_url in the database for a given korean word.
    Returns the final GitHub raw URL.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {"error": "GITHUB_TOKEN not set in environment"}

    # Fetch image bytes
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "korean-vocab-bot"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            image_bytes = resp.read()
    except Exception as e:
        return {"error": f"Failed to fetch image: {e}"}

    encoded = base64.b64encode(image_bytes).decode("utf-8")

    # Check if file already exists (need SHA to update)
    api_path = f"https://api.github.com/repos/herrpreis/korean-vocab/contents/images/{filename}"
    sha = None
    try:
        check_req = urllib.request.Request(
            api_path,
            headers={
                "Authorization": f"token {github_token}",
                "Accept": "application/vnd.github+json"
            }
        )
        with urllib.request.urlopen(check_req) as resp:
            existing = json.loads(resp.read())
            sha = existing.get("sha")
    except Exception:
        pass  # File doesn't exist yet, that's fine

    # Push to GitHub
    payload = {
        "message": f"Add image {filename}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    push_req = urllib.request.Request(
        api_path,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(push_req) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {"error": f"GitHub push failed: {e}"}

    final_url = BASE_IMAGE_URL + filename

    # Update DB if korean word provided
    if korean:
        db = get_db()
        db.execute("UPDATE words SET image_url = ? WHERE korean = ?", (final_url, korean))
        db.commit()

    return {"success": True, "url": final_url, "filename": filename}

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
