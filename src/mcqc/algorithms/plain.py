from typing import Callable, Iterator, List
from logging import Logger
import math

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, DistributedSampler
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel
from torch import distributed as dist
from torch.utils.tensorboard.summary import image
from compressai._CXX import pmf_to_quantized_cdf
from compressai import ans
from cfmUtils.saver import Saver
from cfmUtils.base import FrequecyHook

from mcqc.algorithms.algorithm import Algorithm
from mcqc.evaluation.helpers import evalSSIM, psnr
from mcqc.losses.ssim import MsSSIM
from mcqc.models.whole import WholePQ
from mcqc import Config


WARMUP_STEP = 20000
def _transformerLR(step):
    step = step + 1
    return min(step / WARMUP_STEP, 0.99997 ** (step - WARMUP_STEP))


def _tuneReg(step):
    step = step + 1
    if step < 10000:
        return 2e-4
    elif step < 15000:
        return 2e-3
    else:
        return 2e-3 * 0.9999000638225533 ** (step - WARMUP_STEP)


class Plain(Algorithm):
    def __init__(self, config: Config, model: WholePQ, optimizer: Callable[[float, Iterator[nn.Parameter], float], torch.optim.Optimizer], scheduler: Callable[[torch.optim.Optimizer], torch.optim.lr_scheduler._LRScheduler], saver: Saver, savePath:str, continueTrain: bool, logger: Logger):
        super().__init__()
        self._rank = dist.get_rank()
        self._worldSize = dist.get_world_size()
        if self._rank == 0 and saver is None:
            raise AttributeError("Not passing a saver for main process.")
        if self._rank != 0 and saver is not None:
            raise AttributeError("Try passing a saver for sub-process.")
        torch.cuda.set_device(self._rank)

        self._model = DistributedDataParallel(model.to(self._rank), device_ids=[self._rank], output_device=self._rank, broadcast_buffers=False)

        if self._rank == 0:
            self._evalSSIM = MsSSIM(size_average=False).to(self._rank)

        # self._optimizer = torch.optim.AdamW(optimizer_grouped_parameters, eps=Consts.Eps, amsgrad=True)
        self._optimizer = optimizer(config.LearningRate, self._model.parameters(), 1e-5)
        # self._scheduler = scheduler(self._optimizer)
        self._scheduler = torch.optim.lr_scheduler.LambdaLR(self._optimizer, _transformerLR)

        dist.barrier(device_ids=[self._rank])

        # self._optimizerD = optimizer(1e-5, self._model.module._discriminator.parameters(), 0)
        # self._schedulerD = scheduler(self._optimizerD)
        self._ckpt = "ckpt/global.ckpt"
        self._saver = saver
        self._savePath = savePath
        self._logger = logger
        self._config = config
        self._continue = continueTrain
        if self._rank == 0:
            self._loggingHook = FrequecyHook({100: self._fastHook, 1000: self._mediumHook})
        else:
            self._loggingHook = None

        self._imgSize = 512 * 512
        # self._accumulatedBatches = 32 //  config.BatchSize

    @staticmethod
    def _deTrans(image):
        return ((image * 0.5 + 0.5) * 255).clamp(0.0, 255.0).byte()

    @torch.no_grad()
    def _fastHook(self, **kwArgs):
        ssimLoss, l1l2Loss, reg, step, regCoeff, temp, logits = kwArgs["ssimLoss"], kwArgs["l1l2Loss"], kwArgs["reg"], kwArgs["now"], kwArgs["regCoeff"], kwArgs["temperature"], kwArgs["logits"]
        self._saver.add_scalar("Loss/MS-SSIM", ssimLoss.mean(), global_step=step)
        self._saver.add_scalar("Loss/L1L2", l1l2Loss.mean(), global_step=step)
        self._saver.add_scalar("Loss/Reg", reg.mean(), global_step=step)
        self._saver.add_scalar("Stat/LR", self._scheduler.get_last_lr()[0], global_step=step)
        self._saver.add_scalar("Stat/Reg", regCoeff, global_step=step)
        self._saver.add_scalar("Stat/Temperature", temp, global_step=step)
        self._saver.add_histogram("Stat/Logit", logits[0], global_step=step)

    @torch.no_grad()
    def _mediumHook(self, **kwArgs):
        images, restored, testLoader, step, i, quantized, temperature = kwArgs["images"], kwArgs["restored"], kwArgs["testLoader"], kwArgs["now"], kwArgs["i"], kwArgs["quantized"], kwArgs["temperature"]
        self._saver.add_images("Train/Raw", self._deTrans(images), global_step=step)
        # self._saver.add_images("Train/Masked", self._deTrans(maskedImages), global_step=step)
        self._saver.add_images("Train/Res", self._deTrans(restored), global_step=step)
        self._visualizeIntermediate(quantized, step)
        uniqueCodes, ratio = self._eval(testLoader, step)
        self._saver.save(self._logger, model=self._model, optim=self._optimizer, schdr=self._scheduler, step=step, epoch=i, temperature=temperature)
        self._logger.info("[%3dk]: LR = %.2e, T = %.2e", (step) // 1000, self._scheduler.get_last_lr()[0], temperature)
        return uniqueCodes, ratio

    @torch.no_grad()
    def _visualizeIntermediate(self, latent, step):
        img = latent[0][:, None, ...]
        fMin, fMax = img.min(), img.max()
        img = (img - fMin) / (fMax - fMin)
        img = F.interpolate(img, scale_factor=4, mode="nearest")
        self._saver.add_images(f"Train/Feature", img, step)

    # pylint: disable=too-many-locals,arguments-differ
    def run(self, trainLoader: DataLoader, sampler: DistributedSampler, testLoader: DataLoader):
        step = 0
        # tristate: None (pure latent), False (quantized with straight-through), True (pure quanitzed)
        # uniqueCodes = 2048
        images = None

        temperature = 1.0
        initEpoch = 0

        mapLocation = {"cuda:0": f"cuda:{self._rank}"}
        loaded = Saver.load(self._ckpt, mapLocation, False, self._logger, model=self._model, epoch=initEpoch)
        initEpoch = loaded["epoch"]

        if self._continue:
            loaded = Saver.load(self._savePath, mapLocation, True, self._logger, model=self._model, optim=self._optimizer, schdr=self._scheduler, step=step, epoch=initEpoch)
            step = loaded["step"]
            initEpoch = loaded["epoch"]
            if self._rank == 0:
                self._logger.info("Resume training from %3dk step.", step // 1000)
        if self._rank == 0:
            self._eval(testLoader, step)

        for i in range(initEpoch, self._config.Epoch):
            sampler.set_epoch(i)
            # temperature = -1/3 * temperature + 13/3
            for images in trainLoader:
                self._optimizer.zero_grad(True)
                images = images.to(self._rank, non_blocking=True)
                (ssimLoss, l1l2Loss, reg), (restored, codes, quantized, logits, targets) = self._model(images, temperature)
                ((self._config.Coef.ssim * ssimLoss + self._config.Coef.l1l2 * l1l2Loss).mean() + self._config.Coef.reg * reg).backward()
                # torch.nn.utils.clip_grad_norm_(self._model.parameters(), 1.0)
                self._optimizer.step()
                self._scheduler.step()
                self._config.Coef.reg = _tuneReg(step)
                step += 1
                if self._loggingHook is not None:
                    with torch.no_grad():
                        results = self._loggingHook(step, ssimLoss=ssimLoss, l1l2Loss=l1l2Loss, reg=reg, now=step, images=images, targets=targets, restored=restored, testLoader=testLoader, i=i, temperature=temperature, regCoeff=self._config.Coef.reg, logits=logits, quantized=quantized)

    # pylint: disable=protected-access
    @torch.no_grad()
    def _eval(self, dataLoader: DataLoader, step: int):
        if self._logger is None:
            return
        self._model.eval()
        model = self._model.module._compressor
        ssims = list()
        psnrs = list()
        bs = [list() for _ in range(self._config.Model.m)]
        zs = list()
        qs = list()
        for raw in dataLoader:
            raw = raw.to(self._rank, non_blocking=True)

            latent = model._encoder(raw)

            # M * [n, c // M, h, w]
            splits = torch.chunk(latent, self._config.Model.m, 1)
            lHat = list()
            for i in range(self._config.Model.m):
                b = model._quantizer[i].encode(splits[i])
                q = model._quantizer[i].decode(b)
                lHat.append(q)
                bs[i].append(b.int().detach().cpu())
            quantized = torch.cat(lHat, 1)
            restored = torch.tanh(model._decoder(quantized))

            zs.append(latent.detach().cpu())
            qs.append(quantized.detach().cpu())

            raw = self._deTrans(raw)
            restored = self._deTrans(restored)

            ssim = self._evalSSIM(restored.detach().float(), raw.detach().float())

            ssims.append(-10 * (1.0 - ssim).log10())
            psnrs.append(psnr(restored.detach(), raw.detach()))

        ssims = torch.cat(ssims, 0)
        psnrs = torch.cat(psnrs, 0)
        bs = [torch.cat(x, 0) for x in bs]
        zs = torch.cat(zs, 0)
        qs = torch.cat(qs, 0)
        ssimScore = ssims.mean()
        psnrScore = psnrs.mean()
        self._logger.info("MS-SSIM: %2.2fdB", ssimScore)
        self._logger.info("   PSNR: %2.2fdB", psnrScore)
        self._saver.add_scalar("Eval/MS-SSIM", ssimScore, global_step=step)
        self._saver.add_scalar("Eval/PSNR", psnrScore, global_step=step)
        self._saver.add_images("Eval/Res", restored, global_step=step)
        uniqueCodes, counts = torch.unique(bs[0], return_counts=True)
        self._saver.add_scalar("Eval/UniqueCodes", len(uniqueCodes), global_step=step)
        # [N, C, H, W] -> mean of [N, H, W]
        self._saver.add_scalar("Eval/QError", ((qs - zs) ** 2).sum(1).mean(), global_step=step)
        self._model.train()

        encoded, bpp = self._compress(bs)
        self._saver.add_scalar("Eval/BPP", bpp, global_step=step)

        return uniqueCodes.detach().cuda(), (counts / b.numel()).detach().cuda()

    def _compress(self, codes: List[torch.Tensor]):
        compressed = list()
        cdfs = list()
        # b: Tensor of [N, 32, 32]
        for b in codes:
            # list of 256 probs
            prob = self._calculateFreq(b, self._config.Model.k)
            cdf = pmf_to_quantized_cdf(prob.tolist(), 16)
            # M * [cdf]
            cdfs.append(cdf)
        encoder = ans.RansEncoder()
        # codePerImage: M * [Tensor of [32 * 32]]
        for codePerImage in zip(*codes):
            # [M, 32, 32]
            codePerImage = torch.stack(codePerImage, 0)
            # params: List of symbols, List of indices of pdfs, List of pdfs, List of upper-bounds, List of offsets
            # [0, 1, 2, 3], [0, 0, 1, 1], [[xx, xx, xx, xx], [xx, xx, xx, xx]], [4, 4, 4, 4], [0, 0, 0, 0]
            binary = encoder.encode_with_indexes(codePerImage.flatten().int().tolist(), torch.arange(codePerImage.shape[0])[:, None, None].expand_as(codePerImage).flatten().int().tolist(), cdfs, [self._config.Model.k] * self._config.Model.m, torch.zeros_like(codePerImage).flatten().int().tolist())
            compressed.append(binary)
        # binary: 1 byte per word
        # N * [binaries]
        total = 8 * sum(len(binary) for binary in compressed)
        totalPixel = len(codes[0]) * self._imgSize
        bpp = float(total) / totalPixel
        self._logger.info("%.2fMB for %d images, BPP: %.4f", total / 1048576, len(codes[0]), bpp)
        return compressed, bpp

    def _calculateFreq(self, code: torch.Tensor, k):
        count = torch.bincount(code.flatten(), minlength=k)
        return count / code.numel()
