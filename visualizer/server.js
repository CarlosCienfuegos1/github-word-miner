import express from "express";
import { createClient } from "redis";
import { fileURLToPath } from "url";
import path from "path";
 
// ── Config (env-overridable) ──────────────────────────────────────────────────
const PORT         = parseInt(process.env.PORT         ?? "5000", 10);
const REDIS_URL    = process.env.REDIS_URL             ?? "redis://redis:6379";
const DEFAULT_TOP  = parseInt(process.env.DEFAULT_TOP_N ?? "20", 10);
const SSE_INTERVAL = parseFloat(process.env.SSE_INTERVAL ?? "3.0") * 1000; // ms
 
// ── Redis keys (must match miner.py) ─────────────────────────────────────────
const REDIS_KEYS = {
  python: "word_counts:python",
  java:   "word_counts:java",
  all:    "word_counts:all",
};
const META_KEY = "miner:meta";
 
// ── Redis client ──────────────────────────────────────────────────────────────
const redis = createClient({ url: REDIS_URL });
 
redis.on("error", (err) => console.error("[redis]", err.message));
 
await redis.connect();
console.log(`[visualizer] Connected to Redis at ${REDIS_URL}`);
 
// ── Helpers ───────────────────────────────────────────────────────────────────
 
/**
 * Return the top-N words for a given language as an array of {word, count}.
 * Uses ZREVRANGE … WITHSCORES (redis sorted set, descending by score).
 */
async function fetchTopWords(language, topN) {
  const key = REDIS_KEYS[language] ?? REDIS_KEYS.all;
  // ioredis returns [[member, score], …]; node-redis returns [{value, score}, …]
  const results = await redis.zRangeWithScores(key, 0, topN - 1, { REV: true });
  return results.map(({ value, score }) => ({ word: value, count: Math.floor(score) }));
}
 
/** Return live miner stats from the Redis hash. */
async function fetchMeta() {
  const raw = await redis.hGetAll(META_KEY);
  return {
    repos_processed: parseInt(raw.repos_processed ?? "0", 10),
    words_total:     parseInt(raw.words_total     ?? "0", 10),
    python_total:    parseInt((await redis.zCard(REDIS_KEYS.python)) ?? 0, 10),
    java_total:      parseInt((await redis.zCard(REDIS_KEYS.java))   ?? 0, 10),
  };
}
 
// ── Express app ───────────────────────────────────────────────────────────────
const app = express();
 
const __dirname = path.dirname(fileURLToPath(import.meta.url));
app.use(express.static(path.join(__dirname, "public")));
 
// ── GET /stream  (Server-Sent Events) ─────────────────────────────────────────
app.get("/stream", (req, res) => {
  const language = REDIS_KEYS[req.query.language] ? req.query.language : "all";
  const topN     = Math.min(100, Math.max(1, parseInt(req.query.top_n ?? DEFAULT_TOP, 10)));
 
  res.setHeader("Content-Type",  "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection",    "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");   // disable nginx buffering
  res.flushHeaders();
 
  let active = true;
 
  async function push() {
    if (!active) return;
    try {
      const [words, meta] = await Promise.all([
        fetchTopWords(language, topN),
        fetchMeta(),
      ]);
      const payload = JSON.stringify({ words, meta, language });
      res.write(`data: ${payload}\n\n`);
    } catch (err) {
      console.error("[sse] Redis error:", err.message);
      res.write(`data: ${JSON.stringify({ error: err.message })}\n\n`);
    }
    if (active) setTimeout(push, SSE_INTERVAL);
  }
 
  push();
 
  req.on("close", () => { active = false; });
});
 
// ── GET /api/words  (REST fallback) ──────────────────────────────────────────
app.get("/api/words", async (req, res) => {
  const language = REDIS_KEYS[req.query.language] ? req.query.language : "all";
  const topN     = Math.min(100, Math.max(1, parseInt(req.query.top_n ?? DEFAULT_TOP, 10)));
  try {
    const [words, meta] = await Promise.all([fetchTopWords(language, topN), fetchMeta()]);
    res.json({ words, meta, language });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
 
// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`[visualizer] Dashboard available at http://localhost:${PORT}`);
});