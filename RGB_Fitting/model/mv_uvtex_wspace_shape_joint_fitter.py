import torch
import numpy as np
import torch.nn.functional as F

from .losses import perceptual_loss, photo_loss, vgg_loss, landmark_loss, latents_geocross_loss, coeffs_reg_loss
from utils.preprocess_utils import estimate_norm_torch
from utils.data_utils import tensor2np, draw_mask, draw_landmarks, img3channel


class MVFitter:

    def __init__(self,
                 facemodel,
                 tex_gan,
                 renderer,
                 net_recog,
                 net_vgg,
                 logger,
                 input_datas,
                 init_coeffs,
                 init_latents_z=None,
                 **kwargs):
        # parametric face model
        self.facemodel = facemodel
        # texture gan
        self.tex_gan = tex_gan
        # renderer
        self.renderer = renderer
        # the recognition model
        self.net_recog = net_recog.eval().requires_grad_(False)
        # the vgg model
        self.net_vgg = net_vgg.eval()

        # set fitting args
        self.w_feat = kwargs['w_feat'] if 'w_feat' in kwargs else 0.
        self.w_color = kwargs['w_color'] if 'w_color' in kwargs else 0.
        self.w_vgg = kwargs['w_vgg'] if 'w_vgg' in kwargs else 0.
        self.w_reg_id = kwargs['w_reg_id'] if 'w_reg_id' in kwargs else 0.
        self.w_reg_exp = kwargs['w_reg_exp'] if 'w_reg_exp' in kwargs else 0.
        self.w_reg_gamma = kwargs['w_reg_gamma'] if 'w_reg_gamma' in kwargs else 0.
        self.w_reg_latent = kwargs['w_reg_latent'] if 'w_reg_latent' in kwargs else 0.
        self.w_lm = kwargs['w_lm'] if 'w_lm' in kwargs else 0.
        self.initial_lr = kwargs['initial_lr'] if 'initial_lr' in kwargs else 0.01
        self.tex_lr_scale = kwargs['tex_lr_scale'] if 'tex_lr_scale' in kwargs else 0.05
        self.pose_lr_scale = kwargs['pose_lr_scale'] if 'pose_lr_scale' in kwargs else 0.05
        self.lr_rampdown_length = kwargs['lr_rampdown_length'] if 'lr_rampdown_length' in kwargs else 0.25
        self.total_step = kwargs['total_step'] if 'total_step' in kwargs else 100
        self.print_freq = kwargs['print_freq'] if 'print_freq' in kwargs else 10
        self.visual_freq = kwargs['visual_freq'] if 'visual_freq' in kwargs else 10

        # input data for supervision
        num_imgs = len(init_coeffs)

        self.input_img = [input_datas[i]['img'] for i in range(num_imgs)]
        self.skin_mask = [input_datas[i]['skin_mask'] for i in range(num_imgs)]
        self.parse_mask = [input_datas[i]['parse_mask'] for i in range(num_imgs)]
        self.gt_lm = [input_datas[i]['lm'] for i in range(num_imgs)]
        self.trans_m = [input_datas[i]['M'] for i in range(num_imgs)]

        self.input_img_feat = []
        for i in range(num_imgs):
            with torch.no_grad():
                recog_output = self.net_recog(self.input_img, self.trans_m)
            self.input_img_feat.append(recog_output)

        # init coeffs
        
        self.coeffs_opt = torch.zeros((1, 532 + num_imgs*(45 + 3 + 27 + 3)), dtype=torch.float32, device=init_coeffs[0].device)        
        cur_pos = 532
        for i in range(num_imgs):
            coeffs = init_coeffs[i]
            coeffs_opt_dict = self.facemodel.split_coeff(coeffs)
            self.coeffs_opt[:, :532] += coeffs_opt_dict['id']
            self.coeffs_opt[:, cur_pos:cur_pos+45] = coeffs_opt_dict['exp']
            cur_pos += 45
            self.coeffs_opt[:, cur_pos:cur_pos+3] = coeffs_opt_dict['angle']
            cur_pos += 3
            self.coeffs_opt[:, cur_pos:cur_pos+27] = coeffs_opt_dict['gamma']
            cur_pos += 27
            self.coeffs_opt[:, cur_pos:cur_pos+3] = coeffs_opt_dict['trans']
            cur_pos += 3
        self.coeffs_opt[:, :532] /= num_imgs
        self.coeffs_opt.requires_grad = True
        # init latents
        if init_latents_z is not None:
            self.latents_z_opt = init_latents_z
        else:
            self.latents_z_opt = self.tex_gan.get_init_z_latents()
        self.latents_w_opt = self.tex_gan.map_z_to_w(self.latents_z_opt)
        self.latents_w_opt.requires_grad = True  # search w space

        # optimization
        opt_setting_dict_list = [{
            "params": self.latents_w_opt,
            "lr": self.initial_lr * self.tex_lr_scale
        }, {
            "params": self.coeffs_opt[:, :532],
            "lr": self.initial_lr
        }]
        self.initial_lr_list = [
            self.initial_lr * self.tex_lr_scale,
            self.initial_lr]
        
        for i in range(num_imgs):
            opt_setting_dict_list.extend([
            {
                "params": self.coeffs_opt_dict['exp'],
                "lr": self.initial_lr
            }, {
                "params": self.coeffs_opt_dict['angle'],
                "lr": self.initial_lr * self.pose_lr_scale
            }, {
                "params": self.coeffs_opt_dict['gamma'],
                "lr": self.initial_lr * self.tex_lr_scale
            }, {
                "params": self.coeffs_opt_dict['trans'],
                "lr": self.initial_lr * self.pose_lr_scale
            }])
            self.initial_lr_list.extend([
                self.initial_lr,
                self.initial_lr * self.pose_lr_scale,
                self.initial_lr * self.tex_lr_scale,
                self.initial_lr * self.pose_lr_scale])
            
        self.optimizer = torch.optim.Adam(opt_setting_dict_list, betas=(0.9, 0.999))
        
        self.now_step = 0

        # logger
        self.logger = logger

    def update_learning_rate(self):
        t = float(self.now_step) / self.total_step
        lr_ramp = min(1.0, (1.0 - t) / self.lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        for i, param_group in enumerate(self.optimizer.param_groups):
            lr = self.initial_lr_list[i] * lr_ramp
            param_group['lr'] = lr

    def forward(self):
        # forward face model
        self.pred_vertex, self.pred_tex, self.pred_shading, self.pred_color, self.pred_lm = \
            self.facemodel.compute_for_render(self.coeffs_opt_dict)
        # forward texture gan
        self.pred_uv_map = self.tex_gan.synth_uv_map(self.latents_w_opt)
        # render full head
        vertex_uv_coord = self.facemodel.vtx_vt.unsqueeze(0).repeat(self.latents_w_opt.size()[0], 1, 1)
        render_feat = torch.cat([vertex_uv_coord, self.pred_shading], axis=2)
        self.render_head_mask, _, self.render_head = \
            self.renderer(self.pred_vertex, self.facemodel.head_buf, feat=render_feat, uv_map=self.pred_uv_map)
        # render front face
        self.render_face_mask, _, self.render_face = \
            self.renderer(self.pred_vertex, self.facemodel.face_buf, feat=render_feat, uv_map=self.pred_uv_map)

    def compute_losses(self):
        # initial loss
        self.loss_names = ['all']
        self.loss_all = 0.
        # inset front face with input image
        render_face_mask = self.render_face_mask.detach()
        render_face = self.render_face * render_face_mask + (1 - render_face_mask) * self.input_img
        # id feature loss
        if self.w_feat > 0:
            assert self.net_recog.training == False
            if self.pred_lm.shape[1] == 68:
                pred_trans_m = estimate_norm_torch(self.pred_lm, self.input_img.shape[-2])
            else:
                pred_trans_m = self.trans_m
            pred_feat = self.net_recog(render_face, pred_trans_m)
            self.loss_feat = perceptual_loss(pred_feat, self.input_img_feat)
            self.loss_all += self.w_feat * self.loss_feat
            self.loss_names.append('feat')
        # color loss
        if self.w_color > 0:
            loss_face_mask = render_face_mask * self.parse_mask * self.skin_mask
            self.loss_color = photo_loss(render_face, self.input_img, loss_face_mask)
            self.loss_all += self.w_color * self.loss_color
            self.loss_names.append('color')
        # vgg loss, using the same render_face(face_mask) with color loss
        if self.w_vgg > 0:
            loss_face_mask = render_face_mask * self.parse_mask
            render_face_vgg = render_face * loss_face_mask
            input_face_vgg = self.input_img * loss_face_mask
            self.loss_vgg = vgg_loss(render_face_vgg, input_face_vgg, self.net_vgg)
            self.loss_all += self.w_vgg * self.loss_vgg
            self.loss_names.append('vgg')
        # coeffs regression loss
        if self.w_reg_id > 0 or self.w_reg_exp > 0 or self.w_reg_gamma > 0:
            self.loss_reg_id, self.loss_reg_exp, _, self.loss_reg_gamma = coeffs_reg_loss(self.coeffs_opt_dict)
            self.loss_all += self.w_reg_id * self.loss_reg_id
            self.loss_all += self.w_reg_exp * self.loss_reg_exp
            self.loss_all += self.w_reg_gamma * self.loss_reg_gamma
            self.loss_names.extend(['reg_id', 'reg_exp', 'reg_gamma'])
        # w latent geocross regression
        if self.w_reg_latent > 0:
            self.loss_reg_latent = latents_geocross_loss(self.latents_w_opt)
            self.loss_all += self.w_reg_latent * self.loss_reg_latent
            self.loss_names.append('reg_latent')
        # 68 landmarks loss
        if self.w_lm > 0:
            self.loss_lm = landmark_loss(self.pred_lm, self.gt_lm)
            self.loss_all += self.w_lm * self.loss_lm
            self.loss_names.append('lm')

    def optimize_parameters(self):
        self.update_learning_rate()
        self.forward()
        self.compute_losses()
        self.optimizer.zero_grad()
        self.loss_all.backward()
        self.optimizer.step()
        self.now_step += 1

    def gather_visual_img(self):
        # input data
        input_img = tensor2np(self.input_img[:1, :, :, :])
        skin_img = img3channel(tensor2np(self.skin_mask[:1, :, :, :]))
        parse_mask = tensor2np(self.parse_mask[:1, :, :, :], dst_range=1.0)
        gt_lm = self.gt_lm[0, :, :].detach().cpu().numpy()
        # predict data
        pre_uv_img = tensor2np(F.interpolate(self.pred_uv_map, size=input_img.shape[:2], mode='area')[:1, :, :, :])
        pred_face_img = self.render_face * self.render_face_mask + (1 - self.render_face_mask) * self.input_img
        pred_face_img = tensor2np(pred_face_img[:1, :, :, :])
        pred_head_img = tensor2np(self.render_head[:1, :, :, :])
        pred_lm = self.pred_lm[0, :, :].detach().cpu().numpy()
        # draw mask and landmarks
        parse_img = draw_mask(input_img, parse_mask)
        gt_lm[..., 1] = pred_face_img.shape[0] - 1 - gt_lm[..., 1]
        pred_lm[..., 1] = pred_face_img.shape[0] - 1 - pred_lm[..., 1]
        lm_img = draw_landmarks(pred_face_img, gt_lm, color='b')
        lm_img = draw_landmarks(lm_img, pred_lm, color='r')
        # combine visual images
        combine_img = np.concatenate([input_img, skin_img, parse_img, lm_img, pred_face_img, pred_head_img, pre_uv_img],
                                     axis=1)
        return combine_img

    def gather_loss_log_str(self):
        loss_log = {}
        loss_str = ''
        for name in self.loss_names:
            loss_value = float(getattr(self, 'loss_' + name))
            loss_log[f'loss/{name}'] = loss_value
            loss_str += f'[loss/{name}: {loss_value:.5f}]'
        return loss_log, loss_str

    def iterate(self):
        for _ in range(self.total_step):
            # optimize
            self.optimize_parameters()
            # print log
            if self.now_step % self.print_freq == 0 or self.now_step == self.total_step:
                loss_log, loss_str = self.gather_loss_log_str()
                now_lr = self.optimizer.param_groups[0]['lr']
                self.logger.write_tb_scalar(['lr'], [now_lr], self.now_step)
                self.logger.write_tb_scalar(loss_log.keys(), loss_log.values(), self.now_step)
                self.logger.write_txt_log(f'[step {self.now_step}/{self.total_step}] [lr:{now_lr:.7f}] {loss_str}')
            # save intermediate visual results
            if self.now_step % self.visual_freq == 0 or self.now_step == self.total_step:
                vis_img = self.gather_visual_img()
                self.logger.write_tb_images([vis_img], ['vis'], self.now_step)

        final_coeffs = self.coeffs_opt.detach().clone()
        final_latents_w = self.latents_w_opt.detach().clone()
        final_latents_z = self.tex_gan.inverse_w_to_z(final_latents_w).detach().clone()
        return final_coeffs, final_latents_z, final_latents_w