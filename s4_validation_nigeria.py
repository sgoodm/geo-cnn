"""
specify survey layer (with year) to validate

load s4_surface for same year (use json or fixed args to determine which surface?)

compare point (or buffer zs?) value of surface to survey value for each survey point

output dataframe of survey value, point value, buffer value along with percent differences? (include lat/lon to map errors)

"""


import os
import itertools
import errno
import rasterio
import pandas as pd
import numpy as np
import sklearn.metrics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from utils.settings_builder import Settings


# *****************
# *****************
json_path = "settings/nigeria_acled.json"
# json_path = "settings/settings_example.json"
# json_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), json_path)
# *****************
# *****************


s = Settings()
s.load(json_path)


predict_settings = s.data[s.config["predict"]]
predict_hash = s.build_hash(predict_settings, nchar=7)


s3_info = s.data["third_stage"]

model_tag = s.config["model_tag"]
surface_tag = s.config["surface_tag"]

tasks = s.hashed_iter()


print("-----")

input_stage = s3_info["surface"]["input_stage"]

if input_stage == "s2":
    model_list = s3_info["predict"]["class_models"] + s3_info["predict"]["proba_models"]
    input_list = s3_info["predict"]["inputs"]
    surface_list = itertools.product(model_list, input_list)
elif input_stage == "s1":
    surface_list = [
        (0, s3_info["surface"]["value_type"])
    ]
else:
    raise ValueError("Surface input stage must be either `s1` or `s2`. (`{}` given)".format(input_stage))


survey_path = "/sciclone/aiddata10/REU/projects/lab_oi_nigeria/data/acled/final/acled_{}.csv".format(predict_settings["imagery_year"])
survey_df = pd.read_csv(survey_path)
survey_df["binary"] = (survey_df["fatalities"] > 0).astype(int)
if "lon" not in  survey_df.columns:
    survey_df["lon"] = survey_df["longitude"]


if "lat" not in  survey_df.columns:
    survey_df["lat"] = survey_df["latitude"]

print("Survey points: {}".format(len(survey_df)))



def make_dir(path):
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def surface_win_mean(src, pnt, dim):
    r, c = src.index(pnt.lon, pnt.lat)
    win = ((r-dim/2, r+dim/2), (c-dim/2, c+dim/2))
    data = src.read(1, window=win, boundless=True, masked=True)
    win_val = np.mean(data)
    return win_val


for ix, (param_hash, params) in enumerate(tasks):
    for surface_item in surface_list:
        print("Running: {} - {}".format(param_hash, surface_item))
        if input_stage == "s2":
            model_name, input_name = surface_item
            input_string = "_".join(str(i) for i in [
                model_name,
                param_hash,
                predict_hash,
                s3_info["grid"]["boundary_id"],
                s3_info["predict"]["imagery_year"],
                s.config["version"],
                s.config["predict_tag"],
                s.config["model_tag"]
            ])
        elif input_stage == "s1":
            _, input_name = surface_item
            input_string = "_".join(str(i) for i in [
                param_hash,
                predict_hash,
                s.config["version"],
                s.config["predict_tag"]
            ])
        surface_string = input_string + "_" + surface_tag
        s3_surface_path = os.path.join(s.base_path, "output/s3_surface/surface_{}_{}.tif".format(input_name, surface_string))
        print(s3_surface_path)

        # -------------------------------------

        cnn_surface_path = s3_surface_path

        validation_dir = os.path.dirname(os.path.dirname(cnn_surface_path)) + "/s4_validation"
        validation_fname = os.path.basename(cnn_surface_path)[:-4] + "_" + os.path.basename(survey_path)[:-4]
        validation_base = os.path.join(validation_dir, validation_fname)

        make_dir(validation_dir)

        cnn_surface_src = rasterio.open(cnn_surface_path, 'r')

        validation_data = []
        for i, row in survey_df.iterrows():
            surface_data = {
                "point": cnn_surface_src.sample([(row.lon, row.lat)]).next()[0],
                "dim3": surface_win_mean(cnn_surface_src, row, 3),
                "dim16": surface_win_mean(cnn_surface_src, row, 16),
                "dim33": surface_win_mean(cnn_surface_src, row, 33),
            }
            for i in surface_data.keys():
                try:
                    class_val = int(round(surface_data[i]))
                    class_val = -1 if class_val == 255 else class_val
                    surface_data[i+"_class"] = class_val
                except:
                    surface_data[i+"_class"] = -1
            validation_data.append(surface_data)

        validation_df = pd.DataFrame(validation_data)

        tmp_survey_df = survey_df.copy(deep=True)

        for i in validation_df.columns:
            tmp_survey_df[i] = validation_df[i]

        tmp_survey_df = tmp_survey_df.loc[tmp_survey_df.point_class != -1]

        print("Original points: {}".format(len(survey_df)))
        print("Clean points: {}".format(len(tmp_survey_df)))

        metrics = ["tp", "fn", "tn", "fp", "accuracy", "precision", "recall", "f1"]

        tmp_summary_list = []
        # count = float(len(tmp_survey_df))
        for i in tmp_survey_df.columns:
            if i.endswith("class"):
                print(i)
                y_true = tmp_survey_df["binary"]
                y_pred = tmp_survey_df[i]
                y_prob = tmp_survey_df[i[:-6]]
                tmp_survey_df[i+"_match"] = (y_true == y_pred).astype(int)
                tmp_survey_df[i+"_confusion"] =  None
                tmp_survey_df.loc[((y_true == 1) & (y_pred == 1)), i+"_confusion"] = "tp"
                tmp_survey_df.loc[((y_true == 0) & (y_pred == 0)), i+"_confusion"] = "tn"
                tmp_survey_df.loc[((y_true == 0) & (y_pred == 1)), i+"_confusion"] = "fp"
                tmp_survey_df.loc[((y_true == 1) & (y_pred == 0)), i+"_confusion"] = "fn"
                tmp_summary = {}
                tmp_summary["method"] = i
                tp = sum((y_true == 1) & (y_pred == 1))
                fn = sum((y_true == 1) & (y_pred == 0))
                tn = sum((y_true == 0) & (y_pred == 0))
                fp = sum((y_true == 0) & (y_pred == 1))
                tmp_summary["tp"] = tp / float(tp+fn)
                tmp_summary["fn"] = fn / float(fn+tp)
                tmp_summary["tn"] = tn / float(tn+fp)
                tmp_summary["fp"] = fp / float(fp+tn)
                # tmp_summary["tp"] = sum((y_true == 1) & (y_pred == 1)) / count
                # tmp_summary["fn"] = sum((y_true == 1) & (y_pred == 0)) / count
                # tmp_summary["tn"] = sum((y_true == 0) & (y_pred == 0)) / count
                # tmp_summary["fp"] = sum((y_true == 0) & (y_pred == 1)) / count
                tmp_summary["accuracy"] = sklearn.metrics.accuracy_score(y_true, y_pred)
                tmp_summary["precision"] = sklearn.metrics.precision_score(y_true, y_pred)
                tmp_summary["recall"] = sklearn.metrics.recall_score(y_true, y_pred)
                tmp_summary["f1"] = sklearn.metrics.f1_score(y_true, y_pred)
                tmp_summary_list.append(tmp_summary)
                # ====================
                # ====================
                # ====================
                # generate curves
                auc = sklearn.metrics.roc_auc_score(y_true, y_prob)
                fpr, tpr, _ = sklearn.metrics.roc_curve(y_true, y_prob)
                # 1:1 line (noskill) data
                ns_probs = [0 for _ in range(len(y_true))]
                ns_auc = sklearn.metrics.roc_auc_score(y_true, ns_probs)
                ns_fpr, ns_tpr, _ = sklearn.metrics.roc_curve(y_true, ns_probs)
                plt.figure()
                plt.plot(ns_fpr, ns_tpr, linestyle='--', label='No Skill')
                plt.plot(fpr, tpr, marker='.', label='Actual')
                plt.xlabel('False Positive Rate')
                plt.ylabel('True Positive Rate')
                plt.legend()
                plt.title("ROC Curve")
                plot_path = validation_base + "_roc_" + i + ".png"
                plt.savefig(plot_path)
                print('No Skill: ROC AUC=%.3f' % (ns_auc))
                print('Actual: ROC AUC=%.3f' % (auc))
                precision, recall, thresholds = sklearn.metrics.precision_recall_curve(y_true, y_prob)
                prc_precision, prc_recall, _ = sklearn.metrics.precision_recall_curve(y_true, y_prob)
                no_skill = len(y_true[y_true==1]) / len(y_true)
                plt.figure()
                plt.plot([0, 1], [no_skill, no_skill], linestyle='--', label='No Skill')
                plt.plot(prc_recall, prc_precision, marker='.', label='Actual')
                plt.xlabel('Recall')
                plt.ylabel('Precision')
                plt.legend()
                plt.title("PRC Curve")
                plot_path = validation_base + "_prc_" + i + ".png"
                plt.savefig(plot_path)
                # ====================
                # ====================
                # ====================


        tmp_summary_df = pd.DataFrame(tmp_summary_list)

        tmp_summary_df = tmp_summary_df[["method"] + metrics]

        final_cols = ["lon", "lat", "fatalities", "binary"]
        for i in validation_df.columns:
            final_cols.append(i)
            if i.endswith("class"):
                for j in ["match", "confusion"]:
                    final_cols.append(i+"_"+j)

        final_survey_df = tmp_survey_df[final_cols]


        final_survey_df.to_csv(validation_base + "_survey.csv", index=False)
        tmp_summary_df.to_csv(validation_base + "_summary.csv", index=False)




# -----------------------------------------------------------------------------
# adm2 validation
# - prior to this code, spatial join each year acled points (mean+others) to extract adm2 geojson


import pandas as pd
import sklearn.metrics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

path_2015 = "/home/userz/Desktop/nga_viz_01/nigeria_adm2_extract_2015_acled.csv"
path_2017 = "/home/userz/Desktop/nga_viz_01/nigeria_adm2_extract_2017_acled.csv"
path_2019 = "/home/userz/Desktop/nga_viz_01/nigeria_adm2_extract_2019_acled.csv"

path_adm2_adm1_join = "/home/userz/Desktop/nga_viz_01/adm2_adm1_join.csv"
df_adm2_adm1_join = pd.read_csv(path_adm2_adm1_join)[["id", "adm1_shapeID", "adm1_shapeName"]]

df_dict = {
    2015: pd.read_csv(path_2015).merge(df_adm2_adm1_join, on="id"),
    2017: pd.read_csv(path_2017).merge(df_adm2_adm1_join, on="id"),
    2019: pd.read_csv(path_2019).merge(df_adm2_adm1_join, on="id")
}

base_df = df_dict[2015][['shapeID', 'shapeName', "adm1_shapeID", "adm1_shapeName", 'mean_2014', 'mean_2016', 'mean_2018']].copy(deep=True)

base_df.columns = ['shapeID', 'shapeName', "adm1_shapeID", "adm1_shapeName", 'predicted_raw_2015', 'predicted_raw_2017', 'predicted_raw_2019']

thresh = 0.4
base_df["predicted_binary_2015"] = (base_df["predicted_raw_2015"] > thresh).astype(int)
base_df["predicted_binary_2017"] = (base_df["predicted_raw_2017"] > thresh).astype(int)
base_df["predicted_binary_2019"] = (base_df["predicted_raw_2019"] > thresh).astype(int)

base_df["true_sum_2015"] = df_dict[2015]["fatalities_sum"]
base_df["true_sum_2017"] = df_dict[2017]["fatalities_sum"]
base_df["true_sum_2019"] = df_dict[2019]["fatalities_sum"]

class ConfusionMatrix():
    def __init__(self, true, pred):
        self.true = true
        self.pred = pred
        self.tp = sum((true == 1) & (pred == 1))
        self.fn = sum((true == 1) & (pred == 0))
        self.tn = sum((true == 0) & (pred == 0))
        self.fp = sum((true == 0) & (pred == 1))
        self.cm = (self.tp, self.fn, self.tn, self.fp)
        self.gen_rates()
        self.gen_performance_measures()
    def run(self):
        tpr, fnr, tnr, fpr = self.gen_rates()
        accuracy, precision, recall, f1 = self.gen_performance_measures()
        out = {
            "tp": tpr, "fn": fnr, "tn": tnr, "fp": fpr,
            "accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1
        }
        return out
    def gen_rates(self):
        tpr = self.calc_tp_rate()
        fnr = self.calc_fn_rate()
        tnr = self.calc_tn_rate()
        fpr = self.calc_fp_rate()
        return (tpr, fnr, tnr, fpr)
    def gen_performance_measures(self):
        accuracy = sklearn.metrics.accuracy_score(self.true, self.pred)
        precision = sklearn.metrics.precision_score(self.true, self.pred)
        recall = sklearn.metrics.recall_score(self.true, self.pred)
        f1 = sklearn.metrics.f1_score(self.true, self.pred)
        return (accuracy, precision, recall, f1)
    def calc_tp_rate(self):
        try:
            return self.tp / float(self.tp+self.fn)
        except:
            return None
    def calc_fn_rate(self):
        try:
            return self.fn / float(self.fn+self.tp)
        except:
            return None
    def calc_tn_rate(self):
        try:
            return self.tn / float(self.tn+self.fp)
        except:
            return None
    def calc_fp_rate(self):
        try:
            return self.fp / float(self.fp+self.tn)
        except:
            return None

gen_curves = False
group_col_list = ["adm0_shapeID", "adm1_shapeName"]
adm_summary_list = []
for y in [2015, 2017, 2019]:
    tmp_df = base_df.copy(deep=True)
    tmp_df["adm0_shapeID"] = 0
    for group_col in group_col_list:
        groups = set(tmp_df[group_col])
        for group in groups:
            print("\nGroup {0} : {1}".format(group_col, group))
            group_df = tmp_df.loc[tmp_df[group_col] == group].copy(deep=True)
            adm_summary = {}
            adm_summary["group_col"] = group_col
            adm_summary["group"] = group
            adm_summary["year"] = y
            adm_summary["original_size"] = len(group_df)
            print("Original size: {}".format(adm_summary["original_size"]))
            group_df.dropna(subset=["true_sum_{}".format(y)], inplace=True)
            adm_summary["dropna_size"] = len(group_df)
            print("Dropna size: {}".format(adm_summary["dropna_size"]))
            group_df["true_binary_{}".format(y)] = (group_df["true_sum_{}".format(y)] > 0).astype(int)
            y_true = group_df["true_binary_{}".format(y)]
            y_pred = group_df["predicted_binary_{}".format(y)]
            y_prob = group_df["predicted_raw_{}".format(y)]
            # count = float(len(group_df))
            stats = ConfusionMatrix(y_true, y_pred)
            adm_summary.update(stats.run())
            adm_summary_list.append(adm_summary)
            if gen_curves:
                auc = sklearn.metrics.roc_auc_score(y_true, y_prob)
                fpr, tpr, _ = sklearn.metrics.roc_curve(y_true, y_prob)
                # 1:1 line (noskill) data
                ns_probs = [0 for _ in range(len(y_true))]
                ns_auc = sklearn.metrics.roc_auc_score(y_true, ns_probs)
                ns_fpr, ns_tpr, _ = sklearn.metrics.roc_curve(y_true, ns_probs)
                plt.figure()
                plt.plot(ns_fpr, ns_tpr, linestyle='--', label='No Skill')
                plt.plot(fpr, tpr, marker='.', label='Actual {}'.format(y))
                plt.xlabel('False Positive Rate')
                plt.ylabel('True Positive Rate')
                plt.legend()
                plt.title("{} ROC Curve".format(y))
                plot_path = "/home/userz/Desktop/nigeria_roc_{}.png".format(y)
                plt.savefig(plot_path)
                print('No Skill: ROC AUC=%.3f' % (ns_auc))
                print('Actual: ROC AUC=%.3f' % (auc))
                precision, recall, thresholds = sklearn.metrics.precision_recall_curve(y_true, y_prob)
                prc_precision, prc_recall, _ = sklearn.metrics.precision_recall_curve(y_true, y_prob)
                no_skill = len(y_true[y_true==1]) / float(len(y_true))
                plt.figure()
                plt.plot([0, 1], [no_skill, no_skill], linestyle='--', label='No Skill')
                plt.plot(prc_recall, prc_precision, marker='.', label='Actual')
                plt.xlabel('Recall')
                plt.ylabel('Precision')
                plt.legend()
                plt.title("{} PRC Curve".format(y))
                plot_path = "/home/userz/Desktop/nigeria_prc_{}.png".format(y)
                plt.savefig(plot_path)
                # f1 = sklearn.metrics.f1_score(y_true, yhat)
                # auc = auc(recall, precision)
                # prc_f1, prc_auc = sklearn.metrics.f1_score(y_true, yhat), auc(prc_recall, prc_precision)
                # print('Actual: f1=%.3f auc=%.3f' % (prc_f1, prc_auc))

metrics = ["tp", "fn", "tn", "fp", "accuracy", "precision", "recall", "f1"]
adm_summary_df = pd.DataFrame(adm_summary_list)
adm_summary_df = adm_summary_df[["year", "group_col", "group", "original_size", "dropna_size"] + metrics]
adm_summary_df.to_csv("/home/userz/Desktop/nga_adm2_summary.csv", index=False)
adm_summary_df
