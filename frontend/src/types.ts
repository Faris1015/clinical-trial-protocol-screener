/** Shared API payload types — mirrors the backend Pydantic schemas. */

export type QuantitativeCriterion = {
  attribute: string;
  operator: ">=" | "<=" | ">" | "<" | "==" | "between";
  value: number;
  value_high: number | null;
  unit: string;
  source_text: string;
};

export type CategoricalCriterion = {
  category: "diagnosis" | "prior_treatment" | "medication" | "biomarker" | "condition";
  value: string;
  negated: boolean;
  source_text: string;
};

export type CriteriaSchema = {
  trial_title: string;
  inclusion_quantitative: QuantitativeCriterion[];
  inclusion_categorical: CategoricalCriterion[];
  exclusion_quantitative: QuantitativeCriterion[];
  exclusion_categorical: CategoricalCriterion[];
  unparseable: string[];
};

export type AgentEvent = {
  agent: string;
  status: "started" | "completed" | "rejected" | "escalated" | "failed";
  detail: string;
  timestamp: string;
};

export type CriterionResult = {
  criterion: QuantitativeCriterion | CategoricalCriterion;
  kind: "inclusion" | "exclusion";
  status: "pass" | "fail" | "unknown";
};

export type PatientEvaluation = {
  patient_id: string;
  name: string;
  eligible: boolean;
  needs_review: boolean;
  criterion_results: CriterionResult[];
};

export type StateUpdate = {
  parsed_criteria?: CriteriaSchema;
  events?: AgentEvent[];
  matched_patients?: PatientEvaluation[];
  /** Audit trail (#50): who cleared the human-in-the-loop gate. */
  approved_by?: string | null;
  approved_by_role?: string | null;
  approved_at?: string | null;
  [key: string]: unknown;
};

export type StreamMessage = {
  node: string;
  update?: StateUpdate;
  /** Present only on the terminal `__error__` event. */
  message?: string;
};

/**
 * The phases a screening can be in — mirrors the backend's `ScreeningStatus`
 * (app/graph/state.py), which is both the graph's `current_step` and the list
 * endpoint's accepted status filter.
 */
export type ScreeningStatus =
  | "routing"
  | "parsing"
  | "critiquing"
  | "awaiting_approval"
  | "matching"
  | "done"
  | "failed"
  | "escalated";

/** One row from `GET /api/screenings` — metadata only, no protocol text. */
export type Screening = {
  thread_id: string;
  source_filename: string;
  status: ScreeningStatus;
  created_at: string;
  /** Criteria the parser extracted, across all four inclusion/exclusion buckets. */
  criteria_count: number;
  /** Patients the run matched — the eligible bucket, not the whole cohort. */
  match_count: number;
};

/**
 * `GET /api/screenings` — one page plus the total matching the filter. `total`
 * counts every match, not the rows returned, so it is what says whether there
 * is a next page.
 */
export type ScreeningPage = {
  items: Screening[];
  total: number;
  limit: number;
  offset: number;
};

/** One issue the Critic raised — `reject` blocks screening, `warn` is advisory. */
export type ComplianceFinding = {
  rule_id: string;
  severity: "reject" | "warn";
  message: string;
};

/** `GET /api/screenings/{thread_id}/state` — a run rehydrated from its checkpoint. */
export type ScreeningState = {
  values: StateUpdate & {
    source_filename?: string;
    current_step?: ScreeningStatus;
    compliance_findings?: ComplianceFinding[];
  };
  /** Nodes the graph is parked before — non-empty means it's at the approval gate. */
  pending: string[];
  /**
   * The same metadata row the runs index shows. Present even when the run has
   * no checkpoint yet (uploaded but never streamed), which is exactly when
   * `values` is empty and cannot be trusted for the filename or the phase.
   */
  screening: Screening | null;
};
