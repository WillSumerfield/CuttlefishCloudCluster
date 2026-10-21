import type { ReactNode } from "react";

/**
 * Every page's frame. The `density` prop is the whole point of it.
 *
 *   hero — Karma, Tide. Pages you LOOK AT. Big type, wide air, room for the signature to be loud.
 *   tool — Queue, History, Arms, Users, Spawn detail. Pages you WORK IN, daily. Dense but
 *          breathable: they have to hold forty rows without becoming a scroll.
 *
 * This is "everything else stays quiet so the signature can be loud" applied to space rather than to
 * colour. A queue at hero pacing stops being a tool.
 */
export type Density = "hero" | "tool";

/** Cards read `--pad` off the page, so a card never has to know which kind of page it is on. */
const PAD = {
  hero: "80px",
  tool: "28px",
} as const;

export function Page({
  title,
  density = "tool",
  actions,
  children,
}: {
  title: string;
  density?: Density;
  /** Top-right of the header: the page's primary verb, if it has one. */
  actions?: ReactNode;
  children: ReactNode;
}) {
  const hero = density === "hero";

  return (
    <main
      // Bottom padding on mobile clears the rail, which is a bottom bar down there.
      className={`relative w-full pb-32 md:pb-24 ${
        hero ? "px-6 pt-16 md:px-10 md:pt-24" : "px-5 pt-10 md:px-8 md:pt-14"
      }`}
      style={{ "--pad": PAD[density] } as React.CSSProperties}
    >
      <header className={`flex flex-wrap items-end justify-between gap-6 ${hero ? "mb-4" : "mb-8"}`}>
        <h1
          // Quiet, not loud: the page title recedes to muted ink so the page's own signature (the live
          // rows, the karma bloom) is what the eye lands on. Everything else stays quiet.
          className="text-ink-mute"
          style={{
            fontWeight: 300,
            fontSize: hero ? 48 : 28,
            lineHeight: 1.02,
            letterSpacing: "-0.02em",
          }}
        >
          {title}
        </h1>
        {actions}
      </header>

      {children}
    </main>
  );
}
