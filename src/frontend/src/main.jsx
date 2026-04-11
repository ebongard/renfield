import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import '@fontsource-variable/cormorant';
import '@fontsource-variable/dm-sans';
import App from './App';
import './index.css';
import './i18n'; // Initialize i18n

// Set document title and meta from theme config
const appName = import.meta.env.VITE_APP_NAME || 'Renfield';
document.title = `${appName} AI Assistant`;
const metaDesc = document.querySelector('meta[name="description"]');
if (metaDesc) metaDesc.setAttribute('content', `${appName} AI Assistant`);

// Override CSS custom properties from theme build args
const themeColors = import.meta.env.VITE_THEME_COLORS;
if (themeColors) {
  try {
    const colors = JSON.parse(themeColors);
    const root = document.documentElement;
    for (const [key, value] of Object.entries(colors)) {
      root.style.setProperty(`--color-${key}`, value);
    }
  } catch (e) {
    // Invalid JSON, skip color override
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
