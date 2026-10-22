"""Rendered pages.

These are the point of the whole design: a browser agent gets a URL, not an
API integration. Keep them plain HTML — no build step, no framework.
"""

import retailers

# All design tokens live here. Restyling means editing this block.
TOKENS = """
 :root{
   --ink:#16181d;
   --ink-soft:#5c626e;
   --ink-faint:#9aa0ab;
   --paper:#fff;
   --rule:#e6e8ec;
   --wash:#f6f7f9;
   --accent:#1f6f4a;          /* step numbers, timers */
   --caution:#b3261e;         /* food safety */
   --tip:#8a6100;             /* optional advice */
   --serif:Georgia,"Iowan Old Style",Charter,serif;
   --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
   --measure:66ch;
 }
"""

STYLE = TOKENS + """
 body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      max-width:780px;margin:2.5rem auto;padding:0 1.2rem;color:#1a1a1a}
 h1{margin-bottom:.2rem}
 h2{margin-top:2.2rem;border-bottom:1px solid #e5e5e5;padding-bottom:.3rem}
 .meta{color:#666;margin-top:0}
 table{border-collapse:collapse;width:100%}
 td,th{padding:.45rem .5rem;border-bottom:1px solid #eee;text-align:left;
       vertical-align:top}
 th{font-size:13px;color:#666;font-weight:600}
 .qty{white-space:nowrap;color:#666}
 .price{text-align:right;white-space:nowrap}
 .note{background:#fff8e6;border-left:3px solid #e0a800;padding:.7rem 1rem;
       margin:1rem 0}
 .bad{background:#fdeaea;border-left:3px solid #d33;padding:.7rem 1rem;
      margin:1rem 0}
 .total{font-weight:600;font-size:17px}
 a{color:#0b6bcb}
 .sub{color:#888;font-size:13px}
 .flag{color:#b36b00;font-size:13px}
 .costs{display:flex;gap:2.5rem;margin:1.2rem 0}
 .costs div{line-height:1.3}
 .costs .n{font-size:22px;font-weight:600}
 .bar{display:flex;gap:.5rem;flex-wrap:wrap;margin:.6rem 0}
 button{font:inherit;padding:.4rem .9rem;cursor:pointer;background:#fff;
        border:1px solid #1a1a1a;color:#1a1a1a}
 button:hover{background:#1a1a1a;color:#fff}
 button.quiet{border-color:#ddd;color:#666}
 button.quiet:hover{background:#f4f5f6;color:#1a1a1a}
 .rthumbs{display:flex;flex-wrap:wrap;gap:1.4rem;margin:.8rem 0 0}
 .rthumb{display:flex;gap:.6rem;align-items:center;min-width:14rem}
 .rthumb img,.rthumb .ph{width:52px;height:52px;object-fit:cover;
    flex:none;background:#f0f1f3}
 .rthumb .t{font-weight:600;font-size:.95rem;line-height:1.25}
 @media print{body{margin:0;max-width:none}a{color:#000;text-decoration:none}}
"""


def _rows(items, week_id=None):
    out = ""
    for it in items:
        # A line can be several products: 900g as 600g + 300g.
        lines = it.get("lines") or [{"product": it.get("product") or {},
                                     "packs": it.get("packs", 1),
                                     "cost": it.get("line_cost", 0)}]
        products = ""
        for ln in lines:
            p = ln["product"]
            name = p.get("name", it["name"])
            link = (f'<a href="{p["url"]}" target="_blank">{name}</a>'
                    if p.get("url") else name)
            size = (f' <span class="sub">{ln["packs"]} &times; '
                    f'{p["pack_size"]:g}{p.get("pack_unit") or ""}</span>'
                    if p.get("pack_size") else "")
            products += f'<div>{link}{size}</div>'

        flags = ""
        if it.get("pack_note"):
            flags += f'<div class="flag">? {it["pack_note"]}</div>'
        if it.get("alternatives"):
            flags += (f'<div class="sub">{it["alternatives"]} other pack '
                      f'size(s) available</div>')

        tick = (f'<td><input type="checkbox" class="skip" '
                f'value="{it["ingredient_id"]}"></td>' if week_id else '')

        out += (
            f'<tr>{tick}<td>{products}<div class="sub">{it["name"]}'
            f'{" &middot; cupboard" if it["category"] == "cupboard" else ""}'
            f'</div>{flags}</td>'
            f'<td class="qty">{it["total"]:g} {it["unit"]}</td>'
            f'<td class="price">£{it.get("line_cost", 0):.2f}'
            f'<div class="sub">£{it.get("portion_cost", 0):.2f} used</div>'
            f'</td></tr>')
    return out


def shopping_list_html(week, data, recipes, base_url=""):
    recipe_list = "".join(
        '<div class="rthumb">'
        + (f'<img src="{r["hero_image"]}" alt="" loading="lazy">'
           if r.get("hero_image") else '<div class="ph"></div>')
        + f'<div><div class="t">{r["title"]}</div>'
        + f'<div class="sub">{r["servings"]} servings'
        + (f' &middot; {r["time_max"]} min' if r.get("time_max") else "")
        + '</div></div></div>' for r in recipes)

    warn = ""
    if data["unmatched"]:
        names = ", ".join(i["name"] for i in data["unmatched"])
        warn += (f'<div class="bad"><strong>{len(data["unmatched"])} '
                 f'ingredient(s) have no {retailers.name(week["retailer"])} product:</strong> '
                 f'{names}. This list is incomplete.</div>')

    if data["unresolved"]:
        names = ", ".join(f'{i["name"]} ({i["pack_note"]})'
                          for i in data["unresolved"])
        warn += (f'<div class="note"><strong>{len(data["unresolved"])} pack '
                 f'count(s) could not be calculated</strong> and default to 1: '
                 f'{names}. Add a unit conversion to the ingredient, or fix '
                 f'the pack unit on the product.</div>')

    skipped_note = ""
    if data.get("skipped"):
        names = ", ".join(i["name"] for i in data["skipped"])
        skipped_note = (f'<p class="sub">Ticked as already in stock this week: '
                        f'{names}.</p>')

    ordered_line = (
        f'Ordered on {week["ordered_at"]}. These recipes count as cooked and '
        f'the cupboard items are on the ledger.'
        if week.get("ordered_at") else
        'Marking this tells the picker what you\u2019ve cooked and starts '
        'tracking what\u2019s in your cupboard. Nothing else writes that.')

    # Two ways to hand the week over, because which works better depends on
    # how the trolley is being used the rest of the week.
    from datetime import date, timedelta
    monday = date.today() - timedelta(days=date.today().weekday())
    list_name = f"Meal planner w/c {monday.strftime('%-d %b')}"

    shop = retailers.name(week["retailer"])
    url = f"{base_url}/weeks/{week['id']}/list"

    common = (
        f"Each line shows the product, a link to it, and how many packs. Use "
        f"exactly those quantities. Don't substitute anything \u2014 if a "
        f"product is unavailable, tell me and leave it out. Don't place the "
        f"order.")

    cta_basket = (
        f"Open this page and add everything on it to my {shop} basket:\n\n"
        f"{url}\n\n{common}\n\n"
        f"My trolley already has items my wife and I added during the week, "
        f"so when you're done, read the whole trolley and give me three "
        f"lists:\n"
        f"1. On my recipe list and now in the trolley \u2014 the ones to "
        f"review\n"
        f"2. In the trolley but NOT on my recipe list \u2014 ours, leave "
        f"them alone\n"
        f"3. On my recipe list but missing from the trolley \u2014 anything "
        f"that failed to add\n\n"
        f"For group 1, note where the trolley quantity is higher than my "
        f"list asked for, since that means some was already there.")

    cta_list = (
        f"Open this page and put everything on it into a saved {shop} "
        f"shopping list called \u201c{list_name}\u201d \u2014 not the "
        f"trolley:\n\n"
        f"{url}\n\n{common}\n\n"
        f"Create the list if it doesn't exist. If a list with that name is "
        f"already there, replace its contents rather than adding to them, so "
        f"re-running doesn't double anything up.\n\n"
        f"Leave my trolley completely untouched \u2014 it has things my wife "
        f"and I added during the week and I want them kept separate. When "
        f"you're done, tell me what went on the list and anything that "
        f"couldn't be added.")

    stocked = ""
    if data["assumed_stocked"]:
        names = ", ".join(i["name"] for i in data["assumed_stocked"])
        stocked = (f'<p class="sub">Assumed already in stock, not ordered: '
                   f'{names}.</p>')

    return f"""<!doctype html><meta charset="utf-8">
<title>Shopping list &middot; {week['retailer']}</title>
<style>{STYLE}</style>
<h1>Shopping list</h1>
<p class="meta">{retailers.name(week['retailer'])} &middot; {len(recipes)} recipes</p>

<div class="costs">
  <div><span class="n">£{data['basket_cost']:.2f}</span><br>
       <span class="sub">basket &mdash; whole packs</span></div>
  <div><span class="n">£{data['portion_cost']:.2f}</span><br>
       <span class="sub">portion &mdash; what you actually eat</span></div>
</div>

<h2>Recipes</h2>
<div class="rthumbs">{recipe_list}</div>

{warn}

<h2>Add to basket</h2>
<p class="sub">Tick anything you already have &mdash; it drops off the list and
   tells the system your cupboard was stocked.</p>
<table><tr><th></th><th>Product</th><th>Needed</th>
       <th class="price">Cost</th></tr>
{_rows(data['buy'], week['id'])}</table>
<button onclick="saveSkips()">Update list</button>
{skipped_note}
{stocked}

<p><a href="/weeks/{week['id']}/cards">Recipe cards for this week &rarr;</a></p>

<h2>When it&rsquo;s in the basket</h2>
<p class="sub">{ordered_line}</p>
<button onclick="markOrdered()">I&rsquo;ve ordered this</button>

<h2>Hand it to the shopper</h2>
<p class="sub">Copy one of these into a Cowork session with {shop} open.</p>
<div class="bar">
  <button onclick="showCta('basket')" id="tab-basket">Add to basket</button>
  <button onclick="showCta('list')" id="tab-list"
    class="quiet">Save as &ldquo;{list_name}&rdquo;</button>
</div>
<p class="sub" id="ctahint">Adds straight to the trolley, then tells you which
   items were already yours.</p>
<pre id="cta">{cta_basket}</pre>
<pre id="cta-list" style="display:none">{cta_list}</pre>
<button onclick="copyCta()">Copy instruction</button>

<script>
async function saveSkips() {{
  const ids = [...document.querySelectorAll('.skip:checked')]
    .map(c => +c.value);
  await fetch('/weeks/{week['id']}/skip', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{ingredient_ids: ids}})
  }});
  location.reload();
}}
let ctaMode = 'basket';
function showCta(mode) {{
  ctaMode = mode;
  const basket = mode === 'basket';
  document.getElementById('cta').style.display = basket ? '' : 'none';
  document.getElementById('cta-list').style.display = basket ? 'none' : '';
  document.getElementById('tab-basket').className = basket ? '' : 'quiet';
  document.getElementById('tab-list').className = basket ? 'quiet' : '';
  document.getElementById('ctahint').textContent = basket
    ? 'Adds straight to the trolley, then tells you which items were already yours.'
    : 'Builds a named shopping list and leaves your trolley alone. Add it to the trolley yourself when you are ready to order.';
}}
function copyCta() {{
  const el = document.getElementById(ctaMode === 'basket' ? 'cta' : 'cta-list');
  navigator.clipboard.writeText(el.textContent);
}}
async function markOrdered() {{
  const r = await fetch('/weeks/{week['id']}/order', {{method: 'POST'}});
  const j = await r.json();
  alert(j.ok ? 'Recorded. These recipes now count as cooked, and the '
             + 'cupboard items are on the ledger.'
             : JSON.stringify(j));
  location.reload();
}}
</script>

<p class="sub">Prices are the last recorded value per product and may be out of
date. Basket cost counts whole packs; portion cost counts only the amount the
recipes use, and is the fair comparison against a recipe box.</p>
"""


# ---------------------------------------------------------------
# Recipe cards
# ---------------------------------------------------------------

CARD_STYLE = TOKENS + """
 *{box-sizing:border-box}
 body{margin:0;background:var(--wash);color:var(--ink);font-family:var(--sans)}
 .card{background:var(--paper);max-width:1000px;margin:0 auto 2rem;
       padding:2.5rem 3rem 3rem}
 .head{display:grid;grid-template-columns:1fr 320px;gap:2.5rem;
       align-items:start}
 h1{font-family:var(--serif);font-weight:400;font-size:2.4rem;line-height:1.15;
    margin:0 0 .3rem;letter-spacing:-.01em}
 .sub{font-family:var(--serif);font-style:italic;font-size:1.15rem;
      color:var(--ink-soft);margin:0 0 1rem}
 .facts{font-size:.9rem;color:var(--ink-soft);margin:0 0 1.5rem}
 .babynote{font-size:.88rem;background:#eef7f1;border-left:3px solid var(--accent);
           padding:.4rem .7rem;margin:-.6rem 0 1.4rem}
 .facts b{color:var(--ink);font-weight:600}
 .hero{width:100%;aspect-ratio:4/3;object-fit:cover}
 .ing{width:100%;border-collapse:collapse;margin-top:.5rem}
 .ing td{padding:.35rem 0;border-bottom:1px solid var(--rule);
         font-size:.95rem;vertical-align:baseline}
 .ing td:last-child{text-align:right;color:var(--ink-soft);
                    white-space:nowrap;font-variant-numeric:tabular-nums}
 .ing .prep{color:var(--ink-faint);font-size:.85rem}
 h2{font-size:.8rem;font-weight:700;letter-spacing:.06em;
    color:var(--ink-faint);margin:2rem 0 .4rem;font-family:var(--sans)}
 .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:2rem 2.2rem;
        margin-top:1rem}
 .step img{width:100%;aspect-ratio:3/2;object-fit:cover;margin-bottom:.7rem}
 .n{font-family:var(--serif);font-size:2rem;color:var(--accent);
    line-height:1;margin-bottom:.2rem}
 .step h3{font-size:1.05rem;margin:.1rem 0 .5rem;font-weight:650}
 .step p{margin:0 0 .6rem;font-size:.95rem;line-height:1.5;
         max-width:var(--measure)}
 .step b{font-weight:650}
 .callout{font-size:.9rem;line-height:1.45;padding-left:.7rem;
          border-left:2px solid;margin:0 0 .6rem}
 .callout.tip{border-color:var(--tip);color:var(--tip)}
 .callout.important{border-color:var(--caution);color:var(--caution)}
 .timer{display:inline-block;font-size:.8rem;color:var(--accent);
        border:1px solid var(--accent);padding:.1rem .45rem;margin-top:.2rem}
 .foot{margin-top:2.5rem;padding-top:1rem;border-top:1px solid var(--rule);
       font-size:.82rem;color:var(--ink-faint);display:flex;gap:2.5rem;
       flex-wrap:wrap}
 .foot div{max-width:22rem}
 @media (max-width:820px){
   .head{grid-template-columns:1fr}
   .steps{grid-template-columns:1fr 1fr}
   .card{padding:1.5rem}
 }
 @media (max-width:560px){.steps{grid-template-columns:1fr}}
 .toolbar{max-width:1000px;margin:1.2rem auto 0;padding:0 3rem;
          display:flex;gap:.6rem;align-items:center;font-size:.85rem}
 .toolbar a{color:var(--ink-soft);text-decoration:none}
 .toolbar a:hover{color:var(--ink)}
 .toolbar .spacer{flex:1}
 .toolbar .archnote{color:var(--caution);font-weight:600;margin-right:.2rem}
 .toolbar button{font:inherit;padding:.4rem .9rem;cursor:pointer;
                 background:#fff;border:1px solid var(--ink);color:var(--ink)}
 .toolbar button:hover{background:var(--ink);color:#fff}
 .toolbar button.quiet{border-color:var(--rule);color:var(--ink-soft)}
 .toolbar button.quiet:hover{background:var(--wash);color:var(--ink)}
 @media (max-width:820px){.toolbar{padding:0 1.5rem}}
 @media print{
   body{background:#fff}
   .card{max-width:none;margin:0;padding:1.2cm;page-break-after:always}
   .steps{gap:1.2rem}
   a{color:inherit;text-decoration:none}
   .toolbar{display:none}
 }
"""


def _bold_ingredients(text, names):
    """Bold ingredient mentions, the way a recipe card does.

    Longest first, so "chicken stock paste" wins over "chicken".
    """
    import re
    for name in sorted(names, key=len, reverse=True):
        if len(name) < 3:
            continue
        text = re.sub(rf'(?<!<b>)\b({re.escape(name)}s?)\b', r'<b>\1</b>',
                      text, flags=re.I)
    return text


def _mmss(seconds):
    if not seconds:
        return ""
    mins = round(seconds / 60)
    return f'{mins} min' if mins < 60 else f'{mins // 60}h {mins % 60:02d}'


def recipe_card_html(recipe, ingredients, steps, servings, layout="screen"):
    """One recipe card.

    `layout="print"` puts ingredients in their own column beside the method
    so the whole recipe fits one landscape sheet. On screen they sit under
    the title, because a scrolling page has height to spare.
    """
    names = [i["name"] for i in ingredients]

    ing_rows = "".join(
        f'<tr><td>{i["name"]}'
        + (f'<div class="prep">{i["prep_note"]}</div>' if i.get("prep_note")
           else "")
        + f'</td><td>{i["qty"]:g} {i["unit"]}</td></tr>'
        for i in ingredients if i.get("qty"))

    step_html = ""
    for st in steps:
        body = ""
        for sub in (st.get("substeps") or []):
            text = _bold_ingredients(sub.get("text", ""), names)
            callout = sub.get("callout")
            if callout in ("tip", "important"):
                label = "Tip" if callout == "tip" else "Important"
                body += f'<p class="callout {callout}">{label}. {text}</p>'
            else:
                body += f'<p>{text}</p>'
        img = (f'<img src="{st["image"]}" alt="">' if st.get("image") else "")
        timer = (f'<div class="timer">{_mmss(st["timer_seconds"])}</div>'
                 if st.get("timer_seconds") else "")
        step_html += (f'<div class="step">{img}'
                      f'<div class="n">{st["step_number"]}</div>'
                      f'<h3>{st.get("title") or ""}</h3>{body}{timer}</div>')

    time = ""
    if recipe.get("time_max"):
        lo, hi = recipe.get("time_min"), recipe["time_max"]
        time = f'{lo}&ndash;{hi} min' if lo and lo != hi else f'{hi} min'

    facts = " &nbsp;·&nbsp; ".join(filter(None, [
        f'<b>{servings}</b> servings',
        time,
        ", ".join(recipe.get("tags") or []),
    ]))

    nut = recipe.get("nutrition") or {}
    nut_line = ""
    if nut:
        parts = [f'{v:g} {k.replace("energy_kcal", "kcal").replace("_", " ")}'
                 for k, v in nut.items() if isinstance(v, (int, float))]
        nut_line = (f'<div><b>Per serving</b><br>{", ".join(parts)}</div>')

    tools = (f'<div><b>You&rsquo;ll need</b><br>'
             f'{", ".join(recipe.get("tools") or [])}</div>'
             if recipe.get("tools") else "")
    allergens = (f'<div><b>Allergens</b><br>'
                 f'{", ".join(recipe.get("allergens") or [])}</div>'
                 if recipe.get("allergens") else "")

    hero = (f'<img class="hero" src="{recipe["hero_image"]}" alt="">'
            if recipe.get("hero_image") else "")

    baby_note = ""
    if recipe.get("baby_note") and recipe.get("baby_friendly") in (1, 2):
        import html as _html
        baby_note = ('<p class="babynote"><b>For the baby:</b> '
                     + _html.escape(recipe["baby_note"]) + '</p>')

    title_block = (
        f'<h1>{recipe["title"]}</h1>'
        + (f'<p class="sub">{recipe["subtitle"]}</p>'
           if recipe.get("subtitle") else "")
        + f'<p class="facts">{facts}</p>'
        + baby_note)
    ing_block = f'<h2>Ingredients</h2><table class="ing">{ing_rows}</table>'
    method_block = f'<h2>Method</h2><div class="steps">{step_html}</div>'
    foot_block = f'<div class="foot">{nut_line}{tools}{allergens}</div>'

    if layout == "print":
        return (f'<section class="card">'
                f'<div class="head"><div>{title_block}</div>'
                f'<div>{hero}</div></div>'
                f'<div class="body-cols">'
                f'<div class="col-ing">{ing_block}</div>'
                f'<div class="col-method">{method_block}</div></div>'
                f'{foot_block}</section>')

    return (f'<section class="card">'
            f'<div class="head"><div>{title_block}{ing_block}</div>'
            f'<div>{hero}</div></div>'
            f'{method_block}{foot_block}</section>')


def cards_page_html(cards, title="Recipe cards", recipe_id=None, status=None):
    """`recipe_id`/`status` are only set for the standalone single-recipe
    view (/recipes/{id}/card) — they add the Archive/Restore + Print
    toolbar. Left unset for the week print sheet, which stays print-only
    chrome-free (the toolbar is hidden under @media print anyway, but
    there's no reason to render archive controls for several recipes at
    once there)."""
    toolbar, script = "", ""
    if recipe_id is not None:
        if status == "archived":
            action_btn = ('<span class="archnote">Archived</span>'
                          '<button onclick="setArchived(false)">Restore'
                          '</button>')
        else:
            action_btn = ('<button class="quiet" '
                          'onclick="setArchived(true)">Archive this recipe'
                          '</button>')
        toolbar = (
            '<div class="toolbar"><a href="/">&larr; All recipes</a>'
            '<span class="spacer"></span>'
            f'<a href="/recipes/{recipe_id}/receipt">Receipt</a>'
            '<button class="quiet" onclick="window.print()">Print</button>'
            f'{action_btn}</div>')
        script = f"""<script>
async function setArchived(archive) {{
  if (archive && !confirm('Archive this recipe? It stops showing up in '
      + 'the recipe picker and week suggestions — you can restore it '
      + 'any time from Maintenance → Archived recipes.')) return;
  const action = archive ? 'archive' : 'unarchive';
  const r = await fetch('/recipes/{recipe_id}/' + action, {{method: 'POST'}});
  if (r.ok) location.reload();
  else alert('Could not update: ' + (await r.text()));
}}
</script>"""
    return (f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="viewport" content="width=device-width,'
            f'initial-scale=1"><style>{CARD_STYLE}</style>'
            f'{toolbar}' + "".join(cards) + script)


def receipt_preview_html(recipe_id, title, servings, photos, width,
                         height_cm):
    """The receipt as it will print, with the few choices that change it."""
    import html as _h

    def opts(choices, current):
        return "".join(
            f'<option value="{v}"{" selected" if str(v) == str(current) else ""}>'
            f'{label}</option>' for v, label in choices)

    q = f"servings={servings}&photos={photos}&width={width}"
    return f"""<!doctype html><meta charset="utf-8">
<title>Receipt: {_h.escape(title)}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{{--ink:#16181d;--ink-soft:#5c626e;--rule:#e6e8ec;--wash:#f0f1f4;
        --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
 body{{margin:0;font-family:var(--sans);color:var(--ink);background:var(--wash)}}
 .top{{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center;
       padding:.7rem 1rem;background:#fff;border-bottom:1px solid var(--rule);
       position:sticky;top:0;z-index:5}}
 .top a{{color:var(--ink-soft);font-size:.9rem}}
 .top label{{font-size:.82rem;color:var(--ink-soft)}}
 .top select{{font:inherit;font-size:.88rem;padding:.25rem .4rem;
              border:1px solid var(--rule);background:#fff;margin-left:.25rem}}
 .spacer{{flex:1}}
 .note{{font-size:.8rem;color:var(--ink-soft);padding:.6rem 1rem 0;
        text-align:center}}
 .paper{{margin:1rem auto 3rem;background:#fff;width:{width}px;
         max-width:calc(100% - 1.5rem);
         box-shadow:0 1px 10px rgba(0,0,0,.18)}}
 .paper img{{display:block;width:100%;height:auto;image-rendering:auto}}
</style>
<form class="top" method="get">
 <a href="/recipes/{recipe_id}/card">&larr; Recipe card</a>
 <span class="spacer"></span>
 <label>Servings
  <select name="servings" onchange="this.form.submit()">
   {opts([(2, "2"), (3, "3"), (4, "4")], servings)}</select></label>
 <label>Photos
  <select name="photos" onchange="this.form.submit()">
   {opts([("none", "None"), ("hero", "Main photo only"),
          ("all", "Main and steps")], photos)}</select></label>
 <label>Paper
  <select name="width" onchange="this.form.submit()">
   {opts([(576, "80 mm"), (384, "58 mm")], width)}</select></label>
 <a href="/recipes/{recipe_id}/receipt.png?{q}" download="receipt.png">
  Save image</a>
</form>
<p class="note">Preview &middot; about {height_cm:.0f} cm of paper</p>
<div class="paper"><img src="/recipes/{recipe_id}/receipt.png?{q}"
 alt="Receipt for {_h.escape(title)}"></div>"""


# ---------------------------------------------------------------
# Recipe picker grid
# ---------------------------------------------------------------

GRID_STYLE = """
 .grid{display:grid;gap:1.1rem;margin:1.2rem 0;
       grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
 .rcard{position:relative;cursor:pointer;border:1px solid var(--rule);
        background:var(--paper);overflow:hidden;transition:border-color .12s}
 .rcard:hover{border-color:var(--ink-faint)}
 .rcard.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
 .rcard .thumb{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;
               background:var(--wash)}
 .rcard .noimg{width:100%;aspect-ratio:4/3;background:var(--wash);
               display:flex;align-items:center;justify-content:center;
               color:var(--ink-faint);font-size:.8rem}
 .rcard .body{padding:.6rem .7rem .7rem}
 .rcard h4{margin:0 0 .25rem;font-size:.92rem;line-height:1.3;font-weight:600}
 .rcard .m{font-size:.78rem;color:var(--ink-faint)}
 .rcard .tick{position:absolute;top:.5rem;left:.5rem;width:22px;height:22px;
              border-radius:50%;background:rgba(255,255,255,.92);
              border:1px solid var(--rule);display:flex;align-items:center;
              justify-content:center;font-size:13px;color:transparent}
 .rcard.on .tick{background:var(--accent);border-color:var(--accent);
                 color:#fff}
 .rcard .srv{position:absolute;top:.5rem;right:.5rem;
             background:rgba(255,255,255,.92);border:1px solid var(--rule);
             font-size:.78rem;padding:.1rem}
 .rcard .babytag{font-size:.65rem;font-weight:600;letter-spacing:.04em;
    color:var(--accent);border:1px solid var(--accent);padding:0 .25rem;
    margin-left:.35rem;vertical-align:middle;text-transform:uppercase}
 .rcard .babytag.change{border-style:dashed}
 .bar.more{padding:.6rem;background:var(--wash);border:1px solid var(--rule);
           margin-top:0}
 .bar input[type=search]{font:inherit;font-size:.85rem;padding:.3rem .5rem;
                         border:1px solid var(--rule);min-width:11rem}
 .morebtn .chev{font-size:.6rem;display:inline-block;transition:transform .15s}
 .morebtn.open .chev{transform:rotate(180deg)}
 .clearf{align-self:center;font-size:.82rem;color:var(--ink-soft)}
 .rcard .price-ok{color:var(--accent);font-weight:600}
 .rcard .price-part{color:var(--tip)}
 .cooked{opacity:.55}
 .cooked:hover{opacity:1}
 .rcard{display:flex;flex-direction:column}
 .rcard .body{flex:1;display:flex;flex-direction:column}
 .rcard .links{margin-top:auto;padding-top:.5rem;display:flex;
               justify-content:space-between;align-items:baseline;gap:.5rem}
 .rcard .peek{font-size:.78rem;color:var(--ink-faint);text-decoration:underline;
              cursor:pointer;background:none;border:0;font-family:inherit;
              padding:0}
 .rcard .peek:hover{color:var(--ink)}
 #ov{position:fixed;inset:0;background:rgba(20,22,26,.45);display:none;
     align-items:center;justify-content:center;z-index:50;padding:1rem}
 #ov.on{display:flex}
 #ovbox{background:var(--paper);max-width:560px;width:100%;max-height:82vh;
        overflow:auto;padding:1.6rem 1.8rem}
 #ovbox h3{margin:0 0 .2rem;font-family:var(--serif);font-weight:400;
           font-size:1.4rem}
 #ovbox table{width:100%;border-collapse:collapse;margin-top:.8rem}
 #ovbox td{padding:.4rem 0;border-bottom:1px solid var(--rule);
           font-size:.9rem;vertical-align:top}
 #ovbox td:last-child{text-align:right;white-space:nowrap}
 #ovbox td a.ingname{color:var(--ink);text-decoration:underline;
                     text-decoration-color:var(--rule)}
 #ovbox td a.ingname:hover{color:var(--accent);text-decoration-color:var(--accent)}
 #ovbox .prob{color:var(--caution);font-size:.8rem}
 #ovbox .quietnote{color:var(--ink-faint);font-size:.8rem}
 #ovbox .editnote{background:var(--wash);border-left:2px solid var(--ink-faint);
                  color:var(--ink-soft);font-size:.78rem;line-height:1.4;
                  padding:.5rem .7rem;margin:.6rem 0 .9rem}
 #ovbox .qedit{display:flex;gap:.3rem;align-items:center;margin-top:.25rem;
               flex-wrap:wrap}
 #ovbox .qedit input{font:inherit;font-size:.8rem;padding:.15rem .3rem;
                     border:1px solid var(--rule)}
 #ovbox .qedit button{font-size:.75rem;padding:.15rem .5rem}
 .actionbar{position:fixed;left:0;right:0;bottom:0;z-index:40;
            background:var(--paper);border-top:1px solid var(--rule);
            padding:.75rem 1.2rem calc(.75rem + env(safe-area-inset-bottom));
            box-shadow:0 -2px 10px rgba(0,0,0,.05)}
 .actionbar .inner{max-width:940px;margin:0 auto;display:flex;gap:.6rem;
                   align-items:center}
 .actionbar .count{color:var(--ink-soft);font-size:.9rem;margin-right:auto;
                   background:none;border:0;font:inherit;cursor:pointer;
                   display:flex;align-items:center;gap:.4rem;padding:0}
 .actionbar .count:hover{color:var(--ink)}
 .actionbar .count .chev{font-size:.65rem;transition:transform .15s}
 .actionbar .count.open .chev{transform:rotate(180deg)}
 .actionbar .primary{background:var(--ink);color:var(--paper);
                     border:1px solid var(--ink);padding:.6rem 1.2rem;
                     font-weight:600}
 .actionbar .primary:disabled{background:var(--rule);border-color:var(--rule);
                              color:var(--ink-faint);cursor:default}
 .actionbar .suggest{display:flex;align-items:center;gap:.4rem}
 body.has-actionbar{padding-bottom:6rem}
 .selpanel{display:none;position:absolute;left:0;right:0;bottom:100%;
           max-height:44vh;overflow:auto;background:var(--paper);
           border-top:1px solid var(--rule);
           box-shadow:0 -2px 10px rgba(0,0,0,.05)}
 .selpanel.on{display:block}
 .selpanel .srow{display:flex;align-items:center;gap:.7rem;
                 padding:.55rem 1.2rem;border-bottom:1px solid var(--rule);
                 max-width:940px;margin:0 auto}
 .selpanel .srow img,.selpanel .srow .ph{width:36px;height:36px;
                 object-fit:cover;flex:none;background:var(--wash)}
 .selpanel .srow .t{flex:1;font-size:.88rem;font-weight:600}
 .selpanel .srow .links{display:flex;gap:.9rem;font-size:.78rem;
                        margin-left:auto}
 .selpanel .srow .links a{color:var(--ink-soft);text-decoration:underline}
 .selpanel .srow .links a:hover{color:var(--ink)}
 .selpanel .srow .t{flex:0 1 auto}
 .selpanel .srow button.x{background:none;border:0;font-size:1.3rem;
                          line-height:1;color:var(--ink-faint);padding:0 .3rem;
                          cursor:pointer}
 .selpanel .srow button.x:hover{color:var(--caution)}
 .selpanel .empty{padding:.9rem 1.2rem;color:var(--ink-faint);
                  font-size:.85rem;text-align:center}
 @media (max-width:600px){
   .actionbar .inner{flex-wrap:wrap}
   .actionbar .count{width:100%;margin:0 0 .2rem}
   .actionbar .primary{flex:1 1 100%;order:3;padding:.8rem}
   .actionbar .suggest{flex:1 1 100%;order:2}
   .actionbar .suggest button{flex:1}
   body.has-actionbar{padding-bottom:10rem}
 }
 .divider{grid-column:1/-1;font-size:.8rem;color:var(--ink-faint);
          border-top:1px solid var(--rule);padding-top:.8rem;margin-top:.4rem}
"""


def _ago(iso):
    from datetime import date, datetime
    if not iso:
        return "never cooked"
    try:
        then = datetime.fromisoformat(iso).date()
    except ValueError:
        return ""
    days = (date.today() - then).days
    if days <= 0:
        return "cooked today"
    if days < 14:
        return f"cooked {days}d ago"
    if days < 70:
        return f"cooked {days // 7}w ago"
    return f"cooked {days // 30}mo ago"


SOURCE_LABELS = {
    "hellofresh": "HelloFresh", "greenchef": "Green Chef",
    "green_chef": "Green Chef", "bbc_food": "BBC Food",
    "bbc_good_food": "BBC Good Food", "ocado": "Ocado", "manual": "Own",
}


def _baby_tag(r):
    """1 = fine as it is; 2 = fine with a change (the note says what)."""
    import html as _html
    flag = r.get("baby_friendly")
    if flag == 1:
        return '<span class="babytag" title="Baby friendly">baby</span>'
    if flag == 2:
        note = _html.escape(r.get("baby_note") or "")
        return ('<span class="babytag change" title="Baby friendly with a '
                f'change: {note}">baby*</span>')
    return ""


def recipe_grid_html(recipes, default_servings=4, costs=None, retailer=None,
                     sort="recent"):
    """Pick by photo.

    Default order is longest-uncooked first, with recently cooked dimmed at
    the bottom — the thing you want most weeks. Other sorts are flat, since
    a cooked/uncooked split would fight whatever you sorted by.

    Costs are per portion at one retailer — a week is ordered from a single
    shop, so a per-recipe "cheapest anywhere" figure would be a total that
    can't be spent.
    """
    costs = costs or {}

    def priced(r):
        baby = _baby_tag(r)

        c = costs.get(r["id"])
        return c["per_portion"] if c and not c["unpriced"] else float("inf")

    if sort == "price":
        ordered, split = sorted(recipes, key=lambda r: (priced(r),
                                                        r["title"])), False
    elif sort == "time":
        ordered, split = sorted(recipes, key=lambda r: (r["time_max"] or 999,
                                                        r["title"])), False
    elif sort == "title":
        ordered, split = sorted(recipes, key=lambda r: r["title"]), False
    else:
        ordered, split = recipes, True

    if split:
        fresh = [r for r in recipes if not r["last_cooked"]]
        cooked = sorted([r for r in recipes if r["last_cooked"]],
                        key=lambda r: (r["last_cooked"] or "", r["title"]))
    else:
        fresh, cooked = ordered, []

    def card(r, dim=False):
        thumb = (f'<img class="thumb" src="{r["hero_image"]}" alt="" '
                 f'loading="lazy">' if r.get("hero_image")
                 else '<div class="noimg">No photo</div>')
        meta = " · ".join(filter(None, [
            r.get("protein") or "",
            f'{r["time_max"]} min' if r.get("time_max") else "",
            SOURCE_LABELS.get(r.get("source"), r.get("source") or ""),
        ]))
        opts = "".join(
            f'<option{" selected" if n == default_servings else ""}>{n}</option>'
            for n in (2, 3, 4))

        baby = _baby_tag(r)

        c = costs.get(r["id"])
        if c is None:
            price = ""
        elif c["unpriced"]:
            n = len(c["unpriced"])
            price = (f'<div class="m price-part" title="Not priced: '
                     f'{", ".join(c["unpriced"][:6])}">£{c["per_portion"]:.2f} '
                     f'a portion &middot; {n} item{"s" if n > 1 else ""} '
                     f'unpriced</div>')
        else:
            price = (f'<div class="m price-ok">£{c["per_portion"]:.2f} '
                     f'a portion &middot; £{c["cost"]:.2f} total</div>')
        return (
            f'<div class="rcard{" cooked" if dim else ""}" id="c{r["id"]}" '
            f'onclick="pick({r["id"]},event)">'
            f'{thumb}<div class="tick">&#10003;</div>'
            f'<select class="srv" id="s{r["id"]}" '
            f'onclick="event.stopPropagation()">{opts}</select>'
            f'<div class="body"><h4>{r["title"]}</h4>'
            f'<div class="m">{meta}</div>'
            f'<div class="m">{_ago(r["last_cooked"])}</div>'
            f'{price}'
            f'<div class="links">'
            f'<span class="peek" onclick="peek({r["id"]},event)">'
            f'ingredients</span>'
            f'<span class="peek" onclick="openCard({r["id"]},event)">'
            f'recipe card</span>'
            f'</div></div></div>')

    html = "".join(card(r) for r in fresh)
    if cooked:
        if fresh:
            html += '<div class="divider">Cooked recently</div>'
        html += "".join(card(r, dim=True) for r in cooked)
    return f'<div class="grid">{html}</div>'


# ---------------------------------------------------------------
# Weeks list
# ---------------------------------------------------------------

STATUS_LABEL = {
    "ordered": ("Ordered", "ok"),
    "ready": ("Ready to order", "warn"),
    "attention": ("Needs attention", "bad"),
}


def long_date(iso):
    """'Tuesday 20 October' (year added only when it isn't this year)."""
    if not iso:
        return ""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(iso[:10])
    except ValueError:
        return iso[:10]
    year = f" {d.year}" if d.year != _date.today().year else ""
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}{year}"


def order_title(w):
    """The delivery date as the title; falls back to a plain label."""
    return long_date(w.get("delivery_date")) or "No delivery date"


def weeks_list_html(weeks):
    """Every order. "Ready" vs "needs attention" is never stored — see
    _week_summaries — so this list always reflects today's product mapping,
    not whatever was true when the order was created."""
    import html as _html
    import json as _json
    cards = ""
    cal = []
    for w in weeks:
        thumbs = "".join(
            f'<div class="wthumb" title="{_html.escape(r["title"])}">'
            + (f'<img src="{r["hero_image"]}" alt="" loading="lazy">'
               if r.get("hero_image") else "")
            + '</div>'
            for r in w["recipes"][:4])
        label, kind = STATUS_LABEL[w["display_status"]]
        n = len(w["recipes"])
        meta = f'{n} recipe' + ('s' if n != 1 else '')
        meta += f' &middot; {retailers.name(w["retailer"])}'
        name = _html.escape(w.get("name") or "")
        title = order_title(w)
        dated = "" if w.get("delivery_date") else " undated"
        name_line = f'<div class="wname">{name}</div>' if name else ""
        created = (f'<div class="wcreated">Created '
                   f'{short_date(w["created_at"])}</div>')
        cards += (
            f'<div class="wcard" data-id="{w["id"]}" '
            f'data-date="{w.get("delivery_date") or ""}" '
            f'data-name="{name}">'
            f'<a class="wlink" href="/weeks/{w["id"]}/cards">'
            f'<div class="wthumbs">{thumbs}</div>'
            f'<div class="wrow"><span class="wdate{dated}">{title}</span>'
            f'<span class="wstatus {kind}">{label}</span></div>'
            f'{name_line}<div class="m">{meta}</div>{created}</a>'
            f'<button class="quiet wedit" onclick="editOrder(this)">'
            f'Edit date &amp; name</button></div>')
        cal.append({"id": w["id"], "date": w.get("delivery_date") or "",
                    "name": w.get("name") or "", "kind": kind, "n": n})

    empty = '<p class="sub">No orders yet — build one from All recipes.</p>'
    data = _json.dumps(cal).replace("</", "<\\/")
    return f"""
<div class="whead">
  <h1>Orders</h1>
  <div class="vtoggle" role="group" aria-label="View">
    <button id="vlist" onclick="setView('list')">List</button>
    <button id="vcal" onclick="setView('cal')">Calendar</button>
  </div>
</div>
<p class="sub">Soonest delivery first, past orders below.</p>
<div id="listview" class="wlist">{cards or empty}</div>
<div id="calview" style="display:none">
  <div class="calnav">
    <button class="quiet" onclick="calMove(-1)" aria-label="Previous month">&larr;</button>
    <strong id="calttl"></strong>
    <button class="quiet" onclick="calMove(1)" aria-label="Next month">&rarr;</button>
    <button class="quiet" onclick="calToday()">Today</button>
  </div>
  <div class="calgrid" id="calgrid"></div>
  <div id="calundated"></div>
</div>
<script>
const ORDERS = {data};
let calY, calM;
function setView(v) {{
  document.getElementById('listview').style.display = v === 'list' ? '' : 'none';
  document.getElementById('calview').style.display = v === 'cal' ? '' : 'none';
  document.getElementById('vlist').classList.toggle('on', v === 'list');
  document.getElementById('vcal').classList.toggle('on', v === 'cal');
  try {{ localStorage.setItem('ordersView', v); }} catch (e) {{}}
  if (v === 'cal') renderCal();
}}
function calToday() {{
  const t = new Date(); calY = t.getFullYear(); calM = t.getMonth(); renderCal();
}}
function calMove(d) {{
  calM += d;
  if (calM < 0) {{ calM = 11; calY--; }}
  if (calM > 11) {{ calM = 0; calY++; }}
  renderCal();
}}
function iso(y, m, d) {{
  return y + '-' + String(m + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
}}
function renderCal() {{
  const months = ['January','February','March','April','May','June','July',
    'August','September','October','November','December'];
  document.getElementById('calttl').textContent = months[calM] + ' ' + calY;
  const g = document.getElementById('calgrid');
  g.innerHTML = '';
  ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(n => {{
    const h = document.createElement('div'); h.className = 'calh';
    h.textContent = n; g.appendChild(h);
  }});
  const first = new Date(calY, calM, 1);
  const lead = (first.getDay() + 6) % 7;
  const days = new Date(calY, calM + 1, 0).getDate();
  const todayIso = iso(new Date().getFullYear(), new Date().getMonth(), new Date().getDate());
  for (let i = 0; i < lead; i++) g.appendChild(document.createElement('div'));
  for (let d = 1; d <= days; d++) {{
    const cell = document.createElement('div');
    const key = iso(calY, calM, d);
    cell.className = 'calc' + (key === todayIso ? ' today' : '');
    const num = document.createElement('span');
    num.className = 'cn'; num.textContent = d; cell.appendChild(num);
    ORDERS.filter(o => o.date === key).forEach(o => {{
      const a = document.createElement('a');
      a.className = 'chip ' + o.kind;
      a.href = '/weeks/' + o.id + '/cards';
      a.textContent = o.name || (o.n + ' recipe' + (o.n === 1 ? '' : 's'));
      cell.appendChild(a);
    }});
    g.appendChild(cell);
  }}
  const und = ORDERS.filter(o => !o.date);
  const box = document.getElementById('calundated');
  box.innerHTML = '';
  if (und.length) {{
    const p = document.createElement('p'); p.className = 'sub';
    p.textContent = 'No delivery date yet: switch to List and use Edit date & name.';
    box.appendChild(p);
  }}
}}
function editOrder(btn) {{
  const card = btn.closest('.wcard');
  const f = document.createElement('div');
  f.className = 'wform';
  f.innerHTML = '<input type="date" class="fd"> '
    + '<input type="text" class="fn" maxlength="80" placeholder="Name (optional)"> '
    + '<button class="fs">Save</button> <button class="quiet fc">Cancel</button>';
  f.querySelector('.fd').value = card.dataset.date;
  f.querySelector('.fn').value = card.dataset.name;
  f.querySelector('.fc').onclick = () => {{ f.remove(); btn.style.display = ''; }};
  f.querySelector('.fs').onclick = async () => {{
    const r = await fetch('/weeks/' + card.dataset.id, {{
      method: 'PATCH', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{delivery_date: f.querySelector('.fd').value,
                            name: f.querySelector('.fn').value}})}});
    if (!r.ok) {{ alert('Could not save that.'); return; }}
    location.reload();
  }};
  btn.style.display = 'none';
  card.appendChild(f);
}}
(function() {{
  let v = 'list';
  try {{ v = localStorage.getItem('ordersView') || 'list'; }} catch (e) {{}}
  const t = new Date();
  const next = ORDERS.filter(o => o.date >= iso(t.getFullYear(), t.getMonth(), t.getDate()))
    .map(o => o.date).sort()[0];
  if (next) {{ calY = +next.slice(0, 4); calM = +next.slice(5, 7) - 1; }}
  else {{ calY = t.getFullYear(); calM = t.getMonth(); }}
  setView(v === 'cal' ? 'cal' : 'list');
}})();
</script>
"""


def archived_recipes_html(rows):
    """Recipes taken out of rotation via POST /recipes/{id}/archive, but
    never deleted. Uses the plain table styling from APP_STYLE — the same
    pattern as the broken/orphan lists on Maintenance."""
    body_rows = "".join(
        f'<tr><td>{r["title"]}'
        + (' ' + _baby_tag(r))
        + f'</td><td class="sub">{_ago(r["last_cooked"])} &middot; cooked '
        f'{r["times_cooked"] or 0}&times;</td>'
        f'<td><button onclick="restoreRecipe({r["id"]})">Restore</button> '
        f'<button class="quiet" onclick="purgeRecipe({r["id"]})">'
        f'Delete permanently</button></td></tr>'
        for r in rows)
    table = (f'<table>{body_rows}</table>' if rows
            else '<p class="sub">No archived recipes.</p>')
    return f"""
<h1>Archived recipes</h1>
<p class="sub">Taken out of the picker and week suggestions, kept here in
   case you want them back. <a href="/admin">&larr; Back to Maintenance</a></p>
{table}
"""


WEEKS_STYLE = """
 .wlist{display:flex;flex-direction:column;gap:.8rem;margin-top:1rem}
 .wcard{display:block;background:var(--paper);border:1px solid var(--rule);
       border-radius:10px;padding:.9rem 1rem;text-decoration:none;
       color:inherit}
 .wcard:hover{border-color:var(--ink-faint)}
 .wthumbs{display:flex;gap:6px;margin-bottom:.6rem}
 .wthumb{width:38px;height:38px;border-radius:8px;background:var(--wash);
         overflow:hidden;flex:none}
 .wthumb img{width:100%;height:100%;object-fit:cover;display:block}
 .wrow{display:flex;justify-content:space-between;align-items:baseline;
      gap:.6rem}
 .wdate{font-weight:600;font-size:.98rem}
 .wstatus{font-size:.72rem;padding:2px 8px;border-radius:5px;
          white-space:nowrap}
 .wstatus.ok{background:#eef7f1;color:var(--accent)}
 .wstatus.warn{background:#fff8e6;color:var(--tip)}
 .wstatus.bad{background:#fdeceb;color:var(--caution)}
 .wcard{position:relative}
 .wlink{display:block;text-decoration:none;color:inherit}
 .wname{font-size:.9rem;color:var(--ink);margin-top:.15rem}
 .wcreated{font-size:.72rem;color:var(--ink-faint);margin-top:.2rem}
 .wdate.undated{color:var(--ink-faint);font-weight:400}
 .wedit{font-size:.75rem;padding:.2rem .6rem;margin-top:.6rem}
 .wform{margin-top:.6rem;display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
 .wform input{font:inherit;font-size:.85rem;padding:.25rem .4rem;
              border:1px solid var(--rule)}
 .whead{display:flex;justify-content:space-between;align-items:center;gap:1rem}
 .vtoggle{display:flex}
 .vtoggle button{font:inherit;font-size:.8rem;padding:.3rem .8rem;
                 background:var(--paper);border:1px solid var(--rule);
                 cursor:pointer;color:var(--ink-soft)}
 .vtoggle button.on{background:var(--ink);color:var(--paper);
                    border-color:var(--ink)}
 .calnav{display:flex;align-items:center;gap:.7rem;margin:1rem 0 .6rem}
 .calnav strong{min-width:9rem;text-align:center}
 .calgrid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:2px}
 .calh{font-size:.7rem;color:var(--ink-faint);text-align:center;padding:.2rem 0}
 .calc{min-height:4.6rem;background:var(--paper);border:1px solid var(--rule);
       padding:.2rem .25rem;overflow:hidden}
 .calc.today{border-color:var(--ink)}
 .calc .cn{display:block;font-size:.72rem;color:var(--ink-faint)}
 .calc .chip{display:block;font-size:.7rem;padding:1px 4px;margin-top:2px;
             border-radius:4px;text-decoration:none;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis}
 .chip.ok{background:#eef7f1;color:var(--accent)}
 .chip.warn{background:#fff8e6;color:var(--tip)}
 .chip.bad{background:#fdeceb;color:var(--caution)}
 #ovbox .primary{background:var(--ink);color:var(--paper);
                 border:1px solid var(--ink);padding:.55rem 1.1rem;
                 font-weight:600;cursor:pointer}
 @media (max-width:600px){.calc{min-height:3.4rem}.calc .chip{font-size:.62rem}}
"""


# ---------------------------------------------------------------
# App shell
# ---------------------------------------------------------------

APP_STYLE = TOKENS + GRID_STYLE + WEEKS_STYLE + """
 *{box-sizing:border-box}
 body{font:15px/1.55 var(--sans);max-width:940px;margin:0 auto;
      padding:0 1.2rem 4rem;color:var(--ink)}
 nav{display:flex;gap:1.4rem;align-items:baseline;
     border-bottom:1px solid var(--rule);margin-bottom:2rem;padding:1.4rem 0}
 nav .brand{font-family:var(--serif);font-size:1.15rem;margin-right:auto}
 nav a{color:var(--ink-soft);text-decoration:none;font-size:.9rem}
 nav a.on{color:var(--ink);font-weight:600}
 h1{font-family:var(--serif);font-weight:400;font-size:2rem;margin:0 0 .2rem}
 h2{font-size:.8rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
    color:var(--ink-faint);margin:2.6rem 0 .6rem}
 h2:first-of-type{margin-top:1.4rem}
 p{margin:.4rem 0}
 .sub{color:var(--ink-faint);font-size:.85rem}
 button{font:inherit;padding:.4rem .9rem;cursor:pointer;background:var(--paper);
        border:1px solid var(--ink);color:var(--ink)}
 button:hover{background:var(--ink);color:var(--paper)}
 button.quiet{border-color:var(--rule);color:var(--ink-soft)}
 button.quiet:hover{background:var(--wash);color:var(--ink)}
 select,input,textarea{font:inherit;padding:.35rem;border:1px solid var(--rule);
        background:var(--paper);color:var(--ink)}
 table{border-collapse:collapse;width:100%;margin:.4rem 0}
 td{padding:.45rem .5rem;border-bottom:1px solid var(--rule);
    vertical-align:middle}
 code{background:var(--wash);padding:2px 5px;font-size:.9em}
 pre{background:var(--wash);padding:1rem;overflow:auto;white-space:pre-wrap;
     font-size:.82rem;margin-top:1.5rem}
 .banner{padding:.7rem 1rem;border-left:3px solid;margin:0 0 1.2rem}
 .banner.ok{background:#eef7f1;border-color:var(--accent)}
 .banner.bad{background:#fdeceb;border-color:var(--caution)}
 .counts{color:var(--ink-soft);font-size:.9rem;margin-bottom:1.6rem}
 .bar{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.8rem 0}
 ul{padding-left:1.1rem}
 li{margin:.15rem 0}
"""


def shell(title, current, body, script="", show_logout=False):
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == current else ""}">{label}</a>'
        for key, href, label in (
            ("home", "/", "All recipes"),
            ("weeks", "/weeks", "Orders"),
            ("ingredients", "/ingredients/browse", "Ingredients"),
            ("admin", "/admin", "Maintenance"),
        ))
    logout = ('<form method="post" action="/logout" style="margin:0">'
              '<button type="submit" class="quiet" style="border:0;'
              'padding:0;font-size:.85rem;text-decoration:underline">'
              'Log out</button></form>' if show_logout else "")
    return f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{APP_STYLE}</style>
<nav><span class="brand">Meal Planner</span>{nav}{logout}</nav>
{body}
<pre id="out"></pre>
<script>{script}</script>"""


# ---------------------------------------------------------------
# Week cards: overview, one-per-page detail, print sheet
# ---------------------------------------------------------------

OVERVIEW_STYLE = TOKENS + """
 *{box-sizing:border-box}
 body{font:16px/1.55 var(--sans);max-width:760px;margin:0 auto;
      padding:2rem 1.2rem 4rem;color:var(--ink)}
 h1{font-family:var(--serif);font-weight:400;font-size:2rem;margin:0 0 .2rem}
 .sub{color:var(--ink-faint);font-size:.88rem}
 .rows{margin:1.6rem 0}
 .row{display:flex;gap:1rem;align-items:center;padding:.9rem 0;
      border-bottom:1px solid var(--rule);text-decoration:none;color:inherit}
 .row:first-child{border-top:1px solid var(--rule)}
 .row img,.row .ph{width:76px;height:76px;object-fit:cover;flex:none;
                   background:var(--wash)}
 .row .t{font-weight:600;font-size:1.02rem;line-height:1.25}
 .row .m{color:var(--ink-faint);font-size:.85rem;margin-top:.15rem}
 .row .n{font-family:var(--serif);font-size:1.3rem;color:var(--ink-faint);
         width:1.2rem;flex:none}
 .row:hover .t{text-decoration:underline}
 .actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1.4rem}
 .actions a{font:inherit;padding:.5rem 1rem;border:1px solid var(--ink);
            text-decoration:none;color:var(--ink)}
 .actions a:hover{background:var(--ink);color:var(--paper)}
 .actions a.quiet{border-color:var(--rule);color:var(--ink-soft)}
"""

DETAIL_EXTRA = """
 body{background:var(--paper)}
 .topbar{position:sticky;top:0;background:var(--paper);z-index:10;
         border-bottom:1px solid var(--rule);padding:.7rem 0;margin-bottom:0;
         display:flex;gap:1rem;align-items:center;font-size:.85rem}
 .topbar a{color:var(--ink-soft);text-decoration:none}
 .topbar a:hover{color:var(--ink)}
 .topbar .mid{margin-left:auto;margin-right:auto;color:var(--ink-faint)}
 .card{max-width:900px;margin:0 auto;padding:1.6rem 1.2rem 3rem}
 .pager{display:flex;justify-content:space-between;gap:1rem;
        max-width:900px;margin:0 auto 3rem;padding:0 1.2rem}
 .pager a{padding:.6rem 1rem;border:1px solid var(--rule);
          text-decoration:none;color:var(--ink);font-size:.9rem}
 .pager a:hover{border-color:var(--ink)}
 /* One column on a phone: you are holding it, not reading a spread. */
 @media (max-width:720px){
   .head{grid-template-columns:1fr}
   .steps{grid-template-columns:1fr;gap:1.6rem}
   .step img{aspect-ratio:16/9}
   h1{font-size:1.6rem}
   .step p{font-size:1rem}
 }
"""


def short_date(iso):
    """'8 Oct' — no year, no time. The detail used everywhere a week needs
    a single human date rather than a full timestamp."""
    if not iso:
        return ""
    from datetime import datetime
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    return f"{d.day} {d.strftime('%b')}"


def week_overview_html(week, recipes, display_status="ready"):
    date_label = (long_date(week.get("delivery_date"))
                  or "Recipes from " + short_date(
                      week.get("ordered_at") or week.get("created_at")))
    if week.get("name"):
        import html as _html
        date_label += " &middot; " + _html.escape(week["name"])
    ordered = week.get("status") == "ordered"
    status_label, status_kind = STATUS_LABEL[display_status]

    rows = ""
    for n, r in enumerate(recipes, start=1):
        thumb = (f'<img src="{r["hero_image"]}" alt="" loading="lazy">'
                 if r.get("hero_image") else '<div class="ph"></div>')
        meta = " · ".join(filter(None, [
            f'{r["servings"]} servings',
            f'{r["time_max"]} min' if r.get("time_max") else "",
            r.get("protein") or "",
        ]))
        remove = (f'<button class="rm" data-recipe="{r["recipe_id"]}" '
                 f'onclick="removeRecipe(event,{r["recipe_id"]})" '
                 f'aria-label="Remove">&times;</button>')
        rows += (f'<a class="row" href="/weeks/{week["id"]}/cards/'
                 f'{r["recipe_id"]}"><span class="n">{n}</span>{thumb}'
                 f'<span><span class="t">{r["title"]}</span>'
                 f'<div class="m">{meta}</div></span>{remove}</a>')

    menu_items = []
    if ordered:
        menu_items.append('<button onclick="markNotOrdered()">'
                          'Mark as not ordered</button>')
    else:
        menu_items.append('<button onclick="toggleEdit()">Edit recipes</button>')
        menu_items.append('<button onclick="markOrdered()">'
                          'Mark as ordered</button>')
    menu_items.append('<button class="danger" onclick="deleteWeek()">'
                      'Delete week</button>')

    edit_hint = (
        '<p class="sub editing-only" style="display:none">Tap &times; to '
        'remove a recipe from this week.</p>')
    if ordered:
        edit_hint = (
            '<p class="sub">This week is ordered, so its recipes are '
            'locked in. Use the menu to mark it as not ordered first if '
            'you need to change what\'s in it.</p>')

    return f"""<!doctype html><meta charset="utf-8">
<title>Recipes from {date_label}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{OVERVIEW_STYLE}
 .topbar{{display:flex;align-items:center;gap:.8rem;margin-bottom:1.4rem;
         position:relative}}
 .topbar a{{color:var(--ink-faint);text-decoration:none;font-size:.9rem;
           white-space:nowrap}}
 .topbar a:hover{{color:var(--ink)}}
 .topbar h1{{flex:1;margin:0;text-align:center;font-size:1.3rem;
            display:flex;flex-direction:column;gap:.2rem;align-items:center}}
 .topbar .badges{{display:flex;gap:.4rem;align-items:center;font-weight:400}}
 .topbar .wstatus{{font-size:.68rem;padding:1px 7px;border-radius:5px}}
 .topbar .retailer{{font-size:.72rem;color:var(--ink-faint);
                    text-transform:uppercase;letter-spacing:.04em}}
 .topbar .wstatus.ok{{background:#eef7f1;color:var(--accent)}}
 .topbar .wstatus.warn{{background:#fff8e6;color:var(--tip)}}
 .topbar .wstatus.bad{{background:#fdeceb;color:var(--caution)}}
 .kebab{{background:none;border:0;font-size:1.3rem;line-height:1;
        color:var(--ink-soft);cursor:pointer;padding:.2rem .4rem}}
 .menu{{position:absolute;top:2.2rem;right:0;background:var(--paper);
       border:1px solid var(--rule);min-width:11rem;z-index:5;
       box-shadow:0 2px 10px rgba(0,0,0,.08);display:none}}
 .menu.on{{display:block}}
 .menu button{{display:block;width:100%;text-align:left;background:none;
              border:0;padding:.65rem .9rem;font:inherit;font-size:.88rem;
              cursor:pointer;color:var(--ink)}}
 .menu button:hover{{background:var(--wash)}}
 .menu button.danger{{color:var(--caution)}}
 .row{{position:relative}}
 .row .rm{{display:none;position:absolute;right:.7rem;top:50%;
          transform:translateY(-50%);width:28px;height:28px;border-radius:50%;
          border:1px solid var(--rule);background:var(--paper);
          color:var(--caution);font-size:1rem;line-height:1;cursor:pointer}}
 body.editing .row .rm{{display:block}}
 body.editing .row{{padding-right:2.6rem}}
</style>
<div class="topbar">
  <a href="/weeks">&larr; Orders</a>
  <h1>{date_label}
    <span class="badges">
      <span class="retailer">{retailers.name(week['retailer'])}</span>
      <span class="wstatus {status_kind}">{status_label}</span>
    </span>
  </h1>
  <button class="kebab" onclick="toggleMenu(event)" aria-label="More">&#8942;</button>
  <div class="menu" id="menu">{"".join(menu_items)}</div>
</div>
<p class="sub">In suggested cooking order &mdash; quickest to spoil first.
   Tap one to cook from it.</p>
{edit_hint}
<div class="rows">{rows}</div>
<div class="actions">
  <a href="/weeks/{week['id']}/print">Print all</a>
  <a class="quiet" href="/weeks/{week['id']}/list">Shopping list</a>
</div>
<p class="sub" style="margin-top:1.4rem">Print opens your browser&rsquo;s print
   dialog &mdash; choose &ldquo;Save as PDF&rdquo; as the destination if you
   want a file.</p>
<script>
function toggleMenu(e) {{
  e.stopPropagation();
  document.getElementById('menu').classList.toggle('on');
}}
document.addEventListener('click', () =>
  document.getElementById('menu').classList.remove('on'));

function toggleEdit() {{
  document.body.classList.toggle('editing');
  document.querySelectorAll('.editing-only').forEach(
    el => el.style.display = document.body.classList.contains('editing')
      ? '' : 'none');
}}

async function removeRecipe(e, recipeId) {{
  e.preventDefault();
  e.stopPropagation();
  const r = await fetch('/weeks/{week["id"]}/recipes/' + recipeId,
                        {{method: 'DELETE'}});
  if (!r.ok) {{
    const j = await r.json().catch(() => ({{}}));
    alert(j.detail || 'Could not remove that recipe.');
    return;
  }}
  location.reload();
}}

async function markNotOrdered() {{
  await fetch('/weeks/{week["id"]}/unorder', {{method: 'POST'}});
  location.reload();
}}

async function markOrdered() {{
  const r = await fetch('/weeks/{week["id"]}/order', {{method: 'POST'}});
  if (!r.ok) {{
    const j = await r.json().catch(() => ({{}}));
    alert(j.detail || 'Could not mark this week as ordered.');
    return;
  }}
  location.reload();
}}

async function deleteWeek() {{
  if (!confirm('Delete this week? This can\\'t be undone.')) return;
  await fetch('/weeks/{week["id"]}', {{method: 'DELETE'}});
  location.href = '/weeks';
}}
</script>
"""


def recipe_detail_html(week_id, card, position, total, prev_id, next_id):
    prev_link = (f'<a href="/weeks/{week_id}/cards/{prev_id}">&larr; Previous'
                 f'</a>' if prev_id else '<span></span>')
    next_link = (f'<a href="/weeks/{week_id}/cards/{next_id}">Next &rarr;</a>'
                 if next_id else '<span></span>')

    return f"""<!doctype html><meta charset="utf-8"><title>Recipe</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{CARD_STYLE}{DETAIL_EXTRA}</style>
<div class="card" style="padding-top:0">
  <div class="topbar">
    <a href="/weeks/{week_id}/cards">&larr; Overview</a>
    <span class="mid">{position} of {total}</span>
    <a href="/weeks/{week_id}/list">Shopping list</a>
  </div>
</div>
{card}
<div class="pager">{prev_link}{next_link}</div>
"""


PRINT_STYLE = TOKENS + """
 @page { size: A4 landscape; margin: 7mm; }

 *{box-sizing:border-box;-webkit-print-color-adjust:exact;
   print-color-adjust:exact}
 body{margin:0;background:#fff;color:#000;font-family:var(--sans);
      font-size:8pt;line-height:1.35}

 /* One recipe per sheet. Fixed height so content can't spill onto a
    second page unnoticed — if a recipe is too long it compresses rather
    than silently paginating. */
 .card{width:100%;height:194mm;padding:0;margin:0 0 6mm;
       display:flex;flex-direction:column;overflow:hidden;
       page-break-after:always;break-after:page}
 .card:last-child{page-break-after:auto;break-after:auto}

 .head{display:flex;gap:5mm;align-items:flex-start;flex:none;
       border-bottom:.4pt solid #bbb;padding-bottom:2.5mm;margin-bottom:3mm}
 .head > div:first-child{flex:1;min-width:0}
 .head > div:last-child{width:52mm;flex:none}
 .hero{width:52mm;height:34mm;object-fit:cover;display:block;
       aspect-ratio:auto}

 h1{font-family:var(--serif);font-weight:400;font-size:17pt;line-height:1.05;
    margin:0 0 .6mm;letter-spacing:-.01em}
 .sub{font-family:var(--serif);font-style:italic;font-size:9.5pt;
      color:#444;margin:0 0 1.2mm}
 .facts{font-size:7.5pt;color:#555;margin:0}
 .babynote{font-size:7.5pt;margin:2pt 0 0}
 .facts b{color:#000}

 h2{font-size:6.5pt;font-weight:700;letter-spacing:.08em;
    text-transform:uppercase;color:#888;margin:0 0 1mm;font-family:var(--sans)}

 /* Body: ingredients column beside the method grid. */
 .body-cols{display:flex;gap:5mm;flex:1;min-height:0}
 .col-ing{width:56mm;flex:none}
 .col-method{flex:1;min-width:0;display:flex;flex-direction:column}

 .ing{width:100%;border-collapse:collapse}
 .ing td{padding:.7mm 0;border-bottom:.3pt solid #ddd;font-size:7.5pt;
         vertical-align:baseline;line-height:1.25}
 .ing td:last-child{text-align:right;white-space:nowrap;color:#444;
                    font-variant-numeric:tabular-nums}
 .ing .prep{color:#888;font-size:6.5pt}

 .steps{display:grid;grid-template-columns:repeat(3,1fr);
        gap:3mm 4mm;flex:1;min-height:0}
 .step{display:flex;flex-direction:column;break-inside:avoid;min-height:0}
 .step img{width:100%;height:23mm;object-fit:cover;margin-bottom:1.2mm;
           aspect-ratio:auto}
 .n{font-family:var(--serif);font-size:12pt;color:var(--accent);
    line-height:1;margin-bottom:.3mm}
 .step h3{font-size:8pt;margin:0 0 .8mm;font-weight:700}
 .step p{margin:0 0 1mm;font-size:7pt;line-height:1.3}
 .step b{font-weight:700}
 .callout{font-size:6.5pt;line-height:1.25;padding-left:1.4mm;
          border-left:.8pt solid;margin:0 0 1mm}
 .callout.tip{border-color:var(--tip);color:var(--tip)}
 .callout.important{border-color:var(--caution);color:var(--caution)}
 .timer{display:inline-block;font-size:6pt;color:var(--accent);
        border:.4pt solid var(--accent);padding:0 1mm;margin-top:.3mm}

 .foot{flex:none;margin-top:2.5mm;padding-top:1.5mm;
       border-top:.4pt solid #bbb;font-size:6pt;color:#777;
       display:flex;gap:6mm}
 .foot div{max-width:80mm}
 .foot b{color:#444}

 /* Screen preview: show the sheet at roughly its printed proportions so
    what you see is what comes out. */
 @media screen {
   body{background:#e9eaec;padding:1.5rem}
   .card{width:281mm;margin:0 auto 1.5rem;background:#fff;
         padding:7mm;height:210mm;box-shadow:0 1px 6px rgba(0,0,0,.18)}
   .hint{max-width:281mm;margin:0 auto 1rem;font:13px var(--sans);
         color:#555}
 }
 @media print { .hint{display:none} }
"""


def print_sheet_html(cards):
    """One recipe per A4 sheet, landscape, ingredients beside the method.

    Deliberately a separate stylesheet from the screen cards: the screen
    version is a scrolling column, which paginates into a mess. This is
    laid out to a fixed sheet height instead.
    """
    return f"""<!doctype html><meta charset="utf-8"><title>Recipe cards</title>
<style>{PRINT_STYLE}</style>
<p class="hint">One recipe per A4 sheet, landscape. Use your browser&rsquo;s
   print dialog &mdash; choose &ldquo;Save as PDF&rdquo; for a file. Make sure
   layout is set to Landscape and background graphics are on.</p>
{"".join(cards)}
<script>window.addEventListener('load', () => setTimeout(() => print(), 600));
</script>
"""


# ---------------------------------------------------------------
# Login
# ---------------------------------------------------------------

def login_html(next_path="/", error=False):
    err = ('<p class="err">Wrong passcode.</p>' if error else "")
    return f"""<!doctype html><meta charset="utf-8"><title>Meal Planner</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>{TOKENS}
 body{{font-family:var(--sans);display:flex;min-height:100vh;
      align-items:center;justify-content:center;margin:0;
      background:var(--wash)}}
 form{{background:var(--paper);padding:2.2rem 2rem;width:100%;max-width:22rem;
      border:1px solid var(--rule)}}
 h1{{font-family:var(--serif);font-weight:400;font-size:1.5rem;margin:0 0 1.2rem}}
 input{{width:100%;font-size:1.1rem;padding:.6rem;border:1px solid var(--rule);
       box-sizing:border-box;margin-bottom:.9rem;letter-spacing:.1em}}
 button{{width:100%;padding:.7rem;font:inherit;font-weight:600;
        background:var(--ink);color:var(--paper);border:0;cursor:pointer}}
 .err{{color:var(--caution);font-size:.85rem;margin:-.4rem 0 .9rem}}
</style>
<form method="post" action="/login">
  <h1>Meal Planner</h1>
  <input type="password" name="passcode" inputmode="numeric" autofocus
         placeholder="Passcode" autocomplete="current-password">
  <input type="hidden" name="next" value="{next_path}">
  <button type="submit">Enter</button>
  {err}
</form>
"""
