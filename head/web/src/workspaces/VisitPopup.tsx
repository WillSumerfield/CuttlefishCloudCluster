/**
 * A visit has started (or was already running) — what to do about it.
 *
 * The Dashboard's part ends here, exactly as it does for a Tide Pool: there is no browser terminal, so
 * the shell is the CLI's. What differs from the Tide Pool popup is which command to give: a visit is
 * reached by the workspace's NAME, not the Spawn's id, because `cuttle workspace open` submit-or-
 * attaches — the same one command whether or not a session is already live.
 */
export function VisitPopup({
  name,
  id,
  attached,
  onClose,
}: {
  name: string;
  id: number;
  attached: boolean;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "color-mix(in oklab, var(--abyss) 75%, transparent)" }}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="surface max-w-md space-y-4" style={{ padding: "calc(var(--pad) + 30px)" }}>
        <h2 className="text-[15px] font-semibold text-ink">
          {attached ? `'${name}' is already open — #${id}` : `Opening '${name}' — #${id}`}
        </h2>
        <p className="text-[13px] text-ink-soft">
          {attached
            ? "A visit is already running. A workspace holds one at a time, so this attaches to that session rather than starting a second — two would each overwrite the other's work."
            : "It's queued like any Spawn. Once it's running, attach from the CLI — the Dashboard has no browser terminal to open one from:"}
        </p>
        <p className="rounded-[10px] px-3 py-2 num text-[13px] text-ink"
           style={{ background: "color-mix(in oklab, var(--well) 70%, black)" }}>
          cuttle workspace open {name}
        </p>
        <p className="text-[13px] text-ink-soft">
          It holds the GPU and bills Karma from the moment it starts — whether or not you ever attach —
          until you cancel it or its time runs out. Everything you leave in the workspace is kept
          either way:
        </p>
        <p className="rounded-[10px] px-3 py-2 num text-[13px] text-ink"
           style={{ background: "color-mix(in oklab, var(--well) 70%, black)" }}>
          cuttle cancel {id}
        </p>
        <div className="flex justify-end">
          <button className="btn-primary" onClick={onClose}>Got it</button>
        </div>
      </div>
    </div>
  );
}
