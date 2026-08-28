/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_INSTANCE_NAME: string
  readonly VITE_PRIMARY_COLOR: string
  readonly VITE_API_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module 'react-syntax-highlighter/dist/esm/styles/prism' {
  import type { CSSProperties } from 'react'
  export const oneDark: Record<string, CSSProperties>
  export const oneLight: Record<string, CSSProperties>
  export const vscDarkPlus: Record<string, CSSProperties>
  const styles: Record<string, Record<string, CSSProperties>>
  export default styles
}
