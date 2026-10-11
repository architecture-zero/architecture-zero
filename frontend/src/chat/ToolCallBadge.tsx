import { useState } from 'react'

// ── ToolCallBadge ──────────────────────────────────────────────────────────

interface ToolCallBadgeProps {
  name: string
  result: string
}

export function ToolCallBadge({ name, result }: ToolCallBadgeProps) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="mb-1.5 rounded-lg border border-yellow-700/40 bg-yellow-950/30 text-xs overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-yellow-900/20 transition-colors"
      >
        <span className="text-yellow-500">⚙</span>
        <span className="text-yellow-300 font-mono">{name}()</span>
        <span className="ml-auto text-yellow-700">{expanded ? '▲' : '▼'}</span>
      </button>
      {expanded && (
        <pre className="px-3 pb-2 pt-1 text-gray-400 font-mono text-xs whitespace-pre-wrap break-words border-t border-yellow-800/30 overflow-auto max-h-48">
          {result}
        </pre>
      )}
    </div>
  )
}
