import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import TrustPanel from './TrustPanel'
import Claim from './Claim'
import { installSessionRefresh } from './sessionRefresh'

// Before the first render, so no request can go out unwrapped: a 401 on an
// expired access token becomes one silent refresh and a replay instead of
// the "Session expired" banner after 30 minutes. See sessionRefresh.ts.
installSessionRefresh()

// Hash routes serve standalone screens with no server-side routing, so the
// nginx config stays a plain SPA fallback and nothing here depends on how the
// bundle is served.
//
// ROUTED ONCE, AT LOAD. There is no hashchange listener, so anything that sends
// a visitor to one of these screens must set the hash and then RELOAD - see the
// boot handler in App.tsx. That is deliberate: these are whole screens rather
// than views inside the app, and a listener would leave App mounted underneath
// them.
const routes: Record<string, React.ReactNode> = {
  '#trust': <TrustPanel />,
  '#setup': <Claim />,
}
const page = routes[window.location.hash] ?? <App />

createRoot(document.getElementById('root')!).render(
  <StrictMode>{page}</StrictMode>,
)
