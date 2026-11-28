<!--
	@component
	The argument player (src/lib/server/case-arguments.ts): one panel with the
	player along its top, the Justices and counsel down the left and the
	transcript on the right, the turn being spoken lit and the speaker ringed
	(Trevor approved round 3 on Nov. 28, 2026:
	design/argument-audio/transcript-player/round-3-approved/). Clicking a turn
	plays from it ("allow clicking in the transcript to skip to that part of the
	audio"); where the transcript is our own, the model that made it is named
	beside the Transcript heading ("state the model used . . . next to the
	TRANSCRIPT text so it doesn't clog the main area").
-->
<script lang="ts">
	import { onMount, tick } from 'svelte';
	import ArrowDown from '@lucide/svelte/icons/arrow-down';
	import Download from '@lucide/svelte/icons/download';
	import Info from '@lucide/svelte/icons/info';
	import Mic from '@lucide/svelte/icons/mic';
	import Pause from '@lucide/svelte/icons/pause';
	import Play from '@lucide/svelte/icons/play';
	import RotateCcw from '@lucide/svelte/icons/rotate-ccw';
	import Search from '@lucide/svelte/icons/search';
	import ShieldCheck from '@lucide/svelte/icons/shield-check';
	import Breadcrumbs from '#lib/components/Breadcrumbs.svelte';
	import { clock, initials, partAt, turnAt, type TimedTurn } from '#lib/case-arguments.ts';
	import type { PageProps } from './$types';

	let { data }: PageProps = $props();
	const day = $derived(data.day);
	const trail = $derived([
		{ label: 'Courts and cases', href: '/cases' },
		...(data.court ? [{ label: 'Supreme Court', href: '/supreme-court' }, { label: data.court.name, href: data.court.link }] : []),
		{ label: data.name, href: `/cases/${data.key}` }
	]);
	const own = $derived(day.made === 'speech recognition');
	const ownReason = $derived(
		day.transcripts.length
			? 'The Court’s transcript of this argument is a scan whose speakers could not be read; it can be downloaded as a PDF.'
			: day.date < '1968-10-07'
				? 'The Court kept no official transcript before 1968.'
				: 'The Court’s lists have no transcript of this argument.'
	);
	const ownNote = $derived(
		`${ownReason} The project made this one from the recording with the ${day.model} speech recognition model. It has not been checked against the recording and has errors; speakers are not marked.`
	);
	const partial = $derived(day.span[0] > 30 || day.span[1] < day.total - 30);
	// Turns of the transcript that the recording does not hold: it begins or ends partway through the argument.
	const heard = $derived(data.turns.flatMap((turn, index) => (turn.missing ? [] : [index])));
	const firstHeard = $derived(heard[0] ?? -1);
	const lastHeard = $derived(heard.at(-1) ?? -1);
	const beginsLate = $derived(firstHeard > 0);
	const endsEarly = $derived(lastHeard >= 0 && lastHeard < data.turns.length - 1);
	const unnamedJustices = $derived(data.turns.some((turn) => turn.speaker === 'A Justice'));
	const speeds = [1, 1.25, 1.5, 2, 0.75];

	// The track's marks: each counsel's argument, and the earlier cases of a whole day's reels.
	const marks = $derived([
		...(day.span[0] > 60 ? [{ label: 'Earlier cases that day', t: 0 }] : []),
		...day.sections.map((section) => ({ label: section.label, t: section.t }))
	]);
	const headings = $derived(new Map(data.sections.map((section) => [section.turn, section])));
	const counselHeading = (name: string) => {
		const counsel = day.counsel.find((entry) => entry.name === name);
		return counsel?.for ? `, for ${counsel.for}` : '';
	};

	let audio: HTMLAudioElement | undefined = $state();
	let body: HTMLElement | undefined = $state();
	let partIndex = $state(0);
	let now = $state(0);
	let playing = $state(false);
	let speed = $state(1);
	let follow = $state(true);
	let query = $state('');
	let ready = $state(false);
	let problem = $state(false);

	const current = $derived.by(() => {
		const found = turnAt(heard.map((index) => data.turns[index]), now);
		return found < 0 ? -1 : heard[found];
	});
	const speaking = $derived<TimedTurn | undefined>(data.turns[current]);
	const benchSpeaking = $derived(
		speaking?.role === 'justice' ? day.bench.find((justice) => speaking.speaker.endsWith(` ${justice.surname}`)) : undefined
	);
	// The counsel at the lectern: the one whose argument the current moment falls in.
	const lectern = $derived.by(() => {
		let at: string | undefined;
		for (const section of data.sections) if (section.t <= now) at = section.counsel;
		return at;
	});
	const counselSpeaking = $derived(speaking?.role === 'counsel' && speaking.counsel !== undefined ? day.counsel[speaking.counsel]?.name : undefined);
	const wanted = $derived(query.trim().toLowerCase());
	const shown = $derived(data.turns.map((turn) => !wanted || turn.text.toLowerCase().includes(wanted) || turn.speaker.toLowerCase().includes(wanted)));
	const matches = $derived(shown.filter(Boolean).length);

	const partUrl = (index: number) => `${day.path}/audio/${day.parts[index].number}.mp3`;

	function justiceOf(turn: TimedTurn) {
		return turn.role === 'justice' ? day.bench.find((justice) => turn.speaker.endsWith(` ${justice.surname}`)) : undefined;
	}

	async function load(index: number, within: number, play: boolean) {
		if (!audio) return;
		if (index !== partIndex || !audio.src) {
			partIndex = index;
			audio.src = partUrl(index);
			await new Promise<void>((resolve) => {
				const done = () => {
					audio?.removeEventListener('loadedmetadata', done);
					resolve();
				};
				audio?.addEventListener('loadedmetadata', done);
				audio?.load();
			});
		}
		audio.currentTime = within;
		audio.playbackRate = speed;
		now = day.parts[index].offset + within;
		if (play) await audio.play().catch(() => /* expected: the browser waits for the reader to press Play; a file that cannot load is reported by the audio's error event. */ undefined);
	}

	function seek(seconds: number, play = true) {
		const { index, within } = partAt(day.parts, Math.max(0, Math.min(seconds, day.total)));
		follow = true;
		void load(index, within, play || playing);
	}

	function toggle() {
		if (!audio) return;
		if (audio.paused) void (audio.src ? audio.play().catch(() => /* expected: as in load(); a failed file is reported by the error event. */ undefined) : seek(now));
		else audio.pause();
	}

	function changeSpeed() {
		speed = speeds[(speeds.indexOf(speed) + 1) % speeds.length];
		if (audio) audio.playbackRate = speed;
	}

	function onTime() {
		if (audio) now = day.parts[partIndex].offset + audio.currentTime;
	}

	function onEnded() {
		if (partIndex < day.parts.length - 1) void load(partIndex + 1, 0, true);
		else playing = false;
	}

	function onTrack(event: Event) {
		seek(Number((event.currentTarget as HTMLInputElement).value), false);
	}

	function onTurnClick(event: MouseEvent, turn: TimedTurn) {
		// A reader selecting words to copy does not want the recording to jump.
		if (window.getSelection()?.toString() || turn.missing) return;
		event.preventDefault();
		seek(turn.t);
	}

	// Follow the recording: keep the turn being spoken in view inside the transcript.
	$effect(() => {
		const at = current;
		if (!follow || !body || at < 0 || !playing) return;
		const element = body.querySelector<HTMLElement>(`[data-turn="${at}"]`);
		if (!element) return;
		const top = element.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
		if (top < body.scrollTop + 40 || top > body.scrollTop + body.clientHeight - 120) body.scrollTo({ top: Math.max(0, top - body.clientHeight / 3), behavior: 'smooth' });
	});

	onMount(() => {
		now = day.span[0];
		const asked = /^#t=(\d+(?:\.\d+)?)$/.exec(location.hash);
		if (asked) now = Number(asked[1]);
		ready = true;
		void tick().then(() => {
			const { index, within } = partAt(day.parts, now);
			void load(index, within, false);
		});
	});
</script>

<svelte:head>
	<title>{data.name}, oral argument, {day.when} · Cases · Tribal Law Project</title>
	<meta name="description" content="The oral argument in {data.name} on {day.when}: the recording, with the {own ? 'transcript made by speech recognition' : 'Court’s transcript'} beside it." />
</svelte:head>

<Breadcrumbs {trail} current="Oral argument" />

<article class="ap">
	<header class="ap-head">
		<h1><i>{data.name}</i></h1>
		<div class="ap-sub">
			<span>Oral argument · {day.when} · {clock(day.total)}</span>
			<span class="ap-seg" role="group" aria-label="Recording"><button type="button" aria-pressed="true">{day.source}</button></span>
			<span class="ap-files">
				{#if day.parts.length === 1}
					<a href={partUrl(0)} download title="Download the recording"><Download size={18} aria-hidden="true" />.mp3</a>
				{:else}
					{#each day.parts as part, index (part.key)}<a href={partUrl(index)} download title="Download part {part.number} of {day.parts.length} of the recording"><Download size={18} aria-hidden="true" />.mp3 <small>{part.number}</small></a>{/each}
				{/if}
				{#each day.transcripts as transcript (transcript.link)}<a href={transcript.link} title="Download the official transcript{day.transcripts.length > 1 ? ` (No. ${transcript.docket})` : ''}"><Download size={18} aria-hidden="true" />.pdf</a>{/each}
				<details class="ap-verify">
					<summary title="Verify file (SHA-256)" aria-label="Verify file"><ShieldCheck size={18} aria-hidden="true" /></summary>
					<div class="ap-verify-menu">
						{#each day.parts as part (part.key)}<a href={part.verify}>Verify the recording{day.parts.length > 1 ? `, part ${part.number}` : ''}</a>{/each}
						{#each day.transcripts as transcript (transcript.link)}<a href={transcript.verify}>Verify the transcript{day.transcripts.length > 1 ? `, No. ${transcript.docket}` : ''}</a>{/each}
					</div>
				</details>
			</span>
		</div>
		{#if partial}<p class="ap-sub ap-note">The whole day in court, as the Archives keeps it. This case runs from {clock(day.span[0])} to {clock(day.span[1])}.</p>{/if}
		{#if beginsLate || endsEarly}<p class="ap-sub ap-note">The recording {beginsLate ? 'begins partway through the argument' : ''}{beginsLate && endsEarly ? ' and ' : ''}{endsEarly ? 'ends before the argument does' : ''}; the transcript’s turns it does not hold are marked.</p>{/if}
		{#if data.others.length}<p class="ap-sub ap-note">Also argued {#each data.others as other, index (other.date)}{index ? ', ' : ''}<a href={other.path}>{other.when}</a>{/each}.</p>{/if}
	</header>

	<section class="ap-panel" aria-label="Recording and transcript">
		<div class="ap-player">
			<button type="button" class="play" aria-label={playing ? 'Pause' : 'Play'} onclick={toggle} disabled={!ready}>
				{#if playing}<Pause size={22} fill="currentColor" aria-hidden="true" />{:else}<Play size={22} fill="currentColor" aria-hidden="true" />{/if}
			</button>
			<button type="button" class="tool hide-narrow" title="Back 15 seconds" aria-label="Back 15 seconds" onclick={() => seek(now - 15, false)} disabled={!ready}><RotateCcw size={20} aria-hidden="true" /></button>
			<div class="ap-track">
				<div class="ap-bar">
					<i style:width="{(now / day.total) * 100}%"></i>
					{#each marks.slice(1) as mark (mark.t)}<s style:left="{(mark.t / day.total) * 100}%"></s>{/each}
					<b style:left="{(now / day.total) * 100}%"></b>
					<input type="range" min="0" max={day.total} step="1" value={now} aria-label="Position in the recording" aria-valuetext="{clock(now)} of {clock(day.total)}" oninput={onTrack} disabled={!ready} />
				</div>
				<div class="ap-marks">
					{#each marks as mark, index (mark.t)}<button type="button" style:left="{(mark.t / day.total) * 100}%" class:last={index === marks.length - 1 && index > 0} onclick={() => seek(mark.t)}>{mark.label}</button>{/each}
				</div>
			</div>
			<span class="ap-time">{clock(now)} / {clock(day.total)}</span>
			<button type="button" class="tool" title="Speed" aria-label="Speed, {speed} times" onclick={changeSpeed}>{speed}×</button>
			<audio bind:this={audio} preload="none" onplay={() => (playing = true)} onpause={() => (playing = false)} ontimeupdate={onTime} onended={onEnded} onerror={() => (problem = true)}></audio>
		</div>
		{#if problem}<p class="ap-small ap-problem" role="alert">The recording could not be played here. <a href={day.parts[partIndex].link}>Play it from {day.source === 'Supreme Court' ? 'the Court' : 'the National Archives'}</a>.</p>{/if}
		<noscript><audio class="ap-native" controls preload="none" src={partUrl(0)}></audio></noscript>

		<div class="ap-now-strip" aria-hidden="true">
			<span class="me">
				{#if benchSpeaking?.portrait}<img src={benchSpeaking.portrait} alt="" />{:else}<span class="ph">{speaking?.role === 'counsel' ? initials(counselSpeaking ?? speaking.speaker) : speaking?.speaker === 'A Justice' ? '?' : '—'}</span>{/if}
			</span>
			<div>
				<div class="who">{counselSpeaking ?? (speaking?.speaker || 'Speaker not marked')}</div>
				<div class="role">{speaking?.speaker === 'A Justice' ? 'The transcript does not say which' : 'Speaking'}</div>
			</div>
			<span class="faces">{#each day.bench as justice (justice.link)}{#if justice.portrait}<img class:on={benchSpeaking === justice} src={justice.portrait} alt="" />{/if}{/each}</span>
		</div>

		<div class="ap-cols">
			<aside class="ap-rail">
				<section>
					<h2>Justices</h2>
					<div class="ap-justices">
						{#each day.bench as justice (justice.link)}
							<figure class:on={benchSpeaking === justice}>
								<a href={justice.link} title={justice.name}>
									{#if justice.portrait}<img src={justice.portrait} alt={justice.name} loading="lazy" />{:else}<span class="ph big">{initials(justice.name)}</span>{/if}
								</a>
								{#if benchSpeaking === justice}<span class="pill on-img"><Mic size={14} strokeWidth={2.2} aria-hidden="true" /> Speaking</span>{/if}
								<figcaption>{justice.surname}{#if justice.chief}<small>Chief Justice</small>{/if}</figcaption>
							</figure>
						{/each}
					</div>
					{#if unnamedJustices}<p class="ap-small">Transcripts before 2004 do not say which Justice is asking.</p>{/if}
				</section>
				<section>
					<h2>Counsel</h2>
					{#if day.counsel.length}
						<div class="ap-counsel">
							{#each day.counsel as counsel (counsel.name)}
								<div class="ap-c" class:on={lectern === counsel.name}>
									<span class="ph">{initials(counsel.name)}</span>
									<div class="txt">
										<div class="name">{counsel.name}{#if counselSpeaking === counsel.name}<span class="pill"><Mic size={14} strokeWidth={2.2} aria-hidden="true" /> Speaking</span>{/if}</div>
										{#if counsel.title || counsel.for}<div class="for">{counsel.title ?? ''}{counsel.title && counsel.for ? ', ' : ''}{counsel.for ? `for ${counsel.for}` : ''}</div>{/if}
										{#if counsel.parties.length}<div class="parties">{#each counsel.parties as party (party.name)}<span>{#if party.image}<img src={party.image} alt="" />{:else}<span class="ph">{initials(party.name)}</span>{/if}{party.name}</span>{/each}</div>{/if}
										{#if counsel.times.length}<div class="times">{#each counsel.times as time, index (time)}{index ? ' · ' : ''}<a href="#t={time}" onclick={(event) => { event.preventDefault(); seek(time); }}>{index ? 'Rebuttal' : 'Argues at'} {clock(time)}</a>{/each}</div>{/if}
										{#if counsel.source}<div class="for"><a href={counsel.source}>Named in the United States Reports</a></div>{/if}
									</div>
								</div>
							{/each}
						</div>
					{:else}
						<p class="ap-small">None Identified</p>
					{/if}
				</section>
			</aside>

			<section class="ap-tx" aria-label="Transcript">
				<div class="ap-tx-bar">
					<h2>Transcript</h2>
					{#if own}<span class="ap-made" title={ownNote}><Mic size={14} strokeWidth={2.2} aria-hidden="true" /> Speech recognition · {day.model} <span class="ap-info" aria-label={ownNote} role="img"><Info size={14} aria-hidden="true" /></span></span>{:else}<span class="ap-made">Official transcript</span>{/if}
					<span class="ap-gap"></span>
					<label class="ap-search"><Search size={18} aria-hidden="true" /><input type="search" placeholder="Search the transcript" bind:value={query} aria-label="Search the transcript" /></label>
					<button type="button" class="ap-iconbtn" aria-pressed={follow} title="Follow the recording" aria-label="Follow the recording" onclick={() => (follow = !follow)}><ArrowDown size={18} aria-hidden="true" /></button>
				</div>
				{#if wanted}<p class="ap-small ap-found" role="status">{matches} {matches === 1 ? 'turn' : 'turns'} with “{query.trim()}”</p>{/if}
				<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
				<div class="ap-tx-body" bind:this={body} onwheel={() => (follow = false)} ontouchmove={() => (follow = false)} role="region" aria-label="Transcript text">
					{#each data.turns as turn, index (index)}
						{@const heading = headings.get(index)}
						{#if heading && !wanted}<h3>{heading.label === 'Rebuttal' ? 'Rebuttal argument' : 'Argument'} of {heading.counsel}{counselHeading(heading.counsel)}</h3>{/if}
						{#if !wanted && turn.page && turn.page !== data.turns[index - 1]?.page}<div class="ap-page">PAGE {turn.page}</div>{/if}
						{#if !wanted && beginsLate && index === firstHeard}<div class="ap-page ap-edge">THE RECORDING BEGINS HERE</div>{/if}
						{#if shown[index]}
							{@const justice = justiceOf(turn)}
							<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -- the time link is the keyboard's way in -->
							<div class="ap-turn" class:now={index === current} class:past={index < current && !turn.missing} class:missing={turn.missing} data-turn={index} onclick={(event) => onTurnClick(event, turn)}>
								<span class="av">
									{#if justice?.portrait}<img src={justice.portrait} alt="" loading="lazy" />{:else}<span class="ph">{turn.role === 'counsel' ? initials(turn.counsel !== undefined ? (day.counsel[turn.counsel]?.name ?? turn.speaker) : turn.speaker) : turn.speaker === 'A Justice' ? '?' : '—'}</span>{/if}
								</span>
								<div>
									<div class="head">
										{#if turn.speaker}<span class="who" class:unnamed={turn.speaker === 'A Justice'}>{turn.speaker}</span>{:else}<span class="who unnamed">Speaker not marked</span>{/if}
										{#if turn.missing}<span class="t gone">Not in the recording</span>{:else}
											<span class="seek"><Play size={12} fill="currentColor" aria-hidden="true" /> Play from here</span>
											<a class="t" href="#t={turn.t}" onclick={(event) => onTurnClick(event, turn)} aria-label="Play from {clock(turn.t)}">{clock(turn.t)}</a>
										{/if}
									</div>
									<p>{turn.text}</p>
								</div>
							</div>
						{/if}
						{#if !wanted && endsEarly && index === lastHeard}<div class="ap-page ap-edge">THE RECORDING ENDS HERE</div>{/if}
					{/each}
				</div>
			</section>
		</div>
	</section>
</article>

<style>
	.ap {
		display: grid;
		gap: 1rem;
		padding: 1.25rem 0 3rem;
	}
	.ap-head {
		display: grid;
		gap: 0.35rem;
	}
	.ap-head h1 {
		margin: 0;
	}
	.ap-sub {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 1rem;
		color: var(--color-ink-muted);
	}
	.ap-note {
		margin: 0;
	}
	.ap-seg {
		display: inline-flex;
		border: 1px solid var(--color-control-border, var(--color-line));
		border-radius: 999px;
		overflow: hidden;
	}
	.ap-seg button {
		border: 0;
		background: none;
		color: var(--color-ink);
		font: inherit;
		font-size: 0.9rem;
		padding: 0.3rem 0.8rem;
	}
	.ap-seg button[aria-pressed='true'] {
		background: var(--color-action);
		color: var(--color-action-text);
	}
	.ap-files {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		margin-left: auto;
	}
	.ap-files a,
	.ap-verify summary {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		padding: 0.25rem 0.45rem;
		border-radius: 8px;
		color: var(--color-link);
		font-size: 0.95rem;
	}
	.ap-verify {
		position: relative;
	}
	.ap-verify summary {
		cursor: pointer;
		list-style: none;
	}
	.ap-verify summary::-webkit-details-marker {
		display: none;
	}
	.ap-verify-menu {
		position: absolute;
		right: 0;
		top: 100%;
		z-index: 6;
		display: grid;
		gap: 0.2rem;
		min-width: 14rem;
		padding: 0.5rem;
		border: 1px solid var(--color-line);
		border-radius: 10px;
		background: var(--color-surface-raised, var(--color-paper));
		box-shadow: 0 6px 18px var(--color-menu-shadow);
	}
	.ap-panel {
		border: 1px solid var(--color-line);
		border-radius: 14px;
		background: var(--color-surface-raised, transparent);
		overflow: clip;
	}
	.ap-player {
		position: sticky;
		top: 0;
		z-index: 4;
		display: flex;
		align-items: center;
		gap: 0.7rem;
		padding: 0.7rem 1rem 0.55rem;
		border-bottom: 1px solid var(--color-line);
		background: var(--color-surface-raised, var(--color-paper));
	}
	.ap-player .play {
		flex: none;
		width: 46px;
		height: 46px;
		border-radius: 50%;
		border: 0;
		display: grid;
		place-items: center;
		background: var(--color-action);
		color: var(--color-action-text);
		cursor: pointer;
	}
	.ap-player .tool {
		flex: none;
		height: 36px;
		min-width: 36px;
		border: 0;
		border-radius: 8px;
		background: none;
		color: var(--color-ink);
		display: inline-grid;
		place-items: center;
		font: inherit;
		font-size: 0.9rem;
		cursor: pointer;
	}
	.ap-track {
		flex: 1;
		display: grid;
		gap: 0.25rem;
		min-width: 0;
	}
	.ap-bar {
		position: relative;
		height: 8px;
		border-radius: 4px;
		background: var(--color-line);
	}
	.ap-bar i {
		position: absolute;
		inset: 0 auto 0 0;
		border-radius: 4px;
		background: var(--color-action);
	}
	.ap-bar b {
		position: absolute;
		top: 50%;
		width: 16px;
		height: 16px;
		margin: -8px 0 0 -8px;
		border-radius: 50%;
		background: var(--color-action);
		box-shadow: 0 0 0 3px var(--color-surface-raised);
	}
	.ap-bar s {
		position: absolute;
		top: -3px;
		bottom: -3px;
		width: 2px;
		background: var(--color-ink-muted);
		opacity: 0.55;
	}
	/* The real control, over the drawn bar: it takes the clicks, drags and arrow keys. */
	.ap-bar input {
		position: absolute;
		inset: -8px 0;
		width: 100%;
		height: calc(100% + 16px);
		margin: 0;
		opacity: 0;
		cursor: pointer;
	}
	.ap-bar:has(input:focus-visible) {
		outline: 2px solid var(--color-focus-ring);
		outline-offset: 4px;
	}
	.ap-marks {
		position: relative;
		height: 1rem;
		font-size: 0.72rem;
		color: var(--color-ink-muted);
	}
	.ap-marks button {
		position: absolute;
		white-space: nowrap;
		border: 0;
		padding: 0;
		background: none;
		font: inherit;
		color: inherit;
		cursor: pointer;
	}
	.ap-marks button.last {
		transform: translateX(-70%);
	}
	.ap-time {
		font-variant-numeric: tabular-nums;
		color: var(--color-ink-muted);
		font-size: 0.9rem;
		white-space: nowrap;
	}
	.ap-native {
		display: block;
		width: calc(100% - 2rem);
		margin: 0.6rem 1rem;
	}
	.ap-cols {
		display: grid;
		grid-template-columns: 340px 1fr;
	}
	.ap-rail {
		border-right: 1px solid var(--color-line);
		padding: 1rem 1rem 1.25rem;
		display: grid;
		gap: 1.1rem;
		align-content: start;
	}
	.ap-rail h2,
	.ap-tx-bar h2 {
		font-size: 0.78rem;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--color-ink-muted);
		margin: 0;
		font-family: inherit;
		font-weight: 600;
	}
	.ap-justices {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 0.7rem 0.4rem;
		margin-top: 0.6rem;
	}
	.ap-justices figure {
		position: relative;
		margin: 0;
		display: grid;
		justify-items: center;
		gap: 0.3rem;
		text-align: center;
	}
	.ap-justices a {
		display: inline-grid;
		border-radius: 50%;
	}
	.ap-justices img,
	.ap-justices .ph.big {
		width: 58px;
		height: 58px;
		border-radius: 50%;
		object-fit: cover;
		object-position: 50% 18%;
		box-shadow: 0 0 0 1px var(--color-line);
	}
	.ap-justices figcaption {
		font-size: 0.82rem;
		line-height: 1.2;
	}
	.ap-justices figcaption small {
		display: block;
		color: var(--color-ink-muted);
		font-size: 0.7rem;
	}
	.ap-justices figure.on img {
		box-shadow: 0 0 0 3px var(--color-action);
	}
	.ap-justices figure.on figcaption {
		font-weight: 600;
	}
	.pill.on-img {
		position: absolute;
		top: 40px;
		left: 50%;
		transform: translateX(-50%);
		white-space: nowrap;
		box-shadow: 0 0 0 2px var(--color-surface-raised);
	}
	.pill {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--color-action-text);
		background: var(--color-action);
		border-radius: 999px;
		padding: 0.05rem 0.45rem;
	}
	.ap-small {
		margin: 0.6rem 0 0;
		font-size: 0.82rem;
		color: var(--color-ink-muted);
	}
	.ap-problem {
		margin: 0.6rem 1rem 0;
	}
	.ap-found {
		margin: 0.5rem 1rem 0;
	}
	.ap-counsel {
		display: grid;
		gap: 0.2rem;
		margin-top: 0.5rem;
	}
	.ap-c {
		display: grid;
		grid-template-columns: 38px 1fr;
		align-items: start;
		gap: 0.1rem 0.6rem;
		padding: 0.55rem 0.5rem;
		border-radius: 10px;
	}
	.ap-c.on {
		background: var(--color-selection);
		box-shadow: inset 3px 0 0 var(--color-action);
	}
	.ap-c > .ph {
		width: 38px;
		height: 38px;
		font-size: 0.8rem;
	}
	.ap-c .txt {
		display: grid;
		gap: 0.2rem;
		min-width: 0;
	}
	.ap-c .name {
		font-weight: 600;
		display: flex;
		gap: 0.4rem;
		align-items: center;
		flex-wrap: wrap;
	}
	.ap-c .for {
		font-size: 0.86rem;
		color: var(--color-ink-muted);
	}
	.ap-c .parties {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem 0.55rem;
		font-size: 0.84rem;
	}
	.ap-c .parties span {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}
	.ap-c .parties img {
		width: 20px;
		height: 20px;
		object-fit: contain;
	}
	.ap-c .parties .ph {
		width: 20px;
		height: 20px;
		font-size: 0.55rem;
	}
	.ap-c .times {
		font-size: 0.84rem;
	}
	.ap-c .times a {
		color: var(--color-link);
		font-variant-numeric: tabular-nums;
	}
	.ph {
		display: inline-grid;
		place-items: center;
		border-radius: 50%;
		background: var(--color-surface);
		color: var(--color-ink-muted);
		font-weight: 600;
		border: 1px solid var(--color-line);
	}
	.ap-tx {
		display: grid;
		grid-template-rows: auto auto 1fr;
		min-width: 0;
		max-height: 760px;
	}
	.ap-tx-bar {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		padding: 0.7rem 1rem;
		border-bottom: 1px solid var(--color-line);
	}
	.ap-gap {
		flex: 1;
	}
	.ap-made {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.8rem;
		color: var(--color-ink-muted);
		border: 1px solid var(--color-line);
		border-radius: 999px;
		padding: 0.1rem 0.55rem;
		white-space: nowrap;
	}
	.ap-info {
		display: inline-grid;
		color: var(--color-link);
	}
	.ap-search {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		border: 1px solid var(--color-control-border, var(--color-line));
		border-radius: 8px;
		padding: 0.25rem 0.5rem;
		color: var(--color-ink-muted);
		font-size: 0.9rem;
		min-width: 190px;
	}
	.ap-search input {
		border: 0;
		background: none;
		font: inherit;
		color: var(--color-ink);
		min-width: 0;
		width: 100%;
		height: 1.5rem;
		min-height: 0;
		padding: 0;
		box-shadow: none;
		outline: none;
	}
	.ap-search:focus-within {
		outline: 2px solid var(--color-focus-ring);
		outline-offset: 1px;
	}
	.ap-iconbtn {
		display: inline-grid;
		place-items: center;
		width: 34px;
		height: 34px;
		border-radius: 8px;
		border: 1px solid var(--color-control-border, var(--color-line));
		background: none;
		color: var(--color-ink);
		cursor: pointer;
	}
	.ap-iconbtn[aria-pressed='true'] {
		background: var(--color-action);
		color: var(--color-action-text);
		border-color: transparent;
	}
	.ap-tx-body {
		overflow: auto;
		padding: 0.4rem 1rem 1.5rem;
	}
	.ap-tx-body h3 {
		font-size: 0.92rem;
		margin: 0.8rem 0 0.3rem;
		color: var(--color-ink-muted);
		font-weight: 600;
	}
	.ap-turn {
		display: grid;
		grid-template-columns: 32px 1fr;
		gap: 0.1rem 0.7rem;
		padding: 0.5rem 0.6rem;
		border-radius: 10px;
		cursor: pointer;
	}
	.ap-turn:hover {
		background: var(--color-surface);
	}
	.ap-turn .seek {
		display: none;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.78rem;
		color: var(--color-link);
	}
	.ap-turn:hover .seek {
		display: inline-flex;
	}
	.ap-turn .av img,
	.ap-turn .av .ph {
		width: 32px;
		height: 32px;
		border-radius: 50%;
		object-fit: cover;
		object-position: 50% 18%;
		font-size: 0.66rem;
	}
	.ap-turn .head {
		display: flex;
		align-items: baseline;
		gap: 0.6rem;
	}
	.ap-turn .who {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.ap-turn .who.unnamed {
		font-style: italic;
	}
	.ap-turn .t {
		margin-left: auto;
		font-variant-numeric: tabular-nums;
		font-size: 0.8rem;
		color: var(--color-link);
	}
	.ap-turn p {
		margin: 0.1rem 0 0;
		font-family: var(--font-family-reading, inherit);
		line-height: 1.55;
	}
	.ap-turn.past p {
		color: var(--color-ink-muted);
	}
	.ap-turn.missing {
		cursor: default;
	}
	.ap-turn.missing:hover {
		background: none;
	}
	.ap-turn.missing p,
	.ap-turn.missing .who {
		color: var(--color-ink-muted);
	}
	.ap-turn .t.gone {
		color: var(--color-ink-muted);
	}
	.ap-edge {
		color: var(--color-ink);
		font-weight: 600;
	}
	.ap-turn.now {
		background: var(--color-selection);
		box-shadow: inset 3px 0 0 var(--color-action);
	}
	.ap-page {
		font-size: 0.72rem;
		color: var(--color-ink-muted);
		letter-spacing: 0.04em;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin: 0.3rem 0 -0.1rem;
	}
	.ap-page::after {
		content: '';
		flex: 1;
		border-top: 1px dashed var(--color-line);
	}
	.ap-now-strip {
		display: none;
	}
	@media (max-width: 760px) {
		.ap-cols {
			grid-template-columns: 1fr;
		}
		.ap-rail {
			display: none;
		}
		.ap-files {
			margin-left: 0;
		}
		.ap-now-strip {
			display: flex;
			align-items: center;
			gap: 0.6rem;
			padding: 0.6rem 0.8rem;
			border-bottom: 1px solid var(--color-line);
			position: sticky;
			top: 44px;
			z-index: 3;
			background: var(--color-surface-raised, var(--color-paper));
		}
		.ap-now-strip .me img,
		.ap-now-strip .me .ph {
			width: 40px;
			height: 40px;
			border-radius: 50%;
			object-fit: cover;
			object-position: 50% 18%;
			box-shadow: 0 0 0 2px var(--color-action);
		}
		.ap-now-strip .who {
			font-weight: 600;
			font-size: 0.95rem;
		}
		.ap-now-strip .role {
			font-size: 0.78rem;
			color: var(--color-ink-muted);
		}
		.ap-now-strip .faces {
			margin-left: auto;
			display: flex;
		}
		.ap-now-strip .faces img {
			width: 22px;
			height: 22px;
			border-radius: 50%;
			object-fit: cover;
			object-position: 50% 18%;
			margin-left: -5px;
			box-shadow: 0 0 0 2px var(--color-surface-raised);
		}
		.ap-now-strip .faces img.on {
			box-shadow: 0 0 0 2px var(--color-action);
			position: relative;
			z-index: 1;
		}
		.ap-player {
			position: fixed;
			top: auto;
			bottom: 0;
			left: 0;
			right: 0;
			border-top: 1px solid var(--color-line);
			border-bottom: 0;
			box-shadow: 0 -4px 16px var(--color-menu-shadow);
		}
		.ap-player .hide-narrow,
		.ap-marks {
			display: none;
		}
		.ap-tx {
			max-height: none;
		}
		.ap-tx-bar h2 {
			display: none;
		}
		.ap-search {
			min-width: 0;
			flex: 1;
		}
		.ap-turn {
			grid-template-columns: 28px 1fr;
		}
		.ap-turn .av img,
		.ap-turn .av .ph {
			width: 28px;
			height: 28px;
		}
		.ap {
			padding-bottom: 6rem;
		}
	}
</style>
