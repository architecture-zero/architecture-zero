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


def is_owner(user: dict | None) -> bool:
    """The single god-mode gate: Owner bypasses every permission and level
    check. Everyone else, admin included, is bounded by their preset and
    clearance level."""
    return bool(user) and user.get("role") == "owner"
