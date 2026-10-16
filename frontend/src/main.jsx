import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './i18n'
import './theme.css'
import './index.css'
import App from './App.jsx'
import './brand.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
