// js/lib/fmt.js — formatting utilities (Phase 5)
import { CFG } from '../config.js';

// XSS escape helper — use for all API string data in innerHTML
export function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Unified wall tag badge renderer
export function tagBadge(tag) {
  if (!tag) return '';
  const colors = { ACTIONABLE: 'var(--green)', STRUCTURAL: 'var(--yellow)' };
  const color = colors[tag] || 'var(--muted)';
  return `<span class="tag-badge" style="color:${color};border-color:${color}44">${esc(tag)}</span>`;
}

export function fmtPrice(val, decimals) {
  if (val == null) return '\u2014';
  const n = Number(val);
  const d = decimals != null ? decimals : n >= 10000 ? 0 : n >= 1000 ? 1 : 2;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}

export function fmtPct(val) {
  if (val == null) return '\u2014';
  return Math.round(val) + '%';
}

export function formatModelName(slug) {
  if (!slug) return '\u2014';
  return slug
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

// Format large numbers (GEX/DEX values)
export function fmtBig(v) {
  if (v == null) return '\u2014';
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (abs >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  return v?.toFixed(0);
}
