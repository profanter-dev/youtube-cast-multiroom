import * as fs from "fs";
import * as os from "os";
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
	forgeKey: forge.pki.rsa.PrivateKey;
}

function certHasRequiredExtensions(certPem: string): boolean {
	try {
		const c = forge.pki.certificateFromPem(certPem);
		const names = c.extensions.map((e: { name?: string }) => e.name ?? "");
		if (!names.includes("subjectAltName") || !names.includes("keyUsage")) return false;
		// Require at least one iPAddress SAN so Cast SDK hostname verification
		// against the server IP passes (DNS SAN does not match IP connections per RFC 6125)
		const san = c.extensions.find((e: { name?: string }) => e.name === "subjectAltName") as
			{ altNames?: Array<{ type: number }> } | undefined;
		return (san?.altNames ?? []).some((n) => n.type === 7);
	} catch {
		return false;
	}
}

function generateCert(): CertPair {
	console.log("Generating self-signed TLS certificate…");
	const keys = forge.pki.rsa.generateKeyPair(2048);
	const cert = forge.pki.createCertificate();
	cert.publicKey = keys.publicKey;
	cert.serialNumber = "01";
	cert.validity.notBefore = new Date();
	cert.validity.notAfter = new Date();
	cert.validity.notAfter.setFullYear(cert.validity.notBefore.getFullYear() + 10);

	const attrs = [
		{ name: "commonName", value: DEVICE_NAME },
		{ name: "organizationName", value: "CastBridge" },
	];
	cert.setSubject(attrs);
	cert.setIssuer(attrs);
	const localIps = Object.values(os.networkInterfaces())
		.flat()
		.filter((iface): iface is os.NetworkInterfaceInfo =>
			!!iface && iface.family === "IPv4" && !iface.internal,
		)
		.map((i) => i.address);

	console.log(`Certificate SANs: DNS:${DEVICE_NAME}.local` +
		(localIps.length ? ", IP:" + localIps.join(", IP:") : " (no local IPs found)"));

	cert.setExtensions([
		{
			name: "subjectAltName",
			altNames: [
				{ type: 2, value: `${DEVICE_NAME}.local` },
				...localIps.map((ip) => ({ type: 7, ip })),
			],
		},
		{ name: "basicConstraints", cA: false },
		{ name: "keyUsage", digitalSignature: true, keyEncipherment: true, critical: true },
		{ name: "extKeyUsage", serverAuth: true },
	]);
	cert.sign(keys.privateKey, forge.md.sha256.create());

	const certPem = forge.pki.certificateToPem(cert);
	const keyPem = forge.pki.privateKeyToPem(keys.privateKey);

	fs.mkdirSync(CERTS_DIR, { recursive: true });
	fs.writeFileSync(path.join(CERTS_DIR, "cast.crt"), certPem);
	fs.writeFileSync(path.join(CERTS_DIR, "cast.key"), keyPem);
	console.log("TLS certificate written to volume.");

	return { cert: certPem, key: keyPem, forgeKey: keys.privateKey };
}

function loadOrCreateCert(): CertPair {
	const certFile = path.join(CERTS_DIR, "cast.crt");
	const keyFile = path.join(CERTS_DIR, "cast.key");

	if (fs.existsSync(certFile) && fs.existsSync(keyFile)) {
		const certPem = fs.readFileSync(certFile, "utf8");
		if (!certHasRequiredExtensions(certPem)) {
			console.log("Existing TLS cert lacks required extensions — regenerating…");
			return generateCert();
		}
		console.log("Loaded existing TLS certificate from volume.");
		const keyPem = fs.readFileSync(keyFile, "utf8");
		return {
			cert: certPem,
			key: keyPem,
			forgeKey: forge.pki.privateKeyFromPem(keyPem) as forge.pki.rsa.PrivateKey,
		};
	}

	return generateCert();
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
// Cast DeviceAuth — the sender sends a binary protobuf DeviceAuthChallenge
// and the receiver must sign the nonce with its TLS private key.
// Implemented with manual protobuf encode/decode to avoid extra dependencies.
// ---------------------------------------------------------------------------

const NS_DEVICEAUTH = "urn:x-cast:com.google.cast.tp.deviceauth";

function pbVarint(n: number): Buffer {
	const bytes: number[] = [];
	do {
		let b = n & 0x7f;
		n >>>= 7;
		if (n > 0) b |= 0x80;
		bytes.push(b);
	} while (n > 0);
	return Buffer.from(bytes);
}

function pbLenDelim(fieldNum: number, data: Buffer): Buffer {
	return Buffer.concat([pbVarint((fieldNum << 3) | 2), pbVarint(data.length), data]);
}

function pbVarintField(fieldNum: number, value: number): Buffer {
	return Buffer.concat([pbVarint((fieldNum << 3) | 0), pbVarint(value)]);
}

function parseDeviceAuthNonce(msg: Buffer): Buffer | null {
	// DeviceAuthMessage { challenge(1): DeviceAuthChallenge { sender_nonce: bytes } }
	// sender_nonce is field 1 in the Chromium proto, field 2 in some older implementations
	// (e.g. BRAVIA TV sends: field2=nonce, field3=signature_algorithm).
	// Try field 1 first, fall back to field 2.
	let i = 0;
	while (i < msg.length) {
		const tag = msg[i++];
		const fieldNum = tag >> 3;
		const wireType = tag & 0x7;
		if (wireType === 2) {
			let len = 0, shift = 0;
			while (i < msg.length) { const b = msg[i++]; len |= (b & 0x7f) << shift; shift += 7; if (!(b & 0x80)) break; }
			const sub = msg.slice(i, i + len);
			i += len;
			if (fieldNum === 1) {
				// parse DeviceAuthChallenge — prefer field 1, fall back to field 2
				let nonceF1: Buffer | null = null;
				let nonceF2: Buffer | null = null;
				let j = 0;
				while (j < sub.length) {
					const t = sub[j++];
					const fn = t >> 3, wt = t & 0x7;
					if (wt === 2) {
						let l = 0, s = 0;
						while (j < sub.length) { const b = sub[j++]; l |= (b & 0x7f) << s; s += 7; if (!(b & 0x80)) break; }
						if (fn === 1) nonceF1 = sub.slice(j, j + l);
						else if (fn === 2) nonceF2 = sub.slice(j, j + l);
						j += l;
					} else if (wt === 0) { while (j < sub.length && (sub[j++] & 0x80)); }
				}
				return nonceF1 ?? nonceF2;
			}
		} else if (wireType === 0) { while (i < msg.length && (msg[i++] & 0x80)); }
	}
	return null;
}


function handleDeviceAuth(
	server: CastServerInstance,
	clientId: string,
	sourceId: string,
	rawData: string,
	forgeKey: forge.pki.rsa.PrivateKey,
	certPem: string,
): void {
	const buf = Buffer.isBuffer(rawData)
		? (rawData as unknown as Buffer)
		: Buffer.from(rawData as string, "binary");

	console.log("[deviceauth] raw hex:", buf.slice(0, 40).toString("hex"), "len:", buf.length);

	const nonce = parseDeviceAuthNonce(buf) ?? Buffer.alloc(0);
	if (!nonce.length) {
		console.warn("[deviceauth] no sender_nonce found, proceeding without nonce");
	}

	const md = forge.md.sha256.create();
	md.update(nonce.toString("binary"));
	const sig = Buffer.from(forgeKey.sign(md), "binary");

	const certDER = Buffer.from(
		forge.asn1.toDer(forge.pki.certificateToAsn1(forge.pki.certificateFromPem(certPem))).getBytes(),
		"binary",
	);

	// DeviceAuthMessage { response(2): DeviceAuthResponse { sig(1), cert(2), alg(4), nonce(5)?, hash(6) } }
	const responseFields = [
		pbLenDelim(1, sig),
		pbLenDelim(2, certDER),
		pbVarintField(4, 1),       // signature_algorithm: RSASSA_PKCS1v15
		...(nonce.length ? [pbLenDelim(5, nonce)] : []),
		pbVarintField(6, 2),       // hash_algorithm: SHA256
	];
	const responsePb = pbLenDelim(2, Buffer.concat(responseFields));

	// server.send() checks Buffer.isBuffer(data) and sets payloadType=BINARY automatically
	server.send(clientId, "receiver-0", sourceId, NS_DEVICEAUTH, responsePb as unknown as string);
	console.log("[deviceauth] sent DeviceAuthResponse, nonce length:", nonce.length);
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
		data: string | Buffer,
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
	forgeKey: forge.pki.rsa.PrivateKey,
	certPem: string,
): void {
	if (namespace === NS_DEVICEAUTH) {
		handleDeviceAuth(server, clientId, sourceId, rawData, forgeKey, certPem);
		return;
	}

	const state = getSession(clientId);

	function reply(ns: string, payload: Record<string, unknown>): void {
		server.send(clientId, destinationId, sourceId, ns, JSON.stringify(payload));
	}

	let data: Record<string, unknown>;
	try {
		data = JSON.parse(rawData) as Record<string, unknown>;
	} catch {
		console.warn(`[cast] non-JSON payload in ns=${namespace}`);
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

	const certPair = loadOrCreateCert();
	const identity = loadOrCreateDeviceIdentity();

	registerAvahiService(identity);

	const server = new CastServer({ cert: certPair.cert, key: certPair.key });

	server.on(
		"message",
		(clientId, sourceId, destinationId, namespace, data) => {
			console.log(`[cast] client=${clientId} ns=${namespace}`);
			handleMessage(server, clientId, sourceId, destinationId, namespace, data, certPair.forgeKey, certPair.cert);
		},
	);

	server.on("error", (err) => {
		console.error("Cast server error:", err);
	});

	server.listen(CAST_PORT, () => {
		console.log(`castbridge listening on port ${CAST_PORT}`);
	});

	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	const tlsServer = (server as any).server;
	tlsServer?.on("tlsClientError", (err: Error, socket: { remoteAddress?: string }) => {
		console.error(`[tls] handshake error from ${socket.remoteAddress ?? "?"}: ${err.message}`);
	});
	tlsServer?.on("secureConnection", (socket: { remoteAddress?: string; getProtocol?: () => string }) => {
		console.log(`[tls] secure connection from ${socket.remoteAddress ?? "?"} (${socket.getProtocol?.() ?? "?"})`);
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
