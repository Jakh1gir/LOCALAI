import { api } from '../api/client.ts';
import { problemText } from '../admin/model.ts';
import type { Matches } from './model.ts';

export function lookup(query: () => string) {
  let result = $state<Matches | null>(null);
  let error = $state('');
  $effect(() => {
    const value = query().trim();
    result = null;
    error = '';
    if (!value) return;
    const request = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const next = await api.documents.search(value, request.signal);
        if (!request.signal.aborted) result = next;
      } catch (cause) {
        if (!request.signal.aborted) error = problemText(cause);
      }
    }, 160);
    return () => {
      clearTimeout(timer);
      request.abort();
    };
  });
  return {
    get result() {
      return result;
    },
    get error() {
      return error;
    },
  };
}
