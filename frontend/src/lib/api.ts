export const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8001').replace(/\/$/, '')

export class ApiError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  const payload = response.status === 204 ? null : await response.json().catch(() => null)

  if (!response.ok) {
    const detail = payload?.detail
    throw new ApiError(
      typeof detail === 'object'
        ? detail.code || 'request_failed'
        : typeof detail === 'string' && detail
          ? detail
          : 'request_failed',
      typeof detail === 'object'
        ? detail.message || 'The request could not be completed.'
        : typeof detail === 'string' && detail
          ? detail
          : 'The request could not be completed.',
    )
  }

  return payload as T
}

export const messageOf = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback
