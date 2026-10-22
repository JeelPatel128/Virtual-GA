"""SSB directory + room lookups: exact/fuzzy name & alias match, role match, and place
match. Pure Python (no LLM call), so this keeps working with zero Gemini key, per the rule
that it "runs before the AI". CSV import is an upsert keyed by name/place: a blank CSV cell
never overwrites an existing non-blank value (protects admin edits and approved emails), and
a row missing from a new CSV is marked inactive, never deleted (so session memory and
pending email approvals don't go stale)."""
import csv
import io
import logging
import re
from difflib import SequenceMatcher

from sqlalchemy import func, select

from .db import Person, Room, kv_get, kv_set

log = logging.getLogger("virtual_ga.directory")

NAME_LOW = 0.55    # floor to even consider a fuzzy name/alias candidate
NAME_HIGH = 0.80   # bar for a confident single match
TIE_MARGIN = 0.08  # candidates within this of the top score are offered as "did you mean"

# Filler words stripped before n-gramming a query for NAME matching, so a real sentence
# ("is professor chen in today") reduces to its content words ("professor chen") instead of
# diluting the fuzzy ratio against clean alias strings.
NAME_FILLER_WORDS = set("""is are was were the a an in on at for to of do does did i me my you your
he she they them where who what when why how find looking please can could would should will
now today here there around available right office room extension email phone number hours
whats""".split())

TITLE_WORD_RE = re.compile(r"\b(professor|prof|dr|doctor|dean|mr|ms|mrs)\b", re.I)
TITLE_WORDS = ["professor", "prof", "dr", "doctor", "dean", "mr", "ms", "mrs"]
EXTENSION_RE = re.compile(r"\b(extension|ext\.?|phone\s*number|office\s*number)\b", re.I)
EMAIL_RE = re.compile(r"\bemail\b", re.I)


def get_show_extensions(db) -> bool:
    return (kv_get(db, "show_extensions") or "").strip().lower() in ("1", "true", "yes", "on")


def set_show_extensions(db, value: bool) -> None:
    kv_set(db, "show_extensions", "true" if value else "false")
    db.commit()


def _is_title_word(w: str) -> bool:
    return any(SequenceMatcher(None, w, tw).ratio() >= 0.8 for tw in TITLE_WORDS)

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


# ---------- matching ----------

def _norm(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def _core_words(text: str) -> list[str]:
    """Strips filler words, title words (professor/dr/dean/...), and stray single
    characters (e.g. the leftover "s" from "where's"). Title words are stripped - not just
    detected - from BOTH the query and candidate strings before scoring, because a shared
    title word (e.g. "professor") otherwise inflates the character-ratio score for two
    completely different surnames ("Professor Nguyen" vs "Professor Chen" scored 0.80
    on the raw strings - high enough to falsely trigger disambiguation - verified, then
    fixed, during testing)."""
    return [w for w in _norm(text) if len(w) > 1 and w not in NAME_FILLER_WORDS and not _is_title_word(w)]


def _ngrams(words: list[str], max_n: int = 4):
    """Longest n-grams first, so a more specific phrase wins before falling back to a
    short, more ambiguous one."""
    for n in range(min(max_n, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            yield " ".join(words[i:i + n])


def _candidate_strings(p: Person) -> list[str]:
    out = [p.name.lower()]
    out += [a.lower() for a in (p.aliases or [])]
    if p.first_name:
        out.append(p.first_name.lower())
    if p.last_name:
        out.append(p.last_name.lower())
    return out


def _name_candidates(db, text: str) -> list[tuple["Person", float, str]]:
    words = _core_words(text)
    if not words:
        return []
    people = db.scalars(select(Person).where(Person.active.is_(True))).all()
    if not people:
        return []
    for phrase in _ngrams(words):
        hits = []
        for p in people:
            best, best_str = 0.0, ""
            for cand in _candidate_strings(p):
                cand_core = " ".join(_core_words(cand))
                if not cand_core:
                    continue
                score = 1.0 if cand_core == phrase else SequenceMatcher(None, phrase, cand_core).ratio()
                if score > best:
                    best, best_str = score, cand
            if best >= NAME_LOW:
                hits.append((p, best, best_str))
        if hits:
            log.info("directory.resolve name query=%r phrase=%r candidates=%s", text, phrase,
                     [(h[0].name, round(h[1], 3)) for h in hits])
            return sorted(hits, key=lambda h: h[1], reverse=True)
    return []


def _looks_person_ish(text: str) -> bool:
    """Minimal structural signal for now; refined with the common-word gate later."""
    return bool(TITLE_WORD_RE.search(text))


def _not_found_answer() -> dict:
    return {"answer": "I couldn't find anyone by that name. Want me to get a GA to help?",
            "meta": {"offer_handoff": True}}


def _format_person_answer(p: "Person", text: str, show_extensions: bool) -> dict:
    meta: dict = {"offer_handoff": False}
    lead = f"{p.name}, {p.title}."
    if p.building == "Babbio Center" and p.directions:
        answer = f"{lead} Room {p.room}, floor {p.floor}.\n{p.directions}"
        if p.floor == "4":
            meta["images"] = ["/api/directory/floor-map"]
    elif p.building == "Babbio Center":
        answer = f"{lead} Room {p.room} on floor {p.floor} of the Babbio Center. A GA can point you there."
        meta["offer_handoff"] = True
    elif p.building == "Altorfer":
        answer = f"{lead} Room {p.room} in the Altorfer building."
    elif p.building == "Remote":
        answer = f"{lead} They work remotely; email is the best way to reach them."
    else:
        answer = lead

    if EXTENSION_RE.search(text):
        if show_extensions and p.extension:
            answer += f" Extension: {p.extension}."
        else:
            answer += " I can't share extensions here, but a GA can look it up for you."
            meta["offer_handoff"] = True
    if EMAIL_RE.search(text):
        if p.email:
            answer += f" Email: {p.email}."
        else:
            answer += " I don't have an email on file for them; a GA can help."
            meta["offer_handoff"] = True
    return {"answer": answer, "meta": meta}


def resolve(db, sess, text: str) -> dict | None:
    """Returns an answer dict ({"answer", "meta"}), or None if this doesn't look like a
    person question at all, in which case the caller should fall through to the AI/knowledge
    base. Never invents a person: zero credible candidates with a person-ish signal present
    means "couldn't find them, offer a GA" rather than silently falling through."""
    candidates = _name_candidates(db, text)
    strong = [c for c in candidates if c[1] >= NAME_HIGH]
    if not strong:
        # Weak/no candidates: only treat this as a person question (and refuse to silently
        # fall through) if the text actually looks person-ish. Otherwise a merely coincidental
        # weak fuzzy overlap (e.g. a long sentence sharing a few letters with someone's name)
        # must not hijack an unrelated question into a false "couldn't find them" response.
        return _not_found_answer() if _looks_person_ish(text) else None

    top_score = strong[0][1]
    tied = [c for c in strong if top_score - c[1] <= TIE_MARGIN]
    unique = {c[0].id: c[0] for c in tied}
    if len(unique) > 1:
        sess.pending_query = text
        sess.pending_choices = list(unique.keys())
        choices = [{"id": p.id, "label": f"{p.name} ({p.title})"} for p in unique.values()]
        names = ", ".join(c["label"] for c in choices)
        return {"answer": f"Did you mean {names}? Tap one below.", "meta": {"choices": choices}}

    person = next(iter(unique.values()))
    sess.last_person_id = person.id
    return _format_person_answer(person, text, get_show_extensions(db))


def resolve_choice(db, sess, person_id: int) -> dict:
    """Handles a visitor tapping one of the "Did you mean ...?" buttons."""
    offered = sess.pending_choices or []
    query = sess.pending_query or ""
    sess.pending_query = None
    sess.pending_choices = None
    if person_id not in offered:
        return {"answer": "That wasn't one of the options I offered. Could you ask again?", "meta": {}}
    person = db.get(Person, person_id)
    if person is None or not person.active:
        return _not_found_answer()
    sess.last_person_id = person.id
    return _format_person_answer(person, query, get_show_extensions(db))
