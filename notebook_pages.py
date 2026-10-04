"""Web pages for the Korean Notebook, served by the same Railway service as the bot.

Routes (added with FastMCP's custom_route, so the bot's own /sse endpoint is untouched):
  /notebook            the notebook page, built from the live database on every visit
  /notebook/img/<name> small mnemonic-image thumbnails (stored in notebook_thumbs.json)

Optional privacy: set the variable NOTEBOOK_KEY on Railway and the page only opens with
?key=<that value>, e.g. /notebook?key=abc123 (without it the page answers "Not found").

Files this module reads (all in the same folder as server.py):
  notebook.html            the page itself (a template; the deck is filled in per visit)
  notebook_overrides.json  small fixes for known-bad cards (see _apply_ops)
  notebook_thumbs.json     the thumbnails, base64-encoded
"""
import base64
import hmac
import html
import json
import os
import re
from urllib.parse import quote

from starlette.responses import HTMLResponse, PlainTextResponse, Response

HERE = os.path.dirname(os.path.abspath(__file__))

COLUMNS = ("id", "korean", "english", "type", "topic", "example", "usage", "formation",
           "sentences", "translations", "notes", "image_url", "created_at")
TEXT_FIELDS = tuple(c for c in COLUMNS if c not in ("id", "type"))
MAX_TOPIC = 40            # longer "topics" are really descriptions; show no tag instead

_ZERO_WIDTH = re.compile("[\u200b-\u200d\u2060\ufeff]")
_PUNCT_ONLY = re.compile(r"""^[\s"'“”‘’.,;:!?\-–—…·]*$""")


# ---------- cleaning -------------------------------------------------------------

def _clean(s):
    """Undo import damage: HTML entities (&amp;), escaped quotes, invisible characters."""
    s = html.unescape(str(s))
    s = s.replace("\\'", "'").replace('\\"', '"')
    return _ZERO_WIDTH.sub("", s)


def _unquote(s):
    """Drop a quote pair wrapped around a whole value, or one left dangling.
    Anything with more quote marks is left alone: it may be real content."""
    s = s.strip()
    n = s.count('"')
    if n == 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1].strip()
    if n == 1:
        return s.replace('"', "").strip()
    return s


def _norm(s):
    return _unquote(_clean(s)).strip()


def _apply_ops(card, ops):
    """Apply fixes for known-bad cards. Each op is one of:
         {"field", "from", "to"}            set field to "to" if it currently equals "from"
                                            (omit "from" to always set it)
         {"field", "to", "only_if_empty"}   set field only if it is empty
         {"field", "replace": [old, new]}   replace text inside the field
         {"hide": true, "field", "from"}    hide the card if the field still equals "from"
       A fix switches itself off once the card in the deck no longer matches "from",
       so correcting a card in the deck itself always wins."""
    for op in ops:
        field = op.get("field", "")
        cur = card.get(field, "")
        if "replace" in op:
            old, new = op["replace"]
            if old in cur:
                card[field] = cur.replace(old, new)
        elif op.get("hide"):
            if "from" not in op or _norm(cur) == _norm(op["from"]):
                card["_hidden"] = True
        elif "to" in op:
            if op.get("only_if_empty"):
                if not cur.strip():
                    card[field] = op["to"]
            elif "from" not in op or _norm(cur) == _norm(op["from"]):
                card[field] = op["to"]


def _image_fields(url, thumbs):
    """-> (thumbnail address, full-size address)"""
    if not url:
        return "", ""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name in thumbs:
        return "/notebook/img/" + quote(name), url
    return url, url


def build_cards(rows, overrides, thumbs):
    """Turn database rows into the three card lists the page shows."""
    out = {"grammar": [], "vocab": [], "phrase": []}
    for row in rows:
        card = {k: (row[k] if row[k] is not None else "") for k in COLUMNS}
        card["id"] = int(card["id"])
        for k in TEXT_FIELDS:
            card[k] = _clean(card[k])
        _apply_ops(card, overrides.get(str(card["id"]), []))
        for k in ("korean", "english", "topic"):
            card[k] = _unquote(card[k]).strip()
        if card.pop("_hidden", False):
            continue
        if _PUNCT_ONLY.match(card["korean"]) or _PUNCT_ONLY.match(card["english"]):
            continue                                   # leftover fragment of a broken import row
        if len(card["topic"]) > MAX_TOPIC:
            card["topic"] = ""
        card["image_url"], card["image_full"] = _image_fields(card["image_url"], thumbs)
        category = card["type"] if card["type"] in ("grammar", "phrase") else "vocab"
        out[category].append(card)
    return out


# ---------- web routes -----------------------------------------------------------

def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def register_notebook(mcp, get_db):
    template = _read("notebook.html")
    if "__NOTEBOOK_DATA__" not in template:
        raise RuntimeError("notebook.html has no __NOTEBOOK_DATA__ placeholder")
    overrides = json.loads(_read("notebook_overrides.json"))
    thumbs = {
        name: {"mime": t["mime"], "data": base64.b64decode(t["b64"])}
        for name, t in json.loads(_read("notebook_thumbs.json")).items()
    }
    select = "SELECT " + ", ".join(COLUMNS) + " FROM words ORDER BY id"

    def allowed(request):
        key = os.environ.get("NOTEBOOK_KEY", "")
        return not key or hmac.compare_digest(request.query_params.get("key", ""), key)

    @mcp.custom_route("/notebook", methods=["GET"])
    async def notebook_page(request):
        if not allowed(request):
            return PlainTextResponse("Not found", status_code=404)
        try:
            db = get_db()
            try:
                rows = db.execute(select).fetchall()
            finally:
                db.close()
            cards = build_cards(rows, overrides, thumbs)
        except Exception as e:                         # show the reason instead of a blank page
            return PlainTextResponse(f"The notebook could not read the deck: {e}", status_code=500)
        payload = json.dumps(cards, ensure_ascii=False).replace("<", "\\u003c")
        return HTMLResponse(template.replace("__NOTEBOOK_DATA__", payload, 1),
                            headers={"Cache-Control": "no-cache"})

    @mcp.custom_route("/notebook/img/{name}", methods=["GET"])
    async def notebook_image(request):
        thumb = thumbs.get(request.path_params.get("name", ""))
        if not thumb:
            return PlainTextResponse("Not found", status_code=404)
        return Response(thumb["data"], media_type=thumb["mime"],
                        headers={"Cache-Control": "public, max-age=86400"})
