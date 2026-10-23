"""Front-desk inventory: admin CSV import (upsert by item_id, same replace-by-key rules
as the directory), visitor "do you have X" matching, and GA console status updates.
Never reveals Josa's personal stock to a visitor, and never reveals ga_only stock as
actually available - both are answered as if the item simply isn't here."""
import csv
import io
import logging
import re

from sqlalchemy import select

from .db import Item

log = logging.getLogger("virtual_ga.inventory")

ITEM_TEXT_FIELDS = ["area", "spot", "quantity", "lend_or_give", "audience", "status", "return_within", "notes"]

INTENT_RE = re.compile(
    r"\b(do (you|we) have|is there (a|an|any)|are there (any)?|can i (borrow|get|take)|"
    r"could i (borrow|get|take)|where (can|do) i (get|find|borrow))\b", re.I)

NOT_HERE_ANSWER = "I don't see that here, but I can get a GA to check for you."


def _split_aliases(raw: str) -> list[str]:
    return [a.strip() for a in raw.split("|") if a.strip()]


def import_inventory_csv(db, csv_text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    seen: set[str] = set()
    added = updated = 0
    for row in rows:
        item_id = (row.get("item_id") or "").strip()
        if not item_id:
            continue
        seen.add(item_id)
        existing = db.scalar(select(Item).where(Item.item_id == item_id))
        is_new = existing is None
        if is_new:
            existing = Item(item_id=item_id)
            db.add(existing)
            added += 1
        else:
            updated += 1
        item_name = (row.get("item") or "").strip()
        if item_name or is_new:
            existing.item = item_name
        aliases = _split_aliases(row.get("aliases") or "")
        if aliases or is_new:
            existing.aliases = aliases
        for field in ITEM_TEXT_FIELDS:
            val = (row.get(field) or "").strip()
            if val or is_new:
                setattr(existing, field, val)
        on_order = (row.get("on_order") or "").strip()
        if on_order:
            existing.on_order = on_order.lower() in ("yes", "true", "1")
        elif is_new:
            existing.on_order = False
        last_counted = (row.get("last_counted") or "").strip()
        if last_counted or is_new:
            existing.last_counted = last_counted
        existing.active = True

    inactivated = 0
    for it in db.scalars(select(Item).where(Item.active.is_(True))).all():
        if it.item_id not in seen:
            it.active = False
            inactivated += 1
    db.commit()
    log.info("inventory import: %d added, %d updated, %d inactivated", added, updated, inactivated)
    return {"added": added, "updated": updated, "inactivated": inactivated, "total_in_csv": len(seen)}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _tokens(s: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", s.lower())}


def _matches(query_tail: str, it: Item) -> bool:
    """Any shared significant (3+ letter) word between the item/alias name and the
    query is enough - an item name's own descriptive words ("Small", "Heavy Duty")
    don't all need to show up in a casual question."""
    qt = _tokens(query_tail)
    for name in [it.item, *(it.aliases or [])]:
        if not name:
            continue
        nt = {t for t in _tokens(name) if len(t) >= 3}
        if nt & qt:
            return True
    return False


def find_items(db, query: str, include_josa: bool = False) -> list[Item]:
    """Only runs for an explicit "do you have/can I borrow/is there" style question -
    a bare mention of an item name elsewhere in a sentence never triggers this."""
    m = INTENT_RE.search(query)
    if not m:
        return []
    tail = query[m.end():]
    items = db.scalars(select(Item).where(Item.active.is_(True))).all()
    if not include_josa:
        items = [it for it in items if not it.area.lower().startswith("josa")]
    return [it for it in items if _matches(tail, it)]


def search_items(db, q: str, limit: int = 20) -> list[Item]:
    """GA console search: every active item, including Josa's stock and ga_only items -
    unlike find_items, this is never shown to a visitor."""
    qt = {t for t in _tokens(q) if len(t) >= 2}
    if not qt:
        return []
    items = db.scalars(select(Item).where(Item.active.is_(True))).all()
    hits = [it for it in items if _tokens(it.item) & qt or any(_tokens(a) & qt for a in it.aliases or [])]
    return hits[:limit]


STATUS_CHOICES = ("in stock", "low", "out")


def visitor_answer(db, query: str) -> dict | None:
    """None if nothing in inventory matches (falls through to the knowledge base/AI).
    Josa's stock is excluded from the candidate pool entirely - never mentioned, matched
    or not. A ga_only match is answered as if the item isn't stocked at all, so a
    visitor can never tell "really out" apart from "GA-only and we won't say"."""
    matches = find_items(db, query)
    if not matches:
        return None
    visitor_matches = [it for it in matches if it.audience == "visitors"]
    stocked = [it for it in visitor_matches if it.status in ("in stock", "low")]
    if stocked:
        lend = next((it for it in stocked if it.lend_or_give == "lend"), None)
        meta = {"borrow_item_id": lend.item_id, "borrow_item_name": lend.item} if lend else {}
        return {"answer": "Yes, ask the GA at the front desk.", "meta": meta}
    out = [it for it in visitor_matches if it.status == "out"]
    if out:
        msg = "We're out right now." + (" It's on order." if any(it.on_order for it in out) else "")
        return {"answer": msg, "meta": {}}
    return {"answer": NOT_HERE_ANSWER, "meta": {"offer_handoff": True}}
