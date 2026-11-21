import type { ApiPath, Options } from './client.ts';
import type { Draft, Note, Page, Queue, Results, Term } from '../knowledge/model.ts';
import type { Items } from './types.ts';
export function knowledgeMethods(request: <T>(path: ApiPath, options?: Options) => Promise<T>) {
  return {
    notes: (query: Record<string, string | undefined>, signal: AbortSignal) =>
      request<Page>('/api/v1/knowledge/notes', { query: { ...query, limit: '200' }, signal }),
    note: (id: string, signal: AbortSignal) =>
      request<Note>('/api/v1/knowledge/notes/{id}', { id, signal }),
    queue: (signal: AbortSignal) => request<Queue>('/api/v1/knowledge/queue', { signal }),
    create: (body: Draft) => request<Note>('/api/v1/knowledge/notes', { method: 'POST', body }),
    edit: (id: string, body: Pick<Draft, 'title' | 'body' | 'sources'> & { version: number }) =>
      request<Note>('/api/v1/knowledge/notes/{id}', { method: 'PATCH', id, body }),
    confirm: (id: string, version: number) =>
      request<Note>('/api/v1/knowledge/notes/{id}/confirm', {
        method: 'POST',
        id,
        body: { version },
      }),
    reject: (id: string, reason: string) =>
      request<Note>('/api/v1/knowledge/notes/{id}/reject', {
        method: 'POST',
        id,
        body: { reason },
      }),
    terms: (q: string, signal: AbortSignal) =>
      request<Items<Term>>('/api/v1/knowledge/glossary', { query: { q }, signal }),
    saveTerm: (
      id: string | undefined,
      body: { term: string; meaning: string; kks_ids: string[] },
    ) =>
      id
        ? request<Term>('/api/v1/knowledge/glossary/{id}', { method: 'PUT', id, body })
        : request<Term>('/api/v1/knowledge/glossary', { method: 'POST', body }),
    deleteTerm: (id: string) =>
      request<void>('/api/v1/knowledge/glossary/{id}', { method: 'DELETE', id }),
    search: (q: string, signal: AbortSignal) =>
      request<Results>('/api/v1/search', { query: { q }, signal }),
    document: (id: string, signal: AbortSignal) =>
      request<{ id: string; title: string }>('/api/v1/documents/{id}', { id, signal }),
  };
}
