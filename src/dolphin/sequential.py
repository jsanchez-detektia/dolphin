"""Estimate wrapped phase using batches of ministacks."""
from collections import defaultdict
from math import nan
from pathlib import Path
from typing import List

import numpy as np
from osgeo_utils import gdal_calc

from dolphin.log import get_log
from dolphin.phase_link.mle_gpu import compress, run_mle_gpu
from dolphin.utils import Pathlike, get_raster_xysize, load_gdal, save_arr_like
from dolphin.vrt import VRTStack

logger = get_log()


def run_evd_sequential(
    *,
    slc_vrt_file: Pathlike,
    # weight_file: Pathlike,
    output_folder: Pathlike,
    window: dict,
    ministack_size: int = 10,
    mask_file: Pathlike = None,
    # ps_mask_file: Pathlike = None,
    beta: float = 0.1,
):
    """Estimate wrapped phase using batches of ministacks."""
    # TODO: work in blocks?
    output_folder = Path(output_folder)
    v_all = VRTStack.from_vrt_file(slc_vrt_file)
    file_list_all = v_all.file_list
    logger.info(f"{v_all}: from {v_all.file_list[0]} to {v_all.file_list[-1]}")

    comp_slc_files: List[Pathlike] = []
    output_slc_files = defaultdict(list)  # Map of {ministack_index: [output_slc_files]}

    if mask_file is not None:
        mask = load_gdal(mask_file).astype(bool)
    else:
        xsize, ysize = get_raster_xysize(v_all.file_list[0])
        mask = np.zeros((ysize, xsize), dtype=bool)

    # Solve each ministack using the current chunk (and the previous compressed SLCs)
    ministack_starts = range(0, len(file_list_all), ministack_size)
    for mini_idx, full_stack_idx in enumerate(ministack_starts):
        cur_files = file_list_all[
            full_stack_idx : full_stack_idx + ministack_size
        ].copy()
        name_start, name_end = cur_files[0].stem, cur_files[-1].stem
        start_end = f"{name_start}_{name_end}"

        # Make a new output folder for each ministack
        cur_output_folder = output_folder / start_end
        cur_output_folder.mkdir(parents=True, exist_ok=True)
        logger.info(f"Processing {len(cur_files)} files into {cur_output_folder}")

        # Add the existing compressed SLC files to the start
        cur_files = comp_slc_files + cur_files
        cur_vrt = VRTStack(cur_files, outfile=cur_output_folder / f"{start_end}.vrt")

        # mini_idx is first non-compressed SLC
        logger.info(
            f"{cur_vrt}: from {Path(cur_vrt.file_list[mini_idx]).name} to"
            f" {Path(cur_vrt.file_list[-1]).name}"
        )
        cur_data = cur_vrt.read()

        # Run the phase linking process on the current ministack
        cur_mle_stack = run_mle_gpu(
            cur_data,
            half_window=(window["xhalf"], window["yhalf"]),
            beta=beta,
            reference_idx=mini_idx,
            mask=mask,
        )
        logger.info(f"Finished ministack {mini_idx} of size {cur_mle_stack.shape}.")

        # Save each of the MLE estimates (ignoring the compressed SLCs)
        for filename, cur_image in zip(cur_files[mini_idx:], cur_mle_stack[mini_idx:]):
            slc_name = filename.stem
            cur_filename = cur_output_folder / f"{slc_name}.bin"
            # Save the MLE estimate using the slc_vrt_file's geotransform
            save_arr_like(
                arr=cur_image,
                like_filename=slc_vrt_file,
                output_name=cur_filename,
                driver="ENVI",
            )
            output_slc_files[mini_idx].append(cur_filename)

        # Compress the ministack using only the non-compressed SLCs
        cur_comp_slc = compress(cur_data[mini_idx:], cur_mle_stack[mini_idx:])
        # Save the compressed SLC
        cur_comp_slc_file = cur_output_folder / f"compressed_{start_end}.bin"
        logger.info(f"Saving compressed SLC to {cur_comp_slc_file}")
        save_arr_like(
            arr=cur_comp_slc,
            like_filename=slc_vrt_file,
            output_name=cur_comp_slc_file,
            driver="ENVI",
        )

        # Add it to the list of compressed SLCs so we can
        # prepend it to the VRTStack.file_list
        comp_slc_files.append(cur_comp_slc_file)

    # Find the offsets between stacks by doing a phase linking only compressed SLCs
    adjustment_vrt_stack = VRTStack(
        comp_slc_files, outfile=output_folder / "compressed_stack.vrt"
    )
    logger.info(f"Running EVD on compressed files: {adjustment_vrt_stack}")
    comp_data = adjustment_vrt_stack.read()
    comp_mle_result = run_mle_gpu(
        comp_data,
        half_window=(window["xhalf"], window["yhalf"]),
    )
    comp_output_folder = output_folder / "adjustments"
    comp_output_folder.mkdir(parents=True, exist_ok=True)
    for fname, cur_image in zip(adjustment_vrt_stack.file_list, comp_mle_result):
        name = Path(fname).stem
        cur_filename = comp_output_folder / f"{name}.bin"
        save_arr_like(
            arr=cur_image,
            like_filename=slc_vrt_file,
            output_name=cur_filename,
            driver="ENVI",
        )

    # Compensate for the offsets between ministacks (aka "datum adjustments")
    final_output_folder = output_folder / "final"
    final_output_folder.mkdir(parents=True, exist_ok=True)
    for mini_idx, slc_files in output_slc_files.items():
        adjustment_fname = comp_slc_files[mini_idx]
        driver = "ENVI"
        for slc_fname in slc_files:
            # name = slc_fname.stem
            logger.info(f"Compensating {slc_fname} with {adjustment_fname}")
            outfile = final_output_folder / f"{slc_fname.name}"

            gdal_calc.Calc(
                NoDataValue=nan,
                format=driver,
                outfile=outfile,
                A=slc_fname,
                B=adjustment_fname,
                calc="A * exp(1j * angle(B))",
                quiet=True,
                overwrite=True,
            )
            # TODO: need to copy projection?
            # TODO: delete old?
