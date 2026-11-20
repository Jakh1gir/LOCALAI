import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { Browser, Page } from 'playwright-core';
import { text } from '../../src/text.ts';
import type { Dataset, RecordRow } from '../../src/api/types.ts';
import { openAccessPage } from './server.ts';

const copy = text.find;
const markers = {
  hidden: `закрытоеполе${randomBytes(5).toString('hex')}`,
  denied: `закрытыйреестр${randomBytes(5).toString('hex')}`,
};

async function login(page: Page, login: string, password: string) {
  await page.getByLabel(text.loginField, { exact: true }).fill(login);
  await page.getByLabel(text.passwordField, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.signIn, exact: true }).click();
}

async function openSearch(page: Page) {
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog', { name: text.search });
  await dialog.waitFor();
  return dialog;
}

async function search(page: Page, query: string) {
  const dialog = page.getByRole('dialog', { name: text.search });
  await dialog.getByRole('combobox').fill(query);
  await page.waitForFunction(
    (query) =>
      document.querySelector('[data-result-query]')?.getAttribute('data-result-query') === query,
    query,
  );
  return dialog;
}

async function screenshot(page: Page, name: string) {
  if (!process.env.LOCALAI_SCREENSHOTS) return;
  await mkdir(process.env.LOCALAI_SCREENSHOTS, { recursive: true });
  await page.screenshot({
    path: join(process.env.LOCALAI_SCREENSHOTS, `${name}.webp`),
    type: 'webp',
    fullPage: false,
  });
}

async function firstRecord(page: Page, dataset: Dataset) {
  const result = await page.request.post(`/api/v1/datasets/${dataset.id}/records/query`, {
    data: { limit: 1 },
  });
  assert.equal(result.status(), 200);
  return (await result.json()).items[0] as RecordRow;
}

async function setNote(page: Page, row: RecordRow, note: string) {
  const response = await page.request.patch(`/api/v1/records/${row.id}`, {
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
    data: { expected_version: row.version, values: { note } },
  });
  assert.equal(response.status(), 200, await response.text());
}

async function restrictedPage(browser: Browser, origin: string, admin: Page, dataset: Dataset) {
  const fields = (await (await admin.request.get(`/api/v1/types/${dataset.type_id}`)).json())
    .fields;
  const field = fields.find((item: { key: string }) => item.key === 'note');
  const create = (path: string, data: unknown, method = 'POST') =>
    admin.request.fetch(path, {
      method,
      data,
      headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
    });
  const role = await (
    await create('/api/v1/roles', { name: `Поиск ${randomBytes(4).toString('hex')}` })
  ).json();
  const granted = await create(
    `/api/v1/roles/${role.id}/permissions`,
    {
      permissions: [
        { scope: 'dataset', target: dataset.id, action: 'view' },
        { scope: 'field', target: field.id, action: 'hidden' },
      ],
    },
    'PUT',
  );
  assert.equal(granted.status(), 200);
  const created = await create('/api/v1/users', {
    login: `search-${randomBytes(4).toString('hex')}`,
    name: 'Инженер поиска',
    role_id: role.id,
  });
  assert.equal(created.status(), 201);
  const user = await created.json();
  const context = await browser.newContext({ baseURL: origin });
  const page = await context.newPage();
  await page.goto('/registries');
  await login(page, user.login, user.temporary_password);
  await page.getByRole('heading', { name: text.changeTitle }).waitFor();
  const password = randomBytes(24).toString('base64url');
  await page.getByLabel(text.currentPassword, { exact: true }).fill(user.temporary_password);
  await page.getByLabel(text.newPassword, { exact: true }).fill(password);
  await page.getByLabel(text.repeatPassword, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.savePassword }).click();
  await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
  return { page, context };
}

async function staleResponses(page: Page) {
  await page.clock.install();
  await page.clock.pauseAt(new Date(Date.now() + 1000));
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const seen: string[] = [];
  let finished!: () => void;
  const completed = new Promise<void>((resolve) => {
    finished = resolve;
  });
  await page.route('**/api/v1/search?*', async (route) => {
    const query = new URL(route.request().url()).searchParams.get('q')!;
    seen.push(query);
    if (query === 'старый') await held;
    try {
      await route.fulfill({
        json: { query, kks: [], records: [], more: { records: false }, took_ms: 1 },
      });
    } finally {
      if (query === 'старый') finished();
    }
  });
  try {
    const dialog = await openSearch(page);
    const input = dialog.getByRole('combobox');
    await input.fill('старый');
    await page.clock.runFor(199);
    assert.deepEqual(seen, []);
    const oldRequest = page.waitForRequest(
      (request) => new URL(request.url()).searchParams.get('q') === 'старый',
    );
    await page.clock.runFor(1);
    await oldRequest;
    await input.fill('новый');
    await page.clock.runFor(200);
    await page.waitForFunction(
      () =>
        document.querySelector('[data-result-query]')?.getAttribute('data-result-query') ===
        'новый',
    );
    release();
    await completed;
    assert.equal(
      await dialog.locator('[data-result-query]').getAttribute('data-result-query'),
      'новый',
    );
    assert.deepEqual(seen, ['старый', 'новый']);
    await page.keyboard.press('Escape');
  } finally {
    release();
    await page.unroute('**/api/v1/search?*');
    await page.clock.resume();
  }
}

async function measure(page: Page, query: string): Promise<number> {
  await page.evaluate(() => {
    const input = document.querySelector<HTMLInputElement>('dialog input')!;
    const box = document.querySelector('[role="listbox"]')!;
    (window as any).searchMeasurement = null;
    let started = 0;
    const listener = () => {
      started = performance.now();
    };
    input.addEventListener('input', listener);
    const observer = new MutationObserver(() => {
      if (!started || box.getAttribute('data-result-query') !== input.value) return;
      observer.disconnect();
      input.removeEventListener('input', listener);
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          (window as any).searchMeasurement = performance.now() - started;
        }),
      );
    });
    observer.observe(box, { subtree: true, childList: true, attributes: true });
  });
  await page.getByRole('dialog', { name: text.search }).getByRole('combobox').fill(query);
  await page.waitForFunction(() => (window as any).searchMeasurement !== null);
  return page.evaluate(() => (window as any).searchMeasurement);
}

type SearchStand = {
  page: Page;
  browser: Browser;
  origin: string;
  operating: Dataset;
  row: RecordRow;
  t: TestContext;
  limitedMeasurements: { role: string; query: string; ms: number; server_ms?: number }[];
};

async function keyboardFlow(stand: SearchStand) {
  const { page } = stand;

  const trigger = page.getByRole('button', { name: text.search, exact: true });
  await trigger.click();
  await page.getByRole('dialog', { name: text.search }).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await trigger.evaluate((element) => element === document.activeElement), true);
  const dialog = await openSearch(page);
  await dialog.getByRole('combobox').press('ArrowDown');
  await dialog.getByRole('combobox').press('ArrowUp');
  await dialog.getByRole('combobox').fill('проверки');
  await dialog.getByRole('combobox').press('Enter');
  await page.getByRole('heading', { name: text.checks, level: 1 }).waitFor();
}

async function signalFlow(stand: SearchStand) {
  const { page } = stand;

  await openSearch(page);
  const dialog = await search(page, '10LAC10AP001XQ01');
  await dialog.getByText(copy.signal('XQ01'), { exact: true }).waitFor();
  const link = dialog.getByRole('group', { name: copy.kks, exact: true }).getByRole('link').first();
  assert.match(await link.innerText(), /^10LAC10AP001/);
  await screenshot(page, 'search-signal');
  await dialog.getByRole('combobox').press('Enter');
  await page.getByRole('heading', { name: `${copy.card} · 10LAC10AP001`, exact: true }).waitFor();
  assert.ok(new URL(page.url()).searchParams.get('object'));
  await page.reload();
  await page.getByRole('heading', { name: `${copy.card} · 10LAC10AP001`, exact: true }).waitFor();
}

async function aliasFlow(stand: SearchStand) {
  const { page, operating } = stand;

  const resolved = await page.request.get(
    '/api/v1/sites/' + operating.site_id + '/kks/resolve?code=10LAC10AP001',
  );
  const object = await resolved.json();
  const alias = '10LAC10AP001-ALT';
  const added = await page.request.post(`/api/v1/kks/${object.id}/aliases`, {
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
    data: { alias },
  });
  assert.equal(added.status(), 201);
  await openSearch(page);
  const dialog = await search(page, alias);
  await dialog.getByText(copy.alias, { exact: true }).waitFor();
  await dialog.getByRole('combobox').press('Enter');
  await page.getByRole('heading', { name: `${copy.card} · 10LAC10AP001`, exact: true }).waitFor();
}

async function recordFlow(stand: SearchStand) {
  const { page, operating, row } = stand;

  await openSearch(page);
  const dialog = await search(page, markers.hidden);
  await dialog
    .getByText(`В эксплуатации · Примечание: ${markers.hidden}`, { exact: true })
    .waitFor();
  await dialog.getByRole('combobox').press('Enter');
  await page
    .getByRole('heading', { name: `${copy.inspector} · В эксплуатации`, exact: true })
    .waitFor();
  assert.equal(new URL(page.url()).searchParams.get('record'), row.id);
  assert.equal(new URL(page.url()).searchParams.get('dataset'), operating.id);
  await page
    .getByRole('table', { name: copy.inspector })
    .getByText(markers.hidden, { exact: true })
    .waitFor();
  await page.reload();
  await page
    .getByRole('table', { name: copy.inspector })
    .getByText(markers.hidden, { exact: true })
    .waitFor();
}

async function limitFlow(stand: SearchStand) {
  const { page } = stand;

  await openSearch(page);
  const dialog = await search(page, 'насос');
  const count = await dialog
    .getByRole('group', { name: copy.records, exact: true })
    .getByRole('option')
    .count();
  await dialog.getByText(copy.more(count), { exact: true }).waitFor();
  await screenshot(page, 'search-results');
  await page.keyboard.press('Escape');
}

async function permissionsFlow(stand: SearchStand) {
  const { page, browser, origin, operating, t, limitedMeasurements } = stand;

  const restricted = await restrictedPage(browser, origin, page, operating);
  try {
    for (const query of [markers.hidden, markers.denied]) {
      await openSearch(restricted.page);
      let dialog;
      if (process.env.LOCALAI_SEARCH_PERF === '1') {
        const response = restricted.page.waitForResponse(
          (reply) =>
            new URL(reply.url()).pathname === '/api/v1/search' &&
            new URL(reply.url()).searchParams.get('q') === query,
        );
        const ms = await measure(restricted.page, query);
        const serverMs = (await (await response).json()).took_ms;
        limitedMeasurements.push({ role: 'field-limited', query, ms, server_ms: serverMs });
        t.diagnostic(
          JSON.stringify({
            role: 'field-limited',
            query,
            input_to_results_ms: ms,
            server_ms: serverMs,
          }),
        );
        dialog = restricted.page.getByRole('dialog', { name: text.search });
      } else dialog = await search(restricted.page, query);
      assert.equal(await dialog.getByRole('group', { name: copy.records, exact: true }).count(), 0);
      await dialog.getByRole('status').filter({ hasText: copy.empty }).waitFor();
      await restricted.page.keyboard.press('Escape');
    }
  } finally {
    await restricted.context.close();
  }
}

async function cancellationFlow(stand: SearchStand) {
  const { page, browser, origin } = stand;

  const context = await browser.newContext({
    baseURL: origin,
    storageState: await page.context().storageState(),
  });
  try {
    const isolated = await context.newPage();
    await isolated.goto('/registries');
    await isolated.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
    await staleResponses(isolated);
  } finally {
    await context.close();
  }
}

async function performanceFlow(stand: SearchStand) {
  const { page, t, limitedMeasurements } = stand;

  await openSearch(page);
  const queries = ['насос', 'Siemens', '10LAC10AP001XQ01', markers.hidden, 'ABB'];
  const measurements = [...limitedMeasurements];
  for (const query of queries)
    measurements.push({ role: 'admin', query, ms: await measure(page, query) });
  t.diagnostic(JSON.stringify({ records: 10000, measurements }));
  if (process.env.LOCALAI_SCREENSHOTS)
    await writeFile(
      join(process.env.LOCALAI_SCREENSHOTS, 'search-timings.json'),
      JSON.stringify(measurements, null, 2),
    );
  for (const sample of measurements)
    assert.ok(sample.ms <= 300, `${sample.role}, ${sample.query}: ${sample.ms.toFixed(1)} мс`);
  await page.keyboard.press('Escape');
}
test(
  'C-304 — общий поиск на настоящем сервере, 10 000 записей',
  { timeout: 120_000 },
  async (t) => {
    const { page, browser, origin, config } = await openAccessPage(t);
    await page.goto('/registries');
    await login(page, config.login, config.password);
    await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
    const datasets = (await (await page.request.get('/api/v1/datasets')).json()).items as Dataset[];
    const operating = datasets.find((item) => item.name === 'В эксплуатации')!;
    const warehouse = datasets.find((item) => item.name === 'Склад')!;
    const row = await firstRecord(page, operating);
    const closed = await firstRecord(page, warehouse);
    await setNote(page, row, markers.hidden);
    await setNote(page, closed, markers.denied);
    const errors: string[] = [];
    const limitedMeasurements: { role: string; query: string; ms: number; server_ms?: number }[] =
      [];
    page.on('pageerror', (error) => errors.push(error.message));
    const stand: SearchStand = { page, browser, origin, operating, row, t, limitedMeasurements };
    await t.test('Esc возвращает фокус, стрелки и Enter открывают раздел', () => keyboardFlow(stand));
    await t.test('код сигнала первым результатом и Enter открывает аппарат', () =>
      signalFlow(stand),
    );
    await t.test('псевдоним помечен и открывает тот же аппарат', () => aliasFlow(stand));
    await t.test('найденное поле, реестр и инспектор записи по прямой ссылке', () =>
      recordFlow(stand),
    );
    await t.test('more.records показывает предел выдачи', () => limitFlow(stand));
    await t.test('закрытый реестр и скрытое поле не находятся', () => permissionsFlow(stand));
    await t.test('200 мс без раннего запроса, устаревший ответ не заменяет новый', () =>
      cancellationFlow(stand),
    );
    await t.test(
      'PERF-07: от последнего знака до результатов до 300 мс',
      { skip: process.env.LOCALAI_SEARCH_PERF !== '1' },
      () => performanceFlow(stand),
    );
    assert.deepEqual(errors, []);
  },
);
