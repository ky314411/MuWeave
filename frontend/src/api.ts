export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
  }
}

export async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  const form = body instanceof FormData
  const response = await fetch(`/api${path}`, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    headers: body !== undefined && !form ? { 'Content-Type': 'application/json' } : undefined,
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  })
  const text = await response.text()
  let data: unknown
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!response.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? (data as { detail: unknown }).detail
        : null
    throw new ApiError(
      response.status,
      typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}）`,
    )
  }
  return data as T
}
