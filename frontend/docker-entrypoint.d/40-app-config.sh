#!/bin/sh
# Writes runtime config so one image can point at any backend: API_BASE_URL=https://...
set -e
if [ -n "$API_BASE_URL" ]; then
  echo "window.__APP_CONFIG__ = { API_BASE_URL: \"$API_BASE_URL\" };" > /usr/share/nginx/html/config.js
fi
