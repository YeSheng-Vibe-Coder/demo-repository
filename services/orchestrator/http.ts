// Loopback business-API client on node:http. Built-in fetch (undici) aborts any response
// whose headers take longer than 300 s, below our step budgets under Zetaris load, so the
// only timeout here is the caller's.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
// Loopback only: the business API never listens beyond 127.0.0.1.
export const API_BASE = 'http://127.0.0.1:8010';

export function apiToken(): string {
  return process.env.AML_INTERNAL_TOKEN || fs.readFileSync(path.join(root, '.runtime/api-token'), 'utf8').trim();
}

export function apiClient(base: string, token: string, timeoutMs: number) {
  return function request<T>(route: string, body?: object): Promise<T> {
    const payload = body ? JSON.stringify(body) : undefined;
    return new Promise<T>((resolve, reject) => {
      const req = http.request(base + route, {
        method: body ? 'POST' : 'GET',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {}),
        },
      }, response => {
        const chunks: Buffer[] = [];
        response.on('data', chunk => chunks.push(chunk));
        response.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if (!response.statusCode || response.statusCode >= 400) {
            reject(new Error(`Business API ${response.statusCode}: ${text}`));
            return;
          }
          try { resolve(JSON.parse(text) as T); } catch (error) { reject(error); }
        });
        response.on('error', reject);
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error(`Business API timeout after ${timeoutMs} ms: ${route}`)));
      req.on('error', reject);
      if (payload) req.write(payload);
      req.end();
    });
  };
}
