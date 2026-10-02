"""AN ANSWER THAT IS STORED IS REPORTED AS ANSWERED.

The web client keeps a count of stored rows: a bubble stops being ephemeral
only when its stream reaches [DONE] without an error event, and Regenerate
deletes that many rows from the tail by id. So once the assistant row is
saved, nothing after the save may report the turn as failed.

The model lane has said so since the round-4 tail (its audit write sits in a
try that logs and swallows). The RAG refusal lane runs before the generator's
try, and an audit write that raised there escaped the generator after the
headers had gone: the client read the broken stream as "Could not reach the
backend", left the stored refusal off its count, and the next Regenerate
deleted the refusal and stored the question twice. Both lanes are pinned here
with the audit write made to fail, and both rows read back from history.
"""
from unittest.mock import patch

_FAILING_AUDIT = RuntimeError("audit write failed: database is locked")


def _one_word_stream(messages, model, tools=None, system_prompt="", max_tokens=1024):
    yield {"type": "text", "text": "answered"}


def _roles(client, headers, session):
    rows = client.get(f"/api/history/{session}", headers=headers).json()["messages"]
    return [m["role"] for m in rows]


def test_the_refusal_lane_reports_a_stored_refusal_as_answered(client, admin_headers):
    session = "stored-refusal-audit-fails"
    with patch("app.routers.chat.RAG_ONLY_MODE", True), \
         patch("app.rerank.retrieve", return_value=[]), \
         patch("app.routers.chat.ENABLE_AUDIT_LOG", True), \
         patch("app.routers.chat.log_audit_entry", side_effect=_FAILING_AUDIT) as audit:
        r = client.post("/api/chat", json={"prompt": "something the documents do not cover",
                                           "model": "test-model", "session_id": session,
                                           "use_rag": True},
                        headers=admin_headers)

    # The control: the audit write really was attempted, and really failed.
    assert audit.called
    assert r.status_code == 200, r.text
    assert "I can only answer questions" in r.text
    assert "[DONE]" in r.text
    assert '"error"' not in r.text
    assert _roles(client, admin_headers, session) == ["user", "assistant"]


def test_the_model_lane_reports_a_stored_answer_as_answered(client, admin_headers):
    session = "stored-answer-audit-fails"
    with patch("app.routers.chat.stream_chat_events", side_effect=_one_word_stream), \
         patch("app.routers.chat.ENABLE_AUDIT_LOG", True), \
         patch("app.routers.chat.log_audit_entry", side_effect=_FAILING_AUDIT) as audit:
        r = client.post("/api/chat", json={"prompt": "say one word", "model": "test-model",
                                           "session_id": session, "use_rag": False},
                        headers=admin_headers)

    assert audit.called
    assert r.status_code == 200, r.text
    assert "answered" in r.text
    assert "[DONE]" in r.text
    assert '"error"' not in r.text
    assert _roles(client, admin_headers, session) == ["user", "assistant"]
