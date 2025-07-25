from __future__ import absolute_import, division, print_function

# import allel
from copy import deepcopy
import itertools as it
import networkx as nx
import numpy as np
import pandas as pd
from scipy.linalg import det, pinvh
import scipy.sparse as sp
from scipy.optimize import minimize
from scipy.stats import wishart, norm, chi2

from .utils import cov_to_dist, dist_to_cov, benjamini_hochberg, get_outlier_idx

class Objective(object):
    def __init__(self, sp_graph):
        """Evaluations and gradient of the feems objective function

        Args:
            sp_graph (:obj:`feems.SpatialGraph`): feems spatial graph object
        """
        # spatial graph
        self.sp_graph = sp_graph

        # reg params
        self.lamb = None
        self.alpha = None
        self.lamb_q = None
        self.alpha_q = None

        self.nll = 0.0

        self.C = np.vstack((-np.ones(self.sp_graph.n_observed_nodes-1), np.eye(self.sp_graph.n_observed_nodes-1))).T
        
        # genetic distance matrix
        self.sp_graph.Dhat = cov_to_dist(sp_graph.S)

        self.CDCt = self.C @ self.sp_graph.Dhat @ self.C.T

    def _rank_one_solver(self, B):
        """Solver for linear system (L_{d-o,d-o} + ones/d) * X = B using rank
        ones update equation
        """
        # dims
        d = len(self.sp_graph)
        o = self.sp_graph.n_observed_nodes

        # vector of ones with size d-o
        ones = np.ones(d - o)

        # sparse cholesky factorization
        # solve the systems
        # L_block{dd}\B
        # TODO: how to handle when B is sparse
        U = self.sp_graph.factor(B)

        # L_block{dd}\ones
        v = self.sp_graph.factor(ones)

        # denominator
        denom = d + np.sum(v)
        X = U - np.outer(v, v @ B) / denom

        return (X, v, denom)

    def _solve_lap_sys(self):
        """Solve (L_{d-o,d-o} + ones/d) * X = L_{d-o,o} + ones/d using rank one
        solver
        """
        o = self.sp_graph.n_observed_nodes
        d = len(self.sp_graph)

        # set B = L_{d-o,o}
        B = self.sp_graph.L_block["do"]

        # solve (L_{d-o,d-o} + ones/d) \ B
        self.lap_sol, v, denom = self._rank_one_solver(B.toarray())

        # compute rank one update for vector of ones
        ones = np.ones(o)
        self.lap_sol += np.outer(v, ones) * (1.0 / d - np.sum(v) / (d * denom))

    def _comp_mat_block_inv(self):
        """Computes matrix block inversion formula"""
        d = len(self.sp_graph)
        o = self.sp_graph.n_observed_nodes

        # multiply L_{o,d-o} by solution of lap-system
        A = self.sp_graph.L_block["od"] @ self.lap_sol

        # multiply one matrix by solution of lap-system
        B = np.outer(np.ones(o), self.lap_sol.sum(axis=0)) / d

        # sum up with L_{o,o} and one matrix 
        ## Eqn 16 (pg. 23)
        self.L_double_inv = self.sp_graph.L_block["oo"].toarray() + 1.0 / d - A - B

    def _comp_diag_pinv(self):
        """Compute the diagonal of the pseudo-inverse using LU decomposition."""
        n = len(self.sp_graph)
        L_mod = self.sp_graph.L + np.eye(n) / n  # Make L invertible
        LU = sp.linalg.splu(sp.csc_matrix(L_mod))
        
        diag = np.zeros(n); ei = np.zeros(n)
        for i in range(n):
            ei[i] = 1
            xi = LU.solve(ei)
            diag[i] = xi[i]
            ei[i] = 0
        
        return diag - 1  # Correct for the added identity matrix
        
    def _comp_inv_lap(self, B=None):
        """Computes submatrices of inverse of lap"""
        if B is None:
            B = np.eye(self.sp_graph.n_observed_nodes)

        # inverse of graph laplacian
        # compute o-by-o submatrix of inverse of lap
        self.Linv_block = {}
        self.Linv_block["oo"] = np.linalg.solve(self.L_double_inv, B)
        # compute (d-o)-by-o submatrix of inverse of lap
        self.Linv_block["do"] = -self.lap_sol @ self.Linv_block["oo"]

        # store the diagonal elements of the (d-o) elements
        if self.sp_graph.option == 'onlyc':
            self.Linv_diag = self._comp_diag_pinv()

        # stack the submatrices
        self.Linv = np.vstack((self.Linv_block["oo"], self.Linv_block["do"]))

    def _comp_inv_cov(self, B=None):
        """Computes inverse of the covariance matrix"""
        # helper
        A = (
            -self.sp_graph.q_inv_diag.toarray()
            - (self.sp_graph.q_inv_diag @ self.L_double_inv) @ self.sp_graph.q_inv_diag
        )
        if B is None:
            B = np.eye(self.sp_graph.n_observed_nodes)

        # solve o-by-o linear system to get X
        self.X = np.linalg.solve(A, B)

        # inverse covariance matrix
        self.inv_cov = self.X + np.diag(self.sp_graph.q)
        self.inv_cov_sum = self.inv_cov.sum(axis=0)
        self.denom = self.inv_cov_sum.sum()

    def _comp_grad_obj(self):
        """Computes the gradient of the objective function with respect to the
        latent variables dLoss / dL
        """
        # compute inverses
        self._comp_inv_lap()

        self.comp_B = self.inv_cov - (1.0 / self.denom) * np.outer(
            self.inv_cov_sum, self.inv_cov_sum
        )
        self.comp_A = self.comp_B @ self.sp_graph.S @ self.comp_B
        M = self.comp_A - self.comp_B
        self.grad_obj_L = self.sp_graph.n_snps * (self.Linv @ M @ self.Linv.T)

        # grads
        gradD = np.diag(self.grad_obj_L) @ self.sp_graph.P
        gradW = 2 * self.grad_obj_L[self.sp_graph.nnz_idx_perm]  # use symmetry
        self.grad_obj = gradD - gradW

        # grads for d diag(Jq^-1) / dq
        if self.sp_graph.optimize_q == 'n-dim':
            self.grad_obj_q = np.zeros(len(self.sp_graph))
            self.grad_obj_q[:self.sp_graph.n_observed_nodes] = self.sp_graph.n_snps * (np.diag(M) @ self.sp_graph.q_inv_grad)                   
        elif self.sp_graph.optimize_q == '1-dim':
            self.grad_obj_q = self.sp_graph.n_snps * (np.diag(M) @ self.sp_graph.q_inv_grad) 

    def _comp_grad_obj_c(self):
        """Computes the gradient of the objective function (now defined with source fraction c) with respect to the latent variables dLoss / dL
        """

        # compute inverses
        self._comp_inv_lap()
        

        # calculating the R and Q matrices as per Petkova et al 2016
        Rmat = -2*self.Linv[:self.sp_graph.n_observed_nodes,:self.sp_graph.n_observed_nodes] + np.broadcast_to(np.diag(self.Linv),(self.sp_graph.n_observed_nodes,self.sp_graph.n_observed_nodes)).T + np.broadcast_to(np.diag(self.Linv),(self.sp_graph.n_observed_nodes,self.sp_graph.n_observed_nodes)) 
        Q1mat = np.ones((self.sp_graph.n_observed_nodes,1)) @ self.sp_graph.q_inv_diag.diagonal().reshape(1,-1) 
        resmat = Rmat + (Q1mat + Q1mat.T) - 2*self.sp_graph.q_inv_diag

        # check if length is greater than 0
        if self.sp_graph.c is not None:
            for c, edge in zip(self.sp_graph.c, self.sp_graph.edge):
                
                # getting index of source and destination deme using internal indexing (0, 1, 2, ..., o) 
                sid = np.where(self.sp_graph.perm_idx == edge[0])[0][0]
                did = np.where(self.sp_graph.perm_idx == edge[1])[0][0]
                
                # if source deme is sampled
                if sid<self.sp_graph.n_observed_nodes:
                    # resmat[sid, did] += (0.5 * c**2 - 1.5 * c + 1) * Rmat[sid, did] + (1 + c) / self.sp_graph.q[sid] + \
                    #                     (1 - c) / self.sp_graph.q[did]
                    resmat[sid, did] += (0.5 * c**2 - 1.5 * c) * Rmat[sid, did] + c * Q1mat[sid, sid] - c * Q1mat[did, did]
    
                    resmat[did, sid] = resmat[sid, did]
        
                    # Update for all other demes except source and destination
                    for i in set(range(self.sp_graph.n_observed_nodes)) - {sid, did}:
                        resmat[i, did] += - c * Rmat[i, did] + c * Rmat[i, sid] + 0.5 * (c**2 - c) * Rmat[sid, did] - c * Q1mat[did, did] + c * Q1mat[sid, sid]
                        resmat[did, i] = resmat[i, did]
                # if source deme is unsampled
                else:
                    R1d = -2 * self.Linv[sid, did] + self.Linv_diag[sid] + self.Linv[did, did]
                    R1 = np.array(-2 * self.Linv[sid, :self.sp_graph.n_observed_nodes].T + np.diag(self.Linv) + self.Linv_diag[sid])
                    
                    # Update for non-neighboring demes
                    for i in set(range(self.sp_graph.n_observed_nodes)) - {sid, did}:
                        Ri1 = -2 * self.Linv[sid, i] + self.Linv_diag[i] + self.Linv_diag[sid]
                        resmat[i, did] += - c * Rmat[i, did] + c * Ri1 + 0.5 * (c**2 - c) * R1d + \
                                          - c * Q1mat[did, did] + c * self.sp_graph.q_prox[sid - self.sp_graph.n_observed_nodes]
                        resmat[did, i] = resmat[i, did]
            
        # convert distance matrix to covariance matrix for use in FEEMS
        Sigma = dist_to_cov(resmat)

        # Eqn 18 in Marcus et al 2021 
        CRCt = np.linalg.inv(self.C @ Sigma @ self.C.T) 
        Pi1 = Sigma @ self.C.T @ CRCt @ self.C
        siginv = np.linalg.inv(Sigma)
        if self.sp_graph.optimize_q == 'n-dim':
            # Eqn 12 from Marcus et al 2021 (but is equivalent to Eqn 18 I think?)
            # M = self.C.T @ (CRCt @ (self.C @ self.sp_graph.S @ self.C.T) @ CRCt - CRCt) @ self.C
            M = siginv @ Pi1 @ self.sp_graph.S @ siginv @ Pi1 - siginv @ Pi1
        else:
            self.comp_B = self.inv_cov - (1.0 / self.denom) * np.outer(
                self.inv_cov_sum, self.inv_cov_sum
            )
            self.comp_A = self.comp_B @ self.sp_graph.S @ self.comp_B
            M = self.comp_A - self.comp_B
            
        self.grad_obj_L = self.sp_graph.n_snps * (self.Linv @ M @ self.Linv.T)

        gradD = np.diag(self.grad_obj_L) @ self.sp_graph.P
        gradW = 2 * self.grad_obj_L[self.sp_graph.nnz_idx_perm]  # use symmetry
        self.grad_obj = np.ravel(gradD - gradW)
        
        # grads for d diag(Jq^-1) / dq
        if self.sp_graph.optimize_q == 'n-dim':
            self.grad_obj_q = np.zeros(len(self.sp_graph))
            self.grad_obj_q[:self.sp_graph.n_observed_nodes] = self.sp_graph.n_snps * (np.diag(M) @ self.sp_graph.q_inv_grad)        
        else:
            self.grad_obj_q = self.sp_graph.n_snps * (np.diag(M) @ self.sp_graph.q_inv_grad) 

    def _comp_grad_reg(self):
        """Computes gradient"""
        lamb = self.lamb
        alpha = self.alpha

        # avoid overflow in exp
        # term_0 = 1.0 - np.exp(-alpha * self.sp_graph.w)
        # term_1 = alpha * self.sp_graph.w + np.log(term_0)
        # term_2 = self.sp_graph.Delta.T @ self.sp_graph.Delta @ (lamb * term_1)
        # self.grad_pen = term_2 * (alpha / term_0)
        term = alpha * self.sp_graph.w + np.log(
            1 - np.exp(-alpha * self.sp_graph.w)
        )  # avoid overflow in exp
        self.grad_pen = self.sp_graph.Delta.T @ self.sp_graph.Delta @ (lamb * term)
        self.grad_pen = self.grad_pen * (alpha / (1 - np.exp(-alpha * self.sp_graph.w))) 

        if self.sp_graph.optimize_q == 'n-dim':
            lamb_q = self.lamb_q
            alpha_q = self.alpha_q
            
            term = alpha_q * self.sp_graph.s2 + np.log(
                1 - np.exp(-alpha_q * self.sp_graph.s2)
            )

            self.grad_pen_q = self.sp_graph.Delta_q.T @ self.sp_graph.Delta_q @ (lamb_q * term)
            self.grad_pen_q = self.grad_pen_q * (alpha_q / (1 - np.exp(-alpha_q * self.sp_graph.s2)))

    def inv(self):
        """Computes relevant inverses for gradient computations"""
        # compute inverses
        self._solve_lap_sys()
        self._comp_mat_block_inv()
        self._comp_inv_cov()

    def grad(self, reg=True):
        """Computes relevent gradients the objective"""
        # compute derivatives
        if self.sp_graph.option == 'default':
            self._comp_grad_obj()
        elif self.sp_graph.option == 'onlyc':
            self._comp_grad_obj_c()

        if reg is True:
            self._comp_grad_reg()

    def neg_log_lik(self):
        """Evaluate the negative log-likelihood function given the current
        params"""

        o = self.sp_graph.n_observed_nodes
        self.trA = self.sp_graph.S @ self.inv_cov

        # trace
        self.trB = self.inv_cov_sum @ self.trA.sum(axis=1)
        self.tr = np.trace(self.trA) - self.trB / self.denom

        # det
        # E = self.X + np.diag(self.sp_graph.q)
        # self.det = np.linalg.det(self.inv_cov) * o / self.denom

        # VS: made a change here to accommodate larger data sets (was leading to overflow without the log)
        self.logdet = np.linalg.slogdet(self.inv_cov)[1]

        # negative log-likelihood
        # nll = self.sp_graph.n_snps * (self.tr - np.log(self.det))
        nll = self.sp_graph.n_snps * (self.tr - self.logdet - np.log(o/self.denom))

        return nll

    def loss(self):
        """Evaluate the loss function given the current params"""
        lamb = self.lamb
        alpha = self.alpha

        if self.sp_graph.option == 'default':
            lik = self.neg_log_lik()
        else:
            lik = self.eems_neg_log_lik(self.sp_graph.c, opts={'mode':'compute','edge':self.sp_graph.edge})

        term_0 = 1.0 - np.exp(-alpha * self.sp_graph.w)
        term_1 = alpha * self.sp_graph.w + np.log(term_0)
        pen = 0.5 * lamb * np.linalg.norm(self.sp_graph.Delta @ term_1) ** 2
                
        if self.sp_graph.optimize_q == 'n-dim':
            lamb_q = self.lamb_q
            alpha_q = self.alpha_q
                
            term_0 = 1.0 - np.exp(-alpha_q * self.sp_graph.s2)
            term_1 = alpha_q * self.sp_graph.s2 + np.log(term_0)
            pen += 0.5 * lamb_q * np.linalg.norm(self.sp_graph.Delta_q @ term_1) ** 2  

        # loss
        loss = lik + pen
        return loss 

    def eems_neg_log_lik(self, c=None, opts=None):
        """Function to compute the negative log-likelihood of the model using the EEMS framework (*will* differ from the value output by obj.neg_log_lik() which uses the FEEMS framework *and* does not incorporate source fraction c)"""

        if c is not None:
            # lre passed in as permuted_idx
            if opts is not None:
                opts['lre'] = []
                for edge in opts['edge']:
                    sid = np.where(self.sp_graph.perm_idx == edge[0])[0][0]
                    did = np.where(self.sp_graph.perm_idx == edge[1])[0][0]
                    assert did < self.sp_graph.n_observed_nodes, "ensure that the destination is a sampled deme (check ID from the map or from output of extract_outliers)"
                    opts['lre'].append((sid,did))

            if opts['mode'] != 'update':
                dd = self._compute_delta_matrix(c, opts)
                try:
                    res = dd[1:,1:] + dd[0,0] - dd[0,1:].reshape(1,-1) - dd[1:,0].reshape(-1,1) 
                    nll = -wishart.logpdf(-self.CDCt, self.sp_graph.n_snps, -res/self.sp_graph.n_snps)
                except: 
                    nll = np.inf
            else:
                opts['delta'] = self._compute_delta_matrix(c, opts)
                try:
                    res = opts['delta'][1:,1:] + opts['delta'][0,0] - opts['delta'][0,1:].reshape(1,-1) - opts['delta'][1:,0].reshape(-1,1) 
                    nll = -wishart.logpdf(-self.CDCt, self.sp_graph.n_snps, -res/self.sp_graph.n_snps)
                except:
                    nll = np.inf
        else:
            dd = self._compute_delta_matrix(None, {})
            if opts is not None:
                if opts['mode'] == 'update':
                    opts['delta'] = dd
            
            try:
                res = dd[1:,1:] + dd[0,0] - dd[0,1:].reshape(1,-1) - dd[1:,0].reshape(-1,1)
                nll = -wishart.logpdf(-self.CDCt, self.sp_graph.n_snps, -res/self.sp_graph.n_snps)
            except:
                nll = np.inf
                   
        return nll

    ## checked in simulations to ensure that it gives the same distance matrix with c=0 as FEEMS
    def _compute_delta_matrix(self, cvals, opts):
        """(internal function) Compute a new delta matrix given a previous delta matrix as a perturbation from multiple long range gene flow events OR create a new delta matrix from resmat 
        """

        # do not recompute inverses if already exists
        # if not hasattr(self, 'Linv'):
        #     self.inv(); self.grad(reg=False); self.Linv_diag = self._comp_diag_pinv()

        Rmat = -2*self.Linv[:self.sp_graph.n_observed_nodes, :self.sp_graph.n_observed_nodes] + np.broadcast_to(np.diag(self.Linv),(self.sp_graph.n_observed_nodes, self.sp_graph.n_observed_nodes)).T + np.broadcast_to(np.diag(self.Linv), (self.sp_graph.n_observed_nodes, self.sp_graph.n_observed_nodes)) 
        Q1mat = np.broadcast_to(self.sp_graph.q_inv_diag.diagonal(), (self.sp_graph.n_observed_nodes, self.sp_graph.n_observed_nodes))
        
        resmat = Rmat + (Q1mat + Q1mat.T) - 2*self.sp_graph.q_inv_diag 
        if cvals is None:
            return np.array(resmat)

        for c, lre in zip(cvals, opts['lre']):
            source, target = lre

            if source < self.sp_graph.n_observed_nodes:
                # Case where source is a sampled deme
                resmat[source, target] += (0.5 * c**2 - 1.5 * c) * Rmat[source, target] + \
                                          c * Q1mat[source, source] - c * Q1mat[target, target]
                resmat[target, source] = resmat[source, target]
    
                for i in set(range(self.sp_graph.n_observed_nodes)) - {source, target}:
                    resmat[i, target] +=  - c * Rmat[i, target] + c * Rmat[i, source] + 0.5 * (c**2 - c) * Rmat[source, target] + \
                                          - c * Q1mat[target, target] + c * Q1mat[source, source]
                    resmat[target, i] = resmat[i, target]
            else:
                # Case where source is an unsampled deme
                R1d = -2 * self.Linv[source, target] + self.Linv_diag[source] + self.Linv[target, target]
    
                for i in set(range(self.sp_graph.n_observed_nodes)) - {source, target}: 
                    Ri1 = -2 * self.Linv[source, i] + self.Linv[i, i] + self.Linv_diag[source]
                    resmat[i, target] += - c * Rmat[i, target] + c * Ri1 + 0.5 * (c**2 - c) * R1d + \
                                         - c * Q1mat[target, target] + c * self.sp_graph.q_prox[source - self.sp_graph.n_observed_nodes]
                    resmat[target, i] = resmat[i, target]

        return np.array(resmat)

def neg_log_lik_w0_s2(z, obj):
    """Computes negative log likelihood for a constant w and residual variance"""
    z = np.clip(z, -20, 20)
    theta = np.exp(z)
    obj.lamb = 0.0
    obj.alpha = 1.0
    
    obj.sp_graph.w = theta[0] * np.ones(obj.sp_graph.size())
    obj.sp_graph.comp_graph_laplacian(obj.sp_graph.w)
    obj.sp_graph.comp_precision(s2=theta[1])
    obj.inv()
    nll = obj.neg_log_lik()
    
    return nll


def loss_wrapper(z, obj):
    """Wrapper function to optimize z=log(w,q) which returns the loss and gradient
    (v2.0: changed to include node-specific variances as parameters)"""                
    n_edges = obj.sp_graph.size()
    if obj.sp_graph.optimize_q is not None:
        z = np.clip(z, -20, 20)
        theta = np.exp(z)
        theta0 = theta[:n_edges]
        obj.sp_graph.comp_graph_laplacian(theta0)
        # if obj.optimize_q is not None:
        theta1 = theta[n_edges:]
        obj.sp_graph.comp_precision(s2=theta1)
    else:
        z = np.clip(z, -20, 20)
        theta = np.exp(z)
        obj.sp_graph.comp_graph_laplacian(theta)
    obj.inv()
    obj.grad() 

    # loss / grad
    loss = obj.loss()
    if obj.sp_graph.optimize_q is None:
        grad = obj.grad_obj * obj.sp_graph.w + obj.grad_pen * obj.sp_graph.w
    elif obj.sp_graph.optimize_q == 'n-dim':
        grad = np.zeros_like(theta)
        grad[:n_edges] = obj.grad_obj * obj.sp_graph.w + obj.grad_pen * obj.sp_graph.w
        grad[n_edges:] = obj.grad_obj_q * obj.sp_graph.s2 + obj.grad_pen_q * obj.sp_graph.s2
    else:
        grad = np.zeros_like(theta)
        grad[:n_edges] = obj.grad_obj * obj.sp_graph.w + obj.grad_pen * obj.sp_graph.w
        grad[n_edges:] = obj.grad_obj_q * obj.sp_graph.s2    

    return (loss, grad)

def comp_mats(obj):
    """Compute fitted covariance matrix and its inverse & empirical convariance matrix"""
    obj.inv()
    obj.grad(reg=False)
    sp_graph = obj.sp_graph
    d = len(sp_graph)
    fit_cov = obj.Linv_block['oo'] - 1/d + sp_graph.q_inv_diag.toarray()
    
    inv_cov_sum0 = obj.inv_cov.sum(axis=0)
    inv_cov_sum1 = obj.inv_cov.sum()
    inv_cov = obj.inv_cov + np.outer(inv_cov_sum0, inv_cov_sum0) / (d - inv_cov_sum1)    
    
    assert np.allclose(inv_cov, np.linalg.inv(fit_cov)) == True, "fit_cov must be inverse of inv_cov"
    
    n_snps = sp_graph.n_snps
    
    # VS: changing code here to run even when scale_snps=False
    if hasattr(obj.sp_graph.q, 'mu'):
        frequencies_ns = sp_graph.frequencies * np.sqrt(sp_graph.mu*(1-sp_graph.mu))
        mu0 = frequencies_ns.mean(axis=0) / 2 # compute mean of allele frequencies in the original scale
        mu = 2*mu0 / np.sqrt(sp_graph.mu*(1-sp_graph.mu))
        frequencies_centered = sp_graph.frequencies - mu
    else:
        frequencies_centered = sp_graph.frequencies

    emp_cov = frequencies_centered @ frequencies_centered.T / n_snps
    
    return fit_cov, inv_cov, emp_cov

def exponential_variogram(h, nugget, sill, rangep):
    return nugget + sill * (1 - np.exp(-h / rangep))

def fit_variogram(distances, values):
    """Function to return paramters from fitting an exponential variogram on the 
    estimated q values inferred by FEEMS
    """
    def objective(params):
        nugget, sill, rangep = params
        h = distances.flatten()
        gamma = 0.5 * np.power(values[:, None] - values[None, :], 2).flatten()
        var_model = exponential_variogram(h, nugget, sill, rangep)
        return np.sqrt(np.mean((gamma - var_model)**2))

    result = minimize(objective, [0, np.var(values), np.mean(distances)], method='Nelder-Mead', bounds=((0, None), (1e-10, None), (1e-10, None)))
    return result.x

def interpolate_q(observed_values, distances_to_observed, distances_between_observed):
    """Function to interpolate the q values across the entire grid given the 
    fit variogram 
    """
    n_observed = len(observed_values)
    n_target = distances_to_observed.shape[0]
    
    # Fit variogram
    nugget, sill, rangep = fit_variogram(distances_between_observed, observed_values)
    
    # Construct kriging matrices
    K = exponential_variogram(distances_between_observed, nugget, sill, rangep)
    K = K + 1e-8 * np.eye(n_observed)  # Add small value to diagonal for numerical stability
    k = exponential_variogram(distances_to_observed, nugget, sill, rangep)

    # Add a column of ones to K and k for the lagrange multiplier
    K = np.column_stack((K, np.ones(n_observed)))
    K = np.row_stack((K, np.ones(n_observed + 1)))
    K[-1, -1] = 0
    k = np.column_stack((k, np.ones((n_target, 1))))
    
    # Solve kriging equation
    weights = np.linalg.solve(K, k.T)
    
    # Perform interpolation
    interpolated_values = np.dot(weights[:-1, :].T, observed_values)
    
    return interpolated_values
    