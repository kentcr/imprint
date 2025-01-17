"""
# Testing tools

## Why is this here instead of conftest.py?

Typically, this kind of testing support code would go in conftest.py if imprint
were the only place in which that testing support code were used. But, we're
also importing these testing tools from other packages/repos internal to
Confirm. So, it makes sense to keep it in a separate module for importability.
Then we can load it as a pytest plugin
(https://docs.pytest.org/en/7.1.x/how-to/writing_plugins.html) with:
`pytest -p imprint.testing`
This flag is included in the default pytest configuration in pyproject.toml.

## Snapshot testing

Here you will find tools for snapshot testing. Snapshot testing is a way to
check that the output of a function is the same as it used to be. This is
particularly useful for end to end tests where we don't have a comparison point
for the end result but we want to know when the result changes. Snapshot
testing is very common in numerical computing.

Usage example:

```
def test_foo(snapshot):
    K = 8000
    result = scipy.stats.binom.std(n=K, p=np.linspace(0.4, 0.6, 100)) / K
    np.testing.assert_allclose(result, snapshot(result))
```

If you run `pytest --update-snapshots test_file.py::test_foo`, the snapshot will
be saved to disk. Then later when you run `pytest test_file.py::test_foo`, the
`snapshot(...)` call will automatically load that object so that you can
compare against the loaded object.

It's fine to call `snapshot(...)` multiple times in a test. The snapshot
filename will have an incremented counter indicating which call index is next.

When debugging a snapshot test, you can directly view the snapshot file if you
are using the `TextSerializer`. This is the default. Pandas DataFrame objects
are saved as csv and numpy arrays are saved as txt files.
"""
import glob
import os
import pickle
from pathlib import Path

import jax.config
import jax.numpy
import numpy as np
import pandas as pd
import pytest

from imprint import configure_logging
from imprint import package_settings


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: mark test as slow to run")

    configure_logging(is_testing=True)
    try:
        import dotenv

        dotenv.load_dotenv()
    except ImportError:
        pass
    package_settings()


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        # --run-slow given in cli: do not skip slow tests
        return
    skip_slow = pytest.mark.skip(reason="need --run-slow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


def pytest_addoption(parser):
    """
    Exposes snapshot plugin configuration to pytest.
    https://docs.pytest.org/en/latest/reference.html#_pytest.hookspec.pytest_addoption
    """
    parser.addoption(
        "--run-slow", action="store_true", default=False, help="run slow tests"
    )
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        dest="update_snapshots",
        help="Update snapshots",
    )


@pytest.fixture()
def cur_loc(request):
    """The location of the file containing the current test."""
    return Path(request.fspath).parent


def path_and_check(filebase, ext):
    snapshot_path = filebase + "." + ext
    if not os.path.exists(snapshot_path):
        raise FileNotFoundError(
            f"Snapshot file not found: {snapshot_path}."
            " Did you forget to run with --update-snapshots?"
        )
    return snapshot_path


class Pickler:
    @staticmethod
    def serialize(filebase, obj):
        with open(filebase + ".pkl", "wb") as f:
            pickle.dump(obj, f)

    @staticmethod
    def deserialize(filebase, obj):
        with open(path_and_check(filebase, "pkl"), "rb") as f:
            return pickle.load(f)


class TextSerializer:
    @staticmethod
    def serialize(filebase, obj):
        if isinstance(obj, pd.DataFrame):
            # in all our dataframes, the index is meaningless, so we do not
            # save it here.
            obj.to_csv(filebase + ".csv")
        elif isinstance(obj, np.ndarray) or isinstance(obj, jax.numpy.DeviceArray):
            np.savetxt(filebase + ".txt", obj)
        elif np.isscalar(obj):
            np.savetxt(filebase + ".txt", np.array([obj]))
        else:
            raise ValueError(
                f"TextSerializer cannot serialize {type(obj)}."
                " Try calling snapshot(obj, serializer=Pickler)."
            )

    @staticmethod
    def deserialize(filebase, obj):
        if isinstance(obj, pd.DataFrame):
            return pd.read_csv(path_and_check(filebase, "csv"), index_col=0)
        elif isinstance(obj, np.ndarray) or isinstance(obj, jax.numpy.DeviceArray):
            return np.loadtxt(path_and_check(filebase, "txt"))
        elif np.isscalar(obj):
            return np.loadtxt(path_and_check(filebase, "txt"))
        else:
            raise ValueError(
                f"TextSerializer cannot deserialize {type(obj)}."
                " Try calling snapshot(obj, serializer=Pickler)."
            )


class SnapshotAssertion:
    def __init__(
        self,
        *,
        update_snapshots,
        request,
        default_serializer=TextSerializer,
    ):
        self.update_snapshots = update_snapshots
        self.clear_snapshots = update_snapshots
        self.request = request
        self.default_serializer = default_serializer
        self.calls = 0
        self.test_name = None

    def set_test_name(self, test_name):
        self.test_name = test_name

    def _get_filebase(self):
        test_folder = Path(self.request.fspath).parent
        test_name = self.request.node.name if self.test_name is None else self.test_name
        return test_folder.joinpath("__snapshot__", test_name + f"_{self.calls}")

    def get(self, obj, serializer=None):
        if serializer is None:
            serializer = self.default_serializer

        return serializer.deserialize(str(self._get_filebase()), obj)

    def __call__(self, obj, serializer=None):
        """
        Return the saved copy of the object. If --update-snapshots is passed,
        save the object to disk in the __snapshot__ folder.

        Args:
            obj: The object to compare against. This is needed here to
                 determine the file extension.
            serializer: The serializer for loading the snapshot. Defaults to
                None which means we will use default_serializer. Unless
                default_serializer has been changed, this is TextSerializer, which
                will save the object as a .txt or .csv depending on whether it's a
                pd.DataFrame or np.ndarray.

        Returns:
            The snapshotted object.
        """
        if serializer is None:
            serializer = self.default_serializer

        # We provide the serializer with a filename without an extension. The
        # serializer can choose what extension to use.
        filebase = self._get_filebase()
        self.calls += 1
        if self.update_snapshots:
            filebase.parent.mkdir(exist_ok=True)
            str_filebase = str(filebase)
            # Delete any existing snapshots with the same name and index
            # regardless of the file extension.
            delete_files = glob.glob(str_filebase + ".*")
            for f in delete_files:
                os.remove(f)
            serializer.serialize(str_filebase, obj)
        return serializer.deserialize(str(filebase), obj)


@pytest.fixture
def snapshot(request):
    return SnapshotAssertion(
        update_snapshots=request.config.option.update_snapshots,
        request=request,
    )


def check_imprint_results(g, snapshot, ignore_story=True):
    """
    This is a helper method for snapshot testing of calibration and validation
    outputs. The goal is:
    1. to ensure that the outputs are identical to stored results.
    2. when the results have changed, isolate whether the change should be
        concerning or not:
        - it's very worrying if the calibration outputs change
        - on the other hand, it's not particularly worrying if there's a new
          column in the output!

    The checks proceed from portions of the data where it is absolutely
    necessary to have an exact match on to portions of the data where the data
    schema is more volatile.

    Args:
        g: The grid to compare.
        snapshot: The imprint.testing.snapshot object.
        ignore_story: Should we only test the grid + outputs and ignore
            storyline outputs like id, packet_id, event time, etc. Defaults to
            True.
    """
    if "lams" in g.df.columns:
        lamss = g.prune_inactive().df["lams"].min()
        np.testing.assert_allclose(lamss, snapshot(lamss))
    if "tie_bound" in g.df.columns:
        max_tie = g.prune_inactive().df["tie_bound"].max()
        np.testing.assert_allclose(max_tie, snapshot(max_tie))

    # For a correctly set up problem, the grid should have a unique ordering
    order_cols = (
        ["active"]
        + [f"theta{i}" for i in range(g.d)]
        + [f"radii{i}" for i in range(g.d)]
        + [f"null_truth{i}" for i in range(g.d)]
        + ["K"]
    )
    df = g.df.sort_values(by=order_cols).reset_index(drop=True)

    important_cols = (
        order_cols
        + [c for c in df.columns if "lams" in c]
        + [c for c in df.columns if "tie" in c]
    )
    check_subset = df[important_cols]
    compare = (
        snapshot(check_subset)
        .sort_values(by=order_cols)
        .reset_index(drop=True)[important_cols]
    )

    # First check the cal/val outputs. These are the most important values
    # to get correct.
    pd.testing.assert_frame_equal(check_subset, compare, check_dtype=False)
    if ignore_story:
        return

    df_idx = df.set_index("id")
    compare_all_cols = snapshot(df_idx)
    shared_cols = list(set(df_idx.columns).intersection(compare_all_cols.columns))
    # Compare the shared columns. This is helpful for ensuring that existing
    # columns are identical in a situation where we add a new column.
    pd.testing.assert_frame_equal(
        df_idx[shared_cols],
        compare_all_cols[shared_cols],
        check_like=True,
        check_index_type=False,
        check_dtype=False,
    )

    # Second, we check the remaining values. These are less important to be
    # precisely reproduced, but we still want to make sure they are
    # deterministic.
    pd.testing.assert_frame_equal(
        df_idx,
        compare_all_cols,
        check_like=True,
        check_index_type=False,
        check_dtype=False,
    )
