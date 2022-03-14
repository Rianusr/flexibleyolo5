import os
import sys
from einops import rearrange
from einops.layers.torch import Rearrange

import torch
import torch.nn as nn

sys.path.append(os.path.dirname(__file__).replace('models/backbones', ''))
from models.common import Conv

PARAMS = {
    ##       L                              D 
    'p':{'num_blocks': [2, 2, 3, 5, 2],   'channels': [32,  32,  64,  128, 256],  'downsample':True},   ## pico
    'n':{'num_blocks': [2, 2, 3, 5, 2],   'channels': [32,  48,  96,  192, 384],  'downsample':True},   ## nano
    'm':{'num_blocks': [2, 2, 3, 5, 2],   'channels': [32,  64,  128, 256, 512],  'downsample':True},   ## micro
    't':{'num_blocks': [2, 2, 3, 5, 2],   'channels': [64,  96,  192, 384, 768],  'downsample':True},   ## tiny
    's':{'num_blocks': [2, 2, 6, 14, 2],  'channels': [64,  96,  192, 384, 768],  'downsample':True},   ## small
    'l':{'num_blocks': [2, 2, 6, 14, 2],  'channels': [128, 128, 256, 512, 1024], 'downsample':True},   ## large
    'h':{'num_blocks': [2, 2, 6, 14, 2],  'channels': [192, 192, 384, 768, 1536], 'downsample':True},   ## huge
    'g':{'num_blocks': [2, 2, 12, 28, 2], 'channels': [192, 192, 384, 768, 1536], 'downsample':True},   ## giant
}


def conv_3x3_bn(inp, oup, image_size, downsample=False):
    stride = 1 if downsample == False else 2
    return nn.Sequential(
        nn.Conv2d(inp, oup, 3, stride, 1, bias=False),
        nn.BatchNorm2d(oup),
        nn.GELU()
    )


class PreNorm(nn.Module):
    def __init__(self, dim, fn, norm):
        super().__init__()
        self.norm = norm(dim)
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)


class SE(nn.Module):
    def __init__(self, inp, oup, expansion=0.25):
        super().__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(oup, int(inp * expansion), bias=False),
            nn.GELU(),
            nn.Linear(int(inp * expansion), oup, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y


class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        return self.net(x)


class MBConv(nn.Module):
    def __init__(self, inp, oup, image_size, downsample=False, expansion=4):
        super().__init__()
        self.downsample = downsample
        stride = 1 if self.downsample == False else 2
        hidden_dim = int(inp * expansion)

        if self.downsample:
            self.pool = nn.MaxPool2d(3, 2, 1)
            self.proj = nn.Conv2d(inp, oup, 1, 1, 0, bias=False)

        if expansion == 1:
            self.conv = nn.Sequential(
                # dw
                nn.Conv2d(hidden_dim, hidden_dim, 3, stride, 1, groups=hidden_dim, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.GELU(),
                # pw-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )
        else:
            self.conv = nn.Sequential(
                # pw
                # down-sample in the first conv
                nn.Conv2d(inp, hidden_dim, 1, stride, 0, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.GELU(),
                # dw
                nn.Conv2d(hidden_dim, hidden_dim, 3, 1, 1, groups=hidden_dim, bias=False),
                nn.BatchNorm2d(hidden_dim),
                nn.GELU(),
                SE(inp, hidden_dim),
                # pw-linear
                nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),
                nn.BatchNorm2d(oup),
            )
        
        self.conv = PreNorm(inp, self.conv, nn.BatchNorm2d)

    def forward(self, x):
        if self.downsample:
            return self.proj(self.pool(x)) + self.conv(x)
        else:
            return x + self.conv(x)


class Attention(nn.Module):
    def __init__(self, inp, oup, image_size, heads=8, dim_head=32, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == inp)

        self.ih, self.iw = image_size

        self.heads = heads
        self.scale = dim_head ** -0.5

        # parameter table of relative position bias
        self.relative_bias_table = nn.Parameter(torch.zeros((2 * self.ih - 1) * (2 * self.iw - 1), heads))

        coords = torch.meshgrid((torch.arange(self.ih), torch.arange(self.iw)))
        coords = torch.flatten(torch.stack(coords), 1)
        relative_coords = coords[:, :, None] - coords[:, None, :]

        relative_coords[0] += self.ih - 1
        relative_coords[1] += self.iw - 1
        relative_coords[0] *= 2 * self.iw - 1
        relative_coords = rearrange(relative_coords, 'c h w -> h w c')
        relative_index = relative_coords.sum(-1).flatten().unsqueeze(1)
        self.register_buffer("relative_index", relative_index)

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(inp, inner_dim * 3, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, oup),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        # Use "gather" for more efficiency on GPUs
        relative_bias = self.relative_bias_table.gather(0, self.relative_index.repeat(1, self.heads))
        relative_bias = rearrange(relative_bias, '(h w) c -> 1 c h w', h=self.ih*self.iw, w=self.ih*self.iw)
        dots = dots + relative_bias

        attn = self.attend(dots)
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        out = self.to_out(out)
        return out


class Transformer(nn.Module):
    def __init__(self, inp, oup, image_size, heads=8, dim_head=32, downsample=False, dropout=0.):
        super().__init__()
        hidden_dim = int(inp * 4)

        self.ih, self.iw = image_size
        self.downsample = downsample

        if self.downsample:
            self.pool1 = nn.MaxPool2d(3, 2, 1)
            self.pool2 = nn.MaxPool2d(3, 2, 1)
            self.proj = nn.Conv2d(inp, oup, 1, 1, 0, bias=False)

        self.attn = Attention(inp, oup, image_size, heads, dim_head, dropout)
        self.ff = FeedForward(oup, hidden_dim, dropout)

        self.attn = nn.Sequential(
            Rearrange('b c ih iw -> b (ih iw) c'),
            PreNorm(inp, self.attn, nn.LayerNorm),
            Rearrange('b (ih iw) c -> b c ih iw', ih=self.ih, iw=self.iw)
        )

        self.ff = nn.Sequential(
            Rearrange('b c ih iw -> b (ih iw) c'),
            PreNorm(oup, self.ff, nn.LayerNorm),
            Rearrange('b (ih iw) c -> b c ih iw', ih=self.ih, iw=self.iw)
        )

    def forward(self, x):
        if self.downsample:
            x = self.proj(self.pool1(x)) + self.attn(self.pool2(x))
        else:
            x = x + self.attn(x)
        x = x + self.ff(x)
        return x


class CoAtNetBackbone(nn.Module):
    def __init__(self, variant='0', image_size=(640, 640), block_types=['C', 'C', 'T', 'T'], in_channels=3, focus_ch=32):
        super().__init__()
        
        num_blocks = PARAMS[variant]['num_blocks']
        channels   = PARAMS[variant]['channels']
        
        ih, iw = image_size
        block = {'C': MBConv, 'T': Transformer}
        self.focus = Conv(in_channels, focus_ch, 6, 2, 2) if focus_ch > 3 else nn.Identity()
        if not isinstance(self.focus, nn.Identity):
            in_channels = focus_ch
            ih, iw = ih //2, iw // 2

        self.s0 = self._make_layer(conv_3x3_bn,           in_channels, channels[0], num_blocks[0], (ih // 2,  iw // 2))
        self.s1 = self._make_layer(block[block_types[0]], channels[0], channels[1], num_blocks[1], (ih // 4,  iw // 4))
        self.s2 = self._make_layer(block[block_types[1]], channels[1], channels[2], num_blocks[2], (ih // 8,  iw // 8))
        self.s3 = self._make_layer(block[block_types[2]], channels[2], channels[3], num_blocks[3], (ih // 16, iw // 16))
        self.s4 = self._make_layer(block[block_types[3]], channels[3], channels[4], num_blocks[4], (ih // 32, iw // 32))
        
        self.layers = nn.Sequential(self.focus, self.s0, self.s1, self.s2, self.s3, self.s4)

    def forward(self, x):
        fpn_feats = []
        for idx, m in enumerate(self.layers):
            x = m(x)
            if idx in [2, 3, 4]:            ## 取80、40、20特征图，for_yolox
                fpn_feats.append(x)
        return fpn_feats
    
    def _make_layer(self, block, inp, oup, depth, image_size, downsample=True):
        layers = nn.ModuleList([])
        for i in range(depth):
            if i == 0:
                layers.append(block(inp, oup, image_size, downsample=downsample))
            else:
                layers.append(block(oup, oup, image_size))
        return nn.Sequential(*layers)


if __name__ == '__main__':
    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)
    img = torch.randn(1, 3, 640, 640)
    model = CoAtNetBackbone(variant='p', image_size=(640, 640))
    fpn_feats = model(img)
    print(*[v.shape for v in fpn_feats], sep='\n')
    print(f'params: {count_parameters(model)/(1024*1024):.2f} M')
