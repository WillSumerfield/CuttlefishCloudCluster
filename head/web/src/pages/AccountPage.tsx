import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** Yourself. Everyone has this page, Admins included. */
export function AccountPage() {
  return (
    <Page title="Account">
      <Placeholder what="Who you are, your password, and the W&B key the Head injects into every Spawn you submit." />
    </Page>
  );
}
