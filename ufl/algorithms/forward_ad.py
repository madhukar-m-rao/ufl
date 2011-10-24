"""Forward mode AD implementation."""

# Copyright (C) 2008-2011 Martin Sandve Alnes
#
# This file is part of UFL.
#
# UFL is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# UFL is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with UFL. If not, see <http://www.gnu.org/licenses/>.
#
# Modified by Anders Logg, 2009.
# Modified by Garth N. Wells, 2010.
# Modified by Kristian B. Oelgaard, 2011
#
# First added:  2008-08-19
# Last changed: 2011-10-20

from math import pi
from ufl.log import error, warning, debug
from ufl.assertions import ufl_assert
from ufl.common import unzip, subdict, lstr
from ufl.indexutils import unique_indices

# All classes:
from ufl.terminal import Terminal, Tuple
from ufl.constantvalue import ConstantValue, Zero, IntValue, Identity,\
    is_true_ufl_scalar, is_ufl_scalar
from ufl.variable import Variable
from ufl.coefficient import ConstantBase
from ufl.indexing import MultiIndex, Indexed, Index, indices
from ufl.indexsum import IndexSum
from ufl.tensors import ListTensor, ComponentTensor, as_tensor, as_scalar
from ufl.algebra import Sum, Product, Division, Power, Abs
from ufl.tensoralgebra import Transposed, Outer, Inner, Dot, Cross, Trace, \
    Determinant, Inverse, Deviatoric, Cofactor
from ufl.mathfunctions import MathFunction, Sqrt, Exp, Ln, Cos, Sin, Tan, Acos, Asin, Atan, Erf, BesselJ, BesselY, BesselI, BesselK
from ufl.restriction import Restricted, PositiveRestricted, NegativeRestricted
from ufl.differentiation import Derivative, CoefficientDerivative,\
    SpatialDerivative, VariableDerivative
from ufl.conditional import EQ, NE, LE, GE, LT, GT, Conditional

# Lists of all Expr classes
from ufl.classes import terminal_classes

from ufl.operators import dot, inner, outer, lt, eq, conditional, sign
from ufl.operators import sqrt, exp, ln, cos, sin, tan, acos, asin, atan, \
    erf, bessel_J, bessel_Y, bessel_I, bessel_K
from ufl.algorithms.traversal import iter_expressions
from ufl.algorithms.analysis import extract_type
from ufl.algorithms.transformations import expand_compounds, Transformer, transform, transform_integrands

from ufl.differentiation import is_spatially_constant

class ForwardAD(Transformer):
    def __init__(self, spatial_dim, var_shape, var_free_indices, var_index_dimensions, cache=None):
        Transformer.__init__(self)
        ufl_assert(all(isinstance(i, Index) for i in var_free_indices), \
            "Expecting Index objects.")
        ufl_assert(all(isinstance(i, Index) for i in var_index_dimensions.keys()), \
            "Expecting Index objects.")
        self._spatial_dim = spatial_dim
        self._var_shape = var_shape
        self._var_free_indices = var_free_indices
        self._var_index_dimensions = dict(var_index_dimensions)
        #if self._var_free_indices:
        #    warning("TODO: Free indices in differentiation variable may be buggy!")
        self._cache = {} if cache is None else cache

    def _cache_visit(self, o):
        "Cache hook, disable this by renaming to something else than 'visit'."
        #debug("Visiting object of type %s." % type(o).__name__)

        # TODO: This doesn't work, why?

        # NB! Cache added in after copying from Transformer
        c = self._cache.get(o)
        if c is not None:
            return c

        # Reuse default visit function
        r = Transformer.visit(self, o)

        if (c is not None):
            if r[0].free_indices() != c[0].free_indices():
                print "="*70
                print "=== f: Difference between cache and recomputed indices:"
                print str(c[0].free_indices())
                print str(r[0].free_indices())
                print "="*70
            if r[1].free_indices() != c[1].free_indices():
                print "="*70
                print "=== df: Difference between cache and recomputed indices:"
                print str(c[1].free_indices())
                print str(r[1].free_indices())
                print "="*70
            if (r != c):
                print "="*70
                print "=== Difference between cache and recomputed:"
                print str(c[0])
                print str(c[1])
                print "-"*40
                print str(r[0])
                print str(r[1])
                print "="*70

        # NB! Cache added in after copying from Transformer
        self._cache[o] = r

        return r

    def _debug_visit(self, o):
        "Debugging hook, enable this by renaming to 'visit'."
        r = Transformer.visit(self, o)
        f, df = r
        if not f is o:
            debug("In ForwardAD.visit, didn't get back o:")
            debug("  o:  %s" % str(o))
            debug("  f:  %s" % str(f))
            debug("  df: %s" % str(df))
        fi_diff = set(f.free_indices()) ^ set(df.free_indices())
        if fi_diff:
            debug("In ForwardAD.visit, got free indices diff:")
            debug("  o:  %s" % str(o))
            debug("  f:  %s" % str(f))
            debug("  df: %s" % str(df))
            debug("  f.fi():  %s" % lstr(f.free_indices()))
            debug("  df.fi(): %s" % lstr(df.free_indices()))
            debug("  fi_diff: %s" % str(fi_diff))
        return r

    def _make_zero_diff(self, o):
        # Define a zero with the right indices
        # (kind of cumbersome this... any simpler way?)
        sh = o.shape() + self._var_shape
        fi = o.free_indices()
        idims = dict(o.index_dimensions())
        if self._var_free_indices:
            # Currently assuming only one free variable index
            i, = self._var_free_indices
            if i not in idims:
                fi = unique_indices(fi + (i,))
                idims[i] = self._var_index_dimensions[i]
        fp = Zero(sh, fi, idims)
        return fp

    def _make_ones_diff(self, o):
        ufl_assert(o.shape() == self._var_shape, "This is only used by VariableDerivative, yes?")
        # Define a scalar value with the right indices
        # (kind of cumbersome this... any simpler way?)

        sh = o.shape()

        fi = o.free_indices()
        idims = dict(o.index_dimensions())

        if self._var_free_indices:
            # Currently assuming only one free variable index
            i, = self._var_free_indices
            if i not in idims:
                fi = unique_indices(fi + (i,))
                idims[i] = self._var_index_dimensions[i]

        one = IntValue(1, (), fi, idims)
        if not sh:
            return one

        res = None
        ind1 = ()
        ind2 = ()
        for d in sh:
            i, j = indices(2)
            dij = Identity(d)[i, j]
            if res is None:
                res = dij
            else:
                res *= dij
            ind1 += (i,)
            ind2 += (j,)

        allind = ind1 + ind2

        fp = as_tensor(res, allind)
        if fi:
            fp *= one
        return fp

    # --- Default rules

    def expr(self, o):
        error("Missing ForwardAD handler for type %s" % str(type(o)))

    def terminal(self, o):
        """Terminal objects are assumed independent of the differentiation
        variable by default, and simply 'lifted' to the pair (o, 0).
        Depending on the context, override this with custom rules for
        non-zero derivatives."""
        fp = self._make_zero_diff(o)
        return (o, fp)

    def variable(self, o):
        """Variable objects are just 'labels', so by default the derivative
        of a variable is the derivative of its referenced expression."""
        # Check variable cache to reuse previously transformed variable if possible
        e, l = o.operands()
        r = self._variable_cache.get(l) # cache contains (v, vp) tuple
        if r is not None:
            return r

        # Visit the expression our variable represents
        e2, vp = self.visit(e)

        # Recreate Variable (with same label) only if necessary
        v = self.reuse_if_possible(o, e2, l)

        # Cache and return (v, vp) tuple
        r = (v, vp)
        self._variable_cache[l] = r
        return r

    # --- Indexing and component handling

    def multi_index(self, o):
        return (o, None) # oprime here should never be used, this might even not be called?

    def indexed(self, o):
        A, jj = o.operands()
        A2, Ap = self.visit(A)
        o = self.reuse_if_possible(o, A2, jj)

        if isinstance(Ap, Zero):
            op = self._make_zero_diff(o)
        else:
            r = Ap.rank() - len(jj)
            if r:
                ii = indices(r)
                op = Indexed(Ap, jj._indices + ii)
                op = as_tensor(op, ii)
            else:
                op = Indexed(Ap, jj)
        return (o, op)

    def list_tensor(self, o, *ops):
        ops, dops = unzip(ops)
        o = self.reuse_if_possible(o, *ops)
        op = ListTensor(*dops)
        return (o, op)

    def component_tensor(self, o):
        A, ii = o.operands()
        A, Ap = self.visit(A)
        o = self.reuse_if_possible(o, A, ii)

        if isinstance(Ap, Zero):
            op = self._make_zero_diff(o)
        else:
            Ap, jj = as_scalar(Ap)
            op = ComponentTensor(Ap, ii._indices + jj)
        return (o, op)

    # --- Algebra operators

    def index_sum(self, o):
        A, i = o.operands()

        # Consider the following case:
        #   (v[i]*u[i]).dx(i)
        # which is represented like
        #   SpatialDerivative(IndexSum(..., i), i)
        # if we move the derivative inside the sum,
        # then the derivative suddenly gets accumulated,
        # which is completely wrong!
        if self._var_free_indices:
            if i[0] in self._var_free_indices:
                error("Index scope collision. Work around this by reusing indices less in different expressions.\n"\
                      "An example where this occurs is (v[i]*v[i]).dx(i) where the same index i\n"\
                      "is bound to an index sum inside the derivative .dx(i).")

                # TODO: OR get around this by temporarily setting _var_free_indices to nothing?
                #store = self._var_free_indices
                #self._var_free_indices = nothing?
                #visit children
                #self._var_free_indices = store
                # But... What would this mean? Will it be correct?

                # TODO: Get around this with relabeling algorithm!
                # j = Index()
                # A = relabel(A, { i0: j })
                # i = (j,)

        A2, Ap = self.visit(A)
        o = self.reuse_if_possible(o, A2, i)
        op = IndexSum(Ap, i)
        return (o, op)

    def sum(self, o, *ops):
        ops, opsp = unzip(ops)
        o2 = self.reuse_if_possible(o, *ops)
        op = sum(opsp[1:], opsp[0])
        return (o2, op)

    def product(self, o, *ops):
        # Start with a zero with the right shape and indices
        fp = self._make_zero_diff(o)
        # Get operands and their derivatives
        ops2, dops2 = unzip(ops)
        o = self.reuse_if_possible(o, *ops2)
        for (i, op) in enumerate(ops):
            # Get scalar representation of differentiated value of operand i
            dop = dops2[i]
            dop, ii = as_scalar(dop)
            # Replace operand i with its differentiated value in product
            fpoperands = ops2[:i] + [dop] + ops2[i+1:]
            p = Product(*fpoperands)
            # Wrap product in tensor again
            if ii:
                p = as_tensor(p, ii)
            # Accumulate terms
            fp += p
        return (o, fp)

    def division(self, o, a, b):
        f, fp = a
        g, gp = b
        o = self.reuse_if_possible(o, f, g)

        ufl_assert(is_ufl_scalar(f), "Not expecting nonscalar nominator")
        ufl_assert(is_true_ufl_scalar(g), "Not expecting nonscalar denominator")

        #do_df = 1/g
        #do_dg = -h/g
        #op = do_df*fp + do_df*gp
        #op = (fp - o*gp) / g

        # Get o and gp as scalars, multiply, then wrap as a tensor again
        so, oi = as_scalar(o)
        sgp, gi = as_scalar(gp)
        o_gp = so*sgp
        if oi or gi:
            o_gp = as_tensor(o_gp, oi + gi)
        op = (fp - o_gp) / g

        return (o, op)

    def power(self, o, a, b):
        f, fp = a
        g, gp = b

        # Debugging prints, should never happen:
        if not is_true_ufl_scalar(f):
            print ":"*80
            print "f =", str(f)
            print "g =", str(g)
            print ":"*80
        ufl_assert(is_true_ufl_scalar(f), "Expecting scalar expression f in f**g.")
        ufl_assert(is_true_ufl_scalar(g), "Expecting scalar expression g in f**g.")

        # Derivation of the general case: o = f(x)**g(x)
        #
        #do_df = g * f**(g-1)
        #do_dg = ln(f) * f**g
        #op = do_df*fp + do_dg*gp
        #
        #do_df = o * g / f # f**g * g / f
        #do_dg = ln(f) * o
        #op = do_df*fp + do_dg*gp

        # Got two possible alternatives here:
        if True: # This version looks better.
            # Rewriting o as f*f**(g-1) we can do:
            f_g_m1 = f**(g-1)
            op = f_g_m1*(fp*g + f*ln(f)*gp)
            # In this case we can rewrite o using new subexpression
            o = f*f_g_m1
        else:
            # Pulling o out gives:
            op = o*(fp*g/f + ln(f)*gp)
            # This produces expressions like (1/w)*w**5 instead of w**4
            # If we do this, we reuse o
            o = self.reuse_if_possible(o, f, g)

        return (o, op)

    def abs(self, o, a):
        f, fprime = a
        o = self.reuse_if_possible(o, f)
        #oprime = conditional(eq(f, 0), 0, Product(sign(f), fprime))
        oprime = sign(f)*fprime
        return (o, oprime)

    # --- Mathfunctions

    def math_function(self, o, a):
        if hasattr(o, 'derivative'): # FIXME: Introduce a UserOperator type instead of this hack
            f, fp = a
            o = self.reuse_if_possible(o, f)
            op = fp * o.derivative()
            return (o, op)
        error("Unknown math function.")

    def sqrt(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp / (2*o)
        return (o, op)

    def exp(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp*o
        return (o, op)

    def ln(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        ufl_assert(not isinstance(f, Zero), "Division by zero.")
        return (o, fp/f)

    def cos(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = -fp*sin(f)
        return (o, op)

    def sin(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp*cos(f)
        return (o, op)

    def tan(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp*2.0/(cos(2.0*f) + 1.0)
        return (o, op)

    def acos(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = -fp/sqrt(1.0 - f**2)
        return (o, op)

    def asin(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp/sqrt(1.0 - f**2)
        return (o, op)

    def atan(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp/(1.0 + f**2)
        return (o, op)

    def erf(self, o, a):
        f, fp = a
        o = self.reuse_if_possible(o, f)
        op = fp*(2.0/sqrt(pi)*exp(-f**2))
        return (o, op)

    def bessel_j(self, o, nu, x):
        nu, dummy = nu
        if not (dummy is None or isinstance(dummy, Zero)):
            error("Differentiation of bessel function w.r.t. nu is not supported.")
        f, fp = x
        o = self.reuse_if_possible(o, nu, f)
        if nu == 0:
            op = -bessel_J(1, f)
        else:
            op = 0.5 * (bessel_J(nu-1, f) - bessel_J(nu+1, f))
        return (o, op)

    def bessel_y(self, o, nu, x):
        nu, dummy = nu
        if not (dummy is None or isinstance(dummy, Zero)):
            error("Differentiation of bessel function w.r.t. nu is not supported.")
        f, fp = x
        o = self.reuse_if_possible(o, nu, f)
        if nu == 0:
            op = -bessel_Y(1, f)
        else:
            op = 0.5 * (bessel_Y(nu-1, f) - bessel_Y(nu+1, f))
        return (o, op)

    def bessel_i(self, o, nu, x):
        nu, dummy = nu
        if not (dummy is None or isinstance(dummy, Zero)):
            error("Differentiation of bessel function w.r.t. nu is not supported.")
        f, fp = x
        o = self.reuse_if_possible(o, nu, f)
        if nu == 0:
            op = bessel_I(1, f)
        else:
            op = 0.5 * (bessel_I(nu-1, f) + bessel_I(nu+1, f))
        return (o, op)

    def bessel_k(self, o, nu, x):
        nu, dummy = nu
        if not (dummy is None or isinstance(dummy, Zero)):
            error("Differentiation of bessel function w.r.t. nu is not supported.")
        f, fp = x
        o = self.reuse_if_possible(o, nu, f)
        if nu == 0:
            op = -bessel_K(1, f)
        else:
            op = -0.5 * (bessel_K(nu-1, f) + bessel_K(nu+1, f))
        return (o, op)

    # --- Restrictions

    def restricted(self, o, a):
        # Restriction and differentiation commutes.
        f, fp = a
        o = self.reuse_if_possible(o, f)
        if isinstance(fp, ConstantValue):
            return (o, fp) # TODO: Necessary? Can't restriction simplify directly instead?
        else:
            return (o, fp(o._side)) # (f+-)' == (f')+-

    # --- Conditionals

    def binary_condition(self, o, l, r):
        o = self.reuse_if_possible(o, l[0], r[0])
        if any(not isinstance(op[1], Zero) for op in (l, r)):
            warning("Differentiating a conditional with a condition "\
                        "that depends on the differentiation variable."\
                        "This is probably not a good idea!")
        oprime = None # Shouldn't be used anywhere
        return (o, oprime)

    def not_condition(self, o, c):
        o = self.reuse_if_possible(o, c[0])
        if not isinstance(c[1], Zero):
            warning("Differentiating a conditional with a condition "\
                        "that depends on the differentiation variable."\
                        "This is probably not a good idea!")
        oprime = None # Shouldn't be used anywhere
        return (o, oprime)

    def conditional(self, o, c, t, f):
        o = self.reuse_if_possible(o, c[0], t[0], f[0])
        if isinstance(t[1], Zero) and isinstance(f[1], Zero):
            fi = o.free_indices()
            fid = subdict(o.index_dimensions(), fi)
            op = Zero(o.shape(), fi, fid)
        else:
            op = conditional(c[0], t[1], f[1])
        return (o, op)

    # --- Other derivatives

    def derivative(self, o):
        error("This should never occur.")

    def spatial_derivative(self, o):
        # If we hit this type, it has already been propagated
        # to a terminal, so we can simply apply our derivative
        # to its operand since differentiation commutes.
        f, ii = o.operands()
        f, fp = self.visit(f)
        o = self.reuse_if_possible(o, f, ii)

        # FIXME: Make plenty of test cases around this kind of situation to document what's going on...
        # NB! This will trigger if fp has no cell. Undefined cells from PyDOLFIN make me want to cry...
        if is_spatially_constant(fp):
            sh = fp.shape()
            fi = fp.free_indices()
            idims = fp.index_dimensions()
            j, = ii
            if isinstance(j, Index) and j not in idims:
                fi = fi + (j,)
                idims.update(ii.index_dimensions())
            oprime = Zero(sh, fi, idims)
            #oprime = self._make_zero_diff(o) # FIXME: Can we just use this?
        else:
            oprime = SpatialDerivative(fp, ii)
        return (o, oprime)

class SpatialAD(ForwardAD):
    def __init__(self, spatial_dim, index, cache=None):
        index, = index
        if isinstance(index, Index):
            vfi = (index,)
            vid = { index: spatial_dim }
        else:
            vfi = ()
            vid = {}
        ForwardAD.__init__(self, spatial_dim, var_shape=(), var_free_indices=vfi,
                           var_index_dimensions=vid, cache=cache)
        self._index = index

    def spatial_coordinate(self, o):
        # Need to define dx_i/dx_j = delta_ij?
        if o.shape() == ():
            return (o, IntValue(1))
        else:
            I = Identity(self._spatial_dim)
            oprime = I[:, self._index]
            return (o, oprime)

    # This is implicit for all terminals, but just to make this clear:
    facet_normal = ForwardAD.terminal # returns zero
    constant = ForwardAD.terminal # returns zero

    def form_argument(self, o):
        # Using this index in here may collide with the
        # same index on the outside! (There's a check for
        # this situation in index_sum above.)
        #oprime = o.dx(self._index) # Not using this syntax because it would create a new IndexSum
        oprime = SpatialDerivative(o, self._index)
        return (o, oprime)

class VariableAD(ForwardAD):
    def __init__(self, spatial_dim, var, cache=None):
        ForwardAD.__init__(self, spatial_dim, var_shape=var.shape(),
                           var_free_indices=var.free_indices(),
                           var_index_dimensions=var.index_dimensions(),
                           cache=cache)
        self._variable = var

    def variable(self, o):
        # Check cache
        e, l = o.operands()
        c = self._variable_cache.get(l)

        if c is not None:
            return c

        if o.label() == self._variable.label():
            # dv/dv = "1"
            op = self._make_ones_diff(o)
        else:
            # differentiate expression behind variable
            e2, ep = self.visit(e)
            op = ep
            if not e2 == e:
                o = Variable(e2, l)
        # return variable and derivative of its expression
        c = (o, op)
        self._variable_cache[l] = c
        return c

class CoefficientAD(ForwardAD):
    "Apply AFD (Automatic Functional Differentiation) to expression."
    def __init__(self, spatial_dim, coefficients, arguments, coefficient_derivatives, cache=None):
        ForwardAD.__init__(self, spatial_dim, var_shape=(), var_free_indices=(), var_index_dimensions={}, cache=cache)
        self._functions = zip(coefficients, arguments)
        self._v = arguments
        self._w = coefficients
        self._cd = coefficient_derivatives
        ufl_assert(isinstance(self._w, Tuple), "Eh?") # Greatest error message ever? :)
        ufl_assert(isinstance(self._v, Tuple), "Eh?")
        # Define dw/dw := v (what we really mean by d/dw is d/dw_j,
        # where w = sum_j w_j phi_j, and what we really mean by v is phi_j for any j)

    def coefficient(self, o):
        debug("In CoefficientAD.coefficient:")
        debug("o = %s" % o)
        debug("self._w = %s" % self._w)
        debug("self._v = %s" % self._v)

        # Find o among w
        for (w, v) in zip(self._w, self._v):
            if o == w:
                return (w, v)

        # If o is not among coefficient derivatives, return do/dw=0
        oprimesum = Zero(o.shape())
        oprimes = self._cd._data.get(o)
        if oprimes is None:
            if self._cd._data:
                # TODO: Make it possible to silence this message in particular?
                #       It may be good to have for debugging...
                warning("Assuming d{%s}/d{%s} = 0." % (o, self._w))
        else:
            # Make sure we have a tuple to match the self._v tuple
            if not isinstance(oprimes, tuple):
                oprimes = (oprimes,)
                ufl_assert(len(oprimes) == len(self._v), "Got a tuple of arguments, "+\
                               "expecting a matching tuple of coefficient derivatives.")

            # Compute do/dw_j = do/dw_h : N_j, where v in {N_j}.
            # Since we may actually have a tuple of oprimes and vs in a
            # 'mixed' space, sum over them all to get the complete inner
            # product. Using indices to define a non-compound inner product.
            for (oprime, v) in zip(oprimes, self._v):
                so, oi = as_scalar(oprime)
                rv = len(v.shape())
                oi1 = oi[:-rv]
                oi2 = oi[-rv:]
                prod = so*v[oi2]
                if oi1:
                    oprimesum += as_tensor(prod, oi1)
                else:
                    oprimesum += prod

        # Example:
        # (f : g) -> (dfdu : v) : g + ditto
        # shape(f) == shape(g) == shape(dfdu : v)
        # shape(dfdu) == shape(f) + shape(v)

        return (o, oprimesum)

    def variable(self, o):
        # Check variable cache to reuse previously transformed variable if possible
        e, l = o.operands()
        c = self._variable_cache.get(l)
        if c is not None:
            return c

        # Visit the expression our variable represents
        e2, ep = self.visit(e)
        op = ep

        # If the expression is not the same, reconstruct Variable object
        o = self.reuse_if_possible(o, e2, l)

        # Recreate Variable (with same label) and cache it
        c = (o, op)
        self._variable_cache[l] = c
        return c

def compute_spatial_forward_ad(expr, dim):
    f, v = expr.operands()
    alg = SpatialAD(dim, v)
    e, ediff = alg.visit(f)
    return ediff

def compute_variable_forward_ad(expr, dim):
    f, v = expr.operands()
    alg = VariableAD(dim, v)
    e, ediff = alg.visit(f)
    return ediff

def compute_coefficient_forward_ad(expr, dim):
    f, w, v, cd = expr.operands()
    alg = CoefficientAD(dim, w, v, cd)
    e, ediff = alg.visit(f)
    return ediff

def forward_ad(expr, dim):
    """Assuming expr is a derivative and contains no other
    unresolved derivatives, apply forward mode AD and
    return the computed derivative."""
    if isinstance(expr, SpatialDerivative):
        result = compute_spatial_forward_ad(expr, dim)
    elif isinstance(expr, VariableDerivative):
        result = compute_variable_forward_ad(expr, dim)
    elif isinstance(expr, CoefficientDerivative):
        result = compute_coefficient_forward_ad(expr, dim)
    else:
        error("This shouldn't happen: expr is %s" % repr(expr))
    return result


# TODO: We could expand only the compound objects that have no rule
#       before differentiating, to allow the AD to work on a coarser graph
class UnusedADRules(object):

    def _variable_derivative(self, o, f, v):
        f, fp = f
        v, vp = v
        ufl_assert(isinstance(vp, Zero), "TODO: What happens if vp != 0, i.e. v depends the differentiation variable?")
        # Are there any issues with indices here? Not sure, think through it...
        oprime = o.reconstruct(fp, v)
        return (o, oprime)

    # --- Compound operators

    def commute(self, o, a):
        "This should work for all single argument operators that commute with d/dw."
        aprime = a[1]
        return (o, o.reconstruct(aprime))

    transposed = commute
    trace = commute
    deviatoric = commute

    # TODO: nabla_div, nabla_grad
    div  = commute
    curl = commute
    def grad(self, o, a):
        a, aprime = a
        c = aprime.cell()
        if c is None or c.is_undefined():
            oprime = self._make_zero_diff(o)
        else:
            oprime = o.reconstruct(aprime)
        return (o, oprime)

    def outer(self, o, a, b):
        a, ap = a
        b, bp = b
        return (o, outer(ap, b) + outer(a, bp))

    def inner(self, o, a, b):
        a, ap = a
        b, bp = b
        return (o, inner(ap, b) + inner(a, bp))

    def dot(self, o, a, b):
        a, ap = a
        b, bp = b
        return (o, dot(ap, b) + dot(a, bp))

class UnimplementedADRules(object):

    def cross(self, o, a, b):
        error("Derivative of cross product not implemented, apply expand_compounds before AD.")
        u, up = a
        v, vp = b
        #oprime = ...
        return (o, oprime)

    def determinant(self, o, a):
        error("Derivative of determinant not implemented, apply expand_compounds before AD.")
        A, Ap = a
        #oprime = ...
        return (o, oprime)

    def cofactor(self, o, a):
        error("Derivative of cofactor not implemented, apply expand_compounds before AD.")
        A, Ap = a
        #cofacA_prime = detA_prime*Ainv + detA*Ainv_prime
        #oprime = ...
        return (o, oprime)

    def inverse(self, o, a):
        """Derivation:
        0 = d/dx [Ainv*A] = Ainv' * A + Ainv * A'
        Ainv' * A = - Ainv * A'
        Ainv' = - Ainv * A' * Ainv
        """
        A, Ap = a
        return (o, -o*Ap*o) # Any potential index problems here?
