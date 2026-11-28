/**
 * One file of an argument's recording, for the argument player
 * (src/lib/server/case-arguments.ts): only a file the case's built data names
 * for that day, from the bucket, with byte ranges so the player can seek. The
 * recordings are the Court's and the National Archives' own, which no one
 * may restrict.
 */
import { error } from '@sveltejs/kit';
import type { CasePage } from '#lib/server/case-pages.ts';
import { builtPageData } from '#lib/server/page-data.ts';
import { workerEnvironment } from '#lib/server/environment.ts';
import { servePublicCopy } from '#lib/server/public-copy-serving.ts';
import type { RequestHandler } from './$types';

export const prerender = false;

export const GET: RequestHandler = async ({ params, request, url }) => {
	const page = await builtPageData<CasePage>('cases', params.id, url);
	const day = page.arguments?.find((argument) => argument.date === params.date);
	const part = day?.parts.find((candidate) => String(candidate.number) === params.part.replace(/\.mp3$/, ''));
	if (!day || !part) error(404, 'This site keeps no recording at this address.');
	const copy = {
		key: part.key,
		mediaType: 'audio/mpeg',
		sha256: part.sha256,
		bytes: part.bytes,
		links: [part.link],
		name: `${page.name} argued ${day.date}${day.parts.length > 1 ? ` part ${part.number}` : ''}.mp3`.replace(/[^A-Za-z0-9 ._-]/g, '')
	};
	const bucket = workerEnvironment().sourceDocuments;
	if (!bucket) return new Response(null, { status: 302, headers: { Location: part.link, 'Cache-Control': 'no-store' } });
	return servePublicCopy(request, copy, bucket);
};
