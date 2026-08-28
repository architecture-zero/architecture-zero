"""Outside-model holdout authoring.

The tuned question set was written and refined by the same loop that tunes the
system - so its scores measure fit to the set, not generalization. This script
has an OUTSIDE model (a different provider family than the tuning loop) author
the locked holdout cohort: it reads corpus files and writes exam questions plus
grading keys for them, and its output is taken AS-IS.

Authorship-independence rules (the whole point - break one and the cohort is
just more tuned-set):
  - The authoring model NEVER sees the existing question set, so it cannot
    mimic the tuned phrasings or their blind spots.
  - Nobody edits its output. Validation is MECHANICAL only: JSON parses, the
    fields exist, exact-duplicate texts drop, and a file returning more than
    --per-file items is truncated to the first --per-file (models do not honor
    the requested count, and one over-productive document would otherwise
    dominate the exam). A broken item is DROPPED by rule and reported - never
    rewritten.
  - There are NO RETRIES. A retry is the first step toward coaxing better
    output, which is the independence break wearing a helpful face.
  - File selection spans the WHOLE corpus, endpoints included, as an even
    spread across the sorted list. See main() for the defect this replaced.
  - expected_source is set by THIS script (the file the model was shown), not
    trusted from the model - retrieval scoring needs it exact.
  - File selection is deterministic (sorted paths), not curated.

Output: a JSON array, each item {category, question, notes, expected_source,
holdout}. Review it and merge what survives into backend/eval-questions.json -
the seed file stays the source of truth for the question SET, and the boot sync
lands it in the database. This script only DRAFTS the cohort.

Default author is a model from a third provider family - different from both
the answer writer and the judge; it needs that provider's key configured. Any
registry-routable id works via --model. Run inside the backend container (the
corpus and the provider keys live there):

  docker compose exec backend python scripts/author_holdout.py --count 20
  docker compose exec backend cat /tmp/holdout-questions.json

Costs real API calls - one per sampled file. The run report also lands in
/tmp/author_holdout-last-report.txt inside the container.

Exit codes: 0 = drafted at least --count questions, 2 = fewer survived
validation (usable, but read the drop report), 1 = nothing usable.
"""
import argparse
import json
import math
import os
import re
import sys
import time

REPORT_PATH = "/tmp/author_holdout-last-report.txt"
AUTHOR_MODEL_DEFAULT = os.getenv("EVAL_HOLDOUT_AUTHOR_MODEL", "gemini-3.6-flash")
# A batch truncated mid-JSON is unparseable, and an unparseable batch is dropped
# WHOLE - one file's questions lost to a token ceiling. Env-overridable so a run
# against long documents can raise it without a code change.
AUTHOR_MAX_TOKENS = int(os.getenv("EVAL_HOLDOUT_AUTHOR_MAX_TOKENS", "1500"))
PAUSE = float(os.getenv("EVAL_QUESTION_PAUSE_SECONDS", "2.0"))
MAX_FILE_CHARS = 8000   # bounds tokens; the model only writes questions the
                        # shown excerpt can answer, so a cap cannot mislead it
MIN_FILE_BYTES = 500    # stubs can't ground a fair question


class _Tee:
    """Mirror stdout to REPORT_PATH so a broken pipe (dropped ssh / slept
    laptop) can't lose a completed run's numbers. The file write is the one
    that must survive; the console write is best-effort."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.console = sys.stdout

    def write(self, s):
        self.f.write(s)
        self.f.flush()
        try:
            self.console.write(s)
            self.console.flush()
        except Exception:
            pass  # pipe gone; the file still has everything

    def flush(self):
        self.f.flush()


_AUTHOR_SYSTEM = (
    "You write exam questions that test a personal AI assistant's knowledge "
    "base. You are shown ONE document from that knowledge base. Write "
    "questions that THIS document can answer.\n\n"
    "Rules:\n"
    "- Phrase each question the way a real user would ask it - natural "
    "wording, and do NOT copy sentences from the document verbatim.\n"
    "- Every question must be answerable from this document alone.\n"
    "- Prefer durable facts (numbers, names, dates, decisions, how things "
    "work) over transient status that will be stale next month.\n"
    "- For each question also write a grading key: one sentence stating what "
    "a correct answer must contain.\n"
    "- Give each question a one-word lowercase topic category.\n\n"
    "Reply with ONLY a JSON array, no code fences, no prose around it:\n"
    '[{"question": "...", "notes": "...", "category": "..."}]'
)


def _parse_items(raw: str) -> list[dict] | None:
    """Extract the JSON array from model output. Tolerates code fences and
    stray prose; returns None if unparseable.

    Deliberately does not repair: a model that could not emit the requested
    shape has not earned a guess at what it meant, and guessing is an edit.
    """
    text = (raw or "").strip()
    if not text:
        return None
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _eligible_files(corpus: str) -> list[str]:
    files = []
    for root, _dirs, names in os.walk(corpus):
        for name in sorted(names):
            if not name.lower().endswith(".md"):
                continue
            p = os.path.join(root, name)
            try:
                if os.path.getsize(p) >= MIN_FILE_BYTES:
                    files.append(p)
            except OSError:
                continue
    return sorted(files)


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.providers import stream_chat, _resolve_model  # noqa: E402


def _existing_question_texts(seed_path: str) -> set[str]:
    """Exact question texts already banked - the dedupe floor.

    Two legs on purpose. The SEED FILE is this platform's source of truth for
    the question set and is readable with no app environment at all, so it is
    primary. The DATABASE is the synced superset - it also holds anything added
    through the UI - so it is unioned in when reachable. Only a failure of BOTH
    is worth a warning, because either alone still gives a real floor.

    Exactness matters: the seed sync matches on the stripped question text, so
    a near-duplicate does not update a row, it becomes a second one.
    """
    texts: set[str] = set()
    seed_ok = db_ok = False
    try:
        with open(seed_path, encoding="utf-8") as f:
            texts |= {str(q.get("question", "")).strip() for q in json.load(f)}
        seed_ok = True
    except Exception as e:
        print(f"[warn] could not read the seed file at {seed_path} ({e})")
    try:
        from app.db import get_session
        from app.models import EvalQuestion
        with get_session() as db:
            texts |= {q.question.strip() for q in db.query(EvalQuestion).all()}
        db_ok = True
    except Exception as e:
        print(f"[warn] could not read the question database ({e})")
    if not seed_ok and not db_ok:
        print("[warn] no dedupe floor available - duplicates are caught only "
              "within this batch")
    texts.discard("")
    return texts


def _warn_if_same_family(author_model: str) -> None:
    """The author is the third leg of an independence claim nothing else checks.

    The platform already refuses a judge drawn from the answer writer's family.
    An author from either family makes the holdout a mirror of the thing it is
    supposed to be independent of - the cohort still LOOKS locked, and its gap
    number quietly stops meaning what the trust panel says it means.

    A warning, never a refusal: this script drafts, and a human decides what to
    merge. Refusing here would also make a single-provider evaluation
    impossible to even experiment with.
    """
    author = _resolve_model(author_model)[0]
    judge = _resolve_model(os.getenv("EVAL_JUDGE_MODEL", "claude-sonnet-4-6"))[0]
    writer = _resolve_model(os.getenv("DEFAULT_MODEL", "qwen3:8b"))[0]
    clash = [name for name, prov in (("judge", judge), ("answer writer", writer))
             if prov == author]
    if clash:
        print(f"[warn] the author model resolves to provider '{author}', the "
              f"same family as the {' and the '.join(clash)} - questions "
              "written by the family that answers or grades them are not an "
              "independent holdout")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", default=AUTHOR_MODEL_DEFAULT,
                    help="authoring model id (registry-routable; default "
                         f"{AUTHOR_MODEL_DEFAULT})")
    ap.add_argument("--count", type=int, default=20,
                    help="target number of holdout questions (default 20)")
    ap.add_argument("--per-file", type=int, default=2,
                    help="questions requested per sampled file (default 2)")
    ap.add_argument("--corpus", default=os.getenv("KNOWLEDGE_DIR", "/knowledge"),
                    help="corpus root to sample. Paths relative to THIS root "
                         "become expected_source, so it must be the root the "
                         "ingest keys sources by")
    ap.add_argument("--seed", default=os.getenv("EVAL_SEED_PATH",
                                                "/app/eval-questions.json"),
                    help="question seed file, read as the dedupe floor")
    ap.add_argument("--out", default="/tmp/holdout-questions.json",
                    help="output path for the drafted JSON array")
    ap.add_argument("--pause", type=float, default=PAUSE,
                    help="seconds between authoring calls")
    args = ap.parse_args()

    files = _eligible_files(args.corpus)
    if not files:
        print(f"No eligible .md files under {args.corpus}")
        return 1
    n_files = max(1, math.ceil(args.count / args.per_file))
    # Spread the picks across the WHOLE sorted list, endpoints included.
    #
    # This replaces `files[::stride][:n_files]`, which truncated: stride was
    # len(files)//n_files, so a LARGER --count shrank the stride and the
    # [:n_files] slice stopped EARLIER in the list - asking for more questions
    # sampled LESS of the corpus, and a tail of files was unreachable at every
    # count. The holdout is the one cohort whose whole job is to be un-gameable,
    # and an exam that cannot reach part of the syllabus measures the wrong
    # thing however many questions it asks.
    picked = ([files[0]] if n_files == 1 else
              [files[round(i * (len(files) - 1) / (n_files - 1))]
               for i in range(min(n_files, len(files)))])
    print(f"=== HOLDOUT authoring: {len(picked)}/{len(files)} files, "
          f"{args.per_file}/file, target {args.count}, author = '{args.model}' ===")
    _warn_if_same_family(args.model)

    existing = _existing_question_texts(args.seed)
    seen: set[str] = set(existing)
    items: list[dict] = []
    dropped: list[str] = []
    for i, path in enumerate(picked, 1):
        rel = os.path.relpath(path, args.corpus).replace(os.sep, "/")
        try:
            content = open(path, encoding="utf-8", errors="ignore").read()
        except OSError as e:
            dropped.append(f"{rel}: unreadable ({e})")
            continue
        clipped = content[:MAX_FILE_CHARS]
        user = (f"DOCUMENT ({rel}):\n{clipped}\n\n"
                f"Write exactly {args.per_file} questions per the rules.")
        # The system prompt rides BOTH slots on purpose: some providers take a
        # separate system field and ignore a system role in messages, others
        # only read the messages. Dropping either one silently un-prompts a
        # provider rather than failing.
        msgs = [{"role": "system", "content": _AUTHOR_SYSTEM},
                {"role": "user", "content": user}]
        try:
            tokens = []
            for t in stream_chat(msgs, args.model, system_prompt=_AUTHOR_SYSTEM,
                                 max_tokens=AUTHOR_MAX_TOKENS):
                tokens.append(t)
            raw = "".join(tokens)
        except Exception as e:
            dropped.append(f"{rel}: author call failed ({e})")
            time.sleep(args.pause)
            continue
        batch = _parse_items(raw)
        if batch is None:
            dropped.append(f"{rel}: unparseable output ({raw[:120]!r})")
            time.sleep(args.pause)
            continue
        # Enforce --per-file MECHANICALLY. The prompt asks for exactly N, but
        # the request is not binding - models routinely return several times the
        # requested count for one document, and a single over-productive file
        # would then dominate the exam. Truncation is a DROP by rule, the same
        # contract as a malformed item, never a rewrite, so independence holds.
        if len(batch) > args.per_file:
            dropped.append(f"{rel}: author returned {len(batch)} items for a "
                           f"{args.per_file}-question request - kept the first "
                           f"{args.per_file} BY RULE (even per-file weight)")
            batch = batch[:args.per_file]
        kept_here = 0
        for item in batch:
            q = str((item or {}).get("question") or "").strip() if isinstance(item, dict) else ""
            notes = str((item or {}).get("notes") or "").strip() if isinstance(item, dict) else ""
            cat = str((item or {}).get("category") or "").strip().lower().split()
            if not q or not notes:
                dropped.append(f"{rel}: item missing question/notes")
                continue
            if q in seen:
                dropped.append(f"{rel}: duplicate question text ({q[:60]!r})")
                continue
            seen.add(q)
            # Key order and value types match the seed file, so a merged item is
            # indistinguishable from a hand-written one. holdout is the integer
            # the seed uses, and expected_source is the bare relative path - the
            # scorer strips unknown prefixes per needle, so a wrong form here
            # would score anyway and the convention break would be invisible.
            items.append({
                "category": (cat[0] if cat else "general"),
                "question": q,
                "notes": notes,
                "expected_source": rel,
                "holdout": 1,
            })
            kept_here += 1
        print(f"  [{i:>2}/{len(picked)}] {rel}: kept {kept_here}/{len(batch)}")
        time.sleep(args.pause)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    print(f"\n=== drafted {len(items)} questions (target {args.count}) -> {args.out} ===")
    if dropped:
        print(f"dropped {len(dropped)} item(s) BY RULE (never rewritten):")
        for d in dropped:
            print(f"  - {d}")
    if not items:
        return 1
    return 0 if len(items) >= args.count else 2


if __name__ == "__main__":
    try:
        sys.stdout = _Tee(REPORT_PATH)
    except Exception:
        pass  # unwritable path (e.g. local dev) - console only
    sys.exit(main())
