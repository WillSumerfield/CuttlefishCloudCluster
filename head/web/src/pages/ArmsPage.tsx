import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** The fleet. Everyone sees the whole roster; My Arms and the Admin powers are inline on it. */
export function ArmsPage() {
  return (
    <Page title="Arms">
      <Placeholder what="The roster of donated machines — and, if you own one, the panic switch." />
    </Page>
  );
}
