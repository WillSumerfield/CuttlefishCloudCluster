import { useEffect, useState, type ReactNode } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getArms, getRecipes, RECIPE_ALLOWANCE_BYTES, Unauthorized } from "../api";
import { Link } from "../app/router";
import { isDemo } from "../queue/mock";
import { FleshCell } from "../account/FleshCell";
import { WandbKey } from "../account/WandbKey";
import { PasswordForm } from "../account/PasswordForm";
import { demoAccount, demoShare } from "../account/mock";
import { size } from "../recipes/format";

/**
 * Account — yourself. The identity sits bold on the right with your pigment bloom (you, as the cluster sees
 * you) beneath it; a single surface on the left holds everything you can read or change: your standing and
 * holdings (which link out to where they live), then the two secrets you control, then the way out.
 *
 * Data is split. Identity, password, sign-out, your Arms and your storage are live. Balance/Share (Ph4) and
 * the W&B key (no route yet) are demo-only; under ?demo the whole page wears the mock persona (Nadia) so it
 * can be reviewed whole, and live those two degrade rather than lie.
 */
export function AccountPage() {
  const { me, logout } = useAuth();
  const demo = isDemo();

  const name = demo ? demoAccount.name : me.username;
  const admin = demo ? demoAccount.admin : me.is_admin;
  const balance = demo ? demoAccount.balance : null;
  const share = demo ? demoShare() : null;

  const [armCount, setArmCount] = useState<number | null>(demo ? demoAccount.arms.length : null);
  const [storageUsed, setStorageUsed] = useState<number | null>(demo ? demoAccount.storageUsedBytes : null);

  useEffect(() => {
    if (demo) return;
    const bounce = (e: unknown) => { if (e instanceof Unauthorized) logout(); };
    getArms()
      .then((arms) => setArmCount(arms.filter((a) => a.owner === me.username).length))
      .catch(bounce);
    getRecipes()
      .then((rs) => setStorageUsed(rs.reduce((n, r) => n + r.size_bytes, 0)))
      .catch(bounce);
  }, [demo, me.username, logout]);

  const standing =
    balance === null ? "—" : `${balance >= 0 ? "+" : "−"}${Math.abs(balance)} karma · ${(share! * 100).toFixed(1)}% share`;
  const armsValue = armCount === null ? "—" : armCount === 0 ? "none" : `${armCount} owned`;
  const usedFrac = storageUsed === null ? 0 : Math.min(1, storageUsed / RECIPE_ALLOWANCE_BYTES);

  return (
    <Page title="Account">
      {demo && (
        <p
          className="mb-6 rounded-[10px] px-3 py-2 text-[13px]"
          style={{ background: "color-mix(in oklab, var(--irid-low) 12%, transparent)", color: "var(--ink-soft)" }}
        >
          Demo data — not your real account. Drop the <span className="num">?demo</span> from the URL for yours.
        </p>
      )}

      <div className="grid gap-10 lg:grid-cols-[3fr_2fr] lg:items-start">
        {/* Identity — bold, to the right on wide screens (first in the DOM so it leads on mobile). */}
        <aside className="lg:col-start-2 lg:row-start-1">
          <div className="pl-1">
            <h2 className="text-ink" style={{ fontWeight: 700, fontSize: 34, lineHeight: 1.05, letterSpacing: "-0.02em" }}>
              {name}
            </h2>
            <div className="mt-1.5 flex items-center gap-2">
              <span className="label" style={{ color: admin ? "var(--irid-mid)" : "var(--ink-mute)" }}>
                {admin ? "Admin" : "Member"}
              </span>
            </div>
          </div>
          <FleshCell balance={balance} />
        </aside>

        {/* Everything you can read or change, one column on one surface. */}
        <section className="surface lg:col-start-1 lg:row-start-1" style={{ padding: "calc(var(--pad) + 12px)" }}>
          <div className="space-y-7">
            {/* Standing + holdings — facts that live elsewhere; each row is the way there. */}
            <div className="space-y-1">
              <FactRow to="/karma" label="Karma" value={standing} />
              <FactRow to="/arms" label="Arms" value={armsValue} />
              <FactRow
                to="/recipes"
                label="Storage"
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

            <WandbKey demo={demo} demoKey={demo ? demoAccount.wandbKey : undefined} onUnauthorized={logout} />

            <PasswordForm demo={demo} onUnauthorized={logout} />

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
function FactRow({ to, label, value }: { to: string; label: string; value: ReactNode }) {
  return (
    <Link
      to={to}
      className="group flex items-center justify-between gap-4 rounded-[10px] py-2 pl-1 pr-2 transition-colors"
    >
      <span className="label">{label}</span>
      <span className="flex items-center gap-2 text-[13px] text-ink-soft transition-colors group-hover:text-[color:var(--gold)] group-hover:[text-shadow:0_0_10px_rgba(240,194,90,0.55)]">
        <span className="num">{value}</span>
        <span aria-hidden className="text-[10px] transition-transform group-hover:translate-x-0.5">
          ▸
        </span>
      </span>
    </Link>
  );
}
