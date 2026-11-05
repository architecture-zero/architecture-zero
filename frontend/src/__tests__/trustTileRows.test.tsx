/**
 * THE TILES NAME THE RUN THEIR ROWS COME FROM (2026-11-05). Since 2026-11-04 the
 * judged tiles (correctness, holdout, faithfulness, freshness) anchor on the newest
 * JUDGED run, so after a retrieval-only run their passed/scored are not the latest
 * run's - and the note said "rows in the latest run" all the same. A band's note now
 * says "the latest judged run"; the retrieval tile, which does read the latest run,
 * keeps "the latest run". Every judged-tile case fails on the code before this
 * change; the retrieval cases are the control (true before and after).
 */
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import TrustPanel from '../TrustPanel'
import { TrustTab } from '../admin/TrustTab'

const PANEL = {
  available: true,
  honesty: { pct: 100, n: 4, measured_at: '2026-11-05' },
  correctness: { low: 66.7, high: 100, runs: 2, passed: 2, scored: 3 },
  holdout: { low: 90, high: 90, runs: 1, passed: 9, scored: 10 },
  gap: { low: -16.5, high: -16.5, runs: 1 },
  faithfulness: { low: 100, high: 100, runs: 1, passed: 73, scored: 76 },
  freshness: { low: 100, high: 100, runs: 1, passed: null, scored: null },
  retrieval: { pct: 50, passed: 1, scored: 2 },
  measured_at: '2026-11-05',
  corpus_fingerprint_short: 'abcdef12',
  cross_family_judging: true,
}

function install() {
  vi.stubGlobal('fetch', vi.fn(async () =>
    new Response(JSON.stringify(PANEL), { status: 200, headers: { 'Content-Type': 'application/json' } })))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the public panel names the run behind each tile\'s rows', () => {
  it('a judged tile\'s rows are the latest judged run\'s', async () => {
    install()
    render(<TrustPanel />)
    expect(await screen.findByText(
      'graded against owner-written keys - band across 2 identical runs - 2 of 3 rows in the latest judged run',
    )).toBeInTheDocument()
    expect(screen.getByText(
      'questions the tuning never sees - the un-gameable score - 9 of 10 rows in the latest judged run',
    )).toBeInTheDocument()
    expect(screen.getByText(
      'claims must trace to retrieved documents, not memory - 73 of 76 rows in the latest judged run',
    )).toBeInTheDocument()
  })

  it('control: the retrieval tile reads the latest run itself and says so', async () => {
    install()
    render(<TrustPanel />)
    expect(await screen.findByText(
      'for questions with a known home document - 1 of 2 rows in the latest run')).toBeInTheDocument()
    // A tile with no counts keeps its own words alone.
    expect(screen.getByText('the material served is current - no stale copies')).toBeInTheDocument()
  })
})

describe('the admin tab reads the same way', () => {
  it('a judged tile\'s rows are the latest judged run\'s; retrieval\'s the latest run\'s', async () => {
    install()
    render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    expect(await screen.findByText(
      'tuned cohort, graded against owner-written keys - 2 of 3 rows in the latest judged run',
    )).toBeInTheDocument()
    expect(screen.getByText(
      'for questions with a known home document - 1 of 2 rows in the latest run')).toBeInTheDocument()
  })
})
