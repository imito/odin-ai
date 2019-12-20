from __future__ import absolute_import, division, print_function

import collections
from enum import Flag, auto
from numbers import Number

import numpy as np
import tensorflow as tf
from tensorflow_probability import distributions as tfd
from tensorflow_probability.python.layers.distribution_layer import (
    _get_convert_to_tensor_fn, _serialize)
from tensorflow_probability.python.layers.internal import \
    distribution_tensor_coercible as dtc
from tensorflow_probability.python.layers.internal import \
    tensor_tuple as tensor_tuple

from odin.bay import distributions as obd

__all__ = ['print_dist', 'coercible_tensor', 'kl_divergence']


# ===========================================================================
# Logging
# ===========================================================================
def _dist2text(dist):
  return '[%s]%s batch:%s event:%s' % (dist.__class__.__name__, dist.dtype.name,
                                       dist.batch_shape, dist.event_shape)


def _extract_desc(dist, name, pad):
  text = pad + (name + ':' if len(name) > 0 else '') + _dist2text(dist) + '\n'
  for key, val in dist.parameters.items():
    if isinstance(val, tfd.Distribution):
      text += _extract_desc(val, key, pad + '  ')
    elif tf.is_tensor(val):
      text += pad + '  %s shape:%s dtype:%s\n' % (key, val.shape, val.dtype)
    else:
      text += pad + '  %s:%s\n' % (key, str(val))
  return text


def print_dist(dist, return_text=False):
  assert isinstance(dist, tfd.Distribution)
  text = _extract_desc(dist, '', '')
  if return_text:
    return text[:-1]
  print(text)


# ===========================================================================
# Objectives
# ===========================================================================
def coercible_tensor(d,
                     convert_to_tensor_fn=tfd.Distribution.sample,
                     return_value=False):
  r""" make a distribution convertible to Tensor using the
  `convert_to_tensor_fn`

  This code is copied from: `distribution_layers.py` tensorflow_probability
  """
  assert isinstance(d, tfd.Distribution), \
    "dist must be instance of tensorflow_probability.Distribution"
  convert_to_tensor_fn = _get_convert_to_tensor_fn(convert_to_tensor_fn)
  # Wraps the distribution to return both dist and concrete value."""
  value_is_seq = isinstance(d.dtype, collections.Sequence)
  maybe_composite_convert_to_tensor_fn = (
      (lambda d: tensor_tuple.TensorTuple(convert_to_tensor_fn(d)))
      if value_is_seq else convert_to_tensor_fn)
  distribution = dtc._TensorCoercible(
      distribution=d, convert_to_tensor_fn=maybe_composite_convert_to_tensor_fn)
  ### prepare the value
  value = distribution._value()
  value._tfp_distribution = distribution
  if value_is_seq:
    value.shape = value[-1].shape
    value.get_shape = value[-1].get_shape
    value.dtype = value[-1].dtype
    distribution.shape = value[-1].shape
    distribution.get_shape = value[-1].get_shape
  else:
    distribution.shape = value.shape
    distribution.get_shape = value.get_shape
  ### return
  if return_value:
    return distribution, value
  return distribution


# ===========================================================================
# Objectives
# ===========================================================================
def kl_divergence(q,
                  p,
                  analytic=False,
                  q_sample=lambda q: q.sample(),
                  reduce_axis=(),
                  reverse=True,
                  auto_remove_independent=True):
  r""" Calculating KL(q(x)||p(x)) (if reverse=True) or
  KL(p(x)||q(x)) (if reverse=False)

  Arguments:
    q : `tensorflow_probability.Distribution`, the approximated posterior
      distribution
    p : `tensorflow_probability.Distribution`, the prior distribution
    analytic : bool (default: False)
      if True, use the close-form solution  for
    q_sample : {callable, Tensor, Number}
      callable for extracting sample from `q(x)` (takes `q` posterior distribution
      as input argument)
    reudce_axis : {None, int, tuple}. Reduce axis when use MCMC to estimate KL
      divergence, default `()` mean keep all original dimensions.
    reverse : `bool`. If `True`, calculating `KL(q||p)` which optimizes `q`
      (or p_model) by greedily filling in the highest modes of data (or, in
      other word, placing low probability to where data does not occur).
      Otherwise, `KL(p||q)` a.k.a maximum likelihood, place high probability
      at anywhere data occur (i.e. averagely fitting the data).
    auto_remove_independent : `bool`. If `q` or `p` is
      `tfd.Independent` wrapper, get the original distribution for calculating
      the analytic KL (default: `True`).

  Returns:
    A Tensor with the batchwise KL-divergence between `distribution_a`
      and `distribution_b`.  The shape is `[batch_dims]` for analytic KL,
      otherwise, `[n_mcmc, batch_dims]`.

  Example:
  ```python
  p = bay.distributions.OneHotCategorical(logits=[1, 2, 3])

  w = bk.variable(np.random.rand(2, 3).astype('float32'))
  q = bay.distributions.OneHotCategorical(w)

  opt = tf.optimizers.Adam(learning_rate=0.01,
                           beta_1=0.9,
                           beta_2=0.999,
                           epsilon=1e-07,
                           amsgrad=False)
  for i in range(1000):
    with tf.GradientTape() as tape:
      kl = bay.kl_divergence(q=q, p=p, q_sample=lambda q: q.sample(1000))
      grads = bk.grad(tf.reduce_mean(kl), w, tape=tape)
      opt.apply_gradients(grads_and_vars=[(g, v) for g, v in zip(grads, [w])])
      if i % 10 == 0:
        print("#%3d KL: %.4f" % (i, tf.reduce_mean(kl).numpy()))
  print(q.sample())
  ```
  """
  if auto_remove_independent:
    # only remove Independent if one is Indepedent and another is not.
    if isinstance(q, tfd.Independent) and not isinstance(p, tfd.Independent):
      q = q.distribution
    if not isinstance(q, tfd.Independent) and isinstance(p, tfd.Independent):
      p = p.distribution
  if not bool(reverse):
    q, p = [q, p][::-1]
  if bool(analytic):
    return tfd.kl_divergence(q, p)
  # using MCMC sampling for estimating the KL
  if callable(q_sample):
    z = q_sample(q)
  elif isinstance(q_sample, Number) or tf.is_tensor(q_sample):
    z = q.sample((tf.convert_to_tensor(q_sample, tf.int64),))
  else:
    z = q_sample
  # calculate the output, then perform reduction
  kl = q.log_prob(z) - p.log_prob(z)
  kl = tf.reduce_mean(input_tensor=kl, axis=reduce_axis)
  return kl


class KLdivergence:
  r""" This class freezes the arguments of `kl_divergence` so it could be call
  later without the required arguments.

  Calculating KL(q(x)||p(x)) (if reverse=True) or
  KL(p(x)||q(x)) (if reverse=False)

  Arguments:
    posterior : `tensorflow_probability.Distribution`, the approximated
      posterior distribution
    prior : `tensorflow_probability.Distribution`, the prior distribution
    analytic : bool (default: False)
      if True, use the close-form solution  for
    n_mcmc : {Tensor, Number}
      number of MCMC samples for MCMC estimation of KL-divergence
    reverse : `bool`. If `True`, calculating `KL(q||p)` which optimizes `q`
      (or p_model) by greedily filling in the highest modes of data (or, in
      other word, placing low probability to where data does not occur).
      Otherwise, `KL(p||q)` a.k.a maximum likelihood, place high probability
      at anywhere data occur (i.e. averagely fitting the data).
  Note:
    this class return 0. if the prior is not given (i.e. prior=None)
  """

  def __init__(self,
               posterior,
               prior=None,
               analytic=False,
               n_mcmc=1,
               reverse=True):
    self.posterior = posterior
    self.prior = prior
    self.analytic = bool(analytic)
    self.n_mcmc = int(n_mcmc)
    self.reverse = bool(reverse)

  def __call__(self, prior=None, analytic=None, n_mcmc=None, reverse=None):
    prior = self.prior if prior is None else prior
    analytic = self.analytic if analytic is None else bool(analytic)
    n_mcmc = self.n_mcmc if n_mcmc is None else int(n_mcmc)
    reverse = self.reverse if reverse is None else bool(reverse)
    if prior is None:
      return 0.
    return kl_divergence(q=self.posterior,
                         p=prior,
                         analytic=analytic,
                         reverse=reverse,
                         q_sample=n_mcmc,
                         auto_remove_independent=True)
