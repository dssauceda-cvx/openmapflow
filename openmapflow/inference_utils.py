import os
import re
from collections import defaultdict
from glob import glob
from pathlib import Path

from cropharvest.countries import BBox
import ee
from google.cloud import storage
from tqdm.notebook import tqdm
from typing import Dict, List, Optional, Tuple

from openmapflow.config import GCLOUD_PROJECT_ID
from openmapflow.config import BucketNames as bn
from openmapflow.labeled_dataset import bbox_from_str


def get_available_bboxes(
    buckets_to_check: List[str] = [bn.INFERENCE_TIFS],
) -> List[BBox]:
    """
    Get all available bboxes from the given buckets using regex.
    Args:
        buckets_to_check: List of buckets to check.
    Returns:
        List of BBoxes.
    """
    if len(buckets_to_check) == 0:
        raise ValueError("No buckets to check")
    client = storage.Client()
    previous_matches = []
    available_bboxes = []
    bbox_regex = (
        r".*min_lat=-?\d*\.?\d*_min_lon=-?\d*\.?\d*_max_lat=-?\d*\.?\d*_max_lon=-?\d*\.?\d*_"
        + r"dates=\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}.*?\/"
    )
    for bucket_name in buckets_to_check:
        for blob in client.list_blobs(bucket_or_name=bucket_name):
            match = re.search(bbox_regex, blob.name)
            if not match:
                continue
            p = match.group()
            if p not in previous_matches:
                previous_matches.append(p)
                available_bboxes.append(bbox_from_str(f"gs://{bucket_name}/{p}"))
    return available_bboxes


def get_ee_task_amount(prefix: Optional[str] = None):
    """
    Gets amount of active tasks in Earth Engine.
    Args:
        prefix: Prefix to filter tasks.
    Returns:
        Amount of active tasks.
    """
    amount = 0
    task_list = ee.data.getTaskList()
    for t in tqdm(task_list):
        valid_state = t["state"] in ["READY", "RUNNING"]
        if valid_state and (prefix is None or prefix in t["description"]):
            amount += 1
    return amount


def get_gcs_file_amount(
    bucket_name: str, prefix: str, project: str = GCLOUD_PROJECT_ID
) -> int:
    blobs = storage.Client(project=project).list_blobs(bucket_name, prefix=prefix)
    return len(list(blobs))


def get_gcs_file_dict_and_amount(
    bucket_name: str, prefix: str, project: str = GCLOUD_PROJECT_ID
) -> Tuple[Dict[str, List[str]], int]:
    """
    Gets a dictionary of all files in a bucket with their amount.
    Returns:
        Dictionary of files and their amount.
    """
    blobs = storage.Client(project=project).list_blobs(bucket_name, prefix=prefix)
    files_dict = defaultdict(lambda: [])
    amount = 0
    for blob in tqdm(blobs, desc=f"From {bucket_name}"):
        p = Path(blob.name)
        files_dict[str(p.parent)].append(p.stem.replace("pred_", ""))
        amount += 1
    return files_dict, amount


def print_between_lines(text: str, line: str = "-", is_tabbed: bool = False):
    tab = "\t" if is_tabbed else ""
    print(tab + (line * len(text)))
    print(tab + text)
    print(tab + (line * len(text)))


def get_status(prefix: str) -> Tuple[int, int, int]:
    """
    Args:
        prefix: Prefix to filter tasks.
    Returns:
        Amount of active tasks, amount of files in Google Cloud storage available for inference,
        amount of predictions made.
    """
    print_between_lines(prefix)
    ee_task_amount = get_ee_task_amount(prefix=prefix.replace("/", "-"))
    tifs_amount = get_gcs_file_amount(bn.INFERENCE_TIFS, prefix=prefix)
    predictions_amount = get_gcs_file_amount(bn.PREDS, prefix=prefix)
    print(f"1) Obtaining input data: {ee_task_amount}")
    print(f"2) Input data available: {tifs_amount}")
    print(f"3) Predictions made: {predictions_amount}")
    return ee_task_amount, tifs_amount, predictions_amount


def find_missing_predictions(
    prefix: str, verbose: bool = False
) -> Dict[str, List[str]]:
    """
    Finds all missing predictions by data available for
    inference with the current predictions made
    Args:
        prefix: Prefix to filter tasks.
        verbose: Whether to print the progress.
    Returns:
        Dictionary of missing predictions.
    """
    print("Addressing missing files")
    tif_files, tif_amount = get_gcs_file_dict_and_amount(bn.INFERENCE_TIFS, prefix)
    pred_files, pred_amount = get_gcs_file_dict_and_amount(bn.PREDS, prefix)
    missing = {}
    for full_k in tqdm(tif_files.keys(), desc="Missing files"):
        if full_k not in pred_files:
            diffs = tif_files[full_k]
        else:
            diffs = list(set(tif_files[full_k]) - set(pred_files[full_k]))
        if len(diffs) > 0:
            missing[full_k] = diffs

    batches_with_issues = len(missing.keys())
    if verbose:
        print_between_lines(prefix)

    if batches_with_issues == 0:
        print("\u2714 all files in each batch match")
        return missing

    print(
        f"\u2716 {batches_with_issues}/{len(tif_files.keys())} "
        + f"batches have a total {tif_amount - pred_amount} missing predictions"
    )

    if verbose:
        for batch, files in missing.items():
            print_between_lines(
                text=f"\t{Path(batch).stem}: {len(files)}", is_tabbed=True
            )
            for f in files:
                print(f"\t{f}")

    return missing


def make_new_predictions(
    missing: Dict[str, List[str]], bucket_name: str = bn.INFERENCE_TIFS
):
    """
    Renames missing files which retriggers inference.
    Args:
        missing: Dictionary of missing predictions.
        bucket_name: Bucket name to rename files in.
    """
    bucket = storage.Client(project=GCLOUD_PROJECT_ID).bucket(bucket_name)
    for batch, files in tqdm(missing.items(), desc="Going through batches"):
        for file in tqdm(files, desc="Renaming files", leave=False):
            blob_name = f"{batch}/{file}.tif"
            blob = bucket.blob(blob_name)
            if blob.exists():
                new_blob_name = f"{batch}/{file}-retry.tif"
                bucket.rename_blob(blob, new_blob_name)
            else:
                print(f"Could not find: {blob_name}")


def gdal_cmd(cmd_type: str, in_file: str, out_file: str, msg=None, print_cmd=False):
    """
    Runs a GDAL command: gdalbuildvrt or gdal_translate.
    """
    if cmd_type == "gdalbuildvrt":
        cmd = f"gdalbuildvrt {out_file} {in_file}"
    elif cmd_type == "gdal_translate":
        cmd = f"gdal_translate -a_srs EPSG:4326 -of GTiff {in_file} {out_file}"
    else:
        raise NotImplementedError(f"{cmd_type} not implemented.")
    if msg:
        print(msg)
    if print_cmd:
        print(cmd)
    os.system(cmd)


def build_vrt(prefix):
    """
    Builds a VRT file for each batch and then creates one VRT file for all batches.
    """
    print("Building vrt for each batch")
    for d in tqdm(glob(f"{prefix}_preds/*/*/")):
        if "batch" not in d:
            continue

        match = re.search("batch_(.*?)/", d)
        if match:
            i = int(match.group(1))
        else:
            raise ValueError(f"Cannot parse i from {d}")
        vrt_file = Path(f"{prefix}_vrts/{i}.vrt")
        if not vrt_file.exists():
            gdal_cmd(cmd_type="gdalbuildvrt", in_file=f"{d}*", out_file=str(vrt_file))

    gdal_cmd(
        cmd_type="gdalbuildvrt",
        in_file=f"{prefix}_vrts/*.vrt",
        out_file=f"{prefix}_final.vrt",
        msg="Building full vrt",
    )
