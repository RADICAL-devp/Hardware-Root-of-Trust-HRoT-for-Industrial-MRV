"""CO2 emissions estimation via grid emission factor curve.

Energy savings [kWh] × emission factor [kg CO2/kWh] = avoided emissions [kg CO2].

The emission factor provider is replaceable for regional/time-dependent data.
Currently uses a synthetic hourly profile for simulation.
"""

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


@dataclass(frozen=True)
class EmissionsParams:
    """Emissions estimation configuration."""

    # Synthetic emission factor: hourly profile [kg CO2/kWh]
    # ef(hour) = base + daily_amplitude * sin(2π * hour/24) + seasonal_amplitude * sin(2π * day/365)
    base_ef_kg_per_kwh: float = 0.4
    daily_amplitude: float = 0.1
    seasonal_amplitude: float = 0.05
    # Custom emission factor function (hour_of_year -> kg CO2/kWh)
    custom_ef_func: Optional[Callable[[int], float]] = None


class EmissionsEstimator:
    """Estimate avoided CO2 emissions from energy savings."""

    def __init__(self, params: EmissionsParams):
        self.params = params

    def get_emission_factor(self, hour_of_year: int) -> float:
        """Get grid emission factor for a given hour of year.

        Args:
            hour_of_year: hour index [0, 8759] (0 = Jan 1, 00:00)

        Returns:
            Emission factor [kg CO2/kWh]
        """
        if self.params.custom_ef_func is not None:
            return self.params.custom_ef_func(hour_of_year)

        hour = hour_of_year % 24
        day = hour_of_year // 24

        daily = self.params.daily_amplitude * np.sin(2 * np.pi * hour / 24)
        seasonal = self.params.seasonal_amplitude * np.sin(2 * np.pi * day / 365)

        return self.params.base_ef_kg_per_kwh + daily + seasonal

    def uwh_to_kwh(self, energy_uwh: float) -> float:
        """Convert micro-watt-hours to kilowatt-hours.

        1 kWh = 1,000 Wh = 1,000,000,000 µWh (1e9)
        """
        return energy_uwh / 1_000_000_000.0

    def estimate_avoided_emissions(
        self,
        energy_saved_kwh: float,
        hour_of_year: Optional[int] = None,
    ) -> float:
        """Estimate avoided CO2 emissions for given energy savings.

        Args:
            energy_saved_kwh: energy savings [kWh] (negative → 0 emissions)
            hour_of_year: optional hour for time-varying emission factor

        Returns:
            Avoided emissions [kg CO2]
        """
        if energy_saved_kwh <= 0:
            return 0.0

        if hour_of_year is not None:
            ef = self.get_emission_factor(hour_of_year)
        else:
            ef = self.params.base_ef_kg_per_kwh

        return energy_saved_kwh * ef

    def aggregate_emissions(
        self,
        savings_per_window_kwh: list[float],
        hours_of_year: Optional[list[int]] = None,
    ) -> float:
        """Aggregate avoided emissions over multiple windows.

        Args:
            savings_per_window_kwh: list of energy savings per window [kWh]
            hours_of_year: optional hour of year for each window

        Returns:
            Total avoided emissions [kg CO2]
        """
        if hours_of_year is None:
            return sum(self.estimate_avoided_emissions(s) for s in savings_per_window_kwh)

        if len(savings_per_window_kwh) != len(hours_of_year):
            raise ValueError("savings and hours must have same length")

        total = 0.0
        for s, h in zip(savings_per_window_kwh, hours_of_year):
            total += self.estimate_avoided_emissions(s, h)
        return total

    def estimate_emissions_from_windows(
        self,
        baseline_energy_uwh: np.ndarray,
        measured_energy_uwh: np.ndarray,
        window_start_hours: np.ndarray,
    ) -> tuple[float, np.ndarray]:
        """Estimate emissions from baseline and measured energy per window.

        Args:
            baseline_energy_uwh: baseline predicted energy per window [µWh]
            measured_energy_uwh: measured post-retrofit energy per window [µWh]
            window_start_hours: hour of year for each window start

        Returns:
            (total_avoided_kg, per_window_avoided_kg)
        """
        if not (baseline_energy_uwh.shape == measured_energy_uwh.shape == window_start_hours.shape):
            raise ValueError("All arrays must have same shape")

        savings_uwh = baseline_energy_uwh - measured_energy_uwh
        savings_kwh = savings_uwh / 1_000_000.0

        per_window = np.zeros_like(savings_kwh)
        for i, (s, h) in enumerate(zip(savings_kwh, window_start_hours)):
            per_window[i] = self.estimate_avoided_emissions(s, int(h))

        total = np.sum(per_window)
        return float(total), per_window
