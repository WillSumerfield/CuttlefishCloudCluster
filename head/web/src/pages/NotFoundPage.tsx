import { Page } from "../app/Page";
import { Link } from "../app/router";

export function NotFoundPage() {
  return (
    <Page title="Nothing here">
      <section className="surface" style={{ padding: "var(--pad)" }}>
        <p className="text-[15px] text-ink-soft">
          That page does not exist.{" "}
          <Link to="/" className="text-[var(--gold)] underline-offset-4 hover:underline">
            Go to the queue.
          </Link>
        </p>
      </section>
    </Page>
  );
}
