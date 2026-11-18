import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, createApiClient } from '../src/api/client.ts';

test('создание и восстановление копии не повторяются после потери ответа', async () => {
  const seen: Request[] = [];
  const api = createApiClient({
    baseUrl: 'http://localhost',
    fetchRequest: async (request) => {
      seen.push(request);
      throw new TypeError('Ответ потерян после выполнения операции.');
    },
  });
  const password = crypto.randomUUID();
  for (const operation of [
    () => api.createBackup(password, 'Перед изменением'),
    () => api.restoreBackup('localai-test.localai-backup', password),
  ]) {
    const count = seen.length;
    await assert.rejects(
      operation(),
      (error: unknown) => error instanceof ApiError && error.code === 'network_error',
    );
    assert.equal(seen.length, count + 1);
    assert.equal(seen.at(-1)!.headers.get('Idempotency-Key'), null);
    assert.equal((await seen.at(-1)!.json()).password, password);
  }
});

test('ссылка скачивания кодирует имя и остаётся на адресе сервера', () => {
  const api = createApiClient({ baseUrl: 'http://localhost:8730' });
  const name = 'localai-a #%.localai-backup';
  assert.equal(
    api.backupDownloadUrl(name),
    `http://localhost:8730/api/v1/backups/${encodeURIComponent(name)}/file`,
  );
});
