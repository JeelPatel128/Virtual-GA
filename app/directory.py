"""SSB directory + room lookups: exact/fuzzy name & alias match, role match, and place
match. Pure Python (no LLM call), so this keeps working with zero Gemini key, per the rule
that it "runs before the AI". CSV import is an upsert keyed by name/place: a blank CSV cell
never overwrites an existing non-blank value (protects admin edits and approved emails), and
a row missing from a new CSV is marked inactive, never deleted (so session memory and
pending email approvals don't go stale)."""
import csv
import io
import logging

from sqlalchemy import func, select

from .db import Person, Room

log = logging.getLogger("virtual_ga.directory")

PERSON_TEXT_FIELDS = ["title", "type", "building", "room", "floor", "directions", "office_hours",
                      "extension", "email", "roles", "last_verified", "review_notes"]


def _split_aliases(raw: str) -> list[str]:
    return [a.strip() for a in raw.split("|") if a.strip()]


def import_people_csv(db, csv_text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    seen: set[str] = set()
    added = updated = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        seen.add(key)
        existing = db.scalar(select(Person).where(func.lower(Person.name) == key))
        is_new = existing is None
        if is_new:
            existing = Person(name=name)
            db.add(existing)
            added += 1
        else:
            updated += 1
        existing.name = name

        first_name = (row.get("first_name") or "").strip()
        if first_name or is_new:
            existing.first_name = first_name
        last_name = (row.get("last_name") or "").strip()
        if last_name or is_new:
            existing.last_name = last_name
        aliases = _split_aliases(row.get("aliases") or "")
        if aliases or is_new:
            existing.aliases = aliases
        for field in PERSON_TEXT_FIELDS:
            val = (row.get(field) or "").strip()
            if val or is_new:
                setattr(existing, field, val)

        route = (row.get("route_topics_here") or "").strip()
        if route:
            existing.route_topics_here = route.lower() not in ("no", "false", "0")
        elif is_new:
            existing.route_topics_here = True
        publicly = (row.get("show_publicly") or "").strip()
        if publicly:
            existing.show_publicly = publicly.lower() not in ("no", "false", "0")
        elif is_new:
            existing.show_publicly = True

        existing.active = True

    inactivated = 0
    for p in db.scalars(select(Person).where(Person.active.is_(True))).all():
        if p.name.lower() not in seen:
            p.active = False
            inactivated += 1
    db.commit()
    log.info("directory import: %d added, %d updated, %d inactivated", added, updated, inactivated)
    return {"added": added, "updated": updated, "inactivated": inactivated, "total_in_csv": len(seen)}


def import_rooms_csv(db, csv_text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    seen: set[str] = set()
    added = updated = 0
    for row in rows:
        place = (row.get("place") or "").strip()
        if not place:
            continue
        key = place.lower()
        seen.add(key)
        existing = db.scalar(select(Room).where(func.lower(Room.place) == key))
        is_new = existing is None
        if is_new:
            existing = Room(place=place)
            db.add(existing)
            added += 1
        else:
            updated += 1
        existing.place = place
        directions = (row.get("directions_from_front_desk") or "").strip()
        if directions or is_new:
            existing.directions_from_front_desk = directions
        existing.active = True

    inactivated = 0
    for r in db.scalars(select(Room).where(Room.active.is_(True))).all():
        if r.place.lower() not in seen:
            r.active = False
            inactivated += 1
    db.commit()
    log.info("rooms import: %d added, %d updated, %d inactivated", added, updated, inactivated)
    return {"added": added, "updated": updated, "inactivated": inactivated, "total_in_csv": len(seen)}
