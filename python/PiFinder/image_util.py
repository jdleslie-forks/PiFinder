#!/usr/bin/python
# -*- coding:utf-8 -*-
"""
This module has some general
image processing utils
mainly related to the preview
function

"""

from PIL import Image, ImageChops
import numpy as np


def uniform_filter_numpy(image: np.ndarray, size: int = 25) -> np.ndarray:
    """
    Box blur using cumulative sums (fast, no scipy dependency).

    Replaces scipy.ndimage.filters.uniform_filter to eliminate
    ~60 MB scipy import from MAIN process.
    """
    if size <= 1:
        return image

    # Pad image to handle edges
    pad = size // 2
    padded = np.pad(image, pad, mode='reflect')

    # Cumulative sum approach for O(1) per-pixel box blur
    # Compute cumulative sum along rows
    cumsum = np.cumsum(padded, axis=0, dtype=np.float64)
    cumsum = np.insert(cumsum, 0, 0, axis=0)
    row_sum = cumsum[size:] - cumsum[:-size]

    # Compute cumulative sum along columns
    cumsum = np.cumsum(row_sum, axis=1, dtype=np.float64)
    cumsum = np.insert(cumsum, 0, 0, axis=1)
    box_sum = cumsum[:, size:] - cumsum[:, :-size]

    # Average
    result = box_sum / (size * size)

    return result.astype(image.dtype)


def make_red(in_image, colors):
    return ImageChops.multiply(in_image.convert("RGB"), colors.red_image)


def gamma_correct_low(in_value):
    return gamma_correct(in_value, 0.9)


def gamma_correct_med(in_value):
    return gamma_correct(in_value, 0.7)


def gamma_correct_high(in_value):
    return gamma_correct(in_value, 0.5)


def gamma_correct(in_value, gamma):
    in_value = float(in_value) / 255
    out_value = pow(in_value, gamma)
    out_value = int(255 * out_value)
    return out_value


def subtract_background(image, percent=1):
    image = np.asarray(image, dtype=np.float32)
    if image.ndim == 3:
        assert image.shape[2] in (1, 3), "Colour image must have 1 or 3 colour channels"
        if image.shape[2] == 3:
            # Convert to greyscale
            image = (
                image[:, :, 0] * 0.299 + image[:, :, 1] * 0.587 + image[:, :, 2] * 0.114
            )
        else:
            # Delete empty dimension
            image = image.squeeze(axis=2)
    else:
        assert image.ndim == 2, "Image must be 2D or 3D array"

    image = image - (uniform_filter_numpy(image, size=25) * percent)
    return Image.fromarray(image)


def convert_image_to_mode(image: Image.Image, mode: str):
    if mode == "RGB":
        return Image.fromarray(np.array(image)[:, :, ::-1])
    return image
