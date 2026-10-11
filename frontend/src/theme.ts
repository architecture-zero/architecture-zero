// The instance's brand, read once from the build-time env (the Docker image
// bakes placeholders that docker-inject-env.sh replaces at container start):
// the accent color, and the display name with its initials. Four files each
// computed the color line until the admin split (2026-10-10); App.tsx and
// Login.tsx each computed the name and initials until the App split (2026-10-11).
export const PRIMARY_COLOR = import.meta.env.VITE_PRIMARY_COLOR || '#2563eb'

export const INSTANCE_NAME = import.meta.env.VITE_INSTANCE_NAME || 'Architecture Zero'
export const INITIALS = INSTANCE_NAME.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()

function contrastColor(hex: string): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 ? '#000000' : '#ffffff'
}
export const ON_PRIMARY = contrastColor(PRIMARY_COLOR)
