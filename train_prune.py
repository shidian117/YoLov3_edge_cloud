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
import sys
import numpy as np
import random
import time
import shutil
from utility import parse_args, print_arguments, SmoothedValue

import paddle
import paddle.fluid as fluid
import reader
from models.yolov3 import YOLOv3
from learning_rate import exponential_with_warmup_decay
from config import cfg
from paddleslim.prune import Pruner
from paddleslim.analysis import flops
paddle.enable_static()

def get_pruned_params(train_program):
    params = []
    #skip_vars = ['yolo_input']  # skip the first conv2d layer
    for block in train_program.blocks:
        for param in block.all_parameters():
            if ('conv' in param.name)  and ('yolo_input' not in param.name) and ('downsample' not in param.name):
                if  ('yolo_block' in param.name)  or ('stage.4' in param.name): #or ('stage.3' in param.name)
                    params.append(param.name)#or ('batch_norm' in param.name)

    return params



def train():

    if cfg.debug:
        fluid.default_startup_program().random_seed = 1000
        fluid.default_main_program().random_seed = 1000
        random.seed(0)
        np.random.seed(0)
        
    if not os.path.exists(cfg.model_save_dir):
        os.makedirs(cfg.model_save_dir)

    model = YOLOv3()
    model.build_model()
    input_size = cfg.input_size
    loss = model.loss()
    loss.persistable = True

    devices = os.getenv("CUDA_VISIBLE_DEVICES") or ""
    devices_num = len(devices.split(","))
    print("Found {} CUDA devices.".format(devices_num))

    learning_rate = cfg.learning_rate
    boundaries = cfg.lr_steps
    gamma = cfg.lr_gamma
    step_num = len(cfg.lr_steps)
    values = [learning_rate * (gamma**i) for i in range(step_num + 1)]

    optimizer = fluid.optimizer.Momentum(
        learning_rate=exponential_with_warmup_decay(
            learning_rate=learning_rate,
            boundaries=boundaries,
            values=values,
            warmup_iter=cfg.warm_up_iter,
            warmup_factor=cfg.warm_up_factor),
        regularization=fluid.regularizer.L2Decay(cfg.weight_decay),
        momentum=cfg.momentum)
    optimizer.minimize(loss)

    place = fluid.CUDAPlace(0) if cfg.use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())
    train_program = fluid.default_main_program()

    if cfg.pretrain:
        if not os.path.exists(cfg.pretrain):
            print("Pretrain weights not found: {}".format(cfg.pretrain))
        def if_exist(var):
            return os.path.exists(os.path.join(cfg.pretrain, var.name))
        fluid.io.load_vars(exe, cfg.pretrain, predicate=if_exist,main_program = train_program)

    build_strategy= fluid.BuildStrategy()
    build_strategy.memory_optimize = True
    

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
    
    base_flops = flops(train_program)
    pruner = Pruner()
    train_program = pruner.prune(
        train_program,
        fluid.global_scope(),
        params=pruned_params,
        ratios=pruned_ratios,
        place=place,
        only_graph=False)[0]
    prune_flops = flops(train_program)
    print('pruned_ratio__{:.6f}'.format((base_flops-prune_flops)/base_flops))
    for block in train_program.blocks:
        for param in block.all_parameters():
            print("parameter name: {}\tshape: {}".format(param.name, param.shape))
    '''

    def if_exist1(var):
            return os.path.exists(os.path.join(cfg.weights, var.name))
    fluid.io.load_vars(exe, cfg.weights, predicate=if_exist1,main_program = train_program)
    '''



    compile_program = fluid.compiler.CompiledProgram(
            train_program).with_data_parallel(
            loss_name=loss.name, build_strategy=build_strategy)

    random_sizes = [cfg.input_size]
    if cfg.random_shape:
        random_sizes = [32 * i for i in range(10, 20)]

    total_iter = cfg.max_iter - cfg.start_iter
    mixup_iter = total_iter - cfg.no_mixup_iter
    train_reader = reader.train(input_size, 
                                batch_size=cfg.batch_size, 
                                shuffle=True, 
                                total_iter=total_iter*devices_num, 
                                mixup_iter=mixup_iter*devices_num, 
                                random_sizes=random_sizes, 
                                use_multiprocessing=cfg.use_multiprocess)
    py_reader = model.py_reader
    py_reader.decorate_paddle_reader(train_reader)

    def save_model(postfix):
        model_path = os.path.join(cfg.model_save_dir, postfix)
        if os.path.isdir(model_path):
            shutil.rmtree(model_path)
        fluid.io.save_persistables(exe, model_path)

    fetch_list = [loss]

    py_reader.start()
    smoothed_loss = SmoothedValue()
    try:
        start_time = time.time()
        prev_start_time = start_time
        snapshot_loss = 0
        snapshot_time = 0
        for iter_id in range(cfg.start_iter, cfg.max_iter):
            prev_start_time = start_time
            start_time = time.time()
            losses = exe.run(compile_program, 
                             fetch_list=[v.name for v in fetch_list])
            smoothed_loss.add_value(np.mean(np.array(losses[0])))
            snapshot_loss += np.mean(np.array(losses[0]))
            snapshot_time += start_time - prev_start_time
            lr = np.array(fluid.global_scope().find_var('learning_rate')
                          .get_tensor())
            print("Iter {:d}, lr {:.6f}, loss {:.6f}, time {:.5f}".format(
                  iter_id, lr[0],
                  smoothed_loss.get_mean_value(), 
                  start_time - prev_start_time))
            sys.stdout.flush()
            if (iter_id + 1) % cfg.snapshot_iter == 0:
                save_model("model_iter{}".format(iter_id))
                print("Snapshot {} saved, average loss: {}, \
                      average time: {}".format(
                      iter_id + 1, 
                      snapshot_loss / float(cfg.snapshot_iter), 
                      snapshot_time / float(cfg.snapshot_iter)))
                snapshot_loss = 0
                snapshot_time = 0
    except fluid.core.EOFException:
        py_reader.reset()

    save_model('model_final')


if __name__ == '__main__':
    args = parse_args()
    print_arguments(args)
    train()
