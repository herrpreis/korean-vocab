"""Meal Planner service.

FastAPI for REST endpoints and rendered HTML pages. FastMCP mounted
alongside so Claude gets the same functions as tools. Browser agents and
Comet use the plain HTTP routes.
"""

import html
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi import Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

import auth
import filters
import backup
import catalogue
import conversions
import db
import images
import pages
import picker
import quantities
import receipt
import retailers

# Built before the app so their lifespans can be nested into it. FastMCP's
# HTTP transport keeps a task group that is only started by its lifespan —
# without this, every MCP request fails with "task group is not initialized".
try:
    import mcp_server
    _mcp_apps = {
        "/mcp": mcp_server.mcp.http_app(path="/", transport="http"),
        "/sse": mcp_server.mcp.http_app(path="/", transport="sse"),
    }
except Exception as _exc:                      # never block the web app
    mcp_server = None
    _mcp_apps = {}
    print(f"[mcp] unavailable: {_exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate()
    backup.start()
    async with AsyncExitStack() as stack:
        for sub in _mcp_apps.values():
            await stack.enter_async_context(sub.lifespan(sub))
        yield


app = FastAPI(title="Meal Planner", lifespan=lifespan)
app.middleware("http")(auth.require_login)


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/"):
    return HTMLResponse(pages.login_html(next))


@app.post("/login")
def login_submit(passcode: str = Form(...), next: str = Form("/")):
    if passcode != auth.PASSCODE:
        return HTMLResponse(pages.login_html(next, error=True),
                            status_code=401)
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_cookie_value(),
                    max_age=auth.COOKIE_MAX_AGE, httponly=True,
                    samesite="lax")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/health")
def health():
    """Reports whether the database is on a real mounted volume.

    DB_PATH being set proves nothing — if the volume isn't mounted, the app
    writes to the container filesystem, works perfectly, and is wiped on
    every redeploy. persistent=false means data will be lost.
    """
    import shutil
    from pathlib import Path

    counts = {}
    for table in ("recipes", "ingredients", "products", "weeks"):
        row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
        counts[table] = row["n"]

    db_file = Path(db.DB_PATH)
    mount_dir = db_file.parent

    # A mount point has a different device id from its parent directory.
    persistent = False
    try:
        persistent = os.stat(mount_dir).st_dev != os.stat(mount_dir.parent).st_dev
    except OSError:
        pass

    info = {
        "ok": True,
        "db_path": db.DB_PATH,
        "counts": counts,
        "persistent": persistent,
        "mount_dir": str(mount_dir),
        "db_exists": db_file.exists(),
        "db_size_bytes": db_file.stat().st_size if db_file.exists() else 0,
        "db_modified": (
            datetime.fromtimestamp(db_file.stat().st_mtime).isoformat()
            if db_file.exists() else None),
        "dir_contents": sorted(p.name for p in mount_dir.iterdir())[:20]
                        if mount_dir.exists() else [],
    }

    try:
        usage = shutil.disk_usage(mount_dir)
        info["volume_total_mb"] = round(usage.total / 1024 / 1024)
        info["volume_free_mb"] = round(usage.free / 1024 / 1024)
    except OSError:
        pass

    if not persistent:
        info["warning"] = (
            f"{mount_dir} is NOT a mounted volume. Data will be lost on the "
            "next redeploy. Attach a Railway volume to this service with "
            f"mount path {mount_dir}.")

    return info



# ---------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------
# Data travels to Railway the same way code does — through the repo.
# Drop a JSON file in seed/, push, then restore from the admin page.

SEED_DIR = Path(__file__).parent / "seed"


@app.get("/backup")
def backup_dump():
    """Full JSON dump, for saving by hand before anything risky."""
    return backup.dump()


@app.post("/backup/commit")
def backup_commit():
    """Write the dump to GitHub now, rather than waiting for the schedule."""
    return backup.commit_backup()


@app.get("/backup/status")
def backup_status():
    return {"configured": backup.configured(),
            "repo": backup.GITHUB_REPO,
            "path": backup.BACKUP_PATH,
            "last_backup": backup.get_meta("last_backup"),
            "due": backup.due()}


@app.get("/seed")
def list_seed_files():
    """What's available to restore from, in the repo."""
    if not SEED_DIR.exists():
        return {"files": [], "note": "No seed/ directory in the repo"}
    return {"files": [
        {"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)}
        for p in sorted(SEED_DIR.glob("*.json"))]}


@app.post("/restore")
def restore(filename: str):
    """Load recipes from a file in seed/.

    The file is the same shape as POST /recipes: {"recipes": [...]}.
    Duplicate source_urls are skipped, so this is safe to run twice.
    """
    path = SEED_DIR / Path(filename).name      # no directory traversal
    if not path.exists():
        raise HTTPException(404, f"{filename} not found in seed/")

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"{filename} is not valid JSON: {exc}")

    recipes = payload.get("recipes", payload if isinstance(payload, list) else None)
    if recipes is None:
        raise HTTPException(400, "Expected {\"recipes\": [...]} or a bare list")

    return create_recipes(RecipeBatchIn(recipes=recipes))


def recipe_costs(retailer, servings):
    """Portion cost per recipe: only what each recipe actually uses.

    Not basket cost — charging a recipe for a whole 1kg bag of onions would
    make everything look absurd and would double-count anything two recipes
    share. Cheapest comparable product per ingredient, pro-rata by quantity.

    Built from three bulk queries rather than one per ingredient, so the
    picker page stays fast with a few hundred recipe lines.
    """
    lines = db.query(
        "SELECT ri.recipe_id, ri.qty_2p, ri.qty_3p, ri.qty_4p, ri.qty_base, "
        "       ri.base_servings, ri.unit, i.id AS ing_id, i.name, "
        "       i.category, i.canonical_unit, i.unit_conversions, "
        "       i.assume_stocked "
        "FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id")

    products = {}
    for p in db.query(
            "SELECT ingredient_id, price, pack_size, pack_unit FROM products "
            "WHERE retailer = ? AND delisted = 0 AND price IS NOT NULL "
            "AND pack_size > 0", (retailer,)):
        products.setdefault(p["ingredient_id"], []).append(p)

    out = {}
    for line in lines:
        entry = out.setdefault(line["recipe_id"],
                               {"cost": 0.0, "unpriced": []})
        if line["category"] == "never_buy" or line["assume_stocked"]:
            continue

        qty = quantities.for_servings(line, servings)
        all_products = products.get(line["ing_id"], [])

        reason = None
        needed = None
        usable = []

        if qty is None:
            # A missing quantity is a different failure from a missing
            # conversion — this recipe line has nothing recorded to convert
            # in the first place. Silently skipping it used to under-cost
            # the recipe with no visible sign anything was left out.
            reason = f"no quantity recorded for {servings} servings"
        else:
            needed, _ = quantities.to_canonical(qty, line["unit"], line)
            usable = [p for p in all_products if quantities.compatible(p, line)]
            if needed is None:
                canonical = line["canonical_unit"] or "g"
                reason = f'no conversion from "{line["unit"]}" to {canonical}'
            elif not all_products:
                reason = "no product at this retailer"
            elif not usable:
                units = ", ".join(sorted({(p["pack_unit"] or "?")
                                          for p in all_products}))
                reason = (f'pack sold in {units}, recipe measures in '
                          f'{line["canonical_unit"]}')

        if reason:
            entry["unpriced"].append(line["name"])
            entry.setdefault("reasons", []).append(
                {"ingredient": line["name"], "reason": reason,
                 "ingredient_id": line["ing_id"], "unit": line["unit"],
                 "canonical_unit": line["canonical_unit"]})
            continue

        sized = [(p, quantities.pack_in_canonical(p, line)) for p in usable]
        cheapest = min(((p, sz) for p, sz in sized if sz),
                       key=lambda t: t[0]["price"] / t[1])
        entry["cost"] += cheapest[0]["price"] * needed / cheapest[1]

    for entry in out.values():
        entry["cost"] = round(entry["cost"], 2)
        entry["per_portion"] = round(entry["cost"] / servings, 2)
    return out


@app.get("/ingredients/missing-conversions")
def missing_conversions():
    """Every recipe unit that can't be converted, with a suggested value.

    Retailer-independent: a conversion lives on the ingredient, so fixing
    "1 tbsp of harissa = 15g" once fixes it for every recipe and every shop.
    Staples and never-buy items are skipped since they're never priced.
    """
    rows = db.query(
        "SELECT i.id AS ingredient_id, i.name, i.canonical_unit, "
        "       i.unit_conversions, ri.unit, "
        "       COUNT(DISTINCT ri.recipe_id) AS recipes_affected "
        "FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE i.category != 'never_buy' AND i.assume_stocked = 0 "
        "GROUP BY i.id, ri.unit ORDER BY recipes_affected DESC, i.name")

    out = []
    for r in rows:
        probe = {"canonical_unit": r["canonical_unit"],
                 "unit_conversions": r["unit_conversions"]}
        value, _ = quantities.to_canonical(1, r["unit"], probe)
        if value is not None:
            continue
        guess, confidence, reason = conversions.suggest(
            r["name"], r["unit"], r["canonical_unit"])
        out.append({
            "ingredient_id": r["ingredient_id"], "name": r["name"],
            "unit": r["unit"], "canonical_unit": r["canonical_unit"],
            "recipes_affected": r["recipes_affected"],
            "suggestion": guess, "confidence": confidence,
            "reason": reason})
    return {"count": len(out), "items": out}


@app.get("/admin/conversions", response_class=HTMLResponse)
def conversions_page():
    """Review and accept suggested conversions in one go."""
    data = missing_conversions()
    order = {"high": 0, "medium": 1, "low": 2, "none": 3}
    items = sorted(data["items"],
                   key=lambda i: (order[i["confidence"]],
                                  -i["recipes_affected"], i["name"]))

    rows = ""
    for n, i in enumerate(items):
        ticked = i["confidence"] in ("high", "medium") and i["suggestion"]
        value = "" if i["suggestion"] is None else f'{i["suggestion"]:g}'
        rows += (
            f'<tr class="c-{i["confidence"]}">'
            f'<td><input type="checkbox" class="pick" data-n="{n}"'
            f'{" checked" if ticked else ""}></td>'
            f'<td><strong>{i["name"]}</strong>'
            f'<div class="sub">{i["recipes_affected"]} recipe(s)</div></td>'
            f'<td class="nowrap">1 {i["unit"]} =</td>'
            f'<td class="nowrap"><input id="v{n}" value="{value}" size="5" '
            f'data-ing="{i["ingredient_id"]}" data-unit="{i["unit"]}" '
            f'oninput="tick({n})"> {i["canonical_unit"]}</td>'
            f'<td><span class="conf">{i["confidence"]}</span>'
            f'<div class="sub">{i["reason"]}</div></td></tr>')

    counts = {}
    for i in items:
        counts[i["confidence"]] = counts.get(i["confidence"], 0) + 1
    summary = " &middot; ".join(f'{counts[k]} {k}' for k in
                                ("high", "medium", "low", "none")
                                if counts.get(k))

    body = f"""
<h1>Unit conversions</h1>
<p class="counts">{data['count']} conversion(s) missing &middot; {summary}</p>
<p class="sub">High and medium confidence are ticked for you. Low confidence
   is a generic default worth a look. Nothing is suggested for sachets &mdash;
   they range from half a gram to fifty, so check the packet. Edit any value,
   tick what you want, then save. Each fix applies to every recipe using that
   ingredient.</p>
<div class="bar">
  <button onclick="saveAll()">Save ticked</button>
  <button class="quiet" onclick="tickAll(true)">Tick all with a value</button>
  <button class="quiet" onclick="tickAll(false)">Untick all</button>
</div>
<table class="conv">{rows or '<tr><td class="sub">Nothing missing.</td></tr>'}</table>
"""
    script = """
function tick(n) {
  const cb = document.querySelector('.pick[data-n="' + n + '"]');
  cb.checked = document.getElementById('v' + n).value.trim() !== '';
}
function tickAll(on) {
  document.querySelectorAll('.pick').forEach(cb => {
    const v = document.getElementById('v' + cb.dataset.n).value.trim();
    cb.checked = on && v !== '';
  });
}
async function saveAll() {
  const byIngredient = {};
  document.querySelectorAll('.pick:checked').forEach(cb => {
    const el = document.getElementById('v' + cb.dataset.n);
    const value = parseFloat(el.value);
    if (isNaN(value) || value <= 0) return;
    const id = el.dataset.ing;
    byIngredient[id] = byIngredient[id] || {};
    byIngredient[id][el.dataset.unit] = value;
  });
  const fixes = Object.entries(byIngredient).map(([id, conv]) =>
    ({ingredient_id: +id, unit_conversions: conv}));
  if (!fixes.length) { out.textContent = 'Nothing ticked.'; return; }
  out.textContent = 'Saving ' + fixes.length + ' ingredient(s)...';
  const r = await fetch('/ingredients/fix-units', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({fixes: fixes})});
  const j = await r.json();
  out.textContent = 'Saved ' + j.updated.length + ' ingredient(s).';
  setTimeout(() => location.reload(), 900);
}
"""
    style = """<style>
 .conv td{vertical-align:top}
 .conv .nowrap{white-space:nowrap}
 .conv .conf{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;
             font-weight:700}
 .c-high .conf{color:var(--accent)}
 .c-medium .conf{color:var(--ink-soft)}
 .c-low .conf{color:var(--tip)}
 .c-none .conf{color:var(--caution)}
 .conv input[size]{width:4.5rem}
</style>"""
    return HTMLResponse(pages.shell("Unit conversions", "admin",
                                    style + body, script,
                                    show_logout=auth.configured()))


@app.get("/pricing/gaps")
def pricing_gaps(retailer: str = "ocado", servings: int = 4):
    """Why recipes can't be priced, grouped by cause.

    "Unpriced" covers three different problems with different fixes: a
    missing unit conversion, no product at all, or a pack measured in units
    that can't be compared to the recipe. Lumping them together makes it
    look worse and less fixable than it is.
    """
    costs = recipe_costs(retailer, servings)

    by_ingredient = {}
    for recipe_id, entry in costs.items():
        for item in entry.get("reasons", []):
            key = (item["ingredient"], item["reason"])
            row = by_ingredient.setdefault(key, {
                "ingredient": item["ingredient"],
                "ingredient_id": item["ingredient_id"],
                "recipe_unit": item["unit"],
                "canonical_unit": item["canonical_unit"],
                "reason": item["reason"],
                "recipes_affected": 0})
            row["recipes_affected"] += 1

    rows = sorted(by_ingredient.values(),
                  key=lambda r: -r["recipes_affected"])

    buckets = {}
    for row in rows:
        r = row["reason"]
        kind = ("missing quantity" if "no quantity recorded" in r
                else "missing conversion" if "no conversion" in r
                else "no product" if "no product" in r
                else "pack unit mismatch")
        buckets[kind] = buckets.get(kind, 0) + row["recipes_affected"]

    fully_priced = sum(1 for e in costs.values() if not e["unpriced"])
    return {
        "retailer": retailer,
        "recipes_fully_priced": fully_priced,
        "recipes_total": len(costs),
        "by_cause": buckets,
        "ingredients": rows,
    }


@app.get("/recipes/{recipe_id}/ingredients")
def recipe_ingredient_detail(recipe_id: int, retailer: str = "ocado",
                             servings: int = 4):
    """Every ingredient in a recipe with its cost, or why it has none."""
    recipe = db.query_one("SELECT title FROM recipes WHERE id = ?",
                          (recipe_id,))
    if not recipe:
        raise HTTPException(404, "Recipe not found")

    rows = db.query(
        "SELECT ri.*, i.id AS ing_id, i.name, i.category, i.canonical_unit, "
        "       i.unit_conversions, i.assume_stocked "
        "FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE ri.recipe_id = ? ORDER BY i.category, i.name", (recipe_id,))

    items, total = [], 0.0
    for line in rows:
        qty = quantities.for_servings(line, servings)
        tier = quantities.TIERS.get(servings)
        conversions = line["unit_conversions"] or {}

        # The box always shows and edits the real amount for *this* serving
        # count — never the raw qty_base, which belongs to whatever serving
        # size the source recipe was written for and can silently differ
        # from what's on screen (an Ocado "serves 2" base doubled to 400g
        # at 4 servings looks like "200" here if the raw column is shown,
        # and re-saving 200 would leave the 400g untouched). If this
        # serving count has a tier (2p/3p/4p), saving writes a real value
        # into that exact tier rather than rescaling qty_base, the same
        # "store a real figure, don't rescale" rule used everywhere else.
        qty_field = tier if tier else "qty_base"
        qty_display = (line[tier] if tier and line[tier] is not None
                       else qty)

        item = {"name": line["name"], "category": line["category"],
                "needed": f'{qty:g} {line["unit"]}' if qty else None,
                "prep_note": line["prep_note"],
                # For inline editing: which stored field this serving size
                # maps to, and the per-unit weight if one is recorded.
                "row_id": line["id"],
                "ingredient_id": line["ing_id"],
                "qty": qty_display,
                "qty_field": qty_field,
                "unit": line["unit"],
                "canonical_unit": line["canonical_unit"],
                "conversion": conversions.get(line["unit"])}

        if line["category"] == "never_buy":
            item["note"] = "not bought"
        elif line["assume_stocked"]:
            item["note"] = "staple, always in stock"
        elif qty is None:
            # Distinct from a missing conversion: this recipe line has no
            # quantity recorded at all for this serving count, so there is
            # nothing to convert yet. Reporting it as "no conversion from
            # unit to unit" (possible when the two happen to be the same
            # word) is misleading — the units were never even compared.
            item["problem"] = f"no quantity recorded for {servings} servings"
        else:
            needed, _ = quantities.to_canonical(qty, line["unit"], line)
            candidates = db.query(
                "SELECT * FROM products WHERE ingredient_id = ? "
                "AND retailer = ? AND delisted = 0 AND price IS NOT NULL "
                "AND pack_size > 0", (line["ing_id"], retailer))
            usable = [p for p in candidates
                      if quantities.compatible(p, line)]
            if needed is None:
                item["problem"] = (f'no conversion from "{line["unit"]}" to '
                                   f'{line["canonical_unit"]}')
            elif not candidates:
                item["problem"] = f"no {retailer} product"
            elif not usable:
                item["problem"] = "pack unit can't be compared"
            else:
                sized = [(p, quantities.pack_in_canonical(p, line))
                         for p in usable]
                cheapest_pair = min(((p, sz) for p, sz in sized if sz),
                                    key=lambda t: t[0]["price"] / t[1])
                cheapest, size_c = cheapest_pair
                cost = cheapest["price"] * needed / size_c
                total += cost
                item["cost"] = round(cost, 2)
                item["product"] = cheapest["name"]
                item["product_url"] = cheapest["url"]
                item["pack"] = (f'{cheapest["pack_size"]:g}'
                                f'{cheapest["pack_unit"] or ""} '
                                f'@ £{cheapest["price"]:.2f}')
        items.append(item)

    meta = db.query_one("SELECT baby_friendly FROM recipes WHERE id = ?",
                        (recipe_id,))
    return {"recipe": recipe["title"], "recipe_id": recipe_id,
            "retailer": retailer, "servings": servings,
            "baby_friendly": meta["baby_friendly"],
            "total": round(total, 2),
            "per_portion": round(total / servings, 2) if servings else 0,
            "ingredients": items}


@app.get("/", response_class=HTMLResponse)
def home(retailer: str = "ocado", servings: int = 4, sort: str = "recent",
         protein: str = "", baby: str = "", carb: str = "", cuisine: str = "",
         maxtime: str = "", spice: str = "", tag: str = "", cooked: str = "",
         q: str = "", without: str = ""):
    """This week: choose recipes, open the list. The page used weekly.

    Costs follow the selected retailer — a week is ordered from one shop, so
    showing whichever is cheapest per recipe would be a total you could
    never actually spend.

    Every filter is optional and they all combine.
    """
    h = health()
    everything = db.query(
        "SELECT id, title, protein, time_max, hero_image, last_cooked, "
        "source, baby_friendly, baby_note, cuisine, tags FROM recipes "
        "WHERE status = 'active'")
    ing_rows = db.query(
        "SELECT ri.recipe_id, i.name, i.carb FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id WHERE ri.optional = 0")
    recipes = [r for r in everything
               if (not protein or r["protein"] == protein)
               and filters.baby_matches(baby, r["baby_friendly"])]
    params = {"carb": carb, "cuisine": cuisine, "maxtime": maxtime,
              "spice": spice, "tag": tag, "cooked": cooked, "q": q,
              "without": without}
    recipes, _carbs = filters.apply(recipes, ing_rows, params)

    def opts(pairs, current):
        return "".join(
            f'<option value="{html.escape(v)}"'
            f'{" selected" if v == current else ""}>{html.escape(label)}'
            f'</option>' for v, label in pairs)

    proteins = sorted({r["protein"] for r in everything if r["protein"]})
    protein_options = opts([("", "Any protein")] + [(p, p) for p in proteins],
                           protein)
    baby_options = opts(filters.BABY_OPTIONS, baby)
    carb_options = opts([("", "Any carb")]
                        + [(k, v) for k, v in filters.CARBS.items()]
                        + [("none", "No carb")], carb)
    cuisines = sorted({r["cuisine"] for r in everything if r["cuisine"]})
    cuisine_options = opts([("", "Any cuisine")]
                           + [(c, c.title()) for c in cuisines], cuisine)
    tag_counts = {}
    for r in everything:
        for t in filters._tags(r):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tag_list = sorted((t for t, n in tag_counts.items() if n >= 3),
                      key=lambda t: t.lower())
    tag_options = opts([("", "Any tag")] + [(t, t) for t in tag_list], tag)
    spice_options = opts(filters.SPICE_OPTIONS, spice)
    time_options = opts(filters.TIME_OPTIONS, maxtime)
    cooked_options = opts(filters.COOKED_OPTIONS, cooked)

    more_active = sum(1 for v in (cuisine, maxtime, spice, tag, cooked, q,
                                  without) if v)
    any_active = more_active + sum(1 for v in (protein, carb, baby) if v)
    more_label = "More filters" + (f" ({more_active})" if more_active else "")
    clear = ('<a href="#" class="clearf" onclick="clearFilters();return false">'
             'Clear filters</a>' if any_active else "")

    costs = recipe_costs(retailer, servings)

    sort_options = "".join(
        f'<option value="{key}"{" selected" if key == sort else ""}>{label}'
        f'</option>' for key, label in (
            ("recent", "Longest since cooked"),
            ("price", "Cheapest first"),
            ("time", "Quickest first"),
            ("title", "A–Z")))

    body = f"""
<h1>All recipes</h1>
<p class="counts">{h['counts']['recipes']} recipes &middot;
   <a href="/ingredients/browse">{h['counts']['ingredients']} ingredients</a> &middot;
   {h['counts']['products']} products</p>

<div class="bar">
  <select id="ret" onchange="reprice()" title="Which shop to price against">
    {"".join(f'<option value="{r}"{" selected" if r == retailer else ""}>'
             f'{html.escape(n)}</option>'
             for r, n in retailers.RETAILERS.items())}</select>
  <select id="srv" onchange="reprice()" title="Servings per recipe">
    {"".join(f'<option value="{n}"{" selected" if n == servings else ""}>'
             f'{n} servings</option>' for n in (2, 3, 4))}</select>
  <select id="protein" data-f="protein" onchange="reprice()"
    title="Filter by protein">{protein_options}</select>
  <select id="carb" data-f="carb" onchange="reprice()"
    title="Filter by carb">{carb_options}</select>
  <select id="baby" data-f="baby" onchange="reprice()"
    title="Filter by whether the recipe suits the baby">{baby_options}</select>
  <select id="sort" onchange="reprice()" title="Sort order">{sort_options}</select>
  <button class="quiet morebtn{" open" if more_active else ""}" id="morebtn"
    onclick="toggleMore()">{more_label} <span class="chev">&#9660;</span></button>
  {clear}
</div>
<div class="bar more" id="more"{"" if more_active else ' style="display:none"'}>
  <select data-f="cuisine" onchange="reprice()">{cuisine_options}</select>
  <select data-f="maxtime" onchange="reprice()">{time_options}</select>
  <select data-f="spice" onchange="reprice()">{spice_options}</select>
  <select data-f="tag" onchange="reprice()">{tag_options}</select>
  <select data-f="cooked" onchange="reprice()">{cooked_options}</select>
  <input type="search" data-f="q" placeholder="Name or ingredient"
    value="{html.escape(q)}" onchange="reprice()">
  <input type="search" data-f="without" placeholder="Without (e.g. mushrooms)"
    value="{html.escape(without)}" onchange="reprice()">
</div>
<p class="sub">Prices are per portion at {html.escape(retailers.name(retailer))}.
   {len(recipes)} recipe(s) shown. Tap recipes to select them.</p>

{pages.recipe_grid_html(recipes, servings, costs, retailer, sort)}

<div id="ov" onclick="if(event.target.id==='ov')closePeek()">
  <div id="ovbox"></div>
</div>

<div class="actionbar">
  <div id="selpanel" class="selpanel"></div>
  <div class="inner">
  <button class="count" id="selcount" onclick="toggleExpand()">
    <span id="selcounttext">Nothing selected</span>
    <span class="chev">&#9650;</span>
  </button>
  <span class="suggest">
    <select id="cnt" title="How many recipes to suggest">
      <option value="2">2 recipes</option>
      <option value="3" selected>3 recipes</option>
      <option value="4">4 recipes</option>
      <option value="5">5 recipes</option></select>
    <button class="quiet" onclick="askOrder('suggest')">Suggest a week</button>
  </span>
  <button class="primary" id="create" onclick="askOrder('make')" disabled>
    Create from selected</button>
</div></div>
"""

    script = """
async function peek(id, e) {
  e.stopPropagation();
  const ov = document.getElementById('ov'), box = document.getElementById('ovbox');
  box.innerHTML = '<p>Loading...</p>';
  ov.classList.add('on');
  const ret = document.getElementById('ret').value;
  const srv = document.getElementById('srv').value;
  const r = await fetch('/recipes/' + id + '/ingredients?retailer=' + ret
                        + '&servings=' + srv);
  const d = await r.json();
  window._ing = {};
  const rows = d.ingredients.map(i => {
    if (i.row_id != null) window._ing[i.row_id] = i;
    const right = i.cost != null ? ('\u00a3' + i.cost.toFixed(2))
      : (i.problem ? '<span class="prob">' + i.problem + '</span>'
                   : '<span class="quietnote">' + (i.note || '') + '</span>');
    const sub = i.product ? '<div class="quietnote">' + i.product + ' \u00b7 '
                            + i.pack + '</div>' : '';
    let qtyLine;
    if (i.row_id == null) {
      qtyLine = '<div class="quietnote">' + (i.needed || '') + '</div>';
    } else {
      const conv = (i.unit && i.canonical_unit && i.unit !== i.canonical_unit)
        ? '<span class="quietnote">1 ' + i.unit + ' = </span>'
          + '<input id="cv' + i.row_id + '" value="'
          + (i.conversion == null ? '' : i.conversion) + '" size="4">'
          + '<span class="quietnote">' + i.canonical_unit + '</span>'
        : '';
      qtyLine = '<div class="qedit">'
        + '<input id="q' + i.row_id + '" value="'
        + (i.qty == null ? '' : i.qty) + '" size="4">'
        + '<input id="u' + i.row_id + '" value="'
        + (i.unit || '') + '" size="5">'
        + conv
        + '<button class="quiet" onclick="saveQty(' + id + ',' + i.row_id
        + ')">Save</button></div>';
    }
    const nameHtml = i.product_url
      ? '<a class="ingname" href="' + i.product_url + '" target="_blank" '
        + 'rel="noopener">' + i.name + '</a>'
      : i.name;
    return '<tr><td>' + nameHtml
      + (i.prep_note ? ' <span class="quietnote">' + i.prep_note + '</span>' : '')
      + qtyLine + sub
      + '</td><td>' + right + '</td></tr>';
  }).join('');
  box.innerHTML = '<h3>' + d.recipe + '</h3>'
    + '<p class="quietnote">' + d.servings + ' servings at ' + d.retailer
    + ' \u00b7 \u00a3' + d.total.toFixed(2) + ' total \u00b7 \u00a3'
    + d.per_portion.toFixed(2) + ' a portion</p>'
    + '<p class="editnote">Changes here are saved for ' + d.servings
    + ' servings specifically \u2014 they also update the recipe card at '
    + d.servings + ' servings. Other serving counts are separate and '
    + 'unaffected.</p>'
    + '<table>' + rows + '</table>'
    + '<p style="margin-top:1.2rem"><button class="quiet" '
    + 'onclick="closePeek()">Close</button> '
    + '<a href="/recipes/' + id + '/card?servings=' + srv + '" '
    + 'target="_blank">Open recipe card</a></p>';
}
function openCard(id, e) {
  e.stopPropagation();
  const srv = document.getElementById('s' + id).value;
  window.open('/recipes/' + id + '/card?servings=' + srv, '_blank');
}
async function saveQty(recipeId, rowId) {
  const info = window._ing[rowId];
  if (!info) return;
  const qty = parseFloat(document.getElementById('q' + rowId).value);
  const unit = document.getElementById('u' + rowId).value.trim();
  const cvEl = document.getElementById('cv' + rowId);

  const body = {unit: unit};
  if (!isNaN(qty)) body[info.qty_field] = qty;
  await fetch('/recipe-ingredients/' + rowId, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});

  // A per-unit weight lives on the ingredient, so it fixes every recipe
  // using it, not just this line.
  if (cvEl && cvEl.value.trim() !== '') {
    const conv = {};
    conv[unit] = parseFloat(cvEl.value);
    await fetch('/ingredients/fix-units', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({fixes: [{ingredient_id: info.ingredient_id,
                                     unit_conversions: conv}]})});
  }
  peek(recipeId, {stopPropagation(){}});
}
async function setBaby(id, value) {
  await fetch('/recipes/' + id, {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({baby_friendly: value})});
  peek(id, {stopPropagation(){}});
}
function closePeek() { document.getElementById('ov').classList.remove('on'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePeek(); });
function reprice(clear) {
  const p = new URLSearchParams();
  p.set('retailer', document.getElementById('ret').value);
  p.set('servings', document.getElementById('srv').value);
  p.set('sort', document.getElementById('sort').value);
  if (!clear) document.querySelectorAll('[data-f]').forEach(el => {
    if (el.value.trim() !== '') p.set(el.dataset.f, el.value.trim());
  });
  location.href = '/?' + p.toString();
}
function clearFilters() { reprice(true); }
function toggleMore() {
  const m = document.getElementById('more');
  const open = m.style.display === 'none';
  m.style.display = open ? '' : 'none';
  document.getElementById('morebtn').classList.toggle('open', open);
}
const chosen = new Set();
document.body.classList.add('has-actionbar');
function renderSelPanel() {
  const panel = document.getElementById('selpanel');
  if (chosen.size === 0) {
    panel.innerHTML = '<div class="empty">Nothing selected yet &mdash; '
      + 'tap recipes above to add them here.</div>';
    return;
  }
  panel.innerHTML = [...chosen].map(id => {
    const card = document.getElementById('c' + id);
    const title = card.querySelector('h4').textContent;
    const img = card.querySelector('.thumb');
    const thumb = img ? '<img src="' + img.src + '" alt="">'
                       : '<div class="ph"></div>';
    return '<div class="srow">' + thumb + '<span class="t">' + title
      + '</span><span class="links">'
      + '<a href="#" onclick="peek(' + id + ',event);return false">Ingredients</a>'
      + '<a href="#" onclick="openCard(' + id + ',event);return false">Recipe card</a>'
      + '</span><button class="x" onclick="pick(' + id + ')" '
      + 'aria-label="Remove" title="Remove">&times;</button></div>';
  }).join('');
}
function refreshBar() {
  const n = chosen.size;
  document.getElementById('selcounttext').textContent =
    n ? n + ' recipe' + (n > 1 ? 's' : '') + ' selected' : 'Nothing selected';
  const btn = document.getElementById('create');
  btn.disabled = n === 0;
  btn.textContent = n ? 'Create from ' + n + ' selected' : 'Create from selected';
  renderSelPanel();
}
function toggleExpand() {
  document.getElementById('selpanel').classList.toggle('on');
  document.getElementById('selcount').classList.toggle('open');
}
function pick(id) {
  const el = document.getElementById('c' + id);
  if (chosen.has(id)) { chosen.delete(id); el.classList.remove('on'); }
  else { chosen.add(id); el.classList.add('on'); }
  refreshBar();
}
let orderKind = null;
async function askOrder(kind) {
  if (kind === 'make' && !chosen.size) { out.textContent = 'Pick at least one recipe.'; return; }
  orderKind = kind;
  let d = '';
  try { d = (await (await fetch('/orders/next-date')).json()).date; } catch (e) {}
  const box = document.getElementById('ovbox');
  box.innerHTML = '<h3>New order</h3>'
    + '<p class="quietnote">When should it be delivered?</p>'
    + '<p><input type="date" id="odate" value="' + d + '"></p>'
    + '<p><input type="text" id="oname" maxlength="80" '
    + 'placeholder="Name (optional)" style="width:100%"></p>'
    + '<p style="margin-top:1.2rem"><button class="primary" '
    + 'onclick="confirmOrder()">Create order</button> '
    + '<button class="quiet" onclick="closePeek()">Cancel</button></p>';
  document.getElementById('ov').classList.add('on');
}
function confirmOrder() {
  const d = document.getElementById('odate').value;
  const n = document.getElementById('oname').value;
  closePeek();
  (orderKind === 'suggest' ? suggestWeek : makeWeek)(d, n);
}
async function suggestWeek(deliveryDate, name) {
  out.textContent = 'Choosing...';
  try {
    const r = await fetch('/weeks', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        retailer: document.getElementById('ret').value, auto: true,
        count: +document.getElementById('cnt').value,
        servings: +document.getElementById('srv').value,
        delivery_date: deliveryDate || null, name: name || null})});
    const w = await r.json();
    if (!w.id) { out.textContent = JSON.stringify(w, null, 2); return; }
    location.href = '/weeks/' + w.id + '/list';
  } catch (e) { out.textContent = 'Failed: ' + e; }
}
async function makeWeek(deliveryDate, name) {
  const picked = [...chosen].map(
    id => ({id: id, servings: +document.getElementById('s' + id).value}));
  if (!picked.length) { out.textContent = 'Pick at least one recipe.'; return; }
  out.textContent = 'Creating...';
  try {
    const r = await fetch('/weeks', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({retailer: document.getElementById('ret').value,
                            servings: +document.getElementById('srv').value,
                            delivery_date: deliveryDate || null,
                            name: name || null, recipes: picked})});
    const w = await r.json();
    location.href = '/weeks/' + w.id + '/list';
  } catch (e) { out.textContent = 'Failed: ' + e; }
}
"""
    return HTMLResponse(pages.shell("All recipes", "home", body, script, show_logout=auth.configured()))


@app.get("/admin", response_class=HTMLResponse)
def admin():
    """Maintenance: the things touched rarely, kept out of the weekly path."""
    h = health()
    warn = h.get("warning", "")

    broken = broken_recipes()
    broken_rows = "".join(
        f'<tr><td>{r["title"]}</td>'
        f'<td class="sub">{r["ingredients"]} ingredients, {r["steps"]} steps</td>'
        f'<td><button class="quiet" onclick="delRecipe({r["id"]})">Delete'
        f'</button></td></tr>' for r in broken["recipes"])
    broken_section = (f'<table>{broken_rows}</table>' if broken["count"]
                      else '<p class="sub">Nothing broken.</p>')

    archived_count = db.query_one(
        "SELECT COUNT(*) AS n FROM recipes WHERE status = 'archived'")["n"]

    orphans = orphan_ingredients()
    orphan_rows = "".join(
        f'<tr><td>{o["name"]}</td>'
        f'<td class="sub">{o["products"]} product(s), no recipes</td>'
        f'<td><button class="quiet" onclick="delIng({o["id"]})">Delete'
        f'</button></td></tr>' for o in orphans)
    orphan_section = (f'<table>{orphan_rows}</table>' if orphans
                      else '<p class="sub">No orphans.</p>')

    dupes = find_duplicates()
    dupe_rows = ""
    for g in dupes["groups"]:
        base = g["suggested_base"]
        for dup in g["duplicates"]:
            guess = dup["name"].lower()
            for sep in (",", " for the ", " for "):
                if sep in guess:
                    guess = guess.split(sep)[1] if sep == "," else ""
                    break
            else:
                guess = guess.replace(base["name"].lower(), "")
            dupe_rows += (
                f'<tr><td>{dup["name"]}</td><td class="sub">&rarr; '
                f'{base["name"]}</td>'
                f'<td><input id="p{dup["id"]}" value="{guess.strip()}" '
                f'placeholder="prep note" size="12"></td>'
                f'<td><button onclick="merge({dup["id"]},{base["id"]})">Merge'
                f'</button> <button class="quiet" '
                f'onclick="notDupe({dup["id"]},{base["id"]})">Not a duplicate'
                f'</button></td></tr>')
    dupe_section = (f'<table>{dupe_rows}</table>' if dupes["count"]
                    else '<p class="sub">No duplicates detected.</p>')

    staples = db.query("SELECT name FROM ingredients WHERE assume_stocked = 1 "
                       "ORDER BY name")
    staple_line = ", ".join(r["name"] for r in staples) or "none set"

    missing = missing_images()
    missing_line = "".join(
        f'<li>{r["title"]} <span class="sub">'
        + ("no hero image" if not r["hero_image"] else "")
        + (", " if not r["hero_image"] and r["with_images"] < r["steps"] else "")
        + (f'{r["with_images"]}/{r["steps"]} step photos'
           if r["with_images"] < r["steps"] else "")
        + f' &middot; <a href="{r["source_url"]}" target="_blank">source</a>'
        + '</span></li>' for r in missing["recipes"]) or \
        '<li class="sub">All recipes have full images.</li>'

    img_status = image_status()
    image_line = (
        f'{img_status["external"]} image(s) still linked to an external site.'
        if img_status["external"]
        else 'All images copied into the repo and served locally.')

    bk = backup_status()
    bk_line = (f'Last backup {bk["last_backup"][:16].replace("T", " ")} '
               f'&rarr; <code>{bk["path"]}</code>'
               if bk["last_backup"] else
               ('Never backed up. Runs weekly once configured.'
                if bk["configured"] else
                'Not configured &mdash; set <code>GITHUB_TOKEN</code> and '
                '<code>GITHUB_REPO</code> in Railway.'))

    files = list_seed_files().get("files", [])
    options = "".join(
        f'<option value="{f["name"]}">{f["name"]} ({f["size_kb"]} KB)</option>'
        for f in files) or '<option disabled>No files in seed/</option>'

    body = f"""
<h1>Maintenance</h1>
<div class="banner {'bad' if warn else 'ok'}">
  {warn or 'Volume mounted. Data survives redeploys.'}
</div>

<h2>Cleanup</h2>
<p class="sub">Broken or duplicate recipes</p>
{broken_section}
<p class="sub">Ingredients no recipe uses</p>
{orphan_section}

<h2>Archived recipes</h2>
<p class="sub">{archived_count} recipe{'s' if archived_count != 1 else ''}
   taken out of the picker and week suggestions, not deleted.
   <a href="/recipes/archived">View archived recipes &rarr;</a></p>

<h2>Duplicate ingredients</h2>
{dupe_section}

<h2>Staples &mdash; never on a list</h2>
<p>Currently: <strong>{staple_line}</strong></p>
<div class="bar">
  <input id="st" size="46" placeholder="comma separated">
  <button onclick="setStaples(true)">Mark stocked</button>
  <button class="quiet" onclick="setStaples(false)">Unmark</button>
</div>

<h2>Fix units</h2>
<p><a href="/admin/conversions">Review missing conversions with
   suggestions &rarr;</a></p>
<p class="sub">Set canonical units and conversions in bulk. Conversions merge
   with what&rsquo;s already there.</p>
<textarea id="uf" rows="5" style="width:100%;font-family:ui-monospace,monospace"
  placeholder='[{{"name": "onion", "unit_conversions": {{"unit": 150}}}}]'></textarea>
<div class="bar"><button onclick="fixUnits()">Apply</button></div>

<h2>Images</h2>
<p class="sub">{image_line}</p>
<div class="bar">
  <button onclick="mirrorAll()">Mirror everything</button>
  <button class="quiet" onclick="repairImgs()">Repair links</button>
</div>
<p class="sub">Missing images</p>
<ul>{missing_line}</ul>

<h2>Backup</h2>
<p class="sub">{bk_line}</p>
<div class="bar"><button onclick="backupNow()">Back up now</button></div>

<h2>Restore</h2>
<p class="sub">Files in <code>seed/</code>. Duplicate recipes are skipped.</p>
<div class="bar">
  <select id="f">{options}</select>
  <button class="quiet" onclick="restore()">Restore</button>
</div>

<p class="sub" style="margin-top:2rem">
  <a href="/health">/health</a> &middot;
  <a href="/backup">/backup</a> &middot;
  <a href="/ingredients/duplicates">/ingredients/duplicates</a></p>
"""

    script = """
async function post(url, body) {
  const opts = {method: 'POST'};
  if (body) { opts.headers = {'Content-Type': 'application/json'};
              opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  const j = await r.json();
  out.textContent = JSON.stringify(j, null, 2);
  return j;
}
function reload() { setTimeout(() => location.reload(), 800); }
async function delRecipe(id) {
  if (!confirm('Delete this recipe permanently?')) return;
  const r = await fetch('/recipes/' + id, {method: 'DELETE'});
  out.textContent = JSON.stringify(await r.json(), null, 2); reload();
}
async function delIng(id) {
  if (!confirm('Delete this ingredient and its products?')) return;
  const r = await fetch('/ingredients/' + id, {method: 'DELETE'});
  out.textContent = JSON.stringify(await r.json(), null, 2); reload();
}
async function merge(from, into) {
  const note = (document.getElementById('p' + from) || {}).value || null;
  await post('/ingredients/merge', {from_id: from, into_id: into,
                                    prep_note: note}); reload();
}
async function notDupe(a, b) {
  await post('/ingredients/not-duplicate', {a_id: a, b_id: b}); reload();
}
async function setStaples(stocked) {
  const names = document.getElementById('st').value
    .split(',').map(s => s.trim()).filter(Boolean);
  if (!names.length) { out.textContent = 'Enter some names.'; return; }
  await post('/ingredients/staples', {names: names, stocked: stocked});
  reload();
}
async function fixUnits() {
  let fixes;
  try { fixes = JSON.parse(document.getElementById('uf').value); }
  catch (e) { out.textContent = 'Not valid JSON: ' + e; return; }
  await post('/ingredients/fix-units', {fixes: fixes});
}
async function mirrorAll() {
  let left = 1;
  while (left > 0) {
    const j = await post('/images/mirror?limit=10');
    if (!j.ok || j.done === 0) break;
    left = j.remaining;
    out.textContent = left + ' image(s) left...';
  }
  reload();
}
async function repairImgs() { await post('/images/repair'); reload(); }
async function backupNow() { await post('/backup/commit'); }
async function restore() {
  const f = document.getElementById('f').value;
  await post('/restore?filename=' + encodeURIComponent(f));
}
"""
    return HTMLResponse(pages.shell("Maintenance", "admin", body, script, show_logout=auth.configured()))


@app.get("/recipes")
def list_recipes(status: str = "active", protein: str = None,
                 max_time: int = None):
    sql = "SELECT * FROM recipes WHERE status = ?"
    params = [status]
    if protein:
        sql += " AND protein = ?"
        params.append(protein)
    if max_time:
        sql += " AND (time_max IS NULL OR time_max <= ?)"
        params.append(max_time)
    sql += " ORDER BY last_cooked IS NOT NULL, last_cooked"
    return db.query(sql, params)


class StepImageIn(BaseModel):
    recipe_id: int
    step_number: int
    image: str


class StepImagesIn(BaseModel):
    images: list[StepImageIn]


@app.post("/recipe-steps/images")
def set_step_images(body: StepImagesIn):
    """Backfill step photos in bulk.

    Referenced by recipe id and step number rather than step id, since
    that's what you read straight off the source page.
    """
    done, missing = 0, []
    for item in body.images:
        row = db.query_one(
            "SELECT id FROM recipe_steps WHERE recipe_id = ? "
            "AND step_number = ?", (item.recipe_id, item.step_number))
        if not row:
            missing.append(f"recipe {item.recipe_id} step {item.step_number}")
            continue
        db.execute("UPDATE recipe_steps SET image = ? WHERE id = ?",
                   (item.image, row["id"]))
        done += 1
    return {"updated": done, "not_found": missing}


@app.get("/img/{filename}")
def serve_image(filename: str):
    """Serve a mirrored image from the volume cache or the private repo."""
    content = images.fetch_cached(filename)
    if content is None:
        raise HTTPException(404, "Image not found")
    return Response(content, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.post("/images/repair")
def repair_image_urls():
    """Fix URLs that point at raw.githubusercontent (private repo, so 404)."""
    return {"fixed": images.repair_urls()}


@app.get("/images/status")
def image_status():
    todo = images.pending()
    return {"external": len(todo), "configured": backup.configured(),
            "sample": [t["url"] for t in todo[:3]]}


@app.post("/images/mirror")
def mirror_images(limit: int = 10):
    """Copy images into the repo, a batch at a time."""
    return images.mirror(limit=limit)


@app.get("/recipes/broken")
def broken_recipes():
    """Recipes that look incomplete or were marked for deletion.

    A partial write used to be possible before ingest became atomic, and
    an agent may rename a bad row rather than delete it.
    """
    rows = db.query(
        "SELECT r.id, r.title, r.source, "
        "  (SELECT COUNT(*) FROM recipe_ingredients ri "
        "   WHERE ri.recipe_id = r.id) AS ingredients, "
        "  (SELECT COUNT(*) FROM recipe_steps s "
        "   WHERE s.recipe_id = r.id) AS steps "
        "FROM recipes r ORDER BY r.id")
    bad = [r for r in rows
           if r["ingredients"] == 0 or r["steps"] == 0
           or "DUPLICATE" in (r["title"] or "").upper()
           or "DELETE" in (r["title"] or "").upper()]
    return {"count": len(bad), "recipes": bad}


@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int):
    """Remove a recipe and its ingredient and step rows.

    Ingredients themselves are left alone — they're shared, and an orphan
    is harmless. Clear those separately.
    """
    row = db.query_one("SELECT title FROM recipes WHERE id = ?", (recipe_id,))
    if not row:
        raise HTTPException(404, "Recipe not found")
    with db.transaction() as conn:
        conn.execute("DELETE FROM week_recipes WHERE recipe_id = ?",
                     (recipe_id,))
        conn.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?",
                     (recipe_id,))
        conn.execute("DELETE FROM recipe_steps WHERE recipe_id = ?",
                     (recipe_id,))
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return {"ok": True, "deleted": row["title"]}


@app.get("/ingredients/orphans")
def orphan_ingredients():
    """Ingredients no recipe uses. Usually test rows or leftovers."""
    return db.query(
        "SELECT i.id, i.name, "
        "  (SELECT COUNT(*) FROM products p WHERE p.ingredient_id = i.id) "
        "    AS products "
        "FROM ingredients i WHERE NOT EXISTS ("
        "  SELECT 1 FROM recipe_ingredients ri WHERE ri.ingredient_id = i.id"
        ") ORDER BY i.name")


@app.delete("/ingredients/{ingredient_id}")
def delete_ingredient(ingredient_id: int):
    """Remove an ingredient no recipe uses, with its products."""
    used = db.query_one(
        "SELECT COUNT(*) AS n FROM recipe_ingredients WHERE ingredient_id = ?",
        (ingredient_id,))
    if used["n"]:
        raise HTTPException(
            409, f"Used by {used['n']} recipe line(s). Merge it instead.")
    row = db.query_one("SELECT name FROM ingredients WHERE id = ?",
                       (ingredient_id,))
    if not row:
        raise HTTPException(404, "Ingredient not found")
    with db.transaction() as conn:
        conn.execute("DELETE FROM products WHERE ingredient_id = ?",
                     (ingredient_id,))
        conn.execute("DELETE FROM cupboard_ledger WHERE ingredient_id = ?",
                     (ingredient_id,))
        conn.execute("DELETE FROM ingredients WHERE id = ?", (ingredient_id,))
    return {"ok": True, "deleted": row["name"]}


@app.get("/recipes/missing-images")
def missing_images():
    """Recipes with no hero or incomplete step photos.

    Step images are the best part of a card and can't be recovered later if
    the source page changes, so it's worth knowing which are short.
    """
    rows = db.query(
        "SELECT r.id, r.title, r.source_url, r.hero_image, "
        "  (SELECT COUNT(*) FROM recipe_steps s WHERE s.recipe_id = r.id) "
        "    AS steps, "
        "  (SELECT COUNT(*) FROM recipe_steps s WHERE s.recipe_id = r.id "
        "   AND s.image IS NOT NULL AND s.image != '') AS with_images "
        "FROM recipes r WHERE r.status = 'active' ORDER BY r.title")
    short = [r for r in rows
             if not r["hero_image"] or r["with_images"] < r["steps"]]
    return {"count": len(short), "recipes": short}


@app.get("/recipes/archived", response_class=HTMLResponse)
def archived_recipes_page():
    """Everything archived, with a way to bring it back. Linked from
    Maintenance rather than the main nav — this is cleanup, not weekly use.

    Must stay registered before GET /recipes/{recipe_id} below: Starlette
    matches "archived" against that route's path pattern first and only
    fails on the int() conversion, so a static route placed after it would
    never be reached.
    """
    rows = db.query(
        "SELECT id, title, last_cooked, times_cooked, baby_friendly "
        "FROM recipes WHERE status = 'archived' ORDER BY title")
    body = pages.archived_recipes_html(rows)
    script = """
async function restoreRecipe(id) {
  await fetch('/recipes/' + id + '/unarchive', {method: 'POST'});
  location.reload();
}
async function purgeRecipe(id) {
  if (!confirm('Delete this recipe permanently? This cannot be undone.'))
    return;
  await fetch('/recipes/' + id, {method: 'DELETE'});
  location.reload();
}
"""
    return HTMLResponse(pages.shell("Archived recipes", "admin", body, script,
                                    show_logout=auth.configured()))


@app.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int):
    recipe = db.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if not recipe:
        raise HTTPException(404, "Recipe not found")
    recipe["ingredients"] = db.query(
        "SELECT ri.*, i.name, i.category, i.canonical_unit "
        "FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE ri.recipe_id = ?", (recipe_id,))
    recipe["steps"] = db.query(
        "SELECT * FROM recipe_steps WHERE recipe_id = ? ORDER BY step_number",
        (recipe_id,))
    return recipe


@app.patch("/recipes/{recipe_id}")
def update_recipe(recipe_id: int, body: dict):
    """Correct any recipe field after ingest.

    Ingest is a first pass, not a final answer — a missing hero image, a
    mis-parsed time, a better protein label. Also where archive and
    frequency changes happen.
    """
    if not db.query_one("SELECT 1 AS x FROM recipes WHERE id = ?", (recipe_id,)):
        raise HTTPException(404, "Recipe not found")

    allowed = {
        "title", "subtitle", "source", "source_url", "time_min", "time_max",
        "protein", "cuisine", "effort", "tags", "hero_image", "nutrition",
        "nutrition_basis", "allergens", "tools", "status", "frequency",
        "rating", "rating_note", "last_cooked", "times_cooked",
        "baby_friendly", "baby_note",
    }
    json_fields = {"tags", "nutrition", "allergens", "tools"}

    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(
            400, f"No updatable fields supplied. Allowed: {sorted(allowed)}")

    for key in json_fields & fields.keys():
        if fields[key] is not None and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE recipes SET {sets} WHERE id = ?",
               (*fields.values(), recipe_id))
    return {"ok": True, "updated": sorted(fields)}


class RatingIn(BaseModel):
    rating: int
    note: str | None = None


@app.patch("/recipes/{recipe_id}/rating")
def rate_recipe(recipe_id: int, body: RatingIn):
    """Writable from chat or from the button on the recipe card page."""
    if body.rating not in (-1, 0, 1):
        raise HTTPException(400, "rating must be -1, 0 or 1")
    db.execute("UPDATE recipes SET rating = ?, rating_note = ? WHERE id = ?",
               (body.rating, body.note, recipe_id))
    return {"ok": True}


# TODO: POST /recipes/ingest   — batch, accepts {urls: [...]} or a PDF
# TODO: POST /recipes          — confirm and save a parsed draft


@app.post("/recipes/{recipe_id}/archive")
def archive_recipe(recipe_id: int):
    """Take a recipe out of rotation without deleting it.

    Archived recipes drop out of the picker (`home`) and week suggestions
    (`picker._eligible` filters on status = 'active') but everything about
    them — ingredients, steps, images, rating history — stays put. Reverse
    with /unarchive or the Restore button on the Archived recipes page.
    """
    if not db.query_one("SELECT 1 AS x FROM recipes WHERE id = ?", (recipe_id,)):
        raise HTTPException(404, "Recipe not found")
    db.execute("UPDATE recipes SET status = 'archived' WHERE id = ?",
               (recipe_id,))
    return {"ok": True, "status": "archived"}


@app.post("/recipes/{recipe_id}/unarchive")
def unarchive_recipe(recipe_id: int):
    if not db.query_one("SELECT 1 AS x FROM recipes WHERE id = ?", (recipe_id,)):
        raise HTTPException(404, "Recipe not found")
    db.execute("UPDATE recipes SET status = 'active' WHERE id = ?",
               (recipe_id,))
    return {"ok": True, "status": "active"}


# ---------------------------------------------------------------
# Ingredients and products
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# Recipe ingest
# ---------------------------------------------------------------
# The server cannot browse. A browser agent fetches and parses the page,
# then posts the structured result here. Ingredients are created as a side
# effect of ingesting a recipe — recipes are the source of ingredients,
# not shopping lists.

class IngredientInRecipe(BaseModel):
    name: str
    unit: str
    category: str = "fresh"
    canonical_unit: str = "g"
    aliases: list[str] = []
    qty_2p: float | None = None
    qty_3p: float | None = None
    qty_4p: float | None = None
    qty_base: float | None = None
    base_servings: int | None = None
    prep_note: str | None = None
    optional: bool = False
    needs_review: bool = False
    unit_conversions: dict = {}
    substitution_note: str | None = None


class StepIn(BaseModel):
    step_number: int
    title: str | None = None
    substeps: list[dict] = []          # [{text, callout: null|tip|important}]
    image: str | None = None
    timer_seconds: int | None = None
    ingredient_refs: list[str] = []    # names, resolved to ids on save


class RecipeIn(BaseModel):
    title: str
    subtitle: str | None = None
    source: str | None = None
    source_url: str | None = None
    time_min: int | None = None
    time_max: int | None = None
    protein: str | None = None
    cuisine: str | None = None
    effort: str | None = None
    tags: list[str] = []
    hero_image: str | None = None
    nutrition: dict | None = None
    nutrition_basis: str | None = None
    allergens: list[str] = []
    tools: list[str] = []
    baby_friendly: bool | None = None
    ingredients: list[IngredientInRecipe] = []
    steps: list[StepIn] = []


class RecipeBatchIn(BaseModel):
    recipes: list[RecipeIn]


@app.post("/recipes")
def create_recipes(body: RecipeBatchIn):
    """Batch ingest. Fifteen pasted URLs should be one call, not fifteen."""
    report = {
        "recipes_created": 0,
        "recipes_skipped": [],       # already present, by source_url
        "ingredients_created": [],
        "ingredients_matched": 0,
        "needs_review": [],
        "recipes": [],
    }

    for r in body.recipes:
        if r.source_url:
            dupe = db.query_one(
                "SELECT id, title FROM recipes WHERE source_url = ?",
                (r.source_url,))
            if dupe:
                report["recipes_skipped"].append(
                    {"title": r.title, "existing_id": dupe["id"]})
                continue

        # One transaction per recipe. A failure part-way through must leave
        # nothing behind rather than a recipe with half its ingredients.
        try:
            with db.transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO recipes (title, subtitle, source, source_url, "
                    "time_min, time_max, protein, cuisine, effort, tags, "
                    "hero_image, nutrition, nutrition_basis, allergens, tools, "
                    "baby_friendly) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r.title, r.subtitle, r.source, r.source_url, r.time_min,
                     r.time_max, r.protein, r.cuisine, r.effort,
                     json.dumps(r.tags), r.hero_image,
                     json.dumps(r.nutrition) if r.nutrition else None,
                     r.nutrition_basis, json.dumps(r.allergens),
                     json.dumps(r.tools),
                     None if r.baby_friendly is None
                     else int(r.baby_friendly)))
                recipe_id = cur.lastrowid

                name_to_id = {}
                for ing in r.ingredients:
                    existing = _find_ingredient(ing.name)
                    if existing:
                        ing_id = existing["id"]
                        report["ingredients_matched"] += 1
                        if ing.aliases:
                            merged = sorted(set(existing.get("aliases", []))
                                            | set(ing.aliases))
                            conn.execute(
                                "UPDATE ingredients SET aliases = ? "
                                "WHERE id = ?", (json.dumps(merged), ing_id))
                    else:
                        ing_id = conn.execute(
                            "INSERT INTO ingredients (name, category, "
                            "canonical_unit, aliases, unit_conversions, "
                            "substitution_note, needs_review) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (ing.name.strip(), ing.category,
                             ing.canonical_unit, json.dumps(ing.aliases),
                             json.dumps(ing.unit_conversions),
                             ing.substitution_note,
                             1 if ing.needs_review else 0)).lastrowid
                        report["ingredients_created"].append(ing.name)
                        if ing.needs_review:
                            report["needs_review"].append(
                                {"ingredient": ing.name, "recipe": r.title})

                    name_to_id[ing.name.strip().lower()] = ing_id

                    conn.execute(
                        "INSERT INTO recipe_ingredients (recipe_id, "
                        "ingredient_id, qty_2p, qty_3p, qty_4p, qty_base, "
                        "base_servings, unit, prep_note, optional) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (recipe_id, ing_id, ing.qty_2p, ing.qty_3p, ing.qty_4p,
                         ing.qty_base, ing.base_servings, ing.unit,
                         ing.prep_note, 1 if ing.optional else 0))

                for step in r.steps:
                    refs = [name_to_id[n.strip().lower()]
                            for n in step.ingredient_refs
                            if n.strip().lower() in name_to_id]
                    conn.execute(
                        "INSERT INTO recipe_steps (recipe_id, step_number, "
                        "title, substeps, image, timer_seconds, "
                        "ingredient_refs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (recipe_id, step.step_number, step.title,
                         json.dumps(step.substeps), step.image,
                         step.timer_seconds, json.dumps(refs)))

        except Exception as exc:
            report.setdefault("failed", []).append(
                {"title": r.title, "error": str(exc)[:300]})
            continue

        report["recipes_created"] += 1
        report["recipes"].append({"id": recipe_id, "title": r.title})

    return report


class StepsReplaceIn(BaseModel):
    steps: list[StepIn]


def _step_ref_ids(recipe_id: int, names: list[str]) -> list[int]:
    """Resolve ingredient names (or aliases) used in a step to ids, limited
    to ingredients that are actually part of this recipe."""
    rows = db.query(
        "SELECT i.id, i.name, i.aliases FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE ri.recipe_id = ?", (recipe_id,))
    lookup = {}
    for r in rows:
        lookup[r["name"].strip().lower()] = r["id"]
        aliases = r.get("aliases") or []
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except ValueError:
                aliases = []
        for a in aliases:
            lookup.setdefault(str(a).strip().lower(), r["id"])
    out = []
    for n in names:
        i = lookup.get(n.strip().lower())
        if i is not None and i not in out:
            out.append(i)
    return out


@app.put("/recipes/{recipe_id}/steps")
def replace_recipe_steps(recipe_id: int, body: StepsReplaceIn):
    """Replace the method text of a recipe.

    Send the full list of steps. Where a step omits `image`, the photo
    already stored for that step number is kept, so rewording a method
    never loses its pictures. Step ingredient_refs are names, resolved
    against this recipe's own ingredients.
    """
    if not db.query_one("SELECT 1 AS x FROM recipes WHERE id = ?", (recipe_id,)):
        raise HTTPException(404, "Recipe not found")
    if not body.steps:
        raise HTTPException(400, "Send at least one step")

    old_images = {r["step_number"]: r["image"] for r in db.query(
        "SELECT step_number, image FROM recipe_steps WHERE recipe_id = ?",
        (recipe_id,))}

    with db.transaction() as conn:
        conn.execute("DELETE FROM recipe_steps WHERE recipe_id = ?",
                     (recipe_id,))
        for step in body.steps:
            image = step.image if step.image else old_images.get(step.step_number)
            conn.execute(
                "INSERT INTO recipe_steps (recipe_id, step_number, title, "
                "substeps, image, timer_seconds, ingredient_refs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (recipe_id, step.step_number, step.title,
                 json.dumps(step.substeps), image, step.timer_seconds,
                 json.dumps(_step_ref_ids(recipe_id, step.ingredient_refs))))
    return {"ok": True, "steps": len(body.steps)}


@app.patch("/recipes/{recipe_id}/steps/{step_number}")
def update_recipe_step(recipe_id: int, step_number: int, body: dict):
    """Edit one step: title, substeps, timer_seconds, image or
    ingredient_refs (names)."""
    row = db.query_one(
        "SELECT id FROM recipe_steps WHERE recipe_id = ? AND step_number = ?",
        (recipe_id, step_number))
    if not row:
        raise HTTPException(404, "Step not found")

    allowed = {"title", "substeps", "timer_seconds", "image", "ingredient_refs"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(
            400, f"No updatable fields supplied. Allowed: {sorted(allowed)}")
    if "substeps" in fields:
        fields["substeps"] = json.dumps(fields["substeps"])
    if "ingredient_refs" in fields:
        fields["ingredient_refs"] = json.dumps(
            _step_ref_ids(recipe_id, fields["ingredient_refs"]))

    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE recipe_steps SET {sets} WHERE id = ?",
               (*fields.values(), row["id"]))
    return {"ok": True, "updated": sorted(fields)}


@app.patch("/recipe-ingredients/{row_id}")
def update_recipe_ingredient(row_id: int, body: dict):
    """Fix a mis-parsed quantity, unit or prep note on one recipe line.

    A wrong quantity is worse than a missing one — it silently produces a
    wrong shopping list every time the recipe is cooked. Row ids come from
    GET /recipes/{id}.
    """
    if not db.query_one("SELECT 1 AS x FROM recipe_ingredients WHERE id = ?",
                        (row_id,)):
        raise HTTPException(404, "Recipe ingredient not found")

    allowed = {"qty_2p", "qty_3p", "qty_4p", "qty_base", "base_servings",
               "unit", "prep_note", "optional", "ingredient_id"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(
            400, f"No updatable fields supplied. Allowed: {sorted(allowed)}")
    if "optional" in fields:
        fields["optional"] = 1 if fields["optional"] else 0

    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE recipe_ingredients SET {sets} WHERE id = ?",
               (*fields.values(), row_id))
    return {"ok": True, "updated": sorted(fields)}


@app.delete("/recipe-ingredients/{row_id}")
def delete_recipe_ingredient(row_id: int):
    """Remove a line that shouldn't be there — a pantry item parsed as an
    ingredient, or a duplicate. Leaves the ingredient itself alone."""
    if not db.query_one("SELECT 1 AS x FROM recipe_ingredients WHERE id = ?",
                        (row_id,)):
        raise HTTPException(404, "Recipe ingredient not found")
    db.execute("DELETE FROM recipe_ingredients WHERE id = ?", (row_id,))
    return {"ok": True}


@app.post("/recipes/{recipe_id}/ingredients")
def add_recipe_ingredient(recipe_id: int, body: IngredientInRecipe):
    """Add an ingredient the parser missed. Creates the ingredient row if
    it's new, matching on name and alias like ingest does."""
    if not db.query_one("SELECT 1 AS x FROM recipes WHERE id = ?", (recipe_id,)):
        raise HTTPException(404, "Recipe not found")

    existing = _find_ingredient(body.name)
    if existing:
        ing_id = existing["id"]
    else:
        ing_id = db.execute(
            "INSERT INTO ingredients (name, category, canonical_unit, aliases, "
            "unit_conversions, substitution_note, needs_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.name.strip(), body.category, body.canonical_unit,
             json.dumps(body.aliases), json.dumps(body.unit_conversions),
             body.substitution_note, 1 if body.needs_review else 0))

    row_id = db.execute(
        "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, qty_2p, "
        "qty_3p, qty_4p, qty_base, base_servings, unit, prep_note, optional) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (recipe_id, ing_id, body.qty_2p, body.qty_3p, body.qty_4p,
         body.qty_base, body.base_servings, body.unit, body.prep_note,
         1 if body.optional else 0))
    return {"id": row_id, "ingredient_id": ing_id,
            "ingredient_created": not existing}



@app.get("/ingredients/duplicates")
def find_duplicates():
    """Likely duplicate ingredients — same thing, different phrasing.

    Ingest bakes prep state into names ("chicken breast, diced") or splits
    by use ("olive oil for the dressing"). Each duplicate means searching
    the retailer twice and maintaining two mappings forever.
    """
    rows = db.query("SELECT id, name, category FROM ingredients ORDER BY name")

    def core(name):
        """Strip prep state and purpose to get the underlying ingredient."""
        n = name.lower().split(",")[0]
        for marker in (" for the ", " for "):
            if marker in n:
                n = n.split(marker)[0]
        for word in ("diced ", "sliced ", "chopped ", "grated ", "smashed ",
                     "steamed ", "shredded ", "crushed ", "minced "):
            n = n.replace(word, "")
        n = n.strip()
        return n[:-1] if n.endswith("s") and len(n) > 4 else n

    groups = {}
    for row in rows:
        groups.setdefault(core(row["name"]), []).append(row)

    dismissed = {(r["a_id"], r["b_id"])
                 for r in db.query("SELECT a_id, b_id FROM not_duplicates")}

    pairs = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Shortest name is usually the clean base ingredient.
        members.sort(key=lambda r: len(r["name"]))
        base = members[0]
        rest = [m for m in members[1:]
                if (min(base["id"], m["id"]), max(base["id"], m["id"]))
                not in dismissed]
        if rest:
            pairs.append({"suggested_base": base, "duplicates": rest,
                          "matched_on": key})

    return {"count": len(pairs), "groups": pairs}


class NotDuplicateIn(BaseModel):
    a_id: int
    b_id: int


@app.post("/ingredients/not-duplicate")
def dismiss_duplicate(body: NotDuplicateIn):
    """Stop suggesting this pair. They look alike but are different products.

    The detector is deliberately loose, so it will keep proposing things
    like avocado / smashed avocado. Dismissing is how you teach it.
    """
    a, b = sorted((body.a_id, body.b_id))
    db.execute("INSERT OR IGNORE INTO not_duplicates (a_id, b_id) "
               "VALUES (?, ?)", (a, b))
    return {"ok": True}


class StaplesIn(BaseModel):
    names: list[str]
    stocked: bool = True


@app.post("/ingredients/staples")
def set_staples(body: StaplesIn):
    """Mark ingredients as always in stock, so they never reach a list.

    Faster than editing them one at a time — paste the names you never run
    out of and they drop off every future shopping list.
    """
    done, missing = [], []
    for name in body.names:
        match = _find_ingredient(name)
        if not match:
            missing.append(name)
            continue
        db.execute("UPDATE ingredients SET assume_stocked = ? WHERE id = ?",
                   (1 if body.stocked else 0, match["id"]))
        done.append(match["name"])
    return {"updated": done, "not_found": missing}


class UnitFixIn(BaseModel):
    # [{name | ingredient_id, canonical_unit?, unit_conversions?}]
    fixes: list[dict]


@app.post("/ingredients/fix-units")
def fix_units(body: UnitFixIn):
    """Bulk-set canonical units and conversions.

    Most unresolved pack counts come from the same cause: ingest recorded
    grams while the recipe counts items. "1 onion" needs either a canonical
    unit of `unit`, or a conversion saying one onion weighs ~150g. Both are
    general knowledge, so fixing them in bulk beats a product-by-product
    session.

    unit_conversions are merged, not replaced — existing entries survive.
    """
    done, missing = [], []
    for fix in body.fixes:
        if fix.get("ingredient_id"):
            match = db.query_one("SELECT * FROM ingredients WHERE id = ?",
                                 (fix["ingredient_id"],))
        else:
            match = _find_ingredient(fix.get("name") or "")
        if not match:
            missing.append(fix.get("name") or fix.get("ingredient_id"))
            continue

        updates, params = [], []
        if fix.get("canonical_unit"):
            updates.append("canonical_unit = ?")
            params.append(fix["canonical_unit"])
        if fix.get("unit_conversions"):
            merged = dict(match.get("unit_conversions") or {})
            merged.update(fix["unit_conversions"])
            updates.append("unit_conversions = ?")
            params.append(json.dumps(merged))
        if not updates:
            continue

        db.execute(f"UPDATE ingredients SET {', '.join(updates)} WHERE id = ?",
                   (*params, match["id"]))
        done.append(match["name"])

    return {"updated": done, "not_found": missing}


class MergeIn(BaseModel):
    from_id: int
    into_id: int
    prep_note: str | None = None   # applied to the moved recipe lines


@app.post("/ingredients/merge")
def merge_ingredients(body: MergeIn):
    """Fold one ingredient into another.

    Recipe lines and products move across, aliases combine, and the old
    name becomes an alias so future ingests match the surviving row.
    """
    if body.from_id == body.into_id:
        raise HTTPException(400, "Cannot merge an ingredient into itself")

    src = db.query_one("SELECT * FROM ingredients WHERE id = ?", (body.from_id,))
    dst = db.query_one("SELECT * FROM ingredients WHERE id = ?", (body.into_id,))
    if not src or not dst:
        raise HTTPException(404, "Ingredient not found")

    moved = db.query(
        "SELECT id FROM recipe_ingredients WHERE ingredient_id = ?",
        (body.from_id,))
    db.execute("UPDATE recipe_ingredients SET ingredient_id = ? "
               "WHERE ingredient_id = ?", (body.into_id, body.from_id))

    if body.prep_note:
        for row in moved:
            db.execute(
                "UPDATE recipe_ingredients SET prep_note = "
                "COALESCE(NULLIF(prep_note, ''), ?) WHERE id = ?",
                (body.prep_note, row["id"]))

    # Move products the destination doesn't already have for that retailer.
    products_moved = 0
    for product in db.query("SELECT * FROM products WHERE ingredient_id = ?",
                            (body.from_id,)):
        clash = db.query_one(
            "SELECT 1 AS x FROM products WHERE ingredient_id = ? "
            "AND retailer = ? AND sku IS ?",
            (body.into_id, product["retailer"], product["sku"]))
        if clash:
            db.execute("DELETE FROM products WHERE id = ?", (product["id"],))
        else:
            db.execute("UPDATE products SET ingredient_id = ?, preferred = 0 "
                       "WHERE id = ?", (body.into_id, product["id"]))
            products_moved += 1

    db.execute("UPDATE cupboard_ledger SET ingredient_id = ? "
               "WHERE ingredient_id = ?", (body.into_id, body.from_id))

    aliases = sorted(set(dst.get("aliases", []))
                     | set(src.get("aliases", []))
                     | {src["name"]})
    db.execute("UPDATE ingredients SET aliases = ? WHERE id = ?",
               (json.dumps(aliases), body.into_id))

    db.execute("DELETE FROM ingredients WHERE id = ?", (body.from_id,))

    return {"ok": True, "merged": src["name"], "into": dst["name"],
            "recipe_lines_moved": len(moved),
            "products_moved": products_moved,
            "aliases_now": aliases}


@app.get("/ingredients")
def list_ingredients(category: str = None, needs_review: bool = None):
    sql = "SELECT * FROM ingredients WHERE 1=1"
    params = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if needs_review is not None:
        sql += " AND needs_review = ?"
        params.append(1 if needs_review else 0)
    return db.query(sql + " ORDER BY name", params)


def _find_ingredient(name):
    """Match by name or alias, case-insensitively.

    Stops 'spring onions' and 'salad onions' becoming two rows.
    """
    key = name.strip().lower()
    row = db.query_one(
        "SELECT * FROM ingredients WHERE LOWER(name) = ?", (key,))
    if row:
        return row
    for candidate in db.query("SELECT * FROM ingredients"):
        if any(a.strip().lower() == key for a in candidate.get("aliases", [])):
            return candidate
    return None


@app.post("/ingredients/resolve")
def resolve_ingredients(names: list[str]):
    """Which of these names already exist? Call before creating anything."""
    out = {}
    for name in names:
        match = _find_ingredient(name)
        out[name] = {"id": match["id"], "name": match["name"]} if match else None
    return out


class IngredientIn(BaseModel):
    name: str
    category: str                       # fresh | cupboard | never_buy
    canonical_unit: str = "g"
    aliases: list[str] = []
    unit_conversions: dict = {}
    substitution_note: str | None = None
    needs_review: bool = False


@app.post("/ingredients")
def create_ingredient(body: IngredientIn):
    existing = _find_ingredient(body.name)
    if existing:
        return {"id": existing["id"], "created": False}
    ing_id = db.execute(
        "INSERT INTO ingredients (name, category, canonical_unit, aliases, "
        "unit_conversions, substitution_note, needs_review) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (body.name.strip(), body.category, body.canonical_unit,
         json.dumps(body.aliases), json.dumps(body.unit_conversions),
         body.substitution_note, 1 if body.needs_review else 0))
    return {"id": ing_id, "created": True}


@app.patch("/ingredients/{ingredient_id}")
def update_ingredient(ingredient_id: int, body: dict):
    allowed = {"name", "category", "canonical_unit", "substitution_note",
               "needs_review", "aliases", "unit_conversions",
               "assume_stocked", "carb"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "No updatable fields supplied")
    if "carb" in fields:
        # None = guess from the name; "" = not a carb; else a list of keys.
        c = fields["carb"]
        if isinstance(c, list):
            c = ",".join(c)
        if c is not None:
            bad = [x for x in c.split(",") if x and x not in filters.CARBS]
            if bad:
                raise HTTPException(422, f"Unknown carb {bad}; use "
                                         f"{sorted(filters.CARBS)} or empty")
        fields["carb"] = c
    for key in ("aliases", "unit_conversions"):
        if key in fields:
            fields[key] = json.dumps(fields[key])
    for key in ("needs_review", "assume_stocked"):
        if key in fields:
            fields[key] = 1 if fields[key] else 0
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE ingredients SET {sets} WHERE id = ?",
               (*fields.values(), ingredient_id))
    return {"ok": True}


class ProductIn(BaseModel):
    ingredient_id: int
    retailer: str
    name: str
    sku: str | None = None
    url: str | None = None
    pack_size: float | None = None
    pack_unit: str | None = None
    price: float | None = None
    preferred: bool = False
    match_at_shop_time: bool = False
    substitute: bool | None = None
    note: str | None = None


def _upsert_product(p: dict):
    """Insert, or update if this ingredient already has this product.

    The uniqueness key is (ingredient_id, retailer, sku) — NOT (retailer,
    sku). The same supermarket product can legitimately serve two different
    ingredients: one jar of red wine stock pots covers both "red wine stock
    paste" and "red wine jus paste". Keying on sku alone silently updated
    the other ingredient's row and left this one unmatched.

    The first product for an ingredient at a retailer becomes preferred,
    so a shopping list always resolves even if nothing was marked.
    """
    existing = None
    if p.get("sku"):
        existing = db.query_one(
            "SELECT * FROM products WHERE ingredient_id = ? AND retailer = ? "
            "AND sku = ?", (p["ingredient_id"], p["retailer"], p["sku"]))
    if not existing and p.get("url"):
        existing = db.query_one(
            "SELECT * FROM products WHERE ingredient_id = ? AND retailer = ? "
            "AND url = ?", (p["ingredient_id"], p["retailer"], p["url"]))


    today = date.today().isoformat()

    if existing:
        db.execute(
            "UPDATE products SET name = ?, pack_size = ?, pack_unit = ?, "
            "price = ?, price_updated = ?, last_verified = ?, delisted = 0 "
            "WHERE id = ?",
            (p["name"], p.get("pack_size"), p.get("pack_unit"),
             p.get("price"), today, today, existing["id"]))
        # Only touched when the caller says so, so a price refresh never
        # clears a swap flag by accident.
        if p.get("substitute") is not None:
            db.execute("UPDATE products SET substitute = ? WHERE id = ?",
                       (1 if p["substitute"] else 0, existing["id"]))
        if p.get("note") is not None:
            db.execute("UPDATE products SET note = ? WHERE id = ?",
                       (p["note"], existing["id"]))
        return existing["id"], False

    has_preferred = db.query_one(
        "SELECT 1 AS x FROM products WHERE ingredient_id = ? AND retailer = ? "
        "AND preferred = 1", (p["ingredient_id"], p["retailer"]))
    preferred = 1 if (p.get("preferred") or not has_preferred) else 0

    if preferred:
        db.execute(
            "UPDATE products SET preferred = 0 WHERE ingredient_id = ? "
            "AND retailer = ?", (p["ingredient_id"], p["retailer"]))

    product_id = db.execute(
        "INSERT INTO products (ingredient_id, retailer, sku, url, name, "
        "pack_size, pack_unit, price, price_updated, last_verified, "
        "preferred, match_at_shop_time, substitute, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (p["ingredient_id"], p["retailer"], p.get("sku"), p.get("url"),
         p["name"], p.get("pack_size"), p.get("pack_unit"), p.get("price"),
         today, today, preferred, 1 if p.get("match_at_shop_time") else 0,
         1 if p.get("substitute") else 0, p.get("note")))
    return product_id, True


@app.post("/products")
def create_product(body: ProductIn):
    product_id, created = _upsert_product(body.model_dump())
    return {"id": product_id, "created": created}


@app.patch("/products/{product_id}")
def update_product(product_id: int, body: dict):
    allowed = {"name", "sku", "url", "pack_size", "pack_unit", "price",
               "preferred", "match_at_shop_time", "delisted", "substitute",
               "note"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "No updatable fields supplied")
    for key in ("preferred", "match_at_shop_time", "delisted", "substitute"):
        if key in fields:
            fields[key] = 1 if fields[key] else 0
    if "price" in fields:
        fields["price_updated"] = date.today().isoformat()
    if fields.get("preferred"):
        row = db.query_one("SELECT * FROM products WHERE id = ?", (product_id,))
        db.execute("UPDATE products SET preferred = 0 WHERE ingredient_id = ? "
                   "AND retailer = ?", (row["ingredient_id"], row["retailer"]))
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE products SET {sets} WHERE id = ?",
               (*fields.values(), product_id))
    return {"ok": True}


# ---------------------------------------------------------------
# Harvest — bulk ingredient + product creation
# ---------------------------------------------------------------

class HarvestItem(BaseModel):
    ingredient_name: str
    category: str = "fresh"
    canonical_unit: str = "g"
    aliases: list[str] = []
    needs_review: bool = False
    product_name: str | None = None
    sku: str | None = None
    url: str | None = None
    pack_size: float | None = None
    pack_unit: str | None = None
    price: float | None = None
    match_at_shop_time: bool = False
    substitute: bool | None = None          # a stand-in, not the real thing
    note: str | None = None


class HarvestIn(BaseModel):
    retailer: str
    items: list[HarvestItem]


@app.post("/harvest")
def harvest(body: HarvestIn):
    """Bulk load from a retailer's saved lists.

    One call for a whole shopping list. Matches existing ingredients by name
    and alias so re-running is safe — a second harvest of the same list
    updates prices rather than duplicating rows.
    """
    result = {"ingredients_created": 0, "ingredients_matched": 0,
              "products_created": 0, "products_updated": 0, "items": []}

    for item in body.items:
        existing = _find_ingredient(item.ingredient_name)
        if existing:
            ing_id = existing["id"]
            result["ingredients_matched"] += 1
        else:
            ing_id = db.execute(
                "INSERT INTO ingredients (name, category, canonical_unit, "
                "aliases, needs_review) VALUES (?, ?, ?, ?, ?)",
                (item.ingredient_name.strip(), item.category,
                 item.canonical_unit, json.dumps(item.aliases),
                 1 if item.needs_review else 0))
            result["ingredients_created"] += 1

        product_id = None
        if item.product_name:
            product_id, created = _upsert_product({
                "ingredient_id": ing_id, "retailer": body.retailer,
                "name": item.product_name, "sku": item.sku, "url": item.url,
                "pack_size": item.pack_size, "pack_unit": item.pack_unit,
                "price": item.price,
                "match_at_shop_time": item.match_at_shop_time,
                "substitute": item.substitute, "note": item.note,
            })
            result["products_created" if created else "products_updated"] += 1

        result["items"].append({
            "ingredient": item.ingredient_name,
            "ingredient_id": ing_id,
            "product_id": product_id,
        })

    return result


@app.get("/ingredients/overview")
def ingredients_overview(recipes: bool = False):
    """Every ingredient with its chosen product at each shop, as JSON.

    Add ?recipes=1 to include which recipes use each ingredient.
    """
    data = catalogue.overview()
    if not recipes:
        data.pop("used_in", None)
    return data


@app.get("/ingredients/browse", response_class=HTMLResponse)
def ingredients_browse():
    """The Ingredients page: what each shop sells for every ingredient."""
    body, script = catalogue.page_html(catalogue.overview())
    return pages.shell("Ingredients", "ingredients", body, script,
                       show_logout=auth.configured())


@app.get("/ingredients/unmatched")
def unmatched(retailer: str, limit: int = 200):
    """The search queue: ingredients with no product at this retailer.

    Ordered by how many active recipes need them, so the bot matches the
    ingredients that unblock the most recipes first. Partial coverage is
    fine — the queue just gets shorter.
    """
    rows = db.query(
        "SELECT i.*, "
        "  (SELECT COUNT(DISTINCT ri.recipe_id) FROM recipe_ingredients ri "
        "   JOIN recipes r ON r.id = ri.recipe_id AND r.status = 'active' "
        "   WHERE ri.ingredient_id = i.id) AS recipe_count "
        "FROM ingredients i "
        "WHERE i.category != 'never_buy' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM products p "
        "  WHERE p.ingredient_id = i.id AND p.retailer = ? AND p.delisted = 0"
        ") ORDER BY recipe_count DESC, i.name LIMIT ?", (retailer, limit))

    for row in rows:
        row["used_in"] = [r["title"] for r in db.query(
            "SELECT DISTINCT r.title FROM recipe_ingredients ri "
            "JOIN recipes r ON r.id = ri.recipe_id "
            "WHERE ri.ingredient_id = ? AND r.status = 'active' LIMIT 5",
            (row["id"],))]

    return {"retailer": retailer, "count": len(rows), "queue": rows}



# ---------------------------------------------------------------
# Weeks
# ---------------------------------------------------------------

class WeekIn(BaseModel):
    retailer: str
    recipes: list[dict] | None = None      # [{id, servings}]
    auto: bool = False
    count: int = 3                          # how many recipes to suggest
    servings: int = 4
    seed: int | None = None                 # reproducible suggestions
    delivery_date: str | None = None        # ISO date, e.g. 2026-10-20
    name: str | None = None


def _clean_date(value):
    """Accepts YYYY-MM-DD (or empty = no date). Anything else is a 422."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise HTTPException(422, "Delivery date must look like 2026-10-20")


def _clean_name(value):
    value = (value or "").strip()
    return value[:80] or None


@app.get("/orders/next-date")
def next_order_date():
    """A sensible default delivery date for a new order: a week after the
    latest upcoming order, otherwise the next Tuesday at least two days out."""
    today = date.today()
    latest = db.query_one(
        "SELECT MAX(delivery_date) AS d FROM weeks WHERE delivery_date >= ?",
        (today.isoformat(),))
    if latest and latest["d"]:
        return {"date": (date.fromisoformat(latest["d"])
                         + timedelta(days=7)).isoformat()}
    d = today + timedelta(days=2)
    while d.weekday() != 1:          # Tuesday
        d += timedelta(days=1)
    return {"date": d.isoformat()}


@app.post("/weeks")
def create_week(body: WeekIn):
    """Creates a draft. Retailer is fixed here and never changes."""
    week_id = db.new_week_id()
    delivery = _clean_date(body.delivery_date)
    db.execute("INSERT INTO weeks (id, retailer, status, delivery_date, name) "
               "VALUES (?, ?, 'draft', ?, ?)",
               (week_id, body.retailer, delivery, _clean_name(body.name)))

    note = None
    if body.auto:
        chosen, note = picker.pick(count=body.count, seed=body.seed)
        if chosen is None:
            db.execute("DELETE FROM weeks WHERE id = ?", (week_id,))
            raise HTTPException(409, note)
        for seq, recipe in enumerate(chosen, start=1):
            db.execute(
                "INSERT INTO week_recipes (week_id, recipe_id, servings, "
                "sequence) VALUES (?, ?, ?, ?)",
                (week_id, recipe["id"], body.servings, seq))
    else:
        for seq, item in enumerate(body.recipes or [], start=1):
            db.execute(
                "INSERT INTO week_recipes (week_id, recipe_id, servings, "
                "sequence) VALUES (?, ?, ?, ?)",
                (week_id, item["id"], item.get("servings", body.servings), seq))

    return {"id": week_id, "retailer": body.retailer, "status": "draft",
            "note": note}


class WeekPatch(BaseModel):
    delivery_date: str | None = None
    name: str | None = None


@app.patch("/weeks/{week_id}")
def patch_week(week_id: str, body: WeekPatch):
    """Change an order's delivery date and/or name. Only fields that are
    sent are touched; send an empty string to clear one."""
    if not db.query_one("SELECT id FROM weeks WHERE id = ?", (week_id,)):
        raise HTTPException(404, "Week not found")
    sent = body.model_fields_set
    if "delivery_date" in sent:
        db.execute("UPDATE weeks SET delivery_date = ? WHERE id = ?",
                   (_clean_date(body.delivery_date), week_id))
    if "name" in sent:
        db.execute("UPDATE weeks SET name = ? WHERE id = ?",
                   (_clean_name(body.name), week_id))
    return db.query_one(
        "SELECT id, delivery_date, name FROM weeks WHERE id = ?", (week_id,))


@app.get("/weeks/{week_id}")
def get_week(week_id: str):
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    week["recipes"] = db.query(
        "SELECT wr.*, r.title, r.time_min, r.time_max, r.protein "
        "FROM week_recipes wr JOIN recipes r ON r.id = wr.recipe_id "
        "WHERE wr.week_id = ? ORDER BY wr.sequence", (week_id,))
    return week


@app.get("/weeks/{week_id}/coverage")
def week_coverage(week_id: str):
    """Unmatched ingredients for this week's retailer, per recipe.

    Checked at proposal time so a recipe needing four new matches is a known
    cost before it is accepted, not a surprise at the basket.
    """
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")

    rows = db.query(
        "SELECT DISTINCT r.id AS recipe_id, r.title, i.id AS ingredient_id, "
        "       i.name AS ingredient_name "
        "FROM week_recipes wr "
        "JOIN recipes r ON r.id = wr.recipe_id "
        "JOIN recipe_ingredients ri ON ri.recipe_id = r.id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE wr.week_id = ? AND i.category != 'never_buy' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM products p "
        "  WHERE p.ingredient_id = i.id AND p.retailer = ? AND p.delisted = 0"
        ")", (week_id, week["retailer"]))

    by_recipe = {}
    for row in rows:
        by_recipe.setdefault(row["title"], []).append(row["ingredient_name"])

    return {
        "retailer": week["retailer"],
        "unmatched_count": len(rows),
        "ready": len(rows) == 0,
        "by_recipe": by_recipe,
    }


@app.get("/weeks/{week_id}/shopping-list")
def shopping_list(week_id: str):
    """Resolved products and quantities for the week's retailer only.

    Never falls back to another retailer's product, for any reason.

    Everything buyable goes in the basket — `fresh` and `cupboard` alike.
    Only `assume_stocked` staples and `never_buy` are excluded. The review
    happens in the Ocado basket before ordering, not as a separate
    check-first list that would get skipped.
    """
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")

    rows = db.query(
        "SELECT ri.*, i.id AS ing_id, i.name AS ing_name, i.category, "
        "       i.canonical_unit, i.unit_conversions, i.assume_stocked, "
        "       wr.servings "
        "FROM week_recipes wr "
        "JOIN recipe_ingredients ri ON ri.recipe_id = wr.recipe_id "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE wr.week_id = ?", (week_id,))

    # Merge across recipes: 2 onions + 1 onion = 3 onions.
    # One entry per ingredient. Where a recipe uses a different unit from
    # the canonical one, convert before adding — otherwise the same
    # ingredient appears twice and gets bought twice.
    totals = {}
    for row in rows:
        qty = quantities.for_servings(row, row["servings"])
        if qty is None:
            continue

        entry = totals.get(row["ing_id"])
        if entry is None:
            entry = totals[row["ing_id"]] = {
                "ingredient_id": row["ing_id"],
                "name": row["ing_name"],
                "category": row["category"],
                "canonical_unit": row["canonical_unit"],
                "unit_conversions": row["unit_conversions"],
                "assume_stocked": row["assume_stocked"],
                "unit": row["unit"],
                "total": 0.0,
            }
            entry["total"] = qty
            continue

        if row["unit"] == entry["unit"]:
            entry["total"] += qty
            continue

        # Different units for the same ingredient — convert both to
        # canonical and continue in that unit.
        a, _ = quantities.to_canonical(entry["total"], entry["unit"], entry)
        b, _ = quantities.to_canonical(qty, row["unit"], entry)
        if a is not None and b is not None:
            entry["total"] = a + b
            entry["unit"] = entry["canonical_unit"]
        else:
            # Can't reconcile; keep the larger and flag it downstream.
            entry["mixed_units"] = True
            entry["total"] = max(entry["total"], qty)

    skipped_ids = {r["ingredient_id"] for r in db.query(
        "SELECT ingredient_id FROM week_cupboard_confirmations "
        "WHERE week_id = ? AND buy = 0", (week_id,))}

    buy, stocked, unmatched_items, unresolved, skipped = [], [], [], [], []
    for entry in totals.values():
        if entry["category"] == "never_buy":
            continue
        if entry["assume_stocked"]:
            stocked.append(entry)
            continue
        if entry["ingredient_id"] in skipped_ids:
            skipped.append(entry)
            continue

        candidates = db.query(
            "SELECT * FROM products WHERE ingredient_id = ? AND retailer = ? "
            "AND delisted = 0 ORDER BY preferred DESC, pack_size",
            (entry["ingredient_id"], week["retailer"]))
        if not candidates:
            unmatched_items.append(entry)
            continue

        needed, reason = quantities.to_canonical(
            entry["total"], entry["unit"], entry)

        # Only packs measured in the canonical unit can be compared.
        comparable = [c for c in candidates
                      if quantities.compatible(c, entry)]

        if needed is not None and comparable:
            lines, note = quantities.best_packs(needed, comparable, entry)
        else:
            # Can't compute: fall back to one of the preferred product and
            # say so, rather than presenting a guess as a calculation.
            note = reason or "no comparable pack size at this retailer"
            lines = [{"product": candidates[0], "packs": 1,
                      "cost": round(candidates[0]["price"] or 0, 2)}]

        entry["lines"] = lines
        entry["product"] = lines[0]["product"]
        entry["packs"] = sum(l["packs"] for l in lines)
        entry["pack_note"] = note
        entry["line_cost"] = round(sum(l["cost"] for l in lines), 2)
        entry["alternatives"] = max(0, len(comparable) - len(lines))

        price, basis = quantities.unit_price(entry["product"])
        entry["unit_price"] = price
        entry["unit_price_basis"] = basis

        # Portion cost: only what the recipes actually use.
        priced = [(c, quantities.pack_in_canonical(c, entry))
                  for c in comparable if c.get("price")]
        cheapest = min(((c, sz) for c, sz in priced if sz),
                       key=lambda t: t[0]["price"] / t[1], default=None)
        if needed and cheapest:
            entry["portion_cost"] = round(
                cheapest[0]["price"] * needed / cheapest[1], 2)
        else:
            entry["portion_cost"] = entry["line_cost"]

        if note:
            unresolved.append(entry)
        buy.append(entry)

    buy.sort(key=lambda e: (e["category"] != "fresh", e["name"]))

    return {
        "retailer": week["retailer"],
        "buy": buy,
        "skipped": skipped,
        "assumed_stocked": stocked,
        "unmatched": unmatched_items,
        "unresolved": unresolved,
        "basket_cost": round(sum(e["line_cost"] for e in buy), 2),
        "portion_cost": round(sum(e["portion_cost"] for e in buy), 2),
        "ready": len(unmatched_items) == 0,
    }


@app.get("/weeks/{week_id}/list", response_class=HTMLResponse)
def shopping_list_page(week_id: str, request: Request):
    """The shopping list as a page — this is the URL a browser agent opens."""
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    data = shopping_list(week_id)
    recipes = db.query(
        "SELECT wr.servings, r.title, r.time_max, r.hero_image "
        "FROM week_recipes wr "
        "JOIN recipes r ON r.id = wr.recipe_id WHERE wr.week_id = ? "
        "ORDER BY wr.sequence, r.title", (week_id,))
    base = str(request.base_url).rstrip("/")
    return HTMLResponse(
        pages.shopping_list_html(week, data, recipes, base))


def _card_for(recipe_id, servings, layout="screen"):
    recipe = db.query_one("SELECT * FROM recipes WHERE id = ?", (recipe_id,))
    if not recipe:
        return None
    rows = db.query(
        "SELECT ri.*, i.name FROM recipe_ingredients ri "
        "JOIN ingredients i ON i.id = ri.ingredient_id "
        "WHERE ri.recipe_id = ? ORDER BY ri.id", (recipe_id,))
    for row in rows:
        row["qty"] = quantities.for_servings(row, servings)
    steps = db.query(
        "SELECT * FROM recipe_steps WHERE recipe_id = ? ORDER BY step_number",
        (recipe_id,))
    return pages.recipe_card_html(recipe, rows, steps, servings, layout)


def _week_recipes(week_id):
    return db.query(
        "SELECT wr.recipe_id, wr.servings, r.title, r.time_max, r.protein, "
        "       r.hero_image FROM week_recipes wr "
        "JOIN recipes r ON r.id = wr.recipe_id "
        "WHERE wr.week_id = ? ORDER BY wr.sequence, wr.id", (week_id,))


@app.get("/weeks/{week_id}/cards", response_class=HTMLResponse)
def week_cards(week_id: str):
    """Overview of the week's recipes.

    Scrolling through a stack of full cards to find tonight's recipe is
    painful on a phone, which is where cooking actually happens. So this is
    a short list, and each recipe gets its own page.
    """
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    rows = _week_recipes(week_id)
    if not rows:
        return HTMLResponse("<p>No recipes in this week yet.</p>")
    display = (week["status"] if week["status"] == "ordered"
              else ("ready" if shopping_list(week_id)["ready"]
                    else "attention"))
    return HTMLResponse(pages.week_overview_html(week, rows, display))


@app.get("/weeks/{week_id}/cards/{recipe_id}", response_class=HTMLResponse)
def week_card_detail(week_id: str, recipe_id: int):
    """One recipe, sized for a phone propped up in the kitchen."""
    rows = _week_recipes(week_id)
    if not rows:
        raise HTTPException(404, "Week not found")

    ids = [r["recipe_id"] for r in rows]
    if recipe_id not in ids:
        raise HTTPException(404, "That recipe isn't in this week")
    idx = ids.index(recipe_id)

    card = _card_for(recipe_id, rows[idx]["servings"])
    return HTMLResponse(pages.recipe_detail_html(
        week_id, card, idx + 1, len(ids),
        ids[idx - 1] if idx > 0 else None,
        ids[idx + 1] if idx < len(ids) - 1 else None))


@app.get("/weeks/{week_id}/print", response_class=HTMLResponse)
def week_print(week_id: str):
    """All cards, one per page, print dialog opened automatically."""
    rows = _week_recipes(week_id)
    if not rows:
        raise HTTPException(404, "Week not found")
    cards = [c for c in (_card_for(r["recipe_id"], r["servings"], "print")
                         for r in rows) if c]
    return HTMLResponse(pages.print_sheet_html(cards))


@app.get("/recipes/{recipe_id}/card", response_class=HTMLResponse)
def recipe_card(recipe_id: int, servings: int = 4):
    """The standalone card for one recipe — what the picker's new "recipe
    card" link opens. Unlike the week print sheet, this one is interactive:
    it gets an Archive/Restore + Print toolbar, since it's the page someone
    lands on after deciding a recipe isn't worth keeping."""
    meta = db.query_one("SELECT status FROM recipes WHERE id = ?",
                        (recipe_id,))
    if not meta:
        raise HTTPException(404, "Recipe not found")
    card = _card_for(recipe_id, servings)
    return HTMLResponse(pages.cards_page_html(
        [card], recipe_id=recipe_id, status=meta["status"]))


def _receipt_args(servings, photos, width):
    if photos not in ("none", "hero", "all"):
        photos = "none"
    if width not in (receipt.WIDTH_80, receipt.WIDTH_58):
        width = receipt.WIDTH_80
    return max(1, min(servings, 12)), photos, width


def _receipt_image(recipe_id, servings, photos, width):
    loaded = receipt.load(db, recipe_id, servings)
    if not loaded:
        raise HTTPException(404, "Recipe not found")
    recipe, rows, steps = loaded
    return recipe, receipt.render(recipe, rows, steps, servings, photos,
                                  width)


@app.get("/recipes/{recipe_id}/receipt.png")
def recipe_receipt_png(recipe_id: int, servings: int = 4,
                       photos: str = "none", width: int = receipt.WIDTH_80):
    """The recipe as a black-and-white image the width of receipt paper."""
    servings, photos, width = _receipt_args(servings, photos, width)
    _, img = _receipt_image(recipe_id, servings, photos, width)
    return Response(receipt.to_png(img), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/recipes/{recipe_id}/receipt", response_class=HTMLResponse)
def recipe_receipt(recipe_id: int, servings: int = 4, photos: str = "none",
                   width: int = receipt.WIDTH_80):
    """Preview of the receipt, before anything is printed."""
    servings, photos, width = _receipt_args(servings, photos, width)
    recipe, img = _receipt_image(recipe_id, servings, photos, width)
    height_cm = img.size[1] / 203 * 2.54
    return HTMLResponse(pages.receipt_preview_html(
        recipe_id, recipe["title"], servings, photos, width, height_cm))


class SkipIn(BaseModel):
    ingredient_ids: list[int]      # things you already have; don't buy


@app.post("/weeks/{week_id}/skip")
def skip_ingredients(week_id: str, body: SkipIn):
    """Tick off what you already have. Replaces the whole set each time.

    This is the cupboard confirmation from the spec. Skipping an ingredient
    says you had at least this week's requirement, which is a real reading
    on your cupboard — so it writes an `observed_sufficient` ledger entry
    and corrects the running estimate.
    """
    if not db.query_one("SELECT 1 AS x FROM weeks WHERE id = ?", (week_id,)):
        raise HTTPException(404, "Week not found")

    data = shopping_list(week_id)
    needed = {e["ingredient_id"]: e for e in data["buy"]}

    db.execute("DELETE FROM week_cupboard_confirmations WHERE week_id = ?",
               (week_id,))
    db.execute("DELETE FROM cupboard_ledger WHERE week_id = ? "
               "AND source = 'observed_sufficient'", (week_id,))

    today = date.today().isoformat()
    for ing_id in body.ingredient_ids:
        entry = needed.get(ing_id)
        if not entry:
            continue
        qty, _ = quantities.to_canonical(
            entry["total"], entry["unit"], entry)
        db.execute(
            "INSERT INTO week_cupboard_confirmations (week_id, ingredient_id, "
            "buy, required_qty) VALUES (?, ?, 0, ?)",
            (week_id, ing_id, qty))
        if qty:
            db.execute(
                "INSERT INTO cupboard_ledger (ingredient_id, event_date, "
                "change, source, week_id, note) "
                "VALUES (?, ?, ?, 'observed_sufficient', ?, ?)",
                (ing_id, today, qty, week_id, "already had enough"))

    return {"ok": True, "skipped": len(body.ingredient_ids)}


@app.post("/weeks/{week_id}/order")
def mark_ordered(week_id: str):
    """Record that this week was actually ordered.

    This is what makes the system learn. Until a week is marked ordered,
    `last_cooked` stays null, the picker thinks nothing has ever been
    cooked, and the cupboard ledger has no purchases to reason about.

    Writes, in one transaction:
      - week status and ordered_at
      - last_cooked and times_cooked on each recipe
      - purchase entries for cupboard items (what came into the house)
      - consumption entries for cupboard items (what the recipes use)

    Fresh items aren't tracked — they spoil, so a running balance is
    meaningless. Staples aren't tracked either; they're never bought here.
    """
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    if week["status"] == "ordered":
        return {"ok": True, "note": "Already marked as ordered",
                "ordered_at": week["ordered_at"]}

    data = shopping_list(week_id)
    today = date.today().isoformat()
    recipes = db.query(
        "SELECT recipe_id, servings, cooked_date FROM week_recipes "
        "WHERE week_id = ? AND skipped = 0", (week_id,))

    purchases = consumptions = 0
    with db.transaction() as conn:
        conn.execute(
            "UPDATE weeks SET status = 'ordered', ordered_at = ? WHERE id = ?",
            (today, week_id))

        for r in recipes:
            conn.execute(
                "UPDATE recipes SET last_cooked = ?, "
                "times_cooked = times_cooked + 1 WHERE id = ?",
                (r["cooked_date"] or week["delivery_date"] or today, r["recipe_id"]))

        for entry in data["buy"]:
            if entry["category"] != "cupboard":
                continue
            bought = sum((l["product"].get("pack_size") or 0) * l["packs"]
                         for l in entry.get("lines", []))
            if bought:
                conn.execute(
                    "INSERT INTO cupboard_ledger (ingredient_id, event_date, "
                    "change, source, week_id) VALUES (?, ?, ?, 'purchase', ?)",
                    (entry["ingredient_id"], today, bought, week_id))
                purchases += 1

            used, _ = quantities.to_canonical(
                entry["total"], entry["unit"], entry)
            if used:
                conn.execute(
                    "INSERT INTO cupboard_ledger (ingredient_id, event_date, "
                    "change, source, week_id) "
                    "VALUES (?, ?, ?, 'consumption', ?)",
                    (entry["ingredient_id"], today, -used, week_id))
                consumptions += 1

    return {"ok": True, "ordered_at": today,
            "recipes_marked": len(recipes),
            "purchases_logged": purchases,
            "consumption_logged": consumptions}


def _revert_order_effects(week_id, conn):
    """Undo what marking a week as ordered wrote: cooked counts and ledger.

    Shared by /unorder and by deleting an ordered week, so a week that was
    marked ordered by mistake and then deleted doesn't leave a recipe
    permanently counted as cooked or an orphaned ledger entry behind —
    cupboard_ledger.week_id is ON DELETE SET NULL, not CASCADE, so without
    this the purchase/consumption rows would survive the week and quietly
    keep affecting the cupboard balance forever.
    """
    recipes = db.query(
        "SELECT recipe_id FROM week_recipes WHERE week_id = ? AND skipped = 0",
        (week_id,))
    conn.execute("DELETE FROM cupboard_ledger WHERE week_id = ? "
                 "AND source IN ('purchase', 'consumption')", (week_id,))
    for r in recipes:
        conn.execute(
            "UPDATE recipes SET times_cooked = MAX(0, times_cooked - 1) "
            "WHERE id = ?", (r["recipe_id"],))


@app.post("/weeks/{week_id}/unorder")
def unmark_ordered(week_id: str):
    """Undo an order marked by mistake. The week goes back to not-ordered;
    whether it then shows as ready or needs attention is worked out live
    from the shopping list, not stored."""
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    with db.transaction() as conn:
        conn.execute("UPDATE weeks SET status = 'ready', ordered_at = NULL "
                     "WHERE id = ?", (week_id,))
        _revert_order_effects(week_id, conn)
    return {"ok": True, "note": "last_cooked left as-is; set it if it matters"}


@app.delete("/weeks/{week_id}/recipes/{recipe_id}")
def remove_week_recipe(week_id: str, recipe_id: int):
    """Drop one recipe from a week. Only while it's still editable — once a
    week is ordered, its recipes are locked in alongside the stats already
    written for them; use "mark as not ordered" first if that needs undoing."""
    week = db.query_one("SELECT status FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    if week["status"] == "ordered":
        raise HTTPException(409, "This week is ordered — mark it as not "
                                 "ordered before editing its recipes")
    row = db.query_one("SELECT id FROM week_recipes WHERE week_id = ? "
                       "AND recipe_id = ?", (week_id, recipe_id))
    if not row:
        raise HTTPException(404, "That recipe isn't in this week")
    db.execute("DELETE FROM week_recipes WHERE id = ?", (row["id"],))
    return {"ok": True}


@app.delete("/weeks/{week_id}")
def delete_week(week_id: str):
    """Delete a week outright. If it was ordered, first undo what ordering
    wrote, so deleting a wrongly-marked week doesn't leave stray cooked
    counts or ledger entries behind."""
    week = db.query_one("SELECT * FROM weeks WHERE id = ?", (week_id,))
    if not week:
        raise HTTPException(404, "Week not found")
    with db.transaction() as conn:
        if week["status"] == "ordered":
            _revert_order_effects(week_id, conn)
        conn.execute("DELETE FROM weeks WHERE id = ?", (week_id,))
    return {"ok": True}


def _week_summaries(limit=50):
    """Weeks newest first, each with its recipes and a live-computed status.

    "Ready" isn't a status anyone sets — it's whatever the shopping list
    says right now. A week that was missing a product yesterday and got
    mapped today shows as ready today without anyone touching this row.
    """
    weeks = db.query(
        "SELECT id, retailer, status, created_at, ordered_at, "
        "       delivery_date, name FROM weeks "
        "ORDER BY created_at DESC LIMIT ?", (limit,))
    out = []
    for w in weeks:
        recipes = db.query(
            "SELECT r.id, r.title, r.hero_image FROM week_recipes wr "
            "JOIN recipes r ON r.id = wr.recipe_id "
            "WHERE wr.week_id = ? ORDER BY wr.sequence, wr.id", (w["id"],))
        if w["status"] == "ordered":
            display = "ordered"
        else:
            display = "ready" if shopping_list(w["id"])["ready"] else "attention"
        out.append({**w, "recipes": recipes, "display_status": display})
    # Soonest delivery first, past ones below. Orders without a date go last.
    today = date.today().isoformat()
    def key(w):
        d = w.get("delivery_date")
        if not d:
            return (2, "", w["created_at"])
        return (0, d, "") if d >= today else (1, "", d)
    upcoming = sorted([w for w in out if key(w)[0] == 0], key=lambda w: key(w)[1])
    past = sorted([w for w in out if key(w)[0] == 1],
                  key=lambda w: w["delivery_date"], reverse=True)
    undated = sorted([w for w in out if key(w)[0] == 2],
                     key=lambda w: w["created_at"], reverse=True)
    return upcoming + past + undated


@app.get("/orders", response_class=HTMLResponse)
@app.get("/weeks", response_class=HTMLResponse)
def weeks_page():
    """Every week ever built, newest first. The history that used to be a
    shrinking list at the bottom of the picker page."""
    return HTMLResponse(pages.shell(
        "Orders", "weeks", pages.weeks_list_html(_week_summaries()),
        show_logout=auth.configured()))


# TODO: PATCH /weeks/{id}    swap recipe, servings, status
# TODO: GET   /weeks/{id}/cost                basket and portion cost
# TODO: POST  /backup                         weekly cron, JSON dump to GitHub


# ---------------------------------------------------------------
# MCP
# ---------------------------------------------------------------
# Mounted last, so every route function it wraps already exists. The tools
# call these same functions rather than reimplementing anything.

if mcp_server:
    import sys

    # Bound now that every route function above exists. Tools call these
    # same functions rather than reimplementing anything.
    mcp_server.bind(sys.modules[__name__])
    for path, sub in _mcp_apps.items():
        app.mount(path, sub)
    print(f"[mcp] mounted at {', '.join(_mcp_apps)}", flush=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
