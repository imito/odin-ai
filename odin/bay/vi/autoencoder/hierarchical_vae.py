from typing import Callable, List, Union, Dict, Any

import tensorflow as tf
from odin.backend.interpolation import Interpolation, linear
from odin.bay.helpers import kl_divergence
from odin.bay.random_variable import RVmeta
from odin.bay.vi.autoencoder.beta_vae import annealedVAE, betaVAE
from odin.bay.vi.autoencoder.variational_autoencoder import _parse_layers
from odin.networks import NetConf
from odin.utils import as_tuple
from tensorflow import keras
from tensorflow.keras.layers import Layer
from tensorflow_probability.python.distributions import Independent, Normal
from tensorflow_probability.python.layers import DistributionLambda


# ===========================================================================
# Hierarchical VAE
# ===========================================================================
class stackedVAE(betaVAE):
  """ A hierachical VAE with multiple stochastic layers stacked on top of the previous one
  (autoregressive):

    $q(z|x) = q(z_1|x) \mul_{i=2}^L q(z_i|z_{i-1})$

  Inference: `X -> E(->z1) -> E1(->z2) -> E2 -> z`

  Generation: `z -> D2 -> z2 -> D1 -> z1 -> D -> X~`

  The return from `encode` method: (q_z, q_z2,  q_z1)

  The return from `decode` method: (X~, p_z2, p_z1)

  Hierachical takes longer to train and often more unstable, reduce the learning rate
  is often desired.

  Parameters
  ----------
  ladder_hiddens : List[int], optional
      number of hidden units for layers in the ladder, each element corresponding
      to a ladder latents, by default [256]
  ladder_latents : List[int], optional
      number of latents units for each latent variable in the ladder,
      by default [64]
  ladder_layers : int, optional
      number of layers for each hidden layer in the ladder, by default 2
  batchnorm : bool, optional
      use batch normalization in the ladder hidden layers, by default True
  dropout : float, optional
      dropout rate for the ladder hidden layers, by default 0.0
  activation : Callable[[tf.Tensor], tf.Tensor], optional
      activation function for the ladder hidden layers, by default tf.nn.leaky_relu
  beta : Union[float, Interpolation], optional
      a fixed beta or interpolated beta based on iteration step. It is recommended
      to keep the beta value > 0 at the beginning of training, especially when using
      powerful architecture for encoder and decoder. Otherwise, the suboptimal
      latents could drive the network to very unstable loss region which result NaNs
      during early training,
      by default `linear(vmin=1e-4, vmax=1., length=2000, delay_in=0)`
  tie_latents : bool, optional
      tie the parameters that encoding means and standard deviation for both
      $q(z_i|z_{i-1})$ and $p(z_i|z_{i-1})$, by default False
  all_standard_prior : bool, optional
      use standard normal as prior for all latent variables, by default False

  References
  ----------
  Sønderby, C.K., Raiko, T., Maaløe, L., Sønderby, S.K., Winther, O., 2016.
    Ladder variational autoencoders, Advances in Neural Information Processing Systems.
    Curran Associates, Inc., pp. 3738–3746.
  Tomczak, J.M., Welling, M., 2018. VAE with a VampPrior. arXiv:1705.07120 [cs, stat].
  """

  def __init__(
      self,
      ladder_hiddens: List[int] = [256],
      ladder_latents: List[int] = [64],
      ladder_layers: int = 2,
      batchnorm: bool = True,
      batchnorm_kw: Dict[str, Any] = {'momentum': 0.9},
      dropout: float = 0.0,
      activation: Callable[[tf.Tensor], tf.Tensor] = tf.nn.leaky_relu,
      latents: Union[Layer, RVmeta] = RVmeta(32,
                                             'mvndiag',
                                             projection=True,
                                             name="latents"),
      beta: Union[float, Interpolation] = linear(vmin=1e-6,
                                                 vmax=1.,
                                                 length=2000,
                                                 delay_in=0),
      tie_latents: bool = False,
      all_standard_prior: bool = False,
      stochastic_inference: bool = True,
      only_mean_up: bool = False,
      preserve_latents_order: bool = False,
      name: str = 'HierarchicalVAE',
      **kwargs,
  ):
    super().__init__(latents=latents, beta=beta, name=name, **kwargs)
    assert len(ladder_hiddens) == len(ladder_latents)
    self.all_standard_prior = bool(all_standard_prior)
    self.stochastic_inference = bool(stochastic_inference)
    self.only_mean_up = bool(only_mean_up)
    self.ladder_encoder = [
        NetConf([units] * ladder_layers,
                activation=activation,
                batchnorm=batchnorm,
                batchnorm_kw=batchnorm_kw,
                dropout=dropout,
                name=f'LadderEncoder{i}').create_network()
        for i, units in enumerate(ladder_hiddens)
    ]
    self.ladder_decoder = [
        NetConf([units] * ladder_layers,
                activation=activation,
                batchnorm=batchnorm,
                batchnorm_kw=batchnorm_kw,
                dropout=dropout,
                name=f'LadderDecoder{i}').create_network()
        for i, units in enumerate(ladder_hiddens[::-1])
    ]
    self.ladder_qz = [
        _parse_layers(RVmeta(units, 'normal', projection=True, name=f'qZ{i}'))
        for i, units in enumerate(as_tuple(ladder_latents))
    ]
    if tie_latents:
      self.ladder_pz = self.ladder_qz
    else:
      self.ladder_pz = [
          _parse_layers(RVmeta(units, 'normal', projection=True, name=f'pZ{i}'))
          for i, units in enumerate(as_tuple(ladder_latents))
      ]

  def encode(self, inputs, training=None, mask=None, only_encoding=False):
    h = self.encoder(inputs, training=training, mask=mask)
    latents = []
    for e, z in zip(self.ladder_encoder, self.ladder_qz):
      # stochastic bottom-up inference
      qz = z(h, training=training, mask=mask)
      latents.append(qz)
      if self.stochastic_inference:
        h = tf.convert_to_tensor(qz)
        h = e(h, training=training, mask=mask)
      else:
        # deterministic bottom-up inference
        if self.only_mean_up:
          h = qz.mean()
        h = e(h, training=training, mask=mask)
    if only_encoding:
      return h
    qz = self.latents(h,
                      training=training,
                      mask=mask,
                      sample_shape=self.sample_shape)
    latents.append(qz)
    return tuple(latents[::-1])

  def decode(self, latents, training=None, mask=None, only_decoding=False):
    h = tf.convert_to_tensor(latents[0])
    outputs = []
    for d, z in zip(self.ladder_decoder, self.ladder_pz[::-1]):
      h = d(h, training=training, mask=mask)
      pz = z(h, training=training, mask=mask)
      outputs.append(pz)
      h = tf.convert_to_tensor(h)
    h = self.decoder(h, training=training, mask=mask)
    if only_decoding:
      return h
    h = self.observation(h, training=training, mask=mask)
    outputs.append(h)
    return tuple([outputs[-1]] + outputs[:-1])

  def elbo_components(self, inputs, training=None, mask=None, **kwargs):
    llk, kl = super().elbo_components(inputs=inputs,
                                      mask=mask,
                                      training=training)
    P, Q = self.last_outputs
    ### KL
    Qz, Pz = Q[1:], P[1:]
    for q, p, z in zip(Qz, Pz, self.ladder_qz):
      if self.all_standard_prior:
        for name, dist in [('q', i) for i in as_tuple(q)
                          ] + [('p', i) for i in as_tuple(p)]:
          kl[f'kl{name}_{z.name}'] = self.beta * dist.KL_divergence(
              analytic=self.analytic, reverse=self.reverse)
      else:
        kl[f'kl_{z.name}'] = self.beta * kl_divergence(
            q, p, analytic=self.analytic, reverse=self.reverse)
    return llk, kl

  def __str__(self):
    text = super().__str__()

    text += f"\n LadderEncoder:\n  "
    for i, layers in enumerate(self.ladder_encoder):
      text += "\n  ".join(str(layers).split('\n'))
      text += "\n  "

    text = text[:-3] + f"\n LadderDecoder:\n  "
    for i, layers in enumerate(self.ladder_decoder):
      text += "\n  ".join(str(layers).split('\n'))
      text += "\n  "

    text = text[:-3] + f"\n LadderLatents:\n  "
    for i, layers in enumerate(self.ladder_qz):
      text += "\n  ".join(str(layers).split('\n'))
      text += "\n  "
    return text[:-3]


# ===========================================================================
# Ladder VAE
# ===========================================================================
class LadderMergeDistribution(DistributionLambda):
  """ Merge two Gaussian based on weighed variance

  https://github.com/casperkaae/LVAE/blob/066858a3fb53bb1c529a6f12ae5afb0955722845/run_models.py#L106
  """

  def __init__(self, name='LadderMergeDistribution'):
    super().__init__(make_distribution_fn=LadderMergeDistribution.new,
                     name=name)

  @staticmethod
  def new(dists):
    q_e, q_d = dists
    mu_e = q_e.mean()
    mu_d = q_d.mean()
    prec_e = 1 / q_e.variance()
    prec_d = 1 / q_d.variance()
    mu = (mu_e * prec_e + mu_d * prec_d) / (prec_e + prec_d)
    scale = tf.math.sqrt(1 / (prec_e + prec_d))
    dist = Independent(Normal(loc=mu, scale=scale), reinterpreted_batch_ndims=1)
    dist.KL_divergence = q_d.KL_divergence
    return dist


class ladderVAE(stackedVAE):
  """ The ladder variational autoencoder

  Similar to hierarchical VAE with 2 improvements:

  - Deterministic bottom-up inference
  - Merge q(Z|X) Gaussians based-on weighed variance


  Parameters
  ----------
  ladder_encoder : List[Union[Layer, NetConf]], optional
      the mapping layers between latents in the encoding part
  ladder_decoder : List[Union[Layer, NetConf]], optional
      the mapping layers between latents in the decoding part
  ladder_units : List[Union[Layer, RVmeta]], optional
      number of hidden units for stochastic latents

  References
  ----------
  Sønderby, C.K., Raiko, T., Maaløe, L., Sønderby, S.K., Winther, O., 2016.
    Ladder variational autoencoders, Advances in Neural Information Processing Systems.
    Curran Associates, Inc., pp. 3738–3746.
  https://github.com/casperkaae/LVAE
  """

  def __init__(self,
               merge_gaussians: bool = True,
               name: str = 'LadderVAE',
               **kwargs):
    super().__init__(stochastic_inference=False, name=name, **kwargs)
    self.ladder_merge = LadderMergeDistribution()
    self.merge_gaussians = bool(merge_gaussians)

  def decode(self, latents, training=None, mask=None, only_decoding=False):
    h = tf.convert_to_tensor(latents[0])
    outputs = []
    for d, z, qz_e in zip(self.ladder_decoder, self.ladder_pz[::-1],
                          latents[1:]):
      h = d(h, training=training, mask=mask)
      pz = z(h, training=training, mask=mask)
      if self.merge_gaussians:
        qz = self.ladder_merge([pz, qz_e])
      else:
        qz = qz_e
      # ladder_share_params=True
      outputs.append((qz, pz))
      h = tf.convert_to_tensor(qz)
    # final decoder
    h = self.decoder(h, training=training, mask=mask)
    if only_decoding:
      return h
    h = self.observation(h, training=training, mask=mask)
    outputs.append(h)
    return tuple([outputs[-1]] + outputs[:-1])

  def elbo_components(self, inputs, training=None, mask=None):
    llk, kl = super(stackedVAE, self).elbo_components(inputs=inputs,
                                                      mask=mask,
                                                      training=training)
    P, Q = self.last_outputs
    for (qz, pz), lz in zip(P[1:], self.ladder_qz[::-1]):
      if self.all_standard_prior:
        kl[f'kl_{lz.name}'] = self.beta * qz.KL_divergence(
            analytic=self.analytic, reverse=self.reverse)
      else:
        # z = tf.convert_to_tensor(qz) # sampling
        # kl[f'kl_{lz.name}'] = self.beta * (qz.log_prob(z) - pz.log_prob(z))
        kl[f'kl_{lz.name}'] = self.beta * kl_divergence(
            qz, pz, analytic=self.analytic, reverse=self.reverse)
    return llk, kl
