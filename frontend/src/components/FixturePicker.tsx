import type { FixtureMeta } from "../lib/fixtures";

interface FixturePickerProps {
  id: string;
  label: string;
  fixtures: FixtureMeta[];
  value: string;
  onChange: (value: string) => void;
}

// Phase-2 stub: in the absence of a real engine, the user picks which fixture
// the surface "returns" on submit. Replace with the real /api/run or
// /api/analyze call in Phase 3.
export function FixturePicker({
  id,
  label,
  fixtures,
  value,
  onChange,
}: FixturePickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <label htmlFor={id} className="text-sm text-muted">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-white border border-black/20 rounded-lg text-sm px-2.5 py-1.5"
      >
        {fixtures.length === 0 ? (
          <option value={value}>{value}</option>
        ) : (
          fixtures.map((f) => (
            <option key={f.id} value={f.id}>
              {f.id}
            </option>
          ))
        )}
      </select>
    </div>
  );
}
