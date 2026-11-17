import type { Connect, Plugin } from 'vite';
import { createMockApi } from './mock-api.ts';

const maxBodyBytes = 10 * 1024 * 1024;

async function readBody(request: Connect.IncomingMessage): Promise<string | undefined> {
  if (request.method === 'GET' || request.method === 'HEAD') return;
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.from(chunk);
    size += buffer.length;
    if (size <= maxBodyBytes) chunks.push(buffer);
  }
  if (size > maxBodyBytes) throw new RangeError('Размер тела запроса превышает 10 МиБ.');
  return Buffer.concat(chunks).toString('utf8');
}

export function mockApiPlugin(): Plugin {
  return {
    name: 'localai-mock-api',
    apply: 'serve',
    configureServer(server) {
      const handle = createMockApi();
      server.middlewares.use(async (incoming, outgoing, next) => {
        const path = incoming.url ?? '/';
        if (path !== '/api/v1' && !path.startsWith('/api/v1/')) return next();
        let response: Response;
        try {
          const body = await readBody(incoming);
          response = await handle(new Request(new URL(path, 'http://localhost'), {
            method: incoming.method,
            headers: incoming.headers as Record<string, string>,
            body,
          }));
        } catch (error) {
          if (error instanceof RangeError) {
            response = Response.json({ error: { code: 'body_too_large', message: error.message } }, { status: 413 });
          } else {
            server.config.logger.error(`Ошибка заглушки API. ${String(error)}`);
            response = Response.json({
              error: { code: 'internal', message: 'Заглушка не смогла выполнить запрос.' },
            }, { status: 500 });
          }
        }
        outgoing.statusCode = response.status;
        for (const [key, value] of response.headers) outgoing.setHeader(key, value);
        outgoing.end(Buffer.from(await response.arrayBuffer()));
      });
    },
  };
}
