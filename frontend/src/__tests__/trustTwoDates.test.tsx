/**
 * THE PAGES NAME THE DAY EACH KIND OF TILE WAS MEASURED. The judged tiles read the
 * newest JUDGED run and retrieval reads the newest run; the panel printed one
 * "Last measured" date, the newest run's, so after a retrieval-only run it named a
 * day the judged numbers were not measured on. Where the API's two dates differ both
 * pages print both; where they agree, or no judged date comes, one. The two-date
 * cases fail on the code before this change; the one-date cases are the control.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import TrustPanel from '../TrustPanel'
import { TrustTab } from '../admin/TrustTab'

const BASE = {
  available: true,
  honesty: { pct: 100, n: 4, measured_at: '2025-01-01' },
  correctness: { low: 66.7, high: 66.7, runs: 1, passed: 2, scored: 3 },
  holdout: null,
  gap: null,
  faithfulness: { low: 100, high: 100, runs: 1, passed: 3, scored: 3 },
  freshness: { low: 100, high: 100, runs: 1, passed: 3, scored: 3 },
  retrieval: { pct: 75, passed: 3, scored: 4 },
  measured_at: '2025-01-02',
  corpus_fingerprint_short: 'abcdef12',
  cross_family_judging: true,
}

function install(panel: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () =>
    new Response(JSON.stringify(panel), { status: 200, headers: { 'Content-Type': 'application/json' } })))
}

// The caption is one paragraph built from several text nodes.
function caption(text: string) {
  return screen.findByText((_, el) => el?.tagName === 'P' && (el.textContent ?? '').includes(text))
}

const TWO = 'Judged tiles measured 2025-01-01; retrieval measured 2025-01-02'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('two runs, two dates', () => {
  it('the public panel names both days', async () => {
    install({ ...BASE, judged_measured_at: '2025-01-01' })
    render(<TrustPanel />)
    expect(await caption(TWO)).toBeInTheDocument()
    expect(screen.queryByText((_, el) => el?.tagName === 'P'
      && (el.textContent ?? '').includes('Last measured'))).toBeNull()
  })

  it('the admin tab names both days', async () => {
    install({ ...BASE, judged_measured_at: '2025-01-01' })
    render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    expect(await caption(TWO)).toBeInTheDocument()
  })
})

describe('control: one run, one date', () => {
  for (const judged of ['2025-01-02', null, undefined]) {
    it(`the public panel prints one date (judged ${String(judged)})`, async () => {
      install(judged === undefined ? BASE : { ...BASE, judged_measured_at: judged })
      render(<TrustPanel />)
      expect(await caption('Last measured 2025-01-02')).toBeInTheDocument()
      expect(screen.queryByText((_, el) => el?.tagName === 'P'
        && (el.textContent ?? '').includes('Judged tiles measured'))).toBeNull()
    })

    it(`the admin tab prints one date (judged ${String(judged)})`, async () => {
      install(judged === undefined ? BASE : { ...BASE, judged_measured_at: judged })
      render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
      expect(await caption('Last measured 2025-01-02')).toBeInTheDocument()
    })
  }
})
