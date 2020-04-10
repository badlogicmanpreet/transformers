# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""PyTorch REFORMER model. """

import logging

import torch
import numpy as np
import inspect

from torch import nn
from torch.nn import CrossEntropyLoss

from .activations import gelu, gelu_new, swish
from .configuration_reformer import ReformerConfig
from .modeling_utils import PreTrainedModel

from typing import Callable

# DELETE later
import numpy


logger = logging.getLogger(__name__)

REFORMER_PRETRAINED_MODEL_ARCHIVE_MAP = {}
# TODO: fill with pretrained model weights
#    "reformer-base-uncased": "https://s3.amazonaws.com/models.huggingface.co/reformer/reformer-base-uncased-pytorch_model.bin",
#    "reformer-large-uncased": "https://s3.amazonaws.com/models.huggingface.co/reformer/reformer-large-uncased-pytorch_model.bin",
#    "reformer-base-cased": "https://s3.amazonaws.com/models.huggingface.co/reformer/reformer-base-cased-pytorch_model.bin",
#    "reformer-large-cased": "https://s3.amazonaws.com/models.huggingface.co/reformer/reformer-large-cased-pytorch_model.bin",

PATH_TO_SAVE_WEIGHTS = "/home/patrick/hugging_face/experiments/reformer/intermediate_weights"


def mish(x):
    return x * torch.tanh(nn.functional.softplus(x))


ACT2FN = {"gelu": gelu, "relu": torch.nn.functional.relu, "swish": swish, "gelu_new": gelu_new, "mish": mish}
ReformerLayerNorm = torch.nn.LayerNorm


def create_sinusoidal_embeddings(n_pos, dim, out):
    position_enc = np.array([[pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)] for pos in range(n_pos)])
    out[:, 0::2] = torch.FloatTensor(np.sin(position_enc[:, 0::2]))
    out[:, 1::2] = torch.FloatTensor(np.cos(position_enc[:, 1::2]))
    out.detach_()
    out.requires_grad = False


def apply_chunking_to_forward(chunk_size: int, chunk_dim: int, forward_fn: Callable[..., torch.Tensor], *input_tensors) -> torch.Tensor:
    """
    This function chunks the `input_tensors` into smaller input tensor parts of size `chunk_size` over the dimension `chunk_dim`.
    It then applies a layer `forward_fn` to each chunk independently to save memory.
    If the `forward_fn` is independent across the `chunk_dim` this function will yield the
    same result as not applying it.

    Args:
        chunk_size: int - the chunk size of a chunked tensor. `num_chunks` = `len(input_tensors[0]) / chunk_size`
        chunk_dim: int - the dimension over which the input_tensors should be chunked
        forward_fn: fn - the forward fn of the model
        input_tensors: tuple(torch.Tensor) - the input tensors of `forward_fn` which are chunked
    Returns:
        a Tensor with the same shape the foward_fn would have given if applied
    """

    assert len(input_tensors) > 0, "{} has to be a tuple/list of tensors".format(input_tensors)
    tensor_shape = input_tensors[0].shape
    assert all(input_tensor.shape == tensor_shape for input_tensor in input_tensors), "All input tenors have to be of the same shape"

    # inspect.signature exist since python 3.5 and is a python method -> no problem with backward compability
    num_args_in_forward_chunk_fn = len(inspect.signature(forward_fn).parameters)
    assert num_args_in_forward_chunk_fn == len(input_tensors), "forward_chunk_fn expects {} arguments, but only {} input tensors are given".format(num_args_in_forward_chunk_fn, len(input_tensors))

    if chunk_size > 0:
        assert input_tensors[0].shape[chunk_dim] % chunk_size == 0, "The dimension to be chunked {} has to be a multiple of the chunk size {}".format(input_tensors[0][chunk_dim], chunk_size)

        num_chunks = input_tensors[0].shape[chunk_dim] // chunk_size

        # chunk input tensor into tuples
        input_tensors_chunks = tuple(input_tensor.chunk(num_chunks, dim=chunk_dim) for input_tensor in input_tensors)
        # apply forward fn to every tuple
        output_chunks = tuple(forward_fn(*input_tensors_chunk) for input_tensors_chunk in zip(*input_tensors_chunks))
        # concatenate output at same dimension
        return torch.cat(output_chunks, dim=chunk_dim)

    return forward_fn(*input_tensors)


class ReformerEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings.
    """

    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        # TODO(PVP): check sinusoidal position embeddings -> not 100% the same as done in trax
        if config.sinusoidal_pos_embds:
            create_sinusoidal_embeddings(
                n_pos=config.max_position_embeddings, dim=config.hidden_size, out=self.position_embeddings.weight
            )

        self.dropout = config.hidden_dropout_prob

    def forward(self, input_ids=None, position_ids=None, inputs_embeds=None):
        if input_ids is not None:
            input_shape = input_ids.size()
        else:
            input_shape = inputs_embeds.size()[:-1]

        seq_length = input_shape[1]
        device = input_ids.device if input_ids is not None else inputs_embeds.device
        if position_ids is None:
            position_ids = torch.arange(seq_length, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).expand(input_shape)

        if inputs_embeds is None:
            inputs_embeds = self.word_embeddings(input_ids)

        position_embeddings = self.position_embeddings(position_ids)

        # TODO(PVP): In https://github.com/google/trax/blob/d947ebc184b94bc48d7e48cc63156a047f00992e/trax/models/reformer/reformer.py#L775 there are two dropouts right after each other as
        # it is implemented here. Check if this is ok.

        embeddings = nn.functional.dropout(inputs_embeds, self.dropout, self.training)
        embeddings = embeddings + position_embeddings
        embeddings = nn.functional.dropout(embeddings, self.dropout, self.training)
        return embeddings


class LSHSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.num_attention_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.hash_seed = config.seed
        self.num_hashes = config.num_hashes
        self.num_buckets = config.num_buckets
        self.chunk_length = config.chunk_length
        self.num_chunks_before = config.num_chunks_before
        self.num_chunks_after = config.num_chunks_after
        self.output_attentions = config.output_attentions
        self.is_decoder = config.is_decoder

        self.attention_head_size = int(self.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        if self.hidden_size % self.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads)
            )

        self.query_key = nn.Linear(self.hidden_size, self.all_head_size, bias=False)
        self.value = nn.Linear(self.hidden_size, self.all_head_size, bias=False)

        self.dropout = config.attention_probs_dropout_prob

    def forward(
        self,
        hidden_states,
        head_mask=None,
    ):

        # get SeqLen and BatchSize
        sequence_length = hidden_states.shape[1]
        batch_size = hidden_states.shape[0]

        mixed_query_key_vectors = self.query_key(hidden_states)
        mixed_value_vectors = self.value(hidden_states)

        query_key_vectors = self._transpose_for_scores(mixed_query_key_vectors)
        value_vectors = self._transpose_for_scores(mixed_value_vectors)

        assert query_key_vectors.shape[-1] == self.attention_head_size
        assert value_vectors.shape[-1] == self.attention_head_size

        # hash query key vectors into buckets
        buckets = self._hash_vectors(query_key_vectors)
        assert int(buckets.shape[-1]) == self.num_hashes * sequence_length

        ticker, undo_ticker = self._get_ticker_and_undo_ticker(sequence_length, buckets)

        query_key_vectors = self._gather_by_expansion(query_key_vectors, ticker)
        value_vectors = self._gather_by_expansion(value_vectors, ticker)

        # q_info = ticker

        query_key_vectors = self._split_dim_by(query_key_vectors, -1, self.chunk_length)
        value_vectors = self._split_dim_by(value_vectors, -1, self.chunk_length)
        ticker = self._split_dim_by(ticker, -1, self.chunk_length)

        # Optionally include adjacent chunks.
        if self.chunk_length is None:
            assert self.num_chunks_before == 0 and self.num_chunks_after == 0

        key_vectors = self._len_and_dim_norm(query_key_vectors)

        out_vectors, logits, attention_probs = self._attend(query_key_vectors, key_vectors, value_vectors, ticker, undo_ticker, head_mask)

        if self.num_hashes > 1:
            out_vectors = self._split_dim_by(out_vectors, self.num_hashes, sequence_length)
            logits = self._split_dim_by(logits, self.num_hashes, sequence_length).unsqueeze(-1)

            probs_vectors = torch.exp(logits - torch.logsumexp(logits, dim=2, keepdim=True))
            out_vectors = torch.sum(out_vectors * probs_vectors, dim=2)

        assert out_vectors.shape == (batch_size, self.num_attention_heads, sequence_length, self.attention_head_size)

        out_vectors = self._transpose_for_output(out_vectors)
        outputs = (out_vectors, attention_probs) if self.output_attentions else (out_vectors,)
        return outputs

    def _hash_vectors(self, vectors):
        batch_size = vectors.shape[0]

        # See https://arxiv.org/pdf/1509.02897.pdf
        # We sample a different random rotation for each round of hashing to
        # decrease the probability of hash misses.
        if isinstance(self.num_buckets, int):
            assert self.num_buckets % 2 == 0, 'There should be an even number of bucktes, but `self.num_bucktes`: {}'.format(self.num_buckets)
            rotation_size = self.num_buckets
            num_buckets = self.num_buckets
        else:
            # Factorize the hash if self.num_buckets is a list or tuple
            rotation_size, num_buckets = 0, 1
            for num_bucket in self.num_buckets:
                assert num_bucket % 2 == 0, 'The number of buckets should be even, but `num_bucket`: {}'.format(num_bucket)
                rotation_size += num_bucket
                num_buckets *= num_bucket

        rotations_shape = (vectors.shape[-1], self.num_hashes, rotation_size // 2)

        # TODO: delete later when integration tests are ok
        # create a random self.attention_head_size x self.num_hashes x self.num_buckets/2
#        random_rotations = torch.randn(rotations_shape, device=vectors.device)
        numpy.random.seed(self.hash_seed)
        random_rotations = torch.tensor(numpy.random.normal(size=rotations_shape), dtype=torch.float32, device=vectors.device)
        # rotated_vectors has dim:
        # Output dim: Batch_Size x Num_Attn_Heads x Num_Hashes x Seq_Len x Num_Buckets/2
        # TODO: IMPORTANT: At the moment we use the same random rotation over all batches
        # and heads -> is that bad? It seems like in original reformer a different random
        # rotation is used over heads and batches
        rotated_vectors = torch.einsum('bmtd,dhr->bmhtr', vectors, random_rotations)

        if isinstance(self.num_buckets, int) or len(self.num_buckets) == 1:
            rotated_vectors = torch.cat([rotated_vectors, -rotated_vectors], dim=-1)
            buckets = torch.argmax(rotated_vectors, dim=-1)
        else:
            # Get the buckets for them and combine.
            buckets, cur_sum, cur_product = None, 0, 1
            for num_bucket in self.num_buckets:
                rotated_vectors = rotated_vectors[..., cur_sum:cur_sum + (num_bucket // 2)]
                cur_sum += num_bucket // 2
                rotated_vectors = torch.cat([rotated_vectors, -rotated_vectors], dim=-1)

                if buckets is None:
                    buckets = torch.argmax(rotated_vectors, dim=-1)
                else:
                    buckets += cur_product * torch.argmax(rotated_vectors, dim=-1)

                cur_product *= num_bucket

        # buckets is now (Batch_size x Num_Attn_Heads x Num_Hashes x Seq_Len).
        # Next we add offsets so that bucket numbers from different hashing rounds don't overlap.
        offsets = torch.arange(self.num_hashes, device=vectors.device)
        offsets = torch.reshape(offsets * num_buckets, (-1, 1))

        # repeat same values for Batch_size and Num_Attn_Heads
        offsets = offsets.repeat(batch_size, self.num_attention_heads, 1, 1)
        offset_buckets = self._merge_by_middle_dim(buckets + offsets)

        return offset_buckets

    def _get_ticker_and_undo_ticker(self, sequence_length, buckets):
        batch_size = buckets.shape[0]

        # TODO: what is ticker? Is ticker something like indices?? Ask authors
        ticker = torch.arange(self.num_hashes * sequence_length, device=buckets.device)
        ticker = ticker.repeat(batch_size, self.num_attention_heads, 1)

        buckets_and_t = sequence_length * buckets + (ticker % sequence_length)

        # Hash-based sort
        sorted_ticker = torch.argsort(buckets_and_t, dim=-1)
        undo_sorted_ticker = torch.argsort(sorted_ticker, dim=-1)

        sorted_ticker = (sorted_ticker % sequence_length)
        return sorted_ticker, undo_sorted_ticker

    def _attend(self, query_vectors, key_vectors, value_vectors, ticker, undo_ticker, head_mask):
        key_vectors = self._look_adjacent(key_vectors)
        value_vectors = self._look_adjacent(value_vectors)

        # get logits and dots
        query_key_dots = torch.matmul(query_vectors, key_vectors.transpose(-1, -2))

        # TODO(PVP): better naming
        query_info = ticker
        key_value_info = self._look_adjacent(ticker)

        # Causal mask
        if self.is_decoder:
            # TODO (PVP): This line can be improved in terms of memory. Mask should be inserted and does not have to be created each time layer is called
            mask = torch.lt(query_info.unsqueeze(-1), key_value_info.unsqueeze(-2)).long().to(query_info.device)
            query_key_dots = query_key_dots - mask * 1e9

        # Self mask
        # TODO (PVP): This line can be improved in terms of memory. Mask should be inserted and does not have to be created each time layer is called
        mask = torch.eq(query_info.unsqueeze(-1), key_value_info.unsqueeze(-2)).long().to(query_info.device)
        query_key_dots = query_key_dots - mask * 1e5

        # Note: Causal mask probably uses higher mask value (-1e9) than Self mask (-1e5) so that token is able to attend to itself when it has no other valid attention targets.

        # Note: Self mask is used because Q and K projection weights are shared.
        # From the reformer paper (https://arxiv.org/pdf/2001.04451.pdf):

        # " While attention to the future is not allowed, typical implementations of the
        # Transformer do allow a position to attend to itself.
        # Such behavior is undesirable in a shared-QK formulation because the dot-product
        # of a query vector with itself will almost always be greater than the dot product of a
        # query vector with a vector at another position. We therefore modify the masking
        # to forbid a token from attending to itself, except in situations
        # where a token has no other valid attention targets (e.g. the first token in a sequence) "

        logits = torch.logsumexp(query_key_dots, dim=-1, keepdim=True)
        dots = torch.exp(query_key_dots - logits)

        # dropout
        dots = nn.functional.dropout(dots, self.dropout, self.training)

        # Mask heads if we want to
        if head_mask is not None:
            dots = dots * head_mask

        # attend values
        out_vectors = torch.matmul(dots, value_vectors)

        # merge chunk length
        logits = self._merge_by_middle_dim(logits).squeeze(-1)
        out_vectors = self._merge_by_middle_dim(out_vectors)

        expanded_undo_sort_indices = undo_ticker.unsqueeze(-1).expand(-1, -1, -1, self.attention_head_size)
        out_vectors = torch.gather(out_vectors, 2, expanded_undo_sort_indices)
        logits = torch.gather(logits, 2, undo_ticker)

        return out_vectors, logits, dots

    def _look_adjacent(self, vectors):
        """ Used to implement attention between consecutive chunks.

            Args:
                vectors: array of shape [batch_size, num_attention_heads, n_chunks, chunk_len, ...]
            Returns:
                array of shape [n_chunks, N * chunk_len, ...], where
                N = (1 + n_chunks_before + n_chunks_after).
        """
        if self.num_chunks_before == 0 and self.num_chunks_after == 0:
            return vectors

        slices = []
        for i in range(-self.num_chunks_before, self.num_chunks_after + 1):
            if i == 0:
                slices.append(vectors)
            else:
                slices.append(torch.cat([vectors[:, :, i:, ...], vectors[:, :, :i, ...]], dim=2))
        return torch.cat(slices, dim=3)

    def _len_and_dim_norm(self, vectors):
        vectors = self._len_norm(vectors)
        vectors = vectors / torch.sqrt(torch.tensor(self.attention_head_size, device=vectors.device, dtype=torch.float32))
        return vectors

    def _len_norm(self, x, epsilon=1e-6):
        variance = torch.mean(x**2, -1, keepdim=True)
        norm_x = x / torch.sqrt(variance + epsilon)
        return norm_x

    def _gather_by_expansion(self, vectors, idxs):
        expanded_idxs = idxs.unsqueeze(-1).expand(-1, -1, -1, self.attention_head_size)
        vectors = vectors.repeat(1, 1, self.num_hashes, 1)
        return torch.gather(vectors, 2, expanded_idxs)

    def _transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.transpose(2, 1)

    def _transpose_for_output(self, x):
        x = x.permute(0, 2, 1, 3)
        return torch.reshape(x, (x.size()[0], -1, self.num_attention_heads * self.attention_head_size))

    def _split_dim_by(self, vectors, dim_factor_1, dim_factor_2):
        batch_size = vectors.shape[0]
        split_dim_shape = (batch_size, self.num_attention_heads, dim_factor_1, dim_factor_2)

        if len(vectors.shape) == 4:
            return torch.reshape(vectors, split_dim_shape + (self.attention_head_size,))
        elif len(vectors.shape) == 3:
            return torch.reshape(vectors, split_dim_shape)
        else:
            raise ValueError("Input vector rank should be one of [3, 4], but is: {}".format(len(vectors.shape)))

    def _merge_by_middle_dim(self, vectors):
        batch_size = vectors.shape[0]
        new_dim_shape = (batch_size, self.num_attention_heads, -1)

        if len(vectors.shape) == 5:
            return torch.reshape(vectors, new_dim_shape + (vectors.shape[-1],))
        elif len(vectors.shape) == 4:
            return torch.reshape(vectors, new_dim_shape)
        else:
            raise ValueError("Input vector rank should be one of [4, 5], but is: {}".format(len(vectors.shape)))


class ReformerSelfOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size, bias=False)
        self.dropout = config.hidden_dropout_prob

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = nn.functional.dropout(hidden_states, self.dropout, self.training)
        # residual connection
        output = (hidden_states + input_tensor)
        return output


class ReformerAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.layer_norm = ReformerLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.self_attention = LSHSelfAttention(config)
        self.output = ReformerSelfOutput(config)

    def forward(
        self,
        hidden_states,
        prev_attention_output,
        head_mask=None,
    ):
        norm_hidden_states = self.layer_norm(hidden_states)
        self_attention_outputs = self.self_attention(
            norm_hidden_states, head_mask
        )

        # Implementation of RevNet (see Fig. 6 in https://towardsdatascience.com/illustrating-the-reformer-393575ac6ba0)
        # X_1 = prev_attention_output; X_2 = hidden_states
        attention_output = self.output(self_attention_outputs[0], prev_attention_output)
        outputs = (attention_output,) + self_attention_outputs[1:]  # add attentions if we output them
        return outputs


class ReformerFeedForwardDense(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.feed_forward_size)
        self.dropout = config.hidden_dropout_prob
        if isinstance(config.hidden_act, str):
            self.act_fn = ACT2FN[config.hidden_act]
        else:
            self.act_fn = config.hidden_act

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = nn.functional.dropout(hidden_states, self.dropout, self.training)
        hidden_states = self.act_fn(hidden_states)
        return hidden_states


class ReformerFeedForwardOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.feed_forward_size, config.hidden_size)
        self.dropout = config.hidden_dropout_prob

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = nn.functional.dropout(hidden_states, self.dropout, self.training)
        output = (hidden_states + input_tensor)
        return output


class ChunkReformerFeedForward(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.seq_len_dim = 1
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.layer_norm = ReformerLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dense = ReformerFeedForwardDense(config)
        self.output = ReformerFeedForwardOutput(config)

    # TODO(PVP): Does this work with backpropagation?
    def forward(self, attention_output, prev_attention_output):
        return apply_chunking_to_forward(self.chunk_size_feed_forward, self.seq_len_dim, self.forward_chunk, attention_output, prev_attention_output)

    def forward_chunk(self, attention_output, prev_attention_output):
        norm_attention_output = self.layer_norm(attention_output)
        dense_output = self.dense(norm_attention_output)
        output = self.output(dense_output, prev_attention_output)
        return output


class ReformerLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.attention = ReformerAttention(config)
        self.feed_forward = ChunkReformerFeedForward(config)

    def forward(
        self,
        prev_attention_output,
        hidden_states,
        head_mask=None,
    ):

        # Implementation of RevNet (see Fig. 6 in https://towardsdatascience.com/illustrating-the-reformer-393575ac6ba0)
        # X_1 = prev_attention_output; X_2 = hidden_states
        # Y_1 = attention_output; Y_2 = output
        attention_outputs = self.attention(hidden_states, prev_attention_output, head_mask)
        attention_output = attention_outputs[0]
        output = self.feed_forward(attention_output, hidden_states)

        outputs = (attention_output, output) + attention_outputs[1:]

        return outputs


class ReformerEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.layer = nn.ModuleList([ReformerLayer(config) for _ in range(config.num_hidden_layers)])
        # Reformer is using Rev Nets, thus last layer outputs are concatenated and
        # Layer Norm is done over 2 * hidden_size
        self.layer_norm = ReformerLayerNorm(2 * config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = config.hidden_dropout_prob

    def forward(
        self,
        hidden_states,
        head_mask=None,
    ):
        all_hidden_states = ()
        all_attentions = ()
        attention_output = hidden_states

        for i, layer_module in enumerate(self.layer):
            if self.output_hidden_states:
                all_hidden_states = all_hidden_states + (attention_output, hidden_states)

            layer_outputs = layer_module(attention_output, hidden_states, head_mask[i])
            attention_output, hidden_states = layer_outputs[:2]

            if self.output_attentions:
                all_attentions = all_attentions + (layer_outputs[2],)

        # Add last layer
        if self.output_hidden_states:
            all_hidden_states = all_hidden_states + (attention_output, hidden_states)

        # Concatenate 2 RevNet outputs
        hidden_states = torch.cat([attention_output, hidden_states], dim=-1)

        # Apply layer norm to concatenated hidden states
        hidden_states = self.layer_norm(hidden_states)

        # Apply dropout
        hidden_states = nn.functional.dropout(hidden_states, self.dropout, self.training)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (all_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (all_attentions,)
        return outputs  # last-layer hidden state, (all hidden states), (all attentions)


class ReformerPreTrainedModel(PreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for downloading and loading pretrained models.
    """

    config_class = ReformerConfig
    pretrained_model_archive_map = REFORMER_PRETRAINED_MODEL_ARCHIVE_MAP
    base_model_prefix = "reformer"

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, ReformerLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()


class ReformerModel(ReformerPreTrainedModel):
    """

    The model can behave as an encoder (with only self-attention) as well
    as a decoder, in which case a layer of cross-attention is added between
    the self-attention layers, following the architecture described in `Attention is all you need`_ by Ashish Vaswani,
    Noam Shazeer, Niki Parmar, Jakob Uszkoreit, Llion Jones, Aidan N. Gomez, Lukasz Kaiser and Illia Polosukhin.

    To behave as an decoder the model needs to be initialized with the
    :obj:`is_decoder` argument of the configuration set to :obj:`True`; an
    :obj:`encoder_hidden_states` is expected as an input to the forward pass.

    .. _`Attention is all you need`:
        https://arxiv.org/abs/1706.03762

    """

    def __init__(self, config):
        super().__init__(config)
        self.config = config

        self.embeddings = ReformerEmbeddings(config)
        self.encoder = ReformerEncoder(config)

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
    ):
        r"""
    Return:
        :obj:`tuple(torch.FloatTensor)` comprising various elements depending on the configuration (:class:`~transformers.BertConfig`) and inputs:
        last_hidden_state (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        hidden_states (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_hidden_states=True``):
            Tuple of :obj:`torch.FloatTensor` (one for the output of the embeddings + one for the output of each layer)
            of shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (:obj:`tuple(torch.FloatTensor)`, `optional`, returned when ``config.output_attentions=True``):
            Tuple of :obj:`torch.FloatTensor` (one for each layer) of shape
            :obj:`(batch_size, num_heads, sequence_length, sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.

    Examples::

        """

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()  # noqa: F841
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]  # noqa: F841
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device  # noqa: F841

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        if head_mask is not None:
            if head_mask.dim() == 1:
                head_mask = head_mask.unsqueeze(0).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
                head_mask = head_mask.expand(self.config.num_hidden_layers, -1, -1, -1, -1)
            elif head_mask.dim() == 2:
                head_mask = (
                    head_mask.unsqueeze(1).unsqueeze(-1).unsqueeze(-1)
                )  # We can specify head_mask for each layer
            head_mask = head_mask.to(
                dtype=next(self.parameters()).dtype
            )  # switch to fload if need + fp16 compatibility
        else:
            head_mask = [None] * self.config.num_hidden_layers

        embedding_output = self.embeddings(
            input_ids=input_ids, position_ids=position_ids, inputs_embeds=inputs_embeds
        )

        encoder_outputs = self.encoder(
            embedding_output,
            head_mask=head_mask,
        )
        sequence_output = encoder_outputs[0]

        # add hidden_states and attentions if they are here
        outputs = (sequence_output,) + encoder_outputs[1:]
        return outputs  # sequence_output, pooled_output, (hidden_states), (attentions)


class ReformerOnlyLMHead(nn.Module):
    def __init__(self, config):
        super().__init__()
        # Reformer is using Rev Nets, thus last layer outputs are concatenated and
        # Layer Norm is done over 2 * hidden_size
        self.seq_len_dim = 1
        self.chunk_size_lm_head = config.chunk_size_lm_head
        self.decoder = nn.Linear(2 * config.hidden_size, config.vocab_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.vocab_size))

        # Need a link between the two variables so that the bias is correctly resized with `resize_token_embeddings`
        self.decoder.bias = self.bias

    def forward(self, hidden_states):
        return apply_chunking_to_forward(self.chunk_size_lm_head, self.seq_len_dim, self.forward_chunk, hidden_states)

    # TODO(PVP): Does this work with backpropagation?
    def forward_chunk(self, hidden_states):
        hidden_states = self.decoder(hidden_states)
        return hidden_states


class ReformerModelWithLMHead(ReformerPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.reformer = ReformerModel(config)
        self.lm_head = ReformerOnlyLMHead(config)

        self.init_weights()

    def get_output_embeddings(self):
        return self.lm_head.decoder

    def tie_weights(self):
        # TODO(PVP): output and input embeddings are
        # apparently not tied so skip this step
        pass

    def forward(
        self,
        input_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        lm_labels=None,
    ):
        reformer_outputs = self.reformer(
            input_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
        )

        sequence_output = reformer_outputs[0]
        lm_logits = self.lm_head(sequence_output)
        outputs = (lm_logits,) + reformer_outputs[1:]

        if lm_labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :].contiguous()
            shift_labels = lm_labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(shift_logits.view(-1, self.config.vocab_size), shift_labels.view(-1))
            outputs = (loss,) + outputs

        return outputs  # (lm_loss), lm_logits, (hidden_states), (attentions)