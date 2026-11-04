import os
from pathlib import Path

# Singleton to avoid loading ChromaDB more than once
_chroma_client = None
_embedding_function = None


def _onnx_intra_op_threads() -> int:
    """Thread count for the embedding session; 0 lets ONNX Runtime decide."""
    raw = os.environ.get("GILZABOTS_EMBEDDING_THREADS", "")
    if raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return min(4, os.cpu_count() or 1)


def _get_embedding_function():
    """Chroma's default embedder, reused across calls and with a fixed thread count.

    Two problems with the stock default, both visible as a burst of
    `[E:onnxruntime:...] pthread_setaffinity_np failed ... Operation not
    permitted` lines inside a sandbox that restricts CPU affinity (a container or
    an AppArmor/seccomp profile):

    1. ONNX Runtime sizes its intra-op thread pool from the visible CPU count and
       then pins each thread with `pthread_setaffinity_np`. Such a sandbox
       denies that syscall, so every session start logs one error line per
       thread. The remedy is the one the runtime names in the message itself --
       set the thread count explicitly, which skips the pinning. `OMP_NUM_THREADS`
       does not reach it: the shipped `libonnxruntime` is not linked against
       OpenMP, so its pool ignores that variable.
    2. `DefaultEmbeddingFunction.__call__` builds a brand new `ONNXMiniLM_L6_V2`
       on every call, so the whole `InferenceSession` is reconstructed per
       embedding batch. That is why the errors appear again on each operation.

    The wrapper implements `EmbeddingFunction` directly and reports `name()` as
    `"default"`. Both halves matter:
    - The name must stay `"default"` or Chroma rejects every collection created
      before this change with an embedding-function conflict on open.
    - It must NOT be an instance of `DefaultEmbeddingFunction`: `Collection._embed`
      discards any such instance and embeds with a fresh default rebuilt from
      the persisted configuration, which reintroduces both problems above.

    Returns None when anything here is unavailable, so archival memory falls back
    to Chroma's untuned default (noisy logs) instead of failing to embed at all.
    """
    global _embedding_function
    if _embedding_function is not None:
        return _embedding_function or None

    threads = _onnx_intra_op_threads()
    if threads == 0:
        _embedding_function = False
        return None

    try:
        from functools import cached_property

        from chromadb.api.types import Documents, EmbeddingFunction
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
            ONNXMiniLM_L6_V2,
        )

        class _ThreadBoundedMiniLM(ONNXMiniLM_L6_V2):
            @cached_property
            def model(self):  # type: ignore[override]
                providers = self._preferred_providers or self.ort.get_available_providers()
                # Mirrors the base class: CoreML is slower than CPU here.
                providers = [p for p in providers if p != "CoreMLExecutionProvider"]
                options = self.ort.SessionOptions()
                options.log_severity_level = 3
                options.graph_optimization_level = (
                    self.ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                )
                options.intra_op_num_threads = threads
                return self.ort.InferenceSession(
                    os.path.join(
                        self.DOWNLOAD_PATH, self.EXTRACTED_FOLDER_NAME, "model.onnx"
                    ),
                    providers=providers,
                    sess_options=options,
                )

        class _ReusedDefaultEmbeddingFunction(EmbeddingFunction[Documents]):
            """Same identity as Chroma's default, one session for the process."""

            def __init__(self) -> None:
                self._model = _ThreadBoundedMiniLM()

            def __call__(self, input):  # noqa: A002 - matches the base signature
                return self._model(input)

            @staticmethod
            def name() -> str:
                return "default"

            def get_config(self):
                return {}

            @staticmethod
            def build_from_config(config):
                return _ReusedDefaultEmbeddingFunction()

        _embedding_function = _ReusedDefaultEmbeddingFunction()
    except Exception:
        _embedding_function = False
        return None

    return _embedding_function

def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        try:
            import chromadb
        except ImportError:
            raise ImportError("Please install chromadb: pip install chromadb")
            
        from .config import get_gilzabots_home
        
        # O banco será salvo no mesmo diretório global do projects.db
        # ex: ~/.gilzabots/chroma
        db_path = Path(get_gilzabots_home()) / "chroma"
        db_path.mkdir(parents=True, exist_ok=True)
        
        _chroma_client = chromadb.PersistentClient(path=str(db_path))
    return _chroma_client

def get_collection(project_name: str):
    client = _get_chroma_client()
    # Chroma collection names must be short and alphanumeric
    safe_name = "".join(c if c.isalnum() else "_" for c in project_name).strip("_")
    if not safe_name:
        safe_name = "default_project"
    embedding_function = _get_embedding_function()
    if embedding_function is None:
        return client.get_or_create_collection(name=safe_name)
    return client.get_or_create_collection(
        name=safe_name, embedding_function=embedding_function
    )

def append_to_archival(project_name: str, message_id: str, role: str, content: str, timestamp: str, chat_id: str = None):
    """
    Adiciona uma mensagem ao Archival Memory (ChromaDB) do projeto.
    """
    if not content or not content.strip():
        return
        
    collection = get_collection(project_name)
    
    metadata = {
        "role": role,
        "timestamp": timestamp
    }
    if chat_id:
        metadata["chat_id"] = chat_id
    
    collection.add(
        documents=[content],
        metadatas=[metadata],
        ids=[f"{message_id}"]
    )

def search_archival(project_name: str, query: str, limit: int = 5, chat_id: str = None) -> list[dict]:
    """
    Pesquisa o histórico usando similaridade de cosseno via ChromaDB.
    """
    try:
        collection = get_collection(project_name)
        kwargs = {
            "query_texts": [query],
            "n_results": limit
        }
        if chat_id:
            kwargs["where"] = {"chat_id": chat_id}
            
        results = collection.query(**kwargs)
        
        out = []
        if results and "documents" in results and results["documents"]:
            docs = results["documents"][0]
            metas = results["metadatas"][0] if "metadatas" in results else []
            metas = metas or []
            for i, doc in enumerate(docs):
                # Chroma reports a null entry for a row stored without metadata.
                # Treating that as a dict raised AttributeError, and the handler
                # below turned one such row into an empty result for the whole
                # search rather than one row with unknown provenance.
                meta = (metas[i] if i < len(metas) else None) or {}
                out.append({
                    "content": doc,
                    "role": meta.get("role", "unknown"),
                    "timestamp": meta.get("timestamp", "")
                })
        return out
    except Exception as e:
        import traceback
        traceback.print_exc()
        return []

def clear_archival(project_name: str):
    """
    Exclui a coleção do ChromaDB associada ao projeto, limpando completamente a memória arquivada.
    """
    try:
        client = _get_chroma_client()
        safe_name = "".join(c if c.isalnum() else "_" for c in project_name).strip("_")
        if not safe_name:
            safe_name = "default_project"
        try:
            client.delete_collection(name=safe_name)
        except ValueError:
            pass
    except Exception as e:
        if "does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            print(f"Error clearing archival memory: {e}")

def clear_archival_chat(project_name: str, chat_id: str):
    """
    Exclui mensagens do ChromaDB de um chat específico.
    """
    try:
        collection = get_collection(project_name)
        collection.delete(where={"chat_id": chat_id})
    except Exception as e:
        pass
