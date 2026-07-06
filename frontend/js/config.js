// js/config.js — centralised config (Phase 5)
export const API_BASE = '';          // same origin
export const REFRESH_INTERVAL = 60; // seconds
export const ELITE_PLAN = 'Mamos Elite';

// ── CFG — all magic numbers in one place ──
export const CFG = {
  // Wilson / edge thresholds
  EDGE_MIN_N:        30,
  EDGE_WILSON_FLOOR: 0.50,
  CONTRARIAN_MIN_N:  30,
  // MOPI thresholds
  MOPI_HIGH:         70,
  MOPI_LOW:          30,
  MOPI_OVERBOUGHT:   70,
  MOPI_OVERSOLD:     30,
  MOPI_COLOR_HIGH:   70,  // F9.4 — aligné sur seuil backend (>70 = signal)
  MOPI_COLOR_LOW:    30,  // F9.4 — aligné sur seuil backend (<30 = signal)
  // GEX thresholds
  GEX_BIG_VEX:           20e6,
  GEX_BIG_CEX:           10e6,
  GEX_TREND_VEX_THRESH:  1e6,
  GEX_TREND_CEX_THRESH:  5e5,
  GEX_TREND_GEX_THRESH:  1e8,
  // Gamma flip proximity
  FLIP_NEAR_PCT:  2.0,
  FLIP_WARN_PCT:  5.0,
  // IV / vol thresholds
  IV_EXTREME:  80,
  IV_HIGH:     60,
  IV_ELEVATED: 45,
  IV_MODERATE: 30,
  // Refresh
  REFRESH_SEC: 120,
  // Levels convergence
  LEVELS_NEAR_THRESH: 0.015,
};
