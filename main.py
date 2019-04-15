"""

qsub -I -l nodes=1:hima:gpu:ppn=64 -l walltime=8:00:00

hi06 = p100 x2
hi07 = v100 x2



"""


# import os
# import errno
# from affine import Affine

# def make_dir(path):
#     try:
#         os.makedirs(path)
#     except OSError as exception:
#         if exception.errno != errno.EEXIST:
#             raise


from __future__ import print_function, division

import os
import copy
# import itertools
import datetime
import time
# import pprint

# import json
# import hashlib

import pandas as pd
import numpy as np
import fiona

import torch
import torch.nn as nn

import resnet

from load_data import build_dataloaders
from runscript import RunCNN
from load_survey_data import surveys

from data_prep import *


# -----------------------------------------------------------------------------


quiet = False


cuda_device_id = 0

device = torch.device("cuda:{}".format(cuda_device_id) if torch.cuda.is_available() else "cpu")

print("Running on:", device)


run = {
    "train": True,
    "test": False,
    "predict": False,
    "predict_new": True
}

timestamp = datetime.datetime.fromtimestamp(int(time.time())).strftime(
    '%Y_%m_%d_%H_%M_%S')

date_str = datetime.datetime.now().strftime("%Y%m%d")


# -----------------------------------------------------------------------------


for ix, (param_hash, params) in enumerate(tasks):
    # -----------------
    params['pixel_size'] = pixel_size
    params['ncats'] = len(cat_names)
    params["train_class_sizes"] = train_class_sizes
    params["val_class_sizes"] = val_class_sizes
    # -----------------
    print('-' * 10)
    print("\nParameter combination: {}/{}".format(ix+1, s.param_count))
    print("\nParam hash: {}\n".format(param_hash))
    state_path = os.path.join(base_path, "output/s1_state/state_{}.pt".format(param_hash))
    # -----------------
    if run["train"] or run["test"] or run["predict"]:
        dataloaders = build_dataloaders(
            dataframe_dict,
            base_path,
            params["imagery_year"],
            data_transform=None,
            dim=params["dim"],
            batch_size=params["batch_size"],
            num_workers=params["num_workers"],
            agg_method=params["agg_method"])
        train_cnn = RunCNN(
            dataloaders, device, cat_names,
            parallel=False, quiet=quiet, **params)
        if run["train"]:
            acc_p, class_p, time_p = train_cnn.train()
            params['hash'] = param_hash
            params['id_string'] = "{}_{}.csv".format(param_hash, timestamp)
            params['acc'] = acc_p
            params['class_acc'] = class_p
            params['time'] = time_p
            s.write_to_json(param_hash, params)
            train_cnn.save(state_path)
        else:
            train_cnn.load(state_path)
        if run["test"]:
            epoch_loss, epoch_acc, class_acc, time_elapsed = train_cnn.test()
        if run["predict"]:
            pred_data, _ = train_cnn.predict(features=True)
    # -----------------
    if run["predict_new"]:
        """
        - load data
        - load trained cnn state
        - run predict
        - append cnn features to original data
        - output to csv for second stage models
        """
        new_data = {
            "predict": surveys[params["survey"]].copy(deep=True)
        }
        new_dataloaders = build_dataloaders(
            new_data,
            base_path,
            params["imagery_year"],
            data_transform=None,
            dim=params["dim"],
            batch_size=params["batch_size"],
            num_workers=params["num_workers"],
            agg_method=params["agg_method"],
            shuffle=False)
        new_cnn = RunCNN(
            new_dataloaders, device, cat_names,
            parallel=False, quiet=quiet, **params)
        new_cnn.load(state_path)
        # ---------
        new_pred_data, _ = new_cnn.predict(features=True)
        feat_labels = ["feat_{}".format(i) for i in xrange(1,513)]
        pred_dicts = [dict(zip(feat_labels, i)) for i in new_pred_data]
        pred_df = pd.DataFrame(pred_dicts)
        new_out = new_data["predict"].merge(pred_df, left_index=True, right_index=True)
        col_order = list(new_data["predict"].columns) + feat_labels
        new_out = new_out[col_order]
        new_out_path = os.path.join(base_path, "output/s1_predict/predict_{}_{}.csv".format(param_hash, timestamp))
        new_out.to_csv(new_out_path, index=False, encoding='utf-8')
