import { useEffect, useMemo, useState } from "react";
import { Page } from "../app/Page";
import { getDemand, type DemandSample } from "../api";
import { isDemo } from "../queue/mock";
import { fold } from "../tide/fold";
import { NowCard } from "../tide/NowCard";
import { TideHorizon } from "../tide/TideHorizon";
import { Usage } from "../tide/Usage";
import { demoDemand, demoUsage } from "../tide/mock";

/**
 * The Tide — when to run. The now-card states this hour's cost against a typical one; the horizon draws
 * the day it's read from; the usage band (demo-only) is who's been spending. Tool density, like Karma:
 * the page recedes so the water is what you see.
 *
 * Live data is one call, /karma/demand. A cluster with no history returns nothing, which folds to a flat
 * 1.0 Tide — honest, not broken. ?demo wears a month of mock so the whole shape can be reviewed.
 */
export function TidePage() {
  const demo = isDemo();
  const [samples, setSamples] = useState<DemandSample[] | null>(demo ? demoDemand() : null);

  useEffect(() => {
    if (demo) return;
    // An empty series is the correct cold-start read, so a failed/empty fetch degrades to a flat Tide.
    getDemand(30).then(setSamples).catch(() => setSamples([]));
  }, [demo]);

  const folded = useMemo(() => fold(samples ?? []), [samples]);

  return (
    <Page title="Tide">
      {/* pointer-events-none: this full-width z-10 wrapper sits over the pulled-up horizon, so it must let
          the pointer fall through to the water's hover — NowCard is display-only and needs no events. */}
      <div className="relative z-10 pointer-events-none">
        <NowCard fold={folded} />
      </div>

      {/* The horizon carries no heading — like Standings, it says what it is by being it. The offset is
          the base pull (-12rem) + TIDE.headroom (10rem): the box grew upward, so pull it up to match and
          keep the waterline where it was. Keep in sync with TIDE.headroom / wrapRef height. */}
      {/* md:-mb-24 cancels Page's md:pb-24 on whichever section is last — the Tide page wants no trailing
          blank under the water. Mobile keeps its pb (bottom rail), so the negative margin is md-only. */}
      <section className={`-mt-[22rem] relative z-0 mb-16`}>
        <TideHorizon fold={folded} />
      </section>

      {demo && (
        <section className="mt-34 md:-mb-24 relative z-0">
          <Usage data={demoUsage()} />
        </section>
      )}
    </Page>
  );
}
