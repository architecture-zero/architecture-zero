// The admin split's standing rules (2026-10-10), the frontend counterpart of
// the backend's test_module_hygiene: the shell (AdminPanel.tsx) imports the
// tabs under src/admin/, never the reverse; tabs share through the shared
// modules (stepUp, settingsControls, and src/theme), never through each other;
// no file carries an import it does not read; and every tab the sidebar lists
// is rendered by exactly one line of the shell.
import { describe, it, expect } from 'vitest'

// The sources as text, through Vite (no node types in this project): keys are
// '../AdminPanel.tsx' and '../admin/<file>'.
const raw = import.meta.glob(['../AdminPanel.tsx', '../admin/*.ts', '../admin/*.tsx'],
  { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const files: Record<string, string> = {}
for (const [k, v] of Object.entries(raw)) files[k.replace(/^\.\.\//, '')] = v.replace(/\r\n/g, '\n')
const adminFiles = Object.keys(files).filter(k => k.startsWith('admin/')).map(k => k.slice('admin/'.length)).sort()

// import X from 'm' / import { a, b as c, type D } from 'm' / import type { .. } / import * as ns
const IMPORT_RX = /^import\s+(type\s+)?(?:(\*\s+as\s+\w+)|(\w+)(?:\s*,\s*)?)?(?:\{([^}]*)\})?\s*from\s*'([^']+)'/gm
function imports(text: string) {
  const out: { names: string[]; from: string; stmt: string }[] = []
  for (const m of text.matchAll(IMPORT_RX)) {
    const names: string[] = []
    if (m[2]) names.push(m[2].replace(/\*\s+as\s+/, ''))
    if (m[3]) names.push(m[3])
    if (m[4]) for (const part of m[4].split(',')) {
      const p = part.trim().replace(/^type\s+/, '')
      if (!p) continue
      names.push(p.includes(' as ') ? p.split(' as ')[1].trim() : p)
    }
    out.push({ names, from: m[5], stmt: m[0] })
  }
  return out
}

describe('admin split hygiene', () => {
  it('src/admin never imports the shell or another tab', () => {
    // A sibling import is allowed only for the shared modules; any other './x'
    // is a tab reaching into a tab, whatever the file is called (the template
    // renders its KB tab as KBManage, which a '*Tab' test would have missed).
    const SHARED = ['./stepUp', './settingsControls']
    const offenders: string[] = []
    for (const f of adminFiles) {
      for (const imp of imports(files[`admin/${f}`])) {
        if (/AdminPanel$/.test(imp.from)) offenders.push(`${f} imports the shell (${imp.from})`)
        else if (imp.from.startsWith('./') && !SHARED.includes(imp.from)) offenders.push(`${f} imports another tab (${imp.from})`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('every imported name is read', () => {
    const unread: string[] = []
    for (const [f, text] of Object.entries(files)) {
      const body = text.replace(IMPORT_RX, '')
      for (const imp of imports(text)) for (const name of imp.names) {
        if (!new RegExp(`\\b${name}\\b`).test(body)) unread.push(`${f}: ${name} from '${imp.from}'`)
      }
    }
    expect(unread).toEqual([])
  })

  it('the shell renders every tab the sidebar lists, once', () => {
    const shell = files['AdminPanel.tsx']
    const tabsBlock = shell.match(/const TABS[^=]*=\s*\[([\s\S]*?)\n\]/)
    expect(tabsBlock, 'the TABS list').toBeTruthy()
    const ids = Array.from(tabsBlock![1].matchAll(/id:\s*'(\w+)'/g)).map(m => m[1])
    expect(ids.length).toBeGreaterThan(10)
    // any component, not only a *Tab: the template renders its KB tab as <KBManage>
    const miswired = ids.filter(id => (shell.match(new RegExp(`\\{tab === '${id}'\\s*&&\\s*<[A-Z]\\w*\\b`, 'g')) || []).length !== 1)
    expect(miswired).toEqual([])
  })
})
