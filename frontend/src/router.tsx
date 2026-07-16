import {
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
} from "react";
import { parseAppRoute, type AppRoute } from "./route-model";

export * from "./route-model";

const NAVIGATION_EVENT = "launchplane:navigation";

export function useAppRoute(): AppRoute {
  const [locationKey, setLocationKey] = useState(currentLocationKey);

  useEffect(() => {
    const updateLocation = () => setLocationKey(currentLocationKey());
    window.addEventListener("popstate", updateLocation);
    window.addEventListener(NAVIGATION_EVENT, updateLocation);
    return () => {
      window.removeEventListener("popstate", updateLocation);
      window.removeEventListener(NAVIGATION_EVENT, updateLocation);
    };
  }, []);

  return useMemo(() => {
    const location = new URL(locationKey, window.location.origin);
    return parseAppRoute(location.pathname);
  }, [locationKey]);
}

export function navigateTo(path: string, replace = false): void {
  const href = routeHref(path);
  if (replace) {
    window.history.replaceState(null, "", href);
  } else {
    window.history.pushState(null, "", href);
  }
  window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  window.dispatchEvent(new Event(NAVIGATION_EVENT));
}

function routeHref(path: string): string {
  if (!import.meta.env.DEV) {
    return path;
  }
  const fixture = new URLSearchParams(window.location.search).get("fixture");
  if (!fixture) {
    return path;
  }
  const url = new URL(path, window.location.origin);
  url.searchParams.set("fixture", fixture);
  return `${url.pathname}${url.search}${url.hash}`;
}

interface AppLinkProps
  extends Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  to: string;
}

export function AppLink({ to, onClick, ...props }: AppLinkProps) {
  const href = routeHref(to);
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      props.target === "_blank"
    ) {
      return;
    }
    event.preventDefault();
    navigateTo(to);
  };

  return <a {...props} href={href} onClick={handleClick} />;
}

function currentLocationKey(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}
