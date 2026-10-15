// Failure watchdogs only. Browser tests observe state or drive controlled time.
export const browserDeadlines = {
  assertion: 10_000,
  test: 30_000,
  server: 120_000,
};

// An isolation probe can reach its peer only after that peer starts its server
// and finishes its browser journey. Keep the whole lifecycle in one envelope.
export const isolationDeadline = browserDeadlines.server + browserDeadlines.test;
