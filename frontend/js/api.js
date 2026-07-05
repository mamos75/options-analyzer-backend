// js/api.js — API fetch helpers (Phase 5)
import { API_BASE } from './config.js';

export function getAuthHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const legacyToken = localStorage.getItem('mamos_auth_v1');
  if (legacyToken) headers['Authorization'] = 'Bearer ' + legacyToken;
  return headers;
}

export async function apiFetch(endpoint, signal) {
  try {
    const resp = await fetch(API_BASE + endpoint, { headers: getAuthHeaders(), signal });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return await resp.json();
  } catch(e) {
    if (e.name === 'AbortError') return null;
    return null;
  }
}
