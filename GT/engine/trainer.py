# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import os
import time
import logging
import torch
from torch.nn.parallel import DistributedDataParallel
from fvcore.nn.precise_bn import get_bn_modules
import numpy as np
from collections import OrderedDict,defaultdict

import detectron2.utils.comm as comm
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.engine import DefaultTrainer, SimpleTrainer, TrainerBase
from detectron2.engine.train_loop import AMPTrainer
from detectron2.utils.events import EventStorage
from detectron2.evaluation import verify_results, DatasetEvaluators
# from detectron2.evaluation import COCOEvaluator, verify_results, DatasetEvaluators

from detectron2.data.dataset_mapper import DatasetMapper
from detectron2.engine import hooks
from detectron2.structures.boxes import Boxes
from detectron2.structures.instances import Instances
from detectron2.utils.env import TORCH_VERSION
from detectron2.data import MetadataCatalog

from GT.data.build import (
    build_detection_semisup_train_loader,
    build_detection_test_loader,
    build_detection_semisup_train_loader_two_crops,
)
from GT.data.dataset_mapper import DatasetMapperTwoCropSeparate
from GT.engine.hooks import LossEvalHook
from GT.modeling.meta_arch.ts_ensemble import EnsembleTSModel
from GT.checkpoint.detection_checkpoint import DetectionTSCheckpointer
from GT.solver.build import build_lr_scheduler
from GT.evaluation import PascalVOCDetectionEvaluator, COCOEvaluator
from GT.rcm import RCM

from .probe import OpenMatchTrainerProbe
import copy


# ===GTSFOD===
# import pprint
# from datetime import datetime

# def log_to_file(var, filename, name="Variable"):
#     os.makedirs("logs", exist_ok=True)
#     full_path = os.path.join("logs", filename)
#     with open(full_path, "a") as f:
#         f.write(f"\n--- {name} @ {datetime.now()} ---\n")
#         pp = pprint.PrettyPrinter(indent=2, stream=f)
#         pp.pprint(var)
        
        
import pandas as pd

def extract_bmp_roih(file_path, file_info_list):
    df = pd.read_csv(file_path)
    df = df[df["ImageName"].isin(file_info_list.keys())].reset_index(drop=True)
    df["x1"] = df["X"]
    df["y1"] = df["Y"]
    df["x2"] = df["X"] + df["W"]
    df["y2"] = df["Y"] + df["H"]

    grouped = defaultdict(list)
    for _, row in df.iterrows():
        grouped[row["ImageName"]].append(row)

    proposals_bmp_roih_unsup_k = []

    for image_name in file_info_list.keys():
        if image_name not in grouped:
            continue  # skip images that have no proposals

        records_df = pd.DataFrame(grouped[image_name])

        boxes_tensor = torch.tensor(
            records_df[["x1", "y1", "x2", "y2"]].values,
            dtype=torch.float32,
            device="cuda:0"
        )
        boxes = Boxes(boxes_tensor)

        scores = torch.tensor(
            records_df["MaxConfidence"].values,
            dtype=torch.float32,
            device="cuda:0"
        )
        full_scores = torch.stack([1.0 - scores, scores], dim=1)
        pred_classes = torch.zeros(len(records_df), dtype=torch.int64, device="cuda:0")

        height, width = file_info_list[image_name]
        instance = Instances(image_size=(height, width))
        instance.pred_boxes = boxes
        instance.scores = scores
        instance.full_scores = full_scores
        instance.pred_classes = pred_classes

        proposals_bmp_roih_unsup_k.append(instance)

    return proposals_bmp_roih_unsup_k

# Supervised-only Trainer
class BaselineTrainer(DefaultTrainer):
    def __init__(self, cfg):
        """
        Args:
            cfg (CfgNode):
        Use the custom checkpointer, which loads other backbone models
        with matching heuristics.
        """
        cfg = DefaultTrainer.auto_scale_workers(cfg, comm.get_world_size())
        model = self.build_model(cfg)
        optimizer = self.build_optimizer(cfg, model)
        data_loader = self.build_train_loader(cfg)

        if comm.get_world_size() > 1:
            model = DistributedDataParallel(
                model, device_ids=[comm.get_local_rank()], broadcast_buffers=False
            )

        TrainerBase.__init__(self)
        self._trainer = (AMPTrainer if cfg.SOLVER.AMP.ENABLED else SimpleTrainer)(
            model, data_loader, optimizer
        )

        self.scheduler = self.build_lr_scheduler(cfg, optimizer)
        self.checkpointer = DetectionCheckpointer(
            model,
            cfg.OUTPUT_DIR,
            optimizer=optimizer,
            scheduler=self.scheduler,
        )
        self.start_iter = 0
        self.max_iter = cfg.SOLVER.MAX_ITER
        self.cfg = cfg
        self.register_hooks(self.build_hooks())

    def resume_or_load(self, resume=True):
        """
        If `resume==True` and `cfg.OUTPUT_DIR` contains the last checkpoint (defined by
        a `last_checkpoint` file), resume from the file. Resuming means loading all
        available states (eg. optimizer and scheduler) and update iteration counter
        from the checkpoint. ``cfg.MODEL.WEIGHTS`` will not be used.
        Otherwise, this is considered as an independent training. The method will load model
        weights from the file `cfg.MODEL.WEIGHTS` (but will not load other states) and start
        from iteration 0.
        Args:
            resume (bool): whether to do resume or not
        """
        checkpoint = self.checkpointer.resume_or_load(
            self.cfg.MODEL.WEIGHTS, resume=resume
        )
        if resume and self.checkpointer.has_checkpoint():
            self.start_iter = checkpoint.get("iteration", -1) + 1
            # The checkpoint stores the training iteration that just finished, thus we start
            # at the next iteration (or iter zero if there's no checkpoint).
        if isinstance(self.model, DistributedDataParallel):
            # broadcast loaded data/model from the first rank, because other
            # machines may not have access to the checkpoint file
            if TORCH_VERSION >= (1, 7):
                self.model._sync_params_and_buffers()
            self.start_iter = comm.all_gather(self.start_iter)[0]

    def train_loop(self, start_iter: int, max_iter: int):
        """
        Args:
            start_iter, max_iter (int): See docs above
        """
        logger = logging.getLogger(__name__)
        logger.info("Starting training from iteration {}".format(start_iter))

        self.iter = self.start_iter = start_iter
        self.max_iter = max_iter

        with EventStorage(start_iter) as self.storage:
            try:
                self.before_train()
                for self.iter in range(start_iter, max_iter):
                    self.before_step()
                    self.run_step()
                    self.after_step()
            except Exception:
                logger.exception("Exception during training:")
                raise
            finally:
                self.after_train()

    def run_step(self):
        self._trainer.iter = self.iter

        assert self.model.training, "[SimpleTrainer] model was changed to eval mode!"
        start = time.perf_counter()

        data = next(self._trainer._data_loader_iter)
        data_time = time.perf_counter() - start

        record_dict, _, _, _ = self.model(data, branch="supervised")

        num_gt_bbox = 0.0
        for element in data:
            num_gt_bbox += len(element["instances"])
        num_gt_bbox = num_gt_bbox / len(data)
        record_dict["bbox_num/gt_bboxes"] = num_gt_bbox

        loss_dict = {}
        for key in record_dict.keys():
            if key[:4] == "loss" and key[-3:] != "val":
                loss_dict[key] = record_dict[key]

        losses = sum(loss_dict.values())

        metrics_dict = record_dict
        metrics_dict["data_time"] = data_time
        self._write_metrics(metrics_dict)

        self.optimizer.zero_grad()
        losses.backward()
        self.optimizer.step()

    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
        evaluator_list = []
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type

        if evaluator_type == "coco":
            evaluator_list.append(COCOEvaluator(
                dataset_name, output_dir=output_folder))
        elif evaluator_type == "pascal_voc":
            return PascalVOCDetectionEvaluator(dataset_name)
        elif evaluator_type == "pascal_voc_water":
            return PascalVOCDetectionEvaluator(dataset_name, target_classnames=["bicycle", "bird", "car", "cat", "dog", "person"])
        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        elif len(evaluator_list) == 1:
            return evaluator_list[0]

        return DatasetEvaluators(evaluator_list)

    @classmethod
    def build_train_loader(cls, cfg):
        return build_detection_semisup_train_loader(cfg, mapper=None)

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        """
        Returns:
            iterable
        """
        return build_detection_test_loader(cfg, dataset_name)

    def build_hooks(self):
        """
        Build a list of default hooks, including timing, evaluation,
        checkpointing, lr scheduling, precise BN, writing events.

        Returns:
            list[HookBase]:
        """
        cfg = self.cfg.clone()
        cfg.defrost()
        cfg.DATALOADER.NUM_WORKERS = 0

        ret = [
            hooks.IterationTimer(),
            hooks.LRScheduler(self.optimizer, self.scheduler),
            hooks.PreciseBN(
                cfg.TEST.EVAL_PERIOD,
                self.model,
                self.build_train_loader(cfg),
                cfg.TEST.PRECISE_BN.NUM_ITER,
            )
            if cfg.TEST.PRECISE_BN.ENABLED and get_bn_modules(self.model)
            else None,
        ]

        if comm.is_main_process():
            ret.append(
                hooks.PeriodicCheckpointer(
                    self.checkpointer, cfg.SOLVER.CHECKPOINT_PERIOD
                )
            )

        def test_and_save_results():
            self._last_eval_results = self.test(self.cfg, self.model)
            return self._last_eval_results

        ret.append(hooks.EvalHook(cfg.TEST.EVAL_PERIOD, test_and_save_results))

        if comm.is_main_process():
            ret.append(hooks.PeriodicWriter(self.build_writers(), period=20))
        return ret

    def _write_metrics(self, metrics_dict: dict):
        """
        Args:
            metrics_dict (dict): dict of scalar metrics
        """
        metrics_dict = {
            k: v.detach().cpu().item() if isinstance(v, torch.Tensor) else float(v)
            for k, v in metrics_dict.items()
        }
        # gather metrics among all workers for logging
        # This assumes we do DDP-style training, which is currently the only
        # supported method in detectron2.
        all_metrics_dict = comm.gather(metrics_dict)

        if comm.is_main_process():
            if "data_time" in all_metrics_dict[0]:
                data_time = np.max([x.pop("data_time")
                                   for x in all_metrics_dict])
                self.storage.put_scalar("data_time", data_time)

            metrics_dict = {
                k: np.mean([x[k] for x in all_metrics_dict])
                for k in all_metrics_dict[0].keys()
            }

            loss_dict = {}
            for key in metrics_dict.keys():
                if key[:4] == "loss":
                    loss_dict[key] = metrics_dict[key]

            total_losses_reduced = sum(loss for loss in loss_dict.values())

            self.storage.put_scalar("total_loss", total_losses_reduced)
            if len(metrics_dict) > 1:
                self.storage.put_scalars(**metrics_dict)


# CAT Trainer
class CATTrainer(DefaultTrainer):
    def __init__(self, cfg):
        """
        Args:
            cfg (CfgNode):
        Use the custom checkpointer, which loads other backbone models
        with matching heuristics.
        """
        cfg = DefaultTrainer.auto_scale_workers(cfg, comm.get_world_size())
        data_loader = self.build_train_loader(cfg)

        # create an student model
        model = self.build_model(cfg)
        optimizer = self.build_optimizer(cfg, model)

        # create an teacher model
        model_teacher = self.build_model(cfg)
        self.model_teacher = model_teacher

        # For training, wrap with DDP. But don't need this for inference.
        if comm.get_world_size() > 1:
            model = DistributedDataParallel(
                model, device_ids=[comm.get_local_rank()], broadcast_buffers=False
            )

        TrainerBase.__init__(self)
        self._trainer = (AMPTrainer if cfg.SOLVER.AMP.ENABLED else SimpleTrainer)(
            model, data_loader, optimizer
        )
        self.scheduler = self.build_lr_scheduler(cfg, optimizer)

        # Ensemble teacher and student model is for model saving and loading
        ensem_ts_model = EnsembleTSModel(model_teacher, model)

        self.checkpointer = DetectionTSCheckpointer(
            ensem_ts_model,
            cfg.OUTPUT_DIR,
            optimizer=optimizer,
            scheduler=self.scheduler,
        )
        self.start_iter = 0
        self.max_iter = cfg.SOLVER.MAX_ITER
        self.cfg = cfg

        self.probe = OpenMatchTrainerProbe(cfg)
        self.rcm = RCM(cfg.MODEL.ROI_HEADS.NUM_CLASSES, 50, cfg.OUTPUT_DIR, blocked_classes=self.cfg.BLOCKED_CLASSES,mix_ratio = self.cfg.MIX_RATIO, cfg = self.cfg)
        self.register_hooks(self.build_hooks()) 
        self.top_eval_ap = 0.0
        self.top_eval_ap_stu = 0.0

    def resume_or_load(self, resume=True):
        """
        If `resume==True` and `cfg.OUTPUT_DIR` contains the last checkpoint (defined by
        a `last_checkpoint` file), resume from the file. Resuming means loading all
        available states (eg. optimizer and scheduler) and update iteration counter
        from the checkpoint. ``cfg.MODEL.WEIGHTS`` will not be used.
        Otherwise, this is considered as an independent training. The method will load model
        weights from the file `cfg.MODEL.WEIGHTS` (but will not load other states) and start
        from iteration 0.
        Args:
            resume (bool): whether to do resume or not
        """
        checkpoint = self.checkpointer.resume_or_load(
            self.cfg.MODEL.WEIGHTS, resume=resume
        )
        if resume and self.checkpointer.has_checkpoint():
            self.start_iter = checkpoint.get("iteration", -1) + 1
            # The checkpoint stores the training iteration that just finished, thus we start
            # at the next iteration (or iter zero if there's no checkpoint).
        if isinstance(self.model, DistributedDataParallel):
            # broadcast loaded data/model from the first rank, because other
            # machines may not have access to the checkpoint file
            if TORCH_VERSION >= (1, 7):
                self.model._sync_params_and_buffers()
            self.start_iter = comm.all_gather(self.start_iter)[0]

    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference")
        evaluator_list = []
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type

        if evaluator_type == "coco":
            evaluator_list.append(COCOEvaluator(
                dataset_name, output_dir=output_folder))
        elif evaluator_type == "pascal_voc":
            return PascalVOCDetectionEvaluator(dataset_name)
        elif evaluator_type == "pascal_voc_water":
            return PascalVOCDetectionEvaluator(dataset_name, target_classnames=["bicycle", "bird", "car", "cat", "dog", "person"])
        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        elif len(evaluator_list) == 1:
            return evaluator_list[0]

        return DatasetEvaluators(evaluator_list)

    @classmethod
    def build_train_loader(cls, cfg):
        mapper = DatasetMapperTwoCropSeparate(cfg, True)
        return build_detection_semisup_train_loader_two_crops(cfg, mapper)

    @classmethod
    def build_lr_scheduler(cls, cfg, optimizer):
        return build_lr_scheduler(cfg, optimizer)

    def train(self):
        self.train_loop(self.start_iter, self.max_iter)
        if hasattr(self, "_last_eval_results") and comm.is_main_process():
            verify_results(self.cfg, self._last_eval_results)
            return self._last_eval_results

    def train_loop(self, start_iter: int, max_iter: int):
        logger = logging.getLogger(__name__)
        logger.info("Starting training from iteration {}".format(start_iter))

        self.iter = self.start_iter = start_iter
        self.max_iter = max_iter

        with EventStorage(start_iter) as self.storage:
            try:
                self.before_train()

                for self.iter in range(start_iter, max_iter):
                    self.before_step()
                    self.run_step_full_semisup()
                    self.after_step()
            except Exception:
                logger.exception("Exception during training:")
                raise
            finally:
                self.after_train()

    # =====================================================
    # ================== Pseduo-labeling ==================
    # =====================================================
    def threshold_bbox(self, proposal_bbox_inst, thres=0.7, proposal_type="roih"):
        if proposal_type == "rpn":
            valid_map = proposal_bbox_inst.objectness_logits > thres

            # create instances containing boxes and gt_classes
            image_shape = proposal_bbox_inst.image_size
            new_proposal_inst = Instances(image_shape)

            # create box
            new_bbox_loc = proposal_bbox_inst.proposal_boxes.tensor[valid_map, :]
            new_boxes = Boxes(new_bbox_loc)

            # add boxes to instances
            new_proposal_inst.gt_boxes = new_boxes
            new_proposal_inst.objectness_logits = proposal_bbox_inst.objectness_logits[
                valid_map
            ]
        elif proposal_type == "roih":
            valid_map = proposal_bbox_inst.scores > thres

            full_scores = proposal_bbox_inst.full_scores.clone()

            # create instances containing boxes and gt_classes
            image_shape = proposal_bbox_inst.image_size
            new_proposal_inst = Instances(image_shape)
            
            # create box
            new_bbox_loc = proposal_bbox_inst.pred_boxes.tensor[valid_map, :]
            new_boxes = Boxes(new_bbox_loc)

            # add boxes to instances
            new_proposal_inst.gt_boxes = new_boxes
            new_proposal_inst.gt_classes = proposal_bbox_inst.pred_classes[valid_map]
            new_proposal_inst.scores = proposal_bbox_inst.scores[valid_map]
            new_proposal_inst.full_scores = full_scores[valid_map]

        return new_proposal_inst
    
    

    def process_pseudo_label(
        self, proposals_rpn_unsup_k, cur_threshold, proposal_type, psedo_label_method=""
    ):
        list_instances = []
        num_proposal_output = 0.0
        for proposal_bbox_inst in proposals_rpn_unsup_k:
            # thresholding
            if psedo_label_method == "thresholding":
                proposal_bbox_inst = self.threshold_bbox(
                    proposal_bbox_inst, thres=cur_threshold, proposal_type=proposal_type
                )
            else:
                raise ValueError("Unkown pseudo label boxes methods")
            num_proposal_output += len(proposal_bbox_inst)
            list_instances.append(proposal_bbox_inst)
        num_proposal_output = num_proposal_output / len(proposals_rpn_unsup_k)
        return list_instances, num_proposal_output

    def remove_label(self, label_data):
        for label_datum in label_data:
            if "instances" in label_datum.keys():
                del label_datum["instances"]
        return label_data

    def add_label(self, unlabled_data, label):
        for unlabel_datum, lab_inst in zip(unlabled_data, label):
            unlabel_datum["instances"] = lab_inst
        return unlabled_data
    
    def get_label(self, label_data):
        label_list = []
        for label_datum in label_data:
            if "instances" in label_datum.keys():
                label_list.append(copy.deepcopy(label_datum["instances"]))
        
        return label_list
    
    # def get_label_test(self, label_data):
    #     label_list = []
    #     for label_datum in label_data:
    #         if "instances" in label_datum.keys():
    #             label_list.append(label_datum["instances"])

    # =====================================================
    # =================== Training Flow ===================
    # =====================================================

    def run_step_full_semisup(self):
        self._trainer.iter = self.iter
        assert self.model.training, "[UBTeacherTrainer] model was changed to eval mode!"
        start = time.perf_counter()
        data = next(self._trainer._data_loader_iter)
        # data_q and data_k from different augmentations (q:strong, k:weak)
        # label_strong, label_weak, unlabed_strong, unlabled_weak
        label_data_q, label_data_k, unlabel_data_q, unlabel_data_k = data
        data_time = time.perf_counter() - start
        
        if self.cfg.MIXUP:
            with torch.no_grad():
                self.rcm.save_crops(label_data_k) 
                label_data_k = self.rcm.mix_crop_new(label_data_k)
                label_data_q = self.rcm.add_labels(label_data_q)
        else:
            with torch.no_grad():
                self.rcm.save_crops(label_data_k) 
                label_data_k = self.rcm.add_labels(label_data_k)
                label_data_q = self.rcm.add_labels(label_data_q)
            
        if self.iter == self.cfg.SEMISUPNET.BURN_UP_STEP:
            # update copy the the whole model
            self._update_teacher_model(keep_rate=0.00)
            # self.model.build_discriminator()

        elif (
            self.iter - self.cfg.SEMISUPNET.BURN_UP_STEP
        ) % self.cfg.SEMISUPNET.TEACHER_UPDATE_ITER == 0: # and self.iter >= self.cfg.SEMISUPNET.BURN_UP_STEP:
            self._update_teacher_model(
                keep_rate=self.cfg.SEMISUPNET.EMA_KEEP_RATE)
            
        expert_labels_path=self.cfg.DATASETS.EXPERT_PATH
        
        if not os.path.exists(expert_labels_path):
            raise FileNotFoundError(f"File not found: {expert_labels_path}")
        # burn-in stage (supervised training with labeled data)
        if self.iter < self.cfg.SEMISUPNET.BURN_UP_STEP:

            if self.cfg.CLS_LOSS:
                class_info = self.rcm.class_info
            else:
                class_info = None
            # input both strong and weak supervised data into model
            label_data_k.extend(label_data_q)
            record_dict, _, _, proposals_predictions = self.model(
                label_data_k, branch="supervised",class_info = class_info)
                
            with torch.no_grad():
                self.rcm.get_matches(proposals_predictions,self.iter)

            # weight losses
            loss_dict = {}
            for key in record_dict.keys():
                if key[:4] == "loss":
                    loss_dict[key] = record_dict[key] * 1
            losses = sum(loss_dict.values())

        else:

            record_dict = {}

            ######################## For probe #################################
            # import pdb; pdb. set_trace() 
            gt_unlabel_k = self.get_label(unlabel_data_k)
            # gt_unlabel_q = self.get_label_test(unlabel_data_q)
            

            #  0. remove unlabeled data labels
            unlabel_data_q = self.remove_label(unlabel_data_q)
            unlabel_data_k = self.remove_label(unlabel_data_k)
            # log_to_file(unlabel_data_q, "unlabel_data_q.txt", "Unlabeled Data Q (no labels)")
            # log_to_file(unlabel_data_k, "unlabel_data_k.txt", "Unlabeled Data K (no labels)")
            
            # ===GTSFOD=== extracting the filename and height and weigth 
            file_info_map = {}
            for item in unlabel_data_k:
                key = os.path.basename(item["file_name"])
                file_info_map[key] = (item["height"], item["width"])

            #  1. generate the pseudo-label using teacher model
            with torch.no_grad():
                (
                    _,
                    proposals_rpn_unsup_k,
                    proposals_roih_unsup_k,
                    _,
                ) = self.model_teacher(unlabel_data_k, branch="unsup_data_weak")
                
            # ===GTSFOD=== importing the pseudo-label from biomedparse  
            
            proposals_bmp_roih_unsup_k = extract_bmp_roih(expert_labels_path, file_info_map)
            
            # log_to_file(proposals_rpn_unsup_k, "proposals_rpn_unsup_k.txt", "Proposals RPN (unsup_k)")
            # log_to_file(proposals_roih_unsup_k, "proposals_roih_unsup_k.txt", "Proposals ROIH (unsup_k)")
            # log_to_file(proposals_bmp_roih_unsup_k, "proposals_bmp_roih_unsup_k.txt", "Proposals ROIH BMP (unsup_k)")

            #  2. Pseudo-labeling
            cur_threshold = self.cfg.SEMISUPNET.BBOX_THRESHOLD

            joint_proposal_dict = {}
            joint_proposal_dict["proposals_rpn"] = proposals_rpn_unsup_k
            #Process pseudo labels and thresholding
            (
                pesudo_proposals_rpn_unsup_k,
                nun_pseudo_bbox_rpn,
            ) = self.process_pseudo_label(
                proposals_rpn_unsup_k, cur_threshold, "rpn", "thresholding"
            )

            joint_proposal_dict["proposals_pseudo_rpn"] = pesudo_proposals_rpn_unsup_k
            # Pseudo_labeling for ROI head (bbox location/objectness)
            pesudo_proposals_roih_unsup_k, _ = self.process_pseudo_label(
                proposals_roih_unsup_k, cur_threshold, "roih", "thresholding"
            )
            joint_proposal_dict["proposals_pseudo_roih"] = pesudo_proposals_roih_unsup_k
            
            # ===GTSFOD=== bmp thresholding
            pesudo_proposals_bmp_roih_unsup_k, _ = self.process_pseudo_label(
                proposals_bmp_roih_unsup_k, cur_threshold, "roih", "thresholding"
            )
            joint_proposal_dict["proposals_pseudo_bmp_roih"] = pesudo_proposals_bmp_roih_unsup_k

            # log_to_file(pesudo_proposals_rpn_unsup_k, "pseudo_rpn_labels.txt", "Pseudo Proposals RPN")
            # log_to_file(pesudo_proposals_roih_unsup_k, "pseudo_roih_labels.txt", "Pseudo Proposals ROIH")
            # log_to_file(pesudo_proposals_bmp_roih_unsup_k, "pseudo_bmp_roih_labels.txt", "Pseudo BMP Proposals ROIH")
            # 3. add pseudo-label to unlabeled data

            unlabel_data_q = self.add_label(
                unlabel_data_q, joint_proposal_dict["proposals_pseudo_roih"]
            )
            unlabel_data_k = self.add_label(
                unlabel_data_k, joint_proposal_dict["proposals_pseudo_roih"]
            )
            # ===GTSFOD=== add BMP pseudo-label to unlabeled data
            unlabel_bmp_data_k = self.add_label(
                unlabel_data_k, joint_proposal_dict["proposals_pseudo_bmp_roih"]
            )
            unlabel_bmp_data_q = self.add_label(
                unlabel_data_q, joint_proposal_dict["proposals_pseudo_bmp_roih"]
            )

            all_label_data = label_data_q + label_data_k
            all_unlabel_data = unlabel_data_q
            all_bmp_unlabel_data = unlabel_bmp_data_q
            
            # log_to_file(unlabel_data_q, "labeled_unlabel_data_q.txt", "Labeled Unlabeled Data Q")
            # log_to_file(unlabel_data_k, "labeled_unlabel_data_k.txt", "Labeled Unlabeled Data K")
            # log_to_file(unlabel_bmp_data_q, "labeled_unlabel_bmp_data_k.txt", "Labeled Unlabeled BMP Data K")

            if self.cfg.MIXUP:
                with torch.no_grad():
                    self.rcm.save_crops_target(unlabel_data_k)
                    all_unlabel_data = self.rcm.mix_crop_new(all_unlabel_data, True)
                    all_bmp_unlabel_data = self.rcm.mix_crop_new(all_bmp_unlabel_data, True)
            else:
                with torch.no_grad():
                    self.rcm.save_crops_target(unlabel_data_k)
                    all_unlabel_data = self.rcm.add_labels(all_unlabel_data)
                    all_bmp_unlabel_data = self.rcm.add_labels(all_bmp_unlabel_data)

            if self.cfg.CLS_LOSS:
                class_info = self.rcm.class_info
            else:
                class_info = None

            # 4. input both strongly and weakly augmented labeled data into student model
            record_all_label_data, _, _, proposals_predictions = self.model(
                all_label_data, branch="supervised", class_info = class_info
            )
            record_dict.update(record_all_label_data)

            with torch.no_grad():
                self.rcm.get_matches(proposals_predictions,self.iter)
    


            if self.cfg.CLS_LOSS:
                if self.iter < self.cfg.SEMISUPNET.BURN_UP_STEP+400:
                    class_info = self.rcm.class_info
                else:
                    class_info = self.rcm.target_class_info
            else:
                class_info = None
            

            # 5. input strongly augmented unlabeled data into model
            record_all_unlabel_data, _, _, proposals_predictions_unlabel = self.model(
                all_unlabel_data, branch="supervised_target", class_info = class_info
            )
            new_record_all_unlabel_data = {}
            for key in record_all_unlabel_data.keys():
                new_record_all_unlabel_data[key + "_pseudo"] = record_all_unlabel_data[
                    key
                ]
            record_dict.update(new_record_all_unlabel_data)
            
            
            # ===GTSFOD=== input strongly augmented BMP unlabeled data into model
            record_all_bmp_unlabel_data, _, _, proposals_bmp_predictions_unlabel = self.model(
                all_bmp_unlabel_data, branch="supervised_target", class_info = class_info
            )
            new_record_all_bmp_unlabel_data = {}
            for key in record_all_bmp_unlabel_data.keys():
                new_record_all_bmp_unlabel_data[key + "_pseudo"] = record_all_bmp_unlabel_data[
                    key
                ]
                
            # ===GTSFOD=== creating new record dict
            bmp_record_dict = {}
            bmp_record_dict.update(new_record_all_bmp_unlabel_data)
            
            
            
            # log_to_file(record_all_unlabel_data, "record_all_unlabel_data.txt", "Model Record: Unsupervised")
            # log_to_file(proposals_predictions_unlabel, "proposals_predictions_unlabel.txt", "Proposals Predictions (Unsupervised)")
            
            # log_to_file(record_all_bmp_unlabel_data, "record_all_bmp_unlabel_data.txt", "Model Record: BMP Unsupervised")
            # log_to_file(proposals_bmp_predictions_unlabel, "proposals_predictions_unlabel.txt", "Proposals Predictions (Unsupervised)")

            with torch.no_grad():
                self.rcm.get_matches(proposals_predictions_unlabel,self.iter, True)

            # 6. input weakly labeled data (source) and weakly unlabeled data (target) to student model
            # give sign to the target data

            for i_index in range(len(unlabel_data_k)):
                for k, v in unlabel_data_k[i_index].items():
                    label_data_k[i_index][k + "_unlabeled"] = v

            all_domain_data = label_data_k
            record_all_domain_data, _, _, _ = self.model(all_domain_data, branch="domain")
            record_dict.update(record_all_domain_data)

            # log_to_file(record_all_domain_data, "record_all_domain_data.txt", "Model Record: Domain Input")
            # weight losses
            loss_dict = {}
            for key in record_dict.keys():
                if key.startswith("loss"):
                    if key == "loss_rpn_loc_pseudo" or key == "loss_box_reg_pseudo":
                        # pseudo bbox regression <- 0 already set to 0 in loss weights, used for bbox paste
                        if self.cfg.TARGET_BBOX:
                            loss_dict[key] = record_dict[key] * self.cfg.SEMISUPNET.UNSUP_LOSS_WEIGHT #0
                        else:
                            loss_dict[key] = record_dict[key] * 0
                    elif key[-6:] == "pseudo":  # unsupervised loss
                        loss_dict[key] = (
                            record_dict[key] *
                            self.cfg.SEMISUPNET.UNSUP_LOSS_WEIGHT
                        )
                    elif (
                        key == "loss_D_img_s" or key == "loss_D_img_t"
                    ):  # set weight for discriminator
                        # import pdb
                        # pdb.set_trace()
                        loss_dict[key] = record_dict[key] * self.cfg.SEMISUPNET.DIS_LOSS_WEIGHT #Need to modify defaults and yaml
                    else:  # supervised loss
                        loss_dict[key] = record_dict[key] * 1
            
            # ===GTSFOD=== Compute additional loss from bmp_record_dict 
            bmp_loss_dict = {}
            for key in bmp_record_dict.keys():
                if key.startswith("loss"):
                    if key == "loss_rpn_loc_pseudo" or key == "loss_box_reg_pseudo":
                        if self.cfg.TARGET_BBOX:
                            bmp_loss_dict[key] = bmp_record_dict[key] * self.cfg.SEMISUPNET.UNSUP_LOSS_WEIGHT
                        else:
                            bmp_loss_dict[key] = bmp_record_dict[key] * 0
                    elif key[-6:] == "pseudo":  # unsupervised loss
                        bmp_loss_dict[key] = bmp_record_dict[key] * self.cfg.SEMISUPNET.UNSUP_LOSS_WEIGHT
                    elif key in ["loss_D_img_s", "loss_D_img_t"]:  # discriminator loss
                        bmp_loss_dict[key] = bmp_record_dict[key] * self.cfg.SEMISUPNET.DIS_LOSS_WEIGHT
                    else:  # supervised loss
                        bmp_loss_dict[key] = bmp_record_dict[key] * 1
                        
            # ===GTSFOD=== Combine the losses 
            losses = sum(loss_dict.values()) + sum(bmp_loss_dict.values())
            
            # log_to_file(record_dict, "record_dict.txt", "Record dict")
            # log_to_file(bmp_record_dict, "bmp_record_dict.txt", "BMP Record Dict")

        metrics_dict = record_dict
        metrics_dict["data_time"] = data_time
        self._write_metrics(metrics_dict)
        # log_to_file(loss_dict, "loss_dict.txt", "Loss Dictionary")
        # log_to_file(metrics_dict, "metrics_dict.txt", "Metrics Dictionary")

        self.optimizer.zero_grad()
        losses.backward()
        #self.clip_gradient(self.model, 10.)
        self.optimizer.step()

    def _write_metrics(self, metrics_dict: dict):
        metrics_dict = {
            k: v.detach().cpu().item() if isinstance(v, torch.Tensor) else float(v)
            for k, v in metrics_dict.items()
        }

        # gather metrics among all workers for logging
        # This assumes we do DDP-style training, which is currently the only
        # supported method in detectron2.
        all_metrics_dict = comm.gather(metrics_dict)
        # all_hg_dict = comm.gather(hg_dict)

        if comm.is_main_process():
            if "data_time" in all_metrics_dict[0]:
                # data_time among workers can have high variance. The actual latency
                # caused by data_time is the maximum among workers.
                data_time = np.max([x.pop("data_time")
                                   for x in all_metrics_dict])
                self.storage.put_scalar("data_time", data_time)

            # average the rest metrics
            metrics_dict = {
                k: np.mean([x[k] for x in all_metrics_dict])
                for k in all_metrics_dict[0].keys()
            }

            # append the list
            loss_dict = {}
            for key in metrics_dict.keys():
                if key[:4] == "loss":
                    loss_dict[key] = metrics_dict[key]

            total_losses_reduced = sum(loss for loss in loss_dict.values())

            self.storage.put_scalar("total_loss", total_losses_reduced)
            if len(metrics_dict) > 1:
                self.storage.put_scalars(**metrics_dict)

    @torch.no_grad()
    def _update_teacher_model(self, keep_rate=0.9996):
        if comm.get_world_size() > 1:
            student_model_dict = {
                key[7:]: value for key, value in self.model.state_dict().items()
            }
        else:
            student_model_dict = self.model.state_dict()

        new_teacher_dict = OrderedDict()
        for key, value in self.model_teacher.state_dict().items():
            if key in student_model_dict.keys():
                new_teacher_dict[key] = (
                    student_model_dict[key] *
                    (1 - keep_rate) + value * keep_rate
                )
            else:
                raise Exception("{} is not found in student model".format(key))

        self.model_teacher.load_state_dict(new_teacher_dict)

    def _update_student_model(self, type='full_copy', keep_rate=0.9996, rand_rate = 1.0):
        teacher_model_dict = self.model_teacher.state_dict()

        new_student_dict = OrderedDict()
        for key, value in self.model.state_dict().items():
            if comm.get_world_size() > 1:
                teacher_key = key[7:]
            else:
                teacher_key = key

            if teacher_key in teacher_model_dict.keys():
                
                if type == 'full_copy':
                    new_student_dict[key] = (
                        teacher_model_dict[teacher_key] *
                        (1 - keep_rate) + value * keep_rate
                    )
                elif type == 'random_copy':
                    if np.random.rand() < rand_rate:
                        new_student_dict[key] = (
                            teacher_model_dict[teacher_key] *
                            (1 - keep_rate) + value * keep_rate
                        )
                    else:
                        new_student_dict[key] = value
            else:
                raise Exception("{} is not found in student model".format(key))

        self.model.load_state_dict(new_student_dict)



    @torch.no_grad()
    def _copy_main_model(self):
        # initialize all parameters
        if comm.get_world_size() > 1:
            rename_model_dict = {
                key[7:]: value for key, value in self.model.state_dict().items()
            }
            self.model_teacher.load_state_dict(rename_model_dict)
        else:
            self.model_teacher.load_state_dict(self.model.state_dict())

    @classmethod
    def build_test_loader(cls, cfg, dataset_name):
        return build_detection_test_loader(cfg, dataset_name)

    def build_hooks(self):
        cfg = self.cfg.clone()
        cfg.defrost()
        cfg.DATALOADER.NUM_WORKERS = 0  # save some memory and time for PreciseBN

        ret = [
            hooks.IterationTimer(),
            hooks.LRScheduler(self.optimizer, self.scheduler),
            hooks.PreciseBN(
                # Run at the same freq as (but before) evaluation.
                cfg.TEST.EVAL_PERIOD,
                self.model,
                # Build a new data loader to not affect training
                self.build_train_loader(cfg),
                cfg.TEST.PRECISE_BN.NUM_ITER,
            )
            if cfg.TEST.PRECISE_BN.ENABLED and get_bn_modules(self.model)
            else None,
        ]

        # Do PreciseBN before checkpointer, because it updates the model and need to
        # be saved by checkpointer.
        # This is not always the best: if checkpointing has a different frequency,
        # some checkpoints may have more precise statistics than others.
        if comm.is_main_process():
            ret.append(
                hooks.PeriodicCheckpointer(
                    self.checkpointer, cfg.SOLVER.CHECKPOINT_PERIOD
                )
            )

        def test_and_save_results_student():
            self._last_eval_results_student = self.test(self.cfg, self.model)
            _last_eval_results_student = {
                k + "_student": self._last_eval_results_student[k]
                for k in self._last_eval_results_student.keys()
            }
            return _last_eval_results_student

        def test_and_save_results_teacher():
            self._last_eval_results_teacher = self.test(
                self.cfg, self.model_teacher)
            
            return self._last_eval_results_teacher

        ret.append(hooks.EvalHook(cfg.TEST.EVAL_PERIOD,
                   test_and_save_results_student))
        ret.append(hooks.EvalHook(cfg.TEST.EVAL_PERIOD,
                   test_and_save_results_teacher))

        if comm.is_main_process():
            # run writers in the end, so that evaluation metrics are written
            ret.append(hooks.PeriodicWriter(self.build_writers(), period=20))
        return ret

    def clip_gradient(self, model, clip_norm):
        """Computes a gradient clipping coefficient based on gradient norm."""
        totalnorm = 0
        for p in model.parameters():
            if p.requires_grad and p.grad is not None:
                try:
                    modulenorm = p.grad.norm()
                except:
                    continue
                totalnorm += modulenorm ** 2
        totalnorm = torch.sqrt(totalnorm).item()
        norm = (clip_norm / max(totalnorm, clip_norm))
        for p in model.parameters():
            if p.requires_grad and p.grad is not None:
                p.grad.mul_(norm)