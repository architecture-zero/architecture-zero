/**
 * The chat sidebar's History list (2026-10-03, the 2026-08-01 UI audit's
 * sessions drawer): conversations grouped by their last activity, a name that
 * wins over the first message, a rename in place (Enter saves, Escape
 * cancels, a blank or unchanged name sends nothing), and a delete that asks
 * once. The same file on every surface that ships the chassis chat.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import HistoryList, { groupByActivity, parseStamp, titleOf, type HistoryEntry } from '../HistoryList'

// The server's shape: naive UTC ISO, no zone.
const stamp = (d: Date) => d.toISOString().replace('Z', '')

describe('grouping and titles', () => {
  it('groups by last activity - Today, Last 7 days, Older - and an unreadable stamp is Older', () => {
    const now = new Date(2026, 9, 3, 15, 0)
    const groups = groupByActivity([
      { session: 'today', last_at: stamp(new Date(2026, 9, 3, 9, 0)) },
      { session: 'this-week', last_at: stamp(new Date(2026, 8, 29, 12, 0)) },
      { session: 'august', last_at: stamp(new Date(2026, 7, 1, 12, 0)) },
      { session: 'no-stamp' },
    ], now)
    expect(groups.map(g => [g.label, g.items.map(e => e.session)])).toEqual([
      ['Today', ['today']], ['Last 7 days', ['this-week']], ['Older', ['august', 'no-stamp']],
    ])
  })

  it('reads a zoneless stamp as UTC, the way the server writes it', () => {
    expect(parseStamp('2026-10-03T12:00:00')).toBe(Date.UTC(2026, 9, 3, 12, 0, 0))
    expect(parseStamp('2026-10-03T12:00:00Z')).toBe(Date.UTC(2026, 9, 3, 12, 0, 0))
    expect(parseStamp('not a date')).toBeNull()
  })

  it('a name wins over the first message; a blank one does not', () => {
    expect(titleOf({ session: 's', name: 'Q3 planning', first_message: 'how do I' })).toBe('Q3 planning')
    expect(titleOf({ session: 's', name: '  ', first_message: 'how do I' })).toBe('how do I')
    expect(titleOf({ session: 's' })).toBe('New conversation')
  })
})

const ENTRIES: HistoryEntry[] = [
  { session: 's1', name: 'PTO policy', first_message: 'what is our PTO policy', last_at: stamp(new Date(2026, 9, 3, 9, 0)) },
  { session: 's2', first_message: 'Expense limits?', last_at: stamp(new Date(2026, 8, 30, 9, 0)) },
]

function setup(over: { onRename?: (id: string, name: string) => Promise<boolean>; onDelete?: (id: string) => Promise<boolean> } = {}) {
  const props = {
    entries: ENTRIES, activeId: 's1', activeStyle: {},
    onSelect: vi.fn(),
    onRename: vi.fn(over.onRename ?? (async () => true)),
    onDelete: vi.fn(over.onDelete ?? (async () => true)),
  }
  render(<HistoryList {...props} />)
  return props
}

const renameButton = (i: number) => screen.getAllByRole('button', { name: 'Rename conversation' })[i]
const deleteButton = (i: number) => screen.getAllByRole('button', { name: 'Delete conversation' })[i]

describe('renaming in place', () => {
  it('opens on the current title, and Enter saves the trimmed name', async () => {
    const p = setup()
    fireEvent.click(renameButton(0))
    const box = screen.getByRole('textbox', { name: 'Conversation name' }) as HTMLInputElement
    expect(box.value).toBe('PTO policy')
    fireEvent.change(box, { target: { value: '  PTO for contractors  ' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(p.onRename).toHaveBeenCalledWith('s1', 'PTO for contractors'))
  })

  it('Escape cancels, and a blank or unchanged name sends nothing', () => {
    const p = setup()
    fireEvent.click(renameButton(0))
    fireEvent.change(screen.getByRole('textbox', { name: 'Conversation name' }), { target: { value: 'something else' } })
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Conversation name' }), { key: 'Escape' })
    expect(screen.queryByRole('textbox', { name: 'Conversation name' })).toBeNull()

    fireEvent.click(renameButton(1))
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Conversation name' }), { key: 'Enter' })
    fireEvent.click(renameButton(1))
    fireEvent.change(screen.getByRole('textbox', { name: 'Conversation name' }), { target: { value: '   ' } })
    fireEvent.keyDown(screen.getByRole('textbox', { name: 'Conversation name' }), { key: 'Enter' })
    expect(p.onRename).not.toHaveBeenCalled()
    expect(screen.getByText('PTO policy')).toBeInTheDocument()
  })
})

describe('deleting asks once', () => {
  it('the x asks; Cancel puts the row back and sends nothing', () => {
    const p = setup()
    fireEvent.click(deleteButton(1))
    expect(screen.getByText('Delete this chat?')).toBeInTheDocument()
    expect(screen.queryByText('Expense limits?')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByText('Expense limits?')).toBeInTheDocument()
    expect(p.onDelete).not.toHaveBeenCalled()
  })

  it('Delete sends it; a refusal leaves the row standing', async () => {
    const p = setup({ onDelete: async () => false })
    fireEvent.click(deleteButton(1))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(p.onDelete).toHaveBeenCalledWith('s2'))
    await waitFor(() => expect(screen.getByText('Expense limits?')).toBeInTheDocument())
    expect(screen.queryByText('Delete this chat?')).toBeNull()
  })

  it('a title opens its conversation', () => {
    const p = setup()
    fireEvent.click(screen.getByText('Expense limits?'))
    expect(p.onSelect).toHaveBeenCalledWith('s2')
  })
})
