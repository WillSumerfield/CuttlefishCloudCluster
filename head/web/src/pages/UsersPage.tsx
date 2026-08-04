import { useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { Link } from "../app/router";
import {
  getArms, getStandings, Unauthorized, errMessage, type Arm, type Standing,
} from "../api";
import { pigment } from "../karma/skin";
import { Tip } from "../app/Tip";

// USER · KARMA · ESCROW · SHARE · ARMS. Numeric columns fixed-width and right-aligned; the text
// columns flex — the same grid discipline the Arm roster keeps, so the two read as one family.
const COLS = "minmax(160px,1.6fr) 90px 90px 80px minmax(140px,1.4fr)";

/**
 * Every User, and what each one holds — so an Admin can see what removing one would break before they
 * do it. The only Admin-only page, because it is the one with no non-admin counterpart.
 *
 * **Read-only, permanently.** Creating a User, resetting a password, overriding Karma: all Head-CLI
 * only, and not for want of plumbing. The Head API exposes no admin-write endpoints at all, because
 * minting Karma or seizing an account from a stolen Session is the most valuable thing an attacker
 * could do with one, and no amount of authorization checking makes a remote write as safe as one that
 * cannot be requested remotely. So this page says what is true, and where to go and change it.
 *
 * Composed from two reads that exist already rather than a route of its own: the standings ARE the
 * roster (every non-Admin User, with what they hold), and the Arm list says who donated what. Admins
 * have no standing — they are outside the economy — so they are absent here by construction, which is
 * also the honest rendering: there is nothing about an Admin this page could state.
 */
export function UsersPage() {
  const { logout } = useAuth();
  const [standings, setStandings] = useState<Standing[] | null>(null);
  const [arms, setArms] = useState<Arm[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const bounce = (e: unknown) => {
      if (e instanceof Unauthorized) logout();
      else setError(errMessage(e));
    };
    getStandings().then(setStandings).catch(bounce);
    getArms().then(setArms).catch(bounce);
  }, [logout]);

  const rows = standings ?? [];

  return (
    <Page
      title="Users"
      actions={
        rows.length > 0 ? (
          <span className="text-[13px] text-ink-mute">
            <span className="num text-ink-soft">{rows.length}</span> user{rows.length === 1 ? "" : "s"}
          </span>
        ) : null
      }
    >
      {error && <p className="mb-6 text-[13px]" style={{ color: "var(--alarm)" }}>{error}</p>}

      <div
        className="surface"
        style={{ padding: "calc(var(--pad) + 14px)", "--fade-x": "30px", "--fade-y": "30px" } as React.CSSProperties}
      >
        {rows.length === 0 ? (
          <p className="px-3.5 py-6 text-[14px] text-ink-mute">
            No Users yet — only Admins, who hold no Karma and so appear on no roster. Add one with{" "}
            <span className="num">cuttle user add</span> on the Head.
          </p>
        ) : (
          <>
            {/* Label head — named once, uppercase and muted, the numeric columns right-aligned to match:
                the same idiom the Arm roster uses, and no rules between anything. */}
            <div className="label grid items-center gap-3 px-5 pb-4" style={{ gridTemplateColumns: COLS }}>
              <span>User</span>
              <Tip className="text-right" text="Your standing in the cluster — earned by donating hardware, spent by running work. It only ever decides who goes first when two people want the cluster at once; there is no balance at which your work is refused, and on an idle cluster it does nothing at all.">Karma</Tip>
              <Tip className="text-right" text="Karma held against work you have outstanding. It isn't a charge — it's released when the Spawn ends and the real cost is taken instead. While it's held it lowers your standing, so flooding the queue costs you your own place in it.">Escrow</Tip>
              <Tip className="text-right" text="How much a Balance actually buys, squashed between &quot;always loses&quot; and &quot;always wins&quot;. Hoarding has diminishing returns and debt has diminishing punishment, so neither end runs away.">Share</Tip>
              <Tip text="Machines this person donated. Removing them leaves the machines without an owner to pay.">Arms donated</Tip>
            </div>

            <ul>
              {rows.map((u) => {
                const owned = arms.filter((a) => a.owner === u.username);
                const [r, g, b] = pigment(u.balance, 0.5);
                return (
                  <li
                    key={u.user_id}
                    className="grid items-center gap-3 rounded-[10px] px-5 py-3.5"
                    style={{ gridTemplateColumns: COLS }}
                  >
                    <span className="flex min-w-0 items-center gap-2.5">
                      {/* Standing as colour — the same umber→gold pigment the Karma field wears, so a
                          debtor reads as one here without this table needing a judgement column. */}
                      <span
                        className="h-2 w-2 shrink-0 rounded-full"
                        style={{ background: `rgb(${(r * 255) | 0}, ${(g * 255) | 0}, ${(b * 255) | 0})` }}
                      />
                      <span className="truncate text-[14px] text-ink">{u.username}</span>
                    </span>
                    <span
                      className="num text-right text-[13px]"
                      style={{ color: u.balance >= 0 ? "var(--gold-hi)" : "var(--bronze)" }}
                    >
                      {u.balance >= 0 ? "+" : "−"}
                      {Math.abs(Math.round(u.balance))}
                    </span>
                    <span className="num text-right text-[13px] text-ink-mute">
                      {u.escrow > 0 ? `−${Math.round(u.escrow)}` : "—"}
                    </span>
                    <span className="num text-right text-[13px] text-ink-soft">
                      {(u.share * 100).toFixed(1)}%
                    </span>
                    <span className="min-w-0 truncate text-[13px] text-ink-mute">
                      {owned.length === 0 ? (
                        "none"
                      ) : (
                        <Link to="/arms" className="text-ink-soft">
                          {owned.map((a) => a.name).join(", ")}
                        </Link>
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>

      {/* Said only when it is true of somebody: a standing caveat about a state nobody is in reads as
          boilerplate, and gets skipped on the day it matters. */}
      {rows.some((u) => u.escrow > 0) && (
        <p className="mt-4 px-1 text-[12px] text-ink-mute">
          An Escrow is a live claim — that User has work on the cluster right now, and removing them
          would strand it.
        </p>
      )}
    </Page>
  );
}
