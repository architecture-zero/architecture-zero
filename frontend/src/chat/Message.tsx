import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import type { ToolCall } from './types'
import { ToolCallBadge } from './ToolCallBadge'
import { CitationsPanel } from './CitationsPanel'
import { PRIMARY_COLOR, INITIALS, ON_PRIMARY } from '../theme'
import { SAFE_MARKDOWN_COMPONENTS } from '../markdownSafety'

// ── Markdown code renderer (extracted to avoid re-creation on render) ───────

function renderCode({
  inline,
  className,
  children,
}: {
  inline?: boolean
  className?: string
  children?: React.ReactNode
}) {
  const match = /language-(\w+)/.exec(className || '')
  return !inline && match ? (
    <SyntaxHighlighter
      style={oneDark}
      language={match[1]}
      PreTag="div"
      customStyle={{ borderRadius: '0.5rem', fontSize: '0.8rem', margin: 0 }}
    >
      {String(children).replace(/\n$/, '')}
    </SyntaxHighlighter>
  ) : (
    <code className={className}>{children}</code>
  )
}

// ── Message ────────────────────────────────────────────────────────────────

interface MessageProps {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  sources?: string[]
  // See ChatMessage.help - the answer came from the help lane.
  helpLane?: boolean
  // Shown under the bubble, never part of `content`. See ChatMessage.notice.
  notice?: string
  msgIndex: number
  onFeedback?: (msgIndex: number, value: number) => void
  onRegenerate?: () => void
  // Returns false when the edit was REFUSED (a stream is in flight), so the
  // editor can keep the user's text instead of silently discarding it.
  onEdit?: (newContent: string) => boolean | void
  isStreaming?: boolean
}

export function Message({ role, content, toolCalls, sources, helpLane, notice, msgIndex, onFeedback, onRegenerate, onEdit, isStreaming }: MessageProps) {
  const isUser = role === 'user'
  const [copied, setCopied] = useState(false)
  const [voted, setVoted] = useState<1 | -1 | null>(null)
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState(content)

  const copy = () => {
    navigator.clipboard.writeText(content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const vote = (value: 1 | -1) => {
    if (voted === value) return
    setVoted(value)
    onFeedback?.(msgIndex, value)
  }

  const submitEdit = () => {
    if (!editText.trim()) return
    // Do NOT close the editor or reset the draft until the handler has accepted
    // it. editAndRegenerate returns immediately while a stream is running, and
    // this used to have already discarded the user's rewritten text - typing
    // gone, editor closed, nothing said. Widening the guard to cover the whole
    // stream turned that from a sub-second window into the entire generation,
    // and re-enabling the composer mid-stream made it easy to walk into.
    if (onEdit?.(editText.trim()) === false) return
    setEditing(false)
    setEditText(content)
  }

  // Edit mode - replaces the user bubble with an inline textarea
  if (isUser && editing) {
    return (
      <div className="flex items-end gap-3 mb-6 flex-row-reverse">
        <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0" style={{ backgroundColor: '#4b5563' }}>
          You
        </div>
        <div className="max-w-[75%] flex flex-col gap-2 w-full">
          <textarea
            autoFocus
            value={editText}
            onChange={e => setEditText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitEdit() }}
            rows={Math.max(2, editText.split('\n').length)}
            className="w-full bg-gray-800 border border-blue-500/60 text-white text-sm rounded-xl px-3 py-2 resize-none outline-none leading-relaxed"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => { setEditing(false); setEditText(content) }}
              className="text-xs text-gray-500 hover:text-gray-300 px-3 py-1.5 rounded-lg border border-gray-700 hover:border-gray-600 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={submitEdit}
              disabled={!editText.trim()}
              className="text-xs px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40"
              style={{ backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY }}
            >
              Send
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={`flex items-end gap-3 mb-6 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0"
        style={{ backgroundColor: isUser ? '#4b5563' : PRIMARY_COLOR }}>
        {isUser ? 'You' : INITIALS}
      </div>
      <div className={`group relative max-w-[75%] ${isUser ? 'items-end' : 'items-start'} flex flex-col gap-1`}>
        {!isUser && toolCalls && toolCalls.length > 0 && (
          <div className="w-full mb-1">
            {toolCalls.map((tc, i) => <ToolCallBadge key={i} name={tc.name} result={tc.result} />)}
          </div>
        )}
        <div className={`px-4 py-3 rounded-2xl text-sm leading-relaxed ${
          isUser
            ? 'rounded-br-sm whitespace-pre-wrap'
            : 'bg-gray-800 border border-gray-700 text-gray-100 rounded-bl-sm'
        }`} style={isUser ? { backgroundColor: PRIMARY_COLOR, color: ON_PRIMARY } : {}}>
          {isUser ? content : (
            <ReactMarkdown
              className="prose prose-invert prose-sm max-w-none
                prose-p:my-1 prose-p:leading-relaxed
                prose-headings:text-white prose-headings:font-semibold prose-headings:mt-3 prose-headings:mb-1
                prose-ul:my-1 prose-ul:pl-4 prose-ol:my-1 prose-ol:pl-4
                prose-li:my-0.5
                prose-strong:text-white
                prose-code:text-blue-300 prose-code:bg-gray-900 prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:text-xs
                prose-pre:p-0 prose-pre:bg-transparent prose-pre:my-2"
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              components={{ ...SAFE_MARKDOWN_COMPONENTS, code: renderCode } as any}
            >
              {content}
            </ReactMarkdown>
          )}
          {!isUser && <CitationsPanel sources={sources} openable={helpLane} />}
        </div>
        {notice && (
          <div className="px-1 text-xs text-amber-400/80 italic">{notice}</div>
        )}
        {isUser && onEdit && !isStreaming && (
          <div className="flex justify-end opacity-0 group-hover:opacity-100 transition-opacity px-1">
            <button onClick={() => setEditing(true)} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
              Edit
            </button>
          </div>
        )}
        {!isUser && (
          <div className="flex items-center gap-3 opacity-0 group-hover:opacity-100 transition-opacity px-1">
            {/* Drawn only when a vote has somewhere to go. Withholding the
                handler alone left the thumbs on screen, and a click lit one up
                while nothing was sent - a vote the person believes landed. */}
            {onFeedback && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => vote(1)}
                title="Good response"
                className={`text-base transition-colors ${voted === 1 ? 'text-green-400' : 'text-gray-600 hover:text-gray-300'}`}
              >
                &#128077;
              </button>
              <button
                onClick={() => vote(-1)}
                title="Bad response"
                className={`text-base transition-colors ${voted === -1 ? 'text-red-400' : 'text-gray-600 hover:text-gray-300'}`}
              >
                &#128078;
              </button>
            </div>
            )}
            <button onClick={copy} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
              {copied ? '✓ Copied' : 'Copy'}
            </button>
            {onRegenerate && !isStreaming && (
              <button onClick={onRegenerate} className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
                Regenerate
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
