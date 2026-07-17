import { Page } from "../app/Page";
import { StandingCard } from "./StandingCard";
import { Standings } from "./Standings";
import { Ledger } from "./Ledger";

export function KarmaPage() {
  return (
    <Page title="Karma" density="hero">
      <StandingCard />

      {/* Standings carries no heading — it is the page, and it says what it is by being it. */}
      <section className="mt-48 mb-48">
        <Standings />
      </section>

      <section>
        <Ledger />
      </section>
    </Page>
  );
}
