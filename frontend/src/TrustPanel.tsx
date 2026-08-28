import { useEffect, useState } from 'react'

// Public trust panel (B4 panel-derivations leg; mock confirmed 2026-07-31;
// overview upgrade - tiles + "how this is measured" - mock confirmed 2026-08-02).
// Every number arrives derived from stored eval runs via GET /api/trust -
// nothing here is hardcoded, and the band form (low-high across identical
// runs) is deliberate: a spread is more honest than a lucky point.

interface Band {
  low: number
  high: number
  runs: number
}

interface TrustData {
  available: boolean
  reason?: string
  honesty?: { pct: number; n: number; measured_at: string } | null
  correctness?: Band | null
  holdout?: Band | null
  gap?: Band | null
  faithfulness?: Band | null
  freshness?: Band | null
  retrieval?: { pct: number } | null
  measured_at?: string | null
  corpus_fingerprint_short?: string | null
  cross_family_judging?: boolean
}

// The gap is a difference in points, not a percentage; a positive value means
// the system scores higher on questions it could tune toward - the honest
// overfit number. Rendered signed so a negative (holdout ABOVE tuned) reads
// plainly too.
function gapText(b: Band | null | undefined): string {
  if (!b) return 'not yet measured'
  const fmt = (v: number) => `${v > 0 ? '+' : ''}${v}`
  if (b.low === b.high) return `${fmt(b.low)} pts`
  return `${fmt(b.low)} to ${fmt(b.high)} pts`
}

function bandText(b: Band | null | undefined): string {
  if (!b) return 'not yet measured'
  if (b.low === b.high) return `${b.low}%`
  return `${b.low}-${b.high}%`
}

function bandNote(b: Band | null | undefined, fallback: string): string {
  if (!b || b.runs < 2) return fallback
  return `${fallback} - band across ${b.runs} identical runs`
}

// A perfect score renders in the "good" tone; everything else stays neutral
// ink. The tone never replaces the label - color here is emphasis, not data.
function valueTone(perfect: boolean): string {
  return perfect ? 'text-emerald-400' : 'text-slate-100'
}

// Same resolution as every other screen. This file used to fetch a bare
// '/api/trust', which works only when the API shares an origin with the
// bundle - so the page broke for exactly the operators who split them, and
// broke alone, with the rest of the app fine.
const API = import.meta.env.VITE_API_URL || ''

export default function TrustPanel() {
  const [data, setData] = useState<TrustData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch(`${API}/api/trust`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [])

  const gapGood = !!data?.gap && data.gap.high <= 0

  // Honest caption: with one run at the current configuration, the tiles ARE
  // single points - say so instead of promising a spread that is not there yet.
  const bandN = Math.max(
    data?.correctness?.runs ?? 0,
    data?.holdout?.runs ?? 0,
    data?.faithfulness?.runs ?? 0,
    data?.freshness?.runs ?? 0,
  )

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex justify-center px-4 py-10">
      <div className="w-full max-w-3xl">
        <div className="flex items-baseline justify-between gap-4 mb-2">
          <h1 className="text-2xl font-semibold">
            How much should you trust this assistant?
          </h1>
          <a href="/" className="text-xs text-slate-400 hover:text-slate-200 whitespace-nowrap transition-colors">
            Try the assistant &rarr;
          </a>
        </div>
        <p className="text-slate-400 text-sm mb-8 max-w-2xl">
          These numbers are measured on this instance&apos;s own knowledge
          base, by an automated evaluation the assistant cannot see or tune
          to. Answers are written by one AI company&apos;s model and graded by
          a different company&apos;s model, so the system never grades itself.
        </p>

        {error && (
          <p className="text-red-400 text-sm">
            Could not load the measured numbers ({error}). They are real and
            derived live - try again shortly.
          </p>
        )}
        {!data && !error && <p className="text-slate-500 text-sm">Loading measured numbers...</p>}

        {data && !data.available && (
          <p className="text-slate-400 text-sm">
            No complete measured runs yet - numbers appear after the first
            full evaluation.
          </p>
        )}

        {data && data.available && (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2.5">
              {data.honesty && (
                <Tile
                  value={`${data.honesty.pct}%`}
                  tone={valueTone(data.honesty.pct === 100)}
                  label="Honesty under pressure"
                  note={`${data.honesty.n} demands for records that do not exist - refused or corrected, none invented`}
                />
              )}
              <Tile
                value={bandText(data.correctness)}
                tone={valueTone(false)}
                label="Answer correctness"
                note={bandNote(data.correctness, 'graded against owner-written keys')}
              />
              {data.holdout && (
                <Tile
                  value={bandText(data.holdout)}
                  tone={valueTone(false)}
                  label="Held-out exam"
                  note="questions the tuning never sees - the un-gameable score"
                />
              )}
              {data.gap && (
                <Tile
                  value={gapText(data.gap)}
                  tone={valueTone(false)}
                  label="Tuned-vs-holdout gap"
                  note={
                    gapGood
                      ? 'the locked exam scores HIGHER - published, not hidden'
                      : 'how much the tunable score flatters vs the locked exam - published, not hidden'
                  }
                  noteTone={gapGood ? 'text-emerald-400/90' : undefined}
                />
              )}
              <Tile
                value={bandText(data.faithfulness)}
                tone={valueTone(!!data.faithfulness && data.faithfulness.low === 100)}
                label="Claims grounded in sources"
                note={bandNote(data.faithfulness, 'claims must trace to retrieved documents, not memory')}
              />
              <Tile
                value={bandText(data.freshness)}
                tone={valueTone(false)}
                label="Source freshness"
                note={bandNote(data.freshness, 'the material served is current - no stale copies')}
              />
              {data.retrieval && (
                <Tile
                  value={`${data.retrieval.pct}%`}
                  tone={valueTone(false)}
                  label="Right document found"
                  note="for questions with a known home document"
                />
              )}
            </div>

            <h2 className="text-sm font-semibold text-slate-300 mt-10 mb-3 uppercase tracking-wide">
              How this is measured
            </h2>
            <div className="grid sm:grid-cols-2 gap-2.5">
              <HowCard
                title="The system never grades itself"
                body={"One company's model writes the answers; a different company's model grades them. "
                  + (data.cross_family_judging === false
                    ? 'On this instance the writer and grader are currently the SAME provider family, so these scores are self-graded - read them accordingly.'
                    : 'A guard in the code refuses same-family pairings.')}
              />
              <HowCard
                title="A locked exam it cannot study for"
                body="Part of the question set is held out of all tuning. The gap between the two scores is published above - even when it is ugly."
              />
              <HowCard
                title="Computed live, never typed in"
                body="Every number on this page is derived from stored evaluation runs at the moment you load it. There is no hand-edited scoreboard to go stale."
              />
              <HowCard
                title="Pinned to the exact knowledge base"
                body={`Each run is stamped with a fingerprint of the corpus it measured${
                  data.corpus_fingerprint_short ? ` (${data.corpus_fingerprint_short})` : ''
                }. A score is only comparable to runs on the same material - so that rule is enforced, not assumed.`}
              />
            </div>

            <p className="text-slate-500 text-xs mt-8">
              Last measured {data.measured_at}
              {data.corpus_fingerprint_short &&
                ` - knowledge base fingerprint ${data.corpus_fingerprint_short}`}
              .{' '}
              {bandN > 1
                ? 'Spreads are shown as low-high bands across identical runs, never one lucky point.'
                : 'These values are from a single run at the current configuration so far; repeat runs at the same configuration widen them into low-high bands rather than one lucky point.'}{' '}
              Every value on this page is computed from
              stored evaluation runs at request time; none of it is typed in
              by hand.
            </p>
          </>
        )}
      </div>
    </div>
  )
}

function Tile(props: {
  value: string
  tone: string
  label: string
  note: string
  noteTone?: string
}) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg px-4 py-3.5">
      <div className={`text-2xl font-semibold tabular-nums ${props.tone}`}>{props.value}</div>
      <div className="text-[13px] font-medium mt-0.5">{props.label}</div>
      <div className={`text-xs mt-1 leading-snug ${props.noteTone || 'text-slate-500'}`}>
        {props.note}
      </div>
    </div>
  )
}

function HowCard(props: { title: string; body: string }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-lg px-4 py-3.5">
      <div className="text-[13px] font-semibold mb-1">{props.title}</div>
      <div className="text-xs text-slate-400 leading-relaxed">{props.body}</div>
    </div>
  )
}
