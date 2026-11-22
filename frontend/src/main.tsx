import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { store } from './format';
import './style.css';

const saved = store.get('qra-theme');
if (saved) document.documentElement.dataset.theme = saved;

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
