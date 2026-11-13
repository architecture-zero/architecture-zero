import { useState } from 'react'
import { PRIMARY_COLOR } from '../theme'

// ── The step-up prompt (2026-09-09) ────────────────────────────────────────
// Creating an account, changing a role, adding manage_users/manage_system,
// or registering a peer costs the operator's OWN password server-side
// (jwt_auth.require_step_up: a bearer token proves possession of a browser,
// the password proves the person). Create takes it in its form; every other
// gated write asks through this prompt, which resolves to the password or to
// null on cancel. The server answers a string 400 on a wrong one, and the
// callers show it - a silent snap-back was the failure the attack pass named.
//
// One write takes a different proof: unlocking the operator's OWN account
// costs a current authenticator code, not the password (a locked account's
// password is not accepted anywhere). ask(what, 'code') shows the same
// prompt worded for the code, and resolves to what was typed or to null.

type StepUpKind = 'password' | 'code'
type StepUpRequest = { what: string; kind: StepUpKind; resolve: (proof: string | null) => void }

export function useStepUp() {
  const [req, setReq] = useState<StepUpRequest | null>(null)
  const ask = (what: string, kind: StepUpKind = 'password') =>
    new Promise<string | null>(resolve => setReq({ what, kind, resolve }))
  const prompt = req
    ? <StepUpPrompt what={req.what} kind={req.kind} onDone={pw => { req.resolve(pw); setReq(null) }} />
    : null
  return { ask, prompt }
}

function StepUpPrompt({ what, kind, onDone }: {
  what: string
  kind: StepUpKind
  onDone: (pw: string | null) => void
}) {
  const [pw, setPw] = useState('')
  const code = kind === 'code'
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => onDone(null)}>
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-5 w-full max-w-sm space-y-3" onClick={e => e.stopPropagation()}>
        <p className="text-sm text-white font-medium">
          {code ? 'Confirm with your authenticator code' : 'Confirm with your password'}
        </p>
        <p className="text-xs text-gray-400">
          {code
            ? `To ${what}, enter the current code from your authenticator app.`
            : `To ${what}, enter the password of the account you are signed in as.`}
        </p>
        <input
          type={code ? 'text' : 'password'}
          inputMode={code ? 'numeric' : undefined}
          autoFocus
          autoComplete={code ? 'one-time-code' : 'current-password'}
          placeholder={code ? 'Authenticator code' : 'Your password'}
          value={pw}
          onChange={e => setPw(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && pw) onDone(pw); if (e.key === 'Escape') onDone(null) }}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500/60"
        />
        <div className="flex justify-end gap-2">
          <button onClick={() => onDone(null)} className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200">Cancel</button>
          <button
            onClick={() => onDone(pw)}
            disabled={!pw}
            className="px-3 py-1.5 rounded-lg text-xs font-medium text-white disabled:opacity-50"
            style={{ backgroundColor: PRIMARY_COLOR }}
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  )
}
