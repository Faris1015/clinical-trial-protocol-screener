"""Seeded synthetic EHR generator — reproducible demo data, no real patients.

Patients are drawn from three clinical cohorts so that every sample protocol
(see scripts/make_sample_pdfs.py) actually has an eligible population to find:

  * oncology  — NSCLC, with EGFR biomarkers *correlated to the diagnosis* (a real
                trial's molecular criterion is meaningless if biomarkers are
                sprinkled independently of the cancer). Tagged "advanced solid
                tumor" so basket-trial protocols match too.
  * metabolic — type 2 diabetes with matching HbA1c / BMI / eGFR so the
                cardio-metabolic protocol clears its conjunctive gate.
  * general   — hypertension / CKD / AF / COPD, the non-trial background.

Within each cohort labs are drawn from healthy-leaning ranges so most — but not
all — members qualify: deliberate boundary and exclusion cases (e.g. eGFR 58
against a >=60 cutoff, prior EGFR TKI therapy) keep the demo honest. Each sample
protocol lands roughly 8-22% eligible — the molecularly-targeted NSCLC trial is
the most selective, the broad solid-tumor basket the least — and a small slice
has a missing lab and lands in "needs review".
"""

import json
import random

from faker import Faker

from app.config import get_settings
from app.logging_config import configure_logging, get_logger

log = get_logger("generate_ehr")

SEED = 42
N_PATIENTS = 100

# Cohort mix (weights, not counts). Oncology + metabolic are the trial-relevant
# populations; "general" is realistic background that fails every sample trial.
COHORTS = ["oncology", "metabolic", "general"]
COHORT_WEIGHTS = [34, 24, 42]

NSCLC_STAGES = [
    "non-small cell lung cancer stage IIIB",
    "non-small cell lung cancer stage IV",
]
# EGFR drivers are what the NSCLC protocol screens on; the others co-occur with
# NSCLC but are ineligible for an EGFR-targeted trial (useful negative cases).
EGFR_BIOMARKERS = ["EGFR exon 19 deletion", "EGFR L858R"]
NON_EGFR_BIOMARKERS = ["KRAS G12C", "ALK rearrangement"]
GENERAL_DIAGNOSES = [
    "hypertension",
    "chronic kidney disease stage 3",
    "atrial fibrillation",
    "COPD",
]


def _base_labs(rng: random.Random) -> dict:
    """Healthy-leaning defaults; cohort builders override the relevant fields."""
    return {
        "age": rng.randint(22, 88),
        "egfr": round(rng.gauss(78, 15), 1),
        "creatinine": round(rng.uniform(0.6, 2.4), 2),
        "systolic_bp": rng.randint(105, 175),
        "diastolic_bp": rng.randint(60, 105),
        "hba1c": round(rng.uniform(4.8, 10.5), 1),
        "bmi": round(rng.uniform(17.0, 42.0), 1),
        "anc": round(rng.uniform(1.2, 8.0), 1),
        "platelets": rng.randint(90, 420),
        "ecog": rng.choices([0, 1, 2, 3], weights=[35, 40, 18, 7])[0],
        "ejection_fraction": rng.randint(35, 70),
    }


def _oncology_patient(rng: random.Random, labs: dict) -> dict:
    """NSCLC patient, mostly stage IV. ~80% carry an EGFR driver weighted to
    exon 19 (the NSCLC trial's target) — the eligible-leaning slice; the rest
    carry a non-EGFR biomarker or none, real ineligible negatives."""
    stage = rng.choices(NSCLC_STAGES, weights=[22, 78])[0]
    diagnoses = [stage, "advanced solid tumor"]
    if rng.random() < 0.15:  # a comorbidity, for texture
        diagnoses.append(rng.choice(GENERAL_DIAGNOSES))

    roll = rng.random()
    if roll < 0.80:
        diagnoses.append(rng.choices(EGFR_BIOMARKERS, weights=[70, 30])[0])
    elif roll < 0.92:
        diagnoses.append(rng.choice(NON_EGFR_BIOMARKERS))

    # Oncology labs lean eligible for organ-function gates, with margin so an
    # LLM-picked eGFR cutoff anywhere in 45-60 still admits the cohort.
    labs["egfr"] = round(rng.gauss(80, 12), 1)
    labs["ejection_fraction"] = rng.randint(48, 70)
    labs["anc"] = round(rng.uniform(1.5, 7.0), 1)
    labs["age"] = rng.randint(45, 84)
    labs["ecog"] = rng.choices([0, 1, 2, 3], weights=[40, 42, 14, 4])[0]
    labs["systolic_bp"] = rng.randint(105, 158)  # below the >160 exclusion

    history = []
    if rng.random() < 0.15:  # prior EGFR TKI -> excluded from the NSCLC trial
        history.append("prior EGFR TKI therapy")
    if rng.random() < 0.3:
        history.append(rng.choice(["prior platinum chemotherapy", "prior immunotherapy"]))
    meds = rng.sample(["carboplatin", "pembrolizumab", "osimertinib"], k=rng.randint(0, 2))
    return {"diagnoses": diagnoses, "history": history, "medications": meds}


def _metabolic_patient(rng: random.Random, labs: dict) -> dict:
    """Type 2 diabetes, with HbA1c / BMI / eGFR / age leaning into the
    cardio-metabolic protocol's window; some drift out for texture."""
    diagnoses = ["type 2 diabetes mellitus"]
    if rng.random() < 0.5:
        diagnoses.append("hypertension")
    if rng.random() < 0.2:
        diagnoses.append("chronic kidney disease stage 3")

    labs["age"] = rng.randint(42, 74)
    labs["hba1c"] = round(rng.uniform(6.8, 10.2), 1)
    labs["bmi"] = round(rng.uniform(27.0, 40.0), 1)
    labs["egfr"] = round(rng.gauss(68, 14), 1)
    labs["systolic_bp"] = rng.randint(120, 178)
    labs["platelets"] = rng.randint(120, 400)

    meds = ["metformin"] + rng.sample(["lisinopril", "warfarin"], k=rng.randint(0, 1))
    return {"diagnoses": diagnoses, "history": [], "medications": meds}


def _general_patient(rng: random.Random, labs: dict) -> dict:
    """Non-trial background: real comorbidities, ineligible for every sample."""
    diagnoses = rng.sample(GENERAL_DIAGNOSES, k=rng.randint(1, 3))
    meds = rng.sample(["lisinopril", "warfarin", "metformin"], k=rng.randint(0, 2))
    return {"diagnoses": diagnoses, "history": [], "medications": meds}


BUILDERS = {
    "oncology": _oncology_patient,
    "metabolic": _metabolic_patient,
    "general": _general_patient,
}


def make_patient(fake: Faker, rng: random.Random, idx: int, cohort: str) -> dict:
    labs = _base_labs(rng)
    profile = BUILDERS[cohort](rng, labs)

    # A slice of patients has a missing lab -> "needs review" in the Matcher.
    if rng.random() < 0.08:
        labs.pop(rng.choice(["egfr", "anc", "ejection_fraction"]), None)

    return {
        "id": f"PT-{idx:04d}",
        "name": fake.name(),
        "sex": rng.choice(["F", "M"]),
        "cohort": cohort,
        "labs": labs,
        "diagnoses": profile["diagnoses"],
        "medications": profile["medications"],
        "history": profile["history"],
    }


def main() -> None:
    configure_logging()
    rng = random.Random(SEED)
    fake = Faker()
    Faker.seed(SEED)

    cohorts = rng.choices(COHORTS, weights=COHORT_WEIGHTS, k=N_PATIENTS)
    patients = [make_patient(fake, rng, i + 1, cohorts[i]) for i in range(N_PATIENTS)]

    # Deliberate boundary case: an otherwise-perfect NSCLC/EGFR patient whose
    # eGFR fails a >=60 cutoff by 2 points — the demo's "so close" record. The
    # full lab profile is pinned (not left to patient 0's random draw) so eGFR
    # is the SOLE failing criterion for the NSCLC protocol.
    patients[0]["cohort"] = "oncology"
    patients[0]["diagnoses"] = [
        "non-small cell lung cancer stage IV",
        "advanced solid tumor",
        "EGFR exon 19 deletion",
    ]
    patients[0]["history"] = []
    patients[0]["medications"] = ["osimertinib"]
    patients[0]["labs"] = {
        "age": 64,
        "egfr": 58.0,  # the single near-miss: fails a >=60 cutoff by 2 points
        "creatinine": 1.1,
        "systolic_bp": 132,
        "diastolic_bp": 78,
        "hba1c": 5.4,
        "bmi": 26.5,
        "anc": 3.2,
        "platelets": 240,
        "ecog": 1,
        "ejection_fraction": 60,
    }

    out = get_settings().patients_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patients, indent=2))
    log.info("generate_ehr.wrote", count=len(patients), path=str(out))


if __name__ == "__main__":
    main()
