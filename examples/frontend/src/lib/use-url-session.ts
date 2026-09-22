import { useCallback, useEffect, useState } from "react";

/**
 * Keeps `?c=<sessionId>` in the URL in sync with the active session, using
 * the plain History API — no router dependency. Refresh, or browser
 * back/forward, lands you back on the same conversation instead of a blank
 * new chat.
 */
function readSessionParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("c");
}

export function useUrlSession() {
  const [sessionParam, setSessionParam] = useState<string | null>(readSessionParam);

  useEffect(() => {
    const onPopState = () => setSessionParam(readSessionParam());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const setSession = useCallback((sessionId: string | null, { replace = false } = {}) => {
    const url = new URL(window.location.href);
    if (sessionId) url.searchParams.set("c", sessionId);
    else url.searchParams.delete("c");
    const method = replace ? "replaceState" : "pushState";
    window.history[method](null, "", `${url.pathname}${url.search}`);
    setSessionParam(sessionId);
  }, []);

  return { sessionParam, setSession };
}
