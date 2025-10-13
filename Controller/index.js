require("dotenv").config({ path: "/home/ubuntu/app/.env" });

process.on("uncaughtException", (e) => {
  console.error("[uncaughtException]", e);
  process.exit(1);
});
process.on("unhandledRejection", (e) => {
  console.error("[unhandledRejection]", e);
  process.exit(1);
});
console.log(
  `[boot] pid=${process.pid} node=${process.version} cwd=${process.cwd()}`
);

const express = require("express");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const http = require("http");
const https = require("https");

const app = express();
const PORT = process.env.PORT || 8081;

const REGION = process.env.REGION || "us-east-1";
const ACCOUNT = process.env.ACCOUNT || "205624770703";
const REPO = process.env.REPO || "zoombot";
const DEFAULT_ECR_IMAGE = `${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:latest`;
const IMAGE = process.env.IMAGE || DEFAULT_ECR_IMAGE;
const ECR_LOGIN = (process.env.ECR_LOGIN || "1") !== "0";

const FIXED_CALLBACK_URL = process.env.CONTROLLER_CALLBACK_URL || "";
const SUMMARIZER_URL = process.env.SUMMARIZER_URL || "";

const hostBase = process.cwd();
const recHost = path.join(hostBase, "MeetingScribe-BotSDK/recordings");
const logsHost = path.join(hostBase, "MeetingScribe-BotSDK/zoomsdk-logs");
for (const dir of [recHost, logsHost]) fs.mkdirSync(dir, { recursive: true });

app.use(express.json());

const jobs = new Map();

const JOB_TTL_MS = 24 * 60 * 60 * 1000;

app.use((req, _res, next) => {
  console.log(`[http] ${req.method} ${req.path}`);
  next();
});

app.get("/health", (_req, res) => res.status(200).json({ status: "ok" }));

app.post("/bot/callback", (req, res) => {
  const { jobId, meetingId, presignedUrl, status } = req.body || {};
  console.log("📩 [callback] recv", {
    jobId,
    meetingId,
    hasPresigned: !!presignedUrl,
    status,
  });
  res.status(200).json({ ok: true });

  if (!jobId) return;
  const cur = jobs.get(jobId);
  if (!cur) {
    console.warn("📩 [callback] unknown jobId", jobId);
    return;
  }
  if (presignedUrl) cur.presignedUrl = presignedUrl;
  if (meetingId) cur.meetingId = meetingId;
  cur.status = status || cur.status || "running";
  jobs.set(jobId, cur);

  console.log("📋 [jobs] after callback", jobId, {
    meetingId: cur.meetingId,
    status: cur.status,
    hasPresigned: !!cur.presignedUrl,
    hasResult: !!cur.result,
  });
});

// Start bot
app.post("/bot/start", async (req, res) => {
  try {
    const { id, password } = req.body || {};
    if (!id || !password) {
      console.warn("[start] missing id/password");
      return res
        .status(400)
        .json({ ok: false, error: "id and password required" });
    }

    const meetingId = String(id).replace(/\s+/g, "");
    const passcode = String(password).replace(/\s+/g, "");
    const jobId = genJobId();
    const clientToken = genClientToken();

    const callbackUrl =
      FIXED_CALLBACK_URL || `${req.protocol}://${req.get("host")}/bot/callback`;

    const now = Date.now();
    jobs.set(jobId, {
      meetingId,
      presignedUrl: null,
      clientToken,
      status: "starting",
      result: null,
      expiresAt: now + JOB_TTL_MS,
    });

    console.log("[start] job created", {
      jobId,
      meetingId,
      callbackUrl,
      image: IMAGE,
    });

    const { child, dockerArgs, s3Prefix } = await startMeeting({
      meetingId,
      passcode,
      jobId,
      callbackUrl,
    });

    res.status(202).json({
      ok: true,
      message: "Starting meeting",
      meetingId,
      jobId,
      clientToken,
      s3Prefix,
      image: IMAGE,
      ecrLogin: !!ECR_LOGIN,
      dockerArgs,
    });

    child.stdout.on("data", (d) => process.stdout.write(`[bot ${jobId}] ${d}`));
    child.stderr.on("data", (d) => process.stderr.write(`[bot ${jobId}] ${d}`));

    child.on("close", async (code) => {
      console.log(`[bot ${jobId}] docker exited`, { code });

      const rec = jobs.get(jobId);
      if (!rec) {
        console.warn(`[bot ${jobId}] record missing at close`);
        return;
      }

      if (!rec.presignedUrl) {
        console.warn(`[bot ${jobId}] missing presignedUrl; cannot summarize`);
        rec.status = "error";
        rec.result = { ok: false, error: "missing_presigned_url" };
        jobs.set(jobId, rec);
        scheduleCleanup(jobId);
        return;
      }

      try {
        rec.status = "summarizing";
        jobs.set(jobId, rec);
        console.log(`[summ] calling summarizer`, {
          jobId,
          meetingId: rec.meetingId,
          urlLen: rec.presignedUrl.length,
          target: SUMMARIZER_URL || "http://localhost:9090/summarize",
        });

        const t0 = Date.now();
        const summary = await callSummarizer({
          jobId,
          meetingId: rec.meetingId || meetingId,
          presignedUrl: rec.presignedUrl,
        });
        const ms = Date.now() - t0;

        console.log(`[summ] response`, {
          jobId,
          tookMs: ms,
          ok: !!summary?.ok,
          hasPreview: !!summary?.summaryPreview,
          summaryKey: summary?.summaryKey,
          error: summary?.error,
        });

        rec.status = summary?.ok ? "complete" : "error";
        rec.result = summary || { ok: false, error: "summarizer_no_payload" };
        jobs.set(jobId, rec);
      } catch (e) {
        console.error(`[summ] error jobId=${jobId}`, e);
        rec.status = "error";
        rec.result = { ok: false, error: e.message || "summarizer_failed" };
        jobs.set(jobId, rec);
      } finally {
        console.log("[jobs] finalize", jobId, {
          status: jobs.get(jobId)?.status,
          hasResult: !!jobs.get(jobId)?.result,
        });
        scheduleCleanup(jobId);
      }
    });

    child.on("error", (err) =>
      console.error(`[bot ${jobId}] failed to start docker: ${err.message}`)
    );
  } catch (e) {
    console.error("start error:", e);
    res.status(500).json({ ok: false, error: e.message });
  }
});

//UI feedback
app.get("/bot/result", (req, res) => {
  const jobId = String(req.query.jobId || "");
  const token = req.get("X-Client-Token") || "";

  if (!jobId)
    return res.status(400).json({ ok: false, error: "jobId required" });

  const rec = jobs.get(jobId);
  if (!rec) {
    console.warn("[result] job not found", jobId);
    return res.status(404).json({ ok: false, error: "job not found" });
  }

  const tokenOk = !!token && token === rec.clientToken;
  if (!tokenOk) {
    console.warn("[result] forbidden (bad token)", { jobId });
    return res.status(403).json({ ok: false, error: "forbidden" });
  }

  console.log("[result] serve", {
    jobId,
    status: rec.status,
    hasPresigned: !!rec.presignedUrl,
    hasResult: !!rec.result,
  });

  return res.status(200).json({
    ok: true,
    jobId,
    status: rec.status,
    presignedUrl: rec.presignedUrl || null,
    result: rec.result || null,
  });
});

app.get("/bot/debug/:jobId", (req, res) => {
  const { jobId } = req.params || {};
  const rec = jobs.get(jobId);
  if (!rec) return res.status(404).json({ ok: false, error: "job not found" });

  const out = {
    meetingId: rec.meetingId,
    status: rec.status,
    hasPresigned: !!rec.presignedUrl,
    hasResult: !!rec.result,
    result: rec.result,
    expiresAt: rec.expiresAt,
  };
  console.log("[debug] job", jobId, out);
  res.status(200).json({ ok: true, jobId, ...out });
});

// helpers
function genJobId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${crypto.randomBytes(6).toString("hex")}`;
}
function genClientToken() {
  return crypto.randomBytes(32).toString("hex");
}
function scheduleCleanup(jobId) {
  const rec = jobs.get(jobId);
  if (!rec) return;
  const delay = Math.max(
    5_000,
    (rec.expiresAt || Date.now() + JOB_TTL_MS) - Date.now()
  );
  setTimeout(() => {
    jobs.delete(jobId);
    console.log("[jobs] cleaned", jobId);
  }, delay);
}

async function startMeeting({ meetingId, passcode, jobId, callbackUrl }) {
  if (ECR_LOGIN && IMAGE === DEFAULT_ECR_IMAGE) {
    await dockerLoginECR();
  }

  const s3Prefix = `jobs/${jobId}/meetings/${meetingId}/transcripts/`;

  const dockerArgs = [
    "run",
    "--platform=linux/amd64",
    "--rm",
    "-e",
    "SDK_SYNC=move",
    "-e",
    "SUPERVISE=0",
    "-e",
    "ASR_PROVIDER=deepgram",
    "-e",
    `DEEPGRAM_API_KEY=${process.env.DEEPGRAM_API_KEY || ""}`,
    "-e",
    `S3_BUCKET=${process.env.S3_BUCKET || ""}`,
    "-e",
    `AWS_REGION=${process.env.AWS_REGION || "us-east-1"}`,
    ...(process.env.DEEPGRAM_URL
      ? ["-e", `DEEPGRAM_URL=${process.env.DEEPGRAM_URL}`]
      : []),
    "-e",
    `JOB_ID=${jobId}`,
    "-e",
    `MEETING_ID=${meetingId}`,
    "-e",
    `CALLBACK_URL=${callbackUrl}`,
    "-e",
    `S3_PREFIX=${s3Prefix}`,
    "-e",
    "ASR_LOG_PARTIALS=1",
    "-e",
    "LOG_LEVEL=info",
    "-v",
    `${recHost}:/app/MeetingScribe-BotSDK/build`,
    "-v",
    `${logsHost}:/app/.zoomsdk`,
    IMAGE,
    meetingId,
    passcode,
  ];

  console.log("[docker] run", {
    jobId,
    image: IMAGE,
    args: dockerArgs.slice(0, 6).concat("..."),
  });

  const child = spawn("docker", dockerArgs, { stdio: "pipe" });
  return { child, dockerArgs, s3Prefix };
}

function dockerLoginECR() {
  return new Promise((resolve, reject) => {
    const ecr = `${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com`;
    const cmd = `aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ecr}`;

    const env = { ...process.env };
    delete env.AWS_ACCESS_KEY_ID;
    delete env.AWS_SECRET_ACCESS_KEY;
    delete env.AWS_SESSION_TOKEN;
    delete env.AWS_PROFILE;
    delete env.AWS_DEFAULT_PROFILE;
    delete env.AWS_SHARED_CREDENTIALS_FILE;
    delete env.AWS_CONFIG_FILE;

    const login = spawn("bash", ["-lc", cmd], { stdio: "pipe", env });
    let stderr = "";
    login.stderr.on("data", (d) => (stderr += d.toString()));
    login.on("close", (code) => {
      if (code === 0) return resolve();
      reject(new Error(`ECR login failed (code=${code}): ${stderr.trim()}`));
    });
    login.on("error", (err) => reject(err));
  });
}

function callSummarizer({ jobId, meetingId, presignedUrl }) {
  return new Promise((resolve) => {
    const target = SUMMARIZER_URL || "http://localhost:9090/summarize";
    const u = new URL(target);
    const lib = u.protocol === "https:" ? https : http;

    const req = lib.request(
      {
        hostname: u.hostname,
        port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: u.pathname + (u.search || ""),
        method: "POST",
        headers: { "Content-Type": "application/json" },
      },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(body);
            resolve(parsed);
          } catch {
            resolve({ ok: false, error: "summarizer_non_json", raw: body });
          }
        });
      }
    );

    req.on("error", (e) => {
      console.error("summarizer call failed:", e.message);
      resolve({ ok: false, error: e.message });
    });

    req.end(JSON.stringify({ jobId, meetingId, presignedUrl }));
  });
}

const server = app.listen(PORT, () =>
  console.log(`Controller listening on :${PORT}`)
);
server.on("error", (err) => {
  console.error("[listen error]", err);
  process.exit(1);
});
