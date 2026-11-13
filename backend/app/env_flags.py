"""Every on/off setting the host's environment carries, and the one way each is read.

THE RULE, the same for every row of the table below:

  - the word `true` is on and the word `false` is off, in any letter case;
  - a variable left unset, or set to nothing, is "not set": it reads as the
    row's default;
  - any other value refuses to start. `1`, `yes`, `on`, a word with a space or
    a quote beside it: none is read as on, and none is read as off.

WHY A REFUSAL. An on/off setting compared against one word reads every other
value as off, without a line anywhere. For a protection that is on by default
(sign-in, the audit log, the injection scan) a mistyped value then turns it
off; for one that is off by default an operator who wrote `1` believes it is
on. A control that reads as configured and does something else is worse than
one that is absent, so a value that is neither word stops the import that
read it, and with it the start. Not solved by accepting more words: that
would switch a setting ON at an update on a host that has carried `1` for a
year. Not solved by trimming: a padded value is refused like any other.

The refusal names the variable and the two words it takes and NEVER the value:
a value on the wrong line of an env file can be a secret, and a boot log is
not the place to quote one.

EMPTY IS NOT SET. `NAME=` reads as the row's default, as an operator expects
of an empty line. For a row whose default is off that is what it always
meant. For a row whose default is ON it is the one meaning this module
changed: an empty value used to read as off there.

THE TABLE IS THE LIST. A name that is not a row raises: an on/off setting is
added here, with what unset means, or it has no rule. tests/
test_env_flags_strict.py reads the app's source and fails on an on/off read
of the environment anywhere but this file.

WHAT THIS MODULE MAY IMPORT: the standard library's os and sys, and nothing
of the app. It is the first thing the backend runs (app/main.py calls
check_all before it builds anything), so it must not open the database, the
vector store, a socket or a file - a start that is going to be refused must
be refused with nothing written.

    python -m app.env_flags

runs the same check by itself: exit 0 when every row reads, exit 1 with the
one refusal line when one does not.
"""
import os
import sys


def _legacy_provider_is(word: str):
    """The default of a provider switch: on when the older single-provider
    setting PROVIDER names that provider (it defaults to ollama), so a host
    that only ever set PROVIDER keeps the provider it chose."""
    def default() -> bool:
        return os.environ.get("PROVIDER", "ollama").lower() == word
    return default


# name -> what an unset or empty variable means. A bool, or a function of no
# arguments for a default that depends on another setting.
_TABLE = {
    # sign-in and passwords
    "ENABLE_AUTH": True,
    "REQUIRE_MFA": False,
    "REQUIRE_SPECIAL_CHARS": True,
    "REQUIRE_UPPERCASE": True,
    "ALLOW_GUEST_MODE": False,
    # request protections
    "ENABLE_RATE_LIMIT": False,
    "ENABLE_INJECTION_PROTECTION": True,
    "ENABLE_AUDIT_LOG": True,
    "ENCRYPTION_AT_REST_VERIFIED": False,
    "ENABLE_API_DOCS": False,
    # the agent's tools
    "ENABLE_AGENT_TOOLS": False,
    "ENABLE_READ_TOOLS": True,
    "ENABLE_WRITE_TOOLS": False,
    "ENABLE_SHELL_TOOLS": False,
    # providers (each is also the default of a stored setting an admin can save)
    "ENABLE_OLLAMA": _legacy_provider_is("ollama"),
    "ENABLE_ANTHROPIC": _legacy_provider_is("anthropic"),
    "ENABLE_OPENAI": _legacy_provider_is("openai"),
    # retrieval
    "RAG_ONLY_MODE": False,
    "RERANK_ENABLED": True,
    "RERANK_HOSTED_ALLOWED": False,
    # peers
    "ECO_EXPOSE_KB": False,
    "PEER_ALLOW_PRIVATE": False,
    # background work
    "ENABLE_ASYNC_JOBS": False,
    "BOOT_BACKGROUND_SYNC": True,
    "HELP_DOCS": True,
    "SELF_CHECK_BACKUP": True,
}


def names() -> tuple:
    """Every row's name, in the table's order."""
    return tuple(_TABLE)


def default(name: str) -> bool:
    """What an unset or empty `name` reads as."""
    if name not in _TABLE:
        raise LookupError(_not_a_row(name))
    row = _TABLE[name]
    return bool(row()) if callable(row) else row


def _not_a_row(name: str) -> str:
    return (f"{name} is not a row of the on/off table in app/env_flags.py. Add "
            "it there, with what an unset variable means, and read it with "
            "flag(): an on/off setting read any other way has no rule.")


def _refusal(name: str) -> str:
    unset = "on" if default(name) else "off"
    return (
        f"SECURITY: {name} is set to a value that is neither true nor "
        "false. Refusing to start rather than guess which was meant: "
        f"set {name}=true or {name}=false (unset or empty is {unset}), with "
        "nothing else in the value, and start the backend again. "
        "Under Docker Compose that is `docker compose up -d`: a plain restart "
        "keeps the value the container was created with.")


def stated(name: str):
    """What the host wrote for `name`: True, False, or None for a variable
    that is unset or empty. Any other value raises the refusal.

    For the one caller that has a default of its own to fall back on (a
    stored setting whose default is this variable). Everything else calls
    flag()."""
    if name not in _TABLE:
        raise LookupError(_not_a_row(name))
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    word = raw.lower()
    if word == "true":
        return True
    if word == "false":
        return False
    raise RuntimeError(_refusal(name))


def flag(name: str) -> bool:
    """The setting `name`, on or off, by the rule at the top of this file."""
    said = stated(name)
    return default(name) if said is None else said


def check_all() -> None:
    """Read every row, whether or not any module has asked for it yet, and
    raise the refusal of the first one whose value is neither word.

    Called at the top of app/main.py, ahead of the app object and of every
    import that opens or writes something. A module that reads its own
    setting at import would refuse too - later, after whatever was imported
    before it had run - and a setting read only at call time would not
    refuse until the call."""
    for name in _TABLE:
        flag(name)


def states() -> dict:
    """Every row's effective state by name, as the words on and off - what
    the boot's one line on these settings carries. Never a value."""
    return {name: ("on" if flag(name) else "off") for name in _TABLE}


def _main() -> int:
    """`python -m app.env_flags`: the check by itself. Exit 0 and not a word
    when every row reads; exit 1 with the refusal of the first row that does
    not, as the only line on the error stream. The line is this module's own
    sentence, built from the row's name - never the variable's value."""
    for name in _TABLE:
        try:
            flag(name)
        except RuntimeError:
            print(_refusal(name), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
