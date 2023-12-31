#%%[markdown]
# # Lotka-voltera with Neural ODE, my custom solver
# This script should perform the same task as https://docs.kidger.site/diffrax/examples/neural_ode/
# But withoug using Diffrax

#%%
from functools import partial
import time

import diffrax
import equinox as eqx  # https://github.com/patrick-kidger/equinox
import jax
import jax.nn as jnn
import jax.numpy as jnp
import jax.random as jrandom
import matplotlib.pyplot as plt
import optax  # https://github.com/deepmind/optax

from equinox.internal import error_if

# from graphpint.integrators import *
## Set the environment variable EQX_ON_ERROR=breakpoint
# import os
# os.environ["EQX_ON_ERROR"] = "breakpoint"

# jax.devices("cpu")

#%%
class Func(eqx.Module):
    mlp: eqx.nn.MLP
    params: jnp.ndarray

    def __init__(self, data_size, width_size, depth, *, key, **kwargs):
        super().__init__(**kwargs)
        self.mlp = eqx.nn.MLP(
            in_size=data_size,
            out_size=data_size,
            width_size=width_size,
            depth=depth,
            activation=jnn.softplus,
            key=key,
        )

        self.params = jnp.abs(jax.random.normal(key, (4,)))

    def __call__(self, t, y, args):
    # def __call__(self, y, t):
        # jax.debug.print("Calling the vector field at: {}", t)
        # y = error_if(y, jnp.isfinite(y), "kaboom1")

        dx0 = y[0]*self.params[0] - y[0]*y[1]*self.params[1]
        dx1 = y[0]*y[1]*self.params[2] - y[1]*self.params[3]
        physics = jnp.array([dx0, dx1])

        # ret = self.mlp(y)
        # return physics
        ret = physics + self.mlp(y)        ## Physics + Neural network

        # jax.debug.print("Called at: {}, Result {}", t, ret)

        # ret = error_if(ret, jnp.isfinite(ret), "kaboom2")
        return ret


#%%

def rk4_integrator(rhs, y0, t, rtol, atol, hmax, mxstep, max_steps_rev, kind):
  def step(state, t):
    y_prev, t_prev = state
    h = t - t_prev
    args = None
    k1 = h * rhs(t_prev, y_prev, args)
    k2 = h * rhs(t_prev + h/2., y_prev + k1/2., args)
    k3 = h * rhs(t_prev + h/2., y_prev + k2/2., args)
    k4 = h * rhs(t + h, y_prev + k3, args)
    y = y_prev + 1./6 * (k1 + 2 * k2 + 2 * k3 + k4)
    return (y, t), y
  _, ys = jax.lax.scan(step, (y0, t[0]), t[1:])
  return jnp.concatenate([y0[jnp.newaxis, :], ys], axis=0)

class NeuralODE(eqx.Module):
    func: Func

    def __init__(self, data_size, width_size, depth, *, key, **kwargs):
        super().__init__(**kwargs)
        self.func = Func(data_size, width_size, depth, key=key)

    def __call__(self, ts, y0):
        # ys_hat = rk4_integrator(self.func, y0, ts, None, None, None, None, None, None)
        # return ys_hat
    
        solution = diffrax.diffeqsolve(
            diffrax.ODETerm(self.func),
            diffrax.Tsit5(),
            t0=ts[0],
            t1=ts[-1],
            dt0=ts[1] - ts[0],
            y0=y0,
            stepsize_controller=diffrax.PIDController(rtol=1e-3, atol=1e-6),
            saveat=diffrax.SaveAt(ts=ts),
            max_steps=4096*1,    ## 4096//200 for debugging
        )

        # jax.debug.print("Solution: {}", solution.stats)
        nfes = solution.stats["num_steps"]      ## Actually, the number of steps of taken

        return solution.ys, nfes



#%%


def _get_data(ts, *, key):
    y0 = jrandom.uniform(key, (2,), minval=-0.6, maxval=1)
    y0 = jnp.abs(y0)

    def f(t, y, args):
        # x = y / (1 + y)
        # return jnp.stack([x[1], -x[0]], axis=-1)

        ## Lotka-Volterra
        alpha = 1.5
        beta = 1.0
        delta = 3.0
        gamma = 1.0
        dy0 = alpha * y[0] - beta * y[0] * y[1]
        dy1 = delta * y[0] * y[1] - gamma * y[1]
        return jnp.stack([dy0, dy1], axis=-1)

    solver = diffrax.Tsit5()
    dt0 = 0.1
    saveat = diffrax.SaveAt(ts=ts)
    sol = diffrax.diffeqsolve(
        diffrax.ODETerm(f), solver, ts[0], ts[-1], dt0, y0, saveat=saveat
    )
    ys = sol.ys
    return ys


def get_data(dataset_size, *, key):
    ts = jnp.linspace(0, 10, 100)    ## TODO It originally finished at t=10
    key = jrandom.split(key, dataset_size)
    ys = jax.vmap(lambda key: _get_data(ts, key=key))(key)
    return ts, ys




#%%

def dataloader(arrays, batch_size, *, key):
    dataset_size = arrays[0].shape[0]
    assert all(array.shape[0] == dataset_size for array in arrays)
    indices = jnp.arange(dataset_size)
    while True:
        perm = jrandom.permutation(key, indices)
        (key,) = jrandom.split(key, 1)
        start = 0
        end = batch_size
        while end < dataset_size:
            batch_perm = perm[start:end]
            yield tuple(array[batch_perm] for array in arrays)
            start = end
            end = start + batch_size



#%%

def main(
    dataset_size=32*4,
    batch_size=32*2,
    lr_strategy=(3e-3, 1e-3, 3e-7),
    length_strategy=(0.05, 0.1, 0.2),   ## If you increase the length, you must decrease the learning rate. Relation is non-linear
    steps_strategy=(1000, 1000, 1500),
    width_size=16*1,
    depth=2,
    seed=5678,
    plot=True,
    print_every=100,
):
    key = jrandom.PRNGKey(seed)
    data_key, model_key, loader_key = jrandom.split(key, 3)

    ts, ys = get_data(dataset_size, key=data_key)
    _, length_size, data_size = ys.shape

    ## Save ts and ys in npz file
    # jnp.savez("data/lotka_voltera_diffrax.npz", ts=ts, ys=ys)
    # exit()

    model = NeuralODE(data_size, width_size, depth, key=model_key)

    # Training loop like normal.
    #
    # Only thing to notice is that up until step 500 we train on only the first 10% of
    # each time series. This is a standard trick to avoid getting caught in a local
    # minimum.
    # Another thing is that the small number of time steps avoids the blowup we tipically 
    # face with stiff problems.

    def params_norm(params):
        """ norm of the parameters`"""
        return jnp.array([jnp.linalg.norm(x) for x in jax.tree_util.tree_leaves(params)]).sum()

    @partial(eqx.filter_value_and_grad, has_aux=True)
    def grad_loss(model, ti, yi):
        y_pred, nfes = jax.vmap(model, in_axes=(None, 0))(ti, yi[:, 0])
        # return jnp.mean((yi - y_pred) ** 2)

        ## TODO APHYNITY-style loss: https://arxiv.org/abs/2010.04456
        return jnp.mean((yi - y_pred) ** 2) + 1e-3*params_norm(model.func.mlp.layers), (jnp.sum(nfes))

    @eqx.filter_jit
    def make_step(ti, yi, model, opt_state):
        (loss, nfes), grads = grad_loss(model, ti, yi)
        updates, opt_state = optim.update(grads, opt_state)
        model = eqx.apply_updates(model, updates)

        ## Make sure the model.func.params are all positive. Clip them if not.
        # new_params = jnp.where(model.func.params < 0, 0.0, model.func.params)
        # new_params = jnp.zeros_like(model.func.params)
        # model = eqx.tree_at(lambda m: m.func.params, model, new_params)

        return loss, model, opt_state, nfes

    # def stiffness_ratio(model, ti, yi):
    #     model_grad = jax.jacrev(model)(ti, yi[:, 0], argnums=1)
    #     jacobians = jax.vmap(jax.jacobian(model.func, argnums=1))(ti, yi[:, 0])

    losses = []
    nfes = []
    for lr, steps, length in zip(lr_strategy, steps_strategy, length_strategy):
        optim = optax.adabelief(lr)
        opt_state = optim.init(eqx.filter(model, eqx.is_inexact_array))
        _ts = ts[: int(length_size * length)]
        _ys = ys[:, : int(length_size * length)]
        for step, (yi,) in zip(
            range(steps), dataloader((_ys,), batch_size, key=loader_key)
        ):
            start = time.time()
            loss, model, opt_state, nfe_step = make_step(_ts, yi, model, opt_state)
            end = time.time()
            losses.append(loss)
            nfes.append(nfe_step)
            if (step % print_every) == 0 or step == steps - 1:
                print(f"Step: {step:-5d},    Loss: {loss:-.8f},    NFEs: {nfe_step:-5d},    CPTime: {end - start:-.4f}")

    if plot:
        # fig, ax = plt.subplots(2, 2, figsize=(6*2, 3.5*2))
        fig, ax = plt.subplot_mosaic('AB;CC;DD', figsize=(6*2, 3.5*3))
        model_y, _ = model(ts, ys[0, 0])   ## TODO predicting on the entire trajectory ==forecasting !

        ax['A'].plot(ts, ys[0, :, 0], c="dodgerblue", label="Preys (GT)")
        ax['A'].plot(ts, model_y[:, 0], ".", c="navy", label="Preys (NODE)")

        ax['A'].plot(ts, ys[0, :, 1], c="violet", label="Predators (GT)")
        ax['A'].plot(ts, model_y[:, 1], ".", c="purple", label="Predators (NODE)")
        
        ax['A'].set_xlabel("Time")
        ax['A'].set_title("Trajectories")
        ax['A'].legend()

        ax['B'].plot(ys[0, :, 0], ys[0, :, 1], c="turquoise", label="GT")
        ax['B'].plot(model_y[:, 0], model_y[:, 1], ".", c="teal", label="Neural ODE")
        ax['B'].set_xlabel("Preys")
        ax['B'].set_ylabel("Predators")
        ax['B'].set_title("Phase space")
        ax['B'].legend()

        ax['C'].plot(losses, c="grey", label="Losses")
        ax['C'].set_xlabel("Epochs")
        ax['C'].set_title("Loss")
        ax['C'].set_yscale('log')
        ax['C'].legend()

        ax['D'].plot(nfes, c="brown", label="num_steps_taken")
        ax['D'].set_xlabel("Epochs")
        ax['D'].set_title("(Factor of) Number of Function Evaluations")
        ax['D'].legend()

        plt.tight_layout()
        plt.savefig("data/neural_ode_diffrax.png")
        plt.show()

    return ts, ys, model


# with jax.profiler.trace("/data/jax-trace", create_perfetto_link=False):
ts, ys, model = main()


# %% [markdown]

# # Preliminary results
# - Training on small portions of the trajectories' lenghts is the key. Otherwise it blows up !!
# this basically reduces the time horizon we are using (or change ts in get_data function), which is particularly useful for stiff problems.
# - This can also be explained by the fact the the stiffness increases as we train the Neural ODE
# After all, we are learning a vector fiel, so it shouldn't matter too much.
# - For this Lotka-voltera problem, tf=2 is already too much for a neural ODE to handle during training !!!!!!!!
# - A way to combat this is to have a really small learning rate when we increase the time horizon. The core problem is that the physics can be wrong/unnatural after update, and the Lotka-Voltera concentrations get negative. Be careful !!
# - Also, naturally, the gradual increase of the time horison improves robustness to local minima.

## Future work
# - TODO Is there a reserach question there ??? Does my method of multiplier do better here !!?
# - TODO Is this the same as the NFE stiffness issue? If no, can I publish this ??
# - TODO Help the people on the DIffrax issues: 
#   - https://github.com/patrick-kidger/diffrax/issues/218
#   - https://github.com/patrick-kidger/diffrax/issues/268







# %%

# rhs = model.func

# ## Linearise the rhs, then compute its eigenvalues
# def linearised_rhs(t, y, args):
#     return jax.jacrev(rhs, argnums=1)(t, y, args)

# def eigenvalues(t, y, args):
#     return jnp.linalg.eigvals(linearised_rhs(t, y, args))

# ## Send ys to the CPU
# jax.devices("cpu")

# print(ys.shape, ts.shape)

# model_y, _ = model(ts, ys[10, 0])

# model_y

# # ## Get largest eigenvalue
# # eigenvalues = jax.vmap(eigenvalues, in_axes=(None, 0, None))(0, ys[0, 0], None)

# # ## Compute the stiffness ratio on the CPU
# # stiffness_ratio = jnp.abs(jnp.real(eigenvalues(0, ys[0, 0], None)) / jnp.imag(eigenvalues(0, ys[0, 0], None)))
# # ratio = jnp.abs(jnp.real(eigenvalues(0, ys[0, 0], None)) / jnp.imag(eigenvalues(0, ys[0, 0], None)))

# %%
