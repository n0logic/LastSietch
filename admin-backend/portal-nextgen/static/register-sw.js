// External (CSP-clean, 'self') service-worker registration for the V2 PWA shell.
// The app owns the whole origin now, so the worker registers at the root.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .catch(function () {});
  });
}
