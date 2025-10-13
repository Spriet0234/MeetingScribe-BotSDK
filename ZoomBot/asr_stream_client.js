const net = require("net");
const WebSocket = require("ws");
const dgram = require("dgram");
const http = require("http");
const https = require("https");

const {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
} = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");

function ENV(k, d) {
  var v = process.env[k];
  return (
    v === undefined ? (d === undefined ? "" : String(d)) : String(v)
  ).trim();
}
var LOG_LEVEL = ENV("LOG_LEVEL", "info").toLowerCase();
var LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
var CUR = LEVELS[LOG_LEVEL] || LEVELS.info;
var log = {
  debug: function (m) {
    if (CUR <= LEVELS.debug) console.log("[debug] " + m);
  },
  info: function (m) {
    if (CUR <= LEVELS.info) console.log("[info]  " + m);
  },
  warn: function (m) {
    if (CUR <= LEVELS.warn) console.warn("[warn]  " + m);
  },
  error: function (m) {
    if (CUR <= LEVELS.error) console.error("[error] " + m);
  },
};
function throttle(ms) {
  var last = 0;
  return function (fn) {
    var now = Date.now();
    if (now - last >= ms) {
      last = now;
      try {
        fn();
      } catch (_) {}
    }
  };
}
var throttleConnErr = throttle(3000);

const IGNORE_FROM_CALLBACK = new Set([
  "done",
  "complete",
  "completed",
  "summarizing",
]);
function mapCallbackStatus(s) {
  if (!s) return null;
  s = String(s).toLowerCase();
  if (IGNORE_FROM_CALLBACK.has(s)) return null;
  if (
    s === "in_meeting" ||
    s === "joined" ||
    s === "running" ||
    s === "recording"
  )
    return "in_meeting";
  if (s === "meeting_ended") return "meeting_ended";
  if (s === "transcript_ready") return "transcript_ready";
  if (s === "error") return "error";
  return null;
}

var TCP_HOST = ENV("BOT_PCM_HOST", "127.0.0.1");
var TCP_PORT = parseInt(ENV("BOT_PCM_PORT", "7000"), 10);
var ASR_LOG_PARTIALS = ENV("ASR_LOG_PARTIALS", "1") !== "0";

var RECONNECT_MIN_MS = Math.max(
  200,
  parseInt(ENV("RECONNECT_MIN_MS", "1000"), 10)
);
var RECONNECT_MAX_MS = Math.max(
  RECONNECT_MIN_MS,
  parseInt(ENV("RECONNECT_MAX_MS", "30000"), 10)
);

var DG_URL = ENV(
  "DEEPGRAM_URL",
  "wss://api.deepgram.com/v1/listen?model=nova-2-general&encoding=linear16&sample_rate=16000&channels=1&punctuate=true&language=en"
);
var DG_KEY = ENV("DEEPGRAM_API_KEY");

var AWS_REGION = ENV("AWS_REGION", "us-east-1");
var S3_BUCKET = ENV("S3_BUCKET");

function normPrefix(p) {
  if (!p) return "";
  p = String(p).trim().replace(/^\/+/, "");
  p = p.replace(/\/+$/, "");
  return p ? p + "/" : "";
}
var S3_PREFIX = normPrefix(ENV("S3_PREFIX", "transcripts/"));

var S3_FORMAT = ENV("S3_FORMAT", "txt").toLowerCase();
var S3_FLUSH_SECS = parseInt(ENV("S3_FLUSH_SECS", "30"), 10);
var S3_ALWAYS_UPLOAD = ENV("S3_ALWAYS_UPLOAD", "1") !== "0";

var MEETING_ID = ENV("MEETING_ID");
var JOB_ID = ENV("JOB_ID");
var CALLBACK_URL = ENV("CALLBACK_URL");

var s3 = new S3Client({ region: AWS_REGION });

(function bootDiag() {
  function mask(v) {
    return v ? v.slice(0, 4) + "…" + v.slice(-4) : "(unset)";
  }
  log.info(
    "bridge boot | provider=deepgram pcm=" +
      TCP_HOST +
      ":" +
      TCP_PORT +
      " partials=" +
      (ASR_LOG_PARTIALS ? "on" : "off") +
      " quiet=" +
      (ENV("QUIET", "0") === "1")
  );
  log.info("deepgram url=" + DG_URL);
  log.info("deepgram key=" + mask(DG_KEY));

  if (S3_BUCKET) {
    log.info(
      "s3=s3://" +
        S3_BUCKET +
        "/" +
        (S3_PREFIX || "") +
        " fmt=" +
        S3_FORMAT +
        " flush=" +
        (S3_FLUSH_SECS ? S3_FLUSH_SECS + "s" : "on-exit")
    );
  } else {
    log.warn("s3 disabled (no bucket)");
  }
})();

var activeSock = dgram.createSocket("udp4");
var currentActive = { ids: [], since: Date.now() };
activeSock.on("message", function (msg) {
  var m = msg.toString();
  if (m.indexOf("active=") === 0) {
    var ids = m.slice(7).split(",").filter(Boolean);
    currentActive = { ids: ids, since: Date.now() };
  }
});
activeSock.bind(7100, "127.0.0.1");

var nameSock = dgram.createSocket("udp4");
var nameMap = new Map();
nameSock.on("message", function (msg) {
  var m = msg.toString();
  if (m.indexOf("map=") === 0) {
    var items = m.slice(4).split("|");
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var sp = it.indexOf(":");
      if (sp > 0) {
        var uid = it.slice(0, sp);
        var name = it.slice(sp + 1) || "User";
        if (uid) nameMap.set(uid, name);
      }
    }
  }
});
nameSock.bind(7101, "127.0.0.1");

function currentSpeakerLabel() {
  var uid = currentActive.ids[0] || "";
  var name = uid ? nameMap.get(uid) || "User " + uid : "Unknown";
  return { uid: uid, name: name };
}

var FRAME = 640;
function downsample32kTo16k(buf32k) {
  var samp = new Int16Array(
    buf32k.buffer,
    buf32k.byteOffset,
    buf32k.length / 2
  );
  var out = new Int16Array(Math.floor(samp.length / 2));
  for (var i = 0, j = 0; j < out.length && i + 1 < samp.length; i += 2, j++) {
    out[j] = ((samp[i] + samp[i + 1]) / 2) | 0;
  }
  return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

//transcript buffer and S3
var txtBuffer = "";
var jsonlBuffer = [];
var flushTimer = null;
var startedAt = new Date();
var callbackSent = false;
var lastPut = null;

function iso() {
  return new Date().toISOString();
}

function objectKeyBase() {
  var base = S3_PREFIX;
  var meet = (MEETING_ID || "").trim();
  var ts = startedAt.toISOString().replace(/[:.]/g, "-");
  return base + (meet ? meet + "-" : "") + ts;
}
function s3Key() {
  return S3_FORMAT === "jsonl"
    ? objectKeyBase() + ".jsonl"
    : objectKeyBase() + ".txt";
}

async function uploadToS3(body, contentType) {
  if (!S3_BUCKET) return null;
  var Key = s3Key();
  await s3.send(
    new PutObjectCommand({
      Bucket: S3_BUCKET,
      Key: Key,
      Body: body,
      ContentType: contentType,
    })
  );
  log.info("uploaded s3://" + S3_BUCKET + "/" + Key);
  lastPut = { bucket: S3_BUCKET, key: Key };
  return lastPut;
}

async function flushToS3(opts) {
  opts = opts || {};
  var force = !!opts.force;
  if (!S3_BUCKET) return null;

  if (S3_FORMAT === "jsonl") {
    if (!jsonlBuffer.length && !force) return null;
    var body = jsonlBuffer.length
      ? jsonlBuffer
          .map(function (o) {
            return JSON.stringify(o);
          })
          .join("\n") + "\n"
      : JSON.stringify({ ts: iso(), note: "empty-session" }) + "\n";
    return await uploadToS3(body, "application/x-ndjson");
  } else {
    if (!txtBuffer && !force) return null;
    var str = txtBuffer || "[empty session] " + iso() + "\n";
    return await uploadToS3(str, "text/plain; charset=utf-8");
  }
}

function startPeriodicFlush() {
  if (S3_FLUSH_SECS > 0 && !flushTimer) {
    flushTimer = setInterval(function () {
      flushToS3().catch(function (e) {
        log.error("periodic flush error: " + e.message);
      });
    }, S3_FLUSH_SECS * 1000);
  }
}
function stopPeriodicFlush() {
  if (flushTimer) clearInterval(flushTimer);
  flushTimer = null;
}

function onTranscript(isFinal, text) {
  var spk = currentSpeakerLabel();
  if (isFinal || ASR_LOG_PARTIALS) {
    var line =
      "[" +
      (isFinal ? "FINAL" : "PARTIAL") +
      "][" +
      spk.name +
      "] " +
      text +
      "\n";
    try {
      process.stdout.write(line);
    } catch (_) {}
    if (S3_FORMAT === "jsonl") {
      jsonlBuffer.push({
        ts: iso(),
        final: !!isFinal,
        uid: spk.uid,
        speaker: spk.name,
        text: text,
      });
    } else {
      txtBuffer += line;
    }
  }
}

//callback helpers
function postJson(urlStr, payload) {
  return new Promise(function (resolve) {
    if (!urlStr) return resolve(false);
    try {
      var u = new URL(urlStr);
      var lib = u.protocol === "https:" ? https : http;
      var req = lib.request(
        {
          hostname: u.hostname,
          port: u.port || (u.protocol === "https:" ? 443 : 80),
          path: u.pathname + (u.search || ""),
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
        function (res) {
          res.on("data", function () {});
          res.on("end", function () {
            resolve(res.statusCode >= 200 && res.statusCode < 300);
          });
        }
      );
      req.on("error", function () {
        resolve(false);
      });
      req.end(JSON.stringify(payload));
    } catch (_) {
      resolve(false);
    }
  });
}

async function makePresigned({ bucket, key, expiresSec = 3600 }) {
  try {
    return await getSignedUrl(
      s3,
      new GetObjectCommand({ Bucket: bucket, Key: key }),
      { expiresIn: expiresSec }
    );
  } catch (e) {
    log.warn("presign failed: " + (e.message || String(e)));
    return null;
  }
}

async function sendCallbackOnce(status) {
  if (callbackSent) return;
  callbackSent = true;

  if (!CALLBACK_URL) {
    log.warn("no CALLBACK_URL set; skipping callback");
    return;
  }

  let presignedUrl = null;
  if (lastPut && lastPut.bucket && lastPut.key) {
    presignedUrl = await makePresigned({
      bucket: lastPut.bucket,
      key: lastPut.key,
      expiresSec: 3600,
    });
  }

  const payload = {
    jobId: JOB_ID || null,
    meetingId: MEETING_ID || null,
    status: status || "done",
    presignedUrl,
  };

  const ok = await postJson(CALLBACK_URL, payload);
  log.info("callback -> " + CALLBACK_URL + " (" + (ok ? "ok" : "failed") + ")");
}

function BaseProvider() {
  this.ws = null;
  this.backoff = RECONNECT_MIN_MS;
  this.closedByUs = false;
}
BaseProvider.prototype.nextBackoff = function () {
  var jitter = Math.floor(Math.random() * (this.backoff / 4));
  var ms = Math.min(RECONNECT_MAX_MS, this.backoff + jitter);
  this.backoff = Math.min(RECONNECT_MAX_MS, Math.floor(this.backoff * 1.8));
  return ms;
};
BaseProvider.prototype.connect = async function () {};
BaseProvider.prototype.sendFrame = function (_buf) {};
BaseProvider.prototype.stop = async function () {
  this.closedByUs = true;
  if (this.ws && this.ws.readyState === WebSocket.OPEN) {
    try {
      this.ws.close(1000, "client shutdown");
    } catch (_) {}
  }
};

function DeepgramProvider(url, apiKey) {
  BaseProvider.call(this);
  this.url = url;
  this.apiKey = apiKey;
}
DeepgramProvider.prototype = Object.create(BaseProvider.prototype);
DeepgramProvider.prototype.constructor = DeepgramProvider;

DeepgramProvider.prototype.connect = async function () {
  if (!this.apiKey) throw new Error("Missing DEEPGRAM_API_KEY");
  await this._loop();
};
DeepgramProvider.prototype._loop = function () {
  var self = this;
  return new Promise(function (resolve, reject) {
    var headers = { Authorization: "Token " + self.apiKey };
    log.info("ws -> " + self.url);
    self.ws = new WebSocket(self.url, {
      perMessageDeflate: false,
      headers: headers,
    });
    var resolved = false;

    self.ws.once("open", function () {
      self.backoff = RECONNECT_MIN_MS;
      log.info("ws open (deepgram)");
      if (!resolved) {
        resolved = true;
        resolve();
      }
    });

    self.ws.on("message", function (m) {
      try {
        var data = JSON.parse(m);
        var transcript = "";
        if (
          data &&
          data.channel &&
          data.channel.alternatives &&
          data.channel.alternatives[0]
        ) {
          transcript = data.channel.alternatives[0].transcript || "";
        } else if (data && data.alternatives && data.alternatives[0]) {
          transcript = data.alternatives[0].transcript || "";
        }
        var isFinal = !!(
          data &&
          (data.is_final === true || data.speech_final === true)
        );
        if (transcript) onTranscript(isFinal, transcript);
      } catch (_) {}
    });

    self.ws.on("close", function (code, reason) {
      log.warn("ws close (deepgram) code=" + code + " reason=" + reason);
      if (self.closedByUs) return;
      var wait = self.nextBackoff();
      throttleConnErr(function () {
        log.warn("reconnect in " + wait + "ms…");
      });
      setTimeout(function () {
        self._loop().catch(function () {});
      }, wait);
    });

    self.ws.on("error", function (err) {
      throttleConnErr(function () {
        log.error("ws error (deepgram): " + err.message);
      });
      if (!resolved) {
        resolved = true;
        reject(err);
      }
    });
  });
};
DeepgramProvider.prototype.sendFrame = function (buf16k) {
  if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(buf16k);
};

function makeProvider() {
  return new DeepgramProvider(DG_URL, DG_KEY);
}

// pcm bridge
var provider = null;

async function startASR() {
  provider = makeProvider();
  try {
    await provider.connect();
  } catch (e) {
    log.error("initial connect failed: " + e.message);
  }
}

function startPCMServer() {
  var server = net.createServer(function (sock) {
    log.info("[pcm] bot connected");
    var frameBuf = Buffer.alloc(0);

    sock.on("data", function (chunk) {
      var ds = downsample32kTo16k(chunk);
      frameBuf = Buffer.concat([frameBuf, ds]);
      while (frameBuf.length >= FRAME) {
        var frame = frameBuf.subarray(0, FRAME);
        frameBuf = frameBuf.subarray(FRAME);
        if (provider) provider.sendFrame(frame);
      }
    });

    sock.on("end", async function () {
      log.info("[pcm] bot disconnected");
      try {
        stopPeriodicFlush();
        await flushToS3({ force: S3_ALWAYS_UPLOAD });
        await sendCallbackOnce("done");
      } catch (e) {
        log.error("end flush/callback error: " + e.message);
      }
    });

    sock.on("error", function (err) {
      throttleConnErr(function () {
        log.warn("[pcm] socket error: " + err.message);
      });
    });
  });

  server.on("error", function (err) {
    throttleConnErr(function () {
      log.error("[pcm] server error: " + err.message);
    });
  });

  server.listen(TCP_PORT, TCP_HOST, function () {
    log.info("[pcm] listening on " + TCP_HOST + ":" + TCP_PORT);
  });

  startPeriodicFlush();
  return server;
}

//shutdown
var shutting = false;
async function shutdown() {
  if (shutting) return;
  shutting = true;
  log.info("bridge shutting down…");
  try {
    try {
      activeSock.close();
    } catch (_) {}
    try {
      nameSock.close();
    } catch (_) {}
    if (provider) await provider.stop();
    stopPeriodicFlush();
    await flushToS3({ force: S3_ALWAYS_UPLOAD });
    await sendCallbackOnce("done");
  } catch (e) {
    log.error("shutdown error: " + e.message);
  }
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

(async function () {
  await startASR();
  startPCMServer();
})();
