#!/usr/bin/env python3
"""
Wonderland Online compounding calculator.

    python wlo_calc.py "Cloth Armband"
    python wlo_calc.py --id 1148
    python wlo_calc.py "Polar Ultima" --all          # also show Tier 3
    python wlo_calc.py "Lock Armor" --max-shop-rank 10 --buy-stages 1
    python wlo_calc.py "Cloth Armband" --stdout     # print instead of saving
    python wlo_calc.py "Polar Ultima" --have "Magic Crystal Powder" --have 1234

The guide is saved as Markdown in guides/ (open it with VS Code's preview).

Route priority
  Tier 1  every raw input is bought (NPC shop) or gathered (collections).
          Natural progression: buy only shop items up to Rank 14 and use
          them only in Stage 1-2; later stages compound the items crafted
          earlier (gathered items may be used at any stage).
          Steps may use unconfirmed formulas; each one is labelled.
          Your own shortcut routes (knowledge/shortcuts.json) count as
          confirmed and are always used for the items they make.
          Ranked by: fewest Hawaii-only shop items (not purchasable in
          game right now), fewest unconfirmed steps, fewest steps, fewest
          Alchemy Books, lowest gold.
  Tier 2  confirmed formulas for the target that are not the Tier 1 route.
  Tier 3  unconfirmed formulas for the target (last resort, --all).
  Warning confirmed formulas whose material rank spread > 2.

Only "safe" formulas (2-5 materials, rank spread <= 2) whose Slot 1 primary
base equals the result's primary base are used for recommended routes.
All logic uses compounding rank; equip level is ignored.
"""
import argparse
import heapq
import re
import sys
import threading
from collections import OrderedDict
from collections import Counter, defaultdict
from pathlib import Path

import wlo_data

# Cost of a route, compared left to right:
#   (hard-to-get weight, Hawaii-only items, unconfirmed steps, steps, books used, gold)
# "Hard to get" is only used for partial routes (items that can't be bought or
# gathered under your rules); any normal route has 0 there. Any route avoiding
# Hawaii beats one that needs it.

# Shops listed on wlohub whose items cannot actually be bought in game.
UNAVAILABLE_SHOPS = {"Hawaii"}


def add(*costs):
    return tuple(map(sum, zip(*costs)))


def structure_ok(f, items):
    res = items.get(f["result_id"])
    first = items.get(f["materials"][0]["id"])
    return bool(res and first and res["bases"] and first["bases"]
                and res["bases"][0] == first["bases"][0])


def recommendable(f, items):
    if f.get("user"):  # the player's own shortcut routes are always trusted
        return True
    return f["category"] == "safe" and structure_ok(f, items)


# ---------------------------------------------------------------- sources

def raw_sources(db, max_shop_rank=None, have=(), fallback=False, exclude=(), ex=((), ())):
    """
    item_id -> {"gold", "unavailable", "anytime", "provided", "shops", "spots"}
    gold: cheapest price (0 if gatherable or provided). unavailable: only sold
    in UNAVAILABLE_SHOPS. anytime: gathered or provided by the player, so it
    may be used at any stage. have: item ids the player supplies themselves.

    fallback=True (partial routes): every other item becomes a raw material
    you must get elsewhere – "over_limit" if a normal shop sells it above your
    rank limit, else "missing" (drop, quest, trade). Their "weight" (2^(rank/4),
    x2 for missing) is minimised first, so the route needs as few and as
    low-rank of them as possible. exclude: items never treated that way (the target).

    ex = (excluded locations, excluded item ids): maps the player can't visit
    (e.g. closed for maintenance) and items they can't get. Nothing is bought
    or gathered there, and excluded items are never a raw material.
    """
    banned_locs, banned_items = set(ex[0]), set(ex[1])
    src = defaultdict(lambda: {"shops": [], "spots": [], "provided": False, "note": "", "source": "",
                               "fallback": None, "weight": 0})
    for iid in have:
        if iid not in banned_items:
            src[iid]["provided"] = True
    for iid, x in db.get("self_supplied", {}).items():  # from knowledge/shortcuts.json
        if iid in banned_items:
            continue
        src[iid]["provided"] = True
        src[iid]["note"], src[iid]["source"] = x["note"], x["source"]
    for s in db["shops"]:
        if s["location"] in banned_locs or s["item_id"] in banned_items:
            continue
        if s["item_id"] and (max_shop_rank is None or (s["rank"] or 0) <= max_shop_rank
                             or s.get("always_buy")):
            src[s["item_id"]]["shops"].append(s)
    for c in db["collections"]:
        if c["location"] in banned_locs:
            continue
        for i in c["items"]:
            if i["id"] not in banned_items:
                src[i["id"]]["spots"].append(c)
    for v in src.values():
        v["shops"].sort(key=lambda s: (s["location"] in UNAVAILABLE_SHOPS, s["price"] or 0))
        v["anytime"] = bool(v["spots"]) or v["provided"]
        v["unavailable"] = not v["anytime"] and v["shops"][0]["location"] in UNAVAILABLE_SHOPS
        v["gold"] = 0 if v["anytime"] else (v["shops"][0]["price"] or 0)
    if fallback:
        for iid, it in db["items"].items():
            if iid in src or iid in exclude or iid in banned_items:
                continue
            shops = sorted((sh for sh in db["shops"] if sh["item_id"] == iid
                            and sh["location"] not in UNAVAILABLE_SHOPS
                            and sh["location"] not in banned_locs), key=lambda sh: sh["price"] or 0)
            # grows steeply with rank: many low-rank pieces beat one high-rank finished item
            weight = round(2 ** ((it["rank"] or 50) / 4))
            src[iid] = {"shops": shops, "spots": [], "provided": False, "note": "", "anytime": True,
                        "unavailable": False, "gold": 0,
                        "fallback": "over_limit" if shops else "missing",
                        "weight": weight if shops else weight * 2}
    return dict(src)


# ---------------------------------------------------------------- solver

class Solver:
    """
    Cheapest routes from raw materials, following the stage rule:
    shop purchases may only go into compounds of Stage 1..buy_stages;
    later stages use crafted items (gathered items may be used at any stage).

    Item states searched:
      L[j][id]  best way to hold the item within j stages, purchases allowed
                (j = 0: bought or gathered directly)
      A[id]     crafted or gathered (never bought directly), any stage
    Costs are compared as (hard-to-get weight, Hawaii-only items, unconfirmed,
    steps, books, gold).
    """

    def __init__(self, db, raw, buy_stages=2, banned_items=()):
        self.items, self.raw = db["items"], raw
        self.K = max(1, buy_stages)
        banned = set(banned_items)  # excluded items: never made, never used as a material
        usable = [f for f in db["compounds"] if recommendable(f, self.items)
                  and f["result_id"] not in banned and not any(m["id"] in banned for m in f["materials"])]
        # an item made by one of the player's shortcut recipes is only made that way ...
        pinned = {f["result_id"] for f in usable if f.get("user")}
        for _ in range(2):
            self.formulas = [f for f in usable if f.get("user") or f["result_id"] not in pinned]
            self.by_id = {f["id"]: f for f in self.formulas}
            self._levels()
            self._any()
            # ... unless exclusions broke that shortcut: then wlohub's recipes may make it
            broken = {r for r in pinned if r not in self.A and r not in self.L[-1]}
            if not broken:
                break
            pinned -= broken

    @staticmethod
    def step_cost(f):
        return (0, 0, 0 if f["verified"] else 1, 1, 1 if f["book"] else 0, 0)

    def _levels(self):
        base = {i: (s.get("weight", 0), 1 if s["unavailable"] else 0, 0, 0, 0, s["gold"])
                for i, s in self.raw.items()}
        self.L, self.Lchoice = [base], [{i: None for i in base}]
        for _ in range(1, self.K):
            prev = self.L[-1]
            cur, choice = dict(prev), {i: "inherit" for i in prev}
            for f in self.formulas:
                if all(m["id"] in prev for m in f["materials"]):
                    c = add(self.step_cost(f), *(prev[m["id"]] for m in f["materials"]))
                    r = f["result_id"]
                    if r not in cur or c < cur[r]:
                        cur[r], choice[r] = c, f
            self.L.append(cur)
            self.Lchoice.append(choice)

    def _any(self):
        """Knuth's generalisation of Dijkstra over the AND-OR recipe graph."""
        low = self.L[-1]
        A, choice, heap = {}, {}, []

        def offer(r, c, how):
            if r not in A or c < A[r]:
                A[r], choice[r] = c, how
                heapq.heappush(heap, (c, r))

        for i, s in self.raw.items():
            if s["anytime"]:
                offer(i, (s.get("weight", 0), 0, 0, 0, 0, 0), ("gather", None))
        for f in self.formulas:  # last stage that may still use purchases
            if all(m["id"] in low for m in f["materials"]):
                offer(f["result_id"], add(self.step_cost(f), *(low[m["id"]] for m in f["materials"])),
                      ("low", f))

        uses, waiting = defaultdict(list), []
        for k, f in enumerate(self.formulas):
            ids = {m["id"] for m in f["materials"]}
            waiting.append(len(ids))
            for i in ids:
                uses[i].append(k)
        done = set()
        while heap:
            c, i = heapq.heappop(heap)
            if i in done or c != A[i]:
                continue
            done.add(i)
            for k in uses[i]:
                waiting[k] -= 1
                if waiting[k]:
                    continue
                f = self.formulas[k]
                if f["result_id"] not in done:
                    offer(f["result_id"], add(self.step_cost(f), *(A[m["id"]] for m in f["materials"])),
                          ("high", f))
        self.A, self.Achoice = A, choice

    # -- queries
    def best(self, iid):
        return self.A.get(iid)

    def final_formula(self, iid):
        return self.Achoice.get(iid, (None, None))[1]

    def obtainable(self, iid):
        return iid in self.A or iid in self.L[-1]

    def formula_cost(self, f):
        """Best total cost of a route whose last step is formula f (or None)."""
        mats = [m["id"] for m in f["materials"]]
        options = []
        if all(m in self.L[-1] for m in mats):
            options.append(add(self.step_cost(f), *(self.L[-1][m] for m in mats)))
        if all(m in self.A for m in mats):
            options.append(add(self.step_cost(f), *(self.A[m] for m in mats)))
        return min(options) if options else None

    # -- turning the choices into concrete steps
    def final_mode(self, f):
        """How to source the inputs of final formula f: "low" (may use purchases) or "high"."""
        mats = [m["id"] for m in f["materials"]]
        options = []
        if all(m in self.L[-1] for m in mats):
            options.append((add(self.step_cost(f), *(self.L[-1][m] for m in mats)), "low"))
        if all(m in self.A for m in mats):
            options.append((add(self.step_cost(f), *(self.A[m] for m in mats)), "high"))
        return min(options)[1] if options else None

    def plan(self, tid, final=None):
        """
        final: a formula for tid to use as the last step instead of the best one.

        Returns (steps, shopping, depth):
          steps    [(node, formula, times, child_nodes)] in a valid order
          shopping Counter(item_id -> quantity) of raw materials
          depth    node -> stage number (0 for raw materials)
        Nodes are ints; identical sub-routes share one node.
        """
        nodes, intern = [], {}

        def make(key, item, f, kids):
            if key not in intern:
                intern[key] = len(nodes)
                nodes.append((item, f, kids))
            return intern[key]

        memo = {}

        def node(kind, iid, j=0):
            k = (kind, iid, j)
            if k in memo:
                return memo[k]
            if kind == "A":
                how, f = self.Achoice[iid]
                if how == "gather":
                    n = make(("raw", iid), iid, None, ())
                else:
                    kids = tuple(node("L", m["id"], self.K - 1) if how == "low" else node("A", m["id"])
                                 for m in f["materials"])
                    n = make((f["id"], kids), iid, f, kids)
            else:
                while j > 0 and self.Lchoice[j][iid] == "inherit":
                    j -= 1
                if j == 0:
                    n = make(("raw", iid), iid, None, ())
                else:
                    f = self.Lchoice[j][iid]
                    kids = tuple(node("L", m["id"], j - 1) for m in f["materials"])
                    n = make((f["id"], kids), iid, f, kids)
            memo[k] = n
            return n

        if final is None:
            root = node("A", tid)
        else:
            how = self.final_mode(final)
            kids = tuple(node("L", m["id"], self.K - 1) if how == "low" else node("A", m["id"])
                         for m in final["materials"])
            root = make((final["id"], kids), tid, final, kids)
        depth = {}

        def d(n):
            if n not in depth:
                kids = nodes[n][2]
                depth[n] = 1 + max(d(c) for c in kids) if kids else 0
            return depth[n]

        d(root)
        need = Counter({root: 1})
        steps, shopping = [], Counter()
        for n in sorted(depth, key=depth.get, reverse=True):
            q = need.get(n)
            if not q:
                continue
            item, f, kids = nodes[n]
            if f is None:
                shopping[item] += q
                continue
            steps.append((n, f, q, kids))
            for c in kids:
                need[c] += q
        steps.sort(key=lambda s: (depth[s[0]], self.items[s[1]["result_id"]]["rank"] or 0,
                                  s[1]["result_id"], s[0]))
        return steps, shopping, depth


# ---------------------------------------------------------------- guide data

class GuideBuilder:
    """
    Builds the step-by-step guide for one target as plain data (JSON-ready).
    The web app renders it directly; render_markdown() turns it into Markdown.
    """

    def __init__(self, db, raw, solver, max_shop_rank, rebuild=None, have=(), ex=((), ())):
        self.ex = ex
        self.db, self.items = db, db["items"]
        self.raw, self.solver, self.max_shop_rank = raw, solver, max_shop_rank
        self.rebuild, self.have = rebuild, list(have)  # rebuild(extra_have_ids) -> Solver
        names = Counter(r["name"] for r in self.items.values())
        self.dup = {n for n, c in names.items() if c > 1}

    # -- small helpers
    def name(self, iid):
        n = self.items[iid]["name"]
        return f"{n} [#{iid}]" if n in self.dup else n

    def ref(self, iid):
        it = self.items[iid]
        return {"id": iid, "name": self.name(iid), "rank": it["rank"], "bases": it["bases"],
                "type": it["type"], "subtype": it["subtype"], "icon": it["icon_url"]}

    def formula(self, f):
        return {"id": f["id"], "materials": [self.ref(m["id"]) for m in f["materials"]],
                "book": f["book"], "result": self.ref(f["result_id"]),
                "verified": f["verified"], "verified_by": f["verified_by"],
                "rank_spread": f["rank_spread"], "inferred": f.get("inferred"),
                "user": bool(f.get("user")), "note": f.get("note", "")}

    def source(self, iid):
        s = self.raw[iid]
        if s["provided"]:
            source = s.get("source") or "from your bag"
            return {"kind": "provided", "text": source[0].upper() + source[1:],
                    "source": source, "note": s.get("note", "")}
        if s.get("fallback") == "missing":
            return {"kind": "missing", "text": "Get elsewhere (drop, quest or trade)"}
        if s.get("fallback") == "over_limit":
            sh = s["shops"][0]
            return {"kind": "over_limit", "text": f"Bought – {sh['location']} (above your rank limit)",
                    "location": sh["location"], "price": sh["price"]}
        if s["spots"]:
            c = s["spots"][0]
            xy = f" X {c['x']}, Y {c['y']}" if c["x"] is not None else ""
            return {"kind": "gather", "text": f"Gathered – {c['location']}{xy}",
                    "location": c["location"], "x": c["x"], "y": c["y"], "via": c["via"]}
        sh = s["shops"][0]
        bad = sh["location"] in UNAVAILABLE_SHOPS
        return {"kind": "hawaii" if bad else "buy", "text": f"Bought – {sh['location']}",
                "location": sh["location"], "price": sh["price"]}

    # -- whole guide
    def build(self, tid, show_all=False, unlock=True):
        items, solver = self.items, self.solver
        g = {"target": self.ref(tid),
             "settings": {"max_shop_rank": self.max_shop_rank, "buy_stages": solver.K,
                          "have": [self.ref(i) for i in self.have],
                          "exclude_locations": list(self.ex[0]),
                          "exclude_items": [self.ref(i) for i in self.ex[1] if i in items]},
             "direct": None, "route": None, "no_route": None}
        direct = [f for f in self.db["compounds"] if f["result_id"] == tid]

        if tid in self.raw and not self.raw[tid]["unavailable"]:
            src = self.source(tid)
            if src["kind"] == "buy" and src.get("price"):
                src["text"] += f" for {src['price']}g"
            g["direct"] = src
        elif solver.best(tid) is not None:
            g["route"] = self.route(tid, direct)
        else:
            g["no_route"] = self.explain_no_route(tid, unlock)

        tier1 = solver.final_formula(tid)

        def with_missing(f):
            d = self.formula(f)
            d["missing"] = sorted({self.name(m["id"]) for m in f["materials"]
                                   if not solver.obtainable(m["id"])})
            return d

        g["tier2"] = [with_missing(f) for f in direct
                      if f["verified"] and recommendable(f, items) and f is not tier1][:5]
        tier3 = [f for f in direct if not f["verified"] and recommendable(f, items) and f is not tier1]
        shown = show_all or (solver.best(tid) is None and not g["tier2"])
        tier3.sort(key=lambda f: sum(not solver.obtainable(m["id"]) for m in f["materials"]))
        g["tier3"] = {"count": len(tier3), "shown": shown,
                      "formulas": [with_missing(f) for f in tier3[:5]] if shown else []}
        g["risky"] = [self.formula(f) for f in direct
                      if f["verified"] and f["category"] == "risky"][:5]
        return g

    def route(self, tid, direct, final=None):
        """final: formula to use as the last step (an "Other possible final step")."""
        items, solver = self.items, self.solver
        cost = solver.formula_cost(final) if final else solver.best(tid)
        hard, hawaii, unconf, nsteps, nbooks, _ = cost
        steps, shopping, depth = solver.plan(tid, final)
        producer, consumers = {}, defaultdict(list)
        for k, (node, f, n, kids) in enumerate(steps, 1):
            producer[node] = k
            for kid, q in Counter(kids).items():
                consumers[kid].append((k, q * n))

        # Part A
        groups = defaultdict(list)
        gold = 0
        for iid, qty in shopping.items():
            s, src = self.raw[iid], self.source(iid)
            if src["kind"] == "missing":
                key = (-1, "missing", "Get elsewhere – not sold or gathered (drop, quest or trade)")
            elif src["kind"] == "over_limit":
                key = (4, "over_limit", f"Buy at {src['location']} – above your shop-rank limit")
            elif src["kind"] == "provided":
                key = (0, "provided", "Items you provide yourself (owned, drop, quest or trade)")
            elif src["kind"] == "gather":
                xy = f" (X {src['x']}, Y {src['y']})" if src["x"] is not None else ""
                via = f", via {src['via']}" if src["via"] else ""
                key = (1, "gather", f"Gather at {src['location']}{xy}{via}")
            elif src["kind"] == "hawaii":
                key = (3, "hawaii", f"Buy at {src['location']} – NOT purchasable in game right now")
            else:
                key = (2, "buy", f"Buy at {src['location']}")
            entry = {"item": self.ref(iid), "qty": qty, "price": None, "subtotal": None, "note": "",
                     "alts": [c["location"] for c in s["spots"][1:3]]
                     + [sh["location"] for sh in s["shops"][(0 if s["anytime"] else 1):3]
                        if sh["location"] not in UNAVAILABLE_SHOPS]}
            if src["kind"] in ("buy", "over_limit"):
                if src["price"]:
                    entry.update(price=src["price"], subtotal=src["price"] * qty)
                    gold += src["price"] * qty
                else:
                    entry["note"] = "price not listed on wlohub"
            groups[key].append(entry)
        shopping_groups = [
            {"kind": kind, "title": title,
             "items": sorted(es, key=lambda e: (e["item"]["rank"] or 0, e["item"]["name"]))}
            for (_, kind, title), es in sorted(groups.items())]
        books = Counter()
        for _, f, n, _ in steps:
            if f["book"]:
                books[f["book"]] += n

        timeline = self.timeline(tid, steps, depth)

        # alternative final steps
        best_final = solver.final_formula(tid)
        chosen = final or best_final
        alts = []
        for f in direct:
            if f is chosen or not recommendable(f, items):
                continue
            c = solver.formula_cost(f)
            if c is not None:
                alts.append((c, f))
        alts.sort(key=lambda a: (a[1] is not best_final, a[0]))  # the recommended one first

        return {
            "summary": {"actions": nsteps, "recipes": len(steps), "stages": max(depth.values()),
                        "steps": len(timeline),
                        "unconfirmed": unconf, "books": nbooks, "gold": gold, "hawaii": hawaii,
                        "materials_total": sum(shopping.values()), "materials_distinct": len(shopping)},
            "shopping": shopping_groups,
            "books": [{"book": b, "qty": q} for b, q in sorted(books.items())],
            "timeline": timeline,
            "alternatives": [{"formula": self.formula(f), "actions": c[3], "unconfirmed": c[2],
                              "hawaii": c[1], "recommended": f is best_final}
                             for c, f in alts[:4 if final else 3]],
            "selected_final": self.formula(final) if final else None,
            "partial": bool(hard),
            "get_elsewhere": [{"item": self.ref(i), "qty": q, "kind": self.raw[i]["fallback"],
                               "where": self.source(i)["text"]}
                              for i, q in sorted(shopping.items(),
                                                 key=lambda kv: -(self.raw[kv[0]].get("weight", 0)))
                              if self.raw[i].get("fallback")],
        }

    def timeline(self, tid, steps, depth):
        """
        On-site compounding: compounds can fail, so every map is visited once and,
        right after buying there, you compound everything that has become possible
        before leaving – first what uses the materials just bought, then whatever
        those results unlock. If a compound fails you can rebuy on the spot.

        Order: items from your bag / to get elsewhere first, then maps in the order
        that lets you finish the most compounds on-site at each stop. Within a stop
        the compounds keep a depth-first order (bigger branch first).
        """
        K = self.solver.K
        info = {node: (f, n, kids) for node, f, n, kids in steps}
        parents = defaultdict(list)  # node -> [(parent node, qty used)]
        for node, f, n, kids in steps:
            for kid, q in Counter(kids).items():
                parents[kid].append((node, q * n))
        root = next(node for node, f, n, kids in reversed(steps)
                    if f["result_id"] == tid and not parents[node])

        size = {}

        def subtree(node):
            if node not in size:
                size[node] = 1 + sum(subtree(k) for k in set(info[node][2]) if k in info)
            return size[node]

        order, seen = [], set()  # depth-first, bigger branch first

        def dfs(node):
            seen.add(node)
            kids = list(dict.fromkeys(k for k in info[node][2] if k in info))
            for kid in sorted(kids, key=lambda k: -subtree(k)):
                if kid not in seen:
                    dfs(kid)
            order.append(node)

        dfs(root)
        pos = {node: i for i, node in enumerate(order)}

        # raw materials per compound and where they come from
        raws = {}
        total = Counter()
        for node in order:
            f, n, kids = info[node]
            r = Counter()
            for m, kid in zip(f["materials"], kids):
                if kid not in info:
                    assert self.raw[m["id"]]["anytime"] or depth[node] <= K, \
                        f"shop item {m['id']} used in stage {depth[node]}"
                    r[m["id"]] += n
            raws[node] = r
            total.update(r)
        groups, _ = self.acquire_groups(total)
        gidx = {e["item"]["id"]: gi for gi, g in enumerate(groups) for e in g["items"]}
        need_g = {node: {gidx[i] for i in raws[node]} for node in order}
        need_c = {node: {k for k in info[node][2] if k in info} for node in order}

        def ready_after(visited, done):
            """Compounds that become possible after visiting `visited` (used to pick the next map)."""
            done, new = set(done), []
            while True:
                batch = [nd for nd in order if nd not in done
                         and need_g[nd] <= visited and need_c[nd] <= done]
                if not batch:
                    return new
                done.update(batch)
                new.extend(batch)

        def compound_here(gi):
            """Do every compound that is now possible, those using map gi's materials first."""
            while True:
                batch = [nd for nd in order if nd not in done
                         and need_g[nd] <= visited and need_c[nd] <= done]
                if not batch:
                    return
                nd = min(batch, key=lambda x: (gi not in need_g[x], pos[x]))
                done.add(nd)
                seq.append(("compound", nd))

        # schedule: ("trip", group index) / ("compound", node)
        seq, visited, done = [], set(), set()
        for gi, g in enumerate(groups):  # your bag / items from elsewhere: ready before travelling
            if g["kind"] in ("provided", "missing"):
                visited.add(gi)
                seq.append(("trip", gi))
        compound_here(None)
        maps = [gi for gi in range(len(groups)) if gi not in visited]
        while maps:
            def score(gi):
                unlocked = ready_after(visited | {gi}, done)
                first = min((pos[nd] for nd in order if gi in need_g[nd]), default=len(order))
                return (-len(unlocked), first)
            gi = min(maps, key=score)
            maps.remove(gi)
            visited.add(gi)
            seq.append(("trip", gi))
            compound_here(gi)
        assert len(done) == len(order), "some compounds could not be scheduled"

        # number the steps
        num, trip_no = {}, {}
        for k, (kind, v) in enumerate(seq, 1):
            (trip_no if kind == "trip" else num)[v] = k

        out, standby, here = [], Counter(), None
        for k, (kind, v) in enumerate(seq, 1):
            if kind == "trip":
                g = groups[v]
                if g["kind"] not in ("provided", "missing"):
                    here = g
                out.append({"no": k, "kind": "trip", "group": g,
                            "gold": sum(e["subtotal"] or 0 for e in g["items"])})
                continue
            node = v
            f, n, kids = info[node]
            slots = []
            for s_no, (m, kid) in enumerate(zip(f["materials"], kids), 1):
                iid = m["id"]
                if kid in info:
                    src = num[kid]
                    frm = {"kind": "made" if src == k - 1 else "standby", "step": src,
                           "text": f"from Step {src}"}
                else:
                    g = groups[gidx[iid]]
                    t = trip_no[gidx[iid]]
                    verb = {"gather": "gathered at", "provided": "", "missing": "got"}.get(g["kind"], "bought at")
                    where = g["source"] if g["kind"] == "provided" else (g["place"] or "elsewhere")
                    frm = {"kind": g["kind"], "step": t, "text": f"{verb} {where} (Step {t})".strip()}
                slots.append({"slot": s_no, "role": "Primary" if s_no == 1 else f"Sub {s_no - 1}",
                              "item": self.ref(iid), "qty": n, "from": frm})
            for kid, q in Counter(kids).items():
                if kid in info:
                    standby[kid] -= q * n
                    assert standby[kid] >= 0, f"standby went negative for node {kid}"
            standby[node] += n
            uses_here = here is not None and any(gidx[i] == groups.index(here) for i in raws[node])
            out.append({
                "no": k, "kind": "compound", "result": self.ref(f["result_id"]), "times": n,
                "formula": self.formula(f), "slots": slots, "book": f["book"],
                "acquire": [], "gold": 0,
                "stage": depth[node], "buy_allowed": depth[node] <= K,
                "is_target": node == root,
                "at": here["place"] if here else "", "at_local": uses_here,
                "keep_for": sorted(({"step": num[p], "qty": q} for p, q in parents[node]),
                                   key=lambda d: d["step"]),
                "standby": [{"item": self.ref(info[x][0]["result_id"]), "qty": q, "from_step": num[x]}
                            for x, q in sorted(standby.items(), key=lambda kv: num[kv[0]]) if q > 0],
            })

        # which steps use each bought item, and which steps to do on-site after each trip
        for st in out:
            if st["kind"] != "trip":
                continue
            g = st["group"]
            for e in g["items"]:
                need = Counter()
                for x in out:
                    if x["kind"] == "compound":
                        q = sum(sl["qty"] for sl in x["slots"]
                                if sl["item"]["id"] == e["item"]["id"] and sl["from"]["step"] == st["no"])
                        if q:
                            need[x["no"]] += q
                e["uses"] = [{"step": s, "qty": q} for s, q in sorted(need.items())]
            onsite = []
            for x in out[st["no"]:]:
                if x["kind"] != "compound":
                    break
                onsite.append(x["no"])
            st["onsite"] = onsite
        for st in out:
            st["instruction"] = self.trip_instruction(st) if st["kind"] == "trip" else self.instruction(st)
        return out

    @staticmethod
    def trip_instruction(st):
        g = st["group"]
        T = lambda text, b=False: {"t": text, "b": b}
        if g["kind"] == "provided":
            return [T("Take these " + (g["source"] or "from your bag") + ":")]
        if g["kind"] == "missing":
            return [T("Get these elsewhere (⚠ not sold or gathered – drop, quest or trade):")]
        onsite = st.get("onsite") or []
        span = (f"Step {onsite[0]}" + (f"–{onsite[-1]}" if len(onsite) > 1 else "")) if onsite else ""
        if g["kind"] == "gather":
            then = f", then do {span} right here before leaving" if onsite else ""
            return [T("Go to "), T(g["place"], True), T(f" and gather everything for this route in one visit{then}:")]
        extra = {"hawaii": " (⛔ not purchasable in game right now)",
                 "over_limit": " (above your shop-rank limit)"}.get(g["kind"], "")
        then = (f", then stay here and do {span} on the spot before leaving "
                "(if a compound fails, buy replacements right away)") if onsite else ""
        return [T("Go to "), T(g["place"], True), T(f" and buy everything for this route in one visit{extra}{then}:")]

    def acquire_groups(self, raws):
        """Raw materials for one step, grouped by where you get them."""
        groups = {}
        gold = 0
        for iid, qty in sorted(raws.items(), key=lambda kv: (self.items[kv[0]]["rank"] or 0, kv[0])):
            src = self.source(iid)
            kind = "buy" if src["kind"] == "over_limit" else src["kind"]  # same shop = same visit
            key = (kind, src.get("location", src.get("source", "")))
            if key not in groups:
                place = src.get("location", "")
                if src["kind"] == "gather" and src.get("x") is not None:
                    place += f" (X {src['x']}, Y {src['y']})"
                if src["kind"] == "gather" and src.get("via"):
                    place += f", via {src['via']}"
                groups[key] = {"kind": kind, "place": place, "items": [], "location": src.get("location", ""),
                               "source": src.get("source", ""),
                               "note": src.get("note", "")}
            price = src.get("price") if src["kind"] in ("buy", "over_limit") else None
            groups[key]["items"].append({"item": self.ref(iid), "qty": qty, "price": price,
                                         "subtotal": price * qty if price else None,
                                         "note": "above your shop-rank limit"
                                         if src["kind"] == "over_limit" else ""})
            gold += price * qty if price else 0
        order = {"missing": -1, "provided": 0, "gather": 1, "buy": 2, "hawaii": 3, "over_limit": 4}
        return sorted(groups.values(), key=lambda g: (order[g["kind"]], g["place"])), gold

    @staticmethod
    def instruction(st):
        """
        The step as one plain sentence, as a list of segments:
        {"t": text, "b": bold?} | {"item": ref} | {"step": n}
        """
        seg = []
        T = lambda text, b=False: seg.append({"t": text, "b": b})
        I = lambda ref: seg.append({"item": ref})
        S = lambda n: seg.append({"step": n})

        def listing(entries):
            for k, e in enumerate(entries):
                if k:
                    T(" and " if k == len(entries) - 1 else ", ")
                T(f"{e['qty']}× ")
                I(e["item"])
                if e["price"]:
                    T(f" ({e['price']}g each)")

        for k, g in enumerate(st["acquire"]):
            first = k == 0
            if g["kind"] == "missing":
                T("Get " if first else ", then get ")
                listing(g["items"])
                T(" elsewhere (⚠ not sold or gathered – drop, quest or trade)")
                continue
            if g["kind"] == "provided":
                T("Take " if first else ", then take ")
                listing(g["items"])
                T(" " + (g.get("source") or "from your bag"))
                if g.get("note"):
                    T(f" ({g['note']})")
                continue
            T("Go to " if first else ", then go to ")
            T(g["place"], True)
            T(" and gather " if g["kind"] == "gather" else " and buy ")
            listing(g["items"])
            if g["kind"] == "hawaii":
                T(" (⛔ not purchasable in game right now)")
            if g["kind"] == "over_limit":
                T(" (above your shop-rank limit)")
        if st["acquire"]:
            T(". ")

        if st.get("at"):
            T("Right here at " if st.get("at_local") else "While still at ")
            T(st["at"], True)
            T(", compound ")
        else:
            T("Compound ")
        for k, sl in enumerate(st["slots"]):
            if k:
                T(" + ")
            I(sl["item"])
            if sl["from"]["step"]:
                T(" (from ")
                S(sl["from"]["step"])
                T(")")
        if st["book"]:
            T(f" + Alchemy Book {st['book']}")
        T(" to create ")
        I(st["result"])
        T(f" (R{st['result']['rank']})")
        name = st["result"]["name"]
        if st["times"] > 1:
            T(f". Repeat until you have {st['times']} pieces of {name}")
        if st["is_target"]:
            T(". 🎯 This is your target – finished!")
            return seg
        keep = st["keep_for"]
        if not (len(keep) == 1 and keep[0]["step"] == st["no"] + 1):
            T(". Keep " + ("them" if st["times"] > 1 else "it") + " on standby for ")
            for k, d in enumerate(keep):
                if k:
                    T(" and " if k == len(keep) - 1 else ", ")
                S(d["step"])
                if len(keep) > 1 or d["qty"] != st["times"]:
                    T(f" ({d['qty']}×)")
        T(".")
        return seg

    def explain_no_route(self, tid, unlock=True):
        """Which base type cannot be sourced, and which items would unlock a route."""
        items, solver = self.items, self.solver
        direct = [f for f in self.db["compounds"]
                  if f["result_id"] == tid and recommendable(f, items)]
        primary = lambda i: items[i]["bases"][0] if items[i]["bases"] else None
        have_base = {primary(i) for i in items if primary(i) and solver.obtainable(i)}
        needed = {primary(m["id"]) for f in direct for m in f["materials"] if primary(m["id"])}
        missing = sorted(needed - have_base)
        shop_locs = lambda i: sorted({sh["location"] for sh in self.db["shops"] if sh["item_id"] == i})

        out = {"missing_bases": missing, "base_sources": [], "singles": [], "set": None}
        for b in missing:
            srcs = sorted({sh["item_id"] for sh in self.db["shops"]
                           if sh["bases"] and sh["bases"][0] == b})
            out["base_sources"].append({"base": b, "items": [
                {"item": self.ref(i), "locations": shop_locs(i)} for i in srcs]})
        out["unlock_pending"] = bool(self.rebuild) and not unlock
        if not self.rebuild or not unlock:  # the slow part; the web app asks for it separately
            return out

        cands = []
        for b in missing:
            for sh in self.db["shops"]:
                if sh["bases"] and sh["bases"][0] == b and sh["item_id"] not in cands:
                    cands.append(sh["item_id"])
        for f in sorted(direct, key=lambda f: not f["verified"]):
            for m in f["materials"]:
                if not solver.obtainable(m["id"]) and m["id"] not in cands:
                    cands.append(m["id"])
        found = []
        for c in cands[:25]:
            best = self.rebuild([c]).best(tid)
            if best is not None:
                found.append((best, c))
        found.sort()
        out["singles"] = [{"item": self.ref(c), "actions": b[3], "unconfirmed": b[2], "hawaii": b[1]}
                          for b, c in found[:5]]
        if not found:
            need = self.minimal_set(tid)
            if need:
                best = self.rebuild(need).best(tid)
                out["set"] = {"items": [{"item": self.ref(i), "locations": shop_locs(i)} for i in
                                        sorted(need, key=lambda i: (items[i]["rank"] or 0, self.name(i)))],
                              "actions": best[3], "unconfirmed": best[2]}
        return out

    def minimal_set(self, tid):
        """Smallest set (greedy) of out-of-rules shop items that makes a route possible."""
        pool = sorted({sh["item_id"] for sh in self.db["shops"]
                       if sh["item_id"] and sh["location"] not in self.ex[0] and sh["item_id"] not in self.ex[1]
                       and (sh["item_id"] not in self.raw or self.raw[sh["item_id"]]["unavailable"])})
        trial = self.rebuild(pool)
        if trial.best(tid) is None:
            return []
        _, shopping, _ = trial.plan(tid)
        keep = [i for i in pool if i in shopping]
        for i in sorted(keep, key=lambda i: -(self.items[i]["rank"] or 0)):  # hardest first
            rest = [k for k in keep if k != i]
            if self.rebuild(rest).best(tid) is not None:
                keep = rest
        return keep


# ---------------------------------------------------------------- markdown

def render_markdown(g):
    out = []
    w = out.append
    it = lambda r: f"**{r['name']}** (R{r['rank']})"
    bases = lambda r: " / ".join(r["bases"]) or "-"

    def label(f):
        if f.get("user"):
            return "★ YOUR SHORTCUT"
        if f["verified"]:
            return "✅ CONFIRMED" + (f" (verified by {f['verified_by']})" if f["verified_by"] else "")
        return "⚠️ UNCONFIRMED"

    def line(f):
        parts = [f"{m['name']} (R{m['rank']})" for m in f["materials"]]
        if f["book"]:
            parts.append(f"Alchemy Book {f['book']}")
        return (f"{' + '.join(parts)} → **{f['result']['name']}** (R{f['result']['rank']})  "
                f"{label(f)}  _formula #{f['id']}, rank spread {f['rank_spread']}_")

    def sentence(segs):
        out_ = []
        for x in segs:
            if "item" in x:
                out_.append(f"**{x['item']['name']}**")
            elif "step" in x:
                out_.append(f"Step {x['step']}")
            else:
                out_.append(f"**{x['t']}**" if x.get("b") else x["t"])
        return "".join(out_)

    t, st = g["target"], g["settings"]
    w(f"# Compound guide: {t['name']}")
    w("")
    w(f"**Rank {t['rank']}** · {t['type']} [{t['subtype']}] · Bases: {bases(t)} · item #{t['id']}")
    if st["have"]:
        w("")
        w("Items you said you can provide: " + ", ".join(h["name"] for h in st["have"]))
    if st.get("exclude_locations") or st.get("exclude_items"):
        w("")
        w("Excluded (not used anywhere in this route): " + ", ".join(
            [f"map {l}" for l in st["exclude_locations"]] + [f"item {i['name']}" for i in st["exclude_items"]]))
    w("")
    w("---")
    w("# Tier 1 – Recommended route (shop & gathered materials)")
    w("")

    if g["direct"]:
        w(f"No compounding needed: {g['direct']['text']}.")
    elif g["route"]:
        r = g["route"]
        s = r["summary"]
        if r.get("selected_final"):
            w(f"> 🔀 **Alternative final step chosen:** {line(r['selected_final'])}")
            w("")
        if r.get("partial"):
            w("> ⚠️ **Partial route.** No complete route exists from shop and gathered materials. "
              "This route needs these items that you must get some other way:")
            for x in r["get_elsewhere"]:
                w(f"> - {x['qty']}× **{x['item']['name']}** (R{x['item']['rank']}) – {x['where']}")
            w("")
        w("| Summary | |")
        w("|---|---|")
        w(f"| Steps to follow | **{s['steps']}** |")
        w(f"| Compound actions | **{s['actions']}** ({s['recipes']} different recipes) |")
        w(f"| Stages | {s['stages']} |")
        w(f"| Unconfirmed actions | {s['unconfirmed']} of {s['actions']} |")
        w(f"| Alchemy Books | {s['books']} |")
        w(f"| Materials to collect | {s['materials_total']} items ({s['materials_distinct']} different) |")
        w("")
        w(f"Buy only shop items up to **Rank {st['max_shop_rank']}**, and use bought items only in "
          f"**Stage 1–{st['buy_stages']}**. From Stage {st['buy_stages'] + 1} on, every step uses "
          "items you crafted (or gathered) earlier.")
        w("")
        if s["hawaii"]:
            w(f"> ⛔ **No route avoids Hawaii.** {s['hawaii']} material(s) below are only sold in "
              "Hawaii, which is not purchasable in game right now.")
            w("")
        if s["unconfirmed"]:
            w("> ⚠️ Steps marked **UNCONFIRMED** use wlohub formulas nobody has verified yet. "
              "Keep spare materials in case one fails.")
            w("")

        if r["books"]:
            w("**Have these Alchemy Books ready** (not sold or gathered – obtain separately): "
              + ", ".join(f"{b['qty']}× Alchemy Book {b['book']}" for b in r["books"]))
            w("")
        w("## Your step-by-step timeline")
        w("")
        w("Each map is visited once (🛒 steps). Right after buying there, do the compound steps that follow "
          "on the spot, before leaving – if a compound fails you can buy replacements immediately. "
          "Items in a compound are listed in slot order – the first one "
          "goes into **Slot 1 (Primary)**. Each compound makes 1 item, so *repeat until you have N "
          "pieces* means doing the same compound N times.")
        for stp in r["timeline"]:
            if stp["kind"] == "trip":
                w("")
                w(f"### Step {stp['no']} · 🛒 {sentence(stp['instruction'])}")
                for e in stp["group"]["items"]:
                    price = f" – {e['price']}g each = {e['subtotal']:,}g" if e["price"] else ""
                    used = ", ".join(f"Step {u['step']} ({u['qty']}×)" for u in e["uses"])
                    note = f" _({e['note']})_" if e.get("note") else ""
                    w(f"- **{e['qty']}×** {it(e['item'])} · {bases(e['item'])}{price}{note} → used in {used}")
                if stp["group"].get("note"):
                    w(f"- _{stp['group']['note']}_")
                if stp["gold"]:
                    w(f"- Cost here: {stp['gold']:,}g")
                w("- [ ] Done")
                continue
            f = stp["formula"]
            w("")
            w(f"### Step {stp['no']}  {label(f)}")
            w("")
            w(sentence(stp["instruction"]))
            w("")
            w("| Slot | Put in | Rank | Base | Comes from |")
            w("|---|---|---|---|---|")
            for sl in stp["slots"]:
                w(f"| {sl['slot']} · {sl['role']} | {sl['item']['name']} | {sl['item']['rank']} | "
                  f"{bases(sl['item'])} | {sl['from']['text']} |")
            if stp["book"]:
                w(f"| Book | Alchemy Book {stp['book']} | – | – | have it ready |")
            w("")
            if stp["gold"]:
                w(f"- Cost of this step: {stp['gold']:,}g")
            if stp["standby"] and not stp["is_target"]:
                w("- On standby after this step: " + ", ".join(
                    f"{x['qty']}× {x['item']['name']} (Step {x['from_step']})" for x in stp["standby"]))
            notes = [f"formula #{f['id']}", f"rank spread {f['rank_spread']}",
                     f"stage {stp['stage']}" + (" – shop items allowed" if stp["buy_allowed"] else "")]
            if f["inferred"] == "duplicated_single_material":
                notes.append("wlohub lists only 1 material; assumed 2× the same material")
            w(f"- _{' · '.join(notes)}_")
            w("- [ ] Done")
        w("")
        w("## Full shopping list (optional)")
        w("")
        w("Everything the timeline buys or gathers, grouped by place – useful if you prefer to "
          "collect it all in one trip.")
        for grp in r["shopping"]:
            w("")
            w(f"### {grp['title']}")
            for e in grp["items"]:
                extra = (f" — {e['price']}g each = **{e['subtotal']:,}g**" if e["price"]
                         else (f" — {e['note']}" if e["note"] else ""))
                alt = f" _(also: {', '.join(e['alts'])})_" if e["alts"] else ""
                w(f"- **{e['qty']}×** {it(e['item'])} · {bases(e['item'])}{extra}{alt}")
        w("")
        w(f"**Shop total: {s['gold']:,}g**" + (" (plus Alchemy Books)" if r["books"] else ""))
        w("")
        w("## Other possible final steps")
        w("")
        w("Alternatives for the last step only. Their inputs can also be made from shop / gathered "
          "materials (run the calculator on each input for its own guide).")
        w("")
        if not r["alternatives"]:
            w("_None._")
        for a in r["alternatives"]:
            hw = f", ⛔ {a['hawaii']} Hawaii item(s)" if a["hawaii"] else ""
            w(f"- {line(a['formula'])}  \n  _≈ {a['actions']} actions in total, "
              f"{a['unconfirmed']} unconfirmed_{hw}")
    else:
        nr = g["no_route"]
        w("No route found that starts only from shop and gathered materials.")
        w("")
        if nr["missing_bases"]:
            w(f"**Why:** none of the materials you can buy (Rank ≤ {st['max_shop_rank']}, not Hawaii) "
              f"or gather has the primary base **{' / '.join(nr['missing_bases'])}**, and every "
              "recipe for this item needs it.")
            for bs in nr["base_sources"]:
                if bs["items"]:
                    w(f"- Shop items with primary base {bs['base']}: " + "; ".join(
                        f"{x['item']['name']} (R{x['item']['rank']}) – {', '.join(x['locations'])}"
                        for x in bs["items"]) + " (outside your buying rules)")
                else:
                    w(f"- No shop or gathering spot provides a {bs['base']}-primary item at all.")
        else:
            w("**Why:** the base types exist at low rank, but no chain of known formulas climbs "
              "high enough with the materials you can buy or gather.")
        w("")
        have_ids = [h["id"] for h in st["have"]]
        if nr["singles"]:
            w("**Items that would unlock a route if you can get them** (from a drop, quest, trade "
              "or your own bag) – re-run with `--have`:")
            w("")
            w("| Item | Rank | Route if you have it | Command |")
            w("|---|---|---|---|")
            for x in nr["singles"]:
                hw = f", ⛔ {x['hawaii']} Hawaii" if x["hawaii"] else ""
                w(f"| {x['item']['name']} | {x['item']['rank']} | {x['actions']} actions, "
                  f"{x['unconfirmed']} unconfirmed{hw} | `--have {x['item']['id']}` |")
        elif nr["set"]:
            ns = nr["set"]
            w(f"**No single item is enough. You need all {len(ns['items'])} of these** (they are "
              "only sold outside your buying rules, so get them by drop, quest, trade or from your bag):")
            w("")
            w("| Item | Rank | Base | Normally sold at |")
            w("|---|---|---|---|")
            for x in ns["items"]:
                w(f"| {x['item']['name']} | {x['item']['rank']} | {bases(x['item'])} | "
                  f"{', '.join(x['locations'])} |")
            w("")
            w(f"With them the route is {ns['actions']} compound actions ({ns['unconfirmed']} "
              "unconfirmed). Run:")
            w("")
            w("```")
            w(f"python wlo_calc.py --id {t['id']} " + " ".join(
                f"--have {i}" for i in have_ids + [x["item"]["id"] for x in ns["items"]]))
            w("```")
        else:
            w("No combination of shop items unlocks a route. Use `--have` with items you own "
              "(for example the materials of a Tier 2 formula below).")

    def formulas(title, fs, intro=None):
        w("")
        w("---")
        w("")
        w(f"## {title}")
        w("")
        if intro:
            w(intro)
            w("")
        if not fs and not intro:
            w("_None._")
        for f in fs:
            note = ""
            if "missing" in f:
                note = ("  \n  _All inputs obtainable from shops / gathering._" if not f["missing"] else
                        f"  \n  _Needs items not obtainable from shops / gathering: {', '.join(f['missing'])}_")
            w(f"- {line(f)}{note}")

    formulas("Tier 2 – Other confirmed formulas", g["tier2"])
    t3 = g["tier3"]
    formulas("Tier 3 – Unconfirmed formulas (last resort)", t3["formulas"],
             None if t3["shown"] else f"{t3['count']} unconfirmed formula(s); run with `--all` to list them.")
    if g["risky"]:
        formulas("⚠️ WARNING – Confirmed but dangerous formulas", g["risky"],
                 "> **The rank difference between these materials is greater than 2. These compounds "
                 "are dangerous and highly likely to fail.** Use them only if you accept the risk of "
                 "losing the materials.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- engine

class LRU:
    """Small thread-safe least-recently-used cache."""

    def __init__(self, size):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)
        return value


class Engine:
    """
    Loads the data once and caches solvers and finished guides; shared by the
    CLI and the web app. Safe to call from several threads at once: nothing
    holds a lock while computing, so a slow request never blocks a fast one.
    """

    def __init__(self, data_dir=None, max_solvers=64, max_guides=500):
        self.db = wlo_data.load(data_dir) if data_dir else wlo_data.load()
        self._solvers = LRU(max_solvers)
        self._guides = LRU(max_guides)
        self._unlocks = LRU(max_guides)

    @staticmethod
    def _ex(ex):
        """Normalise exclusions to (sorted location names, sorted item ids)."""
        locs, its = ex or ((), ())
        return tuple(sorted({l for l in locs if l})), tuple(sorted({int(i) for i in its}))

    def solver(self, max_shop_rank=14, buy_stages=2, have=(), partial_for=None, ex=((), ())):
        ex = self._ex(ex)
        key = (max_shop_rank, buy_stages, tuple(sorted(set(have))), partial_for, ex)
        hit = self._solvers.get(key)
        if hit is None:
            raw = raw_sources(self.db, max_shop_rank, key[2], fallback=partial_for is not None,
                              exclude=(partial_for,), ex=ex)
            hit = self._solvers.put(key, (raw, Solver(self.db, raw, buy_stages, banned_items=ex[1])))
        return hit

    def _builder(self, max_shop_rank, buy_stages, have, ex=((), ())):
        raw, solver = self.solver(max_shop_rank, buy_stages, have, ex=ex)

        def rebuild(extra):
            return self.solver(max_shop_rank, buy_stages, list(have) + list(extra), ex=ex)[1]

        return GuideBuilder(self.db, raw, solver, max_shop_rank, rebuild, have, self._ex(ex))

    def _have(self, have):
        return [h for h in dict.fromkeys(have) if h in self.db["items"]]

    def guide(self, tid, max_shop_rank=14, buy_stages=2, have=(), show_all=False, unlock=True,
              final=None, ex=((), ())):
        """
        unlock=False skips the slow "which items would unlock a route" search (see unlock()).
        final: id of one of the "other possible final steps" to build the route around.
        ex: (excluded locations, excluded item ids) – maps / items to avoid completely.
        """
        have = self._have(have)
        ex = self._ex(ex)
        key = (tid, max_shop_rank, buy_stages, tuple(have), show_all, unlock, ex)
        hit = self._guides.get(key)
        if hit is None:
            g = self._builder(max_shop_rank, buy_stages, have, ex).build(tid, show_all, unlock)
            if g["no_route"] is not None:
                g["route"] = self.partial_route(tid, max_shop_rank, buy_stages, have, ex)
            hit = self._guides.put(key, g)
        if final in (None, "") or not hit["route"]:
            return hit
        fkey = key + (str(final),)
        alt = self._guides.get(fkey)
        if alt is None:
            alt = self._guides.put(fkey, self.with_final(hit, tid, max_shop_rank, buy_stages, have, str(final), ex))
        return alt

    def with_final(self, g, tid, max_shop_rank, buy_stages, have, final, ex=((), ())):
        """Copy of guide g whose route ends with the chosen final formula."""
        f = next((f for f in self.db["compounds"]
                  if str(f["id"]) == final and f["result_id"] == tid and recommendable(f, self.db["items"])), None)
        partial = g["no_route"] is not None
        raw, solver = self.solver(max_shop_rank, buy_stages, have, partial_for=tid if partial else None, ex=ex)
        if f is None or f not in solver.formulas or solver.formula_cost(f) is None:
            return dict(g, final_error="That final step can't be built with your current settings.")
        direct = [x for x in self.db["compounds"] if x["result_id"] == tid]
        route = GuideBuilder(self.db, raw, solver, max_shop_rank, None, have, self._ex(ex)).route(
            tid, direct, final=f)
        return dict(g, route=route)

    def partial_route(self, tid, max_shop_rank, buy_stages, have, ex=((), ())):
        """
        Best route when no full one exists: everything else from shops / gathering,
        plus as few (and as low-rank) items as possible that you must get elsewhere.
        """
        raw, solver = self.solver(max_shop_rank, buy_stages, have, partial_for=tid, ex=ex)
        if solver.best(tid) is None:
            return None
        direct = [f for f in self.db["compounds"] if f["result_id"] == tid]
        return GuideBuilder(self.db, raw, solver, max_shop_rank, None, have, self._ex(ex)).route(tid, direct)

    def unlock(self, tid, max_shop_rank=14, buy_stages=2, have=(), ex=((), ())):
        """The slow part of a no-route guide: items that would unlock a route."""
        have = self._have(have)
        ex = self._ex(ex)
        key = (tid, max_shop_rank, buy_stages, tuple(have), ex)
        hit = self._unlocks.get(key)
        if hit is None:
            hit = self._unlocks.put(key, self._builder(max_shop_rank, buy_stages, have, ex)
                                    .explain_no_route(tid, True))
        return hit

    def search(self, query, limit=30):
        q = query.lower().strip()
        if not q:
            return []
        if q.lstrip("#").isdigit():
            r = self.db["items"].get(int(q.lstrip("#")))
            return [r] if r else []
        hits = [r for r in self.db["items"].values() if q in r["name"].lower()]
        hits.sort(key=lambda r: (r["name"].lower() != q, not r["name"].lower().startswith(q),
                                 r["name"].lower(), r["id"]))
        return hits[:limit]


def find_items(db, query, iid):
    items = db["items"]
    if iid is not None:
        return [items[iid]] if iid in items else []
    q = query.lower().strip()
    exact = [r for r in items.values() if r["name"].lower() == q]
    return exact or [r for r in items.values() if q in r["name"].lower()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="target item name")
    ap.add_argument("--id", type=int, help="target item id (when names are ambiguous)")
    ap.add_argument("--all", action="store_true", help="also show Tier 3 unconfirmed formulas")
    ap.add_argument("--max-shop-rank", type=int, default=14,
                    help="highest rank of shop item you may buy (default: 14)")
    ap.add_argument("--have", action="append", default=[], metavar="ITEM",
                    help="item you own or can get yourself (drop, quest, trade); name or id; "
                         "repeat for several. Usable at any stage.")
    ap.add_argument("--buy-stages", type=int, default=2,
                    help="bought items may only be used in Stage 1..N (default: 2)")
    ap.add_argument("--final", help="formula id of an 'other possible final step' to build the route around")
    ap.add_argument("--exclude-location", action="append", default=[], metavar="MAP",
                    help="map/shop location to avoid (e.g. closed for maintenance); repeat for several")
    ap.add_argument("--exclude-item", action="append", default=[], metavar="ITEM",
                    help="item that is unavailable (name or id); repeat for several")
    ap.add_argument("--out", default="guides", help="folder for the Markdown guide (default: guides)")
    ap.add_argument("--stdout", action="store_true", help="print the guide instead of saving it")
    args = ap.parse_args()
    if not args.name and args.id is None:
        ap.error("give an item name or --id")

    engine = Engine()
    db = engine.db
    found = find_items(db, args.name or "", args.id)
    if not found:
        sys.exit("No matching item.")
    if len(found) > 1:
        print("Several items match; re-run with --id:")
        for r in sorted(found, key=lambda r: (r["name"], r["id"]))[:30]:
            print(f"  --id {r['id']:5}  {r['name']}  Rank {r['rank']}  {r['type']} [{r['subtype']}]")
        return
    tid = found[0]["id"]

    have = []
    for h in args.have:
        key = h.lstrip("#")
        hits = find_items(db, "", int(key)) if key.isdigit() else find_items(db, h, None)
        if len(hits) != 1:
            sys.exit(f"--have {h!r}: " + ("no such item" if not hits else
                     "ambiguous, use the id: " + ", ".join(f"{r['id']} {r['name']}" for r in hits[:10])))
        have.append(hits[0]["id"])

    xitems = []
    for h in args.exclude_item:
        key = h.lstrip("#")
        hits = find_items(db, "", int(key)) if key.isdigit() else find_items(db, h, None)
        if len(hits) != 1:
            sys.exit(f"--exclude-item {h!r}: " + ("no such item" if not hits else "ambiguous, use the id"))
        xitems.append(hits[0]["id"])
    g = engine.guide(tid, args.max_shop_rank, args.buy_stages, have, args.all, final=args.final,
                     ex=(args.exclude_location, xitems))
    if g.get("final_error"):
        print(f"--final {args.final}: {g['final_error']} Showing the recommended route.")
    text = render_markdown(g)
    if args.stdout:
        print(text)
        return
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", found[0]["name"]).strip("_")
    path = out / f"{safe}_{tid}.md"
    path.write_text(text, encoding="utf-8")
    print(f"Target: {g['target']['name']}")
    if g["direct"]:
        print(f"  No compounding needed: {g['direct']['text']}")
    elif g["route"]:
        s = g["route"]["summary"]
        print(f"  {s['actions']} compound actions in {s['stages']} stages "
              f"({s['unconfirmed']} unconfirmed), {s['books']} Alchemy Books, {s['gold']:,}g"
              + (f", {s['hawaii']} Hawaii-only item(s)" if s["hawaii"] else ""))
    else:
        print("  No Tier 1 route; see the guide for what would unlock one.")
    print(f"Guide saved: {path}")


if __name__ == "__main__":
    main()
