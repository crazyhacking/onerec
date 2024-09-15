# coding: utf-8
# 
#
# user-graph need to be generated by the following script
# tools/generate-u-u-matrix.py
import os
import numpy as np
import scipy.sparse as sp
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import remove_self_loops, add_self_loops, degree
import torch_geometric
import math
from typing import Optional, Tuple, Union, List, Callable, Dict, Any
from torch.nn import LayerNorm
from common.abstract_recommender import GeneralRecommender
from common.loss import BPRLoss, EmbLoss
from common.init import xavier_uniform_initialization


class DRAGON(GeneralRecommender):
    def __init__(self, config, dataset):
        super(DRAGON, self).__init__(config, dataset)

        num_user = self.n_users
        num_item = self.n_items
        batch_size = config['train_batch_size']         # not used
        dim_x = config['embedding_size']
        self.feat_embed_dim = config['feat_embed_dim']
        self.n_layers = config['n_mm_layers']
        self.knn_k = config['knn_k']
        self.mm_image_weight = config['mm_image_weight']
       
        has_id = True

        self.batch_size = batch_size
        self.num_user = num_user
        self.num_item = num_item
        self.k = 40
        self.aggr_mode = config['aggr_mode']
        self.user_aggr_mode = 'softmax'
        self.num_layer = 1

        self.cold_start = 0
        self.dataset = dataset
        #self.construction = 'weighted_max'
        self.construction = 'cat'
        self.reg_weight = config['reg_weight']
        self.drop_rate = 0.1
        self.v_rep = None
        self.t_rep = None
        self.v_preference = None
        self.t_preference = None
        # self.dim_latent = 64
        # self.dim_feat = 128
        self.dim_latent = 256
        self.dim_feat = 128
        self.MLP_v = nn.Linear(self.dim_latent, self.dim_latent, bias=False)
        self.MLP_t = nn.Linear(self.dim_latent, self.dim_latent, bias=False)
        self.mm_adj = None

        # 实验新增参数
        self.modal_merge = True 
        self.trans_num_layer = 2
        self.use_transformer = True

        dataset_path = os.path.abspath(config['data_path'] + config['dataset'])
        self.user_graph_dict = np.load(os.path.join(dataset_path, config['user_graph_dict_file']), allow_pickle=True).item()
        
        mm_adj_file = os.path.join(dataset_path, 'mm_adj_{}.pt'.format(self.knn_k))

        # t_feat: torch.Size([7050, 4096]) torch.Size([7050, 384]) <class 'torch.Size'>
        print("t_feat:", self.v_feat.shape, self.t_feat.shape,  type(self.t_feat.shape))
        if self.v_feat is not None:
            self.image_embedding = nn.Embedding.from_pretrained(self.v_feat, freeze=False)
            self.image_trs = nn.Linear(self.v_feat.shape[1], self.feat_embed_dim)
        if self.t_feat is not None:
            self.text_embedding = nn.Embedding.from_pretrained(self.t_feat, freeze=False)
            self.text_trs = nn.Linear(self.t_feat.shape[1], self.feat_embed_dim)

        if os.path.exists(mm_adj_file):
            self.mm_adj = torch.load(mm_adj_file)
        else:
            
            if self.v_feat is not None:
                indices, image_adj = self.get_knn_adj_mat(self.image_embedding.weight.detach())
                self.mm_adj = image_adj
            if self.t_feat is not None:
                indices, text_adj = self.get_knn_adj_mat(self.text_embedding.weight.detach())
                self.mm_adj = text_adj
            
            if self.v_feat is not None and self.t_feat is not None:

                self.mm_adj = self.mm_image_weight * image_adj + (1.0 - self.mm_image_weight) * text_adj
                del text_adj
                del image_adj
            torch.save(self.mm_adj, mm_adj_file)

        # 修改1: 如果是多模型，则将想图片和文本一起转成维度相同，再执行concat操作
        if self.modal_merge:
            assert (self.v_feat is not None and self.t_feat is not None)
            self.modal_mlp = nn.Linear(self.feat_embed_dim*2, self.feat_embed_dim)
            print('************ modal_merge:')
          
            hidden_size = 256
            num_attention_heads = 8
            layernorm_epsilon = 1e-07
            hidden_size_per_attention_head = 32
            layernorm = LayerNorm
            inner_hidden_size = None
            use_bias = True
            params_dtype= torch.float
        
            def get_layer(layer_id):
                return GLMBlock(
                    hidden_size=hidden_size,
                    num_attention_heads=num_attention_heads,
                    layernorm_epsilon=layernorm_epsilon,
                    layer_id=layer_id,
                    inner_hidden_size=inner_hidden_size,
                    hidden_size_per_attention_head=hidden_size_per_attention_head,
                    layernorm=layernorm,
                    use_bias=use_bias,
                    params_dtype=params_dtype
                )

        self.layers = torch.nn.ModuleList(
            [get_layer(layer_id) for layer_id in range(self.trans_num_layer)]
        )
        # Final layer norm before output.
        self.final_layernorm = LayerNorm(hidden_size, eps=layernorm_epsilon)

        # packing interaction in training into edge_index
        train_interactions = dataset.inter_matrix(form='coo').astype(np.float32)
        edge_index = self.pack_edge_index(train_interactions)
        # edge_index (118551, 2)
        print("edge_index", edge_index.shape)
        self.edge_index = torch.tensor(edge_index, dtype=torch.long).t().contiguous().to(self.device)
        # self.edge_index torch.Size([2, 118551])
        print("self.edge_index", self.edge_index.shape)

        self.edge_index = torch.cat((self.edge_index, self.edge_index[[1, 0]]), dim=1)
        #  torch.Size([2, 237102])
        print("self.edge_index.cat", self.edge_index.shape)

        # pdb.set_trace()
        self.weight_u = nn.Parameter(nn.init.xavier_normal_(
            torch.tensor(np.random.randn(self.num_user, 2, 1), dtype=torch.float32, requires_grad=True)))
        self.weight_u.data = F.softmax(self.weight_u, dim=1)

        self.weight_i = nn.Parameter(nn.init.xavier_normal_(
            torch.tensor(np.random.randn(self.num_item, 2, 1), dtype=torch.float32, requires_grad=True)))
        self.weight_i.data = F.softmax(self.weight_i, dim=1)

        self.item_index = torch.zeros([self.num_item], dtype=torch.long)
        index = []
        for i in range(self.num_item):
            self.item_index[i] = i
            index.append(i)
        self.drop_percent = self.drop_rate
        self.single_percent = 1
        self.double_percent = 0

        drop_item = torch.tensor(
            np.random.choice(self.item_index, int(self.num_item * self.drop_percent), replace=False))
        drop_item_single = drop_item[:int(self.single_percent * len(drop_item))]

        self.dropv_node_idx_single = drop_item_single[:int(len(drop_item_single) * 1 / 3)]
        self.dropt_node_idx_single = drop_item_single[int(len(drop_item_single) * 2 / 3):]
        self.drop_node_idx_single = drop_item_single

        self.dropv_node_idx = self.dropv_node_idx_single
        self.dropt_node_idx = self.dropt_node_idx_single
        self.drop_node_idx = drop_item_single


        # mask_cnt: item下标， 记录每个item的度
        mask_cnt = torch.zeros(self.num_item, dtype=int).tolist()
        for edge in edge_index:
            mask_cnt[edge[1] - self.num_user] += 1
        
        # 构建保留与否向量
        mask_dropv = []
        mask_dropt = []
        mask_drop = []
        for idx, num in enumerate(mask_cnt):
            temp_false = [False] * num
            temp_true = [True] * num
            mask_dropv.extend(temp_false) if idx in self.dropv_node_idx else mask_dropv.extend(temp_true)
            mask_dropt.extend(temp_false) if idx in self.dropt_node_idx else mask_dropt.extend(temp_true)
            mask_drop.extend(temp_false) if idx in self.drop_node_idx else mask_drop.extend(temp_true)

        edge_index = edge_index[np.lexsort(edge_index.T[1, None])]
        edge_index_dropv = edge_index[mask_dropv]
        edge_index_dropt = edge_index[mask_dropt]
        edge_index_drop = edge_index[mask_drop]

        # edge_index_dropt， edge_index_dropv 为抽样之后的值
        self.edge_index_dropv = torch.tensor(edge_index_dropv).t().contiguous().to(self.device)
        self.edge_index_dropt = torch.tensor(edge_index_dropt).t().contiguous().to(self.device)
        self.edge_index_drop = torch.tensor(edge_index_drop).t().contiguous().to(self.device)


        self.edge_index_dropv = torch.cat((self.edge_index_dropv, self.edge_index_dropv[[1, 0]]), dim=1)
        self.edge_index_dropt = torch.cat((self.edge_index_dropt, self.edge_index_dropt[[1, 0]]), dim=1)
        self.edge_index_drop = torch.cat((self.edge_index_drop, self.edge_index_drop[[1, 0]]), dim=1)

        # edge_index_dropt (114125, 2)
        print("edge_index_dropt", edge_index_dropt.shape)
        self.MLP_user = nn.Linear(self.dim_latent * 2, self.dim_latent)

        if self.modal_merge:
            self.modal_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x * 2, self.aggr_mode,
                         num_layer=self.num_layer, has_id=has_id, dropout=self.drop_rate, dim_latent=self.dim_latent,
                         device=self.device, features=None)  # 256)
            self.v_drop_ze = torch.zeros(len(self.dropv_node_idx), self.v_feat.size(1)).to(self.device)
            self.t_drop_ze = torch.zeros(len(self.dropt_node_idx), self.t_feat.size(1)).to(self.device)
        else:
            if self.v_feat is not None:
                self.v_drop_ze = torch.zeros(len(self.dropv_node_idx), self.v_feat.size(1)).to(self.device)
                self.v_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode,
                            num_layer=self.num_layer, has_id=has_id, dropout=self.drop_rate, dim_latent=self.dim_latent,
                            device=self.device, features=self.v_feat)  # 256)
            if self.t_feat is not None:
                self.t_drop_ze = torch.zeros(len(self.dropt_node_idx), self.t_feat.size(1)).to(self.device)
                self.t_gcn = GCN(self.dataset, batch_size, num_user, num_item, dim_x, self.aggr_mode,
                            num_layer=self.num_layer, has_id=has_id, dropout=self.drop_rate, dim_latent=self.dim_latent,
                            device=self.device, features=self.t_feat)

        self.user_graph = User_Graph_sample(num_user, 'add', self.dim_latent)

        self.result_embed = nn.Parameter(nn.init.xavier_normal_(torch.tensor(np.random.randn(num_user + num_item, dim_x)))).to(self.device)

    def get_knn_adj_mat(self, mm_embeddings):
        context_norm = mm_embeddings.div(torch.norm(mm_embeddings, p=2, dim=-1, keepdim=True))
        print("mm_embeddings.shape:", mm_embeddings.shape, "context_norm.shape", context_norm.shape)
        sim = torch.mm(context_norm, context_norm.transpose(1, 0))
        _, knn_ind = torch.topk(sim, self.knn_k, dim=-1)
        adj_size = sim.size()
        print("sim.shape:", sim.shape, "knn_ind.shape", knn_ind.shape)

        del sim
       
        # construct sparse adj
        indices0 = torch.arange(knn_ind.shape[0]).to(self.device)
        print("indices0.arange:", indices0.shape)

        indices0 = torch.unsqueeze(indices0, 1)
        print("indices0.unsqueeze:", indices0.shape)

        indices0 = indices0.expand(-1, self.knn_k)
        print("indices0.expand:", indices0.shape)

        # indices0.expand: torch.Size([7050, 5]), knn_ind.shape torch.Size([7050, 5])
        indices = torch.stack((torch.flatten(indices0), torch.flatten(knn_ind)), 0)

        # indices0.shape: torch.Size([7050, 5]) indices.shape torch.Size([2, 35250])
        print("indices0.shape:", indices0.shape, "indices.shape", indices.shape)
        # norm
        return indices, self.compute_normalized_laplacian(indices, adj_size)
    
    def compute_normalized_laplacian(self, indices, adj_size):
        # indices.shape torch.Size([2, 35250]), adj_size: torch.Size([7050, 5])
        adj = torch.sparse.FloatTensor(indices, torch.ones_like(indices[0]), adj_size)
        row_sum = 1e-7 + torch.sparse.sum(adj, -1).to_dense()
        r_inv_sqrt = torch.pow(row_sum, -0.5)
        rows_inv_sqrt = r_inv_sqrt[indices[0]]
        cols_inv_sqrt = r_inv_sqrt[indices[1]]
        values = rows_inv_sqrt * cols_inv_sqrt

        # compute_normalized_laplacian.shape: torch.Size([2, 35250]) torch.Size([35250]) torch.Size([7050, 7050])
        print("compute_normalized_laplacian.shape:", adj.shape, indices.shape, values.shape, adj_size)

        return torch.sparse.FloatTensor(indices, values, adj_size)

    
    def pre_epoch_processing(self):
        self.epoch_user_graph, self.user_weight_matrix = self.topk_sample(self.k)
        self.user_weight_matrix = self.user_weight_matrix.to(self.device)

    def pack_edge_index(self, inter_mat):
        rows = inter_mat.row
        cols = inter_mat.col + self.n_users
        # ndarray([598918, 2]) for ml-imdb
        return np.column_stack((rows, cols))

    def forward(self, interaction):
        user_nodes, pos_item_nodes, neg_item_nodes = interaction[0], interaction[1], interaction[2]
        pos_item_nodes += self.n_users
        neg_item_nodes += self.n_users
        representation = None

        if self.modal_merge:
            image_h = self.image_trs(self.v_feat)
            text_h = self.text_trs(self.t_feat)
            
            self.feat = self.modal_mlp(torch.cat((image_h, text_h), dim=1))
            # self.feat torch.Size([7050, 64])
            # print("self.feat", self.feat.shape)
            self.rep, self.preference = self.modal_gcn(self.edge_index_drop, self.edge_index, self.feat)
            representation = self.rep
            user_rep = self.rep[:self.num_user]
            user_rep = torch.squeeze(user_rep)
        else:
            
            if self.v_feat is not None:
                self.v_rep, self.v_preference = self.v_gcn(self.edge_index_dropv, self.edge_index, self.v_feat)
                representation = self.v_rep
            if self.t_feat is not None:
                self.t_rep, self.t_preference = self.t_gcn(self.edge_index_dropt, self.edge_index, self.t_feat)
                if representation is None:
                    representation = self.t_rep
                else:
                    if self.construction == 'cat':
                        representation = torch.cat((self.v_rep, self.t_rep), dim=1)
                    else:
                        representation += self.t_rep


            if self.construction == 'weighted_sum':
                if self.v_rep is not None:
                    self.v_rep = torch.unsqueeze(self.v_rep, 2)
                    user_rep = self.v_rep[:self.num_user]
                if self.t_rep is not None:
                    self.t_rep = torch.unsqueeze(self.t_rep, 2)
                    user_rep = self.t_rep[:self.num_user]
                if self.v_rep is not None and self.t_rep is not None:
                    
                    user_rep = torch.matmul(torch.cat((self.v_rep[:self.num_user], self.t_rep[:self.num_user]), dim=2),
                                            self.weight_u)
                user_rep = torch.squeeze(user_rep)

            if self.construction == 'weighted_max':
                # pdb.set_trace()
                self.v_rep = torch.unsqueeze(self.v_rep, 2)
                
                self.t_rep = torch.unsqueeze(self.t_rep, 2)
                
                user_rep = torch.cat((self.v_rep[:self.num_user], self.t_rep[:self.num_user]), dim=2)
                user_rep = self.weight_u.transpose(1,2)*user_rep
                user_rep = torch.max(user_rep,dim=2).values
            if self.construction == 'cat':
                # pdb.set_trace()
                if self.v_rep is not None:
                    user_rep = self.v_rep[:self.num_user]
                if self.t_rep is not None:
                    user_rep = self.t_rep[:self.num_user]
                if self.v_rep is not None and self.t_rep is not None:
                    self.v_rep = torch.unsqueeze(self.v_rep, 2)
                    self.t_rep = torch.unsqueeze(self.t_rep, 2)
                    user_rep = torch.cat((self.v_rep[:self.num_user], self.t_rep[:self.num_user]), dim=2)
                    user_rep = self.weight_u.transpose(1,2)*user_rep

                    user_rep = torch.cat((user_rep[:,:,0], user_rep[:,:,1]), dim=1)

        item_rep = representation[self.num_user:]

        ############################################ multi-modal information aggregation
        h = item_rep
        # print('device:', self.mm_adj.device, h.device)  # 输出：cpu
        for i in range(self.n_layers):
            h = torch.sparse.mm(self.mm_adj, h)
        h_u1 = self.user_graph(user_rep, self.epoch_user_graph, self.user_weight_matrix)
        user_rep = user_rep + h_u1
        item_rep = item_rep + h
        # print("user_rep", user_rep.shape, user_rep.dtype, item_rep.shape, item_rep.dtype)
        self.result_embed = torch.cat((user_rep, item_rep), dim=0)

        # representation: torch.Size([26495, 256])
        # print("representation:", representation.shape)
        if self.use_transformer:
            hidden_states = representation.unsqueeze(0)
            # hidden_states: torch.Size([1, 26495, 256])
            # print("hidden_states:", hidden_states.shape)

            for i, layer in enumerate(self.layers):
                layer_ret = layer(
                    hidden_states,
                    attention_mask=None,
                    layer_id=torch.tensor(i),
                    layer_past=None,
                    use_cache=None,
                    output_attentions=None
                )
                hidden_states = layer_ret[0]

            # Final layer norm.
            hidden_states = self.final_layernorm(hidden_states)
            self.result_embed = hidden_states[0]
        
        # self.result_embed = nn.Parameter(torch.cat((user_rep, item_rep), dim=0))
        user_tensor = self.result_embed[user_nodes]
        pos_item_tensor = self.result_embed[pos_item_nodes]
        neg_item_tensor = self.result_embed[neg_item_nodes]
        pos_scores = torch.sum(user_tensor * pos_item_tensor, dim=1)
        neg_scores = torch.sum(user_tensor * neg_item_tensor, dim=1)
        return pos_scores, neg_scores

    def calculate_loss(self, interaction):
        user = interaction[0]
        pos_scores, neg_scores = self.forward(interaction)
        loss_value = -torch.mean(torch.log2(torch.sigmoid(pos_scores - neg_scores)))
        reg_embedding_loss_v = (self.v_preference[user] ** 2).mean() if self.v_preference is not None else 0.0
        reg_embedding_loss_t = (self.t_preference[user] ** 2).mean() if self.t_preference is not None else 0.0

        reg_loss = self.reg_weight * (reg_embedding_loss_v + reg_embedding_loss_t)
        if self.construction == 'weighted_sum':
            reg_loss += self.reg_weight * (self.weight_u ** 2).mean()
            reg_loss += self.reg_weight * (self.weight_i ** 2).mean()
        elif self.construction == 'cat':
            reg_loss += self.reg_weight * (self.weight_u ** 2).mean()
        elif self.construction == 'cat_mlp':
            reg_loss += self.reg_weight * (self.MLP_user.weight ** 2).mean()
        return loss_value + reg_loss

    def full_sort_predict(self, interaction):
        user_tensor = self.result_embed[:self.n_users]
        item_tensor = self.result_embed[self.n_users:]

        temp_user_tensor = user_tensor[interaction[0], :]
        score_matrix = torch.matmul(temp_user_tensor, item_tensor.t())
        return score_matrix

    def topk_sample(self, k):
        user_graph_index = []
        count_num = 0
        user_weight_matrix = torch.zeros(len(self.user_graph_dict), k)
        tasike = []
        for i in range(k):
            tasike.append(0)
        for i in range(len(self.user_graph_dict)):
            if len(self.user_graph_dict[i][0]) < k:
                count_num += 1
                if len(self.user_graph_dict[i][0]) == 0:
                    # pdb.set_trace()
                    user_graph_index.append(tasike)
                    continue
                user_graph_sample = self.user_graph_dict[i][0][:k]
                user_graph_weight = self.user_graph_dict[i][1][:k]
                while len(user_graph_sample) < k:
                    rand_index = np.random.randint(0, len(user_graph_sample))
                    user_graph_sample.append(user_graph_sample[rand_index])
                    user_graph_weight.append(user_graph_weight[rand_index])
                user_graph_index.append(user_graph_sample)


                if self.user_aggr_mode == 'softmax':
                    user_weight_matrix[i] = F.softmax(torch.tensor(user_graph_weight), dim=0)  # softmax
                if self.user_aggr_mode == 'mean':
                    user_weight_matrix[i] = torch.ones(k) / k  # mean
                continue
            user_graph_sample = self.user_graph_dict[i][0][:k]
            user_graph_weight = self.user_graph_dict[i][1][:k]

            if self.user_aggr_mode == 'softmax':
                user_weight_matrix[i] = F.softmax(torch.tensor(user_graph_weight), dim=0)  # softmax
            if self.user_aggr_mode == 'mean':
                user_weight_matrix[i] = torch.ones(k) / k  # mean
            user_graph_index.append(user_graph_sample)

        # pdb.set_trace()
        return user_graph_index, user_weight_matrix

class User_Graph_sample(torch.nn.Module):
    def __init__(self, num_user, aggr_mode,dim_latent):
        super(User_Graph_sample, self).__init__()
        self.num_user = num_user
        self.dim_latent = dim_latent
        self.aggr_mode = aggr_mode

    def forward(self, features,user_graph,user_matrix):
        index = user_graph
        u_features = features[index]
        # u_features.shape. torch.Size([19445, 40, 128])
        # print("u_features.shape.", u_features.shape)
        user_matrix = user_matrix.unsqueeze(1)
        # pdb.set_trace()
        u_pre = torch.matmul(user_matrix,u_features)
        # u_pre.shape. torch.Size([19445, 1, 128])
        # print("u_pre.shape.", u_pre.shape)
        u_pre = u_pre.squeeze()

        # u_pre.squeeze.shape. torch.Size([19445, 128])
        # print("u_pre.squeeze.shape.", u_pre.shape)
        return u_pre


class GCN(torch.nn.Module):
    def __init__(self,datasets, batch_size, num_user, num_item, dim_id, aggr_mode, num_layer, has_id, dropout,
                 dim_latent=None,device = None,features=None):
        super(GCN, self).__init__()
        self.batch_size = batch_size
        self.num_user = num_user
        self.num_item = num_item
        self.datasets = datasets
        self.dim_id = dim_id
        if features is None:
            self.dim_feat = dim_latent
        else:
            self.dim_feat = features.size(1)
        self.dim_latent = dim_latent
        self.aggr_mode = aggr_mode
        self.num_layer = num_layer
        self.has_id = has_id
        self.dropout = dropout
        self.device = device

        if self.dim_latent:
            self.preference = nn.Parameter(nn.init.xavier_normal_(torch.tensor(
                np.random.randn(num_user, self.dim_latent), dtype=torch.float32, requires_grad=True),
                gain=1).to(self.device))
            self.MLP = nn.Linear(self.dim_feat, 4*self.dim_latent)
            self.MLP_1 = nn.Linear(4*self.dim_latent, self.dim_latent)
            self.conv_embed_1 = Base_gcn(self.dim_latent, self.dim_latent, aggr=self.aggr_mode)

        else:
            self.preference = nn.Parameter(nn.init.xavier_normal_(torch.tensor(
                np.random.randn(num_user, self.dim_feat), dtype=torch.float32, requires_grad=True),
                gain=1).to(self.device))
            self.conv_embed_1 = Base_gcn(self.dim_latent, self.dim_latent, aggr=self.aggr_mode)

    def forward(self, edge_index_drop, edge_index, features):
        # features.. torch.Size([7050, 64]) 4096 64
        # print("features..", features.shape, self.dim_feat, self.dim_latent)
        # features: item multimodal feature
        temp_features = self.MLP_1(F.leaky_relu(self.MLP(features))) if self.dim_latent else features
        x = torch.cat((self.preference, temp_features), dim=0).to(self.device)
        # x: user + item
        x = F.normalize(x).to(self.device)
        h = self.conv_embed_1(x, edge_index)  # equation 1
        h_1 = self.conv_embed_1(h, edge_index)

        x_hat =h + x +h_1
        return x_hat, self.preference


class Base_gcn(MessagePassing):
    def __init__(self, in_channels, out_channels, normalize=True, bias=True, aggr='add', **kwargs):
        super(Base_gcn, self).__init__(aggr=aggr, **kwargs)
        self.aggr = aggr
        self.in_channels = in_channels
        self.out_channels = out_channels

    def forward(self, x, edge_index, size=None):
        # pdb.set_trace()
        if size is None:
            edge_index, _ = remove_self_loops(edge_index)
            # edge_index, _ = add_self_loops(edge_index, num_nodes=x.size(0))
        x = x.unsqueeze(-1) if x.dim() == 1 else x
        # base.gcn.forward.x.shape: torch.Size([26495, 64])
        # print("base.gcn.forward.x.shape:", x.shape)
        # pdb.set_trace()
        # propagate: message() -> aggregate() -> update()
        return self.propagate(edge_index, size=(x.size(0), x.size(0)), x=x)

    def message(self, x_j, edge_index, size):
        if self.aggr == 'add':
            # pdb.set_trace()
            row, col = edge_index
            deg = degree(row, size[0], dtype=x_j.dtype)
            deg_inv_sqrt = deg.pow(-0.5)
            norm = deg_inv_sqrt[row] * deg_inv_sqrt[col]
            # print("message.x_j.shape:", deg.shape, x_j.shape, norm.view(-1, 1).shape, edge_index.shape, size)
            return norm.view(-1, 1) * x_j
        # message.x_j.shape: torch.Size([26495]) torch.Size([237102, 64]) torch.Size([237102, 1]) torch.Size([2, 237102]) [26495, 26495]
        # print("message.x_j.shape:", x_j.shape, edge_index.shape, size)
        return x_j

    def update(self, aggr_out):
        return aggr_out

    def __repr(self):
        return '{}({},{})'.format(self.__class__.__name__, self.in_channels, self.out_channels)


@torch.jit.script
def gelu_impl(x):
    """OpenAI's gelu implementation."""
    return 0.5 * x * (1.0 + torch.tanh(0.7978845608028654 * x *
                                       (1.0 + 0.044715 * x * x)))


def gelu(x):
    return gelu_impl(x)

def attention_fn(
        self,
        query_layer,
        key_layer,
        value_layer,
        attention_mask,
        hidden_size_per_partition,
        layer_id,
        layer_past=None,
        scaling_attention_score=True,
        use_cache=False,
):
    if layer_past is not None:
        past_key, past_value = layer_past[0], layer_past[1]
        key_layer = torch.cat((past_key, key_layer), dim=0)
        value_layer = torch.cat((past_value, value_layer), dim=0)

    # seqlen, batch, num_attention_heads, hidden_size_per_attention_head
    seq_len, b, nh, hidden_size = key_layer.shape

    if use_cache:
        present = (key_layer, value_layer)
    else:
        present = None

    query_key_layer_scaling_coeff = float(layer_id + 1)
    if scaling_attention_score:
        query_layer = query_layer / (math.sqrt(hidden_size) * query_key_layer_scaling_coeff)

    # ===================================
    # Raw attention scores. [b, np, s, s]
    # ===================================

    # [b, np, sq, sk]
    output_size = (query_layer.size(1), query_layer.size(2), query_layer.size(0), key_layer.size(0))

    # [sq, b, np, hn] -> [sq, b * np, hn]
    query_layer = query_layer.view(output_size[2], output_size[0] * output_size[1], -1)
    # [sk, b, np, hn] -> [sk, b * np, hn]
    key_layer = key_layer.view(output_size[3], output_size[0] * output_size[1], -1)

    matmul_result = torch.zeros(
        1, 1, 1,
        dtype=query_layer.dtype,
        device=query_layer.device,
    )

    matmul_result = torch.baddbmm(
        matmul_result,
        query_layer.transpose(0, 1),  # [b * np, sq, hn]
        key_layer.transpose(0, 1).transpose(1, 2),  # [b * np, hn, sk]
        beta=0.0,
        alpha=1.0,
    )

    # change view to [b, np, sq, sk]
    attention_scores = matmul_result.view(*output_size)
    dtype = attention_scores.dtype
    attention_scores = attention_scores.float()
    attention_scores = attention_scores * query_key_layer_scaling_coeff

    attention_probs = F.softmax(attention_scores, dim=-1)

    attention_probs = attention_probs.type(dtype)

    # =========================
    # Context layer. [sq, b, hp]
    # =========================

    # value_layer -> context layer.
    # [sk, b, np, hn] --> [b, np, sq, hn]

    # context layer shape: [b, np, sq, hn]
    output_size = (value_layer.size(1), value_layer.size(2), query_layer.size(0), value_layer.size(3))

    # change view [sk, b * np, hn]
    value_layer = value_layer.view(value_layer.size(0), output_size[0] * output_size[1], -1)

    # change view [b * np, sq, sk]
    attention_probs = attention_probs.view(output_size[0] * output_size[1], output_size[2], -1)

    # matmul: [b * np, sq, hn]
    context_layer = torch.bmm(attention_probs, value_layer.transpose(0, 1))

    # change view [b, np, sq, hn]
    context_layer = context_layer.view(*output_size)

    # [b, np, sq, hn] --> [sq, b, np, hn]
    context_layer = context_layer.permute(2, 0, 1, 3).contiguous()

    # [sq, b, np, hn] --> [sq, b, hp]
    new_context_layer_shape = context_layer.size()[:-2] + (hidden_size_per_partition,)
    context_layer = context_layer.view(*new_context_layer_shape)

    outputs = (context_layer, present, attention_probs)

    return outputs


def default_init(cls, *args, **kwargs):
    return cls(*args, **kwargs)


class SelfAttention(torch.nn.Module):
    def __init__(self, hidden_size, num_attention_heads,
                 layer_id, hidden_size_per_attention_head=None, bias=True,
                 params_dtype=torch.float):

        init_method = default_init
        super(SelfAttention, self).__init__()

        self.layer_id = layer_id
        self.hidden_size = hidden_size
        self.hidden_size_per_partition = hidden_size
        self.num_attention_heads = num_attention_heads
        self.num_attention_heads_per_partition = num_attention_heads
       
        self.scale_mask_softmax = None

        if hidden_size_per_attention_head is None:
            self.hidden_size_per_attention_head = hidden_size // num_attention_heads
        else:
            self.hidden_size_per_attention_head = hidden_size_per_attention_head

        self.inner_hidden_size = num_attention_heads * self.hidden_size_per_attention_head

        # Strided linear layer.
        self.query_key_value = init_method(
            torch.nn.Linear,
            hidden_size,
            3 * self.inner_hidden_size,
            bias=bias,
            dtype=params_dtype,
        )

        self.dense = init_method(
            torch.nn.Linear,
            self.inner_hidden_size,
            hidden_size,
            bias=bias,
            dtype=params_dtype,
        )

    @staticmethod
    def attention_mask_func(attention_scores, attention_mask):
        attention_scores.masked_fill_(attention_mask, -10000.0)
        return attention_scores

    def split_tensor_along_last_dim(self, tensor, num_partitions,
                                    contiguous_split_chunks=False):
        """Split a tensor along its last dimension.
        Arguments:
            tensor: input tensor.
            num_partitions: number of partitions to split the tensor
            contiguous_split_chunks: If True, make each chunk contiguous
                                    in memory.
        """
        # Get the size and dimension.
        last_dim = tensor.dim() - 1
        last_dim_size = tensor.size()[last_dim] // num_partitions
        # Split.
        tensor_list = torch.split(tensor, last_dim_size, dim=last_dim)
        # Note: torch.split does not create contiguous tensors by default.
        if contiguous_split_chunks:
            return tuple(chunk.contiguous() for chunk in tensor_list)

        return tensor_list

    def forward(
            self,
            hidden_states: torch.Tensor,
            attention_mask: torch.Tensor,
            layer_id,
            layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
            use_cache: bool = False,
            output_attentions: bool = False,
    ):
        """
        hidden_states: [seq_len, batch, hidden_size]
        attention_mask: [(1, 1), seq_len, seq_len]
        """

        # [seq_len, batch, 3 * hidden_size]
        mixed_raw_layer = self.query_key_value(hidden_states)

        # [seq_len, batch, 3 * hidden_size] --> [seq_len, batch, num_attention_heads, 3 * hidden_size_per_attention_head]
        new_tensor_shape = mixed_raw_layer.size()[:-1] + (
            self.num_attention_heads_per_partition,
            3 * self.hidden_size_per_attention_head,
        )
        mixed_raw_layer = mixed_raw_layer.view(*new_tensor_shape)

        # [seq_len, batch, num_attention_heads, hidden_size_per_attention_head]
        (query_layer, key_layer, value_layer) = self.split_tensor_along_last_dim(mixed_raw_layer, 3)

        # [seq_len, batch, hidden_size]
        context_layer, present, attention_probs = attention_fn(
            self=self,
            query_layer=query_layer,
            key_layer=key_layer,
            value_layer=value_layer,
            attention_mask=attention_mask,
            hidden_size_per_partition=self.hidden_size_per_partition,
            layer_id=layer_id,
            layer_past=layer_past,
            use_cache=use_cache
        )

        output = self.dense(context_layer)

        outputs = (output, present)

        if output_attentions:
            outputs += (attention_probs,)

        return outputs  # output, present, attention_probs


class GEGLU(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.activation_fn = F.gelu

    def forward(self, x):
        # dim=-1 breaks in jit for pt<1.10
        x1, x2 = x.chunk(2, dim=(x.ndim - 1))
        return x1 * self.activation_fn(x2)


class GLU(torch.nn.Module):
    def __init__(self, hidden_size, inner_hidden_size=None,
                 layer_id=None, bias=True, activation_func=gelu, params_dtype=torch.float):
        super(GLU, self).__init__()
       
        init_method = default_init
        self.layer_id = layer_id
        self.activation_func = activation_func

        # Project to 4h.
        self.hidden_size = hidden_size
        if inner_hidden_size is None:
            inner_hidden_size = 4 * hidden_size
        self.inner_hidden_size = inner_hidden_size
        self.dense_h_to_4h = init_method(
            torch.nn.Linear,
            self.hidden_size,
            self.inner_hidden_size,
            bias=bias,
            dtype=params_dtype,
        )
        # Project back to h.
        self.dense_4h_to_h = init_method(
            torch.nn.Linear,
            self.inner_hidden_size,
            self.hidden_size,
            bias=bias,
            dtype=params_dtype,
        )

    def forward(self, hidden_states):
        """
        hidden_states: [seq_len, batch, hidden_size]
        """

        # [seq_len, batch, inner_hidden_size]
        intermediate_parallel = self.dense_h_to_4h(hidden_states)

        intermediate_parallel = self.activation_func(intermediate_parallel)

        output = self.dense_4h_to_h(intermediate_parallel)

        return output


class GLMBlock(torch.nn.Module):
    def __init__(
            self,
            hidden_size,
            num_attention_heads,
            layernorm_epsilon,
            layer_id,
            inner_hidden_size=None,
            hidden_size_per_attention_head=None,
            layernorm=LayerNorm,
            use_bias=True,
            params_dtype=torch.float,
            num_layers=28
    ):
        super(GLMBlock, self).__init__()
        # Set output layer initialization if not provided.

        self.layer_id = layer_id

        # Layernorm on the input data.
        self.input_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

        # Self attention.
        self.attention = SelfAttention(
            hidden_size,
            num_attention_heads,
            layer_id,
            hidden_size_per_attention_head=hidden_size_per_attention_head,
            bias=use_bias,
            params_dtype=params_dtype
        )

        # Layernorm on the input data.
        self.post_attention_layernorm = layernorm(hidden_size, eps=layernorm_epsilon)

        self.num_layers = num_layers

        # GLU
        self.mlp = GLU(
            hidden_size,
            inner_hidden_size=inner_hidden_size,
            bias=use_bias,
            layer_id=layer_id,
            params_dtype=params_dtype
        )

    def forward(
            self,
            hidden_states: torch.Tensor,
            attention_mask: torch.Tensor,
            layer_id,
            layer_past: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
            use_cache: bool = False,
            output_attentions: bool = False,
    ):
        """
        hidden_states: [seq_len, batch, hidden_size]
        attention_mask: [(1, 1), seq_len, seq_len]
        """

        # Layer norm at the begining of the transformer layer.
        # [seq_len, batch, hidden_size]
        attention_input = self.input_layernorm(hidden_states)

        # Self attention.
        attention_outputs = self.attention(
            attention_input,
            attention_mask=attention_mask,
            layer_id=layer_id,
            layer_past=layer_past,
            use_cache=use_cache,
            output_attentions=output_attentions
        )

        attention_output = attention_outputs[0]

        outputs = attention_outputs[1:]

        # Residual connection.
        alpha = (2 * self.num_layers) ** 0.5
        hidden_states = attention_input * alpha + attention_output

        mlp_input = self.post_attention_layernorm(hidden_states)

        # MLP.
        mlp_output = self.mlp(mlp_input)

        # Second residual connection.
        output = mlp_input * alpha + mlp_output

        if use_cache:
            outputs = (output,) + outputs
        else:
            outputs = (output,) + outputs[1:]

        return outputs  # hidden_states, present, attentions

