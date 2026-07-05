// js/lib/canvas.js — canvas drawing utilities (Phase 5)

export function drawSparkline(canvasId, values, color, fillColor) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !values?.length) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || canvas.parentElement?.offsetWidth || 300;
  const H = 70;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pad = 4;

  const x = (i) => pad + (i / (values.length - 1)) * (W - pad * 2);
  const y = (v) => H - pad - ((v - min) / range) * (H - pad * 2);

  ctx.beginPath();
  ctx.moveTo(x(0), y(values[0]));
  for (let i = 1; i < values.length; i++) ctx.lineTo(x(i), y(values[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.lineTo(x(values.length - 1), H);
  ctx.lineTo(x(0), H);
  ctx.closePath();
  ctx.fillStyle = fillColor;
  ctx.fill();

  if (min < 0 && max > 0) {
    const yz = y(0);
    ctx.beginPath();
    ctx.moveTo(pad, yz);
    ctx.lineTo(W - pad, yz);
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

export function drawDualAxis(canvasId, timestamps, mopiVals, btcVals) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth || canvas.parentElement?.offsetWidth || 300;
  const H = 160;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const pad = { t: 8, b: 24, l: 8, r: 8 };
  const cW = W - pad.l - pad.r;
  const cH = H - pad.t - pad.b;
  const n = Math.min(mopiVals.length, btcVals.length);
  if (n < 2) return;

  const xAt = (i) => pad.l + (i / (n - 1)) * cW;

  const btcMin = Math.min(...btcVals.slice(0, n));
  const btcMax = Math.max(...btcVals.slice(0, n));
  const btcRange = btcMax - btcMin || 1;
  const yBtc = (v) => pad.t + cH - ((v - btcMin) / btcRange) * cH;

  const yMopi = (v) => pad.t + cH - (Math.max(0, Math.min(100, v)) / 100) * cH;

  ctx.beginPath();
  ctx.moveTo(xAt(0), yBtc(btcVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yBtc(btcVals[i]));
  ctx.strokeStyle = '#3d8eff';
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.lineTo(xAt(n - 1), pad.t + cH);
  ctx.lineTo(xAt(0), pad.t + cH);
  ctx.closePath();
  ctx.fillStyle = 'rgba(61,142,255,0.08)';
  ctx.fill();

  const y30 = yMopi(30);
  const y70 = yMopi(70);
  ctx.fillStyle = 'rgba(255,255,255,0.03)';
  ctx.fillRect(pad.l, y70, cW, y30 - y70);

  ctx.beginPath();
  ctx.moveTo(xAt(0), yMopi(mopiVals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(xAt(i), yMopi(mopiVals[i]));
  ctx.strokeStyle = '#00d47e';
  ctx.lineWidth = 2;
  ctx.stroke();

  [30, 70].forEach(lvl => {
    ctx.beginPath();
    ctx.moveTo(pad.l, yMopi(lvl));
    ctx.lineTo(pad.l + cW, yMopi(lvl));
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font = `${9 * dpr / dpr}px Inter,sans-serif`;
    ctx.fillText(lvl.toString(), pad.l + 2, yMopi(lvl) - 2);
  });

  if (timestamps?.length >= n) {
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font = '9px Inter,sans-serif';
    const fmt = (ts) => { const d = new Date(ts * 1000); return d.getDate() + '/' + (d.getMonth()+1); };
    [[0, 'left'], [Math.floor((n-1)/2), 'center'], [n-1, 'right']].forEach(([i, align]) => {
      ctx.textAlign = align;
      const tx = align === 'left' ? pad.l : align === 'right' ? pad.l + cW : xAt(i);
      ctx.fillText(fmt(timestamps[i]), tx, H - 6);
    });
  }
}

export function drawVcLine(canvasId, points, color) {
  const el = document.getElementById(canvasId);
  if (!el || !points.length) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = el.parentElement.getBoundingClientRect();
  const W = rect.width || 300;
  const H = rect.height || 72;
  el.width  = W * dpr;
  el.height = H * dpr;
  el.style.width  = W + 'px';
  el.style.height = H + 'px';
  const ctx = el.getContext('2d');
  ctx.scale(dpr, dpr);

  const vals = points.map(p => p.v);
  const min  = Math.min(...vals);
  const max  = Math.max(...vals);
  const range = max - min || 1;

  const pad = { t: 6, b: 6, l: 4, r: 4 };
  const w = W - pad.l - pad.r;
  const h = H - pad.t - pad.b;
  const n = points.length;

  const x = i => pad.l + (i / (n - 1)) * w;
  const y = v => pad.t + h - ((v - min) / range) * h;

  const y0 = y(0);
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(pad.l, y0); ctx.lineTo(pad.l + w, y0); ctx.stroke();
  ctx.setLineDash([]);

  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + h);
  grad.addColorStop(0, color + '40');
  grad.addColorStop(1, color + '05');
  ctx.beginPath();
  ctx.moveTo(x(0), y(vals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(vals[i]));
  ctx.lineTo(x(n - 1), pad.t + h);
  ctx.lineTo(x(0), pad.t + h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(x(0), y(vals[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(vals[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  const lx = x(n - 1), ly = y(vals[n - 1]);
  ctx.beginPath();
  ctx.arc(lx, ly, 3, 0, 2 * Math.PI);
  ctx.fillStyle = color;
  ctx.fill();
}
