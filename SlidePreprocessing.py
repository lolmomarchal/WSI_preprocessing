# outside imports
import argparse
import csv
import multiprocessing
import os
import shutil

import numpy as np
import pandas as pd
import torch
from PIL import Image

# classes/functions
import Reports
import SlideEncoding
import TileQualityFilters
import VisulizationUtils
from TileNormalization import normalizeStaining
from TissueMask import TissueMask
from TissueSlide import TissueSlide

"""
SlidePreprocessing.py

Author: Lorenzo Olmo Marchal
Created: March 5, 2024
Last Updated:  June 12, 2024

Description:
This script automates the preprocessing and normalization of Whole Slide Images (WSI) in digital histopathology. 
Input:
- slide directory path or slide path
- slide directory output path

Output:
Processed tiles are saved in the output directory. Each tile is accompanied by metadata in a csv, including its origin
within the WSI file.

"""

summary = []
errors = []
# Defaults
MAG = 20
DESIRED_SIZE = 256
OVERLAP = 0
RECONSTRUCT = False


def best_size(slide: TissueSlide, size, mag) -> int:
    """
Determines the best size for a tile based on its magnification compared to a reference value.

i.e. if natural magnification = 40 and desired magnification = 20 with size 256, return 512

Parameters:
    slide (Slide): The slide object representing the image.
    magnification_reference (int, optional): The reference magnification used for scaling. Defaults to 20.
    size (int, optional): The base size for the image. Defaults to 256.

Returns:
    int: The calculated desired size for the tile.
"""
    if mag > slide.magnification:
        return None

    natural_mag = slide.magnification
    new_size = natural_mag / mag
    return int(size * new_size)


def tiling(ts: TissueSlide, result_path: str, mask: TissueMask, overlap=OVERLAP, desired_size=DESIRED_SIZE,
           mag=MAG, threshold=0.8) -> int:
    """
Tiles the provided slide according to TissueMask

Parameters:
    ts (Slide): The slide object representing the image.
    result_path (int, optional): Where the tiles should be saved.
    mask (TissueMask): mask of the image.
    overlap (bool): if there should be an overlap between tiles.
    desired_size (int, optional): The base size for the tile. Defaults to 256.

Returns:
    int: Number of tiles found.

"""
    print("Tiling slide")
    # Make tile dir
    if not os.path.exists(result_path):
        os.makedirs(result_path)
    tiles = os.path.join(result_path, "tiles")

    columns = ['patient_id', 'x', 'y', 'magnification', 'size', 'path_to_slide', 'scale']
    df_list = []
    os.makedirs(os.path.join(result_path, "tiles"), exist_ok=True)

    size = best_size(ts, desired_size, mag)
    if size is None:
        errors.append((ts.id, ts.path, "The desired magnification is greater than the slide magnification.", "Tiling"))
        return None
    w = ts.dimensions[0]
    h = ts.dimensions[1]

    if overlap != 0:
        stride = desired_size // overlap
    else:
        stride = size

    for x in range(0, w - size + 1, stride):
        for y in range(0, h - size + 1, stride):
            if x <= w - size and y <= h - size:
                tissue_rgb = ts.slide.read_region((x, y), 0, (size, size))
                tile_mask = mask.get_region_mask((x, y), (size, size))
                # get mask of tile
                if mask.is_tissue(tile_mask, threshold=threshold):
                    tile = tissue_rgb.convert("RGB")
                    tile = tile.resize((desired_size, desired_size))
                    tile.save(os.path.join(tiles,
                                           f"{ts.id}_tile_w{x}_h{y}_mag{ts.magnification}_size{size}_scale{ts.SCALE}.png"))
                    df_list.append({
                        'patient_id': ts.id,
                        'x': x,
                        'y': y,
                        'magnification': ts.magnification,
                        'size': size,
                        'path_to_slide': os.path.join(tiles,
                                                      f"{ts.id}_tile_w{x}_h{y}_mag{ts.magnification}_size{size}_scale{ts.SCALE}.png"),
                        'scale': ts.SCALE
                    })
    df = pd.DataFrame(df_list, columns=columns)

    df.to_csv(os.path.join(result_path, "tile_information.csv"), index=False)
    return len(df)


def normalize_tiles(tile_information: str, result_path: str, device="cpu"):
    """
   Normalizes the tiles

   Parameters:
       tile_information (str): path to csv with all the tiles to be normalized .
       result_path (str): path where the normalized tiles should be saved.
   """
    print("Normalizing tiles")
    tiles = pd.read_csv(tile_information)
    path = os.path.join(result_path, "normalized_tiles")
    df_list = []
    columns = ['patient_id', 'x', 'y', 'magnification', 'size', 'path_to_slide', 'scale']
    os.makedirs(os.path.join(result_path, "normalized_tiles"), exist_ok=True)
    for i, row in tiles.iterrows():
        path_to_tile = row["path_to_slide"]
        mag = row["magnification"]
        size = row["size"]
        y = row["y"]
        x = row["x"]
        id = row["patient_id"]
        scale = row["scale"]
        try:
            tile = np.array(Image.open(path_to_tile))
            if tile is not None:
                # tile = torch.from_numpy(tile).to(device)
                normalizeStaining(tile,
                                  os.path.join(path,
                                               f"{id}_tile_w{x}_h{y}_mag{mag}_size{size}_scale{scale}.png"))

                df_list.append({
                    'patient_id': id,
                    'x': x,
                    'y': y,
                    'magnification': mag,
                    'size': size,
                    'path_to_slide': os.path.join(path, f"{id}_tile_w{x}_h{y}_mag{mag}_size{size}_scale{scale}.png"),
                    'scale': scale
                })

            else:
                print("Error: Input tile is None.")

        except Exception as e:
            global errors
            errors.append((id, path_to_tile, e, "Normalization"))
    df = pd.DataFrame(df_list, columns=columns)
    df.to_csv(os.path.join(result_path, "normalized_tile_information.csv"), index=False)


def blurry_filter(tile_information, result_path, threshold=0.015):
    """
Removes any tiles that might be blurry using a laplacian filter

Parameters:
   tile_information (str): path to csv with all the tiles to be checked (normalized tiles).
   result_path (str): path where the tiles that passed should be saved.
"""
    print("removing blurry tiles")

    tiles = pd.read_csv(tile_information)

    # creating folders for both in focus and out of focus tiles for efficacy of filter
    path = os.path.join(result_path, "infocus_tiles")
    blurry_path = os.path.join(result_path, "outfocus_tiles")
    os.makedirs(os.path.join(result_path, "infocus_tiles"), exist_ok=True)
    os.makedirs(os.path.join(result_path, "outfocus_tiles"), exist_ok=True)

    # creating csv for ease of use access
    df_list = []
    columns = ['patient_id', 'x', 'y', 'magnification', 'size', 'path_to_slide', 'scale']
    for i, row in tiles.iterrows():
        path_to_tile = row["path_to_slide"]
        mag = row["magnification"]
        size = row["size"]
        y = row["y"]
        x = row["x"]
        id = row["patient_id"]
        scale = row["scale"]
        try:
            tile = np.array(Image.open(path_to_tile))
            if tile is not None:
                not_blurry = TileQualityFilters.LaplaceFilter(tile, var_threshold=threshold)
                if not_blurry:
                    img = Image.open(path_to_tile)
                    img.save(os.path.join(path, f"{id}_tile_w{x}_h{y}_mag{mag}_size{size}_scale{scale}.png"))
                    df_list.append({
                        'patient_id': id,
                        'x': x,
                        'y': y,
                        'magnification': mag,
                        'size': size,
                        'path_to_slide': os.path.join(path,
                                                      f"{id}_tile_w{x}_h{y}_mag{mag}_size{size}_scale{scale}.png"),
                        'scale': scale
                    })
                else:
                    img = Image.open(path_to_tile)
                    img.save(os.path.join(blurry_path, f"{id}_tile_w{x}_h{y}_mag{mag}_size{size}_scale{scale}.png"))
        except Exception as e:
            print(f"An error occurred: {e}")
            global errors
            errors.append((id, path_to_tile, e, "Blur filter."))

    df = pd.DataFrame(df_list, columns=columns)
    df.to_csv(os.path.join(result_path, "infocus_tile_information.csv"), index=False)
    return len(df)


def move_svs_files(main_directory, results_path):
    # Create a CSV file to store patient ID and SVS file paths

    # Iterate through each patient directory
    for patient_directory in os.listdir(main_directory):
        patient_path = os.path.join(main_directory, patient_directory)

        if os.path.isdir(patient_path):
            # Look for SVS files in the patient directory
            for file_name in os.listdir(patient_path):
                if file_name.endswith((".svs", ".tif", ".dcm", ".ndpi", ".vms", ".vmu", ".scn", ".mrxs", ".tiff",
                                       ".svslide", ".bif")):
                    svs_file_path = os.path.join(patient_path, file_name)

                    # Move the SVS file to the main directory
                    shutil.move(svs_file_path, main_directory)


def patient_csv(input_path, results_path):
    csv_file_path = os.path.join(results_path, "patient_files.csv")
    with open(csv_file_path, 'w', newline='') as csvfile:

        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["Patient ID", "Original Slide Path", "Preprocessing Path"])

        # check if provided with a directory of samples or a sample

        if os.path.isdir(input_path):
            for file in os.listdir(input_path):

                if file.endswith((".svs", ".tif", ".dcm", ".ndpi", ".vms", ".vmu", ".scn", ".mrxs", ".tiff", ".svslide",
                                  ".bif")):

                    patient_id = os.path.basename(file)
                    patient_id = patient_id.split(".")[0]
                    patient_result_path = os.path.join(results_path, patient_id)

                    if not os.path.exists(patient_result_path):
                        os.makedirs(patient_result_path)
                    csv_writer.writerow([patient_id, os.path.join(input_path, file),
                                         os.path.join(results_path, patient_id)])
        else:
            patient_id = os.path.basename(input_path)
            patient_id = patient_id.split(".")[0]
            patient_result_path = os.path.join(results_path, patient_id)
            if not os.path.exists(patient_result_path):
                os.makedirs(patient_result_path)

            csv_writer.writerow([patient_id, input_path,
                                 os.path.join(results_path, patient_id)])

    return csv_file_path


def preprocessing(path, patient_path, patient_id, device, encoder_path, args):
    Tissue = TissueSlide(path)
    total_tiles = None
    blur = None
    error = []
    summary = []

    print(f"processing: {path}")
    if Tissue.slide is not None:
        if args.desired_magnification <= Tissue.magnification:
            tile_inf_path = os.path.join(patient_path, "tile_information.csv")

            if not os.path.isfile(tile_inf_path):
                mask = TissueMask(Tissue, result_path=patient_path)
                total_tiles = tiling(Tissue, patient_path, mask, mag=args.desired_magnification,
                                     desired_size=args.desired_size, overlap=args.overlap,
                                     threshold=args.tissue_threshold)
            else:
                total_tiles = len(pd.read_csv(tile_inf_path))
            normalize_tiles_path = os.path.join(patient_path, "normalized_tile_information.csv")
            if not os.path.isfile(normalize_tiles_path):
                normalize_tiles(tile_inf_path, patient_path, device)

            in_focus_path = os.path.join(patient_path, "infocus_tile_information.csv")
            if not os.path.isfile(in_focus_path):
                blur = blurry_filter(normalize_tiles_path, patient_path, threshold=args.blur_threshold)
            else:
                blur = len(pd.read_csv(in_focus_path))

            if args.tile_graph:
                VisulizationUtils.SlideReconstruction(in_focus_path,
                                                      os.path.join(patient_path, "Reconstructed_Slide.png"))

            # Encoding
            SlideEncoding.encode_tiles(patient_id, in_focus_path, encoder_path, device)
        else:
            error.append((patient_id, path, "Desired magnification is greater than slide magnification", "Tiling"))
            print("Desired magnification is greater than slide magnification")

    else:
        error.append((patient_id, path, "OpenSlide had an error with opening the provided slide.", "Slide Opening"))
    summary.append((patient_id, path, total_tiles, blur))
    return summary, error


def preprocess_patient(row, device, encoder_path, args):
    result = row["Preprocessing Path"]
    original = row["Original Slide Path"]
    patient_id = row["Patient ID"]
    s, e = preprocessing(original, result, patient_id, device, encoder_path, args)
    print(f"done with patient {patient_id}")
    return s, e


def extract_diagnosis(ID):
    ID = ID.split("-")
    diagnosis = ID[3]
    tumor_identification = ''.join([char for char in diagnosis if char.isdigit()])
    if int(tumor_identification) >= 11:
        return 0
    else:
        return 1


def patient_files_encoded(patient_files_path):
    df = pd.read_csv(patient_files_path)
    for i, row in df.iterrows():
        label = extract_diagnosis(row["Patient ID"])
        encoded_path = os.path.join(os.path.dirname(row["Preprocessing Path"]), "encoded", row["Patient ID"] + ".h5")
        df.loc[i, "target"] = label
        df.loc[i, "Encoded Path"] = encoded_path
    df.to_csv(patient_files_path)


def parse_args():
    parser = argparse.ArgumentParser(description="WSI Preprocessing")
    parser.add_argument("-i", "--input_path", type=str,
                        default=r"C:\Users\albao\Downloads\gdc_download_20240320_111546.230274",
                        help="Input path (default: %(default)s)")
    parser.add_argument("-o", "--output_path", type=str, default=r"C:\Users\albao\Masters\WSI_t",
                        help="Result path (default: %(default)s)")
    parser.add_argument("-p", "--processes", type=int, default=2,
                        help="Number of threads for multiprocessing (default: %(default)s)")
    parser.add_argument("-s", "--desired_size", type=int, default=256,
                        help="Desired size of the tiles (default: %(default)s)")
    parser.add_argument("-tg", "--tile_graph", action="store_true",
                        help="Flag to enable graphing of tiles")
    parser.add_argument("-m", "--desired_magnification", type=int, default=40,
                        help="Desired magnification level (default: %(default)s)")
    parser.add_argument("-ov", "--overlap", type=int, default=0,
                        help="Overlap between tiles (default: %(default)s)")
    parser.add_argument("-th", "--tissue_threshold", type=float, default=0.8,
                        help="Threshold to consider a tile as Tissue(default: %(default)s)")
    parser.add_argument("-bh", "--blur_threshold", type=int, default=0.015,
                        help="Threshold for laplace filter variance (default: %(default)s)")

    return parser.parse_args()


def main():
    args = parse_args()

    # obtain args

    input_path = args.input_path
    output_path = args.output_path
    processes = args.processes
    # setting up global
    global MAG
    MAG = args.desired_magnification
    global DESIRED_SIZE
    DESIRED_SIZE = args.desired_size
    global OVERLAP
    OVERLAP = args.overlap
    global RECONSTRUCT
    RECONSTRUCT = args.tile_graph
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # raise error if invalid input path
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"The file '{input_path}' does not exist.")

    if not os.path.exists(output_path):
        os.makedirs(output_path)

    if not os.path.exists(os.path.join(output_path, "encoded")):
        os.makedirs(os.path.join(output_path, "encoded"))
    encoder_path = os.path.join(output_path, "encoded")

    # to handle if given directory of svs files
    if os.path.isdir(input_path):
        move_svs_files(input_path, output_path)

    # create a csv with information about the samples available
    patient_path = patient_csv(input_path, output_path)
    # can then read the information to do multiprocessing of samples
    patients = pd.read_csv(patient_path)

    # multiprocessing of sample preprocessing
    with multiprocessing.Pool(processes=processes, maxtasksperchild=1) as pool:
        results = pool.starmap(preprocess_patient,
                               [(row, device, encoder_path, args) for _, row in patients.iterrows()])
    # rewrite patient_files
    patient_files_encoded(patient_path)

    # write summary and error report
    global summary, errors
    for res in results:
        summary.extend(res[0])
        errors.extend(res[1])

    reports = Reports.Reports(summary, errors, output_path)


if __name__ == "__main__":
    main()
