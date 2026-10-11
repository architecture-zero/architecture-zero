import { useState } from 'react'
import type { ReactNode } from 'react'
import { PRIMARY_COLOR } from '../theme'

// ── Shared by the System Prompt, Chat Controls and Guest Access tabs ──────

// Never confirm a save the server did not accept. An expired token makes
// these PATCHes 401 while the UI still flashes "Saved", and the operator
// walks away believing a setting changed - the failure is silent on both
// sides, which is what makes it worth a helper rather than a habit.
export function useGuardedPatch(api: string, headers: () => Record<string, string>) {
  const [saveErr, setSaveErr] = useState('')
  const guardedPatch = async (path: string, body: object): Promise<boolean> => {
    setSaveErr('')
    try {
      const res = await fetch(`${api}${path}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...headers() },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        setSaveErr(res.status === 401
          ? 'NOT saved - your session expired. Sign in again, then retry.'
          : `NOT saved - server returned ${res.status}.`)
        return false
      }
      return true
    } catch {
      setSaveErr('NOT saved - request failed (network).')
      return false
    }
  }
  return { guardedPatch, saveErr, setSaveErr }
}

export const NEVER_LOADED = 'NOT saved - these settings never loaded, so saving would '
  + 'overwrite them with defaults. Reload the page and try again.'

export function SaveErrorLine({ text }: { text: string }) {
  if (!text) return null
  return (
    <p className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg px-3 py-2">
      {text}
    </p>
  )
}

export function SwitchRow({ label, desc, on, onToggle }: {
  label: string; desc: ReactNode; on: boolean; onToggle: () => void
}) {
  return (
    <div className="flex items-center justify-between px-3 py-2.5 rounded-lg bg-gray-800/50 border border-gray-700">
      <div>
        <p className="text-sm text-gray-300">{label}</p>
        <p className="text-xs text-gray-500 mt-0.5">{desc}</p>
      </div>
      <button
        role="switch"
        aria-checked={on}
        aria-label={label}
        onClick={onToggle}
        className={`w-10 h-5 rounded-full transition-colors relative flex-shrink-0 ${on ? 'bg-blue-600' : 'bg-gray-600'}`}
      >
        <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-all duration-200 ${on ? 'left-5' : 'left-0.5'}`} />
      </button>
    </div>
  )
}

// The default model has ONE control, in Models (2026-10-03). It used to have
// three - the System Prompt tab, Settings and Models - all writing the same
// default_model, so an operator could not tell which one was the setting.
// The other tabs show it and point there.
export function DefaultModelPointer({ model, goToModels }: { model: string; goToModels?: () => void }) {
  return (
    <p className="text-xs text-gray-500">
      Default model: <span className="font-mono text-gray-300">{model || 'the server default'}</span>
      {' - '}
      {goToModels
        ? <button type="button" onClick={goToModels} className="underline underline-offset-2 hover:text-white">change it in Models</button>
        : 'change it in Models'}
    </p>
  )
}

// One Save button's honest states (the 2026-08-01 UI audit's dirty-state
// item, the shape the Models tab already had): disabled while nothing differs
// from what the server holds, "Unsaved changes" beside it once something does,
// and "Saved" only after the server accepted the write. A button that is
// always lit says nothing about whether there is anything to save.
export function SaveStatus({ dirty, saved, savedText = 'Saved' }: { dirty: boolean; saved: boolean; savedText?: string }) {
  if (dirty) return <span className="text-xs text-amber-400">Unsaved changes</span>
  if (saved) return <span className="text-xs text-green-400">{savedText}</span>
  return <span className="text-xs text-gray-600">No unsaved changes</span>
}

export function SaveBar({ label, dirty, saving = false, saved, onSave }: {
  label: string; dirty: boolean; saving?: boolean; saved: boolean; onSave: () => void
}) {
  return (
    <div className="flex items-center justify-end gap-3">
      <SaveStatus dirty={dirty} saved={saved} />
      <button
        onClick={onSave}
        disabled={!dirty || saving}
        className="px-5 py-2 rounded-lg text-sm font-medium text-white transition-colors disabled:opacity-40"
        style={{ backgroundColor: PRIMARY_COLOR }}
      >
        {saving ? 'Saving…' : label}
      </button>
    </div>
  )
}
