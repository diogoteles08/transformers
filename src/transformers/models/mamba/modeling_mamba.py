# coding=utf-8
# Copyright 2024 Tri Dao, Albert Gu and HuggingFace Inc. team.
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
"""PyTorch MAMBA model."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import nn
from torch.nn import CrossEntropyLoss

from ...activations import ACT2FN
from ...modeling_utils import PreTrainedModel
from ...utils import (
    ModelOutput,
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
)
from .configuration_mamba import MambaConfig


logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "state-spaces/mamba-2.8b"
_CONFIG_FOR_DOC = "MambaConfig"

MAMBA_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "state-spaces/mamba-2.8b",
    # See all Mamba models at https://huggingface.co/models?filter=mamba
]



mamba_cuda_kernel = None


# Copied from transformers.models.mamba.modeling_mamba.load_mamba_cuda_kernel with mamba->MAMBA,mamba->mamba
def load_mamba_cuda_kernel(context_length):
    from torch.utils.cpp_extension import load as load_kernel

    global mamba_cuda_kernel

    kernel_folder = Path(__file__).resolve().parent.parent.parent / "kernels" / "mamba"
    cuda_kernel_files = [kernel_folder / f for f in ["mamba_op.cpp", "mamba_cuda.cu", "mamba_cuda_bf16.cu"]]

    # Only load the kernel if it's not been loaded yet or if we changed the context length
    if mamba_cuda_kernel is not None and mamba_cuda_kernel.max_seq_length == context_length:
        return

    logger.info(f"Loading CUDA kernel for MAMBA at context length of {context_length}.")

    flags = [
        "-res-usage",
        "--maxrregcount 60",
        "--use_fast_math",
        "-O3",
        "-Xptxas -O3",
        "--extra-device-vectorization",
        f"-DTmax={context_length}",
    ]
    mamba_cuda_kernel = load_kernel(
        name=f"mamba_{context_length}",
        sources=cuda_kernel_files,
        verbose=(logging.get_verbosity() == logging.DEBUG),
        extra_cuda_cflags=flags,
    )
    mamba_cuda_kernel.max_seq_length = context_length


class MambaMixer(torch.autograd.Function):
    @staticmethod
    def forward(ctx, time_decay, time_first, key, value, state=None, return_state=False):
        batch_size, seq_len, hidden_size = key.size()
        if seq_len > mamba_cuda_kernel.max_seq_length:
            raise ValueError(
                f"Cannot process a batch with {seq_len} tokens at the same time, use a maximum of "
                f"{mamba_cuda_kernel.max_seq_length} with this model."
            )
        if batch_size * hidden_size % min(hidden_size, 32) != 0:
            raise ValueError(
                f"The product of batch size ({batch_size}) and hidden size ({hidden_size}) needs to be a round "
                f"multiple of {min(hidden_size, 32)}."
            )

        ctx.input_dtype = key.dtype

        if (
            time_decay.device.type != "cuda"
            or time_first.device.type != "cuda"
            or key.device.type != "cuda"
            or value.device.type != "cuda"
        ):
            raise ValueError("Calling the CUDA kernel for mamba attention requires all tensors to be on CUDA devices.")

        time_decay = -torch.exp(time_decay.float().contiguous())
        if key.dtype == torch.float16:
            time_first = time_first.float()
            key = key.float()
            value = value.float()
        time_first = time_first.contiguous()
        key = key.contiguous()
        value = value.contiguous()
        # The CUDA kernel will fill this tensor.
        output = torch.empty_like(key, memory_format=torch.contiguous_format)
        if return_state or state is not None:
            if state is None:
                state = torch.zeros(
                    batch_size,
                    hidden_size,
                    3,
                    dtype=torch.float32,
                    device=key.device,
                    memory_format=torch.contiguous_format,
                )
                state[:, :, 2] -= 1e38
            else:
                state = torch.cat([s.unsqueeze(2) for s in state], dim=2).contiguous()
            if key.dtype == torch.bfloat16:
                forward_func = mamba_cuda_kernel.forward_with_state_bf16
            else:
                forward_func = mamba_cuda_kernel.forward_with_state
            forward_func(time_decay, time_first, key, value, output, state)
        else:
            forward_func = mamba_cuda_kernel.forward_bf16 if key.dtype == torch.bfloat16 else mamba_cuda_kernel.forward
            forward_func(time_decay, time_first, key, value, output)

        ctx.save_for_backward(time_decay, time_first, key, value, output)

        if state is not None:
            state = [s.squeeze(2) for s in torch.chunk(state, 3, dim=2)]

        return output.to(ctx.input_dtype), state

    @staticmethod
    # g stands for grad
    def backward(ctx, g_output, g_state=None):
        input_dtype = ctx.input_dtype

        time_decay, time_first, key, value, output = ctx.saved_tensors
        # The CUDA kernel will fill those tensors.
        g_time_decay = torch.empty_like(
            time_decay,
            memory_format=torch.contiguous_format,
            dtype=torch.bfloat16 if input_dtype == torch.bfloat16 else torch.float32,
        )
        g_time_first = torch.empty_like(time_first, memory_format=torch.contiguous_format)
        g_key = torch.empty_like(key, memory_format=torch.contiguous_format)
        g_value = torch.empty_like(value, memory_format=torch.contiguous_format)

        if input_dtype == torch.float16:
            g_output = g_output.float()
        backward_func = mamba_cuda_kernel.backward_bf16 if input_dtype == torch.bfloat16 else mamba_cuda_kernel.backward
        backward_func(
            time_decay,
            time_first,
            key,
            value,
            output,
            g_output.contiguous(),
            g_time_decay,
            g_time_first,
            g_key,
            g_value,
        )

        return (
            g_time_decay.to(input_dtype),
            g_time_first.to(input_dtype),
            g_key.to(input_dtype),
            g_value.to(input_dtype),
            None,
            None,
        )


def mamba_linear_attention_cpu(time_decay, time_first, key, value, state=None, return_state=False):
    # For CPU fallback. Will be slower and probably take more memory than the custom CUDA kernel if not executed
    # within a torch.no_grad.
    _, seq_length, _ = key.size()
    output = torch.zeros_like(key)

    if state is None:
        num_state = torch.zeros_like(key[:, 0], dtype=torch.float32)
        den_state = torch.zeros_like(key[:, 0], dtype=torch.float32)
        max_state = torch.zeros_like(key[:, 0], dtype=torch.float32) - 1e38
    else:
        num_state, den_state, max_state = state
    # For numerical stability
    #    real_numerator_state = num_state * torch.exp(max_state)
    #    real_denominator_state = den_state * torch.exp(max_state)

    time_decay = -torch.exp(time_decay)

    for current_index in range(seq_length):
        current_key = key[:, current_index].float()
        current_value = value[:, current_index]

        # mamba computation at time t
        max_for_output = torch.maximum(max_state, current_key + time_first)
        e1 = torch.exp(max_state - max_for_output)
        e2 = torch.exp(current_key + time_first - max_for_output)
        numerator = e1 * num_state + e2 * current_value
        denominator = e1 * den_state + e2
        output[:, current_index] = (numerator / denominator).to(output.dtype)

        # Update state for next iteration
        max_for_state = torch.maximum(max_state + time_decay, current_key)
        e1 = torch.exp(max_state + time_decay - max_for_state)
        e2 = torch.exp(current_key - max_for_state)
        num_state = e1 * num_state + e2 * current_value
        den_state = e1 * den_state + e2
        max_state = max_for_state

    if return_state or state is not None:
        state = [num_state, den_state, max_state]

    return output, state


def mamba_mixer_forward(time_decay, time_first, key, value, state=None, return_state=False):
    no_cuda = any(t.device.type != "cuda" for t in [time_decay, time_first, key, value])
    # Launching the CUDA kernel for just one token will actually be slower (there is no for loop in the CPU version
    # in this case).
    one_token = key.size(1) == 1
    if mamba_cuda_kernel is None or no_cuda or one_token:
        return mamba_linear_attention_cpu(time_decay, time_first, key, value, state=state, return_state=return_state)
    else:
        return MambaMixer.apply(time_decay, time_first, key, value, state, return_state)


class MambaMixer(nn.Module):

    def __init__(self, config, layer_idx):
        super().__init__()
        self.d_model = config.hidden_size
        self.d_state = config.state_size
        self.d_conv = config.conv_kernel
        self.expand = config.expand
        self.d_inner = int(self.expand * self.d_model)
        self.time_step_rank = math.ceil(self.d_model / 16) if config.time_step_rank == "auto" else config.time_step_rank
        # self.use_fast_path = config.use_fast_path
        self.layer_idx = layer_idx



        self.conv1d = nn.Conv1d(
            in_channels=self.d_inner,
            out_channels=self.d_inner,
            bias=config.use_conv_bias,
            kernel_size=config.conv_kernel,
            groups=self.d_inner,
            padding=config.conv_kernel - 1,
        )

        self.activation = config.hidden_act
        self.act = ACT2FN[config.hidden_act]

        # projection of the input hidden states
        self.in_proj = nn.Linear(self.d_model, self.d_inner * 2, bias=config.use_bias)
        # selective projection used to make dt, B and C input dependant
        self.x_proj = nn.Linear(self.d_inner, self.time_step_rank + self.d_state * 2, bias=False)
        # time step projection (discretization)
        self.dt_proj = nn.Linear(self.time_step_rank, self.d_inner, bias=True)
        # S4D real initialization. These are not discretized!
        # THe core is to load them, compute the discrete states, then write the updates state.
        # Keeps the memory bounded
        A = torch.arange(1, self.d_state + 1, dtype=torch.float32)[None,:].expand(self.d_inner, -1).contiguous()
        A_log = torch.log(A)  # Keep A_log in fp32
        self.A_log = nn.Parameter(A_log)
        self.A_log._no_weight_decay = True

        # D "skip" parameter
        self.D = nn.Parameter(torch.ones(self.d_inner))  # Keep in fp32
        self.D._no_weight_decay = True
        self.out_proj = nn.Linear(self.d_inner, self.d_model, bias=config.use_conv_bias)

    def forward(self, hidden_states: torch.Tensor, inference_params=None):
        """
        hidden_states: (B, L, D)
        Returns: same shape as hidden_states
        """
        _, seqlen, _ = hidden_states.shape
        # conv_state, ssm_state = inference_params.key_value_memory_dict[self.layer_idx]

        projected_states = self.in_proj(hidden_states).transpose(1,2)
        hidden_states, z = projected_states.chunk(2, dim=1)

        if inference_params is not None and inference_params.seq_offset > 0:
            hidden_states = causal_conv1d_update(
                hidden_states,
                conv_state,
                self.conv1d.weight.view(self.conv1d.weight.size(0), self.conv1d.weight.size(2)),
                self.conv1d.bias,
                self.activation,
            )
        else:
            conv_state = F.pad(hidden_states, (self.d_conv - seqlen, 0))
            hidden_states = causal_conv1d_fn(
                hidden_states=hidden_states,
                weight=self.conv1d.weight.view(self.conv1d.weight.size(0), self.conv1d.weight.size(2)),
                bias=self.conv1d.bias,
                activation=self.activation,
            )

        # We're careful here about the layout, to avoid extra transposes.
        # We want dt to have d as the slowest moving dimension
        # and L as the fastest moving dimension, since those are what the ssm_scan kernel expects.
        x_dbl = self.x_proj(rearrange(hidden_states, "b d l -> (b l) d"))  # (bl d)
        dt, B, C = torch.split(x_dbl, [self.dt_rank, self.d_state, self.d_state], dim=-1)
        dt = self.dt_proj.weight @ dt.t()
        dt = rearrange(dt, "d (b l) -> b d l", l=seqlen)
        B = rearrange(B, "(b l) dstate -> b dstate l", l=seqlen).contiguous()
        C = rearrange(C, "(b l) dstate -> b dstate l", l=seqlen).contiguous()
        if inference_params is not None and inference_params.seq_offset > 0:
            y, _ = selective_scan_update(
                ssm_state, hidden_states, dt, self.negA, B, C, self.D, z=z, dt_bias=self.dt_proj.bias, dt_softplus=True
            )
        else:
            y, last_state = selective_scan_fn(
                hidden_states, dt, self.negA, B, C, self.D.float(),z=z,delta_bias=self.dt_proj.bias.float(),delta_softplus=True,return_last_state=True,
            )
        y = rearrange(y, "b d l -> b l d")
        attn_outputs = self.out_proj(y)
        return attn_outputs, conv_state, last_state


class MambaCache:
    def __init__(self, config,  batch_size, conv_dtype=torch.float32, ssm_dtype=torch.float32, device=None):
        self.seqlen_offset = 0
        d_model = config.hidden_size
        d_state = config.state_size
        expand = config.expand
        d_conv = config.conv_kernel

        self.conv_states = { i: torch.zeros(batch_size, d_model * expand, d_conv, device=device, dtype=conv_dtype) for i in range(config.num_hidden_layers)}
        self.ssm_states = { i:  torch.zeros(batch_size, d_model * expand, d_state, device=device, dtype=ssm_dtype)for i in range(config.num_hidden_layers)}


class MambaSlowMixer(MambaMixer):

    def forward(self, hidden_states, inference_params=None):
        """

         Compute ∆ A B C D, the state space parameters.
             A, D are input independent (see Mamba paper [1] Section 3.5.2 "Interpretation of A" for why A isn't selective)
             ∆, B, C are input-dependent (this is a key difference between Mamba and the linear time invariant S4,
                                          and is why Mamba is called **selective** state spaces)

        Args:
            hidden_states:
            inference_params:

        Returns:

        """
        batch_size, seq_len, _ = hidden_states.shape

        # 1. Gated MLP's linear projection
        projected_states = self.in_proj(hidden_states).transpose(1,2)
        hidden_states, gate = projected_states.chunk(2, dim=1)

        # 2. Convolution sequence transformation
        if inference_params.seqlen_offset > 0:
            conv_state = inference_params.conv_states[self.layer_idx]
            conv_state.copy_(torch.roll(conv_state, shifts=-1, dims=-1))
            conv_state[:, :, -1].copy_(hidden_states[:,:,0])
            hidden_states = torch.sum(conv_state * self.conv1d.weight[:, 0, :], dim=-1).unsqueeze(-1)
        else:
            inference_params.conv_states[self.layer_idx].copy_(nn.functional.pad(hidden_states, (self.d_conv - hidden_states.shape[-1], 0)))
            hidden_states = self.act(self.conv1d(hidden_states)[..., :seq_len])

        # 3. State Space Model sequence transformation
        # 3.a. input varying initialization of time_step, B and C
        x_dbl = self.x_proj(hidden_states.transpose(1,2))
        time_step, B, C = torch.split(x_dbl, [self.time_step_rank, self.d_state, self.d_state], dim=-1)
        discrete_time_step = self.dt_proj(time_step)
        A = -torch.exp(self.A_log.float())  # (d_inner, d_state)

        # 3.b. discretize time_step, B and C: zero-order hold from (B,L,D) to  (B,L,D,N)
        discrete_time_step = nn.functional.softplus(discrete_time_step).transpose(1,2)
        #                [batch_size, d, l, 1]     X        [1, d, 1, n] ->  [batch_size, d, l, n]
        dA = torch.exp(discrete_time_step[:, :, :, None] * A[None, :, None, :])
        #     [batch_size, d, l, 1]   [b, d, l, 1]  ->  [batch_size, d, l, 1]  X  [batch_size, 1, l, n] -> [batch_size, d, l, n]
        deltaB_u = (discrete_time_step[:, :, :, None] * hidden_states[:, :, :, None]) * B[:, None, :, :]

        # 3.c perform the recurrence y ← SSM(A, B, C)(x)
        ssm_state = inference_params.ssm_states[self.layer_idx]
        ys = []
        for i in range(seq_len):
            ssm_state.copy_(ssm_state * dA[:, :, i, :] + deltaB_u[:, :, i, :])
            #    [b, d, n]   X  [b, n] -> [b, d]
            y = torch.matmul(ssm_state, C[:,i,:].unsqueeze(-1))
            ys.append(y[:,:,0])
        y = torch.stack(ys, dim=-1)  # shape (b, l, d)
        y = y + (hidden_states * self.D.to(hidden_states.dtype)[None,:,None])
        y = y * self.act(gate) # (B D)
        # 4. Final linear projection
        attn_outputs = self.out_proj(y.transpose(1,2))
        return attn_outputs, None, ssm_state

class MambaRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        LlamaRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


class MambaBlock(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        # self.residual_in_fp32 = config.residual_in_fp32
        self.norm = MambaRMSNorm(config.hidden_size, eps=config.layer_norm_epsilon)
        self.mixer = MambaSlowMixer(config, layer_idx=layer_idx)

    def forward(self, hidden_states, inference_params=None):
        residual = hidden_states
        hidden_states = self.norm(hidden_states.to(dtype=self.norm.weight.dtype))
        # if self.residual_in_fp32:
        #     residual = residual.to(torch.float32)

        hidden_states, conv_states, ssm_state = self.mixer(hidden_states, inference_params=inference_params)
        hidden_states = residual + hidden_states
        return hidden_states, conv_states, ssm_state


class MambaPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = MambaConfig
    base_model_prefix = "mamba"
    _no_split_modules = ["MambaBlock"]
    supports_gradient_checkpointing = True

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, MambaMixer):
            pass
        if isinstance(module, nn.Linear):
            if module.bias is not None:
                if not getattr(module.bias, "_no_reinit", False):
                    nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, std=self.config.initializer_range)
        #
        # # Initialize dt bias so that F.softplus(dt_bias) is between dt_min and dt_max
        # dt = torch.exp(
        #     torch.rand(self.d_inner, **factory_kwargs) * (math.log(dt_max) - math.log(dt_min))
        #     + math.log(dt_min)
        # ).clamp(min=dt_init_floor)
        # # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
        # inv_dt = dt + torch.log(-torch.expm1(-dt))
        # with torch.no_grad():
        #     self.dt_proj.bias.copy_(inv_dt)
        # # Our initialization would set all Linear.bias to zero, need to mark this one as _no_reinit
        # self.dt_proj.bias._no_reinit = True

        # if isinstance(module, nn.Linear):
        #     if module.bias is not None:
        #         if not getattr(module.bias, "_no_reinit", False):
        #             nn.init.zeros_(module.bias)
        # elif isinstance(module, nn.Embedding):
        #     nn.init.normal_(module.weight, std=initializer_range)
        #
        # if rescale_prenorm_residual:
        #     # Reinitialize selected weights subject to the OpenAI GPT-2 Paper Scheme:
        #     #   > A modified initialization which accounts for the accumulation on the residual path with model depth. Scale
        #     #   > the weights of residual layers at initialization by a factor of 1/√N where N is the # of residual layers.
        #     #   >   -- GPT-2 :: https://openai.com/blog/better-language-models/
        #     #
        #     # Reference (Megatron-LM): https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/model/gpt_model.py
        #     for name, p in module.named_parameters():
        #         if name in ["out_proj.weight", "fc2.weight"]:
        #             # Special Scaled Initialization --> There are 2 Layer Norms per Transformer Block
        #             # Following Pytorch init, except scale by 1/sqrt(2 * n_layer)
        #             # We need to reinit p since this code could be called multiple times
        #             # Having just p *= scale would repeatedly scale it down
        #             nn.init.kaiming_uniform_(p, a=math.sqrt(5))
        #             with torch.no_grad():
        #                 p /= math.sqrt(n_residuals_per_layer * n_layer)


@dataclass
class MambaOutput(ModelOutput):
    """
    Class for the MAMBA model outputs.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        inference_params (list of five `torch.FloatTensor` of shape `(batch_size, hidden_size, num_hidden_layers)`):
            The state of the model at the last time step. Can be used in a forward method with the next `input_ids` to
            avoid providing the old `input_ids`.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    """

    last_hidden_state: torch.FloatTensor = None
    inference_params: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None


@dataclass
class MambaCausalLMOutput(ModelOutput):
    """
    Base class for causal language model (or autoregressive) outputs.

    Args:
        loss (`torch.FloatTensor` of shape `(1,)`, *optional*, returned when `labels` is provided):
            Language modeling loss (for next-token prediction).
        logits (`torch.FloatTensor` of shape `(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        inference_params (list of five `torch.FloatTensor` of shape `(batch_size, hidden_size, num_hidden_layers)`):
            The state of the model at the last time step. Can be used in a forward method with the next `input_ids` to
            avoid providing the old `input_ids`.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    """

    loss: Optional[torch.FloatTensor] = None
    logits: torch.FloatTensor = None
    inference_params: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None


MAMBA_START_DOCSTRING = r"""

    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config ([`MambaConfig`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

MAMBA_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, input_ids_length)`):
            `input_ids_length` = `sequence_length` if `past_key_values` is `None` else
            `past_key_values[0][0].shape[-2]` (`sequence_length` of input past key value states). Indices of input
            sequence tokens in the vocabulary.

            If `past_key_values` is used, only `input_ids` that do not have their past calculated should be passed as
            `input_ids`.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
            is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
            model's internal embedding lookup matrix.
        inference_params (tuple of five `torch.FloatTensor` of shape `(batch_size, hidden_size, num_hidden_layers)`, *optional*):
            If passed along, the model uses the previous state in all the blocks (which will give the output for the
            `input_ids` provided as if the model add `state_input_ids + input_ids` as context).
        use_cache (`bool`, *optional*):
            If set to `True`, the last state is returned and can be used to quickly generate the next logits.
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare MAMBA Model transformer outputting raw hidden-states without any specific head on top.",
    MAMBA_START_DOCSTRING,
)
class MambaModel(MambaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.embeddings = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList([MambaBlock(config, layer_idx=idx) for idx in range(config.num_hidden_layers)])

        self.gradient_checkpointing = False

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embeddings

    def set_input_embeddings(self, new_embeddings):
        self.embeddings = new_embeddings

    @add_start_docstrings_to_model_forward(MAMBA_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=MambaOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.LongTensor] = None,
        inference_params: Optional[List[torch.FloatTensor]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MambaOutput]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else (self.config.use_cache if not self.training else False)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is None and inputs_embeds is None:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embeddings(input_ids)

        if use_cache and inference_params is None:
            inference_params = MambaCache(self.config, inputs_embeds.size(0),  device=inputs_embeds.device)

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        hidden_states = inputs_embeds
        all_last_states = () if output_attentions else None
        all_hidden_states = () if output_hidden_states else None
        for idx, layer in enumerate(self.layers):
            if self.gradient_checkpointing and self.training:
                hidden_states, conv_state, ssm_state = self._gradient_checkpointing_func(layer.__call__, hidden_states, inference_params)
            else:
                hidden_states, conv_state, ssm_state = layer(hidden_states, inference_params=inference_params)
                inference_params.ssm_states[idx].copy_(ssm_state)
                # inference_params.conv_states[idx].copy_(conv_state)

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if output_attentions:
                all_last_states = all_last_states + (ssm_state,)
        inference_params.seqlen_offset += inputs_embeds.shape[1]

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(hidden_states for hidden_states in [hidden_states, inference_params, all_hidden_states, all_last_states] if hidden_states is not None)

        return MambaOutput(
            last_hidden_state=hidden_states,
            inference_params=inference_params,
            hidden_states=all_hidden_states,
            attentions=all_last_states,
        )


@add_start_docstrings(
    """
    The MAMBA Model transformer with a language modeling head on top (linear layer with weights tied to the input
    embeddings).
    """,
    MAMBA_START_DOCSTRING,
)
# Copied from transformers.models.mamba.modeling_mamba.mambaForCausalLM with mamba->MAMBA,mamba->Mamba,mamba->mamba
class MambaForCausalLM(MambaPreTrainedModel):
    _tied_weights_keys = ["head.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.backbone = MambaModel(config)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def get_input_embeddings(self):
        return self.backbone.get_input_embeddings()

    def set_input_embeddings(self, new_embeddings):
        return self.backbone.set_input_embeddings(new_embeddings)

    def _update_model_kwargs_for_generation(self,outputs: ModelOutput,
        model_kwargs: Dict[str, Any],
        is_encoder_decoder: bool = False,
        standardize_cache_format: bool = False,
    ) -> Dict[str, Any]:

        model_kwargs["inference_params"] = outputs["inference_params"]
        return model_kwargs


    def prepare_inputs_for_generation(self, input_ids, inference_params=None, inputs_embeds=None, attention_mask=None, **kwargs):
        # only last token for inputs_ids if the state is passed along.
        if inference_params is not None:
            input_ids = input_ids[:, -1].unsqueeze(-1)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and inference_params is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs["inference_params"] = inference_params
        return model_inputs

    @add_start_docstrings_to_model_forward(MAMBA_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=MambaCausalLMOutput,
        config_class=_CONFIG_FOR_DOC,
    )
    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        inference_params:  Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, MambaCausalLMOutput]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for language modeling. Note that the labels **are shifted** inside the model, i.e. you can set
            `labels = input_ids` Indices are selected in `[-100, 0, ..., config.vocab_size]` All labels set to `-100`
            are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        mamba_outputs = self.backbone(
            input_ids,
            inference_params=inference_params,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = mamba_outputs[0]

        logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            # move labels to correct device to enable model parallelism
            labels = labels.to(logits.device)
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

        if not return_dict:
            output = (logits,) + mamba_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return MambaCausalLMOutput(
            loss=loss,
            logits=logits,
            inference_params=mamba_outputs.inference_params,
            hidden_states=mamba_outputs.hidden_states,
            attentions=mamba_outputs.attentions,
        )
