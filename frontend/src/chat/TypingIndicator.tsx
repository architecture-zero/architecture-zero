import { PRIMARY_COLOR, INITIALS, ON_PRIMARY } from '../theme'

// ── TypingIndicator ────────────────────────────────────────────────────────

export function TypingIndicator() {
  return (
    <div className="flex items-end gap-3 mb-6">
      <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0" style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}>
        {INITIALS}
      </div>
      <div className="bg-gray-800 border border-gray-700 px-4 py-3 rounded-2xl rounded-bl-sm flex items-center gap-1.5">
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
        <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
      </div>
    </div>
  )
}
