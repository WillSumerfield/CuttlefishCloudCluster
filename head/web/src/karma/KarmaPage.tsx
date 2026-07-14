import { AbyssBackground } from "./AbyssBackground";
import { StandingCard } from "./StandingCard";
import { Standings } from "./Standings";
import { Ledger } from "./Ledger";

export function KarmaPage() {
  return (
    <>
      <AbyssBackground />

      <main className="relative w-full px-6 pb-24 pt-16 md:px-10 md:pt-24">
        <header className="mb-12">
          <h1
            className="text-ink"
            style={{
              fontFamily: "Figtree, sans-serif",
              fontWeight: 300,
              fontSize: 48,
              lineHeight: 1.02,
              letterSpacing: "-0.02em",
            }}
          >
            Karma
          </h1>
        </header>

        <StandingCard />

        <section className="mt-14">
          <div className="mb-6">
            <h2
              className="text-ink"
              style={{ fontSize: 20, fontWeight: 500, letterSpacing: "-0.01em" }}
            >
              The Shoal
            </h2>
          </div>
          <Standings />
        </section>

        <section className="mt-14">
          <Ledger />
        </section>

        <footer className="mt-20" />
      </main>
    </>
  );
}
