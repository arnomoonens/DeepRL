# -*- coding: utf8 -*-

import itertools
import sys
import argparse
import json
import os
from os import path
import random
import subprocess
from typing import Any, Callable, List, Sequence, Union
import pkg_resources
import tensorflow as tf
from scipy import signal
import numpy as np

import gym
from gym.spaces import Discrete, Box, MultiBinary, MultiDiscrete

def discount_rewards(x: Sequence, gamma: float) -> np.ndarray:
    """
    Given vector x, computes a vector y such that
    y[i] = x[i] + gamma * x[i+1] + gamma^2 x[i+2] + ...
    """
    return signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

# Source: http://stackoverflow.com/a/12201744/1735784
def rgb2gray(rgb: np.ndarray) -> np.ndarray:
    """
    Convert an RGB image to a grayscale image.
    Uses the formula Y' = 0.299*R + 0.587*G + 0.114*B
    """
    return np.dot(rgb[..., :3], [0.299, 0.587, 0.114])

def _process_frame42(frame: np.ndarray) -> np.ndarray:
    import cv2
    frame = frame[34:34 + 160, :160]
    # Resize by half, then down to 42x42 (essentially mipmapping). If
    # we resize directly we lose pixels that, when mapped to 42x42,
    # aren't close enough to the pixel boundary.
    frame = cv2.resize(frame, (80, 80))
    frame = cv2.resize(frame, (42, 42))
    frame = frame.mean(2)
    frame = frame.astype(np.float32)
    frame *= (1.0 / 255.0)
    frame = np.reshape(frame, [42, 42, 1])
    return frame

class AtariRescale42x42(gym.ObservationWrapper):
    def __init__(self, env=None):
        super(AtariRescale42x42, self).__init__(env)
        self.observation_space = Box(0.0, 1.0, [42, 42, 1])

    def observation(self, observation: np.ndarray) -> np.ndarray:
        return _process_frame42(observation)


def preprocess_image(img: np.ndarray) -> np.ndarray:
    """
    Preprocess an image by converting it to grayscale and dividing its values by 256
    """
    img = img[35:195]  # crop
    img = img[::2, ::2]  # downsample by factor of 2
    return (rgb2gray(img) / 256.0)[:, :, None]

def execute_command(cmd: str) -> str:
    """Execute a terminal command and return the stdout."""
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    stdout, _ = p.communicate()
    return stdout.decode()[:-1] # decode to go from bytes to str, [:-1] to remove newline at end

def save_config(directory: str, config: dict, envs: list, repo_path: str = path.join(path.dirname(path.realpath(__file__)), "..")) -> None:
    """Save the configuration of an agent to a file."""
    filtered_config = {k: v for k, v in config.items() if not k.startswith("env")}
    filtered_config["envs"] = envs
    # Save git information if possible
    git_dir = os.path.join(repo_path, ".git")
    try:
        git = {
            "head": execute_command(f"git --git-dir='{git_dir}' branch | grep \* | cut -d ' ' -f2"),
            "commit": execute_command(f"git --git-dir='{git_dir}' rev-parse HEAD"),
            "message": execute_command(f"git --git-dir='{git_dir}' log -1 --pretty=%B")[:-1],
            "diff": execute_command(f"git --git-dir='{git_dir}' diff --no-prefix")
        }
        filtered_config["git"] = git
    except ImportError:
        pass
    # save pip freeze output
    pipfreeze = execute_command(f"{sys.executable} -m pip freeze")
    filtered_config["packages"] = pipfreeze.split("\n")
    with open(path.join(directory, "config.json"), "w") as outfile:
        json.dump(filtered_config, outfile, indent=4)

def json_to_dict(filename: str) -> dict:
    """Load a json file as a dictionary."""
    with open(filename) as f:
        return json.load(f)

def ge(minimum: int) -> Callable[[Any], int]:
    """Require the value for an argparse argument to be an integer >= minimum."""
    def f(value):
        ivalue = int(value)
        if ivalue < minimum:
            raise argparse.ArgumentTypeError("{} must be an integer of at least 1.".format(value))
        return ivalue
    return f

def flatten(x):
    return tf.reshape(x, [-1, np.prod(x.get_shape().as_list()[1:])])

def set_seed(seed: int):
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    tf.random.set_seed(seed)

def load(name: str):
    """Load an object by string."""
    entry_point = pkg_resources.EntryPoint.parse("x={}".format(name))
    result = entry_point.load(False)
    return result

def cluster_spec(num_workers: int, num_ps: int, num_masters: int = 0) -> dict:
    """
    Generate a cluster specification (for distributed Tensorflow).
    """
    cluster = {}
    port = 12222

    all_ps = []
    host = "127.0.0.1"
    for _ in range(num_ps):
        all_ps.append("{}:{}".format(host, port))
        port += 1
    cluster["ps"] = all_ps

    all_workers = []
    for _ in range(num_workers):
        all_workers.append("{}:{}".format(host, port))
        port += 1
    cluster["worker"] = all_workers

    if num_masters > 0:
        all_masters = []
        for _ in range(num_masters):
            all_masters.append("{}:{}".format(host, port))
            port += 1
        cluster["master"] = all_masters
    return cluster


class RunningMeanStd(object):
    """
    Calculates the running mean and standard deviation of values of shape `shape`.
    """

    def __init__(self, shape, epsilon=1e-2):
        super(RunningMeanStd, self).__init__()
        self.count = epsilon
        self._sum = np.zeros(shape, dtype="float64")
        self._sumsq = np.full(shape, epsilon, dtype="float64")

    def add_value(self, x):
        """
        Update count, sum and sum squared using a new value `x`.
        """
        x = np.asarray(x, dtype="float64")
        self.count += 1
        self._sum += x
        self._sumsq += np.square(x)

    def add_values(self, x):
        """
        Update count, sum and sum squared using multiple values `x`.
        """
        x = np.asarray(x, dtype="float64")
        self.count += np.shape(x)[0]
        self._sum += np.sum(x, axis=0)
        self._sumsq += np.square(x).sum(axis=0)

    @property
    def mean(self):
        return self._sum / self.count

    @property
    def std(self):
        return np.sqrt(np.maximum((self._sumsq / self.count) - np.square(self.mean), 1e-2))

number_array = Union[int, float, np.ndarray]
def normalize(x: number_array, mean: number_array, std: number_array) -> Union[float, np.ndarray]:
    if isinstance(x, np.ndarray):
        x = x.astype("float64")
    return np.clip((x - mean) / std, -5.0, 5.0)


def soft_update(source_vars: Sequence[tf.Variable], target_vars: Sequence[tf.Variable], tau: float) -> None:
    """Move each source variable by a factor of tau towards the corresponding target variable.

    Arguments:
        source_vars {Sequence[tf.Variable]} -- Source variables to copy from
        target_vars {Sequence[tf.Variable]} -- Variables to copy data to
        tau {float} -- How much to change to source var, between 0 and 1.
    """
    if len(source_vars) != len(target_vars):
        raise ValueError("source_vars and target_vars must have the same length.")
    for source, target in zip(source_vars, target_vars):
        target.assign((1.0 - tau) * target + tau * source)


def hard_update(source_vars: Sequence[tf.Variable], target_vars: Sequence[tf.Variable]) -> None:
    """Copy source variables to target variables.

    Arguments:
        source_vars {Sequence[tf.Variable]} -- Source variables to copy from
        target_vars {Sequence[tf.Variable]} -- Variables to copy data to
    """
    soft_update(source_vars, target_vars, 1.0) # Tau of 1, so get everything from source and keep nothing from target

def flatten_list(l: List[List]):
    return list(itertools.chain.from_iterable(l))

spaces_mapping = {
    Discrete: "discrete",
    MultiDiscrete: "multidiscrete",
    Box: "continuous",
    MultiBinary: "multibinary"
}
