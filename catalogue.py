"""The Ingredients page: every ingredient, and what each shop sells for it.

One row per ingredient, one column per shop. Built from the same tables the
shopping list uses, so what you see here is what the list will buy.

`overview()` returns plain data (also served as JSON at /ingredients/overview
so a Claude session can read it). `page_html()` turns that into the page.
"""

import html
import json

import db
import quantities
import retailers

STALE_DAYS = 60


def _size_text(size, unit):
    """250 g, 1.5 kg, 750 ml, 1 l, x4, each."""
    if not size:
        return ""
    u = (unit or "").lower()
    if u in ("g", "ml"):
        if size >= 1000:
            big = size / 1000
            return f"{big:g} {'kg' if u == 'g' else 'l'}"
        return f"{size:g} {u}"
    n = f"{size:g}"
    return "each" if n == "1" else f"x{n}"


def _unit_price_text(product):
    value, label = quantities.unit_price(product)
    if value is None:
        return ""
    unit = (product.get("pack_unit") or "").lower()
    if label == "each":
        # A single item's unit price is just its price.
        return "" if (product.get("pack_size") or 0) <= 1 else f"£{value:.2f} each"
    return f"£{value:.2f}/{label.replace('per ', '')}"


def _pick(products, ingredient):
    """The product shown for this ingredient at one shop.

    The preferred one if there is one, otherwise the cheapest per
    100 g / 100 ml / item, otherwise just the first.
    """
    if not products:
        return None
    for p in products:
        if p["preferred"]:
            return p

    def per_unit(p):
        size = quantities.pack_in_canonical(p, ingredient)
        if p.get("price") is None or not size:
            return float("inf")
        return p["price"] / size

    return min(products, key=per_unit)


def overview():
    shops = list(retailers.RETAILERS)

    ingredients = db.query(
        "SELECT id, name, category, canonical_unit, unit_conversions, "
        "assume_stocked, substitution_note FROM ingredients ORDER BY name")
    products = db.query("SELECT * FROM products WHERE delisted = 0")
    uses = db.query(
        "SELECT ri.ingredient_id AS iid, r.id AS rid, r.title AS title "
        "FROM recipe_ingredients ri JOIN recipes r ON r.id = ri.recipe_id "
        "WHERE r.status = 'active' "
        "GROUP BY ri.ingredient_id, r.id ORDER BY r.title")

    by_pair = {}
    for p in products:
        by_pair.setdefault((p["ingredient_id"], p["retailer"]), []).append(p)

    used_in = {}
    for u in uses:
        used_in.setdefault(u["iid"], []).append([u["rid"], u["title"]])

    rows = []
    for ing in ingredients:
        staple = bool(ing["assume_stocked"]) or ing["category"] == "never_buy"
        cells = {}
        missing = []
        for shop in shops:
            plist = by_pair.get((ing["id"], shop), [])
            p = _pick(plist, ing)
            if not p:
                cells[shop] = None
                if not staple:
                    missing.append(shop)
                continue
            cells[shop] = {
                "name": p["name"],
                "url": p.get("url"),
                "price": p.get("price"),
                "size": _size_text(p.get("pack_size"), p.get("pack_unit")),
                "unit_price": _unit_price_text(p),
                "more": len(plist) - 1,
                "swap": bool(p.get("substitute")),
                "note": p.get("note"),
                "checked": p.get("price_updated"),
            }
        rows.append({
            "id": ing["id"], "name": ing["name"], "category": ing["category"],
            "unit": ing["canonical_unit"], "staple": staple,
            "recipes": len(used_in.get(ing["id"], [])),
            "cells": cells, "missing": missing,
        })

    rows.sort(key=lambda r: (-r["recipes"], r["name"]))

    buyable = [r for r in rows if not r["staple"]]
    coverage = {s: sum(1 for r in buyable if r["cells"][s]) for s in shops}
    return {
        "shops": [{"key": s, "name": retailers.name(s)} for s in shops],
        "total": len(rows),
        "buyable": len(buyable),
        "staples": len(rows) - len(buyable),
        "needs_attention": sum(1 for r in buyable if r["missing"]),
        "coverage": coverage,
        "rows": rows,
        "used_in": used_in,
    }


STYLE = """
<style>
 body{max-width:1380px}
 .ibar{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin:1rem 0 .4rem}
 .ibar input[type=search]{width:240px}
 .chips{display:flex;gap:.4rem;flex-wrap:wrap}
 .chip{border:1px solid var(--rule);background:var(--paper);color:var(--ink-soft);
       padding:.25rem .7rem;border-radius:99px;font-size:.85rem;cursor:pointer}
 .chip:hover{background:var(--wash);color:var(--ink)}
 .chip.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
 .chip b{font-weight:600;margin-left:.25rem;opacity:.7}
 .tog{font-size:.85rem;color:var(--ink-soft);display:flex;gap:.35rem;align-items:center}
 .tog input{padding:0}
 .tw{overflow-x:auto}
 .itab{width:100%;min-width:1040px;border-collapse:separate;border-spacing:0;
       font-size:13px;table-layout:fixed;margin-top:.6rem}
 .itab th{position:sticky;top:0;background:var(--paper);z-index:3;text-align:left;
          font-weight:600;font-size:12px;color:var(--ink-soft);padding:.55rem .6rem;
          border-bottom:2px solid var(--rule);vertical-align:bottom}
 .itab th .cov{display:block;font-weight:400;color:var(--ink-faint);margin-top:2px}
 .itab td{padding:.4rem .6rem;border-bottom:1px solid var(--rule);vertical-align:top;
          overflow:hidden}
 .itab col.c-ing{width:210px} .itab col.c-rec{width:64px}
 .itab tr:hover td{background:#fafbfc}
 .nm{font-weight:600;font-size:13.5px}
 .isub{color:var(--ink-faint);font-size:11.5px}
 .rc{display:inline-block;min-width:2rem;text-align:center;border-bottom:1px dotted var(--ink-faint);
     cursor:default;color:var(--ink-soft)}
 .rc:hover{color:var(--ink)}
 .pr{font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;
     text-overflow:ellipsis}
 .pr b{font-weight:600}
 .sz{color:var(--ink-soft);margin-left:.35rem}
 .up{color:var(--ink-faint);margin-left:.35rem;font-size:11.5px}
 .pn{display:block;color:var(--ink-faint);font-size:11.5px;text-decoration:none;
     white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 a.pn:hover{color:var(--ink);text-decoration:underline}
 .tag{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.04em;
      text-transform:uppercase;padding:0 .35rem;border-radius:3px;margin-left:.35rem;
      vertical-align:1px}
 .tag.swap{background:#fbf0d4;color:var(--tip)}
 .tag.more{background:var(--wash);color:var(--ink-soft);text-transform:none;
           letter-spacing:0;font-weight:500}
 td.none{color:var(--ink-faint)}
 td.none.attn{background:#fdf1f0}
 td.none.attn span{color:var(--caution);font-weight:600;font-size:12px}
 tr.stp td{opacity:.6}
 .pop{position:fixed;z-index:20;background:var(--paper);border:1px solid var(--rule);
      box-shadow:0 8px 28px rgba(0,0,0,.14);border-radius:8px;padding:.7rem .9rem;
      max-width:340px;max-height:320px;overflow:auto;font-size:13px;display:none}
 .pop h5{margin:0 0 .4rem;font-size:12px;color:var(--ink-faint);font-weight:600}
 .pop a{display:block;color:var(--ink);text-decoration:none;padding:.15rem 0}
 .pop a:hover{text-decoration:underline}
 .empty{padding:2rem;text-align:center;color:var(--ink-faint)}
</style>
"""


def _fmt_price(p):
    return f"£{p:.2f}" if p is not None else "no price"


def _cell(c, row):
    if c is None:
        if row["staple"]:
            return '<td class="none">&ndash;</td>'
        return '<td class="none attn"><span>No product</span></td>'
    title = html.escape(c["name"])
    name = (f'<a class="pn" href="{html.escape(c["url"])}" target="_blank" '
            f'rel="noopener" title="{title}">{title}</a>' if c.get("url")
            else f'<span class="pn" title="{title}">{title}</span>')
    bits = [f'<b>{_fmt_price(c["price"])}</b>']
    if c["size"]:
        bits.append(f'<span class="sz">{html.escape(c["size"])}</span>')
    if c["unit_price"]:
        bits.append(f'<span class="up">{html.escape(c["unit_price"])}</span>')
    tags = ""
    if c["swap"]:
        tip = html.escape(c["note"] or "A stand-in, not the same product")
        tags += f'<span class="tag swap" title="{tip}">swap</span>'
    if c["more"] > 0:
        tags += f'<span class="tag more" title="Other products stored">+{c["more"]}</span>'
    when = html.escape(c["checked"] or "unknown")
    return (f'<td title="Price checked {when}"><div class="pr">{"".join(bits)}{tags}</div>'
            f'{name}</td>')


def page_html(data):
    shops = data["shops"]
    cols = ('<col class="c-ing"><col class="c-rec">'
            + "".join("<col>" for _ in shops))
    head = ('<th>Ingredient</th><th title="Active recipes that use it">Recipes</th>'
            + "".join(
                f'<th>{html.escape(s["name"])}<span class="cov">'
                f'{data["coverage"][s["key"]]} of {data["buyable"]} priced</span></th>'
                for s in shops))

    body_rows = []
    for r in data["rows"]:
        classes = []
        if r["staple"]:
            classes.append("stp")
        miss = " ".join(r["missing"])
        sub = html.escape(f'{r["category"].replace("_", " ")} · by {r["unit"]}')
        cells = "".join(_cell(r["cells"][s["key"]], r) for s in shops)
        body_rows.append(
            f'<tr class="{" ".join(classes)}" data-id="{r["id"]}" '
            f'data-name="{html.escape(r["name"].lower())}" '
            f'data-staple="{1 if r["staple"] else 0}" data-miss="{miss}" '
            f'data-n="{r["recipes"]}">'
            f'<td><div class="nm">{html.escape(r["name"])}</div>'
            f'<div class="isub">{sub}</div></td>'
            f'<td><span class="rc" data-id="{r["id"]}">{r["recipes"]}</span></td>'
            f'{cells}</tr>')

    shop_opts = "".join(
        f'<option value="{s["key"]}">Missing at {html.escape(s["name"])}</option>'
        for s in shops)

    used = json.dumps({str(k): v for k, v in data["used_in"].items()},
                      separators=(",", ":")).replace("</", "<\\/")

    body = f"""
<h1>Ingredients</h1>
<p class="counts">{data['buyable']} to buy &middot; {data['staples']} staples
 &middot; {data['needs_attention']} missing a product at one or more shops</p>
{STYLE}
<div class="ibar">
  <input type="search" id="q" placeholder="Search ingredients" autocomplete="off">
  <div class="chips">
    <button class="chip on" data-f="all">All</button>
    <button class="chip" data-f="attn">Needs attention <b>{data['needs_attention']}</b></button>
  </div>
  <select id="shopf" title="Show only ingredients with no product at one shop">
    <option value="">Any shop</option>{shop_opts}</select>
  <select id="sort"><option value="n">Most used first</option>
    <option value="a">A&ndash;Z</option></select>
  <label class="tog"><input type="checkbox" id="stp"> Show staples ({data['staples']})</label>
</div>
<div class="tw"><table class="itab" id="tab"><colgroup>{cols}</colgroup>
<thead><tr>{head}</tr></thead>
<tbody id="tb">{"".join(body_rows)}</tbody></table></div>
<div class="empty" id="none" hidden>Nothing matches.</div>
<div class="pop" id="pop"></div>
<script type="application/json" id="used">{used}</script>
"""
    script = """
(function(){
  var rows=[].slice.call(document.querySelectorAll('#tb tr'));
  var tb=document.getElementById('tb'),q=document.getElementById('q'),
      shopf=document.getElementById('shopf'),sortSel=document.getElementById('sort'),
      stp=document.getElementById('stp'),none=document.getElementById('none');
  var mode='all';
  function apply(){
    var t=q.value.trim().toLowerCase(),sh=shopf.value,shown=0;
    rows.forEach(function(r){
      var ok=true;
      if(r.dataset.staple==='1'&&!stp.checked)ok=false;
      if(ok&&t&&r.dataset.name.indexOf(t)<0)ok=false;
      var miss=r.dataset.miss;
      if(ok&&mode==='attn'&&!miss)ok=false;
      if(ok&&sh&&(' '+miss+' ').indexOf(' '+sh+' ')<0)ok=false;
      r.hidden=!ok; if(ok)shown++;
    });
    none.hidden=shown>0;
  }
  function sort(){
    var by=sortSel.value;
    rows.sort(function(a,b){
      if(by==='a')return a.dataset.name<b.dataset.name?-1:1;
      var d=(+b.dataset.n)-(+a.dataset.n);
      return d||(a.dataset.name<b.dataset.name?-1:1);
    });
    rows.forEach(function(r){tb.appendChild(r)});
  }
  [].forEach.call(document.querySelectorAll('.chip'),function(c){
    c.onclick=function(){
      mode=c.dataset.f;
      [].forEach.call(document.querySelectorAll('.chip'),function(x){
        x.classList.toggle('on',x===c)});
      apply();
    };
  });
  q.oninput=apply; shopf.onchange=apply; stp.onchange=apply;
  sortSel.onchange=function(){sort();apply()};

  // Recipe list on hover, or on tap for touch screens.
  var used=JSON.parse(document.getElementById('used').textContent);
  var pop=document.getElementById('pop'),hideT=null,pinned=null;
  function show(el){
    clearTimeout(hideT);
    var list=used[el.dataset.id]||[];
    var name=el.closest('tr').querySelector('.nm').textContent;
    var h='<h5>'+list.length+' recipe'+(list.length===1?'':'s')+' use '+esc(name)+'</h5>';
    h+=list.slice(0,40).map(function(r){
      return '<a href="/recipes/'+r[0]+'/card" target="_blank">'+esc(r[1])+'</a>'}).join('');
    if(list.length>40)h+='<div class="sub">and '+(list.length-40)+' more</div>';
    if(!list.length)h+='<div class="sub">Not used in any active recipe.</div>';
    pop.innerHTML=h; pop.style.display='block';
    var b=el.getBoundingClientRect(),w=pop.offsetWidth,hh=pop.offsetHeight;
    var left=Math.min(b.left,window.innerWidth-w-12);
    var top=b.bottom+6; if(top+hh>window.innerHeight-8)top=Math.max(8,b.top-hh-6);
    pop.style.left=Math.max(8,left)+'px'; pop.style.top=top+'px';
  }
  function hide(){hideT=setTimeout(function(){pop.style.display='none';pinned=null},180)}
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
  tb.addEventListener('mouseover',function(e){
    var el=e.target.closest('.rc'); if(el&&!pinned)show(el)});
  tb.addEventListener('mouseout',function(e){
    if(e.target.closest('.rc')&&!pinned)hide()});
  tb.addEventListener('click',function(e){
    var el=e.target.closest('.rc'); if(!el)return;
    pinned=pinned===el?null:el; if(pinned)show(el); else hide();
  });
  pop.onmouseenter=function(){clearTimeout(hideT)};
  pop.onmouseleave=function(){if(!pinned)hide()};
  document.addEventListener('click',function(e){
    if(!e.target.closest('.rc')&&!e.target.closest('#pop')){pinned=null;pop.style.display='none'}});
  sort(); apply();
})();
"""
    return body, script
