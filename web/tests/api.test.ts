import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createMockApi } from '../dev/mock-api.ts';
import { ApiError, createApiClient } from '../src/api/client.ts';
import type { CountQuery, RecordQuery } from '../src/api/types.ts';

const baseUrl = 'http://localhost';
const mockClient = () => createApiClient({ baseUrl, fetchRequest: createMockApi() });

test('UUID правки сохраняется после потери ответа, следующая правка получает новый ключ', async () => {
  const mock = createMockApi();
  const requests: { key: string | null; body: unknown }[] = [];
  const api = createApiClient({
    baseUrl,
    fetchRequest: async (request) => {
      if (request.method !== 'PATCH') return mock(request);
      requests.push({ key: request.headers.get('Idempotency-Key'), body: await request.clone().json() });
      const response = await mock(request);
      if (requests.length === 1) throw new TypeError('Потерян ответ после фиксации.');
      return response;
    },
  });

  const [dataset] = (await api.listDatasets()).items;
  const [record] = (await api.queryRecords(dataset.id, { limit: 1 })).items;
  const updated = await api.updateRecord(record.id, record.version, { quantity: 24 });
  assert.equal(updated.version, record.version + 1);
  assert.equal(updated.doc.quantity, 24);
  assert.equal((await api.getRecord(record.id)).record.version, updated.version);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0], requests[1]);
  assert.deepEqual(requests[0].body, { expected_version: record.version, values: { quantity: 24 } });
  assert.match(requests[0].key ?? '', /^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i);
  await api.updateRecord(record.id, updated.version, { quantity: 25 });
  assert.notEqual(requests[2].key, requests[0].key);
});

test('POST для чтения не получает ключ, создание записи получает', async () => {
  const mock = createMockApi();
  const seen: (string | null)[] = [];
  const api = createApiClient({
    baseUrl,
    fetchRequest: (request) => {
      if (request.method === 'POST') seen.push(request.headers.get('Idempotency-Key'));
      return mock(request);
    },
  });
  const [dataset] = (await api.listDatasets()).items;
  await api.queryRecords(dataset.id);
  assert.equal((await api.countRecords(dataset.id)).count, 10_000);
  const created = await api.createRecord(dataset.id, { name: 'Насос', quantity: 7 });
  assert.equal(created.version, 1);
  assert.equal((await api.countRecords(dataset.id)).count, 10_001);
  assert.deepEqual(seen.slice(0, 2), [null, null]);
  assert.ok(seen[2]);
});

test('параллельные правки одной версии дают одну запись и структурированную ошибку', async () => {
  const api = mockClient();
  const [dataset] = (await api.listDatasets()).items;
  const [record] = (await api.queryRecords(dataset.id, { limit: 1 })).items;
  const changes = await Promise.allSettled([
    api.updateRecord(record.id, record.version, { quantity: 18 }),
    api.updateRecord(record.id, record.version, { quantity: 20 }),
  ]);
  assert.equal(changes.filter((result) => result.status === 'fulfilled').length, 1);
  const failed = changes.find((result) => result.status === 'rejected');
  assert.ok(failed);
  const error: unknown = failed.reason;
  assert.ok(error instanceof ApiError);
  assert.equal(error.status, 409);
  assert.equal(error.code, 'version_conflict');
  assert.equal(error.message, 'Запись уже изменена.');
  assert.equal(error.field, 'expected_version');
  assert.deepEqual(error.details, { current_version: record.version + 1 });
});

test('ошибка HTTP читается по контракту и не повторяет запрос', async () => {
  let attempts = 0;
  const api = createApiClient({
    baseUrl,
    fetchRequest: async () => {
      attempts += 1;
      return Response.json({
        error: { code: 'value_number', message: 'Ожидается число', field: 'quantity', details: { row: 30 } },
      }, { status: 400 });
    },
  });
  await assert.rejects(api.updateRecord('record', 2, { quantity: 'сто' }), {
    name: 'ApiError', status: 400, code: 'value_number', message: 'Ожидается число',
    field: 'quantity', details: { row: 30 },
  });
  assert.equal(attempts, 1);
});

test('сбой чтения тела ответа повторяет запрос, отмена прерывает без повтора', async () => {
  let attempts = 0;
  const api = createApiClient({
    baseUrl,
    fetchRequest: async () => {
      attempts += 1;
      if (attempts === 1) {
        const interrupted = new ReadableStream({
          start(controller) { controller.error(new TypeError('Ответ прерван.')); },
        });
        return new Response(interrupted);
      }
      return Response.json({ count: 3 });
    },
  });
  assert.deepEqual(await api.countRecords('dataset'), { count: 3 });
  assert.equal(attempts, 2);

  const controller = new AbortController();
  let abortedAttempts = 0;
  const cancelled = createApiClient({
    baseUrl,
    fetchRequest: async (request) => {
      abortedAttempts += 1;
      controller.abort();
      request.signal.throwIfAborted();
      throw new Error('Запрос не отменён.');
    },
  });
  await assert.rejects(cancelled.health(controller.signal), { name: 'AbortError' });
  assert.equal(abortedAttempts, 1);
});

test('ошибки прокси и исчерпанный повтор сети имеют понятные коды', async () => {
  let attempts = 0;
  const noNetwork = createApiClient({
    baseUrl,
    fetchRequest: async () => {
      attempts += 1;
      throw new TypeError('Соединение недоступно.');
    },
  });
  await assert.rejects(noNetwork.health(), { name: 'ApiError', status: 0, code: 'network_error' });
  assert.equal(attempts, 2);
  const proxy = createApiClient({ baseUrl, fetchRequest: async () => new Response('<html>', { status: 502 }) });
  await assert.rejects(proxy.health(), { name: 'ApiError', status: 502, code: 'http_error' });
});

test('у заглушки 10 000 уникальных строк, справочники и последняя страница без курсора', async () => {
  const api = mockClient();
  const [site] = (await api.listSites()).items;
  const [dataset] = (await api.listDatasets({ site_id: site.id })).items;
  const type = await api.getType(dataset.type_id);
  assert.ok(type.fields.some((field) => field.key === 'manufacturer' && field.kind === 'choice'));
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await api.queryRecords(dataset.id, { limit: 200, cursor });
    assert.equal(page.items.length, 200);
    assert.ok(Object.keys(page.refs.choices).length > 0);
    assert.ok(Object.keys(page.refs.kks).length > 0);
    for (const row of page.items) {
      assert.ok(!seen.has(row.id));
      seen.add(row.id);
    }
    cursor = page.next_cursor;
  } while (cursor);
  assert.equal(seen.size, 10_000);
});

test('заглушка фильтрует по условиям и отдаёт ошибку несовместимого курсора', async () => {
  const api = mockClient();
  const [dataset] = (await api.listDatasets()).items;
  const filters: CountQuery = { search: 'насос питат', where: [{ field: 'quantity', op: 'gte', value: 10 }] };
  const query: RecordQuery = { ...filters, limit: 7, sort: { field: 'quantity', dir: 'asc' } };
  const page = await api.queryRecords(dataset.id, query);
  assert.equal(page.items.length, 7);
  assert.ok(page.items.every((row) => Number(row.doc.quantity) >= 10 && String(row.doc.name).includes('Насос')));
  assert.ok((await api.countRecords(dataset.id, filters)).count > 7);
  await assert.rejects(api.queryRecords(dataset.id, {
    ...query, cursor: page.next_cursor, sort: { field: 'quantity', dir: 'desc' },
  }), { name: 'ApiError', status: 400, code: 'query_invalid', field: 'cursor' });
  await assert.rejects(api.queryRecords(dataset.id, { limit: 201 }), { status: 400, field: 'limit' });
  await assert.rejects(api.getRecord('missing'), { status: 404, code: 'record_not_found' });
});
