/**
 * The Monitoring tab's "Download scrape config" file.
 *
 * It is a static string, and that is exactly why it drifted unnoticed: every
 * version of it that shipped broken (the edge port, a header that told you to
 * paste a scrape_configs key under another one, no credential at all) passed
 * every other test, because nothing read it. These pin the shape a working
 * scrape needs, and the one thing the file must never carry: a credential
 * value. The token lives in the file credentials_file names, never in
 * prometheus.yml and never in this download.
 */
import { describe, expect, it } from 'vitest'

import { SCRAPE_CONFIG_YAML } from '../scrapeConfig'

const lines = SCRAPE_CONFIG_YAML.split(/\r?\n/)

describe('downloaded Prometheus scrape config', () => {
  it('carries the Bearer authorization block at job level', () => {
    const i = lines.indexOf('    authorization:')
    expect(i).toBeGreaterThan(-1)
    expect(lines[i + 1]).toBe('      type: Bearer')
    expect(lines[i + 2].startsWith('      credentials_file: /etc/prometheus/metrics_token')).toBe(true)
  })

  it('never carries a credential value', () => {
    const valueLines = lines.filter((l) => {
      const t = l.trimStart()
      return t.startsWith('credentials:') || t.startsWith('bearer_token:') || t.startsWith('password:')
    })
    expect(valueLines).toEqual([])
  })

  it('declares scrape_configs exactly once, and says to merge rather than paste under', () => {
    expect(lines.filter((l) => l === 'scrape_configs:')).toHaveLength(1)
    // The file declares the key itself, so its header must say so. A header
    // that says "add under scrape_configs" makes the operator write the key
    // twice, and Prometheus then refuses its whole configuration.
    expect(SCRAPE_CONFIG_YAML).toContain("copy only the '- job_name:' block")
  })

  it('is exactly the reviewed YAML body, line for line', () => {
    // The pins above check single lines; this one pins every non-comment line,
    // so a mis-indented key (which Prometheus rejects), an extra params entry
    // carrying a token, or a second target all fail here. The literal was
    // validated with promtool check config when this pin was written.
    expect(lines.filter((l) => l !== '' && !l.startsWith('#'))).toEqual([
      'scrape_configs:',
      "  - job_name: 'architecture-zero'",
      '    static_configs:',
      "      - targets: ['YOUR_HOST:8000']",
      '    metrics_path: /metrics',
      '    scrape_interval: 30s',
      '    authorization:',
      '      type: Bearer',
      '      credentials_file: /etc/prometheus/metrics_token   # the file holds METRICS_TOKEN',
    ])
  })

  it('targets the backend port YOUR_HOST:8000, never the :80 or :443 edge', () => {
    const targets = lines.filter((l) => l.trimStart().startsWith('- targets:'))
    expect(targets).toEqual(["      - targets: ['YOUR_HOST:8000']"])
    expect(SCRAPE_CONFIG_YAML).not.toContain(":80']")
    expect(SCRAPE_CONFIG_YAML).not.toContain(":443']")
  })
})
