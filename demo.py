#!/usr/bin/env python
# coding: utf-8
#
# Author: Kazuto Nakashima
# URL:    https://kazuto1011.github.io
# Date:   07 January 2019

from __future__ import absolute_import, division, print_function

import click
import cv2
import matplotlib
import matplotlib.cm as cm
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import yaml
from addict import Dict

from libs.models import DeepLabV2_ResNet101_MSC
from libs.utils import DenseCRF


def get_device(cuda):
    cuda = cuda and torch.cuda.is_available()
    device = torch.device("cuda" if cuda else "cpu")
    if cuda:
        current_device = torch.cuda.current_device()
        print("Device:", torch.cuda.get_device_name(current_device))
    else:
        print("Device: CPU")
    return device


def get_classtable(CONFIG):
    with open(CONFIG.LABELS) as f:
        classes = {}
        for label in f:
            label = label.rstrip().split("\t")
            classes[int(label[0])] = label[1].split(",")[0]
    return classes


def setup_model(model_path, device, CONFIG):
    model = DeepLabV2_ResNet101_MSC(n_classes=CONFIG.N_CLASSES)
    state_dict = torch.load(model_path, map_location=lambda storage, loc: storage)
    model.load_state_dict(state_dict)
    model.eval()
    model.to(device)
    return model


def setup_postprocessor(CONFIG):
    # CRF post-processor
    postprocessor = DenseCRF(
        iter_max=CONFIG.CRF.ITER_MAX,
        pos_xy_std=CONFIG.CRF.POS_XY_STD,
        pos_w=CONFIG.CRF.POS_W,
        bi_xy_std=CONFIG.CRF.BI_XY_STD,
        bi_rgb_std=CONFIG.CRF.BI_RGB_STD,
        bi_w=CONFIG.CRF.BI_W,
    )
    return postprocessor


def preprocessing(image, device, CONFIG):
    # Resize
    scale = CONFIG.IMAGE.SIZE.TEST / max(image.shape[:2])
    image = cv2.resize(image, dsize=None, fx=scale, fy=scale)
    raw_image = image.astype(np.uint8)

    # Subtract mean values
    image = image.astype(np.float32)
    image -= np.array(
        [
            float(CONFIG.IMAGE.MEAN.B),
            float(CONFIG.IMAGE.MEAN.G),
            float(CONFIG.IMAGE.MEAN.R),
        ]
    )

    # Convert to torch.Tensor and add "batch" axis
    image = torch.from_numpy(image.transpose(2, 0, 1)).float().unsqueeze(0)
    image = image.to(device)

    return image, raw_image


def inference(model, image, raw_image=None, postprocessor=None):
    B, C, H, W = image.shape

    # Image -> Probability map
    logits = model(image)
    logits = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=True)
    probs = F.softmax(logits, dim=1)
    probs = probs.data.cpu().numpy()[0]

    # Refine the prob map with CRF
    if postprocessor and raw_image is not None:
        probs = postprocessor(raw_image, probs)

    # Pixel-wise argmax
    labelmap = np.argmax(probs, axis=0)

    return labelmap


@click.group()
@click.pass_context
def main(ctx):
    print("Mode:", ctx.invoked_subcommand)


@main.command(help="Inference from a single image")
@click.option("-c", "--config", type=str, required=True, help="yaml")
@click.option("-i", "--image-path", type=str, required=True)
@click.option("-m", "--model-path", type=str, required=True, help="pth")
@click.option("--cuda/--no-cuda", default=True, help="Switch GPU/CPU")
@click.option("--crf", is_flag=True, help="CRF post processing")
def single(config, image_path, model_path, cuda, crf):
    # Disable autograd globally
    torch.set_grad_enabled(False)

    # Setup
    device = get_device(cuda)
    CONFIG = Dict(yaml.load(open(config)))
    classes = get_classtable(CONFIG)
    model = setup_model(model_path, device, CONFIG)
    postprocessor = setup_postprocessor(CONFIG) if crf else None

    # Inference
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    image, raw_image = preprocessing(image, device, CONFIG)
    labelmap = inference(model, image, raw_image, postprocessor)
    labels = np.unique(labelmap)

    # Show result for each class
    rows = np.floor(np.sqrt(len(labels) + 1))
    cols = np.ceil((len(labels) + 1) / rows)

    plt.figure(figsize=(10, 10))
    ax = plt.subplot(rows, cols, 1)
    ax.set_title("Input image")
    ax.imshow(raw_image[:, :, ::-1])
    ax.axis("off")

    for i, label in enumerate(labels):
        mask = labelmap == label
        ax = plt.subplot(rows, cols, i + 2)
        ax.set_title(classes[label])
        ax.imshow(raw_image[..., ::-1])
        ax.imshow(mask.astype(np.float32), alpha=0.5)
        ax.axis("off")

    plt.tight_layout()
    plt.show()


@main.command(help="Inference from camera stream")
@click.option("-c", "--config", type=str, required=True, help="yaml")
@click.option("-m", "--model-path", type=str, required=True, help="pth")
@click.option("--cuda/--no-cuda", default=True, help="Switch GPU/CPU")
@click.option("--crf", is_flag=True, help="CRF post processing")
@click.option("--camera-id", type=int, default=0)
def live(config, model_path, cuda, crf, camera_id):
    # Disable autograd globally
    torch.set_grad_enabled(False)

    # Setup
    device = get_device(cuda)
    CONFIG = Dict(yaml.load(open(config)))
    classes = get_classtable(CONFIG)
    model = setup_model(model_path, device, CONFIG)
    postprocessor = setup_postprocessor(CONFIG) if crf else None

    # UVC camera stream
    cap = cv2.VideoCapture(camera_id)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))

    def colorize(labelmap):
        # Assign a unique color to each label
        labelmap = labelmap.astype(np.float32) / CONFIG.N_CLASSES
        colormap = cm.jet_r(labelmap)[..., :-1] * 255.0
        return np.uint8(colormap)

    def mouse_event(event, x, y, flags, labelmap):
        # Show a class name of a mouse-overed pixel
        label = labelmap[y, x]
        name = classes[label]
        print(name)

    cv2.namedWindow("Segmentation Result", cv2.WINDOW_AUTOSIZE)

    while True:
        ret, frame = cap.read()
        image, raw_image = preprocessing(frame, device, CONFIG)
        labelmap = inference(model, image, raw_image, postprocessor)
        labels = np.unique(labelmap)
        colormap = colorize(labelmap)

        # Register mouse callback function
        cv2.setMouseCallback("Segmentation Result", mouse_event, labelmap)

        # Overlay prediction
        cv2.addWeighted(colormap, 0.5, raw_image, 0.5, 0.0, raw_image)

        # Quit by pressing "q" key
        cv2.imshow("Segmentation Result", raw_image)
        if cv2.waitKey(10) == ord("q"):
            break


if __name__ == "__main__":
    main()
