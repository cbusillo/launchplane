import {
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
} from "react";

export type AppRoute =
  | { kind: "product-index" }
  | { kind: "product-workspace"; product: string }
  | { kind: "product-activity"; product: string }
  | {
      kind: "product-environment";
      product: string;
      environment: string;
      view: EnvironmentView;
    }
  | { kind: "engineering" }
  | { kind: "not-found"; path: string };

export type EnvironmentView =
  | "overview"
  | "actions"
  | "runtime-settings"
  | "managed-secrets"
  | "diagnostics";

const NAVIGATION_EVENT = "launchplane:navigation";

export function productIndexPath(): string {
  return "/ui/products";
}

export function productPath(product: string): string {
  return `${productIndexPath()}/${encodeURIComponent(product)}`;
}

export function productActivityPath(product: string): string {
  return `${productPath(product)}/activity`;
}

export function productEnvironmentPath(
  product: string,
  environment: string,
  view: EnvironmentView = "overview",
): string {
  const basePath = `${productPath(product)}/environments/${encodeURIComponent(environment)}`;
  return view === "overview" ? basePath : `${basePath}/${view}`;
}

export function engineeringPath(): string {
  return "/ui/engineering";
}

export function parseAppRoute(pathname: string): AppRoute {
  const normalizedPath = pathname.replace(/\/+$/, "") || "/";
  if (
    normalizedPath === "/" ||
    normalizedPath === "/ui" ||
    normalizedPath === productIndexPath()
  ) {
    return { kind: "product-index" };
  }
  if (normalizedPath === engineeringPath()) {
    return { kind: "engineering" };
  }
  const productPrefix = `${productIndexPath()}/`;
  if (normalizedPath.startsWith(productPrefix)) {
    const routeSegments = normalizedPath.slice(productPrefix.length).split("/");
    const product = decodeRouteSegment(routeSegments[0]);
    if (!product) {
      return { kind: "not-found", path: pathname };
    }
    if (routeSegments.length === 1) {
      return { kind: "product-workspace", product };
    }
    if (routeSegments.length === 2 && routeSegments[1] === "activity") {
      return { kind: "product-activity", product };
    }
    if (routeSegments[1] === "environments") {
      const environment = decodeRouteSegment(routeSegments[2] ?? "");
      if (!environment) {
        return { kind: "not-found", path: pathname };
      }
      if (routeSegments.length === 3) {
        return {
          kind: "product-environment",
          product,
          environment,
          view: "overview",
        };
      }
      const view = routeSegments[3] as EnvironmentView;
      if (
        routeSegments.length === 4 &&
        [
          "overview",
          "actions",
          "runtime-settings",
          "managed-secrets",
          "diagnostics",
        ].includes(view)
      ) {
        return { kind: "product-environment", product, environment, view };
      }
    }
  }
  return { kind: "not-found", path: pathname };
}

export function routeProductKey(route: AppRoute): string {
  return route.kind === "product-workspace" ||
    route.kind === "product-activity" ||
    route.kind === "product-environment"
    ? route.product
    : "";
}

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

export function routeHref(path: string): string {
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

function decodeRouteSegment(value: string): string {
  if (!value) {
    return "";
  }
  try {
    const decoded = decodeURIComponent(value).trim();
    return decoded && !decoded.includes("/") ? decoded : "";
  } catch {
    return "";
  }
}
