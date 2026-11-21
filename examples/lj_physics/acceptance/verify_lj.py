"""Measure a two/three-atom LJ implementation against independent reference equations."""

import itertools
import json
import math
import sys
from pathlib import Path

EPSILON = 0.01032
SIGMA = 3.405
MASS = 39.948
EV_PER_AMU_ANGSTROM2_FS2 = 1.66053906660e-27 * (1e-10 / 1e-15) ** 2 / 1.602176634e-19
MINIMUM = SIGMA * 2 ** (1 / 6)
TRIANGLE = ((0.1, -0.2, 0.3), (3.57, 0.4, -0.2), (1.2, 3.5, 1.1))
DIMER = ((0, 0, 0), (4.35, 0, 0))
VELOCITIES = ((-0.0005, 0.00015, 0), (0.0005, -0.00015, 0))
DURATION_FS = 600
COARSE_DT_FS = 4


def reference_energy(positions):
    energy = 0.0
    for first, second in itertools.combinations(positions, 2):
        ratio = SIGMA / math.dist(first, second)
        energy += 4 * EPSILON * (ratio ** 12 - ratio ** 6)
    return energy


def force_metrics(model):
    energy, forces = model.energy_forces(TRIANGLE)
    h = 1e-5  # Angstrom
    errors = []
    for atom, force in enumerate(forces):
        for axis, actual in enumerate(force):
            minus = [list(p) for p in TRIANGLE]
            plus = [list(p) for p in TRIANGLE]
            minus[atom][axis] -= h
            plus[atom][axis] += h
            numerical = (reference_energy(minus) - reference_energy(plus)) / (2 * h)
            errors.append(abs(actual - numerical))
    _, minimum_forces = model.energy_forces(((0, 0, 0), (MINIMUM, 0, 0)))
    return {
        "minimum_force_norm": max(math.hypot(*force) for force in minimum_forces),
        "max_centered_difference_error": max(errors),
        "net_force_norm": math.hypot(*(sum(components) for components in zip(*forces))),
        "three_atom_energy_error": abs(energy - reference_energy(TRIANGLE)),
    }


def energy_excursion(model, dt):
    count = 0
    energies = []
    steps = int(DURATION_FS / dt)
    for positions, velocities in model.nve_trajectory(DIMER, VELOCITIES, dt, steps):
        kinetic = 0.5 * MASS * EV_PER_AMU_ANGSTROM2_FS2 * sum(
            component ** 2 for velocity in velocities for component in velocity
        )
        energies.append(reference_energy(positions) + kinetic)
        count += 1
    if count != steps + 1:
        raise ValueError(f"expected {steps + 1} full-step states, got {count}")
    return max(abs(energy - energies[0]) for energy in energies)


def nve_metrics(model):
    coarse = energy_excursion(model, COARSE_DT_FS)
    fine = energy_excursion(model, COARSE_DT_FS / 2)
    return {"coarse_dt_fs": COARSE_DT_FS, "fine_dt_fs": COARSE_DT_FS / 2, "duration_fs": DURATION_FS,
            "coarse_energy_excursion": coarse, "fine_energy_excursion": fine,
            "coarse_to_fine_ratio": coarse / fine if fine else 0.0}


def collect_metrics(model):
    return {
        "energy_unit": "eV", "length_unit": "Angstrom", "force_unit": "eV/Angstrom",
        "time_unit": "fs", "mass_unit": "amu",
        "pair": {"sigma_energy_error": abs(model.pair_energy(SIGMA)),
                 "minimum_energy_error": abs(model.pair_energy(MINIMUM) + EPSILON)},
        "forces": force_metrics(model),
        "nve": nve_metrics(model),
    }


def main(project_dir, output):
    # Resolve the verifier and its standard-library imports before loading the SUT.
    sys.path.append(str(Path(project_dir).resolve()))
    import lj_model

    metrics = collect_metrics(lj_model)
    Path(output).write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("Independent LJ metrics written")


if __name__ == "__main__":
    main(*sys.argv[1:])
