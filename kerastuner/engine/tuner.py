"Meta classs for hypertuner"
from __future__ import absolute_import, division, print_function

import gc
import hashlib
import json
import os
import socket
import sys
import time
from abc import abstractmethod
from collections import defaultdict
from pathlib import Path

import tensorflow as tf
import tensorflow.keras.backend as K  # pylint: disable=import-error
from termcolor import cprint

from kerastuner import config
from kerastuner.states import TunerState
from kerastuner.distributions import DummyDistributions
from kerastuner.abstractions.io import create_directory, glob, read_file
from kerastuner.abstractions.io import save_model, reload_model
from kerastuner.abstractions.io import read_results, deserialize_loss
from kerastuner.abstractions.display import highlight, print_table, section
from kerastuner.abstractions.display import setting, subsection
from kerastuner.abstractions.display import info, warning, fatal, set_log
from kerastuner.abstractions.display import get_progress_bar
from kerastuner.abstractions.display import colorize, colorize_default
from kerastuner.tools.summary import summary as result_summary
from .cloudservice import CloudService
from .instance import Instance


class Tuner(object):
    """Abstract hypertuner class."""

    def __init__(self, model_fn, objective, name, distributions, **kwargs):
        """ Tuner abstract class

        Args:
            model_fn (function): Function that return a Keras model
            name (str): name of the tuner
            objective (Objective): Which objective the tuner optimize for
            distributions (Distributions): distributions object

        Notes:
            All meta data and varialbles are stored into self.state
            defined in ../states/tunerstate.py
        """

        # hypertuner state init
        self.state = TunerState(name, objective, **kwargs)
        self.cloudservice = CloudService()

        # validate the model and record its hparam
        self._check_and_store_model_fn(model_fn)

        # set global distribution object to the one requested by tuner
        # !MUST be after _eval_model_fn()
        config._DISTRIBUTIONS = distributions

        # instances management
        self.max_fail_streak = 5  # how many failure before giving up
        self.instances = {}  # Instances state we trained
        self.current_instance_idx = -1  # idx of the last instance trained

        # metrics
        self.METRIC_NAME = 0
        self.METRIC_DIRECTION = 1
        self.max_acc = -1
        self.min_loss = sys.maxsize
        self.max_val_acc = -1
        self.min_val_loss = sys.maxsize

        # including user metrics
        user_metrics = kwargs.get('metrics')
        if user_metrics:
            self.key_metrics = []
            for tm in user_metrics:
                if not isinstance(tm, tuple):
                    cprint(
                        "[Error] Invalid metric format: %s (%s) - metric format is (metric_name, direction) e.g ('val_acc', 'max') - Ignoring" % (tm, type(tm)), 'red')
                    continue
                if tm[self.METRIC_DIRECTION] not in ['min', 'max']:
                    cprint(
                        "[Error] Invalid metric direction for: %s - metric format is (metric_name, direction). direction is min or max - Ignoring" % tm, 'red')
                    continue
                self.key_metrics.append(tm)
        else:
            # sensible default
            self.key_metrics = [('loss', 'min'), ('val_loss', 'min'),
                                ('acc', 'max'), ('val_acc', 'max')]

        # initializing stats
        self.stats = {
            'best': {},
            'latest': {},
            'direction': {}
        }
        for km in self.key_metrics:
            self.stats['direction'][km[self.METRIC_NAME]
                                    ] = km[self.METRIC_DIRECTION]
            if km[self.METRIC_DIRECTION] == 'min':
                self.stats['best'][km[self.METRIC_NAME]] = sys.maxsize
            else:
                self.stats['best'][km[self.METRIC_NAME]] = -1
        self.meta_data['statistics'] = self.stats


        # Load existing instances.
        self._load_previously_trained_instances(**kwargs)

        # Set the log
        log_name = "%s_%s_%d.log" % (self.meta_data["project"],
                                     self.meta_data["architecture"],
                                     int(time.time()))
        log_file = os.path.join(
            self.meta_data['server']['local_dir'], log_name)
        set_log(log_file)

        # make sure TF session is configured correctly
        cfg = tf.ConfigProto()
        cfg.gpu_options.allow_growth = True
        K.set_session(tf.Session(config=cfg))

        # FIXME: output metadata (move from backend call)

        # recap
        section("Key parameters")
        available_gpu = self.meta_data['server']['available_gpu']
        setting("GPUs Used: %d / %d" % (self.num_gpu, available_gpu), idx=0)
        setting("Model max params: %.1fM" %
                (self.max_params / 1000000.0), idx=1)
        setting("Saving results in %s" % self.meta_data['server']['local_dir'],
                idx=2)

    def _load_previously_trained_instances(self, **kwargs):
        "Checking for existing models"
        result_path = Path(kwargs.get('local_dir', 'results/'))
        filenames = list(result_path.glob('*-results.json'))
        for filename in get_progress_bar(filenames, unit='model', desc='Finding previously trained models'):
            data = json.loads(open(str(filename)).read())

            # Narrow down to matching project and architecture
            if (data['meta_data']['architecture'] == self.meta_data['architecture']
                    and data['meta_data']['project'] == self.meta_data['project']):
                # storing previous instance results in memory in case the tuner needs them.
                self.previous_instances[data['meta_data']['instance']] = data

    def _check_and_store_model_fn(self, model_fn):
        """
        Check and store model_function, hyperparams and metric info

        Args:
            model_fn (function): user supplied funciton that return a model
        """

        # test and store model_fn
        model_fn()
        self.model_fn = model_fn

        # record hparams
        hp = config._DISTRIBUTIONS.get_hyperparameters_config()
        self.state.hyper_parameters = hp
        if len(self.state.hyper_parameters) == 0:
            warning("No hyperparameters used in model function. Are you sure?")

    def summary(self):
        "Print a summary of the hyperparams search"
        section("Hyper-parmeters search space")

        # Compute the size of the hyperparam space by generating a model
        total_size = 1
        data_by_group = defaultdict(dict)
        group_size = defaultdict(lambda: 1)
        for data in self.hyperparameters_config.values():
            data_by_group[data['group']][data['name']] = data['space_size']
            group_size[data['group']] *= data['space_size']
            total_size *= data['space_size']

        # Generate the table.
        rows = [['param', 'space size']]
        for idx, grp in enumerate(sorted(data_by_group.keys())):
            if idx % 2:
                color = 'blue'
            else:
                color = 'default'

            rows.append([colorize(grp, color), ''])
            for param, size in data_by_group[grp].items():
                rows.append([colorize("|-%s" % param, color),
                             colorize(size, color)])

        rows.append(['', ''])
        rows.append([colorize('total', 'magenta'),
                     colorize(total_size, 'magenta')])
        print_table(rows)

    def enable_cloud(self, api_key, **kwargs):
        """Enable cloud service reporting

            Args:
                api_key (str): The backend API access token.

            Note:
                this is called by the user
        """
        self.cloudservice.enable(api_key)

    def search(self, x, y, **kwargs):
        self.keras_function = 'fit'
        kwargs["verbose"] = 0
        self.tune(x, y, **kwargs)
        if self.cloudservice.is_enable:
            self.cloudservice.complete()

    def search_generator(self, x, **kwargs):
        self.keras_function = 'fit_generator'
        kwargs["verbose"] = 0
        y = None  # fit_generator don't use this so we put none to be able to have a single hypertune function
        self.tune(x, y, **kwargs)
        if self.cloudservice.is_enable:
            self.cloudservice.complete()

    def _clear_tf_graph(self):
        """ Clear the content of the TF graph to ensure
            we have a valid model is in memory
        """
        K.clear_session()
        gc.collect()

    def new_instance(self):
        "Return a never seen before model instance"
        fail_streak = 0
        collision_streak = 0
        over_sized_streak = 0

        while 1:
            # clean-up TF graph from previously stored (defunct) graph
            self._clear_tf_graph()
            self.num_generated_models += 1
            fail_streak += 1
            try:
                model = self.model_fn()
            except:
                if self.debug:
                    import traceback
                    traceback.print_exc()

                self.num_invalid_models += 1
                warning("invalid model %s/%s" %
                        (self.num_invalid_models,
                         self.max_fail_streak))

                if self.num_invalid_models >= self.max_fail_streak:
                    return None
                continue

            # stop if the model_fn() return nothing
            if not model:
                warning("No model returned from model_fn - stopping.")
                return None

            idx = self.__compute_model_id(model)

            if idx in self.previous_instances:
                info("model %s already trained -- skipping" % idx)
                self.num_mdl_previously_trained += 1
                continue

            if idx in self.instances:
                collision_streak += 1
                self.num_collisions += 1
                self.meta_data['tuner']['collisions'] = self.num_collisions
                warning("Collision for %s -- skipping" % (idx))
                if collision_streak >= self.max_fail_streak:
                    return None
                continue
            hparams = config.DISTRIBUTIONS.get_current_hyperparameters()
            self.current_hyperparameters = hparams
            self._update_metadata()
            instance = Instance(idx, model, hparams, self.meta_data, self.num_gpu, self.batch_size,
                                self.display_model, self.key_metrics, self.keras_function, self.checkpoint,
                                self.callback_fn, self.backend)
            num_params = instance.compute_model_size()
            if num_params > self.max_params:
                over_sized_streak += 1
                self.num_over_sized_models += 1
                warning(
                    "Oversized model: %s parameters-- skipping" % (num_params))
                if over_sized_streak >= self.max_fail_streak:
                    return None
                continue

            break

        self.instances[idx] = instance
        self.current_instance_idx = idx

        section("New Instance")
        setting("Remaining Budget: %d" % self.remaining_budget, idx=0)
        setting("Num Instances Trained: %d" % self.num_generated_models, idx=1)
        setting("Model size: %d" % num_params, idx=2)

        subsection("Instance Hyperparameters")
        table = [["Hyperparameter", "Value"]]
        for k, v in self.current_hyperparameters.items():
            table.append([k, v["value"]])
        print_table(table, indent=2)

        return self.instances[idx]

    def record_results(self, idx=None):
        """Record instance results
        Args:
          idx (str): index of the instance. (default last trained)
        """

        if not idx:
            instance = self.instances[self.current_instance_idx]
        else:
            instance = self.instances[idx]

        results = instance.record_results()

        # compute overall statistics
        latest_results = {}
        best_results = {}
        for km in self.key_metrics:
            metric_name = km[self.METRIC_NAME]
            if metric_name in results['key_metrics']:
                current_best = self.stats['best'][metric_name]
                res_val = results['key_metrics'][metric_name]
                latest_results[metric_name] = res_val
                if km[self.METRIC_DIRECTION] == 'min':
                    best_results[metric_name] = min(current_best, res_val)
                else:
                    best_results[metric_name] = max(current_best, res_val)

        # updating
        self.stats['best'] = best_results
        self.stats['latest'] = latest_results
        self.meta_data['statistics'] = self.stats

    def get_best_model(self, **kwargs):
        resultset, models = self.get_best_model(num_models=1, **kwargs)
        return models[0], ResultSet(resultset.results[0])

    def get_best_models(
            self, metric="loss", direction='min', num_models=1, compile=False):
        # Glob/read the results metadata.
        results_dir = self.meta_data["server"]["local_dir"]

        result_set = read_results(results_dir).sorted_by_metric(
            metric, direction).limit(num_models).results

        models = []

        for result in result_set:
            config_file = os.path.join(results_dir, result["config_file"])
            weights_file = os.path.join(results_dir, result["weights_file"])
            results_file = os.path.join(results_dir, result["results_file"])

            model = reload_model(config_file, weights_file,
                                 results_file, compile=compile)
            models.append(model)
        return models, result_set

    def export_best_model(self, **kwargs):
        return self.export_best_models(num_models=1, **kwargs)

    def export_best_models(
            self, metric="loss",
            direction='min',
            output_type="keras",
            num_models=1):
        """ Exports the best model based on the specified metric, to the
            results directory.

            Args:
                metric (str, optional): Defaults to "loss". The metric used to
                     determine the best model.
                direction (str, optional): Defaults to 'min'. The sort
                    direction for the metric:
                        'min' - for losses, and other metrics where smaller is
                        better.
                        'max' - for accuracy, and other metrics where
                        larger is better.
                output_type (str, optional): Defaults to "keras". What format
                    of model to export:

                    "keras" - Save as separate config (JSON) and weights (HDF5)
                        files.
                    "keras_bundle" - Saved in Keras's native format (HDF5), via
                        save_model()
                    "tf" - Saved in tensorflow's SavedModel format. See:
                        https://www.tensorflow.org/alpha/guide/saved_model
                    "tf_frozen" - A SavedModel, where the weights are stored
                        in the model file itself, rather than a variables
                        directory. See:
                        https://www.tensorflow.org/guide/extend/model_files
                    "tf_optimized" - A frozen SavedModel, which has
                        additionally been transformed via tensorflow's graph
                        transform library to remove training-specific nodes
                        and operations.  See:
                        https://github.com/tensorflow/tensorflow/tree/master/tensorflow/tools/graph_transforms
                    "tf_lite" - A TF Lite model.
        """
        models, results = self.get_best_models(
            metric=metric,
            direction=direction,
            num_models=num_models,
            compile=False)
        for idx, (model, result) in enumerate(zip(models, results)):
            name = result["execution_prefix"]
            export_path = os.path.join(
                self.meta_data["server"]["export_dir"],
                name)
            tmp_path = os.path.join(
                self.meta_data["server"]["tmp_dir"],
                name)
            info("Exporting top model (%d/%d) - %s" % (idx + 1, len(models), export_path))
            save_model(model, export_path, tmp_path=tmp_path,
                       output_type=output_type)

    def done(self):
        info("Hypertuning complete - results in %s" %
             self.meta_data['server']['local_dir'])

    def get_model_by_id(self, idx):
        return self.instances.get(idx, None)

    def __compute_model_id(self, model):
        "compute model hash"
        s = str(model.get_config())
        return hashlib.sha256(s.encode('utf-8')).hexdigest()[:32]

    def _update_metadata(self):
        "update metadata with latest hypertuner state"

        md = self.meta_data['tuner']
        md['remaining_budget'] = self.remaining_budget
        # stats are updated at instance selection not training end
        md['trained_models'] = self.num_generated_models
        md['collisions'] = self.num_collisions
        md['invalid_models'] = self.num_invalid_models
        md['over_size_models'] = self.num_over_sized_models

    def display_result_summary(self, metric='loss', direction='min'):
        result_summary(
            self.meta_data["server"]["local_dir"],
            self.meta_data["project"],
            metric,
            direction=direction
        )

    @abstractmethod
    def tune(self, x, y, **kwargs):
        "method called by the hypertuner to train an instance"