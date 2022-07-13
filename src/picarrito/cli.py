from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Union, overload

import click
import matplotlib.pyplot as plt
import pandas as pd
import pydantic
import toml

from picarrito.fluxes import estimate_vol_flux
from picarrito.plot import plot_measurement

from . import database, logging_config, measurements

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("picarrito.toml")
_DEFAULT_OUTDIR = Path("picarrito")
_DB_FILENAME = "database.feather"
_FLUXES_FILENAME = "fluxes.csv"


@click.group()
@click.pass_context
@click.option(
    "config_path",
    "--config",
    type=click.Path(dir_okay=False, path_type=Path, exists=True),
    default=_DEFAULT_CONFIG_PATH,
)
def main(ctx: click.Context, config_path: Path):
    ctx.ensure_object(dict)
    conf = Config.from_toml(config_path)
    ctx.obj["config"] = conf

    work_dir = config_path.parent
    os.chdir(work_dir)

    logging_config.setup_logging(conf.logging, conf.general.outdir)


@main.command(name="import")
@click.pass_context
def import_(ctx: click.Context):
    conf: Config = ctx.obj["config"]
    db_path = _get_db_path(conf)

    try:
        db = database.read_db(db_path)
    except FileNotFoundError:
        logger.info(f"No existing database at '{db_path}'.")
        db = database.create_empty_db(conf.import_.columns, conf.import_.timestamp_col)

    new_data = _read_src_files(
        conf.import_.src,
        conf.import_.columns,
        conf.import_.timestamp_col,
        conf.import_.sep,
    )

    summary_rows = {
        "Before": _build_db_summary_row(db),
        "New data": _build_db_summary_row(new_data),
    }

    db = database.update(db, new_data)

    summary_rows["After"] = _build_db_summary_row(db)
    logger.info(f"Database updated:\n{pd.DataFrame(summary_rows).T}")

    database.save_db(db, db_path)


def _read_src_files(
    glob_patterns: List[str],
    dtypes: database.DTypes,
    timestamp_col: database.Colname,
    sep: str,
) -> pd.DataFrame:
    paths = database.find_files(glob_patterns)
    with click.progressbar(paths, label="Reading source files", show_pos=True) as paths:
        datasets = [
            database.read_src_file(path, dtypes, timestamp_col, sep) for path in paths
        ]
    result = database.update(database.create_empty_db(dtypes, timestamp_col), *datasets)
    return result


def _build_db_summary_row(db: pd.DataFrame):
    return {
        "Size in memory (MB)": f"{(db.memory_usage().sum() / (1024 * 1024)):.1f}",
        "Rows": f"{len(db):,}",
    }


@main.command()
@click.pass_context
def info(ctx: click.Context):
    conf: Config = ctx.obj["config"]
    for _ in _iter_measurements(conf):
        pass


@main.command()
@click.pass_context
def fluxes(ctx: click.Context):
    conf: Config = ctx.obj["config"]
    result = _estimate_fluxes_result_table(_iter_measurements(conf), conf)
    fluxes_path = conf.general.outdir / _FLUXES_FILENAME
    result.to_csv(
        fluxes_path,
        index=False,
    )
    logger.info(f"Saved fluxes to '{fluxes_path}'.")


@main.group()
@click.pass_context
def plot(ctx: click.Context):
    pass


@plot.command()
@click.pass_context
def flux_fits(ctx: click.Context):
    conf: Config = ctx.obj["config"]
    measurements = list(_iter_measurements(conf))
    plot_dir = conf.general.outdir / "plots" / "flux-fits"
    plot_dir.mkdir(parents=True, exist_ok=True)
    with click.progressbar(
        measurements, label="Plotting measurements", show_pos=True
    ) as measurements:
        for m in measurements:
            flux_estimates_by_gas = {
                gas: estimate_vol_flux(
                    m[gas],
                    t0_delay=conf.fluxes.t0_delay,
                    t0_margin=conf.fluxes.t0_margin,
                    tau_s=conf.fluxes.tau_s,
                    h=conf.fluxes.h,
                )
                for gas in conf.fluxes.gases
            }

            fig = plot_measurement(m, conf.fluxes.gases, flux_estimates_by_gas)
            plot_path = plot_dir / _build_measurement_file_name(m, conf, ".png")
            fig.savefig(plot_path)
            plt.close(fig)


@overload
def _get_chamber_label(
    chamber_value: pd.Series, chamber_labels: ChamberLabels
) -> pd.Series:
    ...


@overload
def _get_chamber_label(
    chamber_value: Union[int, float, bool, str], chamber_labels: ChamberLabels
) -> str:
    ...


def _get_chamber_label(chamber_value, chamber_labels):
    type_ = (
        chamber_value.dtype
        if isinstance(chamber_value, pd.Series)
        else type(chamber_value)
    )
    replacements = pd.Series(chamber_labels)
    replacements.index = replacements.index.astype(type_)
    if isinstance(chamber_value, pd.Series):
        return chamber_value.replace(replacements).astype(str)
    else:
        return replacements[chamber_value]


def _build_measurement_file_name(measurement: pd.DataFrame, conf: Config, suffix: str):
    (chamber_value,) = measurement[conf.measurements.chamber_col].unique()
    chamber_label = _get_chamber_label(chamber_value, conf.chamber_labels)
    data_start = measurement.index[0]
    return f"{chamber_label}-{data_start:%Y%m%d-%H%M%S}{suffix}"


def _estimate_fluxes_result_table(measurements: Iterable[pd.DataFrame], conf: Config):
    def build_row(measurement: pd.DataFrame, gas: database.Colname):
        flux_est = estimate_vol_flux(
            measurement[gas],
            t0_delay=conf.fluxes.t0_delay,
            t0_margin=conf.fluxes.t0_margin,
            tau_s=conf.fluxes.tau_s,
            h=conf.fluxes.h,
        )

        (chamber,) = measurement[conf.measurements.chamber_col].unique()

        result_row = {
            **flux_est,
            "molar_flux": flux_est["vol_flux"] * conf.fluxes.vol_to_molar_factor,
            "chamber": chamber,
            "gas": gas,
        }

        return result_row

    # The click.progressbar(list(measurements), ...) makes a full list
    # of all measurements, which increases memory consumption compared the iterator,
    # for the purpose of being able to know the progress.
    # However, this is only one of several places in the source code that
    # requires 2x full database in memory.
    with click.progressbar(
        list(measurements), label="Analyzing measurements", show_pos=True
    ) as measurements:
        result_table = pd.DataFrame.from_records(
            [
                build_row(measurement, gas)
                for measurement in measurements
                for gas in conf.fluxes.gases
            ]
        )

    result_table["chamber_label"] = _get_chamber_label(
        result_table["chamber"], conf.chamber_labels
    )

    result_table = result_table[_FLUXES_COLUMNS_ORDER]

    logger.info(
        f"Estimated {len(result_table)} fluxes ({', '.join(conf.fluxes.gases)}) "
        f"in {result_table['t0'].nunique()} measurements."
    )

    return result_table


_FLUXES_COLUMNS_ORDER = [
    "data_start",
    "t0",
    "chamber",
    "chamber_label",
    "gas",
    "c0",
    "vol_flux",
    "molar_flux",
]


def _iter_measurements(conf: Config):
    db = database.read_db(_get_db_path(conf))
    db = measurements.filter_db(db, conf.filters)
    yield from measurements.iter_measurements(
        db,
        conf.measurements.chamber_col,
        conf.measurements.max_gap,
        conf.measurements.min_duration,
        conf.measurements.max_duration,
    )


def _get_db_path(conf: Config) -> Path:
    return conf.general.outdir / _DB_FILENAME


class General(pydantic.BaseModel):
    outdir: Path = _DEFAULT_OUTDIR


class Import(pydantic.BaseModel):
    src: List[str]
    timestamp_col: str
    sep: str = r"\s"
    columns: Mapping[database.Colname, database.DTypeName]


class Measurements(pydantic.BaseModel):
    chamber_col: str
    max_gap: datetime.timedelta
    min_duration: datetime.timedelta
    max_duration: datetime.timedelta


ChamberLabels = Mapping[str, str]


class Fluxes(pydantic.BaseModel):
    gases: List[str]
    t0_delay: datetime.timedelta
    t0_margin: datetime.timedelta
    A: float
    Q: float
    V: float
    P: float = float("nan")
    T: float = float("nan")
    gas_constant: float = 8.31447

    @property
    def tau_s(self) -> float:
        return self.V / self.Q

    @property
    def h(self) -> float:
        return self.V / self.A

    @property
    def vol_to_molar_factor(self) -> float:
        # PV = nRT <=> n = PV / RT
        return self.P / (self.gas_constant * self.T)


class Config(pydantic.BaseModel):
    general: General = pydantic.Field(default_factory=General)
    import_: Import = pydantic.Field(alias="import")
    filters: Mapping[database.Colname, measurements.Filter] = pydantic.Field(
        default_factory=dict
    )
    measurements: Measurements
    chamber_labels: ChamberLabels = pydantic.Field(default_factory=dict)
    fluxes: Fluxes
    logging: Dict[str, Any] = logging_config.DEFAULT_LOG_SETTINGS

    @classmethod
    def from_toml(cls, path: Path) -> Config:
        logger.debug(f"Reading config file {path}")
        with open(path, "r") as f:
            obj = toml.load(f)
        return cls.parse_obj(obj)
