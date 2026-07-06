import type { CriteriaSchema } from "../types";

export function CriteriaTable({ criteria }: { criteria: CriteriaSchema | null }) {
  if (!criteria) return null;
  const quant = [
    ...(criteria.inclusion_quantitative ?? []).map((c) => ({ ...c, kind: "inclusion" })),
    ...(criteria.exclusion_quantitative ?? []).map((c) => ({ ...c, kind: "exclusion" })),
  ];
  const cat = [
    ...(criteria.inclusion_categorical ?? []).map((c) => ({ ...c, kind: "inclusion" })),
    ...(criteria.exclusion_categorical ?? []).map((c) => ({ ...c, kind: "exclusion" })),
  ];
  const unparseable = criteria.unparseable ?? [];

  return (
    <div className="criteria">
      <h3>{criteria.trial_title || "Parsed criteria"}</h3>
      <div className="chips">
        {quant.map((c, i) => (
          <span key={i} className={`chip ${c.kind}`} title={c.source_text}>
            {c.attribute} {c.operator} {c.value}
            {c.operator === "between" ? `–${c.value_high}` : ""} {c.unit}
          </span>
        ))}
        {cat.map((c, i) => (
          <span key={`c${i}`} className={`chip ${c.kind}`} title={c.source_text}>
            {c.negated ? "¬ " : ""}
            {c.value}
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
