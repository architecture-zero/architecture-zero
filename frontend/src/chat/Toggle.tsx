import { PRIMARY_COLOR } from '../theme'

// ── Toggle ─────────────────────────────────────────────────────────────────

interface ToggleProps {
  enabled: boolean
  onToggle: () => void
}

export function Toggle({ enabled, onToggle }: ToggleProps) {
  return (
    <button
      onClick={onToggle}
      className={`relative w-10 h-5 rounded-full transition-colors duration-200 flex-shrink-0 ${enabled ? '' : 'bg-gray-600'}`}
      style={enabled ? { backgroundColor: PRIMARY_COLOR } : {}}
    >
      <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${enabled ? 'left-5' : 'left-0.5'}`} />
    </button>
  )
}
