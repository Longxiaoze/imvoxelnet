import torch
from torch import nn
from mmdet.core import multi_apply, reduce_mean
from mmdet.models.builder import HEADS, build_loss
from mmcv.cnn import Scale, bias_init_with_prob, normal_init

from mmdet3d.models.detectors.atlas import coordinates, get_origin
from mmdet3d.core.post_processing import box3d_multiclass_nms
from mmdet3d.core.bbox.structures import rotation_3d_in_axis

INF = 1e8


@HEADS.register_module()
class VoxelFCOS3DHeadV2(nn.Module):
    def __init__(self,
                 n_classes,
                 in_channels,
                 n_convs,
                 regress_ranges=((-1., .75), (.75, 1.5), (1.5, INF)),
                 loss_centerness=dict(
                     type='CrossEntropyLoss',
                     use_sigmoid=True,
                     loss_weight=1.0),
                 loss_bbox=dict(type='IoU3DLoss', loss_weight=1.0),
                 loss_cls=dict(
                     type='FocalLoss',
                     use_sigmoid=True,
                     gamma=2.0,
                     alpha=0.25,
                     loss_weight=1.0),
                 train_cfg=None,
                 test_cfg=None):
        super().__init__()
        self.n_classes = n_classes
        self.in_channels = in_channels
        self.regress_ranges = regress_ranges
        self.loss_centerness = build_loss(loss_centerness)
        self.loss_bbox = build_loss(loss_bbox)
        self.loss_cls = build_loss(loss_cls)
        self.train_cfg = train_cfg
        self.test_cfg = test_cfg
        self._init_layers(n_convs)

    def _init_layers(self, n_convs):
        self.reg_convs = nn.Sequential(*[
            nn.Sequential(
                nn.Conv3d(self.in_channels, self.in_channels, 3, padding=1, bias=False),
                nn.BatchNorm3d(self.in_channels),
                nn.ReLU(inplace=True)
            ) for _ in range(n_convs)])
        self.cls_convs = nn.Sequential(*[
            nn.Sequential(
                nn.Conv3d(self.in_channels, self.in_channels, 3, padding=1, bias=False),
                nn.BatchNorm3d(self.in_channels),
                nn.ReLU(inplace=True)
            ) for _ in range(n_convs)])

        self.centerness_conv = nn.Conv3d(self.in_channels, 1, 3, padding=1, bias=False)
        self.reg_conv = nn.Conv3d(self.in_channels, 7, 3, padding=1, bias=False)
        self.cls_conv = nn.Conv3d(self.in_channels, self.n_classes, 3, padding=1)
        self.scales = nn.ModuleList([Scale(1.) for _ in self.regress_ranges])

    # Follow AnchorFreeHead.init_weights
    def init_weights(self):
        for layer in self.reg_convs.modules():
            if isinstance(layer, nn.Conv3d):
                normal_init(layer, std=.01)
        for layer in self.cls_convs.modules():
            if isinstance(layer, nn.Conv3d):
                normal_init(layer, std=.01)

        normal_init(self.centerness_conv, std=.01)
        normal_init(self.reg_conv, std=.01)
        normal_init(self.cls_conv, std=.01, bias=bias_init_with_prob(.01))

    def forward(self, x):
        return multi_apply(self.forward_single, x, self.scales)

    def forward_single(self, x, scale):
        reg = self.reg_convs(x)
        cls = self.cls_convs(x)
        reg_final = self.reg_conv(reg)
        reg_distance = torch.exp(scale(reg_final[:, :6]))
        reg_angle = reg_final[:, 6:7]
        return (
            self.centerness_conv(reg),
            torch.cat((reg_distance, reg_angle), dim=1),
            self.cls_conv(cls)
        )

    def forward_train(self, x, valid, img_metas, gt_bboxes, gt_labels):
        loss_inputs = self(x) + (valid, img_metas, gt_bboxes, gt_labels)
        losses = self.loss(*loss_inputs)
        return losses

    def loss(self,
             centernesses,
             bbox_preds,
             cls_scores,
             valid,
             img_metas,
             gt_bboxes,
             gt_labels):
        """
        Args:
            centernesses (list(Tensor)): Multi-level centernesses
                of shape (batch, 1, nx[i], ny[i], nz[i])
            bbox_preds (list(Tensor)): Multi-level xyz min and max distances
                of shape (batch, 6, nx[i], ny[i], nz[i])
            cls_scores (list(Tensor)): Multi-level class scores
                of shape (batch, n_classes, nx[i], ny[i], nz[i])
            img_metas (list[dict]): Meta information of each image
            gt_bboxes (list(BaseInstance3DBoxes)): Ground truth bboxes for each image
            gt_labels (list(Tensor)): Ground truth class labels for each image

        Returns:
            dict[str, Tensor]: A dictionary of loss components.
        """
        assert len(centernesses[0]) == len(bbox_preds[0]) == len(cls_scores[0]) == \
               len(valid) == len(img_metas) == len(gt_bboxes) == len(gt_labels)

        valids = []
        for x in centernesses:
            valids.append(nn.Upsample(size=x.shape[-3:], mode='trilinear')(valid).round().bool())

        loss_centerness, loss_bbox, loss_cls = [], [], []
        for i in range(len(img_metas)):
            img_loss_centerness, img_loss_bbox, img_loss_cls = self._loss_single(
                centernesses=[x[i] for x in centernesses],
                bbox_preds=[x[i] for x in bbox_preds],
                cls_scores=[x[i] for x in cls_scores],
                valids=[x[i] for x in valids],
                img_meta=img_metas[i],
                gt_bboxes=gt_bboxes[i],
                gt_labels=gt_labels[i]
            )
            loss_centerness.append(img_loss_centerness)
            loss_bbox.append(img_loss_bbox)
            loss_cls.append(img_loss_cls)
        return dict(
            loss_centerness=torch.mean(torch.stack(loss_centerness)),
            loss_bbox=torch.mean(torch.stack(loss_bbox)),
            loss_cls=torch.mean(torch.stack(loss_cls))
        )

    def _loss_single(self,
                     centernesses,
                     bbox_preds,
                     cls_scores,
                     valids,
                     img_meta,
                     gt_bboxes,
                     gt_labels):
        """
        Args:
            centernesses (list(Tensor)): Multi-level centernesses
                of shape (1, nx[i], ny[i], nz[i])
            bbox_preds (list(Tensor)): Multi-level xyz min and max distances
                of shape (6, nx[i], ny[i], nz[i])
            cls_scores (list(Tensor)): Multi-level class scores
                of shape (n_classes, nx[i], ny[i], nz[i])
            img_metas (list[dict]): Meta information
            gt_bboxes (BaseInstance3DBoxes): Ground truth bboxes
                of shape (n_boxes, 7)
            gt_labels (list(Tensor)): Ground truth class labels
                of shape (n_boxes,)

        Returns:
            tuple(Tensor): 3 losses
        """
        featmap_sizes = [featmap.size()[-3:] for featmap in centernesses]
        mlvl_points = self.get_points(
            featmap_sizes=featmap_sizes,
            voxel_size=self.train_cfg['voxel_size'],
            origin=img_meta['lidar2img']['origin'],
            device=gt_bboxes.device
        )
        labels, bbox_targets = self.get_targets(mlvl_points, gt_bboxes, gt_labels)
        flatten_cls_scores = [cls_score.permute(1, 2, 3, 0).reshape(-1, self.n_classes)
                              for cls_score in cls_scores]
        flatten_bbox_preds = [bbox_pred.permute(1, 2, 3, 0).reshape(-1, 7)
                              for bbox_pred in bbox_preds]
        flatten_centerness = [centerness.permute(1, 2, 3, 0).reshape(-1)
                              for centerness in centernesses]
        flatten_valids = [valid.permute(1, 2, 3, 0).reshape(-1)
                          for valid in valids]
        flatten_cls_scores = torch.cat(flatten_cls_scores)
        flatten_bbox_preds = torch.cat(flatten_bbox_preds)
        flatten_centerness = torch.cat(flatten_centerness)
        flatten_valids = torch.cat(flatten_valids)
        flatten_labels = labels.to(centernesses[0].device)
        flatten_bbox_targets = bbox_targets.to(centernesses[0].device)
        flatten_points = torch.cat(mlvl_points)

        # skip background
        pos_inds = torch.nonzero(torch.logical_and(
            flatten_labels < self.n_classes,
            flatten_valids
        )).reshape(-1)
        n_pos = torch.tensor(len(pos_inds), dtype=torch.float, device=centernesses[0].device)
        n_pos = max(reduce_mean(n_pos), 1.)
        loss_cls = self.loss_cls(flatten_cls_scores[flatten_valids], flatten_labels[flatten_valids], avg_factor=n_pos)
        pos_bbox_preds = flatten_bbox_preds[pos_inds]
        pos_centerness = flatten_centerness[pos_inds]

        if len(pos_inds) > 0:
            pos_bbox_targets = flatten_bbox_targets[pos_inds]
            pos_centerness_targets = self.centerness_target(pos_bbox_targets)
            pos_points = flatten_points[pos_inds].to(pos_bbox_preds.device)
            pos_decoded_bbox_preds = distance2rotated_bbox3d(pos_points, pos_bbox_preds)
            pos_decoded_target_preds = distance2rotated_bbox3d(pos_points, pos_bbox_targets)
            # centerness weighted iou loss
            loss_bbox = self.loss_bbox(
                pos_decoded_bbox_preds,
                pos_decoded_target_preds,
                weight=pos_centerness_targets,
                avg_factor=pos_centerness_targets.sum())
            loss_centerness = self.loss_centerness(
                pos_centerness, pos_centerness_targets, avg_factor=n_pos)
        else:
            loss_bbox = pos_bbox_preds.sum()
            loss_centerness = pos_centerness.sum()
        return loss_centerness, loss_bbox, loss_cls

    @torch.no_grad()
    def get_points(self, featmap_sizes, voxel_size, origin, device):
        mlvl_points = []
        for i, featmap_size in enumerate(featmap_sizes):
            scale_voxel_size = torch.tensor(voxel_size) * (2 ** i)
            base_points = coordinates(featmap_size, device).permute(1, 0)
            new_origin = get_origin(torch.tensor(featmap_size), scale_voxel_size, torch.tensor(origin))
            new_origin = new_origin.reshape(1, 3).to(device)
            points = base_points * scale_voxel_size.to(device) + new_origin
            mlvl_points.append(points)
        return mlvl_points

    @torch.no_grad()
    def get_targets(self, points, gt_bboxes, gt_labels):
        assert len(points) == len(self.regress_ranges)
        # expand regress ranges to align with points
        expanded_regress_ranges = [
            points[i].new_tensor(self.regress_ranges[i]).expand(len(points[i]), 2)
            for i in range(len(points))
        ]
        # concat all levels points and regress ranges
        regress_ranges = torch.cat(expanded_regress_ranges, dim=0)
        points = torch.cat(points, dim=0)

        # below is based on FCOSHead._get_target_single
        n_points = len(points)
        n_boxes = len(gt_bboxes)
        volumes = gt_bboxes.volume.to(points.device)
        volumes = volumes.expand(n_points, n_boxes).contiguous()
        regress_ranges = regress_ranges[:, None, :].expand(n_points, n_boxes, 2)
        gt_bboxes = torch.cat((gt_bboxes.gravity_center, gt_bboxes.tensor[:, 3:]), dim=1)
        gt_bboxes = gt_bboxes.to(points.device).expand(n_points, n_boxes, 7)
        points = points.unsqueeze(1).expand(n_points, n_boxes, 3)
        shift = torch.stack((
            points[..., 0] - gt_bboxes[..., 0],
            points[..., 1] - gt_bboxes[..., 1],
            points[..., 2] - gt_bboxes[..., 2]
        ), dim=-1).permute(1, 0, 2)
        shift = rotation_3d_in_axis(shift, -gt_bboxes[0, :, 6], axis=2).permute(1, 0, 2)
        points = gt_bboxes[..., :3] + shift
        dx_min = points[..., 0] - gt_bboxes[..., 0] + gt_bboxes[..., 3] / 2
        dx_max = gt_bboxes[..., 0] + gt_bboxes[..., 3] / 2 - points[..., 0]
        dy_min = points[..., 1] - gt_bboxes[..., 1] + gt_bboxes[..., 4] / 2
        dy_max = gt_bboxes[..., 1] + gt_bboxes[..., 4] / 2 - points[..., 1]
        dz_min = points[..., 2] - gt_bboxes[..., 2] + gt_bboxes[..., 5] / 2
        dz_max = gt_bboxes[..., 2] + gt_bboxes[..., 5] / 2 - points[..., 2]
        bbox_targets = torch.stack((dx_min, dx_max, dy_min, dy_max, dz_min, dz_max, gt_bboxes[..., 6]), dim=-1)

        # condition1: inside a gt bbox
        inside_gt_bbox_mask = bbox_targets[..., :6].min(-1)[0] > 0  # skip angle

        # condition2: limit the regression range for each location
        max_regress_distance = bbox_targets[..., :6].max(-1)[0]  # skip angle
        inside_regress_range = (
                (max_regress_distance >= regress_ranges[..., 0])
                & (max_regress_distance <= regress_ranges[..., 1]))

        # if there are still more than one objects for a location,
        # we choose the one with minimal area
        volumes[inside_gt_bbox_mask == 0] = INF
        volumes[inside_regress_range == 0] = INF
        min_area, min_area_inds = volumes.min(dim=1)

        labels = gt_labels[min_area_inds]
        labels[min_area == INF] = self.n_classes  # set as BG
        bbox_targets = bbox_targets[range(n_points), min_area_inds]

        return labels, bbox_targets

    def centerness_target(self, pos_bbox_targets):
        """Compute centerness targets.
        Args:
            pos_bbox_targets (Tensor): BBox targets of positive bboxes of shape
                (n_pos, 6)
        Returns:
            Tensor: Centerness target
        """
        # only calculate pos centerness targets, otherwise there may be nan
        x_dims = pos_bbox_targets[:, [0, 1]]
        y_dims = pos_bbox_targets[:, [2, 3]]
        z_dims = pos_bbox_targets[:, [4, 5]]
        centerness_targets = x_dims.min(dim=-1)[0] / x_dims.max(dim=-1)[0] * \
                             y_dims.min(dim=-1)[0] / y_dims.max(dim=-1)[0] * \
                             z_dims.min(dim=-1)[0] / z_dims.max(dim=-1)[0]
        return torch.sqrt(centerness_targets)

    def get_bboxes(self,
                   centernesses,
                   bbox_preds,
                   cls_scores,
                   img_metas):
        assert len(centernesses[0]) == len(bbox_preds[0]) == len(cls_scores[0]) \
               == len(img_metas)
        n_levels = len(centernesses)
        result_list = []
        for img_id in range(len(img_metas)):
            centerness_list = [
                centernesses[i][img_id].detach() for i in range(n_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(n_levels)
            ]
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(n_levels)
            ]
            det_bboxes_3d = self._get_bboxes_single(
                centerness_list, bbox_pred_list, cls_score_list, img_metas[img_id]
            )
            result_list.append(det_bboxes_3d)
        return result_list

    def _get_bboxes_single(self,
                           centernesses,
                           bbox_preds,
                           cls_scores,
                           img_meta):
        featmap_sizes = [featmap.size()[-3:] for featmap in centernesses]
        mlvl_points = self.get_points(
            featmap_sizes=featmap_sizes,
            voxel_size=self.test_cfg['voxel_size'],
            origin=img_meta['lidar2img']['origin'],
            device=centernesses[0].device
        )
        mlvl_bboxes, mlvl_scores = [], []
        for centerness, bbox_pred, cls_score, points in zip(
            centernesses, bbox_preds, cls_scores, mlvl_points
        ):
            centerness = centerness.permute(1, 2, 3, 0).reshape(-1).sigmoid()
            bbox_pred = bbox_pred.permute(1, 2, 3, 0).reshape(-1, 7)
            scores = cls_score.permute(1, 2, 3, 0).reshape(-1, self.n_classes).sigmoid()
            scores = scores * centerness[:, None]
            max_scores, _ = scores.max(dim=1)

            if len(scores) > self.test_cfg.nms_pre > 0:
                _, ids = max_scores.topk(self.test_cfg.nms_pre)
                bbox_pred = bbox_pred[ids]
                scores = scores[ids]
                points = points[ids]

            bboxes = distance2rotated_bbox3d(points, bbox_pred)
            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)

        bboxes = torch.cat(mlvl_bboxes)
        scores = torch.cat(mlvl_scores)
        # Add a dummy background class to the end. Nms needs to be fixed in the future.
        padding = scores.new_zeros(scores.shape[0], 1)
        scores = torch.cat([scores, padding], dim=1)
        bboxes_for_nms = torch.stack((
            bboxes[:, 0] - bboxes[:, 3] / 2,
            bboxes[:, 1] - bboxes[:, 4] / 2,
            bboxes[:, 0] + bboxes[:, 3] / 2,
            bboxes[:, 1] + bboxes[:, 4] / 2,
            bboxes[:, 6]
        ), dim=1)
        bboxes, scores, labels, _ = box3d_multiclass_nms(
            mlvl_bboxes=bboxes,
            mlvl_bboxes_for_nms=bboxes_for_nms,
            mlvl_scores=scores,
            score_thr=self.test_cfg.score_thr,
            max_num=self.test_cfg.nms_pre,
            cfg=self.test_cfg,
        )
        bboxes = img_meta['box_type_3d'](bboxes, origin=(.5, .5, .5))
        return bboxes, scores, labels


def distance2rotated_bbox3d(points, distance):
    if distance.shape[0] == 0:
        return distance

    shift = torch.stack((
        (distance[:, 1] - distance[:, 0]) / 2,
        (distance[:, 3] - distance[:, 2]) / 2,
        (distance[:, 5] - distance[:, 4]) / 2
    ), dim=-1).view(-1, 1, 3)
    shift = rotation_3d_in_axis(shift, distance[:, 6], axis=2)[:, 0, :]
    center = points + shift
    size = torch.stack((
        distance[:, 0] + distance[:, 1],
        distance[:, 2] + distance[:, 3],
        distance[:, 4] + distance[:, 5]
    ), dim=-1)
    return torch.cat((center, size, distance[:, 6:7]), dim=-1)
