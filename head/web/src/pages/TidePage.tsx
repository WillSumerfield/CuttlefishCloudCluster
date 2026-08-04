import { useEffect, useMemo, useState } from "react";
import { Page } from "../app/Page";
import { getDemand, getUsage, type DemandSample, type UsageUser } from "../api";
import { fold } from "../tide/fold";
import { NowCard } from "../tide/NowCard";
import { TideHorizon } from "../tide/TideHorizon";
import { Usage } from "../tide/Usage";

/**
 * The Tide — when to run. The now-card states this hour's cost against a typical one; the horizon draws
 * the day it's read from; the usage band is who's been spending. Tool density, like Karma: the page
 * recedes so the water is what you see.
 *
 * Two calls over the same 30 days, and they are counterparts: /karma/demand is card-shaped (how badly
 * each was wanted, hour by hour) and /karma/usage is people-shaped (who spent it). A month of high water
 * up top is explained by whoever's band swallows the plot below. A cluster with no history returns
 * nothing from both, which folds to a flat 1.0 Tide and an absent band — honest, not broken.
 */
export function TidePage() {
  const [samples, setSamples] = useState<DemandSample[] | null>(null);
  const [usage, setUsage] = useState<UsageUser[]>([]);

  useEffect(() => {
    // An empty series is the correct cold-start read, so a failed/empty fetch degrades to a flat Tide.
    getDemand(30).then(setSamples).catch(() => setSamples([]));
    getUsage(30).then(setUsage).catch(() => setUsage([]));
  }, []);

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

      {/* Absent, not empty, on a cluster nobody has spent on yet: a stacked area with no bands is a
          labelled blank rectangle, which says less than saying nothing. The top five carry the shape —
          past that the bands are too thin to read and the legend stops fitting. */}
      {usage.length > 0 && (
        <section className="mt-34 md:-mb-24 relative z-0">
          <Usage data={usage.slice(0, 5)} />
        </section>
      )}
    </Page>
  );
}
