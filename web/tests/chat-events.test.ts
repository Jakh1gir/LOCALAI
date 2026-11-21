import assert from 'node:assert/strict';
import { test } from 'node:test';
import { EventStream } from '../src/chat/events.ts';
import type { Change, EventPage } from '../src/chat/types.ts';

test('События разделяются по виду, страницы more догружаются, после обрыва курсор сохраняется', async () => {
  const stream = new EventStream();
  const abort = new AbortController();
  const calls: [number | undefined, number | undefined][] = [];
  const records: string[] = [],
    chat: string[] = [];
  let resync = 0;
  stream.subscribe('resync', () => {
    resync++;
  });
  const unsubscribe = stream.subscribe('record', (changes) =>
    records.push(...changes.map((c) => c.action)),
  );
  stream.subscribe('chat_message', (changes) => chat.push(...changes.map((c) => c.action)));
  const change = (seq: number, kind: string, action: string): Change => ({
    seq,
    kind,
    action,
    at: '',
    actor: '',
    id: 'id',
    dataset_id: null,
  });
  const read = async (cursor?: number, wait?: number): Promise<EventPage> => {
    calls.push([cursor, wait]);
    switch (calls.length) {
      case 1:
        return { cursor: 30, events: [], more: false };
      case 2:
        return {
          cursor: 34,
          events: [change(32, 'record', 'record.create'), change(34, 'chat_message', 'chat.post')],
          more: true,
        };
      case 3:
        throw new Error('Нет связи');
      case 4:
        unsubscribe();
        return {
          cursor: 36,
          events: [change(35, 'record', 'record.update'), change(36, 'chat_message', 'chat.hide')],
          more: false,
        };
      default:
        abort.abort();
        return { cursor: 36, events: [], more: false };
    }
  };
  await stream.listen(abort.signal, read);
  assert.deepEqual(calls, [
    [undefined, 25],
    [30, 25],
    [34, 0],
    [34, 0],
    [36, 25],
  ]);
  assert.equal(resync, 2);
  assert.deepEqual(records, ['record.create']);
  assert.deepEqual(chat, ['chat.post', 'chat.hide']);
});
