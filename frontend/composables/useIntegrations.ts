// Integrations: API tokens (one's own and the service accounts'), service accounts,
// webhooks. The gateway describes them in docs/api.md.

export interface ApiToken {
  id: number
  name: string
  prefix: string // the token's beginning: to recognize it, not to use it
  scope: 'read' | 'write'
  created_at: string
  expires_at: string
  last_used_at: string | null
  last_used_ip: string | null
}
// at creation, and only there: the secret
export interface ApiTokenCreated extends ApiToken {
  token: string
}
export interface AdminApiToken extends ApiToken {
  user_id: number
  user_email: string
  user_name: string
  is_service: boolean
}
export interface ServiceAccount {
  id: number
  name: string
  email: string
  is_active: boolean
  created_at: string | null
  last_seen_at: string | null
  groups: string[]
  tokens: number
}
export interface TokenRequest {
  name: string
  scope: 'read' | 'write'
  expires_in_days: number
}

export type WebhookEvent =
  | 'flow.run.succeeded'
  | 'flow.run.failed'
  | 'datasource.refresh.succeeded'
  | 'datasource.refresh.failed'
export const WEBHOOK_EVENTS: WebhookEvent[] = [
  'flow.run.succeeded', 'flow.run.failed', 'datasource.refresh.succeeded', 'datasource.refresh.failed',
]
export interface Webhook {
  id: number
  name: string
  url: string
  events: WebhookEvent[]
  project_id: number | null
  project_name: string | null
  active: boolean
  created_at: string
  last_delivery_status: 'pending' | 'sending' | 'delivered' | 'failed' | null
  last_delivery_at: string | null
}
export interface WebhookWithSecret extends Webhook {
  secret: string
}
export interface WebhookDelivery {
  id: number
  event: string
  status: 'pending' | 'sending' | 'delivered' | 'failed'
  attempts: number
  created_at: string
  next_attempt_at: string | null
  delivered_at: string | null
  last_status_code: number | null
  last_error: string | null
  payload: string | null
}

export function useIntegrations() {
  const { apiFetch } = useApiClient()
  return {
    // one's own tokens
    myTokens: () => apiFetch<ApiToken[]>('/auth/tokens'),
    createMyToken: (body: TokenRequest) => apiFetch<ApiTokenCreated>('/auth/tokens', { method: 'POST', body }),
    revokeMyToken: (id: number) => apiFetch<void>(`/auth/tokens/${id}`, { method: 'DELETE' }),
    // all the tokens (administration)
    allTokens: () => apiFetch<AdminApiToken[]>('/admin/api-tokens'),
    revokeAnyToken: (id: number) => apiFetch<void>(`/admin/api-tokens/${id}`, { method: 'DELETE' }),
    // service accounts
    serviceAccounts: () => apiFetch<ServiceAccount[]>('/service-accounts'),
    createServiceAccount: (name: string) => apiFetch<ServiceAccount>('/service-accounts', { method: 'POST', body: { name } }),
    deleteServiceAccount: (id: number) => apiFetch<void>(`/service-accounts/${id}`, { method: 'DELETE' }),
    accountTokens: (id: number) => apiFetch<ApiToken[]>(`/service-accounts/${id}/tokens`),
    createAccountToken: (id: number, body: TokenRequest) =>
      apiFetch<ApiTokenCreated>(`/service-accounts/${id}/tokens`, { method: 'POST', body }),
    revokeAccountToken: (id: number, tokenId: number) =>
      apiFetch<void>(`/service-accounts/${id}/tokens/${tokenId}`, { method: 'DELETE' }),
    // webhook
    webhooks: () => apiFetch<Webhook[]>('/webhooks'),
    createWebhook: (body: { name: string; url: string; events: WebhookEvent[]; project_id: number | null }) =>
      apiFetch<WebhookWithSecret>('/webhooks', { method: 'POST', body }),
    updateWebhook: (id: number, body: Partial<{ name: string; url: string; events: WebhookEvent[]; project_id: number; clear_project: boolean; active: boolean }>) =>
      apiFetch<Webhook>(`/webhooks/${id}`, { method: 'PATCH', body }),
    rotateWebhookSecret: (id: number) => apiFetch<WebhookWithSecret>(`/webhooks/${id}/rotate-secret`, { method: 'POST' }),
    deleteWebhook: (id: number) => apiFetch<void>(`/webhooks/${id}`, { method: 'DELETE' }),
    testWebhook: (id: number) => apiFetch<WebhookDelivery>(`/webhooks/${id}/test`, { method: 'POST' }),
    deliveries: (id: number) => apiFetch<WebhookDelivery[]>(`/webhooks/${id}/deliveries`),
    redeliver: (deliveryId: number) => apiFetch<WebhookDelivery>(`/webhooks/deliveries/${deliveryId}/redeliver`, { method: 'POST' }),
  }
}

// expiries offered for a token: the gateway decides the real cap
export const TOKEN_DURATIONS = [30, 90, 180, 365]
