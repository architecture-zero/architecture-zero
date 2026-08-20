from datetime import datetime, timezone
from sqlalchemy import func
from app.db import get_session
from app.models import Feedback as FeedbackModel


def init_feedback_db():
    pass  # Schema managed by db.init_db()


def save_feedback(session_id: str, turn_index: int, value: int):
    with get_session() as db:
        db.add(FeedbackModel(
            session_id=session_id,
            turn_index=turn_index,
            value=value,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))


def get_feedback_summary() -> dict:
    with get_session() as db:
        total = db.query(func.count(FeedbackModel.id)).scalar() or 0
        up    = db.query(func.count(FeedbackModel.id)).filter(FeedbackModel.value == 1).scalar() or 0
        down  = db.query(func.count(FeedbackModel.id)).filter(FeedbackModel.value == -1).scalar() or 0
    return {"total": total, "thumbs_up": up, "thumbs_down": down}
