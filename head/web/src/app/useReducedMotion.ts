import { useEffect, useState } from "react";

/**
 * Canvas motion — the ground shader, the chromatophore field — is invisible to CSS, so it reads the
 * media query here and freezes itself on a single frame instead.
 *
 * Nothing is lost by stopping: meaning is never carried by motion alone.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}
