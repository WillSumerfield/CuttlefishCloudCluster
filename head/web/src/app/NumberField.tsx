/**
 * A number input with OUR OWN up/down stepper. A native `<input type=number>` spinner is an OS widget —
 * it can't be rounded, recoloured, or softened, so it shows up as a square system control that breaks
 * the "no borders, form from light" language, exactly the way a native <select> does (hence <Select>).
 *
 * The native spinner is hidden (see `input[type=number].field` in index.css); the arrows here are the
 * same chevron the Select draws, split into an up and a down, thin ink that warms to gold on hover.
 * Keyboard users don't need them — ↑/↓ still step the value inside the input natively — so the buttons
 * are mouse affordances only: `tabIndex={-1}`, out of the tab order.
 */
function Chevron({ up }: { up?: boolean }) {
  return (
    <svg
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={up ? { transform: "rotate(180deg)" } : undefined}
    >
      <path d="M2.5 4.5L6 8l3.5-3.5" />
    </svg>
  );
}

export function NumberField({
  value,
  onChange,
  min,
  max,
  step = 1,
  placeholder,
  ariaLabel,
  className,
}: {
  value: number | string;
  /** Always a string, matching a native input's onChange — the caller coerces if it holds a number. */
  onChange: (v: string) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
}) {
  const bump = (dir: 1 | -1) => {
    const cur = value === "" || value == null ? min ?? 0 : Number(value);
    let next = (Number.isFinite(cur) ? cur : min ?? 0) + dir * step;
    if (min != null) next = Math.max(min, next);
    if (max != null) next = Math.min(max, next);
    onChange(String(next));
  };

  return (
    <div className={`stepper ${className ?? ""}`}>
      <input
        className="field"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        aria-label={ariaLabel}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="stepper-arrows" aria-hidden>
        <button type="button" tabIndex={-1} className="stepper-arrow" onClick={() => bump(1)}>
          <Chevron up />
        </button>
        <button type="button" tabIndex={-1} className="stepper-arrow" onClick={() => bump(-1)}>
          <Chevron />
        </button>
      </div>
    </div>
  );
}
