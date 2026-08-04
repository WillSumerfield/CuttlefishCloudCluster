import { useEffect, useState } from "react";
import { Page } from "../app/Page";
import { useAuth } from "../app/auth";
import { getStandings, getLedger, errMessage, Unauthorized, type Standing, type Ledger as LedgerData } from "../api";
import { StandingCard } from "./StandingCard";
import { Standings } from "./Standings";
import { Ledger } from "./Ledger";

/**
 * Karma — where you stand, and how you got there.
 *
 * Two fetches, because they answer two different questions and are scoped differently: the standings are
 * *everyone's* (a Balance is meaningless in isolation — Karma ranks Users against each other, so a User
 * who cannot see the others cannot see themselves), and the ledger is *yours alone* (a transaction
 * history needs nobody else's rows).
 *
 * An Admin has no standing to show. They are outside the economy by design — they earn no Karma, pay
 * none, and hold no Balance — so the page says that rather than drawing them an empty card at zero,
 * which would read as "you're neutral" instead of "you're not in this".
 */
export function KarmaPage() {
  const { me, logout } = useAuth();
  const [standings, setStandings] = useState<Standing[] | null>(null);
  const [ledger, setLedger] = useState<LedgerData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const bounce = (e: unknown) => {
      if (e instanceof Unauthorized) logout();
      else setError(errMessage(e));
    };
    getStandings().then(setStandings).catch(bounce);
    if (!me.is_admin) getLedger().then(setLedger).catch(bounce);
  }, [me.is_admin, logout]);

  const you = standings?.find((s) => s.username === me.username) ?? null;
  const members = (standings ?? []).map((s) => ({ id: s.user_id, name: s.username, balance: s.balance }));

  return (
    <Page title="Karma">
      {error && <p className="mb-6 text-[13px]" style={{ color: "var(--bronze)" }}>{error}</p>}

      {you ? (
        <StandingCard standing={you} />
      ) : (
        me.is_admin && (
          <p className="text-[13px] text-ink-mute">
            Admins hold no Karma. You run the cluster rather than compete for it — an Arm you own is a
            commons Arm, and what it earns is destroyed instead of paid to you.
          </p>
        )
      )}

      {/* Standings carries no heading — it is the page, and it says what it is by being it. */}
      {members.length > 0 && (
        <section className="mt-30 mb-36">
          <Standings members={members} youId={you?.user_id ?? null} />
        </section>
      )}

      {ledger && (
        <section>
          <Ledger data={ledger} />
        </section>
      )}
    </Page>
  );
}
