from __future__ import print_function, absolute_import

from importlib import import_module

from distutils import sysconfig
from distutils import version
from distutils.core import Extension
import distutils.command.build_ext
import glob
import multiprocessing
import os
import platform
import re
import subprocess
from subprocess import check_output
import sys
import warnings
from textwrap import fill
import shutil
import versioneer


PY3min = (sys.version_info[0] >= 3)


if sys.platform != 'win32':
    if not PY3min:
        from commands import getstatusoutput
    else:
        from subprocess import getstatusoutput


if PY3min:
    import configparser
else:
    import ConfigParser as configparser


# matplotlib build options, which can be altered using setup.cfg
options = {
    'display_status': True,
    'verbose': False,
    'backend': None,
    'basedirlist': None
    }


setup_cfg = os.environ.get('MPLSETUPCFG', 'setup.cfg')
if os.path.exists(setup_cfg):
    if PY3min:
        config = configparser.ConfigParser()
    else:
        config = configparser.SafeConfigParser()
    config.read(setup_cfg)

    if config.has_option('status', 'suppress'):
        options['display_status'] = not config.getboolean("status", "suppress")

    if config.has_option('rc_options', 'backend'):
        options['backend'] = config.get("rc_options", "backend")

    if config.has_option('directories', 'basedirlist'):
        options['basedirlist'] = [
            x.strip() for x in
            config.get("directories", "basedirlist").split(',')]

    if config.has_option('test', 'local_freetype'):
        options['local_freetype'] = config.getboolean("test", "local_freetype")
else:
    config = None

def get_win32_compiler():
    """
    Determine the compiler being used on win32.
    """
    # Used to determine mingw32 or msvc
    # This is pretty bad logic, someone know a better way?
    for v in sys.argv:
        if 'mingw32' in v:
            return 'mingw32'
    return 'msvc'
win32_compiler = get_win32_compiler()


def extract_versions():
    """
    Extracts version values from the main matplotlib __init__.py and
    returns them as a dictionary.
    """
    with open('lib/legacycontour/__init__.py') as fd:
        for line in fd.readlines():
            if (line.startswith('__version__numpy__')):
                exec(line.strip())
    return locals()


def has_include_file(include_dirs, filename):
    """
    Returns `True` if `filename` can be found in one of the
    directories in `include_dirs`.
    """
    if sys.platform == 'win32':
        include_dirs += os.environ.get('INCLUDE', '.').split(';')
    for dir in include_dirs:
        if os.path.exists(os.path.join(dir, filename)):
            return True
    return False


def check_include_file(include_dirs, filename, package):
    """
    Raises an exception if the given include file can not be found.
    """
    if not has_include_file(include_dirs, filename):
        raise CheckFailed(
            "The C/C++ header for %s (%s) could not be found.  You "
            "may need to install the development package." %
            (package, filename))


def get_base_dirs():
    """
    Returns a list of standard base directories on this platform.
    """
    if options['basedirlist']:
        return options['basedirlist']

    if os.environ.get('MPLBASEDIRLIST'):
        return os.environ.get('MPLBASEDIRLIST').split(os.pathsep)

    win_bases = ['win32_static', ]
    # on conda windows, we also add the <installdir>\Library of the local interpreter,
    # as conda installs libs/includes there
    if os.getenv('CONDA_DEFAULT_ENV'):
        win_bases.append(os.path.join(os.getenv('CONDA_DEFAULT_ENV'), "Library"))

    basedir_map = {
        'win32': win_bases,
        'darwin': ['/usr/local/', '/usr', '/usr/X11',
                   '/opt/X11', '/opt/local'],
        'sunos5': [os.getenv('MPLIB_BASE') or '/usr/local', ],
        'gnu0': ['/usr'],
        'aix5': ['/usr/local'],
        }
    return basedir_map.get(sys.platform, ['/usr/local', '/usr'])


def get_include_dirs():
    """
    Returns a list of standard include directories on this platform.
    """
    include_dirs = [os.path.join(d, 'include') for d in get_base_dirs()]
    if sys.platform != 'win32':
        # gcc includes this dir automatically, so also look for headers in
        # these dirs
        include_dirs.extend(
            os.environ.get('CPLUS_INCLUDE_PATH', '').split(os.pathsep))
    return include_dirs


def is_min_version(found, minversion):
    """
    Returns `True` if `found` is at least as high a version as
    `minversion`.
    """
    expected_version = version.LooseVersion(minversion)
    found_version = version.LooseVersion(found)
    return found_version >= expected_version


# Define the display functions only if display_status is True.
if options['display_status']:
    def print_line(char='='):
        print(char * 76)

    def print_status(package, status):
        initial_indent = "%22s: " % package
        indent = ' ' * 24
        print(fill(str(status), width=76,
                   initial_indent=initial_indent,
                   subsequent_indent=indent))

    def print_message(message):
        indent = ' ' * 24 + "* "
        print(fill(str(message), width=76,
                   initial_indent=indent,
                   subsequent_indent=indent))

    def print_raw(section):
        print(section)
else:
    def print_line(*args, **kwargs):
        pass
    print_status = print_message = print_raw = print_line


# Remove the -Wstrict-prototypes option, is it's not valid for C++
customize_compiler = distutils.command.build_ext.customize_compiler


def my_customize_compiler(compiler):
    retval = customize_compiler(compiler)
    try:
        compiler.compiler_so.remove('-Wstrict-prototypes')
    except (ValueError, AttributeError):
        pass
    return retval

distutils.command.build_ext.customize_compiler = my_customize_compiler


def make_extension(name, files, *args, **kwargs):
    """
    Make a new extension.  Automatically sets include_dirs and
    library_dirs to the base directories appropriate for this
    platform.

    `name` is the name of the extension.

    `files` is a list of source files.

    Any additional arguments are passed to the
    `distutils.core.Extension` constructor.
    """
    ext = DelayedExtension(name, files, *args, **kwargs)
    for dir in get_base_dirs():
        include_dir = os.path.join(dir, 'include')
        if os.path.exists(include_dir):
            ext.include_dirs.append(include_dir)
        for lib in ('lib', 'lib64'):
            lib_dir = os.path.join(dir, lib)
            if os.path.exists(lib_dir):
                ext.library_dirs.append(lib_dir)
    ext.include_dirs.append('.')

    return ext


def get_file_hash(filename):
    """
    Get the MD5 hash of a given filename.
    """
    import hashlib
    BLOCKSIZE = 1 << 16
    hasher = hashlib.md5()
    with open(filename, 'rb') as fd:
        buf = fd.read(BLOCKSIZE)
        while len(buf) > 0:
            hasher.update(buf)
            buf = fd.read(BLOCKSIZE)
    return hasher.hexdigest()


class CheckFailed(Exception):
    """
    Exception thrown when a `SetupPackage.check` method fails.
    """
    pass


class SetupPackage(object):
    optional = False
    pkg_names = {
        "apt-get": None,
        "yum": None,
        "dnf": None,
        "brew": None,
        "port": None,
        "windows_url": None
        }

    def check(self):
        """
        Checks whether the build dependencies are met.  Should raise a
        `CheckFailed` exception if the dependency could not be met, otherwise
        return a string indicating a version number or some other message
        indicating what was found.
        """
        pass

    def runtime_check(self):
        """
        True if the runtime dependencies of the backend are met.  Assumes that
        the build-time dependencies are met.
        """
        return True

    def get_packages(self):
        """
        Get a list of package names to add to the configuration.
        These are added to the `packages` list passed to
        `distutils.setup`.
        """
        return []

    def get_namespace_packages(self):
        """
        Get a list of namespace package names to add to the configuration.
        These are added to the `namespace_packages` list passed to
        `distutils.setup`.
        """
        return []

    def get_py_modules(self):
        """
        Get a list of top-level modules to add to the configuration.
        These are added to the `py_modules` list passed to
        `distutils.setup`.
        """
        return []

    def get_package_data(self):
        """
        Get a package data dictionary to add to the configuration.
        These are merged into to the `package_data` list passed to
        `distutils.setup`.
        """
        return {}

    def get_extension(self):
        """
        Get a list of C extensions (`distutils.core.Extension`
        objects) to add to the configuration.  These are added to the
        `extensions` list passed to `distutils.setup`.
        """
        return None

    def get_install_requires(self):
        """
        Get a list of Python packages that we require.
        pip/easy_install will attempt to download and install this
        package if it is not installed.
        """
        return []

    def get_setup_requires(self):
        """
        Get a list of Python packages that we require at build time.
        pip/easy_install will attempt to download and install this
        package if it is not installed.
        """
        return []

    def _check_for_pkg_config(self, package, include_file, min_version=None,
                              version=None):
        """
        A convenience function for writing checks for a
        pkg_config-defined dependency.

        `package` is the pkg_config package name.

        `include_file` is a top-level include file we expect to find.

        `min_version` is the minimum version required.

        `version` will override the found version if this package
        requires an alternate method for that. Set version='unknown'
        if the version is not known but you still want to disabled
        pkg_config version check.
        """
        if version is None:
            version = pkg_config.get_version(package)

            if version is None:
                raise CheckFailed(
                    "pkg-config information for '%s' could not be found." %
                    package)

        if min_version == 'PATCH':
            raise CheckFailed(
                "Requires patches that have not been merged upstream.")

        if min_version and version != 'unknown':
            if (not is_min_version(version, min_version)):
                raise CheckFailed(
                    "Requires %s %s or later.  Found %s." %
                    (package, min_version, version))

        ext = self.get_extension()
        if ext is None:
            ext = make_extension('test', [])
            pkg_config.setup_extension(ext, package)

        check_include_file(
            ext.include_dirs + get_include_dirs(), include_file, package)

        return 'version %s' % version

    def do_custom_build(self):
        """
        If a package needs to do extra custom things, such as building a
        third-party library, before building an extension, it should
        override this method.
        """
        pass

    def install_help_msg(self):
        """
        Do not override this method !

        Generate the help message to show if the package is not installed.
        To use this in subclasses, simply add the dictionary `pkg_names` as
        a class variable:

        pkg_names = {
            "apt-get": <Name of the apt-get package>,
            "yum": <Name of the yum package>,
            "dnf": <Name of the dnf package>,
            "brew": <Name of the brew package>,
            "port": <Name of the port package>,
            "windows_url": <The url which has installation instructions>
            }

        All the dictionary keys are optional. If a key is not present or has
        the value `None` no message is provided for that platform.
        """
        def _try_managers(*managers):
            for manager in managers:
                pkg_name = self.pkg_names.get(manager, None)
                if pkg_name:
                    try:
                        # `shutil.which()` can be used when Python 2.7 support
                        # is dropped. It is available in Python 3.3+
                        _ = check_output(["which", manager],
                                         stderr=subprocess.STDOUT)
                        return ('Try installing {0} with `{1} install {2}`'
                                .format(self.name, manager, pkg_name))
                    except subprocess.CalledProcessError:
                        pass

        message = None
        if sys.platform == "win32":
            url = self.pkg_names.get("windows_url", None)
            if url:
                message = ('Please check {0} for instructions to install {1}'
                           .format(url, self.name))
        elif sys.platform == "darwin":
            message = _try_managers("brew", "port")
        elif sys.platform.startswith("linux"):
            release = platform.linux_distribution()[0].lower()
            if release in ('debian', 'ubuntu'):
                message = _try_managers('apt-get')
            elif release in ('centos', 'redhat', 'fedora'):
                message = _try_managers('dnf', 'yum')
        return message


class OptionalPackage(SetupPackage):
    optional = True
    force = False
    config_category = "packages"
    default_config = "auto"

    @classmethod
    def get_config(cls):
        """
        Look at `setup.cfg` and return one of ["auto", True, False] indicating
        if the package is at default state ("auto"), forced by the user (case
        insensitively defined as 1, true, yes, on for True) or opted-out (case
        insensitively defined as 0, false, no, off for False).
        """
        conf = cls.default_config
        if config is not None and config.has_option(cls.config_category, cls.name):
            try:
                conf = config.getboolean(cls.config_category, cls.name)
            except ValueError:
                conf = config.get(cls.config_category, cls.name)
        return conf

    def check(self):
        """
        Do not override this method!

        For custom dependency checks override self.check_requirements().
        Two things are checked: Configuration file and requirements.
        """
        # Check configuration file
        conf = self.get_config()
        # Default "auto" state or install forced by user
        if conf in [True, 'auto']:
            message = "installing"
            # Set non-optional if user sets `True` in config
            if conf is True:
                self.optional = False
        # Configuration opt-out by user
        else:
            # Some backend extensions (e.g. Agg) need to be built for certain
            # other GUI backends (e.g. TkAgg) even when manually disabled
            if self.force is True:
                message = "installing forced (config override)"
            else:
                raise CheckFailed("skipping due to configuration")

        # Check requirements and add extra information (if any) to message.
        # If requirements are not met a CheckFailed should be raised in there.
        additional_info = self.check_requirements()
        if additional_info:
            message += ", " + additional_info

        # No CheckFailed raised until now, return install message.
        return message

    def check_requirements(self):
        """
        Override this method to do custom dependency checks.

         - Raise CheckFailed() if requirements are not met.
         - Return message with additional information, or an empty string
           (or None) for no additional information.
        """
        return ""


class Platform(SetupPackage):
    name = "platform"

    def check(self):
        return sys.platform


class Python(SetupPackage):
    name = "python"

    def check(self):
        major, minor1, minor2, s, tmp = sys.version_info

        if major < 2:
            raise CheckFailed(
                "Requires Python 2.7 or later")
        elif major == 2 and minor1 < 7:
            raise CheckFailed(
                "Requires Python 2.7 or later (in the 2.x series)")
        elif major == 3 and minor1 < 4:
            raise CheckFailed(
                "Requires Python 3.4 or later (in the 3.x series)")

        return sys.version


class Matplotlib(SetupPackage):
    name = "matplotlib"

    def check(self):
        return 


class Tests(OptionalPackage):
    name = "tests"
    pytest_min_version = '3.1'
    default_config = False

    def check(self):
        super(Tests, self).check()

        msgs = []
        msg_template = ('{package} is required to run the Matplotlib test '
                        'suite. Please install it with pip or your preferred '
                        'tool to run the test suite')

        bad_pytest = msg_template.format(
            package='pytest %s or later' % self.pytest_min_version
        )
        try:
            import pytest
            if is_min_version(pytest.__version__, self.pytest_min_version):
                msgs += ['using pytest version %s' % pytest.__version__]
            else:
                msgs += [bad_pytest]
        except ImportError:
            msgs += [bad_pytest]

        if PY3min:
            msgs += ['using unittest.mock']
        else:
            try:
                import mock
                msgs += ['using mock %s' % mock.__version__]
            except ImportError:
                msgs += [msg_template.format(package='mock')]

        return ' / '.join(msgs)

    def get_packages(self):
        return [
            'legacycontour.tests',
            ]

    def get_package_data(self):
        baseline_images = [
            'tests/baseline_images/%s/*' % x
            for x in os.listdir('lib/legacycontour/tests/baseline_images')]

        return {
            'legacycontour':
            baseline_images +
            [
                'tests/cmr10.pfb',
                'tests/mpltest.ttf',
                'tests/test_rcparams.rc',
                'tests/test_utf32_be_rcparams.rc',
            ]}



class DelayedExtension(Extension, object):
    """
    A distutils Extension subclass where some of its members
    may have delayed computation until reaching the build phase.

    This is so we can, for example, get the Numpy include dirs
    after pip has installed Numpy for us if it wasn't already
    on the system.
    """
    def __init__(self, *args, **kwargs):
        super(DelayedExtension, self).__init__(*args, **kwargs)
        self._finalized = False
        self._hooks = {}

    def add_hook(self, member, func):
        """
        Add a hook to dynamically compute a member.

        Parameters
        ----------
        member : string
            The name of the member

        func : callable
            The function to call to get dynamically-computed values
            for the member.
        """
        self._hooks[member] = func

    def finalize(self):
        self._finalized = True

    class DelayedMember(property):
        def __init__(self, name):
            self._name = name

        def __get__(self, obj, objtype=None):
            result = getattr(obj, '_' + self._name, [])

            if obj._finalized:
                if self._name in obj._hooks:
                    result = obj._hooks[self._name]() + result

            return result

        def __set__(self, obj, value):
            setattr(obj, '_' + self._name, value)

    include_dirs = DelayedMember('include_dirs')


class Numpy(SetupPackage):
    name = "numpy"

    @staticmethod
    def include_dirs_hook():
        if PY3min:
            import builtins
            if hasattr(builtins, '__NUMPY_SETUP__'):
                del builtins.__NUMPY_SETUP__
            import imp
            import numpy
            imp.reload(numpy)
        else:
            import __builtin__
            if hasattr(__builtin__, '__NUMPY_SETUP__'):
                del __builtin__.__NUMPY_SETUP__
            import numpy
            reload(numpy)

        ext = Extension('test', [])
        ext.include_dirs.append(numpy.get_include())
        if not has_include_file(
                ext.include_dirs, os.path.join("numpy", "arrayobject.h")):
            warnings.warn(
                "The C headers for numpy could not be found. "
                "You may need to install the development package")

        return [numpy.get_include()]

    def check(self):
        min_version = extract_versions()['__version__numpy__']
        try:
            import numpy
        except ImportError:
            return 'not found. pip may install it below.'

        if not is_min_version(numpy.__version__, min_version):
            raise SystemExit(
                "Requires numpy %s or later to build.  (Found %s)" %
                (min_version, numpy.__version__))

        return 'version %s' % numpy.__version__

    def add_flags(self, ext):
        # Ensure that PY_ARRAY_UNIQUE_SYMBOL is uniquely defined for
        # each extension
        array_api_name = 'MPL_' + ext.name.replace('.', '_') + '_ARRAY_API'

        ext.define_macros.append(('PY_ARRAY_UNIQUE_SYMBOL', array_api_name))
        ext.add_hook('include_dirs', self.include_dirs_hook)

        ext.define_macros.append(('NPY_NO_DEPRECATED_API',
                                  'NPY_1_7_API_VERSION'))

        # Allow NumPy's printf format specifiers in C++.
        ext.define_macros.append(('__STDC_FORMAT_MACROS', 1))

    def get_setup_requires(self):
        return ['numpy>=1.7.1']

    def get_install_requires(self):
        return ['numpy>=1.7.1']


class ContourLegacy(SetupPackage):
    name = "contour_legacy"

    def get_packages(self):
        return [
            'legacycontour',
            'legacycontour.tests',
            ]
 
    def get_extension(self):
        sources = [
            "src/cntr.c"
            ]
        ext = make_extension('legacycontour._cntr', sources)
        Numpy().add_flags(ext)
        return ext


class Matplotlib(SetupPackage):
    name = "matplotlib"

    def check(self):
        try:
            import matplotlib
        except ImportError:
            return (
                "matplotlib was not found. "
                "pip/easy_install may attempt to install it "
                "after legacycontour.")
        return "using matplotlib version %s" % matplotlib.__version__

    def get_install_requires(self):
        return ['matplotlib>=1.5']


class OptionalPackageData(OptionalPackage):
    config_category = "package_data"


class Dlls(OptionalPackageData):
    """
    On Windows, this packages any DLL files that can be found in the
    lib/legacycontour/* directories.
    """
    name = "dlls"

    def check_requirements(self):
        if sys.platform != 'win32':
            raise CheckFailed("Microsoft Windows only")

    def get_package_data(self):
        return {'': ['*.dll']}

    @classmethod
    def get_config(cls):
        """
        Look at `setup.cfg` and return one of ["auto", True, False] indicating
        if the package is at default state ("auto"), forced by the user (True)
        or opted-out (False).
        """
        try:
            return config.getboolean(cls.config_category, cls.name)
        except:
            return False  # <-- default
