// js/api.js — API fetch helpers (Phase 5)
// P5 — Render dedup: per-cycle request cache keyed by (endpoint, signal).
// Multiple widgets calling the same endpoint in the same load cycle share
// one in-flight fetch → no duplicate HTTP requests, no duplicate renders.
import { API_BASE } from './config.js';

// WeakMap<AbortSignal, Map<string, Promise>> — auto-GC'd when signal is GC'd
const _cycleCache = new WeakMap();

export function getAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const legacyToken = localStorage.getItem('mamos_auth_v1');
  if (legacyToken) headers['Authorization'] = 'Bearer ' + legacyToken;
  return headers;
}

export async function apiFetch(endpoint, signal) {
  try {
    // P5 — deduplicate: if same endpoint already in-flight for this cycle, reuse promise
    if (signal) {
      if (!_cycleCache.has(signal)) _cycleCache.set(signal, new Map());
      const cache = _cycleCache.get(signal);
      if (cache.has(endpoint)) return await cache.get(endpoint);
      const promise = fetch(API_BASE + endpoint, { headers: getAuthHeaders(), signal })
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .catch(e => { if (e.name === 'AbortError') return null; return null; });
      cache.set(endpoint, promise);
      return await promise;
    }
    // No signal (one-off call) — no cache
    const resp = await fetch(API_BASE + endpoint, { headers: getAuthHeaders() });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return await resp.json();
  } catch(e) {
    if (e.name === 'AbortError') return null;
    return null;
  }
}
