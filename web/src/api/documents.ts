import type { ApiPath, Options } from './client.ts';
import type { Items } from './types.ts';
import type { Brief, Chunk, Detail, Kind, Matches, Mention } from '../documents/model.ts';

export function documentMethods(request: <T>(path: ApiPath, options?: Options) => Promise<T>) {
  return {
    types: (signal: AbortSignal) =>
      request<Items<{ id: string; name: string }>>('/api/v1/document-types', { signal }),
    list: (query: Record<string, string | undefined>, signal: AbortSignal) =>
      request<Items<Brief> & { more: boolean }>('/api/v1/documents', {
        query: { limit: '500', ...query },
        signal,
      }),
    get: (id: string, signal: AbortSignal) => request<Detail>('/api/v1/documents/{id}', { id, signal }),
    text: (id: string, page: string | undefined, signal: AbortSignal) =>
      request<Items<Chunk> & { version: number }>('/api/v1/documents/{id}/text', {
        id,
        query: { page },
        signal,
      }),
    mentions: (id: string, signal: AbortSignal) =>
      request<Items<Mention>>('/api/v1/documents/{id}/mentions', { id, signal }),
    upload: (file: File, title: string, type: string) =>
      request<Detail>('/api/v1/documents', {
        method: 'POST',
        file,
        query: { name: file.name, title: title || undefined, type_id: type },
      }),
    replace: (id: string, file: File, version: number) =>
      request<Detail>('/api/v1/documents/{id}/file', {
        method: 'PUT',
        id,
        file,
        query: { name: file.name, version: String(version) },
      }),
    archive: (id: string) => request<Detail>('/api/v1/documents/{id}/archive', { method: 'POST', id }),
    link: (id: string, kind: Kind, target: string) =>
      request<Detail>('/api/v1/documents/{id}/links', {
        method: 'POST',
        id,
        body: { kind, id: target },
      }),
    unlink: (id: string, kind: Kind, target: string) =>
      request<Detail>('/api/v1/documents/{id}/links/{kind}/{target}', {
        method: 'DELETE',
        id,
        params: { kind, target },
      }),
    decide: (id: string, kks_id: string, decision: 'confirm' | 'reject') =>
      request<Items<Mention>>('/api/v1/documents/{id}/mentions/{kks_id}', {
        method: 'POST',
        id,
        params: { kks_id },
        body: { decision },
      }),
    audit: (id: string, before: string | undefined, signal: AbortSignal) =>
      request<
        Items<{
          seq: number;
          action: string;
          at: string;
          actor_label: string;
          details: { version?: number };
        }>
      >('/api/v1/audit', { query: { entity_id: id, limit: '200', before }, signal }),
    search: (q: string, signal: AbortSignal) =>
      request<Matches>('/api/v1/search', { query: { q }, signal }),
  };
}
