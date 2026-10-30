import { useCallback, useEffect, useState, type ReactNode } from "react";

/**
 * The whole router. Nine static paths and one `:id` segment do not need a routing library, and the
 * app's dependency list is deliberately three packages long.
 *
 * The Head serves the SPA from FastAPI, which rewrites unknown paths to index.html — so a hard reload
 * on /arms lands here and not on a 404.
 */

export function navigate(to: string, state: unknown = null) {
  if (to === window.location.pathname) return;
  window.history.pushState(state, "", to);
  // pushState fires no event of its own; this is what tells every usePath() to re-read.
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function usePath(): string {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

/**
 * An <a> that stays in the SPA. Modified clicks (new tab, download, middle-click) fall through to the
 * browser — intercepting those is how SPAs get a reputation for breaking the web.
 */
export function Link({
  to,
  className,
  style,
  children,
  "aria-label": ariaLabel,
}: {
  to: string;
  className?: string;
  style?: React.CSSProperties;
  /** Optional: a stretched link (one absolutely positioned over a row) paints no text of its own. */
  children?: ReactNode;
  /** Required when there are no `children`, or the link reads as empty to a screen reader. */
  "aria-label"?: string;
}) {
  const onClick = useCallback(
    (e: React.MouseEvent<HTMLAnchorElement>) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      navigate(to);
      window.scrollTo(0, 0);
    },
    [to],
  );
  return (
    <a href={to} onClick={onClick} className={className} style={style} aria-label={ariaLabel}>
      {children}
    </a>
  );
}

/** `/dive/:id` is the only dynamic route; this pulls the id back out. */
export function diveIdFrom(path: string): string | null {
  const m = /^\/dive\/([^/]+)\/?$/.exec(path);
  return m ? decodeURIComponent(m[1]) : null;
}
