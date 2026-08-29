export async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
    ...(options.headers || {}),
  };
  const canReplay = Boolean(headers["Idempotency-Key"] || headers["idempotency-key"]);
  let response;
  for (let attempt = 0; attempt < (canReplay ? 2 : 1); attempt += 1) {
    try {
      response = await fetch(path, {
        ...options,
        method,
        headers,
        credentials: "same-origin",
      });
    } catch (error) {
      if (!canReplay || attempt > 0 || !(error instanceof TypeError)) throw error;
      continue;
    }
    if (attempt === 0 && [502, 503, 504].includes(response.status)) continue;
    break;
  }
  if (!response) throw new Error("Network request failed");
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

export async function apiBlob(path, options = {}) {
  return (await apiBlobResponse(path, options)).blob;
}

export async function apiBlobResponse(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    method: (options.method || "GET").toUpperCase(),
    credentials: "same-origin",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return { blob: await response.blob(), headers: response.headers };
}
