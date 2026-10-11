// The backend's address, the bearer header every request reads from storage,
// and the per-account conversation pointer - read by the shell and by the chat
// pieces under src/chat/ (the App split, 2026-10-11).
export const API = import.meta.env.VITE_API_URL || ''

export const authHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('az_jwt_token')
  return token ? { 'Authorization': `Bearer ${token}` } : {}
}

export function sessionKey(userId?: number) {
  return userId ? `az_session_id_${userId}` : 'az_session_id'
}

export function getOrCreateSession(userId?: number): string {
  const key = sessionKey(userId)
  let id = localStorage.getItem(key)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(key, id)
  }
  return id
}
