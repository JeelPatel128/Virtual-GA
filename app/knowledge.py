"""Knowledge base: turn every upload into text, chunk it, embed it, and search it."""
import io
import math
import re
from collections import Counter

import numpy as np
from sqlalchemy import select

from . import llm
from .db import Chunk, Doc

CHUNK_CHARS = 900
OVERLAP = 150

STOPWORDS = set("""a an the is are was were be been to of in on at for and or but if with by from as it its
this that these those i me my we our you your he she they them do does did can could how what when where which
who whom why will would should there here about into over under than then so not no yes please hi hello""".split())


# ---------- extraction ----------

def extract_text(filename: str, data: bytes, mime: str) -> tuple[str, str]:
    """Returns (kind, text) for documents. Images and video are handled separately."""
    name = filename.lower()
    if name.endswith(".pdf") or mime == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "pdf", "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "docx", "\n".join(parts)
    if name.endswith((".txt", ".md", ".csv")) or mime.startswith("text/"):
        return "text", data.decode("utf-8", errors="replace")
    raise ValueError(f"Unsupported file type: {filename}")


def chunk_text(text: str) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > CHUNK_CHARS:  # very long paragraph: hard split
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:CHUNK_CHARS])
            p = p[CHUNK_CHARS - OVERLAP:]
        if len(cur) + len(p) + 2 > CHUNK_CHARS and cur:
            chunks.append(cur)
            cur = cur[-OVERLAP:] + "\n\n" + p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def add_document(db, title: str, kind: str, text: str, added_by: str | None = None,
                 image_bytes: bytes | None = None, mime: str | None = None) -> Doc:
    text = text.strip()
    if not text:
        raise ValueError("Nothing readable was found in that upload.")
    doc = Doc(title=title.strip()[:300], kind=kind, extracted_text=text, image_bytes=image_bytes,
              mime=mime, added_by=added_by)
    db.add(doc)
    db.flush()
    # Prefix every chunk with the title so retrieval knows what it belongs to.
    pieces = chunk_text(text)
    vectors = llm.embed([f"{title}\n{p}" for p in pieces]) or [None] * len(pieces)
    for p, v in zip(pieces, vectors):
        db.add(Chunk(doc_id=doc.id, text=p, embedding=v))
    db.commit()
    return doc


def reembed_missing(db) -> int:
    """Embed chunks that were added before the Gemini key existed."""
    rows = db.execute(select(Chunk, Doc.title).join(Doc).where(Chunk.embedding.is_(None))).all()
    if not rows or not llm.enabled():
        return 0
    vectors = llm.embed([f"{t}\n{c.text}" for c, t in rows])
    if not vectors:
        return 0
    for (c, _), v in zip(rows, vectors):
        c.embedding = v
    db.commit()
    return len(rows)


# ---------- retrieval ----------

def _tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOPWORDS and len(w) > 1]


def search(db, query: str, k: int = 5) -> list[dict]:
    rows = db.execute(select(Chunk, Doc).join(Doc)).all()
    if not rows:
        return []
    results = []
    qv = llm.embed([query], for_query=True)
    if qv and all(c.embedding for c, _ in rows):
        q = np.array(qv[0])
        q = q / (np.linalg.norm(q) or 1)
        for c, d in rows:
            v = np.array(c.embedding)
            score = float(q @ (v / (np.linalg.norm(v) or 1)))
            results.append((score, c, d))
        method = "vector"
    else:
        # Keyword fallback (BM25-lite) so the bot still works without an API key.
        # Score = share of the query's (idf-weighted) words found in the chunk, 0 to 1.
        qt = set(_tokens(query))
        docs_tokens = [set(_tokens(d.title + " " + c.text)) for c, d in rows]
        n = len(rows)
        df = Counter(t for dt in docs_tokens for t in dt)
        idf = {t: math.log(1 + n / (df[t] or 0.5)) for t in qt}
        total = sum(idf.values()) or 1
        for (c, d), dt in zip(rows, docs_tokens):
            results.append((sum(idf[t] for t in qt if t in dt) / total, c, d))
        method = "keyword"
    results.sort(key=lambda r: r[0], reverse=True)
    return [
        {"id": c.id, "doc_id": d.id, "title": d.title, "kind": d.kind, "text": c.text,
         "score": round(s, 3), "method": method}
        for s, c, d in results[:k] if s > 0
    ]
