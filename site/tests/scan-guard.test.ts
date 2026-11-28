/**
 * The scan guard (site/SCANNING.md, "The scan guard"). No file that came from
 * outside the project may be stored or served without the virus check. This
 * test reads every Worker's code with the TypeScript checker and finds, by
 * type rather than by name:
 *
 * 1. every place that reads the bytes of a file or a request body (a File, a
 *    Blob, or a Request's body), which is how a file arrives; and
 * 2. every place that writes to or reads from an R2 bucket.
 *
 * Each one must be listed below with what it is. A file from outside must be
 * written with storeSentFile (or in a module that calls requestScan) and
 * served with openScannedFile (or in a function that checks mayServe), and
 * the test checks that. Anything new that is not listed fails the test, so a
 * new way in or out is looked at before it ships.
 */
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { beforeAll, describe, expect, it } from "vitest";

const site = path.resolve(import.meta.dirname, "..");

/** What a bucket call is for. */
type BucketUse =
  /** Writes a file that came from outside the project. */
  | { kind: "sent-file-store" }
  /** Serves or opens a file that came from outside the project. */
  | { kind: "sent-file-open" }
  /** The scanner reading a file in order to scan it. */
  | { kind: "scanner" }
  /** A file the project made or chose itself, never one sent from outside. */
  | { kind: "project-file"; why: string }
  /** A sent file read or kept for a reason that never hands it to anyone. */
  | { kind: "kept-not-served"; why: string };

/**
 * Every bucket write and read in site code, by file, function and call. The
 * first argument (the object's key) is part of the name, so two calls in one
 * function can differ.
 */
const bucketCalls: Record<string, BucketUse> = {
  // The two doors for files from outside (src/lib/server/file-scans.ts).
  "src/lib/server/file-scans.ts storeSentFile put(file.objectKey)": {
    kind: "sent-file-store",
  },
  "src/lib/server/file-scans.ts openScannedFile get(objectKey)": {
    kind: "sent-file-open",
  },

  // Uploads (docs/uploads.md): written piece by piece into a multipart
  // object, put in line by finishUpload, served by serveUploadFile.
  "src/lib/server/uploads.ts startUpload createMultipartUpload(originalKey)": {
    kind: "sent-file-store",
  },
  "src/lib/server/uploads.ts receivePiece resumeMultipartUpload(upload.original_key)":
    { kind: "sent-file-store" },
  "src/lib/server/uploads.ts finishUpload resumeMultipartUpload(upload.original_key)":
    { kind: "sent-file-store" },
  "src/lib/server/uploads.ts refuseFile resumeMultipartUpload(upload.original_key)":
    { kind: "sent-file-store" },
  "src/lib/server/uploads.ts deleteBytes resumeMultipartUpload(upload.original_key)":
    { kind: "sent-file-store" },
  "src/lib/server/uploads.ts finishUpload put(previewKey)": {
    kind: "sent-file-store",
  },
  // Intake (docs/uploads.md, "Intake"): small notes the Worker and the
  // intake script pass through the bucket. They name files; they are not files.
  "src/lib/server/uploads.ts offerForIntake put(`intake/waiting/${upload.reference}.json`)": {
    kind: "project-file",
    why: "A note the Worker writes itself about a pulled file that passed its scan: the file's key, name, size and type.",
  },
  "src/lib/server/uploads.ts readNote get(key)": {
    kind: "project-file",
    why: "Reads an intake note the project's own script wrote (intake/filed or intake/declined); only its fields are used, never served.",
  },
  // The Wayback Worker's own progress (site/WAYBACK.md): which links it has
  // asked the Wayback Machine to save, and what came back.
  "wayback/worker.ts readMarks get(MARKS_OBJECT)": {
    kind: "project-file",
    why: "The lead author's marks on links the Wayback Worker could not save, written by the workspace's Wayback captures page.",
  },
  "src/lib/server/wayback.ts readWaybackProgress get(progressObject)": {
    kind: "project-file",
    why: "The Wayback Worker's progress record, read for the workspace's Wayback captures page.",
  },
  "src/lib/server/wayback.ts readWaybackMarks get(marksObject)": {
    kind: "project-file",
    why: "The lead author's marks on failed Wayback links; only that page writes them.",
  },
  "src/lib/server/wayback.ts writeWaybackMarks put(marksObject)": {
    kind: "project-file",
    why: "The lead author's marks on failed Wayback links, written from a form a signed-in lead author sends; it holds only links, dates and a name.",
  },
  "wayback/worker.ts readProgress get(PROGRESS_OBJECT)": {
    kind: "project-file",
    why: "The Wayback Worker's own progress record, which only it writes.",
  },
  "wayback/worker.ts writeProgress put(PROGRESS_OBJECT)": {
    kind: "project-file",
    why: "The Wayback Worker's own progress record, written from what the Wayback Machine answered; it holds only links and dates.",
  },
  // The source watch (docs/source-watch.md): files it fetches from public
  // sources are kept privately and put in line for the scan by storeCapture;
  // the rest are records the watch Worker or the site writes itself.
  "src/lib/server/watch/capture.ts storeCapture put(key)": {
    kind: "sent-file-store",
  },
  "src/lib/server/watch/drivers/page.ts read get(key)": {
    kind: "kept-not-served",
    why: "Text the watch Worker extracted from a watched page, read only to show a short difference on the review page; never a file, never served.",
  },
  "src/lib/server/watch/drivers/page.ts write put(key)": {
    kind: "kept-not-served",
    why: "Text the watch Worker extracted from a watched page (tags removed), kept to compare with the next version; never a file, never served.",
  },
  "src/lib/server/watch/progress.ts readWatchProgress get(progressObject)": {
    kind: "project-file",
    why: "The watch Worker's own progress record (counts and run outcomes), read by its progress page and the Service checks page.",
  },
  "src/lib/server/watch/progress.ts writeWatchProgress put(progressObject)": {
    kind: "project-file",
    why: "The watch Worker's own progress record: counts, run outcomes and model spend, never an item's text.",
  },
  "src/routes/cases/[id]/argument/[date]/+page.server.ts load get(day.timed.key)": {
    kind: "project-file",
    why: "An argument's timed transcript, which scripts/caselaw/supreme_court_argument_timing.py makes from the Court's transcript or the recording; read as JSON and checked against its SHA-256, never served as a file.",
  },
  "src/lib/server/uploads.ts finishUpload get(upload.original_key)": {
    kind: "kept-not-served",
    why: "Reads a sent picture to make its copy without location details; both are put in line for the scan, and neither is served until it passes.",
  },
  // Recordings of the events video room (site/COLLABORATION.md, "The video
  // room"): streamed from RealtimeKit into the bucket, since a recording can
  // be larger than a Worker's memory, then put in line by syncRoomRecordings
  // and served by serveUploadFile.
  "src/lib/server/video-rooms.ts storeStream put(key)": {
    kind: "sent-file-store",
  },
  "src/lib/server/upload-serving.ts serveUploadFile get(file.key)": {
    kind: "sent-file-open",
  },

  // The project inbox (site/MAIL.md). Attachments both ways go through
  // storeSentFile and openScannedFile, in inbound.ts and outbound.ts.
  "src/lib/server/mail/inbound.ts receiveMail put(rawKey)": {
    kind: "kept-not-served",
    why: "The whole message as it arrived, kept as a record. Nothing reads it back; its attachments are stored and scanned on their own.",
  },
  // Arriving mail kept whole until it is filed (site/MAIL.md, "When something
  // fails"); receiveMail then stores its attachments with storeSentFile.
  "src/lib/server/mail/arrivals.ts keepArrival put(key)": {
    kind: "kept-not-served",
    why: "An arriving message kept whole until it is filed, so a failure loses nothing. Nothing serves it; it is deleted once filed.",
  },
  "src/lib/server/mail/arrivals.ts fileArrival get(key)": {
    kind: "kept-not-served",
    why: "Reads a kept message to file it. receiveMail stores each attachment with storeSentFile, and nothing here hands the message to anyone.",
  },
  "src/lib/server/mail/inbound.ts receiveMail put(htmlKey)": {
    kind: "kept-not-served",
    why: 'The HTML version of a message, not a file: shown only in a sandboxed frame that runs no scripts and loads nothing from outside (site/MAIL.md, "Reading HTML mail").',
  },
  "src/routes/workspace/inbox/messages/[message]/html/+server.ts GET get(key)":
    {
      kind: "kept-not-served",
      why: "The HTML version of a message, shown only in a sandboxed frame that runs no scripts and loads nothing from outside.",
    },
  "src/lib/server/mail/export.ts messageCopy get(row.raw_key)": {
    kind: "sent-file-open",
  },
  // Big files sent as download links (site/MAIL.md, "Big files as links"):
  // written piece by piece into a multipart object, put in line by
  // finishLinkedFile, and served by the link with openScannedFile.
  "src/lib/server/mail/linked-files.ts startLinkedFile createMultipartUpload(key)":
    { kind: "sent-file-store" },
  "src/lib/server/mail/linked-files.ts receiveLinkedPiece resumeMultipartUpload(file.bucket_key)":
    { kind: "sent-file-store" },
  "src/lib/server/mail/linked-files.ts finishLinkedFile resumeMultipartUpload(file.bucket_key)":
    { kind: "sent-file-store" },
  "src/lib/server/mail/linked-files.ts deleteLinkedFiles resumeMultipartUpload(file.bucket_key)":
    { kind: "sent-file-store" },
  "src/lib/server/mail/outbound.ts sendMessage put(rawKey)": {
    kind: "project-file",
    why: "A copy of a message the project sent, built by the site after its attachments passed the check.",
  },

  // The scanner.
  "src/lib/server/scanning/scan-desk.ts scanOne get(due.objectKey)": {
    kind: "scanner",
  },

  // Files the project makes or chooses itself.
  "src/lib/server/essays.ts readVersionText get(row.object_key)": {
    kind: "project-file",
    why: "Essay text written in the site editor.",
  },
  "src/lib/server/essays.ts saveVersion put(objectKey)": {
    kind: "project-file",
    why: "Essay text written in the site editor.",
  },
  "src/lib/server/live/essay-room.ts saveNow put(overlayKey(essay, saved.version.number))": {
    kind: "project-file",
    why: "Who typed each word of a version the live room saved, worked out by the room.",
  },
  "src/lib/server/essay-credits.ts versionBlame get(blameKey(essay, rows[start].number))": {
    kind: "project-file",
    why: "Who wrote each word of an essay version, worked out by the site.",
  },
  "src/lib/server/essay-credits.ts versionBlame get(overlayKey(essay, row.number))": {
    kind: "project-file",
    why: "Who typed each word of a version the live room saved, worked out by the room.",
  },
  "src/lib/server/essay-credits.ts versionBlame put(blameKey(essay, row.number))": {
    kind: "project-file",
    why: "Who wrote each word of an essay version, worked out by the site.",
  },
  "src/lib/server/essay-git-sync.ts syncOne put(gitSnapshotKey(identifier, essay.sha256))":
    {
      kind: "project-file",
      why: "Essay text from the project repository.",
    },
  "src/lib/server/page-history.ts recordBuildChanges put(versionKey(page))": {
    kind: "project-file",
    why: "Page text from the site build.",
  },
  "src/lib/server/page-history.ts readText get(key)": {
    kind: "project-file",
    why: "Page text from the site build.",
  },
  "src/lib/server/request-store.ts saveRequest put(copyKey)": {
    kind: "project-file",
    why: "The filled form as Markdown, written by the site from the answers typed on the form, not a file someone sent.",
  },
  "src/routes/workspace/requests/[reference]/forms/[submission]/copy/+server.ts GET get(submission.filledCopyKey)":
    {
      kind: "project-file",
      why: "The filled form as Markdown, written by the site.",
    },
  "src/lib/server/book-orders.ts readFileSet get(`books/${identifier}/details.json`)":
    { kind: "project-file", why: "The book files the project builds." },
  "src/routes/book/files/[...path]/+server.ts GET get(`books/${fileSet}/${file}`)":
    { kind: "project-file", why: "The book files the project builds." },
  "src/lib/server/map-tile-serving.ts getBytes get(this.key)": {
    kind: "project-file",
    why: "Map tiles the project builds.",
  },
  "src/lib/server/map-tile-serving.ts serveMapAsset get(key)": {
    kind: "project-file",
    why: "Map fonts and symbols the project builds.",
  },
  "src/lib/server/public-copy-serving.ts servePublicCopy get(copy.key)": {
    kind: "project-file",
    why: "Copies of government documents and constitutions the project saved from their official sources.",
  },
  "src/lib/server/public-copy-serving.ts servePublicCopy put(copy.key)": {
    kind: "project-file",
    why: "Copies of government documents and constitutions the project saved from their official sources.",
  },
  "src/routes/cases/[id]/opinions.json/+server.ts GET get(copy.key)": {
    kind: "project-file",
    why: "The served copy of a case's opinions, which the project built from the Caselaw Access Project; only the file indexed with the site is read.",
  },
  "src/routes/copies/reading/[...key]/+server.ts GET get(copy.key)": {
    kind: "project-file",
    why: "A served copy read for its reading version, the same file /copies serves; only the file indexed with the site is read.",
  },
  "src/routes/nations/[id]/image/+server.ts GET get(image.key)": {
    kind: "project-file",
    why: "Nation seals and images the project collected from official sources.",
  },
  "src/routes/nations/[id]/image/+server.ts GET put(image.key)": {
    kind: "project-file",
    why: "Nation seals and images the project collected from official sources.",
  },
  "src/routes/supreme-court/portraits/[person]/+server.ts GET get(portrait.key)": {
    kind: "project-file",
    why: "Pictures of Justices, Presidents and Courts the project checked by hand, each served only if it matches the SHA-256 recorded in content/supreme-court/pictures.yaml.",
  },
  "src/routes/supreme-court/portraits/[person]/+server.ts GET put(portrait.key)": {
    kind: "project-file",
    why: "Pictures of Justices, Presidents and Courts the project checked by hand, kept only if the fetched copy matches the SHA-256 recorded in content/supreme-court/pictures.yaml.",
  },
  "src/lib/server/source-library.ts applyLibraryChanges get(`${changesPrefix}latest.json`)":
    {
      kind: "project-file",
      why: "The source library's change files, written by scripts/library/build_library.py.",
    },
  "src/lib/server/source-library.ts applyLibraryChanges get(next.parts[done])": {
    kind: "project-file",
    why: "The source library's change files, written by scripts/library/build_library.py.",
  },
  "src/routes/workspace/library/[document]/original/+server.ts GET get(document.key)":
    {
      kind: "project-file",
      why: "Research documents the project saved, for the writing team only. The library leaves out every file sent from outside (uploads, requests, pictures, recordings, mail), so only keys the builder indexed are served.",
    },
  "src/lib/server/document-zips.ts entries get(key)": {
    kind: "project-file",
    why: "The project's own copies of public documents (U.S. government works and nations' governing documents), zipped for download.",
  },
  "src/routes/search/download/+server.ts open get(key)": {
    kind: "project-file",
    why: "The project's own copies of the search results a reader ticked, zipped for download. Keys come only from the records built with the site and the list of copies it may serve, never from the request.",
  },
};

/** Every place site code reads the bytes of a file or a request body, and where they go. */
const fileIntakes: Record<string, string> = {
  "src/lib/server/request-form.ts checkFile File.arrayBuffer":
    "Files sent with requests and forms, proof for a nation team, nation page pictures, and photos sent with a problem about a printed-copy order. Stored with storeSentFile by saveRequest, saveApplication, savePicture and reportProblem.",
  "src/routes/profile/edit/+page.server.ts default File.arrayBuffer":
    "Profile pictures, sent with the profile form. Stored with storeSentFile by savePicture (profile-pictures.ts).",
  "src/lib/server/profile-documents.ts saveDocument File.arrayBuffer":
    "A contributor's CV or résumé. Stored with storeSentFile (purpose profile-document) and served only after the scan passes, at /profile/curriculum-vitae and to editors at /workspace/contributors/<identifier>/curriculum-vitae.",
  "src/routes/workspace/events/[identifier]/+page.server.ts addMaterial File.arrayBuffer":
    "An event's materials, such as written materials or slides. Stored with storeSentFile (purpose event-material) and served with serveUploadFile, which checks the scan, at /events/<slug>/materials/<material>.",
  "src/routes/account/sponsorship/+page.server.ts save File.arrayBuffer":
    "Sponsor logos. Stored with storeSentFile by setLogo (sponsors.ts) and served with openScannedFile by logoResponse (sponsor-logos.ts).",
  "src/routes/mail/internal/arrive/+server.ts POST Request.arrayBuffer":
    "Mail arriving from the email Worker. receiveMail stores each attachment with storeSentFile.",
  "src/routes/workspace/inbox/compose.ts readAttachments File.arrayBuffer":
    "Files the team attaches to outgoing mail. queueMessage stores them with storeSentFile, and sendMessage waits until they pass.",
  "src/routes/workspace/inbox/links/[file]/[piece]/+server.ts PUT Request.arrayBuffer":
    "Pieces of a big file sent from the inbox as a link, written into its multipart object; finishLinkedFile puts the file in line, and the message and the link wait until it passes.",
  "src/routes/uploads/pieces/[reference]/[number]/+server.ts PUT Request.arrayBuffer":
    "Upload pieces, written into the upload’s multipart object; finishUpload puts the file in line.",
  "src/lib/server/limited-body.ts formWithin Request.body":
    "The public request form's body, read with a size limit and only turned back into form data. Its files are then read by checkFile (request-form.ts) and stored with storeSentFile by saveRequest.",
  "src/lib/sha256.ts hashFile Blob.arrayBuffer":
    "Verify a file: hashes a file the reader picks, in their own browser. It is never sent or stored.",
  "src/lib/sha256.ts hashFile Blob.stream":
    "Verify a file: hashes a large file the reader picks, piece by piece, in their own browser. It is never sent or stored.",
};

interface Found {
  name: string;
  file: string;
  line: number;
  functionText: string;
  moduleText: string;
}

const bucketMethods = new Set([
  "get",
  "put",
  "createMultipartUpload",
  "resumeMultipartUpload",
]);
const bodyMethods = new Set([
  "arrayBuffer",
  "bytes",
  "stream",
  "text",
  "blob",
  "formData",
  "json",
]);

/** The main file of every Worker in the site folder (wrangler.jsonc "main"), so a new Worker is checked too. */
function workerMains(): string[] {
  const mains: string[] = [];
  const visit = (folder: string) => {
    for (const entry of readdirSync(folder, { withFileTypes: true })) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      const full = path.join(folder, entry.name);
      if (entry.isDirectory()) visit(full);
      else if (/^wrangler\.jsonc?$/.test(entry.name)) {
        const main = /"main"\s*:\s*"([^"]+)"/.exec(
          readFileSync(full, "utf8"),
        )?.[1];
        if (main && !main.startsWith(".svelte-kit"))
          mains.push(path.resolve(folder, main));
      }
    }
  };
  visit(site);
  return mains;
}

function enclosingFunction(node: ts.Node): { name: string; node: ts.Node } {
  for (
    let current: ts.Node | undefined = node.parent;
    current;
    current = current.parent
  ) {
    if (
      (ts.isFunctionDeclaration(current) || ts.isMethodDeclaration(current)) &&
      current.name
    ) {
      return { name: current.name.getText(), node: current };
    }
    if (
      (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) &&
      current.parent
    ) {
      const holder = current.parent;
      if (ts.isVariableDeclaration(holder) || ts.isPropertyAssignment(holder))
        return { name: holder.name.getText(), node: current };
    }
  }
  return { name: "(module)", node: node.getSourceFile() };
}

let bucketFound: Found[] = [];
let intakesFound: Found[] = [];
let untyped: string[] = [];

beforeAll(() => {
  const parsed = ts.getParsedCommandLineOfConfigFile(
    path.join(site, "tsconfig.json"),
    {},
    {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (problem) => {
        throw new Error(
          ts.flattenDiagnosticMessageText(problem.messageText, "\n"),
        );
      },
    },
  );
  if (!parsed)
    throw new Error(
      "Could not read tsconfig.json. Run `npx svelte-kit sync` first.",
    );
  const roots = [
    ...parsed.fileNames.filter(
      (file) => !file.includes(`${path.sep}tests${path.sep}`),
    ),
    ...workerMains(),
  ];
  const program = ts.createProgram(roots, { ...parsed.options, noEmit: true });
  const checker = program.getTypeChecker();
  const typeName = (expression: ts.Expression) =>
    checker.typeToString(
      checker.getNonNullableType(checker.getTypeAtLocation(expression)),
    );

  for (const source of program.getSourceFiles()) {
    const relative = path.relative(site, source.fileName);
    if (
      relative.startsWith("..") ||
      relative.includes("node_modules") ||
      relative.startsWith(".svelte-kit")
    )
      continue;
    const record = (list: Found[], node: ts.Node, name: string) => {
      const holder = enclosingFunction(node);
      list.push({
        name: `${relative} ${holder.name} ${name}`,
        file: relative,
        line: source.getLineAndCharacterOfPosition(node.getStart()).line + 1,
        functionText: holder.node.getText(),
        moduleText: source.getFullText(),
      });
    };
    const visit = (node: ts.Node) => {
      if (
        ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression)
      ) {
        const method = node.expression.name.text;
        const receiver = node.expression.expression;
        const type = typeName(receiver);
        if (bucketMethods.has(method)) {
          if (type === "R2Bucket")
            record(
              bucketFound,
              node,
              `${method}(${node.arguments[0]?.getText() ?? ""})`,
            );
          else if (
            type === "any" &&
            /bucket|documents|storage/i.test(receiver.getText())
          ) {
            untyped.push(
              `${relative}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1} ${node.getText().slice(0, 80)}`,
            );
          }
        }
        if (
          bodyMethods.has(method) &&
          (type === "File" ||
            type === "Blob" ||
            (type === "Request" &&
              method !== "formData" &&
              method !== "json" &&
              method !== "text"))
        ) {
          record(intakesFound, node, `${type}.${method}`);
        }
      }
      // A request body read as a stream.
      if (
        ts.isPropertyAccessExpression(node) &&
        node.name.text === "body" &&
        typeName(node.expression) === "Request"
      ) {
        record(intakesFound, node, "Request.body");
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
}, 120_000);

describe("the scan guard", () => {
  it("finds the bucket calls it knows are there", () => {
    const names = bucketFound.map((found) => found.name);
    expect(names.some((name) => name.includes("storeSentFile put("))).toBe(
      true,
    );
    expect(names.some((name) => name.includes("openScannedFile get("))).toBe(
      true,
    );
  });

  it("finds no bucket that has lost its type", () => {
    expect(
      untyped,
      "Give these a bucket type (R2Bucket) so the scan guard can see them",
    ).toEqual([]);
  });

  it("knows every bucket call in site code", () => {
    const unknown = bucketFound
      .filter((found) => !(found.name in bucketCalls))
      .map((found) => `${found.file}:${found.line}  '${found.name}'`);
    expect(
      unknown,
      "A bucket call the scan guard does not know. If it stores or serves a file from outside, use storeSentFile or openScannedFile (src/lib/server/file-scans.ts); then list it in tests/scan-guard.test.ts",
    ).toEqual([]);
  });

  it("lists no bucket call that is gone", () => {
    const names = new Set(bucketFound.map((found) => found.name));
    expect(Object.keys(bucketCalls).filter((name) => !names.has(name))).toEqual(
      [],
    );
  });

  it("stores every file from outside through the scanner", () => {
    const wrong = bucketFound.filter((found) => {
      if (bucketCalls[found.name]?.kind !== "sent-file-store") return false;
      const isStoreSentFile =
        found.file === "src/lib/server/file-scans.ts" &&
        found.name.includes(" storeSentFile ");
      return !isStoreSentFile && !/\brequestScan\(/.test(found.moduleText);
    });
    expect(wrong.map((found) => found.name)).toEqual([]);
  });

  it("serves every file from outside only after its scan", () => {
    const wrong = bucketFound.filter((found) => {
      if (bucketCalls[found.name]?.kind !== "sent-file-open") return false;
      return !/\bmayServe\(/.test(found.functionText);
    });
    expect(wrong.map((found) => found.name)).toEqual([]);
  });

  it("lets only the scanner read files to scan them", () => {
    const wrong = bucketFound.filter(
      (found) =>
        bucketCalls[found.name]?.kind === "scanner" &&
        !found.file.startsWith("src/lib/server/scanning/"),
    );
    expect(wrong.map((found) => found.name)).toEqual([]);
  });

  it("knows every place a file arrives", () => {
    const unknown = intakesFound
      .filter((found) => !(found.name in fileIntakes))
      .map((found) => `${found.file}:${found.line}  '${found.name}'`);
    expect(
      unknown,
      "A new place where file bytes arrive. Store them with storeSentFile (src/lib/server/file-scans.ts), then list it in tests/scan-guard.test.ts",
    ).toEqual([]);
    const names = new Set(intakesFound.map((found) => found.name));
    expect(Object.keys(fileIntakes).filter((name) => !names.has(name))).toEqual(
      [],
    );
  });

  it("keeps no file in the database, where the guard cannot see it", () => {
    const folder = path.join(site, "migrations");
    const withBlobs = readdirSync(folder).filter((name) =>
      /\bBLOB\b/i.test(
        readFileSync(path.join(folder, name), "utf8").replace(/--.*$/gm, ""),
      ),
    );
    expect(
      withBlobs,
      "Files go in the bucket, through storeSentFile, never in a BLOB column",
    ).toEqual([]);
  });
});
