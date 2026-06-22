import * as fs from "fs";
import * as path from "path";
import * as crypto from "crypto";
import { EventEmitter } from "events";
import { execSync, spawn, ChildProcess } from "child_process";
import forge from "node-forge";
import { v4 as uuidv4 } from "uuid";

const DEVICE_NAME = process.env["DEVICE_NAME"] ?? "Multiroom";
const CAST_PORT = 8009;
const CERTS_DIR = "/certs";
const SNAPFIFO_PATH = "/snapfifo/snapfifo";

// ---------------------------------------------------------------------------
// Named pipe
// ---------------------------------------------------------------------------

function ensureSnapfifo(): void {
	if (!fs.existsSync(SNAPFIFO_PATH)) {
		execSync(`mkfifo ${SNAPFIFO_PATH}`);
		console.log(`Created named pipe at ${SNAPFIFO_PATH}`);
	}
}

// ---------------------------------------------------------------------------
// TLS certificate (self-signed, persisted across restarts)
// ---------------------------------------------------------------------------

interface CertPair {
	cert: string;
	key: string;
}

function loadOrCreateCert(): CertPair {
	const certFile = path.join(CERTS_DIR, "cast.crt");
	const keyFile = path.join(CERTS_DIR, "cast.key");

	if (fs.existsSync(certFile) && fs.existsSync(keyFile)) {
		console.log("Loaded existing TLS certificate from volume.");
		return {
			cert: fs.readFileSync(certFile, "utf8"),
			key: fs.readFileSync(keyFile, "utf8"),
		};
	}

	console.log("Generating self-signed TLS certificate…");
	const keys = forge.pki.rsa.generateKeyPair(2048);
	const cert = forge.pki.createCertificate();
	cert.publicKey = keys.publicKey;
	cert.serialNumber = "01";
	cert.validity.notBefore = new Date();
	cert.validity.notAfter = new Date();
	cert.validity.notAfter.setFullYear(
		cert.validity.notBefore.getFullYear() + 10,
	);

	const attrs = [
		{ name: "commonName", value: DEVICE_NAME },
		{ name: "organizationName", value: "CastBridge" },
	];
	cert.setSubject(attrs);
	cert.setIssuer(attrs);
	cert.sign(keys.privateKey, forge.md.sha256.create());

	const certPem = forge.pki.certificateToPem(cert);
	const keyPem = forge.pki.privateKeyToPem(keys.privateKey);

	fs.mkdirSync(CERTS_DIR, { recursive: true });
	fs.writeFileSync(certFile, certPem);
	fs.writeFileSync(keyFile, keyPem);
	console.log("TLS certificate written to volume.");

	return { cert: certPem, key: keyPem };
}

// ---------------------------------------------------------------------------
// Playback engine
// ---------------------------------------------------------------------------

let ytdlpProcess: ChildProcess | null = null;
let ffmpegProcess: ChildProcess | null = null;
let fifoWriteStream: fs.WriteStream | null = null;

function stopPlayback(): void {
	if (ytdlpProcess) {
		ytdlpProcess.kill("SIGTERM");
		ytdlpProcess = null;
	}
	if (ffmpegProcess) {
		ffmpegProcess.kill("SIGTERM");
		ffmpegProcess = null;
	}
	if (fifoWriteStream) {
		try {
			fifoWriteStream.destroy();
		} catch {
			/* ignore */
		}
		fifoWriteStream = null;
	}
}

function startPlayback(videoId: string): void {
	stopPlayback();
	console.log(`Starting playback for video ID: ${videoId}`);

	const url = `https://www.youtube.com/watch?v=${videoId}`;

	ytdlpProcess = spawn(
		"yt-dlp",
		["--format", "bestaudio", "-o", "-", "--quiet", url],
		{ stdio: ["ignore", "pipe", "inherit"] },
	);

	ffmpegProcess = spawn(
		"ffmpeg",
		["-i", "pipe:0", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"],
		{ stdio: ["pipe", "pipe", "inherit"] },
	);

	(ytdlpProcess.stdout as NodeJS.ReadableStream).pipe(
		ffmpegProcess.stdin as NodeJS.WritableStream,
	);

	fifoWriteStream = fs.createWriteStream(SNAPFIFO_PATH, { flags: "w" });
	(ffmpegProcess.stdout as NodeJS.ReadableStream).pipe(fifoWriteStream);

	ytdlpProcess.on("error", (err) => console.error("yt-dlp error:", err));
	ffmpegProcess.on("error", (err) => console.error("ffmpeg error:", err));

	ytdlpProcess.on("exit", (code) => {
		if (code !== 0 && code !== null)
			console.error(`yt-dlp exited with code ${code}`);
	});
	ffmpegProcess.on("exit", (code) => {
		if (code !== 0 && code !== null)
			console.error(`ffmpeg exited with code ${code}`);
	});
}

// ---------------------------------------------------------------------------
// castv2 Server — @types/castv2 only types Client, not Server, so we define
// the minimal interface from the actual server.js source.
// ---------------------------------------------------------------------------

interface CastServerInstance extends EventEmitter {
	listen(port: number, callback?: () => void): void;
	send(
		clientId: string,
		sourceId: string,
		destinationId: string,
		namespace: string,
		data: string,
	): void;
	close(): void;
	on(
		event: "message",
		cb: (
			clientId: string,
			sourceId: string,
			destinationId: string,
			namespace: string,
			data: string,
		) => void,
	): this;
	on(event: "error", cb: (err: Error) => void): this;
	on(event: "close", cb: () => void): this;
}

// eslint-disable-next-line @typescript-eslint/no-require-imports
const { Server: CastServer } = require("castv2") as {
	Server: new (options: { cert: string; key: string }) => CastServerInstance;
};

// ---------------------------------------------------------------------------
// Per-client session state
// ---------------------------------------------------------------------------

interface ClientState {
	sessionId: string;
	transportId: string;
	mediaSessionId: number;
	currentVideoId: string | null;
}

const clientSessions = new Map<string, ClientState>();

function getSession(clientId: string): ClientState {
	let state = clientSessions.get(clientId);
	if (!state) {
		state = {
			sessionId: uuidv4(),
			transportId: uuidv4(),
			mediaSessionId: 1,
			currentVideoId: null,
		};
		clientSessions.set(clientId, state);
	}
	return state;
}

// ---------------------------------------------------------------------------
// Cast V2 protocol — namespaces and message builders
// ---------------------------------------------------------------------------

const NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection";
const NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat";
const NS_RECEIVER = "urn:x-cast:com.google.cast.receiver";
const NS_MEDIA = "urn:x-cast:com.google.cast.media";

const YOUTUBE_MUSIC_APP_ID = "CAF65D3C";

function makeReceiverStatus(
	state: ClientState,
	includeApp: boolean,
): Record<string, unknown> {
	const applications = includeApp
		? [
				{
					appId: YOUTUBE_MUSIC_APP_ID,
					displayName: "YouTube Music",
					namespaces: [
						{ name: NS_MEDIA },
						{ name: "urn:x-cast:com.google.youtube.mdx" },
					],
					sessionId: state.sessionId,
					statusText: "YouTube Music",
					transportId: state.transportId,
				},
			]
		: [];

	return {
		type: "RECEIVER_STATUS",
		requestId: 0,
		status: {
			applications,
			isActiveInput: true,
			isStandbyMode: false,
			volume: {
				controlType: "attenuation",
				level: 1.0,
				muted: false,
				stepInterval: 0.05,
			},
		},
	};
}

function makeMediaStatus(
	state: ClientState,
	playerState: string,
): Record<string, unknown> {
	const entry: Record<string, unknown> = {
		mediaSessionId: state.mediaSessionId,
		playbackRate: 1,
		playerState,
		currentTime: 0,
		supportedMediaCommands: 4351,
		volume: { level: 1.0, muted: false },
	};

	if (state.currentVideoId) {
		entry["media"] = {
			contentId: state.currentVideoId,
			streamType: "BUFFERED",
			contentType: "video/mp4",
			metadata: { metadataType: 0, title: state.currentVideoId },
			duration: 0,
		};
	}

	if (playerState === "IDLE") {
		entry["idleReason"] = "FINISHED";
	}

	return { type: "MEDIA_STATUS", requestId: 0, status: [entry] };
}

function extractVideoId(media: Record<string, unknown>): string | null {
	const customData = media["customData"] as
		| Record<string, unknown>
		| undefined;
	const contentId = media["contentId"] as string | undefined;

	if (customData?.["videoId"]) {
		return customData["videoId"] as string;
	}
	if (contentId) {
		const match = contentId.match(/(?:v=|youtu\.be\/)([A-Za-z0-9_-]{11})/);
		return match ? (match[1] ?? null) : contentId.length === 11 ? contentId : null;
	}
	return null;
}

// ---------------------------------------------------------------------------
// Message router
// ---------------------------------------------------------------------------

function handleMessage(
	server: CastServerInstance,
	clientId: string,
	sourceId: string,
	destinationId: string,
	namespace: string,
	rawData: string,
): void {
	const state = getSession(clientId);

	function reply(ns: string, payload: Record<string, unknown>): void {
		server.send(clientId, destinationId, sourceId, ns, JSON.stringify(payload));
	}

	let data: Record<string, unknown>;
	try {
		data = JSON.parse(rawData) as Record<string, unknown>;
	} catch {
		console.warn(`[cast] non-JSON payload in ns=${namespace} (binary DeviceAuth?)`);
		return;
	}

	const reqId = (data["requestId"] as number | undefined) ?? 0;

	switch (namespace) {
		case NS_CONNECTION:
			if (data["type"] === "CONNECT")
				reply(NS_CONNECTION, { type: "CONNECTED" });
			break;

		case NS_HEARTBEAT:
			if (data["type"] === "PING")
				reply(NS_HEARTBEAT, { type: "PONG" });
			break;

		case NS_RECEIVER:
			console.log("Receiver:", data["type"]);
			if (data["type"] === "GET_STATUS" || data["type"] === "LAUNCH") {
				reply(NS_RECEIVER, {
					...makeReceiverStatus(state, true),
					requestId: reqId,
				});
			} else if (data["type"] === "STOP") {
				stopPlayback();
				state.currentVideoId = null;
				reply(NS_RECEIVER, {
					...makeReceiverStatus(state, false),
					requestId: reqId,
				});
			}
			break;

		case NS_MEDIA:
			console.log("Media:", data["type"]);
			if (data["type"] === "LOAD") {
				const media = data["media"] as Record<string, unknown> | undefined;
				const videoId = media ? extractVideoId(media) : null;

				if (!videoId) {
					console.warn(
						"Could not extract video ID:",
						JSON.stringify(data),
					);
					state.currentVideoId = null;
					reply(NS_MEDIA, {
						...makeMediaStatus(state, "IDLE"),
						requestId: reqId,
					});
					return;
				}

				state.mediaSessionId += 1;
				state.currentVideoId = videoId;
				console.log(`LOAD — videoId: ${videoId}`);
				startPlayback(videoId);
				reply(NS_MEDIA, {
					...makeMediaStatus(state, "PLAYING"),
					requestId: reqId,
				});
			} else if (data["type"] === "GET_STATUS") {
				const ps = state.currentVideoId ? "PLAYING" : "IDLE";
				reply(NS_MEDIA, {
					...makeMediaStatus(state, ps),
					requestId: reqId,
				});
			} else if (data["type"] === "PAUSE") {
				stopPlayback();
				reply(NS_MEDIA, {
					...makeMediaStatus(state, "PAUSED"),
					requestId: reqId,
				});
			} else if (data["type"] === "PLAY") {
				if (state.currentVideoId) startPlayback(state.currentVideoId);
				reply(NS_MEDIA, {
					...makeMediaStatus(state, "PLAYING"),
					requestId: reqId,
				});
			} else if (data["type"] === "STOP") {
				stopPlayback();
				state.currentVideoId = null;
				reply(NS_MEDIA, {
					...makeMediaStatus(state, "IDLE"),
					requestId: reqId,
				});
			}
			break;
	}
}

// ---------------------------------------------------------------------------
// Device identity — persisted so the Cast device ID is stable across restarts
// ---------------------------------------------------------------------------

interface DeviceIdentity {
	id: string; // 32 lowercase hex (UUID without dashes)
	cd: string; // 32 uppercase hex
	bs: string; // 12 uppercase hex (MAC-like)
}

function loadOrCreateDeviceIdentity(): DeviceIdentity {
	const identityFile = path.join(CERTS_DIR, "device-identity.json");

	if (fs.existsSync(identityFile)) {
		return JSON.parse(fs.readFileSync(identityFile, "utf8")) as DeviceIdentity;
	}

	const identity: DeviceIdentity = {
		id: uuidv4().replace(/-/g, ""),
		cd: crypto.randomBytes(16).toString("hex").toUpperCase(),
		bs: crypto.randomBytes(6).toString("hex").toUpperCase(),
	};

	fs.mkdirSync(CERTS_DIR, { recursive: true });
	fs.writeFileSync(identityFile, JSON.stringify(identity, null, 2));
	console.log("Generated new device identity.");
	return identity;
}

// ---------------------------------------------------------------------------
// mDNS advertisement — write a static avahi service file that avahi-daemon
// picks up via inotify. More reliable than avahi-publish D-Bus registration
// which silently fails under TrueNAS's avahi sandboxing.
// ---------------------------------------------------------------------------

const AVAHI_SERVICES_DIR = "/etc/avahi/services";
const AVAHI_SERVICE_FILE = path.join(AVAHI_SERVICES_DIR, "cast-multiroom.service");

function registerAvahiService(identity: DeviceIdentity): void {
	const records = [
		`id=${identity.id}`, `cd=${identity.cd}`, `rm=`, `ve=05`,
		`md=${DEVICE_NAME}`, `ic=/setup/icon.png`, `fn=${DEVICE_NAME}`,
		`ca=4101`, `st=0`, `bs=${identity.bs}`, `nf=1`, `rs=`,
	].map((r) => `<txt-record>${r}</txt-record>`).join("");

	const xml = `<service-group><name replace-wildcards="no">${DEVICE_NAME}</name><service><type>_googlecast._tcp</type><port>${CAST_PORT}</port>${records}</service></service-group>\n`;
	fs.mkdirSync(AVAHI_SERVICES_DIR, { recursive: true });
	fs.writeFileSync(AVAHI_SERVICE_FILE, xml);
	console.log(`mDNS: wrote avahi service file for "${DEVICE_NAME}"`);
}

function unregisterAvahiService(): void {
	try {
		fs.unlinkSync(AVAHI_SERVICE_FILE);
		console.log("mDNS: removed avahi service file");
	} catch {
		// ignore if already gone
	}
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
	ensureSnapfifo();

	const { cert, key } = loadOrCreateCert();
	const identity = loadOrCreateDeviceIdentity();

	registerAvahiService(identity);

	const server = new CastServer({ cert, key });

	server.on(
		"message",
		(clientId, sourceId, destinationId, namespace, data) => {
			console.log(`[cast] client=${clientId} ns=${namespace}`);
			handleMessage(server, clientId, sourceId, destinationId, namespace, data);
		},
	);

	server.on("error", (err) => {
		console.error("Cast server error:", err);
	});

	server.listen(CAST_PORT, () => {
		console.log(`castbridge listening on port ${CAST_PORT}`);
	});

	process.on("SIGTERM", () => {
		console.log("SIGTERM received — stopping playback and exiting.");
		stopPlayback();
		unregisterAvahiService();
		process.exit(0);
	});
}

main().catch((err) => {
	console.error("Fatal:", err);
	process.exit(1);
});
