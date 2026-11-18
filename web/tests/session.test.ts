import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createMockApi } from '../dev/mock-api.ts';
import { ApiError, createApiClient, sessionEvents } from '../src/api/client.ts';
import { gateForError, gateForMe, newPasswordProblem } from '../src/session.ts';
import { mockMe } from '../dev/fixture.ts';

const baseUrl = 'http://localhost';

test('вход, выход и смена пароля идут без ключа идемпотентности и с cookie сессии', async () => {
  const mock = createMockApi();
  const seen: { path: string; key: string | null; credentials: RequestCredentials }[] = [];
  const api = createApiClient({
    baseUrl,
    fetchRequest: async (request) => {
      seen.push({
        path: new URL(request.url).pathname,
        key: request.headers.get('Idempotency-Key'),
        credentials: request.credentials,
      });
      return mock(request);
    },
  });
  assert.equal((await api.me()).user.login, 'demo');
  await api.logout();
  await assert.rejects(api.me(), (error: unknown) =>
    error instanceof ApiError && error.status === 401 && gateForError(error) === 'login');
  const me = await api.login('demo', 'любой-пароль');
  assert.equal(gateForMe(me), 'ready');
  await api.changePassword('старый-пароль-1', 'новый-пароль-12');
  const [dataset] = (await api.listDatasets()).items;
  await api.createRecord(dataset.id, { name: 'Насос' });
  const keys = seen.map(({ path, key }) => [path, key !== null]);
  assert.deepEqual(keys.slice(0, 6), [
    ['/api/v1/auth/me', false],
    ['/api/v1/auth/logout', false],
    ['/api/v1/auth/me', false],
    ['/api/v1/auth/login', false],
    ['/api/v1/auth/password', false],
    ['/api/v1/datasets', false],
  ]);
  assert.deepEqual(keys.at(-1), [`/api/v1/datasets/${dataset.id}/records`, true]);
  // В Node поле Request.credentials есть не всегда; в браузере оно задаёт отправку cookie.
  assert.ok(seen.every(({ credentials }) => credentials === undefined || credentials === 'same-origin'));
});

test('неверный пароль не считается потерей сессии, истёкшая сессия — считается', async () => {
  let lost = 0;
  const listener = () => { lost += 1; };
  sessionEvents.addEventListener('signed-out', listener);
  try {
    const reply = (status: number, code: string) => async () =>
      Response.json({ error: { code, message: 'Сообщение сервера' } }, { status });
    const wrong = createApiClient({ baseUrl, fetchRequest: reply(401, 'login_failed') });
    await assert.rejects(wrong.login('admin', 'не-тот'), (error: unknown) =>
      error instanceof ApiError && error.message === 'Сообщение сервера' && gateForError(error) === 'offline');
    assert.equal(lost, 0);
    const expired = createApiClient({ baseUrl, fetchRequest: reply(401, 'session_expired') });
    await assert.rejects(expired.listSites());
    assert.equal(lost, 1);
    const temporary = createApiClient({ baseUrl, fetchRequest: reply(403, 'password_change_required') });
    await assert.rejects(temporary.listSites(), (error: unknown) => gateForError(error) === 'change-password');
    assert.equal(gateForError(new TypeError('сеть')), 'offline');
  } finally {
    sessionEvents.removeEventListener('signed-out', listener);
  }
});

test('временный пароль ведёт на смену, новый пароль проверяется до отправки', () => {
  assert.equal(gateForMe({ ...mockMe, must_change_password: true }), 'change-password');
  assert.equal(newPasswordProblem('короткий', 'короткий', 'demo'), 'Пароль короче 12 символов.');
  assert.equal(newPasswordProblem('demo-пароль-2026', 'demo-пароль-2026', 'Demo'), 'Пароль не должен содержать логин.');
  assert.equal(newPasswordProblem('надёжный-пароль', 'надёжный-парол', 'demo'), 'Пароли не совпадают.');
  assert.equal(newPasswordProblem('надёжный-пароль', 'надёжный-пароль', 'demo'), '');
});
