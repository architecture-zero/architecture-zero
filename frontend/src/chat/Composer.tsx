// ── Composer ───────────────────────────────────────────────────────────────
// The box under the transcript: the refusal notice, the textarea, the
// send-or-stop button and the line naming what answers - the shell's JSX, moved
// as it was in the App split (2026-10-11); every prop is one of the shell's
// locals under its own name.
import type { RefObject } from 'react'
import { PRIMARY_COLOR } from '../theme'

interface ComposerProps {
  turnNotice: string | null
  textareaRef: RefObject<HTMLTextAreaElement>
  guestAtLimit: boolean
  helpMode: boolean
  instanceName: string
  input: string
  onInput: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void
  busy: boolean
  send: (text?: string) => void
  stopGeneration: () => void
  providerLabel: string
}

export function Composer({
  turnNotice, textareaRef, guestAtLimit, helpMode, instanceName, input, onInput, onKeyDown, busy, send, stopGeneration,
  providerLabel,
}: ComposerProps) {
  return (
    <div className="border-t border-gray-800 bg-gray-900/30 p-4">
      <div className="max-w-3xl mx-auto">
        {turnNotice && (
          <div className="text-xs text-gray-300 px-1 pb-2" role="status">{turnNotice}</div>
        )}
        <div className="flex gap-3 items-end bg-gray-800 border border-gray-700 focus-within:border-blue-500/60 rounded-2xl px-4 py-3 transition-colors">
          <textarea
            ref={textareaRef}
            className="flex-1 bg-transparent text-white text-sm resize-none outline-none placeholder-gray-500 leading-relaxed disabled:opacity-50"
            placeholder={guestAtLimit ? 'Sign in to continue chatting…' : helpMode ? `Ask how ${instanceName} works...  (Ctrl+Enter to send)` : `Message ${instanceName}...  (Ctrl+Enter to send)`}
            value={input}
            rows={1}
            // Typing is allowed mid-answer; SENDING is not. send() carries
            // the real guard (busy), so a draft cannot become a second
            // concurrent stream. Disabling the box outright also prevented
            // the overlap, but it locked the composer for the whole answer -
            // on a long one you could not even draft the next question, and
            // an unlucky keystroke landed nowhere. Before the streaming fix
            // this box re-enabled a second in, at the first token, which is
            // what let a second stream start; the fix belongs on the send
            // path, not on the keyboard.
            disabled={guestAtLimit}
            onChange={onInput}
            onKeyDown={onKeyDown}
          />
          <button
            onClick={busy ? stopGeneration : () => send()}
            disabled={guestAtLimit || (!busy && !input.trim())}
            className="w-9 h-9 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-xl flex items-center justify-center transition-colors flex-shrink-0 shadow-sm"
            style={busy ? { backgroundColor: '#dc2626' } : (!input.trim() ? {} : { backgroundColor: PRIMARY_COLOR })}
            title={busy ? 'Stop generation' : 'Send message'}
          >
            {busy ? (
              <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
                <rect x="3" y="3" width="18" height="18" rx="2" />
              </svg>
            ) : (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            )}
          </button>
        </div>
        {/* Names the MODEL actually answering (answeringModel): the user's
            pick when one is sent and honoured, else chat_model_effective
            from /api/config - the server's own resolved value, not an
            inference this client makes. (The comment here used to say /api/status;
            that is where an earlier version read status.provider.provider,
            which is the ENABLED-PROVIDER SET rather than the thing that
            answered, and reports the literal string "multi" when more than
            one is on.) It read "Claude API" hardcoded before that - on a
            template that speaks to Ollama, Anthropic, OpenAI, Gemini,
            Mistral, Groq, xAI and DeepSeek, and whose shipped default is
            local Ollama with no Anthropic key configured at all. So the
            stock deployment credited a vendor it was not using, in the one
            line a visitor reads to find out what answered them.
            This client never sends `model` unless the user picked one, so
            the server's chat_model pin resolves and what is named here is
            what ran. A guest's boot read of /api/config is skipped (no
            token), so effectiveModel stays empty and the segment is omitted
            rather than guessed. */}
        <p className="text-center text-xs text-gray-600 mt-2">
          Powered by Architecture Zero{providerLabel ? ` · ${providerLabel}` : ''} · responses are AI-generated
        </p>
      </div>
    </div>
  )
}
