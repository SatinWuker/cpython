import faulthandler
import functools
import gc
import importlib
import io
import os
import sys
import time
import traceback
import unittest

from test import support
from test.support import os_helper
from test.support import threading_helper
from test.libregrtest.cmdline import Namespace
from test.libregrtest.save_env import saved_test_environment
from test.libregrtest.utils import clear_caches, format_duration, print_warning


class TestResult:
    def __init__(
        self,
        name: str,
        duration_sec: float = 0.0,
        xml_data: list[str] | None = None,
    ) -> None:
        self.name = name
        self.duration_sec = duration_sec
        self.xml_data = xml_data

    def __str__(self) -> str:
        return f"{self.name} finished"


class Passed(TestResult):
    def __str__(self) -> str:
        return f"{self.name} passed"


class Failed(TestResult):
    def __init__(
        self,
        name: str,
        duration_sec: float = 0.0,
        xml_data: list[str] | None = None,
        errors: list[tuple[str, str]] | None = None,
        failures: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(name, duration_sec=duration_sec, xml_data=xml_data)
        self.errors = errors
        self.failures = failures

    def __str__(self) -> str:
        if self.errors and self.failures:
            le = len(self.errors)
            lf = len(self.failures)
            error_s = "error" + ("s" if le > 1 else "")
            failure_s = "failure" + ("s" if lf > 1 else "")
            return f"{self.name} failed ({le} {error_s}, {lf} {failure_s})"

        if self.errors:
            le = len(self.errors)
            error_s = "error" + ("s" if le > 1 else "")
            return f"{self.name} failed ({le} {error_s})"

        if self.failures:
            lf = len(self.failures)
            failure_s = "failure" + ("s" if lf > 1 else "")
            return f"{self.name} failed ({lf} {failure_s})"

        return f"{self.name} failed"


class UncaughtException(Failed):
    def __str__(self) -> str:
        return f"{self.name} failed (uncaught exception)"


class EnvChanged(Failed):
    def __str__(self) -> str:
        return f"{self.name} failed (env changed)"

    # Convert Passed to EnvChanged
    @staticmethod
    def from_passed(other):
        return EnvChanged(other.name, other.duration_sec, other.xml_data)


class RefLeak(Failed):
    def __str__(self) -> str:
        return f"{self.name} failed (reference leak)"


class Skipped(TestResult):
    def __str__(self) -> str:
        return f"{self.name} skipped"


class ResourceDenied(Skipped):
    def __str__(self) -> str:
        return f"{self.name} skipped (resource denied)"


class Interrupted(TestResult):
    def __str__(self) -> str:
        return f"{self.name} interrupted"


class ChildError(Failed):
    def __str__(self) -> str:
        return f"{self.name} crashed"


class DidNotRun(TestResult):
    def __str__(self) -> str:
        return f"{self.name} ran no tests"


class Timeout(Failed):
    def __str__(self) -> str:
        return f"{self.name} timed out ({format_duration(self.duration_sec)})"


# Minimum duration of a test to display its duration or to mention that
# the test is running in background
PROGRESS_MIN_TIME = 30.0   # seconds

#If these test directories are encountered recurse into them and treat each
# test_ .py or dir as a separate test module. This can increase parallelism.
# Beware this can't generally be done for any directory with sub-tests as the
# __init__.py may do things which alter what tests are to be run.

SPLITTESTDIRS = {
    "test_asyncio",
    "test_concurrent_futures",
    "test_multiprocessing_fork",
    "test_multiprocessing_forkserver",
    "test_multiprocessing_spawn",
}

# Storage of uncollectable objects
FOUND_GARBAGE = []


def is_failed(result: TestResult, ns: Namespace) -> bool:
    if isinstance(result, EnvChanged):
        return ns.fail_env_changed
    return isinstance(result, Failed)


def findtestdir(path=None):
    return path or os.path.dirname(os.path.dirname(__file__)) or os.curdir


def findtests(*, testdir=None, exclude=(),
              split_test_dirs=SPLITTESTDIRS, base_mod=""):
    """Return a list of all applicable test modules."""
    testdir = findtestdir(testdir)
    tests = []
    for name in os.listdir(testdir):
        mod, ext = os.path.splitext(name)
        if (not mod.startswith("test_")) or (mod in exclude):
            continue
        if mod in split_test_dirs:
            subdir = os.path.join(testdir, mod)
            mod = f"{base_mod or 'test'}.{mod}"
            tests.extend(findtests(testdir=subdir, exclude=exclude,
                                   split_test_dirs=split_test_dirs, base_mod=mod))
        elif ext in (".py", ""):
            tests.append(f"{base_mod}.{mod}" if base_mod else mod)
    return sorted(tests)


def split_test_packages(tests, *, testdir=None, exclude=(),
                        split_test_dirs=SPLITTESTDIRS):
    testdir = findtestdir(testdir)
    splitted = []
    for name in tests:
        if name in split_test_dirs:
            subdir = os.path.join(testdir, name)
            splitted.extend(findtests(testdir=subdir, exclude=exclude,
                                      split_test_dirs=split_test_dirs,
                                      base_mod=name))
        else:
            splitted.append(name)
    return splitted


def get_abs_module(ns: Namespace, test_name: str) -> str:
    if test_name.startswith('test.') or ns.testdir:
        return test_name
    else:
        # Import it from the test package
        return 'test.' + test_name


def _runtest(ns: Namespace, test_name: str) -> TestResult:
    # Handle faulthandler timeout, capture stdout+stderr, XML serialization
    # and measure time.

    output_on_failure = ns.verbose3

    use_timeout = (
        ns.timeout is not None and threading_helper.can_start_thread
    )
    if use_timeout:
        faulthandler.dump_traceback_later(ns.timeout, exit=True)

    start_time = time.perf_counter()
    try:
        support.set_match_tests(ns.match_tests, ns.ignore_tests)
        support.junit_xml_list = xml_list = [] if ns.xmlpath else None
        if ns.failfast:
            support.failfast = True

        if output_on_failure:
            support.verbose = True

            stream = io.StringIO()
            orig_stdout = sys.stdout
            orig_stderr = sys.stderr
            print_warning = support.print_warning
            orig_print_warnings_stderr = print_warning.orig_stderr

            output = None
            try:
                sys.stdout = stream
                sys.stderr = stream
                # print_warning() writes into the temporary stream to preserve
                # messages order. If support.environment_altered becomes true,
                # warnings will be written to sys.stderr below.
                print_warning.orig_stderr = stream

                result = _runtest_inner(ns, test_name,
                                        display_failure=False)
                if not isinstance(result, Passed):
                    output = stream.getvalue()
            finally:
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr
                print_warning.orig_stderr = orig_print_warnings_stderr

            if output is not None:
                sys.stderr.write(output)
                sys.stderr.flush()
        else:
            # Tell tests to be moderately quiet
            support.verbose = ns.verbose

            result = _runtest_inner(ns, test_name,
                                    display_failure=not ns.verbose)

        if xml_list:
            import xml.etree.ElementTree as ET
            result.xml_data = [
                ET.tostring(x).decode('us-ascii')
                for x in xml_list
            ]

        result.duration_sec = time.perf_counter() - start_time
        return result
    finally:
        if use_timeout:
            faulthandler.cancel_dump_traceback_later()
        support.junit_xml_list = None


def runtest(ns: Namespace, test_name: str) -> TestResult:
    """Run a single test.

    ns -- regrtest namespace of options
    test_name -- the name of the test

    Returns a TestResult sub-class depending on the kind of result received.

    If ns.xmlpath is not None, xml_data is a list containing each
    generated testsuite element.
    """
    try:
        return _runtest(ns, test_name)
    except:
        if not ns.pgo:
            msg = traceback.format_exc()
            print(f"test {test_name} crashed -- {msg}",
                  file=sys.stderr, flush=True)
        return Failed(test_name)


def _test_module(the_module):
    loader = unittest.TestLoader()
    tests = loader.loadTestsFromModule(the_module)
    for error in loader.errors:
        print(error, file=sys.stderr)
    if loader.errors:
        raise Exception("errors while loading tests")
    support.run_unittest(tests)


def save_env(ns: Namespace, test_name: str):
    return saved_test_environment(test_name, ns.verbose, ns.quiet, pgo=ns.pgo)


def _runtest_inner2(ns: Namespace, test_name: str) -> bool:
    # Load the test function, run the test function, handle huntrleaks
    # to detect leaks.

    abstest = get_abs_module(ns, test_name)

    # remove the module from sys.module to reload it if it was already imported
    try:
        del sys.modules[abstest]
    except KeyError:
        pass

    the_module = importlib.import_module(abstest)

    if ns.huntrleaks:
        from test.libregrtest.refleak import dash_R

    # If the test has a test_main, that will run the appropriate
    # tests.  If not, use normal unittest test loading.
    test_runner = getattr(the_module, "test_main", None)
    if test_runner is None:
        test_runner = functools.partial(_test_module, the_module)

    try:
        with save_env(ns, test_name):
            if ns.huntrleaks:
                # Return True if the test leaked references
                refleak = dash_R(ns, test_name, test_runner)
            else:
                test_runner()
                refleak = False
    finally:
        # First kill any dangling references to open files etc.
        # This can also issue some ResourceWarnings which would otherwise get
        # triggered during the following test run, and possibly produce
        # failures.
        support.gc_collect()

        cleanup_test_droppings(test_name, ns.verbose)

    if gc.garbage:
        support.environment_altered = True
        print_warning(f"{test_name} created {len(gc.garbage)} "
                      f"uncollectable object(s).")

        # move the uncollectable objects somewhere,
        # so we don't see them again
        FOUND_GARBAGE.extend(gc.garbage)
        gc.garbage.clear()

    support.reap_children()

    return refleak


def _runtest_inner(
    ns: Namespace, test_name: str, display_failure: bool = True
) -> TestResult:
    # Detect environment changes, handle exceptions.

    # Reset the environment_altered flag to detect if a test altered
    # the environment
    support.environment_altered = False

    if ns.pgo:
        display_failure = False

    try:
        clear_caches()
        support.gc_collect()

        with save_env(ns, test_name):
            refleak = _runtest_inner2(ns, test_name)
    except support.ResourceDenied as msg:
        if not ns.quiet and not ns.pgo:
            print(f"{test_name} skipped -- {msg}", flush=True)
        return ResourceDenied(test_name)
    except unittest.SkipTest as msg:
        if not ns.quiet and not ns.pgo:
            print(f"{test_name} skipped -- {msg}", flush=True)
        return Skipped(test_name)
    except support.TestFailedWithDetails as exc:
        msg = f"test {test_name} failed"
        if display_failure:
            msg = f"{msg} -- {exc}"
        print(msg, file=sys.stderr, flush=True)
        return Failed(test_name, errors=exc.errors, failures=exc.failures)
    except support.TestFailed as exc:
        msg = f"test {test_name} failed"
        if display_failure:
            msg = f"{msg} -- {exc}"
        print(msg, file=sys.stderr, flush=True)
        return Failed(test_name)
    except support.TestDidNotRun:
        return DidNotRun(test_name)
    except KeyboardInterrupt:
        print()
        return Interrupted(test_name)
    except:
        if not ns.pgo:
            msg = traceback.format_exc()
            print(f"test {test_name} crashed -- {msg}",
                  file=sys.stderr, flush=True)
        return UncaughtException(test_name)

    if refleak:
        return RefLeak(test_name)
    if support.environment_altered:
        return EnvChanged(test_name)
    return Passed(test_name)


def cleanup_test_droppings(test_name: str, verbose: int) -> None:
    # Try to clean up junk commonly left behind.  While tests shouldn't leave
    # any files or directories behind, when a test fails that can be tedious
    # for it to arrange.  The consequences can be especially nasty on Windows,
    # since if a test leaves a file open, it cannot be deleted by name (while
    # there's nothing we can do about that here either, we can display the
    # name of the offending test, which is a real help).
    for name in (os_helper.TESTFN,):
        if not os.path.exists(name):
            continue

        if os.path.isdir(name):
            import shutil
            kind, nuker = "directory", shutil.rmtree
        elif os.path.isfile(name):
            kind, nuker = "file", os.unlink
        else:
            raise RuntimeError(f"os.path says {name!r} exists but is neither "
                               f"directory nor file")

        if verbose:
            print_warning(f"{test_name} left behind {kind} {name!r}")
            support.environment_altered = True

        try:
            import stat
            # fix possible permissions problems that might prevent cleanup
            os.chmod(name, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            nuker(name)
        except Exception as exc:
            print_warning(f"{test_name} left behind {kind} {name!r} "
                          f"and it couldn't be removed: {exc}")
