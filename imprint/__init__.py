from imprint.log import configure_logging


def package_settings():
    import numpy as np
    import pandas as pd
    import jax.config

    np.set_printoptions(edgeitems=10, linewidth=100)
    pd.set_option("display.max_columns", 100)
    pd.set_option("display.max_rows", 500)
    jax.config.update("jax_enable_x64", True)


from imprint.batching import batch
from imprint.batching import batch_all
from imprint.driver import calibrate
from imprint.driver import validate
from imprint.grid import cartesian_grid
from imprint.grid import create_grid
from imprint.grid import Grid
from imprint.grid import NullHypothesis
from imprint.model import Model
from imprint.nb_util import setup_nb
from imprint.planar_null import HyperPlane
from imprint.planar_null import hypo
