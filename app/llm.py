"""Thin wrapper over the Gemini API. Every function degrades gracefully when no key is set,
so the app still runs (with keyword search) while you are setting things up."""
import json
import logging
import time

from .config import settings

log = logging.getLogger("virtual_ga.llm")

_client = None


def enabled() -> bool:
    return bool(settings.GEMINI_API_KEY)


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# ---------- embeddings ----------

def embed(texts: list[str], for_query: bool = False) -> list[list[float]] | None:
    if not enabled() or not texts:
        return None
    from google.genai import types
    out: list[list[float]] = []
    try:
        for i in range(0, len(texts), 90):  # API batch limit is 100
            resp = client().models.embed_content(
                model=settings.GEMINI_EMBED_MODEL,
                contents=texts[i:i + 90],
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY" if for_query else "RETRIEVAL_DOCUMENT",
                    output_dimensionality=768,
                ),
            )
            out.extend(e.values for e in resp.embeddings)
        return out
    except Exception as e:  # quota, network, model rename
        log.warning("Embedding failed, falling back to keyword search: %s", e)
        return None


# ---------- answering ----------

SYSTEM_PROMPT = """You are the Virtual GA at {desk} at Stevens Institute of Technology.
Students, faculty, staff and visitors message you when no graduate assistant is at the desk.

Rules:
- Answer ONLY from the CONTEXT below. If the context does not clearly answer the question,
  say you are not sure and set "confident" to false. Never guess dates, room numbers, names,
  deadlines, fees or policies.
- Keep answers short and friendly: 1 to 4 sentences, plain language, no markdown headings.
- Greetings and thanks need no context: reply briefly and set "confident" to true.
- Never ask for or discuss grades, student ID numbers, financial or medical details.
  For anything about a specific person's record, say a GA or staff member needs to help.
- If someone needs a human, or is upset, set "confident" to false.

Respond with JSON only: {{"answer": "...", "confident": true|false, "source_ids": [chunk ids you used]}}

CONTEXT:
{context}
"""


def answer(question: str, context_chunks: list[dict], history: list[dict]) -> dict | None:
    """Returns {"answer", "confident", "source_ids"} or None if the model is unavailable."""
    if not enabled():
        return None
    from google.genai import types
    context = "\n\n".join(f"[chunk {c['id']}] ({c['title']})\n{c['text']}" for c in context_chunks) or "(empty)"
    convo = "\n".join(f"{h['sender'].upper()}: {h['text']}" for h in history[-6:])
    prompt = f"Recent conversation:\n{convo}\n\nNew question: {question}" if convo else question
    try:
        resp = client().models.generate_content(
            model=settings.GEMINI_CHAT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT.format(desk=settings.FRONT_DESK_LOCATION, context=context),
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        data = json.loads(resp.text)
        return {
            "answer": str(data.get("answer", "")).strip(),
            "confident": bool(data.get("confident", False)),
            "source_ids": [int(x) for x in data.get("source_ids", []) if str(x).isdigit()],
        }
    except Exception as e:
        log.warning("Gemini answer failed: %s", e)
        return None


# ---------- media understanding (used when GAs upload images / video) ----------

IMAGE_PROMPT = """This image was uploaded to a university front desk knowledge base.
Write out ALL readable text exactly (OCR), then describe what the image shows and what
questions it could answer (e.g. a map: which rooms are where; a flyer: event, date, place).
Plain text, no markdown."""

VIDEO_PROMPT = """This video was uploaded to a university front desk knowledge base.
Give (1) a full transcript of what is said, with rough [mm:ss] timestamps, and
(2) a short summary of anything shown on screen that is useful (directions, steps, text).
Plain text, no markdown."""


def describe_image(data: bytes, mime: str) -> str:
    if not enabled():
        raise RuntimeError("Image upload needs GEMINI_API_KEY (or add the text yourself as a note).")
    from google.genai import types
    resp = client().models.generate_content(
        model=settings.GEMINI_CHAT_MODEL,
        contents=[types.Part.from_bytes(data=data, mime_type=mime), IMAGE_PROMPT],
    )
    return resp.text.strip()


def transcribe_video(path: str, mime: str) -> str:
    if not enabled():
        raise RuntimeError("Video upload needs GEMINI_API_KEY.")
    f = client().files.upload(file=path, config={"mime_type": mime})
    for _ in range(120):  # wait up to ~10 min for processing
        f = client().files.get(name=f.name)
        if f.state.name == "ACTIVE":
            break
        if f.state.name == "FAILED":
            raise RuntimeError("Gemini could not process this video.")
        time.sleep(5)
    try:
        resp = client().models.generate_content(model=settings.GEMINI_CHAT_MODEL, contents=[f, VIDEO_PROMPT])
        return resp.text.strip()
    finally:
        try:
            client().files.delete(name=f.name)
        except Exception:
            pass
