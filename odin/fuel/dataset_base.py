from typing import Dict, List, Optional, Union

import numpy as np
import tensorflow as tf
from typing_extensions import Literal


# ===========================================================================
# Helpers
# ===========================================================================
def get_partition(part,
                  train=None,
                  valid=None,
                  test=None,
                  unlabeled=None,
                  unlabelled=None,
                  **kwargs):
  r""" A function for automatically select the right data partition """
  part = str(part).lower().strip()
  ret = None
  if 'train' in part:
    ret = train
  elif 'valid' in part:
    ret = valid
  elif 'test' in part:
    ret = test
  elif 'unlabeled' in part or 'unlabelled' in part:
    ret = unlabeled if unlabelled is None else unlabelled
  for k, v in kwargs.items():
    if part == str(k).strip().lower():
      ret = v
      break
  if ret is None:
    raise ValueError("No data for partition with name: '%s'" % part)
  return ret


def _merge_list(data):
  return [
      np.concatenate([x[i].numpy()
                      for x in data], axis=0)
      for i in range(len(data[0]))
  ]


def _merge_dict(data):
  data = {k: [x[k] for x in data] for k in data[0].keys()}
  ret = {}
  for k, v in data.items():
    if tf.is_tensor(v[0]):
      ret[k] = _merge_tensor(v)
    elif isinstance(v[0], (tuple, list)):
      ret[k] = _merge_list(v)
    else:
      ret[k] = _merge_dict(v)
  return ret


def _merge_tensor(data):
  return np.concatenate(data, axis=0)


# ===========================================================================
# Main
# ===========================================================================
class IterableDataset:

  @property
  def data_type(self) -> Literal['image', 'audio', 'text', 'gene']:
    raise NotImplementedError

  @property
  def name(self) -> str:
    return self.__class__.__name__.lower()

  @property
  def has_labels(self) -> bool:
    return self.n_labels > 0

  @property
  def n_labels(self) -> int:
    return len(self.labels)

  @property
  def labels_indices(self) -> Dict[str, int]:
    if not hasattr(self, "_labels_indices"):
      self._labels_indices = {j: i for i, j in enumerate(self.labels)}
    return self._labels_indices

  @property
  def labels(self) -> List[str]:
    return np.array([])

  @property
  def shape(self) -> List[int]:
    """Return shape of single example (i.e. no batch dimension)"""
    raise NotImplementedError()

  @property
  def full_shape(self) -> List[Union[None, int]]:
    """Return the shape with batch dimension"""
    return (None,) + tuple([i for i in self.shape])

  @property
  def is_binary(self) -> bool:
    raise NotImplementedError()

  def create_dataset(self,
                     partition: Literal['train', 'valid', 'test',
                                        'unlabelled'] = 'train',
                     *,
                     batch_size: Optional[int] = 32,
                     drop_remainder: bool = False,
                     shuffle: int = 1000,
                     prefetch: int = tf.data.experimental.AUTOTUNE,
                     cache: str = '',
                     parallel: Optional[int] = None,
                     inc_labels: bool = False,
                     seed: int = 1) -> tf.data.Dataset:
    """ Create tensorflow Dataset """
    raise NotImplementedError()

  def numpy(self,
            batch_size: int = 32,
            drop_remainder: bool = False,
            shuffle: int = 1000,
            prefetch: int = tf.data.experimental.AUTOTUNE,
            cache: str = '',
            parallel: Optional[int] = None,
            partition: Literal['train', 'valid', 'test',
                               'unlabelled'] = 'train',
            inc_labels: bool = False,
            seed: int = 1,
            verbose: bool = False):
    """Return the numpy data returned when iterate the partition"""
    kw = dict(locals())
    kw.pop('self', None)
    verbose = kw.pop('verbose')
    ds = self.create_dataset(**kw)
    # load the data
    if verbose:
      from tqdm import tqdm
      ds = tqdm(ds, desc='Converting dataset to numpy')
    data = [x for x in ds]
    # post-process the data
    if isinstance(data[0], (tuple, list)):
      data = _merge_list(data)
    elif tf.is_tensor(data[0]):
      data = _merge_tensor(data)
    elif isinstance(data[0], dict):
      data = _merge_dict(data)
    else:
      raise NotImplementedError(f'{type(data[0])}')
    return data
