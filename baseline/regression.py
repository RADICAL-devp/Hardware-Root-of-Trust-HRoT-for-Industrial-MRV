"""IPMVP-style baseline regression for energy savings estimation.

The baseline model relates energy consumption to operating variables (load, temperature)
using pre-retrofit data only. Post-retrofit data is used only for savings evaluation.

Model: E = β₀ + β₁·Load + β₂·Temp + β₃·Load² + ε

Where:
- E: energy per window [µWh]
- Load: shaft load torque [N·m]
- Temp: motor temperature [°C]

Leakage prevention:
- fit() only accepts pre-retrofit data
- predict() only uses fitted coefficients
- Explicit API prevents post-retrofit data from influencing fit
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split


@dataclass(frozen=True)
class BaselineParams:
    """Baseline regression configuration."""

    validation_fraction: float = 0.2  # fraction for validation
    bootstrap_samples: int = 1000  # bootstrap resamples for CI
    confidence_level: float = 0.95  # confidence level for CI
    random_state: int = 42  # seed for reproducibility


@dataclass
class BaselineRegression:
    """IPMVP-style baseline regression with leakage prevention."""

    params: BaselineParams

    # Fitted state (private, set by fit())
    _coefficients: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _intercept: Optional[float] = field(default=None, init=False, repr=False)
    _is_fitted: bool = field(default=False, init=False, repr=False)
    _feature_names: list = field(
        default_factory=lambda: ["intercept", "load", "temp", "load^2"], init=False, repr=False
    )

    # Validation metrics
    cv_rmse_: Optional[float] = field(default=None, init=False, repr=False)
    nmbe_: Optional[float] = field(default=None, init=False, repr=False)
    val_rmse_: Optional[float] = field(default=None, init=False, repr=False)
    val_mae_: Optional[float] = field(default=None, init=False, repr=False)
    _train_indices: Optional[np.ndarray] = field(default=None, init=False, repr=False)
    _val_indices: Optional[np.ndarray] = field(default=None, init=False, repr=False)

    @property
    def coefficients_(self) -> Optional[np.ndarray]:
        """Return coefficients [intercept, load, temp, load^2]."""
        if not self._is_fitted:
            return None
        return np.concatenate([[self._intercept], self._coefficients])

    def fit(
        self,
        load: np.ndarray,
        temp: np.ndarray,
        energy: np.ndarray,
    ) -> "BaselineRegression":
        """Fit baseline model on pre-retrofit data.

        Args:
            load: load torque [N·m], shape (n_samples,)
            temp: motor temperature [°C], shape (n_samples,)
            energy: measured energy per window [µWh], shape (n_samples,)

        Returns:
            self (for chaining)

        Raises:
            RuntimeError: if already fitted (prevents leakage)
        """
        if self._is_fitted:
            raise RuntimeError(
                "Baseline already fitted. Create a new instance for refitting. "
                "This prevents data leakage from post-retrofit observations."
            )

        load = np.asarray(load, dtype=float).ravel()
        temp = np.asarray(temp, dtype=float).ravel()
        energy = np.asarray(energy, dtype=float).ravel()

        if not (load.shape == temp.shape == energy.shape):
            raise ValueError("load, temp, energy must have same shape")

        n = load.shape[0]
        if n < 10:
            raise ValueError(f"Need at least 10 samples, got {n}")

        # Build feature matrix: [load, temp, load^2]
        X = np.column_stack([load, temp, load**2])
        y = energy

        # Train/validation split
        indices = np.arange(n)
        train_idx, val_idx = train_test_split(
            indices,
            test_size=self.params.validation_fraction,
            random_state=self.params.random_state,
        )
        self._train_indices = train_idx
        self._val_indices = val_idx

        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]

        # Fit OLS
        model = LinearRegression(fit_intercept=True)
        model.fit(X_train, y_train)

        self._coefficients = model.coef_
        self._intercept = model.intercept_
        self._is_fitted = True

        # Compute validation metrics on validation set
        y_pred_val = model.predict(X_val)
        self.val_rmse_ = float(np.sqrt(np.mean((y_val - y_pred_val) ** 2)))
        self.val_mae_ = float(np.mean(np.abs(y_val - y_pred_val)))

        # Compute IPMVP metrics on FULL baseline period (training + validation)
        y_pred_full = model.predict(X)
        rmse_full = np.sqrt(np.mean((y - y_pred_full) ** 2))
        mean_pred_full = np.mean(y_pred_full)
        self.cv_rmse_ = 100.0 * rmse_full / mean_pred_full if mean_pred_full != 0 else np.inf

        # NMBE = mean(baseline - measured) / mean(measured) * 100 (on full baseline period)
        self.nmbe_ = 100.0 * np.mean(y_pred_full - y) / np.mean(y) if np.mean(y) != 0 else np.inf

        return self

    def predict(
        self,
        load: np.ndarray,
        temp: np.ndarray,
    ) -> np.ndarray:
        """Predict energy for given operating conditions.

        Args:
            load: load torque [N·m]
            temp: motor temperature [°C]

        Returns:
            Predicted energy [µWh]

        Raises:
            RuntimeError: if not fitted
        """
        if not self._is_fitted:
            raise RuntimeError("Baseline not fitted. Call fit() first.")

        load = np.asarray(load, dtype=float).ravel()
        temp = np.asarray(temp, dtype=float).ravel()

        if load.shape != temp.shape:
            raise ValueError("load and temp must have same shape")

        X = np.column_stack([load, temp, load**2])
        return self._intercept + X @ self._coefficients

    def estimate_savings(
        self,
        post_load: np.ndarray,
        post_temp: np.ndarray,
        post_measured_energy: np.ndarray,
    ) -> tuple[float, float, float]:
        """Estimate energy savings with confidence interval.

        Args:
            post_load: post-retrofit load [N·m]
            post_temp: post-retrofit temperature [°C]
            post_measured_energy: measured post-retrofit energy [µWh] (NaN for missing)

        Returns:
            (savings_kwh, ci_lower_kwh, ci_upper_kwh)
        """
        if not self._is_fitted:
            raise RuntimeError("Baseline not fitted. Call fit() first.")

        post_load = np.asarray(post_load, dtype=float).ravel()
        post_temp = np.asarray(post_temp, dtype=float).ravel()
        post_measured = np.asarray(post_measured_energy, dtype=float).ravel()

        if not (post_load.shape == post_temp.shape == post_measured.shape):
            raise ValueError("post arrays must have same shape")

        # Baseline prediction for post-retrofit conditions
        baseline_pred = self.predict(post_load, post_temp)

        # Only compute savings for windows with valid measured data
        valid_mask = np.isfinite(post_measured)
        if not np.any(valid_mask):
            return 0.0, 0.0, 0.0

        baseline_valid = baseline_pred[valid_mask]
        measured_valid = post_measured[valid_mask]

        # Savings per window [µWh]
        savings_uwh = baseline_valid - measured_valid

        # Bootstrap confidence interval on TOTAL savings
        n = len(savings_uwh)
        rng = np.random.default_rng(self.params.random_state)
        bootstrap_savings = []
        for _ in range(self.params.bootstrap_samples):
            idx = rng.choice(n, n, replace=True)
            bootstrap_savings.append(np.sum(savings_uwh[idx]))

        bootstrap_savings = np.array(bootstrap_savings)
        alpha = (1 - self.params.confidence_level) / 2
        ci_lower = np.percentile(bootstrap_savings, 100 * alpha)
        ci_upper = np.percentile(bootstrap_savings, 100 * (1 - alpha))

        # Convert to kWh (1 kWh = 1e9 µWh)
        savings_kwh = np.sum(savings_uwh) / 1_000_000_000.0
        ci_lower_kwh = ci_lower / 1_000_000_000.0
        ci_upper_kwh = ci_upper / 1_000_000_000.0

        return float(savings_kwh), float(ci_lower_kwh), float(ci_upper_kwh)

    def get_validation_metrics(self) -> dict:
        """Return validation metrics as dictionary."""
        return {
            "cv_rmse_pct": self.cv_rmse_,
            "nmbe_pct": self.nmbe_,
            "val_rmse_uwh": self.val_rmse_,
            "val_mae_uwh": self.val_mae_,
            "n_train": len(self._train_indices) if self._train_indices is not None else 0,
            "n_val": len(self._val_indices) if self._val_indices is not None else 0,
        }
