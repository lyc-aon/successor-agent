import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

const payload = window.__SUCCESSOR_REVIEWER_BOOTSTRAP__

if (!payload) {
  throw new Error('Successor reviewer bootstrap payload was not found.')
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
