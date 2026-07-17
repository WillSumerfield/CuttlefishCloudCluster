import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** The only Admin-only page — it is the one with no non-admin counterpart. */
export function UsersPage() {
  return (
    <Page title="Users">
      <Placeholder what="Every User, and what each one holds — so you can see what removing them would break before you do it." />
    </Page>
  );
}
