/**
 * The days a Supreme Court case was argued that the site can play, for the
 * argument player at /cases/<key>/argument/<date> (Trevor, Nov. 27, 2026: the
 * Justices and counsel on the left, the transcript on the right; the design
 * he approved on Nov. 28, 2026 is design/argument-audio/transcript-player/round-3-approved/).
 *
 * Built with the site from the case record: its argument_timing entries
 * (scripts/caselaw/supreme_court_argument_timing.py), each naming the
 * recording's files in playing order, where the argument lies in them, who
 * argued and when each began, and the stored file with the timed transcript;
 * its argument_audio entries, for each file's size, SHA-256 and original
 * address; and its argument_transcripts entries, for the Court's PDF. The
 * bench is the Justices serving on the day argued, the Chief Justice first
 * and then in order of seniority (content/supreme-court/justices.yaml).
 *
 * Runs at build time only (it reads the repository).
 */
import { readableDate } from '../readable-date.ts';
import { copyPath } from '../public-copies.ts';
import { verifyLink } from '../checksums.ts';
import { nationNamesAndKinds, servableImages, type SourceRecord } from './content.ts';
import { publicCopyByKey } from './record-document.ts';
import { justicesFile, surname } from './supreme-courts.ts';
import { justicePath, personOf } from './supreme-court-people.ts';
import { pictureOf } from './supreme-court-pictures.ts';
import { displayName } from '../nation-labels.ts';
import type { ArgumentDay, ArgumentParty } from '../case-arguments.ts';

interface StoredFile {
	storage_key: string;
	checksum: string;
	bytes: number;
	media_type?: string;
}

interface TimingEntry {
	date: string;
	made: 'official transcript' | 'speech recognition';
	model: string;
	turns?: number;
	recording: { key: string; seconds: number }[];
	span: [number, number];
	counsel?: { name: string; title?: string; for?: string; source?: string }[];
	sections?: { label: string; counsel: string; t: number }[];
	file: StoredFile;
}

interface AudioEntry {
	kind: string;
	date: string;
	source: string;
	link: string;
	duration_seconds?: number;
	file?: StoredFile;
}

interface TranscriptEntry {
	date: string;
	docket: string;
	link: string;
	file?: StoredFile;
}

export interface ArgumentParts {
	argument_timing?: TimingEntry[];
	argument_audio?: AudioEntry[];
	argument_transcripts?: TranscriptEntry[];
	tribes?: string[];
}

/** The path of an argument day's page. */
export function argumentPath(caseKey: string, date: string): string {
	return `/cases/${caseKey}/argument/${date}`;
}

/** The Justices serving on a day: the Chief Justice first, then the Associate Justices in order of their oaths. */
function benchOn(day: string): ArgumentDay['bench'] {
	const serving = justicesFile().justices.filter((appointment) => appointment.oath <= day && (appointment.left ?? '9999-12-31') >= day);
	serving.sort((one, other) => Number(other.role === 'Chief Justice') - Number(one.role === 'Chief Justice') || one.oath.localeCompare(other.oath));
	return serving.map((appointment) => {
		const person = personOf(appointment);
		return {
			name: appointment.name,
			surname: surname(appointment.name),
			link: justicePath(person),
			...(pictureOf(person.key) ? { portrait: `/supreme-court/portraits/${person.key}` } : {}),
			...(appointment.role === 'Chief Justice' ? { chief: true } : {})
		};
	});
}

const generic = /^(?:the\s+)?(petitioners?|respondents?|appellants?|appellees?|complainants?|plaintiffs?|defendants?)\b/i;

/** The case's two sides as its name gives them: "Williams v. Lee" is Williams and Lee. */
function sidesOf(title: string): [string, string] {
	const [first, ...rest] = title.split(/\s+v\.\s+/);
	return [first.trim(), rest.join(' v. ').trim()];
}

/** The parties a counsel argued for, each with the nation's seal where the party is one of the case's nations. */
function partiesFor(behalf: string, title: string, tribes: string[], docket: string | undefined): ArgumentParty[] {
	const [first, second] = sidesOf(title);
	const names: string[] = [];
	const role = generic.exec(behalf)?.[1]?.toLowerCase();
	// "the Petitioners in Case No. 20-544": a case heard with this one, whose parties the name does not give.
	const other = /Case No\.?\s*([\w-]+)/i.exec(behalf)?.[1];
	if (role && other && docket && other !== docket) return [];
	// "the Respondent United States", "the Respondents John W. Thompson, et al.": the party named after the role.
	const named = role ? behalf.replace(generic, '').replace(/,?\s*et al\.?$/i, '').trim() : '';
	if (/United States/.test(behalf) && !/^the State/i.test(behalf)) names.push('United States');
	else if (/^[A-Z]/.test(named) && !/^(?:in|on|of|for)\b/i.test(named)) names.push(named);
	else if (role) names.push(/^(petitioner|appellant|complainant|plaintiff)/.test(role) ? first : second);
	const nations = nationNamesAndKinds();
	const seals = new Set(servableImages().map((image) => image.nation));
	const plain = (text: string) => text.toLowerCase().replace(/[^a-z ]/g, '').replace(/\s+/g, ' ').trim();
	const candidates = role || names.length ? names : [behalf];
	return candidates
		.filter(Boolean)
		.map((name) => {
			const nation = tribes.find((id) => {
				const known = nations.get(id);
				if (!known) return false;
				const one = plain(displayName(known.name));
				const other = plain(name);
				return one.length > 3 && (other.includes(one) || one.includes(other));
			});
			return { name, ...(nation && seals.has(nation) ? { image: `/nations/${nation}/image` } : {}) };
		})
		// A party named the same as the counsel's "for" line adds nothing unless it carries a seal.
		.filter((party) => role || names.length || party.image);
}

/** The argument days the site can play, each with everything the player draws. */
export function argumentDays(record: SourceRecord & ArgumentParts): ArgumentDay[] {
	const audio = new Map((record.argument_audio ?? []).filter((entry) => entry.file).map((entry) => [entry.file!.storage_key, entry]));
	const title = record.citation.title ?? '';
	return (record.argument_timing ?? [])
		.filter((entry) => entry.recording.every((file) => audio.has(file.key)))
		.map((entry): ArgumentDay => {
			let offset = 0;
			const parts = entry.recording.map(({ key, seconds }, index) => {
				const found = audio.get(key)!;
				const part = {
					number: index + 1,
					key,
					link: found.link,
					sha256: found.file!.checksum,
					bytes: found.file!.bytes,
					offset,
					// The length as the speech recognition pass decoded it, which the transcript's times count from.
					duration: seconds,
					verify: verifyLink(found.file!.checksum, key.split('/').at(-1))
				};
				offset += part.duration;
				return part;
			});
			const docket = (record.argument_transcripts ?? [])[0]?.docket.split(/[\s,]+/)[0];
			const source = audio.get(entry.recording[0].key)!.source.replace(/^Supreme Court of the United States$/, 'Supreme Court');
			// The Court's transcripts of the day, also where the player shows our own because the scan gave no turns.
			const transcripts = (record.argument_transcripts ?? [])
				.filter((transcript) => transcript.file && Math.abs(Date.parse(transcript.date) - Date.parse(entry.date)) <= 4 * 86_400_000)
				.flatMap((transcript) => {
					const copy = publicCopyByKey(transcript.file!.storage_key);
					return copy ? [{ link: copyPath(copy), docket: transcript.docket, verify: verifyLink(copy.sha256, copy.key.split('/').at(-1)) }] : [];
				});
			const sections = entry.sections ?? [];
			return {
				date: entry.date,
				path: argumentPath(record.key, entry.date),
				when: readableDate(entry.date),
				made: entry.made,
				model: entry.model,
				timed: { key: entry.file.storage_key, sha256: entry.file.checksum, bytes: entry.file.bytes },
				source,
				parts,
				total: offset,
				span: entry.span,
				bench: benchOn(entry.date),
				counsel: (entry.counsel ?? []).map((counsel) => ({
					name: counsel.name,
					...(counsel.title ? { title: counsel.title } : {}),
					...(counsel.for ? { for: counsel.for } : {}),
					...(counsel.source ? { source: counsel.source } : {}),
					parties: counsel.for ? partiesFor(counsel.for, title, record.tribes ?? [], docket) : [],
					times: sections.filter((section) => section.counsel === counsel.name).map((section) => section.t)
				})),
				sections: sections.map((section) => ({ label: section.label, t: section.t })),
				transcripts
			};
		});
}
