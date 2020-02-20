#FastAI's XResnet modified to use Mish activation function, MXResNet 
#https://github.com/fastai/fastai/blob/master/fastai/vision/models/xresnet.py
#modified by lessw2020 - github:  https://github.com/lessw2020/mish


from fastai.torch_core import *
import torch.nn as nn
import torch,math,sys
import torch.utils.model_zoo as model_zoo
from functools import partial
#from ...torch_core import Module
from fastai.torch_core import Module

import torch.nn.functional as F  #(uncomment if needed,but you likely already have it)


class Mish(nn.Module):
    def __init__(self):
        super().__init__()
        print("Mish activation loaded...")

    def forward(self, x):  
        #save 1 second per epoch with no x= x*() and then return x...just inline it.
        return x *( torch.tanh(F.softplus(x))) 
        


    

#Unmodified from https://github.com/fastai/fastai/blob/5c51f9eabf76853a89a9bc5741804d2ed4407e49/fastai/layers.py
def conv1d(ni:int, no:int, ks:int=1, stride:int=1, padding:int=0, bias:bool=False):
    "Create and initialize a `nn.Conv1d` layer with spectral normalization."
    conv = nn.Conv1d(ni, no, ks, stride=stride, padding=padding, bias=bias)
    nn.init.kaiming_normal_(conv.weight)
    if bias: conv.bias.data.zero_()
    return spectral_norm(conv)



# Adapted from SelfAttention layer at https://github.com/fastai/fastai/blob/5c51f9eabf76853a89a9bc5741804d2ed4407e49/fastai/layers.py
# Inspired by https://arxiv.org/pdf/1805.08318.pdf
class SimpleSelfAttention(nn.Module):
    
    def __init__(self, n_in:int, ks=1, sym=False):#, n_out:int):
        super().__init__()
           
        self.conv = conv1d(n_in, n_in, ks, padding=ks//2, bias=False)      
       
        self.gamma = nn.Parameter(tensor([0.]))
        
        self.sym = sym
        self.n_in = n_in
        
    def forward(self,x):
        
        
        if self.sym:
            # symmetry hack by https://github.com/mgrankin
            c = self.conv.weight.view(self.n_in,self.n_in)
            c = (c + c.t())/2
            self.conv.weight = c.view(self.n_in,self.n_in,1)
                
        size = x.size()  
        x = x.view(*size[:2],-1)   # (C,N)
        
        # changed the order of mutiplication to avoid O(N^2) complexity
        # (x*xT)*(W*x) instead of (x*(xT*(W*x)))
        
        convx = self.conv(x)   # (C,C) * (C,N) = (C,N)   => O(NC^2)
        xxT = torch.bmm(x,x.permute(0,2,1).contiguous())   # (C,N) * (N,C) = (C,C)   => O(NC^2)
        
        o = torch.bmm(xxT, convx)   # (C,C) * (C,N) = (C,N)   => O(NC^2)
          
        o = self.gamma * o + x
        
          
        return o.view(*size).contiguous()        
    
def ramp_func(r, a=0.3):
    # r is squared distance (scaled down to 0~1) from the center of a 2d image
    # a=0 makes it a step function
    one = torch.ones(r.size()).type(r.dtype).to(r.device)
    zero = torch.zeros(r.size()).type(r.dtype).to(r.device)
    return torch.where(r<=1-a, one, torch.where(r>=1+a, zero, 1-(r-1+a)/(2*a)))

class TwistLayer(Module):
    def __init__(self, ni, nf, stride=1):
        self.XX, self.YY = None, None
        self.mask = None
        self.radii = torch.nn.Parameter(torch.ones(nf), requires_grad=True)
        self.center_x = torch.nn.Parameter(torch.ones(nf), requires_grad=True)
        self.center_y = torch.nn.Parameter(torch.ones(nf), requires_grad=True)
        self.radii.data.uniform_(0.3, 0.7)
        self.center_x.data.uniform_(-0.7, 0.7)
        self.center_y.data.uniform_(-0.7, 0.7)
        self.conv = conv(ni, nf, stride=stride)
        self.convx = conv(ni, nf, stride=stride)
        self.convy = conv(ni, nf, stride=stride)

    def forward(self, x):
        # make convx a first-order operator by symmetrizing it
        self.convx.weight.data = (self.convx.weight - self.convx.weight.flip(2).flip(3)) / 2
        # self.convy.weight.data = (self.convy.weight - self.convy.weight.flip(2).flip(3)) / 2
        # make convy a 90 degree rotation of convx
        self.convy.weight.data = self.convx.weight.transpose(2,3).flip(2)
        x1 = self.conv(x)
        _, c, h, w = x1.size()
        self.XX = torch.from_numpy(np.indices((1,h,w))[2]*2/w).type(x.dtype).to(x.device) - self.center_x.view(-1,1,1)
        self.YY = torch.from_numpy(np.indices((1,h,w))[1]*2/h).type(x.dtype).to(x.device) - self.center_y.view(-1,1,1)
        self.mask = ramp_func((self.XX**2+self.YY**2)/(self.radii.type(x.dtype).to(x.device).view(-1,1,1)**2))
        return x1 + self.mask * (self.XX * self.convx(x) + self.YY * self.convy(x))

    
    
__all__ = ['MXResNet', 'mxresnet18', 'mxresnet34', 'mxresnet50', 'mxresnet101', 'mxresnet152']

# or: ELU+init (a=0.54; gain=1.55)
act_fn = Mish() #nn.ReLU(inplace=True)

class Flatten(Module):
    def forward(self, x): return x.view(x.size(0), -1)

def init_cnn(m):
    if getattr(m, 'bias', None) is not None: nn.init.constant_(m.bias, 0)
    if isinstance(m, (nn.Conv2d,nn.Linear)): nn.init.kaiming_normal_(m.weight)
    for l in m.children(): init_cnn(l)

def conv(ni, nf, ks=3, stride=1, bias=False):
    return nn.Conv2d(ni, nf, kernel_size=ks, stride=stride, padding=ks//2, bias=bias)

def noop(x): return x

def conv_layer(ni, nf, ks=3, stride=1, zero_bn=False, act=True):
    bn = nn.BatchNorm2d(nf)
    nn.init.constant_(bn.weight, 0. if zero_bn else 1.)
    layers = [conv(ni, nf, ks, stride=stride), bn] if ks==1 or ni<=32 else [TwistLayer(ni, nf, stride=stride), bn]
    if act: layers.append(act_fn)
    return nn.Sequential(*layers)

class ResBlock(Module):
    def __init__(self, expansion, ni, nh, stride=1,sa=False, sym=False):
        nf,ni = nh*expansion,ni*expansion
        layers  = [conv_layer(ni, nh, 3, stride=stride),
                   conv_layer(nh, nf, 3, zero_bn=True, act=False)
        ] if expansion == 1 else [
                   conv_layer(ni, nh, 1),
                   conv_layer(nh, nh, 3, stride=stride),
                   conv_layer(nh, nf, 1, zero_bn=True, act=False)
        ]
        self.sa = SimpleSelfAttention(nf,ks=1,sym=sym) if sa else noop
        self.convs = nn.Sequential(*layers)
        # TODO: check whether act=True works better
        self.idconv = noop if ni==nf else conv_layer(ni, nf, 1, act=False)
        self.pool = noop if stride==1 else nn.AvgPool2d(2, ceil_mode=True)

    def forward(self, x): return act_fn(self.sa(self.convs(x)) + self.idconv(self.pool(x)))

def filt_sz(recep): return min(64, 2**math.floor(math.log2(recep*0.75)))

class MXResNet(nn.Sequential):
    def __init__(self, expansion, layers, c_in=3, c_out=1000, sa = False, sym= False):
        stem = []
        sizes = [c_in,32,64,64]  #modified per Grankin
        for i in range(3):
            stem.append(conv_layer(sizes[i], sizes[i+1], stride=2 if i==0 else 1))
            #nf = filt_sz(c_in*9)
            #stem.append(conv_layer(c_in, nf, stride=2 if i==1 else 1))
            #c_in = nf

        block_szs = [64//expansion,64,128,256,512]
        blocks = [self._make_layer(expansion, block_szs[i], block_szs[i+1], l, 1 if i==0 else 2, sa = sa if i in[len(layers)-4] else False, sym=sym)
                  for i,l in enumerate(layers)]
        super().__init__(
            *stem,
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            *blocks,
            nn.AdaptiveAvgPool2d(1), Flatten(),
            nn.Linear(block_szs[-1]*expansion, c_out),
        )
        init_cnn(self)

    def _make_layer(self, expansion, ni, nf, blocks, stride, sa=False, sym=False):
        return nn.Sequential(
            *[ResBlock(expansion, ni if i==0 else nf, nf, stride if i==0 else 1, sa if i in [blocks -1] else False,sym)
              for i in range(blocks)])

def mxresnet(expansion, n_layers, name, pretrained=False, **kwargs):
    model = MXResNet(expansion, n_layers, **kwargs)
    if pretrained: 
        #model.load_state_dict(model_zoo.load_url(model_urls[name]))
        print("No pretrained yet for MXResNet")
    return model

me = sys.modules[__name__]
for n,e,l in [
    [ 18 , 1, [2,2,2 ,2] ],
    [ 34 , 1, [3,4,6 ,3] ],
    [ 50 , 4, [3,4,6 ,3] ],
    [ 101, 4, [3,4,23,3] ],
    [ 152, 4, [3,8,36,3] ],
]:
    name = f'mxresnet{n}'
    setattr(me, name, partial(mxresnet, expansion=e, n_layers=l, name=name))
