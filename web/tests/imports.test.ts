import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createApiClient } from '../src/api/client.ts';

test('Загрузка передаёт файл, план передаёт настройки, применение — только имя профиля', async () => {
  const seen: Request[] = [];
  const api = createApiClient({
    fetchRequest: async (req) => {
      seen.push(req);
      return Response.json({});
    },
  });
  const signal = new AbortController().signal;
  const file = new File([Uint8Array.of(0x50, 0x4b, 3, 4)], 'Цех №1.xlsx');
  await api.imports.upload(file, signal);
  const plan = {
    mode: 'records' as const,
    dataset_id: 'target',
    sheet: 'Лист',
    header_row: 3,
    mapping: { Тег: 'tag' },
    key: 'tag',
  };
  await api.imports.plan('пакет', plan, signal);
  await api.imports.decide('пакет', 5, 'apply', signal);
  await api.imports.apply('пакет', ' Смена ', signal);
  const [upload, preview, decision, apply] = seen;
  assert.equal(new URL(upload.url).searchParams.get('name'), file.name);
  assert.equal(upload.headers.get('Content-Type'), 'application/octet-stream');
  assert.deepEqual(await upload.arrayBuffer(), await file.arrayBuffer());
  for (const req of [upload, preview, apply])
    assert.equal(req.headers.has('Idempotency-Key'), false);
  assert.deepEqual(await preview.json(), plan);
  assert.equal(
    decodeURIComponent(new URL(decision.url).pathname),
    '/api/v1/imports/пакет/rows/5/decision',
  );
  assert.ok(decision.headers.get('Idempotency-Key'));
  assert.deepEqual(await decision.json(), { decision: 'apply' });
  assert.deepEqual(await apply.json(), { save_profile: 'Смена' });
});
