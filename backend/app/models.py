from sqlalchemy import Column, Integer, String, Boolean, Text, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db import Base


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    username        = Column(String(255), nullable=False, unique=True)
    password_hash   = Column(String(255), nullable=False)
    role            = Column(String(50), nullable=False, default="member")
    permissions     = Column(Text, nullable=False, default="{}")
    department      = Column(String(100), nullable=False, default="general")
    is_active       = Column(Boolean, nullable=False, default=True)
    created_at      = Column(String(50), nullable=False)
    mfa_secret      = Column(String(255), nullable=True)
    mfa_enabled     = Column(Boolean, nullable=False, default=False)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until    = Column(String(50), nullable=True)

    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    token_hash = Column(String(255), nullable=False, unique=True)
    expires_at = Column(String(50), nullable=False)
    revoked    = Column(Boolean, nullable=False, default=False)

    user = relationship("User", back_populates="refresh_tokens")


Index("idx_rt_token", RefreshToken.token_hash)


class Message(Base):
    __tablename__ = "messages"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    session   = Column(String(255), nullable=False)
    # Tenant owner. Nullable: guests are NULL, an authed user owns their own
    # rows - so knowing/guessing another user's session id can't read or
    # delete their history.
    user_id   = Column(Integer, nullable=True)
    role      = Column(String(50), nullable=False)
    content   = Column(Text, nullable=False)
    model     = Column(String(100), nullable=True)
    timestamp = Column(String(50), nullable=False)


Index("idx_session", Message.session)
Index("idx_messages_user", Message.user_id)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False, unique=True)
    # Tenant owner: the session list + meta ops scope to this, so one user
    # can't see or rename another's sessions. Nullable = guest/legacy.
    user_id    = Column(Integer, nullable=True)
    name       = Column(String(300), nullable=True)
    category   = Column(String(100), nullable=False, default="general")
    created_at = Column(String(50), nullable=False)
    updated_at = Column(String(50), nullable=False)


Index("idx_chat_sessions_sid", ChatSession.session_id)
Index("idx_chat_sessions_user", ChatSession.user_id)


class Feedback(Base):
    __tablename__ = "feedback"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(255), nullable=False)
    turn_index = Column(Integer, nullable=False)
    value      = Column(Integer, nullable=False)
    created_at = Column(String(50), nullable=False)


class Config(Base):
    __tablename__ = "config"

    key   = Column(String(255), primary_key=True)
    value = Column(Text, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, nullable=True)
    username        = Column(String(100), nullable=True)
    session_id      = Column(String(255), nullable=False)
    timestamp       = Column(String(50), nullable=False)
    prompt_hash     = Column(String(64), nullable=False)
    prompt_preview  = Column(String(200), nullable=False)
    response_length = Column(Integer, nullable=False, default=0)
    model           = Column(String(100), nullable=True)
    use_rag         = Column(Boolean, nullable=False, default=False)
    sources         = Column(Text, nullable=False, default="[]")
    # Full user-experienced answer latency (request arrival -> stream done,
    # retrieval included). NULL = honestly unknown, never 0.
    duration_ms     = Column(Integer, nullable=True)
    # Time to first token: request arrival -> the FIRST event the provider
    # stream yields (text or tool call). duration_ms minus this is generation
    # plus tool time; this is pre-model work plus provider prefill. NULL on
    # no-model lanes, which never get a provider token - unknown or
    # not-applicable, never 0.
    ttft_ms         = Column(Integer, nullable=True)
    # Which lane produced the answer: 'model' | 'rag_refusal' (and any future
    # deterministic lane). Non-model lanes answer deterministically WITHOUT
    # calling a model while still stamping the requested model, so per-model
    # latency is only separable with this column.
    answer_lane     = Column(String(20), nullable=True)
    # Rerank receipt - the production evidence for who served the scores, per
    # answer:
    #   rerank_ms:       wall time of the scoring call(s), fallback attempts
    #                    included - cold-vs-warm and chain engagement show here.
    #   rerank_pool:     candidates handed to the reranker (post per-source cap).
    #   rerank_provider: who actually SERVED - 'remote-http' | 'hosted-api' |
    #                    'local' | 'local-fallback' (chain engaged) | 'none'
    #                    (chain exhausted, retriever order).
    # All three NULL when retrieval never ran this turn - unknown or
    # not-applicable, never 0.
    rerank_ms       = Column(Integer, nullable=True)
    rerank_pool     = Column(Integer, nullable=True)
    rerank_provider = Column(String(20), nullable=True)


Index("idx_audit_ts",   AuditLog.timestamp)
Index("idx_audit_user", AuditLog.user_id)


class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    job_id           = Column(String(64), nullable=False, unique=True)
    status           = Column(String(20), nullable=False, default="queued")  # queued|running|complete|failed
    source           = Column(String(500), nullable=False)
    department       = Column(String(100), nullable=False, default="general")
    chunks_processed = Column(Integer, nullable=False, default=0)
    chunks_total     = Column(Integer, nullable=True)
    error            = Column(Text, nullable=True)
    created_at       = Column(String(50), nullable=False)
    completed_at     = Column(String(50), nullable=True)


Index("idx_ingest_jobs_created", IngestJob.created_at)


class EvalQuestion(Base):
    __tablename__ = "eval_questions"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    question   = Column(Text, nullable=False)
    category   = Column(String(100), nullable=False, default="general")
    notes      = Column(Text, nullable=True, default="")
    # Scoped canonical source the answer SHOULD come from, e.g.
    # "local:handbook/onboarding.md" (this instance's corpus) or
    # "peer:<instance>" (federated). Null = a guardrail question with no
    # source (must refuse). Enables retrieval-recall scoring: did retrieval
    # surface this source?
    expected_source = Column(String(255), nullable=True)
    # Clearance level the question is ASKED at. NULL = Owner / full access
    # (the trusted-internal-caller default retrieve() uses); the
    # tier-isolation cohort sets Member (1) / Guest (0) to measure that a low
    # tier's answer REFUSES higher-tier content instead of leaking it.
    # Levels are app.permissions.ROLE_LEVELS.
    as_level   = Column(Integer, nullable=True)
    # Locked-holdout flag. 1 = outside-model-authored cohort the tuning never
    # targets: runs in every eval but reports ONLY as an aggregate (tuned % /
    # holdout % / GAP) and is structurally excluded from the fix-feeding
    # surfaces (per-row miss diagnostics). The lock is procedural, not
    # cryptographic - the panel says so. 0/null = tuned.
    holdout    = Column(Integer, nullable=True)
    # Multi-turn cohort: JSON list of prior {role, content} turns replayed as
    # conversation history before the question is asked - a single-turn
    # harness structurally cannot see chat-handler behavior (the
    # bare-followup class). NULL = single-turn.
    setup_turns = Column(Text, nullable=True)
    created_at = Column(String(50), nullable=False)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    run_id        = Column(String(64), nullable=False)
    question_id   = Column(Integer, nullable=True)
    question_text = Column(Text, nullable=False)
    category      = Column(String(100), nullable=False, default="general")
    response      = Column(Text, nullable=False)
    score         = Column(Integer, nullable=True)  # 1=pass, 0=fail, null=unscored
    # Answer-mode judge's reasoning (null on retrieval-only rows).
    # "[judge error/unparseable: ...]" marks a judge failure - score stays
    # null there, never a fake fail.
    judge_rationale = Column(Text, nullable=True)
    # Retrieval-recall capture (RAG runs only): which sources came back,
    # whether the question's expected_source was among them, and its 1-based
    # rank (null=miss).
    retrieved_sources = Column(Text, nullable=True)      # JSON list of source names
    retrieval_hit     = Column(Integer, nullable=True)   # 1=hit, 0=miss, null=n/a
    retrieval_rank    = Column(Integer, nullable=True)   # 1-based rank of the hit, null if miss
    # Faithfulness capture: the grounding material the model was ACTUALLY
    # given (formatted context + tool outputs, captured at run time -
    # re-retrieving later would measure today's corpus, not the run's) and
    # the groundedness verdict against it. Same 1/0/null semantics as score.
    context_text            = Column(Text, nullable=True)
    faithfulness            = Column(Integer, nullable=True)  # 1=faithful, 0=unfaithful, null=unjudged
    faithfulness_rationale  = Column(Text, nullable=True)
    # Freshness capture: is the grounding material itself current, or a stale
    # copy? Judged against the question's grading key (the current truth).
    # Null on rows with no grounding or no grading key; 1/0/null semantics.
    freshness               = Column(Integer, nullable=True)  # 1=fresh, 0=stale, null=unjudged
    freshness_rationale     = Column(Text, nullable=True)
    # Holdout stamp copied from the question at run time, so a run's cohort
    # split survives later question edits.
    holdout       = Column(Integer, nullable=True)
    # The model that GENERATED this answer. The judge model lives on
    # EvalJudgeVerdict.judge_model; this stamps the ANSWER model so a run is
    # self-documenting - which model wrote it, not only which graded it.
    answer_model  = Column(String(100), nullable=True)
    # The CORPUS this run was measured against - "src=N;chunks=N;sha=...",
    # computed once per run. The writer stamp and the pinned question set
    # cover two legs of a score's identity (system, corpus, questions); this
    # records the third, so two runs can never silently differ on what was
    # being measured. Null = unknown - do NOT treat as "same corpus".
    corpus_fingerprint = Column(String(100), nullable=True)
    # The JUDGE INSTRUMENT this run was graded with (the judge model id),
    # stamped once per run. A judge or rubric change is a NEW instrument;
    # the trust panel bands only runs sharing one instrument era, and
    # NULL-era rows never band with stamped ones.
    judge_instrument = Column(String(100), nullable=True)
    run_at        = Column(String(50), nullable=False)


Index("idx_eval_results_run", EvalResult.run_id)


class EvalJudgeVerdict(Base):
    """Secondary-judge verdicts. The PRIMARY judge's verdicts live on
    EvalResult (score/faithfulness/freshness); every ADDITIONAL judge
    re-grading a stored run writes rows here, keyed by (result, judge model,
    rubric) - so a third or fourth judge (e.g. a local-model opinion) is a
    config run, not a schema change. Upsert on re-grade: one row per key, the
    latest verdict wins. Same 1/0/null score semantics as EvalResult."""
    __tablename__ = "eval_judge_verdicts"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    result_id   = Column(Integer, nullable=False)   # eval_results.id
    judge_model = Column(String(100), nullable=False)
    rubric      = Column(String(30), nullable=False)  # correctness|faithfulness|freshness
    score       = Column(Integer, nullable=True)      # 1=pass, 0=fail, null=unjudged
    rationale   = Column(Text, nullable=True)
    judged_at   = Column(String(50), nullable=False)

    __table_args__ = (
        UniqueConstraint("result_id", "judge_model", "rubric",
                         name="uq_judge_verdict"),
    )


Index("idx_judge_verdicts_result", EvalJudgeVerdict.result_id)


class QuarantinedDoc(Base):
    """Untrusted content the injection gate withheld from the corpus.

    A quarantined document was NEVER embedded - its full pre-chunking text is
    held here for owner review. Release re-ingests it (quarantine_exempt, tag
    preserved for audit); delete discards it. Curated/system content never
    lands here by policy (corpus_scan.should_quarantine)."""
    __tablename__ = "quarantined_docs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    source      = Column(String(500), nullable=False)
    department  = Column(String(100), nullable=False, default="general")
    trust_tier  = Column(String(20),  nullable=False)
    text        = Column(Text,        nullable=False)
    findings    = Column(Text,        nullable=True)   # JSON [{type, severity}]
    status      = Column(String(20),  nullable=False, default="held")  # held | released | deleted
    created_at  = Column(String(50),  nullable=False)
    reviewed_at = Column(String(50),  nullable=True)


Index("idx_quarantine_status", QuarantinedDoc.status)
