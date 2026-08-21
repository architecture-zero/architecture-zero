"""Knowledge sources are keyed by KNOWLEDGE_DIR-relative path.

The lesson pinned here: basename keying collided same-named files in
different subdirs (three README.md's) into one source - silent chunk
overwrite plus an every-boot re-embed. These pin the fix: relative keys, plus
the self-healing purge of legacy basename keys that must NEVER touch a
top-level file that legitimately owns the name.
"""
import app.main as main_mod


def test_subdir_sources_key_by_relative_path_and_purge_guards(tmp_path, monkeypatch):
    kd = tmp_path / "knowledge"
    (kd / "handbook").mkdir(parents=True)
    (kd / "a.md").write_text("top-level a", encoding="utf-8")
    (kd / "handbook" / "a.md").write_text("handbook a", encoding="utf-8")
    (kd / "handbook" / "b.md").write_text("handbook b", encoding="utf-8")

    ingested, purged = [], []
    monkeypatch.setattr(main_mod, "KNOWLEDGE_DIR", str(kd))
    monkeypatch.setattr(main_mod, "_ingest_file",
                        lambda name, text: ingested.append(name) or 1)
    monkeypatch.setattr(main_mod, "delete_source",
                        lambda source, dept=None: purged.append(source))
    monkeypatch.setattr(main_mod, "_load_ingest_state",
                        lambda: {"b.md": "legacy-fingerprint"})
    monkeypatch.setattr(main_mod, "_save_ingest_state", lambda state: None)

    results = main_mod._sync_knowledge_dir(force=True)

    # every file ingests under its KNOWLEDGE_DIR-relative posix key
    assert sorted(ingested) == ["a.md", "handbook/a.md", "handbook/b.md"]
    assert set(results) == {"a.md", "handbook/a.md", "handbook/b.md"}
    # legacy purge: handbook/b.md's old basename key is purged; handbook/a.md's is
    # NOT - the top-level a.md legitimately owns "a.md"
    assert purged == ["b.md"]
