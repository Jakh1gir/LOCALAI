import { text } from '../text.ts';
import type { paths } from './contract.d.ts';
import type {
  CountQuery, Dataset, Health, Items, NamedEntity, RecordCreate, RecordDetail, RecordPage,
  RecordQuery, RecordRow, RecordType, RecordUpdate, RecordValues, TypeDetails,
} from './types.ts';

type ApiPath = keyof paths;
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

async function send(request: Request, fetchRequest: Transport): Promise<unknown> {
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
      if (attempt === 0) continue;
      throw new ApiError(0, 'network_error', text.networkError, undefined, undefined, { cause });
    }
    return decode(response, raw);
  }
}

type Options = {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  id?: string;
  query?: Record<string, string | undefined>;
  body?: unknown;
  signal?: AbortSignal;
};

export function createApiClient({
  baseUrl = globalThis.location?.origin ?? 'http://localhost',
  fetchRequest = (request) => fetch(request),
}: { baseUrl?: string; fetchRequest?: Transport } = {}) {
  async function request<T>(path: ApiPath, options: Options = {}): Promise<T> {
    const { method = 'GET', id, query, body, signal } = options;
    const url = new URL(id === undefined ? path : path.replace('{id}', encodeURIComponent(id)), baseUrl);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, value);
    }
    const headers = new Headers({ Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    const reads = path === '/api/v1/datasets/{id}/records/query' || path === '/api/v1/datasets/{id}/records/count';
    if (method !== 'GET' && !reads) headers.set('Idempotency-Key', newKey());
    const outgoing = new Request(url, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal,
    });
    return await send(outgoing, fetchRequest) as T;
  }

  return {
    health: (signal?: AbortSignal) =>
      request<Health>('/api/v1/system/health', { signal }),
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
