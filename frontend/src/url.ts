export function safeExternalUrl(value: string): URL | null {
  if (!value.trim()) {
    return null;
  }
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}
