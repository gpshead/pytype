"""Generic functions."""

import atexit
import collections
import contextlib
import errno
import itertools
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import threading
import types


def message(error):
  """A convenience function which extracts a message from an exception.

  Use this to replace exception.message, which is deprecated in python2 and
  removed in python3.

  Args:
    error: The exception.

  Returns:
    A message string.
  """
  return error.args[0] if error.args else ""


class UsageError(Exception):
  """Raise this for top-level usage errors."""
  pass


def format_version(python_version):
  """Format a version tuple into a dotted version string."""
  return ".".join([str(x) for x in python_version])


def parse_version(version_string):
  """Parse a version string like 2.7 into a tuple."""
  return tuple(map(int, version_string.split(".")))


def validate_version(python_version):
  """Raise an exception if the python version is unsupported."""
  if len(python_version) != 2:
    # This is typically validated in the option parser, but check here too in
    # case we get python_version via a different entry point.
    raise UsageError("python_version must be <major>.<minor>: %r" %
                     format_version(python_version))
  if (3, 0) <= python_version <= (3, 3):
    # These have odd __build_class__ parameters, store co_code.co_name fields
    # as unicode, and don't yet have the extra qualname parameter to
    # MAKE_FUNCTION. Jumping through these extra hoops is not worth it, given
    # that typing.py isn't introduced until 3.5, anyway.
    raise UsageError(
        "Python versions 3.0 - 3.3 are not supported. Use 3.4 and higher.")
  if python_version > (3, 6):
    # We have an explicit per-minor-version mapping in opcodes.py
    raise UsageError("Python versions > 3.6 are not yet supported.")


def is_python_2(python_version):
  return python_version[0] == 2


def is_python_3(python_version):
  return python_version[0] == 3


def get_versioned_path(subdir, python_version):
  if is_python_2(python_version):
    return os.path.join(subdir, "2")
  else:
    return os.path.join(subdir, "3")


def replace_extension(filename, new_extension):
  name, _ = os.path.splitext(filename)
  if new_extension.startswith("."):
    return name + new_extension
  else:
    return name + "." + new_extension


def strip_prefix(string, prefix):
  """Strip off prefix if it exists."""
  if string.startswith(prefix):
    return string[len(prefix):]
  return string


def get_absolute_name(prefix, relative_name):
  """Joins a dotted-name prefix and a relative name.

  Args:
    prefix: A dotted name, e.g. foo.bar.baz
    relative_name: A dotted name with possibly some leading dots, e.g. ..x.y

  Returns:
    The relative name appended to the prefix, after going up one level for each
      leading dot.
      e.g. foo.bar.baz + ..hello.world -> foo.bar.hello.world
    None if the relative name has too many leading dots.
  """
  path = prefix.split(".") if prefix else []
  name = relative_name.lstrip(".")
  ndots = len(relative_name) - len(name)
  if ndots > len(path):
    return None
  prefix = "".join([p + "." for p in path[:len(path) + 1 - ndots]])
  return prefix + name


def maybe_truncate(s, length=30):
  """Truncate long strings (and append '...'), but leave short strings alone."""
  s = str(s)
  if len(s) > length-3:
    return s[0:length-3] + "..."
  else:
    return s


def pretty_conjunction(conjunction):
  """Pretty-print a conjunction. Use parentheses as necessary.

  E.g. ["a", "b"] -> "(a & b)"

  Args:
    conjunction: List of strings.
  Returns:
    A pretty-printed string.
  """
  if not conjunction:
    return "true"
  elif len(conjunction) == 1:
    return conjunction[0]
  else:
    return "(" + " & ".join(conjunction) + ")"


def pretty_dnf(dnf):
  """Pretty-print a disjunctive normal form (disjunction of conjunctions).

  E.g. [["a", "b"], ["c"]] -> "(a & b) | c".

  Args:
    dnf: A list of list of strings. (Disjunction of conjunctions of strings)
  Returns:
    A pretty-printed string.
  """
  if not dnf:
    return "false"
  else:
    return " | ".join(pretty_conjunction(c) for c in dnf)


def numeric_sort_key(s):
  return tuple((int(e) if e.isdigit() else e) for e in re.split(r"(\d+)", s))


def concat_tuples(tuples):
  return tuple(itertools.chain.from_iterable(tuples))


def _makedirs(path):
  """Create a nested directory, but don't fail if any of it already exists."""
  try:
    os.makedirs(path)
  except OSError as e:
    if e.errno != errno.EEXIST:
      raise


class Tempdir(object):
  """Context handler for creating temporary directories."""

  def __enter__(self):
    self.path = tempfile.mkdtemp()
    return self

  def create_directory(self, filename):
    """Create a subdirectory in the temporary directory."""
    path = os.path.join(self.path, filename)
    _makedirs(path)
    return path

  def create_file(self, filename, indented_data=None):
    """Create a file in the temporary directory. Dedents the data if needed."""
    filedir, filename = os.path.split(filename)
    if filedir:
      self.create_directory(filedir)
    path = os.path.join(self.path, filedir, filename)
    if isinstance(indented_data, bytes) and not isinstance(indented_data, str):
      # This is binary data rather than text.
      # TODO(rechen): The second isinstance() check can be dropped once we no
      # longer support running under Python 2.
      mode = "wb"
      data = indented_data
    else:
      mode = "w"
      data = textwrap.dedent(indented_data) if indented_data else indented_data
    with open(path, mode) as fi:
      if data:
        fi.write(data)
    return path

  def delete_file(self, filename):
    os.unlink(os.path.join(self.path, filename))

  def __exit__(self, error_type, value, tb):
    shutil.rmtree(path=self.path)
    return False  # reraise any exceptions

  def __getitem__(self, filename):
    """Get the full path for an entry in this directory."""
    return os.path.join(self.path, filename)


@contextlib.contextmanager
def cd(path):
  """Context manager. Change the directory, and restore it afterwards.

  Example usage:
    with cd("/path"):
      ...

  Arguments:
    path: The directory to change to.
  Yields:
    Executes your code, in a changed directory.
  """
  curdir = os.getcwd()
  os.chdir(path)
  try:
    yield
  finally:
    os.chdir(curdir)


class NoSuchDirectory(Exception):
  pass


def load_pytype_file(filename):
  """Get the contents of a data file from the pytype installation.

  Arguments:
    filename: the path, relative to "pytype/"
  Returns:
    The contents of the file as a bytestring
  Raises:
    IOError: if file not found
  """
  path = os.path.join(os.path.dirname(__file__), filename)
  return load_data_file(path)


def load_data_file(path):
  """Get the contents of a data file."""
  with open(path, "rb") as fi:
    return fi.read()


def list_pytype_files(suffix):
  """Recursively get the contents of a directory in the pytype installation.

  This reports files in said directory as well as all subdirectories of it.

  Arguments:
    suffix: the path, relative to "pytype/"
  Yields:
    The filenames, relative to pytype/{suffix}
  Raises:
    NoSuchDirectory: if the directory doesn't exist.
  """
  basedir = os.path.join(os.path.dirname(__file__), suffix)
  assert not suffix.endswith("/")
  loader = globals().get("__loader__", None)
  try:
    # List directory using __loader__.
    # __loader__ exists only when this file is in a Python archive in Python 2
    # but is always present in Python 3, so we can't use the presence or
    # absence of the loader to determine whether calling get_zipfile is okay.
    filenames = loader.get_zipfile().namelist()  # pytype: disable=attribute-error
  except AttributeError:
    # List directory using the file system
    if not os.path.isdir(basedir):
      raise NoSuchDirectory(basedir)
    directories = [""]
    while directories:
      d = directories.pop()
      for basename in os.listdir(os.path.join(basedir, d)):
        filename = os.path.join(d, basename)
        if os.path.isdir(os.path.join(basedir, filename)):
          directories.append(filename)
        elif os.path.exists(os.path.join(basedir, filename)):
          yield filename
  else:
    for filename in filenames:
      directory = "pytype/" + suffix + "/"
      try:
        i = filename.rindex(directory)
      except ValueError:
        pass
      else:
        yield filename[i + len(directory):]


def path_to_module_name(filename):
  """Converts a filename into a dotted module name."""
  module_name = os.path.splitext(filename)[0].replace(os.path.sep, ".")
  # strip __init__ suffix
  m, _, _ = module_name.partition(".__init__")
  return m




def get_python_exe(python_version):
  """Automatically infer the --python_exe argument.

  Arguments:
    python_version: the version tuple (e.g. (2, 7))
  Returns:
    The inferred python_exe argument
  """
  python_exe = "python%d.%d" % python_version
  return python_exe


def is_valid_python_exe(python_exe):
  """Test that python_exe is a valid executable."""
  try:
    with open(os.devnull, "w") as null:
      subprocess.check_call(python_exe + " -V",
                            shell=True, stderr=null, stdout=null)
      return True
  except subprocess.CalledProcessError:
    return False


def list_startswith(l, prefix):
  """Like str.startswith, but for lists."""
  return l[:len(prefix)] == prefix


def list_strip_prefix(l, prefix):
  """Remove prefix, if it's there."""
  return l[len(prefix):] if list_startswith(l, prefix) else l


def _arg_names(f):
  """Return the argument names of a function."""
  return f.__code__.co_varnames[:f.__code__.co_argcount]


class memoize(object):  # pylint: disable=invalid-name
  """A memoizing decorator that supports expressions as keys.

  Use it like this:
    @memoize
    def f(x):
      ...
  or
    @memoize("(id(x), y)")
    def f(x, y, z):
      ...
  .
  Careful with methods. If you have code like
    @memoize("x")
    def f(self, x):
      ...
  then memoized values will be shared across instances.

  This decorator contains some speed optimizations that make it not thread-safe.
  """

  def __new__(cls, key_or_function):
    if isinstance(key_or_function, types.FunctionType):
      f = key_or_function
      key = "(" + ", ".join(_arg_names(f)) + ")"
      return memoize(key)(f)
    else:
      key = key_or_function
      return object.__new__(cls)

  def __init__(self, key):
    self.key = key

  def __call__(self, f):
    key_program = compile(self.key, filename=__name__, mode="eval")
    argnames = _arg_names(f)
    memoized = {}
    no_result = object()
    if f.__defaults__:
      defaults = dict(zip(argnames[-len(f.__defaults__):], f.__defaults__))
    else:
      defaults = {}
    pos_and_arg_tuples = list(zip(range(f.__code__.co_argcount), argnames))
    shared_dict = {}
    # TODO(kramm): Use functools.wraps or functools.update_wrapper to preserve
    # the metadata of the original function.
    def call(*posargs, **kwargs):
      """Call a memoized function."""
      if kwargs or defaults:
        # Slower version; for default arguments, we need two dictionaries.
        args = defaults.copy()
        args.update(dict(zip(argnames, posargs)))
        args.update(kwargs)
        key = eval(key_program, args)  # pylint: disable=eval-used
      else:
        # Faster version, if we have no default args.
        for pos, arg in pos_and_arg_tuples:
          # We know we write *all* the values, so we can re-use the dictionary.
          shared_dict[arg] = posargs[pos]
        key = eval(key_program, shared_dict)  # pylint: disable=eval-used
      result = memoized.get(key, no_result)
      if result is no_result:
        # Call the actual function.
        result = f(*posargs, **kwargs)
        memoized[key] = result
      return result
    return call


def invert_dict(d):
  """Invert a dictionary.

  Converts a dictionary (mapping strings to lists of strings) to a dictionary
  that maps into the other direction.

  Arguments:
    d: Dictionary to be inverted

  Returns:
    A dictionary n with the property that if "y in d[x]", then "x in n[y]".
  """

  inverted = collections.defaultdict(list)
  for key, value_list in d.items():
    for val in value_list:
      inverted[val].append(key)
  return inverted


def is_pyi_directory_init(filename):
  """Checks if a pyi file is path/to/dir/__init__.pyi."""
  if filename is None:
    return False
  return os.path.splitext(os.path.basename(filename))[0] == "__init__"


def get_pyi_package_name(module_name, is_package=False):
  """Figure out a package name for a module."""
  if module_name is None:
    return ""
  parts = module_name.split(".")
  if not is_package:
    parts = parts[:-1]
  return ".".join(parts)


class DynamicVar(object):
  """A dynamically scoped variable.

  This is a per-thread dynamic variable, with an initial value of None.
  The bind() call establishes a new value that will be in effect for the
  duration of the resulting context manager.  This is intended to be used
  in conjunction with a decorator.
  """

  def __init__(self):
    self._local = threading.local()

  def _values(self):
    values = getattr(self._local, "values", None)
    if values is None:
      values = [None]  # Stack of bindings, with an initial default of None.
      self._local.values = values
    return values

  @contextlib.contextmanager
  def bind(self, value):
    """Bind the dynamic variable to the supplied value."""
    values = self._values()
    try:
      values.append(value)  # Push the new binding.
      yield
    finally:
      values.pop()  # Pop the binding.

  def get(self):
    """Return the current value of the dynamic variable."""
    return self._values()[-1]


class AnnotatingDecorator(object):
  """A decorator for storing function attributes.

  Attributes:
    mapping: maps functions to their attributes.
  """

  def __init__(self):
    self.lookup = {}

  def __call__(self, value):
    def decorate(f):
      self.lookup[f.__name__] = value
      return f
    return decorate
