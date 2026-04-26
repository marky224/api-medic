export interface TabDef<T extends string> {
  id: T;
  label: string;
}

interface TabsProps<T extends string> {
  current: T;
  tabs: TabDef<T>[];
  onChange: (id: T) => void;
}

export function Tabs<T extends string>({
  current,
  tabs,
  onChange,
}: TabsProps<T>) {
  return (
    <nav
      role="tablist"
      className="flex gap-1 border-b border-black/10 mb-5 overflow-x-auto"
    >
      {tabs.map((t) => {
        const active = t.id === current;
        return (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(t.id)}
            className={
              "px-3 py-2 text-sm whitespace-nowrap border-b-2 -mb-px " +
              (active
                ? "text-ink border-blue-700 font-medium"
                : "text-muted border-transparent hover:text-ink")
            }
          >
            {t.label}
          </button>
        );
      })}
    </nav>
  );
}
