type Quant = { attribute: string; operator: string; value: number; value_high?: number; unit: string; source_text: string };
type Cat = { category: string; value: string; negated: boolean; source_text: string };

export function CriteriaTable({ criteria }: { criteria: Record<string, unknown> | null }) {
  if (!criteria) return null;
  const quant = [
    ...((criteria.inclusion_quantitative as Quant[]) ?? []).map((c) => ({ ...c, kind: "inclusion" })),
    ...((criteria.exclusion_quantitative as Quant[]) ?? []).map((c) => ({ ...c, kind: "exclusion" })),
  ];
  const cat = [
    ...((criteria.inclusion_categorical as Cat[]) ?? []).map((c) => ({ ...c, kind: "inclusion" })),
    ...((criteria.exclusion_categorical as Cat[]) ?? []).map((c) => ({ ...c, kind: "exclusion" })),
  ];
  const unparseable = (criteria.unparseable as string[]) ?? [];

  return (
    <div className="criteria">
      <h3>{(criteria.trial_title as string) ?? "Parsed criteria"}</h3>
      <div className="chips">
        {quant.map((c, i) => (
          <span key={i} className={`chip ${c.kind}`} title={c.source_text}>
            {c.attribute} {c.operator} {c.value}
            {c.operator === "between" ? `–${c.value_high}` : ""} {c.unit}
          </span>
        ))}
        {cat.map((c, i) => (
          <span key={`c${i}`} className={`chip ${c.kind}`} title={c.source_text}>
            {c.negated ? "¬ " : ""}{c.value}
          </span>
        ))}
      </div>
      {unparseable.length > 0 && (
        <div className="unparseable">
          <strong>Unparseable:</strong> {unparseable.join("; ")}
        </div>
      )}
    </div>
  );
}
