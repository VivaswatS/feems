"""The following helper functions are adapted from Ben Peters' code:
https://github.com/NovembreLab/eems-around-the-world/blob/master/subsetter/
"""

from __future__ import absolute_import, division, print_function

import fiona
import numpy as np
import scipy as sp
from scipy.stats import norm
from shapely.affinity import translate
from shapely.geometry import MultiPoint, Point, Polygon, shape
from sklearn.decomposition import PCA
from sklearn.mixture import GaussianMixture
from sklearn.neighbors import KernelDensity
from statsmodels.distributions.empirical_distribution import ECDF
from statsmodels.stats.multitest import multipletests
import matplotlib.pyplot as plt

def load_tiles(s):
    tiles = fiona.collection(s)
    return [shape(t["geometry"]) for t in tiles]


def wrap_longitude_tiles(tile, long):
    tile = Point(tile)
    if np.max(tile.xy[0]) < long or np.min(tile.xy[0]) < long:
        tile = translate(tile, xoff=360.0)
    return tile.xy[0][0], tile.xy[1][0]


def create_tile_dict(tiles, bpoly, translated, long): 
    pts = dict()  # dict saving ids
    rev_pts = dict()
    edges = set()
    pts_in = dict()  # dict saving which points are in region

    for c, poly in enumerate(tiles):
        x, y = poly.exterior.xy
        points = zip(np.round(x, 3), np.round(y, 3))
        if translated:
            points = [wrap_longitude_tiles(p, long) for p in points] 
        else:
            points = [p for p in points]
        for p in points:
            if p not in pts_in:
                # check if point is in region
                with np.errstate(invalid="ignore"):
                    pts_in[p] = bpoly.intersects(Point(p))
                if pts_in[p]:
                    pts[p] = len(pts)  # if so, give id
                    rev_pts[len(rev_pts)] = p

        for i in range(3):
            pi, pj = points[i], points[i + 1]
            if pts_in[pi] and pts_in[pj]:
                if pts[pi] < pts[pj]:
                    edges.add((pts[pi] + 1, pts[pj] + 1))
                else:
                    edges.add((pts[pj] + 1, pts[pi] + 1))

    pts = [Point(rev_pts[p]) for p in range(len(rev_pts))]
    return pts, rev_pts, edges


def unique2d(a):
    x, y = a.T
    b = x + y * 1.0j
    idx = np.unique(b, return_index=True)[1]
    return a[idx]


def get_closest_point_to_sample(points, samples):
    usamples = unique2d(samples)
    dists = dict(
        (tuple(s), np.argmin([Point(s).distance(Point(p)) for p in points]))
        for s in usamples
    )

    res = [dists[tuple(s)] for s in samples]
    return np.array(res)


def prepare_graph_inputs(coord, ggrid, translated=False, buffer=0, outer=None, wrap_longitude=-40):
    """Prepares the graph input files for feems adapted from Ben Peters
    eems-around-the-world repo

    Args:
        sample_pos (:obj:`numpy.ndarray`): spatial positions for samples
        ggrid (:obj:`str`): path to global grid shape file
        translated (:obj:`bool`): to handle the 'date line problem'
        transform (:obj:`bool`): to translate x coordinates
        buffer (:obj:`float`) buffer on the convex hull of sample pts
        outer (:obj:`numpy.ndarray`): q x 2 matrix of coordinates of outer
            polygon
        wrap_longitude (:obj:`int`): flag to pass in a specific value of the longitude from which to start wrapping (i.e., more fine-tuned control of the translated flag). For example, with North America (and mainly Alaska), we recommend a longitude of -40.
    """
    # no outer so construct with buffer
    if outer is None:
        points = MultiPoint([(x, y) for x, y in coord])
        xy = points.convex_hull.buffer(buffer).exterior.xy
        outer = np.array([xy[0].tolist(), xy[1].tolist()]).T

    if translated:
        outer[:, 0] = outer[:, 0] + 360.0

    # intersect outer with discrete global grid
    bpoly = Polygon(outer)
    bpoly2 = translate(bpoly, xoff=-360.0)
    tiles2 = load_tiles(ggrid)

    np.seterr(invalid='ignore')
    tiles3 = [t for t in tiles2 if bpoly.intersects(t) or bpoly2.intersects(t)]
    pts, rev_pts, e = create_tile_dict(tiles3, bpoly, translated, wrap_longitude)

    # construct grid array
    grid = []
    for i, v in rev_pts.items():
        grid.append((v[0], v[1]))
    grid = np.array(grid)

    # TODO add a more informative message on how users can get out of this pickle
    assert grid.shape[0] != 0, "grid is empty changing translation"

    # un-translate
    if translated:
        pts = []
        for p in range(len(rev_pts)):
            pts.append(Point(rev_pts[p][0] - 360.0, rev_pts[p][1]))
        grid[:, 0] = grid[:, 0] - 360.0
        outer[:, 0] = outer[:, 0] - 360.0

    # construct edge array
    edges = np.array(list(e))
    ipmap = get_closest_point_to_sample(pts, coord)
    res = (outer, edges, grid, ipmap)
    return res

def get_outlier_idx(emp_dist, fit_dist, fdr=0.1):
    bh = benjamini_hochberg(emp_dist, fit_dist, fdr=fdr)

    max_res_node = []
    for k in np.where(bh)[0]:
        # code to convert single index to matrix indices
        x = np.floor(np.sqrt(2*k+0.25)-0.5).astype('int')+1; y = int(k - 0.5*x*(x-1))

        max_res_node.append([x, y])

    return max_res_node

def parametric_bootstrap(sp_graph, emp_dist, fit_dist, lamb, lamb_q, optimize_q='n-dim', numdraws=100, fdr=0.1, dfscaler=5):
    """
    Apply the parametric bootstrap procedure to a obtain a list of p-values for points.
    Required:
        emp_cov, fit_cov (numpy.array)
    Optional:    
        numdraws (int), fdr (float), dfscaler (float): False discovery rate threshold.
    """
    from .objective import Objective, comp_mats
    
    n = sp_graph.n_observed_nodes

    tril_idx = np.tril_indices(n, k=-1)
    
    emp_distmat = np.zeros((n, n))
    emp_distmat[tril_idx] = emp_dist; emp_distmat += emp_distmat.T
    fit_distmat = np.zeros((n, n))
    fit_distmat[tril_idx] = fit_dist; fit_distmat += fit_distmat.T
    D_sample = np.zeros_like(fit_distmat)

    bootstrapped_distances = np.zeros((numdraws, n, n)); bootstrapped_fits = np.zeros_like(bootstrapped_distances)

    oldw = sp_graph.w; olds2 = sp_graph.s2

    C = np.vstack((-np.ones(n-1), np.eye(n-1))).T
    
    print('\n\tNumber of random draws in bootstrap:', end=' ')
    for d in range(numdraws):
        if d%20 == 0:
            print(d, end='...')
            
        # random draw given the EEMS scale matrix
        W = -sp.stats.wishart.rvs(df=sp_graph.n_snps/dfscaler, scale=-dfscaler*(C@fit_distmat@C.T)/sp_graph.n_snps)
        
        # holder for random distance matrix
        D_sample[1:,0] = -np.diagonal(W)/2
        D_sample[0,1:] = D_sample[1:,0]
        for i in range(1,n):
            for j in range(i+1,n):
                D_sample[i,j] = W[i-1,j-1] + D_sample[i,0] + D_sample[j,0]
                D_sample[j,i] = D_sample[i,j]
    
        bootstrapped_distances[d, :, :] = D_sample
    
        # constructing the covariance matrix for fitting
        Sigma = dist_to_cov(D_sample)

        # refitting the weights on the newly drawn samples
        sp_graph.S = Sigma
        
        sp_graph.fit(lamb=lamb, lamb_q=lamb_q, optimize_q=optimize_q)
        # sp_graph.fit(lamb=lamb, lamb_q=lamb_q, optimize_q=optimize_q, option='onlyc', long_range_edges=edges)
        
        objn = Objective(sp_graph); #objn.inv(); objn.grad(reg=False); objn.Linv_diag = objn._comp_diag_pinv()
        fit_cov2, _, _ = comp_mats(objn)
        bootstrapped_fits[d, :, :] = cov_to_dist(fit_cov2)

    print('done!')

    # updating the graph to the baseline state
    sp_graph.S = sp_graph.frequencies @ sp_graph.frequencies.T / sp_graph.n_snps
    sp_graph._update_graph(oldw, olds2)

    log_ratios_emp = np.log(emp_distmat[tril_idx] / fit_distmat[tril_idx])
    log_ratios_boot = np.log(bootstrapped_distances[:, tril_idx[0], tril_idx[1]] /
                         bootstrapped_fits[:, tril_idx[0], tril_idx[1]])

    # Compute p-values for upper triangular elements
    p_values = np.mean(log_ratios_boot <= log_ratios_emp, axis=0)
    
    m = len(p_values)  # total number of hypotheses
    sorted_p_values = np.sort(p_values)

    sorted_indices = np.argsort(p_values)
    critical_values = np.array([fdr * (i + 1) / m for i in range(m)])

    # Find the largest p-value that meets the Benjamini-Hochberg criterion
    is_significant = sorted_p_values <= critical_values
    if np.any(is_significant):
        max_significant = np.max(np.where(is_significant)[0])  # max index where condition is true
    else:
        max_significant = -1  # no significant results
    # All p-values with rank <= max_significant are significant
    significant_indices = sorted_indices[:max_significant + 1]
    results = np.zeros(m, dtype=bool)
    results[significant_indices] = True
    # max_significant + 1 because indices are 0-based, but k should be 1-based
    return results

def benjamini_hochberg(emp_dist, fit_dist, fdr=0.1):
    """
    Apply the Benjamini-Hochberg procedure to a list of p-values to determine significance
    and the largest k such that p_(k) <= k/m * FDR.
    Required:
        emp_dist, fit_dist (numpy.array)
    Optional:    
        fdr (float): False discovery rate threshold.
    """

    logratio = np.log(emp_dist/fit_dist)
    # logratio = emp_dist-fit_dist

    mean_logratio = np.mean(logratio)
    var_logratio = np.var(logratio,ddof=1)
    logratio_norm = (logratio-mean_logratio)/np.sqrt(var_logratio)
    
    # p_value_neg = sp.stats.norm.cdf(logratio_norm)
    # p_values = p_value_neg
    ## if you want to look for outliers in the other directions
    # p_values=1-p_value_neg

    p_values, _, _ = get_robust_normal_pvals_lower(logratio_norm, 25)

    m = len(p_values)  # total number of hypotheses
    sorted_p_values = np.sort(p_values)

    sorted_indices = np.argsort(p_values)
    critical_values = np.array([fdr * (i + 1) / m for i in range(m)])

    # Find the largest p-value that meets the Benjamini-Hochberg criterion
    is_significant = sorted_p_values <= critical_values
    if np.any(is_significant):
        max_significant = np.max(np.where(is_significant)[0])  # max index where condition is true
    else:
        max_significant = -1  # no significant results
    # All p-values with rank <= max_significant are significant
    significant_indices = sorted_indices[:max_significant + 1]
    results = np.zeros(m, dtype=bool)
    results[significant_indices] = True
    # max_significant + 1 because indices are 0-based, but k should be 1-based
    return results
    
def pairwise_PCA_distances(genotypes, numPC = None):
    """Function to compute pairwise distance between individuals on a PCA plot
    genotypes (matrix) : input used for FEEMSmix
    numPC (int) : number of PCs to use when computing the distances
    """
    n, p = genotypes.shape
    
    if numPC is None:
        numPC = n-1
        
    pca = PCA(n_components=numPC)
    pcacoord = pca.fit_transform((genotypes - genotypes.mean(axis=0)) / genotypes.std(axis=0))

    pcdist = sp.spatial.distance.squareform(sp.spatial.distance.pdist(pcacoord, metric='euclidean'))
    D_geno = sp.spatial.distance.squareform(sp.spatial.distance.pdist(genotypes, metric="sqeuclidean")) / p
    tril_idx = np.tril_indices(n, k=-1)
    y = D_geno[tril_idx]
    x = pcdist[tril_idx]

    return x, y   

def pairwise_admixture_distances(pfile, qfile, genotypes):
    """Function to compute pairwise distance between individuals based on the admixture model G = 2QP^\top
    K (number of ancestral populations) will be inferred from the shape of the .P and .Q file
    Required:
        pfile (path) : path to .P file
        qfile (path) : path to .Q file
        genotypes (matrix) : input used for FEEMSmix
    """

    print("Reading in .P file...")
    P = np.loadtxt(pfile)
    K = P.shape[1]
    print("Number of loci: {:d}, K: {:d}".format(P.shape[0], K))

    print("Reading in .Q file...")
    Q = np.loadtxt(qfile)
    if K != Q.shape[1]:
        print("The number of source populations (K) do not match between the .P and .Q files")
        return 
    print("Number of individuals: {:d}".format(Q.shape[0]))

    G = 2 * Q @ P.T

    admixdist = sp.spatial.distance.squareform(sp.spatial.distance.pdist(G, metric='euclidean'))

    D_geno = sp.spatial.distance.squareform(sp.spatial.distance.pdist(genotypes, metric="sqeuclidean")) / P.shape[0]
    tril_idx = np.tril_indices(Q.shape[0], k=-1)
    y = D_geno[tril_idx]
    x = admixdist[tril_idx]

    return x, y
    
def cov_to_dist(S):
    """Convert a covariance matrix to a distance matrix
    """
    s2 = np.diag(S).reshape(-1, 1)
    ones = np.ones((s2.shape[0], 1))
    D = s2 @ ones.T + ones @ s2.T - 2 * S
    return D 

def dist_to_cov(D):
    """Convert a distance matrix to a covariance matrix."""
    n = D.shape[0]
    row_mean = np.mean(D, axis=1)
    col_mean = np.mean(D, axis=0)
    total_mean = np.mean(D)
    
    # Apply the transformation
    S = -0.5 * (D - row_mean - col_mean + total_mean)
    return S