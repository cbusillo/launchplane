/**
 * Shared public health response for Node web frameworks (including Next.js).
 * Each callback must perform a real bounded operation and return true on success.
 * Honor signal AND set a deadline on the database/client operation itself.
 */
export async function dependencyHealth(checks, { timeoutMs = 1000 } = {}) {
  const names = Object.keys(checks);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 10000 ||
      names.some(name => !["database", "email", "cache"].includes(name)) ||
      names.some(name => typeof checks[name] !== "function")) {
    throw new TypeError("Invalid dependency health configuration");
  }
  const entries = await Promise.all(names.map(async name => {
    const controller = new AbortController();
    let timer;
    try {
      const result = await Promise.race([
        Promise.resolve().then(() => checks[name](controller.signal)),
        new Promise(resolve => {
          timer = setTimeout(() => { controller.abort(); resolve(false); }, timeoutMs);
        }),
      ]);
      return [name, result === true ? "pass" : "fail"];
    } catch {
      return [name, "fail"];
    } finally {
      clearTimeout(timer);
    }
  }));
  return { dependencies: Object.fromEntries(entries) };
}

export async function dependencyHealthResponse(checks, options) {
  const body = await dependencyHealth(checks, options);
  return Response.json(body, {
    status: Object.values(body.dependencies).includes("fail") ? 503 : 200,
    headers: { "Cache-Control": "no-store" },
  });
}
