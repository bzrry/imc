#! /usr/bin/env python

"""
A class to model a imaging mass cytometry project.
"""

import os
import pathlib
from typing import Tuple, List, Optional, Union, Iterator  # , cast

import numpy as np  # type: ignore
import pandas as pd  # type: ignore
import parmap  # type: ignore

import matplotlib.pyplot as plt  # type: ignore
import seaborn as sns  # type: ignore

from imc.data_models.sample import IMCSample
from imc.types import Path, Figure, Patch, DataFrame, Series, MultiIndexSeries

# from imc import LOGGER
from imc.operations import (
    derive_reference_cell_type_labels,
    single_cell_analysis,
    get_adjacency_graph,
    measure_cell_type_adjacency,
    cluster_communities,
)
from imc.graphics import (
    get_grid_dims,
    add_legend,
)
from imc.utils import align_channels_by_name
from imc.exceptions import cast  # TODO: replace with typing.cast

FIG_KWS = dict(dpi=300, bbox_inches="tight")

DEFAULT_PROJECT_NAME = "project"
DEFAULT_SAMPLE_NAME_ATTRIBUTE = "sample_name"
DEFAULT_SAMPLE_GROUPING_ATTRIBUTEs = [DEFAULT_SAMPLE_NAME_ATTRIBUTE]
DEFAULT_TOGGLE_ATTRIBUTE = "toggle"
DEFAULT_PROCESSED_DIR_NAME = Path("processed")
DEFAULT_RESULTS_DIR_NAME = Path("results")
DEFAULT_PRJ_SINGLE_CELL_DIR = Path("single_cell")

# processed directory structure
SUBFOLDERS_PER_SAMPLE = True
ROI_STACKS_DIR = Path("tiffs")
ROI_MASKS_DIR = Path("tiffs")
ROI_UNCERTAINTY_DIR = Path("uncertainty")
ROI_SINGLE_CELL_DIR = Path("single_cell")


# def cast(arg: Optional[GenericType], name: str, obj: str) -> GenericType:
#     """Remove `Optional` from `T`."""
#     if arg is None:
#         raise AttributeNotSetError(f"Attribute '{name}' of '{obj}' cannot be None!")
#     return arg


class Project:
    """
    A class to model a IMC project.
    """

    """
    Parameters
    ----------
    metadata : :obj:`str`
        Path to CSV metadata sheet.
    name : :obj:`str`
        Project name. Defaults to "project".

    Attributes
    ----------
    name : :obj:`str`
        Project name
    metadata : :obj:`str`
        Path to CSV metadata sheet.
    metadata : :class:`pandas.DataFrame`
        Metadata dataframe
    samples : List[:class:`IMCSample`]
        List of IMC sample objects.
    """

    def __init__(
        self,
        metadata: Optional[Union[str, Path, DataFrame]] = None,
        name: str = DEFAULT_PROJECT_NAME,
        sample_name_attribute: str = DEFAULT_SAMPLE_NAME_ATTRIBUTE,
        sample_grouping_attributes: Optional[List[str]] = None,
        panel_metadata: Optional[Union[Path, DataFrame]] = None,
        toggle: bool = True,
        subfolder_per_sample: bool = SUBFOLDERS_PER_SAMPLE,
        processed_dir: Path = DEFAULT_PROCESSED_DIR_NAME,
        results_dir: Path = DEFAULT_RESULTS_DIR_NAME,
        **kwargs,
    ):
        # Initialize
        self.name = name
        self.metadata = (
            pd.read_csv(metadata)
            if isinstance(metadata, (str, pathlib.Path, Path))
            else metadata
        )
        self.samples: List["IMCSample"] = list()
        self.sample_name_attribute = sample_name_attribute
        self.sample_grouping_attributes = (
            sample_grouping_attributes or DEFAULT_SAMPLE_GROUPING_ATTRIBUTEs
        )
        self.panel_metadata: Optional[DataFrame] = (
            pd.read_csv(panel_metadata, index_col=0)
            if isinstance(panel_metadata, (str, Path))
            else panel_metadata
        )
        # # TODO: make sure channel labels conform to internal specification: "Label(Metal\d+)"
        # self.channel_labels: Optional[Series] = (
        #     pd.read_csv(channel_labels, index_col=0, squeeze=True)
        #     if isinstance(channel_labels, (str, Path))
        #     else channel_labels
        # )

        self.toggle = toggle
        self.subfolder_per_sample = subfolder_per_sample
        self.processed_dir = Path(processed_dir).absolute()
        self.results_dir = Path(results_dir).absolute()
        self.results_dir.mkdir()
        self.quantification: Optional[DataFrame] = None
        self._clusters: Optional[
            MultiIndexSeries
        ] = None  # MultiIndex: ['sample', 'roi', 'obj_id']

        if not hasattr(self, "samples"):
            self.samples = list()

        self._initialize_project_from_annotation(**kwargs)

        if not self.rois:
            print(
                "Could not find ROIs for any of the samples. "
                "Either pass metadata with one row per ROI, "
                "or set `processed_dir` in order for ROIs to be discovered, "
                "and make sure select the right project stucture with `subfolder_per_sample`."
            )

        # if self.channel_labels is None:
        #     self.set_channel_labels()

    def __repr__(self):
        s = len(self.samples)
        r = len(self.rois)
        return (
            f"Project '{self.name}' with {s} sample"
            + (" " if s == 1 else "s ")
            + f"and {r} ROI"
            + (" " if r == 1 else "s ")
            + "in total."
        )

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

    def __getitem__(self, item: int) -> "IMCSample":
        return self.samples[item]

    def __iter__(self) -> Iterator["IMCSample"]:
        return iter(self.samples)

    def _detect_samples(self) -> DataFrame:
        if self.processed_dir is None:
            print("Project does not have `processed_dir`. Cannot find Samples.")
            return pd.DataFrame()

        content = (
            [x for x in self.processed_dir.iterdir() if x.is_dir()]
            if self.subfolder_per_sample
            else self.processed_dir.glob("*_full.tiff")
        )
        df = pd.Series(content, dtype="object").to_frame()
        if df.empty:
            print(f"Could not find any Samples in '{self.processed_dir}'.")
            return df
        df[DEFAULT_SAMPLE_NAME_ATTRIBUTE] = df[0].apply(
            lambda x: x.name.replace("_full.tiff", "")
        )
        return df.drop(0, axis=1)  # .sort_values(DEFAULT_SAMPLE_NAME_ATTRIBUTE)

    def _initialize_project_from_annotation(
        self,
        toggle: Optional[bool] = None,
        sample_grouping_attributes: Optional[List[str]] = None,
        **kwargs,
    ) -> None:
        def cols_with_unique_values(dfs: DataFrame) -> set:
            return {col for col in dfs if len(dfs[col].unique()) == 1}

        metadata = (
            self.metadata
            if self.metadata is not None
            else self._detect_samples()
        )

        if metadata.empty:
            return

        if (toggle or self.toggle) and ("toggle" in metadata.columns):
            # TODO: logger.info("Removing samples without toggle active")
            metadata = metadata[metadata[DEFAULT_TOGGLE_ATTRIBUTE]]

        sample_grouping_attributes = (
            sample_grouping_attributes
            or self.sample_grouping_attributes
            or metadata.columns.tolist()
        )

        for _, idx in metadata.groupby(
            sample_grouping_attributes, sort=False
        ).groups.items():
            rows = metadata.loc[idx]
            const_cols = cols_with_unique_values(rows)
            row = rows[const_cols].drop_duplicates().squeeze(axis=0)

            sample = IMCSample(
                sample_name=row[self.sample_name_attribute],
                root_dir=(
                    self.processed_dir / str(row[self.sample_name_attribute])
                )
                if self.subfolder_per_sample
                else self.processed_dir,
                subfolder_per_sample=self.subfolder_per_sample,
                metadata=rows if rows.shape[0] > 1 else None,
                panel_metadata=self.panel_metadata,
                prj=self,
                **kwargs,
                **row.drop("sample_name", errors="ignore").to_dict(),
            )
            for roi in sample.rois:
                roi.prj = self
                # If channel labels are given, add them to all ROIs
                # roi._channel_labels = self.channel_labels
            self.samples.append(sample)

    @property
    def rois(self) -> List["ROI"]:
        """
        Return a list of all ROIs of the project samples.
        """
        return [roi for sample in self.samples for roi in sample.rois]

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def n_rois(self) -> int:
        return len(self.rois)

    @property
    def channel_labels(self) -> Union[Series, DataFrame]:
        return pd.concat(
            [sample.channel_labels for sample in self.samples], axis=1
        )

    @property
    def channel_names(self) -> Union[Series, DataFrame]:
        return pd.concat(
            [sample.channel_names for sample in self.samples], axis=1
        )

    @property
    def channel_metals(self) -> Union[Series, DataFrame]:
        return pd.concat(
            [sample.channel_metals for sample in self.samples], axis=1
        )

    def _get_rois(
        self, samples: Optional[List["IMCSample"]], rois: Optional[List["ROI"]]
    ) -> List["ROI"]:
        return [
            r
            for sample in (samples or self.samples)
            for r in sample.rois
            if r in (rois or sample.rois)
        ]

    def _get_input_filename(self, input_type: str) -> Path:
        """Get path to file with data for Sample.

        Available `input_type` values are:
            - "cell_type_assignments": CSV file with cell type assignemts for each cell and each ROI
        """
        to_read = {
            "h5ad": (
                DEFAULT_PRJ_SINGLE_CELL_DIR,
                ".single_cell.processed.h5ad",
            ),
            "cell_cluster_assignments": (
                DEFAULT_PRJ_SINGLE_CELL_DIR,
                ".single_cell.cluster_assignments.csv",
            ),
        }
        dir_, suffix = to_read[input_type]
        return self.results_dir / dir_ / (self.name + suffix)

    def get_samples(self, sample_names: Union[str, List[str]]):
        if isinstance(sample_names, str):
            sample_names = [sample_names]
        samples = [s for s in self.samples if s.name in sample_names]
        if samples:
            return samples[0] if len(samples) == 1 else samples
        else:
            ValueError(f"Sample '{sample_names}' couldn't be found.")

    def get_rois(self, roi_names: Union[str, List[str]]):
        if isinstance(roi_names, str):
            roi_names = [roi_names]
        rois = [r for r in self.rois if r.name in roi_names]
        if rois:
            return rois[0] if len(rois) == 1 else rois
        else:
            ValueError(f"Sample '{roi_names}' couldn't be found.")

    def plot_channels(
        self,
        channels: List[str] = ["mean"],
        per_sample: bool = False,
        merged: bool = False,
        save: bool = False,
        output_dir: Optional[Path] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> Figure:
        """
        Plot a list of channels for all Samples/ROIs.
        """
        if isinstance(channels, str):
            channels = [channels]
        output_dir = Path(output_dir or self.results_dir / "qc")
        if save:
            output_dir.mkdir(exist_ok=True)
            channels_str = ",".join(channels)
            fig_file = output_dir / ".".join(
                [self.name, f"all_rois.{channels_str}.pdf"]
            )
        if per_sample:
            for sample in samples or self.samples:
                fig = sample.plot_channels(channels, **kwargs)
                if save:
                    fig_file = output_dir / ".".join(
                        [self.name, sample.name, f"all_rois.{channels_str}.pdf"]
                    )
                    fig.savefig(fig_file, **FIG_KWS)
        else:
            rois = self._get_rois(samples, rois)

            i = 0
            j = 1 if merged else len(channels)
            n, m = (
                get_grid_dims(len(rois))
                if merged
                else get_grid_dims(len(rois) * j)
            )
            fig, axes = plt.subplots(n, m, figsize=(4 * m, 4 * n))
            axes = axes.flatten()
            for roi in rois:
                roi.plot_channels(
                    channels, axes=axes[i : i + j], merged=merged, **kwargs
                )
                i += j
            for _ax in axes[i:]:
                _ax.axis("off")
            if save:
                fig.savefig(fig_file, **FIG_KWS)
        return fig

    # TODO: write decorator to get/set default outputdir and handle dir creation
    def plot_probabilities_and_segmentation(
        self,
        jointly: bool = False,
        output_dir: Optional[Path] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ):
        # TODO: adapt to detect whether to plot nuclei mask
        samples = samples or self.samples
        # for sample in samples:
        #     sample.read_all_inputs(only_these_keys=["probabilities", "cell_mask", "nuclei_mask"])
        output_dir = Path(output_dir or self.results_dir / "qc")
        os.makedirs(output_dir, exist_ok=True)
        if not jointly:
            for sample in samples:
                plot_file = output_dir / ".".join(
                    [
                        self.name,
                        sample.name,
                        "all_rois.plot_probabilities_and_segmentation.svg",
                    ]
                )
                fig = sample.plot_probabilities_and_segmentation()
                fig.savefig(plot_file, **FIG_KWS)
        else:
            rois = self._get_rois(samples, rois)
            n = len(rois)
            fig, axes = plt.subplots(n, 5, figsize=(4 * 5, 4 * n))
            for i, roi in enumerate(rois):
                roi.plot_probabilities_and_segmentation(axes=axes[i])
            plot_file = output_dir / (
                self.name
                + ".all_rois.plot_probabilities_and_segmentation.all_rois.svg"
            )
            fig.savefig(plot_file, **FIG_KWS)

    def plot_cell_types(
        self,
        cell_type_combinations: Optional[
            Union[str, List[Tuple[str, str]]]
        ] = None,
        cell_type_assignments: Optional[DataFrame] = None,
        palette: Optional[str] = "tab20",
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ):
        # TODO: fix compatibility of `cell_type_combinations`.
        samples = samples or self.samples
        rois = rois or self.rois

        n = len(samples)
        m = max([sample.n_rois for sample in samples])
        fig, axes = plt.subplots(n, m, figsize=(3 * m, 3 * n), squeeze=False)
        patches: List[Patch] = list()
        for i, sample in enumerate(samples):
            for j, roi in enumerate(
                [roi for roi in rois if roi in sample.rois]
            ):
                patches += roi.plot_cell_types(
                    cell_type_combinations=cell_type_combinations,
                    cell_type_assignments=cell_type_assignments,
                    palette=palette,
                    ax=axes[i, j],
                )
        add_legend(patches, axes[0, -1])
        for ax in axes.flatten():
            ax.axis("off")
        return fig

    def channel_summary(
        self,
        red_func: str = "mean",
        channel_exclude: Optional[List[str]] = None,
        plot: bool = True,
        output_prefix: str = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> Union[DataFrame, Tuple[DataFrame, Figure]]:
        # for sample, _func in zip(samples or self.samples, red_func):
        samples = samples or self.samples
        rois = self._get_rois(samples, rois)

        _res = dict()
        for roi in rois:
            _res[roi.name] = pd.Series(
                getattr(roi.stack, red_func)(axis=(1, 2)),
                index=roi.channel_labels,
            )
        res = pd.DataFrame(_res)

        if res.isnull().any().any():
            res = align_channels_by_name(res)

        # filter channels out if requested
        if channel_exclude is not None:
            # to accomodate strings with especial characters like a parenthesis
            # (as existing in the metal), exclude exact matches OR containing strings
            exc = res.index.isin(channel_exclude) | res.index.str.contains(
                "|".join(channel_exclude)
            )
            res = res.loc[res.index[~exc]]
        res = res / res.mean()

        if plot:
            res = np.log1p(res)
            # calculate mean intensity
            channel_mean = res.mean(axis=1).rename("channel_mean")

            # calculate cell density
            cell_density = pd.Series(
                [roi.cells_per_area_unit() for roi in rois],
                index=[roi.name for roi in rois],
                name="cell_density",
            )
            if all(cell_density < 0):
                cell_density *= 1000

            def_kwargs = dict(z_score=0, center=0, robust=True, cmap="RdBu_r")
            def_kwargs.update(kwargs)
            # TODO: add {row,col}_colors colorbar to heatmap
            if output_prefix is None:
                output_prefix = self.results_dir / "qc" / self.name
            for kws, label, cbar_label in [
                ({}, "", ""),
                (def_kwargs, ".z_score", " (Z-score)"),
            ]:
                plot_file = (
                    output_prefix + f".mean_per_channel.clustermap{label}.svg"
                )
                grid = sns.clustermap(
                    res,
                    cbar_kws=dict(label=red_func.capitalize() + cbar_label),
                    row_colors=channel_mean,
                    col_colors=cell_density,
                    metric="correlation",
                    xticklabels=True,
                    yticklabels=True,
                    **kws,
                )
                grid.fig.suptitle("Mean channel intensities", y=1.05)
                grid.savefig(plot_file, dpi=300, bbox_inches="tight")
            grid.fig.grid = grid
            return (res, grid.fig)
        res.index.name = "channel"
        return res

    def image_summary(
        self,
        samples: Optional[List["IMCSample"]] = None,
        rois: List["ROI"] = None,
    ):
        raise NotImplementedError
        from imc.utils import lacunarity, fractal_dimension

        rois = self._get_rois(samples, rois)
        roi_names = [r.name for r in rois]
        densities = pd.Series(
            {roi.name: roi.cells_per_area_unit() for roi in rois},
            name="cell density",
        )
        lacunarities = pd.Series(
            parmap.map(
                lacunarity, [roi.cell_mask_o for roi in rois], pm_pbar=True
            ),
            index=roi_names,
            name="lacunarity",
        )
        fractal_dimensions = pd.Series(
            parmap.map(
                fractal_dimension,
                [roi.cell_mask_o for roi in rois],
                pm_pbar=True,
            ),
            index=roi_names,
            name="fractal_dimension",
        )

        morphos = pd.DataFrame(
            [densities * 1e4, lacunarities, fractal_dimensions]
        ).T

    def channel_correlation(
        self,
        channel_exclude: Optional[List[str]] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ) -> Figure:
        """
        Observe the pairwise correlation of channels across ROIs.
        """
        from imc.operations import _correlate_channels__roi

        rois = self._get_rois(samples, rois)
        _res = parmap.map(_correlate_channels__roi, rois, pm_pbar=True)

        # handling differnet pannels based on channel name
        # that then makes that concatenating dfs with duplicated names in indeces
        res = pd.concat(
            [
                x.groupby(level=0).mean().T.groupby(level=0).mean().T
                for x in _res
            ]
        )
        xcorr = res.groupby(level=0).mean().fillna(0)
        labels = xcorr.index
        if channel_exclude is not None:
            exc = labels.isin(channel_exclude) | labels.str.contains(
                "|".join(channel_exclude)
            )
            xcorr = xcorr.loc[labels[~exc], labels[~exc]]

        grid = sns.clustermap(
            xcorr,
            cmap="RdBu_r",
            center=0,
            robust=True,
            xticklabels=True,
            yticklabels=True,
            cbar_kws=dict(label="Pearson correlation"),
        )
        grid.ax_col_dendrogram.set_title(
            "Pairwise channel correlation\n(pixel level)"
        )
        grid.savefig(
            self.results_dir / "qc" / self.name
            + ".channel_pairwise_correlation.svg",
            **FIG_KWS,
        )
        grid.fig.grid = grid
        return grid.fig

    def quantify_cells(
        self,
        intensity: bool = True,
        morphology: bool = True,
        set_attribute: bool = True,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ) -> Optional[DataFrame]:
        """
        Measure the intensity of each channel in each single cell.
        """
        from imc.operations import quantify_cells_rois

        quantification = quantify_cells_rois(
            self._get_rois(samples, rois), intensity, morphology
        )
        if not set_attribute:
            return quantification
        self.quantification = quantification
        return None

    def quantify_cell_intensity(
        self,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> DataFrame:
        """
        Measure the intensity of each channel in each single cell.
        """
        from imc.operations import quantify_cell_intensity_rois

        return quantify_cell_intensity_rois(
            self._get_rois(samples, rois), **kwargs
        )

    def quantify_cell_morphology(
        self,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> DataFrame:
        """
        Measure the shape parameters of each single cell.
        """
        from imc.operations import quantify_cell_morphology_rois

        return quantify_cell_morphology_rois(
            self._get_rois(samples, rois), **kwargs
        )

    def cluster_cells(
        self,
        output_prefix: Optional[Path] = None,
        plot: bool = True,
        set_attribute: bool = True,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> Optional[Series]:
        """
        Derive clusters of single cells based on their channel intensity.
        """
        output_prefix = Path(
            output_prefix or self.results_dir / "single_cell" / self.name
        )

        if "quantification" not in kwargs and self.quantification is not None:
            kwargs["quantification"] = self.quantification
        if (
            "cell_type_channels" not in kwargs
            and self.panel_metadata is not None
        ):
            if "cell_type" in self.panel_metadata.columns:
                kwargs["cell_type_channels"] = self.panel_metadata.query(
                    "cell_type == 1"
                ).index.tolist()

        clusters = single_cell_analysis(
            output_prefix=output_prefix,
            rois=self._get_rois(samples, rois),
            plot=plot,
            **kwargs,
        )
        # save clusters as CSV in default file
        clusters.reset_index().to_csv(
            self._get_input_filename("cell_cluster_assignments"), index=False
        )
        if not set_attribute:
            return clusters

        # Set clusters for project and propagate for Samples and ROIs.
        # in principle there was no need to pass clusters here as it will be read
        # however, the CSV roundtrip might give problems in edge cases, for
        # example when the sample name is only integers
        self.set_clusters(clusters.astype(str))
        return None

    @property
    def clusters(self):
        if self._clusters is not None:
            return self._clusters
        self.set_clusters()
        return self._clusters

    def set_clusters(
        self,
        clusters: Optional[MultiIndexSeries] = None,
        write_to_disk: bool = False,
        samples: Optional[List["IMCSample"]] = None,
    ) -> None:
        """
        Set the `clusters` attribute of the project and
        propagate it to the Samples and their ROIs.

        If not given, `clusters` is the output of
        :func:`Project._get_input_filename`("cell_cluster_assignments").
        """
        id_cols = ["sample", "roi", "obj_id"]
        fn = self._get_input_filename("cell_cluster_assignments")
        fn.parent.mkdir()
        if clusters is None:
            clusters = (
                pd.read_csv(fn, dtype={"sample": str, "roi": str},).set_index(
                    id_cols
                )
            )[
                "cluster"
            ]  # .astype(str)
        assert isinstance(clusters.index, pd.MultiIndex)
        assert clusters.index.names == id_cols
        self._clusters = clusters
        for sample in samples or self.samples:
            sample.set_clusters(clusters=clusters.loc[sample.name])
        if write_to_disk:
            self._clusters.reset_index().to_csv(fn, index=False)

    def label_clusters(
        self,
        h5ad_file: Optional[Path] = None,
        output_prefix: Optional[Path] = None,
        **kwargs,
    ) -> None:
        """
        Derive labels for each identified cluster
        based on its most abundant markers.
        """
        prefix = self.results_dir / "single_cell" / self.name
        h5ad_file = Path(h5ad_file or prefix + ".single_cell.processed.h5ad")
        output_prefix = Path(output_prefix or prefix + ".cell_type_reference")
        new_labels = derive_reference_cell_type_labels(
            h5ad_file, output_prefix, **kwargs
        )
        self._rename_clusters(new_labels.to_dict())

    def _rename_clusters(self, new_labels: dict, save: bool = True):
        clusters = cast(self.clusters).replace(new_labels)
        if save:
            clusters.reset_index().to_csv(
                self._get_input_filename("cell_cluster_assignments"),
                index=False,
            )
        self.set_clusters(clusters)

    def sample_comparisons(
        self,
        sample_attributes: Optional[List[str]] = None,
        output_prefix: Optional[Path] = None,
        cell_type_percentage_threshold: float = 1.0,
        channel_exclude: List[str] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ):
        # TODO: revamp/separate into smaller functions
        import itertools
        from scipy.stats import mannwhitneyu
        from statsmodels.stats.multitest import multipletests

        sample_attributes = sample_attributes or ["name"]
        samples = samples or self.samples
        rois = self._get_rois(samples, rois)
        output_prefix = (
            output_prefix or self.results_dir / "single_cell" / self.name + "."
        )
        output_prefix.parent.mkdir(exist_ok=True)

        # group samples by desired attributes
        sample_df = (
            pd.DataFrame(
                {k: v for k, v in sample.__dict__.items() if isinstance(v, str)}
                for sample in samples
            )[["name"] + sample_attributes]
            .set_index("name")
            .rename_axis("sample")
            .reset_index()
        )
        sample_groups = sample_df.groupby(sample_attributes)["sample"].apply(
            set
        )
        sample_roi_df = pd.DataFrame(
            [(roi.name, roi.sample.name) for roi in rois],
            columns=["roi", "sample"],
        )

        # Whole channel means
        channel_means: DataFrame = self.channel_summary(
            plot=False, channel_exclude=channel_exclude
        )
        channel_means.index.name = "channel"
        channel_means = (
            channel_means.reset_index()
            .melt(id_vars="channel", var_name="roi")
            .reset_index(drop=True)
        )
        channel_df = (
            channel_means.merge(sample_roi_df)
            .merge(sample_df)
            .sort_values(sample_attributes)
        )

        # cell type abundancy per sample or group of samples
        cluster_counts = (
            self.clusters.groupby(level=["sample", "roi"])
            .value_counts()
            .rename("cell_count")
        )
        cluster_perc = (
            cluster_counts.groupby("cluster").sum() / cluster_counts.sum()
        ) * 100
        filtered_clusters = cluster_perc[
            cluster_perc > cell_type_percentage_threshold
        ].index

        # # absolute
        # # fraction of total
        cluster_df = (
            cluster_counts.reset_index()
            .merge(sample_df)
            .sort_values(sample_attributes)
        )
        cluster_df["cell_perc"] = cluster_df.groupby("roi")["cell_count"].apply(
            lambda x: (x / x.sum()) * 100
        )

        # Test difference between channels/clusters
        # # channels
        _res = list()
        for attribute in sample_attributes:
            for channel in channel_df["channel"].unique():
                for group1, group2 in itertools.permutations(
                    channel_df[attribute].unique(), 2
                ):
                    a = channel_df.query(
                        f"channel == '{channel}' & {attribute} == '{group1}'"
                    )["value"]
                    b = channel_df.query(
                        f"channel == '{channel}' & {attribute} == '{group2}'"
                    )["value"]
                    am = a.mean()
                    bm = b.mean()
                    means = [am, bm, np.log2(a.mean() / b.mean())]
                    try:
                        mu = mannwhitneyu(a, b)
                    except ValueError:
                        mu = (np.nan, np.nan)
                    _res.append(
                        [attribute, channel, group1, group2, *means, *mu]
                    )
        cols = [
            "attribute",
            "channel",
            "group1",
            "group2",
            "mean1",
            "mean2",
            "log2_fold",
            "stat",
            "p_value",
        ]
        channel_stats = pd.DataFrame(_res, columns=cols)
        channel_stats["p_adj"] = multipletests(
            channel_stats["p_value"], method="fdr_bh"
        )[1]

        # # # remove duplication due to lazy itertools.permutations
        channel_stats["abs_log2_fold"] = channel_stats["log2_fold"].abs()
        channel_stats = (
            channel_stats.drop_duplicates(
                subset=["attribute", "channel", "abs_log2_fold", "p_value"]
            )
            .drop("abs_log2_fold", axis=1)
            .reset_index(drop=True)
        )
        # # #  reorder so taht "Healthy" is in second column always
        for i, row in channel_stats.iterrows():
            if "Healthy" in row["group1"]:
                row["group1"] = row["group2"]
                row["group2"] = "Healthy"
                row["log2_fold"] = -row["log2_fold"]
                channel_stats.loc[i] = row
        # # # save
        channel_stats.to_csv(
            output_prefix + f"channel_mean.testing_between_attributes.csv",
            index=False,
        )

        # # clusters
        _res = list()
        for attribute in sample_attributes:
            for cluster in cluster_df["cluster"].unique():
                for group1, group2 in itertools.permutations(
                    cluster_df[attribute].unique(), 2
                ):
                    a = cluster_df.query(
                        f"cluster == '{cluster}' & {attribute} == '{group1}'"
                    )["cell_count"]
                    b = cluster_df.query(
                        f"cluster == '{cluster}' & {attribute} == '{group2}'"
                    )["cell_count"]
                    am = a.mean()
                    bm = b.mean()
                    means = [am, bm, np.log2(a.mean() / b.mean())]
                    try:
                        mu = mannwhitneyu(a, b)
                    except ValueError:
                        mu = (np.nan, np.nan)
                    _res.append(
                        [attribute, cluster, group1, group2, *means, *mu]
                    )
        cols = [
            "attribute",
            "cluster",
            "group1",
            "group2",
            "mean1",
            "mean2",
            "log2_fold",
            "stat",
            "p_value",
        ]
        cluster_stats = pd.DataFrame(_res, columns=cols)
        cluster_stats["p_adj"] = multipletests(
            cluster_stats["p_value"], method="fdr_bh"
        )[1]

        # # # remove duplication due to lazy itertools.permutations
        cluster_stats["abs_log2_fold"] = cluster_stats["log2_fold"].abs()
        cluster_stats = (
            cluster_stats.drop_duplicates(
                subset=["attribute", "cluster", "abs_log2_fold", "p_value"]
            )
            .drop("abs_log2_fold", axis=1)
            .reset_index(drop=True)
        )
        # # # reorder so taht "Healthy" is in second column always
        for i, row in cluster_stats.iterrows():
            if "Healthy" in row["group1"]:
                row["group1"] = row["group2"]
                row["group2"] = "Healthy"
                row["log2_fold"] = -row["log2_fold"]
                cluster_stats.loc[i] = row
        # # # save
        cluster_stats.to_csv(
            output_prefix
            + f"cell_type_abundance.testing_between_attributes.csv",
            index=False,
        )

        # Filter out rare cell types if required
        filtered_cluster_df = cluster_df.loc[
            cluster_df["cluster"].isin(filtered_clusters)
        ]

        # Plot
        # # barplots
        # # # channel means
        n = len(sample_attributes)
        kwargs = dict(
            x="value", y="channel", orient="horiz", ci="sd", data=channel_df
        )  # , estimator=np.std)
        fig, axes = plt.subplots(
            n, 2, figsize=(5 * 2, 10 * n), squeeze=False, sharey="row"
        )
        for i, attribute in enumerate(sample_attributes):
            for axs in axes[i, (0, 1)]:
                sns.barplot(**kwargs, hue=attribute, ax=axs)
            axes[i, 1].set_xscale("log")
            for axs, lab in zip(
                axes[i, :], ["Channel mean", "Channel mean (log)"]
            ):
                axs.set_xlabel(lab)
        fig.savefig(
            output_prefix + f"channel_mean.by_{attribute}.barplot.svg",
            **FIG_KWS,
        )

        # # # clusters
        # # # # plot once for all cell types, another time excluding rare cell types
        n = len(sample_attributes)
        kwargs = dict(
            y="cluster", orient="horiz", ci="sd"
        )  # , estimator=np.std)
        for label, pl_df in [
            ("all_clusters", cluster_df),
            ("filtered_clusters", filtered_cluster_df),
        ]:
            fig, axes = plt.subplots(
                n, 3, figsize=(5 * 3, 10 * n), squeeze=False, sharey="row"
            )
            for i, attribute in enumerate(sample_attributes):
                for axs in axes[i, (0, 1)]:
                    sns.barplot(
                        **kwargs,
                        x="cell_count",
                        hue=attribute,
                        data=pl_df,
                        ax=axs,
                    )
                axes[i, 1].set_xscale("log")
                sns.barplot(
                    **kwargs,
                    x="cell_perc",
                    hue=attribute,
                    data=pl_df,
                    ax=axes[i, 2],
                )
                for axs, lab in zip(
                    axes[i, :],
                    ["Cell count", "Cell count (log)", "Cell percentage"],
                ):
                    axs.set_xlabel(lab)
            fig.savefig(
                output_prefix
                + f"cell_type_abundance.by_{attribute}.barplot.svg",
                **FIG_KWS,
            )

        # # volcano
        # # # channels
        n = len(sample_attributes)
        m = (
            channel_stats[["attribute", "group1", "group2"]]
            .drop_duplicates()
            .groupby("attribute")
            .count()
            .max()
            .max()
        )
        fig, axes = plt.subplots(
            n,
            m,
            figsize=(m * 5, n * 5),
            squeeze=False,  # sharex="row", sharey="row"
        )
        fig.suptitle("Changes in mean channel intensity")
        for i, attribute in enumerate(sample_attributes):
            p = channel_stats.query(f"attribute == '{attribute}'")
            for j, (_, (group1, group2)) in enumerate(
                p[["group1", "group2"]].drop_duplicates().iterrows()
            ):
                q = p.query(f"group1 == '{group1}' & group2 == '{group2}'")
                y = -np.log10(q["p_value"])
                v = q["log2_fold"].abs().max()
                v *= 1.2
                axes[i, j].scatter(q["log2_fold"], y, c=y, cmap="autumn_r")
                for k, row in q.query("p_value < 0.05").iterrows():
                    axes[i, j].text(
                        row["log2_fold"],
                        -np.log10(row["p_value"]),
                        s=row["channel"],
                        fontsize=5,
                        ha="left" if np.random.rand() > 0.5 else "right",
                    )
                axes[i, j].axvline(0, linestyle="--", color="grey")
                title = attribute + f"\n{group1} vs {group2}"
                axes[i, j].set(
                    xlabel="log2(fold-change)",
                    ylabel="-log10(p-value)",
                    title=title,
                )  # , xlim=(-v, v))
            for axs in axes[i, j + 1 :]:
                axs.axis("off")
        fig.savefig(
            output_prefix + f"channel_mean.by_{attribute}.volcano.svg",
            **FIG_KWS,
        )

        # # # clusters
        n = len(sample_attributes)
        m = (
            cluster_stats[["attribute", "group1", "group2"]]
            .drop_duplicates()
            .groupby("attribute")
            .count()
            .max()
            .max()
        )
        fig, axes = plt.subplots(
            n,
            m,
            figsize=(m * 5, n * 5),
            squeeze=False,  # sharex="row", sharey="row"
        )
        fig.suptitle("Changes in cell type composition\nfor each cell type")
        for i, attribute in enumerate(sample_attributes):
            p = cluster_stats.query(f"attribute == '{attribute}'")
            for j, (_, (group1, group2)) in enumerate(
                p[["group1", "group2"]].drop_duplicates().iterrows()
            ):
                q = p.query(f"group1 == '{group1}' & group2 == '{group2}'")
                y = -np.log10(q["p_value"])
                v = q["log2_fold"].abs().max()
                v *= 1.2
                axes[i, j].scatter(q["log2_fold"], y, c=y, cmap="autumn_r")
                for k, row in q.query("p_value < 0.05").iterrows():
                    axes[i, j].text(
                        row["log2_fold"],
                        -np.log10(row["p_value"]),
                        s=row["cluster"],
                        fontsize=5,
                        ha="left" if np.random.rand() > 0.5 else "right",
                    )
                axes[i, j].axvline(0, linestyle="--", color="grey")
                title = attribute + f"\n{group1} vs {group2}"
                axes[i, j].set(
                    xlabel="log2(fold-change)",
                    ylabel="-log10(p-value)",
                    title=title,
                )  # , xlim=(-v, v))
            for axs in axes[i, j + 1 :]:
                axs.axis("off")
        fig.savefig(
            output_prefix + f"cell_type_abundance.by_{attribute}.volcano.svg",
            **FIG_KWS,
        )

        # # heatmap of cell type counts
        cluster_counts = (
            self.clusters.reset_index()
            .assign(count=1)
            .pivot_table(
                index="cluster",
                columns="roi",
                aggfunc=sum,
                values="count",
                fill_value=0,
            )
        )
        roi_areas = pd.Series(
            [np.multiply(*roi.shape[1:]) for roi in rois],
            index=[roi.name for roi in rois],
        )

        cluster_densities = (cluster_counts / roi_areas) * 1e4
        grid = sns.clustermap(
            cluster_densities,
            metric="correlation",
            cbar_kws=dict(label="Cells per area unit (x1e4)"),
            robust=True,
            xticklabels=True,
            yticklabels=True,
        )
        grid.savefig(
            output_prefix + "cell_type_abundance.by_area.svg", **FIG_KWS
        )

        grid = sns.clustermap(
            cluster_densities,
            metric="correlation",
            z_score=0,
            cmap="RdBu_r",
            center=0,
            cbar_kws=dict(label="Cells per area unit (Z-score)"),
            robust=True,
            xticklabels=True,
            yticklabels=True,
        )
        grid.savefig(
            output_prefix + "cell_type_abundance.by_area.zscore.svg", **FIG_KWS
        )

    def measure_adjacency(
        self,
        output_prefix: Optional[Path] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
    ) -> None:
        """
        Derive cell adjacency graphs for each ROI.
        """
        output_prefix = (
            output_prefix or self.results_dir / "single_cell" / self.name + "."
        )
        rois = self._get_rois(samples, rois)

        # Get graph for missing ROIs
        _rois = [r for r in rois if r._adjacency_graph is None]
        if _rois:
            gs = parmap.map(get_adjacency_graph, _rois, pm_pbar=True)
            # gs = [get_adjacency_graph(roi) for roi in _rois]
            for roi, g in zip(_rois, gs):
                roi._adjacency_graph = g

        # TODO: package the stuff below into a function

        # First measure adjacency as odds against background
        freqs = parmap.map(measure_cell_type_adjacency, rois)
        # freqs = [measure_cell_type_adjacency(roi) for roi in rois]
        # freqs = [
        #     pd.read_csv(
        #         roi.sample.root_dir / "single_cell" / roi.name
        #         + ".cluster_adjacency_graph.norm_over_random.csv",
        #         index_col=0,
        #     )
        #     for roi in rois
        # ]

        melted = pd.concat(
            [
                f.reset_index()
                .melt(id_vars="index")
                .assign(sample=roi.sample.name, roi=roi.name)
                for roi, f in zip(rois, freqs)
            ]
        )

        # mean_f = melted.pivot_table(
        #     index="index", columns="variable", values="value", aggfunc=np.mean
        # )
        # sns.clustermap(mean_f, cmap="RdBu_r", center=0, robust=True)

        v = np.percentile(melted["value"].abs(), 95)
        n, m = get_grid_dims(len(freqs))
        fig, axes = plt.subplots(
            n, m, figsize=(m * 5, n * 5), sharex=True, sharey=True
        )
        axes = axes.flatten()
        i = -1
        for i, (dfs, roi) in enumerate(zip(freqs, rois)):
            axes[i].set_title(roi.name)
            sns.heatmap(
                dfs,
                ax=axes[i],
                cmap="RdBu_r",
                center=0,
                rasterized=True,
                square=True,
                xticklabels=True,
                yticklabels=True,
                vmin=-v,
                vmax=v,
            )
        for axs in axes[i + 1 :]:
            axs.axis("off")
        fig.savefig(output_prefix + "adjacency.all_rois.pdf", **FIG_KWS)

    def find_communities(
        self,
        output_prefix: Optional[Path] = None,
        samples: Optional[List["IMCSample"]] = None,
        rois: Optional[List["ROI"]] = None,
        **kwargs,
    ) -> None:
        """
        Find communities and supercommunities of cell types across all images.
        """
        rois = self._get_rois(samples, rois)
        cluster_communities(rois=rois, output_prefix=output_prefix, **kwargs)
