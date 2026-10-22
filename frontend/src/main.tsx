import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { AuthGate } from './features/auth/ui/AuthGate';
import './index.css';

const API_BASE = import.meta.env.VITE_API_BASE ?? window.location.origin;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGate baseUrl={API_BASE}>
      <App />
    </AuthGate>
  </StrictMode>,
);
