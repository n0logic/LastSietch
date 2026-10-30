# usage: find_markers.py on.png off.png -> JSON [[x,y,pixels],...] centroids of blobs that differ
# between a shot with only the probe markers shown and the same view with every layer off.
import sys, json
import numpy as np
from PIL import Image
from scipy import ndimage
a = np.asarray(Image.open(sys.argv[1]).convert('RGB')).astype(int)
b = np.asarray(Image.open(sys.argv[2]).convert('RGB')).astype(int)
x0, y0, x1, y1 = 410, 291, 982, 862
d = np.abs(a - b).sum(axis=2)[y0:y1, x0:x1]
d[:100, 515:] = 0   # compass buttons
mask = d > 60
lab, n = ndimage.label(ndimage.binary_dilation(mask, iterations=3))
out = []
for i in range(1, n + 1):
    ys, xs = np.nonzero((lab == i) & mask)
    if len(xs) >= 4:
        out.append([int(xs.mean()) + x0, int(ys.mean()) + y0, int(len(xs))])
print(json.dumps(out))
