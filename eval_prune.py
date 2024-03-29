#  Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import os
import time
import json
import numpy as np
import paddle
import paddle.fluid as fluid
import reader
from models.yolov3 import YOLOv3
from utility import print_arguments, parse_args
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval, Params
from config import cfg
from paddleslim.prune import Pruner
from paddleslim.analysis import flops
paddle.enable_static()

def get_pruned_params(train_program):
    params = []
    #skip_vars = ['yolo_input']  # skip the first conv2d layer
    for block in train_program.blocks:
        for param in block.all_parameters():
            if ('conv' in param.name)  and ('yolo_input' not in param.name) and ('downsample' not in param.name) : #and ('stage.0' not in param.name)and ('stage.1' not in param.name)and ('stage.2' not in param.name)
                if  ('yolo_block' in param.name) or ('stage.4' in param.name): #or ('stage.3' in param.name) 
                    params.append(param.name)#or ('batch_norm' in param.name)
    return params

def eval():
    if '2014' in cfg.dataset:
        test_list = 'annotations/instances_val2014.json'
    elif '2017' in cfg.dataset:
        test_list = 'annotations/instances_val2017.json'

    if cfg.debug:
        if not os.path.exists('output'):
            os.mkdir('output')

    model = YOLOv3(is_train=False)
    model.build_model()
    outputs = model.get_pred()
    place = fluid.CUDAPlace(1) if cfg.use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)
    train_program = fluid.default_main_program()


    # yapf: disable
    if cfg.pretrain:
        def if_exist(var):
            return os.path.exists(os.path.join(cfg.pretrain, var.name))
        fluid.io.load_vars(exe, cfg.pretrain, predicate=if_exist,main_program = train_program)

    

    #########prune


    pruned_params = get_pruned_params(train_program)
    pruned_ratios = []
    for param in pruned_params:
        if 'yolo_block.0.' in param:
            pruned_ratios.append(0.5)
        elif 'yolo_block.1.' in param:
            pruned_ratios.append(0.5)
        elif 'yolo_block.2.' in param:
            pruned_ratios.append(0.5)
        else:
            pruned_ratios.append(0.2)

    #pruned_params = cfg.prune_par.strip().split(",") #此处也可以通过写正则表达式匹配参数名
    print("pruned params: {}".format(pruned_params))
    #pruned_ratios = [float(n) for n in cfg.prune_ratio]
    print("pruned ratios: {}".format(pruned_ratios))

    pruner = Pruner()
    train_program = pruner.prune(
        train_program,
        fluid.global_scope(),
        params=pruned_params,
        ratios=pruned_ratios,
        place=place,
        only_graph=False)[0]
    
    param_delimit_str = '-' * 20 + "All parameters in current graph" + '-' * 20
    print(param_delimit_str)
    for block in train_program.blocks:
        for param in block.all_parameters():
            print("parameter name: {}\tshape: {}".format(param.name, param.shape))
    print('-' * len(param_delimit_str)) 
    
    if cfg.weights:
        def if_exist(var):
            return os.path.exists(os.path.join(cfg.weights, var.name))
        fluid.io.load_vars(exe, cfg.weights, predicate=if_exist,main_program = train_program)

    





    
    # yapf: enable
    input_size = cfg.input_size
    test_reader = reader.test(input_size, 1)
    label_names, label_ids = reader.get_label_infos()
    if cfg.debug:
        print("Load in labels {} with ids {}".format(label_names, label_ids))
    feeder = fluid.DataFeeder(place=place, feed_list=model.feeds())

    def get_pred_result(boxes, scores, labels, im_id):
        result = []
        for box, score, label in zip(boxes, scores, labels):
            x1, y1, x2, y2 = box
            w = x2 - x1 + 1
            h = y2 - y1 + 1
            bbox = [x1, y1, w, h]
            
            res = {
                    'image_id': im_id,
                    'category_id': label_ids[int(label)],
                    'bbox': list(map(float, bbox)),
                    'score': float(score)
            }
            result.append(res)
        return result

    dts_res = []
    fetch_list = [outputs]
    total_time = 0
    for batch_id, batch_data in enumerate(test_reader()):
        #if batch_id == 10000:
        #    break
        start_time = time.time()
        batch_outputs = exe.run(train_program,
            fetch_list=[v.name for v in fetch_list],
            feed=feeder.feed(batch_data),
            return_numpy=False,
            use_program_cache=True)
        lod = batch_outputs[0].lod()[0]
        nmsed_boxes = np.array(batch_outputs[0])
        if nmsed_boxes.shape[1] != 6:
            continue
        for i in range(len(lod) - 1):
            im_id = batch_data[i][1]
            start = lod[i]
            end = lod[i + 1]
            if start == end:
                continue
            nmsed_box = nmsed_boxes[start:end, :]
            labels = nmsed_box[:, 0]
            scores = nmsed_box[:, 1]
            boxes = nmsed_box[:, 2:6]
            dts_res += get_pred_result(boxes, scores, labels, im_id)

        end_time = time.time()
        print("batch id: {}, time: {}".format(batch_id, end_time - start_time))
        total_time += end_time - start_time

    with open("yolov3_result.json", 'w') as outfile:
        json.dump(dts_res, outfile)
    print("start evaluate detection result with coco api")
    coco = COCO(os.path.join(cfg.data_dir, test_list))
    cocoDt = coco.loadRes("yolov3_result.json")
    cocoEval = COCOeval(coco, cocoDt, 'bbox')
    cocoEval.evaluate()
    cocoEval.accumulate()
    cocoEval.summarize()
    print("evaluate done.")

    print("Time per batch: {}".format(total_time / batch_id))


if __name__ == '__main__':
    args = parse_args()
    print_arguments(args)
    eval()
