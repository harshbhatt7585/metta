"""Functional training utilities for Metta.

This module provides functional implementations of the core training loop components,
extracting the rollout and train logic from MettaTrainer into standalone functions.
"""

import logging
from collections import defaultdict
from contextlib import nullcontext
from typing import Any, Dict, Optional, Tuple

import einops
import numpy as np
import torch
import torch.distributed
from pufferlib import _C  # noqa: F401 - Required for torch.ops.pufferlib
from torch import Tensor

from metta.agent.policy_state import PolicyState
from metta.agent.util.debug import assert_shape
from metta.mettagrid.mettagrid_env import dtype_actions
from metta.mettagrid.util.dict_utils import unroll_nested_dict
from metta.rl.experience import Experience
from metta.rl.losses import Losses

logger = logging.getLogger(__name__)


def perform_rollout_step(
    policy: torch.nn.Module,
    vecenv: Any,
    experience: Experience,
    device: torch.device,
    timer: Optional[Any] = None,
) -> Tuple[int, list]:
    """Performs a single step of the rollout, interacting with the environment.

    Returns:
        Tuple of (num_steps, info_list)
    """
    # Receive environment data
    if timer:
        with timer("_rollout.env"):
            o, r, d, t, info, env_id, mask = vecenv.recv()
    else:
        o, r, d, t, info, env_id, mask = vecenv.recv()

    training_env_id = slice(env_id[0], env_id[-1] + 1)

    mask = torch.as_tensor(mask)
    num_steps = int(mask.sum().item())

    # Convert to tensors
    o = torch.as_tensor(o).to(device, non_blocking=True)
    r = torch.as_tensor(r).to(device, non_blocking=True)
    d = torch.as_tensor(d).to(device, non_blocking=True)
    t = torch.as_tensor(t).to(device, non_blocking=True)

    with torch.no_grad():
        state = PolicyState()
        lstm_h, lstm_c = experience.get_lstm_state(training_env_id.start)
        if lstm_h is not None:
            state.lstm_h = lstm_h
            state.lstm_c = lstm_c

        actions, selected_action_log_probs, _, value, _ = policy(o, state)

        if __debug__:
            assert_shape(selected_action_log_probs, ("BT",), "selected_action_log_probs")
            assert_shape(actions, ("BT", 2), "actions")

        lstm_state_to_store = None
        if state.lstm_h is not None:
            lstm_state_to_store = {"lstm_h": state.lstm_h.detach(), "lstm_c": state.lstm_c.detach()}

        if str(device).startswith("cuda"):
            torch.cuda.synchronize()

    value = value.flatten()

    experience.store(
        obs=o,
        actions=actions,
        logprobs=selected_action_log_probs,
        rewards=r,
        dones=d,
        truncations=t,
        values=value,
        env_id=training_env_id,
        mask=mask,
        lstm_state=lstm_state_to_store,
    )

    # Send actions back to environment
    if timer:
        with timer("_rollout.env"):
            vecenv.send(actions.cpu().numpy().astype(dtype_actions))
    else:
        vecenv.send(actions.cpu().numpy().astype(dtype_actions))

    return num_steps, info


def compute_ppo_losses(
    minibatch: Dict[str, Tensor],
    new_logprobs: Tensor,
    entropy: Tensor,
    newvalue: Tensor,
    importance_sampling_ratio: Tensor,
    adv: Tensor,
    trainer_cfg: Any,
    device: torch.device,
) -> Tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
    """Compute PPO losses for policy and value functions."""
    # Policy loss
    pg_loss1 = -adv * importance_sampling_ratio
    pg_loss2 = -adv * torch.clamp(
        importance_sampling_ratio, 1 - trainer_cfg.ppo.clip_coef, 1 + trainer_cfg.ppo.clip_coef
    )
    pg_loss = torch.max(pg_loss1, pg_loss2).mean()

    # Value loss
    newvalue_reshaped = newvalue.view(minibatch["returns"].shape)
    if trainer_cfg.ppo.clip_vloss:
        v_loss_unclipped = (newvalue_reshaped - minibatch["returns"]) ** 2
        vf_clip_coef = trainer_cfg.ppo.vf_clip_coef
        v_clipped = minibatch["values"] + torch.clamp(
            newvalue_reshaped - minibatch["values"],
            -vf_clip_coef,
            vf_clip_coef,
        )
        v_loss_clipped = (v_clipped - minibatch["returns"]) ** 2
        v_loss = 0.5 * torch.max(v_loss_unclipped, v_loss_clipped).mean()
    else:
        v_loss = 0.5 * ((newvalue_reshaped - minibatch["returns"]) ** 2).mean()

    entropy_loss = entropy.mean()

    # Compute metrics
    with torch.no_grad():
        logratio = new_logprobs - minibatch["logprobs"]
        approx_kl = ((importance_sampling_ratio - 1) - logratio).mean()
        clipfrac = ((importance_sampling_ratio - 1.0).abs() > trainer_cfg.ppo.clip_coef).float().mean()

    return pg_loss, v_loss, entropy_loss, approx_kl, clipfrac


def process_minibatch_update(
    policy: torch.nn.Module,
    experience: Experience,
    minibatch: Dict[str, Tensor],
    advantages: Tensor,
    trainer_cfg: Any,
    kickstarter: Any,
    agent_step: int,
    losses: Losses,
    device: torch.device,
) -> Tensor:
    """Process a single minibatch update and return the total loss."""
    obs = minibatch["obs"]

    lstm_state = PolicyState()
    _, new_logprobs, entropy, newvalue, full_logprobs = policy(obs, lstm_state, action=minibatch["actions"])

    new_logprobs = new_logprobs.reshape(minibatch["logprobs"].shape)
    logratio = new_logprobs - minibatch["logprobs"]
    importance_sampling_ratio = logratio.exp()
    experience.update_ratio(minibatch["indices"], importance_sampling_ratio)

    # Re-compute advantages with new ratios (V-trace)
    adv = compute_advantage(
        minibatch["values"],
        minibatch["rewards"],
        minibatch["dones"],
        importance_sampling_ratio,
        minibatch["advantages"],
        trainer_cfg.ppo.gamma,
        trainer_cfg.ppo.gae_lambda,
        trainer_cfg.vtrace.vtrace_rho_clip,
        trainer_cfg.vtrace.vtrace_c_clip,
        device,
    )

    # Normalize advantages with distributed support, then apply prioritized weights
    adv = normalize_advantage_distributed(adv, trainer_cfg.ppo.norm_adv)
    adv = minibatch["prio_weights"] * adv

    # Compute losses
    pg_loss, v_loss, entropy_loss, approx_kl, clipfrac = compute_ppo_losses(
        minibatch, new_logprobs, entropy, newvalue, importance_sampling_ratio, adv, trainer_cfg, device
    )

    # Kickstarter losses
    ks_action_loss, ks_value_loss = kickstarter.loss(agent_step, full_logprobs, newvalue, obs, teacher_lstm_state=[])

    # L2 init loss
    l2_init_loss = torch.tensor(0.0, device=device, dtype=torch.float32)
    if trainer_cfg.ppo.l2_init_loss_coef > 0:
        l2_init_loss = trainer_cfg.ppo.l2_init_loss_coef * policy.l2_init_loss().to(device)

    # Total loss
    loss = (
        pg_loss
        - trainer_cfg.ppo.ent_coef * entropy_loss
        + v_loss * trainer_cfg.ppo.vf_coef
        + l2_init_loss
        + ks_action_loss
        + ks_value_loss
    )

    # Update values in experience buffer
    experience.update_values(minibatch["indices"], newvalue.view(minibatch["values"].shape))

    # Update loss tracking
    losses.policy_loss_sum += pg_loss.item()
    losses.value_loss_sum += v_loss.item()
    losses.entropy_sum += entropy_loss.item()
    losses.approx_kl_sum += approx_kl.item()
    losses.clipfrac_sum += clipfrac.item()
    losses.l2_init_loss_sum += l2_init_loss.item() if torch.is_tensor(l2_init_loss) else l2_init_loss
    losses.ks_action_loss_sum += ks_action_loss.item()
    losses.ks_value_loss_sum += ks_value_loss.item()
    losses.importance_sum += importance_sampling_ratio.mean().item()
    losses.minibatches_processed += 1
    losses.current_logprobs_sum += new_logprobs.mean().item()

    return loss


def compute_advantage(
    values: Tensor,
    rewards: Tensor,
    dones: Tensor,
    importance_sampling_ratio: Tensor,
    advantages: Tensor,
    gamma: float,
    gae_lambda: float,
    vtrace_rho_clip: float,
    vtrace_c_clip: float,
    device: torch.device,
) -> Tensor:
    """CUDA kernel for puffer advantage with automatic CPU fallback.

    This matches the trainer.py implementation exactly.
    """
    # Get correct device
    device = torch.device(device) if isinstance(device, str) else device

    # Move tensors to device and compute advantage
    tensors = [values, rewards, dones, importance_sampling_ratio, advantages]
    tensors = [t.to(device) for t in tensors]
    values, rewards, dones, importance_sampling_ratio, advantages = tensors

    # Create context manager that only applies CUDA device context if needed
    device_context = torch.cuda.device(device) if str(device).startswith("cuda") else nullcontext()
    with device_context:
        torch.ops.pufferlib.compute_puff_advantage(
            values,
            rewards,
            dones,
            importance_sampling_ratio,
            advantages,
            gamma,
            gae_lambda,
            vtrace_rho_clip,
            vtrace_c_clip,
        )

    return advantages


def normalize_advantage_distributed(adv: Tensor, norm_adv: bool = True) -> Tensor:
    """Normalize advantages with distributed training support while preserving shape.

    This matches the trainer.py implementation exactly.
    """
    if not norm_adv:
        return adv

    if torch.distributed.is_initialized():
        # Compute local statistics
        adv_flat = adv.view(-1)
        local_sum = einops.rearrange(adv_flat.sum(), "-> 1")
        local_sq_sum = einops.rearrange((adv_flat * adv_flat).sum(), "-> 1")
        local_count = torch.tensor([adv_flat.numel()], dtype=adv.dtype, device=adv.device)

        # Combine statistics for single all_reduce
        stats = einops.rearrange([local_sum, local_sq_sum, local_count], "one float -> (float one)")
        torch.distributed.all_reduce(stats, op=torch.distributed.ReduceOp.SUM)

        # Extract global statistics
        global_sum, global_sq_sum, global_count = stats[0], stats[1], stats[2]
        global_mean = global_sum / global_count
        global_var = (global_sq_sum / global_count) - (global_mean * global_mean)
        global_std = torch.sqrt(global_var.clamp(min=1e-8))

        # Normalize and reshape back
        adv = (adv - global_mean) / (global_std + 1e-8)
    else:
        # Local normalization
        adv = (adv - adv.mean()) / (adv.std() + 1e-8)

    return adv


def calculate_explained_variance(values: Tensor, advantages: Tensor) -> float:
    """Calculate explained variance for value function evaluation."""
    y_pred = values.flatten()
    y_true = advantages.flatten() + values.flatten()
    var_y = y_true.var()
    explained_var = torch.nan if var_y == 0 else 1 - (y_true - y_pred).var() / var_y
    return explained_var.item() if torch.is_tensor(explained_var) else float("nan")


def get_lstm_config(policy: Any) -> Tuple[int, int]:
    """Extract LSTM configuration from policy."""
    hidden_size = getattr(policy, "hidden_size", 256)
    num_lstm_layers = 2  # Default value

    # Try to get actual number of LSTM layers from policy
    if hasattr(policy, "components") and "_core_" in policy.components:
        lstm_module = policy.components["_core_"]
        if hasattr(lstm_module, "_net") and hasattr(lstm_module._net, "num_layers"):
            num_lstm_layers = lstm_module._net.num_layers

    return hidden_size, num_lstm_layers


def calculate_batch_sizes(
    forward_pass_minibatch_target_size: int,
    num_agents: int,
    num_workers: int,
    async_factor: int,
) -> Tuple[int, int, int]:
    """Calculate target batch size, actual batch size, and number of environments.

    Returns:
        Tuple of (target_batch_size, batch_size, num_envs)
    """
    target_batch_size = forward_pass_minibatch_target_size // num_agents
    if target_batch_size < max(2, num_workers):  # pufferlib bug requires batch size >= 2
        target_batch_size = num_workers

    batch_size = (target_batch_size // num_workers) * num_workers
    num_envs = batch_size * async_factor

    return target_batch_size, batch_size, num_envs


def validate_policy_environment_match(policy: Any, env: Any) -> None:
    """Validate that policy's observation shape matches environment's."""
    from metta.agent.metta_agent import DistributedMettaAgent, MettaAgent

    # Extract agent from distributed wrapper if needed
    if isinstance(policy, MettaAgent):
        agent = policy
    elif isinstance(policy, DistributedMettaAgent):
        agent = policy.module
    else:
        raise ValueError(f"Policy must be of type MettaAgent or DistributedMettaAgent, got {type(policy)}")

    _env_shape = env.single_observation_space.shape
    environment_shape = tuple(_env_shape) if isinstance(_env_shape, list) else _env_shape

    # The rest of the validation logic continues to work with duck typing
    if hasattr(agent, "components"):
        found_match = False
        for component_name, component in agent.components.items():
            if hasattr(component, "_obs_shape"):
                found_match = True
                component_shape = (
                    tuple(component._obs_shape) if isinstance(component._obs_shape, list) else component._obs_shape
                )
                if component_shape != environment_shape:
                    raise ValueError(
                        f"Observation space mismatch error:\n"
                        f"[policy] component_name: {component_name}\n"
                        f"[policy] component_shape: {component_shape}\n"
                        f"environment_shape: {environment_shape}\n"
                    )

        if not found_match:
            raise ValueError(
                "No component with observation shape found in policy. "
                f"Environment observation shape: {environment_shape}"
            )


def calculate_prioritized_sampling_params(
    epoch: int,
    total_timesteps: int,
    batch_size: int,
    prio_alpha: float,
    prio_beta0: float,
) -> float:
    """Calculate annealed beta for prioritized experience replay."""
    total_epochs = max(1, total_timesteps // batch_size)
    anneal_beta = prio_beta0 + (1 - prio_beta0) * prio_alpha * epoch / total_epochs
    return anneal_beta


def accumulate_rollout_stats(
    raw_infos: list,
    stats: Dict[str, Any],
) -> None:
    """Accumulate rollout statistics from info dictionaries."""
    infos = defaultdict(list)

    # Batch process info dictionaries
    for i in raw_infos:
        for k, v in unroll_nested_dict(i):
            # Detach any tensors before accumulating to prevent memory leaks
            if torch.is_tensor(v):
                v = v.detach().cpu().item() if v.numel() == 1 else v.detach().cpu().numpy()
            elif isinstance(v, np.ndarray) and v.size == 1:
                v = v.item()
            infos[k].append(v)

    # Batch process stats
    for k, v in infos.items():
        if isinstance(v, np.ndarray):
            v = v.tolist()

        if isinstance(v, list):
            stats.setdefault(k, []).extend(v)
        else:
            if k not in stats:
                stats[k] = v
            else:
                try:
                    stats[k] += v
                except TypeError:
                    stats[k] = [stats[k], v]  # fallback: bundle as list


def compute_gradient_stats(policy: torch.nn.Module) -> Dict[str, float]:
    """Compute gradient statistics for the policy.

    Returns:
        Dictionary with 'grad/mean', 'grad/variance', and 'grad/norm' keys
    """
    all_gradients = []
    for param in policy.parameters():
        if param.grad is not None:
            all_gradients.append(param.grad.view(-1))

    if not all_gradients:
        return {}

    all_gradients_tensor = torch.cat(all_gradients).to(torch.float32)

    grad_mean = all_gradients_tensor.mean()
    grad_variance = all_gradients_tensor.var()
    grad_norm = all_gradients_tensor.norm(2)

    grad_stats = {
        "grad/mean": grad_mean.item(),
        "grad/variance": grad_variance.item(),
        "grad/norm": grad_norm.item(),
    }

    return grad_stats


def cleanup_old_policies(checkpoint_dir: str, keep_last_n: int = 5) -> None:
    """Clean up old saved policies to prevent memory accumulation.

    Args:
        checkpoint_dir: Directory containing policy checkpoints
        keep_last_n: Number of most recent policies to keep
    """
    from pathlib import Path

    try:
        # Get checkpoint directory
        checkpoint_path = Path(checkpoint_dir)
        if not checkpoint_path.exists():
            return

        # List all policy files
        policy_files = sorted(checkpoint_path.glob("policy_*.pt"))

        # Keep only the most recent ones
        if len(policy_files) > keep_last_n:
            files_to_remove = policy_files[:-keep_last_n]
            for file_path in files_to_remove:
                try:
                    file_path.unlink()
                except Exception as e:
                    logger.warning(f"Failed to remove old policy file {file_path}: {e}")

    except Exception as e:
        logger.warning(f"Error during policy cleanup: {e}")


def setup_distributed_vars() -> Tuple[bool, int, int]:
    """Set up distributed training variables.

    Returns:
        Tuple of (_master, _world_size, _rank)
    """
    if torch.distributed.is_initialized():
        _master = torch.distributed.get_rank() == 0
        _world_size = torch.distributed.get_world_size()
        _rank = torch.distributed.get_rank()
    else:
        _master = True
        _world_size = 1
        _rank = 0

    return _master, _world_size, _rank


def should_run_on_interval(
    epoch: int,
    interval: int,
    is_master: bool = True,
    force: bool = False,
) -> bool:
    """Check if a periodic task should run based on interval and master status.

    Args:
        epoch: Current epoch
        interval: Interval to check
        is_master: Whether this is the master rank
        force: Force run regardless of interval

    Returns:
        True if should run, False otherwise
    """
    if not is_master or not interval:
        return False

    if force:
        return True

    return epoch % interval == 0


def maybe_update_l2_weights(
    agent: Any,
    epoch: int,
    interval: int,
    is_master: bool = True,
    force: bool = False,
) -> None:
    """Update L2 init weights if on update interval.

    Args:
        agent: The agent/policy
        epoch: Current epoch
        interval: Update interval
        is_master: Whether this is the master rank
        force: Force update
    """
    if not should_run_on_interval(epoch, interval, is_master, force):
        return

    if hasattr(agent, "l2_init_weight_update_interval"):
        l2_interval = getattr(agent, "l2_init_weight_update_interval", 0)
        if isinstance(l2_interval, int) and l2_interval > 0:
            if hasattr(agent, "update_l2_init_weight_copy"):
                agent.update_l2_init_weight_copy()
