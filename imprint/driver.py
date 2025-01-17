import copy
import warnings

import jax
import jax.numpy as jnp
import numpy as np
import pandas as pd

import imprint.bound as bound
from . import batching
from . import grid


bound_dict = {
    "normal": bound.normal.NormalBound,
    "normal2": bound.normal2.Normal2Bound,
    "scaled_chisq": bound.scaled_chisq.ScaledChiSqBound,
    "binomial": bound.binomial.BinomialBound,
    "exponential": bound.exponential.ExponentialBound,
}


# TODO: Need to clean up the interface from driver to the bounds.
# - should the bound classes have staticmethods or should they be objects with
#   __init__?
# - can we pass a single vertex array as a substitute for the many vertex case?
def get_bound(family, family_params):
    bound_type = bound_dict[family]
    return (
        bound_type.get_forward_bound(family_params),
        bound_type.get_backward_bound(family_params),
    )


@jax.jit
def clopper_pearson(tie_sum, K, delta):
    # internally numpyro uses tensorflow_probability
    # https://github.com/pyro-ppl/numpyro/blob/e28a3feaa4f95d76b361101f0c75dcb5add2365e/numpyro/distributions/util.py#L426
    # https://github.com/google/jax/issues/2399#issuecomment-1225990206
    from numpyro.distributions.util import betaincinv

    tie_cp_bound2 = betaincinv(tie_sum + 1, K - tie_sum, 1 - delta)
    # If typeI_sum == K, we get nan. Output 0 instead because the maximum TIE
    # is 1 and the TIE already = 1 if typeI_sum == K.
    tie_cp_bound2 = jnp.where(jnp.isnan(tie_cp_bound2), 0, tie_cp_bound2)
    return tie_cp_bound2


def calc_calibration_threshold(sorted_stats, sorted_order, alpha):
    idx = _calibration_index(sorted_stats.shape[0], alpha)
    # indexing a sorted array with sorted indices results in a sorted array!!
    return sorted_stats[sorted_order[idx]]


def _calibration_index(K, alpha):
    return jnp.maximum(jnp.floor((K + 1) * jnp.maximum(alpha, 0)).astype(int) - 1, 0)


def _groupby_apply_K(df, f):
    """
    Pandas groupby.apply catches TypeError and tries again. This is unpleasant
    because it often causes double exceptions. See:
    https://github.com/pandas-dev/pandas/issues/50980

    So, we work around this by just implementing our own groupby.apply.
    """
    out = []
    for K, K_df in df.groupby("K", group_keys=False):
        out.append(f(K, K_df))
    return pd.concat(out).loc[df.index]


def _check_stats(stats, K, theta):
    if stats.shape[0] != theta.shape[0]:
        raise ValueError(
            f"sim_batch returned test statistics for {stats.shape[0]}"
            f"tiles but {theta.shape[0]} tiles were expected."
        )
    if stats.shape[1] != K:
        raise ValueError(
            f"sim_batch returned test statistics for {stats.shape[1]} "
            f"simulations but {K} simulations were expected."
        )


class Driver:
    def __init__(self, model, *, tile_batch_size):
        self.model = model
        self.tile_batch_size = tile_batch_size
        self.forward_boundv, self.backward_boundv = get_bound(
            model.family, model.family_params if hasattr(model, "family_params") else {}
        )

        self.calibratev = jax.jit(
            jax.vmap(
                calc_calibration_threshold,
                in_axes=(0, None, 0),
            )
        )

    def stats(self, df):
        def f(K, K_df):
            K = K_df["K"].iloc[0]
            K_g = grid.Grid(K_df)
            theta = K_g.get_theta()
            # TODO: batching
            stats = self.model.sim_batch(0, K, theta, K_g.get_null_truth())
            _check_stats(stats, K, theta)
            return stats

        return _groupby_apply_K(df, f)

    def validate(self, df, lam, *, delta=0.01):
        def _batched(K, theta, vertices, null_truth):
            stats = self.model.sim_batch(0, K, theta.copy(), null_truth.copy())
            _check_stats(stats, K, theta)
            tie_sum = jnp.sum(stats < lam, axis=-1)
            tie_est = tie_sum / K
            tie_cp_bound = clopper_pearson(tie_sum, K, delta)
            tie_bound = self.forward_boundv(tie_cp_bound, theta, vertices)
            return tie_sum, tie_est, tie_cp_bound, tie_bound

        def f(K, K_df):
            K_g = grid.Grid(K_df, None)
            theta, vertices = K_g.get_theta_and_vertices()

            tie_sum, tie_est, tie_cp_bound, tie_bound = batching.batch(
                _batched,
                self.tile_batch_size,
                in_axes=(None, 0, 0, 0),
            )(K, theta, vertices, K_g.get_null_truth())

            return pd.DataFrame(
                dict(
                    tie_sum=tie_sum,
                    tie_est=tie_est,
                    tie_cp_bound=tie_cp_bound,
                    tie_bound=tie_bound,
                ),
                index=K_df.index,
            )

        out = _groupby_apply_K(df, f)
        out["K"] = df["K"]
        return out

    def calibrate(self, df, alpha):
        def _batched(K, theta, vertices, null_truth):
            stats = self.model.sim_batch(0, K, theta, null_truth)
            _check_stats(stats, K, theta)
            sorted_stats = jnp.sort(stats, axis=-1)
            alpha0 = self.backward_boundv(
                np.full(theta.shape[0], alpha), theta, vertices
            )
            return self.calibratev(sorted_stats, np.arange(K), alpha0), alpha0

        def f(K, K_df):
            K_g = grid.Grid(K_df, None)

            theta, vertices = K_g.get_theta_and_vertices()
            lams, alpha0 = batching.batch(
                _batched,
                self.tile_batch_size,
                in_axes=(None, 0, 0, 0),
            )(K, theta, vertices, K_g.get_null_truth())
            out = pd.DataFrame(index=K_df.index)
            out["lams"] = lams
            out["alpha0"] = alpha0
            return out

        out = _groupby_apply_K(df, f)
        out["idx"] = _calibration_index(df["K"].to_numpy(), out["alpha0"].to_numpy())
        out["K"] = df["K"]
        return out


# If K is not specified we just use a default value that's a decent
# guess.
default_K = 2**14


def _setup(model_type, g, model_seed, K, model_kwargs):
    g_pruned = g.prune_inactive()
    if g_pruned.n_tiles < g.n_tiles:
        warnings.warn(
            "Pruning inactive tiles before simulation. "
            "Mark these tiles as active if you want to simulate for them."
        )
    else:
        # NOTE: a no_copy parameter would be sensible in cases where the grid is
        # very large.
        # If pruning occured, a copy is not necessary.
        g = copy.deepcopy(g)

    if K is not None:
        g.df["K"] = K
    else:
        if "K" not in g.df.columns:
            g.df["K"] = default_K
        # If the K column is present but has some 0s, we replace those with the
        # default value.
        g.df.loc[g.df["K"] == 0, "K"] = default_K

    if model_kwargs is None:
        model_kwargs = {}
    model = model_type(model_seed, g.df["K"].max(), **model_kwargs)
    return model, g


def validate(
    model_type,
    *,
    g,
    lam,
    delta=0.01,
    model_seed=0,
    K=None,
    tile_batch_size=64,
    model_kwargs=None,
):
    """
    Calculate the Type I Error bound.

    Args:
        model_type: The model class.
        g: The grid.
        lam: The critical threshold in the rejection rule. Test statistics
             below this value will be rejected.
        delta: The bound will hold point-wise with probability 1 - delta.
               Defaults to 0.01.
        model_seed: The random seed. Defaults to 0.
        K: The number of simulations. If this is unspecified, it is assumed
           that the grid has a "K" column containing per-tile simulation counts.
           Defaults to None.
        tile_batch_size: The number of tiles to simulate in a single batch.
        model_kwargs: Keyword arguments passed to the model constructor.
                      Defaults to None.

    Returns:
        A dataframe with one row for each tile with the following columns:
        - tie_sum: The number of test statistics below the critical threshold.
        - tie_est: The estimated Type I Error at the simulation points.
        - tie_cp_bound: The Clopper-Pearson bound on the Type I error at the
                        simulation point.
        - tie_bound: The bound on the Type I error over the whole tile.
    """
    model, g = _setup(model_type, g, model_seed, K, model_kwargs)
    driver = Driver(model, tile_batch_size=tile_batch_size)
    rej_df = driver.validate(g.df, lam, delta=delta)
    return rej_df


def calibrate(
    model_type,
    *,
    g,
    alpha=0.025,
    model_seed=0,
    K=None,
    tile_batch_size=64,
    model_kwargs=None,
):
    """
    Calibrate the critical threshold for a given level of Type I Error control.

    Args:
        model_type: The model class.
        g: The grid.
        model_seed: The random seed. Defaults to 0.
        alpha: The Type I Error control level. Defaults to 0.025.
        K: The number of simulations. If this is unspecified, it is assumed
           that the grid has a "K" column containing per-tile simulation counts.
           Defaults to None.
        tile_batch_size: The number of tiles to simulate in a single batch.
        model_kwargs: Keyword arguments passed to the model constructor.
           Defaults to None.

    Returns:
        A dataframe with one row for each tile containing just the "lams"
        column, which contains lambda* for each tile.
    """
    model, g = _setup(model_type, g, model_seed, K, model_kwargs)
    driver = Driver(model, tile_batch_size=tile_batch_size)
    calibrate_df = driver.calibrate(g.df, alpha)
    return calibrate_df
