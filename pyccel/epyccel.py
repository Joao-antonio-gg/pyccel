# coding: utf-8

from importlib.machinery import ExtensionFileLoader
from collections         import OrderedDict
from types               import ModuleType, FunctionType

import inspect
import subprocess
import importlib
import sys
import os
import string
import random

from pyccel.parser                  import Parser
from pyccel.parser.errors           import Errors, PyccelError
from pyccel.parser.syntax.headers   import parse
from pyccel.codegen                 import Codegen
from pyccel.codegen.utilities       import execute_pyccel
from pyccel.codegen.utilities       import construct_flags as construct_flags_pyccel
from pyccel.ast                     import FunctionHeader
from pyccel.ast.utilities           import build_types_decorator
from pyccel.ast.core                import FunctionDef
from pyccel.ast.core                import FunctionCall
from pyccel.ast.f2py                import F2PY_Function, F2PY_Module
from pyccel.codegen.printing.pycode import pycode


#==============================================================================

PY_VERSION = sys.version_info[0:2]

#==============================================================================

def random_string( n ):
    # we remove uppercase letters because of f2py
    chars    = string.ascii_lowercase + string.digits
    selector = random.SystemRandom()
    return ''.join( selector.choice( chars ) for _ in range( n ) )

#==============================================================================

def mkdir_p(folder):
    if os.path.isdir(folder):
        return
    os.makedirs(folder)

#==============================================================================

def write_code(filename, code, folder=None):
    if not folder:
        folder = os.getcwd()

    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError('{} folder does not exist'.format(folder))

    filename = os.path.basename( filename )
    filename = os.path.join(folder, filename)

    # TODO check if init exists
    # add __init__.py for imports
    cmd = 'touch {}/__init__.py'.format(folder)
    os.system(cmd)

    f = open(filename, 'w')
    for line in code:
        f.write(line)
    f.close()

    return filename

#==============================================================================

def get_source_function(func):
    if not callable(func):
        raise TypeError('Expecting a callable function')

    lines = inspect.getsourcelines(func)
    lines = lines[0]
    # remove indentation if the first line is indented
    a = lines[0]
    leading_spaces = len(a) - len(a.lstrip())
    code = ''
    for a in lines:
        if leading_spaces > 0:
            line = a[leading_spaces:]
        else:
            line = a
        code = '{code}{line}'.format(code=code, line=line)

    return code

#==============================================================================

def construct_flags(compiler, extra_args = '', openmp = False):

    f90flags   = ''
    opt        = ''

    if openmp:
        if compiler == 'gfortran':
            extra_args += ' -lgomp '
            f90flags   += ' -fopenmp '

        elif compiler == 'ifort':
            extra_args += ' -liomp5 '
            f90flags   += ' -openmp -nostandard-realloc-lhs '
            opt         = """ --opt='-xhost -0fast' """

    return extra_args, f90flags, opt


#==============================================================================

# TODO must be moved to pyccel/ast/utilities.py
def get_function_from_ast(ast, func_name):
    node = None
    n_stmt = len(ast)
    i_stmt = 0
    while ( node is None ) and ( i_stmt < n_stmt ):
        stmt = ast[i_stmt]
        if isinstance(stmt, FunctionDef) and str(stmt.name) == func_name:
            node = stmt

        i_stmt += 1

    if node is None:
        print('> could not find {}'.format(func_name))

    return node

#==============================================================================

# TODO must be moved to pyccel/ast/utilities.py
def get_external_function_from_ast(ast):
    nodes   = []
    for stmt in ast:
        if isinstance(stmt, FunctionDef) and stmt.is_external:

            nodes += [stmt]

    if not nodes:
        print('> could not find {}'.format(func_name))

    return nodes

#==============================================================================

def compile_fortran(source, modulename, extra_args='',libs=[], compiler=None ,
                    mpi=False, openmp=False, includes = [], only = []):
    """use f2py to compile a source code. We ensure here that the f2py used is
    the right one with respect to the python/numpy version, which is not the
    case if we run directly the command line f2py ..."""

    args_pattern = """  -c {compilers} --f90flags='{f90flags}' {opt} {libs} -m {modulename} {filename} {extra_args} {includes} {only}"""

    compilers  = ''
    f90flags   = ''
    opt        = ''

    if compiler == 'gfortran':
        _compiler = 'gnu95'

    elif compiler == 'ifort':
        _compiler = 'intelem'

    else:
        raise NotImplementedError('Only gfortran and ifort are available for the moment')

    extra_args, f90flags, opt = construct_flags( compiler,
                                                 extra_args = extra_args,
                                                 openmp = openmp )

    if mpi:
        compilers = '--f90exec=mpif90 '


    if compiler:
        compilers = compilers + '--fcompiler={}'.format(_compiler)

    if only:
        only = 'only: ' + ','.join(str(i) for i in only)
    else:
        only = ''

    if not libs:
        libs = ''

    if not includes:
        includes = ''

    try:
        filename = '{}.f90'.format( modulename.replace('.','/') )
        filename = os.path.basename( filename )
        f = open(filename, "w")
        for line in source:
            f.write(line)
        f.close()
        libs = ' '.join('-l'+i.lower() for i in libs)
        args = args_pattern.format( compilers  = compilers,
                                    f90flags   = f90flags,
                                    opt        = opt,
                                    libs       = libs,
                                    modulename = modulename.rpartition('.')[2],
                                    filename   = filename,
                                    extra_args = extra_args,
                                    includes   = includes,
                                    only       = only )

        cmd = """python{}.{} -m numpy.f2py {}"""

        cmd = cmd.format(PY_VERSION[0], PY_VERSION[1], args)
        output = subprocess.check_output(cmd, shell=True)
        return output, cmd

    finally:
        f.close()

#==============================================================================

def epyccel(func, inputs = None, verbose = False, modules = [], libs = [], libdirs = [], name = None,
            compiler = None , mpi = False, static = None, only = None,
            openmp = False):
    """Pyccelize a python function and wrap it using f2py.

    func: function, str
        a Python function or source code defining the function

    inputs: str, list, tuple, dict
        inputs can be the function header as a string, or a list/tuple of
        strings or the globals() dictionary

    verbose: bool
        talk more

    modules: list, tuple
        list of dependencies

    libs: list, tuple
        list of libraries

    libdirs: list, tuple
        list of paths for libraries

    name: str
        name of the function, if it is given as a string

    static: list/tuple
        a list of 'static' functions as strings

    only: list/tuple
        a list of what should be exposed by f2py as strings

    openmp: bool
        True if openmp is used. Note that in this case, one need to use nogil
        from cython

    Examples

    The following example shows how to use Pyccel within an IPython session

    >>> #$ header procedure static f_static(int [:]) results(int)
    >>> def f_static(x):
    >>>     y = x[0] - 1
    >>>     return y

    >>> from test_epyccel import epyccel
    >>> f = epyccel(f_static, globals()) # appending IPython history

    >>> header = '#$ header procedure static f_static(int [:]) results(int)'
    >>> f = epyccel(f_static, header) # giving the header explicitly

    Now, **f** is a Fortran function that has been wrapped. It is compatible
    with numpy and you can call it

    >>> import numpy as np
    >>> x = np.array([3, 4, 5, 6], dtype=int)
    >>> y = f(x)

    You can also call it with a list instead of numpy arrays

    >>> f([3, 4, 5])
    2
    """
    if compiler is None:
        compiler = 'gfortran'

    is_module = False
    is_function = False

    if isinstance(func, ModuleType):
        is_module = True

    if callable(func):
        is_function = True

    assert(callable(func) or isinstance(func, str) or isinstance(func, ModuleType))

    # ...
    if callable(func) or isinstance(func, ModuleType):
        name = func.__name__

    elif name is None:
        # case of func as a string
        raise ValueError('function name must be provided, in the case of func string')
    # ...

    output_folder = name.rsplit('.',1)[0] if '.' in name else ''

    # fortran module name
    modname = 'epyccel__' + name



    # ...
    if is_module:
        mod = func
        is_sharedlib = isinstance(getattr(mod, '__loader__', None), ExtensionFileLoader)

        if is_sharedlib:
            module_filename = inspect.getfile(mod)

            # clean
            cmd = 'rm -f {}'.format(module_filename)
            os.system(cmd)

            # then re-run again
            mod = importlib.import_module(name)
            # we must reload the module, otherwise it is still the .so one
            importlib.reload(mod)
            epyccel(mod, inputs=inputs, verbose=verbose, modules=modules,
                    libs=libs, name=name, compiler=compiler,
                    mpi=mpi, static=static, only=only, openmp=openmp)
    # ...

    # ...
    ignored_funcs = None
    if not static:
        if isinstance(func, ModuleType):
            mod = func
            funcs = [i for i in dir(mod) if isinstance(getattr(mod, i), FunctionType)]

            # remove pyccel.decorators
            ignored_funcs = [i for i in funcs if getattr(mod, i).__module__ == 'pyccel.decorators']
            static = [i for i in funcs if not(i in ignored_funcs)]

        else:
            static = [name]
    # ...

    # ...
    headers = None
    if inputs:
        if isinstance(inputs, str):
            headers = inputs

        elif isinstance(inputs, (tuple, list)):
            # find all possible headers
            lines = [str(i) for i in inputs if (isinstance(i, str) and
                                                i.lstrip().startswith('#$ header'))]
            # TODO take the last occurence for f => use zip
            headers = "\n".join([str(i) for i in lines])

        elif isinstance(inputs, dict):
            # case of globals() history from ipython
            if not 'In' in inputs.keys():
                raise ValueError('Expecting `In` key in the inputs dictionary')

            inputs = inputs['In']

            # TODO shall we reverse the list

            # find all possible headers
            lines = [str(i) for i in inputs if i.lstrip().startswith('#$ header')]
            # TODO take the last occurence for f => use zip
            headers = "\n".join([str(i) for i in lines])

    # we parse all headers then convert them to static function
    d_headers = {}
    if headers:
        hdr = parse(stmts=headers)
        if isinstance(hdr, FunctionHeader):
            header = hdr.to_static()
            d_headers = {str(name): header}

        elif isinstance(hdr, (tuple, list)):
            hs = [h.to_static() for h in hdr]
            hs = [h for h in hs if hs.func == name]
            # TODO improve
            header = hs[0]
            raise NotImplementedError('TODO')

        else:
            raise NotImplementedError('TODO')
    # ...

    # ...
    if not static:
        raise NotImplementedError('TODO')
    # ...

    # ... get the function source code
    if callable(func):
        code = get_source_function(func)

    elif isinstance(func, ModuleType):
        lines = inspect.getsourcelines(func)[0]
        code = ''.join(lines)

    else:
        code = func
    # ...

    if verbose:
        print ('------')
        print (code)
        print ('------')

    extra_args = ''
    include_args = ''

    if libdirs:
        libdirs = ['-L{}'.format(i) for i in libdirs]
        extra_args += ' '.join(i for i in libdirs)

    try:
        # ...
        pyccel = Parser(code, headers=d_headers, static=static, output_folder = output_folder)
        ast = pyccel.parse()

        settings = {}
        ast = pyccel.annotate(**settings)

        codegen = Codegen(ast, modname)
        code = codegen.doprint()
        # ...

        # reset Errors singleton
        errors = Errors()
        errors.reset()

    except:
        # reset Errors singleton
        errors = Errors()
        errors.reset()

        raise PyccelError('Could not convert to Fortran')

       # Change module name to avoid name clashes: Python cannot import two modules with the same name
    if is_module:
        head, sep, tail = name.rpartition('.')
        name = sep.join( [head, '__epyccel__'+ tail] )
    else:
        name = '__epyccel__'+ name


    # Find directory where Fortran extension module should be created
    if is_module:
        dirname = os.path.dirname(os.path.abspath( mod.__file__ ))
    else:
        dirname = os.path.dirname(os.path.abspath(sys.modules[func.__module__].__file__ ))

    # Move into working directory, create extension module, then move back to original directory
    origin = os.path.abspath( os.curdir )
    os.chdir( dirname )
    output, cmd = compile_fortran( code, name,
        extra_args= extra_args,
        libs      = libs,
        compiler  = compiler,
        mpi       = mpi,
        openmp    = openmp,
        includes  = include_args,
        only      = only)
    os.chdir( origin )

    if verbose:
        print(cmd)

    if verbose:
        print(code)
    # ...

    # ...
    try:
        if PY_VERSION == (3, 7):
            dirname = os.path.relpath(dirname).replace('/','.')
            package = dirname + '.' + name
            package = importlib.import_module( '..'+name, package=package )
            clean_extension_module( package, modname )
        else:
            os.chdir( dirname )
            package = importlib.import_module( name )
            clean_extension_module( package, modname )
            os.chdir( origin )

    except:
        raise ImportError('could not import {0}'.format( name ))
    # ...

    if is_module:
        return package
    else:
        return getattr( package, func.__name__.lower() )

#==============================================================================

# TODO: write similar version for single functions
def epyccel_mpi( mod, comm, root=0 ):
    """
    Collective version of epyccel for modules: root process generates Fortran
    code, compiles it and creates a shared library (extension module), which
    is then loaded by all processes in the communicator.

    Parameters
    ----------
    mod : types.ModuleType
        Python module to be pyccelized.

    comm: mpi4py.MPI.Comm
        MPI communicator where extension module will be made available.

    root: int
        Rank of process responsible for code generation.

    Results
    -------
    fmod : types.ModuleType
        Python extension module.

    """
    from mpi4py import MPI

    assert isinstance(  mod, ModuleType )
    assert isinstance( comm, MPI.Comm   )
    assert isinstance( root, int        )

    # Master process calls epyccel
    if comm.rank == root:
        fmod      = epyccel( mod, mpi=True )
        fmod_path = fmod.__file__
        fmod_name = fmod.__name__
    else:
        fmod_path = None
        fmod_name = None

    # Broadcast Fortran module path/name to all processes
    fmod_path = comm.bcast( fmod_path, root=root )
    fmod_name = comm.bcast( fmod_name, root=root )

    # Non-master processes import Fortran module directly from its path
    if comm.rank != root:
        spec = importlib.util.spec_from_file_location( fmod_name, fmod_path )
        fmod = importlib.util.module_from_spec( spec )
        spec.loader.exec_module( fmod )
        clean_extension_module( fmod, mod.__name__ )

    # Return Fortran module
    return fmod

#==============================================================================

def clean_extension_module( ext_mod, py_mod_name ):
    """
    Clean Python extension module by moving functions contained in f2py's
    "mod_[py_mod_name]" automatic attribute to one level up (module level).
    "mod_[py_mod_name]" attribute is then completely removed from the module.

    Parameters
    ----------
    ext_mod : types.ModuleType
        Python extension module created by f2py from pyccel-generated Fortran.

    py_mod_name : str
        Name of the original (pure Python) module.

    """
    # Get name of f2py automatic attribute
    n = py_mod_name.lower().replace('.','_')

    # Move all functions to module level
    m = getattr( ext_mod, n )
    for a in type( m ).__dir__( m ):
        if a.startswith( '__' ) and a.endswith( '__' ):
            pass
        else:
            setattr( ext_mod, a, getattr( m, a ) )

    # Remove f2py automatic attribute
    delattr( ext_mod, n )


#==============================================================================

# assumes relative path
# TODO add openacc
def compile_f2py(filename,
                        extra_args='',
                        libs=[],
                        libdirs=[],
                        compiler=None ,
                        mpi=False,
                        openmp=False,
                        includes = [],
                        only = []):

    args_pattern = """  -c {compilers} --f90flags='{f90flags}' {opt} {libs} -m {modulename} {filename} {libdirs} {extra_args} {includes} {only}"""

    compilers  = ''
    f90flags   = ''
    opt        = ''

    if compiler == 'gfortran':
        _compiler = 'gnu95'

    elif compiler == 'ifort':
        _compiler = 'intelem'

    else:
        raise NotImplementedError('Only gfortran and ifort are available for the moment')

    if mpi:
        compilers = '--f90exec=mpif90 '

    if compiler:
        compilers = compilers + '--fcompiler={}'.format(_compiler)

    extra_args, f90flags, opt = construct_flags( compiler,
                                                 extra_args = extra_args,
                                                 openmp = openmp )

    if only:
        only = 'only: ' + ','.join(str(i) for i in only)
    else:
        only = ''

    if not libs:
        libs = ''

    if not libdirs:
        libdirs = ''

    if not includes:
        includes = ''

    modulename = filename.split('.')[0]

    libs = ' '.join('-l'+i.lower() for i in libs)
    libdirs = ' '.join('-L'+i.lower() for i in libdirs)

    args = args_pattern.format( compilers  = compilers,
                                f90flags   = f90flags,
                                opt        = opt,
                                libs       = libs,
                                libdirs    = libdirs,
                                modulename = modulename.rpartition('.')[2],
                                filename   = filename,
                                extra_args = extra_args,
                                includes   = includes,
                                only       = only )

    cmd = """python{}.{} -m numpy.f2py {}"""

    cmd = cmd.format(PY_VERSION[0], PY_VERSION[1], args)
    output = subprocess.check_output(cmd, shell=True)
    return output, cmd

#==============================================================================

def epyccel_function(func,
                     namespace  = globals(),
                     compiler   = None,
                     fflags     = None,
                     openmp     = False,
                     openacc    = False,
                     verbose    = False,
                     debug      = False,
                     include    = [],
                     libdir     = [],
                     modules    = [],
                     libs       = [],
                     extra_args = '',
                     folder     = None):

    # ... get the function source code
    if not isinstance(func, FunctionType):
        raise TypeError('> Expecting a function')

    code = get_source_function(func)
    # ...

    # ...
    tag = random_string( 6 )
    # ...

    # ...
    module_name = 'mod_{}'.format(tag)
    fname       = '{}.py'.format(module_name)
    binary      = '{}.o'.format(module_name)
    libname     = tag
    # ...

    # ...
    if folder is None:
        basedir = os.getcwd()
        folder = '__pycache__'
        folder = os.path.join( basedir, folder )

    folder = os.path.abspath( folder )
    mkdir_p(folder)
    # ...

    # ...
    write_code(fname, code, folder=folder)
    # ...

    # ...
    basedir = os.getcwd()
    os.chdir(folder)
    curdir = os.getcwd()
    # ...

    # ...
    accelerator = None
    if openmp:
        accelerator = 'openmp'

    if openacc:
        accelerator = 'openacc'
    # ...

    # ...
    if compiler is None:
        compiler = 'gfortran'
    # ...

    # ...
    if fflags is None:
        fflags = construct_flags_pyccel( compiler,
                                         fflags=None,
                                         debug=debug,
                                         accelerator=accelerator,
                                         include=[],
                                         libdir=[] )
    # ...

    # ... convert python to fortran using pyccel
    #     we ask for the ast so that we can get the FunctionDef node
    output, cmd, ast = execute_pyccel( fname,
                                       compiler    = compiler,
                                       fflags      = fflags,
                                       debug       = debug,
                                       verbose     = verbose,
                                       accelerator = accelerator,
                                       include     = include,
                                       libdir      = libdir,
                                       modules     = modules,
                                       libs        = libs,
                                       binary      = None,
                                       output      = '',
                                       return_ast  = True )
    # ...

    # ...
    cmd = 'ar -r lib{libname}.a {binary} '.format(binary=binary, libname=libname)
    os.system(cmd)

    if verbose:
        print(cmd)
    # ...

    # ... construct a f2py interface for the assembly
    # be careful: because of f2py we must use lower case
    func_name = func.__name__
    func = get_function_from_ast(ast, func_name)

    f2py_func = F2PY_Function(func, module_name)
    code = pycode(f2py_func)

    f2py_module_name = 'f2py_{}'.format(module_name)
    f2py_func_name   = f2py_func.name

    filename = '{}.py'.format(f2py_module_name)
    fname = write_code(filename, code, folder=folder)

    fname = execute_pyccel(fname, output='', convert_only=True)

    output, cmd = compile_f2py( fname,
                                extra_args = extra_args,
                                libs       = [libname],
                                libdirs    = [curdir],
                                compiler   = compiler,
                                mpi        = False,
                                openmp     = openmp )

    if verbose:
        print(cmd)
    # ...

    # ...
    # update module name for dependencies
    # needed for interface when importing assembly
    # name.name is needed for f2py
    name = f2py_module_name

    import_mod = 'from {name} import {module_name}'.format( name        = name,
                                                            module_name = f2py_module_name)
    assign_func = '{func} = {module}.{f2py_func}'.format( func      = func_name,
                                                          module    = f2py_module_name,
                                                          f2py_func = f2py_func_name )

    code = '{import_mod}\n{assign_func}'.format( import_mod = import_mod,
                                                 assign_func = assign_func )

    _module_name = '__epyccel__{}'.format(module_name)
    filename = '{}.py'.format(_module_name)
    fname = write_code(filename, code, folder=folder)

    sys.path.append(folder)
    package = importlib.import_module( _module_name )
    sys.path.remove(folder)

    func = getattr(package, func_name)

    os.chdir(basedir)

    if verbose:
        print('> epyccel interface has been stored in {}'.format(fname))
    # ...

    return func

#==============================================================================

def epyccel_module(module,
                   namespace  = globals(),
                   compiler   = None,
                   fflags     = None,
                   openmp     = False,
                   openacc    = False,
                   verbose    = False,
                   debug      = False,
                   include    = [],
                   libdir     = [],
                   modules    = [],
                   libs       = [],
                   extra_args = '',
                   folder     = None):

    # ... get the module source code
    if not isinstance(module, ModuleType):
        raise TypeError('> Expecting a module')

    lines = inspect.getsourcelines(module)[0]
    code = ''.join(lines)
    # ...

    # ...
    tag = random_string( 6 )
    # ...

    # ...
    module_name = module.__name__
    fname       = module.__file__
    binary      = '{}.o'.format(module_name)
    libname     = tag
    # ...

    # ...
    if folder is None:
        basedir = os.getcwd()
        folder = '__pycache__'
        folder = os.path.join( basedir, folder )

    folder = os.path.abspath( folder )
    mkdir_p(folder)
    # ...

    # ...
    basedir = os.getcwd()
    os.chdir(folder)
    curdir = os.getcwd()
    # ...

    # ... we need to store the python file in the folder, so that execute_pyccel
    #     can run
    cmd = 'cp {fname} {new}'.format(fname=fname, new=os.path.basename(fname))
    os.system(cmd)
    fname = os.path.basename(fname)
    # ...

    # ...
    accelerator = None
    if openmp:
        accelerator = 'openmp'

    if openacc:
        accelerator = 'openacc'
    # ...

    # ...
    if compiler is None:
        compiler = 'gfortran'
    # ...

    # ...
    if fflags is None:
        fflags = construct_flags_pyccel( compiler,
                                         fflags=None,
                                         debug=debug,
                                         accelerator=accelerator,
                                         include=[],
                                         libdir=[] )
    # ...

    # ... convert python to fortran using pyccel
    #     we ask for the ast so that we can get the FunctionDef node
    output, cmd, ast = execute_pyccel( fname,
                                       compiler    = compiler,
                                       fflags      = fflags,
                                       debug       = debug,
                                       verbose     = verbose,
                                       accelerator = accelerator,
                                       include     = include,
                                       libdir      = libdir,
                                       modules     = modules,
                                       libs        = libs,
                                       binary      = None,
                                       output      = '',
                                       return_ast  = True )
    # ...

    # ...
    cmd = 'ar -r lib{libname}.a {binary} '.format(binary=binary, libname=libname)
    os.system(cmd)

    if verbose:
        print(cmd)
    # ...

    # ... construct a f2py interface for the assembly
    # be careful: because of f2py we must use lower case
    funcs = get_external_function_from_ast(ast)

    f2py_module = F2PY_Module(funcs, module_name)
    code = pycode(f2py_module)

    f2py_module_name = f2py_module.name

    filename = '{}.py'.format(f2py_module_name)
    fname = write_code(filename, code, folder=folder)

    fname = execute_pyccel(fname, output='', convert_only=True)

    output, cmd = compile_f2py( fname,
                                extra_args = extra_args,
                                libs       = [libname],
                                libdirs    = [curdir],
                                compiler   = compiler,
                                mpi        = False,
                                openmp     = openmp )

    if verbose:
        print(cmd)
    # ...

    # ...
    # update module name for dependencies
    # needed for interface when importing assembly
    # name.name is needed for f2py
    name = f2py_module_name

    import_mod = 'from {name} import {module_name}'.format( name        = name,
                                                            module_name = f2py_module_name)

    assign_func = ''
    for expr in f2py_module.functions:
        func_name      = expr.func.name
        f2py_func_name = expr.name
        stmt = '{func} = {module}.{f2py_func}'.format( func      = func_name,
                                                       module    = f2py_module_name,
                                                       f2py_func = f2py_func_name )

        assign_func = '{assign_func}\n{stmt}'.format( assign_func = assign_func,
                                                      stmt        = stmt )

    code = '{import_mod}\n{assign_func}'.format( import_mod = import_mod,
                                                 assign_func = assign_func )

    _module_name = '__epyccel__{}'.format(module_name)
    filename = '{}.py'.format(_module_name)
    fname = write_code(filename, code, folder=folder)

    sys.path.append(folder)
    package = importlib.import_module( _module_name )
    sys.path.remove(folder)

    os.chdir(basedir)

    if verbose:
        print('> epyccel interface has been stored in {}'.format(fname))
    # ...

    return package
