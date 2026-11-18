import { ApiError, signedOutCodes } from './api/client.ts';
import type { Me } from './api/types.ts';

/**
 * Что показать вместо рабочего экрана:
 * - `login` — входа нет, сессия закрыта или истекла;
 * - `change-password` — вход по временному паролю, сначала его надо сменить;
 * - `ready` — работа;
 * - `offline` — сервер не ответил: показывается оболочка с состоянием связи.
 */
export type Gate = 'checking' | 'login' | 'change-password' | 'ready' | 'offline';

export const minPasswordLength = 12;

export function gateForMe(me: Me): Gate {
  return me.must_change_password ? 'change-password' : 'ready';
}

export function gateForError(error: unknown): Gate {
  if (!(error instanceof ApiError)) return 'offline';
  if (error.status === 401 && signedOutCodes.has(error.code)) return 'login';
  if (error.status === 403 && error.code === 'password_change_required') return 'change-password';
  return 'offline';
}

/** Ошибка формы нового пароля до отправки на сервер или пустая строка. */
export function newPasswordProblem(password: string, repeat: string, login: string): string {
  if (password.length < minPasswordLength) return `Пароль короче ${minPasswordLength} символов.`;
  if (login && password.toLocaleLowerCase('ru').includes(login.toLocaleLowerCase('ru'))) {
    return 'Пароль не должен содержать логин.';
  }
  if (password !== repeat) return 'Пароли не совпадают.';
  return '';
}
