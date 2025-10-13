require("dotenv").config({ path: "/home/ubuntu/app/.env" });

const express = require("express");
const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const MODEL = process.env.MODEL || "gpt-4o-mini";
const AWS_REGION = process.env.AWS_REGION || "us-east-1";
const S3_BUCKET = process.env.S3_BUCKET;
const PORT = process.env.PORT || 9090;
const S3_SSE = process.env.S3_SSE || "";

if (!OPENAI_API_KEY) throw new Error("Missing OPENAI_API_KEY");
if (!S3_BUCKET) throw new Error("Missing S3_BUCKET");

const s3 = new S3Client({ region: AWS_REGION });
const app = express();
app.use(express.json({ limit: "2mb" }));

const nowIso = () => new Date().toISOString();
const rid = () => Math.random().toString(36).slice(2, 10);

async function downloadText(
  url,
  { timeoutMs = 60_000, maxBytes = 10_000_000 } = {}
) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`download failed: ${res.status}`);

    const lenHeader = res.headers.get("content-length");
    if (lenHeader && Number(lenHeader) > maxBytes) {
      throw new Error(`transcript too large (content-length=${lenHeader})`);
    }

    const reader = res.body.getReader();
    const chunks = [];
    let received = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.length;
      if (received > maxBytes) throw new Error("transcript too large (stream)");
      chunks.push(value);
    }

    const buf = new Uint8Array(received);
    let off = 0;
    for (const c of chunks) {
      buf.set(c, off);
      off += c.length;
    }
    return { text: new TextDecoder("utf-8").decode(buf), bytes: received };
  } finally {
    clearTimeout(t);
  }
}

async function summarizeWithOpenAI(transcriptText) {
  const system = `You are a concise meeting summarizer.
Return a compact JSON object with this shape:
{
  "title": string,
  "bullets": string[],
  "actionItems": [{"owner": string|null, "text": string, "due": string|null}],
  "decisions": string[],
  "risks": string[]
}
Do not include any extra keys.
If some sections have no content, return empty arrays.`;

  const user = `Transcript:\n\n${transcriptText}`;

  const t0 = Date.now();
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: MODEL,
      temperature: 0.2,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    }),
  });

  const ms = Date.now() - t0;
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    throw new Error(`OpenAI error ${res.status} in ${ms}ms: ${t}`);
  }

  const data = await res.json();
  const content = data?.choices?.[0]?.message?.content || "{}";
  const parsed = JSON.parse(content);
  return { parsed, tookMs: ms };
}

async function putJSON(key, obj) {
  const body = JSON.stringify(obj, null, 2);
  const params = {
    Bucket: S3_BUCKET,
    Key: key,
    Body: body,
    ContentType: "application/json",
    Metadata: { "created-at": nowIso() },
  };
  if (S3_SSE) params.ServerSideEncryption = S3_SSE;
  await s3.send(new PutObjectCommand(params));
  return key;
}

// routes
app.post("/summarize", async (req, res) => {
  const reqId = rid();
  try {
    const { jobId, meetingId, presignedUrl } = req.body || {};
    console.log(`[summ:${reqId}] start`, {
      jobId,
      meetingId,
      urlLen: presignedUrl ? presignedUrl.length : 0,
    });

    if (!jobId || !meetingId || !presignedUrl) {
      console.warn(`[summ:${reqId}] bad request`);
      return res.status(400).json({
        ok: false,
        error: "jobId, meetingId, presignedUrl required",
      });
    }

    // download transcript
    const t0 = Date.now();
    const { text, bytes } = await downloadText(presignedUrl);
    console.log(`[summ:${reqId}] transcript`, {
      bytes,
      tookMs: Date.now() - t0,
    });

    // summarize
    const { parsed: summary, tookMs } = await summarizeWithOpenAI(text);
    console.log(`[summ:${reqId}] openai`, {
      tookMs,
      title: summary?.title,
      bullets: Array.isArray(summary?.bullets) ? summary.bullets.length : 0,
    });

    // save to S3
    const summaryKey = `summaries/${jobId}.json`;
    await putJSON(summaryKey, summary);
    console.log(`[summ:${reqId}] s3 put ok`, { summaryKey });

    const summaryPreview = {
      title: typeof summary.title === "string" ? summary.title : undefined,
      bullets: Array.isArray(summary.bullets)
        ? summary.bullets.slice(0, 5)
        : [],
    };

    const payload = {
      ok: true,
      jobId,
      meetingId,
      summaryKey,
      summaryPreview,
    };

    console.log(`[summ:${reqId}] done`, {
      jobId,
      ok: true,
      hasPreview: !!summaryPreview.title || summaryPreview.bullets.length > 0,
    });

    return res.status(200).json(payload);
  } catch (e) {
    console.error(`[summ:${reqId}] error`, e);
    return res
      .status(500)
      .json({ ok: false, error: e.message || "summarize_failed" });
  }
});

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.listen(PORT, () =>
  console.log(
    `Summarizer listening on :${PORT} (bucket=${S3_BUCKET}, sse=${
      S3_SSE || "none"
    })`
  )
);
