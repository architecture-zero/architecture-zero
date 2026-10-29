import { useState, useEffect } from 'react'
import { actionError, emitError, guardedPoll } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'

// ── Tab: Trust ─────────────────────────────────────────────────────────────

// passed/scored are the LATEST run's rows behind the rate (2026-10-28): `runs`
// counts runs, so a rate without its denominator read the same over 3 graded
// rows as over 300. The gap carries none - a difference of two rates that
// each carry their own. The retrieval tile, a point from the latest run
// rather than a band, carries the same two (also 2026-10-28) - it was the
// one bare rate left on the panel.
interface TrustCounts { passed?: number | null; scored?: number | null }
interface TrustBand extends TrustCounts { low: number; high: number; runs: number }

interface AdminTrustData {
  available: boolean
  reason?: string
  honesty?: { pct: number; n: number; measured_at: string } | null
  correctness?: TrustBand | null
  holdout?: TrustBand | null
  gap?: TrustBand | null
  faithfulness?: TrustBand | null
  freshness?: TrustBand | null
  retrieval?: ({ pct: number } & TrustCounts) | null
  measured_at?: string | null
  corpus_fingerprint_short?: string | null
  cross_family_judging?: boolean
  provenance?: {
    writer: string | null
    judge: string
    corpus_fingerprint: string | null
    band_run_ids: string[]
    question_set: { total: number; honesty: number }
  }
  instrument?: { hybrid_persona_grounding: boolean; judge_input_boundary: boolean }
}

function trustBandText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  if (b.low === b.high) return `${b.low}%`
  return `${b.low}-${b.high}%`
}

// The rows behind the latest run's rate, in plain words - the same line the
// public panel shows. Empty for a tile that carries no counts (the gap).
function trustRowsNote(b: TrustCounts | null | undefined): string {
  if (!b || b.passed == null || b.scored == null) return ''
  return ` - ${b.passed} of ${b.scored} rows in the latest run`
}

function trustGapText(b: TrustBand | null | undefined): string {
  if (!b) return 'not yet measured'
  const f = (v: number) => `${v > 0 ? '+' : ''}${v}`
  if (b.low === b.high) return `${f(b.low)} pts`
  return `${f(b.low)} to ${f(b.high)} pts`
}

function TrustTile(props: { value: string; label: string; note: string; good?: boolean }) {
  return (
    <div className="bg-gray-800/40 border border-gray-700/50 rounded-xl px-4 py-3.5">
      <div className={`text-2xl font-semibold tabular-nums ${props.good ? 'text-emerald-400' : 'text-white'}`}>
        {props.value}
      </div>
      <div className="text-[13px] font-medium mt-0.5 text-gray-200">{props.label}</div>
      <div className="text-xs mt-1 leading-snug text-gray-500">{props.note}</div>
    </div>
  )
}

export function TrustTab({ api, headers, currentUser }: {
  api: string
  headers: () => Record<string, string>
  currentUser: { role: string } | null
}) {
  const [data, setData] = useState<AdminTrustData | null>(null)
  const [error, setError] = useState('')
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState('')

  const load = () => {
    fetch(`${api}/api/admin/trust`, { headers: headers() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setData).catch(e => setError(String(e)))
  }
  useEffect(load, [])

  // The first measurement, from the tab that displays it. A retrieval-only
  // pass on purpose: it needs no model calls, so it costs nothing and cannot
  // fail on a missing provider key, and it still produces a real stored run.
  // Answer-mode passes are the API's job (POST /api/admin/evals/run with
  // retrieval_only=false) - this is the floor, not the whole instrument.
  const runFirstMeasurement = async () => {
    setRunning(true)
    setProgress('Seeding the question set...')
    try {
      // /sync, NOT /seed. Seeding REFUSES with 409 once any question exists,
      // and the boot hook reconciles the shipped set on every start - so the
      // question set is always already there and /seed always failed, taking
      // the run with it. /sync is the idempotent form of the same operation.
      const synced = await actionError(
        fetch(`${api}/api/admin/evals/questions/sync`, { method: 'POST', headers: headers() }),
        'Preparing the question set')
      // actionError returns the text and shows nothing itself (2026-10-29: a
      // refused sync ended the run with the button back and no word why).
      if (synced) { emitError(synced); return }
      setProgress('Running retrieval...')
      const started = await fetch(`${api}/api/admin/evals/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify({ retrieval_only: true }),
      })
      const run = await started.json().catch(() => ({}))
      if (!started.ok) { emitError(run.detail || 'Could not start the run'); return }
      // The run is a background thread with a status endpoint; poll it rather
      // than guessing how long a corpus takes.
      // THIS LOOP HAS THREE EXITS AND ALL THREE MUST SPEAK. Only the
      // complete-and-failed one did, so two of the three ways a run can end
      // still presented it as a normal finish - which is the exact shape the
      // fix was supposed to cure.
      let ended = false
      for (let i = 0; i < 600; i++) {
        await new Promise(r => setTimeout(r, 1000))
        const st = await guardedPoll<{ done?: number; total?: number; complete?: boolean;
                                       failed?: boolean; error?: string; known?: boolean }>(
          fetch(`${api}/api/admin/evals/run-status/${run.run_id}`, { headers: headers() }))
        if (!st) {
          // guardedPoll returns null on ANY non-2xx and on any thrown error,
          // silently, because its contract is "the next tick retries". Here
          // there is no next tick - this break ends the poll for good. A single
          // transient failure therefore looked identical to a finished run.
          emitError('Lost contact with the eval run - it may still be going. Reload to check.')
          ended = true
          break
        }
        if (st.known === false) {
          // The backend has never heard of this run id. _eval_runs is an
          // in-process dict, so this means the backend restarted and the run
          // died with it - previously indistinguishable from "just started",
          // which is what made the operator wait out the full ten minutes.
          emitError('The eval run is gone - the backend restarted while it was '
            + 'running. Start it again.')
          ended = true
          break
        }
        setProgress(`Running retrieval... ${st.done ?? 0} / ${st.total ?? '?'}`)
        if (st.complete) {
          // complete=true is set from the runner's FINALLY, so it means
          // "terminal", not "succeeded" - `failed` is what tells the two apart.
          // Reading only complete made a crashed run present as a normal finish,
          // and the operator then went looking for results that were never
          // written, with the reason sitting unread in this same payload.
          if (st.failed) emitError(`The eval run failed: ${st.error || 'no reason reported'}`)
          ended = true
          break
        }
      }
      if (!ended) {
        // Ten minutes elapsed with the run neither complete nor failed. The run
        // registry is an in-process dict, so a backend restart mid-run answers
        // this way forever - the loop would otherwise just stop with no word.
        emitError('The eval run did not report finishing within 10 minutes. '
          + 'If the backend restarted, the run is gone and needs starting again.')
      }
      setProgress('')
      load()
    } finally {
      setRunning(false)
      setProgress('')
    }
  }

  if (error) return <p className="text-sm text-red-400">Could not load the trust panel ({error}).</p>
  if (!data) return <p className="text-sm text-gray-500">Loading measured numbers...</p>
  if (!data.available) {
    return (
      <div className="max-w-2xl space-y-4">
        <p className="text-sm text-gray-400">
          No complete measured runs yet. Every number on this page is derived
          from stored evaluation runs at request time - so until one has been
          run, there is honestly nothing to show.
        </p>
        <p className="text-sm text-gray-500">
          A retrieval-only pass is the cheapest place to start: it makes no
          model calls, needs no provider key, and answers the first question
          worth asking - does retrieval surface the right documents?
        </p>
        {/* Every endpoint this calls is Owner-only, while the tab itself
            opens for view_analytics. Offer the control to someone who can use
            it rather than a button that answers 403. */}
        {currentUser?.role === 'owner' ? (
          <button
            onClick={runFirstMeasurement}
            disabled={running}
            className="text-sm px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-40 transition-colors"
          >
            {running ? (progress || 'Working...') : 'Run the first measurement'}
          </button>
        ) : (
          <p className="text-sm text-gray-600">
            Starting a measurement is an Owner action. Ask the Owner to run one,
            or POST /api/admin/evals/run with an Owner token.
          </p>
        )}
        {running && (
          <p className="text-xs text-gray-600">
            This seeds the shipped question set and runs it. Leaving this tab
            does not stop it - the run continues server-side.
          </p>
        )}
      </div>
    )
  }

  const gapGood = !!data.gap && data.gap.high <= 0

  // Same honesty rule as the public panel: a band of one run is a point, not
  // a spread - the caption must not promise more than the data holds.
  const bandN = Math.max(
    data.correctness?.runs ?? 0,
    data.holdout?.runs ?? 0,
    data.faithfulness?.runs ?? 0,
    data.freshness?.runs ?? 0,
  )

  return (
    <div className="space-y-8 max-w-4xl">
      <div>
        <p className="text-sm text-gray-400 mb-4 max-w-2xl">
          Every number below is derived from stored evaluation runs at request
          time - nothing typed in. Answers are written by one company&apos;s
          model and graded by a different company&apos;s model.{' '}
          <a href="/#trust" target="_blank" rel="noopener noreferrer"
            className="underline hover:text-white" style={{ color: PRIMARY_COLOR }}>
            Open the public trust page
          </a>
        </p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
          {data.honesty && (
            <TrustTile value={`${data.honesty.pct}%`} good={data.honesty.pct === 100}
              label="Honesty under pressure"
              note={`${data.honesty.n} demands for records that do not exist - refused or corrected, none invented`} />
          )}
          <TrustTile value={trustBandText(data.correctness)}
            label="Answer correctness"
            note={`tuned cohort, graded against owner-written keys${trustRowsNote(data.correctness)}`} />
          {data.holdout && (
            <TrustTile value={trustBandText(data.holdout)}
              label="Held-out exam"
              note={`questions the tuning never sees${trustRowsNote(data.holdout)}`} />
          )}
          {data.gap && (
            <TrustTile value={trustGapText(data.gap)} good={gapGood}
              label="Tuned-vs-holdout gap"
              note={gapGood ? 'the locked exam scores HIGHER - published, not hidden'
                            : 'how much the tunable score flatters vs the locked exam'} />
          )}
          <TrustTile value={trustBandText(data.faithfulness)}
            good={!!data.faithfulness && data.faithfulness.low === 100}
            label="Claims grounded in sources"
            note={`claims must trace to retrieved documents${trustRowsNote(data.faithfulness)}`} />
          <TrustTile value={trustBandText(data.freshness)}
            label="Source freshness"
            note={`the material served is current${trustRowsNote(data.freshness)}`} />
          {data.retrieval && (
            <TrustTile value={`${data.retrieval.pct}%`}
              label="Right document found"
              note={`for questions with a known home document${trustRowsNote(data.retrieval)}`} />
          )}
        </div>
        <p className="text-xs text-gray-500 mt-3">
          Last measured {data.measured_at}.{' '}
          {bandN > 1
            ? 'Spreads are low-high bands across identical runs, never one lucky point.'
            : 'Values are from a single run at the current configuration so far; repeat runs at the same configuration widen them into low-high bands rather than one lucky point.'}
        </p>
      </div>

      {data.provenance && (
        <section className="bg-gray-800/30 border border-gray-700/50 rounded-xl p-5">
          <h2 className="text-sm font-semibold text-gray-200 mb-3">Provenance (operator view)</h2>
          <dl className="text-sm space-y-2">
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Answers written by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.writer || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Graded by</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5">{data.provenance.judge || 'unknown'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Corpus fingerprint</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.corpus_fingerprint || 'unstamped'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Band run ids</dt>
              <dd className="text-gray-300 font-mono text-xs pt-0.5 break-all">{data.provenance.band_run_ids.map(id => id.slice(0, 8)).join(', ') || 'none'}</dd></div>
            <div className="flex gap-3"><dt className="w-40 text-gray-500 flex-shrink-0">Question set</dt>
              <dd className="text-gray-300 text-xs pt-0.5">{data.provenance.question_set.total} questions ({data.provenance.question_set.honesty} honesty traps)</dd></div>
          </dl>
          {data.instrument && (
            <p className="text-xs text-gray-500 mt-3">
              Instrument: hybrid persona-grounding rubric {data.instrument.hybrid_persona_grounding ? 'on' : 'off'} -
              judge input boundary {data.instrument.judge_input_boundary ? 'on' : 'off'} -
              cross-family judging {data.cross_family_judging ? 'enforced' : 'off'}.
            </p>
          )}
        </section>
      )}
    </div>
  )
}
