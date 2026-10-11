import { useState, useEffect } from 'react'
import { guardedJson } from '../errorSurface'

// ── Tab: Knowledge Base ────────────────────────────────────────────────────
// Upload, list and remove sources, by department. A browse-and-search view
// over the indexed corpus is NOT here: it would need document-read and
// corpus-search endpoints this API does not serve.

interface KBSource {
  source: string
  count: number
  department: string
}

export function KBManage({ api, headers }: { api: string; headers: () => Record<string, string> }) {
  const [sources, setSources] = useState<KBSource[]>([])
  const [departments, setDepartments] = useState<string[]>(['general'])
  const [filterDept, setFilterDept] = useState<string>('')
  const [uploadDept, setUploadDept] = useState<string>('general')
  const [uploading, setUploading] = useState(false)
  const [status, setStatus] = useState<{ ok: boolean; message: string } | null>(null)

  const loadDepts = () =>
    guardedJson<{ departments?: string[] }>(
      fetch(`${api}/api/ingest/departments`, { headers: headers() }), 'Loading departments')
      .then(d => { if (d) setDepartments(d.departments || ['general']) })

  const loadSources = (dept?: string) => {
    const url = dept ? `${api}/api/ingest/sources?department=${encodeURIComponent(dept)}` : `${api}/api/ingest/sources`
    guardedJson<{ sources?: KBSource[] }>(fetch(url, { headers: headers() }), 'Loading KB sources')
      .then(d => { if (d) setSources(d.sources || []) })
  }

  useEffect(() => {
    loadDepts()
    loadSources()
  }, [])

  const handleFilterChange = (dept: string) => {
    setFilterDept(dept)
    loadSources(dept || undefined)
  }

  const upload = async (file: File) => {
    setUploading(true)
    setStatus(null)
    const form = new FormData()
    form.append('file', file)
    form.append('department', uploadDept)
    try {
      const res = await fetch(`${api}/api/ingest/upload`, { method: 'POST', headers: headers(), body: form })
      const d = await res.json()
      if (!res.ok) throw new Error(d.detail || 'Upload failed')
      // 200 does NOT mean indexed. The ingest gate answers 200 with
      // status:"quarantined" for content it WITHHELD, and a queued upload
      // answers 200 with status:"queued" and no chunk count yet. Reporting all
      // three as success told an operator their document was in the corpus
      // when it was refused.
      if (d.status === 'quarantined') {
        setStatus({
          ok: false,
          message: `${d.source} was WITHHELD by the ingest gate (review id ${d.quarantine_id}). `
            + 'Read it under Quarantine and release it there if it is legitimate.',
        })
      } else if (d.status === 'queued') {
        setStatus({ ok: true, message: `${d.source} queued for ingestion (job ${d.job_id}) - watch it under Ingestion Queue.` })
      } else {
        setStatus({ ok: true, message: `${d.source} -> ${d.department} - ${d.chunks} chunks` })
      }
      loadDepts()
      loadSources(filterDept || undefined)
    } catch (e) {
      setStatus({ ok: false, message: e instanceof Error ? e.message : 'Upload failed' })
    } finally {
      setUploading(false)
    }
  }

  const remove = async (source: string, dept: string) => {
    const url = `${api}/api/ingest/source/${encodeURIComponent(source)}?department=${encodeURIComponent(dept)}`
    await fetch(url, { method: 'DELETE', headers: headers() })
    loadSources(filterDept || undefined)
  }

  const deptBadge = (dept: string) => (
    <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-700 text-gray-400 font-mono">{dept}</span>
  )

  return (
    <div className="space-y-4">
      {/* Upload row */}
      <div className="flex gap-2 items-center">
        <label className="flex-1 border-2 border-dashed border-gray-700 hover:border-gray-500 rounded-xl px-4 py-6 text-center cursor-pointer transition-colors">
          <p className="text-sm text-gray-400">{uploading ? 'Uploading…' : 'Click to upload a document'}</p>
          <p className="text-xs text-gray-600 mt-1">.txt .md .pdf .docx .py .js .ts .json</p>
          <input
            type="file"
            className="hidden"
            accept=".txt,.md,.pdf,.docx,.py,.js,.ts,.json,.yaml,.yml"
            onChange={e => { const f = e.target.files?.[0]; if (f) upload(f) }}
          />
        </label>
        <div className="flex flex-col gap-2 min-w-36">
          <p className="text-xs text-gray-500">Upload to dept:</p>
          <input
            value={uploadDept}
            onChange={e => setUploadDept(e.target.value)}
            list="dept-list-kb"
            placeholder="general"
            className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-blue-500/60"
          />
          <datalist id="dept-list-kb">
            {departments.map(d => <option key={d} value={d} />)}
          </datalist>
        </div>
      </div>

      {status && (
        <p className={`text-xs px-1 ${status.ok ? 'text-green-400' : 'text-red-400'}`}>
          {status.ok ? '✓ ' : '✗ '}{status.message}
        </p>
      )}

      {/* Filter */}
      <div className="flex items-center gap-2">
        <p className="text-xs text-gray-500 uppercase tracking-widest">Sources ({sources.length})</p>
        <select
          value={filterDept}
          onChange={e => handleFilterChange(e.target.value)}
          className="ml-auto bg-gray-900 border border-gray-700 rounded-lg px-2 py-1 text-xs text-white outline-none"
        >
          <option value="">All departments</option>
          {departments.map(d => <option key={d} value={d}>{d}</option>)}
        </select>
      </div>

      {sources.length === 0 && <p className="text-xs text-gray-600">No documents ingested.</p>}
      {sources.map(s => (
        <div key={`${s.source}::${s.department}`} className="flex items-center justify-between px-4 py-3 bg-gray-800/50 border border-gray-700/50 rounded-xl group">
          <div className="flex items-center gap-2 min-w-0">
            {deptBadge(s.department)}
            <div className="min-w-0">
              <p className="text-sm text-gray-300 truncate max-w-xs">{s.source}</p>
              <p className="text-xs text-gray-600">{s.count} chunks</p>
            </div>
          </div>
          <button
            onClick={() => remove(s.source, s.department)}
            className="text-xs text-red-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity ml-4"
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  )
}
