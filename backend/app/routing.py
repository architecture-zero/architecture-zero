"""Query-type routing for retrieval.

The operational session log lives in the history department, OUT of the
default retrieval pool: a long-running log grows to dominate the corpus,
semantically absorbs fact answers, and crowds curated docs out of the
candidates. History-shaped questions need that pool back at query time.

Design constraints:
- Deterministic (regex, no LLM call): retrieval stays cheap, offline, and a
  retrieval-only eval can measure routing directly.
- Recall-biased: routing a query that didn't need it only ADDS the history
  collection to the candidate pool - the global pool is always queried and
  the cross-encoder reranker still arbitrates. NOT routing a history question
  makes its answer unreachable. So patterns err liberal.
"""
import re

from app.logger import get_logger

log = get_logger("routing")

# Signals that a question is about what happened / what was done / when -
# past-work narrative rather than current fact. Month-year and ISO-date
# mentions count: dated events live in the session log.
_HISTORY_RE = re.compile(
    r"""
      \b(last|previous|prior|recent|latest|earlier)\s+
          (session|sessions|week|weeks|night|month|sprint|build|builds|work|time)\b
    | \bmost\s+recent\b
    | \brecently\b
    | \bwhat\s+(did|have|happened|changed|went)\b
    | \b(did|have|had)\s+(we|you|i)\b
    | \bwhen\s+(did|was|were|we)\b
    | \bsession\s+log\b
    | \bhistor(y|ical)\b
    | \b(root\s+cause|outage|incident|postmortem|regression|audit)\b
    | \b(19|20)\d{2}-\d{2}\b
    | \b(january|february|march|april|may|june|july|august|september|october|
         november|december)\s+20\d{2}\b
    # Temporal-span / trajectory phrasings: "how has that number changed since
    # mid-June" carries none of the patterns above, yet its answer lives
    # entirely in the history pool.
    | \bsince\s+((early|mid|late)[- ]?)?
        (january|february|march|april|may|june|july|august|september|october|
         november|december|(19|20)\d{2})\b
    | \b(over|during|in)\s+the\s+(past|last)\s+\w+\b
    | \bover\s+time\b
    | \btrajectory\b
    | \bhow\s+(has|have)\s+\w+(\s+\w+){0,3}\s+(changed|evolved|improved|progressed|grown)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def route_departments(query: str) -> list[str]:
    """Extra departments to include for this query, beyond the caller's own.
    Currently: ["history"] for past-work-shaped questions, else []."""
    if _HISTORY_RE.search(query or ""):
        return ["history"]
    return []


# Status/plan-shaped questions: asking what the CURRENT state/plan/next step
# is, rather than a fact or past-work narrative. Narrative docs legitimately
# contain stale future-tense prose about exactly these questions, and only the
# system-generated DB-truth chunks are current by construction - so this class
# lifts generated chunks over narrative. Recall-biased like _HISTORY_RE: a
# false positive only reorders the kept context, never drops anything.
_STATUS_RE = re.compile(
    r"""
      \bwhat('?s|\s+is|\s+are)\s+(next|the\s+next|coming|left|remaining|pending)\b
    | \bnext\s+(build|step|steps|session|task|tasks|item|items|up)\b
    # Noun-anchored on purpose: a bare "what is the current ..." would drag in
    # guardrail questions like "current price of Bitcoin".
    | \b(current|active)\s+(plan|plans|arc|status|state|focus|work|build|priorit)\w*\b
    | \bwhere\s+(are\s+we|do\s+we\s+stand|did\s+we\s+leave)\b
    | \bstatus\s+of\b
    | \bmy\s+plans?\b
    | \broadmap\b
    | \bpriorit(y|ies)\b
    | \bworking\s+on\s+(now|right\s+now|currently|these\s+days)?\b
    | \bwhat\s+should\s+(i|we)\s+(do|build|work\s+on)\s+next\b
    | \bopen\s+loops?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_status_query(query: str) -> bool:
    """True when the question asks about current state/plans - the class where
    system-generated chunks must outrank narrative docs."""
    return bool(_STATUS_RE.search(query or ""))


# Deictic / continuation follow-ups: a whole message that only carries meaning
# against the previous turn ("current", "more", "what about that"). retrieve()
# is STATELESS - it scores one query string, no conversation memory - so a
# bare follow-up reaches it with no subject and lands on noise. We resolve it
# at the chat edge (the only place history exists) by re-attaching the last
# user turn's topic BEFORE retrieval. Conservative by construction: only fires
# when the ENTIRE message is a follow-up token, so a real question is never
# rewritten; recall-biased/fail-open like _HISTORY_RE - the worst case just
# re-adds context the query already implied.
_FOLLOWUP_RE = re.compile(
    r"""^\s*(
        current | now | latest | recent(ly)? | status | update(s)?
      | next | what'?s\s+next | where\s+(are\s+we|do\s+we\s+stand)
      | more | continue | go\s+on | keep\s+going | and
      | what\s+about\s+(it|that|now|then)
      | why | how\s+so | explain(\s+(more|that|this|it))? | tell\s+me\s+more
      )\s*[\?\.!]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# The status-shaped subset: these mean "the CURRENT state of what we were
# discussing", so their expansion can take a "current status" frame that trips
# is_status_query and lifts the LIVE SYSTEM RECORD chunks over stale
# narrative. The frame is applied only when the TOPIC is status-shaped too.
_STATUS_FOLLOWUP_RE = re.compile(
    r"""^\s*(
        current | now | latest | recent(ly)? | status | update(s)?
      | next | what'?s\s+next | where\s+(are\s+we|do\s+we\s+stand)
      )\s*[\?\.!]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Which TOPICS the "current status" frame is right for. A bare status
# follow-up ("current", "latest") means "the current state of what we were
# discussing" - when that subject is the project/plan/work, the frame is
# correct. When the subject is a specific FACT or how-it-works question
# ("how does the cross-encoder reranker work"), forcing the frame floats
# generated DB chunks over the narrative the user actually wants, so those
# take the neutral form instead. Deliberately BROADER than is_status_query:
# "give me a general review of my project" is not itself a status QUESTION,
# yet its follow-up "current" plainly asks for project state.
_STATUS_TOPIC_RE = re.compile(
    r"""
      \b(my|our|the)\s+(project|projects|plan|plans|roadmap|work|build|builds|
                        arc|port|progress|backlog|priorities)\b
    | \breview\s+(of\s+)?(my|our|the)\b
    | \bwhat\s+(are\s+)?(we|you|i)\s+(doing|building|up\s+to)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# The longest message is_followup will read: the longest follow-up phrase
# with its punctuation is under thirty characters.
FOLLOWUP_MAX_CHARS = 64


def is_followup(query: str) -> bool:
    """True when the WHOLE message is a bare deictic/continuation follow-up
    that only resolves against the previous turn."""
    q = (query or "").strip()
    # A follow-up is a few words, and a long message never is one. The
    # pattern's two whitespace runs around its punctuation went quadratic on
    # a long run of spaces (1.09 s at 24,000 characters; the chat bound lets a
    # signed-in caller send 200,000), so the length is checked first.
    if len(q) > FOLLOWUP_MAX_CHARS:
        return False
    return bool(_FOLLOWUP_RE.match(q))


def is_status_topic(topic: str) -> bool:
    """True when a topic is about the project's state/plans - so a bare status
    follow-up on it should get the 'current status' frame."""
    return is_status_query(topic) or bool(_STATUS_TOPIC_RE.search(topic or ""))


def _last_topic_text(history) -> str:
    """Most recent SUBSTANTIVE user message - the last user turn that is NOT
    itself a bare follow-up. History stores the user's real words (only the
    retrieval query is ever rewritten), so on chained follow-ups ("current"
    then "more") a naive last-user-turn walk re-attaches a prior deictic token
    instead of the subject; those turns are skipped and the walk continues.
    Accepts Message-likes (pydantic objects with .role/.content, or dicts).
    '' when there is no substantive user turn (the caller then fails open)."""
    for m in reversed(history or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        text = (content or "").strip()
        if text and not is_followup(text):
            return text
    return ""


# How much of the last answer the 'exchange' shape carries into the retrieval
# query: enough to name the subject, short enough that the user's own topic
# stays the larger part of most queries.
EXCHANGE_CLIP_CHARS = 400


# What followup_shape has already said in the log. Each thing is said once per
# process, not once per follow-up; the limit keeps a run of different mistyped
# values from growing the set.
_SHAPE_NOTICES: set = set()
_SHAPE_NOTICE_LIMIT = 8


def _note_once(what: str, message: str, *args) -> None:
    if what in _SHAPE_NOTICES or len(_SHAPE_NOTICES) >= _SHAPE_NOTICE_LIMIT:
        return
    _SHAPE_NOTICES.add(what)
    log.warning(message, *args)


def _rerank_query_can_reach_a_vendor() -> bool:
    """True when this host allows hosted reranking: the latch
    RERANK_HOSTED_ALLOWED is open. The latch and nothing else. With it shut no
    provider setting can send a rerank query to a vendor; with it open a
    config change alone can start that, on any turn. The latch is fixed for
    the life of the process, so the answer here and the answer at the rerank
    call are always the same one - there is no turn that can see two. It is
    read through its module when called, never from a copy taken at import."""
    from app import rag_config
    return bool(rag_config.RERANK_HOSTED_ALLOWED)


def followup_shape() -> str:
    """How a bare follow-up becomes a retrieval query. 'topic' (the default)
    re-attaches the last substantive user turn. 'exchange' also carries the
    start of the last assistant turn, which names the subject in the corpus's
    own vocabulary where the user's question often does not. Config key
    `followup_resolver`, read per call so an A/B arm or an operator flips it
    without a restart. Unset, blank and `topic` read as 'topic', and so does a
    config store that cannot be read. Any other value that is not `exchange`
    also reads as 'topic' and says so once in the log: a mistyped value must
    not measure one shape under the other's name.

    The start of an answer never rides a query that can reach a hosted
    vendor. The retrieval query is what the reranker is sent, an answer can be
    written from any collection the caller may read, and the rule that keeps
    non-public collections off the vendor reads the candidates, not the
    query. So while the host allows hosted reranking (the latch
    RERANK_HOSTED_ALLOWED is open), the key's `exchange` is refused and the
    shape is 'topic' on every turn, whatever the provider setting reads (said
    once in the log). The decision is made here, before retrieval, so it
    cannot depend on what a turn retrieves, and on the latch alone, so it
    cannot change between this read and the rerank call. With the latch shut
    the shape is available with any provider setting: the local model, a
    scoring endpoint on the operator's own hardware, and a hosted provider
    that the shut latch keeps from sending anything."""
    try:
        from app.config import get_config
        v = (get_config("followup_resolver", "") or "").strip().lower()
    except Exception:
        v = ""
    if v in ("", "topic"):
        return "topic"
    if v != "exchange":
        _note_once("unrecognised " + v[:60],
                   "unrecognised followup_resolver %r (expected topic or exchange) - "
                   "using 'topic'", v[:60])
        return "topic"
    if _rerank_query_can_reach_a_vendor():
        _note_once("refused",
                   "followup_resolver=exchange is not applied: hosted reranking is allowed "
                   "on this host (RERANK_HOSTED_ALLOWED) and the start of an answer never "
                   "rides a query that can reach a vendor - using 'topic'")
        return "topic"
    return "exchange"


def _last_assistant_text(history) -> str:
    """The first EXCHANGE_CLIP_CHARS of the most recent assistant turn,
    whitespace-collapsed; '' when there is none."""
    for m in reversed(history or []):
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "assistant":
            continue
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        return " ".join((content or "").split())[:EXCHANGE_CLIP_CHARS]
    return ""


def resolve_followup(query: str, history=None) -> str:
    """Expand a bare follow-up into a self-contained RETRIEVAL query by
    re-attaching the topic of the last SUBSTANTIVE user turn (and, in the
    'exchange' shape, the start of the last assistant turn - followup_shape).
    Returns `query` unchanged when it is not a bare follow-up, or when no
    substantive prior turn exists (fail-open - never worse than not
    rewriting). A status-shaped follow-up ON a status-shaped topic also gets a
    'current status' frame so is_status_query fires - decided on the USER's
    topic alone, so words in an answer never flip it; every other pairing
    keeps the neutral '<topic> - <query>' form, which leaves the topic's own
    terms dominant. Rewrites the retrieval query ONLY - the caller still sends
    the user's real words to the model and saves them to history."""
    if not is_followup(query) or not history:
        return query
    topic = _last_topic_text(history)
    if not topic:
        return query
    status_frame = bool(_STATUS_FOLLOWUP_RE.match(query)) and is_status_topic(topic)
    # The shape is read only here, on a turn the branch can act on: one config
    # read on a bare follow-up while the key is unset, none on any other chat
    # question.
    if followup_shape() == "exchange":
        answer = _last_assistant_text(history)
        if answer:
            topic = f"{topic} {answer}"
    if status_frame:
        return f"{topic} - current status"
    return f"{topic} - {query.strip()}"
