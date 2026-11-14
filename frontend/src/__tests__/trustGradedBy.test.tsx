/**
 * "GRADED BY" IS THE JUDGED RUN'S OWN JUDGE. The provenance now names the judge
 * the judged run recorded, and null when the run recorded none. A null used to read
 * "unknown"; it reads "not recorded" - the run kept no record, and the tab never puts
 * today's setting in its place. The null case fails on the code before this change;
 * a recorded judge, shown as recorded, is the control.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { TrustTab } from '../admin/TrustTab'

function panel(judge: string | null) {
  return {
    available: true,
    honesty: null,
    correctness: { low: 66.7, high: 66.7, runs: 1, passed: 2, scored: 3 },
    holdout: null,
    gap: null,
    faithfulness: null,
    freshness: null,
    retrieval: null,
    measured_at: '2025-01-02',
    judged_measured_at: '2025-01-02',
    corpus_fingerprint_short: 'abcdef12',
    judged_fingerprint_short: 'abcdef12',
    cross_family_judging: true,
    provenance: {
      writer: 'writer-model',
      judge,
      corpus_fingerprint: 'src=2;chunks=9;sha=abcdef123456',
      band_run_ids: ['11111111-aaaa'],
      question_set: { total: 5, honesty: 0 },
    },
  }
}

function install(body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () =>
    new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })))
}

// The value beside the "Graded by" label.
async function gradedBy() {
  const label = await screen.findByText('Graded by')
  return label.nextElementSibling?.textContent
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Graded by', () => {
  it('a run that recorded no judge says not recorded', async () => {
    install(panel(null))
    render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    expect(await gradedBy()).toBe('not recorded')
  })

  it('control: a recorded judge reads as recorded', async () => {
    install(panel('judge-a'))
    render(<TrustTab api="" headers={() => ({})} currentUser={{ role: 'owner' }} />)
    expect(await gradedBy()).toBe('judge-a')
  })
})
