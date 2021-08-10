# Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import time
import numpy as np
import paddle
import paddle.fluid as fluid
import box_utils
import reader
from utility import print_arguments, parse_args
from models.yolov3 import YOLOv3
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval, Params
from config import cfg
import cv2
from PIL import Image
from PIL import ImageDraw
paddle.enable_static()


place = fluid.CUDAPlace(0) if True else fluid.CPUPlace()
exe1 = fluid.Executor(place)
exe2 = fluid.Executor(place)
path = 'freezed_model'  # 'model/freeze_model'
[inference_program1, feed_target_names1, fetch_targets1] = fluid.io.load_inference_model(dirname=path, executor=exe1,
                                                                                      model_filename='completed_pruned_model',
                                                                                      params_filename='completed_pruned_params')

def draw_bbox_image(img, boxes, labels, scores,label_names,thre,gt=False):
    """
    给图片画上外接矩形框
    :param img:
    :param boxes:
    :param save_name:
    :param labels
    :return:
    """
    color = ['red', 'blue']
    if gt:
        c = color[1]
    else:
        c = color[0]
    img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)
    for box, label,score in zip(boxes, labels, scores):
        if score >= thre:
            xmin, ymin, xmax, ymax = box[0], box[1], box[2], box[3]
            draw.rectangle((xmin, ymin, xmax, ymax), None, c, width=3)
            draw.text((xmin, ymin), label_names[int(label)], (255, 255, 0))
        
    return img

def read_image(img):
    """
    读取图片
    :param img_path:
    :return:
    """
    origin = img
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img = np.array(img).astype('float32')
    
    h, w, _ = img.shape
    im_scale_x = cfg.input_size / float(w)
    im_scale_y = cfg.input_size / float(h)
    img = cv2.resize(img, None, None, 
                                 fx=im_scale_x, fy=im_scale_y, 
                                 interpolation=cv2.INTER_CUBIC)
    mean = np.array(cfg.pixel_means).reshape((1, 1, -1))
    std = np.array(cfg.pixel_stds).reshape((1, 1, -1))
    resized_img = img.copy()
    img = (img / 255.0 - mean) / std
    img = np.array(img).astype('float32').transpose((2, 0, 1))
    img = img[np.newaxis, :]
    return origin, img, resized_img

def resize_img(img, target_size):
    """
    保持比例的缩放图片
    :param img:
    :param target_size:
    :return:
    """
    img = img.resize(target_size[1:], Image.BILINEAR)

    return img



def infer():

    if not os.path.exists('output'):
        os.mkdir('output')

    path = os.path.join(cfg.image_path, cfg.image_name)
    img = cv2.imread(path)
    image = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    origin, tensor_img, resized_img = read_image(image)
    #print(tensor_img)
    input_w, input_h = origin.size[0], origin.size[1]
    image_shape = np.array([input_h, input_w], dtype='int32')
    outputs = exe1.run(inference_program1,
            fetch_list=fetch_targets1,
            feed={feed_target_names1[0]: tensor_img,
                  feed_target_names1[1]: image_shape[np.newaxis, :]},
            return_numpy=False)
        

    bboxes = np.array(outputs[0])
    if bboxes.shape[1] != 6:
        print("No object found in {}".format(image_name))
    labels = bboxes[:, 0].astype('int32')
    scores = bboxes[:, 1].astype('float32')
    boxes = bboxes[:, 2:].astype('float32')

        
    path = os.path.join(cfg.image_path, cfg.image_name)
    #box_utils.draw_boxes_on_image(path, boxes, scores, labels, label_names, cfg.draw_thresh)
    img = cv2.imread(path)
    input_size = cfg.input_size
    infer_reader = reader.infer(input_size, os.path.join(cfg.image_path, cfg.image_name))
    label_names, _ = reader.get_label_infos()
    img = draw_bbox_image(img, boxes, labels, scores, label_names,cfg.draw_thresh)
    img = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    print('检测到目标')
    cv2.imwrite('result1.jpg', img)


if __name__ == '__main__':
    args = parse_args()
    print_arguments(args)
    infer()
