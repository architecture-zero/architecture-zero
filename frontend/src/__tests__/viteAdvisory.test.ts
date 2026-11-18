// GHSA-p4f5-76q4-ffq7 (Critical, CVSS 4.0 9.1): remote code execution through
// the Vite dev server's `__vite-browser-external` virtual module
// (https://github.com/vitejs/vite/security/advisories/GHSA-p4f5-76q4-ffq7).
// It is reachable when the dev server listens beyond localhost, and
// vite.config.ts sets `server.host: true`, so `npm run dev` here meets that
// condition. A production build is static files and never runs the dev
// server, so the reach is any machine that runs `npm run dev`.
//
// This reads the version the lockfile pins (what `npm ci` installs), not the
// package.json range, and fails if it is below 6.4.6 (the fix on this major)
// or inside any range the advisory lists as affected, so a later major bump
// to an unpatched 7.x or 8.x fails here too.
import { describe, it, expect } from 'vitest'
import lockText from '../../package-lock.json?raw'

type Version = [number, number, number]

const FIXED_ON_6: Version = [6, 4, 6]

// The advisory's affected ranges, inclusive at both ends.
const AFFECTED: [Version, Version][] = [
  [[4, 0, 0], [6, 4, 5]],
  [[7, 0, 0], [7, 3, 7]],
  [[8, 0, 0], [8, 1, 6]],
  [[8, 2, 0], [8, 2, 4]],
  [[8, 3, 0], [8, 3, 6]],
]

function parse(v: string): Version {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(v)
  if (!m) throw new Error(`not a version: ${JSON.stringify(v)}`)
  return [Number(m[1]), Number(m[2]), Number(m[3])]
}

function compare(a: Version, b: Version): number {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] - b[i]
  return 0
}

function patched(version: string): boolean {
  const v = parse(version)
  if (compare(v, FIXED_ON_6) < 0) return false
  return !AFFECTED.some(([lo, hi]) => compare(v, lo) >= 0 && compare(v, hi) <= 0)
}

describe('vite dev server advisory GHSA-p4f5-76q4-ffq7', () => {
  it('compares versions numerically, not as text', () => {
    expect(patched('6.4.5')).toBe(false)
    expect(patched('6.4.6')).toBe(true)
    // As text '6.4.10' sorts below '6.4.6'; as numbers it is above.
    expect(patched('6.4.10')).toBe(true)
    expect(patched('6.10.0')).toBe(true)
    expect(patched('5.4.19')).toBe(false)
    expect(patched('7.3.7')).toBe(false)
    expect(patched('7.3.8')).toBe(true)
    expect(patched('8.3.6')).toBe(false)
    expect(patched('8.3.7')).toBe(true)
  })

  it('every vite the lockfile installs carries the fix', () => {
    const lock = JSON.parse(lockText) as { packages: Record<string, { version?: string }> }
    const vites = Object.entries(lock.packages)
      .filter(([path]) => /(^|\/)node_modules\/vite$/.test(path))
      .map(([path, pkg]) => ({ path, version: pkg.version ?? '' }))
    expect(vites.length).toBeGreaterThan(0)
    for (const { path, version } of vites) {
      expect({ path, version, patched: patched(version) }).toEqual({ path, version, patched: true })
    }
  })
})
