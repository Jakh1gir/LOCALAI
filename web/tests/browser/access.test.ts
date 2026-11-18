import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium, type Browser, type Page } from 'playwright-core';
import { preview } from 'vite';
import type { Dataset, PermissionCatalog, RoleDetails, User } from '../../src/api/types.ts';
import { text } from '../../src/text.ts';
import { startAccessServer } from './server.ts';

const copy = text.access;
const root = fileURLToPath(new URL('../../', import.meta.url));

async function signIn(page: Page, login: string, password: string) {
  await page.getByLabel(text.loginField, { exact: true }).fill(login);
  await page.getByLabel(text.passwordField, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.signIn, exact: true }).click();
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

async function users(page: Page) {
  await page.getByRole('button', { name: copy.users, exact: true }).click();
  await page
    .getByRole('table', { name: copy.users, exact: true })
    .getByRole('row')
    .nth(1)
    .waitFor();
}

async function roles(page: Page) {
  await page.getByRole('button', { name: copy.roles, exact: true }).click();
  await page
    .getByRole('table', { name: copy.roles, exact: true })
    .getByRole('row')
    .nth(1)
    .waitFor();
}

async function copyRole(page: Page, catalog: PermissionCatalog, datasets: Dataset[]) {
  await roles(page);
  const table = page.getByRole('table', { name: copy.roles, exact: true });
  await table
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: 'Инженер', exact: true }) })
    .getByRole('button', { name: copy.copy, exact: true })
    .click();
  const form = page.getByRole('form', { name: copy.createRole });
  const roleName = `Оператор ${randomBytes(3).toString('hex')}`;
  await form.getByLabel(copy.roleName, { exact: true }).fill(roleName);
  await form.getByRole('button', { name: copy.save, exact: true }).click();
  const row = table
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: roleName, exact: true }) });
  await row.waitFor();
  await row.getByRole('button', { name: copy.permissions, exact: true }).click();
  const matrix = page.getByRole('region', { name: `${copy.permissions} · ${roleName}` });
  await matrix
    .getByRole('checkbox', {
      name: `${copy.allDatasets} · ${catalog.dataset_actions.view}`,
      exact: true,
    })
    .waitFor();
  assert.equal(
    await matrix
      .getByRole('table', { name: copy.datasets, exact: true })
      .getByRole('columnheader')
      .count(),
    Object.keys(catalog.dataset_actions).length + 1,
  );
  await matrix
    .getByRole('checkbox', {
      name: `${copy.allDatasets} · ${catalog.dataset_actions.create}`,
      exact: true,
    })
    .uncheck();
  await matrix.getByRole('heading', { name: copy.removed, exact: true }).waitFor();

  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('button', { name: copy.users, exact: true }).click();
  assert.equal(await matrix.count(), 1, 'Отказ от перехода сохраняет черновик.');
  await matrix.getByRole('button', { name: copy.savePermissions, exact: true }).click();
  await matrix.getByRole('status').filter({ hasText: copy.saved }).waitFor();
  const items = (await (await page.request.get('/api/v1/roles')).json()).items as RoleDetails[];
  const found = items.find((item) => item.name === roleName)!;
  const role = (await (await page.request.get(`/api/v1/roles/${found.id}`)).json()) as RoleDetails;
  assert.ok(role.permissions.every((item) => item.scope !== 'dataset' || item.action !== 'create'));
  assert.ok(role.permissions.some((item) => item.scope === 'dataset' && item.action === 'view'));

  await matrix.getByLabel(copy.addDataset, { exact: true }).selectOption(datasets[0].id);
  await matrix.getByRole('button', { name: copy.addDataset, exact: true }).click();
  const targetRow = matrix
    .getByRole('table', { name: copy.datasets, exact: true })
    .getByRole('row')
    .last();
  assert.equal(
    await targetRow.getByRole('checkbox').first().isDisabled(),
    true,
    'Общее право помечено как унаследованное.',
  );
  if (catalog.dataset_actions.purge)
    await targetRow
      .getByRole('checkbox', { name: new RegExp(catalog.dataset_actions.purge) })
      .check();
  const types = (await (await page.request.get('/api/v1/types')).json()).items;
  const details = await (await page.request.get(`/api/v1/types/${types[0].id}`)).json();
  await matrix.getByLabel(copy.recordType, { exact: true }).selectOption(types[0].id);
  const field = details.fields[0];
  await matrix
    .getByRole('combobox', { name: `${copy.permissions} · ${field.name}`, exact: true })
    .selectOption('readonly');
  await matrix.getByRole('button', { name: copy.savePermissions, exact: true }).click();
  await matrix.getByRole('status').filter({ hasText: copy.saved }).waitFor();
  const saved = (await (await page.request.get(`/api/v1/roles/${found.id}`)).json()) as RoleDetails;
  assert.ok(
    saved.permissions.some(
      (item) => item.scope === 'field' && item.target === field.id && item.action === 'readonly',
    ),
  );
  if (catalog.dataset_actions.purge)
    assert.ok(
      saved.permissions.some(
        (item) =>
          item.scope === 'dataset' && item.target === datasets[0].id && item.action === 'purge',
      ),
    );
  await screenshot(page, 'admin-permissions');
  return { id: found.id, name: roleName };
}

async function createUser(page: Page, role: { id: string; name: string }) {
  await users(page);
  await page.getByRole('button', { name: copy.createUser, exact: true }).click();
  const form = page.getByRole('form', { name: copy.createUser });
  const login = `operator-${randomBytes(4).toString('hex')}`;
  await form.getByLabel(copy.login, { exact: true }).fill(login);
  await form.getByLabel(copy.name, { exact: true }).fill('Инженер испытаний');
  await form.getByLabel(copy.role, { exact: true }).selectOption(role.id);
  let lost = 0;
  await page.route('**/api/v1/auth/me', async (route) => {
    if (lost < 2) {
      lost += 1;
      await route.abort('failed');
    } else await route.continue();
  });
  await form.getByRole('button', { name: copy.save, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: copy.temporaryPassword });
  await dialog.waitFor();
  const password = await dialog.getByLabel(copy.temporaryPassword, { exact: true }).inputValue();
  assert.ok(password.length >= 12);
  await dialog.getByRole('button', { name: copy.copy, exact: true }).click();
  await dialog.getByRole('status').filter({ hasText: copy.copied }).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), password);
  await dialog.getByRole('button', { name: copy.close, exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.locator('.secret-dialog input').waitFor({ state: 'detached' });
  assert.equal(await page.getByLabel(copy.temporaryPassword, { exact: true }).count(), 0);
  const items = (await (await page.request.get('/api/v1/users')).json()).items as User[];
  const user = items.find((item) => item.login === login)!;
  assert.equal('temporary_password' in user, false);
  assert.equal(lost, 2, 'Разрыв проверки сессии сохраняет окно выданного пароля.');
  await page.unroute('**/api/v1/auth/me');
  await page.getByRole('button', { name: text.retry, exact: true }).click();
  await page.getByRole('status').filter({ hasText: text.serverReady }).waitFor();
  await screenshot(page, 'admin-users');
  return { ...user, password };
}

async function restrictedUser(
  t: TestContext,
  browser: Browser,
  origin: string,
  user: User & { password: string },
  dataset: Dataset,
) {
  const context = await browser.newContext();
  t.after(() => context.close());
  const page = await context.newPage();
  const requests: string[] = [];
  page.on('request', (request) => requests.push(new URL(request.url()).pathname));
  await page.goto(`${origin}/registries`);
  await signIn(page, user.login, user.password);
  await page.getByRole('heading', { name: text.changeTitle }).waitFor();
  const password = randomBytes(24).toString('base64url');
  await page.getByLabel(text.currentPassword, { exact: true }).fill(user.password);
  await page.getByLabel(text.newPassword, { exact: true }).fill(password);
  await page.getByLabel(text.repeatPassword, { exact: true }).fill(password);
  await page.getByRole('button', { name: text.savePassword }).click();
  await page.getByRole('heading', { name: text.registry, level: 1 }).waitFor();
  assert.equal(await page.getByRole('link', { name: text.admin, exact: true }).count(), 0);
  await page.goto(`${origin}/admin`);
  await page.getByRole('status').filter({ hasText: text.accessDenied }).waitFor();
  assert.ok(!requests.includes('/api/v1/users'), 'Недоступный экран не запрашивает пользователей.');
  await page.keyboard.press('Control+k');
  assert.equal(
    await page.getByRole('dialog').getByRole('link', { name: text.admin, exact: true }).count(),
    0,
  );
  await page.keyboard.press('Escape');
  const denied = await page.request.post(`${origin}/api/v1/datasets/${dataset.id}/records`, {
    data: { values: {} },
    headers: { 'Idempotency-Key': randomBytes(16).toString('hex') },
  });
  assert.equal(denied.status(), 403);
  assert.equal((await denied.json()).error.details.permission, 'dataset.create');
  await page.getByRole('button', { name: text.signOut, exact: true }).click();
  await page.getByRole('heading', { name: text.loginTitle }).waitFor();
}

async function lastAdmin(page: Page, login: string, catalog: PermissionCatalog) {
  await users(page);
  const row = page
    .getByRole('table', { name: copy.users, exact: true })
    .getByRole('row')
    .filter({ hasText: login });
  const rejected = page.waitForResponse(
    (response) => response.url().endsWith('/block') && response.status() === 409,
  );
  await row.getByRole('button', { name: copy.block, exact: true }).click();
  const message = (await (await rejected).json()).error.message;
  await row.getByRole('alert').filter({ hasText: message }).waitFor();
  await roles(page);
  const admin = page
    .getByRole('table', { name: copy.roles, exact: true })
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: 'Администратор', exact: true }) });
  await admin.getByRole('button', { name: copy.disable, exact: true }).click();
  await admin.getByRole('alert').waitFor();
  await admin.getByRole('button', { name: copy.permissions, exact: true }).click();
  const matrix = page.getByRole('region', { name: `${copy.permissions} · Администратор` });
  await matrix.getByRole('checkbox', { name: catalog.sections.admin, exact: true }).uncheck();
  await matrix.getByRole('button', { name: copy.savePermissions, exact: true }).click();
  await matrix.getByRole('alert').waitFor();
  await matrix.getByRole('button', { name: copy.discard, exact: true }).click();
  const current = await (await page.request.get('/api/v1/auth/me')).json();
  assert.ok(current.permissions.sections.includes('admin'));
}

async function lifecycle(page: Page, user: User, role: { id: string; name: string }) {
  await users(page);
  const row = page
    .getByRole('table', { name: copy.users, exact: true })
    .getByRole('row')
    .filter({ hasText: user.login });
  await row.getByRole('button', { name: copy.edit, exact: true }).click();
  const form = page.getByRole('form', { name: copy.editUser });
  await form.getByLabel(copy.name, { exact: true }).fill('Инженер смены');
  await form.getByRole('button', { name: copy.save, exact: true }).click();
  await row.getByRole('cell', { name: 'Инженер смены', exact: true }).waitFor();
  await row.getByRole('button', { name: copy.block, exact: true }).click();
  await row.getByRole('button', { name: copy.unblock, exact: true }).click();
  await row.getByRole('button', { name: copy.block, exact: true }).waitFor();
  await row.getByRole('button', { name: copy.resetPassword, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: copy.temporaryPassword });
  await dialog.waitFor();
  assert.ok(
    (await dialog.getByLabel(copy.temporaryPassword, { exact: true }).inputValue()).length >= 12,
  );
  await dialog.getByRole('button', { name: copy.close, exact: true }).click();
  await page.locator('.secret-dialog input').waitFor({ state: 'detached' });
  await row.getByText(copy.changeRequired, { exact: true }).waitFor();

  await roles(page);
  const before = (await (await page.request.get(`/api/v1/roles/${role.id}`)).json()) as RoleDetails;
  const table = page.getByRole('table', { name: copy.roles, exact: true });
  let roleRow = table
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: role.name, exact: true }) });
  await roleRow.getByRole('cell', { name: '1', exact: true }).waitFor();
  await roleRow.getByRole('button', { name: copy.edit, exact: true }).click();
  const roleForm = page.getByRole('form', { name: copy.editRole });
  role.name += ' · Участок 1';
  await roleForm.getByLabel(copy.roleName, { exact: true }).fill(role.name);
  await roleForm.getByRole('button', { name: copy.save, exact: true }).click();
  roleRow = table
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: role.name, exact: true }) });
  await roleRow.waitFor();
  const after = (await (await page.request.get(`/api/v1/roles/${role.id}`)).json()) as RoleDetails;
  assert.equal(after.id, before.id);
  assert.deepEqual(after.permissions, before.permissions);
  await roleRow.getByRole('button', { name: copy.disable, exact: true }).click();
  await roleRow.getByRole('cell').filter({ hasText: copy.disabled }).waitFor();
  await roleRow.getByRole('button', { name: copy.restore, exact: true }).click();
  await roleRow.getByRole('button', { name: copy.disable, exact: true }).waitFor();
}

async function matrixError(
  page: Page,
  role: { id: string; name: string },
  catalog: PermissionCatalog,
) {
  await roles(page);
  await page
    .getByRole('table', { name: copy.roles, exact: true })
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: role.name, exact: true }) })
    .getByRole('button', { name: copy.permissions, exact: true })
    .click();
  const matrix = page.getByRole('region', { name: `${copy.permissions} · ${role.name}` });
  const current = (await (
    await page.request.get(`/api/v1/roles/${role.id}`)
  ).json()) as RoleDetails;
  const [section, label] = Object.entries(catalog.sections).find(
    ([key]) => !current.permissions.some((item) => item.scope === 'section' && item.target === key),
  )!;
  await matrix.getByRole('checkbox', { name: label, exact: true }).check();
  // Меняем один запрос: ответ и проверка permissions.N остаются настоящими.
  await page.route(`**/roles/${role.id}/permissions`, async (route) => {
    const body = route.request().postDataJSON();
    const index = body.permissions.findIndex(
      (item: { scope: string; target: string }) =>
        item.scope === 'section' && item.target === section,
    );
    body.permissions[index].action = 'invalid_action';
    await route.continue({ postData: JSON.stringify(body) });
  });
  await matrix.getByRole('button', { name: copy.savePermissions, exact: true }).click();
  await matrix
    .getByRole('checkbox', { name: label, exact: true })
    .locator('..')
    .locator('..')
    .getByRole('alert')
    .waitFor();
  await page.unroute(`**/roles/${role.id}/permissions`);
  await matrix.getByRole('button', { name: copy.savePermissions, exact: true }).click();
  await matrix.getByRole('status').filter({ hasText: copy.saved }).waitFor();
  const updated = (await (
    await page.request.get(`/api/v1/roles/${role.id}`)
  ).json()) as RoleDetails;
  assert.deepEqual(
    updated.permissions.filter((item) => item.scope !== 'section'),
    current.permissions.filter((item) => item.scope !== 'section'),
  );
}

async function networks(page: Page) {
  // Uvicorn доверяет заголовкам локального прокси; имитируем рабочее место в сети.
  await page.setExtraHTTPHeaders({ 'X-Forwarded-For': '10.20.30.4' });
  await page.getByRole('button', { name: copy.networks, exact: true }).click();
  const form = page.getByRole('form', { name: copy.addNetwork });
  const table = page.getByRole('table', { name: copy.networks, exact: true });
  for (const network of ['10.20.30.0/24', '192.0.2.0/24']) {
    await form.getByLabel(copy.network, { exact: true }).fill(network);
    await form.getByLabel(copy.note, { exact: true }).fill('Браузерный сценарий');
    await form.getByRole('button', { name: copy.addNetwork, exact: true }).click();
    await table.getByRole('row').filter({ hasText: network }).waitFor();
  }
  const current = table.getByRole('row').filter({ hasText: '10.20.30.0/24' });
  await current.getByRole('button', { name: copy.remove, exact: true }).click();
  await current.getByRole('alert').filter({ hasText: copy.networkLockout }).waitFor();
  await screenshot(page, 'admin-networks');
  await table
    .getByRole('row')
    .filter({ hasText: '192.0.2.0/24' })
    .getByRole('button', { name: copy.remove, exact: true })
    .click();
  await current.getByRole('button', { name: copy.remove, exact: true }).click();
  await table.getByRole('cell', { name: copy.unrestricted, exact: true }).waitFor();
  await page.setExtraHTTPHeaders({});
}

test('C-201 на настоящем сервере с базой C-101', { timeout: 120_000 }, async (t) => {
  const config = await startAccessServer(t);
  const web = await preview({
    root,
    logLevel: 'error',
    preview: {
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
      proxy: { '/api/v1': config.url },
    },
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
    viewport: { width: 1600, height: 1000 },
    permissions: ['clipboard-read', 'clipboard-write'],
  });
  const page = await context.newPage();
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(`${origin}/admin`);
  await signIn(page, config.login, config.password);
  await page.getByRole('heading', { name: copy.users, level: 2 }).waitFor();
  const catalog = (await (
    await page.request.get('/api/v1/auth/permissions')
  ).json()) as PermissionCatalog;
  const datasets = (await (await page.request.get('/api/v1/datasets')).json()).items as Dataset[];
  let role: { id: string; name: string };
  let user: User & { password: string };
  await t.test('копия роли, снятие общего права, ограничения полей и черновик', async () => {
    role = await copyRole(page, catalog, datasets);
  });
  await t.test('создание пользователя и однократный временный пароль', async () => {
    user = await createUser(page, role);
  });
  await t.test('временный пароль, новый пароль и отказ на создание записи', async () => {
    await restrictedUser(t, browser, origin, user, datasets[0]);
  });
  await t.test(
    'имя пользователя, блокировка, сброс пароля, переименование и отключение роли',
    async () => {
      await lifecycle(page, user, role);
    },
  );
  await t.test('отказы при блокировке и лишении прав последнего администратора', async () => {
    await lastAdmin(page, config.login, catalog);
  });
  await t.test('ошибка сервера permissions.N у нужной строки матрицы', async () => {
    await matrixError(page, role, catalog);
  });
  await t.test('сети и настоящий отказ network_lockout', async () => {
    await networks(page);
  });
  assert.deepEqual(errors, []);
});
