// Two levels of choice, drawn differently so a person can tell them apart:
//  <Tabs>      -- the page's own views ("Day by day" | "Summary"): underlined tabs.
//  <Segmented> -- a filter inside a view (a period, a status): one pill group.
// Both are real tablists/radiogroups for keyboard and screen-reader use.

export function Tabs({ value, onChange, items, label = "Views" }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={value === item.id}
          className={"tabs__tab" + (value === item.id ? " tabs__tab--on" : "")}
          onClick={() => onChange(item.id)}
        >
          {item.label}
          {item.count != null ? <span className="tabs__count">{item.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export function Segmented({ value, onChange, items, label = "Filter" }) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="radio"
          aria-checked={value === item.id}
          className={"seg__item" + (value === item.id ? " seg__item--on" : "")}
          onClick={() => onChange(item.id)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
