import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** When to run. Hero density: this is a page you look at, not one you work in. */
export function TidePage() {
  return (
    <Page title="Tide" density="hero">
      <Placeholder what="Demand, forecast by hour. High Tide is busy; Low Tide is when your work is cheap." />
    </Page>
  );
}
