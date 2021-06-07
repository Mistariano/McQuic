from math import log, perm, sqrt
import torch
from torch import nn
from torch.distributions import Categorical

from mcqc.layers.positional import PositionalEncoding2D
from mcqc.models.quantizer import AttentiveQuantizer


class EncoderDecoder(nn.Module):
    def __init__(self, d, nHead, nLayers, dFFN, k, rate=0.1):
        super().__init__()
        self._encoder = nn.TransformerEncoder(nn.TransformerEncoderLayer(d, nHead, dFFN, rate, "gelu"), nLayers)
        self._decoder = nn.TransformerEncoder(nn.TransformerEncoderLayer(d, nHead, dFFN, rate, "gelu"), nLayers)
        self._quantizer = AttentiveQuantizer(k, d, False, True)
        self._position = PositionalEncoding2D(d, 120, 120, rate)

        self._k = k
        self._sqrtK = int(sqrt(self._k))

        self._hori1 = nn.Linear(1024, self._k)
        self._hori2 = nn.Linear(self._k, 1024)

        self._dropout = nn.Dropout(rate, True)
        self._ffn = nn.Linear(d, k)

    def _createInput(self, latent: torch.Tensor):
        n, d, h, w = latent.shape
        # target = torch.zeros_like(latent)
        latent = latent.permute(2, 3, 0, 1)
        # target = target.permute(2, 3, 0, 1)
        latent = self._position(latent)
        # target = self._position(target)
        return latent.reshape(h*w, n, d) #, target.reshape(h*w, n, d)

    def forward(self, latent, code):
        # [hw, n, d], [hw, n, d] target is agnostic to latent
        latent = self._createInput(latent.detach())
        hw, n, d = latent.shape
        # [hw, n, d]
        encoded = self._encoder(latent)
        # [hw, n, d] -> [n, d, hw] -> [n, d, k] -> [k, n, d]
        encoded = self._hori1(encoded.permute(1, 2, 0)).permute(2, 0, 1)

        # [k, n, d] -> [n, d, k', k'] -> [n, d, k', k']
        z, _, _, _ = self._quantizer(encoded.permute(1, 2, 0).reshape(n, d, self._sqrtK, self._sqrtK), 1.0)

        z = z.permute(2, 3, 0, 1)
        z = self._position(z)
        z = z.reshape(self._k, n, d)

        # [k, n, d] -> [k, n, d]
        decoded = self._decoder(z)
        # [k, n, d] -> [n, d, k] -> [n, d, hw] -> [hw, n, d]
        decoded = self._hori2(decoded.permute(1, 2, 0)).permute(2, 0, 1)

        # [hw, n, k]
        # predict logit
        logit = self._ffn(self._dropout(decoded))
        # [n, k, hw], [n, hw]
        return logit.permute(1, 2, 0), code.reshape(n, -1)

    def predict(self, latent, code):
        # [hw, n, d], [hw, n, d] target is agnostic to latent
        latent = self._createInput(latent.detach())
        hw, n, d = latent.shape
        # [hw, n, d]
        encoded = self._encoder(latent)
        # [hw, n, d] -> [n, d, hw] -> [n, d, k] -> [k, n, d]
        encoded = self._hori1(encoded.permute(1, 2, 0)).permute(2, 0, 1)

        # [k, n, d] -> [n, d, k', k'] -> [n, d, k', k']
        z, _, _, _ = self._quantizer(encoded.permute(1, 2, 0).reshape(n, d, self._sqrtK, self._sqrtK), 1.0)

        z = z.permute(2, 3, 0, 1)
        z = self._position(z)
        z = z.reshape(self._k, n, d)

        # [k, n, d] -> [k, n, d]
        decoded = self._decoder(z)
        # [k, n, d] -> [n, d, k] -> [n, d, hw] -> [hw, n, d]
        decoded = self._hori2(decoded.permute(1, 2, 0)).permute(2, 0, 1)

        # [hw, n, k]
        # predict logit
        logit = self._ffn(self._dropout(decoded))
        # [hw, n]
        predict = logit.argmax(-1)
        return predict.permute(1, 0) == code.reshape(n, -1)
