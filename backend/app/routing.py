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


def is_followup(query: str) -> bool:
    """True when the WHOLE message is a bare deictic/continuation follow-up
    that only resolves against the previous turn."""
    return bool(_FOLLOWUP_RE.match(query or ""))


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


def resolve_followup(query: str, history=None) -> str:
    """Expand a bare follow-up into a self-contained RETRIEVAL query by
    re-attaching the topic of the last SUBSTANTIVE user turn. Returns `query`
    unchanged when it is not a bare follow-up, or when no substantive prior
    turn exists (fail-open - never worse than not rewriting). A status-shaped
    follow-up ON a status-shaped topic also gets a 'current status' frame so
    is_status_query fires; every other pairing keeps the neutral
    '<topic> - <query>' form, which leaves the topic's own terms dominant.
    Rewrites the retrieval query ONLY - the caller still sends the user's real
    words to the model and saves them to history."""
    if not is_followup(query) or not history:
        return query
    topic = _last_topic_text(history)
    if not topic:
        return query
    if _STATUS_FOLLOWUP_RE.match(query) and is_status_topic(topic):
        return f"{topic} - current status"
    return f"{topic} - {query.strip()}"
