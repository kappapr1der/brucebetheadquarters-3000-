const CALLBACK_PATH = "/vk/oauth/callback";
const PENDING_PATH = "/vk/oauth/pending";
const TTL_SECONDS = 15 * 60;
const STATE_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

function htmlPage(title, text, status = 200) {
  const body = `<!doctype html><meta charset="utf-8"><title>${title}</title><h1>${title}</h1><p>${text}</p>`;
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

function relayAuthorized(request, expectedSecret) {
  if (!expectedSecret || expectedSecret.length < 24) {
    return false;
  }
  return request.headers.get("Authorization") === `Bearer ${expectedSecret}`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/healthz") {
      return new Response("BruceBet VK OAuth Worker relay is ready.\n", {
        headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
      });
    }

    if (url.pathname === CALLBACK_PATH) {
      if (request.method !== "GET") {
        return new Response("Method not allowed", { status: 405, headers: { Allow: "GET" } });
      }
      const error = url.searchParams.get("error");
      if (error) {
        return htmlPage(
          "VK connection was not completed",
          "VK returned an authorization error. Return to Telegram and run /vk_connect again.",
          400,
        );
      }
      const code = url.searchParams.get("code") || "";
      const state = url.searchParams.get("state") || "";
      const deviceId = url.searchParams.get("device_id") || "";
      if (!code || !STATE_PATTERN.test(state)) {
        return htmlPage(
          "VK connection was not completed",
          "VK did not return a valid authorization response. Return to Telegram and run /vk_connect again.",
          400,
        );
      }
      try {
        await env.OAUTH_CODES.put(
          state,
          JSON.stringify({ code, device_id: deviceId || null, receivedAt: new Date().toISOString() }),
          { expirationTtl: TTL_SECONDS },
        );
      } catch {
        return htmlPage(
          "VK connection was not completed",
          "BruceBet could not securely receive the authorization response. Return to Telegram and try again.",
          503,
        );
      }
      return htmlPage(
        "BruceBet connected to VK",
        "Authorization received. BruceBet will finish the connection automatically in a few seconds.",
      );
    }

    if (url.pathname === PENDING_PATH) {
      if (request.method !== "GET") {
        return new Response("Method not allowed", { status: 405, headers: { Allow: "GET" } });
      }
      if (!relayAuthorized(request, env.RELAY_SECRET)) {
        return new Response("Unauthorized", { status: 401, headers: { "cache-control": "no-store" } });
      }
      const state = url.searchParams.get("state") || "";
      if (!STATE_PATTERN.test(state)) {
        return new Response("Not found", { status: 404, headers: { "cache-control": "no-store" } });
      }
      const pending = await env.OAUTH_CODES.get(state, "json");
      const code = pending && typeof pending.code === "string" ? pending.code : "";
      const deviceId = pending && typeof pending.device_id === "string" ? pending.device_id : "";
      if (!code) {
        return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
      }
      return Response.json({ code, device_id: deviceId || null }, { headers: { "cache-control": "no-store" } });
    }

    return new Response("Not found", { status: 404, headers: { "cache-control": "no-store" } });
  },
};
