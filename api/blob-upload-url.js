import { issueSignedToken, presignUrl } from '@vercel/blob';

const MAX_BYTES = 100 * 1024 * 1024;
const ALLOWED_TYPES = new Set([
  'application/pdf',
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/bmp',
  'image/gif',
  'image/tiff',
]);

function safeName(name) {
  return String(name || 'file')
    .replace(/\\/g, '/')
    .split('/')
    .pop()
    .replace(/[^\w.\- ()]/g, '_')
    .slice(0, 180) || 'file';
}

export default async function handler(request) {
  if (request.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed.' }), {
      status: 405,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  try {
    const body = await request.json();
    const size = Number(body?.size || 0);
    const contentType = String(body?.contentType || 'application/octet-stream');
    const filename = safeName(body?.filename);

    if (!Number.isFinite(size) || size <= 0 || size > MAX_BYTES) {
      return new Response(JSON.stringify({ error: 'Compression files must be between 1 byte and 100 MB.' }), {
        status: 413,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    if (!ALLOWED_TYPES.has(contentType)) {
      return new Response(JSON.stringify({ error: 'Only PDF and common image files can use large-file compression.' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const pathname = `paperpilot/inputs/${crypto.randomUUID()}-${filename}`;
    const token = await issueSignedToken({ operations: ['put'] });
    const { presignedUrl } = await presignUrl(token, {
      pathname,
      operation: 'put',
      validUntil: Date.now() + 15 * 60 * 1000,
    });

    return new Response(JSON.stringify({ url: presignedUrl, pathname }), {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });
  } catch (error) {
    return new Response(JSON.stringify({ error: error?.message || 'Could not prepare upload.' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
