import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createApiClient } from '../src/api/client.ts';
import type { SearchResult } from '../src/api/types.ts';
import { sections } from '../src/routes.ts';
import { kksUrl, recordUrl, resultGroups, searchTarget } from '../src/search/model.ts';
import { text } from '../src/text.ts';

test('точный ККС первый, источник совпадения и реестр записи сохраняются', () => {
  const base = { site_id: 'site', site: 'ТЭЦ', name: null, archived: false };
  const result: SearchResult = {
    query: 'код',
    took_ms: 1,
    more: { records: false },
    kks: [
      { ...base, id: 'partial', code: 'ДРУГОЙ', exact: false },
      { ...base, id: 'exact', code: 'КОД', exact: true, via: 'signal', signal: 'XQ01' },
    ],
    records: [
      {
        id: 'record',
        dataset_id: 'dataset',
        dataset: 'Сигналы',
        label: 'КОДXQ01',
        version: 1,
        match: { field: 'Код сигнала', text: 'КОДXQ01' },
      },
    ],
  };
  const groups = resultGroups(result, sections, 'код');
  assert.equal(groups[0].items[0].href, kksUrl('exact'));
  assert.equal(groups[0].items[0].badge, text.find.signal('XQ01'));
  assert.match(groups[1].items[0].detail, /Сигналы · Код сигнала: КОДXQ01/);
  assert.equal(result.kks[0].id, 'partial');
});

test('команды фильтруются из разрешённых разделов, ссылки сохраняют идентификаторы', () => {
  const allowed = sections.filter((item) => item.path !== '/admin');
  assert.equal(resultGroups(null, allowed, 'администрирование')[2].items.length, 0);
  assert.equal(resultGroups(null, allowed, 'РЕЕСТ')[2].items[0].href, '/registries');
  const id = 'a/b?x&y';
  const record = new URL(recordUrl('dataset/1', id), 'http://localhost');
  assert.deepEqual(searchTarget(record.pathname, record.search), {
    kind: 'record',
    dataset: 'dataset/1',
    id,
  });
  const kks = new URL(kksUrl(id), 'http://localhost');
  assert.deepEqual(searchTarget(kks.pathname, kks.search), { kind: 'kks', id });
  assert.equal(searchTarget('/registries', '?record=missing-dataset'), null);
});

test('поиск кодирует строку и прекращает запрос при отмене без повтора', async () => {
  const controller = new AbortController();
  let calls = 0;
  const api = createApiClient({
    baseUrl: 'http://localhost',
    fetchRequest: (request) => {
      calls += 1;
      assert.equal(new URL(request.url).searchParams.get('q'), 'ККС & сигнал');
      assert.equal(request.method, 'GET');
      return new Promise<Response>((_resolve, reject) => {
        request.signal.addEventListener(
          'abort',
          () => reject(new DOMException('Отмена', 'AbortError')),
          { once: true },
        );
      });
    },
  });
  const pending = api.search('ККС & сигнал', controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(calls, 1);
});
