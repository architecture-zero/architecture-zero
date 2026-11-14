/**
 * EVERY LABEL NAMES THE RUN ITS VALUE CAME FROM. The judged tiles read the newest
 * JUDGED run, retrieval the newest run, honesty the newest run with honesty rows.
 * The caption's one fingerprint was the newest run's, beside the judged tiles' day,
 * and the honesty tile named no day at all. Where the judged run and the newest run
 * were measured on different corpora both pages name each run's fingerprint beside
 * its own day (and the "pinned to the exact knowledge base" card names both); where
 * honesty's day differs from the judged tiles', its tile names it. The two-corpus
 * and honesty-day cases fail on the code before this change; one run, and two runs
 * on one corpus, are the control - one date and one fingerprint.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import TrustPanel from '../TrustPanel'
import { TrustTab } from '../admin/TrustTab'

const ONE_RUN = {
  available: true,
  honesty: { pct: 100, n: 4, measured_at: '2025-01-02' },
  correctness: { low: 66.7, high: 66.7, runs: 1, passed: 2, scored: 3 },
  holdout: null,
  gap: null,
  faithfulness: { low: 100, high: 100, runs: 1, passed: 3, scored: 3 },
  freshness: { low: 100, high: 100, runs: 1, passed: 3, scored: 3 },
  retrieval: { pct: 75, passed: 3, scored: 4 },
  measured_at: '2025-01-02',
  judged_measured_at: '2025-01-02',
  corpus_fingerprint_short: 'abcdef12',
  judged_fingerprint_short: 'abcdef12',
  cross_family_judging: true,
}
// A retrieval-only run a day later, on a changed corpus.
const TWO_CORPORA = { ...ONE_RUN, measured_at: '2025-01-03', corpus_fingerprint_short: '01234567' }
// The same, measured the same day: the dates agree, the corpora do not.
const SAME_DAY_TWO_CORPORA = { ...ONE_RUN, corpus_fingerprint_short: '01234567' }
// A retrieval-only run a day later on the same corpus: two dates, one fingerprint.
const TWO_DAYS_ONE_CORPUS = { ...ONE_RUN, measured_at: '2025-01-03' }
// The newest judged run carried no honesty rows: honesty is an older run's.
const OLDER_HONESTY = { ...ONE_RUN, honesty: { pct: 100, n: 4, measured_at: '2025-01-01' } }

const HONESTY = '4 demands for records that do not exist - refused or corrected, none invented'

function install(panel: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () =>
    new Response(JSON.stringify(panel), { status: 200, headers: { 'Content-Type': 'application/json' } })))
}

// An element whose own text (no child elements) holds `text`: the caption
// paragraph and the card body are each one run of text nodes.
function textOf(text: string) {
  return screen.findByText((_, el) => !!el && el.children.length === 0
    && (el.textContent ?? '').includes(text))
}

function renderTab() {
  return render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('two corpora, each named beside its own run', () => {
  it('the public caption names each run\'s day and fingerprint', async () => {
    install(TWO_CORPORA)
    render(<TrustPanel />)
    expect(await textOf('Judged tiles measured 2025-01-02 on knowledge base fingerprint abcdef12; '
      + 'retrieval measured 2025-01-03 on fingerprint 01234567')).toBeInTheDocument()
    expect(await textOf('(judged tiles abcdef12; retrieval 01234567)')).toBeInTheDocument()
  })

  it('the same when both runs fall on one day', async () => {
    install(SAME_DAY_TWO_CORPORA)
    render(<TrustPanel />)
    expect(await textOf('Judged tiles measured 2025-01-02 on knowledge base fingerprint abcdef12; '
      + 'retrieval measured 2025-01-02 on fingerprint 01234567')).toBeInTheDocument()
  })

  it('the admin caption says the same', async () => {
    install(TWO_CORPORA)
    renderTab()
    expect(await textOf('Judged tiles measured 2025-01-02 on knowledge base fingerprint abcdef12; '
      + 'retrieval measured 2025-01-03 on fingerprint 01234567')).toBeInTheDocument()
  })
})

describe('the honesty tile names its own day where it differs', () => {
  it('on the public page', async () => {
    install(OLDER_HONESTY)
    render(<TrustPanel />)
    expect(await screen.findByText(`${HONESTY} - measured 2025-01-01`)).toBeInTheDocument()
  })

  it('on the admin tab', async () => {
    install(OLDER_HONESTY)
    renderTab()
    expect(await screen.findByText(`${HONESTY} - measured 2025-01-01`)).toBeInTheDocument()
  })
})

describe('control: one corpus, one fingerprint; honesty on the tiles\' day, no date', () => {
  it('one run on the public page', async () => {
    install(ONE_RUN)
    render(<TrustPanel />)
    expect(await textOf('Last measured 2025-01-02 - knowledge base fingerprint abcdef12.')).toBeInTheDocument()
    expect(await textOf('it measured (abcdef12).')).toBeInTheDocument()
    expect(screen.getByText(HONESTY)).toBeInTheDocument()
    expect(screen.getByText('66.7%')).toBeInTheDocument()
  })

  it('two days on one corpus on the public page', async () => {
    install(TWO_DAYS_ONE_CORPUS)
    render(<TrustPanel />)
    expect(await textOf('Judged tiles measured 2025-01-02; retrieval measured 2025-01-03 '
      + '- knowledge base fingerprint abcdef12.')).toBeInTheDocument()
    expect(screen.getByText(HONESTY)).toBeInTheDocument()
  })

  it('one run on the admin tab', async () => {
    install(ONE_RUN)
    renderTab()
    expect(await textOf('Last measured 2025-01-02')).toBeInTheDocument()
    expect(screen.getByText(HONESTY)).toBeInTheDocument()
    expect(screen.getByText('66.7%')).toBeInTheDocument()
  })
})
