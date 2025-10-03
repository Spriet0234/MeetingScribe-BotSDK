const http = require("http");
const net = require("net");

const HTTP_PORT = process.env.HTTP_CONTROL_PORT
  ? parseInt(process.env.HTTP_CONTROL_PORT, 10)
  : 7601;
const TCP_HOST = process.env.IDLE_TCP_HOST || "127.0.0.1";
const TCP_PORT = process.env.IDLE_TCP_PORT
  ? parseInt(process.env.IDLE_TCP_PORT, 10)
  : 7600;

function forwardToIdle(jsonLine) {
  return new Promise((resolve, reject) => {
    const sock = new net.Socket();
    let reply = Buffer.alloc(0);
    sock.setTimeout(5000);

    sock.connect(TCP_PORT, TCP_HOST, () => {
      sock.write(jsonLine + "\n");
    });

    sock.on("data", (chunk) => {
      reply = Buffer.concat([reply, chunk]);
    });
    sock.on("timeout", () => {
      sock.destroy(new Error("timeout"));
    });
    sock.on("error", reject);
    sock.on("close", (hadErr) => {
      if (hadErr) return reject(new Error("connection closed"));
      resolve(reply.toString("utf8").trim());
    });
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    res.writeHead(200, { "Content-Type": "text/plain" });
    return res.end("ok");
  }
  if (req.method !== "POST" || req.url !== "/start") {
    res.writeHead(404);
    return res.end("not found");
  }

  try {
    let body = "";
    req.on("data", (c) => {
      body += c;
      if (body.length > 64 * 1024) req.destroy();
    });
    req.on("end", async () => {
      let j;
      try {
        j = JSON.parse(body || "{}");
      } catch (_) {
        res.writeHead(400);
        return res.end("bad json");
      }

      // Accept either meeting_id or meetingNumber; normalize to meeting_id
      const meeting_id = j.meeting_id || j.meetingNumber;
      const passcode = j.passcode || "";
      const zak = j.zak || "";

      if (!meeting_id) {
        res.writeHead(400);
        return res.end("missing meeting_id");
      }

      const line = JSON.stringify({ meeting_id, passcode, zak });
      try {
        const resp = await forwardToIdle(line); // e.g., "OK queued"
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ status: resp }));
      } catch (e) {
        res.writeHead(502);
        return res.end("idle controller unreachable");
      }
    });
  } catch {
    res.writeHead(500);
    res.end("error");
  }
});

server.listen(HTTP_PORT, "0.0.0.0", () => {
  console.log(
    `[http-gw] listening on 0.0.0.0:${HTTP_PORT} -> TCP ${TCP_HOST}:${TCP_PORT}`
  );
});
