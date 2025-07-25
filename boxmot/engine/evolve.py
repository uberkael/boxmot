#!/usr/bin/env python3
"""
This script runs a hyperparameter tuning process for a multi-object tracking (MOT) tracker using Ray Tune,
with support for resuming (restoring) previous tuning runs.
"""

import os
os.environ["RAY_CHDIR_TO_TRIAL_DIR"] = "0"   # keep CWD constant for all trials
from pathlib import Path

import yaml
from ray import tune
from ray.tune import RunConfig
from ray.tune.search.optuna import OptunaSearch

from boxmot.utils import NUM_THREADS, TRACKER_CONFIGS
from boxmot.engine.val import (
    eval_init,
    load_dataset_cfg,
    run_generate_dets_embs,
    run_generate_mot_results,
    run_trackeval,
)


def load_yaml_config(tracking_method: str) -> dict:
    """
    Loads the YAML configuration file for the given tracking method.
    """
    config_path = TRACKER_CONFIGS / f"{tracking_method}.yaml"
    with open(config_path, "r") as file:
        return yaml.safe_load(file)


def yaml_to_search_space(config: dict) -> dict:
    """
    Converts a YAML configuration dictionary to a Ray Tune search space.
    """
    space = {}
    for param, details in config.items():
        t = details.get("type")
        if t == "uniform":
            space[param] = tune.uniform(*details["range"])
        elif t == "randint":
            space[param] = tune.randint(*details["range"])
        elif t == "qrandint":
            space[param] = tune.qrandint(*details["range"])
        elif t == "choice":
            space[param] = tune.choice(details["options"])
        elif t == "grid_search":
            space[param] = tune.grid_search(details["values"])
        elif t == "loguniform":
            space[param] = tune.loguniform(*details["range"])
    return space


class Tracker:
    """
    Encapsulates the evaluation of a tracking configuration.
    """
    def __init__(self, opt):
        self.opt = opt

    def objective_function(self, config: dict) -> dict:
        # Generate MOT-compliant results
        run_generate_mot_results(self.opt, config)
        # Evaluate and extract objectives
        results = run_trackeval(self.opt)
        return {k: results.get(k) for k in self.opt.objectives}


def main(args):
    # --- initial setup ---
    args.yolo_model = [Path(y).resolve() for y in args.yolo_model]
    args.reid_model = [Path(r).resolve() for r in args.reid_model]

    # Load search space
    yaml_cfg = load_yaml_config(args.tracking_method)
    search_space = yaml_to_search_space(yaml_cfg)
    tracker = Tracker(args)

    # Optuna search setup
    primary_metric = args.objectives[0]
    optuna_search = OptunaSearch(metric=primary_metric, mode="max")

    def tune_wrapper(cfg):
        return tracker.objective_function(cfg)

    # Paths for storage and restore
    tune_name = f"{args.tracking_method}_tune"
    results_dir = args.project / "ray"
    restore_path = results_dir / tune_name

    # Define trainable
    trainable = tune.with_resources(tune_wrapper, {"cpu": NUM_THREADS, "gpu": 0})

    # Ensure evaluation tools are available
    eval_init(args)
    run_generate_dets_embs(args)

    # Check for existing run to resume
    if tune.Tuner.can_restore(restore_path):
        print(f"Resuming tuning from {restore_path}...")
        tuner = tune.Tuner.restore(
            restore_path,
            trainable=trainable,
            resume_errored=True,
        )
    else:
        tuner = tune.Tuner(
            trainable,
            param_space=search_space,
            tune_config=tune.TuneConfig(
                num_samples=args.n_trials,
                search_alg=optuna_search,
            ),
            run_config=RunConfig(
                storage_path=results_dir,
                name=tune_name,
            ),
        )

    # Run or resume
    tuner.fit()
    print(tuner.get_results())


if __name__ == "__main__":
    main()
