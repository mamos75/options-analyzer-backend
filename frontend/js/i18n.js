const I18n = (() => {
  const SUPPORTED = ['en', 'fr'];
  const DEFAULT = 'en';

  function detectLang() {
    const pathMatch = window.location.pathname.match(/^\/(en|fr)(\/|$)/);
    if (pathMatch) return pathMatch[1];
    const stored = localStorage.getItem('mamos_lang');
    if (SUPPORTED.includes(stored)) return stored;
    const browser = (navigator.language || 'en').slice(0, 2);
    return SUPPORTED.includes(browser) ? browser : DEFAULT;
  }

  let currentLang = detectLang();
  let translations = {};
  let fallback = {};

  async function loadFallback() {
    if (Object.keys(fallback).length > 0) return;
    try {
      const res = await fetch(`/locales/${DEFAULT}.json?v=${Date.now()}`);
      if (res.ok) fallback = await res.json();
    } catch (e) {}
  }

  async function load(lang) {
    await loadFallback();
    if (lang === DEFAULT) {
      translations = fallback;
    } else {
      try {
        const res = await fetch(`/locales/${lang}.json?v=${Date.now()}`);
        if (!res.ok) throw new Error(`Failed to load locale: ${lang}`);
        translations = await res.json();
      } catch (e) {
        translations = fallback;
        lang = DEFAULT;
      }
    }
    currentLang = lang;
    localStorage.setItem('mamos_lang', lang);
  }

  function t(key) {
    const val = key.split('.').reduce((obj, k) => obj?.[k], translations);
    if (val !== undefined && val !== null && val !== '') return val;
    const fb = key.split('.').reduce((obj, k) => obj?.[k], fallback);
    return fb ?? key;
  }

  function updateSEOMeta() {
    const origin = window.location.origin;
    const stripped = (window.location.pathname.replace(/^\/(en|fr)(\/|$)/, '/') || '/');
    const enUrl  = origin + stripped;
    const frUrl  = `${origin}/fr${stripped === '/' ? '' : stripped}`;
    const selfUrl = currentLang === DEFAULT ? enUrl : frUrl;

    function ensureLink(rel, attrs) {
      const sel = Object.entries(attrs).map(([k, v]) => `[${k}="${v}"]`).join('');
      let el = document.querySelector(`link[rel="${rel}"]${sel}`);
      if (!el) {
        el = document.createElement('link');
        el.rel = rel;
        Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
        document.head.appendChild(el);
      }
      return el;
    }

    // Canonical
    const canonical = ensureLink('canonical', {});
    canonical.href = selfUrl;

    // hreflang
    const hreflangEn = ensureLink('alternate', { hreflang: 'en' });
    hreflangEn.href = enUrl;

    const hreflangFr = ensureLink('alternate', { hreflang: 'fr' });
    hreflangFr.href = frUrl;

    const hreflangDef = ensureLink('alternate', { hreflang: 'x-default' });
    hreflangDef.href = enUrl;

    // og:locale
    let ogLocale = document.querySelector('meta[property="og:locale"]');
    if (!ogLocale) {
      ogLocale = document.createElement('meta');
      ogLocale.setAttribute('property', 'og:locale');
      document.head.appendChild(ogLocale);
    }
    ogLocale.content = currentLang === 'fr' ? 'fr_FR' : 'en_US';
  }

  function apply() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      el.innerHTML = t(el.dataset.i18nHtml);
    });
    document.querySelectorAll('[data-i18n-attr]').forEach(el => {
      el.dataset.i18nAttr.split(',').forEach(pair => {
        const [attr, key] = pair.trim().split(':');
        el.setAttribute(attr, t(key));
      });
    });
    document.documentElement.lang = currentLang;
    document.querySelectorAll('.lang-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.lang === currentLang);
    });
    updateSEOMeta();
  }

  async function switchLang(lang) {
    if (!SUPPORTED.includes(lang)) return;
    await load(lang);
    apply();
    const stripped = window.location.pathname.replace(/^\/(en|fr)(\/|$)/, '/');
    const prefix = lang === DEFAULT ? '' : `/${lang}`;
    history.pushState({ lang }, '', prefix + (stripped || '/'));
  }

  async function init() {
    await load(currentLang);
    apply();
    window.addEventListener('popstate', async () => {
      const newLang = detectLang();
      if (newLang !== currentLang) {
        await load(newLang);
        apply();
      }
    });
  }

  return { init, switchLang, t, lang: () => currentLang };
})();
