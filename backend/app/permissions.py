"""Two separate authorization axes, resolved from one user record.

PERMISSION SCOPES gate ACTIONS (what you may do); ACCESS LEVELS gate READS
(which knowledge-base departments retrieval may search for you). Keeping them
separate matters: an admin who manages users still must not read owner-only
content, and a member who can read a department still cannot manage it.
"""

PERMISSION_SCOPES: list[str] = [
    "chat",            # use the chat interface
    "view_history",    # see own conversation history
    "manage_kb",       # upload / delete knowledge base documents
    "manage_users",    # create, deactivate, change role / permissions
    "view_analytics",  # read analytics and usage stats
    "view_audit_log",  # read the audit log
    "manage_system",   # edit system prompt, instance config, model settings
]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "owner":  list(PERMISSION_SCOPES),
    # Admin runs content and people, deliberately NOT the system itself -
    # model settings and instance config stay with the Owner.
    "admin":  ["chat", "view_history", "manage_kb", "manage_users", "view_analytics"],
    "member": ["chat", "view_history"],
    "guest":  ["chat"],
}


def effective_permissions(user: dict) -> list[str]:
    """A non-empty stored permission list is an explicit per-user override;
    otherwise the role preset applies."""
    stored = user.get("permissions") or {}
    if isinstance(stored, list) and stored:
        return stored
    return ROLE_PERMISSIONS.get(user.get("role", "member"), ROLE_PERMISSIONS["member"])


# Access levels are a clearance ladder: a higher rung reads everything the
# rungs below read, plus its own tier. Retrieval enforces this at the
# department level (see rag_config.DEPARTMENT_MIN_LEVEL and rerank.retrieve),
# so a lower tier can never pull higher-tier content into an answer.
GUEST_LEVEL  = 0
MEMBER_LEVEL = 1
ADMIN_LEVEL  = 2
OWNER_LEVEL  = 3

ROLE_LEVELS: dict[str, int] = {
    "owner":  OWNER_LEVEL,
    "admin":  ADMIN_LEVEL,
    "member": MEMBER_LEVEL,
    "guest":  GUEST_LEVEL,
}


def effective_level(user: dict | None) -> int:
    """None (no account) is Guest. A present but unrecognized role resolves to
    Member - the lowest authenticated rung, never higher: unknown input fails
    closed toward less privilege."""
    if not user:
        return GUEST_LEVEL
    return ROLE_LEVELS.get(user.get("role", "member"), MEMBER_LEVEL)


# A federated peer key's scope is a CLEARANCE GRANT, expressed on the ladder
# above so the federation seam is gated by the same rungs as every other
# retrieval surface. It used to be a free-standing vocabulary that bypassed the
# ladder entirely: 'all' meant "every department except general", which reached
# `restricted` and `history` (both Owner-only) and every UNLISTED department -
# and unlisted is exactly what DEPARTMENT_DEFAULT_MIN_LEVEL fails closed to
# Owner. The serve path calls query_similar() directly, and query_similar takes
# no clearance argument (the gate lives one layer up, in rerank.retrieve), so
# nothing downstream caught it. A word that reads like "the shared stuff"
# handed an off-box caller the operator's internal docs.
#
#   public -> Guest rung: the global collection only.
#   all    -> Admin rung: every department the operator DELIBERATELY shared at
#             or below Admin. Owner-only departments stay home. This is the
#             sane default for "federate with a trusted peer".
#   owner  -> Owner rung: everything, internal docs included. Named separately
#             so it cannot be reached by picking the friendly-sounding word -
#             an operator who wants this has to say this.
PEER_SCOPE_LEVELS: dict[str, int] = {
    "public": GUEST_LEVEL,
    "all":    ADMIN_LEVEL,
    "owner":  OWNER_LEVEL,
}


def peer_scope_level(scope: str | None) -> int | None:
    """Clearance rung a peer-key scope grants, or None if the scope is unknown.
    None is the refusal signal - an unrecognized scope grants nothing."""
    if not scope:
        return None
    return PEER_SCOPE_LEVELS.get(scope.strip())


def is_owner(user: dict | None) -> bool:
    """The single god-mode gate: Owner bypasses every permission and level
    check. Everyone else, admin included, is bounded by their preset and
    clearance level."""
    return bool(user) and user.get("role") == "owner"


def can_grant(actor: dict | None, target: dict | None,
              permissions: list[str]) -> str | None:
    """Authority ceiling on permission WRITES. Returns None when the grant is
    allowed, or the refusal reason.

    One rule: nobody hands out authority they do not hold themselves.

    Without this, manage_users IS a superuser bootstrap. The role presets
    carefully withhold manage_system from admin, and change_role guards the
    role axis so an Admin cannot promote itself to Owner - but
    effective_permissions treats a non-empty stored list as an override that
    REPLACES the preset. So an Admin with manage_users could PATCH itself
    manage_system, and walk straight through the Owner-only door to
    /api/admin/config, where provider API keys live in cleartext. Locking one
    axis while the other can override it locks nothing.

    Self-targeting needs no separate case: under 'only what you hold' a write
    to your own row can narrow or preserve your authority, never raise it.
    """
    if is_owner(actor):
        return None
    if target and target.get("role") == "owner":
        # Mirrors change_role: an Owner's authority is Owner-managed only.
        return "Only an Owner can change an Owner's permissions"
    held = set(effective_permissions(actor or {}))
    over = sorted(p for p in permissions if p not in held)
    if over:
        return f"Cannot grant permissions you do not hold: {over}"
    return None
