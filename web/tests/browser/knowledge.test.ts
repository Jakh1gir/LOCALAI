import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import test from 'node:test';
import type { Page, Browser } from 'playwright-core';
import { openAccessPage, confirmAccessAction } from './server.ts';

async function api(p: Page, path: string, method = 'GET', data?: unknown) {
  const response = await p.request.fetch('/api/v1' + path, {
    method,
    data,
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.ok(response.ok(), path + ' ' + response.status());
  return response.status() === 204 ? null : response.json();
}
async function user(
  admin: Page,
  browser: Browser,
  origin: string,
  login: string,
  permissions: object[],
) {
  const role = await api(admin, '/roles', 'POST', { name: login });
  await api(admin, '/roles/' + role.id + '/permissions', 'PUT', { permissions });
  const created = await api(admin, '/users', 'POST', { login, name: login, role_id: role.id });
  const context = await browser.newContext({ baseURL: origin }),
    page = await context.newPage();
  page.setDefaultTimeout(12000);
  await api(page, '/auth/login', 'POST', { login, password: created.temporary_password });
  await api(page, '/auth/password', 'POST', {
    current_password: created.temporary_password,
    new_password: randomUUID(),
  });
  return page;
}
async function find(page: Page, q: string) {
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog');
  await Promise.all([
    page.waitForResponse((r) => {
      const u = new URL(r.url());
      return u.pathname === '/api/v1/search' && u.searchParams.get('q') === q;
    }),
    dialog.getByRole('searchbox').fill(q),
  ]);
  return dialog;
}
async function fillNote(p: Page, title: string) {
  await p.getByLabel('Заголовок', { exact: true }).fill(title);
  await p.getByLabel('Текст заметки').fill('Проверить карточку перед обходом.');
  await p.getByRole('button', { name: 'Отправить на проверку' }).click();
  await p.getByRole('heading', { name: title, exact: true }).waitFor();
}

test(
  'C-403 · проверка знаний, права на источники и словарь станции',
  { timeout: 160000 },
  async (t) => {
    const { page: admin, browser, config, origin } = await openAccessPage(t);
    admin.setDefaultTimeout(12000);
    await api(admin, '/auth/login', 'POST', { login: config.login, password: config.password });
    const ds = (await api(admin, '/datasets')).items[0].id;
    const row = (await api(admin, '/datasets/' + ds + '/records/query', 'POST', { limit: 1 }))
      .items[0];
    const site = (await api(admin, '/sites')).items[0].id;
    const pump = await api(admin, '/sites/' + site + '/kks', 'POST', { code: '90LAC10AP995' });
    const limited = await user(admin, browser, origin, 'reviewer403', [
      { scope: 'section', target: 'knowledge', action: 'use' },
    ]);
    const engineer = await user(admin, browser, origin, 'engineer403', [
      { scope: 'dataset', target: ds, action: 'view' },
    ]);
    const reader = await user(admin, browser, origin, 'reader403', []);
    const deniedCalls: string[] = [];
    reader.on('request', (r) => {
      if (r.url().includes('/knowledge/queue')) deniedCalls.push(r.url());
    });
    await admin.goto('/knowledge?tab=queue');
    await admin.getByText('Очередь пуста').waitFor();
    await engineer.goto('/registries?dataset=' + ds + '&record=' + row.id);
    await engineer.getByRole('link', { name: 'Предложить заметку' }).click();
    await engineer.getByRole('link', { name: /Запись ·/ }).waitFor();
    await fillNote(engineer, 'Осмотр уплотнителя');
    const note = new URL(engineer.url()).searchParams.get('note')!;
    await admin.getByLabel('В очереди проверки: 1').waitFor();
    await limited.goto('/knowledge?tab=queue');
    await limited.getByText('Очередь пуста').waitFor();
    assert.equal(await limited.getByLabel('В очереди проверки: 1').count(), 0);
    await limited.context().close();
    await admin.getByRole('link', { name: 'Осмотр уплотнителя' }).click();
    await admin.getByRole('button', { name: 'Подтвердить', exact: true }).click();
    await admin.getByText('Подтверждена · Станция').waitFor();
    const hits = await find(engineer, 'уплотнителя');
    await hits.getByRole('link', { name: /Осмотр уплотнителя/ }).click();
    await engineer.getByText('Подтверждена · Станция').waitFor();
    await reader.goto('/knowledge');
    await reader.getByText('Заметки не найдены').waitFor();
    const hidden = await find(reader, 'уплотнителя');
    assert.equal(await hidden.getByRole('link', { name: /Осмотр уплотнителя/ }).count(), 0);
    await hidden.getByRole('button', { name: 'Закрыть' }).click();
    assert.equal((await reader.request.get('/api/v1/knowledge/notes/' + note)).status(), 404);
    await reader.goto('/knowledge?tab=queue');
    await reader.getByText('Доступ к очереди ограничен').waitFor();
    assert.deepEqual(deniedCalls, []);
    await api(admin, '/records/' + row.id, 'PATCH', {
      expected_version: row.version,
      values: { quantity: row.doc.quantity + 1 },
    });
    await admin.getByRole('link', { name: 'Очередь проверки', exact: true }).click();
    await admin.getByText('Запись-источник изменена').waitFor();
    await admin.getByRole('link', { name: 'Осмотр уплотнителя' }).click();
    await admin
      .getByText('Версия ' + row.version + ' → ' + (row.version + 1), { exact: true })
      .waitFor();
    await admin.getByRole('button', { name: 'Проверено, верно' }).click();
    await admin.getByText('Подтверждена · Станция').waitFor();
    await admin.getByRole('link', { name: 'Словарь станции', exact: true }).click();
    await admin.getByRole('button', { name: 'Добавить термин' }).click();
    await admin.getByLabel('Термин', { exact: true }).fill('ПЭН-1');
    await admin.getByLabel('Значение', { exact: true }).fill('Первый питательный насос');
    await admin.getByLabel('Найти ККС для термина').fill(pump.code);
    await admin.getByRole('button', { name: new RegExp(pump.code) }).click();
    await admin.getByRole('button', { name: 'Сохранить термин' }).click();
    await admin.getByRole('heading', { name: 'ПЭН-1', exact: true }).waitFor();
    const glossary = await find(reader, 'ПЭН-1');
    await glossary.getByRole('link', { name: /90LAC10AP995.*По словарю: ПЭН-1/ }).click();
    await reader.getByRole('heading', { name: /Карточка ККС.*90LAC10AP995/ }).waitFor();
    await reader.getByRole('link', { name: 'Предложить заметку' }).click();
    await fillNote(reader, 'Осмотр ККС');
    await reader.getByText('Предложена · 90LAC10AP995').waitFor();
    await admin.getByRole('button', { name: 'Добавить термин' }).click();
    await admin.getByLabel('Термин', { exact: true }).fill('пэн-1');
    await admin.getByLabel('Значение', { exact: true }).fill('Дубль');
    await admin.getByRole('button', { name: 'Сохранить термин' }).click();
    await admin
      .getByRole('alert')
      .filter({ hasText: /уже есть/ })
      .waitFor();
    assert.equal(
      await admin.getByLabel('Термин', { exact: true }).getAttribute('aria-invalid'),
      'true',
    );
    await admin.getByRole('button', { name: 'Отмена', exact: true }).click();
    await admin.getByRole('button', { name: 'Изменить термин' }).click();
    await admin.getByLabel('Значение', { exact: true }).fill('Насос первой группы');
    await admin.getByRole('button', { name: 'Сохранить термин' }).click();
    await admin.getByText('Насос первой группы', { exact: true }).waitFor();
    await reader.goto('/knowledge?tab=glossary');
    await reader.getByText('Насос первой группы', { exact: true }).waitFor();
    assert.equal(await reader.getByRole('button', { name: 'Добавить термин' }).count(), 0);
    await admin.getByRole('button', { name: 'Удалить термин' }).click();
    await confirmAccessAction(admin, 'Удалить');
    await admin.getByText('Термины не найдены').waitFor();
    assert.deepEqual(deniedCalls, []);
  },
);

test(
  'C-403 · предложение со страницы документа и возврат к источнику',
  { timeout: 100000 },
  async (t) => {
    const { page, config } = await openAccessPage(t);
    page.setDefaultTimeout(12000);
    await api(page, '/auth/login', 'POST', { login: config.login, password: config.password });
    const response = await page.request.post('/api/v1/documents?name=pump.txt', {
      headers: { 'Content-Type': 'application/octet-stream' },
      data: Buffer.from('Обложка\fЗаметки по насосу'),
    });
    assert.equal(response.status(), 201);
    const doc = await response.json(),
      path = '/documents?document=' + doc.id + '&page=2';
    await page.goto(path);
    await page.getByRole('link', { name: 'Предложить заметку' }).click();
    await page.getByRole('link', { name: /pump · стр. 2/ }).waitFor();
    await fillNote(page, 'Проверка по листу');
    await page.getByRole('button', { name: 'Отклонить', exact: true }).click();
    await page.getByLabel('Причина отклонения').fill('Уточните источник');
    await page.getByRole('button', { name: 'Отклонить с причиной' }).click();
    await page.getByText('Причина отклонения · Уточните источник').waitFor();
    await page.getByRole('button', { name: 'Исправить' }).click();
    await page.getByLabel('Текст заметки').fill('Сверено с листом 2');
    await page.getByRole('button', { name: 'Отправить на проверку' }).click();
    await page.getByRole('button', { name: 'Подтвердить', exact: true }).click();
    await page.getByText('Подтверждена · Станция').waitFor();
    await page.getByRole('link', { name: /pump · стр. 2/ }).click();
    await page.waitForURL((url) => url.pathname + url.search === path);
    // На сборке с C-402 проверяем также отрисовку исходной страницы.
    if (process.env.LOCALAI_TEST_DOCUMENT_CARD === '1') {
      await page.getByRole('heading', { name: 'Страница 2', exact: true }).waitFor();
      await page.getByText('Заметки по насосу', { exact: true }).waitFor();
    }
  },
);
