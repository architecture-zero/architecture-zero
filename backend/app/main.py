"""Application assembly.

After the router split this file builds the app and nothing else: middleware,
the ten include_router calls, the boot-time side effects, and the four lifecycle
hooks. No routes, no models, no handlers, no business logic.

Direction is one-way and enforced by tests/test_module_hygiene.py: main imports
routers and the shared modules; nothing under app/ imports main.
"""
import os
import logging
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import AuthMiddleware
from app.audit import purge_old_entries
from app.config import init_config_db
from app.db import init_db as _create_schema
from app.logger import log, log_error
from app.security import setup_claim_code, claim_code_source
from app.users import owner_exists          # the boot banner's occupancy check
# The startup hooks drive these; the kb router imports its own from the same
# module. One-way: main -> ingest_sync.
from app.ingest_sync import _sync_knowledge_dir, _sync_docs, _watch_knowledge_dir
# The seed loader runs at startup; the rest of the eval engine is the evals
# router's.
from app.eval_runner import sync_eval_questions_from_seed
# Imported as a MODULE so _startup_ingest_active is written THROUGH it and the
# evals router sees the rebind. A from-import would snapshot False forever.
from app import runtime_config
from app.runtime_config import _all_origins, _allow_all, ENABLE_AUDIT_LOG

logger = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS         = int(os.getenv("AUDIT_RETENTION_DAYS", "365"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# API docs UI (/docs, /redoc, /openapi.json) is OFF by default - it publishes
# the full API surface. Enable only in local dev with ENABLE_API_DOCS=true.
_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"
app = FastAPI(
    title="Architecture Zero API",
    version="1.0",
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers. Registered at IMPORT time, not in a startup hook: conftest does
# `from app.main import app` and expects a populated route table the moment that
# import returns. An include_router inside @app.on_event leaves the TestClient
# short a router with no error message to say so.
from app.routers import (  # noqa: E402  (app must exist first)
    system as system_router,
    settings as settings_router,
    peers as peers_router,
    auth as auth_router,          # NOT app.auth - that is the middleware module
    sessions as sessions_router,
    users as users_router,
    admin as admin_router,
    kb as kb_router,
    evals as evals_router,
    chat as chat_router,
)

app.include_router(system_router.router)
app.include_router(settings_router.router)
app.include_router(peers_router.router)
app.include_router(auth_router.router)
app.include_router(sessions_router.router)
app.include_router(users_router.router)
app.include_router(admin_router.router)
app.include_router(kb_router.router)
app.include_router(evals_router.router)
app.include_router(chat_router.router)

_create_schema()   # create all tables via SQLAlchemy (idempotent)
init_config_db()   # seed config defaults
if ENABLE_AUDIT_LOG:
    purge_old_entries(AUDIT_RETENTION_DAYS)



@app.on_event("startup")
async def _claim_code_on_startup():
    """Print the first-Owner claim code while this deployment is UNCLAIMED.

    This is the line that makes the claim gate usable. Without it the operator
    has a required secret and no way to learn it, so the banner is part of the
    control rather than a convenience.

    Printed with print(), not log(). The logger fans out to stdout AND to a file
    under LOG_DIR, and a bootstrap secret does not belong in a file that
    outlives the ten minutes it is useful for. Docker still captures stdout,
    which is the surface an operator actually reads.

    The structured line beside it carries the SOURCE and never the value, so
    "the claim gate is armed" is a positive signal in the JSON logs - the same
    reason the persona-divergence hook emits its clean line. A guard that is
    silent when healthy cannot be told apart from a guard that is not running.

    Silent once an Owner exists: there is nothing to claim, setup answers 403,
    and printing a dead code at every boot would train the operator to scroll
    past the banner on the one boot where it matters.
    """
    try:
        if owner_exists():
            log("setup_claim_gate", state="claimed")
            return
        code = setup_claim_code()
        log("setup_claim_gate", state="unclaimed", code_source=claim_code_source())
        print(
            "\n"
            "  ================================================================\n"
            "   THIS DEPLOYMENT IS UNCLAIMED - whoever reaches it first can\n"
            "   take it. Claiming it requires the code below.\n"
            "\n"
            f"     claim code:  {code}\n"
            "\n"
            "   Claim it (see docs/runbook.md step 5):\n"
            "     curl -X POST localhost:8000/api/auth/setup \\\n"
            "       -H 'Content-Type: application/json' \\\n"
            '       -d \'{"username":"owner","password":"<strong password>",\n'
            "            \"claim_code\":\"<the code above>\"}'\n"
            "\n"
            "   The code dies the moment the deployment is claimed, and a\n"
            "   restart before then mints a new one.\n"
            "\n"
            "   Running more than one worker? Each worker mints its OWN code,\n"
            "   so a pasted code fails on the other workers - set\n"
            "   SETUP_CLAIM_CODE to pin one value across all of them.\n"
            "  ================================================================\n",
            flush=True,
        )
    except Exception as e:
        # Never take the boot down over the banner. A failure here leaves the
        # gate itself intact - verify_setup_claim_code mints on first read, so
        # setup still refuses every caller; it just means nobody was handed the
        # code and the operator needs SETUP_CLAIM_CODE.
        log_error("setup_claim_gate_crashed", error=str(e))


@app.on_event("startup")
async def _system_prompt_divergence_on_startup():
    """Report a persona that the 2026-08-27 DB-first change moved.

    get_system_prompt() used to let env SYSTEM_PROMPT beat the stored row, which
    made the persona row PATCH /api/admin/config writes a no-op. Inverting it
    fixes the write path and changes nothing for a never-edited deployment -
    init_config_db() seeds the row from the same env var - EXCEPT where env and
    row disagree (env edited after first boot, or a row saved while the env
    override kept it unread). That deployment has been served the env value and
    is now served its row, invisibly. This names it. The clean line is emitted too, so a guard that
    is silent when healthy cannot be mistaken for a guard that is not running.

    Lengths only, never the text: a persona can carry deployment-specific
    instructions and this line lands in ordinary logs.
    """
    try:
        from app.config import system_prompt_divergence
        diverged = system_prompt_divergence()
        if diverged:
            env_val, served = diverged
            log_error("system_prompt_env_divergence",
                      note=("env SYSTEM_PROMPT differs from the served row; the "
                            "ROW is served. Clear the row to adopt the env value, "
                            "or unset the env var to stop the mismatch."),
                      env_chars=len(env_val), served_chars=len(served))
        else:
            log("system_prompt_source_agrees")
    except Exception as e:
        log_error("system_prompt_divergence_check_crashed", error=str(e))


@app.on_event("startup")
async def startup_tasks():
    # Set BEFORE create_task, not as the first line inside the coroutine. A
    # task is only scheduled by create_task, so the flag stayed False from
    # here until the loop first ran _bg - and the eval runner reads this flag
    # to refuse a run mid-ingest. Narrow window, wrong side of the guard.
    # Module attribute, not a module global: the evals router reads
    # runtime_config.<name>, so a rebind here has to be visible there. A
    # surviving `global` would bind a phantom app.main attribute instead -
    # main arming its own copy while the router reads a permanent False.
    runtime_config._startup_ingest_active = True

    async def _bg():

        def _report_sync(stage: str, res: dict):
            """Startup syncs must not discard their results dict - a per-file
            ingest error (one embed timeout killing a whole big file) is
            otherwise INVISIBLE and only found via fingerprint archaeology.
            Errors log loudly; the summary line doubles as the 'boot sync
            done' sentinel in docker logs."""
            errors = {k: v.get("error", "") for k, v in res.items() if v.get("status") == "error"}
            counts = {"ok": 0, "skipped": 0, "pruned": 0, "error": len(errors)}
            for v in res.values():
                s = v.get("status")
                if s in counts and s != "error":
                    counts[s] += 1
            if errors:
                log_error("startup_sync_errors", stage=stage, errors=errors)
            log("startup_sync_done", stage=stage, **counts)

        # SQL-ONLY sync FIRST - it touches only the relational DB (no
        # embedding), so it must not sit behind the embed-heavy index syncs
        # below: a millisecond SQL upsert waiting many minutes behind docs
        # embedding means the question set silently does not appear until
        # then. Its result log lands in the first second so a miss is
        # visible at once.
        try:
            # Reconcile the live eval-question set with the repo seed file
            # (push=deploy; additive + label updates only, never deletes).
            await asyncio.get_running_loop().run_in_executor(None, sync_eval_questions_from_seed)
        except Exception as e:
            logging.getLogger("uvicorn.error").warning("eval seed sync on startup failed: %s", e)
        try:
            # Guest retention floor: age out anonymous sessions (0 disables).
            # Guest rows are already content-only; aging them out makes "no
            # user tracking" also mean "no content hoarding".
            days = int(os.getenv("GUEST_RETENTION_DAYS", "30"))
            if days > 0:
                from app.history import purge_anonymous_sessions
                purged = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: purge_anonymous_sessions(days))
                log("guest_retention_purge", days=days, **purged)
        except Exception as e:
            logging.getLogger("uvicorn.error").warning("guest retention purge failed: %s", e)
        try:
            # Ingest jobs run on a thread in THIS process, so a restart leaves
            # any queued or running row describing work that nothing is doing.
            # Fail them here, with the reason, rather than let the status
            # surface claim progress forever. SQL-only, so it belongs in this
            # stage rather than behind the embed-heavy syncs.
            from app.jobs import reconcile_orphaned_jobs
            orphans = await asyncio.get_running_loop().run_in_executor(
                None, reconcile_orphaned_jobs)
            log("ingest_jobs_reconciled", **orphans)
        except Exception as e:
            logging.getLogger("uvicorn.error").warning("ingest job reconcile failed: %s", e)

        # INDEX MAINTENANCE, after the SQL-only stages and BEFORE the syncs.
        # The ordering is load-bearing in both directions: this drops records
        # whose vectors died and clears the ingest fingerprints for their
        # sources, and the syncs below are what re-embed them - while
        # _sync_knowledge_dir snapshots the indexed-chunk counts at its TOP, so
        # a heal landing after that snapshot would be invisible for a whole
        # boot. Embed-free, and it rides the background task like every other
        # chroma-touching boot stage so a wedged read cannot sit in front of
        # the first health check. The summary is logged either way: a guard
        # that is silent when healthy is indistinguishable from one that is not
        # running at all.
        try:
            from app.chroma_maintenance import run_chroma_maintenance
            from app.ingest_sync import _clear_ingest_fingerprints
            maint = await asyncio.get_running_loop().run_in_executor(
                None, lambda: run_chroma_maintenance(_clear_ingest_fingerprints))
            log("chroma_maintenance", **maint)
        except Exception as e:
            log_error("chroma_maintenance_crashed", error=str(e))

        # EMBED-HEAVY syncs (knowledge + docs). These can run for many
        # minutes on a loaded box; _startup_ingest_active stays True across
        # them so an eval RUN can't measure a half-embedded corpus.
        try:
            # force=False: skip unchanged files (fingerprint + indexed-count
            # check) - a full re-embed of the whole corpus on every boot is
            # freeze-grade burst load. Manual /api/kb/sync still forces.
            res = await asyncio.get_running_loop().run_in_executor(None, lambda: _sync_knowledge_dir(force=False))
            _report_sync("knowledge", res)
        except Exception as e:
            log_error("startup_sync_crashed", stage="knowledge", error=str(e))
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, lambda: _sync_docs(force=False))
            _report_sync("docs", res)
        except Exception as e:
            log_error("startup_sync_crashed", stage="docs", error=str(e))
        # Live-system records LAST of the three ingest stages: the corpus record
        # reports source and chunk counts, and those are only true for this boot
        # once both file syncs have finished moving them. Still inside the
        # background task, so the startup-ingest window is open and an eval run
        # cannot measure a half-written record.
        try:
            from app.system_records import sync_system_records
            res = await asyncio.get_running_loop().run_in_executor(
                None, sync_system_records)
            _report_sync("system-records", res)
        except Exception as e:
            log_error("startup_sync_crashed", stage="system-records", error=str(e))
        # DEPARTMENT-LIST INVARIANT: report residue at every boot, not just
        # when someone happens to call the endpoint. An empty kb_* collection
        # here means some tool created it and cleaned up imperfectly -
        # list_departments() already excludes it BY CONSTRUCTION; this is what
        # keeps it from being invisible. The clean line is emitted too: a guard
        # silent when healthy is indistinguishable from one that is not
        # running. It rides the BACKGROUND task rather than an awaited startup
        # hook on purpose - every chroma-touching boot task here does, so a
        # wedged metadata read cannot sit in front of the first health check.
        try:
            from app.database import department_residue
            residue = await asyncio.get_running_loop().run_in_executor(
                None, department_residue)
            if residue:
                log_error("department_residue_found", departments=residue)
            else:
                log("department_residue_clean")
        except Exception as e:
            log_error("department_residue_check_crashed", error=str(e))

        runtime_config._startup_ingest_active = False
        asyncio.create_task(_watch_knowledge_dir())

    async def _bg_guarded():
        """The flag is armed before the task exists, so only a finally can be
        trusted to disarm it. _bg clears it on its own happy path; this covers
        the rest - an escape from outside the per-stage try blocks, or a
        cancellation. A stuck True flag is not cosmetic: the eval runner reads
        it to refuse a run mid-ingest, so it would block every eval until the
        process restarted, with nothing in the logs naming why."""
        try:
            await _bg()
        finally:
            runtime_config._startup_ingest_active = False

    asyncio.create_task(_bg_guarded())


@app.on_event("shutdown")
async def _flush_chroma_on_shutdown():
    """Graceful stops must not lose the un-flushed HNSW tail. chroma 0.5.23
    never persists on close (stop() only closes file handles), so this hook
    is the only thing between a clean SIGTERM and sub-threshold vector loss.
    Runs inside uvicorn's drain; compose stop_grace_period is sized so the
    kill cannot beat it. Synchronous on purpose - persist_dirty writes a few
    MB and the executor may already be winding down."""
    try:
        from app.database import flush_vector_segments
        res = flush_vector_segments()
        log("chroma_shutdown_flush", **res)
    except Exception as e:
        log_error("chroma_shutdown_flush_failed", error=str(e))
