import { useState } from 'react'
import { API, authHeaders } from '../api'

// ── CitationsPanel ─────────────────────────────────────────────────────────

interface CitationsPanelProps {
  sources?: string[]
  // The answer came from the help lane, so its help/ chips open a product page.
  openable?: boolean
}

export function CitationsPanel({ sources, openable }: CitationsPanelProps) {
  // The chips name what the answer was grounded in - the claim a reader checks
  // it against. Corpus chips are deliberately NOT clickable: opening one would
  // need a document-read endpoint this API does not expose, and a chip that
  // always answers "preview is not available" is a promise the platform does
  // not keep. Serving GET /api/kb/file would make them openable, and the shape
  // below is ready for it. HELP chips (in-product help, 2026-09-21) DO open:
  // the product's own pages have their own read, GET /api/help/page, gated
  // like chat, so a help citation opens for a guest and a plain user too.
  const [viewing, setViewing] = useState<{ name: string; content: string } | null>(null)
  if (!sources?.length) return null

  const openHelp = (s: string) => {
    fetch(`${API}/api/help/page?name=${encodeURIComponent(s)}`, { headers: authHeaders() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(d => setViewing({ name: d.name, content: d.content }))
      .catch(() => setViewing({ name: s, content: 'This help page could not be loaded.' }))
  }

  return (
    <div className="mt-2 pt-2 border-t border-gray-700/50">
      <p className="text-xs text-gray-500 mb-1.5">Sources</p>
      <div className="flex flex-wrap gap-1">
        {sources.map((s, i) => openable && s.startsWith('help/') ? (
          <button key={i} title={`Open ${s}`} onClick={() => openHelp(s)}
            className="text-xs bg-gray-700/50 text-gray-300 hover:text-white hover:bg-gray-700 px-2 py-0.5 rounded-full border border-gray-600/50 truncate max-w-[200px] transition-colors">
            {s}
          </button>
        ) : (
          <span key={i} title={s}
            className="text-xs bg-gray-700/50 text-gray-400 px-2 py-0.5 rounded-full border border-gray-600/50 truncate max-w-[200px]">
            {s}
          </span>
        ))}
      </div>
      {viewing && (
        <div className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4" onClick={() => setViewing(null)}>
          <div className="bg-gray-900 border border-gray-700 rounded-2xl max-w-2xl w-full max-h-[80vh] flex flex-col" onClick={e => e.stopPropagation()} role="dialog" aria-label={viewing.name}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
              <span className="text-xs text-gray-400 truncate">{viewing.name}</span>
              <button onClick={() => setViewing(null)} className="text-xs text-gray-500 hover:text-white transition-colors">Close</button>
            </div>
            <pre className="px-4 py-3 text-xs text-gray-300 whitespace-pre-wrap break-words overflow-auto font-sans leading-relaxed">{viewing.content}</pre>
          </div>
        </div>
      )}
    </div>
  )
}
