"""Archival memory (ChromaDB) embedding setup and result mapping."""

import importlib

import pytest

chromadb = pytest.importorskip("chromadb")

from gilzabots import archival  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_archival(tmp_path, monkeypatch):
    """Point the store at a temp home and reset both module singletons."""
    monkeypatch.setenv("GILZABOTS_HOME", str(tmp_path))
    importlib.reload(archival)
    yield
    archival._chroma_client = None
    archival._embedding_function = None


def test_the_embedding_function_keeps_chromas_default_identity(tmp_path):
    """`name()` must stay "default" or every existing collection fails to open.

    Chroma persists the embedding function's name in the collection config and
    refuses a get with a different one ("embedding function conflict"). Tuning
    the ONNX session by subclassing `ONNXMiniLM_L6_V2` changes that name to
    `onnx_mini_lm_l6_v2`, which would break archival memory for every project
    whose collection already exists.
    """
    embedding_function = archival._get_embedding_function()
    if embedding_function is None:
        pytest.skip("ONNX embedding backend unavailable")

    assert embedding_function.name() == "default"


def test_a_collection_created_with_chromas_default_still_opens(tmp_path):
    """The real upgrade path: a collection written before this change."""
    if archival._get_embedding_function() is None:
        pytest.skip("ONNX embedding backend unavailable")

    db_path = tmp_path / "chroma"
    db_path.mkdir(parents=True, exist_ok=True)
    legacy_client = chromadb.PersistentClient(path=str(db_path))
    legacy_client.get_or_create_collection(name="proj").add(
        documents=["written by the previous version"], ids=["old-1"]
    )
    del legacy_client

    collection = archival.get_collection("proj")

    assert collection.count() == 1


def test_the_onnx_session_is_reused_across_calls(tmp_path):
    """Chroma's default rebuilds the whole InferenceSession on every call.

    Each rebuild re-emits the confinement warnings and repays model load cost,
    so the session is created once per process and reused.
    """
    embedding_function = archival._get_embedding_function()
    if embedding_function is None:
        pytest.skip("ONNX embedding backend unavailable")

    first = embedding_function._model.model
    second = embedding_function._model.model

    assert first is second
    assert first.get_session_options().intra_op_num_threads == archival._onnx_intra_op_threads()


def test_chroma_embeds_through_the_thread_bounded_session(tmp_path, monkeypatch):
    """Chroma must actually use our session for add and query, not its own.

    `Collection._embed` ignores any `DefaultEmbeddingFunction` instance and
    embeds with a fresh default rebuilt from the collection configuration. A
    wrapper subclassing it was silently bypassed, so the Snap kept logging the
    affinity errors. Every session created while embedding must carry the
    explicit thread count, and only one may be created for the process.
    """
    if archival._get_embedding_function() is None:
        pytest.skip("ONNX embedding backend unavailable")

    import onnxruntime as ort

    thread_counts = []
    original_init = ort.InferenceSession.__init__

    def recording_init(self, path, sess_options=None, *args, **kwargs):
        options = sess_options if sess_options is not None else kwargs.get("sess_options")
        thread_counts.append(None if options is None else options.intra_op_num_threads)
        return original_init(self, path, sess_options, *args, **kwargs)

    monkeypatch.setattr(ort.InferenceSession, "__init__", recording_init)

    db_path = tmp_path / "chroma"
    db_path.mkdir(parents=True, exist_ok=True)
    chromadb.PersistentClient(path=str(db_path)).get_or_create_collection(name="legacy")
    thread_counts.clear()

    for project in ("legacy", "fresh"):
        archival.append_to_archival(project, f"{project}-1", "user", "first", "2026-09-13")
        archival.append_to_archival(project, f"{project}-2", "user", "second", "2026-09-13")
        assert archival.search_archival(project, "first", limit=1)

    assert thread_counts == [archival._onnx_intra_op_threads()]


def test_an_explicit_thread_count_is_requested(tmp_path, monkeypatch):
    """ONNX Runtime only skips `pthread_setaffinity_np` when told the count.

    Leaving it unset is what produced one "Operation not permitted" error line
    per thread inside the Snap.
    """
    monkeypatch.setenv("GILZABOTS_EMBEDDING_THREADS", "2")
    assert archival._onnx_intra_op_threads() == 2

    monkeypatch.setenv("GILZABOTS_EMBEDDING_THREADS", "not-a-number")
    assert archival._onnx_intra_op_threads() >= 1

    # 0 is the documented escape hatch back to Chroma's untuned default.
    monkeypatch.setenv("GILZABOTS_EMBEDDING_THREADS", "0")
    archival._embedding_function = None
    assert archival._get_embedding_function() is None


def test_search_maps_a_row_stored_without_metadata(tmp_path):
    """One metadata-less row must not empty the whole search.

    Chroma reports a null entry for such a row; reading it as a dict raised
    AttributeError, and the broad handler turned that into an empty result for
    every hit instead of one row with unknown provenance.
    """
    if archival._get_embedding_function() is None:
        pytest.skip("ONNX embedding backend unavailable")

    archival.get_collection("proj").add(
        documents=["stored without metadata"], ids=["bare-1"]
    )
    archival.append_to_archival(
        "proj", "new-1", "user", "stored with metadata", "2026-09-13"
    )

    results = archival.search_archival("proj", "stored", limit=5)

    assert {r["content"] for r in results} == {
        "stored without metadata", "stored with metadata",
    }
    roles = {r["content"]: r["role"] for r in results}
    assert roles["stored with metadata"] == "user"
    assert roles["stored without metadata"] == "unknown"
