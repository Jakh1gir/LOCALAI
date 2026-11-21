import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import type { Page, Route } from 'playwright-core';

async function post(page: Page, text: string) {
  const result = await page.request.post('/api/v1/chat/messages', {
    data: { text },
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.equal(result.status(), 201);
  return result.json();
}

export async function recover(admin: Page, other: Page) {
  let connected!: () => void;
  const firstPoll = new Promise<void>((resolve) => {
    connected = resolve;
  });
  let interrupted = true;
  const cursors: string[] = [];
  const intercept = async (route: Route) => {
    const cursor = new URL(route.request().url()).searchParams.get('after');
    if (cursor && interrupted) {
      cursors.push(cursor);
      await route.abort('internetdisconnected');
      connected();
    } else await route.continue();
  };
  await other.route('**/api/v1/events*', intercept);
  try {
    await other.goto('/chat');
    await firstPoll;
    await other.getByText('Сообщение удалено', { exact: true }).waitFor();
    await post(admin, 'Передано при разрыве связи');
    assert.equal(await other.getByText('Передано при разрыве связи', { exact: true }).count(), 0);
    interrupted = false;
    const result = other.waitForRequest((req) => req.url().includes('/events?after='));
    await other.getByText('Передано при разрыве связи', { exact: true }).waitFor();
    assert.equal(new URL((await result).url()).searchParams.get('after'), cursors[0]);
  } finally {
    interrupted = false;
    await other.unroute('**/api/v1/events*', intercept);
  }
}

export async function conflict(page: Page) {
  await page.goto('/chat');
  const message = await post(page, 'Уточнение до правки');
  const row = page.locator('#message-' + message.seq);
  await row.getByRole('button', { name: 'Изменить' }).click();
  const changed = await page.request.patch('/api/v1/chat/messages/' + message.id, {
    data: { version: message.version, text: 'Уточнение из другой вкладки' },
    headers: { 'Idempotency-Key': randomUUID() },
  });
  assert.equal(changed.status(), 200);
  await row.getByText('Уточнение из другой вкладки', { exact: true }).waitFor();
  await row.getByLabel('Текст сообщения').fill('Устаревший черновик');
  await row.getByRole('button', { name: 'Сохранить' }).click();
  await row.getByRole('alert').waitFor();
  await row.getByRole('button', { name: 'Обновить ленту' }).click();
  assert.equal(await row.getByLabel('Текст сообщения').count(), 0);
}

export async function oldPagesAndRate(admin: Page, operator: Page) {
  await Promise.all([admin.goto('/checks'), operator.goto('/checks')]);
  for (let i = 0; i < 26; i++) {
    await Promise.all([
      post(admin, 'Журнал мастера ' + i),
      post(operator, 'Журнал оператора ' + i),
    ]);
  }
  await admin.goto('/chat');
  const feed = admin.getByRole('region', { name: 'Лента сообщений' });
  await feed.getByText('Журнал мастера 25', { exact: true }).waitFor();
  assert.equal(await feed.getByText('Передано при разрыве связи', { exact: true }).count(), 0);
  await admin.getByRole('searchbox', { name: 'Поиск по сообщениям' }).fill('разрыве');
  await admin.getByRole('button', { name: 'Найти', exact: true }).click();
  const before = admin.waitForRequest((r) => r.url().includes('/chat/messages?before='));
  await admin.getByRole('button', { name: 'К сообщению' }).click();
  await before;
  const target = feed.getByRole('article').filter({ hasText: 'Передано при разрыве связи' });
  await target.waitFor();
  await admin.waitForFunction(() => document.activeElement?.tagName === 'ARTICLE');
  assert.ok(await target.evaluate((el) => el === document.activeElement));

  const input = admin.getByLabel('Новое сообщение');
  await input.fill('я'.repeat(4001));
  assert.equal(await admin.getByRole('button', { name: 'Отправить' }).isDisabled(), true);
  for (let i = 0; i < 6; i++) {
    const response = admin.waitForResponse(
      (r) =>
        new URL(r.url()).pathname.endsWith('/chat/messages') && r.request().method() === 'POST',
    );
    await input.fill('Проверка ограничения ' + i);
    await input.press('Enter');
    if ((await response).status() === 429) {
      await admin.getByRole('alert').filter({ hasText: '30 сообщений' }).waitFor();
      assert.equal(await input.inputValue(), 'Проверка ограничения ' + i);
      return;
    }
    await admin.waitForFunction(
      () => !document.querySelector<HTMLTextAreaElement>('#chat-draft')?.value,
    );
  }
  assert.fail('Ожидался ответ 429 от сервера.');
}
