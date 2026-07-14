import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { KarmaPage } from './karma/KarmaPage'
import './index.css'

// The new Dashboard is being built page-by-page. Until it replaces App.tsx wholesale, the Karma page
// lives behind /karma and everything else still lands on the old dashboard.
const page = window.location.pathname === '/karma' ? <KarmaPage /> : <App />

createRoot(document.getElementById('root')!).render(<StrictMode>{page}</StrictMode>)
