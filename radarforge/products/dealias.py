"""Region-based Doppler velocity dealiasing.

Same idea as Py-ART's region based method: split the Nyquist interval into
sub-intervals, label connected regions inside each, then repeatedly merge the
pair of regions sharing the longest boundary, unfolding the smaller one by the
integer number of Nyquist intervals that best matches the boundary.
"""
from __future__ import annotations

import heapq

import numpy as np
from scipy import ndimage


def dealias_region(vel: np.ndarray, nyq: float, splits: int = 3,
                   min_region: int = 3) -> np.ndarray:
    v = np.asarray(vel, np.float32)
    if nyq is None or nyq <= 0:
        return v.copy()
    valid = np.isfinite(v)
    if not valid.any():
        return v.copy()
    nr, ng = v.shape
    interval = 2.0 * nyq
    edges = np.linspace(-nyq, nyq, splits + 1)
    edges[-1] += 1e-3
    edges[0] -= 1e-3
    labels = np.zeros(v.shape, np.int32)
    nxt = 1
    struct = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    for b in range(splits):
        m = valid & (v >= edges[b]) & (v < edges[b + 1])
        if b == splits - 1:
            m |= valid & (v >= edges[-1])
        if b == 0:
            m |= valid & (v < edges[0])
        lab, n = ndimage.label(m, structure=struct)
        if n:
            labels[m] = lab[m] + (nxt - 1)
            nxt += n
    nreg = nxt - 1
    if nreg <= 1:
        return v.copy()

    # --- union regions that touch across the 0/360 seam (same bin => same region)
    parent = np.arange(nreg + 1)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    top, bot = labels[0], labels[-1]
    both = (top > 0) & (bot > 0)
    if both.any():
        bin_of = np.digitize(v, edges[1:-1])
        same = both & (bin_of[0] == bin_of[-1])
        for a, b2 in set(zip(top[same].tolist(), bot[same].tolist())):
            ra, rb = find(a), find(b2)
            if ra != rb:
                parent[rb] = ra
        roots = np.array([find(i) for i in range(nreg + 1)])
        labels = roots[labels]

    # --- boundary statistics between regions
    la = np.concatenate([labels[:, :-1].ravel(), labels.ravel()])
    lb = np.concatenate([labels[:, 1:].ravel(), np.roll(labels, -1, axis=0).ravel()])
    va = np.concatenate([v[:, :-1].ravel(), v.ravel()])
    vb = np.concatenate([v[:, 1:].ravel(), np.roll(v, -1, axis=0).ravel()])
    keep = (la > 0) & (lb > 0) & (la != lb)
    la, lb, va, vb = la[keep], lb[keep], va[keep], vb[keep]
    swap = la > lb
    lo = np.where(swap, lb, la).astype(np.int64)
    hi = np.where(swap, la, lb).astype(np.int64)
    diff = np.where(swap, vb - va, va - vb).astype(np.float64)   # v(lo) - v(hi)
    if len(lo) == 0:
        return v.copy()
    key = lo * (nreg + 1) + hi
    uk, inv = np.unique(key, return_inverse=True)
    cnt = np.bincount(inv).astype(np.float64)
    sm = np.bincount(inv, weights=diff)
    e_lo = (uk // (nreg + 1)).astype(np.int64)
    e_hi = (uk % (nreg + 1)).astype(np.int64)

    sizes = np.bincount(labels.ravel(), minlength=nreg + 1).astype(np.int64)
    adj: dict = {}
    for a, b2, c, s in zip(e_lo.tolist(), e_hi.tolist(), cnt.tolist(), sm.tolist()):
        adj.setdefault(a, {})[b2] = [s, c]
        adj.setdefault(b2, {})[a] = [-s, c]
    offset = np.zeros(nreg + 1, np.int64)
    members = {}
    heap = [(-c, a, b2) for a, b2, c in zip(e_lo.tolist(), e_hi.tolist(), cnt.tolist())]
    heapq.heapify(heap)
    alive = np.ones(nreg + 1, bool)

    while heap:
        negc, a, b2 = heapq.heappop(heap)
        if not (alive[a] and alive[b2]):
            continue
        e = adj.get(a, {}).get(b2)
        if e is None or e[1] != -negc:
            continue
        # a <-> b2 : e[0] = sum(v_a - v_b2)
        if sizes[a] >= sizes[b2]:
            big, small, s = a, b2, e[0]
        else:
            big, small, s = b2, a, -e[0]
        k = int(np.round((s / e[1]) / interval))   # shift small by +k intervals
        mem = members.pop(small, [small])
        if k:
            offset[mem] += k
        members.setdefault(big, [big]).extend(mem)
        sizes[big] += sizes[small]
        alive[small] = False
        nb_small = adj.pop(small, {})
        adj[big].pop(small, None)
        for c_reg, (sc, cc) in nb_small.items():
            if c_reg == big:
                continue
            sc_new = sc + k * interval * cc          # sum(v_small' - v_c)
            nb_c = adj[c_reg]
            nb_c.pop(small, None)
            cur = adj[big].get(c_reg)
            if cur is None:
                adj[big][c_reg] = [sc_new, cc]
                nb_c[big] = [-sc_new, cc]
            else:
                cur[0] += sc_new
                cur[1] += cc
                nb_c[big] = [-cur[0], cur[1]]
            heapq.heappush(heap, (-adj[big][c_reg][1], min(big, c_reg), max(big, c_reg)))

    out = v + (offset[labels] * interval).astype(np.float32)
    out[~valid] = np.nan
    return out
