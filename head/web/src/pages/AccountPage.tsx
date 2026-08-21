import { useEffect, useState, type ReactNode } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getArms, getRecipes, getStandings, RECIPE_ALLOWANCE_BYTES, Unauthorized } from "../api";
import { Link } from "../app/router";
import { Tip } from "../app/Tip";
import { FleshCell } from "../account/FleshCell";
import { Credential } from "../account/Credential";
import { PasswordForm } from "../account/PasswordForm";
import { size } from "../recipes/format";

/**
 * Account — yourself. The identity sits bold on the right with your pigment bloom (you, as the cluster sees
 * you) beneath it; a single surface on the left holds everything you can read or change: your standing and
 * holdings (which link out to where they live), then the two secrets you control, then the way out.
 *
 * Every figure is live. Standing comes from /karma, which serves everyone's — your Share only exists
 * against the others, so there is no cheaper call that could answer it. An Admin has no row there and
 * reads `—`: they are outside the economy, not neutral within it.
 */
export function AccountPage() {
  const { me, logout } = useAuth();

  const name = me.username;
  const admin = me.is_admin;

  const [balance, setBalance] = useState<number | null>(null);
  const [share, setShare] = useState<number | null>(null);
  const [armCount, setArmCount] = useState<number | null>(null);
  const [storageUsed, setStorageUsed] = useState<number | null>(null);

  useEffect(() => {
    const bounce = (e: unknown) => { if (e instanceof Unauthorized) logout(); };
    getArms()
      .then((arms) => setArmCount(arms.filter((a) => a.owner === me.username).length))
      .catch(bounce);
    getRecipes()
      .then((rs) => setStorageUsed(rs.reduce((n, r) => n + r.size_bytes, 0)))
      .catch(bounce);
    getStandings()
      .then((rows) => {
        const you = rows.find((s) => s.username === me.username);
        if (!you) return;
        setBalance(you.balance);
        setShare(you.share);
      })
      .catch(bounce);
  }, [me.username, logout]);

  const standing =
    balance === null || share === null
      ? "—"
      : `${balance >= 0 ? "+" : "−"}${Math.abs(Math.round(balance))} karma · ${(share * 100).toFixed(1)}% share`;
  const armsValue = armCount === null ? "—" : armCount === 0 ? "none" : `${armCount} owned`;
  const usedFrac = storageUsed === null ? 0 : Math.min(1, storageUsed / RECIPE_ALLOWANCE_BYTES);

  return (
    <Page title="Account">
      <div className="grid gap-10 lg:grid-cols-[3fr_2fr] lg:items-start">
        {/* Identity — bold, to the right on wide screens (first in the DOM so it leads on mobile). */}
        <aside className="lg:col-start-2 lg:row-start-1">
          <div className="pl-1">
            <h2 className="text-ink" style={{ fontWeight: 700, fontSize: 34, lineHeight: 1.05, letterSpacing: "-0.02em" }}>
              {name}
            </h2>
            <div className="mt-1.5 flex items-center gap-2">
              <Tip
                className="label"
                text="An Admin runs the cluster rather than competing for it: they hold no Karma, earn none, and never lose contention."
              >
                <span style={{ color: admin ? "var(--irid-mid)" : "var(--ink-mute)" }}>
                  {admin ? "Admin" : "Member"}
                </span>
              </Tip>
            </div>
          </div>
          <FleshCell balance={balance} />
        </aside>

        {/* Everything you can read or change, one column on one surface. */}
        <section className="surface lg:col-start-1 lg:row-start-1" style={{ padding: "calc(var(--pad) + 12px)" }}>
          <div className="space-y-7">
            {/* Standing + holdings — facts that live elsewhere; each row is the way there. */}
            <div className="space-y-1">
              <FactRow to="/karma" label="Karma" help="Your standing in the cluster — earned by donating hardware, spent by running work. It only ever decides who goes first when two people want the cluster at once; there is no balance at which your work is refused, and on an idle cluster it does nothing at all." value={standing} />
              <FactRow to="/arms" label="Arms" help="Machines you've donated. Each one earns you Karma whenever somebody else's work runs on it." value={armsValue} />
              <FactRow
                to="/recipes"
                label="Storage"
                help="Disk your Recipes hold on the Head, against a 32 GB ceiling. It's a hard wall — saving past it fails and you delete something, rather than something older being quietly dropped."
                value={
                  <span className="flex items-center gap-3">
                    <span className="relative h-1.5 w-24 overflow-hidden rounded-full" style={{ filter: "blur(0.6px)" }}>
                      <span
                        className="absolute inset-y-0 left-0"
                        style={{
                          width: `${usedFrac * 100}%`,
                          background: `color-mix(in oklab, var(--irid-mid) ${Math.round(usedFrac * 70)}%, var(--irid-dim))`,
                        }}
                      />
                    </span>
                    <span className="num">
                      {storageUsed === null ? "—" : `${size(storageUsed)} / ${size(RECIPE_ALLOWANCE_BYTES)}`}
                    </span>
                  </span>
                }
              />
            </div>

            <Credential kind="wandb" onUnauthorized={logout} />

            <Credential kind="github" onUnauthorized={logout} />

            <PasswordForm onUnauthorized={logout} />

            <div>
              <button className="btn-ghost" onClick={() => logout()}>
                Sign out
              </button>
            </div>
          </div>
        </section>
      </div>
    </Page>
  );
}

/** A holding stated plainly, that links to where it lives. Label left, value right, an arrow that leans on hover. */
function FactRow({ to, label, value, help }: { to: string; label: string; value: ReactNode; help?: string }) {
  return (
    <Link
      to={to}
      className="group flex items-center justify-between gap-4 rounded-[10px] py-2 pl-1 pr-2 transition-colors"
    >
      {help ? <Tip className="label" text={help}>{label}</Tip> : <span className="label">{label}</span>}
      <span className="flex items-center gap-2 text-[13px] text-ink-soft transition-colors group-hover:text-[color:var(--gold)] group-hover:[text-shadow:0_0_10px_rgba(240,194,90,0.55)]">
        <span className="num">{value}</span>
        <span aria-hidden className="text-[10px] transition-transform group-hover:translate-x-0.5">
          ▸
        </span>
      </span>
    </Link>
  );
}
