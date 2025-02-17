# Copyright The PyTorch Lightning team.
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
from copy import copy
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import torch
from deprecate import void
from torch import Tensor
from torch.optim import Optimizer

from pytorch_lightning.core.optimizer import LightningOptimizer
from pytorch_lightning.loops.base import Loop
from pytorch_lightning.loops.closure import Closure, ClosureResult
from pytorch_lightning.loops.utilities import (
    _block_parallel_sync_behavior,
    _build_training_step_kwargs,
    _check_training_step_output,
    _process_training_step_output,
    check_finite_loss,
)
from pytorch_lightning.trainer.progress import OptimizationProgress
from pytorch_lightning.trainer.supporters import TensorRunningAccum
from pytorch_lightning.utilities import AMPType, AttributeDict, DeviceType, grad_norm
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _TPU_AVAILABLE
from pytorch_lightning.utilities.types import STEP_OUTPUT
from pytorch_lightning.utilities.warnings import WarningCache


class TrainingBatchLoop(Loop):
    """Runs over a single batch of data."""

    def __init__(self) -> None:
        super().__init__()
        self.accumulated_loss: Optional[Tensor] = None
        self.batch_outputs: Optional[List[List[STEP_OUTPUT]]] = None
        self.running_loss: TensorRunningAccum = TensorRunningAccum(window_length=20)
        # the current split index when the batch gets split into chunks in truncated backprop through time
        self.split_idx: Optional[int] = None
        self.optim_progress = OptimizationProgress()

        self._warning_cache: WarningCache = WarningCache()
        self._hiddens: Optional[Tensor] = None
        self._optimizer_freq_cumsum: Optional[int] = None
        self._remaining_splits: Optional[List[Any]] = None
        self._skip_backward: bool = False

    @property
    def done(self) -> bool:
        """Returns if all batch splits have been processed already"""
        return len(self._remaining_splits) == 0

    @property
    def optimizer_freq_cumsum(self) -> int:
        """Returns the cumulated sum of optimizer frequencies"""
        if self._optimizer_freq_cumsum is None:
            self._optimizer_freq_cumsum = np.cumsum(self.trainer.optimizer_frequencies)
        return self._optimizer_freq_cumsum

    def connect(self, **kwargs: "Loop") -> None:
        raise NotImplementedError(f"{self.__class__.__name__} does not connect any child loops.")

    def run(self, batch: Any, batch_idx: int) -> AttributeDict:
        """Runs all the data splits and the ``on_batch_start`` and ``on_train_batch_start`` hooks

        Args:
            batch: the current batch to run the train step on
            batch_idx: the index of the current batch
        """
        if batch is None:
            self._warning_cache.warn("train_dataloader yielded None. If this was on purpose, ignore this warning...")
            return AttributeDict(signal=0, training_step_output=[[]])

        # hook
        self.trainer.logger_connector.on_batch_start()
        response = self.trainer.call_hook("on_batch_start")
        if response == -1:
            return AttributeDict(signal=-1)

        # hook
        response = self.trainer.call_hook("on_train_batch_start", batch, batch_idx, 0)
        if response == -1:
            return AttributeDict(signal=-1)

        self.trainer.fit_loop.epoch_loop.batch_progress.increment_started()

        super().run(batch, batch_idx)
        output = AttributeDict(signal=0, training_step_output=self.batch_outputs)
        self.batch_outputs = None  # free memory
        return output

    def reset(self) -> None:
        """Resets the loop state"""
        self._hiddens = None
        self.batch_outputs = [[] for _ in range(len(self.trainer.optimizers))]

    def on_run_start(self, batch: Any, batch_idx: int):
        """Splits the data into tbptt splits

        Args:
            batch: the current batch to run the trainstep on
            batch_idx: the index of the current batch
        """
        void(batch_idx)
        self._remaining_splits = list(enumerate(self._tbptt_split_batch(batch)))

    def advance(self, batch, batch_idx):
        """Runs the train step together with optimization (if necessary) on the current batch split

        Args:
            batch: the current batch to run the training on (this is not the split!)
            batch_idx: the index of the current batch
        """
        void(batch)
        split_idx, split_batch = self._remaining_splits.pop(0)
        self.split_idx = split_idx

        # let logger connector extract current batch size
        self.trainer.logger_connector.on_train_split_start(batch_idx, split_idx, split_batch)

        if self.trainer.lightning_module.automatic_optimization:
            for opt_idx, optimizer in self.get_active_optimizers(batch_idx):
                # handle optimization restart
                if self.restarting:
                    if opt_idx < self.optim_progress.optimizer_idx:
                        continue

                self.optim_progress.optimizer_idx = opt_idx

                result = self._run_optimization(batch_idx, split_batch, opt_idx, optimizer)
                if result:
                    self.batch_outputs[opt_idx].append(copy(result.result_collection))
        else:
            # in manual optimization, there is no looping over optimizers
            result = self._run_optimization(batch_idx, split_batch)
            if result:
                self.batch_outputs[0].append(copy(result.result_collection))

    def teardown(self) -> None:
        # release memory
        self._remaining_splits = None

    def num_active_optimizers(self, batch_idx: Optional[int] = None) -> int:
        """Gets the number of active optimizers based on their frequency"""
        return len(self.get_active_optimizers(batch_idx))

    def _run_optimization(
        self,
        batch_idx: int,
        split_batch: Any,
        opt_idx: Optional[int] = None,
        optimizer: Optional[torch.optim.Optimizer] = None,
    ) -> Optional[ClosureResult]:
        """Runs closure (train step + backward) together with optimization if necessary.

        Args:
            batch_idx: the index of the current batch
            split_batch: the current tbptt split of the whole batch
            opt_idx: the index of the current optimizer or `None` in case of manual optimization
            optimizer: the current optimizer or `None` in case of manual optimization
        """
        # toggle model params
        self._run_optimization_start(opt_idx, optimizer)

        closure = self._make_closure(split_batch, batch_idx, opt_idx, optimizer, self._hiddens)

        if self.trainer.fit_loop.should_accumulate():
            # For gradient accumulation

            # -------------------
            # calculate loss (train step + train step end)
            # -------------------
            # automatic_optimization: perform ddp sync only when performing optimizer_step
            with _block_parallel_sync_behavior(self._trainer):
                closure()

        # ------------------------------
        # BACKWARD PASS
        # ------------------------------
        # gradient update with accumulated gradients
        else:
            if self.trainer.lightning_module.automatic_optimization:
                self._optimizer_step(optimizer, opt_idx, batch_idx, closure)
            else:
                closure()

        result = closure.get_result()

        if result:
            # if no result, user decided to skip optimization
            # otherwise update running loss + reset accumulated loss
            self._update_running_loss(result.loss)

        # untoggle model params
        self._run_optimization_end(opt_idx)
        return result

    def _make_closure(
        self,
        split_batch: Any,
        batch_idx: int,
        opt_idx: int,
        optimizer: Optimizer,
        hiddens: Any,
    ) -> Closure:
        """
        Build a closure object that captures the given arguments and runs the `training_step` function and optionally
        other functions such as `backward` and `zero_grad`.
        """
        step_fn = self._make_step_fn(split_batch, batch_idx, opt_idx, hiddens)
        backward_fn = self._make_backward_fn(optimizer, opt_idx)
        zero_grad_fn = self._make_zero_grad_fn(batch_idx, opt_idx, optimizer)

        return Closure(
            step_fn=step_fn,
            backward_fn=backward_fn,
            zero_grad_fn=zero_grad_fn,
            profiler=self.trainer.profiler,
        )

    def _make_step_fn(self, split_batch: Any, batch_idx: int, opt_idx: int, hiddens: Any) -> Callable[[], dict]:
        """Build the step function that runs the `training_step` and processes its output."""
        return partial(self._training_step, split_batch, batch_idx, opt_idx, hiddens)

    def _make_zero_grad_fn(self, batch_idx: int, opt_idx: int, optimizer: Optimizer) -> Optional[Callable[[], None]]:
        """
        Build a `zero_grad` function that zeroes the gradients before back-propagation.
        Returns ``None`` in the case backward needs to be skipped, e.g., when manual optimization is on.
        """

        def zero_grad_fn():
            self._on_before_zero_grad(optimizer)
            self._optimizer_zero_grad(batch_idx, optimizer, opt_idx)

        is_first_batch_to_accumulate = batch_idx % self.trainer.accumulate_grad_batches == 0
        if (
            not self._skip_backward
            and self.trainer.lightning_module.automatic_optimization
            and is_first_batch_to_accumulate
        ):
            return zero_grad_fn

    def _make_backward_fn(self, optimizer: Optimizer, opt_idx: int) -> Optional[Callable[[Tensor], Tensor]]:
        """
        Build a `backward` function that handles back-propagation through the output produced by the `training_step`
        function. Returns ``None`` in the case backward needs to be skipped, e.g., when manual optimization is on.
        """

        def backward_fn(loss: Tensor):
            self.backward(loss, optimizer, opt_idx)

            # check if loss or model weights are nan
            if self.trainer.terminate_on_nan:
                check_finite_loss(self.trainer.lightning_module, loss)

            return loss

        if not self._skip_backward and self.trainer.lightning_module.automatic_optimization:
            return backward_fn

    def _training_step(
        self, split_batch: Any, batch_idx: int, opt_idx: int, hiddens: Tensor
    ) -> Optional[AttributeDict]:
        """Performs the actual train step with the tied hooks.

        Args:
            split_batch: the current tbptt split of the current batch
            batch_idx: the index of the current batch
            opt_idx: the index of the current optimizer
            hiddens: the model's hidden state of the previous iteration

        Returns:
            an AttributeDict containing the loss value and the training step output.
        """
        # give the PL module a result for logging
        model_ref = self.trainer.lightning_module

        with self.trainer.profiler.profile("model_forward"):
            step_kwargs = _build_training_step_kwargs(
                model_ref, self.trainer.optimizers, split_batch, batch_idx, opt_idx, hiddens
            )

            # manually capture logged metrics
            model_ref._current_fx_name = "training_step"
            with self.trainer.profiler.profile("training_step"):
                training_step_output = self.trainer.accelerator.training_step(step_kwargs)
                self.trainer.accelerator.post_training_step()

            del step_kwargs

            training_step_output = self.trainer.call_hook("training_step_end", training_step_output)

            _check_training_step_output(self.trainer.lightning_module, training_step_output)

            result_collection, self._hiddens = _process_training_step_output(self.trainer, training_step_output)
            if result_collection is None:
                return

        closure_loss = None
        loss = None
        if self.trainer.lightning_module.automatic_optimization:
            # accumulate loss. if accumulate_grad_batches==1, no effect
            closure_loss = result_collection.minimize / self.trainer.accumulate_grad_batches
            # the loss will get scaled for amp. avoid any modifications to it
            loss = closure_loss.detach().clone()
        return AttributeDict(closure_loss=closure_loss, loss=loss, result_collection=result_collection)

    def _optimizer_step(
        self, optimizer: torch.optim.Optimizer, opt_idx: int, batch_idx: int, train_step_and_backward_closure: Callable
    ) -> None:
        """Performs the optimizer step and some sanity checking.

        Args:
            optimizer: the optimizer to perform the step with
            opt_idx: the index of the current :param:`optimizer`
            batch_idx: the index of the current batch
            train_step_and_backward_closure: the closure function performing the train step and computing the
                gradients. By default called by the optimizer (if possible)
        """
        model_ref = self.trainer.lightning_module

        is_lbfgs = isinstance(optimizer, torch.optim.LBFGS)
        using_native_amp = self.trainer.amp_backend == AMPType.NATIVE

        # native amp + lbfgs is a no go right now
        if using_native_amp and is_lbfgs:
            raise MisconfigurationException(
                "native PyTorch amp and lbfgs are not compatible."
                " To request, please file a Github issue in PyTorch and tag @mcarilli"
            )

        # wraps into LightningOptimizer only for running step
        optimizer = LightningOptimizer._to_lightning_optimizer(optimizer, self.trainer, opt_idx)

        self.optim_progress.optimizer.step.increment_ready()

        # model hook
        model_ref.optimizer_step(
            self.trainer.current_epoch,
            batch_idx,
            optimizer,
            opt_idx,
            train_step_and_backward_closure,
            on_tpu=(self.trainer._device_type == DeviceType.TPU and _TPU_AVAILABLE),
            using_native_amp=using_native_amp,
            using_lbfgs=is_lbfgs,
        )

        self.optim_progress.optimizer.step.increment_completed()

    def _on_before_zero_grad(self, optimizer: torch.optim.Optimizer) -> None:
        """Calls the ``on_before_zero_grad`` hook.

        Args:
            optimizer: the current optimizer
        """
        self.optim_progress.optimizer.zero_grad.increment_ready()
        self.trainer.call_hook("on_before_zero_grad", optimizer)
        self.optim_progress.optimizer.zero_grad.increment_started()

    def _optimizer_zero_grad(self, batch_idx: int, optimizer: torch.optim.Optimizer, opt_idx: int) -> None:
        """Zeroes out all gradients of parameters optimized by the current optimizer.

        Args:
            batch_idx: the index of the current batch
            optimizer: the current optimizer
            opt_idx: the index of the current optimizer
        """
        self.trainer.accelerator.optimizer_zero_grad(self.trainer.current_epoch, batch_idx, optimizer, opt_idx)
        self.optim_progress.optimizer.zero_grad.increment_completed()

    def _track_and_norm_grad(self, optimizer: torch.optim.Optimizer) -> Dict[str, Tensor]:
        """Tracks gradient norms and clips the gradients of all parameters optimized by the current optimizer.

        Args:
            optimizer: the current optimizer
        """
        # track gradient norms
        grad_norm_dict = {}
        can_log = (self.trainer.global_step + 1) % self.trainer.log_every_n_steps == 0
        should_track = float(self.trainer.track_grad_norm) > 0
        if should_track and can_log:
            grad_norm_dict = grad_norm(self.trainer.lightning_module, self.trainer.track_grad_norm)

        # clip gradients
        self.trainer.accelerator.clip_gradients(
            optimizer, self.trainer.gradient_clip_val, gradient_clip_algorithm=self.trainer.gradient_clip_algorithm
        )
        return grad_norm_dict

    def _tbptt_split_batch(self, batch: Any) -> List[Any]:
        """Splits a single batch into a list of sequence steps for tbptt.

        Args:
            batch: the current batch to split
        """
        tbptt_steps = self.trainer.lightning_module.truncated_bptt_steps
        if tbptt_steps == 0:
            return [batch]

        model_ref = self.trainer.lightning_module
        with self.trainer.profiler.profile("tbptt_split_batch"):
            splits = model_ref.tbptt_split_batch(batch, tbptt_steps)
        return splits

    def _run_optimization_start(self, opt_idx: int, optimizer: torch.optim.Optimizer) -> None:
        """Toggles the optimizer to ensure the correct one is used and prevend dangling grads.

        Args:
            opt_idx: the index of the optimizer to use
            optimizer: the optimizer to use

        """
        # make sure only the gradients of the current optimizer's parameters are calculated
        # in the training step to prevent dangling gradients in multiple-optimizer setup.
        if self.trainer.lightning_module.automatic_optimization and len(self.trainer.optimizers) > 1:
            model = self.trainer.lightning_module
            model.toggle_optimizer(optimizer, opt_idx)

    def _run_optimization_end(self, opt_idx: int) -> None:
        if self.trainer.lightning_module.automatic_optimization and len(self.trainer.optimizers) > 1:
            model = self.trainer.lightning_module
            model.untoggle_optimizer(opt_idx)

    def backward(
        self,
        loss: Tensor,
        optimizer: Optional[torch.optim.Optimizer],
        opt_idx: Optional[int] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Tensor:
        """Performs the backward step.

        Args:
            loss: The loss value to back-propagate on
            optimizer: Current optimizer being used. ``None`` if using manual optimization.
            opt_idx: Index of the current optimizer being used. ``None`` if using manual optimization.
        """
        self.trainer.accelerator.backward(loss, optimizer, opt_idx, *args, **kwargs)

        if not self.trainer.fit_loop.should_accumulate():
            # track gradients
            grad_norm_dict = self._track_and_norm_grad(optimizer=optimizer)
            if grad_norm_dict:
                self.trainer.lightning_module._current_fx_name = "on_after_backward"
                self.trainer.lightning_module.log_grad_norm(grad_norm_dict)
        return loss

    def _update_running_loss(self, current_loss: Tensor) -> None:
        """Updates the running loss value with the current value"""
        if self.trainer.lightning_module.automatic_optimization:
            # track total loss for logging (avoid mem leaks)
            self.accumulated_loss.append(current_loss)

        accumulated_loss = self.accumulated_loss.mean()

        if accumulated_loss is not None:
            # calculate running loss for display
            self.running_loss.append(self.accumulated_loss.mean() * self.trainer.accumulate_grad_batches)

        # reset for next set of accumulated grads
        self.accumulated_loss.reset()

    def get_active_optimizers(self, batch_idx: Optional[int] = None) -> List[Tuple[int, Optimizer]]:
        """
        Returns the currently active optimizers. When multiple optimizers are used with different frequencies,
        only one of the optimizers is active at a time.

        Returns:
            A list of tuples (opt_idx, optimizer) of currently active optimizers.
        """
        if not self.trainer.optimizer_frequencies:
            # call training_step once per optimizer
            return list(enumerate(self.trainer.optimizers))

        optimizers_loop_length = self.optimizer_freq_cumsum[-1]
        current_place_in_loop = batch_idx % optimizers_loop_length

        # find optimzier index by looking for the first {item > current_place} in the cumsum list
        opt_idx = int(np.argmax(self.optimizer_freq_cumsum > current_place_in_loop))
        return [(opt_idx, self.trainer.optimizers[opt_idx])]
