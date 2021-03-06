from __future__ import print_function, division

import copy
import time

import torch
import torch.nn as nn

import resnet
import vgg

from torch.utils.data import DataLoader
from torchvision import transforms

from load_landsat_data import BandDataset


def build_dataloaders(df_dict, base_path, imagery_type, imagery_bands, agg_method="mean", data_transform=None, dim=224, batch_size=64, num_workers=16, shuffle=True):

    if data_transform == None:

        data_transform = transforms.Compose([
            # transforms.RandomSizedCrop(224),
            transforms.RandomHorizontalFlip(p=0.25),
            transforms.RandomVerticalFlip(p=0.25),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], # imagenet means
                                 std=[0.229, 0.224, 0.225]), # imagenet stds
        ])

    dataloaders = {}

    # where group is train, val, test, predict
    for group in df_dict:
        tmp_dset = BandDataset(df_dict[group], base_path, imagery_type, imagery_bands, agg_method=agg_method, dim=dim, transform=data_transform)
        dataloaders[group] = DataLoader(tmp_dset, batch_size=batch_size, shuffle=shuffle, num_workers=num_workers)

    return dataloaders


class RunCNN():

    def __init__(self, dataloaders, device, parallel=False, quiet=False, **kwargs):

        self.dataloaders = dataloaders
        self.device = device

        self.parallel = parallel
        self.quiet = quiet

        self.kwargs = kwargs

        # self.dataloaders = {}
        self.model = None
        self.criterion = None
        self.optimizer = None
        self.scheduler = None
        self.state_dict = None
        self.pmodel = None


    def set_train_data(self, data):
        self.dataloaders["train"] = data


    def set_val_data(self, data):
        self.dataloaders["val"] = data


    def set_test_data(self, data):
        self.dataloaders["test"] = data


    def set_predict_data(self, data):
        self.dataloaders["predict"] = data


    def init_training(self):
        self.cat_names = self.kwargs["static"]["cat_names"]
        self.ncats = len(self.cat_names)


    def init_print(self):
        print('')
        print('-' * 20)
        print("RunCNN initializing with parameters: \n\n{}".format(self.kwargs))
        print('-' * 20)
        print('')


    def init_net(self):

        net_args = {
            "pretrained":True,
            "n_input_channels": len(self.kwargs["static"]["imagery_bands"])
        }

        # https://pytorch.org/docs/stable/torchvision/models.html
        if self.kwargs["net"] == "resnet18":
            self.model = resnet.resnet18(**net_args)
        elif self.kwargs["net"] == "resnet34":
            self.model = resnet.resnet34(**net_args)
        elif self.kwargs["net"] == "resnet50":
            self.model = resnet.resnet50(**net_args)
        elif self.kwargs["net"] == "resnet101":
            self.model = resnet.resnet101(**net_args)
        elif self.kwargs["net"] == "resnet152":
            self.model = resnet.resnet152(**net_args)
        elif self.kwargs["net"] == "vgg11":
            self.model = vgg.vgg11(**net_args)
        elif self.kwargs["net"] == "vgg11_bn":
            self.model = vgg.vgg11_bn(**net_args)
        elif self.kwargs["net"] == "vgg13":
            self.model = vgg.vgg13(**net_args)
        elif self.kwargs["net"] == "vgg13_bn":
            self.model = vgg.vgg13_bn(**net_args)
        elif self.kwargs["net"] == "vgg16":
            self.model = vgg.vgg16(**net_args)
        elif self.kwargs["net"] == "vgg16_bn":
            self.model = vgg.vgg16_bn(**net_args)
        elif self.kwargs["net"] == "vgg19":
            self.model = vgg.vgg19(**net_args)
        elif self.kwargs["net"] == "vgg19_bn":
            self.model = vgg.vgg19_bn(**net_args)

        else:
            raise ValueError("Invalid network specified: {}".format(self.kwargs["net"]))

        #  run type: 1 = fine tune, 2 = fixed feature extractor
        #  - replace run type option with "# of layers to fine tune"
        if self.kwargs["run_type"] == 2:
            layer_count = len(list(self.model.parameters()))
            for layer, param in enumerate(self.model.parameters()):
                if layer <= layer_count - 5:
                    param.requires_grad = False

        # Parameters of newly constructed modules have requires_grad=True by default
        # get existing number for input features
        # set new number for output features to number of categories being classified
        # see: https://pytorch.org/tutorials/beginner/finetuning_torchvision_models_tutorial.html
        if "resnet" in self.kwargs["net"]:
            num_ftrs = self.model.fc.in_features
            self.model.fc = nn.Linear(num_ftrs, self.ncats)
        elif "vgg" in self.kwargs["net"]:
            num_ftrs = self.model.classifier[6].in_features
            self.model.classifier[6] = nn.Linear(num_ftrs, self.ncats)



    def init_loss(self):
        loss_weights = torch.tensor(
            map(float, self.kwargs["loss_weights"])).cuda()

        self.criterion = nn.CrossEntropyLoss(weight=loss_weights)


    def save(self, path):
        torch.save(self.model.state_dict(), path)


    def load(self, path):
        self.state_dict = torch.load(path)
        self.model.load_state_dict(self.state_dict)
        self.model.eval()


    def export_to_device(self):

        if self.parallel and torch.cuda.device_count() > 1:
            print("Using", torch.cuda.device_count(), "GPUs")
            self.model = nn.DataParallel(self.model)
            if self.pmodel:
                self.pmodel = nn.DataParallel(self.pmodel)


        self.model = self.model.to(self.device)

        if self.pmodel:
            self.pmodel = self.pmodel.to(self.device)


    def train(self, quiet=None):

        print("Train")

        if quiet == None:
            quiet = self.quiet

        if self.kwargs["optim"] == "sgd":
            self.optimizer = torch.optim.SGD(
                # self.model.fc.parameters(), # only final layer
                self.model.parameters(), # all layers
                lr=self.kwargs["lr"],
                momentum=self.kwargs["momentum"]
            )
        elif self.kwargs["optim"] == "adam":
            self.optimizer = torch.optim.Adam(
                self.model.parameters(),
                lr=self.kwargs["lr"]
            )

        # Decay LR by a factor of `gamma` every `step_size` epochs
        # exp lr scheduler
        self.scheduler = torch.optim.lr_scheduler.StepLR(
            self.optimizer,
            step_size=self.kwargs["step_size"],
            gamma=self.kwargs["gamma"])

        self.export_to_device()

        best_acc, best_class_acc, time_elapsed = self._train(
            self.kwargs["n_epochs"], quiet)

        return best_acc, best_class_acc, time_elapsed


    def _train(self, num_epochs, quiet):
        since = time.time()

        best_model_wts = copy.deepcopy(self.model.state_dict())
        best_acc = 0.0

        for epoch in range(num_epochs):
            if not quiet:
                print('\nEpoch {}/{}'.format(epoch, num_epochs - 1))
                print('-' * 10)

            # Each epoch has a training and validation phase
            for phase in ['train', 'val']:
                if phase == 'train':
                    self.scheduler.step()
                    self.model.train()  # Set model to training mode
                else:
                    self.model.eval()   # Set model to evaluate mode

                running_loss = 0.0
                running_correct = 0
                running_count = 0

                class_correct = [0] * self.ncats
                class_count = [0] * self.ncats

                # iterate over data
                for inputs, labels in self.dataloaders[phase]:
                    inputs = inputs.to(self.device)

                    labels = labels.to(self.device)

                    # zero the parameter gradients
                    self.optimizer.zero_grad()

                    # forward
                    # track history if only in train
                    with torch.set_grad_enabled(phase == 'train'):
                        outputs = self.model(inputs)
                        _, preds = torch.max(outputs, 1)
                        loss = self.criterion(outputs, labels)


                        # backward + optimize only if in training phase
                        if phase == 'train':
                            loss.backward()
                            self.optimizer.step()

                    # statistics
                    running_loss += loss.item() * inputs.size(0)

                    running_correct += torch.sum(preds == labels.data)
                    running_count += inputs.size(0)

                    for i in range(self.ncats):
                        label_indexes = (labels == i).nonzero().squeeze()
                        class_correct[i] += torch.sum(
                            preds[label_indexes] == labels[label_indexes]).item()
                        try:
                            class_count[i] += len(label_indexes)
                        except:
                            pass

                epoch_loss = running_loss / running_count
                epoch_acc = running_correct.item() / running_count

                class_acc = [class_correct[i] / class_count[i] for i in range(self.ncats)]

                if not quiet:
                    print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                        phase, epoch_loss, epoch_acc))

                # deep copy the model
                if phase == 'val' and epoch_acc > best_acc:
                    best_acc = epoch_acc
                    best_class_acc = class_acc
                    best_model_wts = copy.deepcopy(self.model.state_dict())

                if phase == 'val':
                    for i in range(self.ncats):
                        print('Accuracy of class {} : {} / {} = {:.4f} %'.format(
                            i, class_correct[i], class_count[i], class_acc[i]))


        time_elapsed = time.time() - since
        print('\nTraining complete in {:.0f}m {:.0f}s'.format(
            time_elapsed // 60, time_elapsed % 60))
        print('Best val Acc: {:4f}'.format(best_acc))

        # load best model weights
        self.model.load_state_dict(best_model_wts)
        self.state_dict = best_model_wts

        return best_acc, best_class_acc, time_elapsed


    def test(self):

        print("Test")

        epoch_loss, epoch_acc, class_acc, time_elapsed = self._test()

        return epoch_loss, epoch_acc, class_acc, time_elapsed


    def _test(self):

        phase = "test"

        since = time.time()

        self.export_to_device()

        self.model.eval()

        running_loss = 0.0
        running_correct = 0
        running_count = 0

        class_correct = [0] * self.ncats
        class_count = [0] * self.ncats

        # Iterate over data.
        for inputs, labels in self.dataloaders[phase]:
            inputs = inputs.to(self.device)

            labels = labels.to(self.device)

            with torch.set_grad_enabled(False):
                outputs = self.model(inputs)
                _, preds = torch.max(outputs, 1)
                loss = self.criterion(outputs, labels)

            # statistics
            running_loss += loss.item() * inputs.size(0)

            running_correct += torch.sum(preds == labels.data)
            running_count += inputs.size(0)

            for i in range(self.ncats):
                label_indexes = (labels == i).nonzero().squeeze()
                class_correct[i] += sum(preds[label_indexes] == labels[label_indexes]).item()
                class_count[i] += len(label_indexes)


        epoch_loss = running_loss / running_count
        epoch_acc = running_correct.item() / running_count

        class_acc = [class_correct[i] / class_count[i] for i in range(self.ncats)]

        print('{} Loss: {:.4f} Acc: {:.4f}'.format(
            phase, epoch_loss, epoch_acc))

        for i in range(self.ncats):
            print('Accuracy of class {} : {} / {} = {:.4f} %'.format(
                i, class_correct[i], class_count[i], class_acc[i]))


        time_elapsed = time.time() - since
        print('\nTesting complete in {:.0f}m {:.0f}s'.format(
            time_elapsed // 60, time_elapsed % 60))

        return epoch_loss, epoch_acc, class_acc, time_elapsed


    def predict(self):
        print("Predict")

        self.hook_output = []

        def hook(model, input, output):
            self.hook_output += [[i[0][0] for i in k] for k in output.tolist()]

        avgpool_layer = self.model._modules.get('avgpool')

        avgpool_layer.register_forward_hook(hook)

        self.pmodel = copy.deepcopy(self.model)

        pred_out, proba_out, feats_out, time_elapsed = self._predict()

        return pred_out, proba_out, feats_out, time_elapsed


    def _predict(self):

        phase = "predict"

        since = time.time()

        self.export_to_device()

        self.pmodel.eval()

        pred_out = []
        proba_out = []

        # iterate over data
        for inputs, _ in self.dataloaders[phase]:
            inputs = inputs.to(self.device)

            with torch.set_grad_enabled(False):
                outputs = self.pmodel(inputs)
                _, preds = torch.max(outputs, 1)
                pred_out += preds.tolist()
                proba = torch.nn.functional.softmax(outputs, dim=1)
                proba_out += proba.tolist()

        feats_out = self.hook_output

        time_elapsed = time.time() - since
        print('\nPrediction completed in {:.0f}m {:.0f}s \n'.format(
            time_elapsed // 60, time_elapsed % 60))

        # load best model weights
        return pred_out, proba_out, feats_out, time_elapsed
