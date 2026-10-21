import { useAuth } from "./auth";
import { Link, usePath } from "./router";
import { session } from "./session";

/**
 * The left rail. Labels floating on the abyss — no panel, no border, no rule. There is nothing here
 * but words and the light on them, which is the whole rule of the interface applied to navigation.
 *
 * A rail rather than a top bar because eight destinations would crowd a bar and spill into an overflow
 * menu, and because Queue, Arms and History are wide tables that want the full width of the page.
 *
 * The current page is lit with PIGMENT — the one warm thing on the page. That is a deliberate small
 * violation of "pigment means people": here it means *you are here*, which is close enough to the same
 * idea, and it is the only mark in the rail.
 */

type Dest = { to: string; label: string; admin?: boolean };

const DESTINATIONS: Dest[] = [
  { to: "/", label: "Queue" },
  { to: "/history", label: "History" },
  { to: "/arms", label: "Arms" },
  { to: "/karma", label: "Karma" },
  { to: "/tide", label: "Tide" },
  { to: "/account", label: "Account" },
  { to: "/users", label: "Users", admin: true },
];

/** Spawn detail hangs off Queue/History, so it lights Queue rather than nothing. */
function isCurrent(path: string, to: string): boolean {
  if (to === "/") return path === "/" || path.startsWith("/spawn/");
  return path === to || path.startsWith(to + "/");
}

export function Rail() {
  const path = usePath();
  const { me, logout } = useAuth();
  const dests = DESTINATIONS.filter((d) => !d.admin || session.isAdmin);

  return (
    <nav
      aria-label="Primary"
      className="
        rail-ground
        fixed inset-x-0 bottom-0 z-20 flex flex-row justify-around gap-1 overflow-x-auto px-4 py-3
        backdrop-blur-md
        md:inset-y-0 md:right-auto md:left-0 md:h-full md:w-52 md:flex-col md:justify-start
        md:gap-1 md:overflow-visible md:px-8 md:py-10 md:backdrop-blur-none
      "
    >
      <span className="label mb-6 hidden text-left leading-tight md:block" style={{ color: "var(--ink-mute)" }}>
        Cuttlefish<br />
        Cloud<br />
        Cluster
      </span>

      {dests.map((d) => {
        const here = isCurrent(path, d.to);
        return (
          <Link
            key={d.to}
            to={d.to}
            className={`
              rail-link shrink-0 rounded-[10px] px-3 py-2 text-[15px] outline-none
              transition-[color,opacity] duration-200
              focus-visible:ring-2 focus-visible:ring-[var(--gold)]
              ${here ? "is-active" : ""}
            `}
            style={{
              color: here ? "var(--gold-hi)" : "var(--ink-mute)",
              transitionTimingFunction: "var(--ease-swim)",
            }}
          >
            {d.label}
            {here && (
              /* The lit mark. A soft pool of pigment behind the word — not an underline, not a pill,
                 not a 2px accent bar. Those are all lines, and there are no lines here. */
              <span
                aria-hidden
                className="pointer-events-none absolute inset-0 -z-10 rounded-[10px]"
                style={{
                  background:
                    "radial-gradient(70% 120% at 50% 50%, color-mix(in oklab, var(--gold) 16%, transparent) 0%, transparent 70%)",
                }}
              />
            )}
          </Link>
        );
      })}

      {/* Who you are, and the way out. Pinned to the bottom on desktop; just the last item on mobile.
          Sign out proper lives on Account too (once built) — this is the always-reachable copy. */}
      <div className="hidden md:mt-auto md:block">
        <span className="num block px-3 text-[13px] text-ink-soft">{me.username}</span>
        <button
          onClick={logout}
          className="mt-1 rounded-[10px] px-3 py-2 text-left text-[13px] text-ink-mute outline-none transition-colors duration-200 hover:text-ink focus-visible:ring-2 focus-visible:ring-[var(--gold)]"
        >
          Sign out
        </button>
      </div>
    </nav>
  );
}
