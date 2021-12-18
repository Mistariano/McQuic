from typing import List
import torch
from torch import nn
from mcqc.layers import convs
from mcqc.layers.blocks import GroupSwishConv2D, ResidualBlock, ResidualBlockShuffle, ResidualBlockWithStride

from mcqc.models.quantizer import L2Quantizer, UMGMQuantizer
from mcqc.models.encoder import Director, DownSampler, EncoderHead, ResidualBaseEncoder, BaseEncoder5x5, Director5x5, DownSampler5x5, EncoderHead5x5
from mcqc.models.decoder import UpSampler, BaseDecoder5x5, UpSampler5x5, ResidualBaseDecoder


# class Compressor(nn.Module):
#     def __init__(self, encoder: nn.Module, quantizer: nn.Module, decoder: nn.Module):
#         super().__init__()
#         self._encoder = encoder
#         self._quantizer = quantizer
#         self._decoder = decoder

#     def forward(self, x: torch.Tensor):
#         y = self._encoder(x)
#         yHat = self._quantizer(y)
#         xHat = self._decoder(yHat)
#         return xHat

class Compressor(nn.Module):
    def __init__(self, channel, m, k):
        super().__init__()
        self._encoder = nn.Sequential(
            convs.conv1x1(3, channel),
            ResidualBlockWithStride(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m),
            ResidualBlockWithStride(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m),
            ResidualBlockWithStride(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m)
        )
        self._quantizer = UMGMQuantizer(channel, m, k, {
            "latentStageEncoder": lambda: nn.Sequential(
                ResidualBlockWithStride(channel, channel, groups=m),
                ResidualBlock(channel, channel, groups=m),
            ),
            "quantizationHead": lambda: nn.Sequential(
                ResidualBlock(channel, channel, groups=m),
                GroupSwishConv2D(channel, channel, groups=m)
            ),
            "latentHead": lambda: nn.Sequential(
                ResidualBlock(channel, channel, groups=m),
                GroupSwishConv2D(channel, channel, groups=m)
            ),
            "dequantizationHead": lambda: nn.Sequential(
                ResidualBlock(channel, channel, groups=m),
                GroupSwishConv2D(channel, channel, groups=m)
            ),
            "sideHead": lambda: nn.Sequential(
                ResidualBlock(channel, channel, groups=m),
                GroupSwishConv2D(channel, channel, groups=m)
            ),
            "restoreHead": lambda: nn.Sequential(
                ResidualBlock(channel, channel, groups=m),
                ResidualBlockShuffle(channel, channel, groups=m)
            ),
        })
        self._decoder = nn.Sequential(
            ResidualBlock(channel, channel, groups=m),
            ResidualBlockShuffle(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m),
            ResidualBlockShuffle(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m),
            ResidualBlockShuffle(channel, channel, groups=m),
            ResidualBlock(channel, channel, groups=m),
            GroupSwishConv2D(channel, 3),
        )

    def forward(self, x: torch.Tensor):
        y = self._encoder(x)
        # [n, c, h, w], [n, m, h, w], [n, m, h, w, k]
        yHat, codes, logits = self._quantizer(y)
        xHat = self._decoder(yHat)
        return xHat, yHat, codes, logits



class PQCompressorBig(nn.Module):
    def __init__(self, m: int, k: List[int], channel, withGroup, withAtt, withDropout, alias, ema):
        super().__init__()
        self._k = k
        self._m = m
        if withGroup:
            groups = self._m
        else:
            groups = 1

        self._levels = len(k)

        self._encoder = ResidualBaseEncoder(channel, groups, alias)

        self._heads = nn.ModuleList(EncoderHead(channel, 1, alias) for _ in range(self._levels))
        self._mappers = nn.ModuleList(DownSampler(channel, 1, alias) for _ in range(self._levels - 1))
        self._quantizers = nn.ModuleList(nn.ModuleList(L2Quantizer(ki, channel // m, channel // m) for _ in range(m)) for ki in k)

        self._reverses = nn.ModuleList(UpSampler(channel, 1) for _ in range(self._levels))
        self._scatters = nn.ModuleList(Director(channel, 1, alias) for _ in range(self._levels - 1))

        self._groupDropout = None # PointwiseDropout(0.05, True) if withDropout else None
        self._decoder = ResidualBaseDecoder(channel, 1)
        # self._context = ContextModel(m, k, channel)

    def prepare(self, x):
        latent = self._encoder(x)
        allOriginal = list()

        for i in range(self._levels):
            mapper = self._mappers[i] if i < self._levels - 1 else None
            head = self._heads[i]
            z = head(latent)
            if mapper is not None:
                latent = mapper(latent)
            else:
                latent = None
            c = self.encode(z, i)
            hard, _ = self.decode(c, i)
            # n, c, h, w = hard.shape
            if latent is not None:
                latent = latent - hard
            # [n, m, h, w, c//m]
            # z = z.reshape(n, self._m, -1, h, w).permute(0, 1, 3, 4, 2)
            allOriginal.append(c)
        # list of [n, m, h, w]
        return allOriginal

    def quantize(self, latent, level, temp):
        splits = torch.chunk(latent, self._m, 1)
        codes = list()
        trueCodes = list()
        logits = list()
        hards = list()
        features = list()
        quantizeds = list()
        codebooks = list()
        for quantizer, split in zip(self._quantizers[level], splits):
            hard, c, tc, l, (feature, quantized), codebook = quantizer(split, temp)
            codes.append(c)
            trueCodes.append(tc)
            logits.append(l)
            hards.append(hard)
            features.append(feature)
            quantizeds.append(quantized)
            codebooks.append(codebook)

        codes = torch.stack(codes, 1)
        trueCodes = torch.stack(trueCodes, 1)
        hards = torch.cat(hards, 1)
        logits = torch.stack(logits, 1)

        return hards, codes, trueCodes, logits, (features, quantizeds), codebooks

    def encode(self, latent, level):
        splits = torch.chunk(latent, self._m, 1)
        codes = list()
        for quantizer, split in zip(self._quantizers[level], splits):
            c = quantizer.encode(split)
            codes.append(c)

        codes = torch.stack(codes, 1)
        return codes

    def decode(self, code, level):
        qs = list()
        hards = list()
        for quantizer, c in zip(self._quantizers[level], code.permute(1, 0, 2, 3)):
            hard, q = quantizer.decode(c)
            hards.append(hard)
            qs.append(q)

        return torch.cat(hards, 1), torch.cat(qs, 1)

    def nextLevelDown(self, x, level, temp):
        mapper = self._mappers[level] if level < self._levels - 1 else None
        head = self._heads[level]
        z = head(x)
        if mapper is not None:
            latent = mapper(x)
        else:
            latent = None
        hard, c, tc, l, (features, quantizeds), codebooks = self.quantize(z, level, temp)
        if latent is not None:
            latent = latent - hard
        return latent, hard, c, tc, l, (features, quantizeds), codebooks

    def deQuantize(self, q, level):
        reverse = self._reverses[level]
        return reverse(q)

    def nextLevelUp(self, q, upperQ, level):
        latent = self.deQuantize(q, level)
        if upperQ is not None:
            scatter = self._scatters[level - 1]
            return latent + scatter(upperQ)
        return latent

    def rawAndQuantized(self, latent, level):
        splits = torch.chunk(latent, self._m, 1)
        hards = list()
        raws = list()
        codes = list()
        quantizeds = list()
        for quantizer, split in zip(self._quantizers[level], splits):
            c, raw, quantized = quantizer.rawAndQuantized(split)
            codes.append(c)
            raws.append(raw)
            quantizeds.append(quantized)
            hard, _ = quantizer.decode(c)
            hards.append(hard)

        codes = torch.stack(codes, 1)
        raws = torch.stack(raws, 1)
        quantizeds = torch.stack(quantizeds, 1)
        return codes, raws, quantizeds, torch.cat(hards, 1)

    def getLatents(self, x):
        latent = self._encoder(x)

        allZs = list()
        allHards = list()
        allResiduals = list()
        allCodes = list()

        for i in range(self._levels):
            mapper = self._mappers[i] if i < self._levels - 1 else None
            head = self._heads[i]
            z = head(latent)
            if mapper is not None:
                latent = mapper(latent)
            else:
                latent = None
            c, raws, quantizeds, hard = self.rawAndQuantized(z, i)
            allCodes.append(c)
            allZs.append(raws)
            allHards.append(quantizeds)
            if latent is not None:
                latent = latent - hard
                allResiduals.append(latent - hard)
        return allZs, allHards, allCodes, allResiduals


    def test(self, x:torch.Tensor):
        latent = self._encoder(x)

        allHards = list()
        allCodes = list()

        allHards.append(None)
        mathfraks = list()

        for i in range(self._levels):
            mapper = self._mappers[i] if i < self._levels - 1 else None
            head = self._heads[i]
            z = head(latent)
            if mapper is not None:
                latent = mapper(latent)
            else:
                latent = None
            c = self.encode(z, i)
            allCodes.append(c)
            hard, quantized = self.decode(c, i)
            mathfraks.append(quantized)
            if latent is not None:
                latent = latent - hard
            allHards.append(hard)

        quantizeds = list()
        quantizeds.extend(allHards)

        for i in range(self._levels, 0, -1):
            quantized = self.nextLevelUp(quantizeds[i], allHards[i - 1], i - 1)
            quantizeds[i - 1] = quantized

        restored = self._decoder(quantizeds[0])
        return restored, allCodes, mathfraks

    def forward(self, x: torch.Tensor, temp: float, e2e: bool):
        latent = self._encoder(x)

        allHards = list()
        allCodes = list()
        allTrues = list()
        allLogits = list()

        allHards.append(None)

        allFeatures = list()
        allQuantizeds = list()
        allCodebooks = list()

        for i in range(self._levels):
            latent, hards, c, tc, l, (features, quantizeds), codebooks = self.nextLevelDown(latent, i, temp)
            allHards.append(hards)
            allCodes.append(c)
            allTrues.append(tc)
            allLogits.append(l)
            allFeatures.append(features)
            allQuantizeds.append(quantizeds)
            allCodebooks.append(codebooks)

        quantizeds = list()
        quantizeds.extend(allHards)

        for i in range(self._levels, 0, -1):
            quantized = self.nextLevelUp(quantizeds[i], allHards[i - 1], i - 1)
            quantizeds[i - 1] = quantized


        restored = self._decoder(quantizeds[0])
        return restored, allHards, latent, allCodes, allTrues, allLogits, (allFeatures, allQuantizeds), allCodebooks

class PQCompressor5x5(PQCompressorBig):
    def __init__(self, m: int, k: List[int], channel, withGroup, withAtt, withDropout, alias, ema):
        super(PQCompressorBig, self).__init__()
        self._k = k
        self._m = m
        if withGroup:
            groups = self._m
        else:
            groups = 1

        self._levels = len(k)

        self._encoder = BaseEncoder5x5(channel, groups, alias)

        self._heads = nn.ModuleList(EncoderHead5x5(channel, 1, alias) for _ in range(self._levels))
        self._mappers = nn.ModuleList(DownSampler5x5(channel, 1, alias) for _ in range(self._levels - 1))
        self._quantizers = nn.ModuleList(nn.ModuleList(L2Quantizer(ki, channel // m, channel // m) for _ in range(m)) for ki in k)

        self._reverses = nn.ModuleList(UpSampler5x5(channel, 1, alias) for _ in range(self._levels))
        self._scatters = nn.ModuleList(Director5x5(channel, 1, alias) for _ in range(self._levels - 1))

        self._groupDropout = None # PointwiseDropout(0.05, True) if withDropout else None
        self._decoder = BaseDecoder5x5(channel, 1)
