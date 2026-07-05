// js/store.js — module-level state (Phase 5)
import { REFRESH_INTERVAL } from './config.js';

export let authState = { authenticated: false, isElite: false, plan: null, method: null };
export function setAuthState(s) { Object.assign(authState, s); }

export let lastBtcPrice = null;
export function setLastBtcPrice(p) { lastBtcPrice = p; }

export let currentPeriod = '7d';
export function setCurrentPeriod(p) { currentPeriod = p; }

export let vcPeriod = '7d';
export function setVcPeriodState(p) { vcPeriod = p; }

export let lastRegime = null;
export function setLastRegime(r) { lastRegime = r; }

export let secondsLeft = REFRESH_INTERVAL;
export function setSecondsLeft(n) { secondsLeft = n; }

export let countdownInterval = null;
export function setCountdownInterval(id) { countdownInterval = id; }

export let refreshTimer = null;
export function setRefreshTimer(id) { refreshTimer = id; }

export let loadSeq = 0;
export function nextLoadSeq() { return ++loadSeq; }
export function getLoadSeq() { return loadSeq; }

export let loadController = null;
export function setLoadController(c) { loadController = c; }

export let nextRefreshAt = 0;
export function setNextRefreshAt(t) { nextRefreshAt = t; }
