#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import cv2
import numpy as np
import tensorflow as tf
import argparse
from six.moves import zip
import os, sys

from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *
from idcard_dataset import IdCard
from pennfudanped_dataset import PennFudanPed


DATA_DIR = '../data/'
DATASET = None


class Model(ModelDesc):

    def _get_input_vars(self):
        return [InputVar(tf.float32, [None, None, None, 3], 'image'),
                InputVar(tf.int32, [None, None, None], 'heatmap')]

    def _build_graph(self, input_vars):
        image, heatmap = input_vars
        image = image - tf.constant([104, 116, 122], dtype='float32')
        heatmap = tf.expand_dims(heatmap, 3, name='heatmap4d')

        def branch(name, l, up):
            with tf.variable_scope(name) as scope:
                l = Conv2D('convfc', l, 1, kernel_shape=1, nl=tf.identity,
                           use_bias=True,
                           W_init=tf.constant_initializer(),
                           b_init=tf.constant_initializer())
                while up != 1:
                    l = BilinearUpSample('upsample{}'.format(up), l, 2)
                    up = up / 2
                return l

        with argscope(Conv2D, kernel_shape=3, nl=tf.nn.relu):
            l = Conv2D('conv1_1', image, 64)
            l = Conv2D('conv1_2', l, 64)
            b1 = branch('branch1', l, 1)
            l = MaxPooling('pool1', l, 2)

            l = Conv2D('conv2_1', l, 128)
            l = Conv2D('conv2_2', l, 128)
            b2 = branch('branch2', l, 2)
            l = MaxPooling('pool2', l, 2)

            l = Conv2D('conv3_1', l, 256)
            l = Conv2D('conv3_2', l, 256)
            l = Conv2D('conv3_3', l, 256)
            b3 = branch('branch3', l, 4)
            l = MaxPooling('pool3', l, 2)

            l = Conv2D('conv4_1', l, 512)
            l = Conv2D('conv4_2', l, 512)
            l = Conv2D('conv4_3', l, 512)
            b4 = branch('branch4', l, 8)
            l = MaxPooling('pool4', l, 2)

            l = Conv2D('conv5_1', l, 512)
            l = Conv2D('conv5_2', l, 512)
            l = Conv2D('conv5_3', l, 512)
            b5 = branch('branch5', l, 16)

        final_map = Conv2D('convfcweight',
                           tf.concat_v2([b5], 3), 1, 1,
                           W_init=tf.constant_initializer(0.2),
                           use_bias=False, nl=tf.identity)
        costs = []
        for idx, b in enumerate([b5, final_map]):
            output = tf.nn.sigmoid(b, name='output{}'.format(idx + 1))
            xentropy = class_balanced_sigmoid_cross_entropy(
                b, heatmap,
                name='xentropy{}'.format(idx + 1))
            costs.append(xentropy)

        # some magic threshold
        pred = tf.cast(tf.greater(output, 0.5), tf.int32, name='prediction')
        wrong = tf.cast(tf.not_equal(pred, heatmap), tf.float32)
        wrong = tf.reduce_mean(wrong, name='train_error')

        if get_current_tower_context().is_training:
            wd_w = tf.train.exponential_decay(2e-4, get_global_step_var(),
                                              80000, 0.7, True)
            wd_cost = tf.multiply(wd_w, regularize_cost('.*/W', tf.nn.l2_loss), name='wd_cost')
            costs.append(wd_cost)

            add_param_summary(('.*/W', ['histogram']))   # monitor W
            self.cost = tf.add_n(costs, name='cost')
            add_moving_summary(costs + [wrong, self.cost])

    def get_gradient_processor(self):
        return [ScaleGradient([('convfcweight.*', 0.1), ('conv5_.*', 5)])]


def get_data(name):
    isTrain = name == 'train'
    ds = DATASET('val', DATA_DIR)

    class CropMultiple16(imgaug.ImageAugmentor):

        def _get_augment_params(self, img):
            newh = img.shape[0] // 16 * 16
            neww = img.shape[1] // 16 * 16
            assert newh > 0 and neww > 0
            diffh = img.shape[0] - newh
            h0 = 0 if diffh == 0 else self.rng.randint(diffh)
            diffw = img.shape[1] - neww
            w0 = 0 if diffw == 0 else self.rng.randint(diffw)
            return (h0, w0, newh, neww)

        def _augment(self, img, param):
            h0, w0, newh, neww = param
            return img[h0:h0 + newh, w0:w0 + neww]

    class Identity(imgaug.ImageAugmentor):

        def _get_augment_params(self, img):
            return None

        def _augment(self, img, param):
            return img

    if isTrain:
        shape_aug = [
            imgaug.RandomResize(xrange=(0.7, 1.5), yrange=(0.7, 1.5),
                                aspect_ratio_thres=0.15),
            imgaug.RotationAndCropValid(90),
            CropMultiple16(),
            imgaug.Flip(horiz=True),
            imgaug.Flip(vert=True)
        ]
    else:
        shape_aug = [Identity()]
    ds = AugmentImageComponents(ds, shape_aug, (0, 1))

    def f(m):
        m[m >= 0.50] = 1
        m[m < 0.50] = 0
        return m
    ds = MapDataComponent(ds, f, 1)

    if isTrain:
        augmentors = [
            imgaug.Brightness(63, clip=False),
            imgaug.Contrast((0.4, 1.5)),
        ]
        ds = AugmentImageComponent(ds, augmentors)
        ds = BatchDataByShape(ds, 8, idx=0)
        ds = PrefetchDataZMQ(ds, 1)
    else:
        ds = BatchData(ds, 1)
    return ds


def view_data():
    ds = RepeatedData(get_data('train'), -1)
    ds.reset_state()
    for ims, heatmaps in ds.get_data():
        for im, heatmap in zip(ims, heatmaps):
            assert im.shape[0] % 16 == 0 and im.shape[1] % 16 == 0, im.shape
            cv2.imshow("im", im / 255.0)
            cv2.waitKey(1000)
            cv2.imshow("edge", heatmap)
            cv2.waitKey(1000)


def get_config():
    logger.auto_set_dir()
    dataset_train = get_data('train')
    step_per_epoch = dataset_train.size() * 40
    dataset_val = get_data('val')

    lr = get_scalar_var('learning_rate', 3e-5, summary=True)
    return TrainConfig(
        dataflow=dataset_train,
        optimizer=tf.train.AdamOptimizer(lr, epsilon=1e-3),
        callbacks=Callbacks([
            StatPrinter(), ModelSaver(),
            ScheduledHyperParamSetter('learning_rate', [(30, 6e-6), (45, 1e-6), (60, 8e-7)]),
            HumanHyperParamSetter('learning_rate'),
            InferenceRunner(dataset_val,
                            BinaryClassificationStats('prediction', 'heatmap4d'))
        ]),
        model=Model(),
        step_per_epoch=step_per_epoch,
        max_epoch=100,
    )


def generate(model_path):
    pred_config = PredictConfig(
        model=Model(),
        session_init=get_model_loader(model_path),
        input_names=['image'],
        output_names=['output' + str(k) for k in range(1, 3)])
    predict_func = get_predict_func(pred_config)
    while True:
        im = yield
        assert im is not None
        im = cv2.resize(im, (im.shape[1] // 16 * 16, im.shape[0] // 16 * 16))
        outputs = predict_func([[im.astype('float32')]])
        yield outputs[0][0]


def run(model_path, image_path, output):
    generator = generate(model_path)
    generator.next()
    cv2.imwrite('out.png', generator.send(cv2.imread(image_path)) * 255)
    generator.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', help='choose dataset; available options: "idcard", "pennfudanped"', required=True)
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--view', help='view dataset', action='store_true')
    parser.add_argument('--run', help='run model on images')
    parser.add_argument('--output', help='fused output filename. default to out-fused.png')
    args = parser.parse_args()

    if args.dataset == 'idcard':
        DATASET = IdCard
    elif args.dataset == 'pennfudanped':
        DATASET = PennFudanPed
    else:
        raise ValueError('unknown dataset name: {}'.format(args.dataset))

    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.view:
        view_data()
    elif args.run:
        run(args.load, args.run, args.output)
    else:
        config = get_config()
        if args.load:
            config.session_init = get_model_loader(args.load)
        if args.gpu:
            config.nr_tower = len(args.gpu.split(','))
        SyncMultiGPUTrainer(config).train()

