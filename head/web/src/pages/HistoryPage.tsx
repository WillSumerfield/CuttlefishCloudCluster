import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** The searchable archive of finished work. */
export function HistoryPage() {
  return (
    <Page title="History">
      <Placeholder what="Every completed Spawn, with its outcome. Searchable by who ran it, how it ended, and where." />
    </Page>
  );
}
