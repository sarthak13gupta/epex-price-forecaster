"""
Optuna hyperparameter search over the walk-forward folds.

The objective is the mean fold RMSE of the walk-forward backtest, so tuning
optimises the same quantity the leaderboard reports rather than a single
random split.
"""

from typing import Any, cast

import optuna
from sklearn.base import clone

from src.models.backtest import PreparedFold, ScikitLearnModel, evaluate_folds
from src.models.registry import ParamSpace


class StudyEarlyStoppingCallback:
    """
    Stops the study once the best score has not improved for
    `early_stopping_rounds` consecutive trials.
    """

    def __init__(self, early_stopping_rounds: int) -> None:
        self.early_stopping_rounds = early_stopping_rounds
        self._best_trial_number: int | None = None
        self._stagnant_trials = 0

    def __call__(
        self, study: optuna.study.Study, trial: optuna.trial.FrozenTrial
    ) -> None:
        if study.best_trial.number != self._best_trial_number:
            self._best_trial_number = study.best_trial.number
            self._stagnant_trials = 0
        else:
            self._stagnant_trials += 1

        if self._stagnant_trials >= self.early_stopping_rounds:
            print(
                f"[Early Stopping] No improvement in the last "
                f"{self.early_stopping_rounds} trials, stopping study."
            )
            study.stop()


def tune_model(
    folds: list[PreparedFold],
    base_estimator: ScikitLearnModel,
    param_space: ParamSpace,
    model_name: str,
    config: dict[str, Any],
) -> tuple[ScikitLearnModel, optuna.study.Study]:
    """
    Searches `param_space` and returns an unfitted estimator carrying the best
    parameters, alongside the completed study for logging.

    Folds are prepared once by the caller, so each trial only refits the price
    model — the exogenous cascade is not recomputed.
    """
    optuna_config = config["models"]["optuna"]
    n_trials = int(optuna_config["n_trials"])
    timeout = optuna_config.get("timeout_seconds")
    early_stopping_rounds = int(optuna_config["early_stopping_rounds"])
    random_state = int(config["models"]["random_state"])

    print(f"Tuning {model_name}: up to {n_trials} trials over {len(folds)} folds.")

    def objective(trial: optuna.trial.Trial) -> float:
        params = param_space(trial)
        candidate = cast(ScikitLearnModel, clone(cast(Any, base_estimator)))
        candidate.set_params(**params)

        metrics_df, _ = evaluate_folds(
            folds, candidate, model_name=f"{model_name}_trial", verbose=False
        )
        return float(metrics_df['rmse'].mean())

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction='minimize',
        sampler=optuna.samplers.TPESampler(seed=random_state),
        study_name=f"{model_name}_tuning",
    )

    study.optimize(
        objective,
        n_trials=n_trials,
        timeout=int(timeout) if timeout else None,
        callbacks=[StudyEarlyStoppingCallback(early_stopping_rounds)],
    )

    print(
        f"Tuning complete for {model_name}: "
        f"best mean fold RMSE = {study.best_value:.3f} EUR/MWh "
        f"after {len(study.trials)} trials."
    )
    for key, value in study.best_params.items():
        print(f"  {key} = {value}")

    best_estimator = cast(ScikitLearnModel, clone(cast(Any, base_estimator)))
    best_estimator.set_params(**study.best_params)

    return best_estimator, study
