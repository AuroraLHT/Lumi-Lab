"""Bayesian optimisation over growth conditions.

Ported from the code that drove the autonomous HZO campaigns. Needs the `opt` extra:

    uv sync --extra opt

Kept out of the base install because it pulls torch and gpytorch, which the chamber
and camera hosts have no use for -- the same reason `detection` is split out.
"""

from lumi.opt.campaign import GrowthCampaign
from lumi.opt.gp import (
    AcquisitionFunctionEI,
    AcquisitionFunctionMaximumUncertainty,
    AcquisitionFunctionPOI,
    AcquisitionFunctionThompsonSampling,
    AcquisitionFunctionThompsonUncertaintySampling,
    AcquisitionFunctionUCB,
    ActiveLearningWrapper,
    ConvergenceChecker,
    ExactGPModel,
    UCBBetaSchedulerKandasamy,
    UCBBetaSchedulerSrinivas,
)
from lumi.opt.preprocess import RHEEDPreprocessor

__all__ = [
    "AcquisitionFunctionEI",
    "AcquisitionFunctionMaximumUncertainty",
    "AcquisitionFunctionPOI",
    "AcquisitionFunctionThompsonSampling",
    "AcquisitionFunctionThompsonUncertaintySampling",
    "AcquisitionFunctionUCB",
    "ActiveLearningWrapper",
    "ConvergenceChecker",
    "ExactGPModel",
    "GrowthCampaign",
    "RHEEDPreprocessor",
    "UCBBetaSchedulerKandasamy",
    "UCBBetaSchedulerSrinivas",
]
