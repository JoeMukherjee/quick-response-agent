import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { ErrorBoundary } from './ErrorBoundary';
import { store } from './format';
import './style.css';

const motion = store.get('qra-motion');
if (motion) document.documentElement.dataset.motion = motion;
const saved = store.get('qra-theme');
if (saved) document.documentElement.dataset.theme = saved;

createRoot(document.getElementById('root')!).render(<StrictMode><ErrorBoundary><App /></ErrorBoundary></StrictMode>);
