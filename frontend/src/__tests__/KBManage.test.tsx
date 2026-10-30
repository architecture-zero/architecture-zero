/**
 * THE KB PAGE'S REMOVE READS THE DELETE'S STATUS (2026-10-29).
 *
 * `remove` awaited the DELETE and reloaded the list without reading the
 * answer, so a refused remove (403 for a user without manage_kb, a 500 from
 * the index) showed the operator nothing but the same list repainted - the
 * row stays and nothing says why. A failed DELETE now goes through
 * actionError, the guard every admin mutation uses, and is toasted on the
 * page's error surface; the list reloads either way. A 2xx is unchanged:
 * the list reloads and nothing is shown.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { KBManage } from '../admin/KBManage'
import { ErrorSurface } from '../errorSurface'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = globalThis.fetch

interface Calls { sourceLoads: number; deletes: string[] }

// The first sources read lists `source`; every read after the DELETE lists
// what is left (`after`), so a test can tell the reload happened.
function installFetch(source: string, del: () => Response, after: string[]): Calls {
  const calls: Calls = { sourceLoads: 0, deletes: [] }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    if (method === 'DELETE' && url.includes('/api/ingest/source/')) {
      calls.deletes.push(url)
      return del()
    }
    if (url.includes('/api/ingest/departments')) return json({ departments: ['general'] })
    if (url.includes('/api/ingest/sources')) {
      calls.sourceLoads += 1
      const names = calls.deletes.length ? after : [source]
      return json({ sources: names.map(s => ({ source: s, count: 3, department: 'general' })) })
    }
    return json({})
  }) as unknown as typeof fetch
  return calls
}

function mount() {
  render(
    <>
      <ErrorSurface />
      <KBManage api="" headers={() => ({})} />
    </>,
  )
}

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

describe('KB page: removing a source', () => {
  it('a DELETE refused with 403 is shown to the operator, and the list reloads', async () => {
    const calls = installFetch(
      'denied.md', () => json({ detail: 'Permission required: manage_kb' }, 403), ['denied.md'])
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(await screen.findByText(/Removing denied\.md failed: Permission required: manage_kb/))
      .toBeInTheDocument()
    await waitFor(() => expect(calls.sourceLoads).toBe(2))
    expect(calls.deletes).toHaveLength(1)
  })

  it('a DELETE answering 500 with no JSON body is shown with its status, and the list reloads', async () => {
    const calls = installFetch(
      'broken.md', () => new Response('Internal Server Error', { status: 500 }), ['broken.md'])
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(await screen.findByText(/Removing broken\.md failed: HTTP 500/)).toBeInTheDocument()
    await waitFor(() => expect(calls.sourceLoads).toBe(2))
  })

  it('a DELETE answering 200 reloads the list and shows nothing', async () => {
    const calls = installFetch(
      'gone.md', () => json({ status: 'deleted', source: 'gone.md', department: 'general' }), [])
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(await screen.findByText('No documents ingested.')).toBeInTheDocument()
    expect(calls.sourceLoads).toBe(2)
    expect(screen.queryByText(/failed/)).toBeNull()
  })
})

// The hint under the picker named eight types while the input accepted ten
// (.yaml and .yml missing). Both now come from UPLOAD_TYPES (2026-10-29); the
// backend suite pins that list to the extractor's.
describe('KB page: the upload picker', () => {
  it('the hint names every type the file input accepts', async () => {
    const calls = installFetch('a.md', () => json({}), [])
    mount()
    await waitFor(() => expect(calls.sourceLoads).toBe(1))
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    const accepted = (input.getAttribute('accept') || '').split(',')
    expect(accepted).toContain('.yaml')
    expect(accepted).toContain('.yml')
    expect(screen.getByText(accepted.join(' '))).toBeInTheDocument()
  })
})
