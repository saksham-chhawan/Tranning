#!/usr/bin/env node
/**
 * Calls 3 public APIs with robust logging + retries.
 *
 * APIs:
 * 1) GitHub:  https://api.github.com/repos/nodejs/node
 * 2) Dog CEO: https://dog.ceo/api/breeds/image/random
 * 3) CoinDesk:https://api.coindesk.com/v1/bpi/currentprice.json
 *
 * Logs (JSON Lines) to: api_calls.log
 */

import fs from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const LOG_FILE = "api_calls.log";

function utcNowIso() {
  return new Date().toISOString();
}

function logEvent(event) {
  const line = JSON.stringify({ ts: utcNowIso(), ...event });
  fs.appendFileSync(LOG_FILE, line + "\n", "utf8");
}

function isRetryableStatus(status) {
  return [408, 429, 500, 502, 503, 504].includes(status);
}

async function fetchWithRetries(url, options = {}) {
  const {
    maxRetries = 4,
    baseDelayMs = 600,
    timeoutMs = 10_000,
    ...fetchOptions
  } = options;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const attemptNo = attempt + 1;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

    logEvent({
      type: "request",
      attempt: attemptNo,
      method: fetchOptions.method || "GET",
      url,
      headers: fetchOptions.headers || {},
      timeoutMs,
    });

    try {
      const res = await fetch(url, { ...fetchOptions, signal: controller.signal });
      const bodyText = await res.text();

      logEvent({
        type: "response",
        attempt: attemptNo,
        method: fetchOptions.method || "GET",
        url,
        status: res.status,
        headers: Object.fromEntries(res.headers.entries()),
        bodyPreview: bodyText.slice(0, 2000),
      });

      if (res.ok) return { res, bodyText, error: null };

      if (isRetryableStatus(res.status) && attempt < maxRetries) {
        const jitter = Math.floor(Math.random() * 250);
        const delay = baseDelayMs * (2 ** attempt) + jitter;
        logEvent({ type: "retry", reason: `HTTP ${res.status}`, attempt: attemptNo, sleepMs: delay });
        await sleep(delay);
        continue;
      }

      return { res, bodyText, error: new Error(`HTTP ${res.status} for ${url}`) };
    } catch (err) {
      const name = err?.name || "Error";
      logEvent({
        type: "error",
        attempt: attemptNo,
        method: fetchOptions.method || "GET",
        url,
        errorClass: name,
        error: String(err),
      });

      const retryable = name === "AbortError" || name === "TypeError";
      if (retryable && attempt < maxRetries) {
        const jitter = Math.floor(Math.random() * 250);
        const delay = baseDelayMs * (2 ** attempt) + jitter;
        logEvent({ type: "retry", reason: name, attempt: attemptNo, sleepMs: delay });
        await sleep(delay);
        continue;
      }

      return { res: null, bodyText: null, error: err };
    } finally {
      clearTimeout(timeoutId);
    }
  }

  return { res: null, bodyText: null, error: new Error("Unexpected retry loop exit") };
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return { _nonJsonBody: text };
  }
}

async function main() {
    const apis = [
    ["GitHub repo", "https://api.github.com/repos/nodejs/node"],
    ["GitHub zen", "https://api.github.com/zen"],
    ["GitHub rate limit", "https://api.github.com/rate_limit"],
    ];

  const results = {};

  for (const [name, url] of apis) {
    const { res, bodyText, error } = await fetchWithRetries(url, {
      headers: { "User-Agent": "public-api-demo/1.0" },
      maxRetries: 4,
      baseDelayMs: 600,
      timeoutMs: 10_000,
    });

    if (error) {
      results[name] = { ok: false, error: String(error) };
      continue;
    }

    results[name] = { ok: true, status: res.status, data: safeJson(bodyText) };
  }

  console.log(JSON.stringify(results, null, 2));
}

main().catch((e) => {
  logEvent({ type: "fatal", error: String(e) });
  process.exitCode = 1;
});