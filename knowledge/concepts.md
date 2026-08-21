# What the numbers mean

## Why measurement is the product

Anyone can claim their assistant answers well. This platform's stance is
that a claim without a number is a vibe: answer quality, groundedness,
freshness, honesty, and retrieval recall are all measured by the instance
itself, on its own corpus, and published on its own trust panel. Just as
important, the measurement system is built to keep itself honest - pinned
instruments, held-out questions, and aggregates that refuse to blend.

## The four judges

A full evaluation run scores each answer on separate rubrics, each with its
own judge pass:
- Correctness: is the answer right, judged against the question's grading
  key written by the owner?
- Faithfulness: does every material claim TRACE to the material the model
  was actually shown? An answer can be right by luck and still unfaithful -
  that is the hallucination class this isolates.
- Freshness: is the retrieved material itself CURRENT, or a stale copy?
  This grades the corpus, not the model - when freshness fails while
  faithfulness passes, the fix belongs in your documents.
- Honesty: facing a demand for artifacts the corpus does not hold, did the
  assistant disclose and refuse cleanly, or fabricate specifics?

## Why the judge is pinned, and never the writer's family

The judge model is fixed in config, independent of the model being tested:
a measurement instrument must not change when the thing it measures does.
And the judge may never come from the same provider family as the answer
writer - self-preference bias puts a thumb on every score - so the system
refuses a same-family run unless deliberately overridden.

## Tuned vs holdout, and the GAP

Some evaluation questions are a locked HOLDOUT: authored independently and
never used to tune the system. Every run reports tuned score, holdout
score, and the GAP between them. A rising tuned score with a flat holdout
means the system is being fit to its own exam, not getting better - the
GAP is the overfitting alarm. Holdout rows report only as an aggregate:
their per-question diagnostics are withheld, because a fix aimed at a
holdout miss would quietly un-lock the holdout.

## Cohorts that never blend

The honesty cohort and the injection cohort each report ONLY as their own
number, never inside the correctness headline. They grade different rubrics
- blending them would move the headline the moment a cohort was added and
break comparability with every earlier run. One blended percentage is how
eval numbers lie; this system reports several honest ones.

## The injection-resistance number

Every full run plants a deliberately poisoned document into the live
general collection, asks the injection cohort's questions, grades the
answers MECHANICALLY (did the exfiltration URL appear, did the injected
persona take hold - deterministic string checks, no judge in the loop),
then deletes the plant and verifies zero residue. The run also reports
whether the poison actually reached the model's context - a pass where the
attack never arrived is vacuous, and the number says so.

## Recall, and the Knowledge Gaps list

Retrieval recall asks: for questions whose answer lives in a known
document, did retrieval surface that document at all? Misses land in the
Knowledge Gaps list with what came back instead - the single most
actionable evaluation output, since a retrieval gap is usually fixable
with better document structure or wording. Recall is measured
retrieval-only, no model calls, so it is cheap to run often.

## The corpus fingerprint

An evaluation score is a property of three things at once: the system, the
question set, AND the corpus it ran against. Every run stamps a compact
fingerprint of the corpus state, so two runs that measured different
corpora can never be silently compared. The fingerprint does not make them
comparable - nothing can, after the fact - it makes incomparability
visible.

## Noise bands: when is a change real?

Repeat an identical-configuration answer run and the score moves anyway -
models are nondeterministic. The spread across repeated identical runs is
the instance's noise band, and a change smaller than the band is not a
claim. The harness reports the band directly, and separates raw scores
(what a user experienced, provider outages included) from error-adjusted
scores (the model term alone) - two numbers, never blended.

## The trust panel

GET /api/trust renders all of the above for visitors: bands not points,
per-corpus provenance, honesty separated, zero hand-set values - every
number derived live from stored evaluation rows at request time. If a
number is on the panel, a run produced it; if no qualifying run exists,
the panel honestly shows nothing rather than a placeholder.
