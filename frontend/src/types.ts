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
  /**
   * Node outcomes, plus the human-gate ones: `approved` (#50), `edited` (#53).
   * `rejected` is shared — the Critic pushing an extraction back, or a reviewer
   * stopping the run (#91) — and `agent` is what tells the two apart.
   */
  status: "started" | "completed" | "rejected" | "escalated" | "failed" | "approved" | "edited";
  detail: string;
  timestamp: string;
};

/**
 * One step of a run's audit timeline (#55) — derived server-side from the same
 * `events` log, so the run detail view and the exported report tell one story.
 *
 * `label`, `outcome` and `elapsed` arrive already rendered (`"Regulatory
 * Critic"`, `"Rejected"`, `"+1.5s"`); `agent`/`status`/`timestamp` are the raw
 * values behind them, kept for data attributes and colour mapping. `attempt` is
 * the retry round a Parser/Critic step belongs to and 0 for every other step;
 * `revision` is the criteria revision an `edited` step produced, 0 otherwise;
 * `actor` names the human behind a human step and is empty for machine steps.
 */
export type TimelineEntry = {
  seq: number;
  agent: string;
  label: string;
  status: AgentEvent["status"] | string;
  outcome: string;
  detail: string;
  timestamp: string;
  elapsed: string;
  attempt: number;
  revision: number;
  actor: string;
  actor_role: string;
};

/** The run's shape in numbers, above the timeline's entries (#55). */
export type TimelineSummary = {
  started_at: string;
  ended_at: string;
  /** Rendered span from the first event to the last, e.g. `"3m 4s"`. */
  duration: string;
  /** Parser runs — more than one means the Critic sent the extraction back. */
  attempts: number;
  critic_rejections: number;
  revisions: number;
  escalated: boolean;
  approved_by: string;
  approved_by_role: string;
  approved_at: string;
  /** The gate's other decision (#91) — empty for every run that wasn't rejected. */
  rejected_by: string;
  rejected_by_role: string;
  rejected_at: string;
  rejected_reason: string;
};

export type RunTimeline = {
  entries: TimelineEntry[];
  summary: TimelineSummary;
};

/**
 * What one criterion did to the cohort (#94) — mirrors
 * `backend/app/services/attrition.py`.
 *
 * `excluded`, `unresolved` and `passed` are patient counts that partition the
 * cohort the criterion was applied to. `unique` is the part of `excluded` no other
 * criterion also failed and `shared` the rest; `recoverable` is how many patients
 * would actually reach the *eligible* bucket if this criterion were dropped, which
 * is smaller than `unique` whenever one of them still has something a human has to
 * resolve. Rendering `excluded` alone would promise a delta the cohort can't pay.
 *
 * `share` is `excluded` as a percentage of the cohort, rounded by the API so the
 * figure and the bar beside it are one value rather than two roundings of it.
 */
/**
 * The numeric bound a criterion carries, in machine form (#95).
 *
 * Sent beside the rendered `label` rather than in place of it: the label is the
 * one wording a criterion is shown in everywhere, and parsing "egfr >= 60
 * mL/min/1.73m2" back apart in the browser would be a second implementation of a
 * rule that lives in `services/criteria_edits.py`.
 *
 * `observed_min`/`observed_max` are the lowest and highest values this cohort
 * actually recorded for the attribute, so a slider bounded by them has the two
 * trivial answers at its ends and every real one in between. Both null for a run
 * screened before the Matcher recorded the values it compared — which is what
 * tells the panel to offer a number box rather than a slider it cannot bound.
 */
export type CriterionThreshold = {
  operator: QuantitativeCriterion["operator"];
  value: number;
  value_high: number | null;
  unit: string;
  observed_min: number | null;
  observed_max: number | null;
};

export type CriterionAttrition = {
  key: string;
  /** The same one-line label the report and the edit history name it with. */
  label: string;
  kind: "inclusion" | "exclusion";
  source_text: string;
  /** Null for a categorical criterion — there is no bound to move (#95). */
  threshold?: CriterionThreshold | null;
  excluded: number;
  unresolved: number;
  passed: number;
  unique: number;
  shared: number;
  recoverable: number;
  share: number;
};

/** How many patients two criteria both exclude — one entry per pair (#94). */
export type CriterionOverlap = {
  a_key: string;
  b_key: string;
  a_label: string;
  b_label: string;
  patients: number;
};

/**
 * `GET /api/screenings/{id}/state`'s `attrition` block (#94).
 *
 * The three bucket figures are `services/cohort.py`'s own counts, so this panel
 * cannot disagree with the cohort table under it. `excluded` and `unresolved` are
 * patient counts that overlap each other (a patient can fail one criterion and be
 * indeterminate on another), and `unscored` is patients no criterion was applied
 * to at all — named so the rows still reconcile with the buckets.
 */
export type CohortAttrition = {
  totals: {
    patients: number;
    eligible: number;
    review: number;
    ineligible: number;
    excluded: number;
    unresolved: number;
    unscored: number;
  };
  /** Ranked, most exclusions first; includes criteria that excluded nobody. */
  criteria: CriterionAttrition[];
  /** Only pairs that actually share patients, largest first. */
  overlaps: CriterionOverlap[];
};

/**
 * Why one criterion went unchecked (#93). `unparseable` is a protocol sentence the
 * Parser would not invent structure for — a vocabulary gap; `unresolved` is a
 * structured criterion the Matcher could not settle against any patient record — a
 * data gap. They are fixed by different work, so the panel names them apart.
 */
export type CoverageReason = "unparseable" | "unresolved";

/**
 * One thing a run could not check.
 *
 * `text` is the verbatim protocol sentence for an `unparseable` gap and the
 * criterion's usual one-line label for an `unresolved` one, so a reader can find
 * the row it refers to in the criteria table. `patients` is how many patients the
 * Matcher returned `unknown` for, and 0 for a sentence that never reached it.
 */
export type CoverageGap = {
  reason: CoverageReason | string;
  text: string;
  kind: "inclusion" | "exclusion" | "";
  patients: number;
};

/**
 * The screenability score in the three figures a table cell needs (#93) — what
 * the runs index serves per row, rebuilt from that row's denormalized columns.
 *
 * `score` is `checkable` over `criteria` as a percentage, resolved by the API
 * (`services/coverage.score_of`) rather than divided here: the index cell and the
 * run's own panel have to be one formula read twice. `criteria === 0` means the run
 * has no extraction to score, not that it scored zero.
 */
export type CoverageSummary = {
  checkable: number;
  criteria: number;
  score: number;
};

/**
 * `GET /api/screenings/{id}/state`'s `coverage` block (#93) — how much of this
 * protocol the run could actually check, and what it could not.
 *
 * `criteria` is every criterion the extraction produced (`structured` +
 * `unparseable`), which is the number a reviewer counts in the criteria table.
 * `resolved`/`unresolved` split `structured` once a cohort exists; before that both
 * are 0, `scored` is false and `match_score` is null — a run parked at the gate has
 * not failed to resolve anything, and the panel says its figure is provisional
 * rather than implying the Matcher had a go.
 */
export type Coverage = CoverageSummary & {
  structured: number;
  unparseable: number;
  resolved: number;
  unresolved: number;
  /** Whether the Matcher ran; false makes the second layer provisional. */
  scored: boolean;
  /** `structured` over `criteria` — what the parse could cover. */
  parse_score: number;
  /** `resolved` over `structured`, or null before a cohort exists. */
  match_score: number | null;
  /** Sentences that never became criteria first, then the unsettled criteria. */
  gaps: CoverageGap[];
};

/** One threshold as a what-if would have it — the `simulate` request body (#95). */
export type CriterionOverride = {
  /** The criterion's attrition key, so a what-if can only address a row on screen. */
  key: string;
  operator: QuantitativeCriterion["operator"];
  value: number;
  value_high: number | null;
};

/**
 * One override echoed back with what the API made of it (#95).
 *
 * `key` addresses the row in `current`, `simulated_key` the same criterion in
 * `simulated` — they differ because a criterion's identity is its rendered label,
 * so moving its threshold renames it. `findings` is the deterministic Critic's
 * verdict on the *new* value, and `unavailable` how many patients this run scored
 * without recording the value it compared, who therefore could not be re-checked.
 */
export type SimulatedOverride = {
  key: string;
  simulated_key: string;
  kind: "inclusion" | "exclusion";
  attribute: string;
  unit: string;
  before: string;
  after: string;
  findings: ComplianceFinding[];
  unavailable: number;
};

/**
 * `POST /api/screenings/{id}/simulate` — the cohort under moved thresholds (#95).
 *
 * `current` is the same `attrition` block `/state` serves, so the two columns a
 * reviewer compares are one derivation rather than two. `criteria` is the whole
 * extraction with the overrides applied: promoting a what-if is `PATCH
 * /criteria` with this payload and `criteria_revision` as `base_revision`, which
 * is why there is no separate promote endpoint — a threshold that reached the
 * criteria without passing the Critic would be the hole the gate exists to close.
 */
export type Simulation = {
  overrides: SimulatedOverride[];
  current: CohortAttrition;
  simulated: CohortAttrition;
  /** `simulated - current` per bucket; negative when a threshold was tightened. */
  delta: { eligible: number; review: number; ineligible: number };
  criteria: CriteriaSchema;
  criteria_revision: number;
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
   * The gate's other outcome (#91): who stopped the run, when, and why. A run
   * carries these or the `approved_*` trio, never both — and `rejected_reason` is
   * never blank when the others are set, since the API requires it.
   */
  rejected_by?: string | null;
  rejected_by_role?: string | null;
  rejected_at?: string | null;
  rejected_reason?: string | null;
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
  | "escalated"
  /** Terminal, and a decision rather than a breakdown (#91) — see `rejected_by`. */
  | "rejected";

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
  /**
   * How much of the protocol this run could check (#93), denormalized into the
   * row so the index needs no checkpoint per screening. Optional so a payload
   * from an older build renders the rest of the table.
   */
  coverage?: CoverageSummary;
  /**
   * When the run entered a human stop (awaiting_approval or escalated) (#103).
   * Null for runs that never parked.
   */
  gate_entered_at?: string | null;
  /** When a stale reminder was last dispatched for this run (#103). */
  last_reminder_at?: string | null;
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

/**
 * One file's outcome from `POST /api/screenings/batch` (#61).
 *
 * Exactly one of `thread_id` and `error` is set: the file became a screening, or
 * it was refused (an unreadable PDF, a disallowed type, one over the size cap)
 * while the rest of the batch went through. `error`/`detail` are the same pair
 * every API rejection carries, so a refused file renders like a failed single
 * upload. `filename` is the server's sanitized name — the one history will show —
 * and the items echo the order the files were submitted in.
 */
export type BatchItem = {
  filename: string;
  thread_id: string | null;
  error: string | null;
  detail: string | null;
};

/** `POST /api/screenings/batch` — one item per submitted file, plus the tally. */
export type BatchCreated = {
  items: BatchItem[];
  created: number;
  rejected: number;
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

/**
 * One rule from the compliance database (#57) — a row of
 * `app/rules/compliance_rules.yaml`, or the synthetic `LLM-SEM` entry standing
 * for the Critic's semantic layer.
 *
 * `condition`, `check_label` and `severity` arrive already resolved by the API:
 * how a threshold reads depends on the check kind, and that knowledge belongs
 * beside the engine that runs it (services/rules.py) rather than in a component
 * that would re-derive it and drift. `severity` is `"varies"` for the semantic
 * layer, whose findings pick their own, and empty for a check kind the engine
 * has no branch for — such a rule can never fire.
 */
export type ComplianceRule = {
  id: string;
  /** The criterion attribute the rule is scoped to; empty when it isn't. */
  attribute: string;
  check: string;
  check_label: string;
  /** The threshold/operator in one line, e.g. `"90 ≤ systolic_bp ≤ 200"`. */
  condition: string;
  severity: "reject" | "warn" | "varies" | "";
  /** The technical rationale — the wording that rides back to the Parser. */
  description: string;
  /** The same rationale for a non-technical reviewer (#52). */
  plain: string;
  /** Protocol words that bring the rule into play; empty when it always runs. */
  keywords: string[];
  layer: "deterministic" | "semantic";
  /**
   * False for a retired rule (#97). Retirement is soft: the rule stops firing but
   * stays listed, because a finding from a past run still cites its id.
   */
  enabled: boolean;
  /**
   * Whether an admin may author this row. False for the semantic layer, which is
   * described here but is not a rule anyone can edit or switch off. Carried rather
   * than derived from `layer` so a component never decides for itself which rows
   * get an edit button.
   */
  editable: boolean;
  created_by: string;
  created_at: string;
  updated_by: string;
  updated_at: string;
  /** `range` bounds, present only on a rule of that check kind. */
  min_plausible?: number;
  max_plausible?: number;
  /** `keyword_implies_criterion`'s target category. */
  required_category?: string;
};

/** The check kinds the deterministic engine implements, and so the ones an admin may author. */
export type CheckKind =
  "range" | "must_be_quantitative" | "required_attribute" | "keyword_implies_criterion";

/**
 * The rule editor's form state (#97).
 *
 * Every field is a string, including the numeric bounds — see `lib/rules.ruleForm`
 * for why that is the right shape for a form rather than a lossy one.
 */
export type RuleForm = {
  id: string;
  check: CheckKind;
  attribute: string;
  description: string;
  plain: string;
  /** Comma-separated in the input; split on submit. */
  keywords: string;
  min_plausible: string;
  max_plausible: string;
  required_category: string;
};

/** `GET /api/rules` — every rule the Critic checks against, in table order (#57, #97). */
export type RulesPayload = {
  rules: ComplianceRule[];
  /**
   * The file the rules table was *seeded* from, by name. Not what the instance
   * runs — since first boot that is the table itself (#97).
   */
  source: string;
  /** How many rules the engine will actually run on the next screening. */
  active: number;
};

/**
 * One row of the metrics summary (#58) — a count and its percentage of the panel
 * it belongs to.
 *
 * `share` is 0–100 with one decimal, resolved by the API and used for both the
 * figure and the bar's width, so the number a reviewer reads and the bar they
 * compare are the same value rather than two roundings of it.
 */
export type MetricShare = {
  count: number;
  share: number;
};

/** One terminal outcome of the screening funnel, with `label` pre-rendered. */
export type FunnelOutcome = MetricShare & {
  outcome: ScreeningStatus | string;
  label: string;
};

/** Which rules the Critic blocked on, ranked — `share` is of all blocking findings. */
export type RejectionRule = MetricShare & {
  rule_id: string;
  /** `semantic` is the LLM layer, whose findings are not a fixed threshold (#57). */
  layer: "deterministic" | "semantic";
};

/**
 * One `unparseable` phrasing, ranked across runs (#93) — the vocabulary backlog.
 *
 * `runs` is how many runs' extractions contained it, `count` how many times it
 * appeared in total, and `share` that count as a percentage of every unparseable
 * sentence in the window. Grouped case- and whitespace-insensitively by the API,
 * so two uploads of one protocol rank as one phrasing.
 */
export type CoveragePhrase = MetricShare & {
  text: string;
  runs: number;
};

/**
 * Coverage pooled across runs (#93), on the metrics summary.
 *
 * Unlike every other panel there, this one is not read off a Prometheus counter —
 * ranking the phrasings the vocabulary cannot parse means reading the sentences, so
 * the API walks the `sampled` most recent runs' checkpoints. `total` is how many
 * runs exist, and the page states both: a window that read as the whole history
 * would be the one way this panel could mislead.
 *
 * `runs` is the sampled runs that had an extraction at all — the population the
 * figures describe — and `scored` how many of those reached the Matcher. `score` is
 * pooled (`checkable` over `criteria` across the window), not a mean of per-run
 * scores, so a two-criterion protocol does not swing it as far as a forty-criterion
 * one. `phrasings` is how many distinct wordings were seen, against the capped
 * `phrases` ranking.
 */
export type CoverageAggregate = {
  sampled: number;
  total: number;
  runs: number;
  scored: number;
  criteria: number;
  checkable: number;
  structured: number;
  unparseable: number;
  unresolved: number;
  score: number;
  phrasings: number;
  phrases: CoveragePhrase[];
};

/** One node's share of one run's LLM bill (#101). */
export type RunNodeUsage = {
  node: string;
  /** LLM calls this node made — the figure the matcher's caching claim is pinned to. */
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  cost_micro_usd: number;
  cost_usd: number;
};

/**
 * One run's whole LLM bill (#101), from its checkpoint.
 *
 * `nodes` omits nodes that made no call, so a run parked at the gate has no
 * Matcher row — "this has not happened yet" rather than a zero that reads as
 * "this was free". `priced` is false when no model in the run had a price, which
 * is what lets the panel say "no cost" (a local model) instead of "$0.00".
 * `estimated_calls` is how many calls had their tokens estimated rather than
 * reported by the provider — the stub reports nothing, so its runs are all
 * estimated.
 */
export type RunUsage = {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  cost_micro_usd: number;
  cost_usd: number;
  estimated_calls: number;
  priced: boolean;
  nodes: RunNodeUsage[];
};

/**
 * One node's share of the LLM bill (#101), instance-wide.
 *
 * `total_cost_usd` is every dollar this node has cost since the instance came up;
 * `median_cost_usd` is what it costs on one screening, estimated from the
 * per-screening histogram and therefore null until a run has reached this node.
 * `screenings` is that histogram's count — the population the median describes,
 * which for the Matcher is approved runs rather than all runs.
 */
export type NodeCost = {
  node: string;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  total_cost_usd: number;
  median_cost_usd: number | null;
  screenings: number;
};

/**
 * What the models cost this instance, and what one screening costs (#101).
 *
 * `calls_priced` is false on a deployment whose models have no entry in the price
 * table — a local Ollama, or the load-test stub. Those report real tokens at
 * exactly zero dollars, and the page has to say *why* it shows no money rather
 * than printing "$0.00" as though the figure were a measurement of spend.
 *
 * The two percentiles are estimated from histogram buckets (see
 * `MetricsSummary.estimated_percentiles`) and are null when nothing has been
 * observed, or when the value falls in the open-ended top bucket where there is
 * no bound to interpolate towards.
 */
export type CostSummary = {
  /** Terminal runs whose cost was observed — the median's denominator. */
  screenings: number;
  /** Whether any call was priced at all; false on a local-only instance. */
  calls_priced: boolean;
  prompt_tokens: number;
  completion_tokens: number;
  tokens: number;
  total_cost_usd: number;
  median_cost_usd: number | null;
  p95_cost_usd: number | null;
  /**
   * The exact mean, from the histogram's own sum — the companion to the
   * estimated median. Both are served because they fail differently: the median
   * shrugs off one pathological run, the mean is right to the micro-dollar.
   */
  mean_cost_usd: number | null;
  nodes: NodeCost[];
};

/** One node's wall-clock percentiles (#101), estimated from its duration histogram. */
export type NodeLatency = {
  node: string;
  /** Node executions timed — including the runs that ended in an error. */
  runs: number;
  p50_seconds: number | null;
  p95_seconds: number | null;
};

/**
 * The Matcher's term-mapping cache (#101) — the architectural claim as a number.
 *
 * `resolutions` is how many `(criterion, term)` questions the cohorts screened so
 * far required; `llm_pairs` is how many actually reached a model. The gap is what
 * resolving once per screening (rather than once per patient) saved, so the rate
 * climbs with cohort size instead of sitting flat. `hit_rate` is null before
 * anything has been resolved — a share of nothing is not 100%.
 */
export type TermMappingCache = {
  resolutions: number;
  llm_pairs: number;
  served_from_cache: number;
  hit_rate: number | null;
};

/**
 * `GET /api/metrics/summary` — the domain metrics as an in-app summary (#58),
 * plus the coverage aggregate (#93) and the cost accounting (#101).
 *
 * The counter-backed panels are read off the same collectors `/metrics` serializes, so
 * the page and a scrape cannot disagree. The counters live in the serving process's
 * memory and reset with it, which is why `since` travels: these are the runs this
 * instance has seen, not an all-time total. `coverage` is the exception — it comes
 * from the durable checkpoints, so it survives a restart and states its own window.
 */
export type MetricsSummary = {
  /** ISO-8601 epoch of the counters — when this instance's registry came up. */
  since: string;
  /** Whether `/metrics` is exposed here (METRICS_ENABLED); recording is unconditional. */
  exported: boolean;
  funnel: {
    /** Runs that reached a terminal outcome. The denominator of every share. */
    total: number;
    outcomes: FunnelOutcome[];
  };
  rejections: {
    /** Blocking findings, counted once per finding per Critic pass. */
    total: number;
    rules: RejectionRule[];
    /** Blocking findings ÷ terminal runs — not the share of runs rejected. */
    per_run: number;
  };
  attempts: {
    /** Runs whose parse/critic loop resolved; excludes failures, so ≤ funnel total. */
    observations: number;
    mean: number;
    /** Share that needed one attempt, or null when the buckets can't say. */
    first_pass_share: number | null;
    /** De-cumulated histogram, captioned by attempt count ("1", "6–10"). */
    buckets: (MetricShare & { label: string })[];
  };
  /**
   * Screenability across recent runs (#93). Optional so a payload from an older
   * build renders the three counter panels rather than failing on a missing key.
   */
  coverage?: CoverageAggregate;
  /**
   * Whether the percentile figures below are histogram estimates rather than
   * exact quantiles. Always true today; carried in the payload so the page states
   * it rather than hard-coding a claim about how the API works.
   */
  estimated_percentiles?: boolean;
  /** Tokens and money (#101). Optional so an older payload renders the rest. */
  cost?: CostSummary;
  /** Per-node p50/p95 wall-clock (#101), one row per node the registry has timed. */
  latency?: NodeLatency[];
  /** What the Matcher's per-screening term-mapping cache saved (#101). */
  term_mapping?: TermMappingCache;
};

/**
 * One run's column header in a side-by-side comparison (#59).
 *
 * `side` echoes the query parameter that named it, so a row's `a`/`b` fields are
 * unambiguous without matching thread ids. The counts come from the same
 * checkpoint the rows below are built from rather than from the runs index's
 * denormalized columns, so a header can't contradict the table under it.
 */
export type ComparisonRun = {
  side: "a" | "b";
  thread_id: string;
  source_filename: string;
  status: ScreeningStatus | string;
  created_at: string;
  trial_title: string;
  /** 0 for the parser's own extraction, N for the Nth reviewer revision (#53). */
  criteria_revision: number;
  /** Criteria across the four real buckets — `unparseable` excluded, as elsewhere. */
  criteria_count: number;
  cohort: { eligible: number; review: number; ineligible: number; total: number };
  /** Whether this run has an extraction at all — false for one that never streamed. */
  parsed: boolean;
  /** Whether it got as far as scoring a cohort; false for one parked at the gate. */
  matched: boolean;
};

/** `unchanged` is not a difference; the other three are what gets highlighted. */
export type CriteriaCompareKind = "unchanged" | "modified" | "added" | "removed";

/**
 * One criterion as the two runs have it — paired on the protocol sentence both
 * quote, so a criterion that moved position is not reported as a change. Exactly
 * one side is null for an addition or a removal, both are set otherwise.
 */
export type CriteriaCompareRow = {
  kind: CriteriaCompareKind;
  a: string | null;
  b: string | null;
};

/** One criteria bucket's paired rows. Buckets neither run used are absent. */
export type CriteriaCompareBucket = {
  bucket: string;
  rows: CriteriaCompareRow[];
};

/** One patient's verdict in one run, with `label` pre-rendered by the API. */
export type CohortVerdict = {
  bucket: "eligible" | "review" | "ineligible" | string;
  label: string;
};

/** `changed` is the same patient with a different verdict; `only_*` one run scored. */
export type MatchCompareKind = "same" | "changed" | "only_a" | "only_b";

/** One patient's verdict in each run. `a`/`b` are null when that run never scored them. */
export type PatientCompareRow = {
  patient_id: string;
  name: string;
  kind: MatchCompareKind;
  a: CohortVerdict | null;
  b: CohortVerdict | null;
};

/**
 * `GET /api/screenings/compare?a=…&b=…` — two runs diffed side by side (#59).
 *
 * `runs` is `[a, b]`, the order the request named them, and every row below states
 * its A and B side under those same keys. The pairing (criteria by protocol
 * sentence, patients by EHR id) happens server-side, beside the reviewer-edit diff
 * that uses the same keying — see backend/app/services/comparison.py.
 */
export type RunComparison = {
  runs: ComparisonRun[];
  criteria: {
    buckets: CriteriaCompareBucket[];
    totals: Record<CriteriaCompareKind, number>;
    /** modified + added + removed — 0 when the two extractions agree. */
    differences: number;
    /** True only when both runs parsed *and* nothing differs. */
    identical: boolean;
  };
  matches: {
    patients: PatientCompareRow[];
    totals: Record<MatchCompareKind, number>;
    differences: number;
    /** Whether both runs scored a cohort; false leaves one column empty by phase. */
    compared: boolean;
  };
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
   * The run's audit timeline (#55), derived from `values.events` by the API.
   * Optional so a payload from an older build renders the rest of the page
   * instead of failing on a missing key.
   */
  timeline?: RunTimeline;
  /**
   * Per-criterion cohort attrition (#94), derived by the API from
   * `values.matched_patients`. Optional for the same reason the timeline is: a
   * payload from an older build renders the rest of the page rather than failing
   * on a missing key.
   */
  attrition?: CohortAttrition;
  /**
   * Screenability (#93), derived by the API from `values.parsed_criteria` and
   * `values.matched_patients`. Optional for the same reason the two above are.
   */
  coverage?: Coverage;
  /**
   * This run's LLM bill (#101), derived by the API from `values.llm_usage` —
   * tokens and estimated cost, split by the node that spent them. Optional for
   * the same reason the three above are: a run checkpointed by a build that
   * predates the accounting has no `llm_usage`, and the page renders without it.
   */
  usage?: RunUsage;
  /**
   * The same metadata row the runs index shows. Present even when the run has
   * no checkpoint yet (uploaded but never streamed), which is exactly when
   * `values` is empty and cannot be trusted for the filename or the phase.
   */
  screening: Screening | null;
};

/**
 * One entry in the org-wide decision index (#98) — `GET /api/audit`.
 *
 * `label` is `action` in the form a human reads; both travel so a filter can key
 * on the enum while the table prints the name. `revision` is non-zero only for a
 * `criteria_revised` entry, and it is what addresses that revision's before/after
 * diff on the run's own page.
 */
export type AuditEntry = {
  id: number;
  thread_id: string;
  action: AuditAction | string;
  label: string;
  actor: string;
  actor_role: string;
  occurred_at: string;
  detail: string;
  revision: number;
  source_filename: string;
  /**
   * What the decision was about (#97). A run for the four gate decisions, a
   * compliance rule for the authoring ones — which is what lets the table build
   * the right deep link without inferring the subject from the action name.
   */
  subject_kind: AuditSubjectKind | string;
  subject_id: string;
};

/** The decisions the index carries. A future action still renders — see `lib/audit`. */
export type AuditAction =
  | "approved"
  | "rejected"
  | "criteria_revised"
  | "escalated"
  | "rule_created"
  | "rule_updated"
  | "rule_disabled"
  | "rule_enabled";

/** What an entry points at. */
export type AuditSubjectKind = "screening" | "rule";

/**
 * `GET /api/audit` — one page of the index plus the filter that was applied.
 *
 * `scope` is what the server actually narrowed by, not what was asked for: a
 * reviewer is scoped to their own decisions server-side (#98 AC 7), and echoing
 * it back is how the page can say so without inferring it from the role.
 */
export type AuditPage = {
  items: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
  scope: {
    actor: string | null;
    action: string | null;
    thread_id: string | null;
    from: string | null;
    to: string | null;
  };
};

// --- The cohort, and the question asked backwards (#96) ----------------------

/**
 * One row of the cohort index — `GET /api/patients`.
 *
 * Counts rather than the lists themselves: the index is a table of who exists,
 * and shipping every diagnosis and medication for a hundred patients to render
 * it would be most of the EHR fetched to draw a list of names. The record is one
 * click away, which is where a reader who wants it is going anyway.
 */
export type PatientSummary = {
  id: string;
  name: string;
  sex: string;
  cohort: string;
  /** Null when the record carries no age — the same gap the Matcher reads as
   * "could not be checked" rather than as a failure. */
  age: number | null;
  diagnoses: number;
  medications: number;
  history: number;
};

export type PatientPage = {
  items: PatientSummary[];
  total: number;
  limit: number;
  offset: number;
};

/**
 * One patient's whole record — `GET /api/patients/{id}`.
 *
 * `labs` is an open map because the attribute set is the EHR generator's, not
 * this type's: a lab the generator adds should render as a row rather than be
 * silently dropped by a closed interface. The Matcher reads it the same way.
 */
export type PatientRecord = {
  id: string;
  name: string;
  sex: string;
  cohort: string;
  labs: Record<string, number | undefined>;
  diagnoses: string[];
  medications: string[];
  history: string[];
};

/**
 * How this patient fared against one trial — one row of
 * `GET /api/patients/{id}/trials`.
 *
 * `source` is the one field worth reading twice. `"recorded"` means the run
 * itself scored this patient and the verdict here *is* the row from its cohort
 * table. `"rematched"` means the run never saw them — the records were
 * regenerated, or the run predates them — so its criteria were applied to them
 * here, reusing the term mappings that run resolved and calling no model. The
 * two have different standing, and a coordinator deciding whether to act on a
 * match is entitled to know which they are looking at.
 *
 * `unmapped` counts the categorical criteria a rematch could not settle from
 * those stored mappings, and is always 0 for a recorded verdict. Any value above
 * zero means the patient is in needs-review *because the question was never put
 * to anything*, which is a different thing from a criterion that was checked and
 * came back undecidable.
 */
export type TrialMatch = {
  thread_id: string;
  source_filename: string;
  trial_title: string;
  status: ScreeningStatus;
  created_at: string;
  bucket: CohortBucketName;
  eligible: boolean;
  needs_review: boolean;
  summary: string;
  criterion_results: CriterionResult[];
  source: "recorded" | "rematched";
  unmapped: number;
};

/** The bucket names the API sends — the server-side spelling of `CohortBucket`. */
export type CohortBucketName = "eligible" | "review" | "ineligible";

/**
 * `GET /api/patients/{id}/trials` — the pipeline transposed.
 *
 * `scanned`/`total` state the window the answer was reached in: the walk reads
 * the most recent runs, and a page reporting "2 eligible trials" without saying
 * it looked at thirty of forty would be understating by an amount the reader
 * cannot see.
 */
export type ReverseMatch = {
  patient_id: string;
  patient: PatientRecord;
  trials: TrialMatch[];
  counts: Record<CohortBucketName, number>;
  scanned: number;
  total: number;
};
