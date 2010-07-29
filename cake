#!/usr/bin/python -u

import cPickle
import md5
import sys
import commands
import os
from sets import Set

class UserException (Exception):
    def __init__(self, text):
        Exception.__init__(self, text)



def environ(variable, default):
    if default is None:
        if not variable in os.environ:
            raise UserException("Couldn't find required environment variable " + variable)
        return os.environ[variable]
    else:
        if not variable in os.environ:
            return default
        else:
            return os.environ[variable]

def parse_etc():
    """parses /etc/cake as if it was part of the environment.
    os.environ has higher precedence
    """
    if os.path.exists("/etc/cake"):
        f = open("/etc/cake")
        lines = f.readlines()
        f.close()
        
        for l in lines:
            if l.startswith("#"):
                continue
            l = l.strip()            
            
            if len(l) == 0:
                continue            
            key = l[0:l.index("=")].strip()
            value = l[l.index("=") + 1:].strip()
            
            for k in os.environ:
                value = value.replace("$" + k, os.environ[k])
                value = value.replace("${" + k + "}", os.environ[k])            
            
            if not key in os.environ:
                os.environ[key] = str(value)


usage_text = """

Usage: cake [compilation args] filename.cpp [app args]

cake generates and runs C++ executables with almost no configuration.

Options:

    --generate             Only runs the makefile generation step, does not build or run.
    --build                Only runs the makefile generation and build steps, does not run.
    --run (default)        Builds and runs the executable.
    --output=<filename>    Overrides the output filename.
    --variant=<vvv>        Reads the CAKE_<vvv>_CC, CAKE_<vvv>_CXXFLAGS and CAKE_<vvv>_LINKFLAGS
                           environment variables to determine the build flags.
                          
    --CC=<compiler>        Sets the compiler command.
    --CXXFLAGS=<flags>     Sets the compilation flags for all cpp files in the build.
    --LINKFLAGS=<flags>    Sets the flags used while linking.


Source annotations (embed in your hpp and cpp files as magic comments):

     //#CXXFLAGS=<flags>   Appends the given options to the compile step.
     //#LINKFLAGS=<flags>  Appends the given options to the link step

             
Environment Variables:

    CAKE_CCFLAGS           Sets the compiler command.
    CAKE_CXXFLAGS          Sets the compilation flags for all cpp files in the build.
    CAKE_LINKFLAGS         Sets the flags used while linking.

Environment variables can also be set in /etc/cake, which has the lowest priority when finding
compilation settings.

"""


def usage(msg = ""):
    if len(msg) > 0:
        print >> sys.stderr, msg
        print >> sys.stderr, ""
        
    print >> sys.stderr, usage_text.strip() + "\n"
    
    sys.exit(1)


def extractOption(text, option):
    """Extracts the given option from the text, returning the value
    on success and the trimmed text as a tuple, or (None, originaltext)
    on no match.
    """
    
    try:
        length = len(option)
        start = text.index(option)
        end = text.index("\n", start + length)
        
        result = text[start + length:end]
        trimmed = text[:start] + text[end+1:]
        return result, trimmed
        
    except ValueError:
        return None, text


def munge(to_munge):
    if isinstance(to_munge, dict):
        if len(to_munge) == 1:
            return "bin/" + "@@".join([x for x in to_munge]).replace("/", "@")
        else:
            return "bin/" + md5.md5(str([x for x in to_munge])).hexdigest()
    else:    
        return "bin/" + to_munge.replace("/", "@")


def force_get_dependencies_for(deps_file, source_file):
    """Recalculates the dependencies and caches them for a given source file"""
    
    cmd = CC + " -MM -MF " + deps_file + ".tmp " + source_file
    status, output = commands.getstatusoutput(cmd)
    if status != 0:
        raise UserException(output)

    f = open(deps_file + ".tmp")
    text = f.read()
    f.close()
    os.unlink(deps_file + ".tmp")

    files = text.split(":")[1]
    files = files.replace("\\", " ").replace("\t"," ").replace("\n", " ")
    files = [x for x in files.split(" ") if len(x) > 0]
    files = list(Set([os.path.normpath(x) for x in files]))
    files.sort()
    
    headers = [os.path.normpath(h) for h in files if h.endswith(".hpp")]
    sources = [os.path.normpath(h) for h in files if h.endswith(".cpp")]
    
    # determine ccflags and linkflags
    ccflags = {}
    linkflags = {}
    for h in headers + [source_file]:
        f = open(h)
        text = f.read(1024)        
                
        while True:
            result, text = extractOption(text, "//#CXXFLAGS=")
            if result is None:
                break
            else:
                ccflags[result] = True
        while True:
            result, text = extractOption(text, "//#LINKFLAGS=")
            if result is None:
                break
            else:
                linkflags[result] = True
                
            
        f.close()
        pass

    # cache
    f = open(deps_file, "w")
    cPickle.dump((headers, sources, ccflags, linkflags), f)
    f.close()
    
    return headers, sources, ccflags, linkflags

dependency_cache = {}

def get_dependencies_for(source_file):
    """Converts a gcc make command into a set of headers and source dependencies"""    
    
    global dependency_cache
    
    if source_file in dependency_cache:
        return dependency_cache[source_file]

    deps_file = munge(source_file) + ".deps"
    
    # try and reuse the existing if possible    
    if os.path.exists(deps_file):
        deps_mtime = os.stat(deps_file).st_mtime
        all_good = True
        
        try:
            f = open(deps_file)            
            headers, sources, ccflags, linkflags  = cPickle.load(f)
            f.close()
        except:
            all_good = False
    
        if all_good:
            for s in headers + [source_file]:
                try:
                    if os.stat(s).st_mtime > deps_mtime:
                        all_good = False
                        break
                except: # missing file counts as a miss
                    all_good = False
                    break
        if all_good:
            result = headers, sources, ccflags, linkflags
            dependency_cache[source_file] = result
            return result
        
    # failed, regenerate dependencies
    result = force_get_dependencies_for(deps_file, source_file)
    dependency_cache[source_file] = result
    return result


def insert_dependencies(sources, ignored, new_file, linkflags, cause):
    """Given a set of sources already being compiled, inserts the new file."""
    
    if new_file in sources:
        return
        
    if new_file in ignored:
        return
        
    if not os.path.exists(new_file):
        ignored.append(new_file)
        return

    # recursive step
    new_headers, new_sources, newccflags, newlinkflags = get_dependencies_for(new_file)
    
    sources[os.path.normpath(new_file)] = (newccflags, cause, new_headers)
    
    # merge in link options
    for l in newlinkflags:
        linkflags[l] = True
    
    copy = cause[:]
    copy.append(new_file)
    
    for h in new_headers:
        insert_dependencies(sources, ignored, os.path.splitext(h)[0] + ".cpp", linkflags, copy)
    
    for s in new_sources:
        insert_dependencies(sources, ignored, s, linkflags, copy)


def try_set_variant(variant):
    global CC, CXXFLAGS, LINKFLAGS
    CC = environ("CAKE_" + variant.upper() + "_CC", None)
    CXXFLAGS = environ("CAKE_" + variant.upper() + "_CXXFLAGS", None)
    LINKFLAGS = environ("CAKE_" + variant.upper() + "_LINKFLAGS", None)

def lazily_write(filename, newtext):
    oldtext = ""
    try:
        f = open(filename)
        oldtext = f.read()
        f.close()
    except:
        pass        
    if newtext != oldtext:
        f = open(filename, "w")
        f.write(newtext)
        f.close()

def objectname(source, entry):
    ccflags, cause, headers = entry
    h = md5.md5(" ".join([c for c in ccflags]) + " " + CXXFLAGS + " " + CC).hexdigest()
    return munge(source) + str(len(str(ccflags))) + "-" + h + ".o"



def generate_rules(source, output_name, generate_test, makefilename):
    """
    Generates a set of make rules for the given source.
    If generate_test is true, also generates a test run rule.
    """
    
    rules = {}
    sources = {}
    ignored = []
    linkflags = {}
    cause = []
        
    insert_dependencies(sources, ignored, source, linkflags, cause)
    
    # compile rule for each object
    for s in sources:
        obj = objectname(s, sources[s])
        ccflags, cause, headers = sources[s]
        
        definition = []
        definition.append(obj + " : " + " ".join(headers + [s])) 
        definition.append("\t" + CC + " -c " + " " + s + " " " -o " + obj + " " + " ".join(ccflags) + " " + CXXFLAGS)
        rules[obj] = "\n".join(definition)

    # link rule
    definition = []
    definition.append( output_name + " : " + " ".join([objectname(s, sources[s]) for s in  sources]) + " " + makefilename)
    definition.append("\t" + CC + " " + " " .join([objectname(s, sources[s]) for s in  sources]) + " " + LINKFLAGS + " " + " ".join([l for l in linkflags]) + " -o " + output_name )
    rules[output_name] = "\n".join(definition)
    
    if generate_test:
        definition = []
        test = output_name + ".passed"
        definition.append( test + " : " + output_name )
        definition.append( "\t" + "rm -f " + test + " && " + output_name + " && touch " + test)
        rules[test] = "\n".join(definition) 
        
    return rules


def render_makefile(makefilename, rules):
    """Renders a set of rules as a makefile"""
    
    rules_as_list = [rules[r] for r in rules]
    rules_as_list.sort()
    
    objects = [r for r in rules]
    objects.sort()
    
    # top-level build rule
    text = []
    text.append("all : " + " ".join(objects))
    text.append("")
    
    for rule in rules_as_list:
        text.append(rule)
        text.append("")        
    
    text = "\n".join(text)
    lazily_write(makefilename, text)


def cpus():
    f = open("/proc/cpuinfo")
    t = [x for x in f.readlines() if x.startswith("cpu cores")][0].split(":")[1]
    f.close()
    #status, output = commands.getstatusoutput("cat /proc/cpuinfo | grep cpu.cores | head -1 | cut -f2 -d\":\"")
    #return output.strip()
    return t.strip()


def do_generate(source_to_output, tests):
    """Generates all needed makefiles"""

    all_rules = {}
    for source in source_to_output:
        makefilename = munge(source) + ".Makefile"
        rules = generate_rules(source, source_to_output[source], source_to_output[source] in tests, makefilename)
        all_rules.update(rules)
        
        render_makefile(makefilename, rules)
        
    combined_filename = munge(source_to_output) + ".Makefile"
    render_makefile(combined_filename, all_rules)
    return combined_filename

    
def do_build(makefilename, quiet):
    result = os.system("make -r " + {True:"-s ",False:""}[quiet] + "-f " + makefilename + " -j" + cpus())
    if result != 0:
        sys.exit(result)

def do_run(output, args):
    os.execvp(output, [output] + args)




def main():
    global CC, CXXFLAGS, LINKFLAGS
        
    if len(sys.argv) < 2:
        usage()
        
    # parse arguments
    args = sys.argv[1:]
    cppfile = None
    appargs = []
    nextOutput = None
    
    generate = True
    build = True
    quiet = False
    to_build = {}    
    inTests = False
    tests = []
    
    for a in args:        
        if cppfile is None:            
            if a.startswith("--CC="):
                CC = a[a.index("=")+1:]
                continue
                            
            if a.startswith("--variant="):
                variant = a[a.index("=")+1:]      
                try_set_variant(variant)
                continue
                
            if a.startswith("--quiet"):
                quiet = True
                continue
                
            if a == "--generate":
                generate = True
                build = False
                continue
            
            if a == "--build":
                generate = True
                build = True
                continue                
            
            if a.startswith("--LINKFLAGS="):
                LINKFLAGS = a[a.index("=")+1:]
                continue
            
            if a.startswith("--CXXFLAGS="):
                CXXFLAGS = a[a.index("=")+1:]
                continue
            
            if a == "--begintests":                
                inTests = True
                continue
                
            if a == "--endtests":
                inTests = False
                continue
            
            if a == "--help":
                usage()
            
            if a.startswith("--"):
                usage("Invalid option " + a)
                
            if a.startswith("--output="):
                nextOutput = a[a.index("=")+1:]
                continue
                
            if nextOutput is None:
                nextOutput = os.path.splitext("bin/" + os.path.split(a)[1])[0]

            to_build[a] = nextOutput
            if inTests:
                tests.append(nextOutput)
            nextOutput = None
            
    
    if len(to_build) == 0:
        usage("You must specify a filename.")
    
    for c in to_build:
        if not os.path.exists(c):
            print >> sys.stderr, c + " is not found."
            sys.exit(1)
            
    if generate:
        makefilename = do_generate(to_build, tests)
    
    if build:
        do_build(makefilename, quiet)        
    return
    

try:
    
    # data
    CC = "g++"
    LINKFLAGS = ""
    CXXFLAGS = ""
    parse_etc()
    CC = environ("CAKE_CC", CC)
    LINKFLAGS = environ("CAKE_LINKFLAGS", LINKFLAGS)
    CXXFLAGS = environ("CAKE_CXXFLAGS", CXXFLAGS)

    try:
        os.mkdir("bin")
    except:
        pass

    
    main()
except SystemExit:
    raise
except IOError,e :
    print >> sys.stderr, str(e)
    sys.exit(1)
except UserException, e:
    print >> sys.stderr, str(e)
    sys.exit(1)
except KeyboardInterrupt:
    sys.exit(1)

