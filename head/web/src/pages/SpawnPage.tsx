import { Page } from "../app/Page";
import { Placeholder } from "./Placeholder";

/** One Spawn, reached from Queue or History. Not a rail destination. */
export function SpawnPage({ id }: { id: string }) {
  return (
    <Page title={id}>
      <Placeholder what="What this Spawn did: its verdict, its logs (stdout and stderr, separately), its outputs, and what it cost." />
    </Page>
  );
}
