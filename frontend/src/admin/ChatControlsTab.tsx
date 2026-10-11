import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { useGuardedPatch, NEVER_LOADED, SaveErrorLine, SwitchRow, DefaultModelPointer } from './settingsControls'

// ── Tab: Chat Controls ─────────────────────────────────────────────────────

// How every conversation behaves: what happens when one runs long, and the
// defaults users chat with. Split out of the System Prompt tab 2026-10-03.
export function ChatControlsTab({ api, headers, goToModels }: {
  api: string; headers: () => Record<string, string>; goToModels?: () => void
}) {
  const [loading, setLoading] = useState(true)
  const [strategy, setStrategy] = useState<'warn' | 'summarize'>('warn')
  const [maxTokens, setMaxTokens] = useState(6000)
  const [strategySaved, setStrategySaved] = useState(false)
  const [allowModelSelection, setAllowModelSelection] = useState(true)
  const [allowRagToggle, setAllowRagToggle] = useState(true)
  const [defaultModel, setDefaultModel] = useState('')
  const [defaultRagEnabled, setDefaultRagEnabled] = useState(false)
  const [controlsSaved, setControlsSaved] = useState(false)
  // Did /api/config actually answer? The `if (d)` guard below stops a failed
  // read from writing WRONG values into state - it does not stop a switch
  // from saving the state the component still has, which is its initial
  // defaults (default_rag_enabled starts false here while the server's is
  // true). The read failing and the save proceeding are two separate events;
  // only this flag connects them.
  const [controlsLoaded, setControlsLoaded] = useState(false)
  const { guardedPatch, saveErr, setSaveErr } = useGuardedPatch(api, headers)

  useEffect(() => {
    // Through the guard, early-out on null: a 401/403 body is valid JSON, so
    // a bare .then(r => r.json()) RESOLVED, every `!== undefined` check was
    // skipped, and the next save PATCHed this component's initial state back -
    // a failed READ became a destructive WRITE: retrieval turned off by
    // default, and every control the operator had locked silently unlocked.
    guardedJson<{
      allow_model_selection?: boolean
      allow_rag_toggle?: boolean
      default_model?: string
      default_rag_enabled?: boolean
    }>(fetch(`${api}/api/config`, { headers: headers() }), 'Loading settings')
      .then(d => {
        if (d) {
          if (d.allow_model_selection !== undefined) setAllowModelSelection(d.allow_model_selection)
          if (d.allow_rag_toggle !== undefined) setAllowRagToggle(d.allow_rag_toggle)
          if (d.default_model) setDefaultModel(d.default_model)
          if (d.default_rag_enabled !== undefined) setDefaultRagEnabled(d.default_rag_enabled)
          setControlsLoaded(true)
        }
        setLoading(false)
      })
    guardedJson<{ strategy?: 'warn' | 'summarize'; max_tokens?: number }>(
      fetch(`${api}/api/admin/context`, { headers: headers() }), 'Loading context settings')
      .then(d => {
        if (!d) return
        setStrategy(d.strategy || 'warn'); setMaxTokens(d.max_tokens || 6000)
      })
  }, [])

  // ONE KEY PER SWITCH. These switches used to PATCH every control together,
  // so flipping one also rewrote the others' stored rows with whatever the
  // panel was showing - and the server treats a stored row as an operator's
  // decision. Omitting a key leaves its stored value untouched, which is the
  // honest meaning of "I did not touch this control".
  const flip = (key: string, cur: boolean, set: (v: boolean) => void) => {
    if (!controlsLoaded) { setSaveErr(NEVER_LOADED); return }
    set(!cur)
    guardedPatch('/api/admin/config', { [key]: !cur }).then(ok => {
      if (!ok) { set(cur); return }
      setControlsSaved(true)
      setTimeout(() => setControlsSaved(false), 2000)
    })
  }

  const saveStrategy = async (s: 'warn' | 'summarize') => {
    // A refused save puts the choice back: the highlighted card is a claim
    // about what the server holds.
    const prev = strategy
    setStrategy(s)
    if (!(await guardedPatch('/api/admin/context', { strategy: s }))) { setStrategy(prev); return }
    setStrategySaved(true)
    setTimeout(() => setStrategySaved(false), 2000)
  }

  if (loading) return <p className="text-xs text-gray-500">Loading…</p>

  return (
    <div className="space-y-6">
      <SaveErrorLine text={saveErr} />
      {/* Context window strategy */}
      <div className="space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Context Window Strategy</p>
          <p className="text-xs text-gray-500">
            Action taken when conversation history exceeds ~{maxTokens.toLocaleString()} tokens
            (set via <span className="font-mono">MAX_CONTEXT_TOKENS</span>).
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {(['warn', 'summarize'] as const).map(s => (
            <button
              key={s}
              onClick={() => saveStrategy(s)}
              className={`px-4 py-3 rounded-xl text-sm text-left border transition-colors ${
                strategy === s
                  ? 'border-blue-500/50 bg-blue-500/10 text-white'
                  : 'border-gray-700 bg-gray-800/50 text-gray-400 hover:text-white hover:border-gray-600'
              }`}
            >
              <p className="font-medium capitalize">{s}</p>
              <p className="text-xs mt-0.5 opacity-70">
                {s === 'warn'
                  ? 'Show banner to user, continue with full history'
                  : 'Auto-summarize older turns, compress context silently'}
              </p>
            </button>
          ))}
        </div>
        {strategySaved && <p className="text-xs text-green-400">Strategy saved</p>}
      </div>

      {/* Usage controls */}
      <div className="border-t border-gray-800 pt-5 space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Usage Controls</p>
          <p className="text-xs text-gray-500">Global defaults applied to all users. Hiding a control locks users to its default.</p>
        </div>
        <DefaultModelPointer model={defaultModel} goToModels={goToModels} />
        <SwitchRow
          label="Allow model selection"
          desc="Users can switch models in the sidebar"
          on={allowModelSelection}
          onToggle={() => flip('allow_model_selection', allowModelSelection, setAllowModelSelection)}
        />
        <SwitchRow
          label="RAG enabled by default"
          desc="Knowledge base active for all new conversations"
          on={defaultRagEnabled}
          onToggle={() => flip('default_rag_enabled', defaultRagEnabled, setDefaultRagEnabled)}
        />
        <SwitchRow
          label="Allow RAG toggle"
          desc="Users can enable/disable knowledge base in the sidebar"
          on={allowRagToggle}
          onToggle={() => flip('allow_rag_toggle', allowRagToggle, setAllowRagToggle)}
        />
        {controlsSaved && <p className="text-xs text-green-400 text-right">Saved</p>}
      </div>
    </div>
  )
}
