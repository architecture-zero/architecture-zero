// The instance's accent color, read once from the build-time env (the Docker
// image bakes a placeholder that docker-inject-env.sh replaces at container
// start). Four files each computed this line until the admin split (2026-10-10).
export const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'
