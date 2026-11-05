"""The RUN-SETUP stamp: what a band of "identical runs" has to share.

The trust panel bands runs it calls identical, and until 2026-11-05 identical
meant the same writer, corpus, judge instrument and exam shape. Two runs could
match on all four and still measure different systems: one whose eval asked
the instance's peers and one that did not, a changed retrieval threshold, a
new persona, a new build. Their band was two systems wearing one number.

Naming each setting that matters is how the next one gets missed, so this
stamps everything a run's answers could depend on, and leaves out only what is
known NOT to be a setting:

- the build (GIT_SHA - every deploy is a new system);
- this process's boot (an environment change takes effect only on a restart,
  and the environment holds knobs no settings row shows);
- every settings row, a credential reduced to whether it is set - so a key
  rotation changes nothing here and no secret feeds a value that lands in
  exportable eval rows; the peer list is a row;
- minus the rows the app writes as STATE rather than settings (below);
- the run's own options (writer, retrieval on or off, results kept, retrieval
  only, peers asked).

Erring wide is the safe direction: a setting stamped that turns out not to
matter only narrows a band (fewer runs, still honest); a setting missed lets
two systems band as one. tests/test_run_setup.py pins every settings-table
write in the code, so a new row has to be classified before it ships.
"""
import hashlib
import json
import uuid

# Rows the app writes as running state, not as a setting. A row here changes
# while nothing about the system does (peer health updates on every peer call),
# so stamping it would make every run its own era and no band could ever form.
_STATE_PREFIXES = ("peer_health:",)
_STATE_KEYS = frozenset({
    "last_backup_timestamp",
    "last_backup_file",
    "rerank_hosted_vendors_used",
    "setup_wizard_state",
})

# One per process: two runs on either side of a restart never share it.
_BOOT = uuid.uuid4().hex


def is_state_key(key: str) -> bool:
    return key in _STATE_KEYS or key.startswith(_STATE_PREFIXES)


def run_setup_stamp(options: dict) -> str:
    """The stamp for one run: 16 hex characters, the same for every row of
    the run. `options` is the run's own arguments - writer, use_rag,
    n_results, retrieval_only, use_peers and whatever else the runner takes."""
    from app.boot_history import current_sha
    from app.config import get_all_config, is_secret_config_key
    settings = {}
    for k, v in get_all_config().items():
        if is_state_key(k):
            continue
        settings[k] = bool(v and v.strip()) if is_secret_config_key(k) else v
    payload = {"build": current_sha(), "boot": _BOOT,
               "settings": settings, "options": options}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
