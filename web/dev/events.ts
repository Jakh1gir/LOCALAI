import type { Change } from '../src/chat/types.ts';
import { dataset, mockMe } from './fixture.ts';

export function mockRecordEvents() {
  const changes: Change[] = [];
  const notify = new EventTarget();
  return {
    add(id: string, created: boolean, at: string) {
      changes.push({
        seq: changes.length + 1,
        at,
        kind: 'record',
        action: created ? 'record.create' : 'record.update',
        id,
        dataset_id: dataset.id,
        actor: mockMe.user.name,
      });
      notify.dispatchEvent(new Event('change'));
    },
    async page(query: URLSearchParams, signal: AbortSignal) {
      const after = query.get('after');
      if (after === null) return { cursor: changes.length, events: [], more: false };
      const cursor = Number(after);
      const wait = Math.min(25, Number(query.get('wait')));
      if (cursor === changes.length && wait > 0 && !signal.aborted) {
        await new Promise<void>((resolve) => {
          const finish = () => {
            clearTimeout(timer);
            notify.removeEventListener('change', finish);
            signal.removeEventListener('abort', finish);
            resolve();
          };
          const timer = setTimeout(finish, wait * 1000);
          notify.addEventListener('change', finish, { once: true });
          signal.addEventListener('abort', finish, { once: true });
        });
      }
      const events = changes.slice(cursor, cursor + 200);
      const last = events.at(-1)?.seq ?? cursor;
      return { cursor: last, events, more: last < changes.length };
    },
  };
}
