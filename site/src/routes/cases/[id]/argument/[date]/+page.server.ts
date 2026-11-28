/**
 * The argument player for one day a case was argued (src/lib/server/case-arguments.ts;
 * the design Trevor approved on Nov. 28, 2026, design/argument-audio/transcript-player/round-3-approved/).
 *
 * The day's players data comes with the case's built data; the timed
 * transcript is read from the bucket when a reader opens the page, and only
 * the file the built data names, byte for byte, so the page is drawn whole
 * for a reader without scripts too.
 */
import { error } from '@sveltejs/kit';
import type { CasePage } from '#lib/server/case-pages.ts';
import type { TimedTranscript } from '#lib/case-arguments.ts';
import { builtPageData } from '#lib/server/page-data.ts';
import { workerEnvironment } from '#lib/server/environment.ts';
import { hexDigest } from '#lib/server/public-copy-serving.ts';
import type { PageServerLoad } from './$types';

export const prerender = false;

export const load: PageServerLoad = async ({ params, url, setHeaders }) => {
	const page = await builtPageData<CasePage>('cases', params.id, url);
	const day = page.arguments?.find((argument) => argument.date === params.date);
	if (!day) error(404, 'This site has no recording of the case argued on that day.');
	const bucket = workerEnvironment().sourceDocuments;
	if (!bucket) error(503, 'The transcript cannot be read right now.');
	const stored = await bucket.get(day.timed.key);
	const bytes = stored ? await stored.arrayBuffer() : undefined;
	if (!bytes || bytes.byteLength !== day.timed.bytes || (await hexDigest(bytes)) !== day.timed.sha256) error(503, 'The transcript cannot be read right now.');
	const transcript = JSON.parse(new TextDecoder().decode(bytes)) as TimedTranscript;
	setHeaders({ 'cache-control': 'public, max-age=3600' });
	return {
		key: page.key,
		name: page.name,
		court: page.court,
		day,
		others: (page.arguments ?? []).filter((argument) => argument.date !== day.date).map((argument) => ({ date: argument.date, when: argument.when, path: argument.path })),
		sections: transcript.sections,
		turns: transcript.turns
	};
};
