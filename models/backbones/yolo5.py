import os
import sys
import math

import torch
import torch.nn as nn

sys.path.append(os.path.dirname(__file__).replace('models/backbones', ''))
from models.common import Conv, C3, SPPF

YOLO5_VARIANT = {
        'e': {'depth_multiple': 0.33, 'width_multiple': 0.25,  'blocks':[3, 3, 6, 3], 'expansion':0.375},
        'z': {'depth_multiple': 0.33, 'width_multiple': 0.25,  'blocks':[3, 3, 6, 3], 'expansion':0.5},
        'p': {'depth_multiple': 0.33, 'width_multiple': 0.25,  'blocks':[3, 3, 9, 3], 'expansion':0.5},
        't': {'depth_multiple': 0.33, 'width_multiple': 0.25,  'blocks':[3, 6, 9, 3], 'expansion':0.5},
        's': {'depth_multiple': 0.33, 'width_multiple': 0.50,  'blocks':[3, 6, 9, 3], 'expansion':0.5},
        'm': {'depth_multiple': 0.67, 'width_multiple': 0.75,  'blocks':[3, 6, 9, 3], 'expansion':0.5},
        'l': {'depth_multiple': 1.0,  'width_multiple': 1.0,   'blocks':[3, 6, 9, 3], 'expansion':0.5},
        'x': {'depth_multiple': 1.33, 'width_multiple': 1.25,  'blocks':[3, 6, 9, 3], 'expansion':0.5},
    }


class YoloV5Backbone(nn.Module):
    def __init__(self, variant='s'):  # model, input channels, number of classes
        super(YoloV5Backbone, self).__init__()
        self.variant_params = YOLO5_VARIANT[variant]
        gd, gw    = self.variant_params['depth_multiple'], self.variant_params['width_multiple']
        expansion = self.variant_params['expansion']
        blocks    = self.variant_params['blocks']
        # layers
        base_channel = math.ceil(64 * gw / 8) * 8   # 32
        
        ##! backbone!
        backbone_0 = Conv(3, base_channel, 6, 2, 2)             ## Focus
        backbone_1 = Conv(base_channel, base_channel * 2, 3, 2)
        backbone_2 = C3(base_channel * 2, base_channel * 2,   max(round(blocks[0] * gd), 1), e=expansion)
        backbone_3 = Conv(base_channel * 2, base_channel * 4, 3, 2)
        backbone_4 = C3(base_channel * 4, base_channel * 4,   max(round(blocks[1] * gd), 1), e=expansion)
        backbone_5 = Conv(base_channel * 4, base_channel * 8, 3, 2)
        backbone_6 = C3(base_channel * 8, base_channel * 8,   max(round(blocks[2] * gd), 1), e=expansion)
        backbone_7 = Conv(base_channel * 8, base_channel * 16, 3, 2)
        backbone_8 = C3(base_channel * 16, base_channel * 16, max(round(blocks[3] * gd), 1), e=expansion)
        backbone_9 = SPPF(base_channel * 16, base_channel * 16)
        ## build backbone
        bkbo_layers = [backbone_0, backbone_1, backbone_2, backbone_3, backbone_4, backbone_5, backbone_6, backbone_7, backbone_8, backbone_9]
        self.backbone = nn.Sequential(*bkbo_layers)

    def forward(self, x):
        fpn_feats = []
        for idx, layer in enumerate(self.backbone):
            x = layer(x)
            if idx == 4:
                fpn_feats.append(x)
            elif idx == 6:
                fpn_feats.append(x)
            elif idx == 9:
                fpn_feats.append(x)
            # print(x.shape)
        return fpn_feats


if __name__ == '__main__':
    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)
    # Create model
    img = torch.rand(8 if torch.cuda.is_available() else 1, 3, 640, 640)
    model = YoloV5Backbone(variant='e')
    fpn_feats = model(img)
    print(*[v.shape for v in fpn_feats], sep='\n')
    print(f'params: {count_parameters(model)/(1024*1024):.4f} M')
