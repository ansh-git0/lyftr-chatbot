// Runtime config. Overwritten at container start in Docker (see docker-entrypoint.d).
// Left empty here so Vite's VITE_API_BASE_URL (build time) is used instead.
window.__APP_CONFIG__ = window.__APP_CONFIG__ || {};
