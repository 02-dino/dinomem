/**
 * cloudflare-worker-template.js
 * External watchdog for OpenClaw gateway — runs on Cloudflare Workers (free tier).
 *
 * Required env vars (set in Cloudflare Dashboard -> Workers -> Settings -> Variables):
 *   GATEWAY_HEALTH_URL    URL of the gateway health endpoint, e.g. https://your-box.example.com/health
 *   RESTART_WEBHOOK_URL   Webhook to POST when consecutive failures reach threshold.
 *                         The webhook must trigger: systemctl restart openclaw-analyst (or equivalent).
 *   KV_NAMESPACE          KV namespace binding name (set in wrangler.toml or dashboard as KV binding "WATCHDOG_KV").
 *
 * Cron Trigger (set in dashboard -> Triggers -> Cron Triggers):
 *   */5 * * * *   (every 5 minutes)
 *
 * KV key used:  "consecutive_failures"  (integer, reset to 0 on success)
 *
 * Threshold: CONSECUTIVE_FAILURES_THRESHOLD (default 2 = 10 minutes of down before restart).
 */

const CONSECUTIVE_FAILURES_THRESHOLD = 2;
const HEALTH_TIMEOUT_MS = 8000;

export default {
  /**
   * Scheduled handler — invoked by Cron Trigger.
   * @param {ScheduledEvent} event
   * @param {Object} env
   * @param {ExecutionContext} ctx
   */
  async scheduled(event, env, ctx) {
    const healthUrl = env.GATEWAY_HEALTH_URL;
    const webhookUrl = env.RESTART_WEBHOOK_URL;
    const kv = env.WATCHDOG_KV;

    if (!healthUrl || !webhookUrl || !kv) {
      console.error(
        "[watchdog] Missing required env: GATEWAY_HEALTH_URL, RESTART_WEBHOOK_URL, or WATCHDOG_KV binding"
      );
      return;
    }

    const isHealthy = await checkHealth(healthUrl);

    if (isHealthy) {
      await kv.put("consecutive_failures", "0");
      console.log("[watchdog] PASS: gateway healthy at " + healthUrl);
      return;
    }

    // Increment consecutive failure counter
    const prev = parseInt((await kv.get("consecutive_failures")) || "0", 10);
    const current = prev + 1;
    await kv.put("consecutive_failures", String(current));
    console.warn(
      `[watchdog] FAIL: gateway unhealthy. consecutive_failures=${current} (threshold=${CONSECUTIVE_FAILURES_THRESHOLD})`
    );

    if (current >= CONSECUTIVE_FAILURES_THRESHOLD) {
      console.warn("[watchdog] THRESHOLD REACHED — triggering restart webhook");
      await triggerRestart(webhookUrl);
      // Reset counter so we don't spam the webhook every 5 minutes
      await kv.put("consecutive_failures", "0");
    }
  },
};

/**
 * Probe the gateway health endpoint.
 * Returns true if HTTP 200 received within HEALTH_TIMEOUT_MS.
 */
async function checkHealth(url) {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
    const resp = await fetch(url, { signal: controller.signal, method: "GET" });
    clearTimeout(timer);
    if (resp.status === 200) {
      return true;
    }
    console.warn("[watchdog] health endpoint returned HTTP " + resp.status);
    return false;
  } catch (err) {
    console.warn("[watchdog] health check failed: " + err.message);
    return false;
  }
}

/**
 * POST to the restart webhook. Logs but does not throw on failure —
 * a watchdog must not crash on a secondary error.
 */
async function triggerRestart(url) {
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "restart", service: "openclaw-gateway", triggered_by: "cloudflare-watchdog" }),
    });
    console.log("[watchdog] restart webhook responded HTTP " + resp.status);
  } catch (err) {
    console.error("[watchdog] restart webhook call failed: " + err.message);
  }
}
