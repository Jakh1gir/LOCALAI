import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { test } from 'node:test';
import type { Page } from 'playwright-core';
import type { Dataset, Permission, RecordPage } from '../../src/api/types.ts';
import { recover, conflict, oldPagesAndRate } from './chat-history.ts';
import { openAccessPage } from './server.ts';

async function call(page: Page, path: string, method = 'GET', data?: unknown) {
  const response = await page.request.fetch('/api/v1' + path, {
    method,
    data,
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.ok(
    response.ok(),
    method + ' ' + path + ' ' + response.status() + (response.ok() ? '' : await response.text()),
  );
  return response.json();
}

async function provision(admin: Page, other: Page, dataset: string) {
  const role = await call(admin, '/roles', 'POST', { name: 'Дежурная смена' });
  const rights: Permission[] = [
    { scope: 'section', target: 'chat', action: 'use' },
    { scope: 'section', target: 'chat_write', action: 'use' },
    { scope: 'dataset', target: dataset, action: 'view' },
  ];
  await call(admin, '/roles/' + role.id + '/permissions', 'PUT', { permissions: rights });
  const user = await call(admin, '/users', 'POST', {
    login: 'operator',
    name: 'Оператор смены',
    role_id: role.id,
  });
  await call(other, '/auth/login', 'POST', {
    login: 'operator',
    password: user.temporary_password,
  });
  await call(other, '/auth/password', 'POST', {
    current_password: user.temporary_password,
    new_password: randomUUID(),
  });
  return { role: role.id as string, rights };
}

const feed = (page: Page) => page.getByRole('region', { name: 'Лента сообщений' });
const article = (page: Page, text: string) =>
  feed(page).getByRole('article').filter({ hasText: text });
async function write(page: Page, message: string) {
  const input = page.getByLabel('Новое сообщение');
  await input.fill(message);
  await input.press('Enter');
}

async function linksAndDelivery(admin: Page, operator: Page, hidden: Dataset) {
  const records: RecordPage = await call(
    admin,
    '/datasets/' + hidden.id + '/records/query',
    'POST',
    { limit: 1 },
  );
  const id = records.items[0].id;
  await admin.goto('/registries?' + new URLSearchParams({ dataset: hidden.id, record: id }));
  await admin.getByRole('button', { name: 'Поделиться в чат' }).click();
  const input = admin.getByLabel('Новое сообщение');
  assert.equal(await input.inputValue(), '[[record:' + id + ']]');
  await input.press('End');
  await input.press('Shift+Enter');
  await input.pressSequentially('10LAC10AP001');
  await input.fill('🛠 ' + (await input.inputValue()));
  const start = performance.now();
  await input.press('Enter');
  const received = article(operator, '10LAC10AP001');
  await received.getByText('[нет доступа]', { exact: true }).waitFor({ timeout: 1000 });
  const elapsed = performance.now() - start;
  assert.ok(elapsed <= 1000, 'Доставка заняла ' + elapsed.toFixed(1) + ' мс');
  assert.equal(await received.getByRole('link').count(), 1);
  await received.getByRole('link', { name: '10LAC10AP001' }).click();
  await operator.getByRole('heading', { name: /Карточка ККС.*10LAC10AP001/ }).waitFor();
  await article(admin, '10LAC10AP001').getByRole('link', { name: hidden.name }).click();
  await admin.getByRole('heading', { name: 'Инспектор записи · ' + hidden.name }).waitFor();
  return elapsed;
}

async function moderation(admin: Page, operator: Page) {
  await operator
    .getByRole('navigation')
    .getByRole('link', { name: /Общий чат/ })
    .click();
  await write(operator, 'Смена принята');
  const badge = admin.getByRole('navigation').getByLabel('Непрочитано: 1');
  await badge.waitFor();
  await admin
    .getByRole('navigation')
    .getByRole('link', { name: /Общий чат/ })
    .click();
  await badge.waitFor({ state: 'hidden' });
  await article(operator, 'Смена принята').getByRole('button', { name: 'Изменить' }).click();
  await operator.getByLabel('Текст сообщения').fill('Смена передана');
  await operator.getByRole('button', { name: 'Сохранить' }).click();
  const changed = article(admin, 'Смена передана');
  await changed.getByText('изменено', { exact: true }).waitFor();
  const mine = article(operator, 'Смена передана');
  await mine.getByRole('button', { name: 'История' }).click();
  const history = mine.getByRole('region', { name: 'История сообщения' });
  await history.getByText('Смена принята', { exact: true }).waitFor();
  await history.getByText('Смена передана', { exact: true }).waitFor();
  if (process.env.LOCALAI_SCREENSHOTS) {
    await mkdir(process.env.LOCALAI_SCREENSHOTS, { recursive: true });
    await operator.screenshot({
      path: join(process.env.LOCALAI_SCREENSHOTS, 'chat-history.webp'),
      type: 'webp',
    });
  }
  await mine.getByRole('button', { name: 'История' }).click();
  await admin.getByRole('searchbox', { name: 'Поиск по сообщениям' }).fill('передана');
  await admin.getByRole('button', { name: 'Найти', exact: true }).click();
  await admin.getByRole('button', { name: 'К сообщению' }).click();
  await changed.getByRole('button', { name: 'Скрыть', exact: true }).click();
  await changed.getByLabel('Причина (необязательно)').fill('Дублирует журнал');
  await changed.getByRole('button', { name: 'Скрыть сообщение' }).click();
  await feed(operator).getByText('Сообщение скрыто', { exact: true }).waitFor();
  assert.equal(await feed(operator).getByText('Смена передана', { exact: true }).count(), 0);
  await changed.getByRole('button', { name: 'Вернуть' }).click();
  await mine.getByRole('button', { name: 'Удалить' }).click();
  await operator.getByRole('dialog').getByRole('button', { name: 'Отмена' }).click();
  await mine.getByRole('button', { name: 'Удалить' }).click();
  await operator.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await feed(admin).getByText('Сообщение удалено', { exact: true }).waitFor();
}

async function liveRows(admin: Page, other: Page, dataset: Dataset) {
  await other.goto('/registries?' + new URLSearchParams({ dataset: dataset.id }));
  const filter = other.getByLabel('Фильтр записей');
  await filter.fill('СН-');
  const response = other.waitForResponse((r) => r.url().endsWith('/records/query'));
  await other.getByRole('button', { name: 'Применить' }).click();
  assert.equal((await response).status(), 200);
  const holder = other.locator('.tabulator-tableholder');
  await other.locator('.tabulator-row').first().waitFor();
  await holder.evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  const filtered: RecordPage = await call(other, '/datasets/' + dataset.id + '/records/query', 'POST', {
    search: 'СН-', sort: { field: '$created_at', dir: 'desc' }, limit: 100,
  });
  assert.equal(filtered.items.length, 100);
  const selected = other.locator('.tabulator-row').filter({ hasText: String(filtered.items[99].doc.serial) });
  await selected.click();
  const prior = await other.locator('.tabulator-selected').innerText();
  const scroll = await holder.evaluate((el) => el.scrollTop);
  assert.ok(scroll > 0);
  const previousElement = await selected.elementHandle();
  assert.ok(previousElement);
  const refreshed = other.waitForResponse((r) => r.url().endsWith('/records/query'));
  const serial = 'СН-НОВАЯ-СМЕНА';
  await call(admin, '/datasets/' + dataset.id + '/records', 'POST', {
    values: { serial, kks: ['10LAC10AP001'] },
  });
  await refreshed;
  await other.waitForFunction(el => !el.isConnected, previousElement);
  assert.equal(await filter.inputValue(), 'СН-');
  assert.equal(await holder.evaluate((el) => el.scrollTop), scroll);
  assert.equal(await other.locator('.tabulator-selected').innerText(), prior);
  await holder.evaluate((el) => {
    el.scrollTop = 0;
  });
  await other
    .locator('.tabulator-row')
    .first()
    .getByText(serial, { exact: true })
    .waitFor({ state: 'attached' });
}

test('C-301 · два пользователя, общая лента и живой реестр', { timeout: 110_000 }, async (t) => {
  const { page: admin, browser, origin, config } = await openAccessPage(t);
  const context = await browser.newContext({
    baseURL: origin,
    viewport: { width: 1440, height: 900 },
  });
  const operator = await context.newPage();
  const outside: string[] = [],
    errors: string[] = [];
  for (const page of [admin, operator]) {
    page.on('request', (req) => {
      if (new URL(req.url()).origin !== origin) outside.push(req.url());
    });
    page.on('pageerror', (e) => errors.push(e.message));
    page.setDefaultTimeout(6000);
  }
  await call(admin, '/auth/login', 'POST', { login: config.login, password: config.password });
  const datasets: Dataset[] = (await call(admin, '/datasets')).items;
  const operating = datasets.find((d) => d.name === 'В эксплуатации')!;
  const hidden = datasets.find((d) => d.name === 'Склад')!;
  const { role, rights } = await provision(admin, operator, operating.id);
  await operator.goto('/chat');
  await feed(operator).getByText('Сообщений пока нет').waitFor();
  await t.test('AT-44 · доставка, ссылки по правам и переходы', async () => {
    const elapsed = await linksAndDelivery(admin, operator, hidden);
    t.diagnostic('Enter → отображение у второго пользователя: ' + elapsed.toFixed(1) + ' мс.');
  });
  await t.test('Правка, история, непрочитанное, поиск, скрытие и удаление', async () => {
    await moderation(admin, operator);
  });
  await t.test('Догрузка после разрыва с прежнего курсора', () => recover(admin, operator));
  await t.test('Конфликт версий при правке из второй вкладки', () => conflict(admin));
  await t.test('Старая страница по результату поиска, 4000 знаков и ответ 429', () =>
    oldPagesAndRate(admin, operator),
  );
  await t.test('Новая строка при активном фильтре, сохранение прокрутки и выделения', async () => {
    await liveRows(admin, operator, operating);
  });
  await t.test('Чтение без записи и отсутствие обращений к чату при полном запрете', async () => {
    const setRights = (permissions: Permission[]) =>
      call(admin, '/roles/' + role + '/permissions', 'PUT', { permissions });
    await setRights(rights.filter((r) => r.target !== 'chat_write'));
    await operator.goto('/chat');
    await feed(operator).waitFor();
    assert.equal(await operator.getByLabel('Новое сообщение').count(), 0);
    await operator.goto('about:blank');
    await setRights([]);
    const requests: string[] = [];
    operator.on('request', (req) => {
      if (req.url().includes('/api/v1/chat/')) requests.push(req.url());
    });
    await operator.goto('/chat');
    await operator.getByText('Доступ к разделу не разрешён вашей роли.').waitFor();
    assert.equal(await operator.getByRole('link', { name: /Общий чат/ }).count(), 0);
    assert.deepEqual(requests, []);
  });
  assert.deepEqual(outside, []);
  assert.deepEqual(errors, []);
  await Promise.all([admin.close(), operator.close()]);
});
