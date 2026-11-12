/**
 * THE ACCOUNT SCREEN TURNS TWO-FACTOR ON (2026-11-11).
 *
 * The server has long had the two routes an authenticator enrollment needs
 * (POST /api/auth/mfa/setup and POST /api/auth/mfa/enable), the sign-in
 * screen asks for the code, and an administrator can reset it. No screen
 * called setup or enable, so turning two-factor on meant two API calls made
 * by hand.
 *
 * FIX: Account Settings has a third tab, Two-factor. Off: the password starts
 * a setup, the QR code and the key show, and the first code turns it on. On:
 * Replace authenticator takes the password and a current code (rekey), and is
 * confirmed with a code from the new one. Every refusal shows the server's own
 * words, and a setup that expired is said so and started again. The key and
 * the QR code stay in the component: nothing goes to storage, the console or
 * an address, and the QR code is drawn on a canvas, because the page's
 * Content-Security-Policy refuses an image at a data: address.
 * CONTROL: the Username and Password tabs behave as they did, and a guest has
 * no account screen at all.
 */
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import Profile from '../Profile'
import App from '../App'

type Call = { url: string; method: string; body: Record<string, unknown> | null; headers: Record<string, string> }
type Answer = (call: Call) => Response | Promise<Response>

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

// A fixture, not a seed any account uses: eight letters, four of each.
const SEED = 'AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH'
const IN_FOURS = ['AAAA', 'BBBB', 'CCCC', 'DDDD', 'EEEE', 'FFFF', 'GGGG', 'HHHH']
// A real PNG, one pixel, so the bytes handed to the canvas are a picture.
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
const SETUP = { secret: SEED, uri: `otpauth://totp/Test:pat?secret=${SEED}&issuer=Test`, qr: `data:image/png;base64,${PNG}` }
// The server's sentences, as its routes send them.
const EXPIRED = 'This authenticator setup has expired - start it again with POST /api/auth/mfa/setup.'
const NOTHING_PENDING = 'Call /api/auth/mfa/setup first'
const ALREADY_ON = 'MFA is already enabled for this account. To replace the authenticator, re-key: '
  + 'send rekey=true with your password and a current code; that replaces the secret and disables '
  + 'MFA until the new code verifies.'
// What the tab adds wherever it sends a person to an administrator for a reset.
const WHERE_REQUIRED = 'Where two-factor is required to sign in, an account that was reset cannot sign '
  + 'in until it is set up again: do that from a session that is still signed in, or have the person '
  + 'who runs this server switch the requirement off while you do.'

const PAT = { id: 7, username: 'pat', role: 'member' }
const ME = { ...PAT, department: 'general', permissions: ['chat', 'view_history'] }

const realFetch = globalThis.fetch

function serve(answer: Answer): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: Call = {
      url: String(input),
      method: (init?.method || 'GET').toUpperCase(),
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
      headers: Object.fromEntries(new Headers(init?.headers)),
    }
    calls.push(call)
    return answer(call)
  }) as unknown as typeof fetch
  return calls
}

// The three routes the tab talks to, answering as the server does unless a
// test says otherwise. `state` is the account: a setup turns two-factor off
// until it is confirmed, a confirmed code turns it on.
function account(
  state: { enabled: boolean; unreadable?: boolean },
  routes: { me?: Answer; setup?: Answer; enable?: Answer } = {},
): Call[] {
  return serve(call => {
    if (call.url.endsWith('/api/auth/me')) {
      return routes.me ? routes.me(call)
        : json({ ...ME, mfa_enabled: state.enabled, mfa_secret_unreadable: state.unreadable === true })
    }
    if (call.url.endsWith('/api/auth/mfa/setup')) {
      if (routes.setup) return routes.setup(call)
      state.enabled = false
      return json(SETUP)
    }
    if (call.url.endsWith('/api/auth/mfa/enable')) {
      if (routes.enable) return routes.enable(call)
      state.enabled = true
      return json({ status: 'MFA enabled' })
    }
    return json({ detail: 'Not found' }, 404)
  })
}

function mount(user: { id: number; username: string; role: string; mfa_enabled?: boolean } = PAT) {
  const hooks = { onClose: vi.fn(), onUsernameChange: vi.fn(), onPasswordChange: vi.fn(), onMfaChange: vi.fn() }
  const view = render(
    <Profile api="" headers={() => ({ Authorization: 'Bearer tok-a' })} user={user} {...hooks} />)
  return { ...view, ...hooks }
}

const tab = (name: string) => screen.getByRole('button', { name })
const type = (field: HTMLElement, value: string) => fireEvent.change(field, { target: { value } })
const shownKey = () => screen.queryByTestId('two-factor-key')
const setups = (calls: Call[]) => calls.filter(c => c.url.endsWith('/api/auth/mfa/setup'))

async function openOff() {
  fireEvent.click(tab('Two-factor'))
  await screen.findByText('Two-factor is OFF for this account.')
}

function startSetup(password = 'pw-right') {
  type(screen.getByLabelText('1. Your password'), password)
  fireEvent.click(screen.getByRole('button', { name: 'Start setup' }))
}

async function turnOn(code: string) {
  type(screen.getByLabelText('3. The 6-digit code'), code)
  fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
}

// jsdom draws nothing, so a test that looks at the QR code gives it the two
// things a browser has: a decoder for the PNG and a canvas to draw on.
function browserThatDraws() {
  const drawImage = vi.fn()
  const close = vi.fn()
  const blobs: Blob[] = []
  vi.stubGlobal('createImageBitmap', vi.fn(async (blob: Blob) => {
    blobs.push(blob)
    return { width: 490, height: 490, close }
  }))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation((() => ({ drawImage })) as never)
  return { drawImage, close, blobs }
}

const bytesOf = (blob: Blob) => new Promise<number[]>((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(Array.from(new Uint8Array(reader.result as ArrayBuffer)))
  reader.onerror = () => reject(reader.error)
  reader.readAsArrayBuffer(blob)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  globalThis.fetch = realFetch
  localStorage.clear()
})

describe('Account Settings: the Two-factor tab', () => {
  it('sits beside Username and Password, and reads the account when it is opened', async () => {
    const calls = account({ enabled: false })
    mount()
    expect(tab('Username')).toBeInTheDocument()
    expect(tab('Password')).toBeInTheDocument()
    expect(calls).toHaveLength(0)               // nothing is read until the tab is opened
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/api/auth/me', method: 'GET' })
    expect(calls[0].headers.authorization).toBe('Bearer tok-a')
    // The three steps, the later two named before they can be done.
    expect(screen.getByLabelText('1. Your password')).toBeInTheDocument()
    expect(screen.getByText('2. Scan with your authenticator app')).toBeInTheDocument()
    expect(screen.getByText('3. The 6-digit code')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start setup' })).toBeDisabled()
  })

  it('off: the password starts a setup, the QR code and the key show, and the code turns it on', async () => {
    const { drawImage, close, blobs } = browserThatDraws()
    const calls = account({ enabled: false })
    const { onMfaChange } = mount()
    await openOff()
    startSetup()
    const key = await screen.findByTestId('two-factor-key')

    // Setup carried the password, and asked for no replacement.
    expect(calls[1]).toMatchObject({ url: '/api/auth/mfa/setup', method: 'POST' })
    expect(calls[1].body).toEqual({ current_password: 'pw-right', rekey: false })
    expect(calls[1].headers['content-type']).toBe('application/json')
    expect(calls[1].headers.authorization).toBe('Bearer tok-a')
    // The key: shown in fours, and one unbroken run to copy.
    expect(Array.from(key.querySelectorAll('span')).map(s => s.textContent)).toEqual(IN_FOURS)
    expect(key.textContent).toBe(SEED)
    // The QR code: the server's PNG, decoded and drawn at its own size.
    const qr = screen.getByRole('img', { name: 'QR code for your authenticator app' }) as HTMLCanvasElement
    await waitFor(() => expect(drawImage).toHaveBeenCalledTimes(1))
    expect(blobs).toHaveLength(1)
    expect(blobs[0].type).toBe('image/png')
    expect(await bytesOf(blobs[0])).toEqual(Array.from(atob(PNG), c => c.charCodeAt(0)))
    expect([qr.width, qr.height]).toEqual([490, 490])
    expect(close).toHaveBeenCalledTimes(1)
    // The password is not kept once it has been used.
    expect(screen.queryByLabelText('1. Your password')).toBeNull()

    await turnOn('123456')
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(calls[2]).toMatchObject({ url: '/api/auth/mfa/enable', method: 'POST' })
    expect(calls[2].body).toEqual({ code: '123456' })
    expect(calls).toHaveLength(3)
    // The seed is off the page, and the shell was told: "off" when the tab
    // read the account (it had not known), "on" when the code was confirmed.
    expect(shownKey()).toBeNull()
    expect(screen.queryByRole('img')).toBeNull()
    expect(onMfaChange.mock.calls).toEqual([[false], [true]])
  })

  it('a browser that cannot draw the QR code says so, and the key is still there to type', async () => {
    account({ enabled: false })                 // jsdom as it comes: no image decoder
    mount()
    await openOff()
    startSetup()
    expect(await screen.findByText(/could not draw the QR code/)).toBeInTheDocument()
    expect(shownKey()?.textContent).toBe(SEED)
    // And no <img> was pointed at the data: address in its place.
    expect(document.querySelector('img')).toBeNull()
  })

  it('on: replacing sends the password, rekey and a current code, and the new authenticator confirms it', async () => {
    const calls = account({ enabled: true })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    // There is no way to turn it off from here: an administrator resets it.
    expect(screen.queryByRole('button', { name: /turn off|disable|remove/i })).toBeNull()
    expect(screen.getByText(/An administrator can reset two-factor for your account/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Replace authenticator' }))
    const go = screen.getByRole('button', { name: 'Start replacement' })
    type(screen.getByLabelText('Your password'), 'pw-right')
    expect(go).toBeDisabled()                   // the password alone does not replace it
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(go)
    const key = await screen.findByTestId('two-factor-key')

    expect(calls[1]).toMatchObject({ url: '/api/auth/mfa/setup', method: 'POST' })
    expect(calls[1].body).toEqual({ current_password: 'pw-right', rekey: true, mfa_code: '111111' })
    expect(key.textContent).toBe(SEED)
    // Off on the server until the new one is confirmed, and the page says so.
    expect(screen.getByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(screen.getByText(/stays off until a code from the new authenticator is confirmed/)).toBeInTheDocument()
    expect(onMfaChange.mock.calls).toEqual([[false]])

    await turnOn('222222')
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(calls[2].body).toEqual({ code: '222222' })
    expect(onMfaChange.mock.calls).toEqual([[false], [true]])
  })

  it('a replacement that was opened and never started is forgotten once the account reads off', async () => {
    const state = { enabled: true }
    account(state)
    mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    // An administrator resets it meanwhile; the person looks away and comes back.
    state.enabled = false
    fireEvent.click(tab('Username'))
    await openOff()
    // Turned on again from another screen: the form opened before the reset is
    // not found open.
    state.enabled = true
    fireEvent.click(tab('Username'))
    fireEvent.click(tab('Two-factor'))
    await screen.findByText('Two-factor is ON for this account.')
    expect(screen.getByRole('button', { name: 'Replace authenticator' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start replacement' })).toBeNull()
    // Reset once more, and set up from this tab: a first setup, not a
    // replacement, so no word of a "new authenticator".
    state.enabled = false
    fireEvent.click(tab('Username'))
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    expect(screen.queryByText(/new authenticator/)).toBeNull()
  })

  it('the key and the QR code reach no storage, no console line and no address', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const said: unknown[][] = []
    for (const level of ['log', 'info', 'warn', 'error', 'debug'] as const) {
      vi.spyOn(console, level).mockImplementation((...args: unknown[]) => { said.push(args) })
    }
    const address = window.location.href
    account({ enabled: false })
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    // While the setup is on the page: text and a canvas, and no attribute of
    // any element (a link, an image source) carries the seed or the picture.
    for (const el of Array.from(document.querySelectorAll('*'))) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.value).not.toContain(SEED)
        expect(attr.value).not.toContain(PNG)
      }
    }
    await turnOn('123456')
    await screen.findByText('Two-factor is ON for this account.')

    expect(setItem).not.toHaveBeenCalled()
    expect(localStorage.length + sessionStorage.length).toBe(0)
    const spoken = said.map(args => args.map(String).join(' ')).join('\n')
    expect(spoken).not.toContain(SEED)
    expect(spoken).not.toContain(PNG)
    expect(window.location.href).toBe(address)
  })

  it("a refused setup shows the server's own words, and sets nothing up", async () => {
    account({ enabled: false }, { setup: () => json({ detail: 'Current password is incorrect' }, 400) })
    mount()
    await openOff()
    startSetup('pw-wrong')
    expect(await screen.findByRole('alert')).toHaveTextContent('Current password is incorrect')
    expect(shownKey()).toBeNull()
    expect(screen.getByLabelText('1. Your password')).toHaveValue('pw-wrong')
  })

  it("a mistyped code is refused in the server's words and the setup stays on the page", async () => {
    let answer = json({ detail: 'Invalid authenticator code' }, 400)
    account({ enabled: false }, { enable: () => answer })
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('999999')
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid authenticator code')
    expect(shownKey()?.textContent).toBe(SEED)
    expect(screen.getByLabelText('3. The 6-digit code')).toHaveValue('')
    // The cap on wrong codes is the server's too, and so are its words.
    answer = json({ detail: 'Too many wrong authenticator codes on this account. Try again in 12 minute(s).' }, 429)
    await turnOn('999998')
    await waitFor(() => expect(screen.getByRole('alert'))
      .toHaveTextContent('Too many wrong authenticator codes on this account. Try again in 12 minute(s).'))
    expect(shownKey()?.textContent).toBe(SEED)
  })

  it('a refusal with no words reads as its status: not as an unreachable server, and not as a crash', async () => {
    const answers = [
      // A proxy's error page: a body that is not JSON.
      () => new Response('<html><body><h1>502 Bad Gateway</h1></body></html>',
        { status: 502, headers: { 'Content-Type': 'text/html' } }),
      // A validation error: its detail is a list, not text.
      () => json({ detail: [{ loc: ['body', 'rekey'], msg: 'Input should be a valid boolean', type: 'bool_parsing' }] }, 422),
    ]
    account({ enabled: false }, { setup: () => answers.shift()!() })
    mount()
    await openOff()
    startSetup()
    expect(await screen.findByRole('alert')).toHaveTextContent('The server refused this (HTTP 502)')
    expect(screen.queryByText('Could not reach the server')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start setup' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('The server refused this (HTTP 422)'))
    expect(shownKey()).toBeNull()
  })

  it("an answer that is not the API's is not taken for a setup, nor for on: the tab says so and reads the account", async () => {
    const page = () => new Response('<!doctype html><html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } })
    const state = { enabled: false }
    const answers: Answer[] = [page, () => json(SETUP)]
    const calls = account(state, { setup: call => answers.shift()!(call), enable: page })
    const { onMfaChange } = mount()
    const reads = () => calls.filter(c => c.url.endsWith('/api/auth/me')).length
    await openOff()
    startSetup()
    expect(await screen.findByRole('alert')).toHaveTextContent('not with a setup this screen can read')
    expect(shownKey()).toBeNull()
    await waitFor(() => expect(reads()).toBe(2))
    // The second try is a real setup; the page that answers its confirm is not a confirmation.
    await screen.findByText('Two-factor is OFF for this account.')
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('123456')
    expect(await screen.findByRole('alert')).toHaveTextContent('not in a form this screen can read')
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(shownKey()).toBeNull()
    expect(reads()).toBe(3)
    expect(onMfaChange).not.toHaveBeenCalledWith(true)
  })

  it('a server that does not answer is said so', async () => {
    account({ enabled: false }, { setup: () => Promise.reject(new TypeError('Failed to fetch')) })
    mount()
    await openOff()
    startSetup()
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
  })

  it.each([EXPIRED, NOTHING_PENDING])(
    "a setup that is gone shows the server's words, drops the dead key and offers to start again: %s",
    async words => {
      const calls = account({ enabled: false }, { enable: () => json({ detail: words }, 400) })
      mount()
      await openOff()
      startSetup()
      await screen.findByTestId('two-factor-key')
      await turnOn('123456')
      expect(await screen.findByRole('alert')).toHaveTextContent(words)
      expect(shownKey()).toBeNull()
      expect(screen.queryByRole('button', { name: 'Turn on' })).toBeNull()
      // The offer: the password again, under a button that says what it does.
      type(screen.getByLabelText('1. Your password'), 'pw-right')
      fireEvent.click(screen.getByRole('button', { name: 'Start again' }))
      expect((await screen.findByTestId('two-factor-key')).textContent).toBe(SEED)
      expect(setups(calls)).toHaveLength(2)
      expect(setups(calls)[1].body).toEqual({ current_password: 'pw-right', rekey: false })
      expect(screen.queryByRole('alert')).toBeNull()
    })

  it('a setup can be started again before it is confirmed, and the first key leaves the page', async () => {
    account({ enabled: false })
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    fireEvent.click(screen.getByRole('button', { name: 'Start again' }))
    expect(shownKey()).toBeNull()
    expect(screen.getByLabelText('1. Your password')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Start setup' })).toBeInTheDocument()
  })

  it('already on when a setup is asked for: the 409 is shown and the tab reads the account again', async () => {
    const state = { enabled: false }
    const calls = account(state, { setup: () => { state.enabled = true; return json({ detail: ALREADY_ON }, 409) } })
    const { onMfaChange } = mount()
    await openOff()
    startSetup()
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(ALREADY_ON)
    expect(screen.getByRole('button', { name: 'Replace authenticator' })).toBeInTheDocument()
    expect(calls.filter(c => c.url.endsWith('/api/auth/me'))).toHaveLength(2)
    expect(onMfaChange).toHaveBeenLastCalledWith(true)
  })

  it('already on when a code is confirmed: the dead key is dropped and the tab reads the account again', async () => {
    const state = { enabled: false }
    const words = 'MFA is already enabled for this account; there is no pending authenticator to verify.'
    account(state, { enable: () => { state.enabled = true; return json({ detail: words }, 409) } })
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('123456')
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(words)
    expect(shownKey()).toBeNull()
    // The dead key is not held either. A setup still held would keep the tab
    // from reading the account: after a reset and a look at another tab it
    // reads it, and starts clean, the old refusal gone.
    state.enabled = false
    fireEvent.click(tab('Username'))
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(shownKey()).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('once it is on, nothing of the setup is held: the tab reads the account again when it is next opened', async () => {
    const state = { enabled: false }
    account(state)
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('123456')
    expect(await screen.findByText('Turned on just now.')).toBeInTheDocument()
    // A look at another tab and back: still on, and "just now" is not said twice.
    fireEvent.click(tab('Username'))
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(screen.queryByText('Turned on just now.')).toBeNull()
    // An administrator resets it; the person looks at another tab and comes back.
    state.enabled = false
    fireEvent.click(tab('Username'))
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(shownKey()).toBeNull()
    expect(screen.getByLabelText('1. Your password')).toHaveValue('')
    expect(screen.queryByText('Turned on just now.')).toBeNull()
  })

  it('when the account cannot be read, the tab says so and offers nothing until it can', async () => {
    const answers: Answer[] = [
      // A page where the API should be: 200, and not JSON.
      () => new Response('<!doctype html><html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } }),
      () => json({ detail: 'Service unavailable' }, 503),
      () => json({ ...ME, mfa_enabled: false, mfa_secret_unreadable: false }),
    ]
    const calls = account({ enabled: false }, { me: call => answers.shift()!(call) })
    // The shell never learned it either: a sign-in answer does not carry it.
    const { onMfaChange } = mount()
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor could not be read for this account.')).toBeInTheDocument()
    expect(screen.queryByLabelText('1. Your password')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Replace authenticator' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(await screen.findByText('Two-factor could not be read for this account.')).toBeInTheDocument()
    expect(onMfaChange).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(screen.getByLabelText('1. Your password')).toBeInTheDocument()
  })

  it('what the shell last knew stands in when the read fails', async () => {
    account({ enabled: true }, { me: () => json({ detail: 'Service unavailable' }, 503) })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(onMfaChange).not.toHaveBeenCalled()
  })

  it('a factor whose stored key cannot be read is not shown as healthy', async () => {
    account({ enabled: true, unreadable: true })
    mount()
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(screen.getByText(/stored key cannot be read on this server/)).toHaveTextContent(/sign-in.*refused/)
    expect(screen.getByText(/stored key cannot be read on this server/)).toHaveTextContent('Reset MFA')
    // Replacing needs a code checked against that key, so it is not offered.
    expect(screen.queryByRole('button', { name: 'Replace authenticator' })).toBeNull()
  })

  it('a setup in progress survives a look at another tab, and is off the page meanwhile', async () => {
    const calls = account({ enabled: false })
    mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    fireEvent.click(tab('Password'))
    expect(shownKey()).toBeNull()
    fireEvent.click(tab('Two-factor'))
    expect(shownKey()?.textContent).toBe(SEED)
    expect(setups(calls)).toHaveLength(1)
  })

  it('the fields carry what a password manager and a phone keyboard read, and a code is six digits', async () => {
    const state = { enabled: false }
    account(state)
    const { unmount } = mount()
    await openOff()
    const password = screen.getByLabelText('1. Your password')
    expect(password).toHaveAttribute('type', 'password')
    expect(password).toHaveAttribute('autocomplete', 'current-password')
    startSetup()
    const code = await screen.findByLabelText('3. The 6-digit code')
    expect(code).toHaveAttribute('inputmode', 'numeric')
    expect(code).toHaveAttribute('autocomplete', 'one-time-code')
    type(code, ' 12a3-45 6789')
    expect(code).toHaveValue('123456')
    unmount()

    state.enabled = true
    mount()
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    const again = screen.getByLabelText('Your password')
    expect(again).toHaveAttribute('type', 'password')
    expect(again).toHaveAttribute('autocomplete', 'current-password')
    const current = screen.getByLabelText('A code from the authenticator you are replacing')
    expect(current).toHaveAttribute('inputmode', 'numeric')
    expect(current).toHaveAttribute('autocomplete', 'one-time-code')
    type(current, '98x76y54z32')
    expect(current).toHaveValue('987654')
  })

  it('an answer that lands after the screen closed is dropped', async () => {
    let land: (res: Response) => void = () => {}
    const calls = account({ enabled: true }, { setup: () => new Promise<Response>(resolve => { land = resolve }) })
    const { unmount, onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
    await waitFor(() => expect(setups(calls)).toHaveLength(1))
    unmount()
    // The answer lands now. Wait until its body has been read, then one more
    // turn of the loop, so everything the code does after reading it has run.
    let wasRead: () => void = () => {}
    const read = new Promise<void>(resolve => { wasRead = resolve })
    land({ ok: true, status: 200, json: async () => { wasRead(); return SETUP } } as unknown as Response)
    await read
    await new Promise(resolve => setTimeout(resolve, 0))
    // Nothing of the answer is kept or passed on: the account it was for may
    // no longer be the one signed in.
    expect(document.body.textContent).not.toContain('AAAA')
    expect(onMfaChange).not.toHaveBeenCalled()
  })

  it.each([
    ['the lost-authenticator line', false, /Lost your authenticator/],
    ['the line for a key that cannot be read', true, /stored key cannot be read on this server/],
  ])('%s says what a reset needs where two-factor is required to sign in', async (_line, unreadable, line) => {
    account({ enabled: true, unreadable })
    mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    await screen.findByText('Two-factor is ON for this account.')
    expect(screen.getByText(line)).toHaveTextContent('Reset MFA')
    // A reset leaves no second factor, and where one is required that account
    // is refused at sign-in: the line says so, and the two ways through.
    expect(screen.getByText(line)).toHaveTextContent(WHERE_REQUIRED)
  })
})

/**
 * A REPLACEMENT THAT DOES NOT FINISH IS NEVER LEFT UNSAID (2026-11-11).
 *
 * The server stores the new key and turns two-factor off before it answers a
 * replacement start, and nothing puts the old key back. Three ways the tab let
 * that pass in silence: an answer that was lost (or unreadable, or a server
 * error) left "Two-factor is ON" on the page with no fresh read behind it;
 * Close asked nothing while a setup was on the page or a replacement was on
 * its way; and an answer that landed after the screen closed left the shell
 * believing the account was still on.
 *
 * FIX: such an answer is followed by a read of the account, and the page says
 * what the read says (on with the error, off with a warning, or "could not be
 * confirmed", never on, when the read fails too). Close asks first, and a
 * close that goes ahead over an unsettled replacement tells the shell.
 * CONTROL: a refusal changes nothing and reads nothing; with nothing on the
 * page Close asks nothing.
 */
describe('Account Settings: a replacement whose answer does not arrive', () => {
  const lost = () => Promise.reject(new TypeError('Failed to fetch'))
  const page = () => new Response('<!doctype html><html></html>', { status: 200, headers: { 'Content-Type': 'text/html' } })
  const serverError = () => new Response('<html><body><h1>502 Bad Gateway</h1></body></html>',
    { status: 502, headers: { 'Content-Type': 'text/html' } })
  const reads = (calls: Call[]) => calls.filter(c => c.url.endsWith('/api/auth/me')).length

  async function startReplacement() {
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
  }

  it.each([
    ['the request is lost on the way', lost, 'Could not reach the server'],
    ['the server answers with an error', serverError, 'The server refused this (HTTP 502)'],
  ])('%s and the account still reads on: ON stays, with what went wrong', async (_what, answer, words) => {
    const calls = account({ enabled: true }, { setup: answer })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    // "On" is said again only because the server was asked again.
    await waitFor(() => expect(reads(calls)).toBe(2))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(words)
    expect(screen.getByRole('button', { name: 'Start replacement' })).toBeInTheDocument()
    expect(onMfaChange).not.toHaveBeenCalled()
  })

  it.each([
    ['the request is lost on the way', lost],
    ['the answer is a page, not this API', page],
    ['the server answers with an error', serverError],
  ])('%s and the account reads off: OFF with a warning, and the shell is told', async (_what, answer) => {
    const state = { enabled: true }
    // The server took the request: the old key is gone before any answer.
    // Only that first answer goes missing; the setup that follows is answered.
    let first = true
    account(state, {
      setup: () => {
        state.enabled = false
        if (!first) return json(SETUP)
        first = false
        return answer()
      },
    })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    const warning = screen.getByText(/Two-factor is off for this account now/)
    expect(warning).toHaveTextContent(/Set it up below before you sign out/)
    expect(screen.queryByText('Two-factor is ON for this account.')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(onMfaChange.mock.calls).toEqual([[false]])
    // The way back is on the page: the password, a new key, a first code.
    startSetup()
    await screen.findByTestId('two-factor-key')
    expect(screen.getByText(/stays off until a code from the new authenticator is confirmed/)).toBeInTheDocument()
    await turnOn('123456')
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(onMfaChange).toHaveBeenLastCalledWith(true)
    // The warning was about that replacement. Turned on, it is over: a later
    // reset by an administrator reads as plain off.
    state.enabled = false
    fireEvent.click(tab('Username'))
    await openOff()
    expect(screen.queryByText(/Two-factor is off for this account now/)).toBeNull()
  })

  it('the warning ends when the account reads on again, however that came about', async () => {
    const state = { enabled: true }
    account(state, { setup: () => { state.enabled = false; return lost() } })
    mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    await screen.findByText(/Two-factor is off for this account now/)
    // Set up from another screen meanwhile: this tab reads the account as on.
    state.enabled = true
    fireEvent.click(tab('Username'))
    fireEvent.click(tab('Two-factor'))
    await screen.findByText('Two-factor is ON for this account.')
    // A reset after that is not this replacement: plain off, no warning.
    state.enabled = false
    fireEvent.click(tab('Username'))
    await openOff()
    expect(screen.queryByText(/Two-factor is off for this account now/)).toBeNull()
  })

  it.each([
    ['the request is lost on the way', lost],
    ['the answer is a page, not this API', page],
    ['the server answers with an error', serverError],
  ])('%s and the account cannot be read either: not ON, and the page says it may be off', async (_what, answer) => {
    const state = { enabled: true, down: false }
    const calls = account(state, {
      me: () => (state.down ? lost() : json({ ...ME, mfa_enabled: state.enabled, mfa_secret_unreadable: false })),
      setup: () => { state.down = true; state.enabled = false; return answer() },
    })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    expect(await screen.findByText('Two-factor could not be confirmed for this account.')).toBeInTheDocument()
    expect(screen.getByText(/may be off for this\s+account now/)).toHaveTextContent(/Check it on this tab before you sign out/)
    expect(screen.queryByText('Two-factor is ON for this account.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Replace authenticator' })).toBeNull()
    // The shell's copy no longer says on either.
    expect(onMfaChange).toHaveBeenLastCalledWith(false)
    // Still unreadable: still not on. Then the server is back, and says off.
    const before = reads(calls)
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(reads(calls)).toBe(before + 1))
    expect(await screen.findByText('Two-factor could not be confirmed for this account.')).toBeInTheDocument()
    state.down = false
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(screen.getByText(/Two-factor is off for this account now/)).toBeInTheDocument()
  })

  it('a refused replacement start changes nothing and reads nothing: ON stays, with the refusal', async () => {
    const calls = account({ enabled: true }, { setup: () => json({ detail: 'Invalid authenticator code' }, 400) })
    const { onMfaChange } = mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid authenticator code')
    expect(screen.getByText('Two-factor is ON for this account.')).toBeInTheDocument()
    expect(reads(calls)).toBe(1)
    expect(onMfaChange).not.toHaveBeenCalled()
  })

  it('a replacement started again before it is confirmed leaves the page saying two-factor is off now', async () => {
    account({ enabled: true })
    mount({ ...PAT, mfa_enabled: true })
    await startReplacement()
    await screen.findByTestId('two-factor-key')
    fireEvent.click(screen.getByRole('button', { name: 'Start again' }))
    expect(shownKey()).toBeNull()
    // No key is on the page and the old authenticator is gone: said, not implied.
    expect(screen.getByText(/Two-factor is off for this account now/)).toHaveTextContent(/before you sign out/)
  })
})

describe('Account Settings: closing while a setup is on the page', () => {
  const asks = (answer: boolean) => vi.spyOn(window, 'confirm').mockReturnValue(answer)
  const closeIt = () => fireEvent.click(screen.getByRole('button', { name: 'Close' }))

  async function replacementUnderWay(setup?: Answer) {
    const calls = account({ enabled: true }, setup ? { setup } : {})
    const view = mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
    await waitFor(() => expect(setups(calls)).toHaveLength(1))
    return view
  }

  it('a first setup that is not confirmed: Close asks; Cancel keeps the screen and the key; OK closes', async () => {
    const ask = asks(false)
    account({ enabled: false })
    const { onClose } = mount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    closeIt()
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenLastCalledWith(expect.stringMatching(/not confirmed yet\. Closing drops its key/))
    expect(onClose).not.toHaveBeenCalled()
    expect(shownKey()?.textContent).toBe(SEED)
    ask.mockReturnValue(true)
    closeIt()
    expect(ask).toHaveBeenCalledTimes(2)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('a replacement that is not confirmed: the question says two-factor is OFF until the new code is confirmed', async () => {
    const ask = asks(false)
    const { onClose, onMfaChange } = await replacementUnderWay()
    await screen.findByTestId('two-factor-key')
    closeIt()
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenLastCalledWith(expect.stringMatching(
      /^Two-factor is OFF for this account until a code from the new authenticator is confirmed\./))
    expect(onClose).not.toHaveBeenCalled()
    expect(shownKey()?.textContent).toBe(SEED)
    ask.mockReturnValue(true)
    closeIt()
    expect(onClose).toHaveBeenCalledTimes(1)
    // The shell was told when the replacement began; closing adds nothing.
    expect(onMfaChange.mock.calls).toEqual([[false]])
  })

  it('a replacement start still on its way: Close asks; Cancel keeps the screen; OK closes and tells the shell', async () => {
    const ask = asks(false)
    let land: (res: Response) => void = () => {}
    const { onClose, onMfaChange, unmount } = await replacementUnderWay(
      () => new Promise<Response>(resolve => { land = resolve }))
    closeIt()
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenLastCalledWith(expect.stringMatching(/Two-factor may be OFF for this account/))
    expect(onClose).not.toHaveBeenCalled()
    expect(onMfaChange).not.toHaveBeenCalled()
    expect(screen.getByText('Two-factor is ON for this account.')).toBeInTheDocument()
    ask.mockReturnValue(true)
    closeIt()
    expect(onClose).toHaveBeenCalledTimes(1)
    // The answer will be dropped with the screen, so "on" is no longer known.
    expect(onMfaChange.mock.calls).toEqual([[false]])
    unmount()
    let wasRead: () => void = () => {}
    const read = new Promise<void>(resolve => { wasRead = resolve })
    land({ ok: true, status: 200, json: async () => { wasRead(); return SETUP } } as unknown as Response)
    await read
    await new Promise(resolve => setTimeout(resolve, 0))
    // The key did not outlive the screen, and nothing more was said.
    expect(document.body.textContent).not.toContain('AAAA')
    expect(onMfaChange.mock.calls).toEqual([[false]])
  })

  it('a replacement whose answer was lost and cannot be confirmed: Close asks then too', async () => {
    const ask = asks(true)
    const state = { down: false }
    account({ enabled: true }, {
      me: () => (state.down ? Promise.reject(new TypeError('Failed to fetch'))
        : json({ ...ME, mfa_enabled: true, mfa_secret_unreadable: false })),
      setup: () => { state.down = true; return Promise.reject(new TypeError('Failed to fetch')) },
    })
    const { onClose } = mount({ ...PAT, mfa_enabled: true })
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
    await screen.findByText('Two-factor could not be confirmed for this account.')
    closeIt()
    expect(ask).toHaveBeenCalledTimes(1)
    expect(ask).toHaveBeenLastCalledWith(expect.stringMatching(/Two-factor may be OFF for this account/))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('with nothing on the page, Close asks nothing', async () => {
    const ask = asks(false)
    const state = { enabled: false }
    account(state)
    const { onClose } = mount()
    closeIt()                                   // the Username tab
    await openOff()
    closeIt()                                   // off, and nothing started
    type(screen.getByLabelText('1. Your password'), 'pw-right')
    closeIt()                                   // a password typed, nothing sent
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('123456')
    await screen.findByText('Two-factor is ON for this account.')
    closeIt()                                   // on, and nothing left on the page
    fireEvent.click(screen.getByRole('button', { name: 'Replace authenticator' }))
    closeIt()                                   // the replace form open, nothing sent
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
    await screen.findByTestId('two-factor-key')
    await turnOn('222222')
    await screen.findByText('Two-factor is ON for this account.')
    closeIt()                                   // a replacement that finished
    expect(ask).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(6)
  })
})

describe('Account Settings: the Username and Password tabs, as they were', () => {
  const passwordFields = () => Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"]'))

  it('opens on Username: a new name with the password is sent, and the new tokens go up to the shell', async () => {
    const calls = serve(() => json({ status: 'username updated', access_token: 'tok-b', refresh_token: 'ref-b' }))
    const { onUsernameChange } = mount()
    const save = screen.getByRole('button', { name: 'Save username' })
    expect(save).toBeDisabled()
    type(screen.getByDisplayValue('pat'), 'patricia')
    type(passwordFields()[0], 'pw-right')
    fireEvent.click(save)
    expect(await screen.findByText('Username updated')).toBeInTheDocument()
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/api/auth/me/username', method: 'PATCH' })
    expect(calls[0].body).toEqual({ new_username: 'patricia', current_password: 'pw-right' })
    expect(onUsernameChange).toHaveBeenCalledWith('patricia', 'tok-b', 'ref-b')
  })

  it("a refused username change shows the server's words and hands nothing up", async () => {
    serve(() => json({ detail: 'Username already taken' }, 409))
    const { onUsernameChange } = mount()
    type(screen.getByDisplayValue('pat'), 'sam')
    type(passwordFields()[0], 'pw-right')
    fireEvent.click(screen.getByRole('button', { name: 'Save username' }))
    expect(await screen.findByText('Username already taken')).toBeInTheDocument()
    expect(onUsernameChange).not.toHaveBeenCalled()
  })

  it('Password: two different new passwords are refused on the page, with no request', async () => {
    const calls = serve(() => json({}))
    mount()
    fireEvent.click(tab('Password'))
    const [current, next, again] = passwordFields()
    type(current, 'pw-old')
    type(next, 'pw-new-1')
    type(again, 'pw-new-2')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('New passwords do not match')).toBeInTheDocument()
    expect(calls).toHaveLength(0)
  })

  it('Password: a change is sent, said, and signs the account out', async () => {
    const calls = serve(() => json({ status: 'password updated - please sign in again' }))
    const { onPasswordChange } = mount()
    fireEvent.click(tab('Password'))
    const [current, next, again] = passwordFields()
    type(current, 'pw-old')
    type(next, 'pw-new-1')
    type(again, 'pw-new-1')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('Password updated - you will be signed out')).toBeInTheDocument()
    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ url: '/api/auth/me/password', method: 'PATCH' })
    expect(calls[0].body).toEqual({ current_password: 'pw-old', new_password: 'pw-new-1' })
    expect(passwordFields().map(f => f.value)).toEqual(['', '', ''])
    // The sign-out follows 1.5 s after the message. A loaded runner is given
    // room well past that, inside the suite's own 20 s limit.
    await waitFor(() => expect(onPasswordChange).toHaveBeenCalledTimes(1), { timeout: 15000 })
  })

  it("a refused password change shows the server's words and signs nobody out", async () => {
    serve(() => json({ detail: 'Current password is incorrect' }, 400))
    const { onPasswordChange } = mount()
    fireEvent.click(tab('Password'))
    const [current, next, again] = passwordFields()
    type(current, 'pw-bad')
    type(next, 'pw-new-1')
    type(again, 'pw-new-1')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('Current password is incorrect')).toBeInTheDocument()
    expect(onPasswordChange).not.toHaveBeenCalled()
  })

  // A wrong current password counts toward the account's lock, like a failed
  // sign-in, and the lock answers 429. The page has no branch on the status:
  // it shows whatever the server said and leaves the session alone.
  it("a locked account's refusal shows the server's words too, and signs nobody out", async () => {
    serve(() => json({ detail: 'Account locked. Try again in 15 minute(s).' }, 429))
    const { onPasswordChange } = mount()
    fireEvent.click(tab('Password'))
    const [current, next, again] = passwordFields()
    type(current, 'pw-old')
    type(next, 'pw-new-1')
    type(again, 'pw-new-1')
    fireEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('Account locked. Try again in 15 minute(s).')).toBeInTheDocument()
    expect(onPasswordChange).not.toHaveBeenCalled()
  })

  // Each miss counts, so a password manager must not guess which field is
  // which: a stale saved password filled into "Current password" would be a
  // counted miss on every submit.
  it('Password: each field tells a password manager which password it holds', () => {
    serve(() => json({}))
    mount()
    fireEvent.click(tab('Password'))
    expect(passwordFields().map(f => f.getAttribute('autocomplete')))
      .toEqual(['current-password', 'new-password', 'new-password'])
  })
})

describe('the shell and the account screen', () => {
  // Every read the shell makes on the way to the chat screen, answered; the
  // account's three routes as above. `down` fails the account read alone.
  function shell(state: { enabled: boolean; down?: boolean }, setup?: Answer): Call[] {
    return serve(call => {
      const { url } = call
      if (url.endsWith('/api/auth/config')) {
        return json({ needs_setup: false, auth_mode: 'local', guest_mode_enabled: true })
      }
      if (url.endsWith('/api/status')) {
        return json({ ollama: 'connected', rag_documents: 0, auth_enabled: true, needs_setup: false })
      }
      if (url.endsWith('/api/config')) return json({ guest_mode_enabled: true })
      if (url.endsWith('/api/auth/me')) {
        return state.down ? json({ detail: 'Service unavailable' }, 503)
          : json({ ...ME, mfa_enabled: state.enabled, mfa_secret_unreadable: false })
      }
      if (url.endsWith('/api/auth/mfa/setup') && setup) return setup(call)
      if (url.endsWith('/api/auth/mfa/setup')) { state.enabled = false; return json(SETUP) }
      if (url.endsWith('/api/auth/mfa/enable')) { state.enabled = true; return json({ status: 'MFA enabled' }) }
      if (url.endsWith('/api/models')) return json({ groups: [] })
      if (url.endsWith('/api/sessions/mine')) return json({ sessions: [] })
      if (url.includes('/api/history/')) return json({ messages: [] })
      return json({})
    })
  }

  async function signedIn() {
    localStorage.setItem('az_jwt_token', 'tok-a')
    render(<App />)
    await waitFor(() => expect(screen.getByPlaceholderText(/Message/)).toBeInTheDocument())
  }

  const openAccount = async () => fireEvent.click((await screen.findAllByTitle('Account settings'))[0])

  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn()
  })

  it("turning it on reaches the shell's own copy of the account, with no reload", async () => {
    const state = { enabled: false, down: false }
    shell(state)
    await signedIn()
    await openAccount()
    await openOff()
    startSetup()
    await screen.findByTestId('two-factor-key')
    await turnOn('123456')
    await screen.findByText('Two-factor is ON for this account.')
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    // From here the account cannot be read, so what the tab shows on the way
    // back in is the shell's own copy of it, and nothing was reloaded.
    state.down = true
    await openAccount()
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is ON for this account.')).toBeInTheDocument()
  })

  it('closing the account screen asks first, then drops a setup that was not confirmed', async () => {
    const ask = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const state = { enabled: false }
    const calls = shell(state)
    await signedIn()
    await openAccount()
    await openOff()
    startSetup()
    expect((await screen.findByTestId('two-factor-key')).textContent).toBe(SEED)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(ask).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).not.toContain('AAAA')
    await openAccount()
    await openOff()
    expect(shownKey()).toBeNull()
    expect(screen.getByLabelText('1. Your password')).toHaveValue('')
    expect(setups(calls)).toHaveLength(1)
  })

  it('a replacement closed while its start is on the way: the shell no longer holds the account as on', async () => {
    const ask = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const state = { enabled: true, down: false }
    // The start reaches the server and is never answered here.
    const calls = shell(state, () => { state.enabled = false; return new Promise<Response>(() => {}) })
    await signedIn()
    await openAccount()
    fireEvent.click(tab('Two-factor'))
    fireEvent.click(await screen.findByRole('button', { name: 'Replace authenticator' }))
    type(screen.getByLabelText('Your password'), 'pw-right')
    type(screen.getByLabelText('A code from the authenticator you are replacing'), '111111')
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }))
    await waitFor(() => expect(setups(calls)).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(ask).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Account Settings')).toBeNull()
    // From here the account cannot be read, so the tab shows the shell's own
    // copy of it: and that copy must not still say "on".
    state.down = true
    await openAccount()
    fireEvent.click(tab('Two-factor'))
    expect(await screen.findByText('Two-factor is OFF for this account.')).toBeInTheDocument()
    expect(screen.queryByText('Two-factor is ON for this account.')).toBeNull()
  })

  it('a guest has no account screen, so no Two-factor tab', async () => {
    shell({ enabled: false })
    render(<App />)
    fireEvent.click(await screen.findByText('Continue as guest'))
    await waitFor(() => expect(screen.getByPlaceholderText(/Message/)).toBeInTheDocument())
    expect(screen.queryByTitle('Account settings')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Two-factor' })).toBeNull()
  })
})
