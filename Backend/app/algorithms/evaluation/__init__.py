"""Offline, independently labelled evaluation; never connected to runtime services."""
from .offline import load_dataset, evaluate, prediction_from_result

__all__ = ['load_dataset', 'evaluate', 'prediction_from_result']
