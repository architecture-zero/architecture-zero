import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'
import { useGuardedPatch, NEVER_LOADED, SaveErrorLine, SwitchRow } from './settingsControls'

// ── Tab: Guest Access ──────────────────────────────────────────────────────

// Who may chat without signing in, and the operator's record that the host's
// disk is encrypted. Both sat at the bottom of the System Prompt tab until
// 2026-10-03, below the chat suggestions - a security control where nobody
// looking for one would look.
export function GuestAccessTab({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [loading, setLoading] = useState(true)
  // Starts CLOSED and is corrected by /api/config. It started ON, so a failed
  // read drew guests as allowed on an instance that refuses them - a control
  // panel lying about a control is worse than one that is absent.
  const [guestChatEnabled, setGuestChatEnabled] = useState(false)
  // null until the read answers: neither sentence about the host's half is
  // true before then.
  const [guestEnvAllowed, setGuestEnvAllowed] = useState<boolean | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [saved, setSaved] = useState(false)
  const [encryptionVerified, setEncryptionVerified] = useState(false)
  const { guardedPatch, saveErr, setSaveErr } = useGuardedPatch(api, headers)

  useEffect(() => {
    guardedJson<{ guest_mode_configured?: boolean; guest_mode_env_allowed?: boolean }>(
      fetch(`${api}/api/config`, { headers: headers() }), 'Loading settings')
      .then(d => {
        if (d) {
          // The STORED half, not the effective one. This switch edits the
          // row; binding it to `guest_mode_enabled` (which is env AND row)
          // meant a save wrote the AND back and erased a deliberate setting
          // whenever the env half was off.
          if (d.guest_mode_configured !== undefined) setGuestChatEnabled(d.guest_mode_configured)
          if (d.guest_mode_env_allowed !== undefined) setGuestEnvAllowed(d.guest_mode_env_allowed)
          setLoaded(true)
        }
        setLoading(false)
      })
    guardedJson<{ encryption_verified?: boolean }>(
      fetch(`${api}/api/admin/context`, { headers: headers() }), 'Loading context settings')
      .then(d => { if (d) setEncryptionVerified(!!d.encryption_verified) })
  }, [])

  // Sends guest_mode_enabled and nothing else. Before the split every chat
  // control's save carried it, so flipping retrieval rewrote the guest row.
  const flipGuest = () => {
    if (!loaded) { setSaveErr(NEVER_LOADED); return }
    const cur = guestChatEnabled
    setGuestChatEnabled(!cur)
    guardedPatch('/api/admin/config', { guest_mode_enabled: !cur }).then(ok => {
      if (!ok) { setGuestChatEnabled(cur); return }
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    })
  }

  if (loading) return <p className="text-xs text-gray-500">Loading…</p>

  return (
    <div className="space-y-6">
      <SaveErrorLine text={saveErr} />
      <div className="space-y-3">
        <SwitchRow
          label="Guest access"
          desc={<>
            Allow unauthenticated chat; when off, visitors get the sign-in wall.
            This is the admin half of a double gate.
            {guestEnvAllowed === true && ' The host has set ALLOW_GUEST_MODE=true, so this control decides.'}
            {guestEnvAllowed === false && ' The host has NOT set ALLOW_GUEST_MODE=true, so guests stay refused whatever this says - your setting is stored and takes effect if that changes.'}
          </>}
          on={guestChatEnabled}
          onToggle={flipGuest}
        />
        {saved && <p className="text-xs text-green-400 text-right">Saved</p>}
      </div>

      {/* Encryption at rest */}
      <div className="border-t border-gray-800 pt-5 space-y-2">
        <p className="text-xs text-gray-300 font-medium mb-1">Encryption at Rest</p>
        <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border text-sm ${
          encryptionVerified
            ? 'bg-green-900/20 border-green-700/40 text-green-300'
            : 'bg-gray-800/50 border-gray-700 text-gray-500'
        }`}>
          <span className={`text-base ${encryptionVerified ? 'text-green-400' : 'text-gray-600'}`}>
            {encryptionVerified ? '🔒' : '⚠'}
          </span>
          <div>
            <p className="text-xs font-medium">
              {encryptionVerified ? 'Host-verified' : 'Not verified'}
            </p>
            <p className="text-xs opacity-60 mt-0.5">
              {encryptionVerified
                ? 'ENCRYPTION_AT_REST_VERIFIED=true - host-level encryption confirmed'
                : 'Set ENCRYPTION_AT_REST_VERIFIED=true after enabling host-level or volume encryption - this flag records that you did it, it does not perform it.'}
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
