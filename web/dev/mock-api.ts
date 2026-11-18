import type {
  Condition, CountQuery, RecordDoc, RecordQuery, RecordRow, Scalar, Sort,
} from '../src/api/types.ts';
import {
  choiceLabels, dataset, kksLabels, makeRows, mockHealth, mockMe, refsFor, registryType, site,
} from './fixture.ts';

class MockError extends Error {
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
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
    this.details = details;
  }
}

function invalidQuery(field: string): never {
  throw new MockError(400, 'query_invalid', 'Неверный параметр запроса.', field);
}

function lookup(row: RecordRow, key: string): Scalar {
  switch (key) {
    case '$id': return row.id;
    case '$version': return row.version;
    case '$created_at': return row.created_at;
    case '$updated_at': return row.updated_at;
    default: {
      const value = row.doc[key];
      return Array.isArray(value) ? null : value ?? null;
    }
  }
}

function display(value: Scalar): Scalar {
  return typeof value === 'string' ? choiceLabels[value] ?? kksLabels[value] ?? value : value;
}

function fold(value: unknown): string {
  return String(value ?? '').toLowerCase().replaceAll('ё', 'е');
}

function compare(left: Scalar, right: Scalar): number {
  if (left === right) return 0;
  if (left === null) return -1;
  if (right === null) return 1;
  return left < right ? -1 : 1;
}

function matches(row: RecordRow, condition: Condition): boolean {
  const raw = lookup(row, condition.field);
  if (condition.op === 'empty') return raw === null;
  if (condition.op === 'not_empty') return raw !== null;
  if (!('value' in condition)) invalidQuery('where');

  const label = display(raw);
  const equal = (arg: Scalar | { id: string }) => {
    if (arg !== null && typeof arg === 'object') return raw === arg.id;
    if (choiceLabels[String(raw)] || kksLabels[String(raw)]) {
      return arg === raw || fold(arg) === fold(label);
    }
    return typeof raw === 'number' && typeof arg === 'string' && arg.trim()
      ? raw === Number(arg.replace(',', '.'))
      : raw === arg;
  };
  if (condition.op === 'in') return condition.value.some(equal);
  const value = condition.value;
  switch (condition.op) {
    case 'eq': return equal(value);
    case 'ne': return !equal(value);
    case 'contains': return fold(label).includes(fold(value));
    case 'starts': return fold(label).startsWith(fold(value));
    default: {
      if (raw === null || typeof value === 'object') return false;
      const bound = typeof raw === 'number' && typeof value === 'string' ? Number(value.replace(',', '.')) : value;
      if (typeof bound === 'number' && !Number.isFinite(bound)) invalidQuery(condition.field);
      const order = compare(raw, bound);
      if (condition.op === 'lt') return order < 0;
      if (condition.op === 'lte') return order <= 0;
      if (condition.op === 'gt') return order > 0;
      return order >= 0;
    }
  }
}

function selectRows(rows: Iterable<RecordRow>, query: CountQuery): RecordRow[] {
  if (query.search != null && typeof query.search !== 'string') invalidQuery('search');
  if (query.where != null && !Array.isArray(query.where)) invalidQuery('where');
  const words = fold(query.search).split(/\s+/).filter(Boolean);
  return Array.from(rows).filter((row) => {
    if (query.archived !== 'any' && Boolean(row.archived_at) !== (query.archived === true)) return false;
    if (query.kks && row.doc.kks !== query.kks) return false;
    if (words.length) {
      const searchable = Object.values(row.doc).map((value) => {
        return fold(Array.isArray(value) ? value.map(display).join(' ') : display(value));
      });
      if (!words.every((word) => searchable.some((value) => value.includes(word)))) return false;
    }
    return query.where?.every((condition) =>
      'any' in condition ? condition.any.some((part) => matches(row, part)) : matches(row, condition),
    ) ?? true;
  });
}

function readOffset(cursor: string, sort: string): number {
  let data: { sort?: string; offset?: number } | null;
  try {
    data = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
  } catch {
    invalidQuery('cursor');
  }
  if (!data || data.sort !== sort || typeof data.offset !== 'number'
    || !Number.isSafeInteger(data.offset) || data.offset < 0) invalidQuery('cursor');
  return data.offset;
}

function sortKey(sort?: Sort | null): string {
  if (sort == null) return '';
  const fields = registryType.fields.map((field) => field.key);
  if (![...fields, '$id', '$version', '$created_at', '$updated_at'].includes(sort.field)
    || (sort.dir !== 'asc' && sort.dir !== 'desc')) invalidQuery('sort');
  return `${sort.field}:${sort.dir}`;
}

function page(rows: Iterable<RecordRow>, query: RecordQuery) {
  const limit = query.limit ?? 50;
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) invalidQuery('limit');
  const tag = sortKey(query.sort);
  const start = query.cursor == null ? 0 : readOffset(query.cursor, tag);
  const selected = selectRows(rows, query);
  if (query.sort) {
    const { field, dir } = query.sort;
    const direction = dir === 'asc' ? 1 : -1;
    selected.sort((a, b) => direction * compare(display(lookup(a, field)), display(lookup(b, field))));
  }
  const end = start + limit;
  const items = selected.slice(start, end);
  const next_cursor = end < selected.length
    ? Buffer.from(JSON.stringify({ sort: tag, offset: end })).toString('base64url')
    : null;
  return { items, next_cursor, refs: refsFor(items) };
}

function bodyObject(data: unknown, field?: string): Record<string, unknown> {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new MockError(400, 'request_invalid', 'Ожидается объект JSON.', field);
  }
  return data as Record<string, unknown>;
}

function editDoc(doc: RecordDoc, values: unknown): RecordDoc {
  const next = { ...doc };
  for (const [key, value] of Object.entries(bodyObject(values, 'values'))) {
    const field = registryType.fields.find((item) => item.key === key);
    if (!field) throw new MockError(400, 'field_unknown', 'Поле не найдено.', key);
    if (field.kind === 'integer' && value !== null && !Number.isInteger(value)) {
      throw new MockError(400, 'value_number', 'Ожидается целое число.', key);
    }
    if (value && typeof value === 'object' && 'id' in value && typeof value.id === 'string') {
      next[key] = value.id;
    } else if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) {
      next[key] = value as Scalar;
    } else {
      throw new MockError(400, 'request_invalid', 'Неверное значение поля.', key);
    }
  }
  if (!next.name) throw new MockError(400, 'value_required', 'Укажите наименование.', 'name');
  return next;
}

async function readJson(request: Request) {
  try {
    return bodyObject(await request.json());
  } catch (error) {
    if (error instanceof MockError) throw error;
    throw new MockError(400, 'request_json', 'Не удалось прочитать JSON запроса.');
  }
}

type Receipt = { fingerprint: string; row: RecordRow; status: number };

export function createMockApi() {
  const rows = new Map(makeRows().map((row) => [row.id, row]));
  const receipts = new Map<string, Receipt>();
  // Заглушка начинает с выполненным входом, чтобы экраны открывались сразу.
  // После выхода /auth/me отвечает 401, а вход принимает любой непустой пароль.
  let signedIn = true;

  async function auth(request: Request, action: string): Promise<Response> {
    if (request.method === 'GET' && action === 'me') {
      if (!signedIn) throw new MockError(401, 'auth_required', 'Нужно войти в систему');
      return Response.json(mockMe);
    }
    if (request.method !== 'POST') throw new MockError(404, 'route_not_found', 'Маршрут не найден.');
    if (action === 'login') {
      const body = await readJson(request);
      if (typeof body.login !== 'string' || !body.login || typeof body.password !== 'string' || !body.password) {
        throw new MockError(401, 'login_failed', 'Неверный логин или пароль');
      }
      signedIn = true;
      return Response.json(mockMe);
    }
    if (action === 'logout') {
      signedIn = false;
      return new Response(null, { status: 204 });
    }
    if (action === 'password') return Response.json(mockMe);
    throw new MockError(404, 'route_not_found', 'Маршрут не найден.');
  }

  async function change(request: Request, recordId?: string): Promise<Response> {
    const key = request.headers.get('Idempotency-Key');
    const body = await readJson(request);
    const fingerprint = JSON.stringify([request.method, request.url, body]);
    const receipt = key && receipts.get(key);
    if (receipt) {
      if (receipt.fingerprint !== fingerprint) {
        throw new MockError(409, 'idempotency_mismatch', 'Ключ уже использован с другим запросом.');
      }
      return Response.json(receipt.row, { status: receipt.status });
    }
    const record = recordId ? rows.get(recordId) : undefined;
    if (recordId && !record) throw new MockError(404, 'record_not_found', 'Запись не найдена.');
    if (record && !Number.isInteger(body.expected_version)) {
      throw new MockError(400, 'request_invalid', 'Укажите ожидаемую версию.', 'expected_version');
    }
    if (record && record.version !== body.expected_version) {
      throw new MockError(409, 'version_conflict', 'Запись уже изменена.', 'expected_version', {
        current_version: record.version,
      });
    }
    const doc = editDoc(record?.doc ?? {}, body.values);
    const now = new Date().toISOString();
    const row: RecordRow = record
      ? JSON.stringify(doc) === JSON.stringify(record.doc)
        ? record
        : { ...record, doc, version: record.version + 1, updated_at: now }
      : { id: crypto.randomUUID(), dataset_id: dataset.id, doc, version: 1, archived_at: null, created_at: now, updated_at: now };
    rows.set(row.id, row);
    const status = record ? 200 : 201;
    if (key) receipts.set(key, { fingerprint, row, status });
    return Response.json(row, { status });
  }

  async function dispatch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const [, , , group, id, child, action] = url.pathname.split('/');
    const { method } = request;
    if (url.pathname.startsWith('/api/v1/')) {
      if (method === 'GET' && url.pathname === '/api/v1/system/health') return Response.json(mockHealth);
      if (group === 'auth' && id && !child) return auth(request, id);
      if (method === 'GET' && url.pathname === '/api/v1/sites') return Response.json({ items: [site] });
      if (group === 'types' && method === 'GET' && !child) {
        const { fields, ...summary } = registryType;
        if (!id) return Response.json({ items: [summary] });
        if (id === registryType.id) return Response.json({ ...summary, fields });
        throw new MockError(404, 'type_not_found', 'Тип не найден.');
      }
      if (group === 'datasets') {
        if (!id && method === 'GET') {
          const matchesFilters = ['site_id', 'type_id'].every((key) =>
            !url.searchParams.has(key) || url.searchParams.get(key) === dataset[key as 'site_id' | 'type_id'],
          );
          return Response.json({ items: matchesFilters ? [dataset] : [] });
        }
        if (id !== dataset.id) throw new MockError(404, 'dataset_not_found', 'Реестр не найден.');
        if (method === 'GET' && !child) return Response.json(dataset);
        if (method === 'POST' && child === 'records') {
          if (!action) return change(request);
          if (action === 'query') return Response.json(page(rows.values(), await readJson(request) as RecordQuery));
          if (action === 'count') return Response.json({ count: selectRows(rows.values(), await readJson(request) as CountQuery).length });
        }
      }
      if (group === 'records' && id && !child && (method === 'GET' || method === 'PATCH')) {
        if (method === 'PATCH') return change(request, id);
        const record = rows.get(id);
        if (!record) throw new MockError(404, 'record_not_found', 'Запись не найдена.');
        return Response.json({ record, refs: refsFor([record]) });
      }
    }
    throw new MockError(404, 'route_not_found', 'Маршрут не найден.');
  }

  return async (request: Request): Promise<Response> => {
    try {
      return await dispatch(request);
    } catch (error) {
      if (!(error instanceof MockError)) throw error;
      const { code, message, field, details, status } = error;
      return Response.json({ error: { code, message, field, details } }, { status });
    }
  };
}
