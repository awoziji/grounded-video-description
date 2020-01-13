# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import *
import misc.utils as utils
from misc.resnet import resnet
from misc.vgg16 import vgg16
from misc.CaptionModelBU import CaptionModel
from misc.bbox_transform import bbox_overlaps_batch
from torch.autograd import Variable
import math

import numpy as np
import random
import pdb
import pickle

class AttModel(CaptionModel):
    def __init__(self, opt):
        super(AttModel, self).__init__()
        self.image_crop_size = opt.image_crop_size
        self.vocab_size = opt.vocab_size
        self.detect_size = opt.detect_size # number of object classes
        self.input_encoding_size = opt.input_encoding_size
        self.rnn_size = opt.rnn_size
        self.num_layers = opt.num_layers
        self.drop_prob_lm = opt.drop_prob_lm
        self.seq_length = opt.seq_length
        self.fc_feat_size = opt.fc_feat_size
        self.att_feat_size = opt.att_feat_size
        self.att_hid_size = opt.att_hid_size
        self.finetune_cnn = opt.finetune_cnn
        self.cbs = opt.cbs
        self.cbs_mode = opt.cbs_mode
        self.seq_per_img = opt.seq_per_img
        self.itod = opt.itod
        self.att_input_mode = opt.att_input_mode
        self.transfer_mode = opt.transfer_mode
        self.enable_BUTD = opt.enable_BUTD
        self.w_grd = opt.w_grd
        self.w_cls = opt.w_cls
        self.unk_idx = int(opt.wtoi['UNK'])

        if opt.region_attn_mode == 'add':
            self.alpha_net = nn.Linear(self.att_hid_size, 1)
        elif opt.region_attn_mode == 'cat':
            self.alpha_net = nn.Linear(self.att_hid_size*2, 1)

        if opt.cnn_backend == 'vgg16':
            self.stride = 16
        else:
            self.stride = 32

        self.att_size = int(opt.image_crop_size / self.stride)
        self.tiny_value = 1e-8

        if self.enable_BUTD:
            assert(self.att_input_mode == 'region')
            self.pool_feat_size = self.att_feat_size
        else:
            self.pool_feat_size = self.att_feat_size+300+self.detect_size+1

        self.ss_prob = 0.0   # Schedule sampling probability
        self.min_value = -1e8
        opt.beta = 1
        self.beta = opt.beta
        if opt.cnn_backend == 'res101':
            self.cnn = resnet(opt, _num_layers=101, _fixed_block=opt.fixed_block, pretrained=True)
        elif opt.cnn_backend == 'res152':
            self.cnn = resnet(opt, _num_layers=152, _fixed_block=opt.fixed_block, pretrained=True)
        elif opt.cnn_backend == 'vgg16':
            self.cnn = vgg16(opt, pretrained=True)

        self.loc_fc = nn.Sequential(nn.Linear(4, 300),
                                    nn.ReLU(),
                                    nn.Dropout())

        self.embed = nn.Sequential(nn.Embedding(self.vocab_size,
                                self.input_encoding_size), # det is 1-indexed
                                nn.ReLU(),
                                nn.Dropout(self.drop_prob_lm))

        if self.transfer_mode == 'cls':
            self.vis_encoding_size = 2048
        elif self.transfer_mode == 'both':
            self.vis_encoding_size = 2348
        elif self.transfer_mode == 'glove':
            self.vis_encoding_size = 300
        else:
            raise NotImplementedError

        self.vis_embed = nn.Sequential(nn.Embedding(self.detect_size+1,
                                self.vis_encoding_size), # det is 1-indexed
                                nn.ReLU(),
                                nn.Dropout(self.drop_prob_lm)
                                )

        self.fc_embed = nn.Sequential(nn.Linear(self.fc_feat_size, self.rnn_size),
                                    nn.ReLU(),
                                    nn.Dropout(self.drop_prob_lm))

        self.att_embed = nn.Sequential(nn.Linear(self.att_feat_size, self.rnn_size),
                                    nn.ReLU(),
                                    nn.Dropout(self.drop_prob_lm))

        self.pool_embed = nn.Sequential(nn.Linear(self.pool_feat_size, self.rnn_size),
                                    nn.ReLU(),
                                    nn.Dropout(self.drop_prob_lm))

        self.ctx2att = nn.Linear(self.rnn_size, self.att_hid_size)
        self.ctx2pool = nn.Linear(self.rnn_size, self.att_hid_size)

        self.logit = nn.Linear(self.rnn_size, self.vocab_size)

        if opt.obj_interact:
            from misc.transformer import Transformer
            n_layers = 2
            n_heads = 6
            attn_drop = 0.2
            self.obj_interact = Transformer(self.rnn_size, 0, 0,
                d_hidden=int(self.rnn_size/2),
                n_layers=n_layers,
                n_heads=n_heads,
                drop_ratio=attn_drop)

        self.ctx2pool_grd = nn.Sequential(nn.Linear(self.att_feat_size, self.vis_encoding_size), # fc7 layer
                                          nn.ReLU(),
                                          nn.Dropout(self.drop_prob_lm)
                                          ) # fc7 layer in detectron

        #self.grid_size = 1
        self.critLM = utils.LMCriterion(opt)
        self.critBN = utils.BNCriterion(opt)
        self.critFG = utils.FGCriterion(opt)

        # initialize the glove weight for the labels.
        # self.det_fc[0].weight.data.copy_(opt.glove_vg_cls)
        # for p in self.det_fc[0].parameters(): p.requires_grad=False

        # self.embed[0].weight.data.copy_(torch.cat((opt.glove_w, opt.glove_clss)))
        # for p in self.embed[0].parameters(): p.requires_grad=False

        # weights transfer for fc7 layer
        with open('data/detectron_weights/fc7_w.pkl', 'rb') as f:
            fc7_w = torch.from_numpy(pickle.load(f))
        with open('data/detectron_weights/fc7_b.pkl', 'rb') as f:
            fc7_b = torch.from_numpy(pickle.load(f))
        self.ctx2pool_grd[0].weight[:self.att_feat_size].data.copy_(fc7_w)
        self.ctx2pool_grd[0].bias[:self.att_feat_size].data.copy_(fc7_b)

        if self.transfer_mode in ('cls', 'both'):
            # find nearest neighbour class for transfer
            with open('data/detectron_weights/cls_score_w.pkl', 'rb') as f:
                cls_score_w = torch.from_numpy(pickle.load(f)) # 1601x2048
            with open('data/detectron_weights/cls_score_b.pkl', 'rb') as f:
                cls_score_b = torch.from_numpy(pickle.load(f)) # 1601x2048

            assert(len(opt.itod)+1 == opt.glove_clss.size(0)) # index 0 is background
            assert(len(opt.vg_cls) == opt.glove_vg_cls.size(0)) # index 0 is background

            sim_matrix = torch.matmul(opt.glove_vg_cls/torch.norm(opt.glove_vg_cls, dim=1).unsqueeze(1), (opt.glove_clss/torch.norm(opt.glove_clss, dim=1).unsqueeze(1)).transpose(1,0))

            max_sim, matched_cls = torch.max(sim_matrix, dim=0)

            vis_classifiers = opt.glove_clss.new(self.detect_size+1, cls_score_w.size(1)).fill_(0)
            self.vis_classifiers_bias = nn.Parameter(opt.glove_clss.new(self.detect_size+1).fill_(0))
            vis_classifiers[0] = cls_score_w[0] # background
            self.vis_classifiers_bias[0].data.copy_(cls_score_b[0])
            for i in range(1, self.detect_size+1):
                vis_classifiers[i] = cls_score_w[matched_cls[i]]
                self.vis_classifiers_bias[i].data.copy_(cls_score_b[matched_cls[i]])
                if max_sim[i].item() < 0.9:
                    print('index: {}, similarity: {:.2}, {}, {}'.format(i, max_sim[i].item(), \
                        opt.itod[i], opt.vg_cls[matched_cls[i]]))

            if self.transfer_mode == 'cls':
                self.vis_embed[0].weight.data.copy_(vis_classifiers)
            else:
                self.vis_embed[0].weight.data.copy_(torch.cat((vis_classifiers, opt.glove_clss), dim=1))
        elif self.transfer_mode == 'glove':
            self.vis_embed[0].weight.data.copy_(opt.glove_clss)
        else:
            raise NotImplementedError

        # for p in self.ctx2pool_grd.parameters(): p.requires_grad=False
        # for p in self.vis_embed[0].parameters(): p.requires_grad=False

        if opt.enable_visdom:
            import visdom
            self.vis = visdom.Visdom(server=opt.visdom_server, env='vis-'+opt.id)


    def forward(self, img, seq, gt_seq, num, ppls, gt_boxes, mask_boxes, ppls_feat, opt, eval_opt = {}):
        if opt == 'MLE':
            return self._forward(img, seq, gt_seq, ppls, gt_boxes, mask_boxes, num, ppls_feat)
        elif opt == 'GRD':
            return self._forward(img, seq, gt_seq, ppls, gt_boxes, mask_boxes, num, ppls_feat, True)
        elif opt == 'sample':
            seq, seqLogprobs, att2, sim_mat = self._sample(img, ppls, num, ppls_feat, eval_opt)
            return Variable(seq), Variable(att2), Variable(sim_mat)

    def init_hidden(self, bsz):
        weight = next(self.parameters()).data
        return (Variable(weight.new(self.num_layers, bsz, self.rnn_size).zero_()),
                Variable(weight.new(self.num_layers, bsz, self.rnn_size).zero_()))

    def _grounder(self, xt, att_feats, mask, bias=None):
        # xt - B, seq_cnt, enc_size
        # att_feats - B, rois_num, enc_size
        # mask - B, rois_num

        B, S, _ = xt.size()
        _, R, _ = att_feats.size()

        if hasattr(self, 'alpha_net'):
            # Additive attention for grounding
            if self.alpha_net.weight.size(1) == self.att_hid_size:
                dot = xt.unsqueeze(2) + att_feats.unsqueeze(1)
            else:
                dot = torch.cat((xt.unsqueeze(2).expand(B, S, R, self.att_hid_size),
                                 att_feats.unsqueeze(1).expand(B, S, R, self.att_hid_size)), 3)
            dot = F.tanh(dot)
            dot = self.alpha_net(dot).squeeze(-1)
        else:
            # Dot-product attention for grounding
            assert(xt.size(-1) == att_feats.size(-1))
            dot = torch.matmul(xt, att_feats.permute(0,2,1).contiguous()) # B, seq_cnt, rois_num

        if bias is not None:
            assert(bias.numel() == dot.numel())
            dot += bias

        expanded_mask = mask.unsqueeze(1).expand_as(dot)
        dot.masked_fill_(expanded_mask, self.min_value)

        return dot


    def _forward(self, img, input_seq, gt_seq, ppls, gt_boxes, mask_boxes, num, ppls_feat, eval_obj_ground=False):

        seq = gt_seq[:, :self.seq_per_img, :].clone().view(-1, gt_seq.size(2)) # choose the first 5
        seq = torch.cat((Variable(seq.data.new(seq.size(0), 1).fill_(0)), seq), 1)
        input_seq = input_seq.view(-1, input_seq.size(2), input_seq.size(3)) # B*self.seq_per_img, self.seq_length+1, 5
        input_seq_update = input_seq.data.clone()

        batch_size = img.size(0) # B
        seq_batch_size = seq.size(0) # B*self.seq_per_img
        rois_num = ppls.size(1) # max_num_proposal of the batch
        # constructing the mask.

        pnt_mask = ppls.data.new(batch_size, rois_num+1).byte().fill_(1) # +1 is for the dummy indicator, has no impact on the region attention mask
        for i in range(batch_size):
            pnt_mask[i,:num.data[i,1]+1] = 0
        pnt_mask = Variable(pnt_mask)

        state = self.init_hidden(seq_batch_size) # self.num_layers, B*self.seq_per_img, self.rnn_size
        rnn_output = []
        roi_labels = [] # store which proposal match the gt box
        att2_weights = []
        h_att_output = []
        max_grd_output = []

        if self.finetune_cnn:
            conv_feats, fc_feats = self.cnn(img)
        else:
            # with torch.no_grad():
            conv_feats, fc_feats = self.cnn(Variable(img.data, volatile=True))
            conv_feats = Variable(conv_feats.data)
            fc_feats = Variable(fc_feats.data)

        # pooling the conv_feats
        pool_feats = ppls_feat
        pool_feats = self.ctx2pool_grd(pool_feats)
        g_pool_feats = pool_feats

        # calculate the overlaps between the rois/rois and rois/gt_bbox.
        overlaps = utils.bbox_overlaps(ppls.data, gt_boxes.data)

        # visual words embedding
        vis_word = Variable(torch.Tensor(range(0, self.detect_size+1)).type(input_seq.type()))
        vis_word_embed = self.vis_embed(vis_word)
        assert(vis_word_embed.size(0) == self.detect_size+1)
        p_vis_word_embed = vis_word_embed.view(1, self.detect_size+1, self.vis_encoding_size) \
            .expand(batch_size, self.detect_size+1, self.vis_encoding_size).contiguous()
        if hasattr(self, 'vis_classifiers_bias'):
            bias = self.vis_classifiers_bias.type(p_vis_word_embed.type()) \
                                         .view(1,-1,1).expand(p_vis_word_embed.size(0), \
                                         p_vis_word_embed.size(1), g_pool_feats.size(1))
        else:
            bias = None

        sim_target = utils.sim_mat_target(overlaps, gt_boxes[:,:,4].data) # B, num_box, num_rois
        sim_mask = (sim_target > 0)
        sim_mat_static = self._grounder(p_vis_word_embed, g_pool_feats, pnt_mask[:,1:], bias)
        sim_mat_static_update = sim_mat_static.view(batch_size, 1, self.detect_size+1, rois_num) \
            .expand(batch_size, self.seq_per_img, self.detect_size+1, rois_num).contiguous() \
            .view(seq_batch_size, self.detect_size+1, rois_num)
        sim_mat_static = F.softmax(sim_mat_static, dim=1)
        masked_sim = torch.gather(sim_mat_static, 1, sim_target)
        masked_sim = torch.masked_select(masked_sim, sim_mask)
        cls_loss = F.binary_cross_entropy(masked_sim, masked_sim.new(masked_sim.size()).fill_(1))

        if eval_obj_ground:
            sim_target_masked = torch.masked_select(sim_target, sim_mask)
            sim_mat_masked = torch.masked_select(torch.max(sim_mat_static, dim=1)[1].unsqueeze(1).expand_as(sim_target), sim_mask)
            matches = (sim_target_masked == sim_mat_masked)
            cls_pred = torch.stack((sim_target_masked, matches.long()), dim=1).data

        if not self.enable_BUTD:
            loc_input = ppls.data.new(batch_size, rois_num, 4)
            loc_input[:,:,:4] = ppls.data[:,:,:4] / self.image_crop_size
            loc_feats = self.loc_fc(Variable(loc_input)) # encode the locations

            label_feat = sim_mat_static.permute(0,2,1).contiguous()

            pool_feats = torch.cat((F.layer_norm(pool_feats, [pool_feats.size(-1)]), F.layer_norm( \
                loc_feats, [loc_feats.size(-1)]), F.layer_norm(label_feat, [label_feat.size(-1)])), 2)

        # replicate the feature to map the seq size.
        fc_feats = fc_feats.view(batch_size, 1, self.fc_feat_size)\
                .expand(batch_size, self.seq_per_img, self.fc_feat_size)\
                .contiguous().view(-1, self.fc_feat_size)
        pool_feats = pool_feats.view(batch_size, 1, rois_num, self.pool_feat_size)\
                .expand(batch_size, self.seq_per_img, rois_num, self.pool_feat_size)\
                .contiguous().view(-1, rois_num, self.pool_feat_size)
        g_pool_feats = g_pool_feats.view(batch_size, 1, rois_num, self.vis_encoding_size) \
                .expand(batch_size, self.seq_per_img, rois_num, self.vis_encoding_size) \
                .contiguous().view(-1, rois_num, self.vis_encoding_size)
        pnt_mask = pnt_mask.view(batch_size, 1, rois_num+1).expand(batch_size, self.seq_per_img, rois_num+1)\
                .contiguous().view(-1, rois_num+1)
        overlaps = overlaps.view(batch_size, 1, rois_num, overlaps.size(2)) \
                .expand(batch_size, self.seq_per_img, rois_num, overlaps.size(2)) \
                .contiguous().view(-1, rois_num, overlaps.size(2))

        # embed fc and att feats
        fc_feats = self.fc_embed(fc_feats)
        pool_feats = self.pool_embed(pool_feats)
        # object region interactions
        if hasattr(self, 'obj_interact'):
            pool_feats = self.obj_interact(pool_feats)

        # Project the attention feats first to reduce memory and computation comsumptions.
        p_pool_feats = self.ctx2pool(pool_feats) # same here

        if self.att_input_mode in ('both', 'featmap'):
            conv_feats = conv_feats.view(batch_size, self.att_feat_size, -1).transpose(1,2).contiguous() # B, self.att_size*self.att_size, self.att_feat_size
            conv_feats = conv_feats.view(batch_size, 1, self.att_size*self.att_size, self.att_feat_size)\
                .expand(batch_size, self.seq_per_img, self.att_size*self.att_size, self.att_feat_size)\
                .contiguous().view(-1, self.att_size*self.att_size, self.att_feat_size)
            conv_feats = self.att_embed(conv_feats)
            p_conv_feats = self.ctx2att(conv_feats) # self.rnn_size (1024) -> self.att_hid_size (512)
        else:
            # dummy
            conv_feats = pool_feats.new(1,1).fill_(0)
            p_conv_feats = pool_feats.new(1,1).fill_(0)


        for i in range(self.seq_length):
            it = seq[:, i].clone()

            # break if all the sequences end
            if i >= 1 and seq[:, i].data.sum() == 0:
                break

            if not eval_obj_ground:
                roi_label = utils.bbox_target(mask_boxes[:,:,:,i+1], overlaps, input_seq[:,i+1], \
                    input_seq_update[:,i+1], self.vocab_size) # roi_label if for the target seq
                roi_labels.append(roi_label.view(seq_batch_size, -1))

            xt = self.embed(it)

            output, state, att2_weight, att_h, max_grd_val, grd_val = self.core(xt, fc_feats, conv_feats,
                p_conv_feats, pool_feats, p_pool_feats, pnt_mask, pnt_mask, state, sim_mat_static_update)

            att2_weights.append(att2_weight)
            h_att_output.append(att_h) # the hidden state of attention LSTM
            rnn_output.append(output)
            max_grd_output.append(max_grd_val)

        seq_cnt = len(rnn_output)
        rnn_output = torch.cat([_.unsqueeze(1) for _ in rnn_output], 1) # seq_batch_size, seq_cnt, vocab
        h_att_output = torch.cat([_.unsqueeze(1) for _ in h_att_output], 1)
        att2_weights = torch.cat([_.unsqueeze(1) for _ in att2_weights], 1) # seq_batch_size, seq_cnt, att_size
        max_grd_output = torch.cat([_.unsqueeze(1) for _ in max_grd_output], 1)
        if not eval_obj_ground:
            roi_labels = torch.cat([_.unsqueeze(1) for _ in roi_labels], 1)

        decoded = F.log_softmax(self.beta * self.logit(rnn_output), dim=2) # text word prob
        decoded  = decoded.view((seq_cnt)*seq_batch_size, -1)

        # object grounding
        h_att_all = h_att_output # hidden states from the Attention LSTM
        xt_clamp = torch.clamp(input_seq[:, 1:seq_cnt+1, 0].clone()-self.vocab_size, min=0)
        xt_all = self.vis_embed(xt_clamp)

        if hasattr(self, 'vis_classifiers_bias'):
            bias = self.vis_classifiers_bias[xt_clamp].type(xt_all.type()) \
                                            .unsqueeze(2).expand(seq_batch_size, seq_cnt, rois_num)
        else:
            bias = 0
        ground_weights = self._grounder(xt_all,
                                        g_pool_feats, pnt_mask[:,1:], bias+att2_weights[:, :, :]
                                        ) # pnt_mask[:,1:] is attention mask

        if not eval_obj_ground:
            lm_loss, att2_loss, ground_loss = self.critLM(decoded, att2_weights, ground_weights, \
                seq[:, 1:seq_cnt+1].clone(), roi_labels[:, :seq_cnt, :].clone(), input_seq[:, 1:seq_cnt+1, 0].clone())
            return lm_loss.unsqueeze(0), att2_loss.unsqueeze(0), ground_loss.unsqueeze(0), cls_loss.unsqueeze(0)
        else:
            return cls_pred, torch.max(F.softmax(att2_weights, dim=2), dim=2)[1], \
                torch.max(F.softmax(ground_weights, dim=2), dim=2)[1]


    def _sample(self, img, ppls, num, ppls_feat, opt={}):
        sample_max = opt.get('sample_max', 1)
        beam_size = opt.get('beam_size', 1)
        temperature = opt.get('temperature', 1.0)
        inference_mode = opt.get('inference_mode', True)

        # assert(beam_size == 1) # only support greedy search now
        assert(self.cbs == False)

        batch_size = img.size(0)
        rois_num = ppls.size(1)

        if beam_size > 1 or self.cbs:
            return self._sample_beam(img, ppls, num, ppls_feat, opt)

        if self.finetune_cnn:
            conv_feats, fc_feats = self.cnn(img)
        else:
            # with torch.no_grad():
            conv_feats, fc_feats = self.cnn(Variable(img.data, volatile=True))
            conv_feats = Variable(conv_feats.data)
            fc_feats = Variable(fc_feats.data)

        pool_feats = ppls_feat
        pool_feats = self.ctx2pool_grd(pool_feats)
        g_pool_feats = pool_feats

        # constructing the mask.
        pnt_mask = ppls.data.new(batch_size, rois_num+1).byte().fill_(1)
        for i in range(batch_size):
            pnt_mask[i,:num.data[i,1]+1] = 0
        pnt_mask = Variable(pnt_mask)
        att_mask = pnt_mask.clone()

        # visual words embedding
        vis_word = Variable(torch.Tensor(range(0, self.detect_size+1)).type(fc_feats.type())).long()
        vis_word_embed = self.vis_embed(vis_word)
        assert(vis_word_embed.size(0) == self.detect_size+1)
        p_vis_word_embed = vis_word_embed.view(1, self.detect_size+1, self.vis_encoding_size) \
            .expand(batch_size, self.detect_size+1, self.vis_encoding_size).contiguous()
        if hasattr(self, 'vis_classifiers_bias'):
            bias = self.vis_classifiers_bias.type(p_vis_word_embed.type()) \
                                         .view(1,-1,1).expand(p_vis_word_embed.size(0), \
                                         p_vis_word_embed.size(1), g_pool_feats.size(1))
        else:
            bias = None
        sim_mat_static = self._grounder(p_vis_word_embed, g_pool_feats, pnt_mask[:,1:], bias)
        sim_mat_static_update = sim_mat_static
        sim_mat_static = F.softmax(sim_mat_static, dim=1)

        if not self.enable_BUTD:
            loc_input = ppls.data.new(batch_size, rois_num, 4)
            loc_input[:,:,:4] = ppls.data[:,:,:4] / self.image_crop_size
            loc_feats = self.loc_fc(Variable(loc_input))

            label_feat = sim_mat_static.permute(0,2,1).contiguous()

            pool_feats = torch.cat((F.layer_norm(pool_feats, [pool_feats.size(-1)]), F.layer_norm( \
                loc_feats, [loc_feats.size(-1)]), F.layer_norm(label_feat, [label_feat.size(-1)])), 2)

        # embed fc and att feats
        pool_feats = self.pool_embed(pool_feats)
        fc_feats = self.fc_embed(fc_feats)
        # object region interactions
        if hasattr(self, 'obj_interact'):
            pool_feats = self.obj_interact(pool_feats)

        # Project the attention feats first to reduce memory and computation comsumptions.
        p_pool_feats = self.ctx2pool(pool_feats)

        if self.att_input_mode in ('both', 'featmap'):
            conv_feats = conv_feats.view(batch_size, self.att_feat_size, -1).transpose(1,2).contiguous()
            conv_feats = self.att_embed(conv_feats)
            p_conv_feats = self.ctx2att(conv_feats)
        else:
            conv_feats = pool_feats.new(1,1).fill_(0)
            p_conv_feats = pool_feats.new(1,1).fill_(0)

        vis_offset = (torch.arange(0, batch_size)*rois_num).view(batch_size).type_as(ppls.data).long()
        roi_offset = (torch.arange(0, batch_size)*(rois_num+1)).view(batch_size).type_as(ppls.data).long()

        state = self.init_hidden(batch_size)

        seq = []
        seqLogprobs = []
        att2_weights = []

        for t in range(self.seq_length + 1):
            if t == 0: # input <bos>
                it = fc_feats.data.new(batch_size).long().zero_()
            elif sample_max:
                sampleLogprobs_tmp, it_tmp = torch.topk(logprobs.data, 2, dim=1)
                unk_mask = (it_tmp[:,0] != self.unk_idx) # mask on non-unk
                sampleLogprobs = unk_mask.float()*sampleLogprobs_tmp[:,0] + (1-unk_mask.float())*sampleLogprobs_tmp[:,1]
                it = unk_mask.long()*it_tmp[:,0] + (1-unk_mask.long())*it_tmp[:,1]
                it = it.view(-1).long()
            else:
                if temperature == 1.0:
                    prob_prev = torch.exp(logprobs.data) # fetch prev distribution: shape Nx(M+1)
                else:
                    # scale logprobs by temperature
                    prob_prev = torch.exp(torch.div(logprobs.data, temperature))
                it = torch.multinomial(prob_prev, 1)
                sampleLogprobs = logprobs.gather(1, Variable(it)) # gather the logprobs at sampled positions
                it = it.view(-1).long() # and flatten indices for downstream processing

            xt = self.embed(Variable(it))
            if t >= 1:
                seq.append(it) #seq[t] the input of t+2 time step
                seqLogprobs.append(sampleLogprobs.view(-1))

            rnn_output, state, att2_weight, att_h, _, _ = self.core(xt, fc_feats, conv_feats,
                p_conv_feats, pool_feats, p_pool_feats, att_mask, pnt_mask, state, sim_mat_static_update)

            decoded = F.log_softmax(self.beta * self.logit(rnn_output), dim=1)

            logprobs = decoded
            att2_weights.append(att2_weight)

        seq = torch.cat([_.unsqueeze(1) for _ in seq], 1)
        seqLogprobs = torch.cat([_.unsqueeze(1) for _ in seqLogprobs], 1)
        att2_weights = torch.cat([_.unsqueeze(1) for _ in att2_weights], 1)

        return seq, seqLogprobs, att2_weights, sim_mat_static

    def _sample_beam(self, img, ppls, num, ppls_feat, opt={}):

        batch_size = ppls.size(0)
        rois_num = ppls.size(1)

        beam_size = opt.get('beam_size', 10)

        if self.finetune_cnn:
            conv_feats, fc_feats = self.cnn(img)
        else:
            # with torch.no_grad():
            conv_feats, fc_feats = self.cnn(Variable(img.data, volatile=True))
            conv_feats = Variable(conv_feats.data)
            fc_feats = Variable(fc_feats.data)

        pool_feats = ppls_feat
        pool_feats = self.ctx2pool_grd(pool_feats)
        g_pool_feats = pool_feats

        # constructing the mask.
        pnt_mask = ppls.data.new(batch_size, rois_num+1).byte().fill_(1)
        for i in range(batch_size):
            pnt_mask[i,:num.data[i,1]+1] = 0
        pnt_mask = Variable(pnt_mask)

        # visual words embedding
        vis_word = Variable(torch.Tensor(range(0, self.detect_size+1)).type(fc_feats.type())).long()
        vis_word_embed = self.vis_embed(vis_word)
        assert(vis_word_embed.size(0) == self.detect_size+1)
        p_vis_word_embed = vis_word_embed.view(1, self.detect_size+1, self.vis_encoding_size) \
            .expand(batch_size, self.detect_size+1, self.vis_encoding_size).contiguous()
        if hasattr(self, 'vis_classifiers_bias'):
            bias = self.vis_classifiers_bias.type(p_vis_word_embed.type()) \
                                         .view(1,-1,1).expand(p_vis_word_embed.size(0), \
                                         p_vis_word_embed.size(1), g_pool_feats.size(1))
        else:
            bias = None
        sim_mat_static = self._grounder(p_vis_word_embed, g_pool_feats, pnt_mask[:,1:], bias)
        sim_mat_static_update = sim_mat_static
        sim_mat_static = F.softmax(sim_mat_static, dim=1)

        if not self.enable_BUTD:
            loc_input = ppls.data.new(batch_size, rois_num, 4)
            loc_input[:,:,:4] = ppls.data[:,:,:4] / self.image_crop_size
            loc_feats = self.loc_fc(Variable(loc_input))

            label_feat = sim_mat_static.permute(0,2,1).contiguous()

            pool_feats = torch.cat((F.layer_norm(pool_feats, [pool_feats.size(-1)]), F.layer_norm(loc_feats, [loc_feats.size(-1)]), \
                                    F.layer_norm(label_feat, [label_feat.size(-1)])), 2)

        # embed fc and att feats
        pool_feats = self.pool_embed(pool_feats)
        fc_feats = self.fc_embed(fc_feats)
        # object region interactions
        if hasattr(self, 'obj_interact'):
            pool_feats = self.obj_interact(pool_feats)

        # Project the attention feats first to reduce memory and computation comsumptions.
        p_pool_feats = self.ctx2pool(pool_feats)

        if self.att_input_mode in ('both', 'featmap'):
            # transpose the conv_feats
            conv_feats = conv_feats.view(batch_size, self.att_feat_size, -1).transpose(1,2).contiguous()
            conv_feats = self.att_embed(conv_feats)
            p_conv_feats = self.ctx2att(conv_feats)
        else:
            conv_feats = pool_feats.new(1,1).fill_(0)
            p_conv_feats = pool_feats.new(1,1).fill_(0)

        vis_offset = (torch.arange(0, beam_size)*rois_num).view(beam_size).type_as(ppls.data).long()
        roi_offset = (torch.arange(0, beam_size)*(rois_num+1)).view(beam_size).type_as(ppls.data).long()

        seq = ppls.data.new(self.seq_length, batch_size).zero_().long()
        seqLogprobs = ppls.data.new(self.seq_length, batch_size).float()
        att2 = ppls.data.new(self.seq_length, batch_size).fill_(-1).long()

        self.done_beams = [[] for _ in range(batch_size)]
        for k in range(batch_size):
            state = self.init_hidden(beam_size)
            beam_fc_feats = fc_feats[k:k+1].expand(beam_size, fc_feats.size(1))
            beam_pool_feats = pool_feats[k:k+1].expand(beam_size, rois_num, self.rnn_size).contiguous()
            if self.att_input_mode in ('both', 'featmap'):
                beam_conv_feats = conv_feats[k:k+1].expand(beam_size, conv_feats.size(1), self.rnn_size).contiguous()
                beam_p_conv_feats = p_conv_feats[k:k+1].expand(beam_size, conv_feats.size(1), self.att_hid_size).contiguous()
            else:
                beam_conv_feats = beam_pool_feats.new(1,1).fill_(0)
                beam_p_conv_feats = beam_pool_feats.new(1,1).fill_(0)
            beam_p_pool_feats = p_pool_feats[k:k+1].expand(beam_size, rois_num, self.att_hid_size).contiguous()

            beam_ppls = ppls[k:k+1].expand(beam_size, rois_num, 6).contiguous()
            beam_pnt_mask = pnt_mask[k:k+1].expand(beam_size, rois_num+1).contiguous()

            it = fc_feats.data.new(beam_size).long().zero_()
            xt = self.embed(Variable(it))

            beam_sim_mat_static_update = sim_mat_static_update[k:k+1].expand(beam_size, self.detect_size+1, rois_num)

            rnn_output, state, att2_weight, att_h, _, _ = self.core(xt, beam_fc_feats, beam_conv_feats,
                beam_p_conv_feats, beam_pool_feats, beam_p_pool_feats, beam_pnt_mask, beam_pnt_mask,
                state, beam_sim_mat_static_update)

            assert(att2_weight.size(0) == beam_size)
            att2[0, k] = torch.max(att2_weight, 1)[1][0]

            self.done_beams[k] = self.beam_search(state, rnn_output, beam_fc_feats, beam_conv_feats, beam_p_conv_feats, \
                                                  beam_pool_feats, beam_p_pool_feats, beam_sim_mat_static_update, beam_ppls, beam_pnt_mask, vis_offset, roi_offset, opt)
                
            seq[:, k] = self.done_beams[k][0]['seq'].cuda() # the first beam has highest cumulative score
            seqLogprobs[:, k] = self.done_beams[k][0]['logps'].cuda()
            att2[1:, k] = self.done_beams[k][0]['att2'][1:].cuda()

        return seq.t(), seqLogprobs.t(), att2.t()
