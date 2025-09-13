""" Wrap and memoize a variety of os calls """
import os
import shutil
import functools
import stringzilla as sz


def _to_python_str(path):
    """Convert StringZilla Str to Python string for OS calls, ensuring cache key consistency"""
    if isinstance(path, sz.Str):
        return path.decode('utf-8')
    return path


@functools.lru_cache(maxsize=None)
def _getmtime_impl(realpath_str):
    """ Internal cached implementation of os.path.getmtime """
    return os.path.getmtime(realpath_str)

def getmtime(realpath):
    """ Cached version of os.path.getmtime - accepts Python str or StringZilla Str """
    return _getmtime_impl(_to_python_str(realpath))


@functools.lru_cache(maxsize=None)
def _isfile_impl(trialpath_str):
    """ Internal cached implementation of os.path.isfile """
    return os.path.isfile(trialpath_str)

def isfile(trialpath):
    """ Cached version of os.path.isfile - accepts Python str or StringZilla Str """
    return _isfile_impl(_to_python_str(trialpath))


@functools.lru_cache(maxsize=None)
def _isdir_impl(trialpath_str):
    """ Internal cached implementation of os.path.isdir """
    return os.path.isdir(trialpath_str)

def isdir(trialpath):
    """ Cached version of os.path.isdir - accepts Python str or StringZilla Str """
    return _isdir_impl(_to_python_str(trialpath))


@functools.lru_cache(maxsize=None)
def _realpath_impl(trialpath_str):
    """ Internal cached implementation of os.path.realpath """
    # Note: We can't raise an exception on file non-existence
    # because this is sometimes called in order to create the file.
    return os.path.realpath(trialpath_str)

def realpath(trialpath):
    """ Cache os.path.realpath - accepts Python str or StringZilla Str """
    return _realpath_impl(_to_python_str(trialpath))


@functools.lru_cache(maxsize=None)
def _abspath_impl(trialpath_str):
    """ Internal cached implementation of os.path.abspath """
    return os.path.abspath(trialpath_str)

def abspath(trialpath):
    """ Cached version of os.path.abspath - accepts Python str or StringZilla Str """
    return _abspath_impl(_to_python_str(trialpath))


@functools.lru_cache(maxsize=None)
def _dirname_impl(trialpath_str):
    """ Internal cached implementation of os.path.dirname """
    return os.path.dirname(trialpath_str)

def dirname(trialpath):
    """ A cached version of os.path.dirname - accepts Python str or StringZilla Str """
    return _dirname_impl(_to_python_str(trialpath))


@functools.lru_cache(maxsize=None)
def _basename_impl(trialpath_str):
    """ Internal cached implementation of os.path.basename """
    return os.path.basename(trialpath_str)

def basename(trialpath):
    """ A cached version of os.path.basename - accepts Python str or StringZilla Str """
    return _basename_impl(_to_python_str(trialpath))


def isc(trialpath):
    """ Is the given file a C file ? """
    return os.path.splitext(trialpath)[1] == ".c"



def copy(src, dest):
    """ copy the src to the dest and print any errors """
    try:
        shutil.copy2(src, dest)
    except IOError as err:
        print("Unable to copy file {}".format(err))


def clear_cache():
    _getmtime_impl.cache_clear()
    _isfile_impl.cache_clear()
    _isdir_impl.cache_clear()
    _realpath_impl.cache_clear()
    _abspath_impl.cache_clear()
    _dirname_impl.cache_clear()
    _basename_impl.cache_clear()
