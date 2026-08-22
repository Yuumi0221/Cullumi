export default async function globalTeardown() {
  const port = Number(process.env.CULLUMI_DOM_PORT || 4173);
  const token = process.env.CULLUMI_DOM_TOKEN || "cullumi-dom-test";
  try {
    await fetch(`http://127.0.0.1:${port}/__shutdown__?token=${encodeURIComponent(token)}`, {
      method: "POST",
      signal: AbortSignal.timeout(3000),
    });
  } catch {
    // Playwright also terminates its webServer process; this endpoint lets
    // Windows exit cleanly when the server is still available.
  }
}
