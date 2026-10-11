// ── Sidebar ────────────────────────────────────────────────────────────────
// The brand block, New Chat, the conversation list, the model picker, the RAG
// switch and the status rows down the left of the chat screen - the shell's JSX,
// moved as it was in the App split (2026-10-11); every prop is one of the
// shell's locals under its own name.
import type { SessionEntry, SysStatus, Analytics, ModelOption, ModelGroup } from './types'
import { Toggle } from './Toggle'
import { PRIMARY_COLOR, ON_PRIMARY } from '../theme'
import HistoryList from '../HistoryList'

interface SidebarProps {
  initials: string
  instanceName: string
  newChat: () => void
  isGuest: boolean
  sessionList: SessionEntry[]
  sessionId: string
  switchSession: (id: string) => void
  renameSession: (id: string, name: string) => Promise<boolean>
  deleteSession: (id: string) => Promise<boolean>
  allowModelSelection: boolean
  modelGroups: ModelGroup[]
  model: string
  setModel: (value: string) => void
  setModelTouched: (touched: boolean) => void
  allowRagToggle: boolean
  useRag: boolean
  setUseRag: (on: boolean) => void
  setRagTouched: (touched: boolean) => void
  sysStatus: SysStatus | null
  analytics: Analytics | null
}

export function Sidebar({
  initials, instanceName, newChat, isGuest, sessionList, sessionId, switchSession, renameSession, deleteSession,
  allowModelSelection, modelGroups, model, setModel, setModelTouched, allowRagToggle, useRag, setUseRag, setRagTouched,
  sysStatus, analytics,
}: SidebarProps) {
  return (
    <aside className="w-64 bg-gray-900 border-r border-gray-800 flex flex-col flex-shrink-0">
      <div className="p-5 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center font-bold text-sm" style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}>{initials}</div>
          <div>
            <p className="font-semibold text-sm leading-tight">{instanceName}</p>
            <p className="text-xs text-gray-500">Secure AI over your knowledge base</p>
          </div>
        </div>
      </div>


      <div className="p-3 border-b border-gray-800">
        <button
          onClick={newChat}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm text-gray-400 hover:text-white hover:bg-gray-800 border border-gray-700 hover:border-gray-600 transition-all"
        >
          <span className="text-base font-light">+</span> New Chat
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-5">
        {!isGuest && sessionList.length > 0 && (
          <HistoryList
            entries={sessionList}
            activeId={sessionId}
            activeStyle={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}
            onSelect={id => switchSession(id)}
            onRename={renameSession}
            onDelete={deleteSession}
          />
        )}
        {allowModelSelection && modelGroups.length > 0 && (
          <div className="space-y-3">
            {modelGroups.map(group => (
              <div key={group.provider}>
                <p className="text-xs text-gray-500 uppercase tracking-widest mb-1 px-1">{group.label}</p>
                <div className="space-y-1">
                  {group.models.map((m: ModelOption) => (
                    <button
                      key={m.value}
                      onClick={() => { setModel(m.value); setModelTouched(true) }}
                      className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-all ${
                        model === m.value ? '' : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                      }`}
                      style={model === m.value ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}
                    >
                      <span className="truncate text-left">{m.label}</span>
                      <span className={`text-xs px-1.5 py-0.5 rounded ml-2 flex-shrink-0 ${
                        model === m.value ? 'bg-blue-500/60 text-blue-100' : 'bg-gray-700 text-gray-400'
                      }`}>{m.badge}</span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
        {allowRagToggle && (
          <div>
            <p className="text-xs text-gray-500 uppercase tracking-widest mb-2 px-1">Knowledge Base</p>
            <div className={`flex items-center justify-between px-3 py-2.5 rounded-lg border text-sm transition-all ${
              useRag ? 'bg-blue-600/10 border-blue-500/40 text-blue-300' : 'bg-gray-800/50 border-gray-700 text-gray-400'
            }`}>
              <span>RAG Mode</span>
              <Toggle enabled={useRag} onToggle={() => { setUseRag(!useRag); setRagTouched(true) }} />
            </div>
          </div>
        )}
      </div>

      <div className="p-4 border-t border-gray-800 space-y-2">
        {(() => {
          const _prov = sysStatus?.provider?.provider ?? 'ollama'
          const _cloud = _prov !== 'ollama'
          const _ok = _cloud ? !!sysStatus : sysStatus?.ollama === 'connected'
          const _label = _cloud ? 'connected' : (sysStatus?.ollama ?? '…')
          return (
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${_ok ? 'bg-green-500 animate-pulse' : 'bg-red-500'}`} />
                <span className="text-xs text-gray-500 capitalize">{_prov}</span>
              </div>
              <span className={`text-xs ${_ok ? 'text-green-500' : 'text-red-400'}`}>{_label}</span>
            </div>
          )
        })()}
        <div className="flex items-center justify-between">
          <span className="text-xs text-gray-500" title="Answers are grounded in this many documents - the assistant won't make things up beyond them">RAG Docs</span>
          <span className="text-xs text-gray-400">{sysStatus?.rag_documents ?? '…'}</span>
        </div>
        {sysStatus?.auth_enabled && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Auth</span>
            <span className="text-xs text-blue-400">enabled</span>
          </div>
        )}
        {sysStatus?.rag_only_mode && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500" title="Strict grounding: if the answer isn't in the documents, the assistant says so instead of guessing">RAG Only</span>
            <span className="text-xs text-orange-400">enforced</span>
          </div>
        )}
        {sysStatus?.agent_tools?.agent_enabled && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">Agent Tools</span>
            <span className="text-xs text-yellow-400">active</span>
          </div>
        )}
        {analytics && (
          <>
            <div className="border-t border-gray-800 my-1" />
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">Sessions</span>
              <span className="text-xs text-gray-400">{analytics.total_sessions}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-gray-500">Requests today</span>
              <span className="text-xs text-gray-400">{analytics.requests_today}</span>
            </div>
            {analytics.feedback && analytics.feedback.total > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-xs text-gray-500">Feedback</span>
                <span className="text-xs text-gray-400">
                  👍 {analytics.feedback.thumbs_up} · 👎 {analytics.feedback.thumbs_down}
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
