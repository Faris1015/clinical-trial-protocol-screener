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
  [key: string]: unknown;
};

export type StreamMessage = {
  node: string;
  update?: StateUpdate;
  /** Present only on the terminal `__error__` event. */
  message?: string;
};

export type ApproveResponse = {
  matched_patients: PatientEvaluation[];
  events: AgentEvent[];
};

/** One row from `GET /api/screenings` — metadata only, no protocol text. */
export type Screening = {
  thread_id: string;
  source_filename: string;
  status: string;
  created_at: string;
};
