import { describe, expect, it, vi } from 'vitest';
import { clock, initials, partAt, turnAt } from '#lib/case-arguments.ts';
import { argumentDays } from '#lib/server/case-arguments.ts';
import { withArgumentLine } from '#lib/server/case-pages.ts';
import { sources } from '#lib/server/content.ts';

vi.mock('virtual:source-citations', () => ({ sourceCitations: {} }));
vi.mock('virtual:essay-library', () => ({ essaysInGit: {} }));
vi.mock('$app/env', () => ({ version: 'test-build' }));
vi.mock('cloudflare:workers', () => ({ env: {}, waitUntil: () => undefined }));

describe('argument player helpers', () => {
	it('writes times as the player shows them', () => {
		expect(clock(9.6)).toBe('0:09');
		expect(clock(47 * 60 + 51)).toBe('47:51');
		expect(clock(3 * 3600 + 12 * 60 + 20)).toBe('3:12:20');
	});

	it('takes the first two initials, as the approved design draws them', () => {
		expect(initials('Matthew D. McGill')).toBe('MD');
		expect(initials('Judd E. Stone, II')).toBe('JE');
		expect(initials('Mr. Littell')).toBe('L');
	});

	it('finds the turn being spoken', () => {
		const turns = [{ t: 0.9 }, { t: 12.8 }, { t: 104 }];
		expect(turnAt(turns, 0)).toBe(-1);
		expect(turnAt(turns, 12.8)).toBe(1);
		expect(turnAt(turns, 500)).toBe(2);
	});

	it('finds the part of a recording that holds a moment', () => {
		const parts = [
			{ offset: 0, duration: 5664 },
			{ offset: 5664, duration: 1242 }
		];
		expect(partAt(parts, 100)).toEqual({ index: 0, within: 100 });
		expect(partAt(parts, 6000)).toEqual({ index: 1, within: 336 });
	});
});

describe('argument days from the records', () => {
	const withTiming = [...sources().values()].filter((record) => (record as { argument_timing?: unknown[] }).argument_timing?.length);

	it('gives each timed argument its page, recording and bench', () => {
		for (const record of withTiming) {
			const days = argumentDays(record);
			expect(days.length).toBeGreaterThan(0);
			for (const day of days) {
				expect(day.path).toBe(`/cases/${record.key}/argument/${day.date}`);
				expect(day.parts.length).toBeGreaterThan(0);
				expect(day.total).toBeGreaterThan(0);
				// Nine seats, or fewer during a vacancy; the Chief Justice first.
				expect(day.bench.length).toBeGreaterThanOrEqual(7);
				expect(day.bench.length).toBeLessThanOrEqual(9);
				expect(day.bench[0].chief).toBe(true);
				expect(day.span[0]).toBeLessThan(day.span[1]);
			}
		}
	});

	it('links each playable day from Key facts, after the days argued', () => {
		const record = withTiming[0];
		if (!record) return;
		const days = argumentDays(record);
		const facts = withArgumentLine([['Argued', 'December 1, 2015'], ['Decided', 'January 25, 2016']], days);
		expect(facts.map(([label]) => label)).toEqual(['Argued', 'Oral argument', 'Decided']);
		expect(facts[1][1]).toContain(`href="/cases/${record.key}/argument/`);
	});
});
