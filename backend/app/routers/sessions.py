"""Chat sessions, their metadata, feedback, and the analytics rollup.

Fifth router out of main.py. Same rules: no prefix, full literal paths, guards
verbatim on the handlers, never `from app.main import ...`.

upsert_session_meta and get_session_meta read like they belong here and do not
belong only here: the chat handler calls both, so main keeps its own import of
them from app.history and this router imports them independently. Neither is
re-exported from the other - two importers of the same module function, which
is the shape that keeps patch targets unambiguous.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.feedback import save_feedback, get_feedback_summary
from app.history import (get_analytics, list_sessions, upsert_session_meta,
                         get_session_meta, delete_session_meta)
from app.jwt_auth import check_permission, get_current_user, require_permission
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


@router.get("/api/sessions/mine")
def my_sessions(category: str | None = None,
                limit: int = Query(200, ge=1, le=1000),
                current_user: dict = Depends(require_permission("view_history"))):
    """The caller's OWN conversations - what a chat sidebar wants.

    This route did not exist, so the reference client's personal "History" list
    was wired to /api/sessions above. That one is view_analytics-gated and
    deliberately all_users=True, which made the sidebar dead for every ordinary
    member (403 - no history at all, their own included) and cross-user for
    operators: another account's conversation listed as your own history, titled
    with ITS FIRST MESSAGE, opening to nothing because /api/history stayed
    correctly owner-scoped.

    view_history is the permission the taxonomy already defines as "see own
    conversation history". It simply had no route serving it.
    """
    # PASS THE LIMIT. list_sessions defaults to 50, and now that "+ New Chat"
    # correctly stops deleting the conversation it leaves, sessions accumulate -
    # so past 50 the older ones fell off the ONLY surface that can reach them.
    # The rows survive and /api/history/{id} would still serve them; the client
    # just had no way left to learn the session id. Bounded rather than
    # unbounded because this is one query feeding one sidebar.
    mine = list_sessions(user_id=current_user["id"], limit=limit)
    if category:
        mine = [s for s in mine if s.get("category") == category]
    return {"sessions": mine}


class SessionCreateRequest(BaseModel):
    session_id: str
    name: str | None = None
    category: str = "general"


class SessionUpdateRequest(BaseModel):
    name: str | None = None
    category: str | None = None


# A conversation's name as the sidebar shows it (2026-10-03, when the client
# began renaming): trimmed, never blank, and within the column's 300
# characters. None still means "leave the name as it is".
_NAME_MAX = 300


def _clean_name(name: str | None) -> str | None:
    if name is None:
        return None
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A conversation name cannot be blank")
    if len(name) > _NAME_MAX:
        raise HTTPException(status_code=400,
                            detail=f"A conversation name is at most {_NAME_MAX} characters")
    return name


# Each asks for the scope the taxonomy names (AZ-03, 2026-10-07): starting a
# conversation is chatting; renaming or removing one is managing your history.

@router.post("/api/sessions")
def create_session(request: SessionCreateRequest,
                   current_user: dict = Depends(require_permission("chat"))):
    uid = current_user["id"]
    # An upsert: on a conversation that already has a row it renames and
    # recategorises - PATCH's capability - so there it asks PATCH's scope too
    # (the 2026-10-07 security read of AZ-03). Starting one stays "chat".
    if get_session_meta(request.session_id, uid) is not None:
        check_permission(current_user, "view_history")
    upsert_session_meta(request.session_id, name=_clean_name(request.name),
                        category=request.category, user_id=uid)
    return get_session_meta(request.session_id, uid) or {"session_id": request.session_id}


@router.patch("/api/sessions/{session_id}")
def update_session(session_id: str, body: SessionUpdateRequest,
                   current_user: dict = Depends(require_permission("view_history"))):
    uid = current_user["id"]
    name = _clean_name(body.name)
    # A conversation the caller OWNS may have no meta row yet - the chat route
    # names a session only on a first turn sent without history - and the
    # sidebar lists every owned conversation, so a rename 404d on some of the
    # rows it offered. Ownership is the messages' owner column, the same proof
    # /api/feedback asks for; another account's id still finds nothing.
    if not get_session_meta(session_id, uid):
        from app.history import session_belongs_to
        if not session_belongs_to(session_id, uid):
            raise HTTPException(status_code=404, detail="Session not found")
    upsert_session_meta(session_id, name=name, category=body.category, user_id=uid)
    return get_session_meta(session_id, uid)


@router.delete("/api/sessions/{session_id}")
def remove_session(session_id: str,
                   current_user: dict = Depends(require_permission("view_history"))):
    delete_session_meta(session_id, current_user["id"])
    return {"deleted": session_id}
