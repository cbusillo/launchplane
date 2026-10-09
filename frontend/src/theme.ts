export type Theme = "dark" | "light";

declare global {
  interface Window {
    launchplaneTheme: {
      get: () => Theme;
      set: (theme: Theme) => void;
      subscribe: (listener: (theme: Theme) => void) => () => void;
    };
  }
}
