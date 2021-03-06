import torch.nn as nn
import torch
import os
import cv2
import glob
import json

import numpy as np
from torch.utils.data import Dataset


def decode_img(file_path, width=None, height=None):
    """
        Read an image amd resize when needed
    """
    img = cv2.imread(file_path)

    #img = np.subtract(img, 0.4)
    if width is not None and height is not None:
        img = cv2.resize(img, (width, height),
                         interpolation=cv2.INTER_LANCZOS4)
    # img = np.expand_dims(img, 0)
    img = np.transpose(img, (2, 0, 1))
    return img


def decode_mask(file_path, num_classes=80, nr_samples_from_mask=500):
    """
        Read the float file containing the object information
    """
    segim = cv2.imread(file_path)
    h, w, c = segim.shape
    cls_ids = np.unique(segim)
    cls_ids = cls_ids[cls_ids < num_classes]

    binary_mask = np.zeros((h, w, c), dtype=np.float32)

    if len(cls_ids) != 0:
        chosen_cls_id = np.random.choice(cls_ids)

        #down_ratios = 64 / h, 64 / w
        #y_found_ind = (yx[0] * down_ratios[0]).astype(np.int)
        #x_found_ind = (yx[1] * down_ratios[1]).astype(np.int)
        N = len(binary_mask[segim == chosen_cls_id])
        if N > 30000:
            binary_mask[segim == chosen_cls_id] = 255
            class_label_id = (chosen_cls_id + 1)
        else:
            class_label_id = 0
    else:
        class_label_id = 0

    binary_mask = cv2.resize(binary_mask, (128, 128))[:, :, 0]

    return binary_mask, class_label_id


class DataLoader():
    def __init__(self, root, split=None):
        self.root = root
        self.rgbs = []
        self.annos = []
        self.split = split
        if self.split == 'train2017':
            self.sample_no = 16 * 600
        else:
            self.sample_no = 16 * 10
        self.load_image_paths()
        self.load_anno_paths()

    def load_image_paths(self):
        self.rgbs.extend(
            sorted(glob.glob(os.path.join(self.root, 'images', self.split, '*.jpg'))))
        if self.sample_no != -1:
            self.rgbs = self.rgbs[:self.sample_no]
        print("Number of images: ", len(self.rgbs))

    def load_anno_paths(self):
        self.annos.extend(
            sorted(glob.glob(os.path.join(self.root, 'annotations', self.split, '*.png'))))
        if self.sample_no != -1:
            self.annos = self.annos[:self.sample_no]
        print("Number of annotations: ", len(self.annos))


class SamplePointData(Dataset):
    def __init__(self, args, split='train2017', width=320, height=576, test_id=0, root=None):

        self.args = args
        self.width = width
        self.height = height

        # train: <data_dir>/train
        # test: <data_dir>/test
        self.labelmap = {}

        # self.labelmap[0] = "unlabeled"
        with open(os.path.join(root, '../labels.txt')) as f:
            for line in f.readlines():
                id_, label_ = line.split(':')
                id_ = int(id_)
                if id_ > args.num_classes:
                    continue
                # only animals (16-25, bird, cat, dog ~~~ giraffe)
                # print(id_)
                self.labelmap[id_] = label_.strip()

        self.class_size = len(self.labelmap)

        self.dataset = DataLoader(root, split=split)

        self.split = 'train' if split == 'train2017' else 'val'
        self.test_id = test_id
        self.num_classes = args.num_classes

    def __len__(self):
        return len(self.dataset.rgbs)

    def __getitem__(self, idx):
        if self.split == 'train':
            # rand_index = np.random.choice(len(self.dataset.rgbs), 1)[0]
            img_path = self.dataset.rgbs[idx]
            anno_path = self.dataset.annos[idx]
        else:
            # rand_index = np.random.choice(len(self.dataset.rgbs), 1)[0]
            img_path = self.dataset.rgbs[idx]
            anno_path = self.dataset.annos[idx]

        color_img = decode_img(img_path, width=self.width, height=self.height)
        mask_img, class_label = decode_mask(
            anno_path, num_classes=self.num_classes)
        # onehot_class_condition = get_onehot_tensor(self.class_size, self.width,
        #                                 self.height, class_label)  # class id
        onehot_class_condition = np.eye(self.class_size, dtype=np.float32)[
            class_label]

        input, output, label_id, label_str = color_img, \
            mask_img, \
            onehot_class_condition, \
            self.labelmap[class_label]
        """
        if self.split == 'train':
            s = np.random.uniform(0, 1)
            if s > 0.5:
                input = np.flip(input, 2).copy()  # horizontally
                output[1] = 1.0 - output[1]
            s = np.random.uniform(0, 1)
            if s > 0.5:
                input = np.flip(input, 1).copy()  # vertically
                output[0] = 1.0 - output[0]
        """
        return input, output, label_id, label_str


def get_onehot_tensor(class_size, width, height, class_id):
    input = np.tile(np.expand_dims(np.expand_dims(
        np.eye(class_size)[class_id], axis=-1), axis=-1), (1, height, width))
    input = np.expand_dims(input, axis=0)
    return input


if __name__ == '__main__':

    import torch

    dataset = SamplePointData(width=256, height=256,
                              root='/home/syk/cocostuff/dataset')

    train_loader = torch.utils.data.DataLoader(
        dataset=dataset, batch_size=8, shuffle=True,
        num_workers=0, pin_memory=True)

    for x, y in train_loader:
        import pdb
        pdb.set_trace()
