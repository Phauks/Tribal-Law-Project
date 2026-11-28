import { readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { beforeAll, describe, expect, it, vi } from 'vitest';

// Story credits format citations through a module the site build makes; the index doesn't need it.
vi.mock('virtual:source-citations', () => ({ sourceCitations: {} }));
import { collections } from '#lib/server/collections.ts';
import { nations } from '#lib/server/content.ts';
import { nationIndexRows } from '#lib/server/nation-index.ts';
import { collectionSearch, itemLink, namedItemLink, searchIndex, searchNamesFile, sitePages } from '#lib/server/search-index.ts';
import { searchRecords } from '#lib/server/search-records.ts';
import { tribalCourtOpinions } from '#lib/server/tribal-court-opinions.ts';
import { expandRecords, type SearchRecords } from '#lib/search-records.ts';
import { matchEverything, resultGroups, type SearchMatches } from '#lib/search-everything.ts';
import type { SearchEntry } from '#lib/site-search.ts';

// Trevor asked on Nov. 26, 2026 that search cover every kind of material on the
// site, now and as collections are added, and know the names people use for things.

let entries: SearchEntry[];
let records: SearchRecords;
let links: Set<string>;

beforeAll(() => {
	entries = searchIndex();
	records = expandRecords(searchRecords());
	links = new Set([...entries.map((entry) => entry.link), ...records.documents.map((row) => row.link)]);
	// Reading every record the first time takes a while.
}, 180_000);

/** Every link a search for these words finds, in any group. */
function found(query: string): string[] {
	const matches: SearchMatches = matchEverything(entries, records, query);
	return resultGroups.flatMap((group) => (matches[group] as { link?: string; result?: { link: string } }[]).map((match) => match.link ?? match.result?.link ?? ''));
}

const titledOnlyByFile = (title: string, href?: string) => title === href?.replace(/^\/copies\/view\//, '');

describe('every collection is searchable', () => {
	it('gives every collection a group of results', () => {
		for (const collection of collections) expect(resultGroups, collection.identifier).toContain(collection.search.group);
	});

	it('reaches every item of every collection', () => {
		const { reached } = collectionSearch();
		for (const collection of collections) {
			const missing = collection
				.items()
				.filter((item) => item.href && !titledOnlyByFile(item.title, item.href))
				.filter((item) => {
					const link = itemLink(item) ?? '';
					return !links.has(link) && !reached.has(item.href ?? '');
				})
				.map((item) => item.title);
			expect(missing, collection.identifier).toEqual([]);
		}
	});

	it('finds items of every collection by their names', () => {
		for (const collection of collections) {
			// Items listed under this collection, not those found through another one's entry (an opinion's copy is listed as the opinion).
			const byLink = new Map(entries.map((entry) => [entry.link, [entry.title, ...(entry.also ?? [])]]));
			for (const row of records.documents) byLink.set(row.link, [...(byLink.get(row.link) ?? []), row.title]);
			const items = collection.items().filter((item) => item.title.trim().length > 2 && byLink.get(itemLink(item) ?? '')?.includes(item.title));
			if (collection.items().length === 0) continue;
			expect(items.length, collection.identifier).toBeGreaterThan(0);
			const sample = [items[0], items[Math.floor(items.length / 2)], items.at(-1)!];
			for (const item of sample) expect(found(item.title), `${collection.identifier}: ${item.title}`).toContain(itemLink(item));
		}
	});
});

/** The pages under src/routes, as paths, with each [parameter] kept. */
function routes(folder = path.resolve('src/routes'), prefix = ''): string[] {
	const found: string[] = [];
	for (const name of readdirSync(folder)) {
		const full = path.join(folder, name);
		if (!statSync(full).isDirectory()) continue;
		const segment = name.startsWith('(') ? '' : `/${name}`;
		found.push(...routes(full, `${prefix}${segment}`));
	}
	if (readdirSync(folder).includes('+page.svelte')) found.push(prefix || '/');
	return found;
}

/**
 * Pages search leaves out, and why. A new page fails the test below until it
 * has an entry in the index (src/lib/server/search-index.ts) or a line here.
 */
const notSearched: Record<string, string> = {
	'/': 'the home page',
	'/search': 'search itself',
	'/requests': 'where Request a Change sends its form; a visit goes on to the dialog',
	'/workspace': 'for signed-in editors only',
	'/profile': 'a reader’s own account',
	'/account': 'a reader’s own account',
	'/account-deleted': 'the page after deleting an account',
	'/uploads': 'a reader’s own uploads',
	'/sign-in': 'signing in',
	'/sign-up': 'signing up',
	'/settings': 'a reader’s own settings, opened from the header',
	'/mail': 'links in emails',
	'/rooms': 'video meetings, opened from an event',
	'/orders': 'a reader’s own orders',
	'/sponsorship/invite': 'invitations sent by email',
	'/restricted-pages': 'pages drawn again for nations’ restrictions',
	'/starred/shared': 'a collection someone shared by link',
	'/following/email-off': 'a link in an email',
	'/changes/compare': 'comparing two versions, opened from a page’s history',
	'/changes/[id]': 'one change, opened from Recent changes',
	'/[...address]/changes': 'a page’s own list of changes, opened from the page',
	'/licenses/changes': 'as other pages’ lists of changes',
	'/essays/[slug]': 'whether an essay is published lives in the database, which the build cannot read; Essays lists them',
	'/foundations/[slug]': 'as essays',
	'/back-matter/[slug]': 'as essays',
	'/news/[slug]': 'as essays; News lists them',
	'/contributors/[slug]': 'contributors choose whether to be listed, in the database; Contributors lists them',
	'/sponsors/[slug]': 'sponsors are kept in the database; Sponsorship lists them',
	'/events/[slug]': 'events are kept in the database; Events lists them',
	'/licenses/[id]': 'each license is reached from Licenses by material',
	'/copies/list/[group]': 'pages of the list of source documents, each of which is searched',
	'/nations/letter/[letter]': 'the nations by first letter, each of which is searched',
	'/nations/state/[state]': 'the nations of a state, each of which is searched',
	'/nations/[id]/opinions': 'a nation’s opinions, each of which is searched',
	'/nations/data/[chart]/[bar]': 'the records behind one bar of a chart',
	'/nations/data/[chart]/sources': 'the sources of one chart',
	'/forms/[form]/print': 'a form’s print view',
	'/cases/[id]/argument/[date]': 'a case’s oral argument, opened from the case page, which is searched'
};

function isLeftOut(route: string): boolean {
	return Object.keys(notSearched).some((prefix) => route === prefix || route.startsWith(`${prefix}/`));
}

/** A route as a pattern that matches its pages' links. */
function routePattern(route: string): RegExp {
	const source = route
		.split('/')
		.map((segment) => (segment.startsWith('[...') ? '.+' : segment.startsWith('[') ? '[^/#?]+' : segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
		.join('/');
	return new RegExp(`^${source}(?:[#?].*)?$`);
}

describe('every page is searchable or says why not', () => {
	it('has an entry for each page, or a reason to leave it out', () => {
		const indexLinks = [...links];
		const missing = routes()
			.filter((route) => !isLeftOut(route))
			.filter((route) => !indexLinks.some((link) => routePattern(route).test(link)));
		expect(missing).toEqual([]);
	});

	it('lists each of the site’s own pages once', () => {
		const pageLinks = sitePages.map((page) => page.link);
		expect(new Set(pageLinks).size).toBe(pageLinks.length);
	});
});

describe('other names and sets', () => {
	const file = searchNamesFile();
	const opinionLinks = () => new Set(tribalCourtOpinions().map((opinion) => opinion.path));

	it('names only things search lists, and never a Tribal court opinion', () => {
		const opinions = opinionLinks();
		const items = [...file.names.map((name) => name.item), ...file.groups.flatMap((group) => group.items)];
		for (const item of items) {
			const link = namedItemLink(item);
			expect(link, item).toBeTruthy();
			expect(links.has(link ?? ''), item).toBe(true);
			expect(opinions.has(link ?? ''), item).toBe(false);
			expect(item.startsWith('/copies/'), item).toBe(false);
		}
	});

	it('gives every name a basis in a published source', () => {
		for (const entry of [...file.names, ...file.groups]) {
			expect(entry.basis.citation, entry.names[0]).toBeTruthy();
			expect(entry.basis.link, entry.names[0]).toMatch(/^https:\/\//);
			expect(entry.basis.words, entry.names[0]).toBeTruthy();
		}
	});

	it('finds the Marshall Trilogy', () => {
		expect(found('marshall trilogy').sort()).toEqual(['/cases/cherokee-nation-v-georgia', '/cases/johnson-v-mintosh', '/cases/worcester-v-georgia']);
	});

	it('finds the landmark cases', () => {
		const landmark = file.groups.find((group) => group.names.includes('Landmark Native American cases'));
		const expected = (landmark?.items ?? []).map(namedItemLink);
		expect(expected.length).toBeGreaterThan(40);
		expect(found('landmark native american cases').sort()).toEqual([...expected].sort());
	});

	it('finds laws by their acronyms', () => {
		expect(found('ICWA')).toContain('/laws/indian-child-welfare-act-1978');
		expect(found('IGRA')).toContain('/laws/indian-gaming-regulatory-act-1988');
		expect(found('ISDEAA')).toContain('/laws/indian-self-determination-and-education-assistance-act-1975');
		expect(found('Dawes Act')).toContain('/laws/general-allotment-act-1887');
	});
});

describe('nations', () => {
	it('finds each nation by every name its page shows', () => {
		const byId = new Map(nations().map((record) => [record.id, record]));
		const byLink = new Map(entries.filter((entry) => entry.kind === 'nation').map((entry) => [entry.link, entry]));
		for (const row of nationIndexRows()) {
			const entry = byLink.get(`/nations/${row.id}`);
			expect(entry, row.id).toBeDefined();
			const names = new Set([entry!.title, ...(entry!.also ?? [])]);
			const record = byId.get(row.id);
			const shown = [row.listedName, ...(record?.names.self_designation ?? []).map((name) => name.name), ...(record?.names.alternate ?? []).map((name) => name.name), ...(record?.names.former ?? []).map((name) => name.name)];
			for (const name of shown) expect(names.has(name), `${row.id}: ${name}`).toBe(true);
		}
	});
});

describe('opinions published with initials', () => {
	it('are found only by the name the site shows, never another', () => {
		const shown = new Map(tribalCourtOpinions().filter((opinion) => opinion.namesReplaced).map((opinion) => [opinion.path, opinion.name]));
		expect(shown.size).toBeGreaterThan(0);
		for (const entry of entries.filter((candidate) => shown.has(candidate.link))) {
			expect(entry.title).toBe(shown.get(entry.link));
			expect(entry.also ?? []).toEqual([]);
			expect(entry.groups ?? []).toEqual([]);
		}
	});
});
