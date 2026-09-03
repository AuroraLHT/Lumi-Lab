"""Gaussian-process active learning for the growth-condition search.

Ported from ~/HZO_PLD/OpCode/src/gp.py, which drove the autonomous HZO campaigns.
The maths is unchanged -- these are the acquisition functions and the exact-GP
wrapper those runs actually used, and changing their behaviour while porting would
have invalidated the campaign history they produced.

What did change:

- `ActiveLearningWrapper` (v1) is dropped; only V2 was in use, and it is renamed to
  `ActiveLearningWrapper` here since there is no longer a v1 to distinguish it from.
- The debug `print` in the training loop is a `log.debug`, so a 2000-iteration fit
  in a notebook does not emit 2000 lines.
- `matplotlib` is no longer imported: plotting belongs to the notebook, not to the
  optimiser, and importing pyplot at module scope picks a backend as a side effect.
- `save_gp_model` took a `Path` for one call and a str for the next, then used `/`
  on the raw argument; both are coerced now.

Needs the `opt` extra (torch, gpytorch).
"""

from __future__ import annotations

import logging
from pathlib import Path

import gpytorch
import numpy as np
import torch

log = logging.getLogger(__name__)

class BaseAcquisitionFunction:
    def __init__(self):
        pass
    
    def __call__(self, pred, alpha):
        i = torch.argmax(alpha)
        return alpha, i, pred.mean, pred.stddev


class AcquisitionFunctionThompsonUncertaintySampling(BaseAcquisitionFunction):
    def __init__(self, n_samples:int=10):
        assert n_samples > 0, "n_samples must be greater than 0"
        self.n_samples = int(n_samples)

    def __call__(self, pred, mask, train_x, train_y):
        if self.n_samples == 1:
            epsilon = torch.randn( (len(pred.stddev)) )
        else:
            epsilon = torch.randn( (self.n_samples, len(pred.stddev)) ).mean(dim=0)
        # epsilon = torch.randn( (len(pred.stddev)) )
        sampled_uncertainty = epsilon * pred.stddev

        return super().__call__(pred, sampled_uncertainty)
    
    def __str__(self):
        return f"AcquisitionFunctionThompsonUncertaintySampling(n_samples={self.n_samples})"
        # return f"AcquisitionFunctionThompsonUncertaintySampling()"


class AcquisitionFunctionMaximumUncertainty(BaseAcquisitionFunction):
    def __init__(self):
        pass
    
    def __call__(self, pred, mask, train_x, train_y):
        return super().__call__(pred, pred.stddev)
    
    def __str__(self):
        return "AcquisitionFunctionMaximumUncertainty()"


class AcquisitionFunctionThompsonSampling(BaseAcquisitionFunction):
    def __init__(self, n_samples=10):
        self.n_samples = n_samples
    
    def __call__(self, pred, mask, train_x, train_y):
        # 101 * 101 * 101 = 1e6, 1e12, 1e18 bytes. 1000 GB
        # pred.sample() not working because of the memory issue
        epision = torch.randn( (self.n_samples, len(pred.stddev)) ).mean(dim=0)
        sampled_value = pred.mean + epision * pred.stddev

        return super().__call__(pred, sampled_value)
    
    def __str__(self):
        return f"AcquisitionFunctionThompsonSampling(n_samples={self.n_samples})"


class UCBBetaSchedulerSrinivas:
    def __init__(self, search_space_size, lambda_=0.1, scale_factor=1):
        self.search_space_size = search_space_size
        self.lambda_ = lambda_
        self.scale_factor = scale_factor

    def __call__(self, iteration):
        beta = np.sqrt( 2 * np.log(self.search_space_size * np.power(iteration,2) * np.power(np.pi,2) / (6 * self.lambda_) ) )
        return beta / self.scale_factor
    
    def __str__(self):
        return f"UCBBetaSchedulerSrinivas(search_space_size={self.search_space_size}, lambda_={self.lambda_}, scale_factor={self.scale_factor})"

class UCBBetaSchedulerKandasamy:
    def __init__(self, search_space_dimension, scale_factor=1):
        self.search_space_dimension = search_space_dimension
        self.scale_factor = scale_factor
    
    def __call__(self, iteration):
        beta = np.sqrt( 0.2 * self.search_space_dimension * np.log(2*iteration) )
        return beta / self.scale_factor

    def __str__(self):
        return f"UCBBetaSchedulerKandasamy(search_space_dimension={self.search_space_dimension}, scale_factor={self.scale_factor})"

class AcquisitionFunctionUCB(BaseAcquisitionFunction):
    def __init__(self, beta=1):
        """
        beta: the weight of the standard deviation. control the amount of exploration.
        """
        self._beta = beta

    def beta(self, iteration):
        if isinstance(self._beta, UCBBetaSchedulerSrinivas):
            return self._beta(iteration)
        elif isinstance(self._beta, UCBBetaSchedulerKandasamy):
            return self._beta(iteration)
        else:
            return self._beta
    
    def __call__(self, pred, mask, train_x, train_y):
        iteration = train_x.shape[0]
        ucb = pred.mean + self.beta(iteration) * pred.stddev
        ucb[mask] = -torch.inf
        # i = torch.argmax(alpha)
        # return alpha, i, pred.mean, pred.stddev
        return super().__call__(pred, ucb)
    
    def __str__(self):
        if isinstance(self._beta, UCBBetaSchedulerSrinivas):
            beta_str = self._beta.__str__()
        elif isinstance(self._beta, UCBBetaSchedulerKandasamy):
            beta_str = self._beta.__str__()
        else:
            beta_str = str(self._beta)

        return f"AcquisitionFunctionUCB(beta={beta_str})"


class AcquisitionFunctionEI(BaseAcquisitionFunction):
    def __init__(self, psi=0):
        """
        psi: value to control exploration. the higher the more exploration.
        """
        self.psi = psi

    def __call__(self, pred, mask, train_x, train_y):
        """Return the expected improvement.
        
        Arguments
        pred: predictive object from gpytorch
        mask: mask of the test points
        train_x: training data
        train_y: training labels

        reference: https://ekamperi.github.io/machine%20learning/2021/06/11/acquisition-functions.html
        adapted from https://predictivesciencelab.github.io/data-analytics-se/lecture23/hands-on-23.4.html
        equation of EI is: u * Phi(u) + sigma * phi(u)
        where Phi is the cumulative distribution function of the standard normal distribution,
        and phi is the probability density function of the standard normal distribution.
        u is the standardized difference between the mean and the maximum observed value.
        u = (mean - ymax - psi) / sigma
        psi is the exploration parameter, the higher the more exploration.
        """
        ymax = train_y.max()
        m = pred.mean
        sigma = pred.stddev

        diff = m - ymax - self.psi
        u = diff / sigma
        ei = ( diff * torch.distributions.Normal(0, 1).cdf(u) + 
            sigma * torch.distributions.Normal(0, 1).log_prob(u).exp()
        )
        ei[sigma <= 0.] = 0.
        ei[mask] = -torch.inf
        
        # alpha = ei
        # i = torch.argmax(alpha)

        # return alpha, i, pred.mean, pred.stddev
        return super().__call__(pred, ei)

    def __str__(self):
        return f"AcquisitionFunctionEI(psi={self.psi})"
    

class AcquisitionFunctionPOI(BaseAcquisitionFunction):
    def __init__(self, psi=0):
        """
        psi: value to control exploration. the higher the more exploration.
        """
        self.psi = psi

    def __call__(self, pred, mask, train_x, train_y):
        """Return the probability of improvement.
        
        Arguments
        pred: predictive object from gpytorch
        mask: mask of the test points
        train_x: training data
        train_y: training labels

        """
        m = pred.mean
        sigma = pred.stddev
        ymax = train_y.max()

        poi = torch.distributions.Normal(0, 1).cdf((m - ymax - self.psi) / sigma)
        poi[mask] = -torch.inf
        
        return super().__call__(pred, poi)

    def __str__(self):
        return f"AcquisitionFunctionEI(psi={self.psi})"


def get_grid_X(*xs):
    X_grid = torch.meshgrid(*xs, indexing="ij" )    
    X = torch.stack(X_grid , dim=-1 )
    X = X.reshape(-1, len(xs))

    return X

    
# We will use the simplest form of GP model, exact inference
class ExactGPModel(gpytorch.models.ExactGP):
    
    def __init__(self, train_x, train_y, likelihood, mean_module=None, covar_module=None, lengthscale_constraint=None, freeze_mean_module=True):
        super(ExactGPModel, self).__init__(train_x, train_y, likelihood)
        if mean_module is None:
            mean_module = gpytorch.means.ZeroMean()
            # mean_module = gpytorch.means.ConstantMean()
        self.mean_module = mean_module

        # freeze the mean module
        if freeze_mean_module:
            for p in self.mean_module.parameters():
                p.requires_grad_(False)


        if covar_module is None:
            covar_module = gpytorch.kernels.ScaleKernel(gpytorch.kernels.RBFKernel(ard_num_dims=train_x.shape[1], lengthscale_constraint=lengthscale_constraint))
        self.covar_module = covar_module

    def forward(self, x):
        mean_x = self.mean_module(x)
        covar_x = self.covar_module(x)
        return gpytorch.distributions.MultivariateNormal(mean_x, covar_x)

class ConvergenceChecker:
    def __init__(self,  x_columns, y_column, expected_y=None, patience=3, delta_y_abs=0.05, delta_x_propotional=0.02,):
        self.patience = patience
        self.expected_y = expected_y
        self.delta_x_propotional = delta_x_propotional
        self.delta_y_abs = delta_y_abs

        self.best_y = None
        self.best_y_idx = None
        self.best_xs = None

        self.counter = 0
        self.x_columns = x_columns
        self.y_column = y_column

    
    def __call__(self, db, next_point=None):
        db_x = db[self.x_columns].values
        db_y = db[self.y_column].values

        if next_point is not None:
            all_x = np.concatenate([db_x, [next_point.values]], axis=0)
        else:
            all_x = db_x

        self.best_y_idx = db_y.argmax()
        self.best_y = db_y[self.best_y_idx]
        self.best_xs = db_x[self.best_y_idx]

        # last_points = all_x.iloc[-(self.patience+1):-1]
        x_mask = np.all(np.abs(all_x / all_x[-1:] - 1) < self.delta_x_propotional, axis=1)


        # y_mask = db_y > (self.best_y - self.delta_y_abs)
        # x_mask = np.all(np.abs(all_x / self.best_xs - 1) < self.delta_x_propotional, axis=1)

        # self.counter = (y_mask & x_mask).sum() - 1
        self.counter = x_mask.sum() - 1

        if self.counter >= self.patience:
            self.converged = True
        else:
            self.converged = False

        if self.expected_y is not None:
            if self.expected_y is not None and self.best_y >= self.expected_y:
                self.satisfied = True
            else:
                self.satisfied = False
        else:
            self.satisfied = True
        return self.converged, self.satisfied


class ActiveLearningWrapper:

    def __init__(
        self, 
        acq_funs, 
        gp_fit_iter=100, 
        is_percentage=False, 
        gp_lengthscale=None, 
        gp_lengthscale_constraints=None,
        train_step_callback=None, 
        gp_lr=0.1,
        gp_mean_module=None,
    ):
        self.acq_funs = acq_funs
        self._current_acq_fun_name = None

        self.gp_fit_iter=gp_fit_iter
        self.gp_lr=gp_lr
        self.gp_lengthscale = gp_lengthscale
        self.gp_lengthscale_constraints = gp_lengthscale_constraints
        self.gp_mean_module=gp_mean_module
        self.train_step_callback = train_step_callback
        self.is_percentage = is_percentage

        self.model = None
        self.likelihood = None

        self.state = {
            "train_xs": None,
            "train_y": None, 
            "test_xs": None,
            "test_pred": None,           
        }

    @property
    def acq_fun(self):
        if self._current_acq_fun_name is None or self._current_acq_fun_name not in self.acq_funs or self.acq_funs is None:
            return None
        return self.acq_funs[self._current_acq_fun_name]
    
    def use_acq_fun(self, acq_fun_name):
        assert acq_fun_name in self.acq_funs, f"Acquisition function {acq_fun_name} not found"
        self._current_acq_fun_name = acq_fun_name

    def register_acq_fun(self, acq_fun_name, acq_fun):
        assert acq_fun_name not in self.acq_funs, f"Acquisition function {acq_fun_name} already registered"
        self.acq_funs[acq_fun_name] = acq_fun

    def remove_acq_fun(self, acq_fun_name):
        assert acq_fun_name in self.acq_funs, f"Acquisition function {acq_fun_name} not found"
        self.acq_funs.pop(acq_fun_name)
    
    def create_GP_model(self, train_xs : torch.Tensor, train_y: torch.Tensor):
        # initialize likelihood and model
        likelihood = gpytorch.likelihoods.GaussianLikelihood()
        model = ExactGPModel(train_xs, train_y, likelihood, mean_module=self.gp_mean_module, lengthscale_constraint=self.gp_lengthscale_constraints)
        # if self.is_percentage:
        #     model.covar_module.base_kernel.lengthscale = 100 * model.covar_module.base_kernel.lengthscale
        if self.gp_lengthscale is not None:
            self._init_gp_lengthscale = self.gp_lengthscale + float(self.gp_lengthscale/1e2 * torch.randn(1))
            model.covar_module.base_kernel.lengthscale = self._init_gp_lengthscale
        
        self.model = model
        self.likelihood = likelihood
        
        return model, likelihood
            

    def fit_GP_model(self, train_xs: torch.Tensor, train_y: torch.Tensor, training_iter: int, verbose=True):
    
        # Find optimal model hyperparameters
        # print("a", 1, time.time())
        self.model.train()
        self.likelihood.train()
        
        # Use the adam optimizer
        # print("a", 2, time.time())        
        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.gp_lr)  # Includes GaussianLikelihood parameters
        
        # "Loss" for GPs - the marginal log likelihood
        # print("a", 3, time.time())        
        mll = gpytorch.mlls.ExactMarginalLogLikelihood(self.likelihood, self.model)
        
        # print("a", 4, time.time())
        for i in range(training_iter):
            # Zero gradients from previous iteration
            # print("a", 5, time.time())        
            optimizer.zero_grad()
            # Output from model
            # print("a", 6, time.time())        
            output = self.model(train_xs)
            # Calc loss and backprop gradients
            loss = -mll(output, train_y)
            loss.backward()
            # print("a", 7, time.time())    
            if verbose:
                log.debug(
                    "iter %d/%d - loss %.3f  lengthscale %.3f  noise %.3f",
                    i + 1, training_iter, loss.item(),
                    self.model.covar_module.base_kernel.lengthscale.mean(),
                    self.model.likelihood.noise.item(),
                )
            optimizer.step()

            if self.train_step_callback is not None:
               self.train_step_callback(i, training_iter, ) 
    
    def get_GP_pred(self, test_xs: torch.Tensor):
        # Get into evaluation (predictive posterior) mode
        self.model.eval()
        self.likelihood.eval()
        
        # Test points are regularly spaced along [0,1]
        # Make predictions by feeding model through likelihood
        with torch.no_grad(), gpytorch.settings.fast_pred_var():
            observed_pred = self.likelihood(self.model(test_xs))
            self.observed_pred = observed_pred
        return observed_pred

    def drop_linear_dependent_xs(self, train_xs: torch.Tensor, train_y: torch.Tensor, threshold=0.98):
        # drop linear dependent xs
        corr_matrix = torch.corrcoef(train_xs)
        selected = []
        for i in range(corr_matrix.size(0)):
            selected.append(tuple(torch.where(corr_matrix[i] > 0.98)[0].tolist()))
        selected = list(set(selected))

        idx = sorted( [ np.random.choice(s) for s in selected ] )

        return train_xs[idx], train_y[idx]


    def pipeline(self, xs: torch.Tensor, y: torch.Tensor, test_xs: torch.Tensor, mask: torch.Tensor, verbose=False, auto_train=True, snapshot_path=None):
        # print(1, time.time())
        test_xs = test_xs.to(torch.float32)
        self.state['test_xs'] = test_xs

        # print(2, time.time())
        if snapshot_path is not None:
            self.load_gp_model(snapshot_path)

        elif auto_train:
            train_xs = xs.to(torch.float32)
            train_y = y.to(torch.float32)

            if self.is_percentage:
                train_xs = train_xs / 100
                test_xs = test_xs / 100
                # print("train_xs.max()", train_xs.max())
                # print("test_xs.max()", test_xs.max())

            training_success = False
            try:
                self.state['train_xs'] = train_xs
                self.state['train_y'] = train_y
                model, likelihood = self.create_GP_model(train_xs, train_y)
                self.fit_GP_model(train_xs, train_y, self.gp_fit_iter, verbose=verbose)
                training_success = True
            except Exception as e:
                log.warning("GP fit failed (%s); retrying without linearly dependent points", e)
                for i in range(20):
                    try:
                        _train_xs, _train_y = self.drop_linear_dependent_xs(train_xs, train_y)
                        self.state['train_y'] = _train_y
                        self.state['train_xs'] = _train_xs
                        model, likelihood = self.create_GP_model(_train_xs, _train_y)
                        self.fit_GP_model(_train_xs, _train_y, self.gp_fit_iter, verbose=verbose)
                        training_success = True
                        break
                    except Exception as e:
                        log.warning("retry %d failed: %s", i, e)
                        continue
                if not training_success:
                    raise Exception("Training failed")

        assert self.model is not None and self.likelihood is not None, "model or likelihood is not initialized"

        # print(4, time.time())
        observed_pred = self.get_GP_pred(test_xs)
        # print(5, time.time())
        self.state['test_pred'] = observed_pred

        return self.acq_fun(observed_pred, mask, self.state['train_xs'], self.state['train_y'])
    
    def save_gp_model(self, save_folder: Path | str):
        save_folder = Path(save_folder)
        torch.save(self.state, save_folder / "model_state.pth")
        torch.save(self.model.state_dict(), save_folder / "model.pth")
        torch.save(self.likelihood.state_dict(), save_folder / "likelihood.pth")

    def load_gp_model(self, load_folder: Path | str):
        load_folder = Path(load_folder)
        self.state = torch.load(load_folder / "model_state.pth")
        self.create_GP_model(self.state['train_xs'], self.state['train_y'])

        if hasattr(self, "model") and self.model is not None:
            self.model.load_state_dict(torch.load(Path(load_folder) / "model.pth"))
        else:
            raise ValueError("model is not initialized")
        if hasattr(self, "likelihood") and self.likelihood is not None:
            self.likelihood.load_state_dict(torch.load(Path(load_folder) / "likelihood.pth"))
        else:
            raise ValueError("likelihood is not initialized")

    def summarize(self):
        return {
            "gp_lr": self.gp_lr,
            "gp_lengthscale": self.gp_lengthscale,
            "gp_fit_iter": self.gp_fit_iter,
            "is_percentage": self.is_percentage,
            "acq_fun": str(self.acq_fun),
        }