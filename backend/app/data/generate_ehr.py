"""Seeded synthetic EHR generator — reproducible demo data, no real patients.

Distributions are hand-tuned so a realistic oncology protocol yields roughly
10-15% eligible, a few needs-review (missing labs), and clear fails — including
deliberately tricky boundary patients (e.g. eGFR 58 against a >=60 cutoff).
"""
import json
import random

from faker import Faker

from app.config import get_settings

SEED = 42
N_PATIENTS = 100

DIAGNOSES = [
    "non-small cell lung cancer stage IIIB",
    "non-small cell lung cancer stage IV",
    "type 2 diabetes mellitus",
    "hypertension",
    "chronic kidney disease stage 3",
    "atrial fibrillation",
    "COPD",
]
BIOMARKERS = ["EGFR exon 19 deletion", "EGFR L858R", "KRAS G12C", "ALK rearrangement"]
TREATMENTS = ["prior platinum chemotherapy", "prior immunotherapy", "prior EGFR TKI therapy"]
MEDICATIONS = ["metformin", "lisinopril", "carboplatin", "pembrolizumab", "osimertinib", "warfarin"]


def make_patient(fake: Faker, rng: random.Random, idx: int) -> dict:
    labs = {
        "age": rng.randint(22, 88),
        "egfr": round(rng.gauss(75, 22), 1),
        "creatinine": round(rng.uniform(0.6, 2.4), 2),
        "systolic_bp": rng.randint(100, 185),
        "diastolic_bp": rng.randint(60, 110),
        "hba1c": round(rng.uniform(4.8, 10.5), 1),
        "bmi": round(rng.uniform(17.0, 42.0), 1),
        "anc": round(rng.uniform(0.8, 8.0), 1),
        "platelets": rng.randint(60, 420),
        "ecog": rng.choices([0, 1, 2, 3], weights=[30, 40, 20, 10])[0],
        "ejection_fraction": rng.randint(30, 70),
    }
    # A slice of patients has a missing lab -> "needs review" in the Matcher
    if rng.random() < 0.08:
        labs.pop(rng.choice(["egfr", "anc", "ejection_fraction"]))

    return {
        "id": f"PT-{idx:04d}",
        "name": fake.name(),
        "sex": rng.choice(["F", "M"]),
        "labs": labs,
        "diagnoses": rng.sample(DIAGNOSES, k=rng.randint(1, 3))
        + (rng.sample(BIOMARKERS, k=1) if rng.random() < 0.35 else []),
        "medications": rng.sample(MEDICATIONS, k=rng.randint(0, 3)),
        "history": rng.sample(TREATMENTS, k=rng.randint(0, 2)),
    }


def main() -> None:
    rng = random.Random(SEED)
    fake = Faker()
    Faker.seed(SEED)

    patients = [make_patient(fake, rng, i + 1) for i in range(N_PATIENTS)]

    # Deliberate boundary case: fails a >=60 eGFR cutoff by 2 points
    patients[0]["labs"]["egfr"] = 58.0
    patients[0]["diagnoses"] = ["non-small cell lung cancer stage IV", "EGFR exon 19 deletion"]

    out = get_settings().patients_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patients, indent=2))
    print(f"Wrote {len(patients)} synthetic patients to {out}")


if __name__ == "__main__":
    main()
