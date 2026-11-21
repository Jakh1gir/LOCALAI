import { api } from '../api/client.ts';
import type { Change } from './types.ts';

type Listener = (changes: Change[]) => void;

function pause(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

export class EventStream {
  private listeners = new Map<string, Set<Listener>>();

  subscribe(kind: string, listener: Listener) {
    const group = this.listeners.get(kind) ?? new Set<Listener>();
    group.add(listener);
    this.listeners.set(kind, group);
    return () => {
      group.delete(listener);
    };
  }

  private emit(kind: string, changes: Change[]) {
    for (const callback of this.listeners.get(kind) ?? []) callback(changes);
  }

  async listen(signal: AbortSignal, read = api.events) {
    let cursor: number | undefined;
    let more = false;
    let delay = 0;
    while (!signal.aborted) {
      try {
        const page = await read(cursor, more ? 0 : 25, signal);
        if (signal.aborted) return;
        const resync = cursor === undefined || delay > 0;
        cursor = page.cursor;
        more = page.more;
        delay = 0;
        for (const kind of new Set(page.events.map((item) => item.kind))) {
          this.emit(
            kind,
            page.events.filter((item) => item.kind === kind),
          );
        }
        if (resync) this.emit('resync', []);
      } catch {
        if (signal.aborted) return;
        delay = Math.min(10_000, delay ? delay * 2 : 250);
        await pause(delay, signal);
      }
    }
  }
}
export const events = new EventStream();
