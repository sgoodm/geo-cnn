
import os
import rasterio
import numpy as np
from torch.utils.data import Dataset


class BandDataset(Dataset):
    """Get the data
    """
    def __init__(self, dataframe, root_dir, year, dim=224, transform=None, agg_method="mean"):

        self.dim = dim
        self.year = year
        # self.bands = ["b1", "b2", "b3", ]
        self.bands = ["b1", "b2", "b3", "b4", "b5", "b7", "b61", "b62"]
        # self.bands = ["b3", "b4", "b5"]

        self.dataframe = dataframe
        self.root_dir = root_dir
        self.transform = transform

        self.agg_method = agg_method


    def __len__(self):
        return len(self.dataframe)


    def __getitem__(self, idx):
        dim = self.dim

        row = self.dataframe.iloc[idx]

        # label = None
        label = -1
        if 'label' in row:
            label = row['label']

        lon = row['lon']
        lat = row['lat']

        feature = np.empty((len(self.bands), dim, dim))

        for bnum, band in enumerate(self.bands):

            season_mosaics_path = os.path.join(
                self.root_dir, "landsat/data/mosaics/{0}_all".format(self.year), self.agg_method,
                "{0}_all_{1}.tif".format(self.year, band))

            season_mosaics = rasterio.open(season_mosaics_path)

            r, c = season_mosaics.index(lon, lat)
            win = ((r-dim/2, r+dim/2), (c-dim/2, c+dim/2))
            data = season_mosaics.read(1, window=win)

            if data.shape != (dim, dim):
                raise Exception("bad feature (dim: ({0}, {0}), data shape: {1}".format(dim, data.shape))

            feature[bnum] = data

        if self.transform:
            feature = np.transpose(feature,(1,2,0))
            feature = self.transform(feature)

        # return torch.from_numpy(feature).float(), label
        return feature.float(), label