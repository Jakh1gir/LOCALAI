import { events } from '../chat/events.ts';
import { problemText } from '../admin/model.ts';

export const states = {
  queued: 'В очереди',
  reading: 'Читается',
  ready: 'Готово',
  empty: 'Текста нет',
  error: 'Ошибка',
};
export type Kind = 'kks' | 'record';
export type Brief = {
  id: string;
  title: string;
  type: { id: string; name: string };
  version: number;
  state: keyof typeof states;
  updated_at: string;
  pages: number | null;
  archived: boolean;
  file_name: string;
};
export type Detail = Brief & {
  error: string | null;
  can: { edit: boolean };
  links: {
    kind: Kind;
    id: string;
    code?: string;
    dataset?: string;
    dataset_id?: string;
    access: boolean;
  }[];
};
export type Chunk = { page: string; ordinal: number; text: string };
export type Mention = {
  kks_id: string;
  code: string;
  pages: string[];
  hits: number;
  state: 'proposed' | 'confirmed' | 'rejected';
};
export type Hit = { document_id: string; page: string; title: string; snippet: string };
export type Matches = {
  documents: Hit[];
  kks: { id: string; code: string; site: string }[];
  records: { id: string; label: string; dataset: string }[];
};
export function documentUrl(id: string, page?: string) {
  const params = new URLSearchParams({ document: id });
  if (page) params.set('page', page);
  return '/documents?' + params;
}

// При переподключении заново читаем снимок, даже если события уже вышли из окна.
export function watchDocuments<T>(
  read: (signal: AbortSignal) => Promise<T>,
  accept: (data: T) => void,
  fail: (message: string) => void,
  id?: string,
  pending?: (data: T) => boolean,
) {
  let active: AbortController;
  let timer: ReturnType<typeof setTimeout>;
  function refresh() {
    clearTimeout(timer);
    active?.abort();
    const request = new AbortController();
    active = request;
    void read(request.signal)
      .then((data) => {
        if (request.signal.aborted) return;
        accept(data);
        if (pending?.(data)) timer = setTimeout(refresh, 5000);
      })
      .catch((error) => {
        if (!request.signal.aborted) fail(problemText(error));
      });
  }
  const off = events.subscribe('document', (changes) => {
    if (!id || changes.some((change) => change.id === id)) refresh();
  });
  const resync = events.subscribe('resync', refresh);
  refresh();
  return () => {
    off();
    resync();
    clearTimeout(timer);
    active.abort();
  };
}
