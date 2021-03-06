import copy
import torch
import torch.nn as nn
from . import diffeq_layers

__all__ = ["ODEnet", "ODEfunc", "ODEHypernet"]


def divergence_approx(f, y, e=None):
    e_dzdx = torch.autograd.grad(f, y, e, create_graph=True)[0]
    e_dzdx_e = e_dzdx.mul(e)

    cnt = 0
    while not e_dzdx_e.requires_grad and cnt < 10:
        # print("RequiresGrad:f=%s, y(rgrad)=%s, e_dzdx:%s, e:%s, e_dzdx_e:%s cnt=%d"
        #       % (f.requires_grad, y.requires_grad, e_dzdx.requires_grad,
        #          e.requires_grad, e_dzdx_e.requires_grad, cnt))
        e_dzdx = torch.autograd.grad(f, y, e, create_graph=True)[0]
        e_dzdx_e = e_dzdx * e
        cnt += 1

    approx_tr_dzdx = e_dzdx_e.sum(dim=-1)
    assert approx_tr_dzdx.requires_grad, \
        "(failed to add node to graph) f=%s %s, y(rgrad)=%s, e_dzdx:%s, e:%s, e_dzdx_e:%s cnt:%s" \
        % (
            f.size(), f.requires_grad, y.requires_grad, e_dzdx.requires_grad, e.requires_grad, e_dzdx_e.requires_grad, cnt)
    return approx_tr_dzdx


def divergence_bf(dx, y):
    sum_diag = 0.
    for i in range(y.shape[-1]):
        sum_diag += torch.autograd.grad(dx[:, :, i].sum(), y, create_graph=True)[
            0].contiguous()[:, :, i].contiguous()
    return sum_diag.contiguous()


class Swish(nn.Module):
    def __init__(self):
        super(Swish, self).__init__()
        self.beta = nn.Parameter(torch.tensor(1.0))

    def forward(self, x):
        return x * torch.sigmoid(self.beta * x)


class Lambda(nn.Module):
    def __init__(self, f):
        super(Lambda, self).__init__()
        self.f = f

    def forward(self, x):
        return self.f(x)


NONLINEARITIES = {
    "tanh": nn.Tanh(),
    "relu": nn.ReLU(),
    "softplus": nn.Softplus(),
    "elu": nn.ELU(),
    "swish": Swish(),
    "square": Lambda(lambda x: x ** 2),
    "identity": Lambda(lambda x: x),
}


class ODEnet(nn.Module):
    """
    Helper class to make neural nets for use in continuous normalizing flows
    """

    def __init__(self, hidden_dims, input_shape, context_dim, layer_type="concat", nonlinearity="softplus"):
        super(ODEnet, self).__init__()
        base_layer = {
            "ignore": diffeq_layers.IgnoreLinear,
            "squash": diffeq_layers.SquashLinear,
            "scale": diffeq_layers.ScaleLinear,
            "concat": diffeq_layers.ConcatLinear,
            "concat_v2": diffeq_layers.ConcatLinear_v2,
            "concatsquash": diffeq_layers.ConcatSquashLinear,
            "concatscale": diffeq_layers.ConcatScaleLinear,
        }[layer_type]

        # build models and add them
        layers = []
        activation_fns = []
        hidden_shape = input_shape

        for dim_out in (hidden_dims + (input_shape[0],)):
            layer_kwargs = {}
            layer = base_layer(
                hidden_shape[0], dim_out, context_dim, **layer_kwargs)
            layers.append(layer)
            activation_fns.append(NONLINEARITIES[nonlinearity])

            hidden_shape = list(copy.copy(hidden_shape))
            hidden_shape[0] = dim_out

        self.layers = nn.ModuleList(layers)
        self.activation_fns = nn.ModuleList(activation_fns[:-1])

    def forward(self, context, y):
        dx = y
        for l, layer in enumerate(self.layers):
            dx = layer(context, dx)
            # if not last layer, use nonlinearity
            if l < len(self.layers) - 1:
                dx = self.activation_fns[l](dx)
        return dx


class ODEfunc(nn.Module):
    def __init__(self, diffeq):
        super(ODEfunc, self).__init__()
        self.diffeq = diffeq
        self.divergence_fn = divergence_approx
        self.register_buffer("_num_evals", torch.tensor(0.))

    def before_odeint(self, e=None):
        self._e = e
        self._num_evals.fill_(0)

    def forward(self, t, states):
        y = states[0]
        t = torch.ones(y.size(0), 1).to(
            y) * t.clone().detach().requires_grad_(True).type_as(y)
        self._num_evals += 1
        for state in states:
            state.requires_grad_(True)

        # Sample and fix the noise.
        if self._e is None:
            self._e = torch.randn_like(y, requires_grad=True).to(y)

        with torch.set_grad_enabled(True):
            if len(states) == 3:  # conditional CNF
                c = states[2]
                tc = torch.cat([t, c.view(y.size(0), -1)], dim=1)
                dy = self.diffeq(tc, y)
                divergence = self.divergence_fn(dy, y, e=self._e).unsqueeze(-1)
                return dy, -divergence, torch.zeros_like(c).requires_grad_(True)
            elif len(states) == 2:  # unconditional CNF
                dy = self.diffeq(t, y)
                divergence = self.divergence_fn(dy, y, e=self._e).view(-1, 1)
                return dy, -divergence
            else:
                assert 0, "`len(states)` should be 2 or 3"


class ODEhyperfunc(nn.Module):
    def __init__(self, use_div_approx_train, use_div_approx_test, diffeq):
        super(ODEhyperfunc, self).__init__()
        self.diffeq = diffeq
        self.use_div_approx_train = use_div_approx_train
        self.use_div_approx_test = use_div_approx_test
        self.register_buffer("_num_evals", torch.tensor(0.))

    def before_odeint(self, e=None):
        self._e = e
        self._num_evals.fill_(0)

    def forward(self, t, states):
        y = states[0]
        t = torch.ones(y.size(0), 1).to(
            y) * t.clone().detach().requires_grad_(True).type_as(y)
        self._num_evals += 1
        for state in states:
            state.requires_grad_(True)
        # Sample and fix the noise.
        if self._e is None:
            self._e = torch.randn_like(y, requires_grad=True).to(y)

        with torch.set_grad_enabled(True):
            c = states[2]
            dy = self.diffeq(t, y, c)
            if self.training:
                if self.use_div_approx_train:
                    divergence = divergence_approx(
                        dy, y, e=self._e).unsqueeze(-1)
                else:
                    divergence = divergence_bf(dy, y).unsqueeze(-1)
            else:
                if self.use_div_approx_test:
                    divergence = divergence_approx(
                        dy, y, e=self._e).unsqueeze(-1)
                else:
                    divergence = divergence_bf(dy, y).unsqueeze(-1)
            return dy, -divergence, torch.zeros_like(c).requires_grad_(True)


class ODEHypernet(nn.Module):
    """
    Helper class to make neural nets for use in continuous normalizing flows
    """

    def __init__(self, dims, input_dim, nonlinearity="softplus", use_bias=True):
        super(ODEHypernet, self).__init__()

        self.activation = NONLINEARITIES[nonlinearity]
        # self.hypernetwork = hypernetwork
        # self.target_networks_weights = target_networks_weights
        self.use_bias = use_bias
        self.dims = [input_dim] + \
            list(map(int, dims.split("-"))) + [input_dim]

    def forward(self, context, y, target_networks_weights):
        dx = y
        i = 0

        batch_size = target_networks_weights.size(0)
        for l in range(len(self.dims) - 1):
            weight = target_networks_weights[:, i:i + self.dims[l] * self.dims[l + 1]].view(
                (batch_size, self.dims[l], self.dims[l + 1]))

            i += self.dims[l] * self.dims[l + 1]
            dx = torch.bmm(dx, weight)
            dx += target_networks_weights[:,
                                          i:i + self.dims[l + 1]].unsqueeze(1)

            i += self.dims[l + 1]
            # scaling
            weight_scales = target_networks_weights[:, i:i + self.dims[l + 1]].view(
                (batch_size, 1, self.dims[l + 1]))
            i += self.dims[l + 1]
            bias_scales = target_networks_weights[:,
                                                  i:i + self.dims[l + 1]].unsqueeze(1)
            i += self.dims[l + 1]
            scale = torch.sigmoid(
                torch.bmm(context.unsqueeze(2), weight_scales) + bias_scales)
            dx = scale * dx

            # Shifting
            weight_shift = target_networks_weights[:, i:i + self.dims[l + 1]].view(
                (batch_size, 1, self.dims[l + 1]))
            i += self.dims[l + 1]
            dx += torch.bmm(context.unsqueeze(2), weight_shift)

            # if not last layer, use nonlinearity
            if l < len(self.dims) - 2:
                dx = self.activation(dx)
        return dx


class ODEhyperfunc2D(nn.Module):
    def __init__(self, diffeq):
        super(ODEhyperfunc2D, self).__init__()
        self.diffeq = diffeq
        self.divergence_fn = divergence_approx
        self.register_buffer("_num_evals", torch.tensor(0.))

    def before_odeint(self, e=None):
        self._e = e
        self._num_evals.fill_(0)

    def forward(self, t, states):
        y = states[0]
        t = torch.ones(y.size(0), 1).to(
            y) * t.clone().detach().requires_grad_(True).type_as(y)
        self._num_evals += 1
        for state in states:
            state.requires_grad_(True)
        # Sample and fix the noise.
        if self._e is None:
            self._e = torch.randn_like(y, requires_grad=True).to(y)

        with torch.set_grad_enabled(True):
            c = states[2]
            target_networks_weights = states[3]
            dy = self.diffeq(t, y, c, target_networks_weights)
            divergence = self.divergence_fn(dy, y, e=self._e).unsqueeze(-1)
            return dy, -divergence, torch.zeros_like(c).requires_grad_(True), torch.zeros_like(target_networks_weights).requires_grad_(True)


class ODEHypernet2D(nn.Module):
    """
    Helper class to make neural nets for use in continuous normalizing flows
    """

    def __init__(self, dims, input_dim, nonlinearity="softplus", use_bias=True):
        super(ODEHypernet2D, self).__init__()

        self.activation = NONLINEARITIES[nonlinearity]
        # self.hypernetwork = hypernetwork
        # self.target_networks_weights = target_networks_weights
        self.use_bias = use_bias
        self.dims = [input_dim] + list(map(int, dims.split("-"))) + [input_dim]

    def forward(self, context, y, y_points, target_networks_weights):
        dx = y
        i = 0
        batch_size = target_networks_weights.size(0)
        for l in range(len(self.dims) - 1):
            weight = target_networks_weights[:, i:i + (self.dims[l] + 2) * self.dims[l + 1]].view(
                (batch_size, self.dims[l]+2, self.dims[l + 1]))
            i += (self.dims[l]+2) * self.dims[l + 1]
            dx = torch.cat((dx, y_points), dim=2)
            dx = torch.bmm(dx, weight)
            dx += target_networks_weights[:,
                                          i:i + self.dims[l + 1]].unsqueeze(1)
            i += self.dims[l + 1]
            # scaling
            weight_scales = target_networks_weights[:, i:i + self.dims[l + 1]].view(
                (batch_size, 1, self.dims[l + 1]))
            i += self.dims[l + 1]
            bias_scales = target_networks_weights[:,
                                                  i:i + self.dims[l + 1]].unsqueeze(1)
            i += self.dims[l + 1]
            scale = torch.sigmoid(
                torch.bmm(context.unsqueeze(2), weight_scales) + bias_scales)
            dx = scale * dx

            # Shifting
            weight_shift = target_networks_weights[:, i:i + self.dims[l + 1]].view(
                (batch_size, 1, self.dims[l + 1]))
            i += self.dims[l + 1]
            dx += torch.bmm(context.unsqueeze(2), weight_shift)

            # if not last layer, use nonlinearity
            if l < len(self.dims) - 2:
                dx = self.activation(dx)
        return dx
