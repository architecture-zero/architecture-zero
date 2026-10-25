import { render, screen, act, cleanup } from '@testing-library/react'
import { describe, it, expect, afterEach, vi } from 'vitest'
import { ErrorSurface, emitError } from '../errorSurface'

// A toast's dismiss timer must die with the surface that set it. One left
// running fires after the surface is gone - in a test run, after the file's
// environment is torn down, where it throws "window is not defined" outside
// any test: every test passes and the run still fails (a deploy gate went red
// that way, 2026-10-24). Pinned by counting fake timers across an unmount.
describe('a toast dismiss timer', () => {
  afterEach(() => { cleanup(); vi.useRealTimers() })

  it('is cleared when the surface unmounts', () => {
    vi.useFakeTimers()
    const { unmount } = render(<ErrorSurface />)
    const before = vi.getTimerCount()
    act(() => { emitError('a toast raised just before unmount') })
    expect(screen.getByText('a toast raised just before unmount')).toBeInTheDocument()
    expect(vi.getTimerCount()).toBe(before + 1)
    unmount()
    expect(vi.getTimerCount()).toBe(before)
  })

  it('still dismisses its toast after six seconds while mounted', () => {
    vi.useFakeTimers()
    render(<ErrorSurface />)
    act(() => { emitError('a toast that times out') })
    expect(screen.getByText('a toast that times out')).toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(6000) })
    expect(screen.queryByText('a toast that times out')).toBeNull()
  })
})
