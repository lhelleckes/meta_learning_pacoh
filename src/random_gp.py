import copy
import gpytorch
import torch
import math
from pyro.distributions import Normal, LogNormal, Independent
from collections import OrderedDict

from src.models import LearnedGPRegressionModel, ConstantMeanLight, SEKernelLight, GaussianLikelihoodLight, \
    VectorizedModel, CatDist, NeuralNetworkVectorized
from config import device


def _filter(dict, str):
    result = OrderedDict()
    for key, val in dict.items():
        if str in key:
            result[key] = val
    return result


class VectorizedGP(VectorizedModel):

    def __init__(self, input_dim, feature_dim=2, covar_module_str='SE', mean_module_str='constant',
                 mean_nn_layers=(32, 32), kernel_nn_layers=(32, 32), nonlinearlity=torch.tanh):
        super().__init__(input_dim, 1)


        self._params = OrderedDict()
        self.mean_module_str = mean_module_str
        self.covar_module_str = covar_module_str

        if mean_module_str == 'NN':
            self.mean_nn = self._param_module('mean_nn', NeuralNetworkVectorized(input_dim, 1,
                                                         layer_sizes=mean_nn_layers, nonlinearlity=nonlinearlity))
        elif mean_module_str == 'constant':
            self.constant_mean = self._param('constant_mean', torch.zeros(1, 1))
        else:
            raise NotImplementedError


        if covar_module_str == "NN":
            self.kernel_nn = self._param_module('kernel_nn', NeuralNetworkVectorized(input_dim, feature_dim,
                                                        layer_sizes=kernel_nn_layers, nonlinearlity=nonlinearlity))
            self.lengthscale = self._param('lengthscale', torch.ones(1, feature_dim))
        elif covar_module_str == 'SE':
            self.lengthscale = self._param('lengthscale', torch.ones(1, input_dim))
        else:
            raise NotImplementedError

        self.noise = self._param('noise', torch.ones(1, 1))


    def forward(self, x_data, y_data, train=True):
        assert x_data.ndim == 3

        if self.mean_module_str == 'NN':
            learned_mean = self.mean_nn
            mean_module = None
        else:
            mean_module = ConstantMeanLight(self.constant_mean)
            learned_mean = None

        if self.covar_module_str == "NN":
            learned_kernel = self.kernel_nn
        else:
            learned_kernel = None
        lengthscale = self.lengthscale.view(self.lengthscale.shape[0], 1, self.lengthscale.shape[1])
        covar_module = SEKernelLight(lengthscale)

        likelihood = GaussianLikelihoodLight(self.noise)
        gp = LearnedGPRegressionModel(x_data, y_data, likelihood, mean_module=mean_module, covar_module=covar_module,
                                      learned_mean=learned_mean, learned_kernel=learned_kernel)
        if train:
            mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood, gp)
            output = gp(x_data)
            return likelihood(output), mll(output, y_data)
        else: # --> eval
            gp.eval()
            likelihood.eval()
            return gp, likelihood

    def parameter_shapes(self):
        return OrderedDict([(name, param.shape) for name, param in self.named_parameters().items()])

    def named_parameters(self):
        return self._params

    def _param_module(self, name, module):
        assert type(name) == str
        assert hasattr(module, 'named_parameters')
        for param_name, param in module.named_parameters().items():
            self._param(name + '.' + param_name, param)
        return module

    def _param(self, name, tensor):
        assert type(name) == str
        assert isinstance(tensor, torch.Tensor)
        assert name not in list(self._params.keys())
        if not device.type == tensor.device.type:
            tensor = tensor.to(device)
        self._params[name] = tensor
        return tensor

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

class RandomGP:

    def __init__(self, size_in, prior_factor=1.0, weight_prior_std=1.0, bias_prior_std=3.0, **kwargs):

        self._params = OrderedDict()
        self._param_dists = OrderedDict()

        self.prior_factor = prior_factor
        self.gp = VectorizedGP(size_in, **kwargs)

        for name, shape in self.gp.parameter_shapes().items():

            if name == 'constant_mean':
                mean_p_loc = torch.zeros(1).to(device)
                mean_p_scale = torch.ones(1).to(device)
                self._param_dist(name, Normal(mean_p_loc, mean_p_scale).to_event(1))

            if name == 'lengthscale':
                lengthscale_p_loc = torch.zeros(shape[-1]).to(device)
                lengthscale_p_scale = torch.ones(shape[-1]).to(device)
                self._param_dist(name, LogNormal(lengthscale_p_loc, lengthscale_p_scale).to_event(1))

            if name == 'noise':
                noise_p_loc = torch.log(0.1 * torch.ones(1)).to(device)
                noise_p_scale = 0.2 * torch.ones(1).to(device)
                self._param_dist(name, LogNormal(noise_p_loc, noise_p_scale).to_event(1))

            if 'mean_nn' in name or 'kernel_nn' in name:
                    mean = torch.zeros(shape).to(device)
                    if "weight" in name:
                        std = weight_prior_std * torch.ones(shape).to(device)
                    elif "bias" in name:
                        std = bias_prior_std * torch.ones(shape).to(device)
                    else:
                        raise NotImplementedError
                    self._param_dist(name, Normal(mean, std).to_event(1))

        # check that parameters in prior and gp modules are aligned
        for param_name_gp, param_name_prior in zip(self.gp.named_parameters().keys(), self._param_dists.keys()):
            assert param_name_gp == param_name_prior

        self.hyper_prior = CatDist(self._param_dists.values())

    def sample_params_from_prior(self, shape=torch.Size()):
        return self.hyper_prior.sample(shape)

    def sample_fn_from_prior(self, shape=torch.Size()):
        params = self.sample_params_from_prior(shape=shape)
        return self.get_forward_fn(params)

    def get_forward_fn(self, params):
        gp_model = copy.deepcopy(self.gp)
        gp_model.set_parameters_as_vector(params)
        return gp_model

    def _param_dist(self, name, dist):
        assert type(name) == str
        assert isinstance(dist, torch.distributions.Distribution)
        assert name not in list(self._param_dists.keys())
        assert hasattr(dist, 'rsample')
        self._param_dists[name] = dist
        return dist

    def _log_prob_prior(self, params):
        return self.hyper_prior.log_prob(params)

    def _log_prob_likelihood(self, params, x_data, y_data):
        fn = self.get_forward_fn(params)
        _, mll = fn(x_data, y_data)
        return mll

    def log_prob(self, params, x_data, y_data):
        return self.prior_factor * self._log_prob_prior(params) + self._log_prob_likelihood(params, x_data, y_data)

    def parameter_shapes(self):
        param_shapes_dict = OrderedDict()
        for name, dist in self._param_dists.items():
            param_shapes_dict[name] = dist.event_shape
        return param_shapes_dict

class RandomGPPosterior(torch.nn.Module):

    def __init__(self, named_param_shapes, init_std=0.1):
        super().__init__()

        self._param_dist_funcs = OrderedDict()

        _near_zero_params = lambda size: torch.nn.Parameter(torch.normal(0.0, init_std, size=size, device=device))
        _init_scale_raw = lambda size: torch.nn.Parameter(torch.normal(math.log(0.1), init_std, size=size, device=device))

        for name, shape in named_param_shapes.items():

            if name == 'constant_mean':
                self.const_mean_loc = _near_zero_params(shape)
                self.const_mean_scale_raw = _near_zero_params(shape)
                self._dist(name, lambda name: Normal(self.const_mean_loc, self.const_mean_scale_raw.exp()).to_event(1))

            if name == 'lengthscale' or name == 'noise':
                setattr(self, name + '_loc', _near_zero_params(shape))
                setattr(self, name + '_scale_raw', _near_zero_params(shape))
                self._dist(name, lambda name: LogNormal(getattr(self, name + '_loc'), getattr(self, name + '_scale_raw').exp()).to_event(1))


            if 'mean_nn' in name or 'kernel_nn' in name:
                name = name.replace(".", "_")
                setattr(self, name + '_loc', _near_zero_params(shape))
                setattr(self, name + '_scale_raw', _init_scale_raw(shape))
                def dist_fn(name):
                    return Normal(getattr(self, name + '_loc'), getattr(self, name + '_scale_raw').exp()).to_event(1)
                self._dist(name, copy.deepcopy(dist_fn))


        for param_shape, dist in zip(named_param_shapes.values(), self.forward().dists):
            assert dist.event_shape == param_shape

    def forward(self):
        return CatDist([dist_fn(name) for name, dist_fn in self._param_dist_funcs.items()])

    def _dist(self, name, dist_get_fn):
        assert type(name) == str
        assert callable(dist_get_fn)
        assert name not in list(self._param_dist_funcs.keys())
        self._param_dist_funcs[name] = dist_get_fn

    def rsample(self, sample_shape=torch.Size()):
        return self.forward().rsample(sample_shape)

    def sample(self, sample_shape=torch.Size()):
        return self.forward().sample(sample_shape)

    def log_prob(self, value):
        return self.forward().log_prob(value)

    def mode(self):
        modes = []
        for name, dist_fn in self._param_dist_funcs.items():
            dist = _get_base_dist(dist_fn(name))
            if isinstance(dist, Normal):
                mode = dist.mean
            elif isinstance(dist, LogNormal):
                mode = torch.exp(dist.loc - dist.scale ** 2)
            else:
                raise NotImplementedError
            modes.append(mode)
        return torch.cat(modes, dim=-1)

    def stddev_dict(self):
        with torch.no_grad():
            return OrderedDict([(name, torch.mean(dist_fn(name).stddev).item()) for name, dist_fn in self._param_dist_funcs.items()])

    def mean_stddev_dict(self):
        with torch.no_grad():
            return OrderedDict(
                [(name,  (torch.mean(dist_fn(name).mean).item(), torch.mean(dist_fn(name).stddev).item()))
                 for name, dist_fn in self._param_dist_funcs.items()])

def _get_base_dist(dist):
    if isinstance(dist, Independent):
        return _get_base_dist(dist.base_dist)
    else:
        return dist