from typing import Any
import os
import shutil

import matplotlib.pyplot as plt
from sklearn.manifold import TSNE
from tqdm import tqdm, trange
from tqdm.contrib.logging import logging_redirect_tqdm
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import Dataset, DataLoader
from absl import app, flags
from cfmUtils.config import read
import torchvision
from torchvision import transforms

from mcqc.models.compressor import PQCompressorBig
from mcqc.datasets import Basic
from mcqc.utils.vision import getTestTransform
from mcqc.utils.transforms import DeTransform
from mcqc import Config


from matplotlib import rcParams
from matplotlib import rc

from mpl_toolkits.axes_grid1.inset_locator import inset_axes
rcParams['font.family'] = 'serif'
rcParams['mathtext.rm'] = 'CMU Serif'
rcParams['mathtext.fontset'] = 'custom'
rcParams['mathtext.it'] = 'CMU Serif:italic'
rcParams['mathtext.bf'] = 'CMU Serif:bold'
rc('text', usetex=True)
rcParams['text.latex.preamble']= r"\usepackage{amsmath}"

FLAGS = flags.FLAGS

flags.DEFINE_string("cfg", "", "The config.json path.")
flags.DEFINE_string("device", "cuda", "The device to use.")
flags.DEFINE_string("ckpt", "", "The checkpoint path.")
flags.DEFINE_string("dataset", "data/kodak/", "The images path")

class EntropyEstimator:
    def __init__(self, config: Config, model: PQCompressorBig, dataset: Dataset, device: str):
        self._model = model
        self._device = device
        self._model.to(device)
        self._dataset = dataset
        self._dataLoader = DataLoader(dataset, pin_memory=True)
        self._deTrans = DeTransform()
        self._config = config

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        baseDir = "tmp/visualize"
        shutil.rmtree(baseDir, ignore_errors=True)
        os.makedirs(baseDir, exist_ok=True)
        codes = [[[] for _ in range(self._config.Model.m)] for _ in range(self._model._levels)]
        for i, x in enumerate(self._dataLoader):
            x = x.to(self._device, non_blocking=True)
            allZs, allHards, allCodes = self._model.getLatents(x)
            for j, (z, quantized, b) in enumerate(zip(allZs, allHards, allCodes)):
                # [m, h, w]
                for m, mb in enumerate(b[0]):
                    codes[j][m].append(mb.flatten())

        bitsPerToken = []
        for l in range(self._model._levels):
            bits = []
            for m in range(self._config.Model.m):
                code = torch.cat(codes[l][m])
                count = torch.bincount(code.flatten(), minlength=self._config.Model.k[l])
                prob = count / count.sum().sqrt()
                estimateEntropy = prob.log2()
                estimateEntropy[estimateEntropy == float("-inf")] = 0
                # estimateEntropy = (prob * estimateEntropy)
                bits.append(-estimateEntropy)
            bitsPerToken.append(bits)
        return bitsPerToken

def removeAxis(ax):
    ax.tick_params(
        axis='both',       # changes apply to the x-axis
        which='both',      # both major and minor ticks are affected
        bottom=False,      # ticks along the bottom edge are off
        top=False,         # ticks along the top edge are off
        left=False,
        right=False,
        labelbottom=False,
        labeltop=False,
        labelleft=False,
        labelright=False) # labels along the bottom edge are off

    ax.spines['right'].set_color("#888888")
    ax.spines['top'].set_color("#888888")
    ax.spines['left'].set_color("#888888")
    ax.spines['bottom'].set_color("#888888")


class Visualizer:
    def __init__(self, config: Config, model: PQCompressorBig, dataset: Dataset, device: str):
        self._model = model
        self._device = device
        self._model.to(device)
        self._dataset = dataset
        self._dataLoader = DataLoader(dataset, pin_memory=True)
        self._deTrans = DeTransform()
        self._config = config

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        bitsPerToken = args[0]

        baseDir = "tmp/visualize"
        shutil.rmtree(baseDir, ignore_errors=True)
        os.makedirs(baseDir, exist_ok=True)
        for i, x in enumerate(tqdm(self._dataLoader)):
            os.makedirs(os.path.join(baseDir, str(i)))
            nowDir = os.path.join(baseDir, str(i))

            x = x.to(self._device, non_blocking=True)

            toPillow = torchvision.transforms.ToPILImage()


            allZs, allHards, allCodes = self._model.getLatents(x)

            for m in range(1):
                fig = plt.figure(constrained_layout=False, figsize=(8.27, 8.27 / 2.0), dpi=384)
                gs0 = fig.add_gridspec(6, 8, width_ratios=[8,6,6,0.7,6,0.7,6,0.7], height_ratios=[6,2,6,2,6,2], left=0.0, bottom=0.00, right=1.0, top=1.0, wspace=0.0, hspace=0.0)
                rs = toPillow(self._deTrans(x).cpu()[0].cpu())
                for l in range(len(self._config.Model.k)):
                    z, quantized, b = allZs[l][0, m], allHards[l][0, m], allCodes[l][0, m]
                    zs = z[0].cpu().numpy()
                    qs = quantized[0].cpu().numpy()
                    bs = b.cpu().numpy()
                    imageOrResidual = fig.add_subplot(gs0[l*2:l*2+2, 0])
                    if l == 0:
                        imageOrResidual.set_ylabel("Image", fontsize=9)
                        imageOrResidual.imshow(rs)
                    else:
                        imageOrResidual.set_ylabel(r"$\boldsymbol{y}^" + str(l) + r"- \hat{\boldsymbol{y}}^" + str(l) + r"$", fontsize=9)
                        imageOrResidual.imshow(rs,cmap="RdBu")
                    removeAxis(imageOrResidual)
                    latent = fig.add_subplot(gs0[l*2, 1])
                    latent.imshow(zs, cmap="RdBu")
                    removeAxis(latent)
                    latent.set_xlabel(r"$\boldsymbol{y}^" + str(l) + r"$", fontsize=9)
                    quantMap = fig.add_subplot(gs0[l*2, 2])
                    quantIm = quantMap.imshow(qs, cmap="RdBu")
                    removeAxis(quantMap)

                    axins = inset_axes(quantMap,
                                    width="5%",  # width = 5% of parent_bbox width
                                    height="100%",  # height : 100%
                                    loc='lower left',
                                    bbox_to_anchor=(1.02, 0., 1, 1),
                                    bbox_transform=quantMap.transAxes,
                                    borderpad=0,
                                    )
                    cb = fig.colorbar(quantIm, cax=axins, values=None)
                    cb.outline.set_edgecolor('#888888')
                    # cb.outline.set_linewidth(1)
                    removeAxis(axins)

                    quantMap.set_xlabel(r"$\hat{\boldsymbol{y}}^" + str(l) + r"$", fontsize=9)
                    binaryMap = fig.add_subplot(gs0[l*2, 4])
                    binaryIm = binaryMap.imshow(bs, cmap="twilight")
                    removeAxis(binaryMap)
                    binaryMap.set_xlabel(r"$\boldsymbol{b}^" + str(l) + r"$", fontsize=9)

                    axins = inset_axes(binaryMap,
                                    width="5%",  # width = 5% of parent_bbox width
                                    height="100%",  # height : 100%
                                    loc='lower left',
                                    bbox_to_anchor=(1.02, 0., 1, 1),
                                    bbox_transform=binaryMap.transAxes,
                                    borderpad=0,
                                    )
                    cb = fig.colorbar(binaryIm, cax=axins, values=None)
                    cb.outline.set_edgecolor('#888888')
                    # cb.outline.set_linewidth(1)
                    removeAxis(axins)

                    rs = (zs - qs)
                    # [k] entropies
                    bits = bitsPerToken[l][m]
                    # [h, w] required bits
                    requiredBitsJM = bits[b].exp().cpu().numpy()


                    bitMap = fig.add_subplot(gs0[l*2, 6])
                    bitIm = bitMap.imshow(requiredBitsJM, cmap="inferno", vmin=bits.min().exp().item(), vmax=bits.max().exp().item())
                    removeAxis(bitMap)
                    bitMap.set_xlabel(r"Bits allocation", fontsize=9)


                    axins = inset_axes(bitMap,
                                    width="5%",  # width = 5% of parent_bbox width
                                    height="100%",  # height : 100%
                                    loc='lower left',
                                    bbox_to_anchor=(1.02, 0., 1, 1),
                                    bbox_transform=bitMap.transAxes,
                                    borderpad=0,
                                    )
                    cb = fig.colorbar(bitIm, cax=axins, values=None)
                    cb.outline.set_edgecolor('#888888')
                    # cb.outline.set_linewidth(1)
                    removeAxis(axins)


            plt.savefig(os.path.join(nowDir, "vis.pdf"), bbox_inches="tight")
            plt.close()

class Plotter:
    def __init__(self, config: Config, model: PQCompressorBig, dataset: Dataset, device: str):
        self._model = model
        self._device = device
        self._model.to(device)
        self._dataset = dataset
        self._dataLoader = DataLoader(dataset, pin_memory=True)
        self._deTrans = DeTransform()
        self._config = config

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        baseDir = "tmp/tsne"
        shutil.rmtree(baseDir, ignore_errors=True)
        os.makedirs(baseDir, exist_ok=True)

        totalZs = [[[] for _ in range(self._config.Model.m)] for _ in range(self._model._levels)]
        totalHards = [[[] for _ in range(self._config.Model.m)] for _ in range(self._model._levels)]
        totalCodes = [[[] for _ in range(self._config.Model.m)] for _ in range(self._model._levels)]

        for i, x in enumerate(tqdm(self._dataLoader)):
            x = x.to(self._device, non_blocking=True)

            allZs, allHards, allCodes = self._model.getLatents(x)

            for j, (z, quantized, b) in enumerate(zip(allZs, allHards, allCodes)):
                # [h, w], [c // m, h, w], [c // m, h, w]
                for m, (bs, zs, qs) in enumerate(zip(b[0], z[0], quantized[0])):
                    # [k, c]
                    # codebook = self._model._quantizers[j][m]._codebook

                    # [h*w, c]
                    zs = zs.reshape(zs.shape[0], -1).permute(1, 0)
                    qs = qs.reshape(qs.shape[0], -1).permute(1, 0)

                    totalZs[j][m].append(zs)
                    totalHards[j][m].append(qs)
                    totalCodes[j][m].append(bs.flatten())

        for j in trange(self._model._levels):
            for m in trange(self._config.Model.m, leave=False):
                # [N, c]
                z = torch.cat(totalZs[j][m])
                # [N, c]
                q = torch.cat(totalHards[j][m])
                # [N]
                b = torch.cat(totalCodes[j][m])
                # [K]
                counts = torch.bincount(b)
                # [10] indices
                topK = torch.argsort(counts, descending=True)[:20]

                # [N, 10] sum -> [N] > 0 -> at least one hit
                included = (b[:, None] == topK).sum(-1) > 0

                b = b[included]
                z = z[included]
                q = q[included]

                tqdm.write(str(b.shape))

                uniqueCodes, ivf = torch.unique(b, sorted=True, return_inverse=True)
                codebook = self._model._quantizers[j][m]._codebook
                centers = codebook[uniqueCodes]

                vectorsToVis = z
                # vectorsToVis = torch.cat((z, centers))
                result = TSNE(perplexity=45, min_grad_norm=1e-12, learning_rate="auto", init="pca", n_jobs=-1).fit_transform(vectorsToVis.cpu().numpy())

                # categories = torch.cat([ivf.reshape(-1), torch.arange(len(pickedCodes))]).cpu().tolist()
                categories = [(ivf / float(len(uniqueCodes))).cpu().tolist(),
                                (torch.arange(len(uniqueCodes)) / float(len(uniqueCodes))).cpu().tolist()]
                # categories = (torch.arange(len(pickedCodes)) / float(len(pickedCodes))).tolist()
                resultA = result[:len(z)]
                # resultB = result[len(z):]
                plt.scatter(resultA[:, 0], resultA[:, 1], c=categories[0], s=2)
                # plt.scatter(resultB[:, 0], resultB[:, 1], c=categories[1], marker="*", s=100)
                plt.savefig(os.path.join(baseDir, f"level-{j}-group-{m}.pdf"))
                plt.close()



@torch.inference_mode()
def main(_):
    with logging_redirect_tqdm():
        config = read(FLAGS.cfg, None, Config)
        dataset = Basic(FLAGS.dataset, transform=getTestTransform())
        model = PQCompressorBig(config.Model.m, config.Model.k, config.Model.channel, config.Model.withGroup, config.Model.withAtt, False, config.model.alias, -1)
        # print(sum(p.numel() for p in model.parameters() if p.requires_grad))
        savedModel = torch.load(FLAGS.ckpt, map_location={"cuda:0": "cpu"})
        model.load_state_dict({k[len("module._compressor."):]: v for k, v in savedModel["model"].items() if "module._compressor." in k})

        model = model.eval()

        bitsPerToken = EntropyEstimator(config, model, dataset, FLAGS.device)()

        visualizer = Visualizer(config, model, dataset, FLAGS.device)
        visualizer(bitsPerToken)
        return
        plotter = Plotter(config, model, Basic(FLAGS.dataset, transform=getTestTransform()), FLAGS.device)
        plotter()


# pick 1/, 2/, 19/,

if __name__ == "__main__":
    app.run(main)
