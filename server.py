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

    # Original image_url column
    try:
        db.execute("ALTER TABLE words ADD COLUMN image_url TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass

    # New grammar-specific columns
    for col in ["usage", "formation", "sentences", "translations", "notes"]:
        try:
            db.execute(f"ALTER TABLE words ADD COLUMN {col} TEXT DEFAULT ''")
            db.commit()
        except Exception:
            pass

    db.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_date TEXT UNIQUE,
            grammar_points TEXT DEFAULT '',
            vocab TEXT DEFAULT '',
            homework TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            raw_text TEXT DEFAULT '',
            file_urls TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    db.commit()
    
    for korean, filename in IMAGE_MAP.items():
        url = BASE_IMAGE_URL + filename
        db.execute("UPDATE words SET image_url = ? WHERE korean = ?", (url, korean))
    db.commit()

init_db()

@mcp.tool()
def upload_image(filename: str, image_url: str = "", image_base64: str = "", korean: str = "") -> dict:
    """
    Uploads an image to the GitHub images/ folder, either by fetching it from
    a URL or from raw base64-encoded image data (e.g. an image uploaded
    directly in a chat). Validates that the image data is complete and
    undamaged before uploading. Optionally updates the image_url in the
    database for a given korean word. Returns the final GitHub raw URL.

    Provide exactly one of `image_url` or `image_base64`.
    """
    import io
    from PIL import Image

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {"error": "GITHUB_TOKEN not set in environment"}

    if not image_url and not image_base64:
        return {"error": "Provide either image_url or image_base64"}

    if image_base64:
        # Strip a data URL prefix if present, e.g. "data:image/png;base64,...."
        b64_data = image_base64.split(",", 1)[-1] if image_base64.startswith("data:") else image_base64
        try:
            image_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return {"error": f"Failed to decode image_base64: {e}"}
    else:
        try:
            req = urllib.request.Request(image_url, headers={"User-Agent": "korean-vocab-bot"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                image_bytes = resp.read()
        except Exception as e:
            return {"error": f"Failed to fetch image: {e}"}

    # Validate the image is complete and undamaged before uploading.
    # This catches truncated/corrupted data (e.g. from a base64 payload
    # that got cut off in transit) that would otherwise upload "successfully"
    # as a broken file.
    if len(image_bytes) < 100:
        return {"error": f"Image data is suspiciously small ({len(image_bytes)} bytes) - likely corrupted or empty"}

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()  # forces full decode; raises if data is truncated/corrupted
    except Exception as e:
        return {
            "error": f"Image data appears corrupted or incomplete ({len(image_bytes)} bytes received): {e}",
            "bytes_received": len(image_bytes),
        }

    encoded = base64.b64encode(image_bytes).decode("utf-8")
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
        pass

    payload = {"message": f"Add image {filename}", "content": encoded}
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    push_req = urllib.request.Request(
        api_path, data=data, method="PUT",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(push_req) as resp:
            json.loads(resp.read())
    except Exception as e:
        return {"error": f"GitHub push failed: {e}"}

    final_url = BASE_IMAGE_URL + filename
    if korean:
        db = get_db()
        db.execute("UPDATE words SET image_url = ? WHERE korean = ?", (final_url, korean))
        db.commit()

    return {"success": True, "url": final_url, "filename": filename, "bytes_uploaded": len(image_bytes)}


@mcp.tool()
def get_due_cards(limit: int = 5) -> list[dict]:
    """Returns cards due for review today, oldest due first."""
    db = get_db()
    rows = db.execute("""
        SELECT w.id, w.korean, w.english, w.type, w.topic, w.example,
               w.usage, w.formation, w.sentences, w.translations, w.notes, w.image_url
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
    due = db.execute(
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
def add_word(
    korean: str,
    english: str,
    type: str = "vocab",
    topic: str = "",
    example: str = "",
    image_url: str = "",
    usage: str = "",
    formation: str = "",
    sentences: str = "",
    translations: str = "",
    notes: str = ""
) -> dict:
    """
    Adds a new word or grammar point to the database.
    For grammar cards, use the dedicated fields:
      - usage: how to attach the pattern to verb stems
      - formation: 3 formation examples (e.g. 가다 → 가고 싶어요)
      - sentences: 3 Korean example sentences
      - translations: English translations of the sentences
      - notes: irregular forms, negative form, common mistakes
    """
    db = get_db()
    cur = db.execute(
        """INSERT INTO words
           (korean, english, type, topic, example, image_url, usage, formation, sentences, translations, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (korean, english, type, topic, example, image_url, usage, formation, sentences, translations, notes)
    )
    word_id = cur.lastrowid
    db.execute(
        "INSERT INTO card_state (word_id, due_date) VALUES (?, ?)",
        (word_id, date.today().isoformat())
    )
    db.commit()
    return {"id": word_id, "korean": korean, "english": english}

@mcp.tool()
def update_grammar(
    word_id: int,
    usage: str = "",
    formation: str = "",
    sentences: str = "",
    translations: str = "",
    notes: str = ""
) -> dict:
    """
    Updates the grammar-specific fields for an existing card.
    Use this to migrate or correct grammar cards.
    """
    db = get_db()
    db.execute(
        """UPDATE words SET usage=?, formation=?, sentences=?, translations=?, notes=?
           WHERE id=?""",
        (usage, formation, sentences, translations, notes, word_id)
    )
    db.commit()
    return {"success": True, "word_id": word_id}

@mcp.tool()
def update_word(
    word_id: int,
    english: str = "",
    topic: str = "",
    example: str = "",
    notes: str = "",
    image_url: str = ""
) -> dict:
    """
    Partially updates an existing vocab/phrase card. Only pass the fields
    you want to change - any field left blank/omitted keeps its current
    value (unlike update_grammar, which overwrites all its fields
    unconditionally). Use this to add a mnemonic, fix a meaning/topic, or
    attach an image to a word that already exists in the deck.
    """
    fields = {
        "english": english,
        "topic": topic,
        "example": example,
        "notes": notes,
        "image_url": image_url,
    }
    updates = {k: v for k, v in fields.items() if v != ""}
    if not updates:
        return {"error": "No fields provided to update (all were blank)"}

    db = get_db()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [word_id]
    db.execute(f"UPDATE words SET {set_clause} WHERE id=?", values)
    db.commit()
    return {"success": True, "word_id": word_id, "updated_fields": list(updates.keys())}

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
        """SELECT id, korean, english, type, topic, example,
           usage, formation, sentences, translations, notes, image_url
           FROM words WHERE type = ? LIMIT ?""",
        (type, limit)
    ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def get_cards_by_topic(topic: str, limit: int = 10) -> list[dict]:
    """Returns cards filtered by topic (e.g. 'Restaurant', 'weather', 'Shop')."""
    db = get_db()
    rows = db.execute(
        """SELECT id, korean, english, type, topic, example,
           usage, formation, sentences, translations, notes, image_url
           FROM words WHERE topic LIKE ? LIMIT ?""",
        (f"%{topic}%", limit)
    ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def search_cards(query: str, limit: int = 10) -> list[dict]:
    """Searches cards by keyword in Korean or English fields."""
    db = get_db()
    rows = db.execute(
        """SELECT id, korean, english, type, topic, example,
           usage, formation, sentences, translations, notes, image_url
           FROM words WHERE korean LIKE ? OR english LIKE ? LIMIT ?""",
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
            """SELECT id, korean, english, type, topic, example,
               usage, formation, sentences, translations, notes, image_url
               FROM words WHERE type = ? AND topic LIKE ? LIMIT ?""",
            (type, f"%{topic}%", limit)
        ).fetchall()
    else:
        words = db.execute(
            """SELECT id, korean, english, type, topic, example,
               usage, formation, sentences, translations, notes, image_url
               FROM words WHERE type = ? LIMIT ?""",
            (type, limit)
        ).fetchall()
    grammar = db.execute(
        """SELECT id, korean, english, type, topic, example,
           usage, formation, sentences, translations, notes, image_url
           FROM words WHERE type = 'grammar' LIMIT 3"""
    ).fetchall()
    return {
        "words": [dict(r) for r in words],
        "grammar": [dict(r) for r in grammar]
    }



BASE_LESSON_URL = "https://raw.githubusercontent.com/herrpreis/korean-vocab/main/lessons/"
 
 
@mcp.tool()
def upload_lesson(filename: str, file_base64: str, lesson_date: str = "") -> dict:
    """
    Uploads a lesson file (PDF, worksheet, homework sheet, etc.) to the
    GitHub lessons/ folder from base64-encoded file data. Validates PDFs
    for completeness before uploading. If lesson_date (YYYY-MM-DD) is
    given and the filename does not already start with it, the date is
    prepended so files sort chronologically, e.g. 2026-07-15-homework.pdf.
    Returns the final GitHub raw URL.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {"error": "GITHUB_TOKEN not set in environment"}
 
    # Decode and validate the file data
    b64_data = file_base64.split(",", 1)[-1] if file_base64.startswith("data:") else file_base64
    try:
        file_bytes = base64.b64decode(b64_data)
    except Exception as e:
        return {"error": f"Failed to decode file_base64: {e}"}
 
    if len(file_bytes) < 100:
        return {"error": f"File data is suspiciously small ({len(file_bytes)} bytes) - likely corrupted or empty"}
 
    # PDFs must start with the %PDF magic bytes and contain an EOF marker
    if filename.lower().endswith(".pdf"):
        if not file_bytes.startswith(b"%PDF"):
            return {"error": "File does not look like a valid PDF (missing %PDF header)"}
        if b"%%EOF" not in file_bytes[-2048:]:
            return {"error": f"PDF appears truncated ({len(file_bytes)} bytes received, no %%EOF marker) - upload aborted"}
 
    # Prepend the lesson date for chronological sorting
    if lesson_date and not filename.startswith(lesson_date):
        filename = f"{lesson_date}-{filename}"
 
    encoded = base64.b64encode(file_bytes).decode("utf-8")
    api_path = f"https://api.github.com/repos/herrpreis/korean-vocab/contents/lessons/{filename}"
 
    # If the file already exists we need its sha to overwrite it
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
        pass
 
    payload = {"message": f"Add lesson file {filename}", "content": encoded}
    if sha:
        payload["sha"] = sha
 
    data = json.dumps(payload).encode("utf-8")
    push_req = urllib.request.Request(
        api_path, data=data, method="PUT",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(push_req) as resp:
            json.loads(resp.read())
    except Exception as e:
        return {"error": f"GitHub push failed: {e}"}
 
    return {
        "success": True,
        "url": BASE_LESSON_URL + filename,
        "filename": filename,
        "bytes_uploaded": len(file_bytes)
    }


 
@mcp.tool()
def upload_lesson_from_url(url: str, filename: str = "", lesson_date: str = "") -> dict:
    """
    Uploads a lesson file to the GitHub lessons/ folder by fetching it from a
    public URL (the server downloads it directly, so the file never has to be
    base64-encoded through the chat). Use this when the lesson already lives at
    a reachable URL. Validates PDFs for completeness before uploading. If
    filename is omitted it is derived from the URL. If lesson_date (YYYY-MM-DD)
    is given and the filename does not already start with it, the date is
    prepended so files sort chronologically. Returns the final GitHub raw URL.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {"error": "GITHUB_TOKEN not set in environment"}

    if not url:
        return {"error": "Provide a url to fetch the lesson file from"}

    # Fetch the file from the given URL
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "korean-vocab-bot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            file_bytes = resp.read()
    except Exception as e:
        return {"error": f"Failed to fetch file from url: {e}"}

    # Derive a filename from the URL if one was not supplied
    # (strip any ?query / #fragment, then take the last path segment)
    if not filename:
        clean = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        filename = clean.rsplit("/", 1)[-1] or "lesson"

    if len(file_bytes) < 100:
        return {"error": f"File data is suspiciously small ({len(file_bytes)} bytes) - likely corrupted or empty"}

    # PDFs must start with the %PDF magic bytes and contain an EOF marker
    if filename.lower().endswith(".pdf"):
        if not file_bytes.startswith(b"%PDF"):
            return {"error": "File does not look like a valid PDF (missing %PDF header)"}
        if b"%%EOF" not in file_bytes[-2048:]:
            return {"error": f"PDF appears truncated ({len(file_bytes)} bytes received, no %%EOF marker) - upload aborted"}

    # Prepend the lesson date for chronological sorting
    if lesson_date and not filename.startswith(lesson_date):
        filename = f"{lesson_date}-{filename}"

    encoded = base64.b64encode(file_bytes).decode("utf-8")
    api_path = f"https://api.github.com/repos/herrpreis/korean-vocab/contents/lessons/{filename}"

    # If the file already exists we need its sha to overwrite it
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
        pass

    payload = {"message": f"Add lesson file {filename}", "content": encoded}
    if sha:
        payload["sha"] = sha

    data = json.dumps(payload).encode("utf-8")
    push_req = urllib.request.Request(
        api_path, data=data, method="PUT",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(push_req) as resp:
            json.loads(resp.read())
    except Exception as e:
        return {"error": f"GitHub push failed: {e}"}

    return {
        "success": True,
        "url": BASE_LESSON_URL + filename,
        "filename": filename,
        "bytes_uploaded": len(file_bytes)
    }
    
@mcp.tool()
def save_lesson(
    lesson_date: str,
    grammar_points: str = "",
    vocab: str = "",
    homework: str = "",
    summary: str = "",
    raw_text: str = "",
    file_urls: str = ""
) -> dict:
    """
    Saves the structured content extracted from a lesson to the database.
    One row per lesson_date (YYYY-MM-DD); saving again for the same date
    updates the existing entry. Fields:
    - grammar_points: comma-separated patterns, e.g. "-(으)니까, -았/었으면 좋겠다"
    - vocab: comma-separated Korean words covered
    - homework: the homework task(s) as text
    - summary: structured summary incl. descriptions of visual exercises
    - raw_text: full extracted transcript text
    - file_urls: comma-separated GitHub raw URLs of the original files
    """
    if not lesson_date:
        return {"error": "lesson_date (YYYY-MM-DD) is required"}
 
    db = get_db()
    db.execute("""
        INSERT INTO lessons
            (lesson_date, grammar_points, vocab, homework, summary, raw_text, file_urls, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lesson_date) DO UPDATE SET
            grammar_points = excluded.grammar_points,
            vocab = excluded.vocab,
            homework = excluded.homework,
            summary = excluded.summary,
            raw_text = excluded.raw_text,
            file_urls = excluded.file_urls
    """, (
        lesson_date, grammar_points, vocab, homework, summary, raw_text,
        file_urls, datetime.now(timezone.utc).isoformat()
    ))
    db.commit()
 
    row = db.execute(
        "SELECT id FROM lessons WHERE lesson_date = ?", (lesson_date,)
    ).fetchone()
    return {"success": True, "id": row["id"], "lesson_date": lesson_date}
 
 
@mcp.tool()
def get_lessons(lesson_date: str = "", query: str = "", limit: int = 5) -> list[dict]:
    """
    Retrieves lessons from the database.
    - No arguments: the most recent lessons (overview without raw_text)
    - lesson_date "YYYY-MM-DD": that single lesson in full, incl. raw_text
    - lesson_date "latest": the most recent lesson in full
    - query: keyword search across grammar, vocab, homework and summary
      (overview without raw_text)
    """
    db = get_db()
 
    if lesson_date:
        if lesson_date == "latest":
            row = db.execute(
                "SELECT * FROM lessons ORDER BY lesson_date DESC LIMIT 1"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM lessons WHERE lesson_date = ?", (lesson_date,)
            ).fetchone()
        return [dict(row)] if row else []
 
    if query:
        like = f"%{query}%"
        rows = db.execute("""
            SELECT id, lesson_date, grammar_points, vocab, homework, summary, file_urls
            FROM lessons
            WHERE grammar_points LIKE ? OR vocab LIKE ? OR homework LIKE ? OR summary LIKE ?
            ORDER BY lesson_date DESC LIMIT ?
        """, (like, like, like, like, limit)).fetchall()
        return [dict(r) for r in rows]
 
    rows = db.execute("""
        SELECT id, lesson_date, grammar_points, vocab, homework, summary, file_urls
        FROM lessons
        ORDER BY lesson_date DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
