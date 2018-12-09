import multiprocessing
import os
# Requires TensorFlow 1.3+ and Keras 2.0.8+.
from distutils.version import LooseVersion

import keras
import keras.backend as K
import keras.engine as KE
import keras.layers as KL
import keras.models as KM
import numpy as np
import re
import tensorflow as tf
# used for buliding mobilenet backbone
from keras_applications import get_keras_submodule
from keras_applications.mobilenet import _depthwise_conv_block
from mrcnn import utils

import myolo.myolo_utils as mutils
from myolo.config import Config as config

assert LooseVersion(tf.__version__) >= LooseVersion("1.3")
assert LooseVersion(keras.__version__) >= LooseVersion('2.0.8')

backend = get_keras_submodule('backend')


############################################################
# Build an incomplete mobilenetv1 graph as backbone
############################################################


def relu6(x):
    return backend.relu(x, max_value=6)


def conv_block(inputs, filters, alpha=1.0, kernel=(3, 3), strides=(1, 1)):
    channel_axis = 1 if backend.image_data_format() == 'channels_first' else -1
    filters = int(filters * alpha)
    x = KL.ZeroPadding2D(padding=(1, 1), name='conv1_pad')(inputs)
    x = KL.Conv2D(filters, kernel,
                  padding='valid',
                  use_bias=False,
                  strides=strides,
                  name='conv1')(x)
    x = KL.BatchNormalization(axis=channel_axis, name='conv1_bn')(x)
    return KL.Activation(relu6, name='conv1_relu')(x)


def mobilenet_graph(input_image, architecture, stage5=False, alpha=1.0, depth_multiplier=1):
    """ Build a incompleted mobilenetv1 graph so as to generate enough spatial feature map
    resolution (28x28), one more depthwise block added for bigger d dimension.
    architecture: can be mobilenet, resnet50 or resnet100
    stage5: boolean. if create stage5 for the network or not
    alpha and depth_multiplier are parameters for mobilenet, the regular setting is 1 for both
    """
    assert architecture == 'mobilenet'

    # 224x224x3
    x = conv_block(input_image, 32, strides=(2, 2))

    # 112x112x32
    x = _depthwise_conv_block(x, 64, alpha, depth_multiplier, block_id=1)
    x = _depthwise_conv_block(x, 64, alpha, depth_multiplier, strides=(2, 2), block_id=2)

    # 56x56x64
    x = _depthwise_conv_block(x, 128, alpha, depth_multiplier, block_id=3)
    x = _depthwise_conv_block(x, 256, alpha, depth_multiplier, strides=(2, 2), block_id=4)

    # 28x28x256
    x = _depthwise_conv_block(x, 256, alpha, depth_multiplier, block_id=5)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=6)  # added by me

    return x  # output feature map shape [28x28x512]


############################################################
# YOLO branch to generat bbox and objectness prob
############################################################

def yolo_custom_loss(y_true, y_pred, true_boxes):
    mask_shape = tf.shape(y_true)[:4]

    cell_x = tf.to_float(
        tf.reshape(tf.tile(tf.range(config.GRID_W), [config.GRID_H]), (1, config.GRID_H, config.GRID_W, 1, 1)))
    cell_y = tf.transpose(cell_x, (0, 2, 1, 3, 4))

    cell_grid = tf.tile(tf.concat([cell_x, cell_y], -1), [config.BATCH_SIZE, 1, 1, config.N_BOX, 1])

    coord_mask = tf.zeros(mask_shape)
    conf_mask = tf.zeros(mask_shape)
    class_mask = tf.zeros(mask_shape)

    seen = tf.Variable(0.)
    total_recall = tf.Variable(0.)

    """
    Adjust prediction
    """
    ### adjust x and y
    pred_box_xy = tf.sigmoid(y_pred[..., :2]) + cell_grid

    ### adjust w and h
    pred_box_wh = tf.exp(y_pred[..., 2:4]) * np.reshape(config.ANCHORS, [1, 1, 1, config.N_BOX, 2])

    ### adjust confidence
    pred_box_conf = tf.sigmoid(y_pred[..., 4])

    ### adjust class probabilities
    pred_box_class = y_pred[..., 5:]

    """
    Adjust ground truth
    """
    ### adjust x and y
    true_box_xy = y_true[..., 0:2]  # relative position to the containing cell

    ### adjust w and h
    true_box_wh = y_true[..., 2:4]  # number of cells accross, horizontally and vertically

    ### adjust confidence
    true_wh_half = true_box_wh / 2.
    true_mins = true_box_xy - true_wh_half
    true_maxes = true_box_xy + true_wh_half

    pred_wh_half = pred_box_wh / 2.
    pred_mins = pred_box_xy - pred_wh_half
    pred_maxes = pred_box_xy + pred_wh_half

    intersect_mins = tf.maximum(pred_mins, true_mins)
    intersect_maxes = tf.minimum(pred_maxes, true_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]

    true_areas = true_box_wh[..., 0] * true_box_wh[..., 1]
    pred_areas = pred_box_wh[..., 0] * pred_box_wh[..., 1]

    union_areas = pred_areas + true_areas - intersect_areas
    iou_scores = tf.truediv(intersect_areas, union_areas)

    true_box_conf = iou_scores * y_true[..., 4]

    ### adjust class probabilities
    true_box_class = tf.argmax(y_true[..., 5:], -1)

    """
    Determine the masks
    """
    ### coordinate mask: simply the position of the ground truth boxes (the predictors)
    coord_mask = tf.expand_dims(y_true[..., 4], axis=-1) * config.COORD_SCALE

    ### confidence mask: penelize predictors + penalize boxes with low IOU
    # penalize the confidence of the boxes, which have IOU with some ground truth box < 0.6
    true_xy = true_boxes[..., 0:2]
    true_wh = true_boxes[..., 2:4]

    true_wh_half = true_wh / 2.
    true_mins = true_xy - true_wh_half
    true_maxes = true_xy + true_wh_half

    pred_xy = tf.expand_dims(pred_box_xy, 4)
    pred_wh = tf.expand_dims(pred_box_wh, 4)

    pred_wh_half = pred_wh / 2.
    pred_mins = pred_xy - pred_wh_half
    pred_maxes = pred_xy + pred_wh_half

    intersect_mins = tf.maximum(pred_mins, true_mins)
    intersect_maxes = tf.minimum(pred_maxes, true_maxes)
    intersect_wh = tf.maximum(intersect_maxes - intersect_mins, 0.)
    intersect_areas = intersect_wh[..., 0] * intersect_wh[..., 1]

    true_areas = true_wh[..., 0] * true_wh[..., 1]
    pred_areas = pred_wh[..., 0] * pred_wh[..., 1]

    union_areas = pred_areas + true_areas - intersect_areas
    iou_scores = tf.truediv(intersect_areas, union_areas)

    best_ious = tf.reduce_max(iou_scores, axis=4)
    conf_mask = conf_mask + tf.to_float(best_ious < 0.6) * (1 - y_true[..., 4]) * config.NO_OBJECT_SCALE

    # penalize the confidence of the boxes, which are reponsible for corresponding ground truth box
    conf_mask = conf_mask + y_true[..., 4] * config.OBJECT_SCALE

    ### class mask: simply the position of the ground truth boxes (the predictors)
    class_mask = y_true[..., 4] * tf.gather(config.CLASS_WEIGHTS, true_box_class) * config.CLASS_SCALE

    """
    Warm-up training
    """
    no_boxes_mask = tf.to_float(coord_mask < config.COORD_SCALE / 2.)
    seen = tf.assign_add(seen, 1.)

    true_box_xy, true_box_wh, coord_mask = tf.cond(tf.less(seen, config.WARM_UP_BATCHES),
                                                   lambda: [true_box_xy + (0.5 + cell_grid) * no_boxes_mask,
                                                            true_box_wh + tf.ones_like(true_box_wh) * np.reshape(
                                                                config.ANCHORS,
                                                                [1, 1, 1, config.N_BOX, 2]) * no_boxes_mask,
                                                            tf.ones_like(coord_mask)],
                                                   lambda: [true_box_xy,
                                                            true_box_wh,
                                                            coord_mask])

    """
    Finalize the loss
    """
    nb_coord_box = tf.reduce_sum(tf.to_float(coord_mask > 0.0))
    nb_conf_box = tf.reduce_sum(tf.to_float(conf_mask > 0.0))
    nb_class_box = tf.reduce_sum(tf.to_float(class_mask > 0.0))

    loss_xy = tf.reduce_sum(tf.square(true_box_xy - pred_box_xy) * coord_mask) / (nb_coord_box + 1e-6) / 2.
    loss_wh = tf.reduce_sum(tf.square(true_box_wh - pred_box_wh) * coord_mask) / (nb_coord_box + 1e-6) / 2.
    loss_conf = tf.reduce_sum(tf.square(true_box_conf - pred_box_conf) * conf_mask) / (nb_conf_box + 1e-6) / 2.
    loss_class = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=true_box_class, logits=pred_box_class)
    loss_class = tf.reduce_sum(loss_class * class_mask) / (nb_class_box + 1e-6)

    loss = loss_xy + loss_wh + loss_conf + loss_class

    nb_true_box = tf.reduce_sum(y_true[..., 4])
    nb_pred_box = tf.reduce_sum(tf.to_float(true_box_conf > 0.5) * tf.to_float(pred_box_conf > 0.3))

    """
    Debugging code
    """
    current_recall = nb_pred_box / (nb_true_box + 1e-6)
    total_recall = tf.assign_add(total_recall, current_recall)

    # loss = tf.Print(loss, [tf.zeros((1))], message='Dummy Line \t', summarize=1000)
    # loss = tf.Print(loss, [loss_xy], message='Loss XY \t', summarize=1000)
    # loss = tf.Print(loss, [loss_wh], message='Loss WH \t', summarize=1000)
    # loss = tf.Print(loss, [loss_conf], message='Loss Conf \t', summarize=1000)
    # loss = tf.Print(loss, [loss_class], message='Loss Class \t', summarize=1000)
    # loss = tf.Print(loss, [loss], message='Total Loss \t', summarize=1000)
    # loss = tf.Print(loss, [current_recall], message='Current Recall \t', summarize=1000)
    # loss = tf.Print(loss, [total_recall / seen], message='Average Recall \t', summarize=1000)

    return loss


def yolo_branch_graph(x, true_boxes, config, alpha=1.0, depth_multiplier=1):
    """ YOLO branch following the feature map to generate bbox based on prior anchors
    :param x: input feature map
    :param true_boxes: input_true_boxes
    :return: output with shape [None, 7, 7, 5, NUM_CLASSES+5]
    """
    # 28x28x512
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, strides=(2, 2), block_id=7)

    # 14x14x512
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=8)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=9)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=10)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=11)
    x = _depthwise_conv_block(x, 512, alpha, depth_multiplier, block_id=12)

    x = _depthwise_conv_block(x, 1024, alpha, depth_multiplier, strides=(2, 2), block_id=13)

    # 7x7x1024
    x = _depthwise_conv_block(x, 1024, alpha, depth_multiplier, block_id=14)

    # yolo output
    x = KL.Conv2D(config.N_BOX * (4 + 1 + config.NUM_CLASSES), (1, 1), strides=(1, 1),
                  padding='same', name='conv_23')(x)
    output = KL.Reshape((config.GRID_H, config.GRID_W, config.N_BOX, 4 + 1 + config.NUM_CLASSES))(x)

    # small hack to allow true_boxes to be registered when Keras build the model
    output = KL.Lambda(lambda args: args[0])([output, true_boxes])

    return output


def build_yolo_model(config, depth):
    """ Build a keras model for the YOLO model
    :param depth: depth of input feature map, for now is 512
    :return: a keras model object, the last layer of the model is a standard YOLOv2 output
    with shape [None, GRID_H, GRID_W, N_BOX, 5 + NUM_CLASSES]
    """
    input_feature_map = KL.Input(shape=[None, None, depth], name="input_yolo_feature_map")
    input_true_boxes = KL.Input(shape=(1, 1, 1, config.TRUE_BOX_BUFFER, 4))
    output = yolo_branch_graph(input_feature_map, input_true_boxes, config)

    return KM.Model([input_feature_map, input_true_boxes], output, name="yolo_model")


############################################################
#  ROIAlign Layer
############################################################

def log2_graph(x):
    """Implementation of Log2. TF doesn't have a native implementation."""
    return tf.log(x) / tf.log(2.0)


class PyramidROIAlign(KE.Layer):
    """Implements ROI Pooling on multiple levels of the feature pyramid.

    Params:
    - pool_shape: [pool_height, pool_width] of the output pooled regions. Usually [7, 7]

    Inputs:
    - boxes: [batch, num_boxes, (xmin, ymin, xmax, ymax)] in normalized
             coordinates. Possibly padded with zeros if not enough
             boxes to fill the array.
    - image_meta: [batch, (meta data)] Image details. See compose_image_meta()
    - feature_maps: List of feature maps from different levels of the pyramid.
                    Each is [batch, height, width, channels]

    Output:
    Pooled regions in the shape: [batch, num_boxes, pool_height, pool_width, channels].
    The width and height are those specific in the pool_shape in the layer
    constructor.
    """

    def __init__(self, pool_shape, **kwargs):
        super(PyramidROIAlign, self).__init__(**kwargs)
        self.pool_shape = tuple(pool_shape)

    def call(self, inputs):
        # Crop boxes [batch, num_boxes, (xmin, ymin, xmax, ymax)] in normalized coords
        boxes = inputs[0]

        # Image meta
        # Holds details about the image. See compose_image_meta()
        # image_meta = inputs[1]

        # Feature Maps. List of feature maps from different level of the
        # feature pyramid. Each is [batch, height, width, channels]
        feature_maps = inputs[1:]

        # Assign each ROI to a level in the pyramid based on the ROI area.
        x1, y1, x2, y2 = tf.split(boxes, 4, axis=2)
        h = y2 - y1
        w = x2 - x1
        # Use shape of first image. Images in a batch must have the same size.
        # image_shape = parse_image_meta_graph(image_meta)['image_shape'][0]
        image_shape = [224, 224]
        # Equation 1 in the Feature Pyramid Networks paper. Account for
        # the fact that our coordinates are normalized here.
        # e.g. a 224x224 ROI (in pixels) maps to P4
        image_area = tf.cast(image_shape[0] * image_shape[1], tf.float32)
        roi_level = log2_graph(tf.sqrt(h * w) / (224.0 / tf.sqrt(image_area)))
        # roi_level = tf.minimum(5, tf.maximum(
        #     2, 4 + tf.cast(tf.round(roi_level), tf.int32)))
        roi_level = tf.minimum(0, tf.maximum(
            0, 4 + tf.cast(tf.round(roi_level), tf.int32)))
        roi_level = tf.squeeze(roi_level, 2)

        new_roi_level = tf.cast(5, tf.int8)

        # Loop through levels and apply ROI pooling to each. P2 to P5.
        pooled = []
        box_to_level = []
        for i, level in enumerate(range(0, 1)):
            ix = tf.where(tf.equal(roi_level, level))
            level_boxes = tf.gather_nd(boxes, ix)

            # Box indices for crop_and_resize.
            box_indices = tf.cast(ix[:, 0], tf.int32)

            # Keep track of which box is mapped to which level
            box_to_level.append(ix)

            # Stop gradient propogation to ROI proposals
            level_boxes = tf.stop_gradient(level_boxes)
            box_indices = tf.stop_gradient(box_indices)

            # Crop and Resize
            # From Mask R-CNN paper: "We sample four regular locations, so
            # that we can evaluate either max or average pooling. In fact,
            # interpolating only a single value at each bin center (without
            # pooling) is nearly as effective."
            #
            # Here we use the simplified approach of a single value per bin,
            # which is how it's done in tf.crop_and_resize()
            # Result: [batch * num_boxes, pool_height, pool_width, channels]
            pooled.append(tf.image.crop_and_resize(
                feature_maps[i], level_boxes, box_indices, self.pool_shape,
                method="bilinear"))

        # Pack pooled features into one tensor
        pooled = tf.concat(pooled, axis=0)

        # Pack box_to_level mapping into one array and add another
        # column representing the order of pooled boxes
        box_to_level = tf.concat(box_to_level, axis=0)
        box_range = tf.expand_dims(tf.range(tf.shape(box_to_level)[0]), 1)
        box_to_level = tf.concat([tf.cast(box_to_level, tf.int32), box_range],
                                 axis=1)

        # Rearrange pooled features to match the order of the original boxes
        # Sort box_to_level by batch then box index
        # TF doesn't have a way to sort by two columns, so merge them and sort.
        sorting_tensor = box_to_level[:, 0] * 100000 + box_to_level[:, 1]
        ix = tf.nn.top_k(sorting_tensor, k=tf.shape(
            box_to_level)[0]).indices[::-1]
        ix = tf.gather(box_to_level[:, 2], ix)
        pooled = tf.gather(pooled, ix)

        # Re-add the batch dimension
        shape = tf.concat([tf.shape(boxes)[:2], tf.shape(pooled)[1:]], axis=0)
        pooled = tf.reshape(pooled, shape)
        return pooled

    def compute_output_shape(self, input_shape):
        return input_shape[0][:2] + self.pool_shape + (input_shape[1][-1],)


############################################################
#  Detection Target Layer
############################################################

def overlaps_graph(boxes1, boxes2):
    """Computes IoU overlaps between two sets of boxes.
    boxes1, boxes2: [N, (x1, y1, x2, y2)].
    """
    # 1. Tile boxes2 and repeat boxes1. This allows us to compare
    # every boxes1 against every boxes2 without loops.
    # TF doesn't have an equivalent to np.repeat() so simulate it
    # using tf.tile() and tf.reshape.
    b1 = tf.reshape(tf.tile(tf.expand_dims(boxes1, 1),
                            [1, 1, tf.shape(boxes2)[0]]), [-1, 4])
    b2 = tf.tile(boxes2, [tf.shape(boxes1)[0], 1])

    # 2. Compute intersections
    b1_x1, b1_y1, b1_x2, b1_y2 = tf.split(b1, 4, axis=1)
    b2_x1, b2_y1, b2_x2, b2_y2 = tf.split(b2, 4, axis=1)

    x1 = tf.maximum(b1_x1, b2_x1)
    y1 = tf.maximum(b1_y1, b2_y1)
    x2 = tf.minimum(b1_x2, b2_x2)
    y2 = tf.minimum(b1_y2, b2_y2)

    intersection = tf.maximum(x2 - x1, 0) * tf.maximum(y2 - y1, 0)

    # 3. Compute unions
    b1_area = (b1_y2 - b1_y1) * (b1_x2 - b1_x1)
    b2_area = (b2_y2 - b2_y1) * (b2_x2 - b2_x1)
    union = b1_area + b2_area - intersection

    # 4. Compute IoU and reshape to [boxes1, boxes2]
    iou = intersection / union
    overlaps = tf.reshape(iou, [tf.shape(boxes1)[0], tf.shape(boxes2)[0]])

    return overlaps


def detect_mask_target_graph(yolo_proposals, gt_class_ids, gt_boxes, gt_masks, config):
    """Generates detection targets for one image. Subsamples proposals and
    generates target class IDs, bounding box deltas, and masks for each.

    Inputs:
    yolo_rois: [7x7x3, (xmin, ymin, xmax, ymax)] in normalized coordinates. Might
               be zero padded if there are not enough proposals.
    gt_class_ids: [TRUE_BOX_BUFFER] int class IDs
    gt_boxes: [TRUE_BOX_BUFFER, (xmin, ymin, xmax, ymax)] in normalized coordinates.
    gt_masks: [height, width, MAX_GT_INSTANCES] of boolean type.

    Returns: Target ROIs and corresponding class IDs, bounding box shifts,
    and masks.
    rois: [TRAIN_ROIS_PER_IMAGE, (xmin, ymin, xmax, ymax)] in normalized coordinates
    class_ids: [TRAIN_ROIS_PER_IMAGE]. Integer class IDs. Zero padded.
    # deltas: [TRAIN_ROIS_PER_IMAGE, (dy, dx, log(dh), log(dw))]
    masks: [TRAIN_ROIS_PER_IMAGE, height, width]. Masks cropped to bbox
           boundaries and resized to neural network output size.

    Note: Returned arrays might be zero padded if not enough target ROIs.
    """
    # Assertions
    asserts = [
        tf.Assert(tf.greater(tf.shape(yolo_proposals)[0], 0), [yolo_proposals],
                  name="roi_assertion"),
    ]
    with tf.control_dependencies(asserts):
        yolo_proposals = tf.identity(yolo_proposals)

    # Remove zero padding
    # proposals, _ = trim_zeros_graph(proposals, name="trim_proposals")
    # gt_boxes, non_zeros = trim_zeros_graph(gt_boxes, name="trim_gt_boxes")
    # gt_class_ids = tf.boolean_mask(gt_class_ids, non_zeros,
    #                                name="trim_gt_class_ids")
    # gt_masks = tf.gather(gt_masks, tf.where(non_zeros)[:, 0], axis=2,
    #                      name="trim_gt_masks")

    # Handle COCO crowds
    # A crowd box in COCO is a bounding box around several instances. Exclude
    # them from training. A crowd box is given a negative class ID.
    # crowd_ix = tf.where(gt_class_ids < 0)[:, 0]
    # non_crowd_ix = tf.where(gt_class_ids > 0)[:, 0]
    # crowd_boxes = tf.gather(gt_boxes, crowd_ix)
    # crowd_masks = tf.gather(gt_masks, crowd_ix, axis=2)
    # gt_class_ids = tf.gather(gt_class_ids, non_crowd_ix)
    # gt_boxes = tf.gather(gt_boxes, non_crowd_ix)
    # gt_masks = tf.gather(gt_masks, non_crowd_ix, axis=2)

    # Compute overlaps matrix [proposals, gt_boxes]
    overlaps = overlaps_graph(yolo_proposals, gt_boxes)

    # Compute overlaps with crowd boxes [proposals, crowd_boxes]
    # crowd_overlaps = overlaps_graph(proposals, crowd_boxes)
    # crowd_iou_max = tf.reduce_max(crowd_overlaps, axis=1)
    # no_crowd_bool = (crowd_iou_max < 0.001)

    # Determine positive and negative ROIs
    roi_iou_max = tf.reduce_max(overlaps, axis=1)
    # 1. Positive ROIs are those with >= 0.5 IoU with a GT box
    positive_roi_bool = (roi_iou_max >= 0.5)                # TODO
    positive_indices = tf.where(positive_roi_bool)[:, 0]
    # 2. Negative ROIs are those with < 0.5 with every GT box. Skip crowds.
    negative_indices = tf.where(roi_iou_max < 0.5)[:, 0]

    # Subsample ROIs. Aim for 33% positive
    # Positive ROIs
    # positive_count = int(config.TRAIN_ROIS_PER_IMAGE *
    #                      config.ROI_POSITIVE_RATIO)
    # positive_count = tf.shape(positive_indices)[0]
    # positive_indices = tf.random_shuffle(positive_indices)[:positive_count]
    # positive_count = tf.shape(positive_indices)[0]

    # Negative ROIs. Add enough to maintain positive:negative ratio.
    # r = 1.0 / config.ROI_POSITIVE_RATIO
    # negative_count = tf.cast(r * tf.cast(positive_count, tf.float32), tf.int32) - positive_count
    # negative_indices = tf.random_shuffle(negative_indices)[:negative_count]

    # Gather selected ROIs
    positive_rois = tf.gather(yolo_proposals, positive_indices)
    negative_rois = tf.gather(yolo_proposals, negative_indices)

    # Assign positive ROIs to GT boxes.
    positive_overlaps = tf.gather(overlaps, positive_indices)
    roi_gt_box_assignment = tf.cond(
        tf.greater(tf.shape(positive_overlaps)[1], 0),
        true_fn=lambda: tf.argmax(positive_overlaps, axis=1),
        false_fn=lambda: tf.cast(tf.constant([]), tf.int64)
    )
    roi_gt_boxes = tf.gather(gt_boxes, roi_gt_box_assignment)
    roi_gt_class_ids = tf.gather(gt_class_ids, roi_gt_box_assignment)

    # Compute bbox refinement for positive ROIs
    # deltas = mutils.box_refinement_graph(positive_rois, roi_gt_boxes)
    # deltas /= config.BBOX_STD_DEV

    # Assign positive ROIs to GT masks
    # Permute masks to [N, height, width, 1]
    transposed_masks = tf.expand_dims(tf.transpose(gt_masks, [2, 0, 1]), -1)
    # Pick the right mask for each ROI
    roi_masks = tf.gather(transposed_masks, roi_gt_box_assignment)

    # Compute mask targets
    x1, y1, x2, y2 = tf.split(positive_rois, 4, axis=1)
    boxes = tf.concat([y1, x1, y2, x2], axis=1)     # tf.image.crop_and_resize required
    # boxes = positive_rois

    # TODO: correct this
    if config.USE_MINI_MASK:
        pass

        # Transform ROI coordinates from normalized image space
        # to normalized mini-mask space.
        # x1, y1, x2, y2 = tf.split(positive_rois, 4, axis=1)
        # gt_x1, gt_y1, gt_x2, gt_y2 = tf.split(roi_gt_boxes, 4, axis=1)
        # gt_w = gt_x2 - gt_x1
        # gt_h = gt_y2 - gt_y1
        #
        # x1 = (x1 - gt_x1) / gt_w
        # y1 = (y1 - gt_y1) / gt_h
        # x2 = (x2 - gt_x1) / gt_w
        # y2 = (y2 - gt_y1) / gt_h
        #
        # boxes = tf.concat([y1, x1, y2, x2], axis=1)  # tf.image.crop_and_resize required

    box_ids = tf.range(0, tf.shape(roi_masks)[0])
    masks = tf.image.crop_and_resize(tf.cast(roi_masks, tf.float32), boxes,
                                     box_ids,
                                     config.MASK_SHAPE)
    # Remove the extra dimension from masks.
    masks = tf.squeeze(masks, axis=3)

    # Threshold mask pixels at 0.5 to have GT masks be 0 or 1 to use with
    # binary cross entropy loss.
    masks = tf.round(masks)

    # Append negative ROIs and pad bbox deltas and masks that
    # are not used for negative ROIs with zeros.
    yolo_rois = tf.concat([positive_rois, negative_rois], axis=0)
    # N = tf.shape(negative_rois)[0]
    # P = tf.maximum(config.TRAIN_ROIS_PER_IMAGE - tf.shape(yolo_rois)[0], 0)
    # yolo_rois = tf.pad(yolo_rois, [(0, P), (0, 0)])
    # roi_gt_boxes = tf.pad(roi_gt_boxes, [(0, N), (0, 0)])
    # roi_gt_class_ids = tf.pad(roi_gt_class_ids, [(0, N)])
    # deltas = tf.pad(deltas, [(0, N + P), (0, 0)])
    # masks = tf.pad(masks, [[0, N + P], (0, 0), (0, 0)])

    return yolo_rois, roi_gt_class_ids, 0, masks


class DetectMaskTargetLayer(KE.Layer):
    """ Assign targets (target_class_id, target_deltas, target_mask)
    to yolo_rois

    Inputs:
    yolo_rois: [batch, N, (x1, y1, x2, y2)] in normalized coordinates.
                No zero padding
    gt_class_ids: [batch, MAX_GT_INSTANCES] Integer class IDs.
    gt_boxes: [batch, MAX_GT_INSTANCES, (x1, y1, x2, y2)] in normalized
              coordinates.
    gt_masks: [batch, height, width, MAX_GT_INSTANCES] of boolean type

    Returns: Target ROIs and corresponding class IDs, bounding box shifts,
    and masks.
    rois: [batch, N, (x1, y1, x2, y2)] in normalized coordinates
    target_class_ids: [batch, N]. Integer class IDs.
    # target_deltas: [batch, N, (dy, dx, log(dh), log(dw)] used to compute
    box loss in Mask-RCNN but here it's been included in YOLO loss
    target_mask: [batch, N, height, width]
                 Masks cropped to bbox boundaries and resized to neural
                 network output size.

    Note: Returned arrays might be zero padded if not enough target ROIs.
    """

    def __init__(self, config, **kwargs):
        super(DetectMaskTargetLayer, self).__init__(**kwargs)
        self.config = config

    def call(self, inputs):
        proposals = inputs[0]
        gt_class_ids = inputs[1]
        gt_boxes = inputs[2]
        gt_masks = inputs[3]

        # Slice the batch and run a graph for each slice
        # TODO: Rename target_bbox to target_deltas for clarity
        names = ["yolo_rois", "target_class_ids", "target_bbox", "target_mask"]
        outputs = utils.batch_slice(
            [proposals, gt_class_ids, gt_boxes, gt_masks],
            lambda w, x, y, z: detect_mask_target_graph(
                w, x, y, z, self.config),
            self.config.BATCH_SIZE, names=names)
        return outputs

    def compute_output_shape(self, input_shape):
        return [
            (None, self.config.TRAIN_ROIS_PER_IMAGE, 4),  # rois
            (None, self.config.TRAIN_ROIS_PER_IMAGE),  # class_ids
            (None, self.config.TRAIN_ROIS_PER_IMAGE, 4),  # deltas
            (None, self.config.TRAIN_ROIS_PER_IMAGE, self.config.MASK_SHAPE[0],
             self.config.MASK_SHAPE[1])  # masks
        ]

    def compute_mask(self, inputs, mask=None):
        return [None, None, None, None]


############################################################
#  Mask Graph
############################################################


def build_mask_graph(rois, feature_maps, pool_size, num_classes, train_bn=False):
    """Builds the computation graph of the mask head of Feature Pyramid Network.

    rois: [batch, num_rois, (xmin, ymin, xmax, ymax)] Proposal boxes in normalized
          coordinates.
    feature_maps: List of feature maps from different layers of the pyramid,
                  [P2, P3, P4, P5]. Each has a different resolution.
    image_meta: [batch, (meta data)] Image details. See compose_image_meta()
    pool_size: The width of the square feature map generated from ROI Pooling.
    num_classes: number of classes, which determines the depth of the results
    train_bn: Boolean. Train or freeze Batch Norm layers

    Returns: Masks [batch, num_rois, MASK_POOL_SIZE, MASK_POOL_SIZE, NUM_CLASSES]
    """
    # ROI Pooling
    # Shape: [batch, num_rois, MASK_POOL_SIZE, MASK_POOL_SIZE, channels]
    x = PyramidROIAlign([pool_size, pool_size],
                        name="roi_align_mask")([rois] + feature_maps)  # [8, ?, 14, 14, 512]

    # Conv layers
    x = KL.TimeDistributed(KL.Conv2D(512, (3, 3), padding="same"),
                           name="mrcnn_mask_conv1")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(), name='mrcnn_mask_bn1')(x)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(512, (3, 3), padding="same"),
                           name="mrcnn_mask_conv2")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn2')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(512, (3, 3), padding="same"),
                           name="mrcnn_mask_conv3")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn3')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2D(512, (3, 3), padding="same"),
                           name="mrcnn_mask_conv4")(x)
    x = KL.TimeDistributed(KL.BatchNormalization(),
                           name='mrcnn_mask_bn4')(x, training=train_bn)
    x = KL.Activation('relu')(x)

    x = KL.TimeDistributed(KL.Conv2DTranspose(512, (2, 2), strides=2, activation="relu"),
                           name="mrcnn_mask_deconv")(x)
    x = KL.TimeDistributed(KL.Conv2D(num_classes, (1, 1), strides=1, activation="sigmoid"),
                           name="mrcnn_mask")(x)
    return x


def mrcnn_mask_loss_graph(target_masks, target_class_ids, pred_masks):
    """Mask binary cross-entropy loss for the masks head.

    target_masks: [batch, num_rois, height, width].
        A float32 tensor of values 0 or 1. Uses zero padding to fill array.
    target_class_ids: [batch, num_rois]. Integer class IDs. Zero padded.
    pred_masks: [batch, proposals, height, width, num_classes] float32 tensor
                with values from 0 to 1.
    """
    # Reshape for simplicity. Merge first two dimensions into one.
    target_class_ids = K.reshape(target_class_ids, (-1,))
    mask_shape = tf.shape(target_masks)
    target_masks = K.reshape(target_masks, (-1, mask_shape[2], mask_shape[3]))
    pred_shape = tf.shape(pred_masks)
    pred_masks = K.reshape(pred_masks,
                           (-1, pred_shape[2], pred_shape[3], pred_shape[4]))
    # Permute predicted masks to [N, num_classes, height, width]
    pred_masks = tf.transpose(pred_masks, [0, 3, 1, 2])

    # Only positive ROIs contribute to the loss. And only
    # the class specific mask of each ROI.
    positive_ix = tf.where(target_class_ids > 0)[:, 0]
    positive_class_ids = tf.cast(
        tf.gather(target_class_ids, positive_ix), tf.int64)
    indices = tf.stack([positive_ix, positive_class_ids], axis=1)

    # Gather the masks (predicted and true) that contribute to loss
    y_true = tf.gather(target_masks, positive_ix)
    y_pred = tf.gather_nd(pred_masks, indices)

    # Compute binary cross entropy. If no positive ROIs, then return 0.
    # shape: [batch, roi, num_classes]
    loss = K.switch(tf.size(y_true) > 0,
                    K.binary_crossentropy(target=y_true, output=y_pred),
                    tf.constant(0.0))
    loss = K.mean(loss)
    return loss


############################################################
# Mask YOLO class
############################################################


class MaskYOLO():
    """ Build the overall structure of MaskYOLO class
    which generate bbox and class label on the YOLO side based on that then added with a Mask branch
    Note to myself: all the operations have to be built with Tensor and Layer so as to generate TF Graph
    """

    def __init__(self, mode, config, model_dir=None):
        """
        mode: Either "training" or "inference"
        config: A Sub-class of the Config class
        model_dir: Directory to save training logs and trained weights
        """
        assert mode in ['training', 'inference']
        self.mode = mode
        self.config = config
        self.model_dir = model_dir
        self.keras_model = self.build(mode=mode, config=config)
        self.epoch = 0

    def build(self, mode, config):
        assert mode in ['training', 'inference']

        # TODO: make constraints on input image size
        # h, w = config.IMAGE_SHAPE[:2]
        # if h / 2 ** 6 != int(h / 2 ** 6) or w / 2 ** 6 != int(w / 2 ** 6):
        #     raise Exception("Image size must be dividable by 2 at least 6 times "
        #                     "to avoid fractions when downscaling and upscaling."
        #                     "For example, use 256, 320, 384, 448, 512, ... etc. ")

        # input image -> KL.Input
        input_image = KL.Input(shape=[None, None, config.IMAGE_SHAPE[2]], name="input_image")

        if mode == "training":
            # input_yolo_anchors and true_boxes
            input_true_boxes = KL.Input(shape=(1, 1, 1, config.TRUE_BOX_BUFFER, 4), name="input_true_boxes")
            input_yolo_target = KL.Input(
                shape=[config.GRID_H, config.GRID_W, config.N_BOX, 4 + 1 + config.NUM_CLASSES],
                name="input_yolo_target", dtype=tf.float32)

            # Detection GT (class IDs, bounding boxes, and masks)

            # 1. GT Class IDs (zero padded)
            input_gt_class_ids = KL.Input(
                shape=[None], name="input_gt_class_ids", dtype=tf.int32)

            # 2. GT Boxes in pixels (zero padded)

            # [batch, MAX_GT_INSTANCES, (y1, x1, y2, x2)] in image coordinates
            input_gt_boxes = KL.Input(
                shape=[None, 4], name="input_gt_boxes", dtype=tf.float32)

            # Normalize box coordinates (divide by the image width and height)
            gt_boxes = KL.Lambda(lambda x: norm_boxes_graph(
                x, K.shape(input_image)[1:3]))(input_gt_boxes)

            # GT Masks (zero padded)  TODO
            if config.USE_MINI_MASK:
                input_gt_masks = KL.Input(shape=[config.MINI_MASK_SHAPE[0],
                                                 config.MINI_MASK_SHAPE[1], None],
                                          name="input_gt_masks", dtype=bool)
            else:
                input_gt_masks = KL.Input(shape=[config.IMAGE_SHAPE[0],
                                                 config.IMAGE_SHAPE[1], None],
                                          name="input_gt_masks", dtype=bool)
        elif mode == "inference":
            raise NotImplementedError

        myolo_feature_maps = C4 = mobilenet_graph(input_image, config.BACKBONE, stage5=False)

        # build YOLO branch graph
        yolo_model = build_yolo_model(config, config.TOP_FEATURE_MAP_DEPTH)
        yolo_output = yolo_model([myolo_feature_maps, input_true_boxes])

        # feature_map_shape = [int((config.IMAGE_SHAPE[0] / config.BACKBONE_STRIDES)[0]),
        #                      int((config.IMAGE_SHAPE[1] / config.BACKBONE_STRIDES)[0])]

        # yolo_rois = batch_yolo_decode(yolo_output, feature_map_shape, config)
        yolo_rois = DecodeYOLOLayer(name='decode_yolo_layer', config=config)([yolo_output])

        rois, target_class_ids, dummy, target_mask = \
            DetectMaskTargetLayer(config, name="proposal_targets")([
                yolo_rois, input_gt_class_ids, gt_boxes, input_gt_masks])

        myolo_mask = build_mask_graph(rois, [myolo_feature_maps],
                                      config.MASK_POOL_SIZE,
                                      config.NUM_CLASSES)
        output_rois = KL.Lambda(lambda x: x * 1, name="output_rois")(yolo_rois)

        # 1. YOLO custom loss (bbox loss and binary classification loss)
        yolo_sum_loss = KL.Lambda(lambda x: yolo_custom_loss(*x), name="yolo_sum_loss")(
            [input_yolo_target, yolo_output, input_true_boxes])
        # 2. mask_loss
        mask_loss = KL.Lambda(lambda x: mrcnn_mask_loss_graph(*x), name="mrcnn_mask_loss")(
            [target_mask, target_class_ids, myolo_mask])

        # Model
        inputs = [input_image, input_true_boxes, input_yolo_target,
                  input_gt_class_ids, input_gt_boxes, input_gt_masks]

        outputs = [output_rois, myolo_mask, yolo_sum_loss, mask_loss]

        model = KM.Model(inputs, outputs, name="mask_yolo")

        return model

    def train(self, train_dataset, val_dataset, learning_rate, epochs, layers,
              augmentation=None, custom_callbacks=None, no_augmentation_sources=None):
        """Train the model.
        train_dataset, val_dataset: Training and validation Dataset objects.
        learning_rate: The learning rate to train with
        epochs: Number of training epochs. Note that previous training epochs
                are considered to be done alreay, so this actually determines
                the epochs to train in total rather than in this particaular
                call.
        layers: Allows selecting wich layers to train. It can be:
            - A regular expression to match layer names to train
            - One of these predefined values:
              heads: The RPN, classifier and mask heads of the network
              all: All the layers
              3+: Train Resnet stage 3 and up
              4+: Train Resnet stage 4 and up
              5+: Train Resnet stage 5 and up
        augmentation: Optional. An imgaug (https://github.com/aleju/imgaug)
            augmentation. For example, passing imgaug.augmenters.Fliplr(0.5)
            flips images right/left 50% of the time. You can pass complex
            augmentations as well. This augmentation applies 50% of the
            time, and when it does it flips images right/left half the time
            and adds a Gaussian blur with a random sigma in range 0 to 5.

                augmentation = imgaug.augmenters.Sometimes(0.5, [
                    imgaug.augmenters.Fliplr(0.5),
                    imgaug.augmenters.GaussianBlur(sigma=(0.0, 5.0))
                ])
	    custom_callbacks: Optional. Add custom callbacks to be called
	        with the keras fit_generator method. Must be list of type keras.callbacks.
        no_augmentation_sources: Optional. List of sources to exclude for
            augmentation. A source is string that identifies a dataset and is
            defined in the Dataset class.
        """
        assert self.mode == "training", "Create model in training mode."

        # Pre-defined layer regular expressions
        layer_regex = {
            # all layers but the backbone
            "heads": r"(mrcnn\_.*)|(rpn\_.*)|(fpn\_.*)",
            # From a specific Resnet stage and up
            "3+": r"(res3.*)|(bn3.*)|(res4.*)|(bn4.*)|(res5.*)|(bn5.*)|(mrcnn\_.*)|(rpn\_.*)|(fpn\_.*)",
            "4+": r"(res4.*)|(bn4.*)|(res5.*)|(bn5.*)|(mrcnn\_.*)|(rpn\_.*)|(fpn\_.*)",
            "5+": r"(res5.*)|(bn5.*)|(mrcnn\_.*)|(rpn\_.*)|(fpn\_.*)",
            # All layers
            "all": ".*",
        }
        if layers in layer_regex.keys():
            layers = layer_regex[layers]

        # Data generators
        train_generator = mutils.data_generator(train_dataset, self.config, shuffle=True,
                                                augmentation=augmentation,
                                                batch_size=self.config.BATCH_SIZE,
                                                no_augmentation_sources=no_augmentation_sources)
        val_generator = mutils.data_generator(val_dataset, self.config, shuffle=True,
                                              batch_size=self.config.BATCH_SIZE)

        # Create log_dir if it does not exist
        # if not os.path.exists(self.log_dir):
        #     os.makedirs(self.log_dir)

        # Callbacks
        callbacks = [
            keras.callbacks.TensorBoard(log_dir='./', histogram_freq=0, write_graph=True, write_images=False),
            keras.callbacks.ModelCheckpoint('./', verbose=0, save_weights_only=True),
        ]

        # Add custom callbacks to the list
        # if custom_callbacks:
        #     callbacks += custom_callbacks

        # Train
        # log("\nStarting at epoch {}. LR={}\n".format(self.epoch, learning_rate))
        # log("Checkpoint Path: {}".format(self.checkpoint_path))
        self.set_trainable(layers)
        self.compile(learning_rate, self.config.LEARNING_MOMENTUM)

        # Work-around for Windows: Keras fails on Windows when using
        # multiprocessing workers. See discussion here:
        # https://github.com/matterport/Mask_RCNN/issues/13#issuecomment-353124009
        if os.name is 'nt':
            workers = 0
        else:
            workers = multiprocessing.cpu_count()

        self.keras_model.fit_generator(
            train_generator,
            initial_epoch=self.epoch,
            epochs=epochs,
            steps_per_epoch=self.config.STEPS_PER_EPOCH,
            callbacks=callbacks,
            validation_data=val_generator,
            validation_steps=self.config.VALIDATION_STEPS,
            max_queue_size=100,
            workers=workers,
            use_multiprocessing=True,
        )
        self.epoch = max(self.epoch, epochs)

    def compile(self, learning_rate, momentum):
        """Gets the model ready for training. Adds losses, regularization, and
        metrics. Then calls the Keras compile() function.
        """
        # Optimizer object
        optimizer = keras.optimizers.SGD(
            lr=learning_rate, momentum=momentum,
            clipnorm=self.config.GRADIENT_CLIP_NORM)
        # Add Losses
        # First, clear previously set losses to avoid duplication
        self.keras_model._losses = []
        self.keras_model._per_input_losses = {}
        loss_names = ["yolo_sum_loss",  "mrcnn_mask_loss"]
        for name in loss_names:
            layer = self.keras_model.get_layer(name)
            if layer.output in self.keras_model.losses:
                continue
            loss = (
                tf.reduce_mean(layer.output, keepdims=True)
                * self.config.LOSS_WEIGHTS.get(name, 1.))
            self.keras_model.add_loss(loss)

        # Add L2 Regularization
        # Skip gamma and beta weights of batch normalization layers.
        reg_losses = [
            keras.regularizers.l2(self.config.WEIGHT_DECAY)(w) / tf.cast(tf.size(w), tf.float32)
            for w in self.keras_model.trainable_weights
            if 'gamma' not in w.name and 'beta' not in w.name]
        self.keras_model.add_loss(tf.add_n(reg_losses))

        # Compile
        self.keras_model.compile(
            optimizer=optimizer,
            loss=[None] * len(self.keras_model.outputs))

        # Add metrics for losses
        for name in loss_names:
            if name in self.keras_model.metrics_names:
                continue
            layer = self.keras_model.get_layer(name)
            self.keras_model.metrics_names.append(name)
            loss = (
                tf.reduce_mean(layer.output, keepdims=True)
                * self.config.LOSS_WEIGHTS.get(name, 1.))
            self.keras_model.metrics_tensors.append(loss)

    def set_trainable(self, layer_regex, keras_model=None, indent=0, verbose=1):
        """Sets model layers as trainable if their names match
        the given regular expression.
        """
        # Print message on the first call (but not on recursive calls)
        # if verbose > 0 and keras_model is None:
        #     log("Selecting layers to train")

        keras_model = keras_model or self.keras_model

        # In multi-GPU training, we wrap the model. Get layers
        # of the inner model because they have the weights.
        layers = keras_model.inner_model.layers if hasattr(keras_model, "inner_model")\
            else keras_model.layers

        for layer in layers:
            # Is the layer a model?
            if layer.__class__.__name__ == 'Model':
                print("In model: ", layer.name)
                self.set_trainable(
                    layer_regex, keras_model=layer, indent=indent + 4)
                continue

            if not layer.weights:
                continue
            # Is it trainable?
            trainable = bool(re.fullmatch(layer_regex, layer.name))
            # Update layer. If layer is a container, update inner layer.
            if layer.__class__.__name__ == 'TimeDistributed':
                layer.layer.trainable = trainable
            else:
                layer.trainable = trainable
            # Print trainable layer names
            # if trainable and verbose > 0:
            #     log("{}{:20}   ({})".format(" " * indent, layer.name,
            #                                 layer.__class__.__name__))


def norm_boxes_graph(boxes, shape):
    """Converts boxes from pixel coordinates to normalized coordinates.
    boxes: [..., (x1, y1, x2, y2)] in pixel coordinates
    shape: [..., (height, width)] in pixels

    Note: In pixel coordinates (x2, y2) is outside the box. But in normalized
    coordinates it's inside the box.

    Returns:
        [..., (x1, y1, x2, y2)] in normalized coordinates
    """
    h, w = tf.split(tf.cast(shape, tf.float32), 2)
    scale = tf.concat([w, h, w, h], axis=-1) - tf.constant(1.0)
    shift = tf.constant([0., 0., 1., 1.])
    return tf.divide(boxes - shift, scale)


############################################################
# Decode YOLO output to final bbox
# (equivalent to ProposalLayer + DetectionTargetLayer)
############################################################


class DecodeYOLOLayer(KE.Layer):
    """ DecodeYOLOLayer: similar to the idea of 'ProposalLayer' in Mask-RCNN.
    inputs[0] is the output of YOLO last layer with shape [None, 7, 7, 3, 9]
    Here we decode the YOLO output, convert bx, by, tw, th to x1, y1, x2, y2
    in normalized form (0-1)

    """
    def __init__(self, config, **kwargs):
        super(DecodeYOLOLayer, self).__init__(**kwargs)
        self.config = config

    def call(self, inputs):
        y_pred = inputs[0]
        mask_shape = tf.shape(y_pred)[:4]

        cell_x = tf.to_float(
            tf.reshape(tf.tile(tf.range(config.GRID_W), [config.GRID_H]), (1, config.GRID_H, config.GRID_W, 1, 1)))
        cell_y = tf.transpose(cell_x, (0, 2, 1, 3, 4))

        cell_grid = tf.tile(tf.concat([cell_x, cell_y], -1), [config.BATCH_SIZE, 1, 1, config.N_BOX, 1])

        """ Adjust prediction """
        ### adjust x and y
        pred_box_xy = tf.sigmoid(y_pred[..., :2]) + cell_grid

        ### adjust w and h
        pred_box_wh = tf.exp(y_pred[..., 2:4]) * np.reshape(config.ANCHORS, [1, 1, 1, config.N_BOX, 2])

        """ get x, y coordinates """
        # pred_xy = tf.expand_dims(pred_box_xy, 4)
        # pred_wh = tf.expand_dims(pred_box_wh, 4)

        pred_wh_half = pred_box_wh / 2.
        pred_mins = pred_box_xy - pred_wh_half
        pred_maxes = pred_box_xy + pred_wh_half

        # xmin, ymin, xmax, ymax
        output_boxes = tf.concat([pred_mins, pred_maxes], axis=-1)
        output_boxes = tf.reshape(output_boxes, [output_boxes.shape[0],
                                                 output_boxes.shape[1] * output_boxes.shape[2] * output_boxes.shape[3],
                                                 output_boxes.shape[-1]])

        return output_boxes

    def compute_output_shape(self, input_shape):
        return (None, input_shape[1] * input_shape[2] * input_shape[3], 4)


# def decode_yolo4one(yolo_out, anchors, nb_class, feature_map_shape, obj_thre=0.3, nms_thre=0.3):
#     """
#     :param yolo_out: with shape [7, 7, 5, 7]
#     :param anchors:
#     :param nb_class:
#     :param obj_thre:
#     :param nms_thre:
#     :return:
#     """
#     grid_h, grid_w, nb_box = yolo_out.shape[:3]
#
#     boxes = []
#     fm_height = feature_map_shape[0]
#     fm_width = feature_map_shape[1]
#
#     # decode the output by the network
#     # yolo_out[..., 4] = _sigmoid(yolo_out[..., 4])  # sigmoid for confidence score to make it from 0 to 1
#     # yolo_out[..., 5:] = yolo_out[..., 4][..., np.newaxis] * _softmax(yolo_out[..., 5:])  # softmax for class prob
#     # yolo_out[..., 5:] *= yolo_out[..., 5:] > obj_thre  # select bbox with prob higher than threshold
#
#     yolo_out[..., 4] = tf.nn.sigmoid(yolo_out[..., 4])
#     yolo_out[..., 5:] = yolo_out[..., 4][..., tf.newaxis] * tf.nn.softmax(yolo_out[..., 5:])
#
#     for row in range(grid_h):
#         for col in range(grid_w):
#             for b in range(nb_box):
#                 # from 4th element onwards are confidence and class classes
#                 classes = yolo_out[row, col, b, 5:]
#
#                 if np.sum(classes) > 0:
#                     # first 4 elements are x, y, w, and h
#                     x, y, w, h = yolo_out[row, col, b, :4]
#
#                     x = (col + _sigmoid(x)) / grid_w  # center position, unit: image width
#                     y = (row + _sigmoid(y)) / grid_h  # center position, unit: image height
#                     w = anchors[2 * b + 0] * np.exp(w) / grid_w  # unit: image width
#                     h = anchors[2 * b + 1] * np.exp(h) / grid_h  # unit: image height
#                     confidence = yolo_out[row, col, b, 4]
#
#                     # generate bbox on the 28x28 feature max
#                     box = mutils.BoundBox(x - w / 2, y - h / 2, x + w / 2, y + h / 2, confidence, classes)
#                     xmin = min(int(box.xmin * fm_width), fm_height)
#                     ymin = min(int(box.ymin * fm_height), fm_height)
#                     xmax = min(int(box.xmax * fm_width), fm_width)
#                     ymax = min(int(box.ymax * fm_height), fm_height)
#
#                     box = mutils.BoundBox(xmin, ymin, xmax, ymax, confidence, classes)
#                     # box = BoundBox(x - w / 2, y - h / 2, x + w / 2, y + h / 2, confidence, classes)
#                     boxes.append(box)  # xmin, ymin, xmax, ymax, confidence, classes
#
#     # suppress non-maximal boxes
#     for c in range(nb_class):
#         sorted_indices = list(reversed(np.argsort([box.classes[c] for box in boxes])))
#
#         for i in range(len(sorted_indices)):
#             index_i = sorted_indices[i]
#
#             if boxes[index_i].classes[c] == 0:
#                 continue
#             else:
#                 for j in range(i + 1, len(sorted_indices)):
#                     index_j = sorted_indices[j]
#
#                     if mutils.bbox_iou(boxes[index_i], boxes[index_j]) >= nms_thre:
#                         boxes[index_j].classes[c] = 0
#
#     # remove the boxes which are less likely than a obj_threshold
#     # boxes = [box for box in boxes if box.get_score() > obj_thre]
#
#     return boxes


