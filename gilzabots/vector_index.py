"""Vector index for semantic chunk retrieval (used by the bugfix flow).

Chunks every text file in the project into overlapping windows of lines,
embeds them via litellm (Ollama by default, sentence-transformers as fallback),
stores embeddings in SQLite, and retrieves top-K chunks by cosine similarity.

Storage: <project_root>/.gilzabots/vector_index.sqlite
Rebuild: incremental by file mtime — only re-embeds changed files.
"""

import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Chunk(NamedTuple):
    file: str        # relative path from project root
    start: int       # first line (1-based)
    end: int         # last line (1-based, inclusive)
    text: str        # raw content of the chunk


class RankedChunk(NamedTuple):
    chunk: Chunk
    score: float     # cosine similarity [0, 1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              ".mypy_cache", "dist", "build", ".gilzabots"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
               ".ttf", ".eot", ".pdf", ".zip", ".gz", ".bin", ".sqlite",
               ".sqlite-shm", ".sqlite-wal", ".pyc", ".pyo"}


def _iter_project_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS or part.startswith(".") for part in p.parts):
            continue
        if p.suffix.lower() in _BINARY_EXT:
            continue
        yield p


def _make_chunks(rel: str, lines: list[str], chunk_size: int, overlap: int) -> list[Chunk]:
    """Split *lines* into overlapping windows; each window becomes one Chunk."""
    chunks: list[Chunk] = []
    total = len(lines)
    if total == 0:
        return chunks
    step = max(1, chunk_size - overlap)
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        text = "".join(lines[start:end])
        chunks.append(Chunk(file=rel, start=start + 1, end=end, text=text))
        if end == total:
            break
        start += step
    return chunks


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Embedding backends
# ---------------------------------------------------------------------------

def _embed_litellm(texts: list[str], model: str) -> list[list[float]] | None:
    try:
        import litellm
        resp = litellm.embedding(model=model, input=texts)
        return [item["embedding"] for item in resp.data]
    except Exception:
        return None


def _embed_sentence_transformers(texts: list[str], model_name: str) -> list[list[float]] | None:
    # model_name may be "sentence-transformers/all-MiniLM-L6-v2" — strip prefix
    name = model_name.removeprefix("sentence-transformers/")
    try:
        from sentence_transformers import SentenceTransformer
        st = SentenceTransformer(name)
        vecs = st.encode(texts, convert_to_numpy=True)
        return [v.tolist() for v in vecs]
    except Exception:
        return None


def _embed(texts: list[str], primary: str, fallback: str) -> list[list[float]]:
    result = _embed_litellm(texts, primary)
    if result is not None:
        return result
    result = _embed_sentence_transformers(texts, fallback)
    if result is not None:
        return result
    raise RuntimeError(
        f"Both embedding backends failed.\n"
        f"  Primary:  {primary}\n"
        f"  Fallback: {fallback}\n"
        "Ensure Ollama is running or sentence-transformers is installed."
    )


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS file_meta (
    rel_path  TEXT PRIMARY KEY,
    mtime     REAL NOT NULL,
    chunk_size  INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path  TEXT NOT NULL,
    start     INTEGER NOT NULL,
    end       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding TEXT NOT NULL   -- JSON array of floats
);

CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks (rel_path);
"""


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------

class VectorIndex:
    """Manages a project-scoped vector index in SQLite."""

    def __init__(self, project_root: str | Path):
        self._root = Path(project_root)
        db_dir = self._root / ".gilzabots"
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "vector_index.sqlite"
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.executescript(_DDL)
            self._conn.commit()
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Build / rebuild
    # ------------------------------------------------------------------

    def build(
        self,
        embedding_model: str = "ollama/nomic-embed-text",
        embedding_fallback: str = "sentence-transformers/all-MiniLM-L6-v2",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 16,
    ) -> dict:
        """Incrementally embed all project files. Returns stats dict."""
        conn = self._open()
        stats = {"indexed": 0, "skipped": 0, "deleted": 0}

        # Collect current files on disk
        current_files: dict[str, float] = {}
        for p in _iter_project_files(self._root):
            rel = str(p.relative_to(self._root))
            current_files[rel] = p.stat().st_mtime

        # Remove entries for deleted files
        stored_paths = {row[0] for row in conn.execute("SELECT rel_path FROM file_meta")}
        deleted = stored_paths - set(current_files)
        for rel in deleted:
            conn.execute("DELETE FROM chunks WHERE rel_path = ?", (rel,))
            conn.execute("DELETE FROM file_meta WHERE rel_path = ?", (rel,))
            stats["deleted"] += 1

        # Find files that need re-embedding
        to_embed: list[tuple[str, Path]] = []
        for rel, mtime in current_files.items():
            row = conn.execute(
                "SELECT mtime, chunk_size, chunk_overlap FROM file_meta WHERE rel_path = ?", (rel,)
            ).fetchone()
            if (row is None
                    or abs(row[0] - mtime) > 0.01
                    or row[1] != chunk_size
                    or row[2] != chunk_overlap):
                to_embed.append((rel, self._root / rel))
            else:
                stats["skipped"] += 1

        if not to_embed:
            conn.commit()
            return stats

        # Build chunks for all files needing embedding
        all_chunks: list[tuple[str, Chunk]] = []  # (rel, chunk)
        for rel, path in to_embed:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except Exception:
                stats["skipped"] += 1
                continue
            for chunk in _make_chunks(rel, lines, chunk_size, chunk_overlap):
                all_chunks.append((rel, chunk))

        # Embed in batches
        texts = [c.text for _, c in all_chunks]
        embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            vecs = _embed(batch, embedding_model, embedding_fallback)
            embeddings.extend(vecs)

        # Delete old chunks for re-indexed files and insert new ones
        updated_rels = {rel for rel, _ in to_embed}
        for rel in updated_rels:
            conn.execute("DELETE FROM chunks WHERE rel_path = ?", (rel,))
            conn.execute("DELETE FROM file_meta WHERE rel_path = ?", (rel,))

        for (rel, chunk), vec in zip(all_chunks, embeddings):
            conn.execute(
                "INSERT INTO chunks (rel_path, start, end, text, embedding) VALUES (?,?,?,?,?)",
                (rel, chunk.start, chunk.end, chunk.text, json.dumps(vec)),
            )

        for rel in updated_rels:
            mtime = current_files[rel]
            conn.execute(
                "INSERT INTO file_meta (rel_path, mtime, chunk_size, chunk_overlap) VALUES (?,?,?,?)",
                (rel, mtime, chunk_size, chunk_overlap),
            )
            stats["indexed"] += 1

        conn.commit()
        return stats

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        embedding_model: str = "ollama/nomic-embed-text",
        embedding_fallback: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> list[RankedChunk]:
        """Return top-K chunks most similar to *query*."""
        conn = self._open()

        query_vec = _embed([query], embedding_model, embedding_fallback)[0]

        rows = conn.execute(
            "SELECT rel_path, start, end, text, embedding FROM chunks"
        ).fetchall()

        scored: list[RankedChunk] = []
        for rel, start, end, text, emb_json in rows:
            vec = json.loads(emb_json)
            score = _cosine(query_vec, vec)
            scored.append(RankedChunk(
                chunk=Chunk(file=rel, start=start, end=end, text=text),
                score=score,
            ))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------
    # Convenience: format chunks for prompt injection
    # ------------------------------------------------------------------

    def format_for_prompt(self, ranked: list[RankedChunk], max_bytes: int = 8000) -> str:
        parts: list[str] = []
        total = 0
        for r in ranked:
            header = f"### {r.chunk.file} (lines {r.chunk.start}–{r.chunk.end}, score={r.score:.3f})\n"
            body = f"```\n{r.chunk.text}\n```"
            block = header + body
            if total + len(block) > max_bytes:
                parts.append("...(remaining chunks omitted — context budget reached)")
                break
            parts.append(block)
            total += len(block)
        return "\n\n".join(parts) if parts else "(no relevant chunks found)"


# ---------------------------------------------------------------------------
# Module-level singleton (one per project, set via set_project)
# ---------------------------------------------------------------------------

VECTOR_INDEX: VectorIndex | None = None


def set_vector_project(root: str | Path) -> VectorIndex:
    global VECTOR_INDEX
    VECTOR_INDEX = VectorIndex(root)
    return VECTOR_INDEX


def get_vector_index() -> VectorIndex | None:
    return VECTOR_INDEX
