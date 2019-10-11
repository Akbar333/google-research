# coding=utf-8
# Copyright 2019 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wrapper class for a gym-like environment that can be run in parallel."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import math
import os
import pickle
from absl import logging
import gin
import numpy as np
from polish.env import env as game_env


@gin.configurable
class ParallelEnv(object):
  """A class holds a number of Env instances to enable parallel trajectory rollouts.

  Attributes:
    len_segment: Length of one segment in the parallel MCTS rollouts.
    trajectory_states: Array for trajectory states.
    trajectory_actions: Arrays for trajectory actions.
    trajectory_values: Array for trajectory state-values.
    trajectory_returns: Array for trajectory returns.
    trajectory_means: Array for current policy mean values.
    trajectory_logstds: Array for current policy logstd values.
    trajectory_neg_logprobs: Array for trajectory negative log probabilities.
    trajectory_per_episode_rewards: Array for trajectory `episode` rewards. Each
      trajectoy may contain multiple episodes.
    trajectory_per_episode_lengths: Array for trajectory `episode` lengths. Each
      trajectory may contain multiple episodes.
    trajectory_per_step_rewards: Array for trajectory rewards (per step).
    trajectory_dones: Array for trajectory state status. True means a terminal
      state and False means not a terminal state.
    policy_trajectory_values: Array for trajectory values generated by a policy
      during MCTS training.
    policy_trajectory_states: Array for trajectory states generated by a policy
      during MCTS training.
    policy_trajectory_actions: Array for trajectory actions generated by
      a policy during MCTS training.
    policy_trajectory_returns: Array for trajectory returns generated by
      a policy during MCTS training.
    policy_trajectory_neg_logprobs: Array for trajectory negative
      log probabilities generated by a policy during MCTS training.
    policy_trajectory_per_step_rewards: Array for per step trajectory rewards
      generated by a policy during MCTS training.
    sampling_step: The current sampling step. When we do policy sampling, we
      increment it by 1 for every iteration of policy training.
    mcts_start_step: The sampling step to start MCTS training.
    mcts_end_step: The sampling step to end MCTS training.
    mcts_enable: True means enableing MCTS training.
    mcts_sampling: True means the current sampling step is used for MCTS
      training. False means it is used for normal policy training.
    mcts_collect_data_freq: Run MCTS training every mcts_collect_data_freq
      sampling steps.
    master_game: An Env for collecting policy trajectories.
    mcts_game: An Env for collecting MCTS trajectories.
    checkpoint_dir: The directory to save checkpoints to.
    first_mcts: True means this is the first time running MCTS training.
  """

  def __init__(self,
               env,
               estimator,
               serving_input_fn,
               len_segment=1,
               gamma=0.99,
               lam=0.95,
               tanh_action_clipping=False,
               obs_normalized=True,
               reward_normalized=True,
               clip_ob=10.,
               clip_rew=10.,
               epsilon=1e-8,
               mcts_enable=False,
               num_envs=1,
               mcts_start_step_frac=0.1,
               mcts_end_step_frac=0.9,
               num_iterations=156160,
               mcts_sim_decay_factor=0.8,
               mcts_sampling_frac=0.1,
               mcts_collect_data_freq=1,
               random_action_sampling_freq=0.0,
               checkpoint_dir=None,
               update_rms=False):

    self.initialize_episode_data()
    self.initialize_policy_episode_data()

    self.len_segment = len_segment
    self.sampling_step = 0
    self.mcts_start_step = int(mcts_start_step_frac * num_iterations)
    self.mcts_end_step = int(mcts_end_step_frac * num_iterations)
    self.mcts_enable = mcts_enable
    self.mcts_sampling = False
    self.mcts_collect_data_freq = mcts_collect_data_freq
    self.checkpoint_dir = checkpoint_dir

    self.first_mcts = True

    for game_type in ['master_game', 'mcts_game']:
      game = game_env.MCTSEnv(env,
                              estimator,
                              serving_input_fn,
                              gamma,
                              lam,
                              tanh_action_clipping,
                              obs_normalized,
                              reward_normalized,
                              clip_ob,
                              clip_rew,
                              epsilon,
                              mcts_enable,
                              num_envs,
                              mcts_start_step_frac,
                              mcts_end_step_frac,
                              num_iterations,
                              mcts_sim_decay_factor,
                              mcts_sampling_frac,
                              mcts_collect_data_freq,
                              random_action_sampling_freq,
                              checkpoint_dir)

      # master_game is used to generate initial trajectories both for
      # parallel and non-parallel modes.
      if game_type == 'master_game':
        self.master_game = game
        # Update the running mean and std for observations and returns.
        if update_rms:
          norm_file = os.path.join(self.checkpoint_dir, 'norm')
          with open(norm_file, 'rb') as norm_f:
            norm = pickle.load(norm_f)
          norm_f.close()

          # pylint: disable=protected-access
          self.master_game._ob_rms._mean = norm['obs']['mean']
          self.master_game._ob_rms._var = norm['obs']['var']
          self.master_game._ob_rms._count = norm['obs']['count']

          self.master_game._ret_rms._mean = norm['ret']['mean']
          self.master_game._ret_rms._var = norm['ret']['var']
          self.master_game._ret_rms._count = norm['ret']['count']
      else:
        game.prepare_mcts_player()
        self.mcts_game = game

  def mcts_sample_enable(self):
    return (self.mcts_enable and
            self.sampling_step >= self.mcts_start_step and
            self.sampling_step < self.mcts_end_step)

  def initialize_episode_data(self):
    """Initialize all related data in an episode to empty lists."""
    self.trajectory_states = []
    self.trajectory_actions = []
    self.trajectory_values = []
    self.trajectory_neg_logprobs = []
    self.trajectory_means = []
    self.trajectory_logstds = []
    self.trajectory_per_episode_rewards = []
    self.trajectory_per_episode_lengths = []
    self.trajectory_dones = []
    self.trajectory_per_step_rewards = []
    self.trajectory_returns = []

  def initialize_policy_episode_data(self):
    self.policy_trajectory_states = []
    self.policy_trajectory_values = []
    self.policy_trajectory_actions = []
    self.policy_trajectory_returns = []
    self.policy_trajectory_neg_logprobs = []
    self.policy_trajectory_per_step_rewards = []

  def process_trajectory_lengths(self, game, max_steps):
    """Compute the trajectory lengths for use of MCTS rollouts.

    Args:
      game: an Env where trajectories are collected.
      max_steps: Maximum number of steps run for trajectory collection.

    Returns:
      A list containing trajectory lengths (including partial ones).
    """
    # pylint: disable=g-explicit-length-test
    if game.trajectory_dones[-1]:
      len_trajectories = game.trajectory_per_episode_lengths
    elif len(game.trajectory_per_episode_lengths) > 0:
      # There is at least one completed trajectory and one incomplete one.
      len_trajectories = list(
          game.trajectory_per_episode_lengths) + [
              max_steps - sum(game.trajectory_per_episode_lengths)]
    else:
      # The only trajectory is an incomplete one.
      len_trajectories = [max_steps]

    return [int(l) for l in len_trajectories]

  def play(self, max_steps, test_mode=False):
    """Run max_steps in the environment to collect data.

    Args:
      max_steps: Maximum number of steps to run.
      test_mode: If set, it does not call some of the functions.
    """
    logging.info('Sampling Step: %d', self.sampling_step)
    self.master_game.update_estimator(test_mode)
    self.master_game.reset()
    self.master_game.initialize_episode_data()
    # Run master_game to collect policy trajectories.
    self.master_game.run_trajectory(max_steps)

    # If this sampling step is not for MCTS training, directly aggregate data
    # for normal policy training.
    if not self.mcts_sample_enable():
      logging.info('Policy Sampling...')
      self.initialize_episode_data()
      self.initialize_policy_episode_data()
      self.mcts_sampling = False
      self.aggregate_mcts_data()
      self.aggregate_policy_data()
    else:
      if self.first_mcts:
        # Update the running mean and std from master_game at the beginning
        # of the MCTS training.
        # pylint: disable=protected-access
        self.mcts_game._ob_rms = self.master_game._ob_rms
        self.mcts_game._ret_rms = self.master_game._ret_rms
        self.first_mcts = False

      if (self.sampling_step -
          self.mcts_start_step) % self.mcts_collect_data_freq == 0:
        self.initialize_episode_data()
        self.initialize_policy_episode_data()
        logging.info('MCTS Sampling...')
        self.mcts_sampling = True
        self.mcts_game.update_estimator(test_mode)

        base_len = 0
        # Collect information about the trajectory lengths for each episode.
        len_trajectories = self.process_trajectory_lengths(self.master_game,
                                                           max_steps)

        for len_trajectory in len_trajectories:
          # num_inits is the number of initial states we use to start parallel
          # MCTS from.
          num_inits = int(math.ceil(float(len_trajectory) / self.len_segment))

          # The interval to choose initial states for MCTS.
          init_state_interval = self.len_segment
          # The last interval might be of a shorter length.
          last_state_interval = (len_trajectory - (num_inits - 1)
                                 * init_state_interval)

          # A different MCTS rollout is started from equally
          # spaced state selected from the master_game trajectories.
          for i in range(num_inits):
            init_state = self.master_game.env_states[
                base_len + init_state_interval * i]
            init_action = self.master_game.trajectory_actions[
                base_len + init_state_interval * i]
            self.mcts_game.mcts_initialization(init_state, init_action)
            self.mcts_game.initialize_episode_data()
            if i < num_inits - 1:
              self.mcts_game.run_mcts_trajectory(
                  max_horizon=init_state_interval)
            else:
              self.mcts_game.run_mcts_trajectory(
                  max_horizon=last_state_interval)
            self.aggregate_mcts_data()
          base_len += len_trajectory

        self.aggregate_policy_data()

    self.sampling_step += 1

  def aggregate_mcts_data(self):
    """Aggregate data from MCTS rollouts."""

    data_attr_types = {
        'trajectory_states': self.master_game.env.observation_space.dtype,
        'trajectory_actions': np.float32,
        'trajectory_values': np.float32,
        'trajectory_neg_logprobs': np.float32,
        'trajectory_means': np.float32,
        'trajectory_logstds': np.float32,
        'trajectory_per_episode_rewards': np.float32,
        'trajectory_per_episode_lengths': np.float32,
        'trajectory_dones': np.bool,
        'trajectory_per_step_rewards': np.float32,
        'trajectory_returns': np.float32
    }

    for data_attr, data_type in data_attr_types.items():
      if self.mcts_sampling:
        old_attr_val = getattr(self, data_attr)
        mcts_attr_val = getattr(self.mcts_game, data_attr)

        # pylint: disable=g-explicit-length-test
        if len(old_attr_val) > 0:
          # Concatenate MCTS trajectory data, such as states and actions,
          # if they exist.
          new_attr_val = np.concatenate((old_attr_val, mcts_attr_val), axis=0)
        else:
          # Initialize MCTS trajectory related data.
          new_attr_val = np.asarray(mcts_attr_val, dtype=data_type)
      else:
        # For normal policy rollout, simply collect data from master_game.
        new_attr_val = np.asarray(getattr(self.master_game, data_attr),
                                  dtype=data_type)

      setattr(self, data_attr, new_attr_val)

  def aggregate_policy_data(self):
    """Aggregate data from policy rollouts."""

    data_attr_types = {
        'policy_trajectory_states':
            self.master_game.env.observation_space.dtype,
        'policy_trajectory_actions': np.float32,
        'policy_trajectory_values': np.float32,
        'policy_trajectory_neg_logprobs': np.float32,
        'policy_trajectory_per_step_rewards': np.float32,
        'policy_trajectory_returns': np.float32
    }

    for data_attr, data_type in data_attr_types.items():
      master_data_attr = data_attr.split('_', 1)[1]
      new_attr_val = np.asarray(getattr(self.master_game, master_data_attr),
                                dtype=data_type)
      setattr(self, data_attr, new_attr_val)
