import ray
import copy
import torch
import numpy as np
import pandas as pd
import os
import sys
import math

from ray import tune

from ray.tune.suggest.hyperopt import HyperOptSearch
from ray.tune import Analysis
from hyperopt import hp
from datetime import datetime

import argparse
import gpytorch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
HPARAM_EXP_DIR = os.path.join(DATA_DIR, "tune-hparam-ntasks")

SEED = 28
N_THREADS_PER_RUN = 1
TEST_SEEDS = [28, 29, 30, 31, 32]

# configuration for prior learning


def main(args):
    ray.init(
        num_cpus=args.num_cpus,
        memory=3000 * 1024**2,
        object_store_memory=300 * 1024**2,
    )

    def train_reg(config, reporter):
        sys.path.append(BASE_DIR)

        # 1) load / generate data
        from experiments.data_sim import provide_data

        data_train, data_valid, _ = provide_data(dataset=args.dataset, seed=SEED)

        # 2) setup model
        from meta_learn.GPR_meta_vi import GPRegressionMetaLearnedVI

        torch.set_num_threads(N_THREADS_PER_RUN)

        model = GPRegressionMetaLearnedVI(data_train, **config)

        # 3) train and evaluate model
        with gpytorch.settings.max_cg_iterations(300):
            eval_period = 3000
            train_iter = 0
            for i in range(config["num_iter_fit"] // eval_period):
                loss = model.meta_fit(
                    verbose=False, log_period=2000, n_iter=eval_period
                )
                train_iter += eval_period
                ll, rmse, calib_err = model.eval_datasets(data_valid)
                reporter(
                    timesteps_total=train_iter,
                    loss=loss,
                    test_rmse=rmse,
                    test_ll=ll,
                    calib_err=calib_err,
                )

    @ray.remote
    def train_test(config):
        results_dict = config

        try:
            sys.path.append(BASE_DIR)

            # 1) load / generate data
            from experiments.data_sim import provide_data

            data_train, _, data_test = provide_data(dataset=args.dataset, seed=SEED)

            # 2) Fit model
            from meta_learn.GPR_meta_vi import GPRegressionMetaLearnedVI

            torch.set_num_threads(N_THREADS_PER_RUN)
            with gpytorch.settings.max_cg_iterations(500):
                model = GPRegressionMetaLearnedVI(data_train, **config)
                model.meta_fit(data_test, log_period=5000)

                # 3) evaluate on test set
                ll, rmse, calib_err = model.eval_datasets(data_test)

            results_dict.update(ll=ll, rmse=rmse, calib_err=calib_err)

        except Exception as e:
            print(e)
            results_dict.update(ll=np.nan, rmse=np.nan, calib_err=np.nan)

        return results_dict

    assert args.metric in ["test_ll", "test_rmse"]

    exp_name = "tune_meta_vi_%s_kernel_%s" % (args.covar_module, args.dataset)

    if args.load_analysis:
        analysis_dir = os.path.join(HPARAM_EXP_DIR, exp_name)
        assert os.path.isdir(
            analysis_dir
        ), "load_analysis_from must be a valid directory"
        print("Loading existing tune analysis results from %s" % analysis_dir)
        analysis = Analysis(analysis_dir)
    else:
        space = {
            "task_kl_weight": hp.loguniform(
                "task_kl_weight", math.log(5e-2), math.log(1.0)
            ),
            "prior_factor": hp.loguniform(
                "prior_factor", math.log(1e-6), math.log(2e-1)
            ),
            "lr": hp.loguniform("lr", math.log(5e-4), math.log(5e-3)),
            "lr_decay": hp.loguniform("lr_decay", math.log(0.8), math.log(1.0)),
            "svi_batch_size": hp.choice("svi_batch_size", [10, 50]),
            "task_batch_size": hp.choice("task_batch_size", [4, 10]),
        }

        config = {
            "num_samples": 200,
            "config": {
                "num_iter_fit": 30000,
                "kernel_nn_layers": [32, 32, 32, 32],
                "mean_nn_layers": [32, 32, 32, 32],
                "random_seed": SEED,
                "mean_module": "NN",
                "covar_module": args.covar_module,
                "normalize_data": True,
                "cov_type": "diag",
            },
            "stop": {"timesteps_total": 30000},
        }

        # Run hyper-parameter search

        algo = HyperOptSearch(
            space,
            max_concurrent=args.num_cpus,
            metric=args.metric,
            mode="max" if args.metric == "test_ll" else "min",
        )

        analysis = tune.run(
            train_reg,
            name=exp_name,
            search_alg=algo,
            verbose=1,
            raise_on_failed_trial=False,
            local_dir=HPARAM_EXP_DIR,
            **config
        )

    # Select N best configurations re-run train & test with 5 different seeds

    from experiments.hyperparam_search.util import select_best_configs

    if args.metric == "test_ll":
        best_configs = select_best_configs(
            analysis, metric="test_ll", mode="max", N=args.n_test_runs
        )
    elif args.metric == "test_rmse":
        best_configs = select_best_configs(
            analysis, metric="test_rmse", mode="min", N=args.n_test_runs
        )
    else:
        raise AssertionError("metric must be test_ll or test_rmse")

    test_configs = []
    for config in best_configs:
        for seed in TEST_SEEDS:
            test_config = copy.deepcopy(config)
            test_config.update({"random_seed": seed})
            test_configs.append(test_config)

    result_dicts = ray.get([train_test.remote(config) for config in test_configs])

    result_df = pd.DataFrame(result_dicts)
    print(result_df.to_string())

    csv_file_name = os.path.join(
        HPARAM_EXP_DIR,
        "%s_%s.csv" % (exp_name, datetime.now().strftime("%b_%d_%Y_%H:%M:%S")),
    )
    result_df.to_csv(csv_file_name)
    print("\nSaved result csv to %s" % csv_file_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run meta mll hyper-parameter search.")
    parser.add_argument(
        "--covar_module", type=str, default="NN", help="type of kernel function"
    )
    parser.add_argument("--dataset", type=str, default="sin", help="dataset")
    parser.add_argument("--num_cpus", type=int, default=64, help="dataset")
    parser.add_argument(
        "--load_analysis",
        type=bool,
        default=False,
        help="whether to load the analysis from existing results",
    )
    parser.add_argument(
        "--metric", type=str, default="test_ll", help="test metric to optimize"
    )
    parser.add_argument(
        "--n_test_runs", type=int, default=5, help="number of test runs"
    )

    args = parser.parse_args()

    print("Running", os.path.abspath(__file__), "\n")
    print("--- Experiment Settings ---")
    print("Covar Module:", args.covar_module)
    print("Dataset:", args.dataset)
    print("num cpus:", args.num_cpus)
    print("\n")

    main(args)
