type Evaluation = {
  patient_id: string;
  name: string;
  eligible: boolean;
  needs_review: boolean;
  criterion_results: { criterion: { source_text: string }; kind: string; status: string }[];
};

export function PatientMatchTable({ patients }: { patients: Evaluation[] }) {
  if (!patients.length) return null;
  const bucket = (e: Evaluation) =>
    e.needs_review ? "review" : e.eligible ? "eligible" : "ineligible";

  return (
    <table className="matches">
      <thead>
        <tr><th>Patient</th><th>Status</th><th>Failing / unknown criteria</th></tr>
      </thead>
      <tbody>
        {patients.map((e) => (
          <tr key={e.patient_id} className={bucket(e)}>
            <td>{e.patient_id} · {e.name}</td>
            <td>{bucket(e)}</td>
            <td>
              {e.criterion_results
                .filter((r) => r.status !== "pass")
                .map((r, i) => (
                  <span key={i} className={`mini-chip ${r.status}`}>
                    {r.criterion.source_text} ({r.status})
                  </span>
                ))}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
