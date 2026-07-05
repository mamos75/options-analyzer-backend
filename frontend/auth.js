/**
 * Mamos Crypto — Auth & Feature Flags
 * Backend: http://138.68.80.156/ (FastAPI JWT + SQLite)
 *
 * Usage:
 *   import MamosAuth from '/auth.js';
 *   MamosAuth.can('gravity_map')  → true | false
 *   MamosAuth.gate(element, 'gravity_map')  → locks UI if free
 */

const AUTH_BASE = '/api/auth';

const PLANS = {
  free: {
    gravity_map: false,
    advanced_alerts: false,
    ai_assistant: false,
    liquidity_maps: false,
    market_intelligence: false,
    telegram_alerts: false,
    premium_analytics: false,
  },
  premium: {
    gravity_map: true,
    advanced_alerts: true,
    ai_assistant: true,
    liquidity_maps: true,
    market_intelligence: true,
    telegram_alerts: true,
    premium_analytics: true,
  },
};

const STORAGE_KEY = 'mamos_auth_v1';

const MamosAuth = {
  _state: {
    plan: 'free',
    userId: null,
    token: null,
    expiresAt: null,
  },

  /**
   * Initialize from localStorage, then verify token against backend.
   * Call once on page load.
   */
  async init() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const stored = JSON.parse(raw);
        if (!stored.expiresAt || Date.now() < stored.expiresAt) {
          if (stored.plan in PLANS) {
            this._state = { ...this._state, ...stored };
          }
        } else {
          localStorage.removeItem(STORAGE_KEY);
        }
      }
    } catch (e) {
      /* corrupted storage — ignore, default to free */
    }

    // Verify token against backend if we have one
    if (this._state.token) {
      try {
        const result = await this.verify();
        if (!result.valid) {
          this.logout();
        } else if (result.plan !== this._state.plan) {
          this._state.plan = result.plan;
          this._persist();
        }
      } catch (e) {
        /* network error — use cached state */
      }
    }

    this._applyBodyClass();
    return this;
  },

  get plan() {
    return this._state.plan;
  },

  isPremium() {
    return this._state.plan === 'premium';
  },

  /**
   * Check if user's plan includes a specific feature.
   */
  can(feature) {
    return PLANS[this._state.plan]?.[feature] ?? false;
  },

  /**
   * Gate a DOM element behind a feature.
   * If user doesn't have access: adds lock overlay, returns false.
   * If user has access: returns true, no mutation.
   */
  gate(element, feature, { label = 'Fonctionnalité Premium', showOverlay = true } = {}) {
    if (this.can(feature)) return true;

    element.classList.add('mamos-feature-locked');

    if (showOverlay) {
      const overlay = document.createElement('div');
      overlay.className = 'mamos-lock-overlay';
      overlay.innerHTML = `
        <div class="mamos-lock-inner">
          <div class="mamos-lock-icon">🔒</div>
          <div class="mamos-lock-label">${label}</div>
          <a href="/#premium" class="mamos-lock-cta">Rejoindre la liste →</a>
        </div>
      `;
      element.style.position = 'relative';
      element.appendChild(overlay);
    }

    return false;
  },

  /**
   * POST /api/auth/login → { plan, userId, token, expiresAt }
   */
  async login({ email, password }) {
    try {
      const res = await fetch(`${AUTH_BASE}/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return { success: false, message: err.detail || 'Identifiants invalides' };
      }

      const data = await res.json();
      this._state = {
        plan: data.plan,
        userId: data.userId,
        token: data.token,
        expiresAt: data.expiresAt,
      };
      this._persist();
      this._applyBodyClass();
      return { success: true, plan: data.plan };
    } catch (e) {
      return { success: false, message: 'Erreur réseau' };
    }
  },

  /**
   * GET /api/auth/verify → { valid, plan, userId }
   */
  async verify() {
    if (!this._state.token) return { valid: false, plan: 'free' };
    try {
      const res = await fetch(`${AUTH_BASE}/verify`, {
        headers: { Authorization: `Bearer ${this._state.token}` },
      });
      return await res.json();
    } catch (e) {
      return { valid: false, plan: 'free' };
    }
  },

  logout() {
    localStorage.removeItem(STORAGE_KEY);
    this._state = { plan: 'free', userId: null, token: null, expiresAt: null };
    this._applyBodyClass();
  },

  _persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this._state));
    } catch (e) { /* storage full */ }
  },

  _applyBodyClass() {
    document.body.classList.remove('plan-free', 'plan-premium');
    document.body.classList.add(`plan-${this._state.plan}`);
  },
};

// Inject lock overlay CSS once
(function injectStyles() {
  if (document.getElementById('mamos-auth-styles')) return;
  const style = document.createElement('style');
  style.id = 'mamos-auth-styles';
  style.textContent = `
    .mamos-feature-locked {
      pointer-events: none;
      user-select: none;
    }
    .mamos-lock-overlay {
      position: absolute;
      inset: 0;
      z-index: 10;
      background: rgba(7, 12, 24, 0.82);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: inherit;
    }
    .mamos-lock-inner {
      text-align: center;
      padding: 20px;
    }
    .mamos-lock-icon {
      font-size: 22px;
      margin-bottom: 8px;
    }
    .mamos-lock-label {
      font-size: 13px;
      font-weight: 600;
      color: #e2e8f5;
      margin-bottom: 12px;
    }
    .mamos-lock-cta {
      display: inline-block;
      padding: 8px 18px;
      border-radius: 8px;
      background: linear-gradient(135deg, #9b6bff, #7b4fff);
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      text-decoration: none;
      pointer-events: all;
      cursor: pointer;
      transition: opacity 0.2s;
    }
    .mamos-lock-cta:hover { opacity: 0.85; }
  `;
  document.head.appendChild(style);
})();

export default MamosAuth;
