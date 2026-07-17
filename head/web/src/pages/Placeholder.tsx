/**
 * A page that exists in the rail but has not been built yet. Deliberately plain: it should look
 * unfinished, not like a designed empty state, so nobody mistakes it for a page that is done.
 *
 * Delete this import from a page the moment that page becomes real.
 */
export function Placeholder({ what }: { what: string }) {
  return (
    <section className="surface" style={{ padding: "var(--pad)" }}>
      <p className="text-[15px] text-ink-soft">{what}</p>
      <p className="mt-2 text-[13px] text-ink-mute">Not built yet.</p>
    </section>
  );
}
