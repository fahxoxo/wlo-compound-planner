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


def assess_compound(f, items):
    """
    Rules:
    - 2 to MAX_SLOTS materials per compound.
    - Recommended steps must keep the rank difference between materials
      (highest rank - lowest rank) <= MAX_SAFE_RANK_SPREAD. Wider spreads are
      "risky": shown only in a separate warning section (high failure risk).

    Sets f["category"] to "invalid", "risky" or "safe".
    """
    ranks = [items[m["id"]]["rank"] for m in f["materials"]
             if m["id"] in items and items[m["id"]]["rank"] is not None]
    f["rank_spread"] = max(ranks) - min(ranks) if ranks else None
    if not 2 <= len(f["materials"]) <= MAX_SLOTS or len(ranks) != len(f["materials"]):
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
    - shop_fixes: shop items allowed above the shop-rank limit, location labels
    - self_supplied: items with no shop / gathering source the player gets anyway
    """
    if not path.exists():
        return db
    k = json.loads(path.read_text(encoding="utf-8"))
    items = db["items"]
    for fx in k.get("item_fixes", []):
        iid = _resolve(items, fx["item"])
        items[iid] = dict(items[iid], **{key: v for key, v in fx.items() if key not in ("item", "note")})
    for fx in k.get("shop_fixes", []):
        iid = _resolve(items, fx["item"])
        for s in db["shops"]:
            if s["item_id"] == iid:
                s["always_buy"] = fx.get("always_buy", False)
                if fx.get("location_label"):
                    s["location"] = fx["location_label"]
    db["self_supplied"] = {_resolve(items, x["item"]): x.get("note", "") for x in k.get("self_supplied", [])}
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
