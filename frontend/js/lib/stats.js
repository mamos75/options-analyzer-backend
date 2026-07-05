// js/lib/stats.js — statistical helpers (Phase 5)

// Wilson lower bound (mirrors backend wilson_utils.py)
export function wilsonLB(wr, n, z) {
  if (wr == null || n == null || n <= 0) return null;
  z = z || 1.96;
  const z2 = z * z;
  const denom = 1.0 + z2 / n;
  const centre = (wr + z2 / (2 * n)) / denom;
  const margin = z * Math.sqrt(wr * (1 - wr) / n + z2 / (4 * n * n)) / denom;
  return Math.max(0, centre - margin);
}

// Client-side has_edge: n>=30 AND wilsonLB(wr,n) > 0.50
export function clientHasEdge(wr, n) {
  if (wr == null || n == null || n < 30) return false;
  return wilsonLB(wr, n) > 0.50;
}
