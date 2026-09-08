"""Scaling between physical growth conditions and the GP's unit cube.

Ported verbatim from ~/HZO_PLD/OpCode/src/preprocess.py, minus a commented-out
duplicate of normalize_xs's body that sat after an unreachable `return`.

The GP works in [0, 1] per axis; the chamber works in Torr, degC and Hz. Pressure
spans 3e-3 to 1e-1 Torr, so it is scaled in log10 -- `do_log10` on that axis is the
difference between a length scale that means something across the range and one
dominated by the top decade.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

class BasePreprocessor(ABC):
    
    @abstractmethod
    def normalize_xs(self, db):
        """Normalize input features and target values"""
        pass
    
    @abstractmethod
    def normalize_y(self, db):
        """Normalize target values"""
        pass
    
    @abstractmethod
    def denormalize_xs(self, db):
        """Denormalize input features and target values back to original scale"""
        pass

    @abstractmethod
    def denormalize_y(self, db):
        """Denormalize target values back to original scale"""
        pass

    @abstractmethod
    def denormalize_gp(self, gp_mean, gp_std):
        """Denormalize GP mean and std back to original scale"""
        pass


class RHEEDPreprocessor(BasePreprocessor):
    def __init__(self, xs_configs: dict, y_configs: dict):
        self.xs_configs = xs_configs
        self.y_configs = y_configs
        self._y_state = {}

    def min_max_scale(self, xs, min_v, max_v, do_log10=False):
        if do_log10:
            xs = np.log10(xs)
            min_v = np.log10(min_v)
            max_v = np.log10(max_v)
        return (xs - min_v) / (max_v - min_v)

    def mean_scale(self, xs):
        mean_v = xs.mean()
        std_v = xs.std()
        return (xs - mean_v) / std_v, mean_v, std_v

    def reverse_min_max_scale(self, xs, min_v, max_v, do_log10=False):
        if do_log10:
            min_v = np.log10(min_v)
            max_v = np.log10(max_v)
        out = xs * (max_v - min_v) + min_v 
        if do_log10:
            out = np.power(10, out)
        return out

    def reverse_mean_scale(self, xs, mean_v, std_v):
        return xs * std_v + mean_v

    def normalize_xs(self, db: pd.DataFrame):
        _db = db.copy()

        for key, value in self.xs_configs.items():
            _db[key] = self.min_max_scale(
                db[key].values, 
                value['min'], 
                value['max'],
                do_log10=value['do_log10'])
        return _db
    
    def normalize_y(self, db: pd.DataFrame):
        _db = db.copy()

        key = self.y_configs['name']
        if key in db and self.y_configs['do_mean_scale']:
            _db[key], mean_v, std_v = self.mean_scale(db[key].values)
            self._y_state = {'mean': mean_v, 'std': std_v}
        
        return _db


    def denormalize_xs(self, db):
        """Denormalize input features and target values back to original scale"""
        pass

    @abstractmethod
    def denormalize_y(self, db):
        """Denormalize target values back to original scale"""
        pass

    @abstractmethod
    def denormalize_gp(self, gp_mean, gp_std):
        """Denormalize GP mean and std back to original scale"""
        pass


class RHEEDPreprocessor(BasePreprocessor):
    def __init__(self, xs_configs: dict, y_configs: dict):
        self.xs_configs = xs_configs
        self.y_configs = y_configs
        self._y_state = {}

    def min_max_scale(self, xs, min_v, max_v, do_log10=False):
        if do_log10:
            xs = np.log10(xs)
            min_v = np.log10(min_v)
            max_v = np.log10(max_v)
        return (xs - min_v) / (max_v - min_v)

    def mean_scale(self, xs):
        mean_v = xs.mean()
        std_v = xs.std()
        return (xs - mean_v) / std_v, mean_v, std_v

    def reverse_min_max_scale(self, xs, min_v, max_v, do_log10=False):
        if do_log10:
            min_v = np.log10(min_v)
            max_v = np.log10(max_v)
        out = xs * (max_v - min_v) + min_v 
        if do_log10:
            out = np.power(10, out)
        return out

    def reverse_mean_scale(self, xs, mean_v, std_v):
        return xs * std_v + mean_v

    def normalize_xs(self, db: pd.DataFrame):
        _db = db.copy()

        for key, value in self.xs_configs.items():
            _db[key] = self.min_max_scale(
                db[key].values, 
                value['min'], 
                value['max'],
                do_log10=value['do_log10'])
        return _db
    
    def normalize_y(self, db: pd.DataFrame):
        _db = db.copy()

        key = self.y_configs['name']
        if key in db and self.y_configs['do_mean_scale']:
            _db[key], mean_v, std_v = self.mean_scale(db[key].values)
            self._y_state = {'mean': mean_v, 'std': std_v}
        
        return _db

        # _db['Pressure'] = self.min_max_scale( 
        #     db['Pressure'].values, 
        #     self.xs_range['Pressure']['min'], 
        #     self.xs_range['Pressure']['max'], 
        #     do_log10=True
        # )
        # _db['Temperature'] = self.min_max_scale(
        #     db['Temperature'].values, 
        #     self.xs_range['Temperature']['min'], 
        #     self.xs_range['Temperature']['max']
        # )
        # _db['Laser Pulse Rate'] = self.min_max_scale( 
        #     db['Laser Pulse Rate'].values, 
        #     self.xs_range['Laser Pulse Rate']['min'], 
        #     self.xs_range['Laser Pulse Rate']['max'], 
        #     do_log10=True
        # )
        # _db['Laser Power'] = self.min_max_scale(
        #     db['Laser Power'].values, 
        #     self.xs_range['Laser Power']['min'], 
        #     self.xs_range['Laser Power']['max']
        # )
        return _db

    def denormalize_xs(self, db: pd.DataFrame):
        _db = db.copy()
        for key, value in self.xs_configs.items():
            _db[key] = self.reverse_min_max_scale(
                db[key].values, 
                value['min'], 
                value['max'], 
                do_log10=value['do_log10'])
        return _db

    def denormalize_y(self, db: pd.DataFrame):
        _db = db.copy()
        key = self.y_configs['name']
        if key in db and key in self._y_state and self.y_configs['do_mean_scale']:
            _db[key] = self.reverse_mean_scale(db[key].values, self._y_state['mean'], self._y_state['std'])
        return _db

    def denormalize_gp(self, gp_mean, gp_std, keys=None):
        if self.y_configs['do_mean_scale']:
            gp_mean = self.reverse_mean_scale(gp_mean, self._y_state['mean'], self._y_state['std'])
            gp_std = self.reverse_mean_scale(gp_std, self._y_state['std'], self._y_state['std'])
        
        return gp_mean, gp_std