"""Pair interactions in eV, Angstrom, amu, and fs."""

import math

EPSILON_EV = 0.01032
SIGMA_ANGSTROM = 3.405
MASS_AMU = 39.948
ACCELERATION_FACTOR = 0.009648533215665327


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def pair_energy(distance, epsilon=EPSILON_EV, sigma=SIGMA_ANGSTROM):
    _positive(distance, "distance")
    _positive(epsilon, "epsilon")
    _positive(sigma, "sigma")
    sr6 = (sigma / distance) ** 6
    return 4 * epsilon * (sr6 * sr6 - sr6)


def _vectors(points):
    vectors = [tuple(vector) for vector in points]
    if not vectors or any(len(vector) != 3 or not all(map(math.isfinite, vector)) for vector in vectors):
        raise ValueError("expected nonempty finite 3D vectors")
    return vectors


def energy_forces(positions, epsilon=EPSILON_EV, sigma=SIGMA_ANGSTROM):
    """Return total potential (eV) and per-atom forces (eV/Angstrom)."""
    positions = _vectors(positions)
    _positive(epsilon, "epsilon")
    _positive(sigma, "sigma")
    energy = 0.0
    forces = [[0.0, 0.0, 0.0] for _ in positions]
    for i, pos in enumerate(positions):
        for j in range(i):
            delta = [pos[k] - positions[j][k] for k in range(3)]
            r2 = sum(component * component for component in delta)
            _positive(r2, "squared separation")
            sr6 = (sigma * sigma / r2) ** 3
            sr12 = sr6 * sr6
            energy += 4 * epsilon * (sr12 - sr6)
            coefficient = 24 * epsilon * (sr12 - sr6) / r2
            for k, displacement in enumerate(delta):
                pair_force = coefficient * displacement
                forces[i][k] += pair_force
                forces[j][k] -= pair_force
    return energy, tuple(map(tuple, forces))


def nve_trajectory(positions, velocities, dt, steps, epsilon=EPSILON_EV, sigma=SIGMA_ANGSTROM,
                   mass=MASS_AMU):
    """Yield initial and full-step (positions, velocities) states for an isolated system."""
    _positive(dt, "time step")
    _positive(mass, "mass")
    if type(steps) is not int or steps < 0:
        raise ValueError("steps must be a nonnegative integer")
    positions, velocities = _vectors(positions), _vectors(velocities)
    if len(positions) != len(velocities):
        raise ValueError("positions and velocities must have the same length")
    kick = 0.5 * dt * ACCELERATION_FACTOR / mass
    _, forces = energy_forces(positions, epsilon, sigma)
    yield tuple(positions), tuple(velocities)
    for _ in range(steps):
        half_step = [tuple(v + kick * f for v, f in zip(velocity, force))
                     for velocity, force in zip(velocities, forces)]
        positions = [tuple(x + dt * v for x, v in zip(position, velocity))
                     for position, velocity in zip(positions, half_step)]
        _, new_forces = energy_forces(positions, epsilon, sigma)
        velocities = [tuple(v + kick * f for v, f in zip(velocity, force))
                      for velocity, force in zip(half_step, forces)]
        forces = new_forces
        yield tuple(positions), tuple(velocities)
