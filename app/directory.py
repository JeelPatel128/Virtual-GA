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

from .db import EmailCandidate, Person, Room, kv_get, kv_set

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
AVAILABILITY_RE = re.compile(r"\bavailable\b|\b(is|are)\b[\s\w]*\b(in|here|around)\b", re.I)
PRONOUN_RE = re.compile(r"\b(he|she|him|her|his|hers|they|them|their)\b", re.I)

# A visitor introducing THEMSELVES ("I'm Prof. Mario", "this is Sarah Connor", "Dr. Chen
# here") must never run through the person LOOKUP below - "I'm X" looks structurally
# like "is X in" (a title word, a name) but means the opposite thing entirely. A title
# word is a strong enough signal that the name itself doesn't need to be capitalized;
# without one, the name MUST start with a capital letter (a real proper noun), so a
# sentence like "I'm wondering where Mario is" isn't misread as introducing someone
# named "wondering" - "wondering" is lowercase, so the bare pattern below never fires.
_SELF_INTRO_TRIGGER = r"(?:[Ii]'?m|[Ii] am|[Tt]his is|[Mm]y name is)"
_TITLE_ALT = "|".join(TITLE_WORDS)
_NAME_WORD = r"[A-Za-z][A-Za-z'\-]*"
SELF_INTRO_TITLED_RE = re.compile(
    rf"\b{_SELF_INTRO_TRIGGER}\s+((?:{_TITLE_ALT})\.?\s+{_NAME_WORD}(?:\s+{_NAME_WORD})?)\b"
    rf"|\b((?:{_TITLE_ALT})\.?\s+{_NAME_WORD}(?:\s+{_NAME_WORD})?)\s+here\b", re.I)
SELF_INTRO_BARE_RE = re.compile(rf"\b{_SELF_INTRO_TRIGGER}\s+([A-Z][a-zA-Z'\-]*(?:\s+[A-Z][a-zA-Z'\-]*)?)\b")


def extract_self_intro(text: str) -> str | None:
    """Returns the claimed name ("Prof. Mario", "Sarah Connor", ...) if `text` reads as
    a self-introduction, else None. Never verified against the directory here - see
    callers (app/main.py::ask) for how the claimed name is stored and used."""
    m = SELF_INTRO_TITLED_RE.search(text)
    if m:
        return (m.group(1) or m.group(2)).strip()
    m = SELF_INTRO_BARE_RE.search(text)
    return m.group(1).strip() if m else None

# Common English words that are ALSO plausible first/last names in the real directory data.
# Checked every first_name/last_name in the actual ssb_directory.csv against this risk: "an",
# "oh", "ko", "sun", "park", "frank", "kim", "lee", "han", "diamond", "ryan", "wilson", and
# "murphy" are all real names in our data, not hypothetical. A bare single-word match on one
# of these must not be accepted on its own - length alone is too blunt a signal ("Jose", a
# real alias with no English-word meaning, is also 4 characters and must still resolve from
# nothing but itself), so this is an explicit word list, not a length cutoff.
COMMON_WORDS = set("""an oh ko sun park frank kim lee han diamond ryan wilson murphy may
will bill grace hope faith rose june mark victor bud hall young long short field lane stone
brooks bell gray grey white black brown green king page cook baker carter mason potter
taylor smith miller turner walker bond forest ford lake ray rain snow storm sky star moon
summer winter spring autumn holly ivy lily daisy art bear fox wolf""".split())

PERSON_INTENT_PATTERNS = [
    r"\bwhere(?:'s| is)\s+{w}\b",
    r"\bis\s+{w}\s+(?:in|here|around|available)\b",
    r"\bfind\s+{w}\b",
    r"{w}'?s\s*(?:office|room|extension|email)\b",
    r"\blooking for\s+{w}\b",
]


def _is_risky_word(word: str) -> bool:
    return word in COMMON_WORDS


def _has_person_intent_near(text: str, word: str) -> bool:
    """A risky common-word match only counts when the text's own phrasing points at a
    person specifically for THAT word (not just a pattern appearing anywhere in the
    sentence - "oh where is the printer" contains "where is" but not "where is oh", so it
    must not count as support for treating "oh" as a name)."""
    t = text.lower()
    w = re.escape(word)
    return any(re.search(p.format(w=w), t) for p in PERSON_INTENT_PATTERNS)


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


# ---------- email candidates (from tools/find_emails.py, reviewed here before approval) ----------

def import_email_candidates_csv(db, csv_text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    added = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip()
        if not name or not email:
            continue
        db.add(EmailCandidate(name=name, email=email, confidence=(row.get("confidence") or "low").strip(),
                              source_url=(row.get("source_url") or "").strip()))
        added += 1
    db.commit()
    return {"added": added}


def approve_email_candidate(db, candidate_id: int) -> dict:
    cand = db.get(EmailCandidate, candidate_id)
    if not cand:
        raise ValueError("Candidate not found")
    person = db.scalar(select(Person).where(func.lower(Person.name) == cand.name.strip().lower()))
    if not person:
        raise ValueError(f"No directory entry named {cand.name!r} - import the directory first")
    person.email = cand.email
    cand.status = "approved"
    db.commit()
    return {"ok": True, "person_id": person.id}


def reject_email_candidate(db, candidate_id: int) -> dict:
    cand = db.get(EmailCandidate, candidate_id)
    if not cand:
        raise ValueError("Candidate not found")
    cand.status = "rejected"
    db.commit()
    return {"ok": True}


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
    fixed, during testing). Exception: if stripping title words would wipe out the phrase
    entirely, keep them - "the Dean" is a literal alias where "Dean" IS the distinguishing
    word, not decoration (caught when it stopped matching GJ de Vreede at all)."""
    filler_stripped = [w for w in _norm(text) if len(w) > 1 and w not in NAME_FILLER_WORDS]
    title_stripped = [w for w in filler_stripped if not _is_title_word(w)]
    return title_stripped if title_stripped else filler_stripped


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
        if " " not in phrase and _is_risky_word(phrase) and not (
                TITLE_WORD_RE.search(text) or _has_person_intent_near(text, phrase)):
            continue  # a bare common-word name needs title/phrase support, not just itself
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


# Role phrases ("academic advisor (graduate)", "COPA, careers, resume help, recruiters") are
# matched by token recall, not character ratio: ratio("grad advisor", "academic advisor
# (graduate)") = 0.51 loses to ratio("grad advisor", "academic advisor") = 0.71, which would
# make the plain "academic advisor" role win over the graduate-specific one for the exact
# spec test case ("my grad advisor" -> Lisa Burns) - verified, then fixed, during design.
ROLE_SYNONYMS = {"grad": "graduate", "undergrad": "undergraduate"}
ROLE_STOPWORDS = set("""a an the is are was were be been to of in on at for and or but if with by from as it its
this that these those i me my we our you your he she they them do does did can could how what when where which
who whom why will would should there here about into over under than then so not no yes please hi hello
need want get find know help ask tell call reach talk speak open close free busy right now today available
around office room hours extension email phone number person someone anyone""".split())
ROLE_HIGH = 0.75


def _role_tokens(phrase: str) -> set[str]:
    return {ROLE_SYNONYMS.get(w, w) for w in re.findall(r"[a-z]+", phrase.lower())}


def _role_candidates(db, text: str) -> list[tuple["Person", float]]:
    query_tokens = _role_tokens(text) - ROLE_STOPWORDS
    if not query_tokens:
        return []
    # route_topics_here=False people are never surfaced as "the person for this topic" -
    # their name/alias still resolves normally elsewhere, just not via a role query.
    people = db.scalars(select(Person).where(Person.active.is_(True), Person.route_topics_here.is_(True))).all()
    hits = []
    for p in people:
        if not p.roles:
            continue
        best = 0.0
        # ';' separates genuinely distinct roles ("grad programs; undergrad programs");
        # ',' is used WITHIN one role's own description ("PhD program, dissertation forms",
        # "COPA, careers, resume help, recruiters") and must not be split apart, or a query
        # whose tokens land in different comma-separated facets of the same role can't reach
        # a high enough recall against either fragment alone (caught by the "PhD forms" test).
        for phrase in p.roles.split(";"):
            role_tokens = _role_tokens(phrase)
            if not role_tokens:
                continue
            recall = len(query_tokens & role_tokens) / len(query_tokens)
            best = max(best, recall)
        if best >= ROLE_HIGH:
            hits.append((p, best))
    if hits:
        log.info("directory.resolve role query=%r candidates=%s", text,
                 [(p.name, round(s, 3)) for p, s in hits])
    return sorted(hits, key=lambda h: h[1], reverse=True)


PLACE_HIGH = 0.72  # lower bar than names: place phrases are short and more generic
# Deliberately NOT reusing NAME_FILLER_WORDS: "room"/"office" are noise for a person's NAME
# but the actual distinguishing word for a place ("room 414" vs bare "414" wouldn't match
# the "Room 414" candidate string at all once "room" is stripped - caught during testing).
PLACE_FILLER_WORDS = set("where whats what please can could find looking around here there is are the a an s".split())


def _place_candidates(db, text: str) -> list[tuple["Room", float]]:
    words = [w for w in _norm(text) if len(w) > 1 and w not in PLACE_FILLER_WORDS]
    if not words:
        return []
    rooms = db.scalars(select(Room).where(Room.active.is_(True))).all()
    if not rooms:
        return []
    for phrase in _ngrams(words):
        hits = []
        for r in rooms:
            cand = " ".join(w for w in _norm(r.place) if len(w) > 1)
            if not cand:
                continue
            score = 1.0 if cand == phrase else SequenceMatcher(None, phrase, cand).ratio()
            if score >= PLACE_HIGH:
                hits.append((r, score))
        if hits:
            log.info("directory.resolve place query=%r phrase=%r candidates=%s", text, phrase,
                     [(h[0].place, round(h[1], 3)) for h in hits])
            return sorted(hits, key=lambda h: h[1], reverse=True)
    return []


def _format_place_answer(r: "Room") -> dict:
    if r.directions_from_front_desk:
        return {"answer": r.directions_from_front_desk, "meta": {"images": ["/api/directory/floor-map"]}}
    return {"answer": f"I don't have directions to {r.place} yet. A GA can help.",
            "meta": {"offer_handoff": True}}


def _looks_person_ish(text: str) -> bool:
    """Minimal structural signal for now; refined with the common-word gate later."""
    return bool(TITLE_WORD_RE.search(text))


def _disambiguate_or_resolve(db, sess, text: str, people: list["Person"]) -> dict:
    """Shared by name and role matching: a single candidate resolves directly, several
    close candidates become a "did you mean" prompt. Never picks one silently."""
    unique = {p.id: p for p in people}
    if len(unique) > 1:
        sess.pending_query = text
        sess.pending_choices = list(unique.keys())
        choices = [{"id": p.id, "label": f"{p.name} ({p.title})"} for p in unique.values()]
        names = ", ".join(c["label"] for c in choices)
        return {"answer": f"Did you mean {names}? Tap one below.", "meta": {"choices": choices}}
    person = next(iter(unique.values()))
    sess.last_person_id = person.id
    return _format_person_answer(person, text, get_show_extensions(db))


def _not_found_answer() -> dict:
    return {"answer": "I couldn't find anyone by that name. Want me to get a GA to help?",
            "meta": {"offer_handoff": True}}


def _format_person_answer(p: "Person", text: str, show_extensions: bool) -> dict:
    if AVAILABILITY_RE.search(text) and not EXTENSION_RE.search(text) and not EMAIL_RE.search(text):
        # Never claim someone is in right now - offer to check or leave a message instead.
        answer = f"{p.name}: office hours are {p.office_hours}, but they may be in class or a meeting."
        return {"answer": answer,
                "meta": {"availability_buttons": True, "person_id": p.id, "person_name": p.name}}

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


def _location_line(p: "Person") -> str:
    lead = f"{p.name} ({p.title})"
    if p.building == "Babbio Center" and p.directions:
        return f"{lead}: Room {p.room}, floor {p.floor}. {p.directions}"
    if p.building == "Babbio Center":
        return f"{lead}: Room {p.room} on floor {p.floor} of the Babbio Center."
    if p.building == "Altorfer":
        return f"{lead}: Room {p.room} in the Altorfer building."
    if p.building == "Remote":
        return f"{lead}: works remotely."
    return lead


def _format_role_group_answer(people: list["Person"]) -> dict:
    """A role/topic can genuinely belong to several people (e.g. "COPA person" -> two real
    staff members) - that's not the same kind of ambiguity as two people sharing a name.
    List everyone with their room/directions in one answer instead of forcing a pick."""
    answer = "\n".join(_location_line(p) for p in people)
    meta: dict = {}
    if any(p.building == "Babbio Center" and p.floor == "4" and p.directions for p in people):
        meta["images"] = ["/api/directory/floor-map"]
    return {"answer": answer, "meta": meta}


def search_people(db, q: str, limit: int = 10) -> list["Person"]:
    """Plain search for the GA console: no visitor-facing gating (a GA isn't a stranger
    being told about someone - they can see any reasonable match, always with extension
    and email, regardless of the show_extensions setting)."""
    words = _core_words(q)
    if not words:
        return []
    phrase = " ".join(words)
    role_q = _role_tokens(q) - ROLE_STOPWORDS
    people = db.scalars(select(Person).where(Person.active.is_(True))).all()
    scored = []
    for p in people:
        best = 0.0
        for cand in _candidate_strings(p):
            cand_core = " ".join(_core_words(cand)) or cand
            best = max(best, 1.0 if cand_core == phrase else SequenceMatcher(None, phrase, cand_core).ratio())
        if role_q and p.roles:
            for rp in p.roles.split(";"):
                rt = _role_tokens(rp)
                if rt:
                    best = max(best, len(role_q & rt) / len(role_q))
        if best >= 0.5:
            scored.append((best, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:limit]]


def match_person(db, text: str) -> "Person | None":
    """Best single confident directory match for free text (the same fuzzy matching
    resolve() uses), or None. Used by app/pickups.py to check whether a visitor's
    claimed name resolves to the same Person a pickup's recipient was set from - never
    used to decide what to tell a visitor about someone, only to compare two names."""
    hits = _name_candidates(db, text)
    strong = [c for c in hits if c[1] >= NAME_HIGH]
    return strong[0][0] if strong else None


def resolve(db, sess, text: str) -> dict | None:
    """Returns an answer dict ({"answer", "meta"}), or None if this doesn't look like a
    person question at all, in which case the caller should fall through to the AI/knowledge
    base. Never invents a person: zero credible candidates with a person-ish signal present
    means "couldn't find them, offer a GA" rather than silently falling through."""
    # 0. place lookups have their own vocabulary (lounge, washroom, room 4xx, ...) and run
    # independently of the person-ish gating below.
    # Every return path below is tagged with a retention-job topic category (see
    # app/retention.py / QueryLog.category) - "Places" for a room/location lookup,
    # "People & offices" for everything else here (a name, pronoun, or role match).
    place_hits = _place_candidates(db, text)
    if place_hits:
        result = _format_place_answer(place_hits[0][0])
        result["meta"]["category"] = "Places"
        return result

    # 1. exact/fuzzy name or alias match
    name_hits = _name_candidates(db, text)
    strong = [c for c in name_hits if c[1] >= NAME_HIGH]
    if strong:
        top_score = strong[0][1]
        tied = [p for p, score, _ in strong if top_score - score <= TIE_MARGIN]
        result = _disambiguate_or_resolve(db, sess, text, tied)
        result["meta"]["category"] = "People & offices"
        return result

    # 2. a pronoun with no new name/role signal refers back to whoever was last resolved
    # in this session ("is she in?", "what's his extension?"). A re-import can delete-and-
    # reinsert, so a stale remembered id is forgotten rather than crashing.
    if PRONOUN_RE.search(text) and sess.last_person_id:
        person = db.get(Person, sess.last_person_id)
        if person is not None and person.active:
            result = _format_person_answer(person, text, get_show_extensions(db))
            result["meta"]["category"] = "People & offices"
            return result
        sess.last_person_id = None

    # 3. role match ("my grad advisor", "COPA person", "Capstone", "PhD forms", "the Dean").
    # Several people can genuinely share a role - list them all with their location in one
    # answer rather than asking "did you mean?", which is reserved for a NAME query (several
    # people sharing a name, e.g. "Chen") where only one of them is actually who was meant.
    role_hits = _role_candidates(db, text)
    if role_hits:
        top_score = role_hits[0][1]
        tied = {p.id: p for p, score in role_hits if top_score - score <= TIE_MARGIN}
        if len(tied) > 1:
            result = _format_role_group_answer(list(tied.values()))
        else:
            person = next(iter(tied.values()))
            sess.last_person_id = person.id
            result = _format_person_answer(person, text, get_show_extensions(db))
        result["meta"]["category"] = "People & offices"
        return result

    # Nothing credible: only treat this as a person question (and refuse to silently fall
    # through) if the text actually looks person-ish. Otherwise a merely coincidental weak
    # fuzzy overlap (e.g. a long sentence sharing a few letters with someone's name) must
    # not hijack an unrelated question into a false "couldn't find them" response.
    if _looks_person_ish(text):
        result = _not_found_answer()
        result["meta"]["category"] = "People & offices"
        return result
    return None


def resolve_choice(db, sess, person_id: int) -> dict:
    """Handles a visitor tapping one of the "Did you mean ...?" buttons."""
    offered = sess.pending_choices or []
    query = sess.pending_query or ""
    sess.pending_query = None
    sess.pending_choices = None
    if person_id not in offered:
        return {"answer": "That wasn't one of the options I offered. Could you ask again?",
                "meta": {"category": "People & offices"}}
    person = db.get(Person, person_id)
    if person is None or not person.active:
        result = _not_found_answer()
    else:
        sess.last_person_id = person.id
        result = _format_person_answer(person, query, get_show_extensions(db))
    result["meta"]["category"] = "People & offices"
    return result
