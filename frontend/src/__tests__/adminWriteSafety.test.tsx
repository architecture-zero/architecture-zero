/**
 * THE ADMIN'S CHAT SETTINGS, SPLIT (2026-10-03) - and a failed read still
 * never becomes a write.
 *
 * The System Prompt tab held six things: the prompt, the suggestions, the
 * long-conversation strategy, the chat defaults, the guest switch and the
 * encryption record. It is now three tabs - System Prompt, Chat Controls,
 * Guest Access - and the default model, which this tab, Settings and Models
 * all wrote, is set in Models alone. Two shapes changed with the split and are
 * pinned here: each switch sends ONLY its own key (one PATCH used to carry
 * every control, guest switch included, so flipping retrieval rewrote the
 * guest row), and the guest switch starts closed instead of drawing guests as
 * allowed when the read fails. The read guards the August review rounds added
 * are pinned per tab: a refused read leaves the Save refusing, not sending
 * the component's initial state.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { SystemPromptTab, ChatControlsTab, GuestAccessTab, SettingsTab, ModelsTab } from '../AdminPanel'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const CONFIG = {
  suggestions: ['first'], allow_model_selection: true, allow_rag_toggle: true,
  default_model: 'm', default_rag_enabled: true,
  guest_mode_configured: true, guest_mode_env_allowed: true,
}
const SETTINGS = {
  ollama_enabled: true, anthropic_enabled: true, openai_enabled: false,
  ollama_base_url: 'http://ollama:11434', anthropic_key_set: true, openai_key_set: false,
  default_model: 'm', rag_similarity_threshold: 0.4,
}

const MODEL_GROUPS = [{ provider: 'p', label: 'P', models: [
  { value: 'd', label: 'D', badge: '' }, { value: 'e', label: 'E', badge: '' }, { value: 'j', label: 'J', badge: '' }] }]
// The template keeps two keys: default_model (`default`) and the chat_model
// pin over it (`chat`, "" = follow the default).
const MODELCFG = {
  default: { value: 'd', default: 'd', overridden: false },
  chat: { value: '', effective: 'd', default: '', overridden: false },
  eval_writer: { value: '', effective: 'd', default: '', overridden: false },
  eval_judge: { value: 'j', default: 'j', overridden: false },
  same_family_warning: false,
}

type Responder = () => Response
interface Calls {
  patches: Record<string, unknown>[]
  contextPatches: Record<string, unknown>[]
  puts: Record<string, unknown>[]
  modelPatches: Record<string, unknown>[]
}

function installFetch(o: {
  adminConfig?: Responder; config?: Responder; patch?: Responder
  context?: Responder; contextPatch?: Responder
  settings?: Responder; put?: Responder
} = {}): Calls {
  const calls: Calls = { patches: [], contextPatches: [], puts: [], modelPatches: [] }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method || 'GET').toUpperCase()
    const body = () => JSON.parse(String(init?.body ?? '{}'))
    if (url.endsWith('/api/admin/config')) {
      if (method === 'PATCH') {
        calls.patches.push(body())
        return (o.patch ?? (() => json({ ok: true })))()
      }
      return (o.adminConfig ?? (() => json({ system_prompt: 'You are the persona.' })))()
    }
    if (url.endsWith('/api/config')) return (o.config ?? (() => json(CONFIG)))()
    if (url.endsWith('/api/admin/context')) {
      if (method === 'PATCH') {
        calls.contextPatches.push(body())
        return (o.contextPatch ?? (() => json({ ok: true })))()
      }
      return (o.context ?? (() => json({ strategy: 'warn', max_tokens: 6000, encryption_verified: true })))()
    }
    if (url.endsWith('/api/admin/model-config')) {
      if (method === 'PATCH') {
        calls.modelPatches.push(body())
      }
      return json(MODELCFG)
    }
    if (url.endsWith('/api/models')) return json({ groups: MODEL_GROUPS })
    if (url.endsWith('/api/settings')) {
      if (method === 'PUT') {
        calls.puts.push(body())
        return (o.put ?? (() => json(SETTINGS)))()
      }
      return (o.settings ?? (() => json(SETTINGS)))()
    }
    return json({})
  }) as unknown as typeof fetch
  return calls
}

const toggle = (label: string) => screen.getByRole('switch', { name: label })
const isOn = (b: HTMLElement) => b.getAttribute('aria-checked') === 'true'
const noHeaders = () => ({})

afterEach(() => { vi.restoreAllMocks() })

describe('System Prompt tab: the prompt and the suggestions, nothing else', () => {
  it('holds no switch, no model picker and no encryption record', async () => {
    installFetch()
    render(<SystemPromptTab api="" headers={noHeaders} />)
    await screen.findByText('Save Prompt')
    expect(screen.queryAllByRole('switch')).toEqual([])
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryByText('Encryption at Rest')).toBeNull()
  })

  it('Save Prompt after a refused prompt read sends nothing and says why', async () => {
    const calls = installFetch({ adminConfig: () => json({ detail: 'Forbidden' }, 403) })
    render(<SystemPromptTab api="" headers={noHeaders} />)
    const save = await screen.findByText('Save Prompt')
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'typed over nothing' } })
    fireEvent.click(save)
    await screen.findByText(/the system prompt never loaded/)
    expect(calls.patches).toEqual([])
  })

  it('Save Suggestions after a failed settings read sends nothing', async () => {
    const calls = installFetch({ config: () => json({ detail: 'boom' }, 502) })
    render(<SystemPromptTab api="" headers={noHeaders} />)
    const save = await screen.findByText('Save Suggestions')
    fireEvent.change(screen.getAllByRole('textbox')[1], { target: { value: 'typed over nothing' } })
    fireEvent.click(save)
    await screen.findByText(/these settings never loaded/)
    expect(calls.patches).toEqual([])
  })
})

describe('Chat Controls tab', () => {
  it('a switch after a failed settings read sends nothing, says why, and stays put', async () => {
    const calls = installFetch({ config: () => json({ detail: 'boom' }, 500) })
    render(<ChatControlsTab api="" headers={noHeaders} />)
    fireEvent.click(await screen.findByRole('switch', { name: 'Allow model selection' }))
    await screen.findByText(/these settings never loaded/)
    expect(calls.patches).toEqual([])
    expect(isOn(toggle('Allow model selection'))).toBe(true)
  })

  it('each switch sends only its own key - never the guest switch or the default model', async () => {
    const calls = installFetch()
    render(<ChatControlsTab api="" headers={noHeaders} />)
    await waitFor(() => expect(isOn(toggle('RAG enabled by default'))).toBe(true))
    fireEvent.click(toggle('Allow model selection'))
    await waitFor(() => expect(calls.patches).toHaveLength(1))
    fireEvent.click(toggle('RAG enabled by default'))
    await waitFor(() => expect(calls.patches).toHaveLength(2))
    expect(calls.patches).toEqual([
      { allow_model_selection: false },
      { default_rag_enabled: false },
    ])
  })

  it('a refused save rolls the switch back to what the server holds', async () => {
    const calls = installFetch({ patch: () => json({ detail: 'expired' }, 401) })
    render(<ChatControlsTab api="" headers={noHeaders} />)
    await waitFor(() => expect(isOn(toggle('RAG enabled by default'))).toBe(true))
    fireEvent.click(toggle('Allow RAG toggle'))
    await waitFor(() => expect(calls.patches).toHaveLength(1))
    await screen.findByText(/your session expired/)
    await waitFor(() => expect(isOn(toggle('Allow RAG toggle'))).toBe(true))
  })

  it('a refused strategy save puts the highlighted choice back', async () => {
    const calls = installFetch({ contextPatch: () => json({ detail: 'nope' }, 500) })
    render(<ChatControlsTab api="" headers={noHeaders} />)
    const summarize = (await screen.findByText('summarize')).closest('button')!
    fireEvent.click(summarize)
    await waitFor(() => expect(calls.contextPatches).toEqual([{ strategy: 'summarize' }]))
    await screen.findByText(/server returned 500/)
    expect(summarize.className).not.toContain('bg-blue-500/10')
    expect(screen.getByText('warn').closest('button')!.className).toContain('bg-blue-500/10')
  })

  it('shows the default model and sends the operator to Models to change it', async () => {
    installFetch()
    const goToModels = vi.fn()
    render(<ChatControlsTab api="" headers={noHeaders} goToModels={goToModels} />)
    await screen.findByText('m')
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.click(screen.getByText('change it in Models'))
    expect(goToModels).toHaveBeenCalledOnce()
  })
})

describe('Guest Access tab', () => {
  it('the guest switch sends guest_mode_enabled and nothing else', async () => {
    const calls = installFetch()
    render(<GuestAccessTab api="" headers={noHeaders} />)
    await waitFor(() => expect(isOn(toggle('Guest access'))).toBe(true))
    fireEvent.click(toggle('Guest access'))
    await waitFor(() => expect(calls.patches).toHaveLength(1))
    expect(calls.patches[0]).toEqual({ guest_mode_enabled: false })
  })

  it('after a failed read the switch is drawn closed, sends nothing, and claims nothing about the host', async () => {
    const calls = installFetch({ config: () => json({ detail: 'boom' }, 500) })
    render(<GuestAccessTab api="" headers={noHeaders} />)
    const b = await screen.findByRole('switch', { name: 'Guest access' })
    expect(isOn(b)).toBe(false)
    expect(screen.queryByText(/ALLOW_GUEST_MODE/)).toBeNull()
    fireEvent.click(b)
    await screen.findByText(/these settings never loaded/)
    expect(calls.patches).toEqual([])
  })

  it('carries the encryption-at-rest record', async () => {
    installFetch()
    render(<GuestAccessTab api="" headers={noHeaders} />)
    await screen.findByText('Encryption at Rest')
    await screen.findByText('Host-verified')
  })
})

describe('Settings tab', () => {
  it('a save sends no default_model - Models owns it - and still shows it', async () => {
    const calls = installFetch()
    render(<SettingsTab api="" headers={noHeaders} />)
    await screen.findByText('m')
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '0.5' } })
    fireEvent.click(screen.getByText('Save Settings'))
    fireEvent.change(await screen.findByPlaceholderText('Your password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByText('Confirm'))
    await waitFor(() => expect(calls.puts).toHaveLength(1))
    expect('default_model' in calls.puts[0]).toBe(false)
  })
})


describe('Save buttons light only for a real change (dirty-state saves)', () => {
  it('Save Prompt is dark until the prompt changes, and says Saved only after the server accepts', async () => {
    const calls = installFetch()
    render(<SystemPromptTab api="" headers={noHeaders} />)
    const save = (await screen.findByText('Save Prompt')).closest('button')!
    expect(save).toBeDisabled()
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'You are the new persona.' } })
    expect(save).toBeEnabled()
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    fireEvent.click(save)
    await waitFor(() => expect(calls.patches).toEqual([{ system_prompt: 'You are the new persona.' }]))
    await screen.findByText('Saved')
    expect(save).toBeDisabled()
  })

  it('a refused save leaves the change marked unsaved and the button lit', async () => {
    installFetch({ patch: () => json({ detail: 'expired' }, 401) })
    render(<SystemPromptTab api="" headers={noHeaders} />)
    const save = (await screen.findByText('Save Prompt')).closest('button')!
    fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'edited' } })
    fireEvent.click(save)
    await screen.findByText(/your session expired/)
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    expect(save).toBeEnabled()
  })

  it('a blank line in the suggestions is not a change', async () => {
    installFetch()
    render(<SystemPromptTab api="" headers={noHeaders} />)
    const save = (await screen.findByText('Save Suggestions')).closest('button')!
    fireEvent.change(screen.getAllByRole('textbox')[1], { target: { value: 'first\n\n' } })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getAllByRole('textbox')[1], { target: { value: 'first\nsecond' } })
    expect(save).toBeEnabled()
  })

  it('Save Settings is dark until something changes, and a typed key counts', async () => {
    installFetch()
    render(<SettingsTab api="" headers={noHeaders} />)
    const save = (await screen.findByText('Save Settings')).closest('button')!
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText(/leave blank to keep current/), { target: { value: 'sk-ant-new' } })
    expect(save).toBeEnabled()
  })
})

describe('Models tab: the default model has its own row', () => {
  it('sets the default model in its own row and sends only the slot that changed', async () => {
    const calls = installFetch()
    render(<ModelsTab api="" headers={noHeaders} />)
    await screen.findByText('Default model')
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'e' } })
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Save changes'))
    await waitFor(() => expect(calls.modelPatches).toEqual([{ default: 'e' }]))
  })

  it('the chat pin and the eval writer can follow the default model', async () => {
    installFetch()
    render(<ModelsTab api="" headers={noHeaders} />)
    await screen.findByText('Default model')
    expect(screen.getAllByText('Follow the default model')).toHaveLength(2)
    expect(screen.getByText(/do not pick a model\. Currently resolves to d\./)).toBeInTheDocument()
  })
})
