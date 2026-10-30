// ── Header ─────────────────────────────────────────────────────────────────
// The top bar: the sidebar toggle, the Help and Trust pills, the model that
// answers, and the account controls (Admin, Guest, Sign in, the profile, Sign
// out) - the shell's JSX, moved as it was in the App split (2026-10-11); every
// prop is one of the shell's locals under its own name.
import type { AuthUser, View } from './types'
import { PRIMARY_COLOR, ON_PRIMARY } from '../theme'

interface ChatHeaderProps {
  sidebarOpen: boolean
  setSidebarOpen: (open: boolean) => void
  helpEnabled: boolean
  helpMode: boolean
  enterHelp: () => void
  leaveHelp: () => void
  instanceName: string
  answeringModel: string
  currentUser: AuthUser | null
  setView: (view: View) => void
  isGuest: boolean
  setProfileOpen: (open: boolean) => void
  handleLogout: () => void
}

export function ChatHeader({
  sidebarOpen, setSidebarOpen, helpEnabled, helpMode, enterHelp, leaveHelp, instanceName, answeringModel,
  currentUser, setView, isGuest, setProfileOpen, handleLogout,
}: ChatHeaderProps) {
  return (
    <header className="h-14 border-b border-gray-800 bg-gray-900/40 flex items-center px-4 gap-3 flex-shrink-0 backdrop-blur-sm">
      <button onClick={() => setSidebarOpen(!sidebarOpen)} className="text-gray-500 hover:text-white transition-colors p-1 rounded">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="3" y1="6" x2="21" y2="6" /><line x1="3" y1="12" x2="21" y2="12" /><line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>
      <div className="flex-1" />
      {/* In-product help: the same chat, pointed at the product's own help
          pages. A toggle, not a page - pressed means the next question is
          about the assistant; pressed again returns to the knowledge base.
          Hidden when the server runs without the help lane. */}
      {helpEnabled && (
        <button
          onClick={() => (helpMode ? leaveHelp() : enterHelp())}
          aria-pressed={helpMode}
          title={helpMode
            ? 'Back to your documents'
            : `How ${instanceName} works - signing in, documents and who can see them, privacy - answered from its own help pages`}
          className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
            helpMode ? 'border-transparent' : 'text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 border-gray-700'
          }`}
          style={helpMode ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}
        >
          Help
        </button>
      )}
      {/* The measured-trust page is the campaign's proof surface - it must be
          reachable from the front door, not only from inside the admin tour. */}
      <a
        href="/#trust"
        target="_blank"
        rel="noopener noreferrer"
        title="How much should you trust this assistant? Live-derived evaluation numbers - honesty, correctness, a locked holdout exam, grounding, freshness"
        className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
      >
        Trust
      </a>
      {/* The model that answers this request (answeringModel): the user's
          pick when one is sent and honoured, else the server's effective
          model. It fell back to `model` when the server had not said - a
          guess, which an untouched request does not send. Unknown shows
          nothing. */}
      {answeringModel && (
        <span className="text-xs text-gray-500 bg-gray-800 px-2.5 py-1 rounded-full border border-gray-700">{answeringModel}</span>
      )}
      {/* view_audit_log is granted on its own, outside every preset but the
          Owner's, so it opens the panel by itself - or the Audit Log tab is
          out of reach for exactly the account it was granted to (2026-10-30). */}
      {currentUser && (currentUser.role === 'owner' || currentUser.role === 'admin' || currentUser.permissions?.some(p => ['manage_users', 'manage_system', 'manage_kb', 'view_analytics', 'view_audit_log'].includes(p))) && (
        <button
          onClick={() => setView('admin')}
          title={currentUser ? 'Admin panel' : 'Read-only tour of the system behind this assistant: settings, knowledge base, models, and federation'}
          className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
        >
          {currentUser ? 'Admin' : 'System'}
        </button>
      )}
      {isGuest && (
        <span className="text-xs text-gray-500 bg-gray-800 px-2.5 py-1 rounded-full border border-gray-700">
          Guest
        </span>
      )}
      {/* Any unauthenticated viewer gets the door. A signed-out visitor
          who is not flagged as a guest is still someone who may have an
          account, and keying this off anything narrower has already
          produced an instance whose owner could not reach a login screen. */}
      {!currentUser && (
        <button
          onClick={() => setView('login')}
          title="Owner / admin sign in"
          className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
        >
          Sign in
        </button>
      )}
      {currentUser && (
        <>
          <button
            onClick={() => setProfileOpen(true)}
            className="text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 px-2.5 py-1 rounded-full border border-gray-700 transition-colors"
            title="Account settings"
          >
            {currentUser.username}
          </button>
          <button
            onClick={handleLogout}
            className="text-xs text-gray-500 hover:text-red-400 transition-colors"
          >
            Sign out
          </button>
        </>
      )}
    </header>
  )
}
