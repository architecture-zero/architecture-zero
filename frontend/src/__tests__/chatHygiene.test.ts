// The App split's standing rules (2026-10-11), the chat screen's counterpart of
// adminHygiene: the shell (App.tsx) imports the pieces under src/chat/, never
// the reverse; the pieces are layered - the shapes import nothing local, the
// components compose each other and read the shapes, the theme and the API
// client, and the stream hook reads the shapes and the API client but never a
// component (nor does a component read the hook: what it needs arrives as
// props); no file carries an import it does not read; and the shell reads the
// hook once and renders each extracted piece of its screen once.
import { describe, it, expect } from 'vitest'

// The sources as text, through Vite (no node types in this project): keys are
// '../App.tsx' and '../chat/<file>'.
const raw = import.meta.glob(['../App.tsx', '../chat/*.ts', '../chat/*.tsx'],
  { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const files: Record<string, string> = {}
for (const [k, v] of Object.entries(raw)) files[k.replace(/^\.\.\//, '')] = v.replace(/\r\n/g, '\n')
const chatFiles = Object.keys(files).filter(k => k.startsWith('chat/')).map(k => k.slice('chat/'.length)).sort()

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

describe('chat split hygiene', () => {
  it('src/chat never imports the shell', () => {
    const offenders: string[] = []
    for (const f of chatFiles) {
      for (const imp of imports(files[`chat/${f}`])) {
        if (/\/App$/.test(imp.from)) offenders.push(`${f} imports the shell (${imp.from})`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('the pieces are layered: shapes import nothing local, the hook imports no component, no component imports the hook', () => {
    const offenders: string[] = []
    const local = (from: string) => from.startsWith('./') || from.startsWith('../')
    for (const imp of imports(files['chat/types.ts'])) {
      if (local(imp.from)) offenders.push(`types.ts imports ${imp.from}`)
    }
    for (const imp of imports(files['chat/useChatStream.ts'])) {
      if (local(imp.from) && !['./types', '../api', '../errorSurface'].includes(imp.from)) offenders.push(`useChatStream.ts imports ${imp.from}`)
    }
    for (const f of chatFiles) {
      if (f === 'types.ts' || f === 'useChatStream.ts') continue
      for (const imp of imports(files[`chat/${f}`])) {
        if (/useChatStream$/.test(imp.from)) offenders.push(`${f} imports the hook (${imp.from})`)
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

  it('the shell reads the hook once and renders each extracted piece once', () => {
    const shell = files['App.tsx']
    expect((shell.match(/= useChatStream\(/g) || []).length).toBe(1)
    for (const piece of ['Sidebar', 'ChatHeader', 'Composer']) {
      expect((shell.match(new RegExp(`<${piece}\\b`, 'g')) || []).length, piece).toBe(1)
    }
  })
})
