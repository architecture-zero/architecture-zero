import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { useGuardedPatch, SaveErrorLine, SaveBar } from './settingsControls'

// The suggestions as the server stores them - one per line, trimmed, no
// blanks - so a stray empty line does not read as an edit.

interface AdminConfig {
  system_prompt?: string
  instance_name?: string
  primary_color?: string
}
const normLines = (text: string) => text.split('\n').map(s => s.trim()).filter(Boolean).join('\n')

// ── Tab: System Prompt ─────────────────────────────────────────────────────

// What the assistant is told, and what it offers on an empty chat - nothing
// else since 2026-10-03, when the chat controls and the guest switch moved to
// tabs of their own (the 2026-08-01 UI audit's prompt-tab split).
export function SystemPromptTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [prompt, setPrompt] = useState('')
  // What the server holds, as of the last read or accepted save: each Save is
  // lit only while its box differs from it.
  const [promptBaseline, setPromptBaseline] = useState('')
  const [saved, setSaved] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [loading, setLoading] = useState(true)
  const [suggestionsText, setSuggestionsText] = useState('')
  const [suggestionsBaseline, setSuggestionsBaseline] = useState('')
  const [suggestionsSaved, setSuggestionsSaved] = useState(false)
  const [savingSuggestions, setSavingSuggestions] = useState(false)
  // Did each read actually answer? The `if (!d)` guards below stop a failed
  // read from writing WRONG values into state - they do not stop a Save from
  // sending the state the component still has, which is its initial empty
  // strings. Each box has its own read, so each needs its own answer.
  const [promptLoaded, setPromptLoaded] = useState(false)
  const [suggestionsLoaded, setSuggestionsLoaded] = useState(false)
  const { guardedPatch, saveErr, setSaveErr } = useGuardedPatch(api, headers)

  useEffect(() => {
    // BOTH loads go through the guard and BOTH early-out on null. Neither
    // route is public, and a 401/403 body is valid JSON - so a bare
    // .then(r => r.json()) RESOLVES and the failure looks like a load.
    guardedJson<AdminConfig>(
      fetch(`${api}/api/admin/config`, { headers: headers() }), 'Loading system prompt')
      .then(d => {
        if (d) { const p = d.system_prompt || ''; setPrompt(p); setPromptBaseline(p); setPromptLoaded(true) }
        setLoading(false)
      })
    guardedJson<{ suggestions?: string[] }>(
      fetch(`${api}/api/config`, { headers: headers() }), 'Loading settings')
      .then(d => {
        if (!d) return
        const lines = (d.suggestions || []).join('\n')
        setSuggestionsText(lines); setSuggestionsBaseline(lines)
        setSuggestionsLoaded(true)
      })
  }, [])

  const save = async () => {
    // If the read failed this box holds the component's empty initial string
    // rather than the operator's prompt, and saving would overwrite a
    // configured system prompt with nothing - on a failure the screen
    // otherwise renders as "loaded".
    if (!promptLoaded) {
      setSaveErr('NOT saved - the system prompt never loaded, so saving would '
        + 'overwrite it with an empty value. Reload the page and try again.')
      return
    }
    const sent = prompt
    setSavingPrompt(true)
    const ok = await guardedPatch('/api/admin/config', { system_prompt: sent })
    setSavingPrompt(false)
    if (!ok) return
    setPromptBaseline(sent)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const saveSuggestions = async () => {
    if (!suggestionsLoaded) {
      setSaveErr('NOT saved - these settings never loaded, so saving would '
        + 'overwrite them. Reload the page and try again.')
      return
    }
    const list = suggestionsText.split('\n').map(s => s.trim()).filter(Boolean)
    setSavingSuggestions(true)
    const ok = await guardedPatch('/api/admin/config', { suggestions: list })
    setSavingSuggestions(false)
    if (!ok) return
    setSuggestionsBaseline(list.join('\n'))
    setSuggestionsSaved(true)
    setTimeout(() => setSuggestionsSaved(false), 2000)
  }

  const promptDirty = prompt !== promptBaseline
  const suggestionsDirty = normLines(suggestionsText) !== normLines(suggestionsBaseline)

  if (loading) return <p className="text-xs text-gray-500">Loading…</p>

  return (
    <div className="space-y-6">
      <SaveErrorLine text={saveErr} />
      {/* System prompt */}
      <div className="space-y-3">
        <p className="text-xs text-gray-400">
          This prompt is prepended to every conversation and defines the AI's role and persona.
          Changes take effect immediately - no restart required.
        </p>
        <textarea
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          rows={10}
          className="w-full bg-gray-900 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none resize-none font-mono leading-relaxed"
        />
        <SaveBar label="Save Prompt" dirty={promptDirty} saving={savingPrompt} saved={saved} onSave={save} />
      </div>

      {/* Chat suggestions */}
      <div className="border-t border-gray-800 pt-5 space-y-3">
        <div>
          <p className="text-xs text-gray-300 font-medium mb-0.5">Chat Suggestions</p>
          <p className="text-xs text-gray-500">Shown on the empty chat screen. One suggestion per line (max 3 displayed).</p>
        </div>
        <textarea
          value={suggestionsText}
          onChange={e => setSuggestionsText(e.target.value)}
          rows={4}
          placeholder={"What can you help me with?\nHow do I get started?\nWhat kinds of questions can I ask?"}
          className="w-full bg-gray-900 border border-gray-700 focus:border-blue-500/60 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 outline-none resize-none leading-relaxed"
        />
        <SaveBar label="Save Suggestions" dirty={suggestionsDirty} saving={savingSuggestions} saved={suggestionsSaved} onSave={saveSuggestions} />
      </div>
    </div>
  )
}
