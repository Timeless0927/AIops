import type { components } from "@/api/schema"

type CsrfResponse = components["schemas"]["CsrfResponse"]
let requestSequence = 0

export function newClientId() {
  return globalThis.crypto?.randomUUID?.() ?? `req-${Date.now().toString(36)}-${(++requestSequence).toString(36)}`
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId?: string,
  ) {
    super(message)
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Accept": "application/json",
      "X-Request-ID": newClientId(),
      ...init?.headers,
    },
  })
  const payload = await response.json()
  if (!response.ok) {
    throw new ApiError(
      response.status,
      payload.error?.code ?? "request_failed",
      payload.error?.message ?? "请求失败",
      payload.request_id,
    )
  }
  return payload as T
}

export async function write<T = unknown>(path: string, method: "POST" | "PATCH" | "PUT" | "DELETE", body?: object, requestId?: string, signal?: AbortSignal) {
  const {csrf_token} = await request<CsrfResponse>("/auth/csrf")
  return request<T>(path, {
    method,
    headers: {"X-CSRF-Token": csrf_token, ...(body ? {"Content-Type": "application/json"} : {}), ...(requestId ? {"X-Request-ID": requestId} : {})},
    body: body ? JSON.stringify(body) : undefined,
    signal,
  })
}
