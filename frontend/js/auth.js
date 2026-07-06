// js/auth.js — authentication (Phase 5)
import { ELITE_PLAN } from './config.js';
import { authState, setAuthState, countdownInterval, refreshTimer } from './store.js';

export async function checkAuth() {
  try {
    const sessionRaw = localStorage.getItem('mamos_trading_session');
    if (sessionRaw) {
      const session = JSON.parse(sessionRaw);
      if (session && session.plan && session.premium_active !== false) {
        setAuthState({
          authenticated: true,
          isElite: session.plan === ELITE_PLAN,
          plan: session.plan,
          method: 'patreon'
        });
        return authState;
      }
    }
  } catch(e) {}

  try {
    const legacyToken = localStorage.getItem('mamos_auth_v1');
    if (legacyToken) {
      const resp = await fetch('/api/auth/verify', {
        headers: { 'Authorization': 'Bearer ' + legacyToken }
      });
      if (resp.ok) {
        const data = await resp.json();
        setAuthState({
          authenticated: true,
          isElite: data.plan === ELITE_PLAN,
          plan: data.plan || 'Mamos Basic',
          method: 'legacy'
        });
        return authState;
      }
    }
  } catch(e) {}

  setAuthState({ authenticated: false, isElite: false, plan: null, method: null });
  return authState;
}

export function loginWithPatreon() {
  window.location.href = 'https://ncltwnrrzrqtxgxchnaf.supabase.co/functions/v1/patreon-oauth/start?app_url=' + encodeURIComponent('https://options-analyzer.mamoscrypto.com');
}

export async function handleLegacyLogin(e) {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const errEl = document.getElementById('login-error');
  const email = document.getElementById('login-email').value;
  const password = document.getElementById('login-password').value;

  btn.textContent = 'Connexion\u2026';
  btn.disabled = true;
  errEl.classList.remove('visible');

  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await resp.json();
    if (resp.ok && data.token) {
      localStorage.setItem('mamos_auth_v1', data.token);
      await initApp();
    } else {
      errEl.textContent = data.error || 'Identifiants incorrects';
      errEl.classList.add('visible');
    }
  } catch(e) {
    errEl.textContent = 'Erreur de connexion au serveur';
    errEl.classList.add('visible');
  }

  btn.textContent = 'Se connecter';
  btn.disabled = false;
}

export function logout() {
  localStorage.removeItem('mamos_trading_session');
  localStorage.removeItem('mamos_auth_v1');
  setAuthState({ authenticated: false, isElite: false, plan: null, method: null });
  if (countdownInterval) clearInterval(countdownInterval);
  if (refreshTimer) clearInterval(refreshTimer);
  showScreen('login');
}

export function showScreen(screen) {
  document.getElementById('loading-screen').classList.add('hidden');
  document.getElementById('login-wall').classList.add('hidden');
  document.getElementById('upgrade-wall').classList.add('hidden');
  document.getElementById('dashboard').classList.remove('visible');

  if (screen === 'login') {
    document.getElementById('login-wall').classList.remove('hidden');
  } else if (screen === 'upgrade') {
    document.getElementById('current-plan-display').textContent = 'Plan: ' + (authState.plan || 'Mamos Basic');
    document.getElementById('upgrade-wall').classList.remove('hidden');
  } else if (screen === 'dashboard') {
    document.getElementById('dashboard').classList.add('visible');
  }
}

export function applyLockToModules() {
  const ids = ['m2-levels','m3-prob','m4-context','m5-narrative','m6-model'];
  ids.forEach(id => {
    const card = document.getElementById(id);
    if (!card) return;
    card.classList.add('module-locked');
    const content = card.querySelector('[id$="-content"]');
    if (content) content.classList.add('card-content');
    if (!card.querySelector('.lock-overlay')) {
      const overlay = document.createElement('div');
      overlay.className = 'lock-overlay';
      overlay.innerHTML = `
        <div class="lock-icon">\uD83D\uDD12</div>
        <div class="lock-text">Mamos Elite requis</div>
        <div class="lock-sub">Passe \u00e0 Elite sur Patreon</div>
      `;
      card.appendChild(overlay);
    }
  });
}

export async function initApp() {
  // Import scheduler lazily to avoid circular dependency at module parse time
  const { loadAllData, startRefreshLoop } = await import('./scheduler.js');

  try {
    const hash = window.location.hash;

    if (hash.includes('mamos_session=')) {
      const match = hash.match(/mamos_session=([^&]+)/);
      if (match) {
        const decoded = JSON.parse(atob(match[1]));
        if (decoded && decoded.plan) {
          localStorage.setItem('mamos_trading_session', JSON.stringify({
            plan: decoded.plan,
            email: decoded.email || '',
            premium_active: true,
            ts: decoded.ts || Date.now()
          }));
        }
      }
      history.replaceState(null, '', window.location.pathname + window.location.search);

    } else if (hash.includes('access_token=')) {
      const params = new URLSearchParams(hash.slice(1));
      const accessToken = params.get('access_token');
      if (accessToken) {
        // Decode JWT claims directly — no network call needed, plan/premium_active are in the token
        try {
          const payloadB64 = accessToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
          const payload = JSON.parse(atob(payloadB64));
          const meta = payload.user_metadata || {};
          const plan = meta.plan ?? '';
          const email = payload.email ?? meta.email ?? '';
          if (plan) {
            localStorage.setItem('mamos_trading_session', JSON.stringify({
              plan,
              email,
              premium_active: meta.premium_active === true,
              ts: Date.now()
            }));
          }
        } catch (_) {}
      }
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  } catch(e) {}

  document.getElementById('loading-screen').classList.remove('hidden');
  document.getElementById('login-wall').classList.add('hidden');
  document.getElementById('upgrade-wall').classList.add('hidden');
  document.getElementById('dashboard').classList.remove('visible');

  const auth = await checkAuth();
  document.getElementById('loading-screen').classList.add('hidden');

  if (!auth.authenticated) {
    showScreen('login');
    return;
  }
  if (!auth.isElite) {
    showScreen('upgrade');
    applyLockToModules();
    return;
  }

  showScreen('dashboard');
  await loadAllData();
  startRefreshLoop();
}
