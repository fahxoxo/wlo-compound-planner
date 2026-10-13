"""
Load the scraped wlohub data (data/*.json) and apply the compounding
data-correction rules used by the calculator.

All logic uses an item's compounding "rank"; the equip "level" is ignored.
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _load(name, data_dir):
    return json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))


def normalize_compound(formula, items):
    """
    Rule: a formula listed with a single material whose result has rank > 1
    really needs 2 of that same material (A -> A + A).
    Corrected formulas get "inferred": "duplicated_single_material".
    """
    f = dict(formula, materials=list(formula["materials"]), inferred=None)
    result = items.get(f["result_id"])
    if len(f["materials"]) == 1 and result and (result["rank"] or 0) > 1:
        f["materials"] = f["materials"] * 2
        f["inferred"] = "duplicated_single_material"
    return f


MAX_SLOTS = 5
MAX_SAFE_RANK_SPREAD = 2
# highest result rank = Slot 1 (primary) rank + this, by Alchemy Book (None = no book)
BOOK_BONUS = {None: 4, 1: 5, 2: 6, 3: 7, 4: 8}


def assess_compound(f, items):
    """
    Rules:
    - 2 to MAX_SLOTS materials per compound.
    - Recommended steps must keep the rank difference between materials
      (highest rank - lowest rank) <= MAX_SAFE_RANK_SPREAD. Wider spreads are
      "risky": shown only in a separate warning section (high failure risk).

    - The result can be at most Slot 1 rank + BOOK_BONUS[book]; formulas above
      that cap are "invalid" (75 of wlohub's 76 verified formulas obey it; the
      ones that don't are almost certainly wrong community data). The player's
      own shortcut recipes are trusted as given.

    Sets f["category"] to "invalid", "risky" or "safe" ("over_cap": True if capped out).
    """
    ranks = [items[m["id"]]["rank"] for m in f["materials"]
             if m["id"] in items and items[m["id"]]["rank"] is not None]
    f["rank_spread"] = max(ranks) - min(ranks) if ranks else None
    res_rank = (items.get(f["result_id"]) or {}).get("rank")
    first = items.get(f["materials"][0]["id"]) if f["materials"] else None
    f["over_cap"] = bool(res_rank is not None and first and first["rank"] is not None
                         and res_rank > first["rank"] + BOOK_BONUS.get(f.get("book"), 4)
                         and not f.get("user"))
    if not 2 <= len(f["materials"]) <= MAX_SLOTS or len(ranks) != len(f["materials"]) or f["over_cap"]:
        f["category"] = "invalid"
    elif f["rank_spread"] > MAX_SAFE_RANK_SPREAD:
        f["category"] = "risky"
    else:
        f["category"] = "safe"
    return f


KNOWLEDGE = Path(__file__).parent / "knowledge" / "shortcuts.json"


def _resolve(items, ref):
    """Item id from a name (or {"id": ...}); errors clearly on unknown / ambiguous names."""
    if isinstance(ref, dict):
        ref = ref.get("id", ref.get("item"))
    if isinstance(ref, int):
        if ref not in items:
            raise ValueError(f"shortcuts.json: unknown item id {ref}")
        return ref
    hits = [r["id"] for r in items.values() if r["name"].lower() == str(ref).lower()]
    if len(hits) != 1:
        raise ValueError(f"shortcuts.json: item {ref!r} " +
                         ("not found" if not hits else f"is ambiguous, use one of the ids {hits}"))
    return hits[0]


def apply_knowledge(db, path=KNOWLEDGE):
    """
    Add the player's own shortcut routes (knowledge/shortcuts.json):
    - recipes: counted as confirmed ("user": True) and pinned, i.e. the only
      way the calculator makes that result item
    - item_fixes: corrected item data (e.g. a missing base)
    - shop_fixes: shop items allowed above the shop-rank limit ("always_buy"), usable at
      any stage instead of only Stage 1-2 ("any_stage"), location labels
    - substitutes: use one item for any material of the same base & rank
    - self_supplied: items the player gets themselves (own inventory, drops, ...);
      optional "source" text replaces "from your bag" in the guide
    """
    if not path.exists():
        return db
    k = json.loads(path.read_text(encoding="utf-8"))
    items = db["items"]
    for fx in k.get("item_fixes", []):
        iid = _resolve(items, fx["item"])
        items[iid] = dict(items[iid], **{key: v for key, v in fx.items() if key not in ("item", "note")})
    if k.get("item_fixes"):  # corrected ranks / bases change the rank checks of wlohub's formulas
        db["compounds"] = [assess_compound(f, items) for f in db["compounds"]]
    for fx in k.get("shop_fixes", []):
        iid = _resolve(items, fx["item"])
        for s in db["shops"]:
            if s["item_id"] == iid:
                s["always_buy"] = fx.get("always_buy", False)
                s["any_stage"] = fx.get("any_stage", False)
                if "rank" in fx:  # corrected shop rank (e.g. wlohub lists the wrong one)
                    s["rank"] = fx["rank"]
                if fx.get("location_label"):
                    s["location"] = fx["location_label"]
    # substitutes: a material of the same primary base & rank may be replaced by the
    # player's preferred item (e.g. Ninja Suit for Nylon R23); adds a copy of each recipe
    for sub in k.get("substitutes", []):
        sid = _resolve(items, sub["item"])
        extra = []
        for f in db["compounds"]:
            if (items[f["result_id"]]["rank"] or 0) < sub.get("min_result_rank", 0):
                continue
            mats = f["materials"]
            hit = [i for i, m in enumerate(mats) if m["id"] != sid and items[m["id"]]["bases"]
                   and items[m["id"]]["bases"][0] == sub["base"] and items[m["id"]]["rank"] == sub["rank"]]
            if not hit:
                continue
            new = [dict(id=sid, name=items[sid]["name"]) if i in hit else m for i, m in enumerate(mats)]
            extra.append(assess_compound(dict(f, id=f"{f['id']}~{sid}", materials=new,
                                              substituted={"item": items[sid]["name"], "note": sub.get("note", ""),
                                                           "original": f["id"]}), items))
        db["compounds"].extend(extra)
    # "effort": how many compound actions one piece is worth to you (default 1), so the
    # planner does not use dozens of items you had to make yourself
    db["self_supplied"] = {_resolve(items, x["item"]): {"note": x.get("note", ""), "source": x.get("source", ""),
                                                        "effort": x.get("effort", 1)}
                           for x in k.get("self_supplied", [])}
    for n, r in enumerate(k.get("recipes", []), 1):
        rid = _resolve(items, r["result"])
        mats = [{"id": i, "name": items[i]["name"]} for i in (_resolve(items, m) for m in r["materials"])]
        f = {"id": f"S{n}", "alchemy_level": "", "rank_offset": None, "result_id": rid,
             "result_name": items[rid]["name"], "materials": mats, "book": r.get("book"),
             "ingredients": mats, "verified": True, "verified_by": "you (shortcut route)",
             "submitted_by": "", "inferred": None, "user": True, "note": r.get("note", "")}
        db["compounds"].append(assess_compound(f, items))
    return db


def load(data_dir=DATA_DIR, knowledge=KNOWLEDGE):
    items = {r["id"]: r for r in _load("items", data_dir)}
    compounds = [assess_compound(normalize_compound(c, items), items)
                 for c in _load("compounds", data_dir)]
    db = {
        "items": items,
        "shops": _load("shops", data_dir),
        "compounds": compounds,
        "collections": _load("collections", data_dir),
        "self_supplied": {},
    }
    return apply_knowledge(db, knowledge)


if __name__ == "__main__":
    db = load()
    fixed = [c for c in db["compounds"] if c["inferred"]]
    print(f"{len(db['items'])} items, {len(db['compounds'])} formulas, "
          f"{len(fixed)} single-material formulas expanded to 2x")
    for cat in ("safe", "risky", "invalid"):
        group = [c for c in db["compounds"] if c["category"] == cat]
        print(f"  {cat:8} {len(group):6}  (verified {sum(c['verified'] for c in group)})")
