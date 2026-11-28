/**
 * The argument player's data (src/lib/server/case-arguments.ts builds it with
 * the site; the timed transcript is read from the bucket when a reader opens
 * the page) and the small helpers the page and its tests share.
 */

export interface ArgumentParty {
	name: string;
	/** The nation's seal, where the party is one of the case's nations and the site may show its image. */
	image?: string;
}

export interface ArgumentCounsel {
	name: string;
	/** Office or city, as the transcript's appearances give it. */
	title?: string;
	/** Whom the counsel argued for, as the source says it. */
	for?: string;
	/** The report naming the counsel, where there is no transcript. */
	source?: string;
	parties: ArgumentParty[];
	/** When each of the counsel's turns at the lectern begins, in seconds: the argument, then any rebuttal. */
	times: number[];
}

export interface ArgumentPart {
	number: number;
	key: string;
	/** The file at the Court or the National Archives. */
	link: string;
	sha256: string;
	bytes: number;
	/** Where the part starts in the whole recording, in seconds. */
	offset: number;
	duration: number;
	verify: string;
}

export interface ArgumentDay {
	date: string;
	path: string;
	/** "Nov. 9, 2022". */
	when: string;
	made: 'official transcript' | 'speech recognition';
	model: string;
	timed: { key: string; sha256: string; bytes: number };
	/** "Supreme Court" or "National Archives". */
	source: string;
	parts: ArgumentPart[];
	/** The whole recording's length, in seconds. */
	total: number;
	/** Where the case lies in the recording: all of it, or part of a whole day's reels. */
	span: [number, number];
	bench: { name: string; surname: string; link: string; portrait?: string; chief?: boolean }[];
	counsel: ArgumentCounsel[];
	sections: { label: string; t: number }[];
	/** The Court's transcripts, served as PDFs. */
	transcripts: { link: string; docket: string; verify: string }[];
}

/** One turn of the timed transcript, or one paragraph of our own. */
export interface TimedTurn {
	t: number;
	/** "Justice Sotomayor", "Mr. McGill", "A Justice"; empty where the speakers are not marked. */
	speaker: string;
	role: 'justice' | 'counsel' | 'unknown';
	/** The transcript's page. */
	page?: number;
	text: string;
	/** The counsel's place in the counsel list. */
	counsel?: number;
	/** The recording does not hold this turn: it begins or ends partway through the argument. */
	missing?: boolean;
}

export interface TimedTranscript {
	case: string;
	date: string;
	made: 'official' | 'speech recognition';
	model: string;
	sections: { label: string; counsel: string; t: number; turn: number; missing?: boolean }[];
	turns: TimedTurn[];
}

/** 3:12:20, 47:51, 0:09. */
export function clock(seconds: number): string {
	const whole = Math.max(0, Math.floor(seconds));
	const hours = Math.floor(whole / 3600);
	const minutes = Math.floor((whole % 3600) / 60);
	const rest = String(whole % 60).padStart(2, '0');
	return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
}

/** Two initials for a person with no picture, the first two as the approved design draws them: "Matthew D. McGill" is MD. */
export function initials(name: string): string {
	return name
		.replace(/,.*$/, '')
		.replace(/^(?:Mr|Ms|Mrs|Miss)\.\s+/, '')
		.split(/\s+/)
		.filter((word) => /^[A-Z]/.test(word) && !/^(?:Jr|Sr|II|III)\.?$/.test(word))
		.map((word) => word[0])
		.slice(0, 2)
		.join('');
}

/** The turn being spoken at a moment: the last that began at or before it. */
export function turnAt(turns: Pick<TimedTurn, 't'>[], seconds: number): number {
	let low = 0;
	let high = turns.length - 1;
	let found = -1;
	while (low <= high) {
		const middle = (low + high) >> 1;
		if (turns[middle].t <= seconds) {
			found = middle;
			low = middle + 1;
		} else high = middle - 1;
	}
	return found;
}

/** The part of a recording that holds a moment, and the moment within it. */
export function partAt(parts: Pick<ArgumentPart, 'offset' | 'duration'>[], seconds: number): { index: number; within: number } {
	for (let index = parts.length - 1; index >= 0; index -= 1) {
		if (seconds >= parts[index].offset) return { index, within: Math.min(seconds - parts[index].offset, parts[index].duration) };
	}
	return { index: 0, within: 0 };
}
