const defaultAllowedHosts = [
  "www.xiaohongshu.com",
  "xiaohongshu.com"
];

const mutationTimes = [];

export function getAllowedHosts() {
  const raw = process.env.ALLOWED_BROWSER_HOSTS;
  if (!raw) return defaultAllowedHosts;
  return raw
    .split(",")
    .map((host) => host.trim().toLowerCase())
    .filter(Boolean);
}

export function assertAllowedUrl(inputUrl) {
  let parsed;
  try {
    parsed = new URL(inputUrl);
  } catch {
    const error = new Error("Invalid URL.");
    error.statusCode = 400;
    throw error;
  }

  if (!["http:", "https:"].includes(parsed.protocol)) {
    const error = new Error("Only http and https URLs are allowed.");
    error.statusCode = 400;
    throw error;
  }

  const hostname = parsed.hostname.toLowerCase();
  const allowedHosts = getAllowedHosts();
  const allowed = allowedHosts.some((host) => {
    return hostname === host || hostname.endsWith(`.${host}`);
  });

  if (!allowed) {
    const error = new Error(`Host is not allowlisted: ${hostname}`);
    error.statusCode = 403;
    error.details = { allowedHosts };
    throw error;
  }

  return parsed.toString();
}

export function getMutationPolicy() {
  return {
    mutationsEnabled: process.env.ALLOW_BROWSER_MUTATIONS === "1",
    trustedAgentActions: process.env.TRUST_AGENT_ACTIONS === "1",
    maxPerMinute: Number(process.env.MUTATION_MAX_PER_MINUTE || 30),
    minIntervalMs: Number(process.env.MUTATION_MIN_INTERVAL_MS || 1200),
    maxTypeChars: Number(process.env.MAX_TYPE_CHARS || 2000)
  };
}

function assertRateLimit(policy) {
  const now = Date.now();
  const cutoff = now - 60000;

  while (mutationTimes.length && mutationTimes[0] < cutoff) {
    mutationTimes.shift();
  }

  const lastActionAt = mutationTimes[mutationTimes.length - 1] || 0;
  if (now - lastActionAt < policy.minIntervalMs) {
    const error = new Error(`Mutation rate limit: wait at least ${policy.minIntervalMs}ms between actions.`);
    error.statusCode = 429;
    throw error;
  }

  if (mutationTimes.length >= policy.maxPerMinute) {
    const error = new Error(`Mutation rate limit: max ${policy.maxPerMinute} actions per minute.`);
    error.statusCode = 429;
    throw error;
  }

  mutationTimes.push(now);
}

export function assertMutationAllowed(body, action = "mutation") {
  const policy = getMutationPolicy();

  if (process.env.ALLOW_BROWSER_MUTATIONS !== "1") {
    const error = new Error("Browser mutations are disabled. Set ALLOW_BROWSER_MUTATIONS=1 to allow click/type.");
    error.statusCode = 403;
    throw error;
  }

  if (policy.trustedAgentActions !== true && (!body || body.confirm !== true)) {
    const error = new Error("Mutation requires confirm: true.");
    error.statusCode = 403;
    throw error;
  }

  if (action === "type" && String(body?.text || "").length > policy.maxTypeChars) {
    const error = new Error(`Typed text is too long. MAX_TYPE_CHARS is ${policy.maxTypeChars}.`);
    error.statusCode = 413;
    throw error;
  }

  assertRateLimit(policy);
}
