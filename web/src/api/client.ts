import { importMethods } from './imports.ts';
import type { ChatPage, EventPage, Message, ReadState, Revision } from '../chat/types.ts';
import { text } from '../text.ts';
import type { paths } from './contract.d.ts';
import type {
  AllowedNetwork, CountQuery, Dataset, Health, Items, Me, NamedEntity, NetworkCreate,
  Permission, PermissionCatalog, Role, RoleCreate, RoleDetails, RoleUpdate,
  TemporaryUser, User, UserCreate, UserUpdate, RecordCreate, RecordDetail, RecordPage,
  RecordQuery, RecordRow, RecordType, RecordUpdate, RecordValues, TypeDetails, KksRules, KksResolution,
  Backup, BackupEntry, BackupRestoreResult, KksCard,
} from './types.ts';

export type ApiPath = keyof paths;
type Transport = (request: Request) => Promise<Response>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly field?: string;
  readonly details?: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    field?: string,
    details?: Record<string, unknown>,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.field = field;
    this.details = details;
  }
}

/** Коды, после которых нужен вход: сессии нет, она закрыта или истекла. */
export const signedOutCodes = new Set(['auth_required', 'session_invalid', 'session_expired']);

/**
 * Сессия потеряна посреди работы. Слушатель — экран приложения: он
 * показывает вход, не перезагружая страницу.
 */
export const sessionEvents = new EventTarget();

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function decode(response: Response, raw: string): unknown {
  let data: unknown;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch (cause) {
      if (response.ok) {
        throw new ApiError(response.status, 'invalid_response', text.invalidResponse, undefined,
          undefined, { cause });
      }
    }
  }

  if (response.ok) {
    if (response.status === 204 || object(data)) return data;
    throw new ApiError(response.status, 'invalid_response', text.invalidResponse);
  }

  if (object(data) && object(data.error)) {
    const { code, message, field, details } = data.error;
    if (typeof code === 'string' && typeof message === 'string') {
      throw new ApiError(response.status, code, message, typeof field === 'string' ? field : undefined,
        object(details) ? details : undefined);
    }
  }
  throw new ApiError(response.status, 'http_error', text.httpError(response.status));
}

function newKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues доступен и при обращении к локальному серверу по HTTP из сети.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

function signedOut(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401 && signedOutCodes.has(error.code);
}

async function send(request: Request, fetchRequest: Transport, retry = true): Promise<unknown> {
  // Тело и ключ уже зафиксированы. Обе попытки отправляют копию одного запроса.
  for (let attempt = 0; ; attempt += 1) {
    let response: Response;
    let raw: string;
    try {
      response = await fetchRequest(request.clone());
      raw = await response.text();
    } catch (cause) {
      if (request.signal.aborted || (cause instanceof DOMException && cause.name === 'AbortError')) {
        throw cause;
      }
      if (attempt === 0 && retry) continue;
      throw new ApiError(0, 'network_error', text.networkError, undefined, undefined, { cause });
    }
    try {
      return decode(response, raw);
    } catch (error) {
      if (signedOut(error)) sessionEvents.dispatchEvent(new Event('signed-out'));
      if (error instanceof ApiError && error.status === 403 &&
        (error.code === 'password_change_required' || error.code === 'forbidden')) {
        sessionEvents.dispatchEvent(new Event('permissions-changed'));
      }
      throw error;
    }
  }
}

export type Options = {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  id?: string;
  name?: string;
  row?: number;
  file?: File;
  query?: Record<string, string | undefined>;
  body?: unknown;
  signal?: AbortSignal;
};

export function createApiClient({
  baseUrl = globalThis.location?.origin ?? 'http://localhost',
  fetchRequest = (request) => fetch(request),
}: { baseUrl?: string; fetchRequest?: Transport } = {}) {
  async function request<T>(path: ApiPath, options: Options = {}): Promise<T> {
    const { method = 'GET', id, name, query, body, signal } = options;
    const target = id === undefined ? path : path.replace('{id}', encodeURIComponent(id));
    const resolved = name === undefined ? target : target.replace('{name}', encodeURIComponent(name));
    const url = new URL(options.row === undefined ? resolved : resolved.replace('{row}', String(options.row)), baseUrl);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, value);
    }
    const headers = new Headers({ Accept: 'application/json' });
    if (options.file) headers.set('Content-Type', 'application/octet-stream');
    else if (body !== undefined) headers.set('Content-Type', 'application/json');
    const reads = path === '/api/v1/datasets/{id}/records/query' || path === '/api/v1/datasets/{id}/records/count';
    // Вход, выход и смена пароля не повторяются по ключу: сервер их не принимает.
    const session = path.startsWith('/api/v1/auth/');
    const backupWrite = method !== 'GET' && path.startsWith('/api/v1/backups');
    const importPost = method === 'POST' && path.startsWith('/api/v1/imports');
    if (method !== 'GET' && !reads && !session && !backupWrite && !importPost) headers.set('Idempotency-Key', newKey());
    // Сессия живёт в cookie HttpOnly: скрипт страницы её не видит и не хранит.
    const outgoing = new Request(url, {
      method, headers, body: options.file ?? (body === undefined ? undefined : JSON.stringify(body)), signal,
      credentials: 'same-origin',
    });
    // Создание и восстановление копий сервер не повторяет по ключу.
    return await send(outgoing, fetchRequest, !backupWrite && !importPost && path !== '/api/v1/events') as T;
  }

  return {
    imports: importMethods(request),
    health: (signal?: AbortSignal) =>
      request<Health>('/api/v1/system/health', { signal }),
    me: (signal?: AbortSignal) =>
      request<Me>('/api/v1/auth/me', { signal }),
    login: (login: string, password: string, signal?: AbortSignal) =>
      request<Me>('/api/v1/auth/login', { method: 'POST', body: { login, password }, signal }),
    logout: (signal?: AbortSignal) =>
      request<undefined>('/api/v1/auth/logout', { method: 'POST', signal }),
    changePassword: (currentPassword: string, newPassword: string, signal?: AbortSignal) =>
      request<Me>('/api/v1/auth/password', {
        method: 'POST', body: { current_password: currentPassword, new_password: newPassword }, signal,
      }),
    permissionCatalog: (signal?: AbortSignal) =>
      request<PermissionCatalog>('/api/v1/auth/permissions', { signal }),
    listUsers: (signal?: AbortSignal) => request<Items<User>>('/api/v1/users', { signal }),
    createUser: (body: UserCreate) => request<TemporaryUser>('/api/v1/users', { method: 'POST', body }),
    updateUser: (id: string, body: UserUpdate) => request<User>('/api/v1/users/{id}', { method: 'PATCH', id, body }),
    blockUser: (id: string) => request<User>('/api/v1/users/{id}/block', { method: 'POST', id }),
    unblockUser: (id: string) => request<User>('/api/v1/users/{id}/unblock', { method: 'POST', id }),
    resetPassword: (id: string) => request<TemporaryUser>('/api/v1/users/{id}/reset-password', { method: 'POST', id }),
    listRoles: (signal?: AbortSignal) => request<Items<Role>>('/api/v1/roles', { signal }),
    getRole: (id: string, signal?: AbortSignal) => request<RoleDetails>('/api/v1/roles/{id}', { id, signal }),
    createRole: (body: RoleCreate) => request<RoleDetails>('/api/v1/roles', { method: 'POST', body }),
    updateRole: (id: string, body: RoleUpdate) => request<RoleDetails>('/api/v1/roles/{id}', { method: 'PATCH', id, body }),
    archiveRole: (id: string) => request<RoleDetails>('/api/v1/roles/{id}/archive', { method: 'POST', id }),
    restoreRole: (id: string) => request<RoleDetails>('/api/v1/roles/{id}/restore', { method: 'POST', id }),
    setPermissions: (id: string, permissions: Permission[]) => request<RoleDetails>('/api/v1/roles/{id}/permissions', {
      method: 'PUT', id, body: { permissions },
    }),
    listNetworks: (signal?: AbortSignal) => request<Items<AllowedNetwork>>('/api/v1/system/allowed-ips', { signal }),
    addNetwork: (body: NetworkCreate) => request<AllowedNetwork>('/api/v1/system/allowed-ips', { method: 'POST', body }),
    removeNetwork: (id: string) => request<undefined>('/api/v1/system/allowed-ips/{id}', { method: 'DELETE', id }),
    getKksRules: (signal?: AbortSignal) => request<KksRules>('/api/v1/kks/rules', { signal }),
    setKksRules: (signalPattern: string, signal?: AbortSignal) => request<KksRules>('/api/v1/kks/rules', {
      method: 'PUT', body: { signal_pattern: signalPattern }, signal,
    }),
    resolveKks: (siteId: string, code: string, signal?: AbortSignal) => request<KksResolution>('/api/v1/sites/{id}/kks/resolve', {
      id: siteId, query: { code }, signal,
    }),
    getKksCard: (id: string, signal?: AbortSignal) => request<KksCard>('/api/v1/kks/{id}', { id, signal }),
    events: (after?: number, wait = 25, signal?: AbortSignal) => request<EventPage>('/api/v1/events', {
      query: { after: after?.toString(), wait: after === undefined ? undefined : String(wait) }, signal,
    }),
    messages: (page: { after?: number; before?: number; limit?: number } = {}, signal?: AbortSignal) =>
      request<ChatPage>('/api/v1/chat/messages', {
        query: { after: page.after?.toString(), before: page.before?.toString(), limit: page.limit?.toString() }, signal,
      }),
    postMessage: (text: string) => request<Message>('/api/v1/chat/messages', { method: 'POST', body: { text } }),
    editMessage: (id: string, version: number, text: string) => request<Message>('/api/v1/chat/messages/{id}', {
      id, method: 'PATCH', body: { version, text },
    }),
    deleteMessage: (id: string, version: number) => request<Message>('/api/v1/chat/messages/{id}', {
      id, method: 'DELETE', query: { version: String(version) },
    }),
    hideMessage: (id: string, reason: string) => request<Message>('/api/v1/chat/messages/{id}/hide', {
      id, method: 'POST', body: { reason },
    }),
    unhideMessage: (id: string) => request<Message>('/api/v1/chat/messages/{id}/unhide', { id, method: 'POST' }),
    messageHistory: (id: string, signal?: AbortSignal) =>
      request<Items<Revision>>('/api/v1/chat/messages/{id}/history', { id, signal }),
    findMessages: (q: string, signal?: AbortSignal) =>
      request<Items<Message>>('/api/v1/chat/search', { query: { q }, signal }),
    readChat: (seq: number, signal?: AbortSignal) =>
      request<ReadState>('/api/v1/chat/read', { method: 'POST', body: { seq }, signal }),
    listBackups: (signal?: AbortSignal) => request<Items<BackupEntry>>('/api/v1/backups', { signal }),
    createBackup: (password: string, label?: string) => request<Backup>('/api/v1/backups', {
      method: 'POST', body: { password, label: label || null },
    }),
    restoreBackup: (name: string, password: string) => request<BackupRestoreResult>('/api/v1/backups/{name}/restore', {
      method: 'POST', name, body: { password },
    }),
    backupDownloadUrl: (name: string) => new URL(`/api/v1/backups/${encodeURIComponent(name)}/file`, baseUrl).href,
    listSites: (signal?: AbortSignal) =>
      request<Items<NamedEntity>>('/api/v1/sites', { signal }),
    listTypes: (signal?: AbortSignal) =>
      request<Items<RecordType>>('/api/v1/types', { signal }),
    getType: (id: string, signal?: AbortSignal) =>
      request<TypeDetails>('/api/v1/types/{id}', { id, signal }),
    listDatasets: (query?: { site_id?: string; type_id?: string }, signal?: AbortSignal) =>
      request<Items<Dataset>>('/api/v1/datasets', { query, signal }),
    getDataset: (id: string, signal?: AbortSignal) =>
      request<Dataset>('/api/v1/datasets/{id}', { id, signal }),
    queryRecords: (id: string, body: RecordQuery = {}, signal?: AbortSignal) =>
      request<RecordPage>('/api/v1/datasets/{id}/records/query', { method: 'POST', id, body, signal }),
    countRecords: (id: string, body: CountQuery = {}, signal?: AbortSignal) =>
      request<{ count: number }>('/api/v1/datasets/{id}/records/count', { method: 'POST', id, body, signal }),
    getRecord: (id: string, signal?: AbortSignal) =>
      request<RecordDetail>('/api/v1/records/{id}', { id, signal }),
    createRecord: (id: string, values: RecordValues, signal?: AbortSignal) =>
      request<RecordRow>('/api/v1/datasets/{id}/records', { method: 'POST', id, body: { values } satisfies RecordCreate, signal }),
    updateRecord: (id: string, version: number, values: RecordValues, signal?: AbortSignal) =>
      request<RecordRow>('/api/v1/records/{id}', {
        method: 'PATCH', id, body: { expected_version: version, values } satisfies RecordUpdate, signal,
      }),
  };
}

export const api = createApiClient();
