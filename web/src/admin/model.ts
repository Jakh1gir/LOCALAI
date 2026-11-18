import { ApiError } from '../api/client.ts';
import type { Permission, User } from '../api/types.ts';
import { text } from '../text.ts';

export function problemText(error: unknown): string {
  return error instanceof Error ? error.message : text.networkError;
}

export function permissionKey(item: Permission): string {
  return JSON.stringify([item.scope, item.target, item.action]);
}

export function permissionChanges(before: Permission[], after: Permission[]) {
  const oldKeys = new Set(before.map(permissionKey));
  const newKeys = new Set(after.map(permissionKey));
  return {
    added: after.filter((item) => !oldKeys.has(permissionKey(item))),
    removed: before.filter((item) => !newKeys.has(permissionKey(item))),
  };
}

export function permissionError(error: unknown, sent: Permission[]): Permission | undefined {
  if (!(error instanceof ApiError)) return;
  const match = /^permissions\.(\d+)(?:\.|$)/.exec(error.field ?? '');
  return match ? sent[Number(match[1])] : undefined;
}

export function userStates(user: User, roleArchived: boolean, now = Date.now()): string[] {
  const states: string[] = [];
  if (user.blocked_at) states.push(text.access.blocked);
  if (roleArchived) states.push(text.access.roleDisabled);
  if (user.locked_until && Date.parse(user.locked_until) > now)
    states.push(text.access.loginLocked);
  if (user.must_change_password) states.push(text.access.changeRequired);
  return states.length ? states : [text.access.active];
}

export function showTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('ru-RU') : text.access.never;
}
