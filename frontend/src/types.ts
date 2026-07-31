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

/**
 * Where one criterion's `source_text` sits in the protocol (#54) — `[start, end)`
 * in characters of `ProtocolPayload.text`.
 *
 * `exact` is false when only the leading run of words could be located, which
 * happens when the model paraphrased the tail of a sentence it was told to quote
 * verbatim; the viewer says so rather than presenting a partial hit as the
 * sentence itself. A `source_text` that could not be located at all has no span.
 */
export type SourceSpan = {
  source_text: string;
  start: number;
  end: number;
  exact: boolean;
};

/** `GET /api/screenings/{thread_id}/protocol` — the upload plus its spans (#54). */
export type ProtocolPayload = {
  thread_id: string;
  source_filename: string;
  text: string;
  spans: SourceSpan[];
};

export type AgentEvent = {
  agent: string;
  /** Node outcomes, plus the human-gate ones: `approved` (#50), `edited` (#53). */
  status: "started" | "completed" | "rejected" | "escalated" | "failed" | "approved" | "edited";
  detail: string;
  timestamp: string;
};

/**
 * One field-level difference between two revisions of the criteria (#53).
 *
 * `before`/`after` are labels rendered by the backend rather than raw criteria:
 * the diff is an audit record, and the server is the one place that can render it
 * identically for the editor, the run detail view, and anything reading the
 * checkpoint later. Exactly one side is null for an addition or a removal.
 */
export type CriteriaChange = {
  bucket: string;
  /** Where a reclassified criterion came from; null for every other kind. */
  from_bucket: string | null;
  kind: "modified" | "added" | "removed" | "reclassified";
  before: string | null;
  after: string | null;
};

/** One reviewer revision of the extraction — who changed what, and when (#53). */
export type CriteriaEdit = {
  revision: number;
  edited_by: string;
  edited_by_role: string;
  edited_at: string;
  changes: CriteriaChange[];
};

export type CriterionResult = {
  criterion: QuantitativeCriterion | CategoricalCriterion;
  kind: "inclusion" | "exclusion";
  status: "pass" | "fail" | "unknown";
  /**
   * Why this criterion landed on that status, in plain language (#52) — e.g.
   * "The patient's eGFR is 42 mL/min, but the trial asks for at least 60".
   * Optional because a run screened before #52 has none in its checkpoint; the
   * views fall back to the technical layer rather than rendering a blank.
   */
  explanation?: string;
};

export type PatientEvaluation = {
  patient_id: string;
  name: string;
  eligible: boolean;
  needs_review: boolean;
  criterion_results: CriterionResult[];
  /** The verdict for this patient in one plain-language sentence (#52). */
  summary?: string;
};

export type StateUpdate = {
  parsed_criteria?: CriteriaSchema;
  events?: AgentEvent[];
  matched_patients?: PatientEvaluation[];
  /**
   * Plain-language result summaries (#52) — the Critic's verdict and the cohort
   * split, each in one sentence. Written by their own node, so the critic frame
   * carries `compliance_summary` and the matcher frame `match_summary`.
   */
  compliance_summary?: string;
  match_summary?: string;
  /** Audit trail (#50): who cleared the human-in-the-loop gate. */
  approved_by?: string | null;
  approved_by_role?: string | null;
  approved_at?: string | null;
  /**
   * Edit-and-rerun (#53). `criteria_revision` is 0 for the parser's own
   * extraction and increments per reviewer revision — it is the token a PATCH has
   * to echo back, so a stale editor can't overwrite someone else's corrections.
   */
  criteria_revision?: number;
  criteria_edits?: CriteriaEdit[];
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
  /** The technical layer: rule wording, attributes, thresholds. */
  message: string;
  /**
   * The same issue for a non-technical reviewer (#52). Optional for runs
   * screened before it existed — the views fall back to `message`.
   */
  explanation?: string;
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
