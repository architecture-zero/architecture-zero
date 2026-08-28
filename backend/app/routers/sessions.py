"""Chat sessions, their metadata, feedback, and the analytics rollup.

Fifth router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

upsert_session_meta and get_session_meta read like they belong here and do not
belong only here: the chat handler calls both, so main keeps its own import of
them from app.history and this router imports them independently. Neither is
re-exported from the other - two importers of the same module function, which
is the shape that keeps patch targets unambiguous.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.feedback import save_feedback, get_feedback_summary
from app.history import (get_analytics, list_sessions, upsert_session_meta,
                         get_session_meta, delete_session_meta)
from app.jwt_auth import get_current_user, require_permission
from app.logger import log

router = APIRouter()


class FeedbackRequest(BaseModel):
    session_id: str
    turn_index: int
    value: int  # 1 = thumbs up, -1 = thumbs down


@router.post("/api/feedback")
def feedback(request: FeedbackRequest, current_user: dict = Depends(get_current_user)):
    if request.value not in (1, -1):
        raise HTTPException(status_code=400, detail="value must be 1 or -1")
    # Authenticated is not the same as entitled. The session id arrives in the
    # request body, so without this any logged-in caller could rate any other
    # user's turns - not a content leak, but it poisons the aggregate the
    # analytics and eval lanes read as a quality signal. Declared as a
    # parameter rather than in `dependencies=[]` on purpose: the identity has
    # to be IN SCOPE to be checked against.
    from app.history import session_belongs_to
    if not session_belongs_to(request.session_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="No such session for this user")
    save_feedback(request.session_id, request.turn_index, request.value)
    log("feedback", session_id=request.session_id, turn_index=request.turn_index, value=request.value)
    return {"status": "ok"}


@router.get("/api/feedback/summary")
def feedback_summary(current_user: dict = Depends(require_permission("view_analytics"))):
    return get_feedback_summary()


@router.get("/api/analytics")
def analytics(current_user: dict = Depends(require_permission("view_analytics"))):
    return get_analytics()


@router.get("/api/sessions")
def sessions(category: str | None = None, current_user: dict = Depends(require_permission("view_analytics"))):
    # Lists EVERY session (all conversations). Operator-only - route-level
    # guard so it holds even if ENABLE_AUTH is ever flipped off. (Defense in
    # depth; middleware also gates it.) all_users=True is the deliberate
    # operator override to the per-owner scoping.
    all_sessions = list_sessions(all_users=True)
    if category:
        all_sessions = [s for s in all_sessions if s.get("category") == category]
    return {"sessions": all_sessions}


class SessionCreateRequest(BaseModel):
    session_id: str
    name: str | None = None
    category: str = "general"


class SessionUpdateRequest(BaseModel):
    name: str | None = None
    category: str | None = None


@router.post("/api/sessions")
def create_session(request: SessionCreateRequest,
                   current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    upsert_session_meta(request.session_id, name=request.name,
                        category=request.category, user_id=uid)
    return get_session_meta(request.session_id, uid) or {"session_id": request.session_id}


@router.patch("/api/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdateRequest,
                   current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    meta = get_session_meta(session_id, uid)
    if not meta:
        raise HTTPException(status_code=404, detail="Session not found")
    upsert_session_meta(session_id, name=body.name, category=body.category, user_id=uid)
    return get_session_meta(session_id, uid)


@router.delete("/api/sessions/{session_id}")
def remove_session(session_id: str, current_user: dict = Depends(get_current_user)):
    delete_session_meta(session_id, current_user["id"])
    return {"deleted": session_id}
