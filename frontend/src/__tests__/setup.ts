import '@testing-library/jest-dom'

// jsdom implements no layout, so scrollIntoView does not exist on it. App.tsx
// calls it in an effect on every message change, which means ANY test that
// mounts the chat client dies on the first render without this - and until the
// stream-invariant tests there were none, which is part of why the client had
// no coverage at all. A no-op is the right stub: nothing here asserts on
// scrolling, and the alternative (mocking the ref) would couple every test to
// an implementation detail.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() { /* no layout in jsdom */ }
}

// findBy* and waitFor give up after Testing Library's own 1 s by default, below
// vitest's 20 s testTimeout above: on a loaded CI runner a refusal that renders
// in 80 ms on a workstation took over 1 s and failed a gate whose code was
// green 12 minutes earlier (2026-10-31). 5 s still fails a wait that never ends.
import { configure } from '@testing-library/react'
configure({ asyncUtilTimeout: 5000 })
