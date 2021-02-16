from alphafold2_pytorch.alphafold2 import exists, partial, PreNorm, FeedForward
from alphafold2_pytorch import constants
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from einops import rearrange
from torch import nn, einsum
from torch.utils.data import Dataset

from alphafold2_pytorch import Alphafold2

# n: 20171106 : changed-to: '.' = 0
AA = {
    '.': 32,
    'A': 1,
    'C': 2,
    'D': 3,
    'E': 4,
    'F': 5,
    'G': 6,
    'H': 7,
    'I': 8,
    'K': 9,
    'L': 10,
    'M': 11,
    'N': 12,
    'P': 13,
    'Q': 14,
    'R': 15,
    'S': 16,
    'T': 17,
    'V': 18,
    'W': 19,
    'Y': 20,
    'B': 21,
    'J': 21,
    'O': 21,
    'U': 21,
    'X': 21,
    'Z': 21,
}

GRADIENT_ACCUMULATE_EVERY = 16
LEARNING_RATE = 3e-5
NUM_SS_CLASS = 3


def cycle(loader, cond=lambda x: True):
    while True:
        for data in loader:
            if not cond(data):
                continue
            yield data


class SampleHackDataset(Dataset):
    def __init__(self, meta, msa, seq, sst) -> None:
        super(SampleHackDataset, self).__init__()
        # TODO
        """
        1. load a meta df with all the ids and meta
        2. __getitem__(n) read n-th (seq, msa), sst from filesystem
        3. __dict__(key) read id -> (seq, msa), sst
        """
        self.meta = pd.read_csv(meta, index_col=0).reset_index(drop=True)
        self.msa_root = msa
        self.seq_root = seq
        self.sst_root = sst

    def __getitem__(self, index):
        item = self.meta.iloc[index, :]
        return self.read_from_disk_return_x_y(item)

    def read_from_disk_return_x_y(self, item):
        id = item["id"]
        seq_len = item["seq_len"]
        msa_depth = item["msa_depth"]
        with open(self.seq_root / f"{id}.seq", "rb") as f:
            seq = np.load(f)
            f.close()
        with open(self.msa_root / f"{id}.msa", "rb") as f:
            msa = np.load(f)
            f.close()
        with open(self.sst_root / f"{id}.sst", "rb") as f:
            sst = np.load(f)
            f.close()

        #
        return id, seq_len, msa_depth, seq, msa, sst

    def __len__(self):
        return self.meta.shape[0]

    def query(self, *args):
        return self.meta.query(*args)


class SecondaryAttention(nn.Module):
    def __init__(
        self,
        in_dim,
        out_dim,
        heads = 8,
        dim_head = 8,
        ff_dropout = 0.,
    ):
        super().__init__()
        inner_dim = dim_head * heads
        self.heads = heads
        self.scale = dim_head ** -0.5
        self.dropout = nn.Dropout(ff_dropout)
        self.to_q = nn.Linear(in_dim, inner_dim, bias =False)
        self.to_kv = nn.Linear(in_dim, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, out_dim)
        self.norm = nn.LayerNorm(in_dim)
    def forward(self, x):
        device, orig_shape, h = x.device, x.shape, self.heads
        x = self.norm(x)
        q, k, v = (self.to_q(x), *self.to_kv(x).chunk(2, dim = -1))
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))
        
        dots = einsum('b h i d, b h j d -> b h i j', q, k) * self.scale
        
        attn = dots.softmax(dim = -1)
        attn = self.dropout(attn)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

class SSModule(nn.Module):
    def __init__(self, num_class, **kwargs):
        super().__init__()
        ff_dropout = kwargs.get('ff_dropout', 0.)
        prenorm_256 = partial(PreNorm, 256)
        prenorm_64 = partial(PreNorm, 64)
        prenorm_8 = partial(PreNorm, 8)

        self.q1 = SecondaryAttention(in_dim=256, out_dim=64, heads=8, dim_head=8, ff_dropout=ff_dropout)
        self.ff1 = FeedForward(dim=64, dropout = ff_dropout)
        self.q2 = SecondaryAttention(in_dim=64, out_dim=8, heads=4, dim_head=2, ff_dropout=ff_dropout)
        self.ff2 = FeedForward(dim=8, dropout= ff_dropout)

        self.out = nn.Sequential(nn.LayerNorm(8), nn.Linear(8, num_class))
        

    def forward(self, x):
        # print(f'pre_ssp_forward: x.shape -> {x.shape}')
        x = self.q1(x)
        # print(f'post_ssp_q1: x.shape -> {x.shape}')
        x = self.ff1(x)
        # print(f'post_ssp_ff1: x.shape -> {x.shape}')
        x = self.q2(x)
        # print(f'post_ssp_q2: x.shape -> {x.shape}')
        x = self.ff2(x)
        # print(f'post_ssp_ff1: x.shape -> {x.shape}')
        x = self.out(x)
        # print(f'post_ssp_out: x.shape -> {x.shape}')
        return x



def rand_choice(orginal_size: int, target_size: int, container):
    idxs = np.random.choice(orginal_size, target_size)
    container.extend(idxs)
    return idxs


def rand_chunk(max_depth, size, container):
    start = np.random.randint(low=0, high=max_depth - size - 1)
    res = range(start, start + size)
    container.extend([start])
    return res


def test(root: str):
    root = Path(root)
    device = torch.device('cuda:0')
    ds = SampleHackDataset(root / "sample.csv", root / "msa", root / "seq", root / "sst")
    af2 = Alphafold2(
        dim=256,
        depth=12,
        heads=8,
        dim_head=64,
        num_tokens=constants.NUM_AMINO_ACIDS_EXP,
        sparse_self_attn=(True, False) * 3,
        cross_attn_compress_ratio=3,
        reversible=True,
    ).cuda()
    ssp = SSModule(num_class=NUM_SS_CLASS).cuda()

    optim = torch.optim.Adam(list(af2.parameters()) + list(ssp.parameters()), lr=LEARNING_RATE)
    lossFn = nn.CrossEntropyLoss()
    from torch.utils.tensorboard import SummaryWriter
    writer = SummaryWriter()

    dl = cycle(ds, lambda x: x[2] > 1)

    step = 0
    global_counter = 0
    gradient_counter = 0

    def accumulated_enough(acc_size, curr):
        if curr >= acc_size:
            return True
        return False

    while True:
        step += 1
        id, seq_len, msa_depth, seq, msa, sst = next(dl)
        # preprocessing of seq, msa and sst
        # print(id, seq_len, msa_depth, seq.shape, msa.shape, sst.shape)

        # need seq to be shape of (1, l), msa of (1, l, d), sst of (1, l)

        seq = torch.tensor([int(AA[x]) for x in seq], dtype=torch.long)
        msa = torch.tensor([[int(AA[x]) for x in _seq] for _seq in msa], dtype=torch.long)
        sst = torch.tensor(sst, dtype=torch.long)
        print(f"id {id}: seq {seq.dtype}{seq.shape} msa {msa.dtype}{msa.shape} sst {sst.dtype}{sst.shape}")

        # seq = pad(seq, MAX_SEQ_LEN, b, seq_len)
        # msa = pad(msa, MAX_SEQ_LEN, b, seq_len, msa_depth=msa_depth)
        # sst = pad(sst, MAX_SEQ_LEN, b, seq_len)

        l = 256
        k = np.math.ceil(seq_len / l)
        r_k = k * l - seq_len
        id_extension_list = [f"{id}_k{ch_i}" for ch_i in range(k)]
        seq = torch.nn.functional.pad(seq, (0, r_k))
        seq = torch.tensor([[chunk for chunk in seq[i * l:(i + 1) * l]] for i in range(k)])
        d = 4
        msa = torch.nn.functional.pad(msa, (0, r_k))
        msa_row_idxs = []
        msa = torch.tensor(
            [
                [[chunk for chunk in _seq[i * l:(i + 1) * l]] for _seq in msa[rand_chunk(msa_depth, d, msa_row_idxs)]]
                for i in range(k)
            ]
        )
        sst = torch.nn.functional.pad(sst, (0, r_k), value=-1)
        sst = torch.tensor([[chunk for chunk in sst[i * l:(i + 1) * l]] for i in range(k)])
        print(f"padded: seq {seq.shape} msa {msa.shape} sst {sst.shape}")
        # seq = rearrange(seq, 'b (limit chunk) -> (b chunk) limit', b = 1, limit = MAX_SEQ_LEN) # group result
        # msa = rearrange(msa, 'b (l_w w) (l_h h) -> (l_h b) (l_w b) h w', l_w = MAX_SEQ_LEN, l_h=MAX_NUM_MSA)
        # sst = rearrange(sst, 'b (l c) -> (b c) l', b = 1, l = MAX_SEQ_LEN)
        tloss = 0
        for i in range(k):
            n_seq, n_msa, cut_off = reshape_input(seq, msa, sst, i)
            trunk,_,_,_ = af2(seq=n_seq, msa=n_msa, ss_only=True, msa_row_pos=msa_row_idxs[i], seq_pos=i * l)
            ss = ssp(trunk)
            ss, ss_t = reshape_output(sst, i, cut_off, ss)
            loss = lossFn(ss, ss_t)
            tloss += loss

        write_loss(writer, global_counter, id, tloss)
        tloss.backward()
        global_counter += 1
        optim.step()
        optim.zero_grad()


def write_loss(writer, global_counter, id, tloss):
    writer.add_scalar(f"Loss v0.2", tloss, global_step=global_counter)
    writer.add_text("Data id", f"{id}", global_step=global_counter)


def reshape_output(sst, i, cut_off, ss):
    ss = rearrange(ss, 'b n c -> n b c', c=NUM_SS_CLASS)
    ss = ss[:cut_off, ...]
    ss = rearrange(ss, 'n b c -> b c n')
    ss_t = sst[i][:cut_off]
    ss_t = rearrange(ss_t, 'l -> () l').cuda()
    return ss, ss_t


def reshape_input(seq, msa, sst, i):
    n_seq = rearrange(seq[i], 'l -> () l').cuda()
    n_msa = rearrange(msa[i], 's w -> () s w').cuda()
    valid = sst[i][:] != -1
    cut_off = None
    for z in range(len(valid)):
        if valid[z]:
            cut_off = z
    return n_seq, n_msa, cut_off


if __name__ == '__main__':
    test('./experiment/sample_msa')
