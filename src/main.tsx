import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'
import { discoverSidecarPort } from './lib/api'

// Warm the sidecar port cache before React starts firing /api/status
// polls. In prod the sidecar may have fallen back from 8765 to an
// adjacent port (port already in use); discovery memoizes the winner
// in sessionStorage so the rest of the app sees a stable URL. No-op
// in dev (Vite proxies /api for us).
discoverSidecarPort().catch(() => {
  // Failure here just means the default port will be used; the app
  // surfaces the resulting fetch errors through its normal channels.
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
