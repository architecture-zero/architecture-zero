from datetime import datetime, date, timedelta
from sqlalchemy import text
from app.db import get_session
from app.models import Message


def init_db():
    pass  # Schema managed by db.init_db()


def _scope(query, session: str, user_id: int | None):
    """Scope a messages query to one session AND one owner. A guest (user_id=None) sees only
    NULL-owner rows; an authenticated user sees only their own - so knowing/guessing another
    user's session id can't read or delete their history."""
    query = query.filter(Message.session == session)
    if user_id is None:
        return query.filter(Message.user_id.is_(None))
    return query.filter(Message.user_id == user_id)


def save_message(session: str, role: str, content: str, model: str = None, user_id: int | None = None):
    with get_session() as db:
        db.add(Message(
            session=session,
            user_id=user_id,
            role=role,
            content=content,
            model=model,
            timestamp=datetime.utcnow().isoformat(),
        ))


def load_history(session: str, user_id: int | None = None) -> list[dict]:
    with get_session() as db:
        rows = _scope(db.query(Message), session, user_id).order_by(Message.id).all()
        return [{"role": r.role, "content": r.content, "model": r.model, "timestamp": r.timestamp} for r in rows]


def clear_session(session: str, user_id: int | None = None):
    with get_session() as db:
        _scope(db.query(Message), session, user_id).delete()


def delete_tail_messages(session_id: str, count: int, user_id: int | None = None) -> None:
    with get_session() as db:
        rows = (
            _scope(db.query(Message.id), session_id, user_id)
            .order_by(Message.id.desc())
            .limit(count)
            .all()
        )
        ids = [r.id for r in rows]
        if ids:
            db.query(Message).filter(Message.id.in_(ids)).delete(synchronize_session=False)


def purge_anonymous_sessions(days: int) -> dict:
    """Guest-chat retention floor: delete anonymous sessions whose newest
    message is older than `days`. Anonymous = every message NULL-owner
    (MAX(user_id) over an all-NULL session is NULL; one authenticated
    message excludes the whole session). Guest rows are already
    content-only - no account, no IP - so aging them out makes "no user
    tracking" also mean "no content hoarding". Feedback rows die with
    their session (delete them FIRST - the session list is derived from
    messages, so it must still exist when feedback is matched)."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    stale = ("SELECT session FROM messages GROUP BY session "
             "HAVING MAX(user_id) IS NULL AND MAX(timestamp) < :cutoff")
    with get_session() as db:
        sessions = db.execute(
            text(f"SELECT COUNT(*) FROM ({stale})"), {"cutoff": cutoff}
        ).scalar() or 0
        if not sessions:
            return {"sessions": 0, "messages": 0, "feedback": 0}
        feedback = db.execute(
            text(f"DELETE FROM feedback WHERE session_id IN ({stale})"),
            {"cutoff": cutoff},
        ).rowcount
        messages = db.execute(
            text(f"DELETE FROM messages WHERE session IN ({stale})"),
            {"cutoff": cutoff},
        ).rowcount
        return {"sessions": sessions, "messages": messages, "feedback": feedback}


def _scope_meta(query, session_id: str, user_id: int | None):
    """Owner-scope a chat_sessions query - same rule as _scope, for the meta
    row (name/category) so one user can't read, rename, or delete another's
    session."""
    from app.models import ChatSession
    query = query.filter(ChatSession.session_id == session_id)
    if user_id is None:
        return query.filter(ChatSession.user_id.is_(None))
    return query.filter(ChatSession.user_id == user_id)


def upsert_session_meta(session_id: str, name: str | None = None,
                        category: str | None = None, user_id: int | None = None):
    """Create or update session metadata. Pass None to leave a field
    unchanged. Update is owner-scoped; a create stamps the owner."""
    from app.models import ChatSession
    now = datetime.utcnow().isoformat()
    with get_session() as db:
        row = _scope_meta(db.query(ChatSession), session_id, user_id).first()
        if row:
            if name is not None:
                row.name = name
            if category is not None:
                row.category = category
            row.updated_at = now
        else:
            db.add(ChatSession(
                session_id=session_id,
                user_id=user_id,
                name=name,
                category=category or "general",
                created_at=now,
                updated_at=now,
            ))


def get_session_meta(session_id: str, user_id: int | None = None) -> dict | None:
    from app.models import ChatSession
    with get_session() as db:
        row = _scope_meta(db.query(ChatSession), session_id, user_id).first()
        if not row:
            return None
        return {
            "session_id": row.session_id,
            "name": row.name,
            "category": row.category,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def delete_session_meta(session_id: str, user_id: int | None = None):
    """Delete session metadata and all messages for a session -
    owner-scoped."""
    from app.models import ChatSession
    with get_session() as db:
        _scope_meta(db.query(ChatSession), session_id, user_id).delete()
    clear_session(session_id, user_id)


def list_sessions(limit: int = 50, user_id: int | None = None,
                  all_users: bool = False) -> list[dict]:
    """List sessions. Owner-scoped by default so one user never sees
    another's (guest = NULL-owner rows only). all_users=True is the operator
    view (every session, all owners) for the analytics endpoint -
    route-gated to operators."""
    from app.models import ChatSession
    if all_users:
        own_outer, own_sub = "1=1", "1=1"
        params = {"limit": limit}
    elif user_id is None:
        own_outer, own_sub = "m.user_id IS NULL", "user_id IS NULL"
        params = {"limit": limit}
    else:
        own_outer, own_sub = "m.user_id = :uid", "user_id = :uid"
        params = {"limit": limit, "uid": user_id}
    with get_session() as db:
        result = db.execute(text(f"""
            SELECT
                session,
                MIN(timestamp) AS started,
                COUNT(*)       AS message_count,
                (SELECT content FROM messages
                 WHERE session = m.session AND role = 'user' AND {own_sub}
                 ORDER BY id LIMIT 1) AS first_message
            FROM messages m
            WHERE {own_outer}
            GROUP BY session
            ORDER BY MAX(id) DESC
            LIMIT :limit
        """), params)
        rows = [dict(r._mapping) for r in result]

        session_ids = [r["session"] for r in rows]
        if session_ids:
            meta_rows = db.query(ChatSession).filter(
                ChatSession.session_id.in_(session_ids)
            ).all()
            meta_map = {m.session_id: m for m in meta_rows}
        else:
            meta_map = {}

        for row in rows:
            meta = meta_map.get(row["session"])
            row["name"] = meta.name if meta else None
            row["category"] = meta.category if meta else "general"

    return rows


def get_analytics() -> dict:
    today_prefix = date.today().isoformat()
    with get_session() as db:
        sessions = db.execute(
            text("SELECT COUNT(DISTINCT session) FROM messages")
        ).scalar() or 0

        total_requests = db.execute(
            text("SELECT COUNT(*) FROM messages WHERE role='user'")
        ).scalar() or 0

        today_requests = db.execute(
            text("SELECT COUNT(*) FROM messages WHERE role='user' AND timestamp LIKE :p"),
            {"p": f"{today_prefix}%"},
        ).scalar() or 0

        top_model = db.execute(
            text(
                "SELECT model FROM messages WHERE role='user' AND model IS NOT NULL "
                "GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1"
            )
        ).scalar()

        try:
            fb = db.execute(
                text(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN value=1 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN value=-1 THEN 1 ELSE 0 END) FROM feedback"
                )
            ).first()
            fb_total, fb_up, fb_down = (fb[0] or 0, fb[1] or 0, fb[2] or 0)
        except Exception:
            fb_total, fb_up, fb_down = 0, 0, 0

    return {
        "total_sessions": sessions,
        "total_requests": total_requests,
        "requests_today": today_requests,
        "top_model": top_model,
        "feedback": {"total": fb_total, "thumbs_up": fb_up, "thumbs_down": fb_down},
    }


def session_belongs_to(session: str, user_id: int | None) -> bool:
    """Does this session hold at least one message owned by this caller?

    The ownership question the write paths need. A session id is client-chosen
    and travels in the request body, so any endpoint that accepts one and acts
    on it is trusting the caller about whose conversation it is. Absence of
    rows reads as "not yours": a session with no messages under your ownership
    is either someone else's or does not exist, and both answer the same way.
    """
    with get_session() as db:
        return _scope(db.query(Message.id), session, user_id).first() is not None
