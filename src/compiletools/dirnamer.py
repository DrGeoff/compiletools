""" Wrapper around appdirs that intercepts user_cache_dir 
    and uses the CTCACHE environment variable and other ct config files 
"""
import sys
import os
import shutil
import appdirs
import compiletools.configutils
import compiletools.apptools

user_data_dir = appdirs.user_data_dir
user_config_dir = appdirs.user_config_dir
site_config_dir = appdirs.site_config_dir


def add_arguments(cap):
    cap.add_argument(
        "--CTCACHE",
        default="None",
        help="Location to cache the magicflags and deps. None means no caching.",
    )
    cap.add_argument(
        "--CTCACHE_TYPE",
        default="None",
        choices=["None", "null", "memory", "disk", "sqlite", "redis", "mmap", "oracle"],
        help="Type of cache for FileAnalyzer results. None means no caching, oracle/mmap is recommended for best performance.",
    )


def _verbose_write(output, verbose=0, newline=False):
    if verbose > 2:
        sys.stdout.write(output)
        if newline:
            sys.stdout.write("\n")


def _verbose_write_found(cachedir, verbose=0):
    _verbose_write("Using CTCACHE=", verbose=verbose)
    _verbose_write(cachedir, verbose=verbose)
    if cachedir == "None":
        _verbose_write(". Disk caching is disabled.", verbose=verbose)
    if verbose > 0:
        sys.stdout.write("\n")


def user_cache_dir(
    appname="ct",
    appauthor=None,
    version=None,
    opinion=True,
    args=None,
    argv=None,
    exedir=None,
):
    if args is None:
        verbose = 0
    else:
        verbose = args.verbose
    # command line > environment variables > config file values > defaults

    cachedir = compiletools.configutils.extract_value_from_argv(key="CTCACHE", argv=argv)
    if cachedir:
        _verbose_write(
            "Highest priority CTCACHE is the command line.",
            verbose=verbose,
            newline=True,
        )
        _verbose_write_found(cachedir, verbose=verbose)
        return cachedir

    _verbose_write(
        "CTCACHE not on commandline. Falling back to environment variables.",
        verbose=verbose,
        newline=True,
    )
    try:
        cachedir = os.environ["CTCACHE"]
        _verbose_write_found(cachedir, verbose=verbose)
        return cachedir

    except KeyError:
        pass

    _verbose_write(
        "CTCACHE not in environment variables. Falling back to config files.",
        verbose=verbose,
        newline=True,
    )

    cachedir = compiletools.configutils.extract_item_from_ct_conf(
        "CTCACHE", exedir=exedir, verbose=verbose
    )
    if cachedir:
        _verbose_write_found(cachedir, verbose=verbose)
        return cachedir

    _verbose_write(
        "CTCACHE not in config files.  Falling back to python-appdirs (which on linux wraps XDG variables).",
        verbose=verbose,
        newline=True,
    )
    cachedir = appdirs.user_cache_dir(appname, appauthor, version, opinion)
    _verbose_write_found(cachedir, verbose=verbose)
    return cachedir


def get_cache_type(args=None, argv=None, exedir=None):
    """Get the cache type for FileAnalyzer following config priority.
    
    Priority: command line > environment variables > config file > default
    """
    if args is None:
        verbose = 0
    else:
        verbose = args.verbose if hasattr(args, 'verbose') else 0
    
    # Check command line first
    cache_type = compiletools.configutils.extract_value_from_argv(key="CTCACHE_TYPE", argv=argv)
    if cache_type:
        _verbose_write(
            "CTCACHE_TYPE from command line: " + cache_type,
            verbose=verbose,
            newline=True,
        )
        return None if cache_type == "None" else cache_type
    
    # Check environment variables
    _verbose_write(
        "CTCACHE_TYPE not on commandline. Checking environment variables.",
        verbose=verbose,
        newline=True,
    )
    try:
        cache_type = os.environ["CTCACHE_TYPE"]
        _verbose_write("CTCACHE_TYPE from environment: " + cache_type, verbose=verbose, newline=True)
        return None if cache_type == "None" else cache_type
    except KeyError:
        pass
    
    # Check config files
    _verbose_write(
        "CTCACHE_TYPE not in environment. Checking config files.",
        verbose=verbose,
        newline=True,
    )
    cache_type = compiletools.configutils.extract_item_from_ct_conf(
        "CTCACHE_TYPE", exedir=exedir, verbose=verbose
    )
    if cache_type:
        _verbose_write("CTCACHE_TYPE from config: " + cache_type, verbose=verbose, newline=True)
        return None if cache_type == "None" else cache_type
    
    # Default to None (no caching)
    _verbose_write("CTCACHE_TYPE not found, defaulting to None", verbose=verbose, newline=True)
    return None


def main(argv=None):
    cap = compiletools.apptools.create_parser("Cache directory and type management tool", argv=argv, include_config=False)
    add_arguments(cap)
    cap.add_argument(
        "--type",
        action="store_true",
        help="Report the CTCACHE_TYPE setting instead of cache directory"
    )
    cap.add_argument(
        "--clean",
        action="store_true",
        help="Remove the cache directory"
    )
    cap.add_argument(
        "--all",
        action="store_true",
        help="Report both cache directory and CTCACHE_TYPE setting"
    )
    args = cap.parse_args(args=argv)
    
    # If --clean was requested, remove the cache directory
    if args.clean:
        cachedir = compiletools.dirnamer.user_cache_dir(args=args)
        if args.verbose >= 1:
            print(" ".join(["Removing cache directory =", cachedir]))
        
        if os.path.isdir(cachedir):
            shutil.rmtree(cachedir)
    elif args.all:
        # If --all was requested, print both cache directory and type
        print(compiletools.dirnamer.user_cache_dir(args=args))
        cache_type = get_cache_type(args=args, argv=argv)
        cache_type_display = cache_type if cache_type is not None else "None"
        print(f"CTCACHE_TYPE={cache_type_display}")
    elif args.type:
        # If --type was requested, print the cache type
        cache_type = get_cache_type(args=args, argv=argv)
        cache_type_display = cache_type if cache_type is not None else "None"
        print(f"CTCACHE_TYPE={cache_type_display}")
    else:
        # Otherwise print the cache directory
        print(compiletools.dirnamer.user_cache_dir(args=args))


def main_clean(argv=None):
    """Entry point for ct-cache-clean command (synonym for ct-cache --clean)."""
    if argv is None:
        argv = sys.argv[1:]
    # Add --clean to the arguments if not already present
    if '--clean' not in argv:
        argv = ['--clean'] + argv
    return main(argv)
