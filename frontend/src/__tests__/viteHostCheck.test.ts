// The dev server keeps Vite's Host-header check (2026-11-17).
// `server.host: true` puts the dev server on the network, which the Docker dev
// flow needs. `allowedHosts: true` also turned off the Host check, Vite's
// guard against DNS rebinding: a web page a developer had open could reach the
// dev server through a name rebound to it, and read what it serves. Vite's
// default allows localhost, *.localhost and any IP address, which is how the
// Docker flow reaches it, so the default is kept. A production build is static
// files served by nginx and never runs the dev server.
import { describe, it, expect } from 'vitest'
import configText from '../../vite.config.ts?raw'

describe('the dev server Host check', () => {
  it('FIX: allowedHosts does not turn the check off', () => {
    expect(configText).not.toMatch(/allowedHosts\s*:\s*true/)
  })

  it('CONTROL: the dev server still listens on the network for the Docker flow', () => {
    expect(configText).toMatch(/host\s*:\s*true/)
  })
})
