const net = require("net");
const WebSocket = require("ws");

const TCP_PORT = process.env.BOT_PCM_PORT || 7000;
const ASR_URL = process.env.ASR_WS_URL || "ws://asr-lb:8080/ws";
const AUTH_TOKEN = process.env.ASR_TOKEN || "";

const FRAME = 640;
let ws;

const dgram = require("dgram");

const activeSock = dgram.createSocket("udp4");
let currentActive = { ids: [], since: Date.now() };

activeSock.on("message", (msg) => {
  const m = msg.toString();
  if (m.startsWith("active=")) {
    const ids = m.slice(7).split(",").filter(Boolean);
    currentActive = { ids, since: Date.now() };
  }
});
activeSock.bind(7100, "127.0.0.1");

const nameSock = dgram.createSocket("udp4");
const nameMap = new Map();

nameSock.on("message", (msg) => {
  const m = msg.toString();
  if (m.startsWith("map=")) {
    const items = m.slice(4).split("|");
    for (const it of items) {
      const [uid, name] = it.split(":");
      if (uid) nameMap.set(uid, name || "User");
    }
  }
});
nameSock.bind(7101, "127.0.0.1");

// Helper: label to print now
function currentSpeakerLabel() {
  const uid = currentActive.ids[0] || "";
  const name = uid ? nameMap.get(uid) || `User ${uid}` : "Unknown";
  return { uid, name };
}

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
    console.log(`[bridge] listening on 127.0.0.1:${TCP_PORT}, ASR=${ASR_URL}`);
  });
  server.on("connection", () => console.log("[bridge] bot connected"));

  ws.on("message", (m) => {
    try {
      const data = JSON.parse(m);
      if (data.type === "partial") {
        const { name } = currentSpeakerLabel();
        console.log(`[partial][${name}] ${data.text}`);
      } else if (data.type === "final") {
        const { name } = currentSpeakerLabel();
        console.log(`[final][${name}] ${data.text}`);
        // TODO: append to a file/DB if you want persistence
      } else {
        console.log("[ASR]", data);
      }
    } catch {
      console.log("[ASR raw]", m.toString());
    }
  });
})();
