import { events } from '../chat/events.ts';
import { problemText } from '../admin/model.ts';

export type Kind = 'record' | 'document' | 'kks';
export type Source = {
  kind: Kind;
  id: string;
  page?: string | null;
  version?: number | null;
  current_version?: number | null;
  code?: string;
  title?: string;
};
export type Scope = {
  kind: 'station' | 'kks' | 'type';
  id?: string | null;
  name?: string;
  code?: string;
};
export const states = {
  proposed: 'Предложена',
  confirmed: 'Подтверждена',
  stale: 'Устарела',
  rejected: 'Отклонена',
};
export type Note = {
  id: string;
  title: string;
  body: string;
  state: keyof typeof states;
  version: number;
  scope: Scope;
  sources: Source[];
  author: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  created_at: string;
  updated_at: string;
  stale_reason: string | null;
  reject_reason: string | null;
};
export type Kks = { id: string; code: string; site?: string };
export type Term = { id: string; term: string; meaning: string; kks: Kks[] };
export type Results = {
  records: { id: string; dataset: string; label: string }[];
  documents: { document_id: string; title: string; page: string }[];
  kks: (Kks & { via?: string; term?: string })[];
  notes: { id: string; title: string; snippet: string }[];
  glossary: Term[];
};
export type Page = { items: Note[]; more: boolean };
export type Queue = Page & { counts: { proposed: number; stale: number } };
export type Draft = { title: string; body: string; scope: Scope; sources: Source[] };
export function knowledgeUrl(params: Record<string, string>) {
  return '/knowledge?' + new URLSearchParams(params);
}
export function proposeUrl(kind: Kind, id: string, page?: string) {
  return knowledgeUrl({ new: '1', source: kind, id, ...(page ? { page } : {}) });
}
export function sourceFrom(params: URLSearchParams): Source | undefined {
  const kind = params.get('source'),
    id = params.get('id');
  if (id && (kind === 'kks' || kind === 'record' || kind === 'document'))
    return { kind, id, page: kind === 'document' ? params.get('page') : undefined };
}
export function scopeLabel(scope: Scope) {
  return scope.code || scope.name || 'Станция';
}

// Отбрасываем запоздалые ответы при смене фильтра, роли и после разрыва потока событий.
export function observe<T>(
  kind: string,
  read: (signal: AbortSignal) => Promise<T>,
  accept: (value: T) => void,
  fail: (message: string) => void,
  delay = 0,
) {
  let current: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout>;
  function refresh() {
    current?.abort();
    clearTimeout(timer);
    const request = new AbortController();
    current = request;
    timer = setTimeout(async () => {
      try {
        const result = await read(request.signal);
        if (!request.signal.aborted) accept(result);
      } catch (error) {
        if (!request.signal.aborted) fail(problemText(error));
      }
    }, delay);
  }
  const off = events.subscribe(kind, refresh),
    resume = events.subscribe('resync', refresh);
  refresh();
  return () => {
    clearTimeout(timer);
    current?.abort();
    off();
    resume();
  };
}
