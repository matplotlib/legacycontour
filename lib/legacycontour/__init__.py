from contextlib import contextmanager
import functools
import inspect
import os
import sys
import warnings

import matplotlib as mpl
from matplotlib.cbook import dedent
import six

from legacycontour.contourset import LegacyContourSet

# Get the version from the _version.py versioneer file. For a git checkout,
# this is computed based on the number of commits since the last tag.
from ._version import get_versions
__version__ = str(get_versions()['version'])
del get_versions

# This is the minimum numpy version supported
__version__numpy__ = str('1.7.1')  # minimum required numpy version

__all__ = ['contour', 'contourf']


# Copied over from the v2.1.2 matplotlib/cbook/__init__.py
def _sanitize_sequence(data):
    """Converts dictview object to list"""
    import collections
    return list(data) if isinstance(data, collections.MappingView) else data


def _get_label(y, default_name):
    try:
        return y.name
    except AttributeError:
        return default_name


# Copied over from the v2.1.2 matplotlib/__init__.py
def _replacer(data, key):
    """Either returns data[key] or passes data back. Also
    converts input data to a sequence as needed.
    """
    # if key isn't a string don't bother
    if not isinstance(key, six.string_types):
        return (key)
    # try to use __getitem__
    try:
        return _sanitize_sequence(data[key])
    # key does not exist, silently fall back to key
    except KeyError:
        return key


_DATA_DOC_APPENDIX = """

.. note::
    In addition to the above described arguments, this function can take a
    **data** keyword argument. If such a **data** argument is given, the
    following arguments are replaced by **data[<arg>]**:

    {replaced}
"""


def _preprocess_data(replace_names=None, replace_all_args=False,
                     label_namer=None, positional_parameter_names=None):
    """
    A decorator to add a 'data' kwarg to any a function.  The signature
    of the input function must include the ax argument at the first position ::

       def foo(ax, *args, **kwargs)

    so this is suitable for use with Axes methods.

    Parameters
    ----------
    replace_names : list of strings, optional, default: None
        The list of parameter names which arguments should be replaced by
        `data[name]`. If None, all arguments are replaced if they are
        included in `data`.
    replace_all_args : bool, default: False
        If True, all arguments in *args get replaced, even if they are not
        in replace_names.
    label_namer : string, optional, default: None
        The name of the parameter which argument should be used as label, if
        label is not set. If None, the label keyword argument is not set.
    positional_parameter_names : list of strings or callable, optional
        The full list of positional parameter names (excluding an explicit
        `ax`/'self' argument at the first place and including all possible
        positional parameter in `*args`), in the right order. Can also include
        all other keyword parameter. Only needed if the wrapped function does
        contain `*args` and (replace_names is not None or replace_all_args is
        False). If it is a callable, it will be called with the actual
        tuple of *args and the data and should return a list like
        above.
        NOTE: callables should only be used when the names and order of *args
        can only be determined at runtime. Please use list of names
        when the order and names of *args is clear before runtime!

    .. note:: decorator also converts MappingView input data to list.
    """
    if replace_names is not None:
        replace_names = set(replace_names)

    def param(func):
        new_sig = None
        # signature is since 3.3 and wrapped since 3.2, but we support 3.4+.
        python_has_signature = python_has_wrapped = six.PY3

        # if in a legacy version of python and IPython is already imported
        # try to use their back-ported signature
        if not python_has_signature and 'IPython' in sys.modules:
            try:
                import IPython.utils.signatures
                signature = IPython.utils.signatures.signature
                Parameter = IPython.utils.signatures.Parameter
            except ImportError:
                pass
            else:
                python_has_signature = True
        else:
            if python_has_signature:
                signature = inspect.signature
                Parameter = inspect.Parameter

        if not python_has_signature:
            arg_spec = inspect.getargspec(func)
            _arg_names = arg_spec.args
            _has_varargs = arg_spec.varargs is not None
            _has_varkwargs = arg_spec.keywords is not None
        else:
            sig = signature(func)
            _has_varargs = False
            _has_varkwargs = False
            _arg_names = []
            params = list(sig.parameters.values())
            for p in params:
                if p.kind is Parameter.VAR_POSITIONAL:
                    _has_varargs = True
                elif p.kind is Parameter.VAR_KEYWORD:
                    _has_varkwargs = True
                else:
                    _arg_names.append(p.name)
            data_param = Parameter('data',
                                   Parameter.KEYWORD_ONLY,
                                   default=None)
            if _has_varkwargs:
                params.insert(-1, data_param)
            else:
                params.append(data_param)
            new_sig = sig.replace(parameters=params)
        # Import-time check: do we have enough information to replace *args?
        arg_names_at_runtime = False
        # there can't be any positional arguments behind *args and no
        # positional args can end up in **kwargs, so only *varargs make
        # problems.
        # http://stupidpythonideas.blogspot.de/2013/08/arguments-and-parameters.html
        if not _has_varargs:
            # all args are "named", so no problem
            # remove the first "ax" / self arg
            arg_names = _arg_names[1:]
        else:
            # Here we have "unnamed" variables and we need a way to determine
            # whether to replace a arg or not
            if replace_names is None:
                # all argnames should be replaced
                arg_names = None
            elif len(replace_names) == 0:
                # No argnames should be replaced
                arg_names = []
            elif len(_arg_names) > 1 and (positional_parameter_names is None):
                # we got no manual parameter names but more than an 'ax' ...
                if len(replace_names - set(_arg_names[1:])) == 0:
                    # all to be replaced arguments are in the list
                    arg_names = _arg_names[1:]
                else:
                    msg = ("Got unknown 'replace_names' and wrapped function "
                           "'%s' uses '*args', need "
                           "'positional_parameter_names'!")
                    raise AssertionError(msg % func.__name__)
            else:
                if positional_parameter_names is not None:
                    if callable(positional_parameter_names):
                        # determined by the function at runtime
                        arg_names_at_runtime = True
                        # so that we don't compute the label_pos at import time
                        arg_names = []
                    else:
                        arg_names = positional_parameter_names
                else:
                    if replace_all_args:
                        arg_names = []
                    else:
                        msg = ("Got 'replace_names' and wrapped function "
                               "'%s' uses *args, need "
                               "'positional_parameter_names' or "
                               "'replace_all_args'!")
                        raise AssertionError(msg % func.__name__)

        # compute the possible label_namer and label position in positional
        # arguments
        label_pos = 9999  # bigger than all "possible" argument lists
        label_namer_pos = 9999  # bigger than all "possible" argument lists
        if (label_namer and  # we actually want a label here ...
                arg_names and  # and we can determine a label in *args ...
                (label_namer in arg_names)):  # and it is in *args
            label_namer_pos = arg_names.index(label_namer)
            if "label" in arg_names:
                label_pos = arg_names.index("label")

        # Check the case we know a label_namer but we can't find it the
        # arg_names... Unfortunately the label_namer can be in **kwargs,
        # which we can't detect here and which results in a non-set label
        # which might surprise the user :-(
        if label_namer and not arg_names_at_runtime and not _has_varkwargs:
            if not arg_names:
                msg = ("label_namer '%s' can't be found as the parameter "
                       "without 'positional_parameter_names'.")
                raise AssertionError(msg % label_namer)
            elif label_namer not in arg_names:
                msg = ("label_namer '%s' can't be found in the parameter "
                       "names (known argnames: %s).")
                raise AssertionError(msg % (label_namer, arg_names))
            else:
                # this is the case when the name is in arg_names
                pass

        @functools.wraps(func)
        def inner(ax, *args, **kwargs):
            # this is needed because we want to change these values if
            # arg_names_at_runtime==True, but python does not allow assigning
            # to a variable in a outer scope. So use some new local ones and
            # set them to the already computed values.
            _label_pos = label_pos
            _label_namer_pos = label_namer_pos
            _arg_names = arg_names

            label = None

            data = kwargs.pop('data', None)

            if data is None:  # data validation
                args = tuple(_sanitize_sequence(a) for a in args)
            else:
                if arg_names_at_runtime:
                    # update the information about replace names and
                    # label position
                    _arg_names = positional_parameter_names(args, data)
                    if (label_namer and  # we actually want a label here ...
                            _arg_names and  # and we can find a label in *args
                            (label_namer in _arg_names)):  # and it is in *args
                        _label_namer_pos = _arg_names.index(label_namer)
                        if "label" in _arg_names:
                            _label_pos = arg_names.index("label")

                # save the current label_namer value so that it can be used as
                # a label
                if _label_namer_pos < len(args):
                    label = args[_label_namer_pos]
                else:
                    label = kwargs.get(label_namer, None)
                # ensure a string, as label can't be anything else
                if not isinstance(label, six.string_types):
                    label = None

                if (replace_names is None) or (replace_all_args is True):
                    # all should be replaced
                    args = tuple(_replacer(data, a) for
                                 j, a in enumerate(args))
                else:
                    # An arg is replaced if the arg_name of that position is
                    #   in replace_names ...
                    if len(_arg_names) < len(args):
                        raise RuntimeError(
                            "Got more args than function expects")
                    args = tuple(_replacer(data, a)
                                 if _arg_names[j] in replace_names else a
                                 for j, a in enumerate(args))

                if replace_names is None:
                    # replace all kwargs ...
                    kwargs = dict((k, _replacer(data, v))
                                  for k, v in six.iteritems(kwargs))
                else:
                    # ... or only if a kwarg of that name is in replace_names
                    kwargs = dict((k, _replacer(data, v)
                                   if k in replace_names else v)
                                  for k, v in six.iteritems(kwargs))

            # replace the label if this func "wants" a label arg and the user
            # didn't set one. Note: if the user puts in "label=None", it does
            # *NOT* get replaced!
            user_supplied_label = (
                (len(args) >= _label_pos) or  # label is included in args
                ('label' in kwargs)  # ... or in kwargs
            )
            if (label_namer and not user_supplied_label):
                if _label_namer_pos < len(args):
                    kwargs['label'] = _get_label(args[_label_namer_pos], label)
                elif label_namer in kwargs:
                    kwargs['label'] = _get_label(kwargs[label_namer], label)
                else:
                    import warnings
                    msg = ("Tried to set a label via parameter '%s' in "
                           "func '%s' but couldn't find such an argument. \n"
                           "(This is a programming error, please report to "
                           "the matplotlib list!)")
                    warnings.warn(msg % (label_namer, func.__name__),
                                  RuntimeWarning, stacklevel=2)
            return func(ax, *args, **kwargs)
        pre_doc = inner.__doc__
        if pre_doc is None:
            pre_doc = ''
        else:
            pre_doc = dedent(pre_doc)
        _repl = ""
        if replace_names is None:
            _repl = "* All positional and all keyword arguments."
        else:
            if len(replace_names) != 0:
                _repl = "* All arguments with the following names: '{names}'."
            if replace_all_args:
                _repl += "\n    * All positional arguments."
            _repl = _repl.format(names="', '".join(sorted(replace_names)))
        inner.__doc__ = (pre_doc +
                         _DATA_DOC_APPENDIX.format(replaced=_repl))
        if not python_has_wrapped:
            inner.__wrapped__ = func
        if new_sig is not None:
            inner.__signature__ = new_sig
        return inner
    return param

try:
    import matplotlib.preprocess_data as _preprocess_data
except ImportError:
    # Not available in this version of matplotlib. Fail
    # back to the vendored version copied from an earlier
    # matplotlib release.
    pass


@contextmanager
def _legacy_hold(ax, kwargs):
    """
    This only does anything if hold was specified
    and hold is even supported by matplotlib.
    """
    h = kwargs.pop('hold', None)
    if hasattr(ax, '_hold'):
        _tmp_hold = ax._hold
        if h is not None:
            ax._hold = h
            if not h:
                ax.cla()
    try:
        yield
    finally:
        if hasattr(ax, '_hold'):
            ax._hold = _tmp_hold


@_preprocess_data()
def contour(ax, *args, **kwargs):
    with _legacy_hold(ax, kwargs):
        kwargs['filled'] = False
        contours = LegacyContourSet(ax, *args, **kwargs)
        ax.autoscale_view()

    #if contours._A is not None: ax.figure.sci(contours)
    return contours
contour.__doc__ = LegacyContourSet.contour_doc

@_preprocess_data()
def contourf(ax, *args, **kwargs):
    with _legacy_hold(ax, kwargs):
        kwargs['filled'] = True
        contours = LegacyContourSet(ax, *args, **kwargs)
        ax.autoscale_view()

    #if contours._A is not None: ax.figure.sci(contours)
    return contours
contourf.__doc__ = LegacyContourSet.contour_doc


default_test_modules = [
    'legacycontour.tests',
]


def _init_tests():
    try:
        import faulthandler
    except ImportError:
        pass
    else:
        # CPython's faulthandler since v3.6 handles exceptions on Windows
        # https://bugs.python.org/issue23848 but until v3.6.4 it was
        # printing non-fatal exceptions https://bugs.python.org/issue30557
        import platform
        if not (sys.platform == 'win32' and
                (3, 6) < sys.version_info < (3, 6, 4) and
                platform.python_implementation() == 'CPython'):
            faulthandler.enable()

    # The version of FreeType to install locally for running the
    # tests.  This must match the value in `setupext.py`
    LOCAL_FREETYPE_VERSION = '2.6.1'

    from matplotlib import ft2font
    if (ft2font.__freetype_version__ != LOCAL_FREETYPE_VERSION or
        ft2font.__freetype_build_type__ != 'local'):
        warnings.warn(
            "Matplotlib is not built with the correct FreeType version to run "
            "tests.  Set local_freetype=True in setup.cfg and rebuild. "
            "Expect many image comparison failures below. "
            "Expected freetype version {0}. "
            "Found freetype version {1}. "
            "Freetype build type is {2}local".format(
                LOCAL_FREETYPE_VERSION,
                ft2font.__freetype_version__,
                "" if ft2font.__freetype_build_type__ == 'local' else "not "
            )
        )

    try:
        import pytest
        try:
            from unittest import mock
        except ImportError:
            import mock
    except ImportError:
        print("matplotlib.test requires pytest and mock to run.")
        raise


def test(verbosity=None, coverage=False, switch_backend_warn=True,
         recursionlimit=0, **kwargs):
    """run the legacycontour test suite"""
    _init_tests()
    if not os.path.isdir(os.path.join(os.path.dirname(__file__), 'tests')):
        raise ImportError("legacycontour test data is not installed")

    old_backend = mpl.get_backend()
    old_recursionlimit = sys.getrecursionlimit()
    try:
        mpl.use('agg')
        if recursionlimit:
            sys.setrecursionlimit(recursionlimit)
        import pytest

        args = kwargs.pop('argv', [])
        provide_default_modules = True
        use_pyargs = True
        for arg in args:
            if any(arg.startswith(module_path)
                   for module_path in default_test_modules):
                provide_default_modules = False
                break
            if os.path.exists(arg):
                provide_default_modules = False
                use_pyargs = False
                break
        if use_pyargs:
            args += ['--pyargs']
        if provide_default_modules:
            args += default_test_modules

        if coverage:
            args += ['--cov']

        if verbosity:
            args += ['-' + 'v' * verbosity]

        retcode = pytest.main(args, **kwargs)
    finally:
        if old_backend.lower() != 'agg':
            mpl.use(old_backend, warn=switch_backend_warn)
        if recursionlimit:
            sys.setrecursionlimit(old_recursionlimit)

    return retcode


test.__test__ = False  # pytest: this function is not a test

