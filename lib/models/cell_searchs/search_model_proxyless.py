###########################################################################
# Searching for A Robust Neural Architecture in Four GPU Hours, CVPR 2019 #
###########################################################################
import torch
import torch.nn as nn
from copy import deepcopy
from ..cell_operations import ResNetBasicblock
from .search_cells     import NAS201SearchCell as SearchCell
from .genotypes        import Structure


class TinyNetworkProxyless(nn.Module):

  #def __init__(self, C, N, max_nodes, num_classes, search_space, affine=False, track_running_stats=True):
  def __init__(self, C, N, max_nodes, num_classes, search_space, affine, track_running_stats, inp_size):
    super(TinyNetworkProxyless, self).__init__()
    self._C        = C
    self._layerN   = N
    self.max_nodes = max_nodes
    self.stem = nn.Sequential(
                    nn.Conv2d(3, C, kernel_size=3, padding=1, bias=False),
                    nn.BatchNorm2d(C))
  
    layer_channels   = [C    ] * N + [C*2 ] + [C*2  ] * N + [C*4 ] + [C*4  ] * N    
    layer_reductions = [False] * N + [True] + [False] * N + [True] + [False] * N
    layer_sizes      = [inp_size] * N + [inp_size/2] + [inp_size/2] * N + [inp_size/4] + [inp_size/4] * N

    C_prev, num_edge, edge2index = C, None, None
    self.cells = nn.ModuleList()
    for index, (C_curr, reduction, size) in enumerate(zip(layer_channels, layer_reductions, layer_sizes)):
      if reduction:
        cell = ResNetBasicblock(C_prev, C_curr, 2)
      else:
        cell = SearchCell(C_prev, C_curr, 1, max_nodes, search_space, size, affine, track_running_stats)
        if num_edge is None: num_edge, edge2index = cell.num_edges, cell.edge2index
        else: assert num_edge == cell.num_edges and edge2index == cell.edge2index, 'invalid {:} vs. {:}.'.format(num_edge, cell.num_edges)
      self.cells.append( cell )
      C_prev = cell.out_dim
    self.op_names   = deepcopy( search_space )
    self._Layer     = len(self.cells)
    self.edge2index = edge2index
    self.lastact    = nn.Sequential(nn.BatchNorm2d(C_prev), nn.ReLU(inplace=True))
    self.global_pooling = nn.AdaptiveAvgPool2d(1)
    self.classifier = nn.Linear(C_prev, num_classes)
    self.arch_parameters = nn.Parameter( 1e-3*torch.randn(num_edge, len(search_space)) )
    self.tau        = 10
    self.const      = bool(inp_size)

  def get_weights(self):
    xlist = list( self.stem.parameters() ) + list( self.cells.parameters() )
    xlist+= list( self.lastact.parameters() ) + list( self.global_pooling.parameters() )
    xlist+= list( self.classifier.parameters() )
    return xlist

  def set_tau(self, tau):
    self.tau = tau

  def get_tau(self):
    return self.tau

  def get_alphas(self):
    return [self.arch_parameters]

  def show_alphas(self):
    with torch.no_grad():
      return 'arch-parameters :\n{:}'.format( nn.functional.softmax(self.arch_parameters, dim=-1).cpu() )

  def get_message(self):
    string = self.extra_repr()
    for i, cell in enumerate(self.cells):
      string += '\n {:02d}/{:02d} :: {:}'.format(i, len(self.cells), cell.extra_repr())
    return string

  def extra_repr(self):
    return ('{name}(C={_C}, Max-Nodes={max_nodes}, N={_layerN}, L={_Layer})'.format(name=self.__class__.__name__, **self.__dict__))

  def sample_weights(self, n):
    sampled_weights = []
    for i in range(n):
      with torch.no_grad():
        while True:
          probs   = nn.functional.softmax(self.arch_parameters, dim=1)
          index   = torch.multinomial(probs, 1)
          one_h   = torch.zeros_like(probs).scatter_(-1, index, 1.0)
          hardwts = one_h - probs.detach() + probs
          if ((torch.isinf(probs).any()) or (torch.isnan(probs).any())):
            continue
          else:
            sampled_weights.append(one_h)
            break
    return sampled_weights

  def genotype(self, w=None):
    genotypes = []
    for i in range(1, self.max_nodes):
      xlist = []
      for j in range(i):
        node_str = '{:}<-{:}'.format(i, j)
        with torch.no_grad():
          if w is None:
            weights = self.arch_parameters[ self.edge2index[node_str] ]
          else:
            weights = w[ self.edge2index[node_str] ]
          op_name = self.op_names[ weights.argmax().item() ]
        xlist.append((op_name, j))
      genotypes.append( tuple(xlist) )
    return Structure( genotypes )

  def forward(self, inputs, weights=None):
    while weights is None:
      probs   = nn.functional.softmax(self.arch_parameters, dim=1)
      index   = torch.multinomial(probs, 1)
      one_h   = torch.zeros_like(probs).scatter_(-1, index, 1.0)
      hardwts = one_h - probs.detach() + probs
      if ((torch.isinf(probs).any()) or (torch.isnan(probs).any())):
        continue
      else:
        break

    feature = self.stem(inputs)
    full_cost = []
    for i, cell in enumerate(self.cells):
      if isinstance(cell, SearchCell) and weights is None and not self.const:
        feature = cell.forward_gdas(feature, hardwts, index)
      elif isinstance(cell, SearchCell) and weights is None:
        feature, cost = cell.forward_gdas_const(feature, hardwts, index)
        full_cost.append(cost)
      elif isinstance(cell, SearchCell):
        index = weights.max(-1, keepdim=True)[1]
        feature = cell.forward_gdas(feature, weights, index)
      else:
        feature = cell(feature)
    out = self.lastact(feature)
    out = self.global_pooling( out )
    out = out.view(out.size(0), -1)
    logits = self.classifier(out)

    return out, logits, sum(full_cost)
