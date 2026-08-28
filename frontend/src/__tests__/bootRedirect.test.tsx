import { describe, it, expect } from 'vitest'
import { wantsSetupRedirect } from '../App'

// The boot handler's setup branch, tested at the decision rather than at the
// navigation: jsdom makes window.location non-configurable, so stubbing
// location.reload is a fight with no payoff. The navigation is verified by hand
// against a fresh DB; what a refactor can silently invert is the predicate, and
// that is what these pin.
describe('wantsSetupRedirect', () => {
  // The payload is GET /api/auth/config - the only endpoint readable before a
  // session exists. /api/status carries no needs_setup on this backend AND
  // requires auth, so a boot decision taken from it could never reach this
  // branch at all.
  const base: { needs_setup?: boolean } = {}
  const fresh = { ...base, needs_setup: true }

  it('routes an unclaimed instance to the wizard', () => {
    expect(wantsSetupRedirect(fresh, '', false)).toBe(true)
  })

  it('leaves a claimed instance alone', () => {
    expect(wantsSetupRedirect({ ...base, needs_setup: false }, '', false)).toBe(false)
  })

  it('treats a backend that omits the key as claimed, never as an invitation', () => {
    expect(wantsSetupRedirect(base, '', false)).toBe(false)
  })

  it('never redirects when already at #setup - the loop guard', () => {
    expect(wantsSetupRedirect(fresh, '#setup', false)).toBe(false)
  })

  // An instance that reaches zero active admins reports needs_setup again, and
  // its claim code was burned at first claim. If the redirect outranked ?login=1
  // there would be no reachable login screen at all: the wizard's own escape
  // link points back through the boot handler.
  it('yields to an explicit ?login=1 - the owner door outranks the inference', () => {
    expect(wantsSetupRedirect(fresh, '', true)).toBe(false)
  })
})
