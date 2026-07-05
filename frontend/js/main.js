// js/main.js -- entry point (Phase 5)
import { initApp, loginWithPatreon, handleLegacyLogin, logout } from './auth.js';
import { setPeriod } from './widgets/gex_dex.js';
import { setVcPeriod } from './widgets/vex_cex.js';

// Expose for HTML onclick= handlers (ES modules are not global by default)
window.setPeriod          = setPeriod;
window.setVcPeriod        = setVcPeriod;
window.loginWithPatreon   = loginWithPatreon;
window.handleLegacyLogin  = handleLegacyLogin;
window.logout             = logout;

document.addEventListener('DOMContentLoaded', initApp);
