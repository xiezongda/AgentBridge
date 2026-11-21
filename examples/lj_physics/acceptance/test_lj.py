"""Independent checks of the specified two- and three-atom trajectories."""

import unittest

import lj_model
from verify_lj import EPSILON, MINIMUM, SIGMA, force_metrics, nve_metrics


class LennardJonesTests(unittest.TestCase):
    def test_zero_energy_at_sigma(self):
        self.assertAlmostEqual(lj_model.pair_energy(SIGMA), 0.0, delta=1e-12)

    def test_energy_at_minimum(self):
        self.assertAlmostEqual(lj_model.pair_energy(MINIMUM), -EPSILON, delta=1e-12)

    def test_zero_force_at_minimum(self):
        self.assertLessEqual(force_metrics(lj_model)["minimum_force_norm"], 1e-10)

    def test_multibody_force_matches_centered_energy_difference(self):
        metrics = force_metrics(lj_model)
        self.assertLessEqual(metrics["three_atom_energy_error"], 1e-12)
        self.assertLessEqual(metrics["max_centered_difference_error"], 1e-8)

    def test_third_law(self):
        self.assertLessEqual(force_metrics(lj_model)["net_force_norm"], 1e-12)

    def test_nve_energy_conservation(self):
        nve = nve_metrics(lj_model)
        self.assertLessEqual(nve["coarse_energy_excursion"], 1e-7)
        self.assertLessEqual(nve["fine_energy_excursion"], 3e-8)

    def test_nve_timestep_halving(self):
        self.assertAlmostEqual(nve_metrics(lj_model)["coarse_to_fine_ratio"], 4.0, delta=0.5)

    def test_invalid_inputs(self):
        for distance, epsilon, sigma in ((0, EPSILON, SIGMA), (-1, EPSILON, SIGMA),
                                         (SIGMA, -EPSILON, SIGMA), (SIGMA, EPSILON, -SIGMA)):
            with self.subTest(distance=distance, epsilon=epsilon, sigma=sigma), self.assertRaises(ValueError):
                lj_model.pair_energy(distance, epsilon, sigma)
        with self.assertRaises(ValueError):
            lj_model.energy_forces(((0, 0, 0), (0, 0, 0)))
        with self.assertRaises(ValueError):
            list(lj_model.nve_trajectory(((0, 0, 0),), ((0, 0, 0),), -1, 1))
        with self.assertRaises(ValueError):
            list(lj_model.nve_trajectory(((0, 0, 0),), ((0, 0, 0),), 1, 1, mass=-1))
