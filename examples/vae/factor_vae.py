from __future__ import absolute_import, division, print_function

import os
from functools import partial

import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
import tensorflow_probability as tfp
from matplotlib import pyplot as plt
from scipy.stats import describe
from tensorflow.python import keras
from tqdm import tqdm

from odin import backend as bk
from odin import bay, networks
from odin import visual as vs
from odin.backend import Trainer, interpolation
from odin.fuel import Shapes3D
from odin.utils import ArgController, as_tuple

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_FORCE_GPU_ALLOW_GROWTH'] = 'true'

tf.random.set_seed(8)
np.random.seed(8)

args = ArgController(
).add('-zdim', 'Number of latent dim, 5 for dspries, 6 for shapes3d', 6\
).add('-gamma', 'Gamma >0 turn on FactorVAE', 1\
).add('-beta', 'Beta > 0 BetaVAE', 1\
).add('-interpolation', 'Type of interpolation for AnnealVAE', 'const'\
).add('-ds', 'name of dataset: shapes3d, dsprites', 'shapes3d'\
).parse()
# ===========================================================================
# Configs
# - Color: white
# - Shape: square, ellipse, heart
# - Scale: 6 values linearly spaced in [0.5, 1]
# - Orientation: 40 values in [0, 2pi]
# - Position X: 32 values in [0, 1]
# - Position Y: 32 values in [0, 1]
# ===========================================================================
ZDIM = int(args.zdim)
GAMMA = int(args.gamma)
BETA = int(args.beta)
BATCH_SIZE = 64
DS = args.ds
SUMMARY_STEPS = [100, 20]

SAVE_DIR = os.path.expanduser('~/exp')
if not os.path.exists(SAVE_DIR):
  os.mkdir(SAVE_DIR)
SAVE_PATH = os.path.join(
    SAVE_DIR,
    'vae_%s_%s_%s_%d' % (DS.lower(), 'gamma%d' % GAMMA, 'beta%d' % BETA, ZDIM))
if not os.path.exists(SAVE_PATH):
  os.mkdir(SAVE_PATH)

BETA = interpolation.get(name=args.interpolation)(vmin=0.,
                                                  vmax=float(BETA),
                                                  cyclical=True,
                                                  norm=100,
                                                  delayOut=10)

# ===========================================================================
# Loading data
# ===========================================================================
if DS == 'dsprites':
  train, valid, test = tfds.load(
      'dsprites:2.0.0', split=['train[:70%]', 'train[70%:80%]', 'train[80%:]'])
  prepare = partial(Trainer.prepare,
                    postprocess=lambda x: tf.cast(x['image'], tf.float32),
                    batch_size=BATCH_SIZE,
                    parallel_postprocess=tf.data.experimental.AUTOTUNE,
                    drop_remainder=True)
  input_spec = tf.data.experimental.get_structure(train)['image']
  input_shape = input_spec.shape
  x_valid = tf.concat([
      tf.expand_dims(tf.cast(x['image'], tf.float32), axis=0)
      for x in valid.take(16)
  ],
                      axis=0)
  x_valid = tf.squeeze(x_valid, axis=-1)
else:
  shapes3D = Shapes3D()
  train, valid, test = shapes3D.create_dataset(batch_size=BATCH_SIZE,
                                               return_mode=0)
  prepare = lambda ds, epochs=1, shuffle=False: \
    ds.shuffle(1000).repeat(epochs) if shuffle else ds.repeat(epochs)
  input_spec = tf.data.experimental.get_structure(train)
  input_shape = input_spec.shape[1:]
  x_valid = [x for x in train.take(1)][0][:16]

# input description
input_dtype = input_spec.dtype
plt.figure(figsize=(12, 12))
for i, x in enumerate(x_valid.numpy()):
  plt.subplot(4, 4, i + 1)
  plt.imshow(x, cmap='Greys_r' if x.ndim == 2 else None)
  plt.axis('off')
plt.tight_layout()
vs.plot_save(os.path.join(SAVE_PATH, 'x.pdf'))


# ===========================================================================
# Create Model
# ===========================================================================
class FactorDiscriminator(keras.Model):

  def __init__(self, zdim, hdim, nlayer):
    super().__init__()
    # 1: real(z) and 0: fake(z_perm)
    self.discriminator = networks.DenseNetwork(
        units=[hdim] * nlayer,
        activation=tf.nn.leaky_relu,
        batchnorm=True,
        flatten=True,
        end_layers=[keras.layers.Dense(2, activation='linear')],
        input_shape=(zdim,),
        name="Discriminator")

  @tf.function
  def permute_dims(z):
    perm = tf.TensorArray(dtype=z.dtype,
                          size=z.shape[1],
                          dynamic_size=False,
                          clear_after_read=False,
                          element_shape=(z.shape[0],))
    ids = tf.range(z.shape[0], dtype=tf.int32)
    for i in tf.range(z.shape[1]):
      z_i = tf.gather(z[:, i], tf.random.shuffle(ids))
      perm = perm.write(i, z_i)
    return tf.transpose(perm.stack())

  def tc(self, z, training=None):
    r""" Total correlation Eq(3)
      We want to minimize the total-correlation of the latents """
    d_z = self.discriminator(z, training=training)
    return tf.reduce_mean(d_z[:, 0] - d_z[:, 1])

  def dtc(self, z, training=None):
    r""" Discriminated total correlation loss """
    n = z.shape[0]
    # we don't want the gradient to be propagated to the encoder
    z = tf.stop_gradient(z)
    z_perm = FactorDiscriminator.permute_dims(z)
    d_z = self.discriminator(z, training=training)[:, 0]
    d_zperm = self.discriminator(z_perm, training=training)[:, 1]
    loss = 0.5 * tf.reduce_mean(
        tf.math.log_sigmoid(d_z) + tf.math.log_sigmoid(d_zperm))
    return loss

  def call(self, inputs, training=None, sigmoid=False):
    outputs = self.discriminator(inputs, training=training)
    if sigmoid:
      outputs = tf.nn.sigmoid(outputs)
    return outputs


class FactorVAE(keras.Model):

  def __init__(self, input_shape, zdim, posterior='bernoulli'):
    super().__init__()
    self.encoder = networks.ConvNetwork(filters=[32, 32, 64, 64],
                                        rank=2,
                                        kernel_size=[4, 4, 4, 4],
                                        strides=[2, 2, 2, 2],
                                        end_layers=[
                                            keras.layers.Flatten(),
                                            keras.layers.Dense(
                                                256, activation='linear')
                                        ],
                                        input_shape=input_shape)
    encoder_shape = self.encoder.layers[-3].output.shape[1:]
    self.decoder = networks.DeconvNetwork(
        filters=[64, 64, 32, 32, input_shape[-1]],
        rank=2,
        kernel_size=[4, 4, 4, 4, 4],
        strides=[1, 2, 2, 2, 2],
        activation=['relu'] * 4 + ['linear'],
        start_layers=[
            keras.layers.Dense(256, activation='relu'),
            keras.layers.Dense(int(np.prod(encoder_shape)), activation='relu'),
            keras.layers.Reshape(encoder_shape),
        ],
        end_layers=[keras.layers.Flatten()],
        input_shape=(zdim,))
    self.qZ = bay.layers.DenseDistribution(
        event_shape=zdim,
        posterior='normaldiag',
        prior=tfp.distributions.MultivariateNormalDiag(
            loc=tf.zeros(shape=(zdim,)), scale_identity_multiplier=1.))
    self.pX = bay.layers.DenseDistribution(event_shape=input_shape,
                                           posterior=posterior)

  def sample(self, sample_shape=(), seed=8):
    r""" Sample from latent prior distribution """
    return self.qZ.sample(sample_shape, seed=seed)

  def decode(self, Z):
    r""" Decode the latent samples """
    D = self.decoder(Z, training=False)
    return self.pX(D, training=False)

  def call(self, inputs, training=None):
    E = self.encoder(inputs, training=training)
    qZ = self.qZ(E, training=training)
    Z = tf.squeeze(qZ, axis=0)  # remove mcmc dimension
    D = self.decoder(Z, training=training)
    pX = self.pX(D, training=training)
    return pX, qZ


# ===========================================================================
# Create the network
# ===========================================================================
vae = FactorVAE(input_shape, zdim=ZDIM)
discriminator = FactorDiscriminator(zdim=ZDIM, hdim=1000, nlayer=6)
opt = tf.optimizers.Adam(learning_rate=10e-5,
                         beta_1=0.9,
                         beta_2=0.999,
                         epsilon=1e-07,
                         amsgrad=False)
z_samples = vae.sample(16)
z_animation = vs.Animation(figsize=(12, 12))
x_animation = vs.Animation(figsize=(12, 12))


# ===========================================================================
# Training
# ===========================================================================
def optimize(X, tape, n_iter, training):
  pX, qZ = vae(X, training=training)
  z = tf.squeeze(qZ, axis=0)
  beta = 1 if not training else BETA(n_iter)
  gamma = 1 if not training else GAMMA

  llk = pX.log_prob(X)
  kl = qZ.KL_divergence(analytic=True)
  if gamma > 0:
    total_correlation = discriminator.tc(z, training=training)
  else:
    total_correlation = 0.
  # ELBO = tf.reduce_mean(tf.reduce_logsumexp(llk - beta * kl, axis=0))
  ELBO = tf.reduce_mean(llk - beta * kl - gamma * total_correlation)
  loss = -ELBO
  Trainer.apply_gradients(tape, opt, loss, vae)

  # optimize the discriminator
  if gamma > 0:
    dtc_loss = discriminator.dtc(z, training=training)
    Trainer.apply_gradients(tape, opt, dtc_loss, discriminator)
  else:
    dtc_loss = 0.

  return loss, dict(llk=tf.reduce_mean(llk),
                    kl=tf.reduce_mean(kl),
                    tc=tf.reduce_mean(total_correlation),
                    dtc=dtc_loss,
                    beta=beta,
                    gamma=gamma)


def callback():
  signal = Trainer.early_stop(trainer.valid_loss, threshold=0.25, verbose=1)
  if signal == Trainer.SIGNAL_BEST:
    Trainer.save_checkpoint(os.path.join(SAVE_PATH, 'model'),
                            optimizer=opt,
                            models=vae,
                            trainer=trainer)

  z_animation.plot_images(vae.decode(z_samples).mean())
  z_animation.save(os.path.join(SAVE_PATH, 'z.gif'))

  x_animation.plot_images(vae(x_valid)[0].mean())
  x_animation.save(os.path.join(SAVE_PATH, 'x.gif'))

  trainer.plot_learning_curves(os.path.join(SAVE_PATH, 'learning_curves.pdf'),
                               summary_steps=SUMMARY_STEPS)


trainer = Trainer()
trainer.fit(prepare(train, epochs=80, shuffle=True),
            optimize,
            valid_ds=prepare(valid),
            valid_freq=1000,
            max_iter=50000,
            callback=callback)
trainer.plot_learning_curves(os.path.join(SAVE_PATH, 'learning_curves.pdf'),
                             summary_steps=SUMMARY_STEPS)
