import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { PRIMARY_COLOR } from '../theme'

// ── Tab: Models ────────────────────────────────────────────────────────────

interface ModelGroup { provider: string; label: string; models: { value: string; label: string; badge: string }[] }
// `resolved` is the version a slot sends right now: a family alias ("Claude
// Opus - newest") resolved to its newest model, any other value as it is.
interface ModelSlot { value: string; effective?: string; resolved?: string; default: string; overridden: boolean }
// `default` is default_model; `chat` is the chat_model pin over it ("" = follow
// the default) - the server's two keys, both of them shown below.
interface ModelConfig { default: ModelSlot; chat: ModelSlot; eval_writer: ModelSlot; eval_judge: ModelSlot; same_family_warning: boolean }
type ModelKey = 'default' | 'chat' | 'eval_writer' | 'eval_judge'

function ModelSelect(props: {
  groups: ModelGroup[]
  value: string
  onChange: (v: string) => void
  followOption?: string   // label for a "" option (the eval writer's default)
  savedLabel?: string     // how a value the list does not show is marked (default "saved")
}) {
  const known = new Set(props.groups.flatMap(g => g.models.map(m => m.value)))
  return (
    <select value={props.value} onChange={e => props.onChange(e.target.value)}
      className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-gray-500 min-w-[260px]">
      {props.followOption !== undefined && <option value="">{props.followOption}</option>}
      {props.value && !known.has(props.value) && (
        <option value={props.value}>{props.value} ({props.savedLabel || 'saved'})</option>
      )}
      {props.groups.map(g => (
        <optgroup key={`${g.provider}:${g.label}`} label={g.label}>
          {g.models.map(m => (
            <option key={m.value} value={m.value}>
              {m.label}{m.badge ? ` - ${m.badge}` : ''}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  )
}

// The default model's one control (2026-10-03). This tab showed the chat pin
// and the two eval slots but never the server's `default` slot, so once the
// System Prompt tab and Settings stopped editing default_model nothing in the
// admin could set it.
const draftOf = (d: ModelConfig): Record<ModelKey, string> =>
  ({ default: d.default.value, chat: d.chat.value, eval_writer: d.eval_writer.value, eval_judge: d.eval_judge.value })

export function ModelsTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [groups, setGroups] = useState<ModelGroup[]>([])
  const [cfg, setCfg] = useState<ModelConfig | null>(null)
  const [draft, setDraft] = useState<Record<ModelKey, string> | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  // Off by default: each list shows what is current - one entry per Claude
  // family ("newest"), the newest model of every other family - never every
  // version. On, every version the providers list appears too, so a setting
  // can be PINNED to one: the lever when a new release misbehaves, or for a
  // deployment that must hold one version.
  const [allVersions, setAllVersions] = useState(false)

  useEffect(() => {
    guardedJson<{ groups?: ModelGroup[] }>(
      fetch(`${api}/api/models${allVersions ? '?all_versions=true' : ''}`, { headers: headers() }),
      'Loading models')
      .then(d => { if (d) setGroups(d.groups || []) })
  }, [api, allVersions])

  useEffect(() => {
    guardedJson<ModelConfig>(
      fetch(`${api}/api/admin/model-config`, { headers: headers() }), 'Loading model config')
      .then(d => {
        if (!d) return
        setCfg(d)
        setDraft(draftOf(d))
      })
  }, [api])

  const dirty = !!cfg && !!draft && (Object.keys(draft) as ModelKey[]).some(k => draft[k] !== cfg[k].value)

  const save = async () => {
    if (!draft || !cfg) return
    setSaving(true); setMsg('')
    // Only the slots that changed. The whole draft used to go, so saving one
    // slot wrote every other slot's current value into a stored row - and a
    // slot showing its default (DEFAULT_MODEL, the judge's default) then
    // stayed pinned to that literal when the default itself changed.
    const changed = Object.fromEntries(
      (Object.keys(draft) as ModelKey[]).filter(k => draft[k] !== cfg[k].value).map(k => [k, draft[k]]))
    try {
      const r = await fetch(`${api}/api/admin/model-config`, {
        method: 'PATCH',
        headers: { ...headers(), 'Content-Type': 'application/json' },
        body: JSON.stringify(changed),
      })
      if (!r.ok) { const d = await r.json().catch(() => ({})); setMsg(d.detail || `Save failed (HTTP ${r.status})`); return }
      const d: ModelConfig = await r.json()
      setCfg(d)
      setDraft(draftOf(d))
      setMsg('Saved')
    } catch {
      setMsg('Save failed - could not reach the backend')
    } finally {
      setSaving(false)
    }
  }

  if (!cfg || !draft) return <p className="text-xs text-gray-600">Loading model config…</p>

  // A "" value means "follow the default model" for the chat pin and the eval
  // writer - the server's own chains (chat: chat_model else default_model;
  // writer: eval_answer_model else default_model, never via the chat pin).
  const rows: { key: ModelKey; label: string; desc: string; slot: ModelSlot; followOption?: string }[] = [
    { key: 'default', label: 'Default model', slot: cfg.default,
      desc: 'What chat and the eval writer use when nothing is pinned for them.' },
    { key: 'chat', label: 'Chat', slot: cfg.chat, followOption: 'Follow the default model',
      desc: 'What visitors get when they do not pick a model.' },
    { key: 'eval_writer', label: 'Eval answer writer', slot: cfg.eval_writer, followOption: 'Follow the default model',
      desc: 'Answers the eval questions. A family rolls with new releases, so the next run shows what a new model brings; every run records the exact version it used.' },
    { key: 'eval_judge', label: 'Eval judge', slot: cfg.eval_judge,
      desc: 'Grades every answer - the ruler, so it stays put: picking a family pins the version it is today. Must come from a different company than the writer - the guard blocks same-family runs.' },
  ]
  const isPinned = (k: ModelKey) => k === 'eval_judge'

  // One row per setting - its name on the left, its dropdown on the right - in
  // one list: the dropdowns ARE the catalogue (a second list of every model
  // below them made the tab long and said nothing the dropdowns do not).
  return (
    <div className="space-y-4 max-w-3xl">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs text-gray-500 uppercase tracking-widest">Models</p>
          <p className="text-xs text-gray-500 mt-1">
            A family ("Claude Opus - newest") moves to each new release by itself; a version stays put.
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
          <input type="checkbox" checked={allVersions}
            onChange={e => setAllVersions(e.target.checked)} />
          Show every version (to pin one)
        </label>
      </div>

      {cfg.same_family_warning && (
        <div className="px-4 py-3 rounded-xl border border-amber-500/40 bg-amber-500/10 text-sm text-amber-300">
          ⚠ The eval writer and judge resolve to the SAME provider family - eval runs
          will be refused until one changes (or is deliberately overridden per run).
        </div>
      )}

      <div className="bg-gray-800/40 border border-gray-700/50 rounded-xl divide-y divide-gray-700/50">
        {rows.map(row => {
          const draftVal = draft[row.key]
          const overridden = row.key === 'eval_writer' ? draftVal !== '' : draftVal !== row.slot.default
          return (
            <div key={row.key} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium text-gray-200">{row.label}</p>
                  {/* "set here", not "overridden from default": an explicit value
                      can equal the default - the tag marks the setting, and
                      claiming a difference that may not exist reads as a bug. */}
                  {overridden ? (
                    <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-300">set here</span>
                  ) : (
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-700/50 text-gray-500">default</span>
                  )}
                </div>
                <p className="text-xs text-gray-500 mt-0.5">{row.desc}
                  {(row.key === 'eval_writer' || row.key === 'chat') && row.slot.effective && draftVal === '' &&
                    ` Currently resolves to ${row.slot.effective}.`}
                  {draftVal === row.slot.value && row.slot.resolved &&
                    row.slot.resolved !== (draftVal || row.slot.effective) &&
                    ` That is ${row.slot.resolved} today.`}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <ModelSelect groups={groups} value={draftVal}
                  followOption={row.followOption}
                  savedLabel={isPinned(row.key) ? 'pinned' : undefined}
                  onChange={v => { setMsg(''); setDraft({ ...draft, [row.key]: v }) }} />
                {overridden && (
                  <button
                    onClick={() => { setMsg(''); setDraft({ ...draft, [row.key]: row.key === 'eval_writer' ? '' : row.slot.default }) }}
                    className="text-xs text-gray-500 hover:text-white underline">
                    reset
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="flex items-center gap-3">
        <button onClick={save} disabled={!dirty || saving}
          className="px-5 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-40"
          style={{ backgroundColor: PRIMARY_COLOR }}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        {msg && <span className={`text-xs ${msg === 'Saved' ? 'text-emerald-400' : 'text-red-400'}`}>{msg}</span>}
        {!dirty && !msg && <span className="text-xs text-gray-600">No unsaved changes</span>}
        {dirty && !msg && <span className="text-xs text-amber-400">Unsaved changes</span>}
      </div>
      {groups.length === 0 && (
        <p className="text-xs text-gray-600">No models available. Check provider flags in .env.</p>
      )}
    </div>
  )
}
