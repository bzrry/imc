#!/usr/bin/env python

"""
Segment image stacks.
"""

import sys
import argparse
from typing import List, Optional
from dataclasses import dataclass

import pandas as pd
import tifffile
import matplotlib.pyplot as plt

from imc.types import Path, Series, Array
from imc.segmentation import segment_roi
from imc.scripts import cli_config


@dataclass
class ROI_mock:
    # CYX array
    stack_file: Path
    # Series where index are int, values are channel names
    channel_labels_file: Path
    # Series where index are channels, values are bool
    channel_exclude: Optional[Series] = None

    name: Optional[str] = None

    def __repr__(self):
        return self.name

    @property
    def stack(self) -> Array:
        return tifffile.imread(self.stack_file)

    @property
    def channel_labels(self) -> Series:
        return pd.read_csv(self.channel_labels_file, index_col=0, squeeze=True)


def main(cli: List[str] = None) -> int:
    parser = get_args()
    args = parser.parse_args(cli)

    fs = "\n\t- " + "\n\t- ".join([f.as_posix() for f in args.tiffs])
    print(f"Starting analysis of {len(args.tiffs)} TIFF files: {fs}!")

    # Prepare mock ROI objects
    rois = list()
    for tiff in args.tiffs:
        channel_labels_file = tiff.replace_(".tiff", ".csv")
        if not channel_labels_file.exists():
            print(
                "Stack file does not have accompaning "
                f"channel labels CSV file: '{tiff}'"
            )
            continue

        roi = ROI_mock(
            tiff, channel_labels_file, name=tiff.stem.replace("_full", "")
        )

        # convert string given at CLI to channel_exclude Series
        exc = pd.Series(index=roi.channel_labels, dtype=bool)
        if args.channel_exclude != "":
            for ch in args.channel_exclude.split(","):
                exc.loc[exc.index.str.contains(ch)] = True
        roi.channel_exclude = exc
        rois.append(roi)

    # Run segmentation
    for roi in rois:
        print(f"Started segmentation of '{roi} with shape: '{roi.stack.shape}'")
        mask = segment_roi(
            roi,
            args.model,
            args.compartment,
            False,
            args.overwrite,
            args.plot,
        )
        if args.save:
            add = "nuc" if args.compartment == "nuclear" else ""
            mask_file = roi.stack_file.replace_(
                ".tiff", f"_{add}mask{args.output_mask_suffix}.tiff"
            )
            if args.overwrite or (not mask_file.exists()):
                tifffile.imwrite(mask_file, mask)

            if args.plot:
                fig_file = roi.stack_file.replace_(
                    "_full.tiff",
                    f"_segmentation_{args.model}_{args.compartment}.svg",
                )
                if args.overwrite or (
                    not args.overwrite and not fig_file.exists()
                ):
                    fig = plt.gca().figure
                    print(fig, fig_file)
                    fig.savefig(fig_file, dpi=300, bbox_inches="tight")
        print(f"Finished segmentation of '{roi}'.")

    print("Finished with all files!")
    return 0


def get_args() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(**cli_config["subcommands"]["segment"])  # type: ignore[index]
    _help = "TIFF files with array stack."
    parser.add_argument(dest="tiffs", nargs="+", type=Path, help=_help)
    parser.add_argument(
        "-m", "--model", choices=["stardist", "deepcell"], default="stardist"
    )
    parser.add_argument(
        "-c",
        "--compartment",
        choices=["nuclear", "cytoplasm", "both"],
        default="nuclear",
    )
    _help = "Comma-delimited list of channels to exclude from stack."
    parser.add_argument("-e", "--channel-exclude", default="")
    parser.add_argument("--output-mask-suffix", default="")
    parser.add_argument("--no-save", dest="save", action="store_false")
    parser.add_argument("--overwrite", dest="overwrite", action="store_true")
    parser.add_argument("--no-plot", dest="plot", action="store_false")
    return parser


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
