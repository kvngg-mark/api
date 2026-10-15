/* Page side of the KV client cache. Registers /sw.js and ties the big, persistent cache
 * to the site being installed (Add to Home Screen).
 * Usage: KVCache.on(state => ...); KVCache.init();
 * Any element with [data-kv-install] becomes an install button (hidden when not applicable). */
(() => {
  'use strict';
  const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const state = { supported: 'serviceWorker' in navigator, installed: false, persisted: false, canInstall: false, ios: isIOS, bytes: 0, episodes: 0, budget: 0 };
  const listeners = new Set();
  let deferred = null;

  const emit = () => listeners.forEach((f) => { try { f({ ...state }); } catch {} });
  const on = (f) => { listeners.add(f); f({ ...state }); };

  function ask(msg) {
    return new Promise((resolve) => {
      const sw = navigator.serviceWorker.controller;
      if (!sw) return resolve(null);
      const ch = new MessageChannel();
      ch.port1.onmessage = (e) => resolve(e.data);
      sw.postMessage(msg, [ch.port2]);
      setTimeout(() => resolve(null), 3000);
    });
  }

  async function refreshUsage() {
    const u = await ask({ type: 'usage' });
    if (u) { state.bytes = u.bytes; state.episodes = u.episodes; state.budget = u.budget; }
    emit();
  }

  async function sync() {
    state.installed = standalone();
    let persisted = false;
    try {
      if (navigator.storage && navigator.storage.persisted) persisted = await navigator.storage.persisted();
      // only ask for protection once the user has installed the app
      if (state.installed && !persisted && navigator.storage && navigator.storage.persist) persisted = await navigator.storage.persist();
    } catch {}
    // iOS home-screen apps are exempt from Safari's 7-day script-storage cleanup
    if (state.installed && isIOS && !(navigator.storage && navigator.storage.persist)) persisted = true;
    state.persisted = persisted;
    await ask({ type: 'mode', installed: state.installed, persisted });
    await refreshUsage();
  }

  async function install() {
    if (deferred) {
      deferred.prompt();
      await deferred.userChoice;
      deferred = null; state.canInstall = false; emit();
    } else if (isIOS) {
      alert('To install: tap the Share button, then "Add to Home Screen".');
    }
  }

  on(() => {
    document.querySelectorAll('[data-kv-install]').forEach((el) => {
      el.hidden = state.installed || !(state.canInstall || state.ios);
      el.onclick = install;
    });
  });

  async function init({ swUrl = '/sw.js' } = {}) {
    if (!state.supported) return emit();
    window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferred = e; state.canInstall = true; emit(); });
    window.addEventListener('appinstalled', () => sync());
    matchMedia('(display-mode: standalone)').addEventListener('change', () => sync());
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshUsage(); });
    await navigator.serviceWorker.register(swUrl, { scope: '/' });
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise((r) => { navigator.serviceWorker.addEventListener('controllerchange', r, { once: true }); setTimeout(r, 1500); });
    }
    await sync();
  }

  async function clear() { await ask({ type: 'clear' }); await refreshUsage(); }

  window.KVCache = { init, on, clear, install, refresh: refreshUsage, get state() { return { ...state }; } };
})();
