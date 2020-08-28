# ------------------------------------------------------------------------------------------------ #
# MIT License                                                                                      #
#                                                                                                  #
# Copyright (c) 2020, Microsoft Corporation                                                        #
#                                                                                                  #
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software    #
# and associated documentation files (the "Software"), to deal in the Software without             #
# restriction, including without limitation the rights to use, copy, modify, merge, publish,       #
# distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the    #
# Software is furnished to do so, subject to the following conditions:                             #
#                                                                                                  #
# The above copyright notice and this permission notice shall be included in all copies or         #
# substantial portions of the Software.                                                            #
#                                                                                                  #
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING    #
# BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND       #
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,     #
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,   #
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.          #
# ------------------------------------------------------------------------------------------------ #

import warnings

import jax
import jax.numpy as jnp
import haiku as hk
import optax
from gym.spaces import Discrete

from .._core.value_q import Q
from .._core.policy import Policy
from ..utils import get_grads_diagnostics
from ._base import BaseTDLearning


class ClippedDoubleQLearning(BaseTDLearning):  # TODO(krholshe): make this less ugly
    r"""

    TD-learning with `TD3 <https://arxiv.org/abs/1802.09477>`_ style double q-learning updates, in
    which the target network is only used in selecting the would-be next action.

    For discrete actions, the :math:`n`-step bootstrapped target is constructed as:

    .. math::

        G^{(n)}_t\ =\ R^{(n)}_t + I^{(n)}_t\,\min_{i,j}q_i(S_{t+n}, \arg\max_a q_j(S_{t+n}, a))

    where :math:`q_i(s,a)` is the :math:`i`-th target q-function provided in :code:`q_targ_list`.

    Similarly, for non-discrete actions, the target is constructed as:

    .. math::

        G^{(n)}_t\ =\ R^{(n)}_t + I^{(n)}_t\,\min_{i,j}q_i(S_{t+n}, a_j(S_{t+n}))

    where :math:`a_i(s)` is the **mode** of the :math:`i`-th target policy provided in
    :code:`pi_targ_list`.


    where

    .. math::

        R^{(n)}_t\ &=\ \sum_{k=0}^{n-1}\gamma^kR_{t+k} \\
        I^{(n)}_t\ &=\ \left\{\begin{matrix}
            0           & \text{if $S_{t+n}$ is a terminal state} \\
            \gamma^n    & \text{otherwise}
        \end{matrix}\right.

    Parameters
    ----------
    q : Q

        The main q-function to update.

    pi_targ_list : list of Policy, optional

        The list of policies that are used for constructing the TD-target. This is ignored if the
        action space is discrete and *required* otherwise.

    q_targ_list : list of Q

        The list of q-functions that are used for constructing the TD-target.

    optimizer : optax optimizer, optional

        An optax-style optimizer. The default optimizer is :func:`optax.adam(1e-3)
        <optax.adam>`.

    loss_function : callable, optional

        The loss function that will be used to regress to the (bootstrapped) target. The loss
        function is expected to be of the form:

        .. math::

            L(y_\text{true}, y_\text{pred})\in\mathbb{R}

        If left unspecified, this defaults to :func:`coax.value_losses.huber`. Check out the
        :mod:`coax.value_losses` module for other predefined loss functions.

    value_transform : ValueTransform or pair of funcs, optional

        If provided, the returns are transformed as follows:

        .. math::

            G^{(n)}_t\ \mapsto\ f\left(G^{(n)}_t\right)\ =\
                f\left(R^{(n)}_t + I^{(n)}_t\,f^{-1}\left(q(S_{t+n}, A_{t+n})\right)\right)

        where :math:`f` and :math:`f^{-1}` are given by ``value_transform.transform_func`` and
        ``value_transform.inverse_func``, respectively. See :mod:`coax.td_learning` for examples of
        value-transforms. Note that a ValueTransform is just a glorified pair of functions, i.e.
        passing ``value_transform=(func, inverse_func)`` works just as well.

    """
    def __init__(
            self, q, pi_targ_list=None, q_targ_list=None,
            optimizer=None, loss_function=None, value_transform=None):

        super().__init__(
            f=q,
            f_targ=None,
            optimizer=optimizer,
            loss_function=loss_function,
            value_transform=value_transform)

        self._check_input_lists(pi_targ_list, q_targ_list)
        del self._f_targ  # no need for this (only potential source of confusion)
        self.q_targ_list = q_targ_list
        self.pi_targ_list = [] if pi_targ_list is None else pi_targ_list

        # consistency check
        if isinstance(self.q.action_space, Discrete):
            if len(self.q_targ_list) < 2:
                raise ValueError("len(q_targ_list) must be at least 2")
        elif len(self.q_targ_list) * len(self.pi_targ_list) < 2:
            raise ValueError("len(q_targ_list) * len(pi_targ_list) must be at least 2")

        def loss_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S, A = transition_batch[:2]
            A = self.q.action_preprocessor(A)
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            Q, state_new = self.q.function_type1(params, state, next(rngs), S, A, True)
            loss = self.loss_function(G, Q)
            return loss, (loss, G, Q, S, A, state_new)

        def grads_and_metrics_func(
                params, target_params, state, target_state, rng, transition_batch):

            rngs = hk.PRNGSequence(rng)
            grads, (loss, G, Q, S, A, state_new) = jax.grad(loss_func, has_aux=True)(
                params, target_params, state, target_state, next(rngs), transition_batch)

            # target-network estimate
            Q_targ_list = []
            qs = list(zip(self.q_targ_list, target_params['q_targ'], target_state['q_targ']))
            for q, pm, st in qs:
                Q_targ, _ = q.function_type1(pm, st, next(rngs), S, A, False)
                assert Q_targ.ndim == 1, f"bad shape: {Q_targ.shape}"
                Q_targ_list.append(Q_targ)

            # get min target estimate
            Q_targ_list = jnp.stack(Q_targ_list, axis=-1)
            assert Q_targ_list.ndim == 2, f"bad shape: {Q_targ_list.shape}"
            Q_targ = jnp.min(Q_targ_list, axis=-1)

            # residuals: estimate - better_estimate
            err = Q - G
            err_targ = Q_targ - Q

            name = self.__class__.__name__
            metrics = {
                f'{name}/loss': loss,
                f'{name}/bias': jnp.mean(err),
                f'{name}/rmse': jnp.sqrt(jnp.mean(jnp.square(err))),
                f'{name}/bias_targ': jnp.mean(err_targ),
                f'{name}/rmse_targ': jnp.sqrt(jnp.mean(jnp.square(err_targ)))}

            # add some diagnostics of the gradients
            metrics.update(get_grads_diagnostics(grads, key_prefix=f'{name}/grads_'))

            return grads, state_new, metrics

        def td_error_func(params, target_params, state, target_state, rng, transition_batch):
            rngs = hk.PRNGSequence(rng)
            S = transition_batch.S
            A = self.q.action_preprocessor(transition_batch.A)
            G = self.target_func(target_params, target_state, next(rngs), transition_batch)
            Q, _ = self.q.function_type1(params, state, next(rngs), S, A, False)
            dL_dQ = jax.grad(self.loss_function, argnums=1)
            return -dL_dQ(G, Q)

        def apply_grads_func(opt, opt_state, params, grads):
            updates, new_opt_state = opt.update(grads, opt_state)
            new_params = optax.apply_updates(params, updates)
            return new_opt_state, new_params

        self._apply_grads_func = jax.jit(apply_grads_func, static_argnums=0)
        self._grads_and_metrics_func = jax.jit(grads_and_metrics_func)
        self._td_error_func = jax.jit(td_error_func)

    @property
    def q(self):
        return self._f

    @property
    def target_params(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.params,
            'q_targ': [q.params for q in self.q_targ_list],
            'pi_targ': [pi.params for pi in self.pi_targ_list]})

    @property
    def target_function_state(self):
        return hk.data_structures.to_immutable_dict({
            'q': self.q.function_state,
            'q_targ': [q.function_state for q in self.q_targ_list],
            'pi_targ': [pi.function_state for pi in self.pi_targ_list]})

    def target_func(self, target_params, target_state, rng, transition_batch):
        rngs = hk.PRNGSequence(rng)
        Rn, In, S_next = transition_batch[3:6]

        # collect list of q-values
        if isinstance(self.q.action_space, Discrete):
            Q_sa_next_list = []
            qs = list(zip(self.q_targ_list, target_params['q_targ'], target_state['q_targ']))

            # compute A_next from q_i
            for q_i, params_i, state_i in qs:
                Q_s_next, _ = q_i.function_type2(params_i, state_i, next(rngs), S_next, False)
                assert Q_s_next.ndim == 2, f"bad shape: {Q_s_next.shape}"
                A_next = (Q_s_next == Q_s_next.max(axis=1, keepdims=True)).astype(Q_s_next.dtype)
                A_next /= A_next.sum(axis=1, keepdims=True)  # there may be ties

                # evaluate on q_j
                for q_j, params_j, state_j in qs:
                    Q_sa_next, _ = q_j.function_type1(
                        params_j, state_j, next(rngs), S_next, A_next, False)
                    assert Q_sa_next.ndim == 1, f"bad shape: {Q_sa_next.shape}"
                    Q_sa_next_list.append(Q_sa_next)

        else:
            Q_sa_next_list = []
            qs = list(zip(self.q_targ_list, target_params['q_targ'], target_state['q_targ']))
            pis = list(zip(self.pi_targ_list, target_params['pi_targ'], target_state['pi_targ']))

            # compute A_next from pi_i
            for pi_i, params_i, state_i in pis:
                dist_params, _ = pi_i.function(params_i, state_i, next(rngs), S_next, False)
                A_next = pi_i.proba_dist.mode(dist_params)  # greedy action

                # evaluate on q_j
                for q_j, params_j, state_j in qs:
                    Q_sa_next, _ = q_j.function_type1(
                        params_j, state_j, next(rngs), S_next, A_next, False)
                    assert Q_sa_next.ndim == 1, f"bad shape: {Q_sa_next.shape}"
                    Q_sa_next_list.append(Q_sa_next)

        # take the min to mitigate over-estimation
        Q_sa_next_list = jnp.stack(Q_sa_next_list, axis=-1)
        assert Q_sa_next_list.ndim == 2, f"bad shape: {Q_sa_next_list.shape}"
        Q_sa_next = jnp.min(Q_sa_next_list, axis=-1)

        assert Q_sa_next.ndim == 1, f"bad shape: {Q_sa_next.shape}"
        f, f_inv = self.value_transform
        return f(Rn + In * f_inv(Q_sa_next))

    def _check_input_lists(self, pi_targ_list, q_targ_list):
        # check input: pi_targ_list
        if isinstance(self.q.action_space, Discrete):
            if pi_targ_list is not None:
                warnings.warn("pi_targ_list is ignored, because action space is discrete")
        else:
            if pi_targ_list is None:
                raise TypeError("pi_targ_list must be provided if action space is not discrete")
            if not isinstance(pi_targ_list, (tuple, list)):
                raise TypeError(
                    f"pi_targ_list must be a list or a tuple, got: {type(pi_targ_list)}")
            if len(pi_targ_list) < 1:
                raise ValueError("pi_targ_list cannot be empty")
            for pi in pi_targ_list:
                if not isinstance(pi, Policy):
                    raise TypeError(
                        f"all pi_targ in pi_targ_list must be a coax.Policy, got: {type(pi)}")

        # check input: q_targ_list
        if not isinstance(q_targ_list, (tuple, list)):
            raise TypeError(f"q_targ_list must be a list or a tuple, got: {type(q_targ_list)}")
        if not q_targ_list:
            raise ValueError("q_targ_list cannot be empty")
        for q_targ in q_targ_list:
            if not isinstance(q_targ, Q):
                raise TypeError(f"all q_targ in q_targ_list must be a coax.Q, got: {type(q_targ)}")