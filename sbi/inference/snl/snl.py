from typing import Callable, Union, Optional, Dict

import os
from copy import deepcopy

import numpy as np
import torch
from pyro.infer.mcmc import HMC, NUTS
from pyro.infer.mcmc.api import MCMC
from torch import distributions
from torch import optim
from torch.nn.utils import clip_grad_norm_
from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from sbi.inference.posteriors.sbi_posterior import Posterior

import sbi.simulators as simulators
import sbi.utils as utils
from sbi.mcmc import Slice, SliceSampler
from sbi.simulators.simutils import (
    set_simulator_attributes,
    check_prior_and_data_dimensions,
)
from sbi.utils.torchutils import get_default_device


class SNL:
    """
    Implementation of
    'Sequential Neural Likelihood: Fast Likelihood-free Inference with Autoregressive Flows'
    Papamakarios et al.
    AISTATS 2019
    https://arxiv.org/abs/1805.07226
    """

    def __init__(
        self,
        simulator,
        prior: torch.distributions,
        true_observation: torch.Tensor,
        density_estimator: Optional[torch.nn.Module],
        simulation_batch_size: int = 1,
        summary_writer: SummaryWriter = None,
        device: torch.device = None,
        mcmc_method: str = "slice-np",
    ):
        """
        Args:
            simulator: Python object with 'simulate' method which takes a torch.Tensor
                of parameter values, and returns a simulation result for each parameter as a torch.Tensor.
            prior: Distribution object with 'log_prob' and 'sample' methods.
            true_observation: torch.Tensor containing the observation x0 for which to
            density_estimator: Conditional density estimator q(x | theta) in the form of an
                nets.Module. Must have 'log_prob' and 'sample' methods.
            simulation_batch_size: the number of parameter sets the simulator takes and converts to data x at
                the same time. If simulation_batch_size==-1, we simulate all parameter sets at the same time.
                If simulation_batch_size==1, the simulator has to process data of shape (1, num_dim).
                If simulation_batch_size>1, the simulator has to process data of shape (simulation_batch_size, num_dim).
            summary_writer: SummaryWriter
                Optionally pass summary writer. A way to change the log file location.
                If None, will create one internally, saving logs to cwd/logs.
            device: torch.device
                Optionally pass device
                If None, will infer it
            mcmc_method: MCMC method to use for posterior sampling. Must be one of
                ['slice', 'hmc', 'nuts'].
        """

        true_observation = utils.torchutils.atleast_2d(true_observation)
        check_prior_and_data_dimensions(prior, true_observation)
        # set name and dimensions of simulator
        simulator = set_simulator_attributes(simulator, prior, true_observation)

        self._simulator = simulator
        self._prior = prior
        self._true_observation = true_observation
        self._simulation_batch_size = simulation_batch_size
        self._device = get_default_device() if device is None else device

        # create the deep neural density estimator
        if density_estimator is None:
            density_estimator = utils.likelihood_nn(
                model="maf", prior=self._prior, context=self._true_observation,
            )

        # create neural posterior which can sample()
        self._neural_posterior = Posterior(
            algorithm_family="snl",
            neural_net=density_estimator,
            prior=prior,
            context=true_observation,
            mcmc_method=mcmc_method,
            get_potential_function=PotentialFunctionProvider(),
        )

        # switch to training mode
        self._neural_posterior.neural_net.train()

        # Need somewhere to store (parameter, observation) pairs from each round.
        self._parameter_bank, self._observation_bank = [], []

        # Each SNL run has an associated log directory for TensorBoard output.
        if summary_writer is None:
            log_dir = os.path.join(
                utils.get_log_root(), "snl", simulator.name, utils.get_timestamp()
            )
            self._summary_writer = SummaryWriter(log_dir)
        else:
            self._summary_writer = summary_writer

        # Each run also has a dictionary of summary statistics which are populated
        # over the course of training.
        self._summary = {
            "mmds": [],
            "median-observation-distances": [],
            "negative-log-probs-true-parameters": [],
            "neural-net-fit-times": [],
            "mcmc-times": [],
            "epochs": [],
            "best-validation-log-probs": [],
        }

    def __call__(self, num_rounds: int, num_simulations_per_round):
        """
        Run SNL over multiple rounds.
        
        This method requires num_simulations_per_round calls to
        the simulator per each of `num_rounds`.

        :param num_rounds: Number of rounds to run.
        :param num_simulations_per_round: Number of simulator calls per round.
        :return: None
        """

        round_description = ""
        tbar = tqdm(range(num_rounds))
        for round_ in tbar:

            tbar.set_description(round_description)

            # Generate parameters from prior in first round, and from most recent posterior
            # estimate in subsequent rounds.
            if round_ == 0:
                parameters, observations = simulators.simulate_in_batches(
                    simulator=self._simulator,
                    parameter_sample_fn=lambda num_samples: self._prior.sample(
                        (num_samples,)
                    ),
                    num_samples=num_simulations_per_round,
                    simulation_batch_size=self._simulation_batch_size,
                    x_dim=self._true_observation.shape[1:],  # do not pass batch_dim
                )
            else:
                parameters, observations = simulators.simulate_in_batches(
                    simulator=self._simulator,
                    parameter_sample_fn=lambda num_samples: self._neural_posterior.sample(
                        num_samples
                    ),
                    num_samples=num_simulations_per_round,
                    simulation_batch_size=self._simulation_batch_size,
                    x_dim=self._true_observation.shape[1:],  # do not pass batch_dim
                )

            # Store (parameter, observation) pairs.
            self._parameter_bank.append(torch.Tensor(parameters))
            self._observation_bank.append(torch.Tensor(observations))

            # Fit neural likelihood to newly aggregated dataset.
            self._fit_likelihood()

            # Update description for progress bar.
            round_description = (
                f"-------------------------\n"
                f"||||| ROUND {round_ + 1} STATS |||||:\n"
                f"-------------------------\n"
                f"Epochs trained: {self._summary['epochs'][-1]}\n"
                f"Best validation performance: {self._summary['best-validation-log-probs'][-1]:.4f}\n\n"
            )

            # Update TensorBoard and summary dict.
            self._summary_writer, self._summary = utils.summarize(
                summary_writer=self._summary_writer,
                summary=self._summary,
                round_=round_,
                true_observation=self._true_observation,
                parameter_bank=self._parameter_bank,
                observation_bank=self._observation_bank,
                simulator=self._simulator,
            )
        return self._neural_posterior

    def _fit_likelihood(
        self,
        batch_size=100,
        learning_rate=5e-4,
        validation_fraction=0.1,
        stop_after_epochs=20,
    ):
        """
        Trains the conditional density estimator for the likelihood by maximum likelihood
        on the most recently aggregated bank of (parameter, observation) pairs.
        Uses early stopping on a held-out validation set as a terminating condition.

        :param batch_size: Size of batch to use for training.
        :param learning_rate: Learning rate for Adam optimizer.
        :param validation_fraction: The fraction of data to use for validation.
        :param stop_after_epochs: The number of epochs to wait for improvement on the
        validation set before terminating training.
        :return: None
        """

        # Get total number of training examples.
        num_examples = torch.cat(self._parameter_bank).shape[0]

        # Select random train and validation splits from (parameter, observation) pairs.
        permuted_indices = torch.randperm(num_examples)
        num_training_examples = int((1 - validation_fraction) * num_examples)
        num_validation_examples = num_examples - num_training_examples
        train_indices, val_indices = (
            permuted_indices[:num_training_examples],
            permuted_indices[num_training_examples:],
        )

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(
            torch.cat(self._observation_bank), torch.cat(self._parameter_bank)
        )

        # Create neural_net and validation loaders using a subset sampler.
        train_loader = data.DataLoader(
            dataset,
            batch_size=batch_size,
            drop_last=True,
            sampler=SubsetRandomSampler(train_indices),
        )
        val_loader = data.DataLoader(
            dataset,
            batch_size=min(batch_size, num_examples - num_training_examples),
            shuffle=False,
            drop_last=False,
            sampler=SubsetRandomSampler(val_indices),
        )

        optimizer = optim.Adam(
            self._neural_posterior.neural_net.parameters(), lr=learning_rate
        )
        # Keep track of best_validation log_prob seen so far.
        best_validation_log_prob = -1e100
        # Keep track of number of epochs since last improvement.
        epochs_since_last_improvement = 0
        # Keep track of model with best validation performance.
        best_model_state_dict = None

        epochs = 0
        while True:

            # Train for a single epoch.
            self._neural_posterior.neural_net.train()
            for batch in train_loader:
                optimizer.zero_grad()
                inputs, context = batch[0].to(self._device), batch[1].to(self._device)
                log_prob = self._neural_posterior.log_prob(
                    inputs, context=context, normalize_snpe=False
                )
                loss = -torch.mean(log_prob)
                loss.backward()
                clip_grad_norm_(
                    self._neural_posterior.neural_net.parameters(), max_norm=5.0
                )
                optimizer.step()

            epochs += 1

            # Calculate validation performance.
            self._neural_posterior.neural_net.eval()
            log_prob_sum = 0
            with torch.no_grad():
                for batch in val_loader:
                    inputs, context = (
                        batch[0].to(self._device),
                        batch[1].to(self._device),
                    )
                    log_prob = self._neural_posterior.log_prob(
                        inputs, context=context, normalize_snpe=False
                    )
                    log_prob_sum += log_prob.sum().item()
            validation_log_prob = log_prob_sum / num_validation_examples

            # Check for improvement in validation performance over previous epochs.
            if validation_log_prob > best_validation_log_prob:
                best_validation_log_prob = validation_log_prob
                epochs_since_last_improvement = 0
                best_model_state_dict = deepcopy(
                    self._neural_posterior.neural_net.state_dict()
                )
            else:
                epochs_since_last_improvement += 1

            # If no validation improvement over many epochs, stop training.
            if epochs_since_last_improvement > stop_after_epochs - 1:
                self._neural_posterior.neural_net.load_state_dict(best_model_state_dict)
                break

        # Update summary.
        self._summary["epochs"].append(epochs)
        self._summary["best-validation-log-probs"].append(best_validation_log_prob)

    @property
    def summary(self):
        return self._summary


class PotentialFunctionProvider:
    """
    This class is initialized without arguments during the initialization of the Posterior class. When called, it specializes to the potential function appropriate to the requested mcmc_method.
    
   
    NOTE: Why use a class?
    ----------------------
    During inference, we use deepcopy to save untrained posteriors in memory. deepcopy uses pickle which can't serialize nested functions (https://stackoverflow.com/a/12022055).
    
    It is important to NOT initialize attributes upon instantiation, because we need the most current trained Posterior.neural_net.
    
    Returns:
        [Callable]: potential function for use by either numpy or pyro sampler
    """

    def __call__(
        self,
        prior: torch.distributions.Distribution,
        likelihood_nn: torch.nn.Module,
        observation: torch.Tensor,
        mcmc_method: str,
    ) -> Callable:
        """Return potential function. 
        
        Switch on numpy or pyro potential function based on mcmc_method.
        
        Args:
            prior: prior distribution that can be evaluated
            likelihood_nn: neural likelihood estimator that can be evaluated
            observation: actually observed conditioning context, x_o
            mcmc_method (str): one of slice-np, slice, hmc or nuts
        
        Returns:
            Callable: potential function for sampler.
        """ """        
        
        Args: 
        
        """
        self.likelihood_nn = likelihood_nn
        self.prior = prior
        self.observation = observation

        if mcmc_method in ("slice", "hmc", "nuts"):
            return self.pyro_potential
        else:
            return self.np_potential

    def np_potential(self, parameters: np.array) -> Union[torch.Tensor, float]:
        """Return posterior log prob. of parameters."
        
        Args:
            parameters: parameter vector, batch dimension 1
        
        Returns:
            Posterior log probability of the parameters, -Inf if impossible under prior.
        """
        parameters = torch.FloatTensor(parameters)
        log_likelihood = self.likelihood_nn.log_prob(
            inputs=self.observation.reshape(1, -1), context=parameters.reshape(1, -1)
        )

        # notice opposite sign to pyro potential
        return log_likelihood + self.prior.log_prob(parameters)

    def pyro_potential(self, parameters: Dict[str, torch.Tensor]) -> torch.Tensor:
        """Return posterior log prob. of parameters.
        
         Args:
            parameters: {name: tensor, ...} dictionary (from pyro sampler). The tensor's shape will be (1, x) if running a single chain or just (x) for multiple chains.
        
        Returns:
            potential: -[log r(x0, theta) + log p(theta)]
        """

        parameter = next(iter(parameters.values()))

        log_likelihood = self.likelihood_nn.log_prob(
            inputs=self.observation.reshape(1, -1), context=parameter.reshape(1, -1)
        )

        return -(log_likelihood + self.prior.log_prob(parameter))