from __future__ import print_function, division

import os
import copy
import itertools
import errno

import pandas as pd
import numpy as np
import fiona

from create_grid import PointGrid, SampleFill
from load_ntl_data import NTL_Reader


def make_dirs(path_list):
    for path in path_list:
        make_dir(path)

def make_dir(path):
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def gen_sample_size(count, weights):
    """Given a `count` of total samples and list of weights,
    determine the number of samples associated with each weight
    """
    type_sizes = np.zeros(len(weights)).astype(int)
    # note: sizes are based on nkeep which is for a single NTL class label
    # subsequent steps to label data class (train, val, etc) must be repeated
    # for each NTL class
    for i,x in enumerate(weights):
        type_sizes[i] = int(np.floor(count * x))
    # used floor for all counts, so total can be short by one
    active_type_weights_indexes = [i for i,j in enumerate(weights) if j>0]
    for _ in range(count - sum(type_sizes)):
        bonus_type_weights_index = active_type_weights_indexes[np.random.randint(0, len(active_type_weights_indexes))]
        type_sizes[bonus_type_weights_index] += 1
    return type_sizes


def apply_types(data, classes, names, weights):
    """For each subset of data based on class,
    assign data type names based on given weights.
    Weight values must sum to one and all names must
    be assigned a weight.
    """
    for c in classes:
        cat_size = len(data.loc[data['label'] == c])
        # example of alternative method (simpler, but does no ensure consistent class sizes)
        # data.loc[data['label'] == c, 'type'] = np.random.choice(type_names, size=(cat_size,), p=type_weights)
        type_sizes = gen_sample_size(cat_size, weights)
        type_list = [[x] * type_sizes[i] for i,x in enumerate(names)]
        type_list = list(itertools.chain.from_iterable(type_list))
        np.random.shuffle(type_list)
        data.loc[data['label'] == c, 'type'] = type_list
    return data


def normalize(data, type_field, type_values, class_field, class_values):
    """
    create equal class sizes based on smallest class size
        - randomizes which extras from larger classes are dropped
    """
    df_list = []
    for j in type_values:
        tmp_data = data.loc[data[type_field] == j].copy(deep=True)
        tmp_data.reset_index(inplace=True)
        raw_class_sizes = [sum(tmp_data[class_field] == i) for i in class_values]
        nkeep = min(raw_class_sizes)
        tmp_data['drop'] = 'drop'
        for i in class_values:
            class_index = tmp_data.loc[tmp_data[class_field] == i].index
            keepers = np.random.permutation(class_index)[:nkeep]
            tmp_data.loc[keepers, 'drop'] = 'keep'
        tmp_data_keep = tmp_data.loc[tmp_data['drop'] == 'keep'].copy(deep=True)
        df_list.append(tmp_data_keep)

    normalized_data = pd.concat(df_list)
    return normalized_data


def prepare_sample(base_path, name, definition):
    tmp_ntl_sample_df_list = []

    if not isinstance(definition["sample"], list):
        definition["sample"] = [definition["sample"]]

    # load each sample
    for s in definition["sample"]:
        sample_name = s
        if os.path.isfile(s):
            sample_name = os.path.splitext(os.path.basename(s))[0]
            tmp_s_df = pd.read_csv(s, quotechar='\"', na_values='',
                                    keep_default_na=False, encoding='utf-8')
        elif s == "grid":
            # boundary path defining grid area
            boundary_path = os.path.join(base_path, "data/boundary", definition["grid_boundary_file"])
            pixel_size = definition["grid_pixel_size"]
            tmp_s_df = prepare_grid_sample(boundary_path, pixel_size)
        elif s == "random":
            raise ValueError("Random samples are not implemented yet")
        else:
            source_path = base_path + "/data/sample/{}.csv".format(s)
            tmp_s_df = prepare_source_sample(source_path)

        tmp_s_df["definition"] = name
        tmp_s_df["sample"] = sample_name

        # create copy of sample for each imagery id
        for i in definition["imagery"]:
            tmp_si_df = tmp_s_df.copy(deep=True)
            tmp_si_df["temporal"] = i

            # init_sample_df_list.append(tmp_si_df)

            # fill in the tmp df with extra points if needed
            fill = SampleFill(tmp_si_df)
            nfill = 0 if not "sample_nfill" in definition.keys() else definition["sample_nfill"]
            fill_dist = 0 if not "sample_fill_dist" in definition.keys() else definition["sample_fill_dist"]
            if fill_dist < 0:
                raise ValueError("Sample fill dist must be greater than or equal to zero (Given: {})".format(fill_dist))
            fill_mode = None if not "sample_fill_mode" in definition.keys() else definition["sample_fill_mode"]
            fill.gfill(nfill, distance=fill_dist, mode=fill_mode)
            tmp_si_fill_df = fill.df.copy(deep=True)
            # fill_sample_df_list.append(tmp_si_fill_df)

            # add to list for adding supplemental data (e.g., ntl)
            tmp_ntl_sample_df_list.append(tmp_si_fill_df)

    tmp_ntl_df = pd.concat(tmp_ntl_sample_df_list)

    if "ntl_type" in definition and definition["ntl_type"] is not None:
        ntl = NTL_Reader(
            ntl_type=definition["ntl_type"],
            calibrated=definition["ntl_calibrated"],
            year=definition["ntl_year"],
            dim=definition["ntl_dim"],
            min_val=definition["ntl_min"])
        tmp_ntl_df = ntl.assign_df_values(tmp_ntl_df)

    # append to list whether ntl data was added or not
    # ntl_sample_df_list.append(tmp_ntl_df)

    return tmp_ntl_df


def prepare_source_sample(source_path):
    df = pd.read_csv(source_path, quotechar='\"',
                    na_values='', keep_default_na=False,
                    encoding='utf-8')

    cols = df.columns
    if "lon" not in cols and "longitude" in cols:
        df["lon"] = df.longitude
    if "lat" not in cols and "latitude" in cols:
        df["lat"] = df.latitude
    if "lon" not in df.columns and "lat" not in df.columsn:
        raise Exception("Source for sample data must contain longitude/latitude or lon/lat columns")
    df["sample_id"] = range(len(df))
    return df


def prepare_grid_sample(boundary_path, pixel_size):
    boundary_src = fiona.open(boundary_path)
    grid = PointGrid(boundary_src)
    boundary_src.close()
    grid.grid(pixel_size)
    # geo_path = os.path.join(base_path, "data/sample_grid.geojson")
    # grid.to_geojson(geo_path)
    return grid.to_dataframe()


class PrepareSamples():

    def __init__(self, base_path, static_params, static_hash, version, overwrite=False):

        self.base_path = base_path
        self.static_params = static_params

        # -----------------
        # sample settings

        self.overwrite = overwrite

        # init = raw sample data
        # fill = raw sample data filled with additional sample points (grouped with original points)
        # ntl  = ntl values added
        # full = full set of data (before trimming)
        # trim = final set of data (after trimming)
        sample_stages = ["init", "fill", "ntl", "full", "trim"]

        sample_tag = "{}_{}".format(static_hash, version)

        self.sample_path = {}
        for i in sample_stages:
            self.sample_path[i] = os.path.join(
                base_path, "data/grid/sample_{}_{}.csv".format(i, sample_tag)
            )

        # -----------------
        # sample fill  settings

        # # number of additional points to fill for each base point
        # self.nfill = static_params["sample_nfill"]
        # # distance from base point to fill in
        # self.fill_dist = static_params["sample_fill_dist"]
        # # fixed or random fill of point
        # self.fill_mode = static_params["sample_fill_mode"]

        # -----------------
        # ntl settings

        # # dmsp or viirs
        # self.ntl_type = static_params["ntl_type"]

        # # whether to use calibrated ntl data or original
        # self.ntl_calibrated = static_params["ntl_calibrated"]

        # # ntl year
        # self.ntl_year = static_params["ntl_year"]

        # # size of square (pixels) to calculate ntl
        # self.ntl_dim = static_params["ntl_dim"]

        # # minimum NTL to keep (for dropping zero/low values that may be noise)
        # self.ntl_min = static_params["ntl_min"]

        # -----------------

        # field name used to define cat values
        self.cat_field = static_params["cat_field"]

        # starting value for each bin, ends at (not including) following value
        self.cat_bins = static_params["cat_bins"]

        # number of items in cat_names must match number of items in cat_bins
        self.cat_names = static_params["cat_names"]

        # data types to subset
        self.type_names = static_params["type_names"]

        # ratio for data types (must sum to 1.0)
        self.type_weights = static_params["type_weights"]


    def prepare_multi_sample(self):
        """
        for each temporal id in imagery list
        create copy of sample df with "temporal" column
        then append them all so final df has copy of each sample data for each temporal step
        that temporal col will then be used in dataloader instead of a "year" arg to the function
        """
        # init_sample_df_list = []
        # fill_sample_df_list = []
        ntl_sample_df_list = []
        for name in self.static_params["sample_definition"].keys():
            definition = self.static_params["sample_definition"][name]
            tmp_ntl_df = prepare_sample(self.base_path, name, definition)
            ntl_sample_df_list.append(tmp_ntl_df)

        # init_df = pd.concat(init_sample_df_list)
        # init_df.to_csv(self.sample_path["init"])

        # fill_df = pd.concat(fill_sample_df_list)
        # fill_df.to_csv(self.sample_path["fill"])

        ntl_df = pd.concat(ntl_sample_df_list)
        ntl_df.to_csv(self.sample_path["ntl"], encoding="utf-8")

        self.df = ntl_df


    def assign_labels(self):
        # label each point based on cat ntl value for point and class bins
        self.df["label"] = None
        for c, b in enumerate(self.cat_bins):
            self.df.loc[self.df[self.cat_field] >= b, 'label'] = int(c)
        # ----------------------------------------
        # determine size of each data type (train, val, etc)
        self.df['type'] = None
        # subset to original grid (no spatial overlap)
        tmp_df = self.df.loc[self.df["group"] == "original"].copy(deep=True)
        # define data group type for original grid
        tmp_df = apply_types(tmp_df, self.cat_names, self.type_names, self.type_weights)
        # based on classes for original grid subset, apply classes to
        # all associated subgrid points
        for _, row in tmp_df.iterrows():
            sample_id = row["sample_id"]
            self.df.loc[self.df["sample_id"] == sample_id, 'type'] = row['type']
        # save full set of data
        self.df.to_csv(self.sample_path["full"], index=False, encoding='utf-8')


    def normalize_classes(self):
        self.ndf = normalize(self.df, 'type', self.type_names, 'label', self.cat_names)
        self.ndf.to_csv(self.sample_path["trim"], index=False, encoding='utf-8')


    def run(self):

        print("\nPreparing sample...")
        # define, load or build, and save sample dataframe
        if not os.path.isfile(self.sample_path["ntl"]) or self.overwrite:
            self.prepare_multi_sample()
        else:
            self.df = pd.read_csv(self.sample_path["ntl"], sep=",", encoding='utf-8')

        print("\nAssign class labels...")
        # label samples based on class definitions from settings
        if not os.path.isfile(self.sample_path["full"]) or self.overwrite:
            self.assign_labels()
        else:
            self.df = pd.read_csv(self.sample_path["full"], sep=",", encoding='utf-8')

        print("\nNormalizing class sizes...")
        # make sure all class sizes are same by trimming all to smallest class size
        if not os.path.isfile(self.sample_path["trim"]) or self.overwrite:
            self.normalize_classes()
        else:
            self.ndf = pd.read_csv(self.sample_path["trim"], sep=",", encoding='utf-8')


        print("\nPreparing dataframe dict...")
        # create dict suitable for using with PyTorch/dataloaders code

        dataframe_dict = {}
        for i in self.type_names:
            dataframe_dict[i] = self.ndf.loc[self.ndf['type'] == i]

        class_sizes = {
            j:[sum(dataframe_dict[j]['label'] == i) for i in self.cat_names] for j in self.type_names
        }

        return dataframe_dict, class_sizes


    def print_counts(self):
        """print resulting split of classes for each data type in final full/normalized samples
        """
        print("\nFull data:")
        for i in self.type_names:
            tmp_df = self.df.loc[self.df['type'] == i]
            print("\tSamples per cat ({}):".format(i))
            for j in self.cat_names: print("\t{0}: {1}".format(j, sum(tmp_df['label'] == j)))


        print("\nNormalized data:")
        for i in self.type_names:
            tmp_df = self.ndf.loc[self.ndf['type'] == i]
            print("\tSamples per cat ({}):".format(i))
            for j in self.cat_names: print("\t{0}: {1}".format(j, sum(tmp_df['label'] == j)))
