// The Monitoring tab's "Download scrape config" file. It lives in its own
// module, not inline in MonitoringTab, so a test can pin it
// (src/__tests__/scrapeConfig.test.ts): a static string that nothing reads
// is how every broken version of it shipped.
//
// THREE things were wrong with the file this used to hand out, and each one
// alone made it useless. Port 80 is not published by the shipped compose
// (8000 is the backend, 5173 the client), so the scrape got connection
// refused. Pointing it at the client port instead returns HTTP 200 and an
// HTML page - the SPA fallback answers any unmatched path, and nginx only
// proxies /api/, so Prometheus would have parsed index.html as metrics.
// And /metrics is authenticated, so even the right host and port answered
// 401 unless a credential rides along - which no scraper could hold,
// because the only credential this platform issued was a 30-minute access
// token. METRICS_TOKEN now exists for exactly this.
//
// The credential rides in a FILE (credentials_file), never inline and never
// in this download: prometheus.yml is often committed or shared, and a
// one-line token file can be readable by the Prometheus user alone.
export const SCRAPE_CONFIG_YAML = `# Prometheus scrape config for Architecture Zero
# MERGE this file into prometheus.yml. It declares scrape_configs itself, so
# pasting it UNDER an existing scrape_configs key gives you the key twice and
# Prometheus refuses to load its whole configuration - not just this job. If
# prometheus.yml already has scrape_configs, copy only the '- job_name:' block
# below into it.
#
# /metrics is authenticated. Set METRICS_TOKEN in the .env at the repo root
# (the one docker-compose.yml reads for both services - there is no
# backend/.env) to a long random string (openssl rand -hex 32), then write the
# SAME value, alone on one line, into the file credentials_file names below,
# readable only by the Prometheus user. The token is never part of this
# download. A user login will not work here, because its access token expires
# in 30 minutes and Prometheus cannot refresh one.
#
# The target is the BACKEND port (8000 in the shipped compose), not the client
# port: nginx only proxies /api/, so /metrics on the client port returns the
# HTML page with a 200 and Prometheus would scrape markup.
scrape_configs:
  - job_name: 'architecture-zero'
    static_configs:
      - targets: ['YOUR_HOST:8000']
    metrics_path: /metrics
    scrape_interval: 30s
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/metrics_token   # the file holds METRICS_TOKEN
`
