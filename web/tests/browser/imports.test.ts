import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { mkdir } from 'node:fs/promises';
import { test } from 'node:test';
import type { Page } from 'playwright-core';
import { openAccessPage } from './server.ts';
import { actions } from '../../src/imports/model.ts';
import type { RecordPage, RecordRow } from '../../src/api/types.ts';

const fixture = fileURLToPath(new URL('../../../tests/e2e/import_workbook.py', import.meta.url));
function workbook(spec: Record<string, unknown>) {
  return execFileSync(process.env.LOCALAI_TEST_PYTHON ?? 'python', [fixture], {
    input: JSON.stringify(spec),
  });
}
async function call(p: Page, path: string, method = 'GET', data?: unknown) {
  const r = await p.request.fetch('/api/v1' + path, {
    method,
    data,
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.ok(r.ok(), path + ' ' + (r.ok() ? r.status() : await r.text()));
  return r.json();
}
async function plant(p: Page) {
  const site = (await call(p, '/sites')).items[0].id;
  const type = await call(p, '/types', 'POST', { name: 'Параметры привода' });
  for (const spec of [
    { name: 'Тег', key: 'tag', kind: 'text', unique: true },
    { name: 'Мощность', key: 'power', kind: 'number' },
    { name: 'ККС', key: 'kks', kind: 'kks', unique: true, kks_role: 'object' },
  ]) {
    await call(p, '/types/' + type.id + '/fields', 'POST', spec);
  }
  const d = await call(p, '/datasets', 'POST', {
    name: 'Приводы',
    site_id: site,
    type_id: type.id,
  });
  await call(p, '/datasets/' + d.id + '/records', 'POST', { values: { tag: 'М1', power: 10 } });
  const second = await call(p, '/datasets/' + d.id + '/records', 'POST', {
    values: { tag: 'М2', power: 20 },
  });
  return { id: d.id as string, second: second as RecordRow };
}
async function upload(p: Page, buffer: Buffer) {
  await p
    .getByLabel('Файл .xlsx')
    .setInputFiles({ name: 'Ведомость.xlsx', mimeType: 'application/octet-stream', buffer });
  await p.getByRole('button', { name: 'К сопоставлению' }).waitFor();
}
const button = (p: Page, name: string) => p.getByRole('button', { name, exact: true });
async function plan(p: Page) {
  await button(p, 'Построить план').click();
  await p.getByRole('heading', { name: 'Проверка плана' }).waitFor();
}
async function apply(p: Page, profile?: string) {
  await button(p, 'К применению').click();
  if (profile) await p.getByLabel('Сохранить профиль под именем').fill(profile);
  await button(p, 'Применить импорт').click();
  await p.getByRole('heading', { name: 'Импорт завершён' }).waitFor();
}
async function counts(p: Page, values: number[]) {
  const text = await p.locator('.import-totals dd').allTextContents();
  assert.deepEqual(text, values.map(String), Object.values(actions).join(', '));
}
async function capture(p: Page, name: string) {
  if (!process.env.LOCALAI_SCREENSHOTS) return;
  await mkdir(process.env.LOCALAI_SCREENSHOTS, { recursive: true });
  await p.screenshot({ path: join(process.env.LOCALAI_SCREENSHOTS, name + '.webp'), type: 'webp' });
}
async function records(p: Page, id: string) {
  const page: RecordPage = await call(p, '/datasets/' + id + '/records/query', 'POST', {
    limit: 50,
  });
  return page.items;
}
async function recordsFlow(p: Page, other: Page, id: string, second: RecordRow) {
  await p.goto('/registries');
  await p.getByLabel('Реестр', { exact: true }).selectOption(id);
  await button(p, 'Импорт из Excel').click();
  const book = workbook({
    rows: [
      ['Паспорт приводов'],
      [],
      ['Тег', 'Мощность', 'ККС'],
      ['М1', 10],
      ['М2', 22],
      ['М3', 30],
      ['М1', 99],
      ['Ошибка', 'текст'],
    ],
  });
  await upload(p, book);
  assert.ok(await p.getByRole('radio', { name: 'Строка 3', exact: true }).isChecked());
  await p.getByRole('radio', { name: 'Строка 1', exact: true }).check();
  await p.getByRole('radio', { name: 'Строка 3', exact: true }).check();
  await button(p, 'К сопоставлению').click();
  assert.equal(await p.getByLabel('Целевой реестр').inputValue(), id);
  const key = p.getByLabel('Ключ записи');
  assert.deepEqual(await key.locator('option').allTextContents(), [
    'Без ключа · UUID или добавление',
    'Тег',
  ]);
  await key.selectOption('tag');
  await plan(p);
  await counts(p, [1, 1, 1, 1, 1]);
  await p.getByLabel('Действие', { exact: true }).selectOption('update');
  await p.getByRole('cell', { name: /20 → 22/ }).waitFor();
  await p.getByLabel('Действие', { exact: true }).selectOption('conflict');
  await p.getByLabel('Решение · 7').waitFor();
  assert.equal(await p.getByLabel('Решение · 7').locator('option').count(), 1);
  await p.getByLabel('Действие', { exact: true }).selectOption('');
  await p.getByText('key_duplicate', { exact: true }).waitFor();
  await capture(p, 'import-review');
  await call(other, '/records/' + second.id, 'PATCH', {
    expected_version: second.version,
    values: { power: 21 },
  });
  await button(p, 'К применению').click();
  await button(p, 'Применить импорт').click();
  await button(p, 'Перестроить план').waitFor();
  assert.equal((await records(p, id)).length, 2);
  await button(p, 'Перестроить план').click();
  await p.getByRole('cell', { name: /21 → 22/ }).waitFor();
  await apply(p, 'Профиль приводов');
  assert.deepEqual((await records(p, id)).map((r) => r.doc.power).sort(), [10, 22, 30]);
  await p.getByRole('heading', { name: 'Пропущенные строки' }).waitFor();
  await p.getByRole('link', { name: 'Приводы', exact: true }).click();
  await p.getByRole('heading', { name: 'Приводы', exact: true }).waitFor();
  return book;
}
async function profileFlow(p: Page, book: Buffer, id: string) {
  await p.goto('/imports');
  await upload(p, book);
  await button(p, 'К сопоставлению').click();
  await button(p, 'Использовать профиль «Профиль приводов»').click();
  await p.waitForFunction(
    (want) =>
      document.querySelector<HTMLSelectElement>('select[aria-label="Поле · Тег"]')?.value ===
        'tag' && Array.from(document.querySelectorAll('select')).some((s) => s.value === want),
    id,
  );
  assert.equal(await p.getByLabel('Ключ записи').inputValue(), 'tag');
  await plan(p);
  await counts(p, [0, 0, 3, 1, 1]);
  await apply(p);
  await button(p, 'Профили').click();
  const row = p.getByRole('row').filter({ hasText: 'Профиль приводов' });
  await row.getByRole('button', { name: 'Удалить' }).click();
  await p.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await row.waitFor({ state: 'detached' });
}
async function templateFlow(p: Page, other: Page, id: string) {
  const first = (await records(p, id)).find((r) => r.doc.tag === 'М1')!;
  const result = await p.request.post('/api/v1/datasets/' + id + '/export', {
    data: {
      format: 'xlsx',
      columns: ['power'],
      query: { where: [{ field: '$id', op: 'eq', value: first.id }] },
    },
  });
  assert.equal(result.status(), 200);
  const file = workbook({
    template: (await result.body()).toString('base64'),
    cell: 'C2',
    value: 17,
  });
  await call(other, '/records/' + first.id, 'PATCH', {
    expected_version: first.version,
    values: { power: 14 },
  });
  await p.goto('/imports?dataset=' + id);
  await upload(p, file);
  await button(p, 'К сопоставлению').click();
  assert.equal(await p.getByLabel('Поле · UUID').inputValue(), '@id');
  assert.equal(await p.getByLabel('Поле · Версия').inputValue(), '@version');
  await plan(p);
  await counts(p, [0, 0, 0, 1, 0]);
  await p.getByLabel('Решение · 2').selectOption('apply');
  await apply(p);
  await counts(p, [0, 1, 0, 0, 0]);
  assert.equal((await call(p, '/records/' + first.id)).record.doc.power, 17);
}
async function failedApply(p: Page, other: Page, id: string) {
  await p.goto('/imports?dataset=' + id);
  await upload(
    p,
    workbook({
      rows: [
        ['Тег', 'Мощность'],
        ['Резерв', 1],
        ['Занят', 2],
      ],
    }),
  );
  await button(p, 'К сопоставлению').click();
  await plan(p);
  await counts(p, [2, 0, 0, 0, 0]);
  await call(other, '/datasets/' + id + '/records', 'POST', { values: { tag: 'Занят', power: 9 } });
  await button(p, 'К применению').click();
  await button(p, 'Применить импорт').click();
  await p
    .getByRole('alert')
    .filter({ hasText: /Строка 3.*Ничего не записано/ })
    .waitFor();
  const rows = await records(p, id);
  assert.ok(!rows.some((r) => r.doc.tag === 'Резерв'));
  assert.equal(rows.find((r) => r.doc.tag === 'Занят')?.doc.power, 9);
}
async function newType(p: Page) {
  await p.goto('/imports');
  await upload(
    p,
    workbook({
      rows: [
        ['Обозначение', 'Ду'],
        ['З1', 50],
        ['З2', 80],
      ],
    }),
  );
  await button(p, 'К сопоставлению').click();
  await p.getByLabel('Режим', { exact: true }).selectOption('new_type');
  await p.getByLabel('Название типа').fill('Арматура');
  await p.getByLabel('Название реестра').fill('Задвижки');
  await p.getByLabel('Ключ · Обозначение').check();
  await p.getByLabel('Вид · Ду').selectOption('integer');
  await p.getByLabel('Название · Ду').fill('Диаметр');
  await plan(p);
  await counts(p, [2, 0, 0, 0, 0]);
  assert.ok(!(await call(p, '/types')).items.some((v: { name: string }) => v.name === 'Арматура'));
  await apply(p);
  await p.getByRole('link', { name: 'Задвижки', exact: true }).click();
  await p.getByText('Записей · 2').waitFor();
}
async function magnet(p: Page) {
  const file = workbook({
    rows: [
      ['ККС', 'ККС сигнала', 'Шкаф ПТК', '№ кабеля', 'Клеммник ПТК', 'Клемма ПТК 1'],
      ['10ABC01CP001', '10ABC01CP001XQ01', '00ZZA90GH001', 'К-25', 'X25', '11'],
      [null, '10ABC02AA101XB02', '00ZZA90GH001', 'К-25', 'X25', '12'],
    ],
    merge: ['A2:A3'],
  });
  for (const first of [true, false]) {
    await p.goto('/imports');
    await upload(p, file);
    await button(p, 'К сопоставлению').click();
    assert.equal(await p.getByLabel('Режим', { exact: true }).inputValue(), 'magnet');
    assert.equal(await p.getByLabel('Роль · Клеммник ПТК').inputValue(), 'contact:Шкаф ПТК');
    await p.getByLabel('Имя раскладки').fill('Каналы');
    await p.getByText('Целевые реестры · Каналы — сигналы, Каналы — кабели').waitFor();
    await plan(p);
    await counts(p, first ? [3, 0, 0, 0, 0] : [0, 0, 3, 0, 0]);
    await p.getByText(/Строка 3 · Код сигнала/).waitFor();
    await apply(p);
    await p.getByRole('link', { name: 'Каналы — сигналы' }).waitFor();
    await p.getByRole('link', { name: 'Каналы — кабели' }).waitFor();
  }
}
test('C-302 · мастер импорта на базе C-101', { timeout: 150_000 }, async (t) => {
  const { page, origin, browser, config } = await openAccessPage(t);
  page.setDefaultTimeout(6500);
  await call(page, '/auth/login', 'POST', { login: config.login, password: config.password });
  const other = await page.context().newPage();
  const { id, second } = await plant(page);
  await page.goto('/imports');
  await page.getByLabel('Файл .xlsx').setInputFiles({
    name: 'ошибка.xlsx',
    mimeType: 'application/octet-stream',
    buffer: Buffer.from('abc'),
  });
  await page.getByRole('alert').filter({ hasText: 'не является книгой' }).waitFor();
  await page.getByLabel('Файл .xlsx').setInputFiles({
    name: 'Широкий.xlsx',
    mimeType: 'application/octet-stream',
    buffer: workbook({ rows: [Array.from({ length: 201 }, (_, index) => 'Столбец ' + index)] }),
  });
  await page.getByRole('alert').filter({ hasText: 'больше 200 столбцов' }).waitFor();
  const book = await recordsFlow(page, other, id, second);
  await profileFlow(page, book, id);
  await templateFlow(page, other, id);
  await failedApply(page, other, id);
  await newType(page);
  await magnet(page);
  const role = await call(page, '/roles', 'POST', { name: 'Просмотр реестра' });
  await call(page, '/roles/' + role.id + '/permissions', 'PUT', {
    permissions: [{ scope: 'dataset', target: id, action: 'view' }],
  });
  const user = await call(page, '/users', 'POST', {
    login: 'viewer',
    name: 'Читатель',
    role_id: role.id,
  });
  const ctx = await browser.newContext({ baseURL: origin });
  const viewer = await ctx.newPage();
  await call(viewer, '/auth/login', 'POST', { login: 'viewer', password: user.temporary_password });
  await call(viewer, '/auth/password', 'POST', {
    current_password: user.temporary_password,
    new_password: randomUUID(),
  });
  const requests: string[] = [];
  viewer.on('request', (r) => {
    if (/\/api\/v1\/import/.test(r.url())) requests.push(r.url());
  });
  await viewer.goto('/imports');
  await viewer.getByText('Доступ к разделу не разрешён вашей роли.').waitFor();
  assert.equal(
    await viewer.getByRole('navigation').getByRole('link', { name: 'Импорт', exact: true }).count(),
    0,
  );
  assert.deepEqual(requests, []);
  await ctx.close();
});
