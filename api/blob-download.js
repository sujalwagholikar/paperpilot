import { issueSignedToken, presignUrl } from '@vercel/blob';

export default async function handler(request) {
  const url = new URL(request.url);
  const pathname = url.searchParams.get('pathname') || '';

  if (!pathname.startsWith('paperpilot/outputs/')) {
    return new Response('Invalid output path.', { status: 400 });
  }

  try {
    const token = await issueSignedToken({ operations: ['get'] });
    const { presignedUrl } = await presignUrl(token, {
      pathname,
      operation: 'get',
      validUntil: Date.now() + 15 * 60 * 1000,
    });
    return Response.redirect(presignedUrl, 302);
  } catch (error) {
    return new Response(error?.message || 'Could not create download URL.', { status: 500 });
  }
}
