DATA_DIR = os.getenv("DATA_DIR", "/data");
WRITE_PARTIALS = os.getenv("WRITE_PARTIALS", "false").lower() == "true";

const net = require("net");
const dgram = require("dgram");
const WebSocket = require("ws");

const TCP_PORT = parseInt(process.env.BOT_PCM_PORT || "7000", 10);
const ASR_URL = process.env.ASR_WS_URL || "ws://asr-lb:8080/ws";
const AUTH_TOKEN = process.env.ASR_TOKEN || "";
const UDP_PORT = parseInt(process.env.BRIDGE_UDP_PORT || "8125", 10);

const FRAME = 640;
let ws;

let currentUid = null;
const uidToName = Object.create(null);

function displayName() {
  if (currentUid && uidToName[currentUid]) return uidToName[currentUid];
  return "[Unknown]";
}

const udp = dgram.createSocket("udp4");
udp.on("message", (msg) => {
  const s = msg.toString("utf8").trim();
  if (s.startsWith("active ")) {
    const uid = s.slice(7).trim();
    if (/^\d+$/.test(uid)) currentUid = uid;
  } else if (s.startsWith("roster ")) {
    const body = s.slice(7).trim();
    for (const pair of body.split(";")) {
      if (!pair) continue;
      const [uid, name] = pair.split("=");
      if (uid && name) uidToName[uid.trim()] = name.trim();
    }
  }
});
udp.bind(UDP_PORT, "127.0.0.1", () => {
  console.log(`[bridge] UDP roster on 127.0.0.1:${UDP_PORT}`);
});

function downsample32kTo16k(buf32k) {
  const samp = new Int16Array(
    buf32k.buffer,
    buf32k.byteOffset,
    buf32k.length / 2
  );
  const out = new Int16Array(Math.floor(samp.length / 2));
  for (let i = 0, j = 0; j < out.length && i + 1 < samp.length; i += 2, j++) {
    out[j] = ((samp[i] + samp[i + 1]) / 2) | 0;
  }
  return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

// ---- ASR WS ----
async function connectASR() {
  return new Promise((resolve, reject) => {
    const headers = AUTH_TOKEN ? { Authorization: `Bearer ${AUTH_TOKEN}` } : {};
    ws = new WebSocket(ASR_URL, { perMessageDeflate: false, headers });
    ws.once("open", () => {
      ws.send(
        JSON.stringify({ type: "start", sample_rate: 16000, language: "en" })
      );
      resolve();
    });
    ws.once("error", reject);
  });
}

(async () => {
  await connectASR();

  const server = net.createServer((sock) => {
    let frameBuf = Buffer.alloc(0);
    console.log("[bridge] bot connected");

    sock.on("data", (chunk) => {
      const ds = downsample32kTo16k(chunk);
      frameBuf = Buffer.concat([frameBuf, ds]);
      while (frameBuf.length >= FRAME) {
        const frame = frameBuf.subarray(0, FRAME);
        frameBuf = frameBuf.subarray(FRAME);
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(frame);
      }
    });

    sock.on("end", () => {
      try {
        ws.send(JSON.stringify({ type: "stop" }));
      } catch {}
    });
  });

  server.listen(TCP_PORT, "127.0.0.1", () => {
    console.log(`[bridge] PCM TCP on 127.0.0.1:${TCP_PORT}, ASR=${ASR_URL}`);
  });

  ws.on("message", (m) => {
    try {
      const msg = JSON.parse(m.toString());
      if (msg.type === "partial" || msg.type === "final") {
        console.log(`[${msg.type}][${displayName()}] ${msg.text}`);
      } else {
        console.log("[ASR]", msg);
      }
    } catch {
      console.log("[ASR RAW]", m.toString());
    }
  });
})();
