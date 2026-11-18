import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type Page } from 'playwright-core';
import { preview } from 'vite';
import type { Backup, RecordRow } from '../../src/api/types.ts';
import { text } from '../../src/text.ts';
import { startAccessServer } from './server.ts';

const copy = text.backups;
const root = fileURLToPath(new URL('../../', import.meta.url));

async function submitLogin(page: Page, login: string, password: string) {
  await page.getByRole('heading', { name: text.loginTitle, level: 1 }).waitFor();
  await page.getByLabel(text.loginField, { exact: true }).fill(login);
  await page.getByLabel(text.passwordField, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.signIn, exact: true }).click();
}

async function signIn(page: Page, login: string, password: string) {
  await submitLogin(page, login, password);
  await page.getByRole('heading', { name: text.backupsTitle, level: 1 }).waitFor();
}

async function screenshot(page: Page, name: string) {
  if (!process.env.LOCALAI_SCREENSHOTS) return;
  await mkdir(process.env.LOCALAI_SCREENSHOTS, { recursive: true });
  await page.screenshot({
    path: join(process.env.LOCALAI_SCREENSHOTS, `${name}.webp`),
    type: 'webp',
    fullPage: true,
  });
}

async function patchRecord(page: Page, record: RecordRow, note: string) {
  const response = await page.request.patch(`/api/v1/records/${record.id}`, {
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
    data: { expected_version: record.version, values: { note } },
  });
  assert.equal(response.status(), 200, await response.text());
  return response.json() as Promise<RecordRow>;
}

async function currentRecord(page: Page, id: string) {
  return (await (await page.request.get(`/api/v1/records/${id}`)).json()).record as RecordRow;
}

async function createCopy(page: Page, password: string, timings: Record<string, number>) {
  const form = page.getByRole('form', { name: copy.create, exact: true });
  await form.getByLabel(copy.label, { exact: true }).fill('Перед правкой записи');
  await form.getByLabel(copy.password, { exact: true }).fill(password);
  await form.getByLabel(copy.repeat, { exact: true }).fill(`${password}-другой`);
  await form.getByRole('button', { name: copy.create, exact: true }).click();
  await form.getByRole('alert').filter({ hasText: copy.passwordMismatch }).waitFor();
  assert.equal((await (await page.request.get('/api/v1/backups')).json()).items.length, 0);
  await form.getByLabel(copy.repeat, { exact: true }).fill(password);
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let calls = 0;
  await page.route('**/api/v1/backups', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    calls += 1;
    const started = performance.now();
    const response = await route.fetch();
    timings.create_ms = performance.now() - started;
    await held;
    await route.fulfill({ response });
  });
  try {
    await form.getByRole('button', { name: copy.create, exact: true }).click();
    await form.getByRole('progressbar', { name: copy.creating, exact: true }).waitFor();
    assert.equal(
      await form.getByRole('button', { name: copy.create, exact: true }).isDisabled(),
      true,
    );
    await form.evaluate((element: HTMLFormElement) => element.requestSubmit());
  } finally {
    release();
  }
  await form.getByRole('status').filter({ hasText: copy.created }).waitFor();
  await page.unroute('**/api/v1/backups');
  assert.equal(calls, 1);
  assert.equal(await form.getByLabel(copy.password, { exact: true }).inputValue(), '');
  assert.equal(await form.getByLabel(copy.repeat, { exact: true }).inputValue(), '');
  const items = (await (await page.request.get('/api/v1/backups')).json()).items as Backup[];
  assert.equal(items.length, 1);
  return items[0];
}

async function downloadCopy(page: Page, backup: Backup) {
  const row = page
    .getByRole('table', { name: copy.list, exact: true })
    .getByRole('row')
    .filter({ hasText: backup.name });
  const downloaded = page.waitForEvent('download');
  await row.getByRole('link', { name: copy.download, exact: true }).click();
  const file = await downloaded;
  const path = await file.path();
  assert.ok(path);
  const data = await readFile(path);
  assert.equal(file.suggestedFilename(), backup.name);
  assert.equal(data.length, backup.size);
  const magic = Buffer.from('LOCALAI-BACKUP\n');
  assert.ok(data.subarray(0, magic.length).equals(magic));
}

async function rejectPassword(page: Page, backup: Backup, record: RecordRow) {
  const table = page.getByRole('table', { name: copy.list, exact: true });
  const row = table.getByRole('row').filter({ hasText: backup.name });
  await row.getByRole('button', { name: copy.restore, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText(copy.restoreWarning, { exact: true }).waitFor();
  const before = await page.request
    .get('/api/v1/audit?limit=100')
    .then((response) => response.json());
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(
    await page.request.get('/api/v1/audit?limit=100').then((response) => response.json()),
    before,
  );
  await row.getByRole('button', { name: copy.restore, exact: true }).click();
  await dialog
    .getByLabel(copy.password, { exact: true })
    .fill(randomBytes(24).toString('base64url'));
  const rejected = page.waitForResponse((response) => response.url().endsWith('/restore'));
  await dialog.getByRole('button', { name: copy.restore, exact: true }).click();
  const response = await rejected;
  assert.equal(response.status(), 400);
  const error = (await response.json()).error;
  assert.equal(error.code, 'backup_password');
  await dialog.getByRole('alert').filter({ hasText: error.message }).waitFor();
  assert.deepEqual(await currentRecord(page, record.id), record);
  await dialog.getByRole('button', { name: text.access.cancel, exact: true }).click();
}

async function restoreCopy(
  page: Page,
  backup: Backup,
  password: string,
  timings: Record<string, number>,
  key: string,
) {
  const row = page
    .getByRole('table', { name: copy.list, exact: true })
    .getByRole('row')
    .filter({ hasText: backup.name });
  await row.getByRole('button', { name: copy.restore, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText(copy.restoreWarning, { exact: true }).waitFor();
  await dialog.getByLabel(copy.password, { exact: true }).fill(password);
  await screenshot(page, 'backup-restore-confirmation');
  const restored = page.waitForResponse((response) => response.url().endsWith('/restore'));
  const started = performance.now();
  await dialog.getByRole('button', { name: copy.restore, exact: true }).click();
  const response = await restored;
  timings[key] = performance.now() - started;
  assert.equal(response.status(), 200);
  const result = await response.json();
  await page.getByRole('heading', { name: text.loginTitle, exact: true }).waitFor();
  await page.getByRole('status').filter({ hasText: 'База восстановлена из копии от' }).waitFor();
  assert.equal((await page.request.get('/api/v1/auth/me')).status(), 401);
  return result.protective_backup as Backup;
}

async function corruptCopies(
  page: Page,
  folder: string,
  original: Backup,
  password: string,
  expected: RecordRow,
) {
  const targetFolder = join(folder, 'backups');
  const brokenName = 'localai-broken.localai-backup';
  const truncatedName = 'localai-truncated.localai-backup';
  await writeFile(join(targetFolder, brokenName), Buffer.from('corrupt backup'));
  const valid = await readFile(join(targetFolder, original.name));
  await writeFile(join(targetFolder, truncatedName), valid.subarray(0, valid.length - 10));
  await page.getByRole('button', { name: text.access.refresh, exact: true }).click();
  const table = page.getByRole('table', { name: copy.list, exact: true });
  const invalid = table.getByRole('row').filter({ hasText: brokenName });
  await invalid.waitFor();
  assert.equal(await invalid.getByRole('link').count(), 0);
  assert.equal(await invalid.getByRole('button').count(), 0);
  assert.ok((await invalid.textContent())?.includes('Файл не является копией LOCALAI'));
  const truncated = table.getByRole('row').filter({ hasText: truncatedName });
  await truncated.getByRole('button', { name: copy.restore, exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel(copy.password, { exact: true }).fill(password);
  const rejected = page.waitForResponse((response) => response.url().endsWith('/restore'));
  await dialog.getByRole('button', { name: copy.restore, exact: true }).click();
  const response = await rejected;
  assert.equal(response.status(), 400);
  assert.equal((await response.json()).error.code, 'backup_corrupt');
  await dialog.getByRole('alert').waitFor();
  assert.deepEqual(await currentRecord(page, expected.id), expected);
  await dialog.getByRole('button', { name: text.access.cancel, exact: true }).click();
  await screenshot(page, 'backups-protective-copy');
}

async function permissions(t: TestContext, browser: Browser, origin: string, admin: Page) {
  const roles = (await (await admin.request.get('/api/v1/roles')).json()).items;
  const role = roles.find((item: { name: string }) => item.name === 'Инженер');
  const response = await admin.request.post('/api/v1/users', {
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
    data: {
      login: `observer-${randomBytes(3).toString('hex')}`,
      name: 'Инженер проверки',
      role_id: role.id,
    },
  });
  assert.equal(response.status(), 201);
  const user = await response.json();
  const context = await browser.newContext({ baseURL: origin, timezoneId: 'Asia/Tashkent' });
  t.after(() => context.close());
  const page = await context.newPage();
  let backupReads = 0;
  page.on('request', (request) => {
    if (request.url().includes('/api/v1/backups')) backupReads += 1;
  });
  await page.goto('/backups');
  await submitLogin(page, user.login, user.temporary_password);
  await page.getByRole('heading', { name: text.changeTitle }).waitFor();
  const password = randomBytes(24).toString('base64url');
  await page.getByLabel(text.currentPassword, { exact: true }).fill(user.temporary_password);
  await page.getByLabel(text.newPassword, { exact: true }).fill(password);
  await page.getByLabel(text.repeatPassword, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.savePassword }).click();
  await page.getByRole('status').filter({ hasText: text.accessDenied }).waitFor();
  assert.equal(await page.getByRole('link', { name: text.backupsTitle, exact: true }).count(), 0);
  await page.keyboard.press('Control+k');
  assert.equal(
    await page
      .getByRole('dialog')
      .getByRole('link', { name: text.backupsTitle, exact: true })
      .count(),
    0,
  );
  await page.keyboard.press('Escape');
  assert.equal(backupReads, 0);
}

async function rejectNewerSchema(
  page: Page,
  backup: Backup,
  password: string,
  expected: RecordRow,
) {
  const path = `**/api/v1/backups/${backup.name}/restore`;
  const message = 'Копия сделана более новой версией программы. Обновите программу.';
  await page.route(path, (route) =>
    route.fulfill({
      status: 400,
      json: { error: { code: 'schema_newer', message } },
    }),
  );
  try {
    const row = page
      .getByRole('table', { name: copy.list, exact: true })
      .getByRole('row')
      .filter({ hasText: backup.name });
    await row.getByRole('button', { name: copy.restore, exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel(copy.password, { exact: true }).fill(password);
    await dialog.getByRole('button', { name: copy.restore, exact: true }).click();
    await dialog.getByRole('alert').filter({ hasText: message }).waitFor();
    assert.deepEqual(await currentRecord(page, expected.id), expected);
    await dialog.getByRole('button', { name: text.access.cancel, exact: true }).click();
  } finally {
    await page.unroute(path);
  }
}

test(
  'C-203 — копия, восстановление и защитная копия на настоящем сервере',
  { timeout: 120_000 },
  async (t) => {
    const config = await startAccessServer(t);
    const web = await preview({
      root,
      logLevel: 'error',
      preview: { host: '127.0.0.1', port: 0, strictPort: true, proxy: { '/api/v1': config.url } },
    });
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE });
    t.after(async () => {
      await browser.close();
      await web.close();
    });
    const address = web.httpServer.address();
    assert.ok(address && typeof address !== 'string');
    const origin = `http://127.0.0.1:${address.port}`;
    const context = await browser.newContext({
      baseURL: origin,
      timezoneId: 'Asia/Tashkent',
      viewport: { width: 1600, height: 1000 },
    });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto('/backups');
    await signIn(page, config.login, config.password);
    const dataset = (await (await page.request.get('/api/v1/datasets')).json()).items[0];
    const record = (
      await (
        await page.request.post(`/api/v1/datasets/${dataset.id}/records/query`, {
          data: { limit: 1 },
        })
      ).json()
    ).items[0] as RecordRow;
    const password = randomBytes(24).toString('base64url');
    const timings: Record<string, number> = {};
    let backup!: Backup;
    let modified!: RecordRow;
    let protective!: Backup;
    await t.test('создание с паролем, ход операции и обычная ссылка скачивания', async () => {
      backup = await createCopy(page, password, timings);
      await downloadCopy(page, backup);
    });
    assert.ok(backup);
    await t.test('неверный пароль оставляет изменённую запись прежней', async () => {
      modified = await patchRecord(page, record, 'Изменение после копии');
      await rejectPassword(page, backup, modified);
    });
    await t.test('восстановление исходного значения и выход на вход', async () => {
      protective = await restoreCopy(page, backup, password, timings, 'restore_ms');
      await signIn(page, config.login, config.password);
      assert.deepEqual((await currentRecord(page, record.id)).doc, record.doc);
      const row = page
        .getByRole('table', { name: copy.list, exact: true })
        .getByRole('row')
        .filter({ hasText: protective.name });
      await row.getByText(copy.protective, { exact: true }).waitFor();
    });
    await t.test('восстановление защитной копии возвращает изменение', async () => {
      await restoreCopy(page, protective, password, timings, 'undo_restore_ms');
      await signIn(page, config.login, config.password);
      assert.deepEqual((await currentRecord(page, record.id)).doc, modified.doc);
    });
    await t.test('повреждённый файл в списке и отказ повреждённой копии', async () => {
      await corruptCopies(
        page,
        config.folder,
        backup,
        password,
        await currentRecord(page, record.id),
      );
    });
    await t.test('schema_newer у формы: ответ по контракту', async () => {
      await rejectNewerSchema(page, backup, password, await currentRecord(page, record.id));
    });
    await t.test('раздел без права backups скрыт и не запрашивает список', async () => {
      await permissions(t, browser, origin, page);
    });
    t.diagnostic(`Время HTTP, мс: ${JSON.stringify(timings)}`);
    if (process.env.LOCALAI_SCREENSHOTS)
      await writeFile(
        join(process.env.LOCALAI_SCREENSHOTS, 'backup-timings.json'),
        JSON.stringify(timings, null, 2),
      );
    assert.deepEqual(errors, []);
  },
);
