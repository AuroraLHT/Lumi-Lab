"""The search over growth conditions, with the growth database as its memory.

Replaces `GPManager` from ~/HZO_PLD/OpCode/src/manager.py. Same job -- hold the
observations, fit the GP, propose the next conditions -- with one structural change:
the observations are not a CSV any more.

`GPManager` kept `gp_db/<project>.csv` beside the notebook and `add_data_point` wrote
to it. That file was the only link between a growth and its metric, it lived on
whichever laptop ran the notebook, and keeping it in step with the growth database was
manual. Here `refresh()` pulls the observations from `list_measurements` over the
contract, so the training set is whatever the lab actually recorded, from any machine,
and a campaign can be resumed on a different one.

The GP itself is untouched -- see lumi.opt.gp, ported as-is from the code that ran the
real campaigns.

Needs the `opt` extra (torch, gpytorch).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from lumi.contracts.payloads.experiment import ListMeasurements
from lumi.opt.gp import ActiveLearningWrapper, get_grid_X
from lumi.opt.preprocess import RHEEDPreprocessor

log = logging.getLogger(__name__)


class GrowthCampaign:
    """A Bayesian-optimisation campaign over PLD growth conditions.

    `axes` maps a condition name to its search range, exactly as the notebooks wrote
    it:

        {"Pressure":    {"min": 3e-3, "max": 1e-1, "do_log10": False, "num": 101},
         "Temperature": {"min": 500,  "max": 700,  "do_log10": False, "num": 101}}

    The names are the keys `list_measurements` returns in `conditions`, so what the GP
    regresses on and what the chamber was actually asked for cannot drift apart.
    """

    def __init__(
        self,
        al: ActiveLearningWrapper,
        axes: dict[str, dict],
        *,
        metric_kind: str = "rheed_metric",
        y_column: str = "metric",
        do_mean_scale: bool = False,
        snapshot_folder: Path | str | None = None,
    ) -> None:
        self.al = al
        self.axes = axes
        self.metric_kind = metric_kind
        self.x_columns = list(axes)
        self.y_column = y_column
        self.preprocessor = RHEEDPreprocessor(
            axes, {"name": y_column, "do_mean_scale": do_mean_scale}
        )
        self.snapshot_folder = Path(snapshot_folder) if snapshot_folder else None
        if self.snapshot_folder:
            self.snapshot_folder.mkdir(parents=True, exist_ok=True)

        self.db = pd.DataFrame(columns=[*self.x_columns, y_column, "sample_id"])
        #: Points already proposed or grown, excluded from the next proposal. Kept as
        #: a mask over the test grid rather than by deleting rows, so the GP still
        #: predicts there -- you want the posterior at a point you have measured.
        self._masked: set[int] = set()
        self._test_x_raw, self._test_x_norm = self._build_grid()

    # --- the search grid -------------------------------------------------------

    def _build_grid(self) -> tuple[torch.Tensor, torch.Tensor]:
        axes_raw = []
        for cfg in self.axes.values():
            if cfg.get("do_log10"):
                axis = torch.logspace(
                    np.log10(cfg["min"]), np.log10(cfg["max"]), cfg.get("num", 101)
                )
            else:
                axis = torch.linspace(cfg["min"], cfg["max"], cfg.get("num", 101))
            axes_raw.append(axis)

        raw = get_grid_X(*axes_raw)
        frame = pd.DataFrame(raw.numpy(), columns=self.x_columns)
        norm = torch.tensor(
            self.preprocessor.normalize_xs(frame)[self.x_columns].values, dtype=torch.float32
        )
        return raw, norm

    def forbid(self, column: str, low: float, high: float) -> int:
        """Exclude a band of one axis from being proposed.

        The HZO campaigns used this for a pressure window the chamber could not hold
        stably. Returns how many grid points were removed from consideration.
        """
        index = self.x_columns.index(column)
        values = self._test_x_raw[:, index]
        hit = torch.where((values >= low) & (values <= high))[0].tolist()
        self._masked.update(hit)
        return len(hit)

    # --- observations ----------------------------------------------------------

    async def refresh(self, driver, *, substrate_id: int | None = None) -> pd.DataFrame:
        """Pull the training set from the growth database over the contract.

        `driver` is an ExperimentDriverClient (`session.driver`). Rows missing any
        search axis are skipped rather than imputed -- a growth whose pressure was
        never recorded is not an observation about pressure.
        """
        listing = await driver.list_measurements(
            ListMeasurements(kind=self.metric_kind, substrate_id=substrate_id)
        )

        rows, skipped = [], 0
        for m in listing.measurements:
            if m.value is None or not all(c in m.conditions for c in self.x_columns):
                skipped += 1
                continue
            row = {c: float(m.conditions[c]) for c in self.x_columns}
            row[self.y_column] = float(m.value)
            row["sample_id"] = m.sample_id
            rows.append(row)

        if skipped:
            log.info("skipped %d measurement(s) with no value or incomplete conditions", skipped)
        self.db = pd.DataFrame(rows, columns=[*self.x_columns, self.y_column, "sample_id"])
        return self.db

    # --- proposing -------------------------------------------------------------

    def propose(self, acq_fun: str = "exploitation", *, verbose: bool = False) -> pd.Series:
        """Fit the GP on what is in `db` and return the next conditions to try.

        The returned Series is in **physical units** -- Torr, degC, Hz -- ready to hand
        to a deposition, with `alpha`, `gp_mean` and `gp_std` alongside it.
        """
        if self.db.empty:
            raise RuntimeError(
                "no observations yet -- call refresh() after at least one measurement, "
                "or seed the campaign with random conditions"
            )

        self.al.use_acq_fun(acq_fun)
        normalised = self.preprocessor.normalize_y(self.preprocessor.normalize_xs(self.db))
        train_x = torch.tensor(normalised[self.x_columns].values, dtype=torch.float32)
        train_y = torch.tensor(normalised[self.y_column].values, dtype=torch.float32)

        mask = torch.zeros(len(self._test_x_norm), dtype=torch.bool)
        if self._masked:
            mask[list(self._masked)] = True

        alpha, index, mean, std = self.al.pipeline(
            train_x, train_y, self._test_x_norm, mask, verbose=verbose
        )
        index = int(index)
        self._masked.add(index)

        proposal = pd.Series(
            {c: float(self._test_x_raw[index, i]) for i, c in enumerate(self.x_columns)}
        )
        gp_mean, gp_std = self.preprocessor.denormalize_gp(
            mean[index].item(), std[index].item()
        )
        proposal["alpha"] = float(alpha[index])
        proposal["gp_mean"] = float(gp_mean)
        proposal["gp_std"] = float(gp_std)
        return proposal

    def best(self) -> pd.Series | None:
        """The best growth so far, or None before anything has been measured."""
        if self.db.empty:
            return None
        return self.db.loc[self.db[self.y_column].idxmax()]

    def save_snapshot(self, name: str | int) -> Path | None:
        if self.snapshot_folder is None:
            return None
        folder = self.snapshot_folder / str(name)
        folder.mkdir(parents=True, exist_ok=True)
        self.db.to_csv(folder / "observations.csv", index=False)
        # During the random-seed iterations there is no fitted GP yet -- that is the
        # normal path, not a problem, so it does not deserve a traceback.
        if self.al.model is None:
            log.debug("no GP fitted yet; snapshotted observations only")
        else:
            try:
                self.al.save_gp_model(folder)
            except Exception:
                log.warning("could not snapshot the GP", exc_info=True)
        return folder
