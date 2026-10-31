"""A department's clearance floor is read by the collection its name keys
(2026-10-31).

department_min_level compared the raw spelling: with "sales" listed at the
Member floor, an account stored in "Sales" - the same collection
(database._collection_name lowercases and sanitizes) - read the Owner-only
default, so the chat dropped the account's own shared department from its
retrieval while the KB write check, which compares collections, let it write
there. Fail-closed, but two answers for one department.

FIX: the floor is looked up by the sanitized name the collection is keyed
by, the one derivation (rag_config.department_key, which _collection_name
now calls); two listed spellings of one collection take the higher floor.
CONTROL: a spelling that keys another collection (" general", "sales team")
is still its own department, unlisted and Owner-only.
"""
import pytest

from app import rag_config
from app.rag_config import department_min_level


@pytest.fixture
def shared_sales(monkeypatch):
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "sales", 1)


@pytest.mark.parametrize("spelling", ["sales", "Sales", "SALES"])
def test_every_spelling_of_a_listed_department_reads_its_floor(shared_sales, spelling):
    assert department_min_level(spelling) == 1


@pytest.mark.parametrize("spelling", ["General", "GENERAL"])
def test_every_spelling_of_general_is_the_shared_floor(spelling):
    assert department_min_level(spelling) == rag_config.DEPARTMENT_MIN_LEVEL["general"]


@pytest.mark.parametrize("spelling", [" general", "sales team", "sales-team", "Marketing"])
def test_a_spelling_of_another_collection_stays_unlisted(shared_sales, spelling):
    assert department_min_level(spelling) == rag_config.DEPARTMENT_DEFAULT_MIN_LEVEL


def test_two_listed_spellings_of_one_collection_take_the_higher_floor(monkeypatch):
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "Finance Team", 1)
    monkeypatch.setitem(rag_config.DEPARTMENT_MIN_LEVEL, "finance_team", 2)
    assert department_min_level("finance team") == 2


def test_the_key_is_the_collections_own_derivation():
    from app.database import GLOBAL_COLLECTION, _collection_name
    for spelling in ("sales", "Sales", "Finance Team", "a.b/c", " general", "General", "x-y_z"):
        key = rag_config.department_key(spelling)
        expected = GLOBAL_COLLECTION if key == "general" else f"kb_{key}"
        assert _collection_name(spelling) == expected, spelling


def test_the_chat_retrieval_keeps_an_accounts_own_department_in_any_spelling(
        shared_sales, monkeypatch):
    """An Admin stored in "Sales" reads its shared department."""
    from app import rerank
    asked = []

    def _query(query, n_results=0, department=None, only_department=False):
        asked.append(department)
        return []

    monkeypatch.setattr("app.database.query_similar", _query)
    rerank.retrieve("what is in the pipeline", department="Sales", user_level=2)
    assert asked and "Sales" in (asked[0] or []), asked
