import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'
import { useStepUp } from './stepUp'
import { DefaultModelPointer, SaveStatus } from './settingsControls'

// ── Tab: Settings ──────────────────────────────────────────────────────────

interface ProviderSettings {
  ollama_enabled: boolean
  anthropic_enabled: boolean
  openai_enabled: boolean
  ollama_base_url: string
  anthropic_key_set: boolean
  openai_key_set: boolean
  default_model: string
  rag_similarity_threshold: number
}

export function SettingsTab({ api, headers, goToModels }: {
  api: string; headers: () => Record<string, string>; goToModels?: () => void
}) {
  const stepUp = useStepUp()
  const [settings, setSettings] = useState<ProviderSettings | null>(null)
  const [ollamaEnabled, setOllamaEnabled] = useState(false)
  const [anthropicEnabled, setAnthropicEnabled] = useState(false)
  const [openaiEnabled, setOpenaiEnabled] = useState(false)
  const [ollamaBase, setOllamaBase] = useState('')
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [ragThreshold, setRagThreshold] = useState('0.40')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    // Same class as SystemPromptTab. This route is require_owner while the
    // panel opens for several lesser permissions, so a non-owner admin gets a
    // 403 - which unguarded renders as a confident, fully-drawn form reporting
    // every provider disabled on an instance where they may all be live.
    guardedJson<ProviderSettings>(
      fetch(`${api}/api/settings`, { headers: headers() }), 'Loading provider settings')
      .then(d => {
        if (!d) {
          // The guard already raised a toast; this is what the tab BODY says.
          // Without it the render guard below sits on "Loading..." forever,
          // which is a quieter version of the dishonesty this call was fixed
          // for.
          setError('Could not load provider settings - this section is Owner-only.')
          return
        }
        setError('')
        setSettings(d)
        setOllamaEnabled(d.ollama_enabled)
        setAnthropicEnabled(d.anthropic_enabled)
        setOpenaiEnabled(d.openai_enabled)
        setOllamaBase(d.ollama_base_url)
        setAnthropicKey('')
        setOpenaiKey('')
        setRagThreshold(String(d.rag_similarity_threshold))
      })
      .catch(() => setError('Failed to load settings'))
  }

  useEffect(() => { load() }, [])

  const save = async () => {
    setSaving(true); setSaved(false); setError('')
    // REFUSE HERE RATHER THAN SEND SOMETHING ELSE. Omitting the key makes the
    // server skip the write and still answer 200, which is the same
    // silently-discarded-but-reported-saved defect being fixed on that end.
    const parsedThreshold = parseFloat(ragThreshold)
    if (!Number.isFinite(parsedThreshold) || parsedThreshold < 0 || parsedThreshold > 1) {
      setError('Similarity threshold must be a number between 0 and 1.')
      setSaving(false)
      return
    }
    try {
      const body: Record<string, unknown> = {
        ollama_enabled: ollamaEnabled,
        anthropic_enabled: anthropicEnabled,
        openai_enabled: openaiEnabled,
        ollama_base_url: ollamaBase,
        // No default_model: Models owns it (2026-10-03), and the server
        // leaves an omitted key as it is.
        // NOT `parseFloat(x) || 0.4`. Zero is a legitimate threshold, the input
        // advertises min="0", and `0 || 0.4` is 0.4 - so typing 0 silently sent
        // 0.4 while the field went on displaying 0. Validated above instead.
        rag_similarity_threshold: parsedThreshold,
      }
      if (anthropicKey.trim()) body.anthropic_api_key = anthropicKey.trim()
      if (openaiKey.trim()) body.openai_api_key = openaiKey.trim()
      // Step-up (2026-09-21): this route holds the egress address and every
      // provider key, so saving costs the caller's own password. Cancel = no save.
      const pw = await stepUp.ask('change provider settings')
      if (pw === null) return
      body.current_password = pw

      const r = await fetch(`${api}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!r.ok) { const d = await r.json(); setError(d.detail || 'Save failed'); return }
      const updated: ProviderSettings = await r.json()
      setSettings(updated)
      setOllamaEnabled(updated.ollama_enabled)
      setAnthropicEnabled(updated.anthropic_enabled)
      setOpenaiEnabled(updated.openai_enabled)
      setOllamaBase(updated.ollama_base_url)
      // REHYDRATE FROM THE RESPONSE. The input kept displaying whatever was
      // typed, so a value the server did not store still read back as the
      // current setting under a green "Saved" banner. What the server returns
      // is what the server has.
      setRagThreshold(String(updated.rag_similarity_threshold))
      setAnthropicKey(''); setOpenaiKey('')
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch { setError('Request failed') }
    finally { setSaving(false) }
  }

  const testOllama = async () => {
    setTesting(true); setTestResult(null)
    try {
      const r = await fetch(`${api}/api/settings/test-ollama`, { headers: headers() })
      const d = await r.json()
      setTestResult(d.ok
        ? { ok: true, message: `Connected - ${d.model_count} model(s) available` }
        : { ok: false, message: d.error || 'Connection failed' })
    } catch { setTestResult({ ok: false, message: 'Request failed' }) }
    finally { setTesting(false) }
  }

  const ToggleRow = ({ label, desc, value, onChange }: { label: string; desc: string; value: boolean; onChange: (v: boolean) => void }) => (
    <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        <p className="text-xs text-gray-500 mt-0.5">{desc}</p>
      </div>
      <button
        onClick={() => onChange(!value)}
        className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${value ? 'bg-blue-600' : 'bg-gray-600'}`}
      >
        <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${value ? 'left-5' : 'left-0.5'}`} />
      </button>
    </div>
  )

  // Lit only while the form differs from what the server last returned. A
  // typed key always counts: a blank key box means "keep the current one".
  const dirty = !!settings && (
    ollamaEnabled !== settings.ollama_enabled ||
    anthropicEnabled !== settings.anthropic_enabled ||
    openaiEnabled !== settings.openai_enabled ||
    ollamaBase !== settings.ollama_base_url ||
    anthropicKey.trim() !== '' || openaiKey.trim() !== '' ||
    parseFloat(ragThreshold) !== settings.rag_similarity_threshold
  )

  if (!settings) return <p className="text-xs text-gray-500">{error || 'Loading…'}</p>

  return (
    <div className="max-w-xl space-y-8">
      {stepUp.prompt}

      {/* Providers */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Providers</p>
        <ToggleRow
          label="Ollama (local models)"
          desc="Route requests with no cloud prefix to your Ollama instance"
          value={ollamaEnabled}
          onChange={setOllamaEnabled}
        />
        <ToggleRow
          label="Anthropic (Claude)"
          desc="Enable claude-* models - requires API key"
          value={anthropicEnabled}
          onChange={setAnthropicEnabled}
        />
        <ToggleRow
          label="OpenAI (GPT / o-series)"
          desc="Enable gpt-* and o* models - requires API key"
          value={openaiEnabled}
          onChange={setOpenaiEnabled}
        />
      </section>

      {/* Ollama */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Ollama</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Base URL</label>
          <div className="flex gap-2">
            <input
              value={ollamaBase}
              onChange={e => { setOllamaBase(e.target.value); setTestResult(null) }}
              placeholder="http://host.docker.internal:11434"
              className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500 font-mono"
            />
            <button
              onClick={testOllama}
              disabled={testing}
              className="px-3 py-2 text-xs rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 disabled:opacity-50 transition-colors whitespace-nowrap"
            >
              {testing ? 'Testing…' : 'Test'}
            </button>
          </div>
          {testResult && (
            <p className={`text-xs mt-1.5 ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
              {testResult.ok ? '✓' : '✕'} {testResult.message}
            </p>
          )}
          <p className="text-xs text-gray-600 mt-1">
            For cloud-to-local GPU routing: run <span className="font-mono bg-gray-800 px-1 rounded">cloudflared tunnel --url http://localhost:11434</span> and paste the tunnel URL here.
          </p>
        </div>
      </section>

      {/* Default model - shown here, set in Models */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">Default Model</p>
        <DefaultModelPointer model={settings.default_model} goToModels={goToModels} />
      </section>

      {/* API Keys */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">API Keys</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Anthropic API Key
            {settings.anthropic_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          {/* new-password on both key fields: blank-means-keep, so a browser
              autofilling the operator's login here would silently OVERWRITE
              the real key on Save. */}
          <input
            type="password"
            autoComplete="new-password"
            value={anthropicKey}
            onChange={e => setAnthropicKey(e.target.value)}
            placeholder={settings.anthropic_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-ant-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            OpenAI API Key
            {settings.openai_key_set && <span className="ml-2 text-green-500">● set</span>}
          </label>
          <input
            type="password"
            autoComplete="new-password"
            value={openaiKey}
            onChange={e => setOpenaiKey(e.target.value)}
            placeholder={settings.openai_key_set ? '●●●●●●●● (leave blank to keep current)' : 'sk-…'}
            className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-gray-500"
          />
        </div>
        <p className="text-xs text-gray-600">Keys are stored in the instance database. Leave blank to keep the current value.</p>
      </section>

      {/* RAG */}
      <section className="space-y-3">
        <p className="text-xs text-gray-400 font-medium uppercase tracking-widest">RAG</p>
        <div>
          <label className="block text-xs text-gray-500 mb-1">Similarity Threshold (0.0 - 1.0)</label>
          <input
            type="number"
            min="0" max="1" step="0.05"
            value={ragThreshold}
            onChange={e => setRagThreshold(e.target.value)}
            className="w-32 bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-gray-500"
          />
          <p className="text-xs text-gray-600 mt-1">Chunks scoring below this value are excluded from context. Higher = stricter matching.</p>
        </div>
      </section>

      {/* Save */}
      <div className="flex items-center gap-3 pt-2 border-t border-gray-800">
        {error && <p className="text-xs text-red-400 flex-1">{error}</p>}
        <div className="ml-auto flex items-center gap-3">
          <SaveStatus dirty={dirty} saved={saved} savedText="Saved - changes take effect immediately." />
          <button
            onClick={save}
            disabled={!dirty || saving}
            className="px-5 py-2 rounded-lg text-sm font-medium text-white disabled:opacity-40 transition-colors"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            {saving ? 'Saving…' : 'Save Settings'}
          </button>
        </div>
      </div>
    </div>
  )
}
