import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import init, Parameter
import numpy as np
from IPython import embed

from . import basic
import conf


class BinaryTreeLSTMLayer(nn.Module):

    def __init__(self, hidden_dim):
        super(BinaryTreeLSTMLayer, self).__init__()
        self.hidden_dim = hidden_dim
        self.comp_linear = nn.Linear(in_features=2 * hidden_dim,
                                     out_features=5 * hidden_dim)
        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_normal(self.comp_linear.weight.data)
        init.constant(self.comp_linear.bias.data, val=0)

    def forward(self, l=None, r=None):
        """
        Args:
            l: A (h_l, c_l, lens_l) tuple, where h and c have the size (batch_size, length-1, hidden_dim), lens has the size (batch_size, length-1)
            r: A (h_r, c_r, lens_r) tuple
        Returns:
            h, c, lens: The hidden and cell state of the composed parent
        """

        hl, cl, lens_l = l
        hr, cr, lens_r = r
        hlr_cat = torch.cat([hl, hr], dim=2)
        treelstm_vector = basic.apply_nd(fn=self.comp_linear, input=hlr_cat)
        i, fl, fr, u, o = torch.chunk(treelstm_vector, chunks=5, dim=2)
        c = (cl*(fl + 1).sigmoid() + cr*(fr + 1).sigmoid()
             + u.tanh()*i.sigmoid())
        h = o.sigmoid() * c.tanh()
        return h, c, lens_l+lens_r # directly sum up the children's lens to obtain parent's lens

class SimpleLayer(nn.Module):

    def __init__(self, hidden_dim):
        super(SimpleLayer, self).__init__()
        self.hidden_dim = hidden_dim
        self.comp_linear = nn.Linear(in_features=2 * hidden_dim,
                            out_features = hidden_dim)
        # TODO: more parameters
        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_normal(self.comp_linear.weight.data)
        init.constant(self.comp_linear.bias.data, val=0)

    def forward(self, l=None, r=None):
        hl, cl, lens_l = l
        hr, cr, lens_r = r
        hlr_cat = torch.cat([hl, hr], dim=2)
        h = basic.apply_nd(fn=self.comp_linear, input=hlr_cat)
        h = h.sigmoid()
        return h, None, lens_l+lens_r


class BinaryTreeLSTM(nn.Module):

    def __init__(self, word_dim, hidden_dim, use_leaf_rnn, intra_attention,
                 gumbel_temperature, bidirectional, weighted_by_interval_length, weighted_base,
                 weighted_update, cell_type):
        super(BinaryTreeLSTM, self).__init__()
        self.word_dim = word_dim
        self.hidden_dim = hidden_dim
        self.use_leaf_rnn = use_leaf_rnn
        self.intra_attention = intra_attention
        self.gumbel_temperature = gumbel_temperature
        self.bidirectional = bidirectional
        self.weighted_by_interval_length = weighted_by_interval_length
        # Note base of <torch.pow> should be a Variable. requires_grad can control whether it is trainable
        if weighted_update:
            self.weighted_base = Parameter(torch.Tensor([weighted_base]).cuda())
        else:
            self.weighted_base = Variable(torch.Tensor([weighted_base]).cuda(), requires_grad=False)
        self.cell_type = cell_type
        assert self.cell_type in ['treelstm', 'simple']

        ComposeCell = None
        if self.cell_type == 'treelstm':
            ComposeCell = BinaryTreeLSTMLayer
        elif self.cell_type == 'simple':
            ComposeCell = SimpleLayer

        assert not (self.bidirectional and not self.use_leaf_rnn)

        if use_leaf_rnn:
            self.leaf_rnn_cell = nn.LSTMCell(
                input_size=word_dim, hidden_size=hidden_dim)
            if bidirectional:
                self.leaf_rnn_cell_bw = nn.LSTMCell(
                    input_size=word_dim, hidden_size=hidden_dim)
        else:
            self.word_linear = nn.Linear(in_features=word_dim,
                                         out_features=2 * hidden_dim)
        if self.bidirectional:
            self.treelstm_layer = ComposeCell(2 * hidden_dim)
            self.comp_query = nn.Parameter(torch.FloatTensor(2 * hidden_dim))
        else:
            self.treelstm_layer = ComposeCell(hidden_dim)
            self.comp_query = nn.Parameter(torch.FloatTensor(hidden_dim))

        self.reset_parameters()

    def reset_parameters(self):
        if self.use_leaf_rnn:
            init.kaiming_normal(self.leaf_rnn_cell.weight_ih.data)
            init.orthogonal(self.leaf_rnn_cell.weight_hh.data)
            init.constant(self.leaf_rnn_cell.bias_ih.data, val=0)
            init.constant(self.leaf_rnn_cell.bias_hh.data, val=0)
            # Set forget bias to 1
            self.leaf_rnn_cell.bias_ih.data.chunk(4)[1].fill_(1)
            if self.bidirectional:
                init.kaiming_normal(self.leaf_rnn_cell_bw.weight_ih.data)
                init.orthogonal(self.leaf_rnn_cell_bw.weight_hh.data)
                init.constant(self.leaf_rnn_cell_bw.bias_ih.data, val=0)
                init.constant(self.leaf_rnn_cell_bw.bias_hh.data, val=0)
                # Set forget bias to 1
                self.leaf_rnn_cell_bw.bias_ih.data.chunk(4)[1].fill_(1)
        else:
            init.kaiming_normal(self.word_linear.weight.data)
            init.constant(self.word_linear.bias.data, val=0)
        self.treelstm_layer.reset_parameters()
        init.normal(self.comp_query.data, mean=0, std=0.01)

    @staticmethod
    def update_state(old_state, new_state, done_mask):
        old_h, old_c, old_lens = old_state
        new_h, new_c, new_lens = new_state
        done_len_mask = done_mask.float().unsqueeze(1).expand_as(new_lens)
        done_mask = done_mask.float().unsqueeze(1).unsqueeze(2).expand_as(new_h)
        # If the sentence has been done, then done_mask=0 and h=old_h[:-1]. Else, done_mask=1 and h=new_h. (regardless of batch dimension)
        h = done_mask * new_h + (1 - done_mask) * old_h[:, :-1, :]
        if new_c is not None:
            c = done_mask * new_c + (1 - done_mask) * old_c[:, :-1, :]
        else:
            c = None
        lens = done_len_mask * new_lens + (1 - done_len_mask) * old_lens[:, :-1]
        return h, c, lens

    def select_composition(self, old_state, new_state, mask):
        new_h, new_c, new_lens = new_state
        old_h, old_c, old_lens = old_state
        old_h_left, old_h_right = old_h[:, :-1, :], old_h[:, 1:, :]
        if old_c is not None:
            old_c_left, old_c_right = old_c[:, :-1, :], old_c[:, 1:, :]
        old_lens_left, old_lens_right = old_lens[:, :-1], old_lens[:, 1:]
        comp_weights = basic.dot_nd(query=self.comp_query, candidates=new_h)
        if conf.debug:
            if np.isnan(comp_weights.data.cpu().numpy()).any():
                print('nan in comp_weights')
                embed()
                raise Exception('')
        if self.training:
            select_mask = basic.st_gumbel_softmax(
                    logits=comp_weights, 
                    temperature=self.gumbel_temperature, 
                    mask=mask, 
                    use_weight=self.weighted_by_interval_length, 
                    weights=new_lens, 
                    base=self.weighted_base)
        else:
            select_mask = basic.greedy_select(
                    logits=comp_weights, 
                    mask=mask, 
                    use_weight=self.weighted_by_interval_length,
                    weights=new_lens, 
                    base=self.weighted_base).float()
        select_mask_expand = select_mask.unsqueeze(2).expand_as(new_h)
        select_mask_cumsum = select_mask.cumsum(1)
        left_mask = 1 - select_mask_cumsum
        left_mask_expand = left_mask.unsqueeze(2).expand_as(old_h_left)
        right_mask_leftmost_col = Variable(
            select_mask_cumsum.data.new(new_h.size(0), 1).zero_())
        right_mask = torch.cat(
            [right_mask_leftmost_col, select_mask_cumsum[:, :-1]], dim=1)
        right_mask_expand = right_mask.unsqueeze(2).expand_as(old_h_right)
        new_h = (select_mask_expand * new_h
                 + left_mask_expand * old_h_left
                 + right_mask_expand * old_h_right)
        if new_c is not None: # If cell_type==simple, then all hidden c will be None
            new_c = (select_mask_expand * new_c
                 + left_mask_expand * old_c_left
                 + right_mask_expand * old_c_right)
        new_lens = (select_mask * new_lens + left_mask * old_lens_left + right_mask * old_lens_right) 
        selected_h = (select_mask_expand * new_h).sum(1)
        return new_h, new_c, new_lens, select_mask, selected_h

    def forward(self, input, length, return_select_masks=False):
        max_depth = input.size(1)
        length_mask = basic.sequence_mask(sequence_length=length,
                                          max_length=max_depth)
        select_masks = []
        # After each composition step from n nodes to n-1 nodes, interval_lens should be updated
        # Note interval_lens must be FloatTensor
        interval_lens = length_mask.clone().float()

        if self.use_leaf_rnn:
            hs = []
            cs = []
            batch_size, max_length, _ = input.size()
            zero_state = Variable(input.data.new(batch_size, self.hidden_dim)
                                  .zero_())
            h_prev = c_prev = zero_state
            for i in range(max_length):
                h, c = self.leaf_rnn_cell(
                    input=input[:, i, :], hx=(h_prev, c_prev))
                hs.append(h)
                cs.append(c)
                h_prev = h
                c_prev = c
            hs = torch.stack(hs, dim=1)
            cs = torch.stack(cs, dim=1)

            if self.bidirectional:
                hs_bw = []
                cs_bw = []
                h_bw_prev = c_bw_prev = zero_state
                lengths_list = list(length.data)
                input_bw = basic.reverse_padded_sequence(
                    inputs=input, lengths=lengths_list, batch_first=True)
                for i in range(max_length):
                    h_bw, c_bw = self.leaf_rnn_cell_bw(
                        input=input_bw[:, i, :], hx=(h_bw_prev, c_bw_prev))
                    hs_bw.append(h_bw)
                    cs_bw.append(c_bw)
                    h_bw_prev = h_bw
                    c_bw_prev = c_bw
                hs_bw = torch.stack(hs_bw, dim=1)
                cs_bw = torch.stack(cs_bw, dim=1)
                hs_bw = basic.reverse_padded_sequence(
                    inputs=hs_bw, lengths=lengths_list, batch_first=True)
                cs_bw = basic.reverse_padded_sequence(
                    inputs=cs_bw, lengths=lengths_list, batch_first=True)
                hs = torch.cat([hs, hs_bw], dim=2)
                cs = torch.cat([cs, cs_bw], dim=2)
            state = (hs, cs, interval_lens)
        else:
            state = basic.apply_nd(fn=self.word_linear, input=input)
            state = state.chunk(num_chunks=2, dim=2) + (interval_lens,)
        nodes = []
        if self.intra_attention:
            nodes.append(state[0])
        for i in range(max_depth - 1):
            h, c, lens = state
            if c is not None:
                l = (h[:, :-1, :], c[:, :-1, :], lens[:, :-1])
                r = (h[:, 1:, :], c[:, 1:, :], lens[:, 1:])
            else:
                l = (h[:, :-1, :], None, lens[:, :-1])
                r = (h[:, 1:, :], None, lens[:, 1:])  
            new_state = self.treelstm_layer(l=l, r=r)
            if i < max_depth - 2:
                # We don't need to greedily select the composition in the
                # last iteration, since it has only one option left.
                new_h, new_c, new_lens, select_mask, selected_h = self.select_composition(
                    old_state=state, new_state=new_state,
                    mask=length_mask[:, i+1:])
                new_state = (new_h, new_c, new_lens)
                select_masks.append(select_mask.data) # store Tensor instead of Variable
                if self.intra_attention:
                    nodes.append(selected_h)
            done_mask = length_mask[:, i+1] # 0 means done and 1 means not done
            state = self.update_state(old_state=state, new_state=new_state,
                                      done_mask=done_mask)
            if self.intra_attention and i >= max_depth - 2:
                nodes.append(state[0])
        h, c, lens = state
        if self.intra_attention:
            att_mask = torch.cat([length_mask, length_mask[:, 1:]], dim=1)
            att_mask = att_mask.float()
            # nodes: (batch_size, num_tree_nodes, hidden_dim)
            nodes = torch.cat(nodes, dim=1)
            att_mask_expand = att_mask.unsqueeze(2).expand_as(nodes)
            nodes = nodes * att_mask_expand
            # nodes_mean: (batch_size, hidden_dim, 1)
            nodes_mean = nodes.mean(1).squeeze(1).unsqueeze(2)
            # att_weights: (batch_size, num_tree_nodes)
            att_weights = torch.bmm(nodes, nodes_mean).squeeze(2)
            att_weights = basic.masked_softmax(
                logits=att_weights, mask=att_mask)
            # att_weights_expand: (batch_size, num_tree_nodes, hidden_dim)
            att_weights_expand = att_weights.unsqueeze(2).expand_as(nodes)
            # h: (batch_size, 1, 2 * hidden_dim)
            h = (att_weights_expand * nodes).sum(1)
        assert h.size(1) == 1 and (c is None or c.size(1) == 1)
        if not return_select_masks:
            return h.squeeze(1), c
        else:
            return h.squeeze(1), c, select_masks
