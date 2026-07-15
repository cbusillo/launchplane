export type DevFixtureMode =
  | "products"
  | "empty"
  | "error"
  | "missing"
  | "denied"
  | "";

type DevFixturesModule = typeof import("./dev-fixtures");

export function readDevFixtureMode(): DevFixtureMode {
  if (!import.meta.env.DEV) {
    return "";
  }
  const fixture = new URLSearchParams(window.location.search).get("fixture");
  return fixture === "products" ||
    fixture === "empty" ||
    fixture === "error" ||
    fixture === "missing" ||
    fixture === "denied"
    ? fixture
    : "";
}

export async function loadDevFixtures(): Promise<DevFixturesModule> {
  if (!import.meta.env.DEV) {
    throw new Error("Development fixtures are unavailable in production builds.");
  }
  const fixtureModulePath = `${import.meta.env.BASE_URL}src/dev-fixtures.ts`;
  return import(/* @vite-ignore */ fixtureModulePath) as Promise<DevFixturesModule>;
}
